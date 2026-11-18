#!/usr/bin/env python3
"""Tests unitaires de durcissement sécurité (T-013) : validations, injections, chemins hors dépôt."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.context_compiler import compile_ticket_brief

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestHardening(unittest.TestCase):
    """Vérifie le durcissement des scripts shell et du compilateur de contexte."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_cockpit_secret_invalid_key_refused_without_touching_file(self) -> None:
        """Une clé secrète invalide est refusée (code 1) avant lecture ou création de .env.cockpit."""
        cockpit_bin = REPO_ROOT / "bin" / "cockpit"
        env_file = self.tmp / ".env.cockpit"
        env = {
            **os.environ,
            "COCKPIT_HOME": str(self.tmp),
            "DB_PATH": str(self.tmp / "db" / "cockpit_pmo.db"),
        }
        for bad_key in ("minuscule", "123_KEY", "BAD-KEY", "KEY;rm -rf", "KEY'=1"):
            res = subprocess.run(
                ["bash", str(cockpit_bin), "secret", bad_key],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(res.returncode, 1, f"La clé invalide {bad_key!r} aurait dû être refusée")
            self.assertFalse(env_file.exists(), ".env.cockpit ne doit pas être créé sur une clé invalide")

    def test_dispatch_ticket_invalid_ticket_or_project_refused(self) -> None:
        """dispatch-ticket.sh refuse un ticket ou un projet invalide sans appeler Zellij ni la base."""
        script = REPO_ROOT / "scripts" / "dispatch-ticket.sh"
        marker = self.tmp / "zellij_called"
        fake_bin = self.tmp / "bin"
        fake_bin.mkdir()
        fake_zellij = fake_bin / "zellij"
        fake_zellij.write_text(f"#!/usr/bin/env bash\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
        fake_zellij.chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "COCKPIT_HOME": str(self.tmp),
            "DB_PATH": str(self.tmp / "nonexistent.db"),
        }
        invalid_pairs = [
            ("T-abc", "demo"),
            ("ticket-1", "demo"),
            ("T-12;id", "demo"),
            ("T-013", "Demo"),
            ("T-013", "9proj"),
            ("T-013", "bad_underscore"),
            ("T-013", "proj;evil"),
        ]
        for ticket, project in invalid_pairs:
            res = subprocess.run(
                ["bash", str(script), ticket, project],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(res.returncode, 1, f"Couple ({ticket!r}, {project!r}) aurait dû échouer")
            self.assertTrue(res.stderr.strip(), "Un message d'erreur sur stderr est attendu")
            self.assertFalse(marker.exists(), "Zellij ne doit jamais être exécuté sur entrée invalide")

    def test_new_project_invalid_id_or_port_refused_before_creation(self) -> None:
        """new-project.sh refuse un identifiant ou un port invalide avant toute création."""
        script = REPO_ROOT / "scripts" / "new-project.sh"
        toml_path = self.tmp / "config" / "projects.toml"
        db_path = self.tmp / "db" / "cockpit_pmo.db"
        env = {
            **os.environ,
            "HOME": str(self.tmp),
            "COCKPIT_HOME": str(REPO_ROOT),
            "PROJECTS_TOML": str(toml_path),
            "DB_PATH": str(db_path),
        }
        invalid_cases = [
            ("123proj", "8790"),
            ("-proj", "8790"),
            ("bad_underscore", "8790"),
            ("Majuscule", "8790"),
            ("valide-id", "80"),
            ("valide-id", "1023"),
            ("valide-id", "65536"),
            ("valide-id", "notaport"),
        ]
        for proj_id, port in invalid_cases:
            proj_root = self.tmp / "projets" / proj_id
            res = subprocess.run(
                ["bash", str(script), proj_id, str(proj_root), port],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(res.returncode, 1, f"Cas ({proj_id!r}, {port!r}) aurait dû être refusé")
            self.assertTrue(res.stderr.strip(), "Un message d'erreur sur stderr est attendu")
            self.assertFalse(proj_root.exists(), "Aucun dossier projet ne doit être créé")
            self.assertFalse(toml_path.exists(), "projects.toml ne doit pas être créé/modifié")
            self.assertFalse(db_path.exists(), "La base SQLite ne doit pas être créée")

    def test_context_compiler_ignores_outside_readonly_files(self) -> None:
        """compile_ticket_brief ignore et signale tout chemin hors dépôt sans jamais le lire."""
        proj_root = self.tmp / "repo"
        proj_root.mkdir()
        rules_dir = proj_root / ".nexus" / "rules"
        rules_dir.mkdir(parents=True)

        inside_file = proj_root / "module_interne.py"
        inside_file.write_text("def fonction_publique(x: int) -> int:\n    return x + 1\n", encoding="utf-8")

        outside_file = self.tmp / "secret_externe.py"
        secret_marker = "SECRET_ABSOLUMENT_INTERDIT_DE_LIRE"
        outside_file.write_text(f"def {secret_marker}() -> None:\n    pass\n", encoding="utf-8")

        symlink_escape = proj_root / "lien_sortant.py"
        symlink_escape.symlink_to(outside_file)

        ticket = {
            "id": "T-013",
            "title": "Test durcissement",
            "milestone_id": "C1",
            "priority": "P1",
            "complexity": "M",
            "files_owned_json": '["core/context_compiler.py"]',
            "acceptance_json": '["Critère 1"]',
        }
        brief = compile_ticket_brief(
            project_root=proj_root,
            ticket=ticket,
            rules_dir=rules_dir,
            readonly_files=[
                "module_interne.py",
                str(outside_file.resolve()),
                "../secret_externe.py",
                "sub/../../secret_externe.py",
                "lien_sortant.py",
            ],
        )
        self.assertIn("def fonction_publique(x: int) -> int: ...", brief)
        self.assertNotIn(secret_marker, brief)
        self.assertGreaterEqual(brief.count("fichier ignoré : hors du dépôt"), 4)


if __name__ == "__main__":
    unittest.main()
