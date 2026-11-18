"""Tests unitaires pour les sous-tickets (T-018) : parent, validation, délégation des verrous et file."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from core import pipeline, scheduler
from core.pmo_db import CockpitDB


class TestSubtickets(unittest.TestCase):
    """Vérifie le contrat de T-018 sur une base SQLite temporaire sans réseau."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db = CockpitDB(self.tmp_path / "test_pmo.db")
        self.project_id = "proj"
        self.db.register_project(
            self.project_id,
            "Projet Test",
            str(self.tmp_path / "repo"),
            str(self.tmp_path / "wt_sessions"),
            str(self.tmp_path / "wt_tickets"),
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-001",
            "Chantier parent",
            priority="P1",
            complexity="L",
            files_owned=["./core/a.py", "core/b.py", "tests/test_ab.py"],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_1_valid_subticket_creation_and_children(self) -> None:
        """1. Création valide d'un sous-ticket et récupération via children() et get_prioritized_queue()."""
        self.db.upsert_ticket(
            self.project_id,
            "T-003",
            "Sous-ticket B",
            files_owned=["core/b.py"],
            parent_id="T-001",
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Sous-ticket A",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        kids = self.db.children(self.project_id, "T-001")
        self.assertEqual([k["id"] for k in kids], ["T-002", "T-003"])
        self.assertTrue(all(k["parent_id"] == "T-001" for k in kids))

        queue = self.db.get_prioritized_queue(self.project_id)
        by_id = {item["id"]: item for item in queue}
        self.assertEqual(by_id["T-001"]["parent_id"], "")
        self.assertEqual(by_id["T-002"]["parent_id"], "T-001")

    def test_2_reject_file_outside_parent(self) -> None:
        """2. Refus (ValueError) si un fichier du sous-ticket n'appartient pas au parent."""
        with self.assertRaises(ValueError):
            self.db.upsert_ticket(
                self.project_id,
                "T-002",
                "Sous-ticket hors périmètre",
                files_owned=["core/a.py", "core/intrus.py"],
                parent_id="T-001",
            )

    def test_3_reject_grandchild_and_invalid_parents(self) -> None:
        """3. Refus d'un petit-enfant (un seul niveau de profondeur), parent inexistant ou MERGED."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Sous-ticket niveau 1",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        with self.assertRaises(ValueError):
            self.db.upsert_ticket(
                self.project_id,
                "T-003",
                "Petit-enfant interdit",
                files_owned=["core/a.py"],
                parent_id="T-002",
            )

        with self.assertRaises(ValueError):
            self.db.upsert_ticket(
                self.project_id,
                "T-004",
                "Parent fantôme",
                files_owned=["core/a.py"],
                parent_id="T-999",
            )

        self.db.update_ticket(self.project_id, "T-001", state="MERGED")
        with self.assertRaises(ValueError):
            self.db.upsert_ticket(
                self.project_id,
                "T-005",
                "Parent déjà fusionné",
                files_owned=["core/a.py"],
                parent_id="T-001",
            )

    def test_4_reject_beyond_max_children_and_cli_views(self) -> None:
        """4. Refus (PipelineError) au-delà de max_children via cockpit ticket add, et vues list/show."""
        fake_runners = {"defaults": {"max_children": 2}, "runners": {}}
        with patch.object(pipeline, "DB_PATH", self.db.db_path), patch.object(
            pipeline, "load_runners", return_value=fake_runners
        ):
            rc1 = pipeline.main(
                ["ticket", "add", self.project_id, "Enfant 1", "--id", "T-002", "--files", "core/a.py", "--parent", "T-001"]
            )
            rc2 = pipeline.main(
                ["ticket", "add", self.project_id, "Enfant 2", "--id", "T-003", "--files", "core/b.py", "--parent", "T-001"]
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)

            args_3 = pipeline. argparse.Namespace(
                action="add",
                project=self.project_id,
                title="Enfant 3 en trop",
                id="T-004",
                milestone="J1",
                prio="P2",
                size="S",
                files="tests/test_ab.py",
                readonly="",
                depends="",
                accept=None,
                contract="",
                contract_file="",
                test_cmd="",
                sensitive=False,
                parent="T-001",
            )
            with self.assertRaises(pipeline.PipelineError):
                pipeline.cmd_ticket(self.db, args_3)

            buf_list = io.StringIO()
            with redirect_stdout(buf_list):
                pipeline.main(["ticket", "list", self.project_id])
            list_lines = buf_list.getvalue().splitlines()
            self.assertTrue(any(line.startswith("T-001") for line in list_lines))
            self.assertTrue(any(line.startswith("  T-002") for line in list_lines))
            self.assertTrue(any(line.startswith("  T-003") for line in list_lines))

            buf_show = io.StringIO()
            with redirect_stdout(buf_show):
                pipeline.main(["ticket", "show", self.project_id, "--id", "T-002"])
            shown = json.loads(buf_show.getvalue())
            self.assertEqual(shown.get("parent_id"), "T-001")

    def test_5_acquire_locks_transfers_from_parent_to_child(self) -> None:
        """5. acquire_locks de l'enfant réussit quand le parent détient le verrou et le transfère."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Enfant A",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        ok_p, conflicts_p = self.db.acquire_locks(self.project_id, "T-001", ["core/a.py", "core/b.py"])
        self.assertTrue(ok_p)
        self.assertEqual(conflicts_p, [])

        ok_c, conflicts_c = self.db.acquire_locks(self.project_id, "T-002", ["core/a.py"])
        self.assertTrue(ok_c)
        self.assertEqual(conflicts_c, [])

        with self.db._connect() as conn:
            locks = {
                r["file_path"]: r["ticket_id"]
                for r in conn.execute(
                    "SELECT file_path, ticket_id FROM file_locks WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
        self.assertEqual(locks["core/a.py"], "T-002")
        self.assertEqual(locks["core/b.py"], "T-001")

    def test_6_conflict_between_two_brothers_on_same_file(self) -> None:
        """6. Conflit entre deux frères sur le même fichier."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Frère 1",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-003",
            "Frère 2",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        self.db.acquire_locks(self.project_id, "T-001", ["core/a.py"])
        ok1, _ = self.db.acquire_locks(self.project_id, "T-002", ["core/a.py"])
        self.assertTrue(ok1)

        ok2, conflicts2 = self.db.acquire_locks(self.project_id, "T-003", ["core/a.py"])
        self.assertFalse(ok2)
        self.assertEqual(len(conflicts2), 1)
        self.assertIn("T-002", conflicts2[0])

    def test_7_return_locks_to_parent(self) -> None:
        """7. return_locks_to_parent rend les verrous au parent (et est sans effet sans parent)."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Enfant A",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        self.db.acquire_locks(self.project_id, "T-001", ["core/a.py", "core/b.py"])
        self.db.acquire_locks(self.project_id, "T-002", ["core/a.py"])

        self.db.return_locks_to_parent(self.project_id, "T-002")
        with self.db._connect() as conn:
            locks = {
                r["file_path"]: r["ticket_id"]
                for r in conn.execute(
                    "SELECT file_path, ticket_id FROM file_locks WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
        self.assertEqual(locks["core/a.py"], "T-001")
        self.assertEqual(locks["core/b.py"], "T-001")

        # Sans effet pour un ticket sans parent : ses verrous restent en place
        self.db.return_locks_to_parent(self.project_id, "T-001")
        with self.db._connect() as conn:
            locks_after = {
                r["file_path"]: r["ticket_id"]
                for r in conn.execute(
                    "SELECT file_path, ticket_id FROM file_locks WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
        self.assertEqual(locks_after["core/a.py"], "T-001")

    def test_8_complete_and_merge_child_returns_locks_to_parent(self) -> None:
        """8. complete_and_merge_ticket d'un enfant rend ses verrous au parent au lieu de les supprimer."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Enfant A",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-003",
            "Enfant B dépendant de A",
            files_owned=["core/b.py"],
            depends_on=["T-002"],
            parent_id="T-001",
        )
        self.db.acquire_locks(self.project_id, "T-001", ["core/a.py", "core/b.py"])
        self.db.acquire_locks(self.project_id, "T-002", ["core/a.py"])

        unblocked = self.db.complete_and_merge_ticket(self.project_id, "T-002")
        self.assertEqual(unblocked, ["T-003"])
        t2 = self.db.get_ticket(self.project_id, "T-002")
        self.assertIsNotNone(t2)
        assert t2 is not None
        self.assertEqual(t2["state"], "MERGED")

        with self.db._connect() as conn:
            locks = {
                r["file_path"]: r["ticket_id"]
                for r in conn.execute(
                    "SELECT file_path, ticket_id FROM file_locks WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
        self.assertEqual(locks["core/a.py"], "T-001")
        self.assertEqual(locks["core/b.py"], "T-001")

    def test_9_plan_batch_parent_filtering_and_parent_locks(self) -> None:
        """9. plan_batch sans parent_id ignore les enfants ; avec parent_id ne retourne que les enfants exécutables."""
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Enfant A",
            complexity="S",
            files_owned=["core/a.py"],
            parent_id="T-001",
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-003",
            "Enfant B",
            complexity="S",
            files_owned=["core/b.py"],
            parent_id="T-001",
        )
        # Sans parent_id : seul T-001 (sans parent) est candidat
        batch_root = scheduler.plan_batch(self.db, self.project_id, slots=4)
        self.assertEqual([b["id"] for b in batch_root], ["T-001"])

        # Le parent prend son chantier (ASSIGNED + verrous)
        self.db.update_ticket(self.project_id, "T-001", state="ASSIGNED")
        self.db.acquire_locks(self.project_id, "T-001", ["core/a.py", "core/b.py", "tests/test_ab.py"])

        # Sans parent_id : T-001 est ASSIGNED et ses enfants sont ignorés -> []
        self.assertEqual(scheduler.plan_batch(self.db, self.project_id, slots=4), [])

        # Avec parent_id="T-001" : les verrous du parent ne bloquent pas ses enfants
        batch_children = scheduler.plan_batch(self.db, self.project_id, slots=4, parent_id="T-001")
        self.assertEqual([b["id"] for b in batch_children], ["T-002", "T-003"])


if __name__ == "__main__":
    unittest.main()
