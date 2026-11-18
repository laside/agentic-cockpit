#!/usr/bin/env python3
"""Tests de routage CLI de cockpit lessons (bin/cockpit).

Le lanceur est exécuté sur une copie temporaire du dépôt : le fichier réel
templates/core.md, la base db/cockpit_pmo.db, le réseau et un LLM ne sont
jamais touchés. Base SQLite temporaire créée via CockpitDB.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

from pmo_db import CockpitDB  # noqa: E402

SECTION_BEGIN = "<!-- BEGIN COCKPIT LESSONS -->"
SECTION_END = "<!-- END COCKPIT LESSONS -->"
MOTIF_RECURRENT = "Gate0: échec de compilation du module paiement"


class TestRoutageLessonsDansLeLanceur(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

        self.home = self.tmp_path / "cockpit-home"
        self.home.mkdir()
        shutil.copytree(COCKPIT_ROOT / "bin", self.home / "bin")
        shutil.copytree(COCKPIT_ROOT / "core", self.home / "core")

        self.db_path = self.tmp_path / "cockpit.db"
        self.db = CockpitDB(self.db_path)
        for index in range(3):
            self.db.add_event("demo", f"T-{index + 1}", "GATE0_FAIL", MOTIF_RECURRENT)

        self.cockpit = self.home / "bin" / "cockpit"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["COCKPIT_HOME"] = str(self.home)
        env["DB_PATH"] = str(self.db_path)
        env["HOME"] = str(self.tmp_path)
        return env

    def _lancer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.cockpit), *args],
            cwd=self.tmp_path,
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_mentionne_lessons_et_syntaxe_valide(self) -> None:
        syntaxe = subprocess.run(
            ["bash", "-n", str(self.cockpit)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntaxe.returncode, 0, syntaxe.stderr)

        resultat = self._lancer("help")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn("lessons", resultat.stdout)

    def test_branche_lessons_delegue_vers_core_lessons_avec_arguments(self) -> None:
        contenu = self.cockpit.read_text(encoding="utf-8")
        self.assertRegex(contenu, r"(?m)^\s*lessons\)")
        self.assertIn("core/lessons.py", contenu)
        self.assertIn('"$@"', contenu)

    def test_sans_apply_ne_modifie_pas_le_fichier_existant(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text("SENTINELLE\n", encoding="utf-8")
        resultat = self._lancer(
            "lessons", "demo", "--db", str(self.db_path), "--rules", str(cible)
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertEqual(cible.read_text(encoding="utf-8"), "SENTINELLE\n")

    def test_sans_apply_ne_cree_pas_le_fichier_absent(self) -> None:
        cible = self.tmp_path / "absent.md"
        resultat = self._lancer(
            "lessons", "demo", "--db", str(self.db_path), "--rules", str(cible)
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertFalse(cible.exists())

    def test_apply_rules_ecrit_le_bloc_delimite(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        resultat = self._lancer(
            "lessons", "demo", "--apply", "--db", str(self.db_path),
            "--rules", str(cible),
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        texte = cible.read_text(encoding="utf-8")
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertEqual(texte.count(SECTION_END), 1)
        debut = texte.index(SECTION_BEGIN)
        fin = texte.index(SECTION_END)
        self.assertLess(debut, fin)
        self.assertIn("\n- ", texte[debut:fin])

    def test_le_fichier_reel_templates_core_md_reste_intact(self) -> None:
        reel = COCKPIT_ROOT / "templates" / "core.md"
        avant = reel.read_bytes() if reel.exists() else None
        self._lancer("lessons", "demo", "--apply", "--db", str(self.db_path),
                     "--rules", str(self.tmp_path / "AGENTS.md"))
        apres = reel.read_bytes() if reel.exists() else None
        self.assertEqual(avant, apres)


if __name__ == "__main__":
    unittest.main()
