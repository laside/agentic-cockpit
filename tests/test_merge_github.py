#!/usr/bin/env python3
"""
Tests du mode de fusion GitHub de `cockpit merge` (core/pipeline.py, merge_ticket).

Un dépôt bare local sert d'`origin` et un faux `gh` (COCKPIT_GH) journalise ses arguments
et simule `pr create`, `pr checks` et `pr merge` : aucun réseau, aucun LLM.
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

FAKE_GH = '''#!/usr/bin/env python3
"""Faux gh : journalise les arguments et simule les commandes PR sans réseau."""
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

bare = os.environ["BARE_REPO"]
if args[:2] == ["pr", "view"]:
    sys.exit(1)  # aucune PR existante
if args[:2] == ["pr", "create"]:
    print("https://github.com/demo/demo/pull/7")
    sys.exit(0)
if args[:2] == ["pr", "checks"]:
    sys.exit(int(os.environ.get("GH_CHECKS_RC", "0")))
if args[:2] == ["pr", "merge"]:
    branch = args[2]
    sha = subprocess.run(
        ["git", "--git-dir", bare, "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "--git-dir", bare, "update-ref", "refs/heads/main", sha], check=True)
    sys.exit(0)
sys.exit(0)
'''


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def git_bare(bare: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--git-dir", str(bare), *args], capture_output=True, text=True, check=check)


class MergeGithubTests(unittest.TestCase):
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
            "lint:\n\t@true\n\ntest:\n\t@touch .artefact-de-test\n\t@python3 -B -m unittest discover -s tests -q 2>&1\n",
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
        os.environ["BARE_REPO"] = str(self.bare)
        os.environ.pop("GH_CHECKS_RC", None)

        self.overlay = base / "config-overlay"
        self.overlay.mkdir()
        (self.overlay / "projects.toml").write_text(
            '[projects.demo]\nmerge_mode = "github"\nremote = "origin"\n', encoding="utf-8"
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
        os.environ.pop("BARE_REPO", None)
        os.environ.pop("GH_CHECKS_RC", None)
        os.environ.pop("PUSH_LOG", None)
        self.tmp.cleanup()

    def add_upstream_commit(self) -> None:
        """Avance `main` sur origin depuis un autre clone : la racine locale est en retard."""
        other = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        git(other, "config", "user.name", "Amont")
        git(other, "config", "user.email", "amont@local")
        (other / "upstream.txt").write_text("amont\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "amont")
        git(other, "push", "-q", "origin", "main")

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line]

    def pushed_refs(self) -> list[str]:
        if not self.push_log.exists():
            return []
        return [line for line in self.push_log.read_text(encoding="utf-8").splitlines() if line]

    def prepare_ticket(
        self, ticket_id: str, title: str = "Fusion GitHub", contract: str = "champ imposé : `texte`"
    ) -> None:
        self.db.upsert_ticket(
            "demo", ticket_id, title, milestone_id="M1", complexity="S",
            files_owned=["a.py"], contract=contract,
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", ticket_id, use_gate1=False), 0)
        self.assertEqual(self.db.get_ticket("demo", ticket_id)["state"], "GATE2")

    def test_github_merge_success_uses_pr_and_fast_forwards_root(self) -> None:
        self.prepare_ticket("T-010", "Fusion GitHub")
        self.add_upstream_commit()
        self.push_log.write_text("", encoding="utf-8")

        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-010", approve=True), 0)

        ticket = self.db.get_ticket("demo", "T-010")
        self.assertEqual(ticket["state"], "MERGED")

        # La branche a bien été fusionnée dans `main` du dépôt bare.
        self.assertEqual(git_bare(self.bare, "show", "main:a.py").stdout.strip(), "X = 1")
        self.assertIn("amont", git_bare(self.bare, "show", "main:upstream.txt").stdout)

        # La racine locale a avancé en fast-forward, sans commit local supplémentaire.
        git(self.root, "fetch", "origin")
        self.assertEqual(
            git(self.root, "rev-parse", "HEAD").stdout.strip(),
            git(self.root, "rev-parse", "origin/main").stdout.strip(),
        )
        self.assertTrue((self.root / "a.py").exists())
        self.assertTrue((self.root / "upstream.txt").exists())
        self.assertIn("MERGED", [e["kind"] for e in self.db.recent_events("demo", 20)])
        merged_event = next(e for e in self.db.recent_events("demo", 20) if e["kind"] == "MERGED")
        self.assertIn("https://github.com/demo/demo/pull/7", merged_event["detail"])

        calls = self.gh_calls()
        create = next(c for c in calls if c[:2] == ["pr", "create"])
        self.assertIn("--base", create)
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(create[create.index("--head") + 1], "feat/T-010")
        self.assertEqual(create[create.index("--title") + 1], "T-010: Fusion GitHub")
        body = create[create.index("--body") + 1]
        self.assertEqual(body.split("\n\nFichiers modifiés")[0], "champ imposé : `texte`")
        self.assertIn("a.py", body)
        self.assertTrue(any(c[:2] == ["pr", "checks"] and "--watch" in c and "--fail-fast" in c for c in calls))
        self.assertTrue(any(c[:2] == ["pr", "merge"] and "--squash" in c and "--delete-branch" in c for c in calls))

        # Aucun push direct sur `main` : seule la branche du ticket est poussée.
        pushed = self.pushed_refs()
        self.assertIn("refs/heads/feat/T-010", pushed)
        self.assertNotIn("refs/heads/main", pushed)

        # Le worktree et la branche locale sont nettoyés, la roadmap n'est pas commitée.
        self.assertFalse((self.wts / "T-010").exists())
        self.assertEqual(git(self.root, "branch", "--list", "feat/T-010").stdout.strip(), "")
        self.assertTrue((self.root / "docs" / "ROADMAP.md").exists())
        self.assertNotEqual(git(self.root, "status", "--porcelain").stdout.strip(), "")

    def test_squash_merge_flags_private_commit_email(self) -> None:
        git(self.root, "config", "user.email", "dev@entreprise-privee.fr")  # privacy-check: allow
        self.prepare_ticket("T-011", "Auteur du squash")
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-011", approve=True), 0)
        merge = next(c for c in self.gh_calls() if c[:2] == ["pr", "merge"])
        self.assertNotIn("--author-email", merge)  # refusé par GitHub pour une adresse noreply
        leak = next(e for e in self.db.recent_events("demo", 20) if e["kind"] == "PRIVACY_LEAK")
        self.assertIn("author-email: email-non-autorise", leak["detail"])
        self.assertNotIn("entreprise-privee", leak["detail"])

    def test_noreply_commit_email_is_not_flagged(self) -> None:
        git(self.root, "config", "user.email", "1+bot@users.noreply.github.com")
        git(self.root, "config", "user.name", "bot")
        self.prepare_ticket("T-013", "Auteur noreply")
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-013", approve=True), 0)
        self.assertNotIn("PRIVACY_LEAK", [e["kind"] for e in self.db.recent_events("demo", 20)])

    def test_commit_identity_leaks_accepts_noreply_github_address(self) -> None:
        git(self.root, "config", "user.email", "noreply@github.com")
        (self.root / "identite.txt").write_text("1\n", encoding="utf-8")
        git(self.root, "add", "identite.txt")
        git(self.root, "commit", "-q", "-m", "identite noreply")
        self.assertEqual(pipeline.commit_identity_leaks(self.root, "HEAD"), [])

    def test_commit_identity_leaks_flags_unknown_github_email(self) -> None:
        git(self.root, "config", "user.email", "inconnu@github.com")  # privacy-check: allow
        (self.root / "identite.txt").write_text("2\n", encoding="utf-8")
        git(self.root, "add", "identite.txt")
        git(self.root, "commit", "-q", "-m", "identite inconnue")
        leaks = pipeline.commit_identity_leaks(self.root, "HEAD")
        self.assertIn("author-email: email-non-autorise", leaks)

    def test_private_word_in_contract_blocks_push_and_pr(self) -> None:
        (self.overlay / "forbidden-words.txt").write_text("# liste\nmotprive\n", encoding="utf-8")
        self.prepare_ticket("T-012", "Contrat privé", contract="Ne pas citer MotPrive ici")
        self.push_log.write_text("", encoding="utf-8")
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.merge_ticket(self.db, "demo", "T-012", approve=True)
        self.assertNotIn("MotPrive", str(ctx.exception))
        self.assertEqual(self.db.get_ticket("demo", "T-012")["state"], "GATE2")
        self.assertIn("PR_PRIVACY_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.gh_calls()))
        self.assertNotIn("refs/heads/feat/T-012", self.pushed_refs())

    def test_checks_failure_leaves_ticket_and_main_untouched(self) -> None:
        self.prepare_ticket("T-020", "Checks rouges")
        self.add_upstream_commit()
        os.environ["GH_CHECKS_RC"] = "1"

        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-020", approve=True)

        self.assertEqual(self.db.get_ticket("demo", "T-020")["state"], "GATE2")
        self.assertIn("PR_CHECKS_FAIL", [e["kind"] for e in self.db.recent_events("demo", 20)])
        self.assertNotEqual(git_bare(self.bare, "show", "main:a.py", check=False).returncode, 0)
        self.assertFalse((self.root / "a.py").exists())
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))

    def test_contract_is_truncated_to_4000_chars_in_pr_body(self) -> None:
        self.prepare_ticket("T-040", "Contrat long", contract="A" * 4500)

        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-040", approve=True), 0)

        create = next(c for c in self.gh_calls() if c[:2] == ["pr", "create"])
        body = create[create.index("--body") + 1]
        contract_part = body.split("\n\nFichiers modifiés")[0]
        self.assertEqual(len(contract_part), 4000)
        self.assertEqual(contract_part, "A" * 4000)
        self.assertIn("a.py", body)

    def test_existing_pr_is_reused_with_url_in_merged_event(self) -> None:
        self.prepare_ticket("T-030", "PR existante")
        url = "https://github.com/demo/demo/pull/12"
        script = self.gh.read_text(encoding="utf-8")
        script = script.replace(
            'if args[:2] == ["pr", "view"]:\n    sys.exit(1)  # aucune PR existante',
            'if args[:2] == ["pr", "view"]:\n'
            f'    print(json.dumps({{"number": 12, "url": "{url}"}}))\n    sys.exit(0)',
        )
        self.gh.write_text(script, encoding="utf-8")

        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-030", approve=True), 0)
        calls = self.gh_calls()
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in calls))
        self.assertTrue(any(c[:2] == ["pr", "checks"] for c in calls))
        self.assertTrue(any(c[:2] == ["pr", "merge"] for c in calls))
        merged_event = next(e for e in self.db.recent_events("demo", 20) if e["kind"] == "MERGED")
        self.assertIn(url, merged_event["detail"])


if __name__ == "__main__":
    unittest.main()
