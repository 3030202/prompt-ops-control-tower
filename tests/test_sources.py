import asyncio

import pytest

from prompt_ops_app import (
    DEFAULT_SOURCE_BLUEPRINTS,
    classify_artifact,
    fetch_x_search_items,
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
