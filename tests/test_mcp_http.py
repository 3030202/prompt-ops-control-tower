import json
import os

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


BASE_URL = os.getenv("PROMPT_OPS_E2E_URL", "http://127.0.0.1:8000")
MCP_TOKEN = os.getenv("MCP_API_KEY", "replace-with-a-long-random-token")


@pytest.mark.anyio
async def test_remote_mcp_transport_lists_and_calls_tools():
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {MCP_TOKEN}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
    ) as http_client:
        transport = streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)
        async with Client(transport) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {
                "list_prompts",
                "get_prompt",
                "semantic_search_prompts",
                "export_prompts",
            }.issubset(names)

            result = await client.call_tool("list_prompts", {"limit": 2})
            assert not result.is_error
            payload = json.loads(result.content[0].text)
            assert {"items", "count", "total", "offset", "limit"} == set(payload)


@pytest.mark.anyio
async def test_remote_mcp_rejects_anonymous_requests():
    async with httpx2.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/mcp")
    assert response.status_code == 401
