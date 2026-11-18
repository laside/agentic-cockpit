#!/usr/bin/env python3
"""
Tests du module fixer (core/fixer.py) : parsing lint, charge utile Ollama,
extraction de correctif et réécriture de fichier.
Aucun appel réseau réel : la fonction `call` est injectée ou simulée.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT))

from core import fixer  # noqa: E402


class ExtractLintErrorsTests(unittest.TestCase):
    def test_parse_ruff(self) -> None:
        sortie = "core/app.py:12:5: E501 Line too long (99 > 88)\n"
        resultat = fixer.extract_lint_errors(sortie)
        self.assertEqual(len(resultat), 1)
        erreur = resultat[0]
        self.assertEqual(erreur["path"], "core/app.py")
        self.assertEqual(erreur["line"], 12)
        self.assertIn("E501", erreur["message"])
        self.assertIn("Line too long", erreur["message"])

    def test_parse_mypy(self) -> None:
        sortie = 'core/app.py:42: error: Incompatible return value type\n'
        resultat = fixer.extract_lint_errors(sortie)
        self.assertEqual(
            resultat,
            [
                {
                    "path": "core/app.py",
                    "line": 42,
                    "message": "Incompatible return value type",
                }
            ],
        )

    def test_lignes_ignorees(self) -> None:
        sortie = (
            "Found 2 errors\n"
            "Success: no issues found\n"
            "\n"
            "core/app.py:3:1: F401 'os' imported but unused\n"
            "core/app.py:9: error: Name 'x' is not defined\n"
            "Some unrelated log line\n"
        )
        resultat = fixer.extract_lint_errors(sortie)
        self.assertEqual(len(resultat), 2)
        self.assertEqual(resultat[0]["line"], 3)
        self.assertEqual(resultat[1]["line"], 9)
        self.assertTrue(all(set(e) == {"path", "line", "message"} for e in resultat))

    def test_sortie_vide(self) -> None:
        self.assertEqual(fixer.extract_lint_errors(""), [])


class BuildFixerPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = "def f():\n    return 1\n"
        self.errors = [
            {"path": "core/app.py", "line": 2, "message": "E225 missing whitespace"}
        ]

    def test_cles_et_valeurs_par_defaut(self) -> None:
        payload = fixer.build_fixer_payload(self.source, self.errors, "SYSTEME")
        self.assertEqual(
            set(payload),
            {"model", "prompt", "stream", "keep_alive", "options"},
        )
        self.assertEqual(payload["model"], "qwen2.5-coder-16k")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(payload["options"], {"num_ctx": 16384, "temperature": 0.0})

    def test_personnalisation_modele_et_contexte(self) -> None:
        payload = fixer.build_fixer_payload(
            self.source, self.errors, "SYSTEME", model="autre-modele", num_ctx=4096
        )
        self.assertEqual(payload["model"], "autre-modele")
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(payload["options"]["temperature"], 0.0)

    def test_prompt_contient_systeme_erreurs_et_source(self) -> None:
        payload = fixer.build_fixer_payload(self.source, self.errors, "SYSTEME")
        prompt = payload["prompt"]
        self.assertIn("SYSTEME", prompt)
        self.assertIn("core/app.py", prompt)
        self.assertIn("2", prompt)
        self.assertIn("E225 missing whitespace", prompt)
        self.assertIn(self.source, prompt)


class ApplyFixTests(unittest.TestCase):
    def test_bloc_valide(self) -> None:
        source = "def f():\n    return 1\n"
        reponse = "Voici la correction :\n```python\ndef f():\n    return 2\n```\n"
        self.assertEqual(fixer.apply_fix(reponse, source), "def f():\n    return 2")

    def test_aucun_bloc(self) -> None:
        self.assertIsNone(fixer.apply_fix("Aucun code ici.", "x = 1\n"))

    def test_syntaxe_invalide(self) -> None:
        reponse = "```python\ndef f(:\n    pass\n```"
        self.assertIsNone(fixer.apply_fix(reponse, "def f():\n    pass\n"))

    def test_longueur_aberrante(self) -> None:
        source = "x = 1\n"
        reponse = "```python\n" + ("y = 2\n" * 10) + "```"
        self.assertIsNone(fixer.apply_fix(reponse, source))

    def test_code_vide_est_invalide(self) -> None:
        self.assertIsNone(fixer.apply_fix("```\n```", "x = 1\n"))


class FixFileTests(unittest.TestCase):
    def test_succes_modifie_le_fichier(self) -> None:
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / "app.py"
            original = "def f():\n    return 1\n"
            chemin.write_text(original, encoding="utf-8")
            corrige = "def f():\n    return 2"

            appels: list[dict] = []

            def call(payload: dict) -> str:
                appels.append(payload)
                return "```python\n" + corrige + "\n```"

            resultat = fixer.fix_file(
                chemin,
                [{"path": str(chemin), "line": 2, "message": "E225"}],
                call=call,
                system_prompt="SYSTEME",
            )

            self.assertTrue(resultat)
            self.assertEqual(chemin.read_text(encoding="utf-8"), corrige)
            self.assertEqual(len(appels), 1)
            self.assertEqual(appels[0]["model"], "qwen2.5-coder-16k")

    def test_echec_reseau_ne_touche_pas_le_fichier(self) -> None:
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / "app.py"
            original = "def f():\n    return 1\n"
            chemin.write_text(original, encoding="utf-8")

            def call(payload: dict) -> str:
                raise ConnectionError("Ollama indisponible")

            resultat = fixer.fix_file(chemin, [], call=call)

            self.assertFalse(resultat)
            self.assertEqual(chemin.read_text(encoding="utf-8"), original)

    def test_reponse_invalide_ne_touche_pas_le_fichier(self) -> None:
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / "app.py"
            original = "def f():\n    return 1\n"
            chemin.write_text(original, encoding="utf-8")

            def call(payload: dict) -> str:
                return "Je ne peux pas corriger ce fichier."

            resultat = fixer.fix_file(chemin, [], call=call)

            self.assertFalse(resultat)
            self.assertEqual(chemin.read_text(encoding="utf-8"), original)

    def test_erreur_syntaxe_ne_touche_pas_le_fichier(self) -> None:
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / "app.py"
            original = "def f():\n    return 1\n"
            chemin.write_text(original, encoding="utf-8")

            def call(payload: dict) -> str:
                return "```python\ndef f(:\n    pass\n```"

            resultat = fixer.fix_file(chemin, [], call=call)

            self.assertFalse(resultat)
            self.assertEqual(chemin.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
