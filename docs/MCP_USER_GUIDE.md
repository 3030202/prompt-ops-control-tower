# MCP: инструкция пользователя

## Что это

Prompt Ops MCP даёт AI-клиенту read-only доступ к публичному каталогу промптов через Model Context Protocol. Endpoint использует Streamable HTTP и доступен по адресу:

```text
https://8.0x101.lol/mcp
```

Для подключения нужен отдельный Bearer-токен `MCP_API_KEY`. Токен не совпадает с паролем dashboard.

## Подключение

В интерфейсе MCP-клиента создайте remote/Streamable HTTP server:

- Name: `prompt-ops`
- URL: `https://8.0x101.lol/mcp`
- Header: `Authorization: Bearer <MCP_API_KEY>`

Если клиент использует JSON-конфигурацию, базовая форма выглядит так:

```json
{
  "mcpServers": {
    "prompt-ops": {
      "url": "https://8.0x101.lol/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_API_KEY}"
      }
    }
  }
}
```

Синтаксис подстановки переменных окружения отличается между клиентами. Если `${MCP_API_KEY}` не поддерживается, добавьте заголовок через защищённое поле настроек клиента, а не коммитьте токен в репозиторий.

## Доступные инструменты

| Tool | Назначение |
| --- | --- |
| `list_prompts` | Плотный поиск и фильтрация без загрузки полных тел |
| `get_prompt` | Полная публичная карточка по serial |
| `semantic_search_prompts` | Поиск по смыслу через Qdrant |
| `export_prompts` | Сборка выбранных serial в Markdown или JSON |

Ресурсы:

- `promptops://catalog/stats` — количество, фасеты и состояние индекса;
- `promptops://prompts/{serial}` — полный prompt по стабильному serial.

Prompt template `analyze_prompt_collection` задаёт последовательность поиска, чтения и сравнения выбранных промптов.

## Примеры запросов

```text
Найди промпты для генерации FAQ, оставь literacy выше среднего,
прочитай пять лучших и объясни различия.
```

```text
Через semantic search найди промпты для разбора пользовательских интервью.
Экспортируй три наиболее структурированных в Markdown.
```

```text
Прочитай P-000123 и оцени, какие переменные надо заполнить перед использованием.
Не меняй исходное тело промпта.
```

## Проверка соединения

Актуальная версия протокола позволяет выполнить самостоятельный `tools/list`:

```bash
curl -sS https://8.0x101.lol/mcp \
  -X POST \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "MCP-Method: tools/list" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Ожидаемый результат: JSON-RPC response со списком четырёх tools. Без токена endpoint отвечает `401`.

## Ограничения и безопасность

- MCP работает только на чтение и не публикует материалы в Telegram.
- Ответы строятся из public allowlist; raw source, пути, session ID и приватные метаданные не выдаются.
- Один экспорт ограничен 50 промптами, semantic search — 30 результатами.
- Токен хранится только в `.env` production-сервера.
- При утечке замените `MCP_API_KEY` и перезапустите `osint-radar`.
