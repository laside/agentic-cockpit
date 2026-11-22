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


@unittest.skipUnless(TEXTUAL, "Textual non installé (voir scripts/tui-setup.sh)")
class TestBoutonsGlobaux(TestCommandeApp):
    """Un test par bouton du haut (Synchroniser, Rafraîchir, Brief, Coûts, Simulation)."""

    def cli_factice(self, corps: str) -> None:
        f = self.base / "cli-echo"
        f.write_text("#!/bin/sh\n" + corps + "\n", encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
        patch = mock.patch.object(actions, "CLI", str(f))
        patch.start()
        self.addCleanup(patch.stop)

    def verifier_modale(self, bouton: str, libelle: str, attendu: str) -> None:
        self.cli_factice('echo "SORTIE $*"')

        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await pilot.click(f"#g-{bouton}")
                await self.attendre(app, pilot)
                self.assertIsInstance(app.screen, app_mod.SortieScreen)
                self.assertEqual(app.screen.titre, libelle)
                self.assertIn(f"SORTIE {attendu}", app.screen.contenu)
                self.assertEqual(app.procs, [])  # pas de processus détaché
                self.assertTrue(any(f"Terminé : {libelle}" in m for m in app.messages))
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, app_mod.SortieScreen)
                # réouverture puis fermeture par le bouton Fermer
                await pilot.click(f"#g-{bouton}")
                await self.attendre(app, pilot)
                self.assertIsInstance(app.screen, app_mod.SortieScreen)
                await pilot.click("#sortie-fermer")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, app_mod.SortieScreen)

        asyncio.run(scenario())

    def test_brief_dans_la_fenetre(self) -> None:
        self.verifier_modale("brief", "Brief", "brief demo")

    def test_couts_dans_la_fenetre(self) -> None:
        self.verifier_modale("costs", "Coûts", "costs demo")

    def test_simulation_dans_la_fenetre(self) -> None:
        self.verifier_modale("auto_dry", "Simulation de la boucle", "auto demo --slots 1 --dry-run")

    def test_erreur_et_commande_introuvable_affichees(self) -> None:
        self.cli_factice('echo "panne" >&2\nexit 3')

        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await pilot.click("#g-costs")
                await self.attendre(app, pilot)
                self.assertIsInstance(app.screen, app_mod.SortieScreen)
                self.assertIn("code 3", app.screen.contenu)
                self.assertIn("panne", app.screen.contenu)
                self.assertTrue(any("Erreur : Coûts (code 3)" in m for m in app.messages))
                await pilot.press("escape")
                await pilot.pause()
                with mock.patch.object(actions, "CLI", str(self.base / "absent")):
                    await app._maj_boutons()
                    await pilot.click("#g-brief")
                    await self.attendre(app, pilot)
                self.assertIsInstance(app.screen, app_mod.SortieScreen)
                self.assertIn("introuvable", app.screen.contenu)
                self.assertTrue(any("introuvable" in m for m in app.messages))

        asyncio.run(scenario())

    def test_synchroniser_journal_puis_rafraichit(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await pilot.click("#g-sync_projet")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, app_mod.SortieScreen)
                self.assertEqual(len(app.procs), 1)
                proc, a, journal = app.procs[0]
                self.assertEqual(a.argv, [str(self.fake), "sync", "demo"])
                self.assertEqual(app.journal_actif, journal)
                proc.kill()
                proc.wait()
                with mock.patch.object(app, "action_rafraichir") as rafraichir:
                    app._sonder_processus()
                rafraichir.assert_called_once()
                self.assertTrue(any("Terminé : Synchroniser" in m for m in app.messages))

        asyncio.run(scenario())

    def test_rafraichir_recharge_sans_commande(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                libelles = [str(b.label) for b in app.query("#boutons-globaux Button")]
                self.assertIn("Rafraîchir", libelles)
                self.assertNotIn("État", libelles)
                self.assertEqual(len(app.tickets), 4)
                self.db.upsert_ticket("demo", "T-005", "Titre T-005", priority="P2", state="READY")
                with mock.patch.object(app_mod.subprocess, "Popen") as popen, \
                        mock.patch.object(app_mod.subprocess, "run") as run:
                    await pilot.click("#g-rafraichir")
                    await self.attendre(app, pilot)
                popen.assert_not_called()
                run.assert_not_called()
                self.assertEqual(app.procs, [])
                self.assertIn("T-005", [t["id"] for t in app.tickets])

        asyncio.run(scenario())

    def test_infos_bulles(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                attendus = {
                    "g-sync_projet": "PR fusionnées", "g-rafraichir": "Recharge tout de suite",
                    "g-brief": "30 lignes", "g-costs": "sans mélanger les unités",
                    "g-auto_dry": "sans rien lancer",
                }
                for bid, extrait in attendus.items():
                    self.assertIn(extrait, str(app.query_one(f"#{bid}", Button).tooltip))
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id="T-001")))
                await self.attendre(app, pilot)
                boutons = list(app.query("#boutons-ticket Button"))
                self.assertTrue(boutons)
                for b in boutons:
                    self.assertTrue(b.tooltip, b.id)

        asyncio.run(scenario())

    # les tests hérités de TestCommandeApp ne sont pas rejoués ici
    test_boutons_recalcules_selon_etat = None
    test_approuver_ouvre_la_confirmation = None
    test_une_seule_action_longue_a_la_fois = None
    test_commande_introuvable_ne_plante_pas = None
    test_filtre_a_relire_ne_montre_que_gate2 = None


