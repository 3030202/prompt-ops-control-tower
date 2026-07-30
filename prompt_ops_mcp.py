import os
import secrets
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import JSONResponse

load_dotenv()


ListPromptsCallback = Callable[..., Awaitable[dict]]
GetPromptCallback = Callable[[str], Awaitable[dict]]
SemanticSearchCallback = Callable[..., Awaitable[dict]]
ExportPromptsCallback = Callable[..., Awaitable[dict]]
CatalogStatsCallback = Callable[[], Awaitable[dict]]


@dataclass
class PromptOpsMCPBackend:
    list_prompts: ListPromptsCallback
    get_prompt: GetPromptCallback
    semantic_search: SemanticSearchCallback
    export_prompts: ExportPromptsCallback
    catalog_stats: CatalogStatsCallback


_backend: PromptOpsMCPBackend | None = None

mcp = MCPServer(
    "Prompt Ops Prompt Register",
    instructions=(
        "Read-only access to the public Prompt Register. Search before reading full prompt bodies, "
        "prefer semantic_search_prompts for intent-based discovery, and use serial numbers as stable identifiers."
    ),
)


def configure_mcp(backend: PromptOpsMCPBackend) -> None:
    global _backend
    _backend = backend


def _require_backend() -> PromptOpsMCPBackend:
    if _backend is None:
        raise RuntimeError("Prompt Ops MCP backend is not configured")
    return _backend


@mcp.tool()
async def list_prompts(
    query: Annotated[str, Field(description="Text matched against title, tags, type, and prompt body.")] = "",
    tags: Annotated[list[str] | None, Field(description="English tags; all supplied tags must match.")] = None,
    prompt_types: Annotated[list[str] | None, Field(description="Allowed prompt types.")] = None,
    min_complexity: Annotated[int, Field(ge=0, le=100)] = 0,
    max_complexity: Annotated[int, Field(ge=0, le=100)] = 100,
    sort: Literal["newest", "oldest", "complexity_desc", "literacy_desc", "title"] = "newest",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """List compact public prompt records with deterministic filters and pagination."""
    return await _require_backend().list_prompts(
        query=query,
        tags=tags or [],
        prompt_types=prompt_types or [],
        min_complexity=min_complexity,
        max_complexity=max_complexity,
        sort=sort,
        offset=offset,
        limit=limit,
    )


@mcp.tool()
async def get_prompt(
    serial: Annotated[str, Field(pattern=r"^P-\d{6}$", description="Stable serial such as P-000123.")],
) -> dict:
    """Read one complete public prompt with mechanics, output expectations, tags, and token estimates."""
    return await _require_backend().get_prompt(serial)


@mcp.tool()
async def semantic_search_prompts(
    query: Annotated[str, Field(min_length=2, max_length=500, description="Intent or use case to search by meaning.")],
    tags: Annotated[list[str] | None, Field(description="Optional English tags; all supplied tags must match.")] = None,
    prompt_types: Annotated[list[str] | None, Field(description="Optional prompt type allowlist.")] = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict:
    """Find public prompts by semantic similarity using the persistent vector index."""
    return await _require_backend().semantic_search(
        query=query,
        tags=tags or [],
        prompt_types=prompt_types or [],
        limit=limit,
    )


@mcp.tool()
async def export_prompts(
    serials: Annotated[list[str], Field(min_length=1, max_length=50, description="Prompt serials to export.")],
    format: Literal["md", "json"] = "md",
    register_name: Annotated[str, Field(min_length=1, max_length=80)] = "MCP_SELECTION",
) -> dict:
    """Export selected public prompts as Markdown or JSON without private source metadata."""
    return await _require_backend().export_prompts(
        serials=serials,
        format_name=format,
        register_name=register_name,
    )


@mcp.resource("promptops://catalog/stats", mime_type="application/json")
async def catalog_stats() -> dict:
    """Current public catalog counts and facets."""
    return await _require_backend().catalog_stats()


@mcp.resource("promptops://prompts/{serial}", mime_type="application/json")
async def prompt_resource(serial: str) -> dict:
    """A complete public prompt addressed by stable serial."""
    return await _require_backend().get_prompt(serial)


@mcp.prompt()
def analyze_prompt_collection(goal: str = "Find the strongest reusable prompt patterns") -> str:
    """Instruction for comparing a user-selected prompt collection."""
    return (
        f"Goal: {goal}\n"
        "Use list_prompts or semantic_search_prompts first. Read full candidates with get_prompt. "
        "Compare their structure, constraints, expected outputs, literacy, complexity, token cost, and failure modes. "
        "Return: recommended prompts, why they work, conflicts or gaps, and a concrete merged pattern. "
        "Do not invent source metadata that is absent from the public records."
    )


class BearerTokenASGI:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        expected = os.getenv("MCP_API_KEY", "").strip()
        if not expected:
            response = JSONResponse(
                {"error": "mcp_not_configured", "error_description": "MCP_API_KEY is not configured"},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "Bearer authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="prompt-ops-mcp"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _csv_env(name: str, default: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


transport_security = TransportSecuritySettings(
    allowed_hosts=_csv_env(
        "MCP_ALLOWED_HOSTS",
        "8.0x101.lol,8.0x101.lol:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*,testserver",
    ),
    allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS", "https://8.0x101.lol"),
)

_transport_app = mcp.streamable_http_app(
    transport_security=transport_security,
)
mcp_http_app = BearerTokenASGI(_transport_app)
