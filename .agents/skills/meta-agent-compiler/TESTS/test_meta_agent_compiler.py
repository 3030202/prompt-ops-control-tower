"""
Тесты для MCP-инструмента meta_agent_compiler.

Запуск:
    python -m pytest .agents/skills/meta-agent-compiler/TESTS/test_meta_agent_compiler.py -v
"""

import pytest
import asyncio
import sys
from pathlib import Path

# Добавляем путь к MCP_TOOL.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from MCP_TOOL import MetaAgentCompilerTool


@pytest.fixture
def tool():
    """Фикстура: экземпляр инструмента с пустым кэшем."""
    return MetaAgentCompilerTool(idempotency_cache={})


@pytest.fixture
def valid_input():
    """Фикстура: валидные входные данные."""
    return {
        "trace_context": {
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "span_id": "5fb397be3474a44f",
            "parent_span_id": None,
            "trace_flags": "01",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-5fb397be3474a44f-01"
        },
        "task_metadata": {
            "task_id": "test-task-001",
            "attempt_number": 1,
            "max_attempts": 3
        },
        "payload": {
            "raw_task_description": "Создать валидатор JSON Schema для проверки выходных данных компилятора",
            "target_node_hint": None,
            "preferred_execution_model": None
        },
        "circuit": {
            "consecutive_failures": 0,
            "state": "CLOSED"
        },
        "error_diff": None
    }


class TestMetaAgentCompilerTool:
    """Тесты основного функционала."""
    
    @pytest.mark.asyncio
    async def test_valid_input_returns_success(self, tool, valid_input):
        """Тест: валидный вход возвращает SUCCESS."""
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["execution_status"] == "SUCCESS"
        assert result["error"]["code"] is None
        assert result["routing"]["next_node"] == "ast_critic_validator"
        assert result["result"]["inferred_topology"] is not None
        assert len(result["result"]["inferred_topology"]) > 0
    
    @pytest.mark.asyncio
    async def test_traceparent_generation(self, tool, valid_input):
        """Тест: генерация нового traceparent."""
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        output_trace = result["trace_context"]
        assert output_trace["trace_id"] == valid_input["trace_context"]["trace_id"]
        assert output_trace["span_id"] != valid_input["trace_context"]["span_id"]
        assert output_trace["parent_span_id"] == valid_input["trace_context"]["span_id"]
        assert output_trace["traceparent"].startswith("00-")
    
    @pytest.mark.asyncio
    async def test_idempotency_cache(self, tool, valid_input):
        """Тест: идемпотентность через кэш."""
        # Первый вызов
        result1 = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        # Второй вызов с тем же task_id
        result2 = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        # Результат должен быть идентичен (кэширован)
        assert result1["trace_context"]["span_id"] == result2["trace_context"]["span_id"]
        assert result1["result"]["inferred_topology"] == result2["result"]["inferred_topology"]
    
    @pytest.mark.asyncio
    async def test_circuit_open_returns_error(self, tool, valid_input):
        """Тест: circuit OPEN возвращает ошибку."""
        valid_input["circuit"]["state"] = "OPEN"
        
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["execution_status"] == "CIRCUIT_OPEN"
        assert result["error"]["code"] == "CIRCUIT_OPEN"
        assert result["error"]["is_retryable"] is False
    
    @pytest.mark.asyncio
    async def test_topology_detection_critic(self, tool, valid_input):
        """Тест: детекция топологии для валидатора."""
        valid_input["payload"]["raw_task_description"] = "Нужен валидатор JSON Schema для проверки выходных данных"
        
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["result"]["target_node_id"] == "ast_critic_validator"
        assert result["result"]["graph_role"] == "Critic"
        assert "ast_critic_validator" in result["result"]["inferred_topology"]
    
    @pytest.mark.asyncio
    async def test_topology_detection_orchestrator(self, tool, valid_input):
        """Тест: детекция топологии для оркестратора."""
        valid_input["payload"]["raw_task_description"] = "Оркестрация многоагентной системы с маршрутизацией"
        
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["result"]["target_node_id"] == "compiler_orchestrator"
        assert result["result"]["graph_role"] == "Orchestrator"
        assert "compiler_orchestrator" in result["result"]["inferred_topology"]
    
    @pytest.mark.asyncio
    async def test_invalid_trace_id_format(self, tool, valid_input):
        """Тест: невалидный trace_id (не 32 hex)."""
        valid_input["trace_context"]["trace_id"] = "invalid-trace-id"
        
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["execution_status"] == "FATAL_FAILURE"
        assert result["error"]["code"] == "VALIDATION_ERR"
        assert result["error"]["is_retryable"] is False
    
    @pytest.mark.asyncio
    async def test_missing_required_field(self, tool, valid_input):
        """Тест: отсутствие обязательного поля."""
        del valid_input["payload"]["raw_task_description"]
        
        result = await tool(
            trace_context=valid_input["trace_context"],
            task_metadata=valid_input["task_metadata"],
            payload=valid_input["payload"],
            circuit=valid_input["circuit"],
            error_diff=valid_input["error_diff"]
        )
        
        assert result["execution_status"] == "FATAL_FAILURE"
        assert result["error"]["code"] == "VALIDATION_ERR"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
