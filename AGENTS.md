# Prompt Ops Control Tower — Workspace Guidelines & Rules

## 1. Архитектурные инварианты (Strict Isolation)
- **Public Allowlist Guard**: Эндпоинты `/api/prompts` и `/mcp` возвращают ТОЛЬКО поля из allowlist (`serial`, `title`, `body`/`prompt`, `mechanics`, `tags`, `type`, `complexity`, `rating`, `token_ranges`). Поля `references`, `raw_paths`, `session_id`, source metadata и приватные данные ЗАПРЕЩЕНО отдавать наружу.
- **Prompt-Only Host Isolation**: Для хостов `PROMPT_ONLY_HOSTS` (`8.0x101.lol`, `08.0x101.lol`) корень мапится на `/prompts`. Запросы к панели управления, Studio и админ-API должны быть защищены (HTTP Basic Auth) или возвращать 404. Анализ `/api/prompts/analyze` — строго под HTTP Basic Auth.
- **Telegram Safety**: При тестах и генерации кода использовать моки. Запрещено отправлять боевые запросы в Telegram/Telethon (`live_enabled=False`, `approval_mode=manual`).

## 2. Разработка и стандарты кода
- **Python & FastAPI**:
  - Строгая асинхронность (`async`/`await`), правильное использование `lifespan` для фоновых задач.
  - Все сетевые вызовы — через `httpx.AsyncClient` с таймаутами.
  - Корректная обработка `asyncio.CancelledError` при остановке приложения.
- **Интерфейсы (Web UI & Telegram Mini Apps)**:
  - Использовать Vanilla CSS и CSS переменные (без TailwindCSS, если нет явного запроса).
  - Эстетика: Dark Mode, Cyberpunk / Glassmorphism, неоновые акценты (`#00ff88`, `#00d2ff`), шрифт `JetBrains Mono` / `Outfit`.
  - Мобильная адаптивность с поддержкой safe-area insets и Telegram WebApp SDK (`Telegram.WebApp.ready()`, `expand()`, `HapticFeedback`).
- **Редактирование файлов**:
  - Всегда делать точечные правки (diffs), никогда не затирать файлы целиком.
  - Сохранять существующие комментарии и логику.
  - Добавлять и поддерживать тесты в каталоге `tests/`.
