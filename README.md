# Yandex Wiki MCP

Реализация MCP-сервера для Яндекс Вики с режимами read/write и readonly.

## Содержимое
- `yandex-wiki-mcp` — полный режим (чтение + создание/обновление/append)
- `yandex-wiki-mcp-ro` — read-only режим (только чтение)

## Установка

1. Установить `uv` [(если ещё не установлен)](https://docs.astral.sh/uv/getting-started/installation/).
2. Получить OAuth-токен Яндекс и `org_id`.

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
  TOOLS_CACHE_ENABLED=true REDIS_ENDPOINT=127.0.0.1 REDIS_PORT=6379 uvx yandex_wiki_mcp
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
  uvx yandex_wiki_mcp
```

## Быстрый запуск (через PyPI)

```bash
TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
  uvx yandex_wiki_mcp

TRACKER_TOKEN=your_token TRACKER_ORG_ID=your_org_id \
  uvx yandex_wiki_mcp_ro
```

Альтернатива (после установки):

```bash
pip install yandex_wiki_mcp
python -m yandex_wiki_mcp
```

## Подключение в MCP-агентах (через PyPI)

### Claude Code

```bash
claude mcp add yandex-wiki uvx yandex_wiki_mcp \
  -e WIKI_TOKEN=your_token \
  -e WIKI_ORG_ID=your_org_id

claude mcp add yandex-wiki-ro uvx yandex_wiki_mcp_ro \
  -e WIKI_TOKEN=your_token \
  -e WIKI_ORG_ID=your_org_id
```

`uvx` нормализует имена исполняемых файлов, поэтому для запуска read-only обычно используется `yandex_wiki_mcp_ro` (с `_`), даже если в документации встречается `yandex-wiki-mcp-ro`.
```

Если используете `TRACKER_*`-переменные, замените их на:

```bash
claude mcp add yandex-wiki uvx yandex_wiki_mcp \
  -e TRACKER_TOKEN=your_token \
  -e TRACKER_ORG_ID=your_org_id
```

### Codex (конфиг проекта)

```toml
[mcp_servers.yandex-wiki]
command = "uvx"
args = ["yandex_wiki_mcp"]
env = { WIKI_TOKEN = "your_token", WIKI_ORG_ID = "your_org_id" }

[mcp_servers.yandex-wiki-ro]
command = "uvx"
args = ["yandex_wiki_mcp_ro"]
env = { WIKI_TOKEN = "your_token", WIKI_ORG_ID = "your_org_id" }
```

### Другие MCP-клиенты (JSON, общий шаблон)

```json
{
  "mcpServers": {
    "yandex-wiki": {
      "command": "uvx",
      "args": ["yandex_wiki_mcp"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    },
    "yandex-wiki-ro": {
      "command": "uvx",
      "args": ["yandex_wiki_mcp_ro"],
      "env": {
        "WIKI_TOKEN": "your_token",
        "WIKI_ORG_ID": "your_org_id"
      }
    }
  }
}
```

## Инструменты

### `yandex-wiki-mcp` (rw)
- `wiki_page_get`
- `wiki_page_get_by_url`
- `wiki_page_get_text_by_url`
- `wiki_page_create`
- `wiki_page_update`
- `wiki_page_append_content`

### `yandex-wiki-mcp-ro`
- `wiki_page_get`
- `wiki_page_get_by_url`
- `wiki_page_get_text_by_url`
- write-инструменты возвращают `403`
