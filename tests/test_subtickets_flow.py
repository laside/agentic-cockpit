#!/usr/bin/env python3
"""
Tests du flux de sous-tickets (T-019) :
- Branche de l'enfant créée depuis feat/<parent> (et refus si feat/<parent> n'existe pas)
- Base de comparaison git merge-base HEAD feat/<parent> dans run_ticket et submit_ticket
- Intégration d'un enfant dans le chantier parent via merge_ticket sans --approve
- Refus d'intégration si enfant escaladé (Porte 0/1 en échec) ou worktree parent sale
- Refus de fusion du parent tant qu'un enfant n'est pas MERGED
- Exécution et intégration via cockpit auto --parent --auto-merge
"""
from __future__ import annotations

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


class SubticketsFlowTests(unittest.TestCase):
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
            "demo",
            "Demo",
            str(self.root),
            str(self.wts),
            str(self.wts),
            milestones=[("C3", "Chantiers")],
        )
        self.script = base / "worker.sh"
        self.runner_cfg = {
            "defaults": {"timeout_s": 30, "max_attempts": 3, "gate_timeout_s": 60},
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

    def test_child_worktree_requires_parent_branch(self) -> None:
        """Un sous-ticket refuse de s'exécuter si la branche feat/<parent> n'existe pas."""
        self.db.upsert_ticket(
            "demo",
            "T-100",
            "Chantier parent",
            milestone_id="C3",
            complexity="L",
            files_owned=["a.py", "tests/test_a.py"],
        )
        self.db.upsert_ticket(
            "demo",
            "T-101",
            "Enfant A",
            milestone_id="C3",
            complexity="S",
            files_owned=["a.py", "tests/test_a.py"],
            parent_id="T-100",
        )
        self.worker("echo 'A = 1' > a.py")
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_ticket(self.db, "demo", "T-101", use_gate1=False)
        self.assertIn(
            "le chantier T-100 doit d'abord être pris (cockpit claim) ou exécuté",
            str(ctx.exception),
        )

    def test_full_subtickets_scenario(self) -> None:
        """Scénario complet : claim du parent, 2 enfants disjoints, merge dans le chantier, verrous et garde-fous."""
        parent_files = ["a.py", "b.py", "tests/test_a.py", "tests/test_b.py"]
        self.db.upsert_ticket(
            "demo",
            "T-200",
            "Chantier parent",
            milestone_id="C3",
            complexity="L",
            files_owned=parent_files,
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-200", "agy"), 0)

        # Ajout d'un commit initial sur la branche du chantier pour vérifier que les enfants en partent bien.
        parent_wt = self.wts / "T-200"
        (parent_wt / "a.py").write_text("# socle parent\n", encoding="utf-8")
        git(parent_wt, "add", "a.py")
        git(parent_wt, "commit", "-q", "-m", "chore(T-200): socle chantier")
        parent_head_before = git(parent_wt, "rev-parse", "HEAD").strip()

        self.db.upsert_ticket(
            "demo",
            "T-201",
            "Enfant A",
            milestone_id="C3",
            complexity="S",
            files_owned=["a.py", "tests/test_a.py"],
            parent_id="T-200",
        )
        self.db.upsert_ticket(
            "demo",
            "T-202",
            "Enfant B",
            milestone_id="C3",
            complexity="S",
            files_owned=["b.py", "tests/test_b.py"],
            parent_id="T-200",
        )

        # Exécution de l'enfant 1
        self.worker(
            "grep -q 'socle parent' a.py\n"
            "echo 'A = 10' >> a.py\n"
            "cat > tests/test_a.py << 'EOF'\n"
            "import unittest, a\n"
            "class TestA(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertEqual(a.A, 10)\n"
            "EOF"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-201", use_gate1=False), 0)
        child1_wt = self.wts / "T-201"
        self.assertEqual(
            git(child1_wt, "merge-base", "HEAD", "feat/T-200").strip(),
            parent_head_before,
        )

        # Exécution de l'enfant 2
        self.worker(
            "grep -q 'socle parent' a.py\n"
            "echo 'B = 20' > b.py\n"
            "cat > tests/test_b.py << 'EOF'\n"
            "import unittest, b\n"
            "class TestB(unittest.TestCase):\n"
            "    def test_b(self):\n"
            "        self.assertEqual(b.B, 20)\n"
            "EOF"
        )
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-202", use_gate1=False), 0)

        # Merge du parent refusé tant que les enfants ne sont pas MERGED
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.merge_ticket(self.db, "demo", "T-200", approve=True)
        self.assertIn("T-201", str(ctx.exception))
        self.assertIn("T-202", str(ctx.exception))

        # Worktree du parent sale -> intégration de l'enfant refusée
        (parent_wt / "a.py").write_text("# modification non commitée\n", encoding="utf-8")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-201")
        git(parent_wt, "checkout", "--", "a.py")

        # Merge de T-201 sans --approve (bien que GATE2, car gate0=PASS et gate1!=STATIC_FAIL)
        self.assertEqual(self.db.get_ticket("demo", "T-201")["state"], "GATE2")
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-201"), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-201")["state"], "MERGED")
        self.assertFalse(child1_wt.exists())

        # Merge du parent toujours refusé tant que T-202 n'est pas MERGED
        with self.assertRaises(pipeline.PipelineError) as ctx2:
            pipeline.merge_ticket(self.db, "demo", "T-200", approve=True)
        self.assertNotIn("T-201", str(ctx2.exception))
        self.assertIn("T-202", str(ctx2.exception))

        # Merge de T-202 sans --approve
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-202"), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-202")["state"], "MERGED")
        self.assertFalse((self.wts / "T-202").exists())

        # Les fichiers des deux enfants sont présents dans le worktree du parent, pas encore dans main
        self.assertIn("A = 10", (parent_wt / "a.py").read_text(encoding="utf-8"))
        self.assertTrue((parent_wt / "b.py").exists())
        self.assertTrue((parent_wt / "tests" / "test_a.py").exists())
        self.assertTrue((parent_wt / "tests" / "test_b.py").exists())
        self.assertFalse((self.root / "b.py").exists())
        self.assertFalse((self.root / "docs" / "ROADMAP.md").exists())

        # Les verrous sont revenus au parent T-200
        with self.db._connect() as conn:
            locks = {
                r["file_path"]: r["ticket_id"]
                for r in conn.execute("SELECT file_path, ticket_id FROM file_locks WHERE project_id = 'demo'").fetchall()
            }
        self.assertEqual(locks, {f: "T-200" for f in parent_files})

        # Les événements MERGED des enfants mentionnent « intégré dans T-200 »
        events = [(e["ticket_id"], e["kind"], e["detail"]) for e in self.db.recent_events("demo", 30)]
        self.assertIn(("T-201", "MERGED", "intégré dans T-200"), events)
        self.assertIn(("T-202", "MERGED", "intégré dans T-200"), events)

    def test_escalated_child_is_not_integrable(self) -> None:
        """Un enfant escaladé (Porte 0 refusée) ne peut pas être intégré dans le chantier."""
        self.db.upsert_ticket(
            "demo",
            "T-300",
            "Parent",
            milestone_id="C3",
            complexity="L",
            files_owned=["a.py"],
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-300", "agy"), 0)
        self.db.upsert_ticket(
            "demo",
            "T-301",
            "Enfant en échec",
            milestone_id="C3",
            complexity="S",
            files_owned=["a.py"],
            parent_id="T-300",
        )
        self.worker("echo 'pirate' > hors_perimetre.py")
        self.assertEqual(pipeline.run_ticket(self.db, "demo", "T-301", max_attempts=1, use_gate1=False), 1)
        self.assertEqual(self.db.get_ticket("demo", "T-301")["state"], "GATE2")
        last = self.db.last_run("demo", "T-301")
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last["gate0"], "FAIL")

        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-301")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.merge_ticket(self.db, "demo", "T-301", approve=True)

    def test_auto_with_and_without_parent(self) -> None:
        """cockpit auto sans --parent ignore les enfants ; avec --parent --auto-merge les exécute et les intègre."""
        self.db.upsert_ticket(
            "demo",
            "T-400",
            "Chantier auto",
            milestone_id="C3",
            complexity="L",
            files_owned=["a.py", "b.py"],
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-400", "agy"), 0)
        self.db.upsert_ticket(
            "demo",
            "T-401",
            "Enfant auto 1",
            milestone_id="C3",
            complexity="S",
            files_owned=["a.py"],
            parent_id="T-400",
        )
        self.db.upsert_ticket(
            "demo",
            "T-402",
            "Enfant auto 2",
            milestone_id="C3",
            complexity="S",
            files_owned=["b.py"],
            parent_id="T-400",
        )

        # Sans --parent : aucun enfant n'est lancé
        self.worker("echo 'X = 1' > a.py")
        self.assertEqual(pipeline.main(["auto", "demo", "--slots", "2", "--max-tickets", "4"]), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-401")["state"], "READY")
        self.assertEqual(self.db.get_ticket("demo", "T-402")["state"], "READY")

        # Avec --parent T-400 --auto-merge : les enfants sont exécutés et intégrés dans le chantier T-400
        self.worker(
            "case \"$PWD\" in *T-401) echo 'A = 1' > a.py ;; *) echo 'B = 2' > b.py ;; esac"
        )
        self.assertEqual(
            pipeline.main(["auto", "demo", "--parent", "T-400", "--auto-merge", "--slots", "2", "--max-tickets", "4"]),
            0,
        )
        self.assertEqual(self.db.get_ticket("demo", "T-401")["state"], "MERGED")
        self.assertEqual(self.db.get_ticket("demo", "T-402")["state"], "MERGED")
        parent_wt = self.wts / "T-400"
        self.assertTrue((parent_wt / "a.py").exists())
        self.assertTrue((parent_wt / "b.py").exists())

    def test_submit_child_uses_parent_branch_as_base(self) -> None:
        """cockpit submit sur un sous-ticket interactif compare le périmètre par rapport à feat/<parent>."""
        self.db.upsert_ticket(
            "demo",
            "T-500",
            "Chantier",
            milestone_id="C3",
            complexity="L",
            files_owned=["a.py", "b.py"],
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-500", "agy"), 0)
        parent_wt = self.wts / "T-500"
        # Le parent modifie et commite a.py dans sa branche feat/T-500
        (parent_wt / "a.py").write_text("A = 1\n", encoding="utf-8")
        git(parent_wt, "add", "a.py")
        git(parent_wt, "commit", "-q", "-m", "feat(T-500): init a.py")

        # Un sous-ticket qui ne possède que b.py ne doit pas se voir reprocher a.py commité sur feat/T-500
        self.db.upsert_ticket(
            "demo",
            "T-501",
            "Enfant interactif B",
            milestone_id="C3",
            complexity="S",
            files_owned=["b.py"],
            parent_id="T-500",
        )
        self.assertEqual(pipeline.claim_ticket(self.db, "demo", "T-501", "claude"), 0)
        child_wt = self.wts / "T-501"
        (child_wt / "b.py").write_text("B = 2\n", encoding="utf-8")
        self.assertEqual(pipeline.submit_ticket(self.db, "demo", "T-501"), 0)
        self.assertEqual(pipeline.merge_ticket(self.db, "demo", "T-501"), 0)
        self.assertEqual(self.db.get_ticket("demo", "T-501")["state"], "MERGED")
        self.assertTrue((parent_wt / "b.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
