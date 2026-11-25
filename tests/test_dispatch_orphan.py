#!/usr/bin/env python3
"""
Tests T-059 — dispatch vérifie le panneau cible et ne laisse plus de ticket ASSIGNED orphelin.

Couvre :
- scripts/dispatch-ticket.sh avec un faux zellij dans le PATH : le panneau cible doit exécuter
  un shell inactif (commande courante du panneau via zellij, ou absence de processus enfant au
  premier plan) ; sinon refus avec la commande à lancer à la main. Dispatch ne modifie jamais
  l'état du ticket (c'est run qui passe à ASSIGNED).
- core/pipeline.py : aucun chemin ne produit ASSIGNED avec lane vide (un run qui échoue avant le
  démarrage du worker remet le ticket à READY) ; release accepte un ticket ASSIGNED sans voie ni
  pid vivant (état orphelin) et le remet à READY, verrous libérés, refus inchangé si un pid vivant
  le tient ; status signale les orphelins avec la commande release à lancer.

Aucun réseau, aucun LLM, base SQLite temporaire.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import pipeline  # noqa: E402
from pmo_db import CockpitDB  # noqa: E402

SCRIPT = ROOT / "scripts" / "dispatch-ticket.sh"

FAKE_ZELLIJ = """#!/usr/bin/env bash
echo "$*" >> "${FAKE_ZELLIJ_LOG:?}"
case " $* " in
  *" list-panes "*) cat "${FAKE_ZELLIJ_PANES:?}" ;;
