"""Tests unitaires pour le garde de confidentialité (T-034)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PRIVACY_SCRIPT = ROOT_DIR / "scripts" / "privacy-check.py"
INSTALL_HOOKS_SCRIPT = ROOT_DIR / "scripts" / "install-hooks.sh"
CI_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "ci.yml"
ZERO_SHA = "0" * 40


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path, email: str = "dev@example.com", name: str = "Dev Test") -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", email)
    _git(repo, "config", "user.name", name)


class TestPrivacyCheck(unittest.TestCase):
    """Vérifie le comportement de scripts/privacy-check.py, install-hooks.sh et ci.yml."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.repo = self.tmp_path / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)

        self.deny_file = self.tmp_path / "forbidden-words.txt"
        self.deny_file.write_text(
            "# Commentaire ignoré\n\nmotsecret\nprojet_confidentiel_[0-9]+\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_check(
        self,
        *args: str,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(PRIVACY_SCRIPT), "--repo", str(self.repo), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_allow_marker_skips_generic_checks_but_not_deny_list(self) -> None:
        """Le marqueur de fin de ligne exempte des contrôles génériques, jamais de la liste privée."""
        marker = "privacy-check" + ": allow"
        doc = self.repo / "fixture.txt"
        doc.write_text(f"ip 10.9.8.7 et a@b-prive.fr  # {marker}\n", encoding="utf-8")  # privacy-check: allow
        _git(self.repo, "add", "fixture.txt")
        self.assertEqual(self._run_check("--generic").returncode, 0)
        deny = self.tmp_path / "deny.txt"
        deny.write_text("b-prive\n", encoding="utf-8")
        self.assertEqual(self._run_check("--deny-file", str(deny)).returncode, 1)

    def test_tracked_file_with_forbidden_word_fails_without_leaking_line(self) -> None:
        """Fichier suivi contenant un mot interdit -> code 1, sans réafficher la ligne entière."""
        secret_line = "Voici une phrase complète contenant MOTSECRET au milieu."
        target = self.repo / "notes.txt"
        target.write_text(f"Ligne saine\n{secret_line}\n", encoding="utf-8")
        _git(self.repo, "add", "notes.txt")

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 1)
        self.assertIn("notes.txt:2:", res.stdout)
        self.assertNotIn(secret_line, res.stdout)
        self.assertNotIn(secret_line, res.stderr)

    def test_untracked_file_is_ignored(self) -> None:
        """Fichier non suivi contenant un mot interdit ou une IP privée -> ignoré (code 0)."""
        clean = self.repo / "propre.txt"
        clean.write_text("Contenu parfaitement public.\n", encoding="utf-8")
        _git(self.repo, "add", "propre.txt")

        untracked = self.repo / "brouillon.txt"
        untracked.write_text("motsecret et 10.1.2.3 et secret@domaine-prive.fr\n", encoding="utf-8")  # privacy-check: allow

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "")

    def test_tracked_file_name_with_forbidden_word_fails_without_leaking_name(self) -> None:
        """Nom de fichier suivi contenant un mot interdit -> code 1, sans afficher le nom."""
        target = self.repo / "motsecret_notes.txt"
        target.write_text("Contenu parfaitement public.\n", encoding="utf-8")
        _git(self.repo, "add", "motsecret_notes.txt")

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 1)
        self.assertIn("mot-interdit#", res.stdout)
        self.assertNotIn("motsecret", res.stdout)
        self.assertNotIn("motsecret", res.stderr)

    def test_tracked_directory_name_with_forbidden_word_fails_without_leaking_name(self) -> None:
        """Nom de dossier suivi contenant un mot interdit -> code 1, sans afficher le chemin."""
        folder = self.repo / "motsecret_dossier"
        folder.mkdir()
        (folder / "notes.txt").write_text("Contenu public.\n", encoding="utf-8")
        _git(self.repo, "add", "motsecret_dossier/notes.txt")

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 1)
        self.assertIn("mot-interdit#", res.stdout)
        self.assertNotIn("motsecret", res.stdout)
        self.assertNotIn("notes.txt", res.stdout)

    def test_clean_tracked_path_is_accepted(self) -> None:
        """Un chemin suivi sans motif interdit -> code 0."""
        folder = self.repo / "documents"
        folder.mkdir()
        (folder / "notes_propres.txt").write_text("Contenu public.\n", encoding="utf-8")
        _git(self.repo, "add", "documents/notes_propres.txt")

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)

    def test_generic_flag_checks_paths_for_private_ip_without_leaking_name(self) -> None:
        """--generic applique l'IPv4 RFC1918 aux chemins sans afficher le nom."""
        target = self.repo / "10.42.0.7.txt"
        target.write_text("Contenu public.\n", encoding="utf-8")
        _git(self.repo, "add", "10.42.0.7.txt")

        res = self._run_check("--generic")
        self.assertEqual(res.returncode, 1)
        self.assertIn("ipv4-rfc1918", res.stdout)
        self.assertNotIn("10.42.0.7", res.stdout)

    def test_generic_flag_ignores_deny_words_in_paths(self) -> None:
        """--generic n'applique pas la liste privée aux chemins."""
        target = self.repo / "motsecret_notes.txt"
        target.write_text("Contenu public.\n", encoding="utf-8")
        _git(self.repo, "add", "motsecret_notes.txt")

        res = self._run_check("--generic")
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)

    def test_binary_file_is_ignored(self) -> None:
        """Fichier binaire suivi -> ignoré sans erreur."""
        binary = self.repo / "image.bin"
        binary.write_bytes(b"\x00\x01\x02motsecret\x0010.0.0.1")
        _git(self.repo, "add", "image.bin")

        res = self._run_check("--deny-file", str(self.deny_file))
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)

    def test_rfc1918_ips_and_exemptions(self) -> None:
        """IP RFC1918 -> code 1 ; 192.168.0.10 et 127.0.0.1 -> code 0."""
        allowed = self.repo / "config.txt"
        allowed.write_text(
            "Serveur exemple: 192.168.0.10\nLoopback: 127.0.0.1\nPublic: 8.8.8.8\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "config.txt")

        res_ok = self._run_check("--generic")
        self.assertEqual(res_ok.returncode, 0, msg=res_ok.stdout + res_ok.stderr)

        for bad_ip in ("10.0.0.1", "172.16.5.4", "172.31.255.1", "192.168.1.25"):  # privacy-check: allow
            with self.subTest(ip=bad_ip):
                allowed.write_text(f"Hôte interne: {bad_ip} actif\n", encoding="utf-8")
                res_bad = self._run_check("--generic")
                self.assertEqual(res_bad.returncode, 1)
                self.assertIn("config.txt:1:", res_bad.stdout)
                self.assertNotIn(f"Hôte interne: {bad_ip} actif", res_bad.stdout)

    def test_emails_allowed_and_forbidden(self) -> None:
        """Adresses e-mail autorisées -> code 0 ; autres domaines -> code 1."""
        doc = self.repo / "contacts.md"
        doc.write_text(
            "Exemples: a@example.com, b@example.org, root@localhost, "
            "123+bot@users.noreply.github.com, claude@anthropic.com\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "contacts.md")

        res_ok = self._run_check("--generic")
        self.assertEqual(res_ok.returncode, 0, msg=res_ok.stdout + res_ok.stderr)

        doc.write_text("Contact privé: utilisateur@domaine-interne.org en copie\n", encoding="utf-8")  # privacy-check: allow
        res_bad = self._run_check("--generic")
        self.assertEqual(res_bad.returncode, 1)
        self.assertIn("contacts.md:1:", res_bad.stdout)
        self.assertNotIn("Contact privé: utilisateur@domaine-interne.org en copie", res_bad.stdout)  # privacy-check: allow

    def test_github_web_flow_address_allowed_but_other_github_com_rejected(self) -> None:
        doc = self.repo / "gh.md"
        doc.write_text("Committer des squash : noreply@github.com\n", encoding="utf-8")
        _git(self.repo, "add", "gh.md")
        res_ok = self._run_check("--generic")
        self.assertEqual(res_ok.returncode, 0, msg=res_ok.stdout + res_ok.stderr)

        doc.write_text("Contact : quelquun@github.com\n", encoding="utf-8")  # privacy-check: allow
        res_bad = self._run_check("--generic")
        self.assertEqual(res_bad.returncode, 1)

    def test_generic_flag_does_not_open_deny_file(self) -> None:
        """--generic n'ouvre jamais la liste privée, même si elle correspondrait."""
        f = self.repo / "doc.txt"
        f.write_text("Ce fichier contient motsecret mais aucune IP ni email.\n", encoding="utf-8")
        _git(self.repo, "add", "doc.txt")

        # Liste lisible et correspondante : si elle était chargée, --generic renverrait 1.
        deny_list = self.tmp_path / "deny-lisible.txt"
        deny_list.write_text("motsecret\n", encoding="utf-8")

        res = self._run_check(
            "--generic",
            "--deny-file",
            str(deny_list),
            env_extra={"COCKPIT_CONFIG_DIR": str(self.tmp_path)},
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertNotIn("Avertissement", res.stderr)

    def test_missing_deny_file_warns_on_stderr_and_runs_generic(self) -> None:
        """Si la liste privée est absente, avertissement sur stderr et contrôles génériques actifs."""
        f = self.repo / "doc.txt"
        f.write_text("Texte propre.\n", encoding="utf-8")
        _git(self.repo, "add", "doc.txt")

        missing = self.tmp_path / "inexistant.txt"
        res = self._run_check("--deny-file", str(missing))
        self.assertEqual(res.returncode, 0)
        self.assertNotEqual(res.stderr.strip(), "")

    def test_range_detects_forbidden_author_email_and_new_branch(self) -> None:
        """--range A..B détecte un e-mail d'auteur interdit et gère A = 40 zéros."""
        f = self.repo / "code.py"
        f.write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "code.py")
        _git(self.repo, "commit", "-m", "Commit initial propre")
        sha1 = _git(self.repo, "rev-parse", "HEAD")

        f.write_text("x = 2\n", encoding="utf-8")
        _git(self.repo, "add", "code.py")
        _git(
            self.repo,
            "-c",
            "user.email=employe@entreprise-privee.fr",  # privacy-check: allow
            "commit",
            "-m",
            "Second commit",
        )
        sha2 = _git(self.repo, "rev-parse", "HEAD")

        res_range = self._run_check("--generic", "--range", f"{sha1}..{sha2}")
        self.assertEqual(res_range.returncode, 1)
        self.assertIn(f"commit {sha2[:7]}:", res_range.stdout)

        res_new_branch = self._run_check("--generic", "--range", f"{ZERO_SHA}..{sha2}")
        self.assertEqual(res_new_branch.returncode, 1)
        self.assertIn(f"commit {sha2[:7]}:", res_new_branch.stdout)

    def test_install_hooks_and_pre_push_execution(self) -> None:
        """Installe le hook pre-push, vérifie l'idempotence, son exécution et la protection d'un hook étranger."""
        res1 = subprocess.run(
            ["bash", str(INSTALL_HOOKS_SCRIPT)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res1.returncode, 0, msg=res1.stdout + res1.stderr)

        hook_path = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook_path.is_file())
        self.assertTrue(os.access(hook_path, os.X_OK))

        # Idempotent au second appel
        res2 = subprocess.run(
            ["bash", str(INSTALL_HOOKS_SCRIPT)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res2.returncode, 0, msg=res2.stdout + res2.stderr)

        # Le hook résout privacy-check.py depuis la racine du dépôt : on l'y copie.
        shutil.copytree(ROOT_DIR / "scripts", self.repo / "scripts")

        code = self.repo / "code.py"
        code.write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "code.py")
        _git(self.repo, "commit", "-m", "Commit initial propre")
        clean_sha = _git(self.repo, "rev-parse", "HEAD")

        code.write_text("x = 2\n", encoding="utf-8")
        _git(self.repo, "add", "code.py")
        _git(
            self.repo,
            "-c",
            "user.email=employe@entreprise-privee.fr",  # privacy-check: allow
            "commit",
            "-m",
            "Commit privé",
        )
        dirty_sha = _git(self.repo, "rev-parse", "HEAD")

        # Push autorisé : la plage ne contient que le commit propre.
        clean_push = subprocess.run(
            [str(hook_path)],
            cwd=str(self.repo),
            input=f"refs/heads/main {clean_sha} refs/heads/main {ZERO_SHA}\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(clean_push.returncode, 0, msg=clean_push.stdout + clean_push.stderr)

        # Push refusé : l'e-mail d'auteur du commit est interdit.
        dirty_push = subprocess.run(
            [str(hook_path)],
            cwd=str(self.repo),
            input=f"refs/heads/main {dirty_sha} refs/heads/main {clean_sha}\n",
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(dirty_push.returncode, 0)
        self.assertIn(f"commit {dirty_sha[:7]}:", dirty_push.stdout)

        # Refuse d'écraser un hook étranger
        hook_path.write_text("#!/bin/sh\necho custom hook\n", encoding="utf-8")
        res_foreign = subprocess.run(
            ["bash", str(INSTALL_HOOKS_SCRIPT)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_foreign.returncode, 1)
        self.assertNotEqual((res_foreign.stdout + res_foreign.stderr).strip(), "")

    def test_ci_workflow_exists_and_configured(self) -> None:
        """Vérifie la présence et le contenu requis de .github/workflows/ci.yml."""
        self.assertTrue(CI_WORKFLOW.is_file())
        content = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("tests:", content)
        self.assertIn("ubuntu-latest", content)
        self.assertIn("3.12", content)
        self.assertIn("python3 -B -m unittest discover -s tests -p 'test_*.py'", content)
        self.assertIn("python3 scripts/privacy-check.py --generic", content)


if __name__ == "__main__":
    unittest.main()
