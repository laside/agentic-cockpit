"""Tests de l'onglet HELP : page d'aide, commandes citées et disposition Zellij.

Aucun appel réseau, aucun LLM, aucune base de données : lecture de fichiers seulement.
"""
import re
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
HELP_MD = RACINE / "docs" / "HELP.md"
COCKPIT = RACINE / "bin" / "cockpit"
LAYOUT = RACINE / "layouts" / "cockpit.kdl.tmpl"

# Une commande citée = « cockpit <sous-commande> » en début de ligne ou après « : »
# (évite les proses comme « le cockpit ouvre la PR »).
RE_COMMANDE_CITEE = re.compile(
    r"(?:^[ \t]*|:[ \t]*)cockpit[ \t]+([a-z][a-z-]*)", re.MULTILINE
)
RE_USAGE = re.compile(r"Usage:\s*cockpit\s*\{([^}]*)\}")


def sous_commandes_usage(texte):
    """Retourne l'ensemble des sous-commandes de la ligne « Usage: cockpit {…} »."""
    m = RE_USAGE.search(texte)
    if m is None:
        return None
    return {s.strip() for s in re.split(r"[|,]", m.group(1)) if s.strip()}


class TestHelpMd(unittest.TestCase):
    def test_help_md_existe_et_non_vide(self):
        self.assertTrue(HELP_MD.is_file(), "docs/HELP.md doit exister")
        self.assertTrue(HELP_MD.read_text(encoding="utf-8").strip(), "docs/HELP.md ne doit pas être vide")

    def test_help_md_commence_par_le_titre_du_contrat(self):
        premiere_ligne = HELP_MD.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(premiere_ligne, "# AGENTIC COCKPIT — AIDE")

    def test_chaque_commande_citee_existe_dans_usage(self):
        texte_aide = HELP_MD.read_text(encoding="utf-8")
        commandes = set(RE_COMMANDE_CITEE.findall(texte_aide))
        self.assertTrue(commandes, "HELP.md doit citer au moins une commande « cockpit <sous-commande> »")

        usage = sous_commandes_usage(COCKPIT.read_text(encoding="utf-8"))
        self.assertIsNotNone(usage, "bin/cockpit doit contenir une ligne « Usage: cockpit {…} »")

        manquantes = sorted(commandes - usage)
        self.assertFalse(
            manquantes,
            f"sous-commandes citées dans HELP.md absentes de bin/cockpit : {manquantes}",
        )


class TestDispositionHelp(unittest.TestCase):
    def test_modele_contient_onglet_help_qui_reference_help_md(self):
        texte = LAYOUT.read_text(encoding="utf-8")
        position = texte.find('name="HELP"')
        self.assertGreaterEqual(position, 0, "le modèle de disposition doit contenir un onglet HELP")

        bloc_help = texte[position:]
        self.assertIn(
            "__COCKPIT_HOME__/docs/HELP.md",
            bloc_help,
            "l'onglet HELP doit afficher __COCKPIT_HOME__/docs/HELP.md",
        )
        self.assertIn("less", bloc_help, "l'onglet HELP doit utiliser less (lecture seule)")


if __name__ == "__main__":
    unittest.main()
