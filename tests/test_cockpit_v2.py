#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT / "core"))
sys.path.insert(0, str(COCKPIT_ROOT / "tui"))

import context_compiler
import gatekeeper
import pmo_db
import pmo_dashboard


class TestAgenticCockpitV2(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test_pmo.db"
        self.db = pmo_db.CockpitDB(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_01_mitigation_secrets_and_ports(self) -> None:
        self.assertTrue(pmo_db.is_secret_or_forbidden_path(".env"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("backend/.env.local"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("secrets/db_url"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("/mnt/c/Projets/demo/secrets/exclusion.key"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("../x.py"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("a/../../x.py"))
        self.assertTrue(pmo_db.is_secret_or_forbidden_path("/etc/passwd"))
        self.assertFalse(pmo_db.is_secret_or_forbidden_path(".env.example"))
        self.assertFalse(pmo_db.is_secret_or_forbidden_path("backend/src/app/api.py"))
        self.assertFalse(pmo_db.is_secret_or_forbidden_path("backend/src/demo/api.py"))

        collisions = pmo_db.validate_projects_ports(COCKPIT_ROOT / "config/projects.example.toml")
        self.assertEqual(collisions, [], f"Collisions de ports détectées : {collisions}")

    def test_01b_acquire_locks_normalization_and_no_lock_stealing(self) -> None:
        """(e) acquire_locks : conflit détecté entre « ./a.py » et « a.py », et un second ticket ne peut pas voler le verrou."""
        ok1, conflicts1 = self.db.acquire_locks("demo", "T-001", ["./a.py"])
        self.assertTrue(ok1)
        self.assertEqual(conflicts1, [])

        ok2, conflicts2 = self.db.acquire_locks("demo", "T-002", ["a.py"])
        self.assertFalse(ok2)
        self.assertEqual(conflicts2, ["a.py (verrouillé par T-001)"])

        # Ré-acquisition idempotente par le même ticket
        ok3, conflicts3 = self.db.acquire_locks("demo", "T-001", ["a.py"])
        self.assertTrue(ok3)
        self.assertEqual(conflicts3, [])

        # Vérifie que T-001 détient toujours le verrou dans la base
        with self.db._connect() as conn:
            row = conn.execute(
                "SELECT ticket_id FROM file_locks WHERE project_id = 'demo' AND file_path = 'a.py'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ticket_id"], "T-001")

    def test_02_quota_governor_mistral_standby_and_argon_escalation(self) -> None:
        route_xs = self.db.select_optimal_lane(complexity="XS", priority="P2")
        self.assertEqual(route_xs["lane"], "deepseek_coder")

        self.db.set_mistral_key_active(True)
        route_xs_active = self.db.select_optimal_lane(complexity="XS", priority="P2")
        self.assertEqual(route_xs_active["lane"], "mistral_vibe")
        self.assertEqual(route_xs_active["cli"], "vibe")

        self.db.update_quota_usage("agy", window_used_pct=20.0)
        route_m_ok = self.db.select_optimal_lane(complexity="M", priority="P2")
        self.assertEqual(route_m_ok["lane"], "agy_standard")

        self.db.update_quota_usage("agy", window_used_pct=68.0)
        route_m_shield = self.db.select_optimal_lane(complexity="M", priority="P2")
        self.assertIn(route_m_shield["lane"], ("mistral_vibe", "deepseek_coder"))

        route_argon = self.db.select_optimal_lane(complexity="M", priority="P2", fail_count=2)
        self.assertEqual(route_argon["lane"], "agy_argon")
        self.assertEqual(route_argon["cli"], "agy --model argon")

    def test_03_pmo_dag_and_critical_path_ordering(self) -> None:
        self.db.register_project(
            "demo", "Demo", "/tmp/demo", "/tmp/demo-wt-s", "/tmp/demo-wt",
            milestones=[("J1", "Socle"), ("J2", "UI")]
        )
        self.db.upsert_ticket("demo", "T-001", "Socle DB", milestone_id="J1", priority="P2", complexity="M")
        self.db.upsert_ticket("demo", "T-002", "API dépendante", milestone_id="J1", priority="P1", depends_on=["T-001"])
        self.db.upsert_ticket("demo", "T-003", "UI dépendante", milestone_id="J1", priority="P1", depends_on=["T-001"])
        self.db.upsert_ticket("demo", "T-004", "Ticket isolé", milestone_id="J1", priority="P2", complexity="S")

        q1 = self.db.get_prioritized_queue("demo")
        states = {t["id"]: t["state"] for t in q1}
        self.assertEqual(states["T-002"], "BLOCKED")
        self.assertEqual(states["T-003"], "BLOCKED")
        ready_ids = [t["id"] for t in q1 if t["state"] == "READY"]
        self.assertEqual(ready_ids[0], "T-001")

        unblocked = self.db.complete_and_merge_ticket("demo", "T-001")
        self.assertEqual(sorted(unblocked), ["T-002", "T-003"])

    def test_04_context_compiler_and_gatekeeper(self) -> None:
        sample_py = (
            "class ConnectorBase:\n"
            '    """Contrat de base des connecteurs."""\n'
            "    def fetch(self, url: str, timeout: int = 30) -> bytes:\n"
            "        x = 1 + 2\n"
            "        return b'data'\n"
        )
        skel = context_compiler.extract_python_ast_skeleton(sample_py)
        self.assertIn("class ConnectorBase:", skel)
        self.assertIn("def fetch(self, url: str, timeout: int=30) -> bytes: ...", skel)
        self.assertNotIn("x = 1 + 2", skel)

        long_log = "\n".join([f"test_ok_{i} PASSED" for i in range(100)] + ["FAILED tests/test_api.py::test_auth - AssertionError: 401 != 200"])
        sieved = gatekeeper.sieve_test_output(long_log, max_lines=10)
        self.assertIn("FAILED tests/test_api.py::test_auth", sieved)
        self.assertLessEqual(len(sieved.splitlines()), 10)

        ok, unauth, sec = gatekeeper.verify_worktree_scope(
            modified_files=["backend/src/demo/api.py", ".env"],
            allowed_files=["backend/src/demo/api.py"],
        )
        self.assertFalse(ok)
        self.assertEqual(sec, [".env"])

    def test_05_new_project_script_and_rollback(self) -> None:
        real_projects_toml = COCKPIT_ROOT / "config/projects.example.toml"
        real_toml_before = real_projects_toml.read_text(encoding="utf-8")
        tmp_projects_toml = self.tmp_path / "projects.toml"
        tmp_projects_toml.write_text(real_toml_before, encoding="utf-8")

        proj_dir = self.tmp_path / "projets/kalipso"
        env = os.environ.copy()
        env["COCKPIT_HOME"] = str(COCKPIT_ROOT)
        env["DB_PATH"] = str(self.db_path)
        env["HOME"] = str(self.tmp_path)
        env["PROJECTS_TOML"] = str(tmp_projects_toml)

        res = subprocess.run(
            ["bash", str(COCKPIT_ROOT / "scripts/new-project.sh"), "kalipso", str(proj_dir), "8805"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("OK : Nouveau projet 'kalipso' initialisé", res.stdout)
        self.assertTrue((proj_dir / "AGENTS.md").exists())
        self.assertTrue((proj_dir / "docs/ROADMAP.md").exists())
        self.assertFalse((proj_dir / ".agents/board/inbox").exists())
        agents_md = (proj_dir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("cockpit status", agents_md)
        self.assertTrue((self.tmp_path / "projets/kalipso-worktrees/wt-claude").is_dir())

        # 1. Aucun ticket T-001 parasite dans la base temporaire pour le projet
        self.assertIsNone(self.db.get_ticket("kalipso", "T-001"))

        # 2. .nexus/rules/core.md contient « Zéro exploration »
        core_rules = (proj_dir / ".nexus/rules/core.md").read_text(encoding="utf-8")
        self.assertIn("Zéro exploration", core_rules)

        # 3. .nexus/project.toml contient test_cmd
        proj_toml = (proj_dir / ".nexus/project.toml").read_text(encoding="utf-8")
        self.assertIn("test_cmd", proj_toml)

        # 4. La copie temporaire contient [projects.kalipso]
        toml_after_first = tmp_projects_toml.read_text(encoding="utf-8")
        self.assertIn("[projects.kalipso]", toml_after_first)
        self.assertEqual(toml_after_first.count("[projects.kalipso]"), 1)

        # 5. Un second lancement du script ne duplique pas le bloc
        subprocess.run(
            ["bash", str(COCKPIT_ROOT / "scripts/new-project.sh"), "kalipso", str(proj_dir), "8805"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        toml_after_second = tmp_projects_toml.read_text(encoding="utf-8")
        self.assertEqual(toml_after_second.count("[projects.kalipso]"), 1)

        # 6. Un port déjà utilisé (sidecar de mon-app) fait échouer le script avec un code non nul sans modifier la copie
        res_collision = subprocess.run(
            ["bash", str(COCKPIT_ROOT / "scripts/new-project.sh"), "kalipso2", str(self.tmp_path / "projets/kalipso2"), "8790"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res_collision.returncode, 0)
        self.assertEqual(tmp_projects_toml.read_text(encoding="utf-8"), toml_after_second)

        # 7. Le fichier d'exemple config/projects.example.toml n'a pas changé
        self.assertEqual(real_projects_toml.read_text(encoding="utf-8"), real_toml_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
