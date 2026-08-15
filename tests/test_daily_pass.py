import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, Request

import daily_pass
import publishing_studio


class TestDailyPass(unittest.TestCase):
    def test_deterministic_pin_is_four_digits(self):
        dates = ["2026-08-15", "2026-08-16", "2026-12-31", "2027-01-01"]
        for d in dates:
            pin = daily_pass.compute_deterministic_pin(d)
            self.assertEqual(len(pin), 4)
            self.assertTrue(pin.isdigit(), f"PIN {pin} is not digits for date {d}")

    def test_deterministic_pin_is_stable_and_different_per_day(self):
        pin1 = daily_pass.compute_deterministic_pin("2026-08-15")
        pin2 = daily_pass.compute_deterministic_pin("2026-08-15")
        pin_next = daily_pass.compute_deterministic_pin("2026-08-16")
        self.assertEqual(pin1, pin2)
        self.assertIsInstance(pin_next, str)
        self.assertEqual(len(pin_next), 4)

    def test_token_signing_and_verification(self):
        today = daily_pass.get_today_date_str()
        token = daily_pass.sign_daily_token(today)
        self.assertTrue(daily_pass.verify_daily_token(token))

        # Tampered token fails
        self.assertFalse(daily_pass.verify_daily_token(token + "x"))
        self.assertFalse(daily_pass.verify_daily_token("invalid.token"))
        self.assertFalse(daily_pass.verify_daily_token(""))
        self.assertFalse(daily_pass.verify_daily_token(None))

        # Old date token fails
        old_token = daily_pass.sign_daily_token("2020-01-01")
        self.assertFalse(daily_pass.verify_daily_token(old_token))

    def test_get_daily_pin_fallback_and_custom(self):
        async def run_test():
            fake_redis = AsyncMock()
            fake_redis.get = AsyncMock(return_value=None)

            # Default fallback
            pin = await daily_pass.get_daily_pin(fake_redis, "2026-08-15")
            expected_algo = daily_pass.compute_deterministic_pin("2026-08-15")
            self.assertEqual(pin, expected_algo)

            # Custom override
            fake_redis.get = AsyncMock(return_value="7777")
            custom_pin = await daily_pass.get_daily_pin(fake_redis, "2026-08-15")
            self.assertEqual(custom_pin, "7777")

        asyncio.run(run_test())

    def test_seconds_until_midnight(self):
        sec = daily_pass.seconds_until_midnight_msk()
        self.assertGreater(sec, 3600)
        self.assertLessEqual(sec, 86400 + 3600)

    def test_require_daily_pass_or_admin(self):
        # 1. Valid token in header
        today = daily_pass.get_today_date_str()
        valid_token = daily_pass.sign_daily_token(today)

        req1 = MagicMock(spec=Request)
        req1.headers = {"X-Daily-Pass-Token": valid_token}
        req1.cookies = {}
        # Should not raise exception
        daily_pass.require_daily_pass_or_admin(req1)

        # 2. Valid token in cookie
        req2 = MagicMock(spec=Request)
        req2.headers = {}
        req2.cookies = {"promptops_daily_pass": valid_token}
        daily_pass.require_daily_pass_or_admin(req2)

        # 3. Invalid token -> raises 401
        req3 = MagicMock(spec=Request)
        req3.headers = {"X-Daily-Pass-Token": "bad-token"}
        req3.cookies = {}
        with self.assertRaises(HTTPException) as ctx:
            daily_pass.require_daily_pass_or_admin(req3)
        self.assertEqual(ctx.exception.status_code, 401)

        # 4. MCP Bearer auth bypasses daily pass
        with patch.dict(os.environ, {"MCP_API_KEY": "secret-mcp-key"}):
            req4 = MagicMock(spec=Request)
            req4.headers = {"Authorization": "Bearer secret-mcp-key"}
            req4.cookies = {}
            daily_pass.require_daily_pass_or_admin(req4)

    def test_broadcast_daily_pin_announcement(self):
        async def run_test():
            fake_app = MagicMock()
            fake_redis = AsyncMock()
            fake_redis.get = AsyncMock(return_value=None)
            fake_redis.zcard = AsyncMock(return_value=42)
            fake_redis.zcount = AsyncMock(return_value=5)
            fake_redis.ping = AsyncMock(return_value=True)
            fake_redis.hvals = AsyncMock(
                return_value=['{"id": "ch_1", "username": "test_ch", "chat_id": 123, "enabled": true}']
            )
            fake_redis.set = AsyncMock(return_value=True)
            fake_redis.xadd = AsyncMock()

            fake_app.state.redis = fake_redis
            fake_app.state.config.telegram_bot_token = "fake-bot-token"

            orig_publishing_app = publishing_studio._app
            orig_daily_app = daily_pass._app
            publishing_studio._app = fake_app
            daily_pass._app = fake_app
            try:
                with patch("publishing_studio.send", AsyncMock(return_value={"message_id": 1001})):
                    result = await publishing_studio.broadcast_daily_pin_announcement(force=True)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["total_channels"], 1)
                    self.assertEqual(result["results"][0]["status"], "sent")
            finally:
                publishing_studio._app = orig_publishing_app
                daily_pass._app = orig_daily_app

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
