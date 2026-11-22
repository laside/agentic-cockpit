#!/usr/bin/env python3
"""Tests du ticket T-054 : écran Textual de l'onglet Commande (ignorés sans Textual)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
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
    from textual.widgets import Button  # noqa: E402
    from tui.commande import actions, app as app_mod, model  # noqa: E402


@unittest.skipUnless(TEXTUAL, "Textual non installé (voir scripts/tui-setup.sh)")
class TestCommandeApp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        cfg = self.base / "cfg"
        cfg.mkdir()
        env = mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(cfg)})
        env.start()
        self.addCleanup(env.stop)
        self.db = CockpitDB(self.base / "db" / "t.db")
        for tid, etat in [("T-001", "GATE2"), ("T-002", "READY"), ("T-003", "DRAFT"), ("T-004", "MERGED")]:
            self.db.upsert_ticket("demo", tid, f"Titre {tid}", priority="P1", state=etat)
        # commande factice : dort un peu, sans réseau ni cockpit réel
        self.fake = self.base / "fake-cockpit"
        self.fake.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        patch = mock.patch.object(actions, "CLI", str(self.fake))
        patch.start()
        self.addCleanup(patch.stop)
        self.model = model.CommandeModel(self.base / "db" / "t.db", home=self.base)

    def nouvelle_app(self):
        return app_mod.CommandeApp("demo", model=self.model, log_dir=self.base / "logs", intervalle=60)

    async def attendre(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    @staticmethod
    def ids_boutons(app) -> list[str]:
        return [b.id for b in app.query("#boutons-ticket Button")]

    def test_boutons_recalcules_selon_etat(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id="T-002")))
                await self.attendre(app, pilot)
                ids_ready = self.ids_boutons(app)
                self.assertIn("act-run", ids_ready)
                self.assertNotIn("act-approve", ids_ready)
                # « Lancer » reste désactivé tant qu'aucune voie n'est choisie
                self.assertTrue(app.query_one("#act-run", Button).disabled)
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id="T-001")))
                await self.attendre(app, pilot)
                ids_gate2 = self.ids_boutons(app)
                self.assertIn("act-approve", ids_gate2)
                self.assertNotEqual(ids_ready, ids_gate2)
                # un seul Synchroniser dans tout l'écran
                libelles = [str(b.label) for b in app.query(Button)]
                self.assertEqual(libelles.count("Synchroniser"), 1)

        asyncio.run(scenario())

    def test_approuver_ouvre_la_confirmation(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id="T-001")))
                await self.attendre(app, pilot)
                await pilot.click("#act-approve")
                await pilot.pause()
                self.assertIsInstance(app.screen, app_mod.ConfirmScreen)
                self.assertEqual(app.procs, [])  # rien n'est lancé avant confirmation
                await pilot.click("#non")
                await pilot.pause()
                self.assertEqual(app.procs, [])

        asyncio.run(scenario())

    def test_une_seule_action_longue_a_la_fois(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id="T-001")))
                await self.attendre(app, pilot)
                await pilot.click("#act-approve")
                await pilot.pause()
                await pilot.click("#oui")
                await pilot.pause()
                self.assertEqual(len(app.procs), 1)
                self.assertTrue(app.query_one("#act-approve", Button).disabled)
                # une seconde action longue est refusée
                app.executer(next(a for a in app.actions_ticket if a.id == "approve"))
                self.assertEqual(len(app.procs), 1)
                for proc, _, _ in app.procs:
                    proc.kill()
                    proc.wait()
                app._sonder_processus()
                self.assertEqual(app.procs, [])
                self.assertFalse(app.query_one("#act-approve", Button).disabled)

        asyncio.run(scenario())

    def test_commande_introuvable_ne_plante_pas(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                app.executer(actions.Action("x", "X", False, False, [str(self.base / "absent")]))
                self.assertEqual(app.procs, [])
                self.assertTrue(any("introuvable" in m for m in app.messages))

        asyncio.run(scenario())

    def test_filtre_a_relire_ne_montre_que_gate2(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                self.assertEqual(len(app.tickets), 4)
                await pilot.click("#filtre-3")  # À relire
                await self.attendre(app, pilot)
                self.assertEqual([t["id"] for t in app.tickets], ["T-001"])
                self.assertEqual({t["state"] for t in app.tickets}, {"GATE2"})

        asyncio.run(scenario())


class TestNormalisation(unittest.TestCase):
    @unittest.skipUnless(TEXTUAL, "Textual non installé (voir scripts/tui-setup.sh)")
    def test_normaliser_ticket(self) -> None:
        self.assertEqual(app_mod.normaliser_ticket({"id": "T-1", "state": "READY", "lane": "a"})["voie"], "a")
        self.assertEqual(app_mod.normaliser_ticket({"id": "T-1", "etat": "GATE2", "voie": "b"})["state"], "GATE2")
        self.assertIsNone(app_mod.normaliser_ticket(None))


class TestInstallation(unittest.TestCase):
    def test_dependance_isolee(self) -> None:
        reqs = (ROOT / "requirements-tui.txt").read_text(encoding="utf-8")
        self.assertRegex(reqs, r"(?m)^textual==\d")
        script = (ROOT / "scripts" / "tui-setup.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m venv", script)
        self.assertIn("tui-venv", script)
        self.assertNotIn("sudo", script)
        self.assertNotIn("--break-system-packages", script)


if __name__ == "__main__":
    unittest.main()
