from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP

mcp = FastMCP("yandex-wiki")

DEFAULT_FIELDS = "content,attributes,breadcrumbs,redirect"
HTTP_TRANSPORTS = {"http", "streamable-http", "sse"}
SERVER_READONLY = False
TOOLS_CACHE_PREFIX = "yandex-wiki-mcp:tools_cache"


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int_env(var_name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(var_name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        parsed_value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {var_name} должна быть целым числом.") from exc

    if parsed_value < minimum:
        raise RuntimeError(
            f"Переменная {var_name} должна быть >= {minimum}, получено: {parsed_value}.",
        )

    return parsed_value


def _runtime_settings() -> tuple[str, str, str]:
    token = os.getenv("WIKI_TOKEN") or os.getenv("TRACKER_TOKEN")
    org_id = os.getenv("WIKI_ORG_ID") or os.getenv("TRACKER_ORG_ID")
    base_url = os.getenv("WIKI_API_BASE_URL", "https://api.wiki.yandex.net/v1")
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


def _tools_cache_enabled() -> bool:
    return _parse_bool(os.getenv("TOOLS_CACHE_ENABLED"), default=False)


def _build_tools_cache() -> tuple[Any | None, int]:
    if not _tools_cache_enabled():
        return None, 0

    try:
        from aiocache import Cache
        from aiocache.serializers import PickleSerializer
    except ImportError as exc:
        raise RuntimeError(
            "Для TOOLS_CACHE_ENABLED=true требуется зависимость aiocache[redis].",
        ) from exc

    cache = Cache(
        Cache.REDIS,
        endpoint=os.getenv("REDIS_ENDPOINT", "localhost"),
        port=_parse_int_env("REDIS_PORT", 6379, minimum=1),
        db=_parse_int_env("REDIS_DB", 0, minimum=0),
        password=os.getenv("REDIS_PASSWORD"),
        pool_max_size=_parse_int_env("REDIS_POOL_MAX_SIZE", 10, minimum=1),
        serializer=PickleSerializer(),
    )
    ttl = _parse_int_env("TOOLS_CACHE_REDIS_TTL", 3600, minimum=0)
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


async def _cache_index_add(index_key: str, cache_key: str):
    if TOOLS_CACHE is None:
        return

    existing_keys = await TOOLS_CACHE.get(index_key)
    if not isinstance(existing_keys, list):
        existing_keys = []

    if cache_key not in existing_keys:
        existing_keys.append(cache_key)
        await TOOLS_CACHE.set(index_key, existing_keys, ttl=_cache_ttl_or_none())


async def _cache_invalidate_index(index_key: str):
    if TOOLS_CACHE is None:
        return

    existing_keys = await TOOLS_CACHE.get(index_key)
    if isinstance(existing_keys, list):
        for cache_key in set(existing_keys):
            await TOOLS_CACHE.delete(cache_key)

    await TOOLS_CACHE.delete(index_key)


async def _cache_link_page(page_id: int, slug: str):
    if TOOLS_CACHE is None:
        return
    await TOOLS_CACHE.set(
        _cache_page_slug_mapping_key(page_id),
        _normalize_slug(slug),
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
        mapped_slug = await TOOLS_CACHE.get(_cache_page_slug_mapping_key(page_id))
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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method,
                url=request_url,
                headers=headers,
                params=params,
                json=body,
            )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "status_code": 504,
            "url": request_url,
            "error": "Таймаут при обращении к API Yandex Wiki.",
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
    cached_payload = await TOOLS_CACHE.get(cache_key)
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

    await TOOLS_CACHE.set(cache_key, payload, ttl=_cache_ttl_or_none())
    await _cache_register_page_entry(
        cache_key=cache_key,
        request_slug=cache_slug,
        response_payload=payload,
    )
    return _with_cache_hit(payload, cache_hit=False)


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
    return forced_readonly or cli_readonly or _parse_bool(os.getenv("READONLY"), default=False)


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
