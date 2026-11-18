#!/usr/bin/env python3
"""
Tests du mode PR seule (`merge_mode = "github_pr"`) de `cockpit merge`.

Un dépôt bare local sert d'`origin` et un faux `gh` (COCKPIT_GH) journalise ses
arguments : la PR est ouverte puis le pipeline s'arrête, sans checks ni fusion.
Aucun réseau, aucun LLM.
"""
from __future__ import annotations

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
import scheduler  # noqa: E402

FAKE_GH = '''#!/usr/bin/env python3
"""Faux gh : journalise les arguments et simule les commandes PR sans réseau."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

if args[:2] == ["pr", "view"]:
    sys.exit(1)  # aucune PR existante
if args[:2] == ["pr", "create"]:
    print("https://github.com/demo/demo/pull/9")
    sys.exit(0)
sys.exit(0)
'''


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def git_bare(bare: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--git-dir", str(bare), *args], capture_output=True, text=True, check=check)


class PrOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.root = base / "demo"
        self.wts = base / "demo-worktrees"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@local")
        (self.root / "Makefile").write_text(
            "lint:\n\t@true\n\ntest:\n\t@true\n",
            encoding="utf-8",
        )
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_base.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.bare = base / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.bare)], check=True)
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-u", "origin", "main")

        # Journalise les refs réellement poussées vers le dépôt bare.
        self.push_log = base / "push.log"
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text(
            '#!/usr/bin/env bash\nwhile read old new ref; do\n  echo "$ref" >> "$PUSH_LOG"\ndone\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        os.environ["PUSH_LOG"] = str(self.push_log)

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project(
            "demo", "Demo", str(self.root), str(self.wts), str(self.wts), milestones=[("M1", "Socle")]
        )

        self.script = base / "worker.sh"
        self.script.write_text("#!/usr/bin/env bash\nset -e\necho 'X = 1' > a.py\n", encoding="utf-8")
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
        self.home = base / "home"
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "sys_coder_tdd.md").write_text("Implémente le brief.", encoding="utf-8")
        (self.home / "prompts" / "sys_gate1_nas32k.md").write_text("Audite.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.home
        pipeline.DB_PATH = base / "pmo.db"

        self.gh = base / "fake_gh.py"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(0o755)
        self.gh_log = base / "gh.log"
        self._orig_gh = os.environ.get("COCKPIT_GH")
        os.environ["COCKPIT_GH"] = str(self.gh)
        os.environ["GH_LOG"] = str(self.gh_log)

        self.overlay = base / "config-overlay"
        self.overlay.mkdir()
        (self.overlay / "projects.toml").write_text(
            '[projects.demo]\nmerge_mode = "github_pr"\nremote = "origin"\n', encoding="utf-8"
        )
        self._orig_config_dir = os.environ.get("COCKPIT_CONFIG_DIR")
        os.environ["COCKPIT_CONFIG_DIR"] = str(self.overlay)

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        if self._orig_gh is None:
            os.environ.pop("COCKPIT_GH", None)
        else:
            os.environ["COCKPIT_GH"] = self._orig_gh
        if self._orig_config_dir is None:
            os.environ.pop("COCKPIT_CONFIG_DIR", None)
        else:
            os.environ["COCKPIT_CONFIG_DIR"] = self._orig_config_dir
        os.environ.pop("GH_LOG", None)
        os.environ.pop("PUSH_LOG", None)
        self.tmp.cleanup()

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line]

    def pushed_refs(self) -> list[str]:
        if not self.push_log.exists():
            return []
        return [line for line in self.push_log.read_text(encoding="utf-8").splitlines() if line]

    def prepare_ticket(
        self, ticket_id: str, title: str = "PR seule", contract: str = "champ imposé : `texte`"
    ) -> None:
        self.db.upsert_ticket(
            "demo", ticket_id, title, milestone_id="M1", complexity="S",
            files_owned=["a.py"], contract=contract,
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", ticket_id, use_gate1=False), 0)
        self.assertEqual(self.db.get_ticket("demo", ticket_id)["state"], "GATE2")

    def test_github_pr_pushes_opens_pr_and_stops_without_merging(self) -> None:
        self.prepare_ticket("T-050", "PR seule")
        self.db.record_run(
            "demo", "T-050", attempt=99, lane="test", gate0="PASS", gate1="SKIPPED",
            cost_usd=0.2717, cost_eur=0.2500,
        )
        self.push_log.write_text("", encoding="utf-8")

        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-050", approve=True), 0)

        ticket = self.db.get_ticket("demo", "T-050")
        self.assertEqual(ticket["state"], "PR_OPEN")
        event = next(e for e in self.db.recent_events("demo", 20) if e["kind"] == "PR_OPENED")
        self.assertIn("https://github.com/demo/demo/pull/9", event["detail"])

        # La PR est ouverte mais rien n'est vérifié ni fusionné.
        calls = self.gh_calls()
        self.assertFalse(any(c[:2] == ["pr", "checks"] for c in calls))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in calls))
        create = next(c for c in calls if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(create[create.index("--head") + 1], "feat/T-050")
        self.assertEqual(create[create.index("--title") + 1], "T-050: PR seule")

        # Corps généré sans LLM : contrat, fichiers, diffstat, portes et coût du run.
        body = create[create.index("--body") + 1]
        self.assertEqual(body.split("\n\nFichiers modifiés")[0], "champ imposé : `texte`")
        self.assertIn("a.py", body)
        self.assertIn("Diffstat", body)
        self.assertIn("Porte 0 : PASS", body)
        self.assertIn("Porte 1 : SKIPPED", body)
        self.assertIn("0.2500 €", body)

        # Seule la branche du ticket est poussée : `main` distant reste intact.
        pushed = self.pushed_refs()
        self.assertIn("refs/heads/feat/T-050", pushed)
        self.assertNotIn("refs/heads/main", pushed)
        self.assertNotEqual(git_bare(self.bare, "show", "main:a.py", check=False).returncode, 0)

    def test_existing_pr_is_reused_and_reported_in_pr_opened_event(self) -> None:
        self.prepare_ticket("T-051", "PR existante")
        url = "https://github.com/demo/demo/pull/12"
        script = self.gh.read_text(encoding="utf-8")
        script = script.replace(
            'if args[:2] == ["pr", "view"]:\n    sys.exit(1)  # aucune PR existante',
            'if args[:2] == ["pr", "view"]:\n'
            f'    print(json.dumps({{"number": 12, "url": "{url}"}}))\n    sys.exit(0)',
        )
        self.gh.write_text(script, encoding="utf-8")

        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-051", approve=True), 0)

        self.assertEqual(self.db.get_ticket("demo", "T-051")["state"], "PR_OPEN")
        calls = self.gh_calls()
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in calls))
        self.assertFalse(any(c[:2] == ["pr", "checks"] for c in calls))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in calls))
        event = next(e for e in self.db.recent_events("demo", 20) if e["kind"] == "PR_OPENED")
        self.assertIn(url, event["detail"])

    def test_private_word_blocks_push_and_pr_opening(self) -> None:
        (self.overlay / "forbidden-words.txt").write_text("# liste\nmotprive\n", encoding="utf-8")
        self.prepare_ticket("T-052", "Contrat privé", contract="Ne pas citer MotPrive ici")
        self.push_log.write_text("", encoding="utf-8")

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.merge_ticket(self.db, "demo", "T-052", approve=True)

        self.assertNotIn("MotPrive", str(ctx.exception))
        self.assertEqual(self.db.get_ticket("demo", "T-052")["state"], "GATE2")
        self.assertIn("PR_PRIVACY_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.gh_calls()))
        self.assertNotIn("refs/heads/feat/T-052", self.pushed_refs())

    def test_auto_does_not_count_pr_open_as_failure(self) -> None:
        self.db.upsert_ticket(
            "demo", "T-062", "PR déjà ouverte", milestone_id="M1", complexity="S", files_owned=["a.py"]
        )
        self.db.update_ticket("demo", "T-062", state="PR_OPEN")
        batch = [{"id": "T-062", "lane": "deepseek_coder", "files": ["a.py"]}]
        calls = {"plan": 0}
        orig_plan = scheduler.plan_batch
        orig_run = pipeline.run_ticket

        def fake_plan(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            calls["plan"] += 1
            return batch if calls["plan"] == 1 else []

        def fake_run(db: pmo_db.CockpitDB, project_id: str, ticket_id: str, **_kwargs: object) -> int:
            db.update_ticket(project_id, ticket_id, state="PR_OPEN")
            return 1

        try:
            scheduler.plan_batch = fake_plan  # type: ignore[assignment]
            pipeline.run_ticket = fake_run  # type: ignore[assignment]
            code = pipeline.main(["auto", "demo", "--slots", "1", "--max-escalations", "1"])
        finally:
            scheduler.plan_batch = orig_plan  # type: ignore[assignment]
            pipeline.run_ticket = orig_run  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(calls["plan"], 2)
        self.assertEqual(self.db.get_ticket("demo", "T-062")["state"], "PR_OPEN")
        self.assertEqual(self.db.get_ticket("demo", "T-062")["fail_count"], 0)
        events = [(e["kind"], e["detail"]) for e in self.db.recent_events("demo", 20)]
        self.assertNotIn(("AUTO_STOP", "escalades"), events)

    def test_pr_open_is_not_active_indexed_or_relaunched_and_blocks_dependents(self) -> None:
        self.assertNotIn("PR_OPEN", pipeline.ACTIVE_STATES)
        self.db.upsert_ticket(
            "demo", "T-060", "En attente de PR", milestone_id="M1", complexity="S", files_owned=["a.py"]
        )
        self.db.update_ticket("demo", "T-060", state="PR_OPEN")
        self.db.upsert_ticket(
            "demo", "T-061", "Dépendant", milestone_id="M1", complexity="S",
            files_owned=["b.py"], depends_on=["T-060"],
        )

        self.assertEqual(scheduler.plan_batch(self.db, "demo", slots=5), [])
        self.assertEqual(self.db.get_ticket("demo", "T-061")["state"], "BLOCKED")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.run_ticket(self.db, "demo", "T-060", use_gate1=False)

        status = pipeline.build_status(self.db, "demo")
        self.assertIn("T-060", status)
        self.assertIn("PR_OPEN", status)


if __name__ == "__main__":
    unittest.main()
