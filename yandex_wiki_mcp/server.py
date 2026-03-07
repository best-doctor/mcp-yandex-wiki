from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse

import backoff
import httpx
from pydantic import Field, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from fastmcp import FastMCP

mcp = FastMCP(
    "yandex-wiki",
    instructions=(
        "Этот сервер предоставляет доступ к Яндекс Вики (wiki.yandex.ru). "
        "Когда пользователь присылает ссылку вида https://wiki.yandex.ru/..., "
        "используй wiki_page_get_text_by_url или wiki_page_get_by_url для получения содержимого страницы. "
        "НЕ используй WebFetch или WebSearch для wiki.yandex.ru — они не пройдут аутентификацию."
    ),
)

DEFAULT_FIELDS = "content,attributes,breadcrumbs,redirect"
HTTP_TRANSPORTS = {"http", "streamable-http", "sse"}
SERVER_READONLY = False
TOOLS_CACHE_PREFIX = "yandex_wiki_mcp:tools_cache:v2json"
_CACHE_INDEX_ADAPTER = TypeAdapter(list[str])


class _RuntimeEnv(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    wiki_token: str | None = Field(default=None, validation_alias="WIKI_TOKEN")
    tracker_token: str | None = Field(default=None, validation_alias="TRACKER_TOKEN")
    wiki_org_id: str | None = Field(default=None, validation_alias="WIKI_ORG_ID")
    tracker_org_id: str | None = Field(default=None, validation_alias="TRACKER_ORG_ID")
    wiki_api_base_url: str = Field(
        default="https://api.wiki.yandex.net/v1",
        validation_alias="WIKI_API_BASE_URL",
    )


class _ToolsCacheEnv(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    enabled: bool = Field(default=False, validation_alias="TOOLS_CACHE_ENABLED")
    redis_endpoint: str = Field(default="localhost", validation_alias="REDIS_ENDPOINT")
    redis_port: int = Field(default=6379, ge=1, validation_alias="REDIS_PORT")
    redis_db: int = Field(default=0, ge=0, validation_alias="REDIS_DB")
    redis_password: str | None = Field(default=None, validation_alias="REDIS_PASSWORD")
    redis_pool_max_size: int = Field(default=10, ge=1, validation_alias="REDIS_POOL_MAX_SIZE")
    redis_ttl: int = Field(default=3600, ge=0, validation_alias="TOOLS_CACHE_REDIS_TTL")


class _ReadonlyEnv(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    readonly: bool = Field(default=False, validation_alias="READONLY")


def _runtime_settings() -> tuple[str, str, str]:
    env = _RuntimeEnv()
    token = env.wiki_token or env.tracker_token
    org_id = env.wiki_org_id or env.tracker_org_id
    base_url = env.wiki_api_base_url
    return token or "", org_id or "", base_url


def _authorization_header(token: str) -> str:
    value = token.strip()
    lowered = value.lower()
    if lowered.startswith("oauth ") or lowered.startswith("bearer "):
        return value
    return f"OAuth {value}"


def _require_env() -> tuple[str, str, str]:
    token, org_id, base_url = _runtime_settings()
    missing = []
    if not token:
        missing.append("WIKI_TOKEN (или TRACKER_TOKEN)")
    if not org_id:
        missing.append("WIKI_ORG_ID (или TRACKER_ORG_ID)")
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))
    return token, org_id, base_url


def _normalize_slug(slug: str) -> str:
    normalized = (slug or "").strip()
    normalized = normalized.lstrip("/")
    normalized = normalized.rstrip("/")
    return normalized


def _slug_from_full_url(full_url: str) -> str:
    parsed_url = urlparse(full_url)
    return _normalize_slug(parsed_url.path or "")


def _normalize_fields(fields: str) -> str:
    raw_fields = (fields or "").strip()
    if not raw_fields:
        return DEFAULT_FIELDS
    if raw_fields.lower() in {"text", "body"}:
        return "content"

    parts = [part.strip() for part in raw_fields.split(",") if part.strip()]
    return ",".join(parts) if parts else DEFAULT_FIELDS


def _error_response(status_code: int, error: str) -> dict:
    return {"ok": False, "status_code": status_code, "error": error}


def _normalize_page_id(page_id: int) -> tuple[int | None, dict | None]:
    try:
        normalized = int(page_id)
    except (TypeError, ValueError):
        return None, _error_response(400, "Параметр page_id должен быть целым числом.")
    if normalized <= 0:
        return None, _error_response(400, "Параметр page_id должен быть положительным целым числом.")
    return normalized, None


def _assert_write_enabled(tool_name: str):
    if SERVER_READONLY:
        return _error_response(
            403,
            f"Инструмент '{tool_name}' отключен: сервер запущен в режиме readonly.",
        )
    return None


def _build_tools_cache() -> tuple[Any | None, int]:
    try:
        settings = _ToolsCacheEnv()
    except ValidationError as exc:
        raise RuntimeError("Некорректные значения переменных окружения кэша:") from exc

    if not settings.enabled:
        return None, 0

    try:
        from aiocache import Cache
        from aiocache.serializers import JsonSerializer
    except ImportError as exc:
        raise RuntimeError(
            "Для TOOLS_CACHE_ENABLED=true требуется зависимость aiocache[redis].",
        ) from exc

    class _RedisJsonSerializer(JsonSerializer):
        def dumps(self, value):
            if not isinstance(value, (dict, list)):
                return json.dumps(value)
            return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    cache = Cache(
        Cache.REDIS,
        endpoint=settings.redis_endpoint,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password,
        pool_max_size=settings.redis_pool_max_size,
        serializer=_RedisJsonSerializer(),
    )
    ttl = settings.redis_ttl
    return cache, ttl


TOOLS_CACHE, TOOLS_CACHE_TTL = _build_tools_cache()


def _cache_ttl_or_none() -> int | None:
    return TOOLS_CACHE_TTL if TOOLS_CACHE_TTL > 0 else None


def _cache_key_for_get(path: str, params: dict | None = None) -> str:
    serialized_params = json.dumps(
        params or {},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = f"{path}|{serialized_params}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{TOOLS_CACHE_PREFIX}:get:{digest}"


def _cache_slug_index_key(slug: str) -> str:
    return f"{TOOLS_CACHE_PREFIX}:index:slug:{_normalize_slug(slug)}"


def _cache_page_index_key(page_id: int) -> str:
    return f"{TOOLS_CACHE_PREFIX}:index:page:{page_id}"


def _cache_page_slug_mapping_key(page_id: int) -> str:
    return f"{TOOLS_CACHE_PREFIX}:mapping:page_slug:{page_id}"


def _is_error_result(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("ok") is False


def _with_cache_hit(payload: Any, *, cache_hit: bool) -> Any:
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    result["_mcp_cache_hit"] = cache_hit
    return result


def _extract_page_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    candidate = payload.get("id")
    if candidate is None:
        return None

    try:
        parsed = int(candidate)
    except (TypeError, ValueError):
        return None

    return parsed if parsed > 0 else None


def _extract_page_slug(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    raw_slug = payload.get("slug")
    if not isinstance(raw_slug, str):
        return None

    normalized = _normalize_slug(raw_slug)
    return normalized or None


def _validate_cache_index(raw_value: Any) -> list[str] | None:
    try:
        values = _CACHE_INDEX_ADAPTER.validate_python(raw_value)
    except ValidationError:
        return None

    if any(not isinstance(item, str) or not item for item in values):
        return None

    return values


def _validate_cached_payload(raw_value: Any) -> Any | None:
    try:
        json.dumps(
            raw_value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    return raw_value


def _validate_cached_slug(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = _normalize_slug(raw_value)
    return normalized or None


async def _cache_index_add(index_key: str, cache_key: str):
    if TOOLS_CACHE is None:
        return

    existing_keys = await TOOLS_CACHE.get(index_key)
    validated_keys = _validate_cache_index(existing_keys)
    if existing_keys is not None and validated_keys is None:
        await TOOLS_CACHE.delete(index_key)
        validated_keys = []
    if validated_keys is None:
        existing_keys = []
    else:
        existing_keys = validated_keys

    if cache_key not in existing_keys:
        existing_keys.append(cache_key)
        await TOOLS_CACHE.set(index_key, existing_keys, ttl=_cache_ttl_or_none())


async def _cache_invalidate_index(index_key: str):
    if TOOLS_CACHE is None:
        return

    existing_keys = await TOOLS_CACHE.get(index_key)
    validated_keys = _validate_cache_index(existing_keys)
    if existing_keys is not None and validated_keys is None:
        await TOOLS_CACHE.delete(index_key)
        return

    if validated_keys is not None:
        for cache_key in set(validated_keys):
            await TOOLS_CACHE.delete(cache_key)

    await TOOLS_CACHE.delete(index_key)


async def _cache_link_page(page_id: int, slug: str):
    if TOOLS_CACHE is None:
        return
    normalized_slug = _normalize_slug(slug)
    if not normalized_slug:
        return
    await TOOLS_CACHE.set(
        _cache_page_slug_mapping_key(page_id),
        normalized_slug,
        ttl=_cache_ttl_or_none(),
    )


async def _cache_register_page_entry(cache_key: str, request_slug: str | None, response_payload: Any):
    if TOOLS_CACHE is None or _is_error_result(response_payload):
        return

    normalized_request_slug = _normalize_slug(request_slug or "")
    response_slug = _extract_page_slug(response_payload)
    page_id = _extract_page_id(response_payload)

    slugs_to_index: set[str] = set()
    if normalized_request_slug:
        slugs_to_index.add(normalized_request_slug)
    if response_slug:
        slugs_to_index.add(response_slug)

    for slug in slugs_to_index:
        await _cache_index_add(_cache_slug_index_key(slug), cache_key)

    if page_id is not None:
        await _cache_index_add(_cache_page_index_key(page_id), cache_key)
        if response_slug:
            await _cache_link_page(page_id, response_slug)
        elif normalized_request_slug:
            await _cache_link_page(page_id, normalized_request_slug)


async def _invalidate_page_cache(page_id: int | None = None, slug: str | None = None):
    if TOOLS_CACHE is None:
        return

    normalized_slug = _normalize_slug(slug or "")

    if page_id is not None:
        await _cache_invalidate_index(_cache_page_index_key(page_id))
        mapping_key = _cache_page_slug_mapping_key(page_id)
        mapped_slug_raw = await TOOLS_CACHE.get(mapping_key)
        mapped_slug = _validate_cached_slug(mapped_slug_raw)
        if mapped_slug_raw is not None and mapped_slug is None:
            await TOOLS_CACHE.delete(mapping_key)
        await TOOLS_CACHE.delete(_cache_page_slug_mapping_key(page_id))

        if not normalized_slug and isinstance(mapped_slug, str):
            normalized_slug = _normalize_slug(mapped_slug)

    if normalized_slug:
        await _cache_invalidate_index(_cache_slug_index_key(normalized_slug))


async def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    try:
        token, org_id, base_url = _require_env()
    except RuntimeError as exc:
        return _error_response(400, str(exc))

    headers = {
        "Authorization": _authorization_header(token),
        "X-Org-Id": org_id,
    }
    request_url = f"{base_url}{path}"

    @backoff.on_exception(
        backoff.expo,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ),
        max_tries=4,
        max_time=30,
        giveup=lambda e: isinstance(e, httpx.HTTPStatusError) and e.response is not None and e.response.status_code < 500,
    )
    async def _send_request() -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method,
                url=request_url,
                headers=headers,
                params=params,
                json=body,
            )
            response.raise_for_status()
            return response

    try:
        response = await _send_request()
    except httpx.TimeoutException:
        return {
            "ok": False,
            "status_code": 504,
            "url": request_url,
            "error": "Таймаут при обращении к API Yandex Wiki.",
        }
    except httpx.HTTPStatusError as exc:
        response = exc.response
        if response is None:
            return {
                "ok": False,
                "status_code": 502,
                "url": request_url,
                "error": f"Ошибка HTTP при обращении к API Yandex Wiki: {exc}",
            }
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        return {
            "ok": False,
            "status_code": response.status_code,
            "url": str(response.request.url),
            "response": payload,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 502,
            "url": request_url,
            "error": f"Ошибка HTTP при обращении к API Yandex Wiki: {exc}",
        }

    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        return {
            "ok": False,
            "status_code": response.status_code,
            "url": str(response.request.url),
            "response": payload,
        }

    try:
        return response.json()
    except Exception:
        return {
            "ok": True,
            "status_code": response.status_code,
            "url": str(response.request.url),
            "response": response.text,
        }


async def _request_get(path: str, params: dict | None = None, *, cache_slug: str | None = None) -> Any:
    if TOOLS_CACHE is None:
        payload = await _request(method="GET", path=path, params=params)
        return _with_cache_hit(payload, cache_hit=False)

    cache_key = _cache_key_for_get(path=path, params=params)
    raw_cached_payload = await TOOLS_CACHE.get(cache_key)
    cached_payload = _validate_cached_payload(raw_cached_payload)
    if raw_cached_payload is not None and cached_payload is None:
        await TOOLS_CACHE.delete(cache_key)
    if cached_payload is not None:
        await _cache_register_page_entry(
            cache_key=cache_key,
            request_slug=cache_slug,
            response_payload=cached_payload,
        )
        return _with_cache_hit(cached_payload, cache_hit=True)

    payload = await _request(method="GET", path=path, params=params)
    if _is_error_result(payload):
        return _with_cache_hit(payload, cache_hit=False)

    validated_payload = _validate_cached_payload(payload)
    if validated_payload is None:
        return _error_response(500, "Получен некорректный формат ответа для кэширования.")

    await TOOLS_CACHE.set(cache_key, validated_payload, ttl=_cache_ttl_or_none())
    await _cache_register_page_entry(
        cache_key=cache_key,
        request_slug=cache_slug,
        response_payload=validated_payload,
    )
    return _with_cache_hit(validated_payload, cache_hit=False)


async def _get_page_by_slug(slug: str, fields: str, raise_on_redirect: bool = False):
    normalized_slug = _normalize_slug(slug)
    if not normalized_slug:
        return _error_response(400, "Параметр slug не должен быть пустым.")

    params = {
        "slug": normalized_slug,
        "fields": _normalize_fields(fields),
        "raise_on_redirect": str(bool(raise_on_redirect)).lower(),
    }
    return await _request_get(path="/pages", params=params, cache_slug=normalized_slug)


@mcp.tool()
async def wiki_page_get_by_url(url: str, fields: str = DEFAULT_FIELDS, raise_on_redirect: bool = False):
    """Read-only: получить страницу по полной ссылке вида https://wiki.yandex.ru/<path...>/"""
    slug = _slug_from_full_url(url)
    return await _get_page_by_slug(slug=slug, fields=fields, raise_on_redirect=raise_on_redirect)


@mcp.tool()
async def wiki_page_get(slug: str, fields: str = DEFAULT_FIELDS, raise_on_redirect: bool = False):
    """Read-only: получить страницу по slug (путь без домена)."""
    return await _get_page_by_slug(slug=slug, fields=fields, raise_on_redirect=raise_on_redirect)


@mcp.tool()
async def wiki_page_get_text_by_url(url: str):
    """Read-only: вернуть только content страницы по полной ссылке."""
    data = await wiki_page_get_by_url(url=url, fields="content")
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    result = {"ok": True, "content": data.get("content")}
    if isinstance(data, dict) and isinstance(data.get("_mcp_cache_hit"), bool):
        result["_mcp_cache_hit"] = data["_mcp_cache_hit"]
    return result


@mcp.tool()
async def wiki_page_create(
    slug: str,
    title: str,
    content: str,
    page_type: str = "wysiwyg",
    fields: str = DEFAULT_FIELDS,
    is_silent: bool = False,
):
    """Write: создать новую страницу."""
    readonly_error = _assert_write_enabled("wiki_page_create")
    if readonly_error:
        return readonly_error

    normalized_slug = _normalize_slug(slug)
    if not normalized_slug:
        return _error_response(400, "Параметр slug не должен быть пустым.")
    if not (title or "").strip():
        return _error_response(400, "Параметр title не должен быть пустым.")

    body = {
        "page_type": page_type,
        "slug": normalized_slug,
        "title": title.strip(),
        "content": content,
    }
    params = {
        "fields": _normalize_fields(fields),
        "is_silent": str(bool(is_silent)).lower(),
    }
    result = await _request(method="POST", path="/pages", params=params, body=body)
    if not _is_error_result(result):
        await _invalidate_page_cache(
            page_id=_extract_page_id(result),
            slug=_extract_page_slug(result) or normalized_slug,
        )
    return result


@mcp.tool()
async def wiki_page_update(
    page_id: int,
    title: str | None = None,
    content: str | None = None,
    allow_merge: bool = False,
    fields: str = DEFAULT_FIELDS,
    is_silent: bool = False,
):
    """Write: обновить существующую страницу по ID (заголовок и/или контент)."""
    readonly_error = _assert_write_enabled("wiki_page_update")
    if readonly_error:
        return readonly_error

    normalized_page_id, page_id_error = _normalize_page_id(page_id)
    if page_id_error:
        return page_id_error

    body = {}
    if title is not None:
        stripped_title = title.strip()
        if not stripped_title:
            return _error_response(400, "Если title передан, он не должен быть пустым.")
        body["title"] = stripped_title
    if content is not None:
        body["content"] = content
    if not body:
        return _error_response(400, "Нужно передать хотя бы одно поле для обновления: title или content.")

    params = {
        "allow_merge": str(bool(allow_merge)).lower(),
        "fields": _normalize_fields(fields),
        "is_silent": str(bool(is_silent)).lower(),
    }
    result = await _request(method="POST", path=f"/pages/{normalized_page_id}", params=params, body=body)
    if not _is_error_result(result):
        await _invalidate_page_cache(
            page_id=normalized_page_id,
            slug=_extract_page_slug(result),
        )
    return result


@mcp.tool()
async def wiki_page_append_content(
    page_id: int,
    content: str,
    location: str = "bottom",
    fields: str = DEFAULT_FIELDS,
    is_silent: bool = False,
):
    """Write: добавить контент в начало/конец страницы или по якорю (#anchor)."""
    readonly_error = _assert_write_enabled("wiki_page_append_content")
    if readonly_error:
        return readonly_error

    normalized_page_id, page_id_error = _normalize_page_id(page_id)
    if page_id_error:
        return page_id_error

    if not (content or "").strip():
        return _error_response(400, "Параметр content не должен быть пустым.")

    body = {"content": content}
    normalized_location = (location or "").strip()
    if normalized_location.lower() in {"top", "bottom", ""}:
        body["body"] = {"location": (normalized_location or "bottom").lower()}
    elif normalized_location.startswith("#"):
        body["anchor"] = {"name": normalized_location}
    else:
        return _error_response(
            400,
            "Параметр location должен быть top, bottom или якорем в формате #anchor.",
        )

    params = {
        "fields": _normalize_fields(fields),
        "is_silent": str(bool(is_silent)).lower(),
    }
    result = await _request(
        method="POST",
        path=f"/pages/{normalized_page_id}/append-content",
        params=params,
        body=body,
    )
    if not _is_error_result(result):
        await _invalidate_page_cache(
            page_id=normalized_page_id,
            slug=_extract_page_slug(result),
        )
    return result


def _build_parser(default_transport: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Yandex Wiki MCP server (read/write + readonly mode).",
    )
    parser.add_argument(
        "--transport",
        default=os.getenv("TRANSPORT", default_transport),
        help="MCP transport, например: stdio, http, streamable-http.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "127.0.0.1"),
        help="Host для HTTP транспорта.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8088")),
        help="Port для HTTP транспорта.",
    )
    parser.add_argument(
        "--path",
        default=os.getenv("MCP_PATH", "/mcp"),
        help="Path для HTTP транспорта.",
    )
    parser.add_argument(
        "--readonly",
        action="store_true",
        help="Отключает все write-инструменты (только чтение страниц).",
    )
    return parser


def _configure_readonly(cli_readonly: bool, forced_readonly: bool = False) -> bool:
    return forced_readonly or cli_readonly or _ReadonlyEnv().readonly


def _run_mcp(transport: str, host: str, port: int, path: str):
    run_kwargs = {"transport": transport}
    if transport in HTTP_TRANSPORTS:
        run_kwargs.update({"host": host, "port": port, "path": path})
    mcp.run(**run_kwargs)


def main(
    argv: list[str] | None = None,
    *,
    forced_readonly: bool = False,
    default_transport: str = "stdio",
):
    global SERVER_READONLY

    parser = _build_parser(default_transport=default_transport)
    args = parser.parse_args(argv)
    SERVER_READONLY = _configure_readonly(
        cli_readonly=bool(args.readonly),
        forced_readonly=forced_readonly,
    )
    _run_mcp(
        transport=str(args.transport),
        host=str(args.host),
        port=int(args.port),
        path=str(args.path),
    )


def main_readonly(argv: list[str] | None = None):
    main(argv=argv, forced_readonly=True, default_transport="stdio")
