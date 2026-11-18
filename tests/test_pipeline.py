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

    def test_require_tests_refuses_then_accepts_second_attempt(self) -> None:
        self.runner_cfg["defaults"]["require_tests"] = True
        self.db.upsert_ticket("demo", "T-040", "Porte 1 statique", milestone_id="M1", complexity="S",
                              files_owned=["core.py", "tests/test_core.py"])
        # 1re tentative : source seule, refusée par la Porte 1 statique. 2e : test ajouté.
        self.worker(
            'if echo "$1" | grep -q "Aucun test"; then\n'
            "mkdir -p tests\n"
            "cat > tests/test_core.py << EOF\n"
            "import unittest\n\nclass C(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
            "EOF\n"
            "fi\n"
            "echo 'X = 1' > core.py"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-040", use_gate1=False), 0)
        t = self.db.get_ticket("demo", "T-040")
        self.assertEqual(t["fail_count"], 1)
        self.assertEqual(t["state"], "GATE2")
        self.assertIn("GATE1_STATIC_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])
        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT gate1 FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-040' AND attempt = 1"
            ).fetchone()
        self.assertEqual(run_row["gate1"], "STATIC_FAIL")

    def test_require_tests_accepts_docs_only_ticket(self) -> None:
        self.runner_cfg["defaults"]["require_tests"] = True
        self.db.upsert_ticket("demo", "T-041", "Documentation seule", milestone_id="M1", complexity="S",
                              files_owned=["docs/README.md"])
        self.worker("mkdir -p docs; echo '# doc' > docs/README.md")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-041", use_gate1=False), 0)
        t = self.db.get_ticket("demo", "T-041")
        self.assertEqual((t["state"], t["fail_count"]), ("GATE2", 0))
        self.assertNotIn("GATE1_STATIC_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])

    def test_require_tests_disabled_has_no_effect(self) -> None:
        self.runner_cfg["defaults"]["require_tests"] = False
        self.db.upsert_ticket("demo", "T-042", "Option désactivée", milestone_id="M1", complexity="S",
                              files_owned=["core.py"])
        self.worker("echo 'X = 1' > core.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-042", use_gate1=False), 0)
        t = self.db.get_ticket("demo", "T-042")
        self.assertEqual((t["state"], t["fail_count"]), ("GATE2", 0))
        self.assertNotIn("GATE1_STATIC_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])

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

    def test_auto_max_tickets_bounds_slots(self) -> None:
        """(a) max_tickets=1 avec slots=2 ne lance qu'un seul ticket."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket("demo", "T-020", "Ticket 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-021", "Ticket 2", milestone_id="M1", complexity="S", files_owned=["b.py"])
        called: list[str] = []

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            called.append(ticket_id)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            code = pipeline.main(["auto", "demo", "--slots", "2", "--max-tickets", "1"])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(called, ["T-020"])
        self.assertEqual(self.db.get_ticket("demo", "T-020")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-021")["state"], "READY")

    def test_auto_batch_failure_then_success_keeps_escalation_count(self) -> None:
        """(b) un lot [échec, succès] n'efface pas le compteur d'escalades et l'arrêt « escalades » survient quand le seuil est atteint."""
        self.db.set_mistral_key_active(True)
        # Lot 1 : T-022 (échec) et T-023 (succès) -> 1 escalade
        # Lot 2 : T-024 (échec) et T-025 (succès) -> 2 escalades -> arrêt "escalades" avant T-026
        self.db.upsert_ticket("demo", "T-022", "Échec lot 1", milestone_id="M1", priority="P0", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-023", "Succès lot 1", milestone_id="M1", priority="P1", complexity="S", files_owned=["b.py"])
        self.db.upsert_ticket("demo", "T-024", "Échec lot 2", milestone_id="M1", priority="P2", complexity="S", files_owned=["c.py"])
        self.db.upsert_ticket("demo", "T-025", "Succès lot 2", milestone_id="M1", priority="P2", complexity="S", files_owned=["d.py"])
        self.db.upsert_ticket("demo", "T-026", "Non lancé", milestone_id="M1", priority="P3", complexity="S", files_owned=["e.py"])

        outcomes = {"T-022": 1, "T-023": 0, "T-024": 1, "T-025": 0, "T-026": 0}

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return outcomes[ticket_id]

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            code = pipeline.main([
                "auto", "demo", "--slots", "2", "--max-tickets", "6", "--max-escalations", "2"
            ])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-026")["state"], "READY")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertIn(("AUTO_STOP", "escalades"), events)

    def test_auto_worker_exception_resets_ticket_to_ready_keeps_locks_and_lets_sibling_finish(self) -> None:
        """(c) une exception dans un worker remet son ticket à READY, conserve ses verrous et n'empêche pas l'autre ticket du lot d'aboutir."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket("demo", "T-027", "Crash worker", milestone_id="M1", priority="P0", complexity="S", files_owned=["crash.py"])
        self.db.upsert_ticket("demo", "T-028", "Succès voisin", milestone_id="M1", priority="P1", complexity="S", files_owned=["ok.py"])

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            ticket = db.get_ticket(project_id, ticket_id)
            assert ticket is not None
            files = ["crash.py"] if ticket_id == "T-027" else ["ok.py"]
            db.acquire_locks(project_id, ticket_id, files)
            db.update_ticket(project_id, ticket_id, state="ASSIGNED")
            if ticket_id == "T-027":
                raise RuntimeError("panne inattendue dans le thread")
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            code = pipeline.main([
                "auto", "demo", "--slots", "2", "--max-tickets", "2", "--max-escalations", "1"
            ])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-027")["state"], "READY")
        self.assertEqual(self.db.get_ticket("demo", "T-028")["state"], "GATE2")
        # Les verrous de T-027 sont conservés
        ok_steal, conflicts = self.db.acquire_locks("demo", "T-OTHER", ["crash.py"])
        self.assertFalse(ok_steal)
        self.assertTrue(any("T-027" in c for c in conflicts))
        events = [(e["ticket_id"], e["kind"]) for e in self.db.recent_events("demo", 20)]
        self.assertIn(("T-027", "WORKER_CRASH"), events)

    def test_gate2_ticket_retains_locks_after_cmd_auto(self) -> None:
        """(f) un ticket en GATE2 conserve ses verrous après cmd_auto."""
        self.db.upsert_ticket("demo", "T-029", "Ticket GATE2", milestone_id="M1", complexity="S", files_owned=["locked.py"])
        self.worker("echo 'X = 1' > locked.py")

        code = pipeline.main(["auto", "demo", "--slots", "1", "--max-tickets", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-029")["state"], "GATE2")
        ok, conflicts = self.db.acquire_locks("demo", "T-030", ["locked.py"])
        self.assertFalse(ok)
        self.assertTrue(any("T-029" in c for c in conflicts))

    def test_recent_runs_by_lane_counts_only_recent_provider_runs(self) -> None:
        """Compte les runs récents par voie pour un provider, sans les runs hors fenêtre ni les autres providers."""
        self.db.record_run("demo", "T-058", attempt=1, lane="agy_standard", provider="agy")
        self.db.record_run("demo", "T-059", attempt=1, lane="agy_standard", provider="agy")
        self.db.record_run("demo", "T-060", attempt=1, lane="agy_argon", provider="agy")
        self.db.record_run("demo", "T-061", attempt=1, lane="agy_standard", provider="deepseek")
        with self.db._connect() as conn:
            conn.execute("UPDATE runs SET created_at = datetime('now', '-6 hours') WHERE ticket_id = 'T-060'")
        self.assertEqual(self.db.recent_runs_by_lane("agy"), {"agy_standard": 2})
        self.assertEqual(self.db.recent_runs_by_lane("agy", hours=7.0), {"agy_standard": 2, "agy_argon": 1})

    def test_estimate_window_accumulates_recent_runs(self) -> None:
        """(a) deux runs sur agy_standard (6 % chacun) donnent 12 % de fenêtre utilisée."""
        self.runner_cfg["runners"]["agy_standard"].update(
            provider="agy", cost_source="estimate", window_pct_per_run=6.0
        )
        self.db.upsert_ticket("demo", "T-050", "Estimation 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-051", "Estimation 2", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-050", lane_override="agy_standard", use_gate1=False), 0
        )
        self.worker("echo 'Y = 1' > b.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-051", lane_override="agy_standard", use_gate1=False), 0
        )
        with self.db._connect() as conn:
            pct = conn.execute(
                "SELECT window_used_pct FROM quota_state WHERE provider = 'agy'"
            ).fetchone()["window_used_pct"]
        self.assertAlmostEqual(pct, 12.0)

    def test_estimate_ignores_runs_older_than_window(self) -> None:
        """(b) un run daté de plus de 5 h n'est pas compté dans la fenêtre glissante."""
        self.runner_cfg["runners"]["agy_standard"].update(
            provider="agy", cost_source="estimate", window_pct_per_run=6.0
        )
        self.db.upsert_ticket("demo", "T-052", "Ancien run", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-053", "Run récent", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-052", lane_override="agy_standard", use_gate1=False), 0
        )
        with self.db._connect() as conn:
            conn.execute("UPDATE runs SET created_at = datetime('now', '-6 hours') WHERE ticket_id = 'T-052'")
        self.worker("echo 'Y = 1' > b.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-053", lane_override="agy_standard", use_gate1=False), 0
        )
        with self.db._connect() as conn:
            pct = conn.execute(
                "SELECT window_used_pct FROM quota_state WHERE provider = 'agy'"
            ).fetchone()["window_used_pct"]
        self.assertAlmostEqual(pct, 6.0)

    def test_lane_without_cost_source_leaves_quota_and_run_unchanged(self) -> None:
        """(c) une voie sans cost_source ne touche ni quota_state ni les champs de coût du run."""
        runner = self.runner_cfg["runners"]["deepseek_coder"]
        runner["provider"] = "deepseek"
        runner.pop("cost_source", None)
        self.db.upsert_ticket("demo", "T-054", "Sans estimation", milestone_id="M1", complexity="S", files_owned=["a.py"])
        with self.db._connect() as conn:
            before = dict(conn.execute("SELECT * FROM quota_state WHERE provider = 'deepseek'").fetchone())
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-054", lane_override="deepseek_coder", use_gate1=False), 0
        )
        with self.db._connect() as conn:
            after = dict(conn.execute("SELECT * FROM quota_state WHERE provider = 'deepseek'").fetchone())
            run_row = conn.execute(
                "SELECT tokens_in, tokens_out, cost_eur FROM runs WHERE ticket_id = 'T-054'"
            ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual((run_row["tokens_in"], run_row["tokens_out"], run_row["cost_eur"]), (0, 0, 0.0))

    def test_estimate_window_is_capped_at_100(self) -> None:
        """(d) la jauge de fenêtre glissante est plafonnée à 100.0."""
        self.runner_cfg["runners"]["agy_standard"].update(
            provider="agy", cost_source="estimate", window_pct_per_run=60.0
        )
        self.db.upsert_ticket("demo", "T-056", "Plafond 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-057", "Plafond 2", milestone_id="M1", complexity="S", files_owned=["b.py"])
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-056", lane_override="agy_standard", use_gate1=False), 0
        )
        self.worker("echo 'Y = 1' > b.py")
        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-057", lane_override="agy_standard", use_gate1=False), 0
        )
        with self.db._connect() as conn:
            pct = conn.execute(
                "SELECT window_used_pct FROM quota_state WHERE provider = 'agy'"
            ).fetchone()["window_used_pct"]
        self.assertAlmostEqual(pct, 100.0)

    def test_auto_stops_on_budget_after_batch(self) -> None:
        """cmd_auto s'arrête sur « budget » après le lot quand la dépense atteint --max-cost-eur."""
        import contextlib
        import io

        self.db.upsert_ticket("demo", "T-080", "Ticket budget 1", milestone_id="M1", complexity="S",
                              files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-081", "Ticket budget 2", milestone_id="M1", complexity="S",
                              files_owned=["b.py"])

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            db.record_run(project_id, ticket_id, attempt=1, lane="fake", provider="local", cost_eur=0.30)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        buf = io.StringIO()
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            with contextlib.redirect_stdout(buf):
                code = pipeline.main([
                    "auto", "demo", "--slots", "1", "--max-tickets", "6", "--max-cost-eur", "0.25"
                ])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-080")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-081")["state"], "READY")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertIn(("AUTO_STOP", "budget"), events)
        self.assertIn("Arrêt : budget", buf.getvalue())
        self.assertIn("0.30", buf.getvalue())

    def test_auto_without_max_cost_is_not_bounded_by_budget(self) -> None:
        """Sans --max-cost-eur, aucune limite de budget n'arrête la boucle."""
        import contextlib
        import io

        self.db.upsert_ticket("demo", "T-082", "Ticket sans budget 1", milestone_id="M1", complexity="S",
                              files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-083", "Ticket sans budget 2", milestone_id="M1", complexity="S",
                              files_owned=["b.py"])

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            db.record_run(project_id, ticket_id, attempt=1, lane="fake", provider="local", cost_eur=0.30)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            with contextlib.redirect_stdout(io.StringIO()):
                code = pipeline.main(["auto", "demo", "--slots", "1", "--max-tickets", "6"])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-082")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-083")["state"], "GATE2")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertNotIn(("AUTO_STOP", "budget"), events)

    def test_auto_budget_ignores_runs_before_start(self) -> None:
        """Seuls les runs créés après le démarrage de la boucle comptent pour le budget."""
        import contextlib
        import io

        self.db.record_run("demo", "T-OLD", attempt=1, lane="fake", provider="local", cost_eur=5.0)
        self.db.upsert_ticket("demo", "T-084", "Ticket budget tardif 1", milestone_id="M1", complexity="S",
                              files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-085", "Ticket budget tardif 2", milestone_id="M1", complexity="S",
                              files_owned=["b.py"])

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            db.record_run(project_id, ticket_id, attempt=1, lane="fake", provider="local", cost_eur=0.0)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            with contextlib.redirect_stdout(io.StringIO()):
                code = pipeline.main([
                    "auto", "demo", "--slots", "1", "--max-tickets", "6", "--max-cost-eur", "0.25"
                ])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(self.db.get_ticket("demo", "T-084")["state"], "GATE2")
        self.assertEqual(self.db.get_ticket("demo", "T-085")["state"], "GATE2")
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertNotIn(("AUTO_STOP", "budget"), events)

    def test_usage_format_opencode_json_records_real_tokens_cost_and_updates_quota(self) -> None:
        """Worker écrivant du JSON opencode_json : runs reçoit tokens/cost_usd/cost_kind/cost_eur et quota_state augmente."""
        runner = self.runner_cfg["runners"]["deepseek_coder"]
        runner["provider"] = "deepseek"
        runner["usage_format"] = "opencode_json"
        runner["cost_source"] = "opencode_stats"  # ne doit pas être exécuté en double si parse_usage réussit
        self.db.upsert_ticket("demo", "T-070", "Usage opencode_json", milestone_id="M1", complexity="S", files_owned=["a.py"])
        json_line = (
            '{"type": "step_finish", "timestamp": 1794997514199, "sessionID": "ses_x", '
            '"part": {"type": "step-finish", "reason": "stop", '
            '"tokens": {"total": 10684, "input": 9018, "output": 2, "reasoning": 0, '
            '"cache": {"write": 0, "read": 1664}}, "cost": 0.001358892}}'
        )
        self.worker(f"echo 'X = 1' > a.py\necho '{json_line}'")

        with self.db._connect() as conn:
            before_quota = conn.execute(
                "SELECT monthly_spent_eur, daily_spent_eur FROM quota_state WHERE provider = 'deepseek'"
            ).fetchone()

        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-070", lane_override="deepseek_coder", use_gate1=False), 0
        )

        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT tokens_in, tokens_out, tokens_cache, cost_usd, cost_kind, cost_eur "
                "FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-070'"
            ).fetchone()
            after_quota = conn.execute(
                "SELECT monthly_spent_eur, daily_spent_eur FROM quota_state WHERE provider = 'deepseek'"
            ).fetchone()

        expected_eur = round(0.001358892 * 0.92, 4)
        self.assertEqual(run_row["tokens_in"], 9018)
        self.assertEqual(run_row["tokens_out"], 2)
        self.assertEqual(run_row["tokens_cache"], 1664)
        self.assertAlmostEqual(run_row["cost_usd"], 0.001358892)
        self.assertEqual(run_row["cost_kind"], "measured")
        self.assertAlmostEqual(run_row["cost_eur"], expected_eur)
        self.assertAlmostEqual(after_quota["monthly_spent_eur"] - before_quota["monthly_spent_eur"], expected_eur)
        self.assertAlmostEqual(after_quota["daily_spent_eur"] - before_quota["daily_spent_eur"], expected_eur)

    def test_usage_format_claude_json_keeps_cost_eur_zero_and_quota_unchanged(self) -> None:
        """Worker écrivant du JSON claude_json : cost_usd et tokens enregistrés, mais cost_eur = 0.0 et quota_state inchangé."""
        self.runner_cfg["runners"]["claude_haiku"] = {
            "agent": "claude",
            "provider": "claude",
            "usage_format": "claude_json",
            "argv": ["bash", str(self.script), "{prompt}"],
        }
        self.db.upsert_ticket("demo", "T-071", "Usage claude_json", milestone_id="M1", complexity="S", files_owned=["a.py"])
        json_line = (
            '{"duration_api_ms":605,"stop_reason":"end_turn","total_cost_usd":0.00326982,'
            '"usage":{"input_tokens":2,"cache_creation_input_tokens":15877,'
            '"cache_read_input_tokens":9222,"output_tokens":4}}'
        )
        self.worker(f"echo 'X = 1' > a.py\necho '{json_line}'")

        with self.db._connect() as conn:
            before_quota = dict(conn.execute("SELECT * FROM quota_state WHERE provider = 'claude'").fetchone())

        self.assertEqual(
            pipeline.run_ticket(self.db, "demo", "T-071", lane_override="claude_haiku", use_gate1=False), 0
        )

        with self.db._connect() as conn:
            run_row = conn.execute(
                "SELECT tokens_in, tokens_out, tokens_cache, cost_usd, cost_kind, cost_eur "
                "FROM runs WHERE project_id = 'demo' AND ticket_id = 'T-071'"
            ).fetchone()
            after_quota = dict(conn.execute("SELECT * FROM quota_state WHERE provider = 'claude'").fetchone())

        self.assertEqual(run_row["tokens_in"], 2)
        self.assertEqual(run_row["tokens_out"], 4)
        self.assertEqual(run_row["tokens_cache"], 15877 + 9222)
        self.assertAlmostEqual(run_row["cost_usd"], 0.00326982)
        self.assertEqual(run_row["cost_kind"], "api_equivalent")
        self.assertEqual(run_row["cost_eur"], 0.0)
        self.assertEqual(before_quota, after_quota)

    def test_auto_merge_requires_gate1_claude_and_valid_calibration(self) -> None:
        """cmd_auto avec --auto-merge exige --gate1 + backend=claude + calibration valide (code 2, rien lancé sinon)."""
        import contextlib
        import io
        from unittest import mock

        self.db.upsert_ticket("demo", "T-090", "Auto merge test", milestone_id="M1", complexity="S", files_owned=["a.py"])
        called: list[str] = []

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_: object) -> int:
            called.append(ticket_id)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            return 0

        orig_run = pipeline.run_ticket
        try:
            pipeline.run_ticket = fake_run  # type: ignore[assignment]

            # 1) Sans --gate1 -> refusé code 2, rien lancé
            self.runner_cfg["gate1"] = {"backend": "claude"}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = pipeline.main(["auto", "demo", "--auto-merge"])
            self.assertEqual(code, 2)
            self.assertEqual(called, [])
            self.assertIn("--auto-merge exige --gate1 avec le juge Claude", buf.getvalue())

            # 2) Avec --gate1 mais backend != "claude" -> refusé code 2, rien lancé
            self.runner_cfg["gate1"] = {"backend": "nas"}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = pipeline.main(["auto", "demo", "--gate1", "--auto-merge"])
            self.assertEqual(code, 2)
            self.assertEqual(called, [])
            self.assertIn("--auto-merge exige --gate1 avec le juge Claude", buf.getvalue())

            # 3) Avec --gate1 et backend == "claude" mais calibration invalide -> refusé code 2, rien lancé
            self.runner_cfg["gate1"] = {"backend": "claude"}
            with mock.patch.object(
                pipeline.judge_calibrate,
                "calibration_status",
                return_value=(False, "aucun rapport de calibration"),
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = pipeline.main(["auto", "demo", "--gate1", "--auto-merge"])
            self.assertEqual(code, 2)
            self.assertEqual(called, [])
            self.assertIn(
                "--auto-merge refusé : aucun rapport de calibration. "
                "Lancez : cockpit judge-calibrate --runs 3 --json logs/judge-calibration.json",
                buf.getvalue(),
            )

            # 4) --dry-run n'est pas bloqué même avec --auto-merge sans calibration
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = pipeline.main(["auto", "demo", "--auto-merge", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertEqual(called, [])
            self.assertIn("T-090", buf.getvalue())

            # 5) Avec calibration valide, la boucle démarre
            with mock.patch.object(
                pipeline.judge_calibrate,
                "calibration_status",
                return_value=(True, ""),
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = pipeline.main(["auto", "demo", "--gate1", "--auto-merge", "--max-tickets", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(called, ["T-090"])
        finally:
            pipeline.run_ticket = orig_run  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main(verbosity=2)
