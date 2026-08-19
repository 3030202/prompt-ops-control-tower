import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

import prompt_ops_app as app_module
from prompt_ops_app import (
    Config,
    app,
    handle_telegram_bot_update,
    init_telegram_bot_menu,
    load_config,
    send_telegram_bot_message,
)


class TestTelegramBot(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(
            dashboard_user="admin",
            dashboard_pass="admin",
            redis_host="redis",
            redis_port=6379,
            poll_tick_seconds=45,
            scan_roots=["/app"],
            telegram_bot_token="8956213260:AAFxN6jqFStDsHB6OiqL38Ew-MaW3qmqdeI",
            telegram_webapp_url="https://0x101.lol/webapp",
            telegram_chat_id="-1001234567890",
            telegram_api_id=None,
            telegram_api_hash=None,
            target_channels=[],
            provider_name="Perplexity",
            provider_kind="openai_compatible",
            provider_base_url="https://api.perplexity.ai",
            provider_api_key="pplx-test",
            provider_model="xai/grok-4.5",
            monthly_token_limit=500000,
            monthly_budget_usd=25.0,
            input_price_per_1m=2.0,
            output_price_per_1m=8.0,
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="",
        )
        app.state.config = self.cfg
        app.state.vector_status = "ready"
        app.state.session_id = "test_session_1234"

    def test_config_has_telegram_bot(self):
        self.assertTrue(self.cfg.has_telegram_bot)
        self.assertEqual(self.cfg.telegram_webapp_url, "https://0x101.lol/webapp")

        empty_cfg = Config(
            dashboard_user="admin",
            dashboard_pass="admin",
            redis_host="redis",
            redis_port=6379,
            poll_tick_seconds=45,
            scan_roots=["/app"],
            telegram_bot_token="",
            telegram_webapp_url="https://0x101.lol/webapp",
            telegram_chat_id="",
            telegram_api_id=None,
            telegram_api_hash=None,
            target_channels=[],
            provider_name="",
            provider_kind="",
            provider_base_url="",
            provider_api_key="",
            provider_model="",
            monthly_token_limit=0,
            monthly_budget_usd=0.0,
            input_price_per_1m=0.0,
            output_price_per_1m=0.0,
            qdrant_url="",
            qdrant_api_key="",
        )
        self.assertFalse(empty_cfg.has_telegram_bot)

    def test_init_telegram_bot_menu(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post = AsyncMock(return_value=mock_resp)

            await init_telegram_bot_menu(mock_client, self.cfg)
            self.assertEqual(mock_client.post.call_count, 2)
            
            first_call = mock_client.post.call_args_list[0]
            self.assertIn("setMyCommands", first_call[0][0])
            self.assertIn("commands", first_call[1]["json"])

            second_call = mock_client.post.call_args_list[1]
            self.assertIn("setChatMenuButton", second_call[0][0])
            self.assertEqual(second_call[1]["json"]["menu_button"]["web_app"]["url"], "https://0x101.lol/webapp")

        asyncio.run(run())

    def test_handle_start_command(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1001,
                "message": {
                    "chat": {"id": 12345678},
                    "text": "/start",
                    "from": {"first_name": "Alex", "username": "alex"},
                }
            }

            await handle_telegram_bot_update(mock_client, update, self.cfg)
            self.assertTrue(mock_client.post.called)
            call_args = mock_client.post.call_args
            payload = call_args[1]["json"]
            self.assertEqual(payload["chat_id"], 12345678)
            self.assertIn("Prompt Ops Control Tower", payload["text"])
            self.assertIn("reply_markup", payload)
            buttons = payload["reply_markup"]["inline_keyboard"]
            self.assertEqual(buttons[0][0]["web_app"]["url"], "https://0x101.lol/webapp")

        asyncio.run(run())

    def test_handle_status_command(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1002,
                "message": {
                    "chat": {"id": 12345678},
                    "text": "/status",
                }
            }

            with patch("prompt_ops_app.load_prompt_catalog", AsyncMock(return_value=[{"serial": "P-000001"}])), \
                 patch("prompt_ops_app.load_source_catalog", AsyncMock(return_value=[{"id": "habr"}])):
                await handle_telegram_bot_update(mock_client, update, self.cfg)

            call_args = mock_client.post.call_args
            payload = call_args[1]["json"]
            self.assertIn("Status", payload["text"])
            self.assertIn("Qdrant Vectors", payload["text"])

        asyncio.run(run())

    def test_handle_callback_query(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1003,
                "callback_query": {
                    "id": "cq_999",
                    "data": "cmd:studio",
                    "message": {
                        "chat": {"id": 12345678},
                    }
                }
            }

            await handle_telegram_bot_update(mock_client, update, self.cfg)
            # Both answerCallbackQuery and sendMessage
            self.assertGreaterEqual(mock_client.post.call_count, 2)

        asyncio.run(run())

    def test_handle_digest_command(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1004,
                "message": {
                    "chat": {"id": -1001234567890},
                    "text": "/digest",
                }
            }

            fake_draft = {
                "id": "draft-123456",
                "title": "Test Digest Title",
                "text": "Post text preview",
                "mode": "my_take",
                "status": "review",
                "channel_id": "chan-1",
            }

            with patch("prompt_ops_app.studio_list_items", AsyncMock(return_value=[fake_draft])), \
                 patch("prompt_ops_app.notify_draft_for_moderation", AsyncMock(return_value={"ok": True})) as mock_notify:
                await handle_telegram_bot_update(mock_client, update, self.cfg)
                self.assertTrue(mock_notify.called)

        asyncio.run(run())

    def test_handle_moderation_callback_publish(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1005,
                "callback_query": {
                    "id": "cq_pub_1",
                    "data": "pub:draft:draft-123456",
                    "message": {
                        "chat": {"id": -1001234567890},
                        "message_id": 555,
                    }
                }
            }

            fake_draft = {
                "id": "draft-123456",
                "title": "Publishable Draft",
                "status": "review",
                "channel_id": "chan-1",
            }
            published_draft = {
                **fake_draft,
                "status": "published",
                "telegram_link": "https://t.me/testchannel/123",
            }

            with patch("prompt_ops_app.studio_get_item", AsyncMock(return_value=fake_draft)), \
                 patch("prompt_ops_app.publish_draft", AsyncMock(return_value=published_draft)) as mock_pub:
                await handle_telegram_bot_update(mock_client, update, self.cfg)
                self.assertTrue(mock_pub.called)
                
            # Verify editMessageText and answerCallbackQuery were called
            methods_called = [call[0][0] for call in mock_client.post.call_args_list]
            self.assertTrue(any("editMessageText" in m for m in methods_called))
            self.assertTrue(any("answerCallbackQuery" in m for m in methods_called))

        asyncio.run(run())

    def test_handle_moderation_callback_unauthorized(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1006,
                "callback_query": {
                    "id": "cq_unauth",
                    "data": "pub:draft:draft-123456",
                    "message": {
                        "chat": {"id": 99999999},  # Unauthorized chat
                        "message_id": 556,
                    }
                }
            }

            with patch("prompt_ops_app.publish_draft", AsyncMock()) as mock_pub:
                await handle_telegram_bot_update(mock_client, update, self.cfg)
                self.assertFalse(mock_pub.called)

        asyncio.run(run())

    def test_handle_moderation_callback_archive(self):
        async def run():
            mock_client = AsyncMock(spec=AsyncClient)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True})
            mock_client.post = AsyncMock(return_value=mock_resp)

            update = {
                "update_id": 1007,
                "callback_query": {
                    "id": "cq_arch_1",
                    "data": "archive:draft:draft-123456",
                    "message": {
                        "chat": {"id": -1001234567890},
                        "message_id": 557,
                    }
                }
            }

            fake_draft = {
                "id": "draft-123456",
                "title": "Archivable Draft",
                "status": "review",
            }

            with patch("prompt_ops_app.studio_get_item", AsyncMock(return_value=fake_draft)), \
                 patch("prompt_ops_app.studio_save_item", AsyncMock()) as mock_save:
                await handle_telegram_bot_update(mock_client, update, self.cfg)
                self.assertTrue(mock_save.called)
                saved_draft = mock_save.call_args[0][1]
                self.assertEqual(saved_draft["status"], "archived")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
