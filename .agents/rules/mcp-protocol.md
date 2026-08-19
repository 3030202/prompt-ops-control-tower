# FastMCP & Prompt Register Rules

## 1. FastMCP Tools Protocol
- Все инструменты MCP (`list_prompts`, `get_prompt`, `semantic_search_prompts`, `export_prompts`) должны иметь аннотации типов `Pydantic` и подробные описания `Field(description="...")`.
- Формат стабильного идентификатора промпта: `P-\d{6}` (например, `P-000123`).

## 2. Безопасность и фильтрация данных
- Доступ к MCP через HTTP транспорт защищён Bearer токеном `MCP_API_KEY`.
- MCP-сервер работает строго в режиме **read-only** для публичного каталога.
- Запрещено модифицировать или удалять промпты через MCP.
- Ответы MCP не должны содержать:
  - Внутренние пути файловой системы (`PROMPT_OPS_SCAN_ROOTS`).
  - IP-адреса, токены, ключи API и токены Redis.
  - Метаданные приватных каналов Telethon.
