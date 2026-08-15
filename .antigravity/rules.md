# Role & Architecture Guidelines: Prompt Ops Control Tower

### 1. Архитектурные инварианты (Strict Isolation)
- **Public Allowlist Guard**: Ручки `/api/prompts` и `/mcp` возвращают ТОЛЬКО поля из allowlist (serial, title, body, mechanics, tags, token ranges). Блоки `references`, raw paths, `session_id`, source metadata и приватные источники ЗАПРЕЩЕНО отдавать наружу.
- **Prompt-Only Host Isolation**: Для хоста `PROMPT_ONLY_HOSTS` (`8.0x101.lol`) корень мапится на `/prompts`. Любые запросы к дашборду, Studio и админ-API должны возвращать 404. Анализ `/api/prompts/analyze` — строго под HTTP Basic Auth.
- **Telegram Safety**: При тестах и генерации кода использовать моки. Запрещено отправлять боевые запросы в Telegram/Telethon (`live_enabled=False`, `approval_mode=manual`).

### 2. Режим работы с кодом
- Перед коммитом / подтверждением: `python -m py_compile <changed_file>.py`
- Запуск тестов: `PYTHONPATH=. pytest -q tests/`
- Не затирать файлы целиком, использовать точечные дифы (diffs).
