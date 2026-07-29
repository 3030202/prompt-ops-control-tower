# Prompt Ops Control Tower

Операционный радар для свежих prompts, skills, agents, rules, MCP и workflow-артефактов с semantic search, AI-телеметрией и Telegram Publishing Studio.

## Возможности

- адаптивный сбор RSS, GitHub Atom, web pages, X Recent Search, Telegram и локального workspace;
- группировка источников по типам артефактов с сохраняемыми свёртками и отдельной видимостью Sources/Errors;
- хранение артефактов в Redis и Qdrant;
- комбинированные фильтры и semantic search;
- OpenAI-compatible AI provider с загрузкой списка моделей;
- учёт токенов, стоимости, сессии и месячного бюджета;
- режимы Telegram-поста: пошаговый разбор, прожарка, мои мысли, новый артефакт и full editorial;
- drafts, preview, ручная публикация и scheduled autopost;
- строгий live-контур с AI fail-closed, rate limits и дедупликацией;
- style profiles по последним 50 сообщениям Telegram-канала;
- публичная read-only Lite-витрина только для явно опубликованных материалов;
- отдельный Prompt Register: только нормализованные промпты, оценки сложности/грамотности, теги, пометки и замечания.

## Быстрый старт

```bash
cp .env.example .env
# заполните реальные ключи и смените DASHBOARD_PASS
docker compose up -d --build
```

Откройте:

- Dashboard: `http://localhost:8000/`
- Publishing Studio: `http://localhost:8000/studio`
- Public Lite: `http://localhost:8000/lite`
- Prompt Register: `http://localhost:8000/prompts`
- Qdrant: `http://localhost:6333/dashboard`

Dashboard и Studio используют HTTP Basic из `DASHBOARD_USER` / `DASHBOARD_PASS`.

## Prompt-only поверхность

`8.0x101.lol` предназначен только для каталога промптов. На этом hostname корень переписывается в `/prompts`, доступны лишь `/api/prompts` и `/health`, а dashboard, Studio, Lite и административные API возвращают `404`.

Публичная модель содержит только: серийный номер, название, тело промпта, краткое описание, теги, сложность, инженерную грамотность, специальные пометки, замечания и тип промпта. Raw-материалы, пути, source metadata, session ID и приватные workspace/Telegram-источники не экспортируются. Список prompt-only host задаётся через `PROMPT_ONLY_HOSTS`.

## Источники и TUI

Каталог включает отключённые по умолчанию пресеты PromptCentral, PromptPort, PromptPortal, Prompta, image/video prompt collections, NotebookLM workflows, user-refined system prompts и prompt distillates. Включайте нужные потоки из панели Sources; их рекомендуемый cadence уже настроен.

X-источники используют официальный Recent Search API. Добавьте `X_BEARER_TOKEN` в `.env`, затем явно включите нужные X-пресеты. Без токена они остаются выключенными и не расходуют трафик.

Основные клавиши dashboard:

- `` ` `` / `ё` — открыть интерактивное ASCII-облако тегов;
- `s` — скрыть/показать Sources;
- `e` — скрыть/показать Errors;
- `g-` / `g+` в панели — свернуть/раскрыть все группы;
- `Shift+S` — AI summary;
- `Shift+E` — export;
- `j` / `k`, `Space`, `/`, `Enter`, `Ctrl+K` — навигация, выбор, поиск, inspect и command palette.

В облаке тегов стрелки или `hjkl` перемещают курсор, `Space` собирает несколько тегов, а `Enter` применяет их как комбинированный `AND`-фильтр. Состояние панелей и каждой группы сохраняется в `localStorage` браузера.

## Telegram

1. Создайте бота через BotFather и укажите `TELEGRAM_BOT_TOKEN`.
2. Добавьте бота администратором выходного канала с правом публикации.
3. Для чтения истории и построения style profile настройте `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` и Telethon session.
4. Добавьте destination в `/studio`, нажмите `Test`.
5. Создайте правило выключенным, проверьте условия, затем отдельно включите rule и при необходимости `live_enabled`.

Live отправляет короткий flash. Длинные материалы по умолчанию создаются в review; `approval_mode=auto` включается явно.

## Публикация через Cloudflare Tunnel

1. Создайте named tunnel и направьте public hostname на `http://osint-radar:8000`.
2. Сохраните tunnel token в `secrets/cloudflare_tunnel_token` без завершающих пробелов.
3. Назначьте файл UID официального image: `sudo chown 65532:65532 secrets/cloudflare_tunnel_token && sudo chmod 600 secrets/cloudflare_tunnel_token`.
4. Запустите production-профиль: `docker compose --profile tunnel up -d --build`.

Папка `secrets/` исключена из Git и Docker build context. Основной dashboard остаётся под HTTP Basic, а `/lite` доступен публично.

## Безопасность

`.env`, sessions, logs и persistent data исключены из Git и Docker context. Публичный API возвращает только allowlisted поля опубликованных материалов. Перед публичным деплоем используйте HTTPS/reverse proxy и замените пароль dashboard.

## Проверка

```bash
python -m py_compile prompt_ops_app.py publishing_studio.py
PYTHONPATH=. pytest -q tests/test_publishing.py tests/test_sources.py
docker compose config
```

Архитектура описана в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Проект распространяется по лицензии MIT.
