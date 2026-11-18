#!/usr/bin/env python3
"""
Tests du pipeline (core/pipeline.py) avec un faux worker : aucun LLM, aucun réseau.
Vérifie : exécution nominale, refus hors périmètre, relance sur échec de test, contrat dans le brief,
porte d'intégration et fusion avec déblocage des dépendants.
"""
from __future__ import annotations

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


class PipelineTests(unittest.TestCase):
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
            "lint:\n\t@true\n\ntest:\n\t@touch .artefact-de-test\n\t@python3 -B -m unittest discover -s tests -q 2>&1\n", encoding="utf-8"
        )
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_base.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project("demo", "Demo", str(self.root), str(self.wts), str(self.wts),
                                 milestones=[("M1", "Socle")])
        self.script = base / "worker.sh"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60},
            "runners": {name: {"agent": "fake", "provider": "local", "argv": ["bash", str(self.script), "{prompt}"]}
                        for name in ("deepseek_coder", "agy_standard", "agy_argon", "mistral_vibe", "local_nas_coder")},
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

    def test_nominal_run_then_merge_unblocks_dependents(self) -> None:
        self.db.upsert_ticket("demo", "T-001", "Module a", milestone_id="M1", complexity="S",
                              files_owned=["a.py"], contract="champ imposé : `texte`")
        self.db.upsert_ticket("demo", "T-002", "Dépend de a", milestone_id="M1", complexity="S",
                              files_owned=["b.py"], depends_on=["T-001"])
        self.assertEqual(self.db.get_ticket("demo", "T-002")["state"], "BLOCKED")
        self.worker("echo 'X = 1' > a.py")

        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-001", use_gate1=False), 0)
        t = self.db.get_ticket("demo", "T-001")
        # Sans Porte 1, le ticket attend la revue de l'architecte (GATE2) et porte sa voie.
        self.assertEqual(t["state"], "GATE2")
        self.assertEqual(t["lane"], "deepseek_coder")
        self.assertEqual(t["owner"], "fake")
        brief = (self.wts / "T-001" / ".nexus_brief.md").read_text(encoding="utf-8")
        self.assertIn("Contrat d'interface", brief)
        self.assertIn("champ imposé : `texte`", brief)
        self.assertIn("feat(T-001)", git(self.wts / "T-001", "log", "--oneline", "-1"))
        self.assertNotIn(".nexus_brief.md", git(self.wts / "T-001", "show", "--stat", "HEAD"))

        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-001")  # GATE2 sans --approve
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-001", approve=True), 0)
        self.assertTrue((self.root / "a.py").exists())
        self.assertFalse((self.wts / "T-001").exists())
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "MERGED")
        self.assertEqual(self.db.get_ticket("demo", "T-002")["state"], "READY")
        self.assertEqual(git(self.root, "status", "--porcelain").strip(), "")

    def test_out_of_scope_then_escalation(self) -> None:
        self.db.upsert_ticket("demo", "T-003", "Périmètre", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.worker("echo 'X = 1' > a.py; echo 'pirate' > autre.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-003", max_attempts=2, use_gate1=False), 1)
        t = self.db.get_ticket("demo", "T-003")
        self.assertEqual(t["state"], "GATE2")
        self.assertEqual(t["fail_count"], 2)
        kinds = [e["kind"] for e in self.db.recent_events("demo", 20)]
        self.assertIn("GATE0_FAIL", kinds)
        self.assertIn("ESCALATED", kinds)

    def test_failing_test_feedback_reaches_second_attempt(self) -> None:
        self.db.upsert_ticket("demo", "T-004", "Test rouge puis vert", milestone_id="M1", complexity="S",
                              files_owned=["tests/test_x.py"])
        # 1re tentative : test faux. 2e tentative (le prompt contient le retour de la Porte 0) : test corrigé.
        self.worker(
            'if echo "$1" | grep -q "refusée"; then V=2; else V=3; fi\n'
            "cat > tests/test_x.py << EOF\n"
            "import unittest\n\nclass X(unittest.TestCase):\n    def test_two(self):\n        self.assertEqual(1 + 1, $V)\n"
            "EOF"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-004", use_gate1=False), 0)
        t = self.db.get_ticket("demo", "T-004")
        self.assertEqual(t["fail_count"], 1)
        self.assertEqual(t["state"], "GATE2")

    def test_secret_file_stops_immediately(self) -> None:
        self.db.upsert_ticket("demo", "T-005", "Secret", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.worker("echo 'X = 1' > a.py; echo 'K=1' > .env")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-005", use_gate1=False), 2)
        self.assertIn("GATE0_SECRET", [e["kind"] for e in self.db.recent_events("demo", 20)])

    def test_worker_runs_in_ticket_worktree_even_if_pwd_differs(self) -> None:
        self.db.upsert_ticket("demo", "T-007", "PWD", milestone_id="M1", complexity="S", files_owned=["a.py"])
        # Le faux worker écrit via $PWD, comme opencode : il doit viser le worktree, pas le dossier appelant.
        self.worker("echo 'X = 1' > \"$PWD/a.py\"")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-007", use_gate1=False), 0)
        self.assertTrue((self.wts / "T-007" / "a.py").exists())

    def test_cli_failure_is_not_a_ticket_failure(self) -> None:
        self.db.upsert_ticket("demo", "T-008", "Panne CLI", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.worker("echo 'modèle inconnu' >&2; exit 1")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-008", use_gate1=False), 3)
        t = self.db.get_ticket("demo", "T-008")
        self.assertEqual((t["state"], t["fail_count"]), ("READY", 0))

    def test_integration_gate_blocks_merge_when_main_breaks_it(self) -> None:
        self.db.upsert_ticket("demo", "T-006", "Intégration", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-006", use_gate1=False), 0)
        # Entre-temps, main reçoit un test incompatible avec le ticket (écart de contrat entre tickets).
        (self.root / "tests" / "test_contract.py").write_text(
            "import unittest, a\n\nclass C(unittest.TestCase):\n    def test_c(self):\n        self.assertEqual(a.X, 2)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "contrat")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-006", approve=True)
        self.assertFalse((self.root / "a.py").exists())  # rien n'a été fusionné
        self.assertEqual(self.db.get_ticket("demo", "T-006")["state"], "GATE2")

    def test_opencode_stats_records_tokens_and_cost_and_updates_quota(self) -> None:
        self.db.upsert_ticket("demo", "T-009", "Coûts réels", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.runner_cfg["runners"]["deepseek_coder"]["cost_source"] = "opencode_stats"
        self.runner_cfg["runners"]["deepseek_coder"]["provider"] = "deepseek"
        self.worker("echo 'X = 1' > a.py")

        snapshots = iter([
            {"total_cost_usd": 1.00, "input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 500},
            {"total_cost_usd": 1.50, "input_tokens": 3500, "output_tokens": 700, "cache_read_tokens": 1500},
        ])
        orig_snap = getattr(getattr(pipeline, "costs", None), "snapshot_opencode_stats", None)
        try:
            pipeline.costs.snapshot_opencode_stats = lambda timeout=30: next(snapshots)  # type: ignore[attr-defined]
            with self.db._connect() as conn:
                before_quota = conn.execute(
                    "SELECT monthly_spent_eur, daily_spent_eur FROM quota_state WHERE provider = 'deepseek'"
                ).fetchone()
            self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-009", use_gate1=False), 0)
        finally:
            if orig_snap is not None:
                pipeline.costs.snapshot_opencode_stats = orig_snap  # type: ignore[attr-defined]

        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT tokens_in, tokens_out, cost_eur FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-009'"
            ).fetchone()
            after_quota = conn.execute(
                "SELECT monthly_spent_eur, daily_spent_eur FROM quota_state WHERE provider = 'deepseek'"
            ).fetchone()

        self.assertEqual(run_row["tokens_in"], 2500)
        self.assertEqual(run_row["tokens_out"], 500)
        self.assertAlmostEqual(run_row["cost_eur"], round(0.50 * 0.92, 4))
        self.assertGreater(after_quota["monthly_spent_eur"], before_quota["monthly_spent_eur"])
        self.assertGreater(after_quota["daily_spent_eur"], before_quota["daily_spent_eur"])
        self.assertAlmostEqual(after_quota["monthly_spent_eur"] - before_quota["monthly_spent_eur"], round(0.50 * 0.92, 4))

    def test_opencode_stats_none_snapshot_does_not_fail_run(self) -> None:
        self.db.upsert_ticket("demo", "T-010", "Snapshot None", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.runner_cfg["runners"]["deepseek_coder"]["cost_source"] = "opencode_stats"
        self.worker("echo 'X = 1' > a.py")

        orig_snap = getattr(getattr(pipeline, "costs", None), "snapshot_opencode_stats", None)
        try:
            pipeline.costs.snapshot_opencode_stats = lambda timeout=30: None  # type: ignore[attr-defined]
            self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-010", use_gate1=False), 0)
        finally:
            if orig_snap is not None:
                pipeline.costs.snapshot_opencode_stats = orig_snap  # type: ignore[attr-defined]

        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT tokens_in, tokens_out, cost_eur FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-010'"
            ).fetchone()
        self.assertEqual((run_row["tokens_in"], run_row["tokens_out"], run_row["cost_eur"]), (0, 0, 0.0))

    def test_auto_dry_run_lists_batch_without_executing(self) -> None:
        import contextlib
        import io

        self.db.upsert_ticket("demo", "T-011", "Ticket A", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-012", "Ticket B", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.worker("echo 'X = 1' > a.py")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pipeline.main(["auto", "demo", "--dry-run"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("T-011", out)
        self.assertIn("a.py", out)
        self.assertIn("T-012", out)
        self.assertIn("b.py", out)
        self.assertEqual(self.db.get_ticket("demo", "T-011")["state"], "READY")
        self.assertEqual(self.db.get_ticket("demo", "T-012")["state"], "READY")

    def test_auto_stops_at_max_tickets(self) -> None:
        self.db.upsert_ticket("demo", "T-013", "Ticket 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-014", "Ticket 2", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.worker("echo 'X = 1' > a.py")

        code = pipeline.main(["auto", "demo", "--slots", "1", "--max-tickets", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-013")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-014")["state"], "READY")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertIn(("AUTO_STOP", "max_tickets"), events)

    def test_auto_stops_after_max_escalations(self) -> None:
        self.db.upsert_ticket("demo", "T-015", "Échec 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-016", "Échec 2", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.db.upsert_ticket("demo", "T-017", "Non touché", milestone_id="M1", complexity="S", files_owned=["c.py"])
        self.worker("echo 'pirate' > hors_perimetre.py")

        code = pipeline.main(["auto", "demo", "--slots", "1", "--max-attempts", "1", "--max-escalations", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-015")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-016")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-017")["state"], "READY")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertIn(("AUTO_STOP", "escalades"), events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
