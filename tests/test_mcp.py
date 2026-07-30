import json

import pytest
from mcp import Client

from prompt_ops_app import mcp_tags_match, mcp_types_match
from prompt_ops_mcp import (
    BearerTokenASGI,
    PromptOpsMCPBackend,
    configure_mcp,
    mcp,
)


def test_mcp_filters_require_all_tags_and_allowlisted_types():
    prompt = {
        "tags": ["faq-generation", "structured-output", "product-research"],
        "prompt_type": "Prompt",
    }
    assert mcp_tags_match(prompt, ["faq-generation", "structured-output"])
    assert not mcp_tags_match(prompt, ["faq-generation", "video-prompting"])
    assert mcp_types_match(prompt, ["Prompt", "System Prompt"])
    assert not mcp_types_match(prompt, ["Video Prompt"])


async def fake_list_prompts(**kwargs):
    return {"items": [{"serial": "P-000001", "title": "Test"}], "total": 1, **kwargs}


async def fake_get_prompt(serial):
    return {"serial": serial, "title": "Test", "prompt_body": "Return JSON."}


async def fake_semantic_search(**kwargs):
    return {"items": [{"serial": "P-000001", "semantic_score": 0.9}], **kwargs}


async def fake_export_prompts(**kwargs):
    return {"count": len(kwargs["serials"]), "content": "# Export", **kwargs}


async def fake_catalog_stats():
    return {"total": 1, "vector_status": "online"}


def configure_fake_backend():
    configure_mcp(PromptOpsMCPBackend(
        list_prompts=fake_list_prompts,
        get_prompt=fake_get_prompt,
        semantic_search=fake_semantic_search,
        export_prompts=fake_export_prompts,
        catalog_stats=fake_catalog_stats,
    ))


@pytest.mark.anyio
async def test_mcp_lists_tools_and_calls_public_prompt_backend():
    configure_fake_backend()
    async with Client(mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "list_prompts",
            "get_prompt",
            "semantic_search_prompts",
            "export_prompts",
        }.issubset(names)

        result = await client.call_tool("get_prompt", {"serial": "P-000001"})
        payload = json.loads(result.content[0].text)
        assert payload["serial"] == "P-000001"
        assert payload["prompt_body"] == "Return JSON."


@pytest.mark.anyio
async def test_mcp_semantic_search_uses_bounded_arguments():
    configure_fake_backend()
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "semantic_search_prompts",
            {"query": "build a FAQ", "tags": ["faq-generation"], "limit": 5},
        )
        payload = json.loads(result.content[0].text)
        assert payload["query"] == "build a FAQ"
        assert payload["limit"] == 5


@pytest.mark.anyio
async def test_bearer_wrapper_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "correct-token")
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    wrapper = BearerTokenASGI(downstream)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await wrapper(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )

    assert not downstream_called
    assert messages[0]["status"] == 401
    body = json.loads(messages[1]["body"])
    assert body["error"] == "invalid_token"


@pytest.mark.anyio
async def test_bearer_wrapper_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "correct-token")
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    wrapper = BearerTokenASGI(downstream)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await wrapper(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"authorization", b"Bearer correct-token")],
            "query_string": b"",
        },
        receive,
        send,
    )

    assert downstream_called
