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
- отдельный Dense Prompt Register: полноэкранный web-TUI, независимые цветные регистры, практический разбор механики, оценки токенов и экспорт MD/JSON.
- remote MCP Streamable HTTP для безопасного поиска, чтения, semantic search и экспорта публичных промптов.

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
- MCP endpoint: `http://localhost:8000/mcp`
- Qdrant: `http://localhost:6333/dashboard`

Dashboard и Studio используют HTTP Basic из `DASHBOARD_USER` / `DASHBOARD_PASS`.

## Prompt-only поверхность

`8.0x101.lol` предназначен только для каталога промптов. На этом hostname корень переписывается в `/prompts`; публичны `/health`, чтение `/api/prompts`, detail `/api/prompts/{serial}` и экспорт `/api/prompts/export`. AI-анализ `/api/prompts/analyze` требует HTTP Basic, а dashboard, Studio, Lite и остальные административные API возвращают `404`.

## MCP

Remote MCP доступен по `https://8.0x101.lol/mcp` и использует современный Streamable HTTP transport. Запросы требуют отдельный заголовок `Authorization: Bearer <MCP_API_KEY>`.

MCP предоставляет read-only tools:

- `list_prompts` — компактный поиск, AND-фильтр тегов и пагинация;
- `get_prompt` — полная allowlisted-карточка по serial;
- `semantic_search_prompts` — поиск по смыслу через Qdrant;
- `export_prompts` — экспорт выбранных serial в MD/JSON.

Перед запуском задайте длинный случайный `MCP_API_KEY`; при пустом ключе endpoint fail-closed возвращает `503`. Подключение клиента и примеры запросов описаны в [docs/MCP_USER_GUIDE.md](docs/MCP_USER_GUIDE.md), первичный production rollout и ротация токена — в [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Публичная модель содержит только allowlisted-поля: серийный номер, название, тело промпта, человеческое описание операции, объяснение механики, структуру, покрытие, ожидаемый результат, сложность освоения, приблизительные диапазоны входных/выходных токенов, 3–5 английских тегов, оценки, пометки и замечания. Блоков `references` нет. Raw-материалы, пути, source metadata, session ID и приватные workspace/Telegram-источники не экспортируются. Список prompt-only host задаётся через `PROMPT_ONLY_HOSTS`.

## Источники и TUI

Каталог включает отключённые по умолчанию пресеты PromptCentral, PromptPort, PromptPortal, Prompta, image/video prompt collections, NotebookLM workflows, user-refined system prompts и prompt distillates. Включайте нужные потоки из панели Sources; их рекомендуемый cadence уже настроен.

X-источники используют официальный Recent Search API. Добавьте `X_BEARER_TOKEN` в `.env`, затем явно включите нужные X-пресеты. Без токена они остаются выключенными и не расходуют трафик.

Prompt Register использует три панели: дерево фасетов, виртуализированную таблицу и лениво загружаемый preview. Фильтры внутри одной группы объединяются через `OR`, а между группами — через `AND`. Если исходных тегов нет или недостаточно, дешёвая детерминированная нормализация создаёт 3–5 тегов в `lowercase-kebab-case`; первым ставится узкий предметный тег вроде `language-detection` или `faq-generation`.

Основные клавиши Prompt Register:

- `1` / `2` / `3`, `h` / `l` — переключение панелей;
- `j` / `k`, `g` / `G` — навигация по плотной таблице;
- `r`, `1–5`, `Space`, `v`, `u` — выбор регистра, маркировка и диапазоны;
- `b` / `p` — скрыть дерево или preview;
- `/`, `:`, `Ctrl+K`, `?` — поиск, command palette и помощь;
- `e` / `E` — экспорт активного или всех регистров;
- `a` — защищённый AI-анализ активного регистра.

Пять регистров `KEEP`, `REVIEW`, `DROP`, `RESEARCH`, `REMIX` независимы: один промпт может находиться в нескольких. Метки и переименования хранятся только в versioned `localStorage` и никогда не отправляются на сервер, кроме выбранных serial при явном экспорте или анализе.

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
python -m py_compile prompt_ops_app.py prompt_ops_mcp.py publishing_studio.py
PYTHONPATH=. pytest -q tests/test_publishing.py tests/test_sources.py tests/test_mcp.py
docker compose config
```

Архитектура описана в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Проект распространяется по лицензии MIT.
