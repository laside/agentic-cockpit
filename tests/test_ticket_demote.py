#!/usr/bin/env python3
"""Tests de « cockpit ticket demote <projet> <id>... » (T-054) : READY/BLOCKED -> DRAFT, événement DEMOTED, refus."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import pipeline  # noqa: E402
import pmo_db  # noqa: E402


class TicketDemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.wts = base / "demo-worktrees"
        (base / "demo").mkdir()
        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project("demo", "Demo", str(base / "demo"), str(self.wts), str(self.wts),
                                 milestones=[("M1", "Socle")])
        orig = pipeline.DB_PATH
        pipeline.DB_PATH = base / "pmo.db"
        self.addCleanup(setattr, pipeline, "DB_PATH", orig)

    def ticket(self, tid: str, state: str) -> None:
        self.db.upsert_ticket("demo", tid, f"Titre {tid}", milestone_id="M1", complexity="S", state=state,
                              files_owned=[f"core/{tid}.py"])

    def state(self, tid: str) -> str:
        return self.db.get_ticket("demo", tid)["state"]

    def demote(self, *ids: str) -> tuple[int, str]:
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = pipeline.main(["ticket", "demote", "demo", *ids])
        return code, err.getvalue()

    def events(self, tid: str) -> list[str]:
        with self.db._connect() as conn:
            rows = conn.execute("SELECT kind FROM events WHERE project_id = ? AND ticket_id = ?",
                                ("demo", tid)).fetchall()
        return [r["kind"] for r in rows]

    def test_ready_et_blocked_deviennent_draft(self) -> None:
        for tid, etat in (("T-001", "READY"), ("T-002", "BLOCKED")):
            self.ticket(tid, etat)
            code, _ = self.demote(tid)
            self.assertEqual(code, 0)
            self.assertEqual(self.state(tid), "DRAFT")
            self.assertIn("DEMOTED", self.events(tid))

    def test_plusieurs_ids(self) -> None:
        self.ticket("T-001", "READY")
        self.ticket("T-002", "BLOCKED")
        self.assertEqual(self.demote("T-001", "T-002")[0], 0)
        self.assertEqual((self.state("T-001"), self.state("T-002")), ("DRAFT", "DRAFT"))

    def test_un_refus_annule_tout(self) -> None:
        self.ticket("T-001", "READY")
        self.ticket("T-002", "GATE2")
        code, err = self.demote("T-001", "T-002")
        self.assertEqual(code, 1)
        self.assertIn("REFUS :", err)
        self.assertEqual(self.state("T-001"), "READY")

    def test_autres_etats_refuses(self) -> None:
        for i, etat in enumerate(("DRAFT", "ASSIGNED", "GATE2", "PR_OPEN", "MERGED", "ABANDONED")):
            tid = f"T-1{i:02d}"
            self.ticket(tid, etat)
            code, err = self.demote(tid)
            self.assertEqual(code, 1, etat)
            self.assertIn("REFUS :", err)
            self.assertEqual(self.state(tid), etat)
            self.assertNotIn("DEMOTED", self.events(tid))

    def test_ticket_reserve_refuse(self) -> None:
        self.ticket("T-001", "READY")
        with self.db._connect() as conn:
            conn.execute("UPDATE tickets SET claimed_pid = 999999 WHERE id = 'T-001'")
        code, err = self.demote("T-001")
        self.assertEqual(code, 1)
        self.assertIn("réservé", err)
        self.ticket("T-002", "READY")
        self.db.update_ticket("demo", "T-002", lane="interactive")
        self.assertEqual(self.demote("T-002")[0], 1)
        self.assertEqual((self.state("T-001"), self.state("T-002")), ("READY", "READY"))

    def test_worktree_actif_refuse(self) -> None:
        self.ticket("T-001", "BLOCKED")
        (self.wts / "T-001").mkdir(parents=True)
        code, err = self.demote("T-001")
        self.assertEqual(code, 1)
        self.assertIn("worktree", err)
        self.assertEqual(self.state("T-001"), "BLOCKED")

    def test_ticket_inconnu(self) -> None:
        code, err = self.demote("T-999")
        self.assertEqual(code, 1)
        self.assertIn("REFUS :", err)


if __name__ == "__main__":
    unittest.main()
