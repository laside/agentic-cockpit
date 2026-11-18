#!/usr/bin/env python3
"""
Tests unitaires du parseur de consommation réelle (core/usage.py).
Aucun appel réseau ni LLM ; stdlib uniquement.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import usage  # noqa: E402


OPENCODE_SAMPLE = (
    '{"type": "step_finish", "timestamp": 1794997514199, "sessionID": "ses_x", '
    '"part": {"type": "step-finish", "reason": "stop", '
    '"tokens": {"total": 10684, "input": 9018, "output": 2, "reasoning": 0, '
    '"cache": {"write": 0, "read": 1664}}, "cost": 0.001358892}}'
)

CLAUDE_SAMPLE = (
    '{"duration_api_ms":605,"stop_reason":"end_turn","total_cost_usd":0.00326982,'
    '"usage":{"input_tokens":2,"cache_creation_input_tokens":15877,'
    '"cache_read_input_tokens":9222,"output_tokens":4}}'
)

AGY_SAMPLE = (
    '{"conversation_id":"x","status":"SUCCESS","response":"ok\\n","duration_seconds":7.2,'
    '"num_turns":1,"usage":{"input_tokens":9810,"output_tokens":23,'
    '"thinking_tokens":18,"cache_read_tokens":0,"total_tokens":9833}}'
)


class UsageParserTests(unittest.TestCase):
    def test_opencode_json_single_event(self) -> None:
        res = usage.parse_usage("opencode_json", OPENCODE_SAMPLE)
        self.assertEqual(
            res,
            {
                "tokens_in": 9018,
                "tokens_out": 2,
                "tokens_cache": 1664,
                "cost_usd": 0.001358892,
                "cost_kind": "measured",
            },
        )

    def test_opencode_json_multiple_step_finish_summed_with_warnings(self) -> None:
        line2 = (
            '{"type": "step-finish", "part": {"tokens": {"input": 100, "output": 10, '
            '"reasoning": 5, "cache": {"write": 20, "read": 30}}, "cost": 0.002}}'
        )
        other_event = '{"type": "text", "part": {"text": "bonjour"}}'
        text = "\n".join([
            "WARN: configuration expérimentale activée",
            other_event,
            OPENCODE_SAMPLE,
            "info: outil exécuté",
            line2,
        ])
        res = usage.parse_usage("opencode_json", text)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["tokens_in"], 9018 + 100)
        self.assertEqual(res["tokens_out"], 2 + 10 + 5)
        self.assertEqual(res["tokens_cache"], 1664 + 20 + 30)
        self.assertAlmostEqual(res["cost_usd"], 0.001358892 + 0.002)
        self.assertEqual(res["cost_kind"], "measured")

    def test_claude_json_real_sample_with_warning_and_last_object(self) -> None:
        earlier = '{"usage":{"input_tokens":1,"output_tokens":1},"total_cost_usd":0.0001}'
        text = f"Avertissement CLI stderr\n{earlier}\n{CLAUDE_SAMPLE}\n"
        res = usage.parse_usage("claude_json", text)
        self.assertEqual(
            res,
            {
                "tokens_in": 2,
                "tokens_out": 4,
                "tokens_cache": 15877 + 9222,
                "cost_usd": 0.00326982,
                "cost_kind": "api_equivalent",
            },
        )

    def test_agy_json_real_sample(self) -> None:
        text = f"Note: démarrage agy\n{AGY_SAMPLE}\n"
        res = usage.parse_usage("agy_json", text)
        self.assertEqual(
            res,
            {
                "tokens_in": 9810,
                "tokens_out": 23 + 18,
                "tokens_cache": 0,
                "cost_usd": None,
                "cost_kind": "tokens_only",
            },
        )

    def test_text_without_json_returns_none(self) -> None:
        for fmt in ("opencode_json", "claude_json", "agy_json"):
            self.assertIsNone(usage.parse_usage(fmt, " simple texte sans json \n{json cassé"))
            self.assertIsNone(usage.parse_usage(fmt, ""))

    def test_unknown_format_returns_none(self) -> None:
        self.assertIsNone(usage.parse_usage("inconnu", OPENCODE_SAMPLE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
