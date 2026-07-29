import asyncio

import pytest

from prompt_ops_app import (
    DEFAULT_SOURCE_BLUEPRINTS,
    build_prompt_projection,
    classify_artifact,
    extract_prompt_body,
    fetch_x_search_items,
    prompt_literacy_score,
    public_prompt_item,
    source_artifact_group,
)


def test_multimodal_and_distilled_artifact_classification():
    cases = {
        "NotebookLM steering prompt for Audio Overview": "NotebookLM",
        "Veo prompt with camera movement and image-to-video": "Video Prompt",
        "Midjourney image prompt with inpainting": "Image Prompt",
        "Context distillation: compress the prompt by 70%": "Distillate",
        "Developer prompt with custom instructions": "System Prompt",
    }
    for text, expected in cases.items():
        assert classify_artifact("", text)[0] == expected


def test_every_source_resolves_to_an_artifact_group():
    groups = {source_artifact_group(source) for source in DEFAULT_SOURCE_BLUEPRINTS}
    assert {
        "General Prompts",
        "System Prompts",
        "Image / Visual",
        "Video / Motion",
        "NotebookLM",
        "Distillates",
    }.issubset(groups)
    assert all(source_artifact_group(source) != "" for source in DEFAULT_SOURCE_BLUEPRINTS)


def test_x_search_fails_fast_without_bearer_token(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    source = {
        "id": "x_test",
        "name": "X test",
        "kind": "x_search",
        "query": "prompt engineering -is:retweet",
        "artifact_group": "General Prompts",
    }
    with pytest.raises(RuntimeError, match="X_BEARER_TOKEN"):
        asyncio.run(fetch_x_search_items(None, source))


def test_prompt_projection_normalizes_public_prompt():
    body = """
    You are a product researcher.
    Task: compare {product_a} and {product_b}.
    Constraints: use only supplied evidence and never invent prices.
    Output format: return JSON with verdict, risks, and citations.
    """
    record = {
        "id": "prompt-1",
        "title": "Product comparison",
        "raw": body,
        "type": "Prompt",
        "source_kind": "prompt_csv",
        "tags": ["Research", "JSON"],
        "complexity": 67,
        "published_ts": 1.0,
    }
    projected = build_prompt_projection(record)
    assert projected is not None
    assert projected["prompt_body"].startswith("You are")
    assert projected["literacy_score"] >= 70
    assert "STRUCTURED_OUTPUT" in projected["special_marks"]
    assert "HAS_VARIABLES" in projected["special_marks"]


def test_prompt_projection_rejects_private_and_non_prompt_materials():
    private = {
        "id": "private-1",
        "title": "Private prompt",
        "raw": "You are an assistant. Return only a detailed private report.",
        "type": "Prompt",
        "source_kind": "workspace",
    }
    noise = {
        "id": "noise-1",
        "title": "Release note",
        "raw": "Version 2.1 fixes two minor rendering bugs.",
        "type": "Noise",
        "source_kind": "rss",
    }
    assert build_prompt_projection(private) is None
    assert build_prompt_projection(noise) is None


def test_public_prompt_item_is_a_strict_allowlist():
    public = public_prompt_item({
        "serial": "P-000007",
        "title": "Safe prompt",
        "prompt_body": "You are a safe assistant. Return only JSON.",
        "description": "Produces JSON.",
        "tags": ["json"],
        "complexity": 50,
        "literacy_score": 80,
        "special_marks": ["STRUCTURED_OUTPUT"],
        "remarks": [],
        "prompt_type": "Prompt",
        "raw": "private raw",
        "path": "/private/path",
        "source_kind": "workspace",
        "session_id": "secret-session",
    })
    assert set(public) == {
        "serial",
        "title",
        "prompt_body",
        "description",
        "tags",
        "complexity",
        "literacy_score",
        "special_marks",
        "remarks",
        "prompt_type",
    }
    assert extract_prompt_body("Title: Ignore\n```\nAct as an editor and return only JSON.\n```") == "Act as an editor and return only JSON."
    assert prompt_literacy_score(public["prompt_body"]) > 40
