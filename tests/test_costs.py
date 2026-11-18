#!/usr/bin/env python3
"""
Tests du module costs (core/costs.py) : parsing de `opencode stats`, snapshots et deltas.
Aucun appel réel à opencode : subprocess.run est simulé par mock.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT))

from core import costs  # noqa: E402

EXTRAIT = (
    "│Total Cost                                       $12.37 │\n"
    "│Input                                              2.8M │\n"
    "│Output                                           521.7K │\n"
    "│Cache Read                                       340.5M │\n"
    "│Cache Write                                           0 │\n"
)


class ParseOpencodeStatsTests(unittest.TestCase):
    def test_parsing_extrait_contractuel(self) -> None:
        resultat = costs.parse_opencode_stats(EXTRAIT)
        self.assertEqual(resultat["total_cost_usd"], 12.37)
        self.assertEqual(resultat["input_tokens"], 2_800_000)
        self.assertEqual(resultat["output_tokens"], 521_700)
        self.assertEqual(resultat["cache_read_tokens"], 340_500_000)
        self.assertEqual(
            set(resultat),
            {"total_cost_usd", "input_tokens", "output_tokens", "cache_read_tokens"},
        )

    def test_parsing_avec_codes_ansi(self) -> None:
        texte_ansi = "\x1b[1m" + EXTRAIT.replace(
            "│Total Cost", "\x1b[0m│Total Cost"
        ).replace("$12.37", "\x1b[32m$12.37\x1b[0m") + "\x1b[0m"
        resultat = costs.parse_opencode_stats(texte_ansi)
        self.assertEqual(resultat["total_cost_usd"], 12.37)
        self.assertEqual(resultat["input_tokens"], 2_800_000)
        self.assertEqual(resultat["output_tokens"], 521_700)
        self.assertEqual(resultat["cache_read_tokens"], 340_500_000)

    def test_suffixes_k_et_m(self) -> None:
        texte = (
            "Total Cost $0.42\n"
            "Input 1.5K\n"
            "Output 2M\n"
            "Cache Read 7K\n"
        )
        resultat = costs.parse_opencode_stats(texte)
        self.assertEqual(resultat["total_cost_usd"], 0.42)
        self.assertEqual(resultat["input_tokens"], 1500)
        self.assertEqual(resultat["output_tokens"], 2_000_000)
        self.assertEqual(resultat["cache_read_tokens"], 7000)

    def test_lignes_absentes_donnent_zero(self) -> None:
        resultat = costs.parse_opencode_stats("")
        self.assertEqual(resultat["total_cost_usd"], 0.0)
        self.assertEqual(resultat["input_tokens"], 0)
        self.assertEqual(resultat["output_tokens"], 0)
        self.assertEqual(resultat["cache_read_tokens"], 0)

        partiel = costs.parse_opencode_stats("│Output  3 │\n")
        self.assertEqual(partiel["total_cost_usd"], 0.0)
        self.assertEqual(partiel["input_tokens"], 0)
        self.assertEqual(partiel["output_tokens"], 3)
        self.assertEqual(partiel["cache_read_tokens"], 0)


class DeltaTests(unittest.TestCase):
    def test_delta_positif(self) -> None:
        avant = {
            "total_cost_usd": 1.0,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
        }
        apres = {
            "total_cost_usd": 12.37,
            "input_tokens": 2_800_000,
            "output_tokens": 521_700,
            "cache_read_tokens": 340_500_000,
        }
        resultat = costs.delta(avant, apres)
        self.assertAlmostEqual(resultat["total_cost_usd"], 11.37)
        self.assertEqual(resultat["input_tokens"], 2_799_900)
        self.assertEqual(resultat["output_tokens"], 521_650)
        self.assertEqual(resultat["cache_read_tokens"], 340_499_990)

    def test_delta_jamais_negatif(self) -> None:
        avant = {
            "total_cost_usd": 5.0,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_tokens": 100,
        }
        apres = {
            "total_cost_usd": 1.0,
            "input_tokens": 10,
            "output_tokens": 0,
            "cache_read_tokens": 5,
        }
        resultat = costs.delta(avant, apres)
        self.assertEqual(resultat["total_cost_usd"], 0.0)
        self.assertEqual(resultat["input_tokens"], 0)
        self.assertEqual(resultat["output_tokens"], 0)
        self.assertEqual(resultat["cache_read_tokens"], 0)

    def test_delta_avec_none(self) -> None:
        apres = {
            "total_cost_usd": 12.37,
            "input_tokens": 2_800_000,
            "output_tokens": 521_700,
            "cache_read_tokens": 340_500_000,
        }
        for avant, suivant in ((None, apres), (apres, None), (None, None)):
            resultat = costs.delta(avant, suivant)
            self.assertEqual(
                resultat,
                {
                    "total_cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                },
            )


class SnapshotTests(unittest.TestCase):
    def test_snapshot_retourne_none_si_commande_absente(self) -> None:
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("opencode absent")
        ):
            self.assertIsNone(costs.snapshot_opencode_stats())

    def test_snapshot_retourne_none_si_timeout(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="opencode stats", timeout=30),
        ):
            self.assertIsNone(costs.snapshot_opencode_stats())

    def test_snapshot_parse_la_sortie(self) -> None:
        acheve = subprocess.CompletedProcess(
            args=["opencode", "stats"], returncode=0, stdout=EXTRAIT, stderr=""
        )
        with mock.patch("subprocess.run", return_value=acheve):
            resultat = costs.snapshot_opencode_stats()
        self.assertIsNotNone(resultat)
        self.assertEqual(resultat["total_cost_usd"], 12.37)
        self.assertEqual(resultat["input_tokens"], 2_800_000)
        self.assertEqual(resultat["output_tokens"], 521_700)
        self.assertEqual(resultat["cache_read_tokens"], 340_500_000)


if __name__ == "__main__":
    unittest.main()