@unittest.skipUnless(TEXTUAL, "Textual non installé (voir scripts/tui-setup.sh)")
class TestRafraichissementSousModale(TestBoutonsGlobaux):
    """Le rafraîchissement périodique vise l'écran principal, même sous une fenêtre modale."""

    def verifier_sous_modale(self, ticket: str, ouvrir, type_ecran, fermer) -> None:
        self.cli_factice('echo "SORTIE $*"')

        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                await app._ticket_surligne(mock.Mock(option=mock.Mock(id=ticket)))
                await self.attendre(app, pilot)
                await ouvrir(app, pilot)
                await self.attendre(app, pilot)
                self.assertIsInstance(app.screen, type_ecran)
                for _ in range(2):
                    app._tic()
                    await self.attendre(app, pilot)
                    await app._appliquer({"projet": "demo", "filtre": app.filtre, "ticket_id": ticket,
                                          "erreur": None, "tickets": app.model.list_tickets("demo", app.filtre),
                                          "detail": app.model.ticket_detail("demo", ticket),
                                          "worktree": None, "evenements": []})
                self.assertTrue(app.is_running)
                self.assertIsInstance(app.screen, type_ecran)
                self.assertFalse(any("introuvable" in m or "échec" in m for m in app.messages), app.messages)
                await fermer(app, pilot)
                await pilot.pause()
                self.assertNotIsInstance(app.screen, type_ecran)
                self.assertIs(app.screen, app.ecran_principal)
                self.assertIn(ticket, str(app.query_one("#detail").render()))

        asyncio.run(scenario())

    @staticmethod
    async def echap(app, pilot) -> None:
        await pilot.press("escape")

    def _bouton_haut(self, bouton: str) -> None:
        async def ouvrir(app, pilot) -> None:
            await pilot.click(f"#g-{bouton}")
        self.verifier_sous_modale("T-002", ouvrir, app_mod.SortieScreen, self.echap)

    def test_brief_sous_rafraichissement(self) -> None:
        self._bouton_haut("brief")

    def test_couts_sous_rafraichissement(self) -> None:
        self._bouton_haut("costs")

    def test_simulation_sous_rafraichissement(self) -> None:
        self._bouton_haut("auto_dry")

    def test_diff_sous_rafraichissement(self) -> None:
        async def ouvrir(app, pilot) -> None:
            app._afficher_diff(actions.Action("diff", "Voir le diff", False, False, ["echo", "+ligne"]))
        self.verifier_sous_modale("T-001", ouvrir, app_mod.DiffScreen, self.echap)

    def _confirmation(self, ticket: str, action_id: str) -> None:
        async def ouvrir(app, pilot) -> None:
            app.demander(next(a for a in app.actions_ticket if a.id == action_id))

        async def fermer(app, pilot) -> None:
            await pilot.click("#non")
        self.verifier_sous_modale(ticket, ouvrir, app_mod.ConfirmScreen, fermer)

    def test_confirmation_approuver_sous_rafraichissement(self) -> None:
        self._confirmation("T-001", "approve")

    def test_confirmation_abandonner_sous_rafraichissement(self) -> None:
        self._confirmation("T-002", "abandon")

    def test_exception_de_rafraichissement_notee(self) -> None:
        async def scenario() -> None:
            app = self.nouvelle_app()
            async with app.run_test(size=(180, 50)) as pilot:
                await self.attendre(app, pilot)
                with mock.patch.object(app, "_maj_liste", side_effect=RuntimeError("boum")):
                    app.action_rafraichir()
                    await self.attendre(app, pilot)
                self.assertTrue(app.is_running)
                self.assertTrue(any("boum" in m for m in app.messages))

        asyncio.run(scenario())

    test_brief_dans_la_fenetre = None
    test_couts_dans_la_fenetre = None
    test_simulation_dans_la_fenetre = None
    test_erreur_et_commande_introuvable_affichees = None
    test_synchroniser_journal_puis_rafraichit = None
    test_rafraichir_recharge_sans_commande = None
    test_infos_bulles = None


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

    def test_repli_uv(self) -> None:
        script = (ROOT / "scripts" / "tui-setup.sh").read_text(encoding="utf-8")
        self.assertIn("uv venv", script)
        self.assertIn('uv pip install --quiet --python "${VENV}/bin/python" -r', script)
        self.assertIn("Deux solutions", script)


if __name__ == "__main__":
    unittest.main()
