import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import publishing_studio as studio


class PublishingRulesTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            "id": "artifact-1",
            "source_id": "habr_ai",
            "source_name": "Habr AI",
            "type": "Skill",
            "tags": ["agent", "prompt"],
            "rating": 88,
            "complexity": 72,
            "published_ts": datetime.now(timezone.utc).timestamp(),
            "title": "Agent pipeline",
            "summary": "Fresh skill and prompt pipeline",
            "raw": "production workflow",
        }

    def test_and_rule_matches_all_conditions(self):
        rule = {"operator": "AND", "sources": ["habr_ai"], "types": ["Skill"], "tags_all": ["agent"], "min_rating": 80, "keywords": ["pipeline"]}
        matched, reasons = studio.rule_matches(rule, self.record)
        self.assertTrue(matched)
        self.assertIn("rating", reasons)

    def test_or_rule_matches_one_condition(self):
        rule = {"operator": "OR", "sources": ["missing"], "tags_any": ["prompt"]}
        self.assertTrue(studio.rule_matches(rule, self.record)[0])

    def test_invalid_regex_fails_closed(self):
        matched, reasons = studio.rule_matches({"regex": "["}, self.record)
        self.assertFalse(matched)
        self.assertEqual(reasons, ["invalid_regex"])

    def test_empty_rule_never_matches(self):
        self.assertEqual(studio.rule_matches({}, self.record), (False, ["no_conditions"]))

    def test_score_uses_documented_weights(self):
        fixed = datetime(2026, 7, 29, tzinfo=timezone.utc)
        with patch.object(studio, "utc_now", return_value=fixed):
            record = {**self.record, "published_ts": fixed.timestamp(), "novelty": 80}
            score = studio.candidate_score(record, {"source_weight": 60}, 0.9)
        expected = 0.45 * 88 + 0.20 * 100 + 0.15 * 80 + 0.10 * 60 + 0.10 * 90
        self.assertAlmostEqual(score, expected)

    def test_artifact_mode_requires_type(self):
        with self.assertRaises(ValueError):
            studio.normalize_rule({"mode": "artifact_from_source"})

    def test_live_rule_requires_channel(self):
        with self.assertRaises(ValueError):
            studio.normalize_rule({"live_enabled": True})

    def test_fallback_modes_are_first_person_and_source_bound(self):
        for mode in studio.POST_MODES:
            artifact_type = "skill" if mode == "artifact_from_source" else None
            result = studio.fallback_post(self.record, mode, artifact_type)
            self.assertIn("Источник:", result["text"])
            self.assertTrue(result["title"])

    def test_resolve_media_payload(self):
        import asyncio
        async def run():
            # Test direct URL
            res = await studio.resolve_media_payload("https://example.com/image.png")
            self.assertEqual(res, ("photo", "https://example.com/image.png"))

            # Test empty
            res_empty = await studio.resolve_media_payload("")
            self.assertIsNone(res_empty)

            # Test card hash in cache
            studio._cards_cache["testcard123"] = "<svg>Test</svg>"
            res_card = await studio.resolve_media_payload("/api/publishing/cards/testcard123")
            self.assertIsNotNone(res_card)
            self.assertEqual(res_card[0], "document")
            self.assertEqual(res_card[1][0], "prompt_card.svg")
        asyncio.run(run())

    def test_send_media_group_payload(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        async def run():
            fake_app = MagicMock()
            fake_app.state.redis = AsyncMock()
            fake_app.state.redis.set = AsyncMock(return_value=True)
            studio._app = fake_app

            with patch("publishing_studio.telegram_request", AsyncMock(return_value={"message_id": 999})) as mock_tg:
                channel = {"chat_id": "-10012345", "enabled": True}
                media_list = ["https://example.com/1.png", "https://example.com/2.png"]
                await studio.send(
                    channel=channel,
                    text="Hello Media Group",
                    idem="test_group_idem",
                    kind="manual",
                    media_url="https://example.com/1.png",
                    media_mode="auto",
                    media_urls=media_list
                )
                self.assertTrue(mock_tg.called)
                method = mock_tg.call_args[0][0]
                self.assertEqual(method, "sendMediaGroup")
                data = mock_tg.call_args[0][1]
                self.assertEqual(data["chat_id"], "-10012345")
                self.assertIn("media", data)
        asyncio.run(run())

    def test_notify_draft_for_moderation(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        async def run():
            fake_app = MagicMock()
            fake_cfg = MagicMock()
            fake_cfg.telegram_bot_token = "fake-token"
            fake_cfg.telegram_chat_id = "-10012345"
            fake_cfg.telegram_webapp_url = "https://0x101.lol/webapp"
            fake_app.state.config = fake_cfg
            fake_app.state.http_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value={"ok": True, "result": {"message_id": 111}})
            fake_app.state.http_client.post = AsyncMock(return_value=mock_resp)
            studio._app = fake_app

            draft = {
                "id": "draft-abc-123",
                "title": "Moderation Post",
                "text": "Post body text",
                "mode": "my_take",
                "channel_id": "chan-1",
            }
            with patch("publishing_studio.get_item", AsyncMock(return_value={"name": "Channel 1"})):
                res = await studio.notify_draft_for_moderation(draft)
                self.assertIsNotNone(res)
                self.assertTrue(fake_app.state.http_client.post.called)
                post_json = fake_app.state.http_client.post.call_args[1]["json"]
                self.assertEqual(post_json["chat_id"], "-10012345")
                self.assertIn("draft-abc", post_json["text"])
                self.assertIn("reply_markup", post_json)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
