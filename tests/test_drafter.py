#!/usr/bin/env python3
"""
Tests de T-043 : rédaction de tickets par un worker (core/drafter.py).
Aucun réseau, aucun LLM : le worker est un faux script bash qui écrit draft.json.
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
from unittest import mock

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import drafter  # noqa: E402
import pipeline  # noqa: E402
import pmo_db  # noqa: E402
import scheduler  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


VALID_DRAFT = {
    "title": "Ajouter le module de calcul",
    "size": "S",
    "prio": "P2",
    "files": ["core/calcul.py", "tests/test_calcul.py"],
    "contract": "Le module expose une fonction `ajoute(a, b)` qui retourne la somme.",
    "accept": ["`ajoute(1, 2)` retourne 3", "un test couvre le cas nominal"],
}


class DrafterTests(unittest.TestCase):
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
        # Branche distante simulée : le brouillon doit partir de origin/main en détaché.
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
        self.home = base / "home"
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "sys_drafter.md").write_text("Rédige un ticket.", encoding="utf-8")
        (self.home / "prompts" / "sys_coder_tdd.md").write_text("Implémente le brief.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.home
        pipeline.DB_PATH = base / "pmo.db"

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        self.tmp.cleanup()

    def worker(self, body: str) -> None:
        self.script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n", encoding="utf-8")

    def write_draft(self, draft: dict | str) -> str:
        payload = draft if isinstance(draft, str) else json.dumps(draft, ensure_ascii=False)
        return f"cat > draft.json <<'JSON'\n{payload}\nJSON\n"

    def draft_names(self) -> list[str]:
        if not self.wts.exists():
            return []
        return [p.name for p in self.wts.iterdir() if p.name.startswith("_draft-")]

    def ticket_count(self) -> int:
        with self.db._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM tickets WHERE project_id = 'demo'"
            ).fetchone()["n"]

    def run_draft(self, draft: dict | str) -> tuple[int, str, str]:
        self.worker(self.write_draft(draft))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "draft", "demo", "Objectif"])
        return code, out.getvalue(), err.getvalue()

    def log_files(self) -> list[Path]:
        root = self.home / "logs" / "demo"
        return sorted(root.glob("*/attempt-*.log")) if root.exists() else []

    def assert_draft_refused(self, draft: dict | str, expected: str) -> None:
        code, _out, err = self.run_draft(draft)
        self.assertEqual(code, 1)
        self.assertIn(expected, err)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.draft_names(), [])

    def test_valid_draft_creates_draft_ticket_and_cleans_worktree(self) -> None:
        """Un brouillon valide devient un ticket DRAFT, consomme un run et nettoie le worktree jetable."""
        self.worker(self.write_draft(VALID_DRAFT))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(["ticket", "draft", "demo", "Créer le module de calcul"])

        self.assertEqual(code, 0)
        t = self.db.get_ticket("demo", "T-001")
        self.assertIsNotNone(t)
        self.assertEqual(t["state"], "DRAFT")
        self.assertEqual(t["title"], VALID_DRAFT["title"])
        self.assertEqual(t["complexity"], "S")
        self.assertEqual(t["priority"], "P2")
        self.assertEqual(json.loads(t["files_owned_json"]), VALID_DRAFT["files"])
        self.assertEqual(json.loads(t["acceptance_json"]), VALID_DRAFT["accept"])
        self.assertEqual(t["contract"], VALID_DRAFT["contract"])
        self.assertEqual(self.draft_names(), [])
        self.assertIn(VALID_DRAFT["title"], out.getvalue())
        self.assertIn("T-001", out.getvalue())

        run = self.db.last_run("demo", "T-001")
        self.assertIsNotNone(run)
        self.assertEqual(run["lane"], "deepseek_coder")

    def test_run_worker_streams_by_default_and_only_logs_without_stream(self) -> None:
        """run_worker affiche la sortie par défaut et ne l'écrit que dans le log avec stream=False."""
        sentinel = "SENTINELLE-WORKER"
        self.worker(f'echo "{sentinel}"\n')
        log_default = self.base / "default.log"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, _ = pipeline.run_worker(["bash", str(self.script), "p"], self.root, log_default, 30)
        self.assertEqual(code, 0)
        self.assertIn(sentinel, out.getvalue())
        self.assertIn(sentinel, log_default.read_text(encoding="utf-8"))

        log_quiet = self.base / "quiet.log"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, _ = pipeline.run_worker(
                ["bash", str(self.script), "p"], self.root, log_quiet, 30, stream=False
            )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(sentinel, log_quiet.read_text(encoding="utf-8"))

    def test_draft_success_keeps_worker_journal_out_of_stdout(self) -> None:
        """Un brouillon réussi n'émet sur stdout que la synthèse, jamais le journal du worker."""
        self.worker("echo 'SENTINELLE-DRAFT'\n" + self.write_draft(VALID_DRAFT))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "draft", "demo", "Objectif"])

        self.assertEqual(code, 0)
        self.assertNotIn("SENTINELLE-DRAFT", out.getvalue())
        self.assertIn(VALID_DRAFT["title"], out.getvalue())
        logs = self.log_files()
        self.assertTrue(logs)
        self.assertTrue(any("SENTINELLE-DRAFT" in p.read_text(encoding="utf-8") for p in logs))

    def test_draft_failure_tail_is_bounded_to_five_truncated_lines(self) -> None:
        """En échec, stderr ne reprend que 5 lignes non vides tronquées, le log reste complet."""
        lines = [f"worker-line-{i:02d}" for i in range(1, 20)] + ["X" * 500]
        self.worker("".join(f'echo "{line}"\n' for line in lines) + "exit 3\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "draft", "demo", "Objectif"])

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        error = err.getvalue()
        self.assertIn("Dernieres lignes du log", error)
        for line in lines[-5:]:
            self.assertIn(line[:300], error)
        self.assertNotIn(lines[-6], error)
        self.assertIn("X" * 300, error)
        self.assertNotIn("X" * 301, error)
        logs = self.log_files()
        self.assertTrue(logs)
        self.assertTrue(
            any(all(line in p.read_text(encoding="utf-8") for line in lines) for p in logs)
        )

    def test_invalid_draft_retries_once_then_creates_no_ticket(self) -> None:
        """Un brouillon invalide déclenche une seule relance, avec l'erreur dans le prompt, puis aucun ticket."""
        invalid = dict(VALID_DRAFT, size="XL")
        self.worker(
            f'echo relance >> "{self.counter}"\n'
            + self.write_draft(invalid)
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "draft", "demo", "Objectif"])

        self.assertEqual(code, 1)
        self.assertEqual(len(self.counter.read_text(encoding="utf-8").splitlines()), 2)
        with self.db._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM tickets WHERE project_id = 'demo'").fetchone()["n"]
        self.assertEqual(count, 0)
        self.assertEqual(self.draft_names(), [])
        self.assertIn("taille", err.getvalue().lower())

    def test_draft_file_without_existing_parent_is_refused(self) -> None:
        """Un fichier absent dont le dossier parent n'existe pas est refusé, sans ticket."""
        self.assert_draft_refused(
            dict(VALID_DRAFT, files=["absent/absent.py"]),
            "n'existe pas et son dossier parent non plus",
        )

    def test_draft_paths_outside_repo_git_and_env_are_refused(self) -> None:
        """Aucun chemin hors dépôt, dans .git ou vers .env ne peut entrer dans un brouillon."""
        cases = {
            "../hors.py": "hors dépôt",
            ".git/config": ".git",
            ".env": ".env",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assert_draft_refused(dict(VALID_DRAFT, files=[path]), expected)

    def test_draft_more_than_eight_files_is_refused(self) -> None:
        """La liste de fichiers est bornée à huit entrées."""
        files = [f"core/fichier{i}.py" for i in range(9)]
        self.assert_draft_refused(dict(VALID_DRAFT, files=files), "Trop de fichiers")

    def test_draft_contract_over_1500_chars_is_refused(self) -> None:
        """Le contrat est borné à 1500 caractères."""
        self.assert_draft_refused(
            dict(VALID_DRAFT, contract="x" * 1501),
            "Contrat trop long",
        )

    def test_draft_empty_accept_is_refused(self) -> None:
        """Les critères d'acceptation doivent être non vides."""
        self.assert_draft_refused(dict(VALID_DRAFT, accept=[]), "acceptation sont vides")

    def test_draft_private_text_in_title_contract_or_accept_is_refused(self) -> None:
        """private_text_hits est appliqué au titre, au contrat et aux critères d'acceptation."""
        cfg = self.base / "config"
        cfg.mkdir()
        (cfg / "forbidden-words.txt").write_text("secret_projet_xyz\n", encoding="utf-8")
        cases = {
            "titre": dict(VALID_DRAFT, title="Projet secret_projet_xyz"),
            "contrat": dict(VALID_DRAFT, contract="Contrat secret_projet_xyz"),
            "accept": dict(VALID_DRAFT, accept=["Vérifier secret_projet_xyz"]),
        }
        with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(cfg)}):
            for name, draft in cases.items():
                with self.subTest(champ=name):
                    self.assert_draft_refused(draft, "Données privées détectées")

    def test_validate_draft_enforces_rule_order(self) -> None:
        """Quand plusieurs règles échouent, la première de l'ordre contractuel est renvoyée."""
        wt = self.root
        nine = [f"core/fichier{i}.py" for i in range(9)]
        cases = [
            (["absent/absent.py"], "n'existe pas"),
            (["../dehors.py"], "hors dépôt"),
            ([".git/config"], ".git"),
            ([".env"], ".env"),
            (nine, "Trop de fichiers"),
            (VALID_DRAFT["files"], "Taille invalide"),
        ]
        for files, expected in cases:
            with self.subTest(files=files):
                draft = dict(
                    VALID_DRAFT,
                    files=files,
                    size="XL",
                    prio="P9",
                    contract="x" * 1600,
                    accept=[],
                )
                refusal = drafter.validate_draft(draft, wt)
                self.assertIsNotNone(refusal)
                self.assertIn(expected, refusal or "")
        draft = dict(VALID_DRAFT, prio="P9", contract="x" * 1600, accept=[])
        self.assertIn("Priorité invalide", drafter.validate_draft(draft, wt) or "")
        draft = dict(VALID_DRAFT, contract="x" * 1600, accept=[])
        self.assertIn("Contrat trop long", drafter.validate_draft(draft, wt) or "")
        draft = dict(VALID_DRAFT, accept=[])
        self.assertIn("acceptation sont vides", drafter.validate_draft(draft, wt) or "")

    def test_retry_reinjects_validation_error_in_prompt(self) -> None:
        """La relance passe au worker le prompt contenant l'erreur de validation précédente."""
        prompts = self.base / "prompts.txt"
        invalid = dict(VALID_DRAFT, size="XL")
        body = (
            f'printf "===PROMPT===\\n%s\\n" "$1" >> "{prompts}"\n'
            f'if [ -f "{self.counter}" ]; then\n'
            + self.write_draft(VALID_DRAFT)
            + "else\n"
            + f'touch "{self.counter}"\n'
            + self.write_draft(invalid)
            + "fi\n"
        )
        self.worker(body)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(["ticket", "draft", "demo", "Objectif"])

        self.assertEqual(code, 0)
        self.assertEqual(self.ticket_count(), 1)
        sent = [part for part in prompts.read_text(encoding="utf-8").split("===PROMPT===") if part.strip()]
        self.assertEqual(len(sent), 2)
        self.assertNotIn("Taille invalide", sent[0])
        self.assertIn("Taille invalide", sent[1])
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "DRAFT")

    def test_auto_and_run_never_take_a_draft(self) -> None:
        """DRAFT n'est ni planifié par auto ni exécutable par run avant promotion."""
        self.db.upsert_ticket(
            "demo", "T-001", "Brouillon", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/calcul.py"],
        )
        self.assertEqual(scheduler.plan_batch(self.db, "demo", slots=4), [])

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["run", "T-001", "demo", "--no-gate1"])
        self.assertEqual(code, 1)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "DRAFT")
        self.assertIn("DRAFT", err.getvalue())

    def test_promote_draft_without_dependency_becomes_ready(self) -> None:
        """promote passe un DRAFT sans dépendance à READY."""
        self.db.upsert_ticket(
            "demo", "T-001", "Brouillon", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/calcul.py"],
        )
        self.assertEqual(pipeline.main(["ticket", "promote", "T-001", "demo"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "READY")

    def test_promote_draft_with_unmerged_dependency_becomes_blocked(self) -> None:
        """promote passe un DRAFT à BLOCKED si une dépendance n'est pas MERGED."""
        self.db.upsert_ticket(
            "demo", "T-000", "Dépendance", milestone_id="M1", complexity="S", state="GATE2",
            files_owned=["core/base.py"],
        )
        self.db.upsert_ticket(
            "demo", "T-001", "Brouillon", milestone_id="M1", complexity="S", state="DRAFT",
            files_owned=["core/calcul.py"], depends_on=["T-000"],
        )
        self.assertEqual(pipeline.main(["ticket", "promote", "T-001", "demo"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "BLOCKED")

    def test_promote_refuses_non_draft(self) -> None:
        """promote refuse tout ticket qui n'est pas DRAFT et ne change pas son état."""
        self.db.upsert_ticket(
            "demo", "T-001", "Déjà prêt", milestone_id="M1", complexity="S", state="READY",
            files_owned=["core/calcul.py"],
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["ticket", "promote", "T-001", "demo"])
        self.assertEqual(code, 1)
        self.assertEqual(self.db.get_ticket("demo", "T-001")["state"], "READY")
        self.assertIn("DRAFT", err.getvalue())


if __name__ == "__main__":
    unittest.main()
