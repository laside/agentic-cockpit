#!/usr/bin/env python3
"""
Tests du verrou exclusif de worktree (T-058) : un seul worker actif par worktree.

- claim_ticket pose le verrou de worktree dans la même transaction (BEGIN IMMEDIATE)
  que le passage READY -> ASSIGNED : un second claim sur le même worktree est refusé
  avec un message explicite (état non READY ou pid vivant), sans changer l'état.
- Reaper d'un pid mort : un nouveau worker reprend le ticket et le verrou.
- ensure_worktree, run_ticket et claim refusent un second worker sur un worktree
  déjà actif (verrou détenu par un autre ticket avec pid vivant ou mode interactif).
- Le verrou est relâché après submit, abandon, release ou échec (run_ticket).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import pipeline  # noqa: E402
import pmo_db  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


class WorktreeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "demo"
        self.wts = base / "demo-worktrees"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.com")
        (self.root / "Makefile").write_text(
            "lint:\n\t@true\n\ntest:\n\t@python3 -B -m unittest discover -s tests -q 2>&1\n",
            encoding="utf-8",
        )
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_base.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.db = pmo_db.CockpitDB(base / "pmo.db")
        self.db.register_project(
            "demo",
            "Demo",
            str(self.root),
            str(self.wts),
            str(self.wts),
            milestones=[("C3", "Chantiers")],
        )
        self.script = base / "worker.sh"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60, "max_children": 2},
            "runners": {
                name: {"agent": "fake", "provider": "local", "argv": ["bash", str(self.script), "{prompt}"]}
                for name in ("deepseek_coder", "agy_standard", "agy_argon", "mistral_vibe", "local_nas_coder")
            },
            "gate1": {"max_reworks": 1},
        }
        self._orig_runners = pipeline.load_runners
        self._orig_home = pipeline.COCKPIT_HOME
        self._orig_db_path = pipeline.DB_PATH
        pipeline.load_runners = lambda: self.runner_cfg  # type: ignore[assignment]
        self.home = base / "home"
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "sys_coder_tdd.md").write_text("Implemente le brief.", encoding="utf-8")
        (self.home / "prompts" / "sys_gate1_nas32k.md").write_text("Audite.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.home
        pipeline.DB_PATH = base / "pmo.db"

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        self.tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────

    def worker(self, body: str) -> None:
        self.script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n", encoding="utf-8")

    def dead_pid(self) -> int:
        """Un pid sûrement mort : processus fils déjà terminé."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return int(proc.pid)

    def locks(self, ticket_id: str | None = None) -> list[dict]:
        with self.db._connect() as conn:
            if ticket_id:
                rows = conn.execute(
                    "SELECT * FROM worktree_locks WHERE project_id = 'demo' AND ticket_id = ?",
                    (ticket_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM worktree_locks WHERE project_id = 'demo'").fetchall()
        return [dict(r) for r in rows]

    def wt_path(self, ticket_id: str) -> str:
        proj = self.db.get_project("demo")
        assert proj is not None
        return os.path.normpath(str(Path(proj["wt_tickets_dir"]) / ticket_id))

    def pose_verrou_etranger(self, ticket_id: str, detenteur: str, pid: int | None) -> None:
        """Simule un verrou de worktree détenu par un autre ticket sur le worktree de `ticket_id`."""
        with self.db._connect() as conn:
            conn.execute(
                "INSERT INTO worktree_locks (project_id, worktree_path, ticket_id, pid) VALUES ('demo', ?, ?, ?)",
                (self.wt_path(ticket_id), detenteur, pid),
            )

    def add_ticket(self, ticket_id: str, files: list[str]) -> None:
        self.db.upsert_ticket(
            "demo", ticket_id, f"Ticket {ticket_id}", milestone_id="C3", files_owned=files
        )

    # ── pose du verrou dans la transaction de claim ──────────

    def test_claim_pose_le_verrou_worktree(self) -> None:
        """claim_ticket READY -> ASSIGNED pose le verrou de worktree (même transaction)."""
        self.add_ticket("T-001", ["a.py"])
        ok, msg = self.db.claim_ticket("demo", "T-001", os.getpid())
        self.assertTrue(ok, msg)
        t = self.db.get_ticket("demo", "T-001")
        assert t is not None
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t["claimed_pid"], os.getpid())
        locks = self.locks("T-001")
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["pid"], os.getpid())
        self.assertEqual(locks[0]["worktree_path"], self.wt_path("T-001"))

    def test_second_claim_meme_worktree_refuse_pid_vivant(self) -> None:
        """Deux claims sur le même worktree : le second est refusé (pid vivant), verrou inchangé."""
        self.add_ticket("T-001", ["a.py"])
        ok1, _ = self.db.claim_ticket("demo", "T-001", os.getpid())
        self.assertTrue(ok1)
        ok2, msg = self.db.claim_ticket("demo", "T-001", self.dead_pid())
        self.assertFalse(ok2)
        self.assertIn("déjà en cours d'exécution", msg)
        locks = self.locks("T-001")
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["pid"], os.getpid())

    def test_claim_refuse_si_worktree_deja_actif_par_autre_ticket(self) -> None:
        """Le worktree est déjà verrouillé par un autre ticket (pid vivant) : claim refusé,
        et la transaction ne change pas l'état du ticket."""
        self.add_ticket("T-001", ["a.py"])
        self.add_ticket("T-002", ["b.py"])
        self.pose_verrou_etranger("T-002", "T-001", os.getpid())

        ok, msg = self.db.claim_ticket("demo", "T-002", self.dead_pid())
        self.assertFalse(ok)
        self.assertIn("déjà actif", msg)
        self.assertIn("T-001", msg)
        t = self.db.get_ticket("demo", "T-002")
        assert t is not None
        self.assertEqual(t["state"], "READY")
        self.assertIsNone(t["claimed_pid"])

    def test_reaper_pid_mort_reprend_verrou(self) -> None:
        """Un pid mort est reapé : le nouveau worker reprend le ticket ET le verrou de worktree."""
        self.add_ticket("T-001", ["a.py"])
        dead = self.dead_pid()
        ok, msg = self.db.claim_ticket("demo", "T-001", dead)
        self.assertTrue(ok, msg)
        self.assertEqual(self.locks("T-001")[0]["pid"], dead)

        ok2, msg2 = self.db.claim_ticket("demo", "T-001", os.getpid())
        self.assertTrue(ok2, msg2)
        t = self.db.get_ticket("demo", "T-001")
        assert t is not None
        self.assertEqual(t["claimed_pid"], os.getpid())
        locks = self.locks("T-001")
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["pid"], os.getpid())

    def test_reaper_verrou_etranger_pid_mort(self) -> None:
        """Verrou étranger sur le même worktree mais pid mort : il est reapé, le claim passe."""
        self.add_ticket("T-001", ["a.py"])
        self.add_ticket("T-002", ["b.py"])
        self.pose_verrou_etranger("T-002", "T-001", self.dead_pid())

        ok, msg = self.db.claim_ticket("demo", "T-002", os.getpid())
        self.assertTrue(ok, msg)
        locks = self.locks("T-002")
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["pid"], os.getpid())

    # ── refus côté pipeline ──────────────────────────────────

    def test_ensure_worktree_refuse_second_worker(self) -> None:
        """ensure_worktree refuse un second worker sur un worktree déjà actif (message explicite)."""
        self.add_ticket("T-001", ["a.py"])
        proj = self.db.get_project("demo")
        assert proj is not None
        self.pose_verrou_etranger("T-001", "T-999", os.getpid())

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.ensure_worktree(proj, "T-001", db=self.db)
        self.assertIn("déjà actif", str(ctx.exception))
        self.assertIn("T-999", str(ctx.exception))
        self.assertFalse(Path(self.wt_path("T-001")).exists())

        # Sans verrou étranger, ensure_worktree réutilise/crée le worktree normalement.
        self.db.release_worktree_lock("demo", "T-999")
        wt = pipeline.ensure_worktree(proj, "T-001", db=self.db)
        self.assertTrue(wt.exists())

        # Le détenteur du verrou lui-même n'est pas refusé (son propre worktree).
        self.pose_verrou_etranger("T-001", "T-001", os.getpid())
        wt = pipeline.ensure_worktree(proj, "T-001", db=self.db)
        self.assertTrue(wt.exists())

    def test_run_ticket_refuse_si_ticket_deja_en_cours(self) -> None:
        """run_ticket sur un ticket dont le worktree est déjà actif (pid vivant) : refus explicite."""
        self.add_ticket("T-001", ["a.py"])
        ok, _ = self.db.claim_ticket("demo", "T-001", os.getpid())
        self.assertTrue(ok)
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_ticket(self.db, "demo", "T-001")
        self.assertIn("exécution refusée", str(ctx.exception))
        self.assertIn("pid", str(ctx.exception))

    def test_run_ticket_refuse_si_worktree_partage_actif(self) -> None:
        """run_ticket refusé quand le worktree est verrouillé par un autre ticket (pid vivant)."""
        self.add_ticket("T-001", ["a.py"])
        self.add_ticket("T-002", ["b.py"])
        self.pose_verrou_etranger("T-002", "T-001", os.getpid())
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_ticket(self.db, "demo", "T-002")
        self.assertIn("déjà actif", str(ctx.exception))
        t = self.db.get_ticket("demo", "T-002")
        assert t is not None
        self.assertEqual(t["state"], "READY")

    def test_double_claim_interactif_refuse(self) -> None:
        """Deux claims interactifs sur le même worktree : le second est refusé, le verrou reste au premier."""
        self.add_ticket("T-008", ["a.py"])
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-008", "agy"), 0)
        locks = self.locks("T-008")
        self.assertEqual(len(locks), 1)
        self.assertIsNone(locks[0]["pid"])  # mode interactif

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.claim_ticket(self.db, "demo", "T-008", "claude")
        self.assertIn("claim refusé", str(ctx.exception))

        with self.assertRaises(pipeline.PipelineError):
            pipeline.run_ticket(self.db, "demo", "T-008")

        locks = self.locks("T-008")
        self.assertEqual(len(locks), 1)

    # ── libération du verrou ─────────────────────────────────

    def test_verrou_relaché_apres_run_succes(self) -> None:
        """Après un run réussi (et son finally), le verrou de worktree est relâché."""
        self.add_ticket("T-003", ["a.py", "tests/test_a.py"])
        self.worker(
            "echo 'A = 1' > a.py\n"
            "cat > tests/test_a.py << 'EOF'\n"
            "import unittest, a\n"
            "class TestA(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertEqual(a.A, 1)\n"
            "EOF"
        )
        rc = pipeline.run_ticket(self.db, "demo", "T-003", use_gate1=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.locks("T-003"), [])
        t = self.db.get_ticket("demo", "T-003")
        assert t is not None
        self.assertIsNone(t["claimed_pid"])
        self.assertIn(t["state"], ("VALIDATED", "GATE2"))

    def test_verrou_relaché_apres_echec(self) -> None:
        """Après un échec (GATE2), le verrou de worktree est relâché."""
        self.add_ticket("T-004", ["a.py"])
        self.worker("true")  # le worker ne produit rien : Porte 0 FAIL
        rc = pipeline.run_ticket(self.db, "demo", "T-004", max_attempts=1, use_gate1=False)
        self.assertEqual(rc, 1)
        t = self.db.get_ticket("demo", "T-004")
        assert t is not None
        self.assertEqual(t["state"], "GATE2")
        self.assertEqual(self.locks("T-004"), [])

    def test_verrou_relaché_apres_submit(self) -> None:
        """Après submit d'un claim interactif, le verrou de worktree est relâché."""
        self.add_ticket("T-005", ["a.py", "tests/test_a.py"])
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-005", "agy"), 0)
        self.assertEqual(len(self.locks("T-005")), 1)

        proj = self.db.get_project("demo")
        assert proj is not None
        wt = Path(proj["wt_tickets_dir"]) / "T-005"
        (wt / "a.py").write_text("A = 1\n", encoding="utf-8")
        (wt / "tests" / "test_a.py").write_text(
            "import unittest, a\n\nclass TestA(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(a.A, 1)\n",
            encoding="utf-8",
        )
        rc = pipeline.submit_ticket(self.db, "demo", "T-005")
        self.assertEqual(rc, 0)
        self.assertEqual(self.locks("T-005"), [])

    def test_verrou_relaché_apres_abandon(self) -> None:
        """Après abandon, le verrou de worktree est relâché."""
        self.add_ticket("T-006", ["a.py"])
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-006", "agy"), 0)
        self.assertEqual(len(self.locks("T-006")), 1)

        abandoned = pipeline.abandon_ticket(self.db, "demo", "T-006")
        self.assertEqual(abandoned, ["T-006"])
        self.assertEqual(self.locks(), [])
        t = self.db.get_ticket("demo", "T-006")
        assert t is not None
        self.assertEqual(t["state"], "ABANDONED")

    def test_verrou_relaché_apres_release(self) -> None:
        """Après release d'un claim interactif, le verrou de worktree est relâché."""
        self.add_ticket("T-007", ["a.py"])
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-007", "agy"), 0)
        self.assertEqual(len(self.locks("T-007")), 1)

        self.assertEqual(pipeline.release_ticket(self.db, "demo", "T-007"), 0)
        self.assertEqual(self.locks("T-007"), [])
        t = self.db.get_ticket("demo", "T-007")
        assert t is not None
        self.assertEqual(t["state"], "READY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
