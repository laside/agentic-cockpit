#!/usr/bin/env python3
"""Tests du ticket T-053 : modèle de l'onglet Commande (lecture seule, filtres, tri, détail, worktree)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

from pmo_db import CockpitDB  # noqa: E402
from tui.commande import model  # noqa: E402


class TestCommandeModel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = CockpitDB(self.base / "db" / "t.db")
        for tid, prio, etat in [
            ("T-003", "P2", "READY"), ("T-001", "P1", "MERGED"), ("T-002", "P0", "GATE2"),
            ("T-004", "P1", "ASSIGNED"), ("T-005", "P1", "PR_OPEN"), ("T-006", "P3", "DRAFT"),
            ("T-007", "P1", "READY"), ("T-008", "P1", "ABANDONED"), ("T-009", "P2", "BLOCKED"),
            ("T-010", "P2", "GATE0"),
        ]:
            self.db.upsert_ticket("demo", tid, f"Titre {tid}", priority=prio, state=etat,
                                  contract="x" * 2000, files_owned=["a.py"], acceptance=["ok"])
        self.m = model.CommandeModel(self.base / "db" / "t.db", home=self.base)

    def ids(self, filtre: str) -> list[str]:
        return [t["id"] for t in self.m.list_tickets("demo", filtre)]

    def test_filtres(self) -> None:
        self.assertEqual(len(self.ids("Tous")), 10)
        self.assertEqual(set(self.ids("À faire")), {"T-003", "T-006", "T-007", "T-009"})
        self.assertEqual(set(self.ids("En cours")), {"T-004", "T-010"})
        self.assertEqual(self.ids("À relire"), ["T-002"])
        self.assertEqual(self.ids("PR"), ["T-005"])
        self.assertEqual(set(self.ids("Terminés")), {"T-001", "T-008"})

    def test_tri_actifs_puis_priorite_puis_id(self) -> None:
        ids = self.ids("Tous")
        self.assertEqual(ids[:3], ["T-002", "T-004", "T-005"])
        self.assertEqual(ids[-2:], ["T-001", "T-008"])
        self.assertLess(ids.index("T-007"), ids.index("T-003"))

    def test_detail(self) -> None:
        d = self.m.ticket_detail("demo", "T-002")
        self.assertEqual(d["etat"], "GATE2")
        self.assertEqual(d["priorite"], "P0")
        self.assertEqual(len(d["contrat"]), 1500)
        self.assertEqual(d["fichiers"], ["a.py"])
        self.assertIsNone(self.m.ticket_detail("demo", "T-999"))

    def test_detail_limites_runs_et_evenements(self) -> None:
        for i in range(12):
            self.db.record_run("demo", "T-002", attempt=i, lane="l")
        for i in range(20):
            self.db.add_event("demo", "T-002", "K", str(i))
        d = self.m.ticket_detail("demo", "T-002")
        self.assertEqual(len(d["runs"]), 10)
        self.assertEqual(len(d["evenements"]), 15)

    def test_lecture_seule(self) -> None:
        conn = self.m._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE tickets SET state = 'DRAFT'")
        finally:
            conn.close()

    def test_voies_et_projets(self) -> None:
        cfg = self.base / "cfg"
        cfg.mkdir()
        (cfg / "runners.toml").write_text(
            '[runners.alpha]\nargv = ["x"]\n[runners.beta]\nargv = ["y"]\n', encoding="utf-8")
        (cfg / "projects.toml").write_text('[projects.demo]\nroot = "/tmp/x"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(cfg)}):
            self.assertEqual(self.m.voies(), ["alpha", "beta"])
            self.assertIn("demo", self.m.projets())

    def test_worktree_of(self) -> None:
        repo = self.base / "repo"
        repo.mkdir()

        def git(*a: str) -> None:
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

        git("init", "-b", "main")
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-m", "i")
        wt = self.base / "wts" / "T-002"
        git("worktree", "add", "-b", "feat/T-002", str(wt))
        cfg = self.base / "cfg"
        cfg.mkdir()
        (cfg / "projects.toml").write_text(f'[projects.demo]\nroot = "{repo}"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(cfg)}):
            self.assertEqual(Path(self.m.worktree_of("demo", "T-002")).resolve(), wt.resolve())
            self.assertIsNone(self.m.worktree_of("demo", "T-003"))


if __name__ == "__main__":
    unittest.main()
