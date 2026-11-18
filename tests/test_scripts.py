"""Tests de syntaxe et d'arguments des scripts shell du cockpit."""

from pathlib import Path
import subprocess
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestShellScripts(unittest.TestCase):
    """Vérifie la syntaxe bash et la gestion d'arguments des scripts shell."""

    def test_syntaxe_bash_scripts_et_bin_cockpit(self) -> None:
        """Chaque fichier de scripts/*.sh et bin/cockpit doit passer 'bash -n'."""
        scripts_dir = ROOT_DIR / "scripts"
        fichiers = sorted(scripts_dir.glob("*.sh"))
        bin_cockpit = ROOT_DIR / "bin" / "cockpit"
        self.assertTrue(
            fichiers, "Aucun script *.sh trouvé dans le dossier scripts/."
        )
        self.assertTrue(
            bin_cockpit.is_file(), "Le fichier bin/cockpit est introuvable."
        )
        cibles = [*fichiers, bin_cockpit]

        for cible in cibles:
            chemin_relatif = cible.relative_to(ROOT_DIR)
            with self.subTest(fichier=str(chemin_relatif)):
                resultat = subprocess.run(
                    ["bash", "-n", str(cible)],
                    cwd=ROOT_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    resultat.returncode,
                    0,
                    f"Erreur de syntaxe bash dans {chemin_relatif} : {resultat.stderr}",
                )

    def test_relaunch_option_inconnue(self) -> None:
        """Lancer 'bash scripts/relaunch.sh --bogus' retourne un code non nul et affiche 'Option inconnue' sur stderr."""
        relaunch = ROOT_DIR / "scripts" / "relaunch.sh"
        resultat = subprocess.run(
            ["bash", str(relaunch), "--bogus"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(
            resultat.returncode,
            0,
            "Le script relaunch.sh aurait dû échouer avec une option inconnue.",
        )
        self.assertIn(
            "Option inconnue",
            resultat.stderr,
            f"Sortie stderr inattendue : {resultat.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
