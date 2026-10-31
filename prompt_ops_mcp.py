"""Prompt Ops MCP Server.

MCP-сервер Prompt Register: список промптов, получение по серийному номеру,
семантический поиск, экспорт подборок и компиляция мета-агентов
(``meta_agent_compiler`` из ``.agents/skills/meta-agent-compiler``).

Модуль не импортирует ``prompt_ops_app`` на уровне модуля — это разрывает
циклическую зависимость между приложением и MCP-слоем. Бэкенд инструментов
инжектируется через :func:`configure_mcp` при старте lifespan-приложения.

HTTP-эндпоинт доступен по адресу ``/mcp`` (Streamable HTTP) и защищён
Bearer-токеном ``MCP_API_KEY`` (см. ``docs/DEPLOYMENT.md`` и
``.agents/rules/mcp-protocol.md``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount

logger = logging.getLogger(__name__)

# Идентификаторы инструментов meta_agent_compiler из контракта v4.
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(slots=True)
class PromptOpsMCPBackend:
    """Асинхронные функции-адаптеры к каталогу промптов основного приложения."""

    list_prompts: Callable[..., Awaitable[dict[str, Any]]]
    get_prompt: Callable[[str], Awaitable[dict[str, Any]]]
    semantic_search: Callable[..., Awaitable[dict[str, Any]]]
    export_prompts: Callable[..., Awaitable[dict[str, Any]]]
    catalog_stats: Callable[[], Awaitable[dict[str, Any]]]


_backend: Optional[PromptOpsMCPBackend] = None
_meta_agent_tool = None


def configure_mcp(backend: PromptOpsMCPBackend) -> None:
    """Инжектирует бэкенд данных для MCP-инструментов (вызывается из приложения)."""
    global _backend
    _backend = backend


def _require_backend() -> PromptOpsMCPBackend:
    if _backend is None:
        raise RuntimeError("MCP backend is not configured; call configure_mcp() first")
    return _backend


def _load_meta_agent_tool():
    """Лениво загружает MetaAgentCompilerTool из skills-дерева репозитория.

    Возвращает ``None``, если зависимости скилла недоступны — тогда инструмент
    просто не появляется в ``tools/list`` (graceful degradation вместо падения
    всего MCP-сервера).
    """
    global _meta_agent_tool
    if _meta_agent_tool is not None:
        return _meta_agent_tool
    try:
        import importlib.util
        from pathlib import Path

        tool_path = (
            Path(__file__).resolve().parent
            / ".agents"
            / "skills"
            / "meta-agent-compiler"
            / "MCP_TOOL.py"
        )
        if not tool_path.exists():
            return None
        spec = importlib.util.spec_from_file_location("meta_agent_compiler_mcp_tool", tool_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _meta_agent_tool = module.MetaAgentCompilerTool(idempotency_cache={})
    except Exception as exc:  # pragma: no cover - зависит от окружения
        logger.warning("meta_agent_compiler skill unavailable: %s", exc, exc_info=True)
        _meta_agent_tool = None
    return _meta_agent_tool


META_AGENT_COMPILER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trace_context": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "span_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
                "parent_span_id": {"type": ["string", "null"]},
                "trace_flags": {"type": "string", "enum": ["00", "01"]},
            },
            "required": ["trace_id", "span_id", "parent_span_id", "trace_flags"],
        },
        "task_metadata": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "attempt_number": {"type": "integer", "minimum": 1},
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["task_id", "attempt_number", "max_attempts"],
        },
        "payload": {
            "type": "object",
            "properties": {
                "raw_task_description": {"type": "string", "minLength": 5},
                "target_node_hint": {"type": ["string", "null"]},
                "preferred_execution_model": {
                    "type": ["string", "null"],
                    "enum": [None, "STATELESS_FN", "STATEFUL_ORCHESTRATOR"],
                },
            },
            "required": ["raw_task_description"],
        },
        "circuit": {
            "type": "object",
            "properties": {
                "consecutive_failures": {"type": "integer", "minimum": 0},
                "state": {"type": "string", "enum": ["CLOSED", "OPEN", "HALF_OPEN"]},
            },
        },
        "error_diff": {"type": ["object", "null"]},
    },
    "required": ["trace_context", "task_metadata", "payload"],
}


def _tool_definitions() -> list[Tool]:
    tools = [
        Tool(
            name="list_prompts",
            description=(
                "Получение отфильтрованного списка промптов публичного каталога "
                "(read-only). Фильтры: текстовый запрос, теги, типы, диапазон сложности."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                    "prompt_types": {"type": "array", "items": {"type": "string"}, "default": []},
                    "min_complexity": {"type": "integer", "minimum": 0, "default": 0},
                    "max_complexity": {"type": "integer", "minimum": 0, "default": 100},
                    "sort": {
                        "type": "string",
                        "enum": ["updated_desc", "complexity_desc", "complexity_asc", "literacy_desc"],
                        "default": "updated_desc",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        ),
        Tool(
            name="get_prompt",
            description="Получение полной спецификации промпта по серийному номеру P-XXXXXX.",
            inputSchema={
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "pattern": "^P-\\d{6}$"},
                },
                "required": ["serial"],
            },
        ),
        Tool(
            name="semantic_search_prompts",
            description="Поиск промптов по смыслу через векторную базу Qdrant.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                    "prompt_types": {"type": "array", "items": {"type": "string"}, "default": []},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="export_prompts",
            description="Экспорт подборки промптов в формате Markdown или JSON.",
            inputSchema={
                "type": "object",
                "properties": {
                    "serials": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^P-\\d{6}$"},
                        "minItems": 1,
                        "maxItems": 50,
                    },
                    "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                    "register_name": {"type": "string", "default": "MCP Export"},
                },
                "required": ["serials"],
            },
        ),
        Tool(
            name="catalog_stats",
            description="Статистика каталога промптов: количество, фасеты, статус векторного поиска.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="validate_json_schema",
            description="Валидация данных по JSON Schema Draft 2020-12.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "object"},
                    "data": {},
                },
                "required": ["schema", "data"],
            },
        ),
    ]
    if _load_meta_agent_tool() is not None:
        tools.append(
            Tool(
                name="meta_agent_compiler",
                description=(
                    "Компиляция неструктурированной задачи в графовую топологию "
                    "с JSON Schema контрактами (W3C Trace Context, circuit breaker)."
                ),
                inputSchema=META_AGENT_COMPILER_SCHEMA,
            )
        )
    return tools


mcp = Server("prompt-ops")


@mcp.list_tools()
async def _handle_list_tools() -> list[Tool]:
    return _tool_definitions()


@mcp.call_tool()
async def _handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    def payload(result: Any) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    try:
        if name == "validate_json_schema":
            from jsonschema import Draft202012Validator, ValidationError

            schema = arguments.get("schema") or {}
            data = arguments.get("data")
            validator = Draft202012Validator(schema)
            errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
            if not errors:
                return payload({"valid": True})
            return payload(
                {
                    "valid": False,
                    "errors": [
                        {"path": list(e.absolute_path), "message": e.message} for e in errors[:20]
                    ],
                }
            )

        backend = _require_backend()
        if name == "list_prompts":
            return payload(await backend.list_prompts(**arguments))
        if name == "get_prompt":
            return payload(await backend.get_prompt(str(arguments.get("serial", ""))))
        if name == "semantic_search_prompts":
            return payload(await backend.semantic_search(**arguments))
        if name == "export_prompts":
            return payload(await backend.export_prompts(**arguments))
        if name == "catalog_stats":
            return payload(await backend.catalog_stats())
        if name == "meta_agent_compiler":
            tool = _load_meta_agent_tool()
            if tool is None:
                return payload({"error": "meta_agent_compiler skill is not available"})
            return payload(
                await tool(
                    trace_context=arguments.get("trace_context"),
                    task_metadata=arguments.get("task_metadata"),
                    payload=arguments.get("payload"),
                    circuit=arguments.get("circuit"),
                    error_diff=arguments.get("error_diff"),
                )
            )
        return payload({"error": f"Unknown tool: {name}"})
    except Exception as exc:
        logger.warning("MCP tool %s failed: %s", name, exc, exc_info=True)
        return payload({"error": str(exc)})


class BearerTokenASGI:
    """ASGI-обёртка, требующая заголовок ``Authorization: Bearer <MCP_API_KEY>``."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        import os
        import secrets

        expected = os.getenv("MCP_API_KEY", "").strip()
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        if not expected or not secrets.compare_digest(token, expected):
            body = json.dumps({"error": "invalid_token"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


session_manager = StreamableHTTPSessionManager(app=mcp, stateless=True)


def _build_mcp_http_app() -> Starlette:
    inner = Starlette(routes=[Mount("/", app=session_manager.handle_request)])
    return Starlette(routes=[Mount("/mcp", app=BearerTokenASGI(inner))])


mcp_http_app = _build_mcp_http_app()


async def main() -> None:
    """Автономный запуск MCP-сервера через stdio (для IDE-клиентов)."""
    import asyncio

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
