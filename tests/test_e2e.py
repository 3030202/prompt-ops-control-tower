import os

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("PROMPT_OPS_E2E_URL", "http://127.0.0.1:8000")
USER = os.getenv("DASHBOARD_USER", "admin")
PASSWORD = os.getenv("DASHBOARD_PASS", "admin_pass_9988")


@pytest.fixture
def authed_page(browser):
    context = browser.new_context(http_credentials={"username": USER, "password": PASSWORD})
    page = context.new_page()
    yield page
    context.close()


def test_studio_channel_button_and_lite_visibility(authed_page: Page):
    page = authed_page
    page.goto(f"{BASE_URL}/studio")
    expect(page).to_have_title("Prompt Ops Publishing Studio")
    page.get_by_role("button", name="Channels").click()
    page.locator("#channelName").fill("E2E channel")
    page.locator("#channelChat").fill("-1000000000999")
    page.locator("#channelTest").check()
    page.locator("#addChannel").click()
    expect(page.get_by_role("heading", name="E2E channel")).to_be_visible()
    channel_id = page.evaluate("state.channels.find(x => x.name === 'E2E channel').id")
    page.request.delete(f"{BASE_URL}/api/channels/{channel_id}")
    page.goto(f"{BASE_URL}/lite")
    expect(page).to_have_title("Prompt Ops Lite")
    expect(page.get_by_role("heading", name="Prompt Ops Lite")).to_be_visible()


def test_dashboard_tui_keyboard_flow(authed_page: Page):
    page = authed_page
    page.goto(f"{BASE_URL}/")
    expect(page).to_have_title("Prompt Ops // TUI")
    expect(page.locator(".artifact-row").first).to_be_visible()
    expect(page.locator(".source-group").first).to_be_visible()
    page.keyboard.press("s")
    expect(page.locator(".workspace")).to_have_class("workspace sources-hidden")
    page.keyboard.press("s")
    expect(page.locator(".workspace")).to_have_class("workspace")
    page.keyboard.press("e")
    expect(page.locator("#sourceErrors")).to_be_hidden()
    page.keyboard.press("e")
    page.keyboard.press("Space")
    expect(page.locator("#selectedCount")).to_have_text("1")
    page.keyboard.press("3")
    expect(page.locator("#previewMode")).to_have_text("Canvas")
    page.keyboard.press("/")
    expect(page.locator("#quickSearch")).to_be_focused()
    page.keyboard.press("Escape")
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+k")
    expect(page.locator("#commandPalette")).to_be_visible()
    expect(page.get_by_text("Export selection")).to_be_visible()
    expect(page.get_by_text("Interactive ASCII tag cloud")).to_be_visible()
    page.keyboard.press("Escape")
    page.keyboard.press("Backquote")
    expect(page.locator("#tagCloudDialog")).to_be_visible()
    expect(page.locator(".cloud-tag").first).to_be_visible()
    page.keyboard.press("Space")
    expect(page.locator("#cloudSelectedCount")).to_have_text("1")
    page.keyboard.press("Enter")
    expect(page.locator("#tagCloudDialog")).to_be_hidden()
    expect(page.locator("#quickSearch")).not_to_have_value("")


def test_prompt_dense_workspace_keyboard_registers_and_api(authed_page: Page, browser):
    page = authed_page
    page.goto(f"{BASE_URL}/prompts")
    page.evaluate("localStorage.removeItem('prompt-register-workspace.v1')")
    page.reload()
    expect(page).to_have_title("Prompt Register // Dense TUI")
    expect(page.locator("#treePane")).to_be_visible()
    expect(page.locator("#tablePane")).to_be_visible()
    expect(page.locator("#previewPane")).to_be_visible()
    expect(page.locator(".prompt-row").first).to_be_visible(timeout=60_000)
    expect(page.locator("#previewCode")).not_to_have_text("LOADING_PROMPT...", timeout=20_000)
    expect(page.locator("#pHow")).not_to_have_text("-")
    expect(page.locator("#pInputTokens")).not_to_have_text("-")

    compact_response = page.request.get(f"{BASE_URL}/api/prompts?view=compact&limit=10")
    assert compact_response.ok
    compact = compact_response.json()
    assert {"items", "count", "total", "offset", "limit", "facets"} == set(compact)
    assert compact["items"]
    assert "prompt_body" not in compact["items"][0]
    assert 3 <= len(compact["items"][0]["tags"]) <= 5
    assert compact["items"][0]["token_estimate"]["total"]["max"] > 0
    serial = compact["items"][0]["serial"]
    detail_response = page.request.get(f"{BASE_URL}/api/prompts/{serial}")
    assert detail_response.ok
    detail = detail_response.json()
    assert detail["prompt_body"]
    assert detail["how_it_works"]
    assert detail["structure"]
    assert detail["coverage"]
    assert detail["expected_output"]
    assert "references" not in detail

    page.keyboard.press("2")
    page.keyboard.press("Space")
    expect(page.locator(".prompt-row.active .reg-token.on")).to_have_count(1)
    expect(page.locator("#activeRegister")).to_contain_text("KEEP [1]")
    page.keyboard.press("r")
    expect(page.locator("#registerDialog")).to_be_visible()
    page.keyboard.press("2")
    page.keyboard.press("Escape")
    page.keyboard.press("Space")
    expect(page.locator(".prompt-row.active .reg-token.on")).to_have_count(2)
    expect(page.locator("#activeRegister")).to_contain_text("REVIEW [1]")

    page.keyboard.press("v")
    page.keyboard.press("j")
    expect(page.locator(".prompt-row.in-range")).to_have_count(2)
    page.keyboard.press("Space")
    expect(page.locator("#activeRegister")).to_contain_text("REVIEW [2]")
    page.reload()
    expect(page.locator(".prompt-row").first).to_be_visible(timeout=30_000)
    expect(page.locator("#activeRegister")).to_contain_text("REVIEW [2]")

    page.keyboard.press("b")
    expect(page.locator("#workspace")).to_have_class("workspace tree-hidden")
    page.keyboard.press("b")
    page.keyboard.press("p")
    expect(page.locator("#workspace")).to_have_class("workspace preview-hidden")
    page.keyboard.press("p")
    page.keyboard.press("/")
    expect(page.locator("#search")).to_be_focused()
    page.keyboard.press("Escape")
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+k")
    expect(page.locator("#commandDialog")).to_be_visible()
    expect(page.get_by_text("Export registers")).to_be_visible()
    page.keyboard.press("Escape")
    page.keyboard.press("?")
    expect(page.locator("#helpDialog")).to_be_visible()
    page.keyboard.press("Escape")

    export_response = page.request.post(
        f"{BASE_URL}/api/prompts/export",
        data={"format": "json", "registers": [{"id": "keep", "name": "KEEP", "color": "#57ff8f", "serials": [serial]}]},
    )
    assert export_response.ok
    assert export_response.json()["registers"][0]["prompts"][0]["serial"] == serial

    anonymous = browser.new_context().request
    unauthorized = anonymous.post(
        f"{BASE_URL}/api/prompts/analyze",
        data={"serials": [serial], "register_name": "KEEP"},
    )
    assert unauthorized.status == 401
