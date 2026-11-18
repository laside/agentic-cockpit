#!/usr/bin/env python3
"""
Tests de l'abandon propre d'un ticket ou d'un chantier (T-030) :
- Abandon d'un ticket simple (état ABANDONED, verrous libérés, événement, worktree et branche absents)
- Abandon d'un chantier avec deux enfants dont un MERGED dans le chantier (ordre enfants puis parent,
  les trois en ABANDONED, branches et worktrees supprimés, verrous libérés, branche principale intacte)
- Refus sur ticket MERGED, déjà ABANDONED, inconnu, ou réservé par un processus vivant (claimed_pid)
- Dépendances : un ticket dépendant d'un ABANDONED reste BLOCKED
- Planification : get_prioritized_queue, dependents_count, plan_batch et build_status ignorent ABANDONED
- Progression : export_markdown_roadmap et render_pmo_screen excluent ABANDONED du dénominateur (1 / 2)
- Sous-tickets : un enfant ABANDONED ne bloque ni la création d'un nouvel enfant (max_children) ni la fusion du parent
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))
sys.path.insert(0, str(COCKPIT_ROOT / "tui"))

import pipeline  # noqa: E402
import pmo_dashboard  # noqa: E402
import pmo_db  # noqa: E402
import scheduler  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def branch_exists(root: Path, branch: str) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


class AbandonTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "demo"
        self.wts = base / "demo-worktrees"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@local")
        (self.root / "Makefile").write_text(
            "lint:\n\t@true\n\ntest:\n\t@python3 -B -m unittest discover -s tests -q 2>&1\n",
            encoding="utf-8",
        )
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_base.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project(
            "demo",
            "Demo",
            str(self.root),
            str(self.wts),
            str(self.wts),
            milestones=[("C3", "Chantiers")],
        )
        self.script = base / "worker.sh"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60, "max_children": 2},
            "runners": {
                name: {"agent": "fake", "provider": "local", "argv": ["bash", str(self.script), "{prompt}"]}
                for name in ("deepseek_coder", "agy_standard", "agy_argon", "mistral_vibe", "local_nas_coder")
            },
            "gate1": {"max_reworks": 1},
        }
        self._orig_runners = pipeline.load_runners
        self._orig_home = pipeline.COCKPIT_HOME
        self._orig_db_path = pipeline.DB_PATH
        pipeline.load_runners = lambda: self.runner_cfg  # type: ignore[assignment]
        self.logs = base / "home"
        (self.logs / "prompts").mkdir(parents=True)
        (self.logs / "prompts" / "sys_coder_tdd.md").write_text("Implémente le brief.", encoding="utf-8")
        (self.logs / "prompts" / "sys_gate1_nas32k.md").write_text("Audite.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.logs
        pipeline.DB_PATH = base / "pmo.db"

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        self.tmp.cleanup()

    def worker(self, body: str) -> None:
        self.script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n", encoding="utf-8")

    def test_abandon_simple_ready_ticket(self) -> None:
        """Abandon d'un ticket simple READY (avec worktree, branche et verrous) : tout est nettoyé."""
        self.db.upsert_ticket(
            "demo",
            "T-010",
            "Ticket simple",
            milestone_id="C3",
            files_owned=["a.py"],
        )
        proj = self.db.get_project("demo")
        assert proj is not None
        wt = pipeline.ensure_worktree(proj, "T-010")
        ok, _ = self.db.acquire_locks("demo", "T-010", ["a.py"])
        self.assertTrue(ok)
        self.assertTrue(wt.exists())
        self.assertTrue(branch_exists(self.root, "feat/T-010"))

        abandoned = pipeline.abandon_ticket(self.db, "demo", "T-010", reason="obsolète")
        self.assertEqual(abandoned, ["T-010"])

        t = self.db.get_ticket("demo", "T-010")
        self.assertIsNotNone(t)
        assert t is not None
        self.assertEqual(t["state"], "ABANDONED")
        self.assertFalse(wt.exists())
        self.assertFalse(branch_exists(self.root, "feat/T-010"))

        with self.db._connect() as conn:
            locks = conn.execute(
                "SELECT * FROM file_locks WHERE project_id = 'demo' AND ticket_id = 'T-010'"
            ).fetchall()
        self.assertEqual(locks, [])

        events = [(e["ticket_id"], e["kind"], e["detail"]) for e in self.db.recent_events("demo", 10)]
        self.assertIn(("T-010", "ABANDONED", "obsolète"), events)

    def test_abandon_chantier_with_two_children_one_merged(self) -> None:
        """Abandon d'un chantier avec 2 enfants dont 1 MERGED dans le chantier : les 3 passent ABANDONED, main intact."""
        main_before = git(self.root, "rev-parse", "main").strip()
        parent_files = ["a.py", "b.py", "tests/test_a.py", "tests/test_b.py"]
        self.db.upsert_ticket(
            "demo",
            "T-200",
            "Chantier parent",
            milestone_id="C3",
            complexity="L",
            files_owned=parent_files,
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-200", "agy"), 0)

        self.db.upsert_ticket(
            "demo",
            "T-201",
            "Enfant 1 (sera MERGED dans le chantier)",
            milestone_id="C3",
            complexity="S",
            files_owned=["a.py", "tests/test_a.py"],
            parent_id="T-200",
        )
        self.db.upsert_ticket(
            "demo",
            "T-202",
            "Enfant 2 (en cours)",
            milestone_id="C3",
            complexity="S",
            files_owned=["b.py", "tests/test_b.py"],
            parent_id="T-200",
        )

        self.worker(
            "echo 'A = 1' > a.py\n"
            "cat > tests/test_a.py << 'EOF'\n"
            "import unittest, a\n"
            "class TestA(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertEqual(a.A, 1)\n"
            "EOF"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-201", use_gate1=False), 0)
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-201"), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-201")["state"], "MERGED")

        self.worker(
            "echo 'B = 2' > b.py\n"
            "cat > tests/test_b.py << 'EOF'\n"
            "import unittest, b\n"
            "class TestB(unittest.TestCase):\n"
            "    def test_b(self):\n"
            "        self.assertEqual(b.B, 2)\n"
            "EOF"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-202", use_gate1=False), 0)
        self.assertTrue((self.wts / "T-202").exists())
        self.assertTrue((self.wts / "T-200").exists())

        abandoned = pipeline.abandon_ticket(self.db, "demo", "T-200")
        self.assertEqual(abandoned, ["T-201", "T-202", "T-200"])

        for tid in ("T-200", "T-201", "T-202"):
            t = self.db.get_ticket("demo", tid)
            self.assertIsNotNone(t)
            assert t is not None
            self.assertEqual(t["state"], "ABANDONED")
            self.assertFalse((self.wts / tid).exists())
            self.assertFalse(branch_exists(self.root, f"feat/{tid}"))

        with self.db._connect() as conn:
            locks = conn.execute("SELECT * FROM file_locks WHERE project_id = 'demo'").fetchall()
        self.assertEqual(locks, [])

        main_after = git(self.root, "rev-parse", "main").strip()
        self.assertEqual(main_before, main_after)
        self.assertFalse((self.root / "a.py").exists())
        self.assertFalse((self.root / "b.py").exists())

        events = [(e["ticket_id"], e["kind"], e["detail"]) for e in self.db.recent_events("demo", 10)]
        self.assertIn(("T-201", "ABANDONED", "abandon demandé"), events)
        self.assertIn(("T-202", "ABANDONED", "abandon demandé"), events)
        self.assertIn(("T-200", "ABANDONED", "abandon demandé"), events)

        # Le chantier abandonné n'apparaît plus dans TRAVAUX EN COURS
        status_text = pipeline.build_status(self.db, "demo")
        self.assertNotIn("TRAVAUX EN COURS", status_text)

    def test_refuse_merged_abandoned_unknown_and_active_pid(self) -> None:
        """Refus sur ticket inconnu, déjà MERGED, déjà ABANDONED, ou réservé par un processus vivant."""
        # Ticket inconnu
        with self.assertRaises(pipeline.PipelineError):
            pipeline.abandon_ticket(self.db, "demo", "T-999")

        # Ticket MERGED
        self.db.upsert_ticket("demo", "T-020", "Déjà fusionné", state="MERGED", files_owned=["a.py"])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.abandon_ticket(self.db, "demo", "T-020")
        self.assertEqual(self.db.get_ticket("demo", "T-020")["state"], "MERGED")

        # Ticket déjà ABANDONED
        self.db.upsert_ticket("demo", "T-021", "À abandonner", files_owned=["b.py"])
        self.assertEqual(pipeline.main(["abandon", "T-021", "demo", "--reason", "test cli"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-021")["state"], "ABANDONED")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.abandon_ticket(self.db, "demo", "T-021")

        # Ticket avec claimed_pid vivant (processus courant)
        self.db.upsert_ticket("demo", "T-022", "En cours par processus actif", files_owned=["c.py"])
        claimed, _ = self.db.claim_ticket("demo", "T-022", os.getpid())
        self.assertTrue(claimed)
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.abandon_ticket(self.db, "demo", "T-022")
        self.assertIn("réservé par un processus actif", str(ctx.exception))
        self.assertEqual(self.db.get_ticket("demo", "T-022")["state"], "ASSIGNED")

    def test_dependent_stays_blocked_and_queue_ignores_abandoned(self) -> None:
        """Un ticket qui dépend d'un ABANDONED reste BLOCKED ; la file et plan_batch ignorent ABANDONED."""
        self.db.upsert_ticket("demo", "T-030", "Socle abandonné", files_owned=["a.py"])
        self.db.upsert_ticket(
            "demo",
            "T-031",
            "Dépendant de T-030",
            files_owned=["b.py"],
            depends_on=["T-030"],
        )
        self.assertEqual(self.db.get_ticket("demo", "T-031")["state"], "BLOCKED")

        pipeline.abandon_ticket(self.db, "demo", "T-030", reason="annulé")
        self.assertEqual(self.db.get_ticket("demo", "T-031")["state"], "BLOCKED")

        # Même si un nouveau ticket dépend d'un ticket déjà ABANDONED, il naît BLOCKED
        self.db.upsert_ticket(
            "demo",
            "T-032",
            "Nouveau dépendant de T-030",
            files_owned=["c.py"],
            depends_on=["T-030"],
        )
        self.assertEqual(self.db.get_ticket("demo", "T-032")["state"], "BLOCKED")

        queue_ids = [t["id"] for t in self.db.get_prioritized_queue("demo")]
        self.assertNotIn("T-030", queue_ids)

        batch = scheduler.plan_batch(self.db, "demo", slots=4)
        self.assertEqual(batch, [])

    def test_milestone_progress_excludes_abandoned_from_denominator(self) -> None:
        """Progression d'un jalon : 1 MERGED, 1 ABANDONED, 1 READY -> 1 / 2 (50.0%)."""
        self.db.upsert_ticket("demo", "T-040", "Fusionné", milestone_id="C3", state="MERGED", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-041", "Abandonné", milestone_id="C3", files_owned=["b.py"])
        self.db.upsert_ticket("demo", "T-042", "Prêt", milestone_id="C3", state="READY", files_owned=["c.py"])
        pipeline.abandon_ticket(self.db, "demo", "T-041")

        roadmap_md = self.db.export_markdown_roadmap("demo", self.root / "docs" / "ROADMAP.md")
        self.assertIn("1 / 2", roadmap_md)
        self.assertIn("50.0%", roadmap_md)

        screen = pmo_dashboard.render_pmo_screen(self.db, "demo")
        self.assertIn("(1/2 fusionnés)", screen)
        self.assertIn("50.0%", screen)

    def test_abandoned_child_does_not_block_new_child_or_parent_merge(self) -> None:
        """La création d'un nouvel enfant n'est pas bloquée par un enfant ABANDONED (max_children=2)."""
        self.db.upsert_ticket(
            "demo",
            "T-050",
            "Chantier parent",
            milestone_id="C3",
            complexity="L",
            files_owned=["a.py", "b.py", "c.py"],
        )
        self.assertEqual(
            pipeline.main(["ticket", "add", "demo", "Enfant 1", "--id", "T-051", "--parent", "T-050", "--files", "a.py"]),
            0,
        )
        self.assertEqual(
            pipeline.main(["ticket", "add", "demo", "Enfant 2", "--id", "T-052", "--parent", "T-050", "--files", "b.py"]),
            0,
        )
        # Limite max_children=2 atteinte
        self.assertEqual(
            pipeline.main(["ticket", "add", "demo", "Enfant 3", "--id", "T-053", "--parent", "T-050", "--files", "c.py"]),
            1,
        )

        # Abandon de T-051 : libère une place sous max_children
        pipeline.abandon_ticket(self.db, "demo", "T-051")
        self.assertEqual(
            pipeline.main(["ticket", "add", "demo", "Enfant 3", "--id", "T-053", "--parent", "T-050", "--files", "c.py"]),
            0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
