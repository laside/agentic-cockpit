#!/usr/bin/env python3
"""Tests unitaires (aucun réseau) de la Porte 1 statique du gatekeeper."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import gatekeeper  # noqa: E402

check_tests_present = gatekeeper.check_tests_present
is_source_file = gatekeeper.is_source_file
is_test_file = gatekeeper.is_test_file


class TestIsTestFile(unittest.TestCase):
    def test_dossier_tests(self) -> None:
        self.assertTrue(is_test_file("tests/a.py"))

    def test_dossier_test(self) -> None:
        self.assertTrue(is_test_file("test/a.py"))

    def test_prefixe_test_(self) -> None:
        self.assertTrue(is_test_file("test_a.py"))

    def test_suffixe_dot_test(self) -> None:
        self.assertTrue(is_test_file("a.test.ts"))

    def test_suffixe_dot_spec(self) -> None:
        self.assertTrue(is_test_file("a.spec.js"))

    def test_source_hors_tests(self) -> None:
        self.assertFalse(is_test_file("src/a.py"))


class TestIsSourceFile(unittest.TestCase):
    def test_python_est_source(self) -> None:
        self.assertTrue(is_source_file("core/a.py"))

    def test_markdown_n_est_pas_source(self) -> None:
        self.assertFalse(is_source_file("README.md"))

    def test_fichier_de_test_n_est_pas_source(self) -> None:
        self.assertFalse(is_source_file("tests/a.py"))

    def test_html_n_est_pas_source(self) -> None:
        self.assertFalse(is_source_file("index.html"))


class TestCheckTestsPresent(unittest.TestCase):
    def test_ticket_source_avec_test_modifie(self) -> None:
        ok, message = check_tests_present(
            ["core/a.py", "tests/test_a.py"], ["core/a.py"]
        )
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_ticket_source_sans_test(self) -> None:
        ok, message = check_tests_present(
            ["core/a.py"], ["core/a.py"]
        )
        self.assertFalse(ok)
        self.assertTrue(message.startswith("Aucun test n'a été ajouté ou modifié"))
        self.assertIn("test", message)

    def test_ticket_docs_uniquement_sans_test(self) -> None:
        ok, message = check_tests_present(
            ["README.md", "Makefile"], ["README.md", "Makefile"]
        )
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_changed_files_vide_avec_files_owned_source(self) -> None:
        ok, message = check_tests_present([], ["core/a.py"])
        self.assertFalse(ok)
        self.assertTrue(message.startswith("Aucun test n'a été ajouté ou modifié"))


if __name__ == "__main__":
    unittest.main()
