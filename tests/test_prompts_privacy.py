"""Tests du prompt des workers TDD sur les données factices et la confidentialité."""

from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestPromptPrivacy(unittest.TestCase):
    """Vérifie les consignes de confidentialité du prompt des workers."""

    def test_prompt_contient_marqueur_et_domaine_exemple(self) -> None:
        """Le prompt interdit les vraies données et mentionne le marqueur d'exception."""
        prompt = ROOT_DIR / "prompts" / "sys_coder_tdd.md"
        contenu = prompt.read_text(encoding="utf-8")
        self.assertIn("privacy-check: allow", contenu)
        self.assertIn("example.com", contenu)


if __name__ == "__main__":
    unittest.main()
