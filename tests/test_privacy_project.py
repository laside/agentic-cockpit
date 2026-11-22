"""Tests T-056 : mots interdits par projet et refus explicites (sans réseau)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "core"))

import config_paths  # noqa: E402
import pipeline  # noqa: E402

PRIVACY_SCRIPT = ROOT_DIR / "scripts" / "privacy-check.py"
INSTALL_HOOKS = ROOT_DIR / "scripts" / "install-hooks.sh"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()
        self._old = os.environ.get("COCKPIT_CONFIG_DIR")
        os.environ["COCKPIT_CONFIG_DIR"] = str(self.cfg)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._old is None:
            os.environ.pop("COCKPIT_CONFIG_DIR", None)
        else:
            os.environ["COCKPIT_CONFIG_DIR"] = self._old


class TestForbiddenWordsPath(_Base):
    def test_projet_prioritaire(self) -> None:
        (self.cfg / "forbidden-words.txt").write_text("general\n", encoding="utf-8")
        (self.cfg / "forbidden-words.alpha.txt").write_text("alpha\n", encoding="utf-8")
        self.assertEqual(config_paths.forbidden_words_path("alpha"), self.cfg / "forbidden-words.alpha.txt")

    def test_repli_general(self) -> None:
        (self.cfg / "forbidden-words.txt").write_text("general\n", encoding="utf-8")
        self.assertEqual(config_paths.forbidden_words_path("beta"), self.cfg / "forbidden-words.txt")
        self.assertEqual(config_paths.forbidden_words_path(), self.cfg / "forbidden-words.txt")

    def test_matches_ligne_reelle_et_liste_du_projet(self) -> None:
        (self.cfg / "forbidden-words.txt").write_text("general\n", encoding="utf-8")
        (self.cfg / "forbidden-words.alpha.txt").write_text("# c\n\n# d\nmotalpha\n", encoding="utf-8")
        self.assertEqual(
            pipeline.private_text_matches("un MotAlpha ici", "alpha"), [(4, "forbidden-words.alpha.txt")]
        )
        self.assertEqual(pipeline.private_text_matches("un MotAlpha ici", "beta"), [])


class TestPrivacyCli(_Base):
    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PRIVACY_SCRIPT), "--repo", str(repo), *args],
            capture_output=True, text=True, env=os.environ.copy(), check=False,
        )

    def _repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / "a.txt").write_text("ok\nsecretalpha\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        return repo

    def test_option_project_ligne_reelle_sans_motif(self) -> None:
        repo = self._repo()
        (self.cfg / "forbidden-words.alpha.txt").write_text("# c\n# d\nsecretalpha\n", encoding="utf-8")
        res = self._run(repo, "--project", "alpha")
        self.assertEqual(res.returncode, 1)
        self.assertIn("ligne 3 de forbidden-words.alpha.txt", res.stdout)
        self.assertNotIn("secretalpha", res.stdout)

    def test_sans_option_liste_generale(self) -> None:
        repo = self._repo()
        (self.cfg / "forbidden-words.txt").write_text("rien\n", encoding="utf-8")
        (self.cfg / "forbidden-words.alpha.txt").write_text("secretalpha\n", encoding="utf-8")
        self.assertEqual(self._run(repo).returncode, 0)
        self.assertEqual(self._run(repo, "--project", "alpha").returncode, 1)


class TestPrDeleted(_Base):
    def test_fichiers_supprimes_exclus(self) -> None:
        wt = self.tmp / "wt"
        wt.mkdir()
        (wt / "reste.py").write_text("x\n", encoding="utf-8")
        kept, deleted = pipeline.split_deleted(wt, ["reste.py", "parti.py"])
        self.assertEqual((kept, deleted), (["reste.py"], ["parti.py"]))
        section = pipeline._files_section(kept, deleted)
        self.assertNotIn("parti.py", section)
        self.assertIn("1 fichier(s) supprimé(s)", section)


class TestHook(_Base):
    def test_hook_retrouve_le_projet_par_sa_racine(self) -> None:
        repo = self.tmp / "depot"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (self.cfg / "projects.toml").write_text(
            f'[projects.alpha]\nroot = "{repo}"\n[projects.beta]\nroot = "{self.tmp / "autre"}"\n',
            encoding="utf-8",
        )
        scripts = repo / "scripts"
        scripts.mkdir()
        (scripts / "privacy-check.py").write_text(
            "import sys\nprint('ARGS', *sys.argv[1:])\n", encoding="utf-8"
        )
        subprocess.run(["bash", str(INSTALL_HOOKS)], cwd=repo, check=True, capture_output=True)
        hook = repo / ".git" / "hooks" / "pre-push"
        sha = "1" * 40
        res = subprocess.run(
            [str(hook)], cwd=repo, input=f"refs/heads/x {sha} refs/heads/x {'0' * 40}\n",
            capture_output=True, text=True, env=os.environ.copy(), check=False,
        )
        self.assertIn("--project alpha", res.stdout)
        (self.cfg / "projects.toml").write_text("[projects.beta]\nroot = \"/nulle/part\"\n", encoding="utf-8")
        res = subprocess.run(
            [str(hook)], cwd=repo, input=f"refs/heads/x {sha} refs/heads/x {'0' * 40}\n",
            capture_output=True, text=True, env=os.environ.copy(), check=False,
        )
        self.assertNotIn("--project", res.stdout)


if __name__ == "__main__":
    unittest.main()
