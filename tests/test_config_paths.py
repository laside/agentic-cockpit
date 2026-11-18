#!/usr/bin/env python3
"""Tests unitaires pour la résolution de la configuration externalisée (T-031)."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import config_paths  # noqa: E402
import judge_calibrate  # noqa: E402
import pipeline  # noqa: E402


class TestConfigPaths(unittest.TestCase):
    def test_config_dir_uses_env_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": tmp}, clear=False):
                self.assertEqual(config_paths.config_dir(), Path(tmp))

    def test_config_dir_default_under_home(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home:
            env = {k: v for k, v in os.environ.items() if k != "COCKPIT_CONFIG_DIR"}
            env["HOME"] = fake_home
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(
                    config_paths.config_dir(),
                    Path(fake_home) / ".config" / "agentic-cockpit",
                )

    def test_config_file_step1_overlay(self) -> None:
        """Étape 1 : config_dir()/name prime sur cockpit_home/config/name et .example."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            overlay = base / "overlay"
            home = base / "cockpit"
            overlay.mkdir()
            (home / "config").mkdir(parents=True)

            f_overlay = overlay / "runners.toml"
            f_repo = home / "config" / "runners.toml"
            f_example = home / "config" / "runners.example.toml"
            f_overlay.write_text("[runners]\n", encoding="utf-8")
            f_repo.write_text("[runners]\n", encoding="utf-8")
            f_example.write_text("[runners]\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                resolved = config_paths.config_file("runners.toml", cockpit_home=home)
                self.assertEqual(resolved, f_overlay)

    def test_config_file_step2_repo_fallback(self) -> None:
        """Étape 2 : <cockpit_home>/config/name s'il existe quand la surcouche n'a pas le fichier."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            overlay = base / "overlay-vide"
            home = base / "cockpit"
            (home / "config").mkdir(parents=True)

            f_repo = home / "config" / "runners.toml"
            f_example = home / "config" / "runners.example.toml"
            f_repo.write_text("[runners]\n", encoding="utf-8")
            f_example.write_text("[runners]\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                resolved = config_paths.config_file("runners.toml", cockpit_home=home)
                self.assertEqual(resolved, f_repo)

    def test_config_file_step3_example_fallback_even_if_missing(self) -> None:
        """Étape 3 : <cockpit_home>/config/<stem>.example<suffix>, retourné même s'il n'existe pas."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            overlay = base / "overlay-absent"
            home = base / "cockpit"
            (home / "config").mkdir(parents=True)
            expected = home / "config" / "projects.example.toml"

            with mock.patch.dict(
                os.environ,
                {"COCKPIT_CONFIG_DIR": str(overlay), "COCKPIT_HOME": str(home)},
                clear=False,
            ):
                # Sans que projects.example.toml existe encore
                self.assertEqual(config_paths.config_file("projects.toml"), expected)
                # Et quand il existe
                expected.write_text('default_project = "cockpit"\n', encoding="utf-8")
                self.assertEqual(config_paths.config_file("projects.toml"), expected)

    def test_load_runners_reads_overlay_and_reports_path_on_error(self) -> None:
        """load_runners() lit la surcouche pointée par COCKPIT_CONFIG_DIR et cite le chemin résolu en cas d'erreur."""
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "cfg"
            overlay.mkdir()
            runners_toml = overlay / "runners.toml"
            runners_toml.write_text(
                '[runners.custom_lane]\nagent = "local"\nprovider = "local"\nargv = ["true"]\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                cfg = pipeline.load_runners()
                self.assertIn("custom_lane", cfg["runners"])

            # Fichier invalide (sans section [runners]) -> message d'erreur citant le chemin résolu
            runners_toml.write_text("[defaults]\ntimeout_s = 10\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                with self.assertRaises(pipeline.PipelineError) as ctx:
                    pipeline.load_runners()
                self.assertIn(str(runners_toml), str(ctx.exception))

    def test_project_commands_reads_overlay_projects_toml(self) -> None:
        """project_commands() résout projects.toml via config_file()."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            overlay = base / "cfg"
            proj_dir = base / "proj"
            overlay.mkdir()
            proj_dir.mkdir()
            (overlay / "projects.toml").write_text(
                '[projects.demo]\nlint_cmd = "ruff check"\ntest_unit_cmd = "pytest -q"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                lint_cmd, test_cmd = pipeline.project_commands("demo", proj_dir, proj_dir)
                self.assertEqual(lint_cmd, "ruff check")
                self.assertEqual(test_cmd, "pytest -q")

    def test_judge_calibrate_runners_path_uses_config_file(self) -> None:
        """judge_calibrate.RUNNERS_PATH est résolu via config_paths.config_file('runners.toml')."""
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "overlay"
            overlay.mkdir()
            overlay_runners = overlay / "runners.toml"
            overlay_runners.write_text("[runners]\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(overlay)}, clear=False):
                importlib.reload(judge_calibrate)
                self.assertEqual(judge_calibrate.RUNNERS_PATH, overlay_runners)
        importlib.reload(judge_calibrate)
        self.assertEqual(
            judge_calibrate.RUNNERS_PATH,
            config_paths.config_file("runners.toml"),
        )

    def test_cockpit_env_sh_defaults_and_overlay(self) -> None:
        """cockpit-env.sh utilise PROJECT=cockpit par défaut et résout la surcouche."""
        repo = Path(__file__).resolve().parent.parent
        script = repo / "scripts" / "cockpit-env.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            overlay = base / "cfg"
            fake_home.mkdir()
            overlay.mkdir()

            # Sans surcouche -> retombe sur config/projects.example.toml avec PROJECT=cockpit
            cmd = (
                f"source {script} && "
                "printf '%s\\n%s\\n%s\\n%s\\n' \"$PROJECT\" \"$PROJECT_NAME\" \"$PROJECT_ROOT\" \"$MIRROR_DIR\""
            )
            env = {
                "HOME": str(fake_home),
                "COCKPIT_HOME": str(repo),
                "COCKPIT_CONFIG_DIR": str(overlay),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=True)
            lines = res.stdout.splitlines()
            self.assertEqual(lines[0], "cockpit")
            self.assertEqual(lines[1], "Agentic Cockpit")
            self.assertEqual(lines[2], f"{fake_home}/agentic-cockpit")
            self.assertEqual(lines[3], "")

            # Avec un projet dans la surcouche sans mirror_dir -> défauts génériques ~/projets/<id>
            (overlay / "projects.toml").write_text('[projects.alpha]\nname = "Alpha"\n', encoding="utf-8")
            env["PROJECT"] = "alpha"
            res2 = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=True)
            lines2 = res2.stdout.splitlines()
            self.assertEqual(lines2[0], "alpha")
            self.assertEqual(lines2[1], "Alpha")
            self.assertEqual(lines2[2], f"{fake_home}/projets/alpha")
            self.assertEqual(lines2[3], "")

    def test_new_project_sh_initializes_overlay_and_git_identity(self) -> None:
        """new-project.sh écrit dans la surcouche COCKPIT_CONFIG_DIR/projects.toml et utilise cockpit@localhost sans config git globale."""
        repo = Path(__file__).resolve().parent.parent
        script = repo / "scripts" / "new-project.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            overlay = base / "overlay"
            proj_root = base / "projets" / "beta"
            db_path = base / "pmo.db"
            fake_home.mkdir()

            env = {
                "HOME": str(fake_home),
                "COCKPIT_HOME": str(repo),
                "COCKPIT_CONFIG_DIR": str(overlay),
                "DB_PATH": str(db_path),
                "GIT_CONFIG_GLOBAL": str(fake_home / ".gitconfig-vide"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            subprocess.run(
                ["bash", str(script), "beta", str(proj_root), "8812"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            overlay_toml = overlay / "projects.toml"
            self.assertTrue(overlay_toml.exists())
            content = overlay_toml.read_text(encoding="utf-8")
            self.assertIn("[projects.cockpit]", content)
            self.assertIn("[projects.beta]", content)

            user_name = subprocess.run(
                ["git", "-C", str(proj_root), "config", "user.name"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            user_email = subprocess.run(
                ["git", "-C", str(proj_root), "config", "user.email"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(user_name, "cockpit")
            self.assertEqual(user_email, "cockpit@localhost")


if __name__ == "__main__":
    unittest.main()
