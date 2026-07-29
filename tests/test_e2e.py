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
