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


if __name__ == "__main__":
    unittest.main()
