#!/usr/bin/env python3
"""
Tests du dashboard PMO (tui/pmo_dashboard.py) avec une base SQLite temporaire : aucun réseau.
Vérifie l'affichage de la voie réelle du ticket et la section des derniers événements.
"""
from __future__ import annotations

import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))
sys.path.insert(0, str(COCKPIT_ROOT / "tui"))

import pmo_dashboard  # noqa: E402
import pmo_db  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = pmo_db.CockpitDB(self.base / "pmo.db")
        self.db.register_project(
            "demo",
            "Demo",
            str(self.base / "demo"),
            str(self.base / "sessions"),
            str(self.base / "tickets"),
            milestones=[("M1", "Socle")],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_voie_reelle_du_ticket_dans_la_file(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-A", "Ticket avec voie réelle", milestone_id="M1",
            priority="P1", complexity="S",
        )
        self.db.update_ticket("demo", "T-A", lane="lane_perso")
        self.db.upsert_ticket(
            "demo", "T-B", "Ticket sans voie réelle", milestone_id="M1",
            priority="P3", complexity="L",
        )

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))

        self.assertIn("lane_perso", out, "La voie réelle du ticket doit être affichée")
        rec = self.db.select_optimal_lane("L", "P3", 0)
        self.assertIn(rec["lane"], out, "La voie recommandée doit rester affichée en repli")

    def test_section_derniers_evenements(self) -> None:
        detail_long = "X" * 400
        for idx in range(6):
            self.db.add_event(
                "demo",
                f"T-{idx}",
                f"EV{idx}",
                detail_long if idx == 5 else f"détail {idx}",
            )

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))

        self.assertIn("3. DERNIERS ÉVÉNEMENTS", out)
        for idx in range(1, 6):
            self.assertIn(f"EV{idx}", out)
            self.assertIn(f"T-{idx}", out)
        self.assertNotIn("EV0", out, "Seuls les 5 derniers événements doivent être listés")
        self.assertNotIn(detail_long, out, "Le détail doit être tronqué à la largeur")

    def test_section_evenements_omise_si_vide(self) -> None:
        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        self.assertNotIn("3. DERNIERS ÉVÉNEMENTS", out)

    def test_section_couts_et_runs_du_jour(self) -> None:
        self.db.record_run(
            "demo", "T-A", attempt=1, lane="lane_a", provider="deepseek",
            tokens_in=100, tokens_out=50, cost_eur=0.0123,
        )
        self.db.record_run(
            "demo", "T-B", attempt=1, lane="lane_a", provider="deepseek",
            tokens_in=200, tokens_out=60, cost_eur=0.0456,
        )
        self.db.record_run(
            "demo", "T-A", attempt=1, lane="lane_b", provider="agy",
            tokens_in=400, tokens_out=80, cost_eur=0.5,
        )

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))

        self.assertIn("4. COÛTS ET RUNS DU JOUR", out)
        self.assertIn("deepseek", out)
        self.assertIn("agy", out)
        self.assertIn("2 runs", out, "Le nombre de runs du provider doit être cumulé")
        self.assertIn("1 run", out, "Un provider avec un seul run doit être listé")
        self.assertIn("0.06€", out, "Le coût cumulé doit être arrondi à 2 décimales")
        self.assertIn("0.50€", out)
        self.assertIn("300", out, "Les tokens_in doivent être cumulés")
        self.assertIn("110", out, "Les tokens_out doivent être cumulés")

    def test_section_couts_omise_sans_run_du_jour(self) -> None:
        self.db.record_run(
            "demo", "T-A", attempt=1, lane="lane_a", provider="deepseek",
            tokens_in=100, tokens_out=50, cost_eur=0.5,
            created_at="2020-01-01 00:00:00",
        )

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))

        self.assertNotIn("4. COÛTS ET RUNS DU JOUR", out)
        self.assertNotIn("deepseek", out)

    def test_sous_ticket_affiche_sous_son_parent(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-P", "Parent du chantier", milestone_id="M1",
            priority="P0", complexity="S",
        )
        self.db.upsert_ticket(
            "demo", "T-C", "Sous-ticket délégué", milestone_id="M1",
            priority="P3", complexity="S", parent_id="T-P",
        )

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        lignes = [
            ligne for ligne in out.splitlines()
            if " T-P " in ligne or " T-C " in ligne
        ]
        idx_parent = next(i for i, ligne in enumerate(lignes) if " T-P " in ligne)
        idx_enfant = next(i for i, ligne in enumerate(lignes) if " T-C " in ligne)

        self.assertEqual(idx_enfant, idx_parent + 1, "Le sous-ticket doit suivre son parent")
        self.assertIn("└", lignes[idx_enfant], "Le sous-ticket doit être précédé de « └ »")

    def test_owner_affiche_pour_ticket_assigned(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-O", "Ticket tenu par un chef de chantier", milestone_id="M1",
            priority="P1", complexity="S",
        )
        self.db.update_ticket("demo", "T-O", state="ASSIGNED", owner="ouvrier-42")

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        ligne = next(ligne for ligne in out.splitlines() if " T-O " in ligne)

        self.assertIn("ouvrier-42", ligne, "Le nom du détenteur doit être affiché")

    def test_lane_interactive_affichee_comme_chantier(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-I", "Ticket interactif", milestone_id="M1",
            priority="P1", complexity="S",
        )
        self.db.update_ticket("demo", "T-I", lane="interactive")

        out = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        ligne = next(ligne for ligne in out.splitlines() if " T-I " in ligne)

        self.assertIn("chantier", ligne, "La voie interactive doit s'afficher « chantier »")
        self.assertNotIn("interactive", ligne)

    def test_signature_render_pmo_screen_inchangee(self) -> None:
        params = inspect.signature(pmo_dashboard.render_pmo_screen).parameters
        self.assertEqual(list(params), ["db", "project_id", "width"])
        self.assertEqual(params["width"].default, 105)

    def test_mistral_affiche_forfait_actif_ou_veille(self) -> None:
        out_off = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        self.assertIn("EN VEILLE", out_off)
        self.db.set_mistral_key_active(True)
        out_on = strip_ansi(pmo_dashboard.render_pmo_screen(self.db, "demo", width=105))
        self.assertIn("Mistral : Forfait (ACTIF)", out_on)


if __name__ == "__main__":
    unittest.main()
