# Skill: Meta-Agent Compiler

## Описание

Автономный синтез топологии отказоустойчивого графа (DAG/Swarm) из неструктурированной бизнес- или технической спецификации.

## Версия

v4.0 (Meta-Agent Compiler Standard)

## Входные данные

- `task_intent`: неструктурированное описание задачи
- `trace_context`: W3C Trace Context (trace_id, span_id, parent_span_id, trace_flags)
- `task_metadata`: task_id, attempt_number, max_attempts
- `payload.raw_task_description`: произвольное текстовое описание

## Выходные данные

- `inferred_topology`: список узлов графа
- `target_node_id`: идентификатор целевого узла
- `graph_role`: Worker | Orchestrator | Critic | IngressRouter | EgressGateway | DeadLetter
- `execution_model`: STATELESS_FN | STATEFUL_ORCHESTRATOR
- `compiled_system_prompt`: детерминированный системный промпт узла
- JSON Schema контракты (Draft 2020-12)

## Контракты

- Input: `agentbuilder://contracts/v4/meta_agent_compiler_worker_input.schema.json`
- Output: `agentbuilder://contracts/v4/meta_agent_compiler_worker_output.schema.json`

## Маршрутизация

- Upstream: `prompt_ingress_router`
- Downstream success: `ast_critic_validator`
- Downstream failure: `compiler_orchestrator`
- Dead letter: `compiler_dead_letter`

## Ограничения

- Zero Ambient Dialogue: вывод только в формате JSON
- State Store: NONE (идемпотентность через внешний кэш)
- Circuit breaker: проверка на входе, не мутирует состояние

## Примеры использования

См. `tests/test_mcp.py::test_meta_agent_compiler`
