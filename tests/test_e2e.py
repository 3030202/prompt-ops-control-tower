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
