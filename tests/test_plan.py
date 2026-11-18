#!/usr/bin/env python3
"""
Tests de T-046 : plan multi-tickets et promotion multiple (core/drafter.py, core/pipeline.py).

Aucun réseau, aucun LLM : le worker est un faux script bash qui écrit `plan.json`.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import drafter  # noqa: E402
import pipeline  # noqa: E402
import pmo_db  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def ticket(title: str, files: list[str], depends_on: list[int] | None = None,
           size: str = "S", prio: str = "P2", accept: list[str] | None = None,
           contract: str = "") -> dict:
    return {
        "title": title,
        "size": size,
        "prio": prio,
        "files": files,
        "contract": contract or f"Contrat de {title}.",
        "accept": accept if accept is not None else [f"{title} est vérifié"],
        "depends_on": depends_on or [],
    }


class PlanTests(unittest.TestCase):
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
        (self.root / "core").mkdir()
        (self.root / "core" / ".keep").write_text("", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_base.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project(
            "demo", "Demo", str(self.root), str(self.wts), str(self.wts),
            milestones=[("M1", "Socle")],
        )

        self.script = base / "worker.sh"
        self.counter = base / "counter.txt"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60},
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
        pipeline.DB_PATH = base / "pmo.db"
        self.home = base / "home"
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "sys_drafter.md").write_text("Rédige un ticket.", encoding="utf-8")
        (self.home / "prompts" / "sys_drafter_plan.md").write_text("Rédige un plan.", encoding="utf-8")
        (self.home / "prompts" / "sys_coder_tdd.md").write_text("Implémente le brief.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.home

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        self.tmp.cleanup()

    def worker(self, body: str) -> None:
        self.script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n", encoding="utf-8")

    def write_plan(self, plan: dict | str) -> str:
        payload = plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
        return f"cat > plan.json <<'JSON'\n{payload}\nJSON\n"

    def draft_names(self) -> list[str]:
        if not self.wts.exists():
            return []
        return [p.name for p in self.wts.iterdir() if p.name.startswith("_draft-")]

    def ticket_count(self) -> int:
        with self.db._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM tickets WHERE project_id = 'demo'"
            ).fetchone()["n"]

    def plan_events(self) -> list[dict]:
        with self.db._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT ticket_id, kind, detail FROM events "
                "WHERE project_id = 'demo' AND kind = 'PLAN_CREATED' ORDER BY ticket_id"
            ).fetchall()]

    def plan_event_count(self) -> int:
        return len(self.plan_events())

    def run_plan(self, plan: dict | str, *extra: str) -> tuple[int, str, str]:
        self.worker(self.write_plan(plan))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["plan", "demo", "Objectif", *extra])
        return code, out.getvalue(), err.getvalue()

    def assert_plan_refused(self, plan: dict | str, expected: str) -> None:
        code, _out, err = self.run_plan(plan)
        self.assertEqual(code, 1)
        self.assertIn(expected, err)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.draft_names(), [])

    def test_valid_plan_creates_three_consecutive_drafts_with_mapped_dependencies(self) -> None:
        """Un plan de 3 tickets crée 3 DRAFT consécutifs, mappe depends_on et affiche un résumé compact."""
        plan = {"tickets": [
            ticket("Socle", ["core/base.py"], size="S", prio="P1"),
            ticket("API", ["core/api.py"], depends_on=[0], size="M", prio="P2"),
            ticket("UI", ["tests/test_ui.py"], depends_on=[1], size="XS", prio="P3"),
        ]}
        self.worker(self.write_plan(plan))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(["plan", "demo", "Construire le socle"])

        self.assertEqual(code, 0)
        self.assertEqual(self.ticket_count(), 3)
        ids = ["T-001", "T-002", "T-003"]
        for tid in ids:
            self.assertIsNotNone(self.db.get_ticket("demo", tid))
        first = self.db.get_ticket("demo", "T-001")
        second = self.db.get_ticket("demo", "T-002")
        third = self.db.get_ticket("demo", "T-003")
        for t in (first, second, third):
            self.assertEqual(t["state"], "DRAFT")
            self.assertEqual(t["owner"], "drafter")
        self.assertEqual(json.loads(first["depends_on_json"]), [])
        self.assertEqual(json.loads(second["depends_on_json"]), ["T-001"])
        self.assertEqual(json.loads(third["depends_on_json"]), ["T-002"])
        self.assertEqual(self.draft_names(), [])

        events = self.plan_events()
        self.assertEqual([event["ticket_id"] for event in events], ids)
        self.assertTrue(all("Construire le socle" in event["detail"] for event in events))

        text = out.getvalue()
        self.assertIn("T-001 [S/P1] Socle", text)
        self.assertIn("T-002 [M/P2] API", text)
        self.assertIn("dépend de T-001", text)
        self.assertIn("Total : 3 tickets DRAFT", text)

    def test_one_invalid_ticket_refuses_whole_plan_with_one_retry_and_no_worktree(self) -> None:
        """Un seul ticket invalide suffit : code 1, aucun ticket, une relance, aucun worktree résiduel."""
        plan = {"tickets": [
            ticket("Socle", ["core/base.py"]),
            ticket("API", ["core/api.py"], size="XL"),
        ]}
        self.worker(f'echo relance >> "{self.counter}"\n' + self.write_plan(plan))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["plan", "demo", "Objectif"])

        self.assertEqual(code, 1)
        self.assertEqual(len(self.counter.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.draft_names(), [])
        self.assertIn("Taille invalide", err.getvalue())

    def test_plan_with_one_or_five_tickets_is_refused(self) -> None:
        """Un plan doit contenir entre 2 et 4 tickets."""
        one = {"tickets": [ticket("Seul", ["core/base.py"])]}
        five = {"tickets": [ticket(f"Lot {i}", [f"core/lot{i}.py"]) for i in range(5)]}
        for plan, expected in ((one, "entre 2 et 4"), (five, "entre 2 et 4")):
            with self.subTest(tickets=len(plan["tickets"])):
                self.assert_plan_refused(plan, expected)

    def test_out_of_range_dependency_and_cycle_are_refused(self) -> None:
        """Les indices de dépendance doivent exister et ne pas former de cycle."""
        out_of_range = {"tickets": [
            ticket("A", ["core/a.py"]),
            ticket("B", ["core/b.py"], depends_on=[5]),
        ]}
        self.assert_plan_refused(out_of_range, "dépendance")

        cycle = {"tickets": [
            ticket("A", ["core/a.py"], depends_on=[1]),
            ticket("B", ["core/b.py"], depends_on=[0]),
        ]}
        self.assert_plan_refused(cycle, "cycle")

    def test_each_ticket_obeys_existing_path_rules(self) -> None:
        """Chaque ticket passe validate_draft : chemins, 8 fichiers, contrat et acceptation."""
        cases = {
            "absentdir/absent.py": "n'existe pas et son dossier parent non plus",
            "../hors.py": "hors dépôt",
            ".git/config": ".git",
            ".env": ".env",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assert_plan_refused(
                    {"tickets": [ticket("A", ["core/a.py"]), ticket("B", [path])]},
                    expected,
                )

        nine = [f"core/fichier{i}.py" for i in range(9)]
        self.assert_plan_refused(
            {"tickets": [ticket("A", ["core/a.py"]), ticket("B", nine)]},
            "Trop de fichiers",
        )
        self.assert_plan_refused(
            {"tickets": [ticket("A", ["core/a.py"]), ticket("B", ["core/b.py"], contract="x" * 1501)]},
            "Contrat trop long",
        )
        self.assert_plan_refused(
            {"tickets": [ticket("A", ["core/a.py"]), ticket("B", ["core/b.py"], accept=[])]},
            "acceptation sont vides",
        )

    def test_file_overlap_is_reported_but_does_not_block_creation(self) -> None:
        """Un chevauchement de fichiers est signalé dans le résumé sans empêcher la création."""
        plan = {"tickets": [
            ticket("Socle", ["core/commun.py", "core/a.py"]),
            ticket("Suite", ["core/commun.py", "core/b.py"]),
        ]}
        code, out, _err = self.run_plan(plan)
        self.assertEqual(code, 0)
        self.assertEqual(self.ticket_count(), 2)
        self.assertIn("Chevauchement", out)
        self.assertIn("core/commun.py", out)

    def test_atomic_plan_creation_rolls_back_when_a_later_ticket_fails(self) -> None:
        """Si un upsert échoue au 2e ticket, aucun ticket ni événement ne reste."""
        plan = {"tickets": [
            ticket("Socle", ["core/base.py"]),
            ticket("API", ["core/api.py"], depends_on=[0]),
        ]}
        self.worker(self.write_plan(plan))
        original = self.db.upsert_ticket
        calls = {"n": 0}

        def flaky_upsert(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("échec simulé au 2e ticket")
            return original(*args, **kwargs)

        self.db.upsert_ticket = flaky_upsert  # type: ignore[assignment]
        with self.assertRaises(pipeline.PipelineError) as ctx:
            drafter.plan_tickets(self.db, "demo", "Objectif")
        self.assertIn("échec simulé au 2e ticket", str(ctx.exception))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.plan_event_count(), 0)
        self.assertEqual(self.draft_names(), [])

    def test_cli_max_tickets_accepts_a_valid_bound(self) -> None:
        """`--max-tickets 3` est accepté et transmet la borne au plan."""
        plan = {"tickets": [ticket(f"Lot {i}", [f"core/lot{i}.py"]) for i in range(3)]}
        code, _out, _err = self.run_plan(plan, "--max-tickets", "3")
        self.assertEqual(code, 0)
        self.assertEqual(self.ticket_count(), 3)
        self.assertEqual(self.plan_event_count(), 3)

    def test_cli_max_tickets_out_of_bounds_is_refused(self) -> None:
        """`--max-tickets` hors de 2..4 est refusé sans créer de ticket."""
        plan = {"tickets": [ticket("A", ["core/a.py"]), ticket("B", ["core/b.py"])]}
        for value in ("1", "5"):
            with self.subTest(max_tickets=value):
                code, _out, err = self.run_plan(plan, "--max-tickets", value)
                self.assertEqual(code, 1)
                self.assertIn("borné entre 2 et 4", err)
                self.assertEqual(self.ticket_count(), 0)
                self.assertEqual(self.draft_names(), [])

    def test_cli_max_tickets_limits_the_number_of_tickets(self) -> None:
        """Un plan plus long que `--max-tickets 2` est refusé."""
        plan = {"tickets": [ticket(f"Lot {i}", [f"core/lot{i}.py"]) for i in range(3)]}
        code, _out, err = self.run_plan(plan, "--max-tickets", "2")
        self.assertEqual(code, 1)
        self.assertIn("entre 2 et 2", err)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.draft_names(), [])

    def test_promote_multiple_drafts_and_refuse_if_one_is_not_draft(self) -> None:
        """promote accepte plusieurs ids mais n'en promeut aucun si l'un n'est pas DRAFT."""
        self.db.upsert_ticket(
            "demo", "T-001", "Un", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/a.py"],
        )
        self.db.upsert_ticket(
            "demo", "T-002", "Deux", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/b.py"],
        )
        self.assertEqual(pipeline.main(["ticket", "promote", "demo", "T-001", "T-002"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "READY")
        self.assertEqual(self.db.get_ticket("demo", "T-002")["state"], "READY")

        self.db.upsert_ticket(
            "demo", "T-003", "Trois", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/c.py"],
        )
        self.db.upsert_ticket(
            "demo", "T-004", "Quatre", milestone_id="M1", complexity="S", state="READY",
            files_owned=["core/d.py"],
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "promote", "demo", "T-003", "T-004"])
        self.assertEqual(code, 1)
        self.assertEqual(self.db.get_ticket("demo", "T-003")["state"], "DRAFT")
        self.assertEqual(self.db.get_ticket("demo", "T-004")["state"], "READY")
        self.assertIn("DRAFT", err.getvalue())

    def test_historical_promote_form_still_works(self) -> None:
        """La forme historique `cockpit ticket promote T-001 demo` reste fonctionnelle."""
        self.db.upsert_ticket(
            "demo", "T-001", "Un", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/a.py"],
        )
        self.assertEqual(pipeline.main(["ticket", "promote", "T-001", "demo"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "READY")

    def test_validate_plan_rejects_cycle_and_accepts_valid_plan(self) -> None:
        """validate_plan est réutilisable et renvoie le premier refus explicite."""
        wt = self.root
        valid = {"tickets": [ticket("A", ["core/a.py"]), ticket("B", ["core/b.py"], depends_on=[0])]}
        self.assertIsNone(drafter.validate_plan(valid, wt, 4))
        cycle = {"tickets": [
            ticket("A", ["core/a.py"], depends_on=[1]),
            ticket("B", ["core/b.py"], depends_on=[0]),
        ]}
        self.assertIn("cycle", drafter.validate_plan(cycle, wt, 4) or "")


if __name__ == "__main__":
    unittest.main()
