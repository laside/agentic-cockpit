#!/usr/bin/env python3
"""Tests TDD du ticket T-041 : commande `cockpit brief <projet>` (0 token, sans réseau)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import pipeline  # noqa: E402
from pmo_db import CockpitDB  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


class TestBrief(unittest.TestCase):
    """Couvre le contrat T-041 : projet vide, PR_OPEN + READY, borne de 30 lignes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "test_pmo.db"
        self.db = CockpitDB(self.db_path)
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / "a.py").write_text("def fa() -> int:\n    return 1\n", encoding="utf-8")
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@cockpit.local")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "init")

        self.proj_id = "demo"
        self.db.register_project(
            self.proj_id,
            "Projet Démo",
            str(self.repo),
            str(self.base / "wt_sessions"),
            str(self.base / "wt_tickets"),
            milestones=[("M1", "Socle")],
        )
        self._old_db_path = pipeline.DB_PATH
        pipeline.DB_PATH = self.db_path

    def tearDown(self) -> None:
        pipeline.DB_PATH = self._old_db_path
        self.tmp.cleanup()

    def test_1_projet_vide_texte_court_sans_erreur(self) -> None:
        """Projet sans ticket ni événement : brief lisible, borné, sans lever d'exception."""
        out = pipeline.brief_project(self.db, self.proj_id)
        self.assertIsInstance(out, str)
        self.assertLessEqual(len(out.splitlines()), 30)
        self.assertIn("branche main", out)
        self.assertIn("Dernier commit", out)
        self.assertIn("PROCHAIN : aucun", out)
        self.assertIn("COÛT DU JOUR : 0.0000 € (0 run(s))", out)
        for state in ("PR_OPEN", "GATE2", "ASSIGNED", "READY"):
            self.assertIn(state, out)
        self.assertEqual(pipeline.main(["brief", self.proj_id]), 0)

    def test_2_pr_open_ready_etat_reel_sans_reseau(self) -> None:
        """PR_OPEN et READY listés, prochain ticket, 3 derniers événements, coût du jour et écart local."""
        self.db.upsert_ticket(
            self.proj_id, "T-001", "Ticket en revue", milestone_id="M1", state="PR_OPEN", files_owned=["a.py"]
        )
        self.db.upsert_ticket(
            self.proj_id, "T-002", "Ticket prêt prioritaire", milestone_id="M1", priority="P0",
            state="READY", files_owned=["b.py"],
        )
        self.db.upsert_ticket(
            self.proj_id, "T-003", "Ticket assigné", milestone_id="M1", state="ASSIGNED",
            owner="agy", files_owned=["c.py"],
        )
        self.db.upsert_ticket(
            self.proj_id, "T-004", "Ticket en attente de revue", milestone_id="M1", state="GATE2",
            files_owned=["d.py"],
        )

        for idx in range(1, 5):
            self.db.add_event(self.proj_id, "T-003", "NOTE", f"événement numéro {idx}")
        self.db.record_run(self.proj_id, "T-002", attempt=1, lane="agy_standard", cost_eur=0.5)
        self.db.record_run(
            self.proj_id, "T-002", attempt=2, lane="agy_standard", cost_eur=9.0,
            created_at="2000-01-01 00:00:00",
        )

        (self.repo / "b.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "second")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")

        orig_sh = pipeline.sh
        calls: list[list[str]] = []

        def spy(args: list[str], cwd: Path, check: bool = True, timeout: int = 120):
            calls.append(list(args))
            return orig_sh(args, cwd, check=check, timeout=timeout)

        pipeline.sh = spy  # type: ignore[assignment]
        try:
            out = pipeline.brief_project(self.db, self.proj_id)
        finally:
            pipeline.sh = orig_sh  # type: ignore[assignment]

        lines = out.splitlines()
        self.assertLessEqual(len(lines), 30)
        self.assertIn("T-001", next(line for line in lines if line.startswith("PR_OPEN")))
        self.assertIn("T-004", next(line for line in lines if line.startswith("GATE2")))
        self.assertIn("T-003", next(line for line in lines if line.startswith("ASSIGNED")))
        self.assertIn("T-002", next(line for line in lines if line.startswith("READY")))
        head = self.db.get_prioritized_queue(self.proj_id)[0]["id"]
        self.assertIn(f"PROCHAIN : {head}", out)
        self.assertIn("COÛT DU JOUR : 0.5000 € (1 run(s))", out)
        self.assertIn("origin/main", out)
        self.assertIn("événement numéro 2", out)
        self.assertIn("événement numéro 3", out)
        self.assertIn("événement numéro 4", out)
        self.assertNotIn("événement numéro 1", out)
        self.assertFalse(any("fetch" in a or (a and Path(a[0]).name == "gh") for a in calls))

    def test_3_borne_stricte_30_lignes(self) -> None:
        """Même avec beaucoup de tickets et d'événements, la sortie reste à 30 lignes maximum."""
        states = ("READY", "ASSIGNED", "GATE2", "PR_OPEN", "GATE0", "VALIDATED")
        for i in range(40):
            state = states[i % len(states)]
            self.db.upsert_ticket(
                self.proj_id, f"T-{i:03d}", f"Ticket {i}", state=state, files_owned=[f"f{i}.py"]
            )
        for i in range(10):
            self.db.add_event(self.proj_id, f"T-{i:03d}", "WORKER_START", f"démarrage {i}")

        out = pipeline.brief_project(self.db, self.proj_id)
        self.assertLessEqual(len(out.splitlines()), 30)
        self.assertIn("TICKETS :", out)


if __name__ == "__main__":
    unittest.main()
