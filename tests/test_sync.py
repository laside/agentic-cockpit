#!/usr/bin/env python3
"""
Tests de `cockpit sync` : clôture des tickets `PR_OPEN` dont la PR est fusionnée.

Un dépôt bare local sert d'`origin` et un faux `gh` (COCKPIT_GH) répond à
`gh pr view <branche> --json state,mergedAt`. Aucun réseau, aucun LLM.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import pipeline  # noqa: E402
import pmo_db  # noqa: E402

FAKE_GH = '''#!/usr/bin/env python3
"""Faux gh : journalise les arguments et simule `gh pr view` sans réseau."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

if args[:2] == ["pr", "view"]:
    mode = os.environ.get("GH_MODE", "OPEN")
    if mode == "ERROR":
        sys.exit(1)
    payload = {"state": mode, "mergedAt": "2026-01-01T00:00:00Z" if mode == "MERGED" else None}
    print(json.dumps(payload))
    sys.exit(0)
sys.exit(0)
'''


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.root = base / "demo"
        self.wts = base / "demo-worktrees"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.com")
        (self.root / "a.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.bare = base / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.bare)], check=True)
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-u", "origin", "main")

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project(
            "demo", "Demo", str(self.root), str(self.wts), str(self.wts), milestones=[("M1", "Socle")]
        )

        self.gh = base / "fake_gh.py"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(0o755)
        self.gh_log = base / "gh.log"
        self._env = {
            "COCKPIT_GH": os.environ.get("COCKPIT_GH"),
            "GH_LOG": os.environ.get("GH_LOG"),
            "GH_MODE": os.environ.get("GH_MODE"),
            "COCKPIT_CONFIG_DIR": os.environ.get("COCKPIT_CONFIG_DIR"),
            "PUSH_LOG": os.environ.get("PUSH_LOG"),
        }
        os.environ["COCKPIT_GH"] = str(self.gh)
        os.environ["GH_LOG"] = str(self.gh_log)
        os.environ["GH_MODE"] = "OPEN"
        overlay = base / "config-overlay"
        overlay.mkdir()
        (overlay / "projects.toml").write_text("[projects.demo]\nremote = \"origin\"\n", encoding="utf-8")
        os.environ["COCKPIT_CONFIG_DIR"] = str(overlay)

        self._orig_db_path = pipeline.DB_PATH
        self._orig_home = pipeline.COCKPIT_HOME
        pipeline.DB_PATH = base / "pmo.db"
        pipeline.COCKPIT_HOME = base / "home"

    def tearDown(self) -> None:
        pipeline.DB_PATH = self._orig_db_path
        pipeline.COCKPIT_HOME = self._orig_home
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line]

    def push_merged_commit(self, email: str = "test@example.com") -> None:
        other = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        git(other, "config", "user.name", "Test")
        git(other, "config", "user.email", email)
        (other / "merged.txt").write_text("fusionné\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "commit fusionné")
        git(other, "push", "-q", "origin", "main")

    def prepare_pr_ticket(self, ticket_id: str = "T-100") -> None:
        self.db.upsert_ticket(
            "demo", ticket_id, "CI verte", milestone_id="M1", complexity="S",
            files_owned=["a.txt"], contract="champ imposé : `texte`",
        )
        self.db.update_ticket("demo", ticket_id, state="PR_OPEN")

    def event_kinds(self) -> list[str]:
        return [e["kind"] for e in self.db.recent_events("demo", 50)]

    def test_merged_pr_completes_ticket_and_unblocks_dependents(self) -> None:
        self.prepare_pr_ticket("T-100")
        self.db.upsert_ticket(
            "demo", "T-101", "Dépendant", milestone_id="M1", complexity="S",
            files_owned=["b.txt"], depends_on=["T-100"],
        )
        self.assertEqual(self.db.get_ticket("demo", "T-101")["state"], "BLOCKED")

        git(self.root, "branch", "feat/T-100")
        wt = self.wts / "T-100"
        self.wts.mkdir()
        git(self.root, "worktree", "add", str(wt), "feat/T-100")
        self.push_merged_commit()
        os.environ["GH_MODE"] = "MERGED"

        self.assertEqual(pipeline.sync_project(self.db, "demo"), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "MERGED")
        self.assertIn("MERGED", self.event_kinds())
        self.assertNotIn("PRIVACY_LEAK", self.event_kinds())
        self.assertEqual(self.db.get_ticket("demo", "T-101")["state"], "READY")
        self.assertFalse(wt.exists())
        self.assertNotEqual(
            git(self.root, "rev-parse", "--verify", "--quiet", "feat/T-100", check=False).returncode, 0
        )
        self.assertTrue((self.root / "merged.txt").exists())
        self.assertEqual(git(self.root, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        calls = self.gh_calls()
        self.assertIn(["pr", "view", "feat/T-100", "--json", "state,mergedAt"], calls)

    def test_merged_pr_emits_privacy_leak_on_unauthorized_identity(self) -> None:
        self.prepare_pr_ticket("T-100")
        git(self.root, "branch", "feat/T-100")
        wt = self.wts / "T-100"
        self.wts.mkdir()
        git(self.root, "worktree", "add", str(wt), "feat/T-100")
        self.push_merged_commit(email="secret@internal.corp")
        os.environ["GH_MODE"] = "MERGED"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pipeline.sync_project(self.db, "demo"), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "MERGED")
        self.assertIn("PRIVACY_LEAK", self.event_kinds())
        self.assertIn("MERGED", self.event_kinds())
        self.assertIn("identité de commit non autorisée", out.getvalue())

    def test_sync_refused_when_root_not_on_default_branch(self) -> None:
        self.prepare_pr_ticket()
        git(self.root, "checkout", "-q", "-b", "autre")
        self.push_merged_commit()
        os.environ["GH_MODE"] = "MERGED"

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.sync_project(self.db, "demo")

        self.assertIn("main", str(ctx.exception))
        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "PR_OPEN")
        self.assertNotIn("MERGED", self.event_kinds())

    def test_sync_refused_when_root_has_tracked_changes(self) -> None:
        self.prepare_pr_ticket()
        (self.root / "a.txt").write_text("modifié\n", encoding="utf-8")
        self.push_merged_commit()
        os.environ["GH_MODE"] = "MERGED"

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.sync_project(self.db, "demo")

        self.assertIn("modifications suivies", str(ctx.exception))
        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "PR_OPEN")
        self.assertNotIn("MERGED", self.event_kinds())

    def test_closed_pr_keeps_state_and_asks_for_decision(self) -> None:
        self.prepare_pr_ticket()
        os.environ["GH_MODE"] = "CLOSED"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pipeline.sync_project(self.db, "demo"), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "PR_OPEN")
        self.assertIn("PR_CLOSED", self.event_kinds())
        self.assertNotIn("MERGED", self.event_kinds())
        self.assertTrue("relancer" in out.getvalue() or "abandonner" in out.getvalue())

    def test_open_pr_is_ignored(self) -> None:
        self.prepare_pr_ticket()
        os.environ["GH_MODE"] = "OPEN"

        self.assertEqual(pipeline.sync_project(self.db, "demo"), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "PR_OPEN")
        self.assertNotIn("MERGED", self.event_kinds())
        self.assertNotIn("PR_CLOSED", self.event_kinds())

    def test_gh_error_changes_nothing(self) -> None:
        self.prepare_pr_ticket()
        os.environ["GH_MODE"] = "ERROR"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pipeline.sync_project(self.db, "demo")

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "PR_OPEN")
        self.assertNotIn("MERGED", self.event_kinds())
        self.assertNotIn("PR_CLOSED", self.event_kinds())
        self.assertNotEqual(out.getvalue().strip(), "")

    def test_status_syncs_first(self) -> None:
        self.prepare_pr_ticket()
        self.push_merged_commit()
        os.environ["GH_MODE"] = "MERGED"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pipeline.main(["status", "demo"]), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-100")["state"], "MERGED")
        self.assertIn("T-100", out.getvalue())

    def test_status_without_pr_open_calls_no_gh(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-102", "Prêt", milestone_id="M1", complexity="S", files_owned=["a.txt"]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(["status", "demo"]), 0)

        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
