"""Tests du ticket T-055 : sous-commande « cockpit ui », premier onglet Commande, aide et README.

Aucun réseau, aucun LLM, aucune base réelle : les scripts de l'écran sont remplacés par des
stubs écrits dans un répertoire temporaire, et les fichiers du dépôt sont lus seulement.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
COCKPIT = RACINE / "bin" / "cockpit"
LAYOUT = RACINE / "layouts" / "cockpit.kdl.tmpl"
HELP_MD = RACINE / "docs" / "HELP.md"
README = RACINE / "README.md"

RE_USAGE = re.compile(r"Usage:\s*cockpit\s*\{([^}]*)\}")
RE_ONGLET = re.compile(r'^[ \t]*tab name="[^"]*"', re.MULTILINE)

# Stub de l'environnement Textual : trace le PATH, le répertoire courant et les arguments.
STUB_PYTHON = """#!/usr/bin/env bash
{
  printf 'PATH=%s\\n' "$PATH"
  printf 'PWD=%s\\n' "$PWD"
  printf 'ARGS=%s\\n' "$*"
} > "$COCKPIT_TEST_TRACE"
"""

# Stub de scripts/tui-setup.sh : crée l'environnement factice et le signale dans la trace.
STUB_SETUP = """#!/usr/bin/env bash
echo "SETUP" >> "$COCKPIT_TEST_SETUP_LOG"
mkdir -p "$TUI_VENV/bin"
cp "$COCKPIT_TEST_STUB_PYTHON" "$TUI_VENV/bin/python"
chmod +x "$TUI_VENV/bin/python"
"""


def _lire_trace(chemin: Path) -> dict:
    valeurs = {}
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        cle, _, valeur = ligne.partition("=")
        valeurs[cle] = valeur
    return valeurs


class TestUsageEtAide(unittest.TestCase):
    def test_ui_figure_dans_la_ligne_usage(self):
        m = RE_USAGE.search(COCKPIT.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "bin/cockpit doit contenir « Usage: cockpit {…} »")
        sous_commandes = {s.strip() for s in m.group(1).split("|")}
        self.assertIn("ui", sous_commandes)

    def test_cockpit_help_affiche_ui(self):
        resultat = subprocess.run(
            ["bash", str(COCKPIT), "help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertRegex(resultat.stdout, r"[{|]ui[|}]")


class TestDispositionOngletCommande(unittest.TestCase):
    def setUp(self):
        self.texte = LAYOUT.read_text(encoding="utf-8")
        self.onglets = list(RE_ONGLET.finditer(self.texte))

    def test_premier_onglet_est_commande_avec_focus(self):
        self.assertTrue(self.onglets, "le modèle doit contenir des onglets")
        premiere_ligne = self.texte[self.onglets[0].start():].splitlines()[0]
        self.assertEqual(premiere_ligne.strip(), 'tab name="Commande" focus=true {')

    def test_onglet_commande_lance_cockpit_ui_avec_le_jeton_projet(self):
        debut = self.onglets[0].start()
        fin = self.onglets[1].start() if len(self.onglets) > 1 else len(self.texte)
        bloc = self.texte[debut:fin]
        self.assertIn('command="__COCKPIT_HOME__/bin/cockpit"', bloc)
        self.assertIn('args "ui" "__PROJECT_ID__"', bloc)
        self.assertEqual(bloc.count("pane "), 1, "l'onglet Commande doit avoir un seul panneau")

    def test_un_seul_onglet_a_le_focus(self):
        self.assertEqual(self.texte.count("focus=true"), 1)

    def test_onglet_help_toujours_present(self):
        self.assertIn('name="HELP"', self.texte)


class TestHelpEtReadme(unittest.TestCase):
    def test_help_md_section_commande_en_tete_de_5_lignes_au_plus(self):
        lignes = HELP_MD.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lignes[0], "# AGENTIC COCKPIT — AIDE")
        self.assertIn("## L'onglet Commande", lignes)
        debut = lignes.index("## L'onglet Commande")
        self.assertLess(debut, 3, "la section doit être en tête du document")
        corps = []
        for ligne in lignes[debut + 1:]:
            if ligne.startswith("## "):
                break
            if ligne.strip():
                corps.append(ligne)
        self.assertGreaterEqual(len(corps), 1)
        self.assertLessEqual(len(corps), 5, "la section « L'onglet Commande » tient en 5 lignes au plus")

    def test_readme_section_onglet_commande(self):
        texte = README.read_text(encoding="utf-8")
        self.assertIn("## Onglet Commande", texte)
        section = texte.split("## Onglet Commande", 1)[1].split("\n## ", 1)[0]
        self.assertIn("scripts/tui-setup.sh", section)
        self.assertIn("cockpit ui", section)
        self.assertIn("actions.toml", section)


class TestLancementCockpitUi(unittest.TestCase):
    """Exécute bin/cockpit ui avec un COCKPIT_HOME temporaire et un environnement factice."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        (self.home / "scripts").mkdir(parents=True)
        self.setup = self.home / "scripts" / "tui-setup.sh"
        self.setup.write_text(STUB_SETUP, encoding="utf-8")
        self.setup.chmod(self.setup.stat().st_mode | stat.S_IXUSR)
        self.venv = self.tmp / "venv"
        self.stub_python = self.tmp / "stub-python"
        self.stub_python.write_text(STUB_PYTHON, encoding="utf-8")
        self.stub_python.chmod(self.stub_python.stat().st_mode | stat.S_IXUSR)
        self.trace = self.tmp / "trace.txt"
        self.setup_log = self.tmp / "setup.log"
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()

    def _env(self, avec_venv=True):
        if avec_venv:
            (self.venv / "bin").mkdir(parents=True, exist_ok=True)
            py = self.venv / "bin" / "python"
            py.write_text(STUB_PYTHON, encoding="utf-8")
            py.chmod(py.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ)
        env.update({
            "COCKPIT_HOME": str(self.home),
            "TUI_VENV": str(self.venv),
            "COCKPIT_TEST_TRACE": str(self.trace),
            "COCKPIT_TEST_SETUP_LOG": str(self.setup_log),
            "COCKPIT_TEST_STUB_PYTHON": str(self.stub_python),
            "COCKPIT_CONFIG_DIR": str(self.cfg),
            "PROJECTS_TOML": str(self.cfg / "absent.toml"),
        })
        return env

    def _lancer(self, *args, env):
        return subprocess.run(
            ["bash", str(COCKPIT), "ui", *args],
            env=env, capture_output=True, text=True, check=False, timeout=30,
        )

    def test_ui_place_bin_cockpit_en_tete_du_path(self):
        resultat = self._lancer("demo", env=self._env())
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        trace = _lire_trace(self.trace)
        premier = trace["PATH"].split(":")[0]
        self.assertEqual(premier, str(self.home / "bin"))

    def test_ui_lance_le_module_textual_pour_le_projet(self):
        resultat = self._lancer("demo", env=self._env())
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        trace = _lire_trace(self.trace)
        self.assertEqual(trace["ARGS"], "-m tui.commande.app demo")
        self.assertEqual(os.path.realpath(trace["PWD"]), os.path.realpath(self.home))

    def test_ui_sans_projet_utilise_default_project(self):
        (self.cfg / "projects.toml").write_text('default_project = "demo"\n', encoding="utf-8")
        env = self._env()
        env["PROJECTS_TOML"] = ""
        resultat = self._lancer(env=env)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertEqual(_lire_trace(self.trace)["ARGS"], "-m tui.commande.app demo")

    def test_ui_cree_l_environnement_s_il_manque(self):
        resultat = self._lancer("demo", env=self._env(avec_venv=False))
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn("tui-setup.sh", resultat.stderr, "message clair avant la création")
        self.assertTrue(self.setup_log.is_file(), "tui-setup.sh doit être appelé quand l'environnement manque")
        self.assertEqual(_lire_trace(self.trace)["ARGS"], "-m tui.commande.app demo")


if __name__ == "__main__":
    unittest.main()
