"""Tests du Gouverneur de Quotas (select_optimal_lane).

Les tests utilisent une base SQLite temporaire et ne font aucun appel
réseau ni LLM.
"""

import tempfile
import unittest
from pathlib import Path

from core.pmo_db import CockpitDB


class GovernorTests(unittest.TestCase):
    """Vérifie les règles de sélection de voie du Gouverneur."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = CockpitDB(Path(self._tmp.name) / "g.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lane(self, complexity: str, priority: str = "P2", fail_count: int = 0) -> str:
        return self.db.select_optimal_lane(complexity, priority, fail_count=fail_count)["lane"]

    def test_xs_sans_mistral(self) -> None:
        self.assertEqual(self._lane("XS"), "deepseek_coder")

    def test_xs_avec_mistral(self) -> None:
        self.db.set_mistral_key_active(True)
        self.assertEqual(self._lane("XS"), "mistral_vibe")

    def test_s_par_defaut(self) -> None:
        self.assertEqual(self._lane("S"), "deepseek_coder")

    def test_s_avec_mistral(self) -> None:
        self.db.set_mistral_key_active(True)
        self.assertEqual(self._lane("S"), "mistral_vibe")

    def test_s_mistral_actif_deepseek_epuise(self) -> None:
        self.db.set_mistral_key_active(True)
        self.db.update_quota_usage("deepseek", add_spent_eur=19.5)
        self.assertEqual(self._lane("S"), "mistral_vibe")

    def test_mistral_forfait_limite_zero(self) -> None:
        self.db.set_mistral_key_active(True)
        with self.db._connect() as conn:
            conn.execute("UPDATE quota_state SET monthly_limit_eur = 0.0 WHERE provider = 'mistral'")
        self.assertTrue(self.db.is_mistral_ready())
        self.assertEqual(self._lane("XS"), "mistral_vibe")
        self.assertEqual(self._lane("S"), "mistral_vibe")

    def test_m_agy_a_zero(self) -> None:
        self.assertEqual(self._lane("M"), "agy_standard")

    def test_m_agy_a_70_mistral_inactif(self) -> None:
        self.db.update_quota_usage("agy", window_used_pct=70.0)
        self.assertEqual(self._lane("M"), "deepseek_coder")

    def test_m_agy_a_70_mistral_actif(self) -> None:
        self.db.set_mistral_key_active(True)
        self.db.update_quota_usage("agy", window_used_pct=70.0)
        self.assertEqual(self._lane("M"), "mistral_vibe")

    def test_fail_count_deux(self) -> None:
        self.assertEqual(self._lane("M", fail_count=2), "agy_argon")

    def test_complexite_l(self) -> None:
        self.assertEqual(self._lane("L"), "agy_argon")

    def test_p0_agy_a_90_pas_d_argon(self) -> None:
        self.db.update_quota_usage("agy", window_used_pct=90.0)
        self.assertNotEqual(self._lane("M", "P0"), "agy_argon")

    def test_xs_budget_deepseek_epuise(self) -> None:
        self.db.update_quota_usage("deepseek", add_spent_eur=19.0)
        self.assertEqual(self._lane("XS"), "local_nas_coder")


if __name__ == "__main__":
    unittest.main()
