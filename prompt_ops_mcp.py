"""
Prompt Ops MCP Server

MCP-сервер для управления Prompt Ops: компиляция мета-агентов, публикация в Telegram,
валидация JSON Schema, трассировка W3C Trace Context.
"""

import os
import json
import asyncio
from typing import Any, Dict, Optional
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Импортируем существующие модули
from prompt_ops_app import PromptOpsApp
from publishing_studio import PublishingStudio

# Импортируем новый инструмент meta_agent_compiler
from .agents.skills.meta_agent_compiler.MCP_TOOL import MetaAgentCompilerTool


# Инициализация MCP-сервера
mcp = Server("prompt-ops")

# Инициализация приложений
prompt_ops_app = PromptOpsApp()
publishing_studio = PublishingStudio()
meta_agent_compiler = MetaAgentCompilerTool(
    idempotency_cache={}  # В продакшене заменить на Redis/Supabase
)


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    """Список доступных MCP-инструментов."""
    return [
        Tool(
            name="meta_agent_compiler",
            description="Компиляция неструктурированной задачи в графовую топологию с JSON Schema контрактами",
            inputSchema={
                "type": "object",
                "properties": {
                    "trace_context": {
                        "type": "object",
                        "properties": {
                            "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                            "span_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
                            "parent_span_id": {"type": ["string", "null"]},
                            "trace_flags": {"type": "string", "enum": ["00", "01"]}
                        },
                        "required": ["trace_id", "span_id", "parent_span_id", "trace_flags"]
                    },
                    "task_metadata": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "attempt_number": {"type": "integer", "minimum": 1},
                            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 3}
                        },
                        "required": ["task_id", "attempt_number", "max_attempts"]
                    },
                    "payload": {
                        "type": "object",
                        "properties": {
                            "raw_task_description": {"type": "string", "minLength": 5},
                            "target_node_hint": {"type": ["string", "null"]},
                            "preferred_execution_model": {
                                "type": ["string", "null"],
                                "enum": [None, "STATELESS_FN", "STATEFUL_ORCHESTRATOR"]
                            }
                        },
                        "required": ["raw_task_description"]
                    },
                    "circuit": {
                        "type": "object",
                        "properties": {
                            "consecutive_failures": {"type": "integer", "minimum": 0},
                            "state": {"type": "string", "enum": ["CLOSED", "OPEN", "HALF_OPEN"]}
                        }
                    },
                    "error_diff": {"type": ["object", "null"]}
                },
                "required": ["trace_context", "task_metadata", "payload"]
            }
        ),
        Tool(
            name="publish_telegram_post",
            description="Публикация поста в Telegram через Publishing Studio",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "channel_id": {"type": "string"}
                },
                "required": ["draft_id", "channel_id"]
            }
        ),
        Tool(
            name="validate_json_schema",
            description="Валидация JSON Schema Draft 2020-12",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "object"},
                    "data": {"type": "object"}
                },
                "required": ["schema", "data"]
            }
        )
    ]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Вызов MCP-инструмента."""
    try:
        if name == "meta_agent_compiler":
            result = await meta_agent_compiler(
                trace_context=arguments.get("trace_context"),
                task_metadata=arguments.get("task_metadata"),
                payload=arguments.get("payload"),
                circuit=arguments.get("circuit"),
                error_diff=arguments.get("error_diff")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "publish_telegram_post":
            draft_id = arguments.get("draft_id")
            channel_id = arguments.get("channel_id")
            result = await publishing_studio.publish(draft_id, channel_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "validate_json_schema":
            from jsonschema import Draft202012Validator, ValidationError
            schema = arguments.get("schema")
            data = arguments.get("data")
            
            try:
                validator = Draft202012Validator(schema)
                validator.validate(data)
                return [TextContent(type="text", text=json.dumps({"valid": True}, indent=2))]
            except ValidationError as e:
                return [TextContent(type="text", text=json.dumps({"valid": False, "error": str(e.message)}, indent=2))]
        
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Запуск MCP-сервера."""
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
