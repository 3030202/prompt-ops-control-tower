# Prompt Ops Control Tower

Операционный радар для свежих prompts, skills, agents, rules, MCP и workflow-артефактов с semantic search, AI-телеметрией и Telegram Publishing Studio.

## Возможности

- адаптивный сбор RSS, GitHub Atom, web pages, Telegram и локального workspace;
- хранение артефактов в Redis и Qdrant;
- комбинированные фильтры и semantic search;
- OpenAI-compatible AI provider с загрузкой списка моделей;
- учёт токенов, стоимости, сессии и месячного бюджета;
- режимы Telegram-поста: пошаговый разбор, прожарка, мои мысли, новый артефакт и full editorial;
- drafts, preview, ручная публикация и scheduled autopost;
- строгий live-контур с AI fail-closed, rate limits и дедупликацией;
- style profiles по последним 50 сообщениям Telegram-канала;
- публичная read-only Lite-витрина только для явно опубликованных материалов.

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
- Qdrant: `http://localhost:6333/dashboard`

Dashboard и Studio используют HTTP Basic из `DASHBOARD_USER` / `DASHBOARD_PASS`.

## Telegram

1. Создайте бота через BotFather и укажите `TELEGRAM_BOT_TOKEN`.
2. Добавьте бота администратором выходного канала с правом публикации.
3. Для чтения истории и построения style profile настройте `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` и Telethon session.
4. Добавьте destination в `/studio`, нажмите `Test`.
5. Создайте правило выключенным, проверьте условия, затем отдельно включите rule и при необходимости `live_enabled`.

Live отправляет короткий flash. Длинные материалы по умолчанию создаются в review; `approval_mode=auto` включается явно.

## Безопасность

`.env`, sessions, logs и persistent data исключены из Git и Docker context. Публичный API возвращает только allowlisted поля опубликованных материалов. Перед публичным деплоем используйте HTTPS/reverse proxy и замените пароль dashboard.

## Проверка

```bash
python -m py_compile prompt_ops_app.py publishing_studio.py
python -m unittest discover -s tests -v
docker compose config
```

Архитектура описана в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Проект распространяется по лицензии MIT.
