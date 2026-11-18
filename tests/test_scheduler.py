"""Tests unitaires pour le module core.scheduler (TDD T-004)."""

from pathlib import Path
import tempfile
import unittest

from core.pmo_db import CockpitDB
from core.scheduler import plan_batch, should_stop


class TestScheduler(unittest.TestCase):
    """Vérifie la planification sans conflit de fichiers et les conditions d'arrêt."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "x.db"
        self.db = CockpitDB(self.db_path)
        self.project_id = "proj_test"
        self.db.register_project(
            proj_id=self.project_id,
            name="Projet Test",
            root_path=self.tmpdir.name,
            wt_sessions_dir=self.tmpdir.name,
            wt_tickets_dir=self.tmpdir.name,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_queue_order_respected_and_slots(self) -> None:
        """L'ordre de la file priorisée est respecté dans le lot planifié."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            self.project_id,
            "T-001",
            "Ticket basse priorité",
            priority="P3",
            complexity="S",
            state="READY",
            files_owned=["a.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Ticket haute priorité",
            priority="P0",
            complexity="S",
            state="READY",
            files_owned=["b.py"],
        )
        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual([item["id"] for item in batch], ["T-002", "T-001"])
        self.assertEqual(batch[0]["files"], ["b.py"])
        self.assertIn("lane", batch[0])

    def test_ticket_ignored_if_file_locked_by_other_ticket(self) -> None:
        """Un ticket dont un fichier est verrouillé par un autre ticket est ignoré."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            self.project_id,
            "T-LOCK",
            "Ticket verrouillant",
            priority="P0",
            complexity="S",
            state="IN_PROGRESS",
            files_owned=["shared.py"],
        )
        ok, _ = self.db.acquire_locks(self.project_id, "T-LOCK", ["shared.py"])
        self.assertTrue(ok)

        self.db.upsert_ticket(
            self.project_id,
            "T-001",
            "Ticket en conflit avec verrou",
            priority="P1",
            complexity="S",
            state="READY",
            files_owned=["shared.py", "other.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Ticket libre",
            priority="P2",
            complexity="S",
            state="READY",
            files_owned=["free.py"],
        )

        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual([item["id"] for item in batch], ["T-002"])

    def test_two_tickets_same_file_never_in_same_batch(self) -> None:
        """Deux tickets partageant un même fichier ne sont jamais dans le même lot."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            self.project_id,
            "T-001",
            "Premier sur common.py",
            priority="P0",
            complexity="S",
            state="READY",
            files_owned=["common.py", "one.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Second sur common.py",
            priority="P1",
            complexity="S",
            state="READY",
            files_owned=["common.py", "two.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-003",
            "Troisième indépendant",
            priority="P2",
            complexity="S",
            state="READY",
            files_owned=["three.py"],
        )

        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual([item["id"] for item in batch], ["T-001", "T-003"])

    def test_only_one_agy_ticket_per_batch(self) -> None:
        """Au plus un ticket de voie agy_* est retenu dans un même lot."""
        self.db.set_mistral_key_active(False)
        self.db.upsert_ticket(
            self.project_id,
            "T-AGY1",
            "Premier ticket agy",
            priority="P0",
            complexity="L",
            state="READY",
            files_owned=["f1.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-AGY2",
            "Second ticket agy",
            priority="P1",
            complexity="L",
            state="READY",
            files_owned=["f2.py"],
        )

        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["id"], "T-AGY1")
        self.assertTrue(batch[0]["lane"].startswith("agy_"))

    def test_blocked_and_empty_files_owned_ignored(self) -> None:
        """Les tickets BLOCKED et les tickets sans files_owned sont ignorés."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            self.project_id,
            "T-BLOCKED",
            "Ticket bloqué",
            priority="P0",
            complexity="S",
            state="BLOCKED",
            files_owned=["blocked.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-NOFILES",
            "Ticket sans fichiers",
            priority="P0",
            complexity="S",
            state="READY",
            files_owned=[],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-VALID",
            "Ticket valide",
            priority="P2",
            complexity="S",
            state="READY",
            files_owned=["valid.py"],
        )

        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual([item["id"] for item in batch], ["T-VALID"])

    def test_should_stop_three_cases(self) -> None:
        """Vérifie should_stop pour max_tickets, escalades et None."""
        self.assertEqual(should_stop(done=6, escalations_in_a_row=0, max_tickets=6, max_escalations=2), "max_tickets")
        self.assertEqual(should_stop(done=8, escalations_in_a_row=2, max_tickets=6, max_escalations=2), "max_tickets")
        self.assertEqual(should_stop(done=2, escalations_in_a_row=2, max_tickets=6, max_escalations=2), "escalades")
        self.assertEqual(should_stop(done=2, escalations_in_a_row=3, max_tickets=6, max_escalations=2), "escalades")
        self.assertIsNone(should_stop(done=5, escalations_in_a_row=1, max_tickets=6, max_escalations=2))

    def test_normalized_relative_paths_conflict_in_batch_and_locks(self) -> None:
        """(d) « ./shared.py » et « shared.py » ne sont jamais dans le même lot."""
        self.db.set_mistral_key_active(True)
        self.db.upsert_ticket(
            self.project_id,
            "T-001",
            "Ticket avec ./shared.py",
            priority="P0",
            complexity="S",
            state="READY",
            files_owned=["./shared.py"],
        )
        self.db.upsert_ticket(
            self.project_id,
            "T-002",
            "Ticket avec shared.py",
            priority="P1",
            complexity="S",
            state="READY",
            files_owned=["shared.py"],
        )
        batch = plan_batch(self.db, self.project_id, slots=2)
        self.assertEqual([item["id"] for item in batch], ["T-001"])


if __name__ == "__main__":
    unittest.main()
