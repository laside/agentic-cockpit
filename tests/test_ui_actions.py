#!/usr/bin/env python3
"""Tests du ticket T-053 : actions de l'onglet Commande (par état, argv, personnalisées, run_action)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

from tui.commande import actions  # noqa: E402

PROJET_IDS = ["sync_projet", "status", "brief", "costs", "auto_dry"]


def ids(lst):
    return [a.id for a in lst]


def tk(etat, lane=""):
    return {"id": "T-1", "state": etat, "lane": lane}


class TestActionsParEtat(unittest.TestCase):
    def test_draft(self):
        a = actions.actions_for("demo", tk("DRAFT"), "v")
        self.assertEqual(ids(a), ["promote", "abandon"] + PROJET_IDS)
        self.assertEqual(a[0].argv, ["cockpit", "ticket", "promote", "demo", "T-1"])
        self.assertTrue(a[1].confirmation)
        self.assertIn("--reason", a[1].argv)

    def test_ready(self):
        a = actions.actions_for("demo", tk("READY"), "alpha")
        self.assertEqual(ids(a)[:4], ["demote", "run", "claim", "abandon"])
        self.assertEqual(a[0].libelle, "Remettre en brouillon")
        self.assertFalse(a[0].confirmation)
        self.assertEqual(a[0].argv, ["cockpit", "ticket", "demote", "demo", "T-1"])
        self.assertTrue(a[1].longue)
        self.assertEqual(a[1].argv, ["cockpit", "run", "T-1", "demo", "--lane", "alpha", "--no-gate1"])
        self.assertEqual(a[2].argv[-2:], ["--as", "claude"])

    def test_blocked(self):
        a = actions.actions_for("demo", tk("BLOCKED"))
        self.assertEqual(ids(a)[:3], ["demote", "events", "abandon"])
        self.assertEqual(a[0].argv, ["cockpit", "ticket", "demote", "demo", "T-1"])

    def test_draft_libelle_marquer_pret(self):
        a = actions.actions_for("demo", tk("DRAFT"))
        self.assertEqual(a[0].libelle, "Marquer prêt")
        self.assertFalse(a[0].confirmation)

    def test_demote_absent_des_autres_etats(self):
        for etat in ("DRAFT", "ASSIGNED", "GATE2", "PR_OPEN", "MERGED"):
            self.assertNotIn("demote", ids(actions.actions_for("demo", tk(etat))))

    def test_assigned_interactive(self):
        a = actions.actions_for("demo", tk("ASSIGNED", "interactive"))
        self.assertEqual(ids(a)[0], "submit")
        self.assertTrue(a[0].longue)
        self.assertEqual(ids(actions.actions_for("demo", tk("ASSIGNED", "alpha"))), PROJET_IDS)

    def test_gate2(self):
        a = actions.actions_for("demo", tk("GATE2"), worktree="/w/T-1", branche_defaut="main")
        self.assertEqual(ids(a)[:2], ["diff", "approve"])
        self.assertEqual(a[0].argv, ["git", "-C", "/w/T-1", "diff", "origin/main...HEAD"])
        self.assertTrue(a[1].confirmation and a[1].longue)
        self.assertEqual(a[1].argv, ["cockpit", "merge", "T-1", "demo", "--approve"])

    def test_pr_open(self):
        self.assertEqual(ids(actions.actions_for("demo", tk("PR_OPEN")))[0], "sync")

    def test_projet_toujours_present_et_argv_sans_shell(self):
        for etat in ("DRAFT", "READY", "BLOCKED", "GATE2", "PR_OPEN", "MERGED"):
            a = actions.actions_for("demo", tk(etat), "v", worktree="/w")
            self.assertEqual(ids(a)[-5:], PROJET_IDS)
            for act in a:
                self.assertIsInstance(act.argv, list)
                self.assertTrue(all(isinstance(x, str) for x in act.argv))
        auto = [x for x in a if x.id == "auto_dry"][0]
        self.assertEqual(auto.argv, ["cockpit", "auto", "demo", "--slots", "1", "--dry-run"])

    def test_pas_de_shell_avec_caracteres_speciaux(self):
        a = actions.actions_for("p;rm -rf", {"id": "T-1; ls", "state": "PR_OPEN"})
        self.assertEqual(a[0].argv, ["cockpit", "sync", "p;rm -rf"])


class TestPersonnalisees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name)
        p = mock.patch.dict(os.environ, {"COCKPIT_CONFIG_DIR": str(self.cfg)})
        p.start()
        self.addCleanup(p.stop)
        self.home = Path(self.tmp.name) / "home"

    def ecrire(self, texte):
        (self.cfg / "actions.toml").write_text(texte, encoding="utf-8")

    def appel(self, projet="demo", etat="GATE2"):
        return actions.actions_personnalisees(projet, {"id": "T-9", "state": etat}, "/w/T-9", self.home)

    def test_jetons_et_filtres(self):
        self.ecrire(
            '[[action]]\nid = "a"\nlibelle = "A"\netats = ["GATE2"]\nprojets = []\nconfirmation = true\n'
            'longue = true\nargv = ["s.sh", "{projet}", "{ticket}", "{worktree}", "{cockpit_home}"]\n'
            '[[action]]\nid = "b"\nlibelle = "B"\netats = ["READY"]\nargv = ["x"]\n'
            '[[action]]\nid = "c"\nlibelle = "C"\nprojets = ["autre"]\nargv = ["x"]\n'
        )
        res = self.appel()
        self.assertEqual(ids(res), ["a"])
        self.assertEqual(res[0].argv, ["s.sh", "demo", "T-9", "/w/T-9", str(self.home)])
        self.assertTrue(res[0].confirmation and res[0].longue)
        self.assertEqual(ids(self.appel(etat="READY")), ["b"])
        self.assertEqual(ids(self.appel(projet="autre", etat="READY")), ["b", "c"])

    def test_fichier_absent(self):
        self.assertEqual(self.appel(), [])

    def test_fichier_invalide_ignore(self):
        self.ecrire("ceci n'est pas = du [toml")
        self.assertEqual(self.appel(), [])

    def test_entree_invalide_ignoree(self):
        self.ecrire('[[action]]\nid = "x"\nlibelle = "X"\nargv = "pas une liste"\n'
                    '[[action]]\nid = "ok"\nlibelle = "OK"\nargv = ["y"]\n')
        self.assertEqual(ids(self.appel()), ["ok"])


class TestRunAction(unittest.TestCase):
    def test_journal_et_detachement(self):
        with tempfile.TemporaryDirectory() as d:
            act = actions.Action("echo", "Echo", False, False, ["echo", "bonjour"])
            with mock.patch("subprocess.Popen", wraps=subprocess.Popen) as popen:
                journal = actions.run_action(act, Path(d))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            self.assertTrue(str(journal).startswith(d))
            for _ in range(50):
                if "bonjour" in journal.read_text(encoding="utf-8"):
                    break
                time.sleep(0.1)
            self.assertIn("bonjour", journal.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
