---
name: prompt-ops-mcp
description: >-
  Процедуры работы с Model Context Protocol (MCP) Prompt Register: семантический поиск,
  выгрузка коллекций промптов и интеграция с внешними LLM/IDE.
---

# FastMCP Prompt Register Guide

## 1. Инструменты MCP
- `list_prompts(query, tags, prompt_types, min_complexity, max_complexity, sort, offset, limit)`:
  Получение отфильтрованного списка промптов.
- `get_prompt(serial)`:
  Получение полной спецификации промпта по номеру `P-000123`.
- `semantic_search_prompts(query, tags, prompt_types, limit)`:
  Поиск промптов по смыслу через векторную базу Qdrant.
- `export_prompts(serials, format, register_name)`:
  Экспорт подборки промптов в формате Markdown или JSON.

## 2. Проверка и тестирование MCP
- Запуск тестов: `PYTHONPATH=. pytest tests/test_mcp.py`
- HTTP эндпоинт доступен по адресу `http://localhost:8000/mcp`
- Заголовок авторизации: `Authorization: Bearer <MCP_API_KEY>`
