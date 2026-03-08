# Yandex Wiki MCP

[![PyPI version](https://badge.fury.io/py/mcp-yandex-wiki.svg)](https://pypi.org/project/mcp-yandex-wiki/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Реализация MCP-сервера для Яндекс Вики с режимами read/write и readonly.

## Содержимое
- `mcp-yandex-wiki` — полный режим (чтение + создание/обновление/append)
- `mcp-yandex-wiki-ro` — read-only режим (только чтение)

## Установка

1. Установить `uv` [(если ещё не установлен)](https://docs.astral.sh/uv/getting-started/installation/).
2. Получить OAuth-токен Яндекс и `org_id`:
   1. Создать приложение на [oauth.yandex.ru](https://oauth.yandex.ru/) с правами Wiki.
   2. Подставить `client_id` в URL:
      ```
      https://oauth.yandex.ru/authorize?response_type=token&client_id=<CLIENT_ID>
      ```
      и авторизоваться.

## Переменные окружения

Обязательные:
- `WIKI_TOKEN` или `TRACKER_TOKEN`
- `WIKI_ORG_ID` или `TRACKER_ORG_ID`

Опциональные:
- `WIKI_API_BASE_URL` (по умолчанию `https://api.wiki.yandex.net/v1`)
- `TRANSPORT` (`stdio` по умолчанию)
- `HOST` (`127.0.0.1`)
- `PORT` (`8088`)
- `MCP_PATH` (`/mcp`)
- `TOOLS_CACHE_ENABLED` (`true/false`, по умолчанию `false`)
- `TOOLS_CACHE_REDIS_TTL` (в секундах, по умолчанию `3600`)
- `REDIS_ENDPOINT` (`localhost`)
- `REDIS_PORT` (`6379`)
- `REDIS_DB` (`0`)
- `REDIS_PASSWORD`
- `REDIS_POOL_MAX_SIZE` (`10`)
- `READONLY` (`true/false`)

### Кэширование (Redis)

Кэшируются только read-операции для Wiki:
- `wiki_page_get`
- `wiki_page_get_by_url`
- `wiki_page_get_text_by_url`

Особенности:
- включается через `TOOLS_CACHE_ENABLED=true`
- кэш живёт в Redis (`REDIS_*`)
- при любых write-операциях (`create`, `update`, `append_content`) кэш инвалидируется для затронутых страниц/slug
- в ответах добавляется флаг `_mcp_cache_hit` (`true/false`)

Минимальный пример для локального Redis:

```bash
docker run -p 6379:6379 --name redis-cache -d redis:alpine

TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
  TOOLS_CACHE_ENABLED=true REDIS_ENDPOINT=127.0.0.1 REDIS_PORT=6379 uvx mcp-yandex-wiki
```

Production-подобный пример:

```bash
TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
TOOLS_CACHE_ENABLED=true \
  REDIS_ENDPOINT=redis.internal \
  REDIS_PORT=6379 \
  REDIS_DB=0 \
  REDIS_PASSWORD=secret \
  TOOLS_CACHE_REDIS_TTL=7200 \
  uvx mcp-yandex-wiki
```

## Быстрый запуск (через PyPI)

```bash
TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
  uvx mcp-yandex-wiki

TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
  uvx --from mcp-yandex-wiki mcp-yandex-wiki-ro
```

Альтернатива (после установки):

```bash
pip install mcp-yandex-wiki
python -m yandex_wiki_mcp
```

## Подключение в MCP-агентах (через PyPI)

### Claude Code

```bash
claude mcp add yandex-wiki uvx mcp-yandex-wiki \
  -e WIKI_TOKEN=your_token \
  -e WIKI_ORG_ID=your_org_id

claude mcp add yandex-wiki-ro -- uvx --from mcp-yandex-wiki mcp-yandex-wiki-ro \
  -e WIKI_TOKEN=your_token \
  -e WIKI_ORG_ID=your_org_id
```

Если используете `TRACKER_*`-переменные, замените их на:

```bash
claude mcp add yandex-wiki uvx mcp-yandex-wiki \
  -e TRACKER_TOKEN=your_token \
  -e TRACKER_ORG_ID=your_org_id
```

### Codex (конфиг проекта)

```toml
[mcp_servers.yandex-wiki]
command = "uvx"
args = ["mcp-yandex-wiki"]
env = { WIKI_TOKEN = "your_token", WIKI_ORG_ID = "your_org_id" }

[mcp_servers.yandex-wiki-ro]
command = "uvx"
args = ["--from", "mcp-yandex-wiki", "mcp-yandex-wiki-ro"]
env = { WIKI_TOKEN = "your_token", WIKI_ORG_ID = "your_org_id" }
```

### Cursor

1. Открыть **Settings** → **Cursor Settings** → **MCP** → **+ Add new global MCP server**.
   Откроется файл `~/.cursor/mcp.json`.
2. Добавить конфигурацию:

```json
{
  "mcpServers": {
    "yandex-wiki": {
      "command": "uvx",
      "args": ["mcp-yandex-wiki"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    }
  }
}
```

Для read-only режима:

```json
{
  "mcpServers": {
    "yandex-wiki-ro": {
      "command": "uvx",
      "args": ["--from", "mcp-yandex-wiki", "mcp-yandex-wiki-ro"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    }
  }
}
```

> Можно также добавить на уровне проекта — создайте файл `.cursor/mcp.json` в корне репозитория с аналогичным содержимым.

3. Вернуться в **Settings** → **MCP** и убедиться, что у сервера зелёный индикатор (статус «running»).

### Другие MCP-клиенты (JSON, общий шаблон)

```json
{
  "mcpServers": {
    "yandex-wiki": {
      "command": "uvx",
      "args": ["mcp-yandex-wiki"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    },
    "yandex-wiki-ro": {
      "command": "uvx",
      "args": ["--from", "mcp-yandex-wiki", "mcp-yandex-wiki-ro"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    }
  }
}
```

## Инструменты

### `mcp-yandex-wiki` (rw)
- `wiki_page_get`
- `wiki_page_get_by_url`
- `wiki_page_get_text_by_url`
- `wiki_page_create`
- `wiki_page_update`
- `wiki_page_append_content`

### `mcp-yandex-wiki-ro`
- `wiki_page_get`
- `wiki_page_get_by_url`
- `wiki_page_get_text_by_url`
- write-инструменты возвращают `403`

## Отладка (MCP Inspector)

Для интерактивной отладки MCP-сервера можно использовать [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

1. Запустить сервер в режиме SSE:

```bash
uv run fastmcp run yandex_wiki_mcp/server.py --transport sse
```

2. В другом терминале запустить Inspector:

```bash
npx @modelcontextprotocol/inspector@latest
```

3. В открывшемся интерфейсе Inspector выбрать **Transport Type: SSE** и указать URL:

```
http://localhost:8000/sse
```

4. Нажать **Connect** — Inspector подключится к серверу и покажет список доступных инструментов, позволяя вызывать их вручную и видеть ответы.

## Настройки FastMCP для production

Сервер поддерживает переменные окружения FastMCP для тонкой настройки поведения:

- `FASTMCP_MASK_ERROR_DETAILS` — при `true` маскирует детали ошибок в ответах клиентам. Показываются только сообщения из явно выброшенных `ToolError`. Рекомендуется для production.
- `FASTMCP_STRICT_INPUT_VALIDATION` — при `true` включает строгую валидацию входных данных инструментов по JSON-схемам. При `false` (по умолчанию) допускаются совместимые преобразования типов (например, строка `"10"` → число `10`).
