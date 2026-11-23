#!/usr/bin/env python3
"""
Tests Multi-CLI (T-016) : prise de ticket atomique (claim_ticket / unclaim_ticket),
non-collision de deux run_ticket simultanés, limites de parallélisme par fournisseur
dans plan_batch / cmd_auto, et sérialisation de merge_ticket par verrou de fichier.
Aucun réseau, aucun LLM.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))

import pipeline  # noqa: E402
import pmo_db  # noqa: E402
import scheduler  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def _dead_pid() -> int:
    """Retourne un PID garanti inexistant (ProcessLookupError sur os.kill(pid, 0))."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    pid = proc.pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    # Repli : grand PID hors plage usuelle si recyclage immédiat
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise RuntimeError("Impossible de trouver un PID mort")


class MultiCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "demo"
        self.wts = base / "demo-worktrees"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@local")
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
            "demo", "Demo", str(self.root), str(self.wts), str(self.wts), milestones=[("M1", "Socle")]
        )
        self.script = base / "worker.sh"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60, "require_tests": False},
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
        self.logs = base / "home"
        (self.logs / "prompts").mkdir(parents=True)
        (self.logs / "prompts" / "sys_coder_tdd.md").write_text("Implémente le brief.", encoding="utf-8")
        (self.logs / "prompts" / "sys_gate1_nas32k.md").write_text("Audite.", encoding="utf-8")
        pipeline.COCKPIT_HOME = self.logs
        pipeline.DB_PATH = base / "pmo.db"

    def tearDown(self) -> None:
        pipeline.load_runners = self._orig_runners  # type: ignore[assignment]
        pipeline.COCKPIT_HOME = self._orig_home
        pipeline.DB_PATH = self._orig_db_path
        self.tmp.cleanup()

    def worker(self, body: str) -> None:
        self.script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n", encoding="utf-8")

    def test_claim_ticket_states_and_pid_handling(self) -> None:
        """claim_ticket : READY pris, pid vivant refusé, pid mort repris, interactif refusé, autre état refusé."""
        self.db.upsert_ticket("demo", "T-001", "Ticket 1", milestone_id="M1", complexity="S", files_owned=["a.py"])
        my_pid = os.getpid()

        # 1. Ticket READY -> ASSIGNED + claimed_pid = my_pid
        ok, reason = self.db.claim_ticket("demo", "T-001", my_pid)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        t = self.db.get_ticket("demo", "T-001")
        assert t is not None
        self.assertEqual(t["state"], "ASSIGNED")
        self.assertEqual(t["claimed_pid"], my_pid)

        # 2. PID vivant -> refusé
        for active_state in ("ASSIGNED", "GATE0", "GATE1"):
            self.db.update_ticket("demo", "T-001", state=active_state)
            ok, reason = self.db.claim_ticket("demo", "T-001", my_pid + 1)
            self.assertFalse(ok)
            self.assertEqual(reason, f"déjà en cours d'exécution (pid {my_pid})")

        # 3. PID mort -> repris
        dead = _dead_pid()
        with self.db._connect() as conn:
            conn.execute("UPDATE tickets SET state = 'GATE0', claimed_pid = ? WHERE id = 'T-001'", (dead,))
        ok, reason = self.db.claim_ticket("demo", "T-001", my_pid)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        t = self.db.get_ticket("demo", "T-001")
        assert t is not None
        self.assertEqual(t["claimed_pid"], my_pid)

        # 4. unclaim_ticket remet claimed_pid à NULL sans toucher à l'état -> mode interactif refusé
        self.db.update_ticket("demo", "T-001", state="ASSIGNED", owner="claude-session")
        self.db.unclaim_ticket("demo", "T-001")
        t = self.db.get_ticket("demo", "T-001")
        assert t is not None
        self.assertIsNone(t["claimed_pid"])
        self.assertEqual(t["state"], "ASSIGNED")

        ok, reason = self.db.claim_ticket("demo", "T-001", my_pid)
        self.assertFalse(ok)
        self.assertEqual(reason, "pris en mode interactif par claude-session")

        # 5. Autre état (GATE2, VALIDATED, MERGED, BLOCKED) -> refusé
        for other_state in ("GATE2", "VALIDATED", "MERGED", "BLOCKED"):
            self.db.update_ticket("demo", "T-001", state=other_state)
            ok, reason = self.db.claim_ticket("demo", "T-001", my_pid)
            self.assertFalse(ok)
            self.assertEqual(reason, f"état {other_state}")

    def test_two_simultaneous_run_ticket_only_one_executes(self) -> None:
        """Deux run_ticket simultanés du même ticket (threads) : un seul s'exécute."""
        self.db.upsert_ticket("demo", "T-002", "Concurrence", milestone_id="M1", complexity="S", files_owned=["a.py"])
        counter_file = Path(self.tmp.name) / "worker_calls.txt"
        self.worker(f"sleep 0.25\necho 1 >> '{counter_file}'\necho 'X = 1' > a.py")

        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, int | str]] = []
        lock = threading.Lock()

        def _target() -> None:
            barrier.wait()
            try:
                rc = pipeline.run_ticket(self.db, "demo", "T-002", use_gate1=False)
                with lock:
                    outcomes.append(("ok", rc))
            except pipeline.PipelineError as exc:
                with lock:
                    outcomes.append(("err", str(exc)))

        t1 = threading.Thread(target=_target)
        t2 = threading.Thread(target=_target)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        oks = [v for k, v in outcomes if k == "ok"]
        errs = [v for k, v in outcomes if k == "err"]
        self.assertEqual(oks, [0])
        self.assertEqual(len(errs), 1)
        self.assertIn("déjà en cours d'exécution", str(errs[0]))
        calls = counter_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(calls), 1)

    def test_claimed_pid_is_null_after_run_ticket_and_after_exception(self) -> None:
        """claimed_pid revient à NULL après run_ticket normal et après une exception."""
        self.db.upsert_ticket("demo", "T-003", "Normal", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-003", use_gate1=False), 0)
        t3 = self.db.get_ticket("demo", "T-003")
        assert t3 is not None
        self.assertIsNone(t3["claimed_pid"])

        self.db.upsert_ticket("demo", "T-004", "Exception", milestone_id="M1", complexity="S", files_owned=["b.py"])
        orig_worker = pipeline.run_worker

        def boom(*_args: object, **_kwargs: object) -> tuple[int, float]:
            # Pendant l'exécution, claimed_pid doit être renseigné
            cur = self.db.get_ticket("demo", "T-004")
            assert cur is not None
            self.assertEqual(cur["claimed_pid"], os.getpid())
            raise RuntimeError("panne simulée")

        try:
            pipeline.run_worker = boom  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):
                pipeline.run_ticket(self.db, "demo", "T-004", use_gate1=False)
        finally:
            pipeline.run_worker = orig_worker  # type: ignore[assignment]

        t4 = self.db.get_ticket("demo", "T-004")
        assert t4 is not None
        self.assertIsNone(t4["claimed_pid"])

    def test_plan_batch_provider_limits(self) -> None:
        """plan_batch respecte provider_limits (en comptant ceux en cours, absent = illimité, None = règle agy_)."""
        self.db.set_mistral_key_active(False)
        # T-A1 est déjà en cours (ASSIGNED) sur agy_standard (provider agy)
        self.db.upsert_ticket(
            "demo", "T-A1", "AGY en cours", priority="P0", complexity="M", state="ASSIGNED", files_owned=["a1.py"]
        )
        self.db.update_ticket("demo", "T-A1", lane="agy_standard")

        # T-A2 est READY et choisirait agy_standard (complexity="M")
        self.db.upsert_ticket(
            "demo", "T-A2", "AGY prêt", priority="P0", complexity="M", state="READY", files_owned=["a2.py"]
        )
        # T-D1, T-D2 sont READY sur deepseek_coder (complexity="S", mistral inactif)
        self.db.upsert_ticket(
            "demo", "T-D1", "DS 1", priority="P1", complexity="S", state="READY", files_owned=["d1.py"]
        )
        self.db.upsert_ticket(
            "demo", "T-D2", "DS 2", priority="P1", complexity="S", state="READY", files_owned=["d2.py"]
        )

        lane_providers = {
            "agy_standard": "agy",
            "agy_argon": "agy",
            "deepseek_coder": "deepseek",
            "mistral_vibe": "mistral",
        }
        provider_limits = {"agy": 1, "deepseek": 1}

        # Avec agy=1 (déjà 1 ASSIGNED -> 0 possible), deepseek=1 (1 seul pris)
        batch = scheduler.plan_batch(
            self.db, "demo", slots=4, provider_limits=provider_limits, lane_providers=lane_providers
        )
        self.assertEqual([b["id"] for b in batch], ["T-D1"])

        # Avec Mistral actif et mistral=2 : T-D1, T-D2 (S) partent sur mistral_vibe (limite 2 atteinte),
        # T-A2 (M), T-M1, T-M2 (XS) attendent le lot suivant.
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            "demo", "T-M1", "Mistral 1", priority="P2", complexity="XS", state="READY", files_owned=["m1.py"]
        )
        self.db.upsert_ticket(
            "demo", "T-M2", "Mistral 2", priority="P2", complexity="XS", state="READY", files_owned=["m2.py"]
        )
        batch_ms = scheduler.plan_batch(
            self.db,
            "demo",
            slots=4,
            provider_limits={"agy": 1, "deepseek": 1, "mistral": 2},
            lane_providers=lane_providers,
        )
        self.assertEqual([b["id"] for b in batch_ms], ["T-D1", "T-D2"])
        self.assertTrue(all(b["lane"] == "mistral_vibe" for b in batch_ms))

        # Si provider_limits est None : comportement historique (T-A1 déjà ASSIGNED n'empêche pas T-A2 dans le lot,
        # mais un seul agy_* par lot)
        self.db.set_mistral_key_active(False)
        self.db.upsert_ticket(
            "demo", "T-A3", "AGY prêt 2", priority="P0", complexity="L", state="READY", files_owned=["a3.py"]
        )
        batch_none = scheduler.plan_batch(self.db, "demo", slots=4, provider_limits=None)
        agy_in_batch = [b["id"] for b in batch_none if b["lane"].startswith("agy_")]
        self.assertEqual(len(agy_in_batch), 1)
        self.assertIn("T-D1", [b["id"] for b in batch_none])
        self.assertIn("T-D2", [b["id"] for b in batch_none])

    def test_two_simultaneous_merge_ticket_both_succeed(self) -> None:
        """Deux merge_ticket simultanés sur le même dépôt sont sérialisés et aboutissent tous les deux."""
        self.db.upsert_ticket("demo", "T-010", "Fichier A", milestone_id="M1", complexity="S", files_owned=["a.py"])
        self.db.upsert_ticket("demo", "T-011", "Fichier B", milestone_id="M1", complexity="S", files_owned=["b.py"])

        self.worker("echo 'A = 1' > a.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-010", use_gate1=False), 0)
        self.worker("echo 'B = 2' > b.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-011", use_gate1=False), 0)

        barrier = threading.Barrier(2)
        results: dict[str, int] = {}

        def _merge(tid: str) -> None:
            barrier.wait()
            results[tid] = pipeline.merge_ticket(self.db, "demo", tid, approve=True)

        t1 = threading.Thread(target=_merge, args=("T-010",))
        t2 = threading.Thread(target=_merge, args=("T-011",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results, {"T-010": 0, "T-011": 0})
        self.assertTrue((self.root / "a.py").exists())
        self.assertTrue((self.root / "b.py").exists())
        self.assertEqual(self.db.get_ticket("demo", "T-010")["state"], "MERGED")
        self.assertEqual(self.db.get_ticket("demo", "T-011")["state"], "MERGED")

    def test_cmd_auto_uses_max_parallel_and_runners_toml_defines_it(self) -> None:
        """cmd_auto construit lane_providers et provider_limits depuis max_parallel ; runners.toml a max_parallel=1."""
        real_cfg = pipeline.load_toml(COCKPIT_ROOT / "config" / "runners.example.toml")
        self.assertEqual(real_cfg["runners"]["agy_standard"].get("max_parallel"), 1)
        self.assertEqual(real_cfg["runners"]["agy_argon"].get("max_parallel"), 1)

        # Avec max_parallel=1 sur deepseek_coder et un ticket deepseek déjà ASSIGNED, cmd_auto --dry-run l'ignore
        self.runner_cfg["runners"]["deepseek_coder"]["provider"] = "deepseek"
        self.runner_cfg["runners"]["deepseek_coder"]["max_parallel"] = 1
        self.db.upsert_ticket(
            "demo", "T-020", "DS en cours", priority="P0", complexity="S", state="ASSIGNED", files_owned=["a.py"]
        )
        self.db.update_ticket("demo", "T-020", lane="deepseek_coder")
        self.db.upsert_ticket(
            "demo", "T-021", "DS prêt", priority="P1", complexity="S", state="READY", files_owned=["b.py"]
        )
        self.assertEqual(pipeline.main(["auto", "demo", "--dry-run"]), 0)
        # Sans max_parallel défini sur aucune voie, provider_limits=None est passé
        self.runner_cfg["runners"]["deepseek_coder"].pop("max_parallel", None)
        captured: list[tuple[object, object]] = []
        orig_plan = scheduler.plan_batch

        def spy_plan(db: pmo_db.CockpitDB, project_id: str, slots: int = 2,
                     provider_limits: dict[str, int] | None = None,
                     lane_providers: dict[str, str] | None = None) -> list[dict[str, object]]:
            captured.append((provider_limits, lane_providers))
            return orig_plan(db, project_id, slots, provider_limits=provider_limits, lane_providers=lane_providers)

        try:
            scheduler.plan_batch = spy_plan  # type: ignore[assignment]
            self.assertEqual(pipeline.main(["auto", "demo", "--dry-run"]), 0)
        finally:
            scheduler.plan_batch = orig_plan  # type: ignore[assignment]
        self.assertEqual(captured[0][0], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
