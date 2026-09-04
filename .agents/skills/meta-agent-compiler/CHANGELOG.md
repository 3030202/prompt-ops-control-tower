# Changelog: Meta-Agent Compiler Skill

## [4.0.0] - 2026-09-04

### Добавлено

- **SKILL.md**: документация навыка с описанием контрактов и маршрутизации
- **CONTRACTS/input.schema.json**: JSON Schema Draft 2020-12 для входных данных
- **CONTRACTS/output.schema.json**: JSON Schema Draft 2020-12 для выходных данных
- **PROMPTS/system.md**: детерминированный системный промпт узла
- **MCP_TOOL.py**: реализация MCP-инструмента для Antigravity IDE
- **TESTS/test_meta_agent_compiler.py**: pytest-тесты с фикстурами

### Особенности

- W3C Trace Context compliance (traceparent формат)
- Идемпотентность через кэш ключей `task_id:meta_agent_compiler_worker`
- Circuit breaker проверка на входе
- Авто-детекция топологии по ключевым словам (Critic/Orchestrator/Worker)
- Zero Ambient Dialogue: вывод только в формате JSON

### Известные ограничения

- Эвристика детекции топологии упрощённая (в продакшене заменить на LLM-компиляцию)
- Кэш идемпотентности in-memory (в продакшене использовать Redis/Supabase)
- Нет интеграции с `ast_critic_validator` для реальной валидации схем

### Roadmap

- [ ] Интеграция с `ast_critic_validator` для валидации JSON Schema
- [ ] Вынос кэша идемпотентности в Supabase/Redis
- [ ] LLM-компиляция топологии вместо эвристики
- [ ] CI/CD валидация `.schema.json` файлов через `jsonschema.Draft202012Validator.check_schema()`
- [ ] Dashboard-визуализация trace-цепочек

## Ссылки

- Ветка: `feature/meta-agent-compiler`
- Коммиты: от `fcc7b01` до `a0a500d`
- Тесты: `.agents/skills/meta-agent-compiler/TESTS/test_meta_agent_compiler.py`
