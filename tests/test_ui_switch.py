#!/usr/bin/env python3
"""Tests du ticket T-057 : bascule de projet (sélecteur, cockpit switch, ui = false)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

TEXTUAL = importlib.util.find_spec("textual") is not None
if TEXTUAL:
    from pmo_db import CockpitDB  # noqa: E402
    from tui.commande import actions, app as app_mod, model  # noqa: E402

TOML = """default_project = "alpha"
[projects.alpha]
root = "/tmp/alpha"
[projects.beta]
root = "/tmp/beta"
[projects.cache]
root = "/tmp/cache"
ui = false
[projects.visible]
root = "/tmp/visible"
ui = true
"""


@unittest.skipUnless(TEXTUAL, "Textual non installé (voir scripts/tui-setup.sh)")
class TestBasculeProjet(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        cfg = self.base / "cfg"
        cfg.mkdir()
        (cfg / "projects.toml").write_text(TOML, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(cfg)})
        env.start()
        self.addCleanup(env.stop)
        CockpitDB(self.base / "db" / "t.db")
        self.model = model.CommandeModel(self.base / "db" / "t.db", home=self.base)

    def nouvelle_app(self):
        return app_mod.CommandeApp("alpha", model=self.model, log_dir=self.base / "logs", intervalle=60)

    async def attendre(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def lancer(self, zellij: bool, retour: int = 0, effet: Exception | None = None):
        """Sélectionne « beta » avec subprocess.run simulé ; renvoie (résultat, appels)."""
        appels: list[list[str]] = []

        def faux_run(argv, *a, **k):
            appels.append(list(argv))
            if effet:
                raise effet
            return subprocess.CompletedProcess(argv, retour, "", "boum")

        resultat: dict = {}

        async def scenario() -> None:
            app = self.nouvelle_app()
            with mock.patch.dict(os.environ, {"ZELLIJ": "0"} if zellij else {}):
                if not zellij:
                    os.environ.pop("ZELLIJ", None)
                with mock.patch.object(app_mod.subprocess, "run", faux_run):
                    async with app.run_test(size=(180, 50)) as pilot:
                        await self.attendre(app, pilot)
                        appels.clear()
                        app.query_one("#projet").value = "beta"
                        await self.attendre(app, pilot)
                        resultat["projet"] = app.projet
                        resultat["select"] = app.query_one("#projet").value
                        resultat["messages"] = list(app.messages)
                        n = len(appels)
                        app.query_one("#projet").value = app.projet
                        await self.attendre(app, pilot)
                        resultat["meme_projet"] = len(appels) - n

        asyncio.run(scenario())
        return resultat, appels

    def test_ui_false_masque_du_selecteur(self) -> None:
        self.assertNotIn("cache", self.model.projets_visibles())
        for p in ("alpha", "beta", "visible"):
            self.assertIn(p, self.model.projets_visibles())

        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                valeurs = [v for _, v in app.query_one("#projet")._options]
                self.assertNotIn("cache", valeurs)
                self.assertIn("beta", valeurs)

        asyncio.run(scenario())

    def test_dans_zellij_appelle_cockpit_switch(self) -> None:
        res, appels = self.lancer(zellij=True)
        self.assertEqual(appels, [[actions.CLI, "switch", "beta"]])
        self.assertEqual(res["projet"], "alpha")  # l'écran reste sur son projet
        self.assertEqual(res["select"], "alpha")
        self.assertEqual(res["meme_projet"], 0)  # projet déjà affiché : aucune commande

    def test_hors_zellij_recharge_sans_commande(self) -> None:
        res, appels = self.lancer(zellij=False)
        self.assertEqual(appels, [])
        self.assertEqual(res["projet"], "beta")

    def test_erreur_de_bascule_dans_le_journal(self) -> None:
        res, _ = self.lancer(zellij=True, retour=3)
        self.assertEqual(res["projet"], "alpha")
        self.assertTrue(any("Erreur" in m and "beta" in m for m in res["messages"]))
        res, _ = self.lancer(zellij=True, effet=OSError("introuvable"))
        self.assertTrue(any("beta" in m and "introuvable" in m for m in res["messages"]))


class TestBinCockpitSwitch(unittest.TestCase):
    """Branche Zellij de « cockpit switch » avec un zellij simulé (jamais de vrai Zellij)."""

    def preparer(self, supporte: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        (home / "scripts").mkdir(parents=True)
        (home / "layouts").mkdir()
        (home / "layouts" / "cockpit.kdl.tmpl").write_text("layout __PROJECT_ID__\n", encoding="utf-8")
        (home / "scripts" / "cockpit-env.sh").write_text(
            'PROJECT_NAME="$PROJECT"; PROJECT_ROOT=/tmp; WT_SESSIONS_DIR=/tmp; SIDECAR_PORT=1\n',
            encoding="utf-8")
        binaire = base / "bin"
        binaire.mkdir()
        journal = base / "appels.log"
        fake = binaire / "zellij"
        fake.write_text(
            '#!/bin/sh\necho "$@" >> "%s"\n'
            'case "$*" in\n'
            '  "action switch-session --help") exit %d;;\n'
            '  "list-sessions --no-formatting") echo "cockpit-autre [Created 1s ago]";;\n'
            'esac\nexit 0\n' % (journal, 0 if supporte else 2), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, COCKPIT_HOME=str(home), HOME=str(base), ZELLIJ="0",
                   PATH=str(binaire) + os.pathsep + os.environ["PATH"])
        return env, journal

    def test_switch_dans_zellij_bascule_sans_session_imbriquee(self) -> None:
        env, journal = self.preparer(supporte=True)
        res = subprocess.run([str(ROOT / "bin" / "cockpit"), "switch", "demo"], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        appels = journal.read_text(encoding="utf-8").splitlines()
        self.assertIn("action switch-session cockpit-demo", appels)
        self.assertFalse(any(a.startswith("attach cockpit-demo") or "--new-session-with-layout" in a
                             for a in appels))

    def test_switch_dans_zellij_version_trop_ancienne(self) -> None:
        env, journal = self.preparer(supporte=False)
        res = subprocess.run([str(ROOT / "bin" / "cockpit"), "switch", "demo"], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("switch-session", res.stderr)
        appels = journal.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any("--new-session-with-layout" in a or a.startswith("attach") for a in appels))


if __name__ == "__main__":
    unittest.main()
