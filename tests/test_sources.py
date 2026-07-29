import asyncio

import pytest

from prompt_ops_app import (
    DEFAULT_SOURCE_BLUEPRINTS,
    build_prompt_projection,
    build_prompt_register_export,
    classify_artifact,
    compact_prompt_item,
    filter_prompt_items,
    extract_prompt_body,
    fetch_x_search_items,
    prompt_literacy_score,
    prompt_mechanics,
    prompt_mechanics_description,
    prompt_facets,
    prompt_tags,
    prompt_token_estimate,
    public_prompt_item,
    sort_prompt_items,
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
    assert 3 <= len(projected["tags"]) <= 5
    assert projected["how_it_works"]
    assert projected["structure"]
    assert projected["coverage"]
    assert projected["token_estimate"]["total"]["max"] > projected["token_estimate"]["input"]["max"]
    assert "references" not in projected


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
        "how_it_works",
        "why_it_works",
        "structure",
        "coverage",
        "expected_output",
        "learning_complexity",
        "token_estimate",
        "tags",
        "complexity",
        "literacy_score",
        "special_marks",
        "remarks",
        "prompt_type",
    }
    assert extract_prompt_body("Title: Ignore\n```\nAct as an editor and return only JSON.\n```") == "Act as an editor and return only JSON."
    assert prompt_literacy_score(public["prompt_body"]) > 40


def test_prompt_description_explains_mechanics_reason_and_output():
    description = prompt_mechanics_description(
        "Product researcher",
        "You are a product researcher. Compare {a} and {b}. Return only JSON. Never invent facts.",
    )
    assert description.startswith("Как работает:")
    assert "Почему работает:" in description
    assert "На выходе:" in description
    assert "JSON" in description
    assert "переменн" in description
    assert "ограничен" in description


def test_prompt_output_intent_avoids_code_keyword_false_positive():
    description = prompt_mechanics_description(
        "Translation assistant",
        "Act as a translator. Translate the synopsis and preserve the language code metadata.",
    )
    assert "переведённый и адаптированный текст" in description
    assert "техническая реализация" not in description


def test_missing_prompt_tags_are_generated_in_english_with_specific_labels():
    tags, origin = prompt_tags(
        "Product FAQ builder",
        "Generate a complete FAQ for {product}. Group questions and return Markdown.",
        "Prompt",
        [],
    )
    assert 3 <= len(tags) <= 5
    assert tags[:2] == ["faq-generation", "question-coverage"]
    assert all(tag == tag.lower() and " " not in tag for tag in tags)
    assert origin == "generated"
    fallback_tags, _ = prompt_tags("Language Detection", "Identify the language used in the supplied text.", "Prompt", [])
    assert fallback_tags[0] == "language-detection"


def test_prompt_mechanics_and_token_estimate_are_structured_without_references():
    body = "Act as a researcher. Compare {product_a} and {product_b}. Return only JSON."
    mechanics = prompt_mechanics("Product comparison", body, 68)
    estimate = prompt_token_estimate(body, mechanics["expected_output"])
    assert "Сопоставляет" in mechanics["how_it_works"]
    assert mechanics["structure"]
    assert mechanics["coverage"]
    assert mechanics["learning_complexity"]["level"] == "средняя"
    assert estimate["input"]["max"] > estimate["input"]["min"]
    assert estimate["total"]["max"] == estimate["input"]["max"] + estimate["output"]["max"]
    assert "references" not in mechanics
    script = prompt_mechanics(
        "UGC-Style TikTok Script Generator",
        "Create a TikTok script and organize scenes in a table with hook, dialogue, visual, and CTA.",
        70,
    )
    assert "сценарий короткого видео" in script["how_it_works"]
    assert "готовый сценарий" in script["expected_output"]
    assert "аналитическая таблица" not in script["expected_output"]



def prompt_fixture(serial, title, tags, prompt_type, complexity, literacy, published_ts):
    return {
        "id": serial,
        "serial": serial,
        "title": title,
        "prompt_body": f"Act as {title}. Return only JSON.",
        "description": "Как работает: role. Почему работает: focus. На выходе: JSON.",
        "tags": tags,
        "complexity": complexity,
        "literacy_score": literacy,
        "special_marks": ["STRUCTURED_OUTPUT"],
        "remarks": ["test"],
        "prompt_type": prompt_type,
        "published_ts": published_ts,
        "path": "/private/path",
    }


def test_dense_prompt_filters_use_or_within_groups_and_and_between_groups():
    prompts = [
        prompt_fixture("P-000001", "Alpha", ["agent"], "Prompt", 45, 60, 1),
        prompt_fixture("P-000002", "Beta", ["image"], "Image Prompt", 85, 90, 2),
        prompt_fixture("P-000003", "Gamma", ["video"], "Video Prompt", 70, 75, 3),
    ]
    filtered = filter_prompt_items(
        prompts,
        tags={"agent", "image"},
        prompt_types={"prompt", "image prompt"},
        complexity_buckets={"40-59", "80-100"},
    )
    assert [item["serial"] for item in filtered] == ["P-000001", "P-000002"]
    assert filter_prompt_items(prompts, tags={"agent"}, prompt_types={"image prompt"}) == []


def test_dense_prompt_sort_facets_and_compact_projection():
    prompts = [
        prompt_fixture("P-000001", "Zulu", ["agent", "json"], "Prompt", 45, 60, 1),
        prompt_fixture("P-000002", "Alpha", ["json"], "Image Prompt", 85, 90, 2),
    ]
    assert [item["serial"] for item in sort_prompt_items(prompts, "title")] == ["P-000002", "P-000001"]
    assert sort_prompt_items(prompts, "complexity")[0]["serial"] == "P-000002"
    facets = prompt_facets(prompts)
    assert facets["tags"]["json"] == 2
    assert facets["complexity"]["40-59"] == 1
    assert facets["complexity"]["80-100"] == 1
    compact = compact_prompt_item(prompts[0])
    assert "prompt_body" not in compact
    assert "remarks" not in compact
    assert "path" not in compact


def test_prompt_register_exports_are_allowlisted_and_fence_safe():
    prompt = prompt_fixture("P-000001", "Exporter", ["json"], "Prompt", 60, 80, 1)
    prompt["prompt_body"] = "Use ``` inside the prompt"
    content, media_type = build_prompt_register_export([
        {"id": "keep", "name": "KEEP", "color": "#57ff8f", "prompts": [prompt]},
    ], "json")
    assert media_type == "application/json"
    assert "/private/path" not in content
    assert '"serial": "P-000001"' in content
    assert '"token_estimate"' in content
    assert '"references"' not in content
    markdown, markdown_type = build_prompt_register_export([
        {"id": "keep", "name": "KEEP", "color": "#57ff8f", "prompts": [prompt]},
    ], "md")
    assert markdown_type == "text/markdown"
    assert "````text" in markdown
