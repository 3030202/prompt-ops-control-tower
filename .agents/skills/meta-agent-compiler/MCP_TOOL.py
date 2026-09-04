"""
MCP Tool: meta_agent_compiler

Интеграция узла meta_agent_compiler_worker как MCP-инструмента.
Вызывается из Antigravity IDE или других MCP-клиентов.
"""

import json
import hashlib
import secrets
from typing import Any, Dict, Optional
from jsonschema import Draft202012Validator, ValidationError


class MetaAgentCompilerTool:
    """
    MCP-инструмент для компиляции неструктурированных задач в графовую топологию.
    
    Реализует:
    - Валидацию входных данных по JSON Schema Draft 2020-12
    - Генерацию traceparent (W3C Trace Context)
    - Идемпотентность через кэш ключей
    - Circuit breaker проверку
    """
    
    def __init__(self, idempotency_cache: Optional[Dict[str, Any]] = None):
        self.idempotency_cache = idempotency_cache or {}
        self._load_schemas()
    
    def _load_schemas(self) -> None:
        """Загрузка JSON Schema контрактов."""
        # В продакшене загружать из файлов CONTRACTS/*.schema.json
        self.input_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "agentbuilder://contracts/v4/meta_agent_compiler_worker_input.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": ["trace_context", "task_metadata", "payload"],
            "properties": {
                "trace_context": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["trace_id", "span_id", "parent_span_id", "trace_flags"],
                    "properties": {
                        "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32},
                        "span_id": {"type": "string", "pattern": "^[0-9a-f]{16}$", "minLength": 16, "maxLength": 16},
                        "parent_span_id": {"type": ["string", "null"], "pattern": "^[0-9a-f]{16}$", "minLength": 16, "maxLength": 16},
                        "trace_flags": {"type": "string", "enum": ["00", "01"]},
                        "traceparent": {"type": "string", "pattern": "^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$"},
                        "correlation_id": {"type": "string", "minLength": 1}
                    }
                },
                "task_metadata": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "attempt_number", "max_attempts"],
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "attempt_number": {"type": "integer", "minimum": 1},
                        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 3}
                    }
                },
                "circuit": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "consecutive_failures": {"type": "integer", "minimum": 0},
                        "state": {"type": "string", "enum": ["CLOSED", "OPEN", "HALF_OPEN"]}
                    }
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["raw_task_description"],
                    "properties": {
                        "raw_task_description": {"type": "string", "minLength": 5},
                        "target_node_hint": {"type": ["string", "null"]},
                        "preferred_execution_model": {
                            "type": ["string", "null"],
                            "enum": [None, "STATELESS_FN", "STATEFUL_ORCHESTRATOR"]
                        }
                    }
                },
                "error_diff": {"type": ["object", "null"]}
            }
        }
        
        self.output_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "agentbuilder://contracts/v4/meta_agent_compiler_worker_output.schema.json",
            "type": "object",
            "additionalProperties": False,
            "required": ["trace_context", "execution_status", "routing", "result", "error"],
            "properties": {
                "trace_context": {"type": "object"},
                "execution_status": {"type": "string", "enum": ["SUCCESS", "RETRY_REQUIRED", "FATAL_FAILURE", "HANDOFF", "CIRCUIT_OPEN"]},
                "routing": {"type": "object"},
                "result": {"type": "object"},
                "error": {"type": "object"}
            }
        }
    
    def _generate_span_id(self) -> str:
        """Генерация нового 16-значного hex span_id."""
        return secrets.token_hex(8)
    
    def _generate_traceparent(self, trace_id: str, span_id: str, trace_flags: str = "01") -> str:
        """Формирование W3C traceparent header."""
        return f"00-{trace_id}-{span_id}-{trace_flags}"
    
    def _compute_idempotency_key(self, task_id: str) -> str:
        """Вычисление ключа идемпотентности."""
        return f"{task_id}:meta_agent_compiler_worker"
    
    def _validate_input(self, data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Валидация входных данных по JSON Schema."""
        try:
            validator = Draft202012Validator(self.input_schema)
            validator.validate(data)
            return True, None
        except ValidationError as e:
            return False, str(e.message)
    
    def _compile_topology(self, raw_description: str) -> Dict[str, Any]:
        """
        Синтез топологии графа из неструктурированного описания.
        
        В продакшене здесь будет LLM-компиляция с детерминированным промптом.
        Для демо возвращаем статическую топологию.
        """
        # Эвристика: определяем тип задачи по ключевым словам
        lower_desc = raw_description.lower()
        
        if any(kw in lower_desc for kw in ["валид", "проверк", "schema", "критик"]):
            topology = [
                "prompt_ingress_router",
                "meta_agent_compiler_worker",
                "ast_critic_validator",
                "egress_gateway"
            ]
            target_node = "ast_critic_validator"
            graph_role = "Critic"
        elif any(kw in lower_desc for kw in ["оркестр", "координац", "маршрутизаци"]):
            topology = [
                "prompt_ingress_router",
                "meta_agent_compiler_worker",
                "compiler_orchestrator",
                "egress_gateway"
            ]
            target_node = "compiler_orchestrator"
            graph_role = "Orchestrator"
        else:
            topology = [
                "prompt_ingress_router",
                "meta_agent_compiler_worker",
                "fallback_template_compiler",
                "egress_gateway"
            ]
            target_node = "fallback_template_compiler"
            graph_role = "Worker"
        
        return {
            "inferred_topology": topology,
            "target_node_id": target_node,
            "graph_role": graph_role,
            "execution_model": "STATELESS_FN"
        }
    
    def _compile_system_prompt(self, topology_info: Dict[str, Any]) -> str:
        """
        Компиляция системного промпта для целевого узла.
        
        В продакшене использовать детерминированный шаблонизатор.
        """
        return f"# СИСТЕМНЫЙ ПРОМПТ АГЕНТА: {topology_info['target_node_id'].upper()}\n\n..."
    
    async def __call__(
        self,
        trace_context: Dict[str, Any],
        task_metadata: Dict[str, Any],
        payload: Dict[str, Any],
        circuit: Optional[Dict[str, Any]] = None,
        error_diff: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Основной метод MCP-инструмента.
        
        Args:
            trace_context: W3C Trace Context (trace_id, span_id, parent_span_id, trace_flags)
            task_metadata: метаданные задачи (task_id, attempt_number, max_attempts)
            payload: полезная нагрузка (raw_task_description, target_node_hint, preferred_execution_model)
            circuit: опционально, circuit breaker состояние
            error_diff: опционально, информация об ошибке при retry
        
        Returns:
            Dict[str, Any]: результат по output schema
        """
        # Сборка входных данных
        input_data = {
            "trace_context": trace_context,
            "task_metadata": task_metadata,
            "payload": payload
        }
        if circuit:
            input_data["circuit"] = circuit
        if error_diff:
            input_data["error_diff"] = error_diff
        
        # Валидация входа
        is_valid, error_msg = self._validate_input(input_data)
        if not is_valid:
            return self._build_error_response(
                trace_context=trace_context,
                code="VALIDATION_ERR",
                message=error_msg,
                is_retryable=False
            )
        
        # Circuit breaker check
        if circuit and circuit.get("state") == "OPEN":
            return self._build_error_response(
                trace_context=trace_context,
                code="CIRCUIT_OPEN",
                message="Circuit breaker is OPEN",
                is_retryable=False,
                execution_status="CIRCUIT_OPEN"
            )
        
        # Ideмпотентность check
        idempotency_key = self._compute_idempotency_key(task_metadata["task_id"])
        if idempotency_key in self.idempotency_cache:
            return self.idempotency_cache[idempotency_key]
        
        # TRACE BINDING: генерация нового span_id
        new_span_id = self._generate_span_id()
        output_trace_context = {
            "trace_id": trace_context["trace_id"],
            "span_id": new_span_id,
            "parent_span_id": trace_context["span_id"],
            "trace_flags": trace_context["trace_flags"],
            "traceparent": self._generate_traceparent(
                trace_context["trace_id"],
                new_span_id,
                trace_context["trace_flags"]
            )
        }
        
        # ATOMIC EXECUTION: компиляция топологии
        topology_info = self._compile_topology(payload["raw_task_description"])
        compiled_prompt = self._compile_system_prompt(topology_info)
        
        # Формирование ответа
        result = {
            "trace_context": output_trace_context,
            "execution_status": "SUCCESS",
            "routing": {
                "next_node": "ast_critic_validator",
                "fallback_node": "fallback_template_compiler",
                "dead_letter_node": None,
                "handoff_reason": None
            },
            "result": {
                "task_intent": "Автономный синтез топологии и системного промпта целевого узла",
                "inferred_topology": topology_info["inferred_topology"],
                "target_node_id": topology_info["target_node_id"],
                "graph_role": topology_info["graph_role"],
                "execution_model": topology_info["execution_model"],
                "compiled_system_prompt": compiled_prompt
            },
            "error": {
                "code": None,
                "message": None,
                "is_retryable": False,
                "failed_node": None,
                "stack_trace": None
            }
        }
        
        # Кэширование для идемпотентности
        self.idempotency_cache[idempotency_key] = result
        
        return result
    
    def _build_error_response(
        self,
        trace_context: Dict[str, Any],
        code: str,
        message: str,
        is_retryable: bool,
        execution_status: str = "FATAL_FAILURE"
    ) -> Dict[str, Any]:
        """Формирование ответа об ошибке."""
        return {
            "trace_context": trace_context,
            "execution_status": execution_status,
            "routing": {
                "next_node": None,
                "fallback_node": "fallback_template_compiler",
                "dead_letter_node": "compiler_dead_letter",
                "handoff_reason": None
            },
            "result": {
                "task_intent": None,
                "inferred_topology": [],
                "target_node_id": None,
                "graph_role": None,
                "execution_model": None,
                "compiled_system_prompt": None
            },
            "error": {
                "code": code,
                "message": message,
                "is_retryable": is_retryable,
                "failed_node": "meta_agent_compiler_worker",
                "stack_trace": None
            }
        }


# Экземпляр инструмента для MCP-регистрации
meta_agent_compiler = MetaAgentCompilerTool()
