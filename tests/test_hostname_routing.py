import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient

# Mock redis and other dependencies before importing app if needed
import prompt_ops_app


class TestHostnameRouting(unittest.TestCase):
    def setUp(self):
        # Configure app state before each test
        class MockConfig:
            dashboard_user = "admin"
            dashboard_pass = "pizdatebe"
            telegram_bot_token = "fake-bot-token"

        class MockRedis:
            async def ping(self):
                return True
            async def hvals(self, *args, **kwargs):
                return []
            async def zrevrange(self, *args, **kwargs):
                return []
            async def hget(self, *args, **kwargs):
                return None
            async def get(self, *args, **kwargs):
                return None
            async def zcard(self, *args, **kwargs):
                return 10

        fake_redis = MockRedis()
        prompt_ops_app.app.state.redis = fake_redis
        prompt_ops_app.app.state.http_client = AsyncMock()

        import publishing_studio
        publishing_studio._app = prompt_ops_app.app
        prompt_ops_app.configure_publishing(
            prompt_ops_app.app,
            AsyncMock(return_value=[]),
            AsyncMock(),
        )
        prompt_ops_app.configure_daily_pass(prompt_ops_app.app)
        self.client = TestClient(prompt_ops_app.app)

    def test_root_rewrites_to_prompts_on_08_domain(self):
        response = self.client.get("/", headers={"Host": "08.0x101.lol"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt Register", response.text)

    def test_root_rewrites_to_prompts_on_8_domain(self):
        response = self.client.get("/", headers={"Host": "8.0x101.lol"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt Register", response.text)

    def test_root_rewrites_to_prompts_with_port(self):
        response = self.client.get("/", headers={"Host": "08.0x101.lol:8000"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prompt Register", response.text)

    def test_public_get_endpoints_allowed_on_08_domain(self):
        endpoints = [
            "/health",
            "/prompts",
            "/lite",
            "/api/daily-pass/status",
            "/api/public/feed",
        ]
        for ep in endpoints:
            response = self.client.get(ep, headers={"Host": "08.0x101.lol"})
            self.assertNotEqual(
                response.status_code,
                404,
                f"Endpoint {ep} should not return 404 on 08.0x101.lol",
            )
            self.assertEqual(response.status_code, 200)

    def test_studio_and_management_require_auth_on_08_domain(self):
        unauthed_endpoints = [
            "/studio",
            "/api/channels",
            "/api/styles",
            "/api/post-drafts",
            "/api/publishing-rules",
        ]
        for ep in unauthed_endpoints:
            response = self.client.get(ep, headers={"Host": "08.0x101.lol"})
            self.assertEqual(
                response.status_code,
                401,
                f"Endpoint {ep} without auth must return 401, got {response.status_code}",
            )

        # With Basic Auth, /studio returns 200
        authed_response = self.client.get(
            "/studio",
            headers={"Host": "08.0x101.lol"},
            auth=("admin", "pizdatebe"),
        )
        self.assertEqual(authed_response.status_code, 200)
        self.assertIn("Publishing Studio", authed_response.text)

    def test_internal_radar_endpoints_blocked_on_08_domain(self):
        blocked_endpoints = [
            "/api/artifacts",
            "/api/sources",
            "/api/sources/groups",
            "/api/sources/errors",
        ]
        for ep in blocked_endpoints:
            response = self.client.get(ep, headers={"Host": "08.0x101.lol"})
            self.assertEqual(
                response.status_code,
                404,
                f"Internal endpoint {ep} must return 404 on 08.0x101.lol",
            )
            data = response.json()
            self.assertEqual(data.get("detail"), "Prompt-only surface")

    def test_internal_radar_endpoints_allowed_on_localhost(self):
        # On localhost / 127.0.0.1, internal endpoints are NOT blocked by prompt_hostname_router
        response = self.client.get("/api/artifacts", headers={"Host": "localhost:8000"})
        # Should not be 'Prompt-only surface' 404
        if response.status_code == 404:
            data = response.json()
            self.assertNotEqual(data.get("detail"), "Prompt-only surface")


if __name__ == "__main__":
    unittest.main()
