#!/usr/bin/env python3
"""
Tests du mode chantier interactif (claim / submit / release) dans core/pipeline.py :
aucun LLM, aucun réseau, base et dépôt git temporaires.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import pipeline  # noqa: E402
import pmo_db  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


class InteractiveTests(unittest.TestCase):
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
            "demo", "Demo", str(self.root), str(self.wts), str(self.wts), milestones=[("M1", "Socle")]
        )
        self.script = base / "worker.sh"
        self.script.write_text("#!/usr/bin/env bash\nset -e\necho 'X = 1' > a.py\n", encoding="utf-8")
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60, "require_tests": True},
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

    def test_claim_creates_worktree_brief_locks_and_prints_worktree(self) -> None:
        """claim crée worktree, brief et verrous et affiche WORKTREE=."""
        self.db.upsert_ticket(
            "demo", "T-001", "Chantier A", milestone_id="M1", complexity="L", files_owned=["a.py", "tests/test_a.py"]
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pipeline.main(["claim", "T-001", "demo", "--as", "agy"])
        out = buf.getvalue()
        self.assertEqual(code, 0)

        wt = self.wts / "T-001"
        self.assertTrue(wt.exists())
        self.assertTrue((wt / ".nexus_brief.md").exists())
        self.assertIn(".nexus_brief.md", out)
        last_line = [line for line in out.strip().splitlines() if line.strip()][-1]
        self.assertEqual(last_line, f"WORKTREE={wt.resolve()}")

        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t["owner"], "agy")
        self.assertEqual(t["lane"], "interactive")
        self.assertIsNone(t["claimed_pid"])

        ok, conflicts = self.db.acquire_locks("demo", "T-002", ["a.py"])
        self.assertFalse(ok)
        self.assertTrue(any("T-001" in c for c in conflicts))
        self.assertIn("CLAIMED", [e["kind"] for e in self.db.recent_events("demo", 10)])

    def test_second_claim_is_refused(self) -> None:
        """second claim refusé (état non READY ou conflit de verrous)."""
        self.db.upsert_ticket("demo", "T-001", "Chantier A", milestone_id="M1", complexity="L", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-002", "Conflit fichier", milestone_id="M1", complexity="S", files_owned=["a.py"])

        self.assertEqual(pipeline.main(["claim", "T-001", "demo", "--as", "agy"]), 0)
        # Même ticket déjà ASSIGNED
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code_same = pipeline.main(["claim", "T-001", "demo", "--as", "deepseek"])
        self.assertEqual(code_same, 1)
        # Autre ticket READY mais fichier verrouillé par T-001
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            code_conflict = pipeline.main(["claim", "T-002", "demo", "--as", "claude"])
        self.assertEqual(code_conflict, 1)
        self.assertEqual(self.db.get_ticket("demo", "T-002")["state"], "READY")

    def test_run_refused_on_interactive_ticket(self) -> None:
        """cockpit run refusé sur un ticket interactif."""
        self.db.upsert_ticket("demo", "T-001", "Chantier A", milestone_id="M1", complexity="L", files_owned=["a.py"])
        self.assertEqual(pipeline.main(["claim", "T-001", "demo", "--as", "agy"]), 0)

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_ticket(self.db, "demo", "T-001", use_gate1=False)
        self.assertIn("interactif", str(ctx.exception))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["run", "T-001", "demo", "--no-gate1"])
        self.assertEqual(code, 1)
        self.assertIn("interactif", err.getvalue())

    def test_submit_with_in_scope_file_and_test_passes_to_gate2_and_commits(self) -> None:
        """submit avec fichier du périmètre + test -> GATE2 et commit (et enregistre un run interactive)."""
        self.db.upsert_ticket(
            "demo",
            "T-001",
            "Chantier A",
            milestone_id="M1",
            complexity="L",
            files_owned=["a.py", "tests/test_a.py"],
        )
        self.assertEqual(pipeline.main(["claim", "T-001", "demo", "--as", "agy"]), 0)
        wt = self.wts / "T-001"
        (wt / "a.py").write_text("VAL = 42\n", encoding="utf-8")
        # Simule un premier commit déjà fait par le CLI dans la branche + un fichier de test non commité
        git(wt, "add", "a.py")
        git(wt, "commit", "-q", "-m", "wip: module a")
        (wt / "tests" / "test_a.py").write_text(
            "import unittest, a\n\nclass TA(unittest.TestCase):\n    def test_val(self):\n        self.assertEqual(a.VAL, 42)\n",
            encoding="utf-8",
        )

        code = pipeline.main(["submit", "T-001", "demo"])
        self.assertEqual(code, 0)

        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "GATE2")
        self.assertEqual(t["fail_count"], 0)
        self.assertIn("feat(T-001)", git(wt, "log", "--oneline", "-1"))
        self.assertEqual(git(wt, "status", "--porcelain", "--untracked-files=no").strip(), "")
        self.assertIn("SUBMITTED", [e["kind"] for e in self.db.recent_events("demo", 10)])

        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT lane, gate0 FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-001'"
            ).fetchone()
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["lane"], "interactive")
        self.assertEqual(run_row["gate0"], "PASS")

    def test_submit_with_out_of_scope_file_fails_and_keeps_assigned(self) -> None:
        """submit avec fichier hors périmètre -> code 1, ticket toujours ASSIGNED, fail_count 1."""
        self.db.upsert_ticket(
            "demo",
            "T-001",
            "Chantier A",
            milestone_id="M1",
            complexity="L",
            files_owned=["a.py", "tests/test_a.py"],
        )
        self.assertEqual(pipeline.main(["claim", "T-001", "demo", "--as", "deepseek"]), 0)
        wt = self.wts / "T-001"
        (wt / "a.py").write_text("VAL = 1\n", encoding="utf-8")
        (wt / "intrus.py").write_text("INTRUS = True\n", encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pipeline.main(["submit", "T-001", "demo"])
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("intrus.py", out)

        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t["lane"], "interactive")
        self.assertEqual(t["fail_count"], 1)

    def test_release_restores_ready_and_releases_locks(self) -> None:
        """release rend le ticket READY et libère les verrous (en conservant le worktree)."""
        self.db.upsert_ticket("demo", "T-001", "Chantier A", milestone_id="M1", complexity="L", files_owned=["a.py"])
        self.assertEqual(pipeline.main(["claim", "T-001", "demo", "--as", "claude"]), 0)
        wt = self.wts / "T-001"
        self.assertTrue(wt.exists())

        code = pipeline.main(["release", "T-001", "demo"])
        self.assertEqual(code, 0)

        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["owner"], "auto")
        self.assertEqual(t["lane"], "")
        self.assertTrue(wt.exists())

        ok, conflicts = self.db.acquire_locks("demo", "T-002", ["a.py"])
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])
        self.assertIn("RELEASED", [e["kind"] for e in self.db.recent_events("demo", 10)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