esac
exit 0
"""


def dead_pid() -> int:
    """Un pid qui vient de mourir (ProcessLookupError pour os.kill(pid, 0))."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class DispatchScriptTests(unittest.TestCase):
    """dispatch-ticket.sh face à un faux zellij : panneau inactif requis, état du ticket intact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "pmo.db"
        self.db = CockpitDB(self.db_path)
        proj_root = self.base / "demo"
        wts = self.base / "worktrees"
        proj_root.mkdir()
        wts.mkdir()
        self.db.register_project("demo", "Démo", str(proj_root), str(wts), str(wts))
        self.db.upsert_ticket(
            "demo", "T-001", "Ticket test", complexity="M", priority="P2", files_owned=["a.py"]
        )
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        self.log = self.base / "zellij.log"
        self.panes_file = self.base / "panes.json"
        zellij = self.fake_bin / "zellij"
        zellij.write_text(FAKE_ZELLIJ, encoding="utf-8")
        zellij.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_panes(self, panes: list[dict]) -> None:
        self.panes_file.write_text(json.dumps(panes), encoding="utf-8")

    def run_dispatch(self) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ.get('PATH', '')}",
            "COCKPIT_HOME": str(ROOT),
            "DB_PATH": str(self.db_path),
            "FAKE_ZELLIJ_LOG": str(self.log),
            "FAKE_ZELLIJ_PANES": str(self.panes_file),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "T-001", "demo"], env=env, capture_output=True, text=True, timeout=30
        )

    def zellij_log(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_panneau_shell_inactif_dispache_sans_modifier_etat(self) -> None:
        """Shell inactif (commande courante = bash) : write-chars envoyé, état du ticket inchangé."""
        self.set_panes([{"id": 3, "is_plugin": False, "title": "AGY — démo", "current_command": "bash"}])
        res = self.run_dispatch()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OK", res.stdout)
        log = self.zellij_log()
        self.assertIn("write-chars", log)
        self.assertIn("run T-001 demo", log)
        # dispatch ne modifie jamais l'état : c'est run qui passera à ASSIGNED
        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t.get("lane") or "", "")

    def test_panneau_occupe_refuse_avec_commande_manuelle(self) -> None:
        """Panneau occupé (session agy interactive en commande courante) : refus sans write-chars."""
        self.set_panes([{"id": 3, "is_plugin": False, "title": "AGY — démo", "current_command": "agy"}])
        res = self.run_dispatch()
        self.assertEqual(res.returncode, 1)
        self.assertIn("run T-001 demo", res.stderr, "le message doit donner la commande à lancer à la main")
        self.assertNotIn("write-chars", self.zellij_log())
        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t.get("lane") or "", "")

    def test_panneau_avec_processus_enfant_refuse(self) -> None:
        """Sans commande courante, un processus enfant du pid du panneau = premier plan occupé."""
        busy = subprocess.Popen(["bash", "-c", "sleep 60 & wait"])
        self.addCleanup(busy.terminate)
        time.sleep(0.7)  # laisser le temps au fils d'apparaître dans /proc
        self.set_panes([{"id": 3, "is_plugin": False, "title": "AGY — démo", "pid": busy.pid}])
        res = self.run_dispatch()
        self.assertEqual(res.returncode, 1)
        self.assertIn("run T-001 demo", res.stderr)
        self.assertNotIn("write-chars", self.zellij_log())

    def test_panneau_sans_processus_enfant_accepte(self) -> None:
        """Sans commande courante, aucun processus enfant : shell inactif, dispatch accepté."""
        idle = subprocess.Popen(["sleep", "60"])
        self.addCleanup(idle.terminate)
        self.set_panes([{"id": 3, "is_plugin": False, "title": "AGY — démo", "pid": idle.pid}])
        res = self.run_dispatch()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("write-chars", self.zellij_log())
        t = self.db.get_ticket("demo", "T-001")
        self.assertEqual(t["state"], "READY")


class OrphanPipelineTests(unittest.TestCase):
    """core/pipeline.py : plus d'ASSIGNED orphelin (lane vide), release et status adaptés."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "pmo.db"
        self.db = CockpitDB(self.db_path)
        proj_root = self.base / "demo"
        wts = self.base / "worktrees"
        proj_root.mkdir()
        wts.mkdir()
        self.db.register_project("demo", "Démo", str(proj_root), str(wts), str(wts))
        self._old_db_path = pipeline.DB_PATH
        self._old_load_runners = pipeline.load_runners
        pipeline.DB_PATH = self.db_path
        pipeline.load_runners = lambda: {  # type: ignore[assignment]
            "defaults": {"max_attempts": 1},
            "runners": {},
            "gate1": {"max_reworks": 0},
        }

    def tearDown(self) -> None:
        pipeline.DB_PATH = self._old_db_path
        pipeline.load_runners = self._old_load_runners
        self.tmp.cleanup()

    def _make_orphan(self, tid: str) -> dict:
        """Ticket ASSIGNED sans voie et sans pid vivant (état orphelin), verrous posés."""
        self.db.upsert_ticket("demo", tid, "Ticket orphelin", files_owned=["a.py"])
        ok, conflicts = self.db.acquire_locks("demo", tid, ["a.py"])
        self.assertTrue(ok, conflicts)
        claimed, reason = self.db.claim_ticket("demo", tid, dead_pid())
        self.assertTrue(claimed, reason)
        t = self.db.get_ticket("demo", tid)
        assert t is not None
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t.get("lane") or "", "")
        return t

    def _worktree_lock_count(self, tid: str) -> int:
        with self.db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM worktree_locks WHERE project_id = 'demo' AND ticket_id = ?",
                (tid,),
            ).fetchone()
        return int(row["n"])

    def test_run_sans_fichiers_ne_laisse_pas_assigned_sans_lane(self) -> None:
        """run qui échoue avant le worker (files_owned vide) : retour à READY, jamais ASSIGNED/lane vide."""
        self.db.upsert_ticket("demo", "T-002", "Sans fichiers", files_owned=[])
        with contextlib.redirect_stderr(io.StringIO()):
            rc = pipeline.main(["run", "T-002", "demo"])
        self.assertEqual(rc, 1)
        t = self.db.get_ticket("demo", "T-002")
        assert t is not None
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t.get("lane") or "", "")
        self.assertIsNone(t["claimed_pid"])

    def test_run_en_conflit_de_verrous_ne_laisse_pas_orphelin(self) -> None:
        """run refusé sur verrous d'un autre ticket : T-004 revient à READY, verrous de T-003 intacts."""
        self.db.upsert_ticket("demo", "T-003", "A", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-004", "B", files_owned=["a.py"])
        ok, _ = self.db.acquire_locks("demo", "T-003", ["a.py"])
        self.assertTrue(ok)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = pipeline.main(["run", "T-004", "demo"])
        self.assertEqual(rc, 1)
        t = self.db.get_ticket("demo", "T-004")
        assert t is not None
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t.get("lane") or "", "")
        self.assertIsNone(t["claimed_pid"])
        ok2, conflicts = self.db.acquire_locks("demo", "T-005", ["a.py"])
        self.assertFalse(ok2)
        self.assertTrue(any("T-003" in c for c in conflicts))

    def test_release_accepte_ticket_orphelin_et_remet_ready(self) -> None:
        """release d'un ASSIGNED sans voie ni pid vivant : READY, owner=auto, verrous libérés."""
        self._make_orphan("T-005")
        self.assertEqual(self._worktree_lock_count("T-005"), 1)

        rc = pipeline.main(["release", "T-005", "demo"])
        self.assertEqual(rc, 0)

        t = self.db.get_ticket("demo", "T-005")
        assert t is not None
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["owner"], "auto")
        self.assertEqual(t.get("lane") or "", "")
        self.assertIsNone(t["claimed_pid"])
        ok, conflicts = self.db.acquire_locks("demo", "T-006", ["a.py"])
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])
        self.assertEqual(self._worktree_lock_count("T-005"), 0)
        self.assertIn("RELEASED", [e["kind"] for e in self.db.recent_events("demo", 10)])

    def test_release_refuse_orphelin_avec_pid_vivant(self) -> None:
        """Refus inchangé si un pid vivant tient le ticket ASSIGNED sans voie."""
        self.db.upsert_ticket("demo", "T-007", "Orphelin vivant", files_owned=["a.py"])
        claimed, reason = self.db.claim_ticket("demo", "T-007", os.getpid())
        self.assertTrue(claimed, reason)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = pipeline.main(["release", "T-007", "demo"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUS", err.getvalue())
        t = self.db.get_ticket("demo", "T-007")
        assert t is not None
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t.get("lane") or "", "")
        self.assertEqual(t["claimed_pid"], os.getpid())

    def test_status_signale_orphelin_avec_commande_release(self) -> None:
        """status marque ORPHELIN un ASSIGNED sans voie ni pid vivant, avec la commande release."""
        self._make_orphan("T-008")
        self.db.upsert_ticket(
            "demo", "T-009", "Actif avec voie", state="ASSIGNED", owner="agy", files_owned=["b.py"]
        )
        self.db.update_ticket("demo", "T-009", lane="agy_standard")

        out = pipeline.build_status(self.db, "demo")
        lines = out.splitlines()
        orphan_lines = [l for l in lines if l.strip().startswith("T-008 ") and "ASSIGNED" in l]
        self.assertEqual(len(orphan_lines), 1)
        self.assertIn("ORPHELIN", orphan_lines[0])
        self.assertIn("cockpit release T-008 demo", orphan_lines[0])
        lane_lines = [l for l in lines if l.strip().startswith("T-009 ") and "ASSIGNED" in l]
        self.assertEqual(len(lane_lines), 1)
        self.assertNotIn("ORPHELIN", lane_lines[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
