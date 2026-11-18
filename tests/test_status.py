#!/usr/bin/env python3
"""Tests TDD du ticket T-020 : cockpit status / note et section « Travaux voisins » du brief."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import pipeline  # noqa: E402
from pmo_db import CockpitDB  # noqa: E402


class TestStatusAndNeighbors(unittest.TestCase):
    """Couvre les 7 cas du contrat T-020 sur base et dépôt temporaires, sans réseau."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "test_pmo.db"
        self.db = CockpitDB(self.db_path)
        self.repo = self.base / "repo"
        self.wt_sessions = self.base / "wt_sessions"
        self.wt_tickets = self.base / "wt_tickets"
        self.repo.mkdir(parents=True)
        (self.repo / ".nexus" / "rules").mkdir(parents=True)
        (self.repo / ".nexus" / "rules" / "core.md").write_text("- Règle test.", encoding="utf-8")
        (self.repo / "a.py").write_text("def fa() -> int:\n    return 1\n", encoding="utf-8")

        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@cockpit.local"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

        self.proj_id = "demo"
        self.db.register_project(
            self.proj_id,
            "Projet Démo",
            str(self.repo),
            str(self.wt_sessions),
            str(self.wt_tickets),
        )
        self._old_db_path = pipeline.DB_PATH
        pipeline.DB_PATH = self.db_path

    def tearDown(self) -> None:
        pipeline.DB_PATH = self._old_db_path
        self.tmp.cleanup()

    def test_1_status_chantier_sous_tickets_ready_et_borne_40_lignes(self) -> None:
        """status avec un chantier pris, deux sous-tickets, un ticket READY, verrous et <= 40 lignes même avec 30 actifs."""
        chantier_files = [f"mod_{i}.py" for i in range(1, 9)]
        self.db.upsert_ticket(
            self.proj_id,
            "T-010",
            "Chantier principal très important pour l'architecture globale du projet",
            state="READY",
            files_owned=chantier_files,
        )
        pipeline.claim_ticket(self.db, self.proj_id, "T-010", "claude")

        self.db.upsert_ticket(
            self.proj_id,
            "T-011",
            "Premier sous-ticket du chantier",
            state="ASSIGNED",
            owner="agy",
            files_owned=["mod_1.py"],
            parent_id="T-010",
        )
        self.db.update_ticket(self.proj_id, "T-011", lane="agy_standard")
        self.db.acquire_locks(self.proj_id, "T-011", ["mod_1.py"])

        self.db.upsert_ticket(
            self.proj_id,
            "T-012",
            "Deuxième sous-ticket validé",
            state="VALIDATED",
            owner="deepseek",
            files_owned=["mod_2.py"],
            parent_id="T-010",
        )
        self.db.update_ticket(self.proj_id, "T-012", lane="deepseek_coder")

        self.db.upsert_ticket(
            self.proj_id,
            "T-020",
            "Ticket prêt en attente",
            state="READY",
            files_owned=["other.py"],
        )

        out = pipeline.build_status(self.db, self.proj_id)
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 40)

        # Ordre des sections
        idx_travaux = out.index("TRAVAUX EN COURS")
        idx_verrous = out.index("FICHIERS VERROUILLÉS")
        idx_prets = out.index("PRÊTS")
        idx_events = out.index("DERNIERS ÉVÉNEMENTS")
        self.assertLess(idx_travaux, idx_verrous)
        self.assertLess(idx_verrous, idx_prets)
        self.assertLess(idx_prets, idx_events)

        # Chantier et sous-tickets indentés de deux espaces
        self.assertTrue(any(line.startswith("T-010 ") and "claude" in line and "interactive" in line for line in lines))
        self.assertTrue(any(line.startswith("  T-011 ") and "ASSIGNED" in line and "agy" in line for line in lines))
        self.assertTrue(any(line.startswith("  T-012 ") and "VALIDATED" in line and "deepseek" in line for line in lines))

        # Verrous : T-010 détient 7 fichiers restants -> 6 fichiers au plus puis +1
        lock_lines = [line for line in lines if line.startswith("T-010 :")]
        self.assertEqual(len(lock_lines), 1)
        self.assertIn("+1", lock_lines[0])

        # Ticket READY présent dans PRÊTS
        prets_idx = lines.index("PRÊTS")
        self.assertIn("T-020", lines[prets_idx + 1])

        # Test de la borne stricte <= 40 lignes même avec 30 tickets actifs
        for i in range(100, 130):
            tid = f"T-{i}"
            self.db.upsert_ticket(
                self.proj_id,
                tid,
                f"Ticket actif numéro {i}",
                state="ASSIGNED",
                owner="agy",
                files_owned=[f"f_{i}.py"],
            )
            self.db.acquire_locks(self.proj_id, tid, [f"f_{i}.py"])
            self.db.add_event(self.proj_id, tid, "WORKER_START", f"démarrage {tid}")

        out_30 = pipeline.build_status(self.db, self.proj_id)
        self.assertLessEqual(len(out_30.splitlines()), 40)
        self.assertIn("TRAVAUX EN COURS", out_30)
        self.assertIn("FICHIERS VERROUILLÉS", out_30)
        self.assertIn("PRÊTS", out_30)
        self.assertIn("DERNIERS ÉVÉNEMENTS", out_30)

    def test_2_projet_sans_activite_texte_court_sans_erreur(self) -> None:
        """Projet sans activité -> texte court sans erreur et sections vides omises."""
        out = pipeline.build_status(self.db, self.proj_id)
        self.assertIsInstance(out, str)
        self.assertLessEqual(len(out.splitlines()), 5)
        self.assertNotIn("TRAVAUX EN COURS", out)
        self.assertNotIn("FICHIERS VERROUILLÉS", out)
        self.assertNotIn("PRÊTS", out)
        self.assertNotIn("DERNIERS ÉVÉNEMENTS", out)

    def test_3_note_enregistree_et_visible_dans_status(self) -> None:
        """cockpit note T-xxx <projet> 'texte' enregistre l'événement NOTE (tronqué à 200 car.) visible dans status."""
        self.db.upsert_ticket(self.proj_id, "T-001", "Ticket A", files_owned=["a.py"])
        rc = pipeline.main(["note", "T-001", self.proj_id, "signature de parse() changée"])
        self.assertEqual(rc, 0)

        events = self.db.recent_events(self.proj_id, limit=5)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "NOTE")
        self.assertEqual(events[0]["ticket_id"], "T-001")
        self.assertEqual(events[0]["detail"], "signature de parse() changée")

        out = pipeline.build_status(self.db, self.proj_id)
        self.assertIn("DERNIERS ÉVÉNEMENTS", out)
        self.assertIn("T-001", out)
        self.assertIn("NOTE", out)
        self.assertIn("signature de parse() changée", out)

    def test_4_note_vide_ou_ticket_inconnu_refusee(self) -> None:
        """cockpit note refuse un texte vide ou un ticket inconnu."""
        self.db.upsert_ticket(self.proj_id, "T-001", "Ticket A", files_owned=["a.py"])
        self.assertEqual(pipeline.main(["note", "T-001", self.proj_id, "   "]), 1)
        self.assertEqual(pipeline.main(["note", "T-999", self.proj_id, "note valide"]), 1)
        self.assertEqual(len(self.db.recent_events(self.proj_id)), 0)

    def test_5_brief_sous_ticket_contient_frere_parent_readonly_et_notes(self) -> None:
        """Brief d'un sous-ticket contient son frère, son parent, un ticket touchant ses readonly et les 3 dernières NOTE."""
        self.db.upsert_ticket(
            self.proj_id,
            "T-100",
            "Chantier parent",
            state="ASSIGNED",
            owner="claude",
            files_owned=["a.py", "b.py", "c.py"],
        )
        self.db.upsert_ticket(
            self.proj_id,
            "T-101",
            "Sous-ticket courant",
            state="ASSIGNED",
            owner="agy",
            files_owned=["b.py"],
            files_readonly=["a.py", "shared.py"],
            parent_id="T-100",
        )
        self.db.upsert_ticket(
            self.proj_id,
            "T-102",
            "Sous-ticket frère",
            state="GATE0",
            owner="deepseek",
            files_owned=["c.py"],
            parent_id="T-100",
        )
        self.db.upsert_ticket(
            self.proj_id,
            "T-200",
            "Ticket voisin par fichier readonly",
            state="VALIDATED",
            owner="mistral",
            files_owned=["shared.py", "x1.py", "x2.py", "x3.py", "x4.py"],
        )
        for idx in range(1, 5):
            self.db.add_event(self.proj_id, "T-102", "NOTE", f"Note frère {idx}")

        t_cur = self.db.get_ticket(self.proj_id, "T-101")
        assert t_cur is not None
        wt = self.base / "wt_101"
        wt.mkdir(parents=True, exist_ok=True)
        brief_path = pipeline.write_brief(t_cur, wt, self.db)
        content = brief_path.read_text(encoding="utf-8")

        self.assertIn("## Travaux voisins (ne pas modifier leurs fichiers)", content)
        sec = content.split("## Travaux voisins (ne pas modifier leurs fichiers)", 1)[1]
        self.assertIn("T-100", sec)
        self.assertIn("T-102", sec)
        self.assertIn("T-200", sec)
        self.assertNotIn("T-101", sec)
        # Seules les 3 dernières NOTE (2, 3, 4) figurent
        self.assertNotIn("Note frère 1", sec)
        self.assertIn("Note frère 2", sec)
        self.assertIn("Note frère 3", sec)
        self.assertIn("Note frère 4", sec)

    def test_6_brief_sans_voisin_omet_la_section(self) -> None:
        """Brief avec db fourni mais aucun voisin actif n'inclut pas la section Travaux voisins."""
        self.db.upsert_ticket(
            self.proj_id,
            "T-300",
            "Ticket isolé",
            state="ASSIGNED",
            owner="claude",
            files_owned=["solo.py"],
        )
        # Un autre ticket mais seulement à l'état READY (non actif)
        self.db.upsert_ticket(
            self.proj_id,
            "T-301",
            "Ticket non démarré",
            state="READY",
            files_owned=["other.py"],
        )
        t_cur = self.db.get_ticket(self.proj_id, "T-300")
        assert t_cur is not None
        wt = self.base / "wt_300"
        wt.mkdir(parents=True, exist_ok=True)
        brief_path = pipeline.write_brief(t_cur, wt, self.db)
        content = brief_path.read_text(encoding="utf-8")
        self.assertNotIn("## Travaux voisins", content)

    def test_7_write_brief_sans_db_inchange(self) -> None:
        """write_brief(ticket, wt) sans db produit exactement le même brief qu'avant."""
        self.db.upsert_ticket(
            self.proj_id,
            "T-400",
            "Chantier parent",
            state="ASSIGNED",
            owner="claude",
            files_owned=["a.py", "b.py"],
        )
        self.db.upsert_ticket(
            self.proj_id,
            "T-401",
            "Sous-ticket avec parent actif",
            state="ASSIGNED",
            owner="agy",
            files_owned=["b.py"],
            parent_id="T-400",
        )
        t_cur = self.db.get_ticket(self.proj_id, "T-401")
        assert t_cur is not None
        wt = self.base / "wt_401"
        wt.mkdir(parents=True, exist_ok=True)
        brief_path = pipeline.write_brief(t_cur, wt)
        content = brief_path.read_text(encoding="utf-8")
        expected = pipeline.compile_ticket_brief(
            wt, t_cur, wt / ".nexus" / "rules", json.loads(t_cur.get("readonly_json") or "[]") or None
        )
        self.assertEqual(content, expected)
        self.assertNotIn("## Travaux voisins", content)


if __name__ == "__main__":
    unittest.main()
