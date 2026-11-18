#!/usr/bin/env python3
"""
Tests du rapport `cockpit costs` (core/costs_report.py).
Base SQLite temporaire via pmo_db, journaux JSONL temporaires : aucun accès réseau,
aucun accès à ~/.claude, aucun accès à la base réelle.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT))

from core.costs_report import build_report  # noqa: E402
from core.pmo_db import CockpitDB  # noqa: E402


def _ajoute_colonnes_runs(db_path: Path) -> None:
    # Idempotent : selon la version de pmo_db, ces colonnes existent déjà (migration additive de la table runs).
    with sqlite3.connect(str(db_path)) as conn:
        existantes = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for colonne, ddl in (("tokens_cache", "INTEGER DEFAULT 0"), ("cost_usd", "REAL DEFAULT 0.0"),
                             ("cost_kind", "TEXT DEFAULT ''")):
            if colonne not in existantes:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {colonne} {ddl}")


def _insere_run(db_path: Path, lane: str, tokens_in: int, tokens_out: int, cache: int = 0,
                cost_eur: float = 0.0, cost_usd: float = 0.0, cost_kind: str = "") -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO runs (project_id, ticket_id, attempt, lane, tokens_in, tokens_out, "
            "tokens_cache, cost_eur, cost_usd, cost_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("demo", "T-1", 1, lane, tokens_in, tokens_out, cache, cost_eur, cost_usd, cost_kind),
        )


def _ecrit_journal(path: Path, lignes: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lignes) + "\n", encoding="utf-8")
    return path


def _message(message_id: str, timestamp: str, output: int, cache_read: int = 0,
             input_tokens: int = 0, cache_creation: int = 0,
             model: str = "claude-opus-4") -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


class CostsReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "cockpit.db"
        self.db = CockpitDB(self.db_path)
        self.db.register_project("demo", "Demo", str(self.root), str(self.root / "sessions"), str(self.root / "tickets"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rapport(self, **kwargs) -> str:
        kwargs.setdefault("transcripts", [])
        return build_report("demo", self.db_path, **kwargs)

    def test_workers_trois_types_de_couts_separes(self) -> None:
        _ajoute_colonnes_runs(self.db_path)
        _insere_run(self.db_path, "agy_standard", 100, 50, cache=10, cost_eur=0.5, cost_kind="measured")
        _insere_run(self.db_path, "deepseek_coder", 10, 5, cost_usd=0.25, cost_eur=9.99,
                    cost_kind="api_equivalent")
        _insere_run(self.db_path, "local_nas_coder", 1, 2, cost_kind="tokens_only")
        rapport = self._rapport()
        self.assertIn("WORKERS", rapport)
        self.assertIn("0,50 € mesuré", rapport)
        self.assertIn("0,25 $ équivalent API (abonnement)", rapport)
        self.assertIn("tokens seulement", rapport)
        self.assertIn("TOTAL workers : entrée 111 · sortie 57 · cache 10 · entrée+sortie 168", rapport)
        self.assertIn("TOTAL coût mesuré : 0,50 €", rapport)
        self.assertNotIn("10,49", rapport)
        self.assertLessEqual(len(rapport.splitlines()), 60)

    def test_base_sans_colonnes_optionnelles(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DROP TABLE runs")
            conn.execute(
                """
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    lane TEXT NOT NULL,
                    provider TEXT DEFAULT '',
                    exit_code INTEGER,
                    duration_s REAL DEFAULT 0.0,
                    gate0 TEXT DEFAULT '',
                    gate1 TEXT DEFAULT '',
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    cost_eur REAL DEFAULT 0.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO runs (project_id, ticket_id, attempt, lane, tokens_in, tokens_out) "
                "VALUES ('demo', 'T-1', 1, 'agy_standard', 7, 3)"
            )
        rapport = self._rapport()
        self.assertIn("WORKERS", rapport)
        self.assertIn("TOTAL workers : entrée 7 · sortie 3 · cache 0 · entrée+sortie 10", rapport)

    def test_doublons_journal_comptes_une_fois(self) -> None:
        journal = _ecrit_journal(
            self.root / "session.jsonl",
            [
                _message("msg-1", "2026-03-01T10:00:00Z", output=100, input_tokens=10),
                _message("msg-1", "2026-03-01T10:00:00Z", output=100, input_tokens=10),
                _message("msg-1", "2026-03-01T10:00:00Z", output=80, input_tokens=10),
            ],
        )
        rapport = build_report("demo", self.db_path, transcripts=[journal])
        self.assertIn("1 message", rapport)
        self.assertIn("sortie 100", rapport)
        self.assertNotIn("sortie 280", rapport)

    def test_filtre_since(self) -> None:
        journal = _ecrit_journal(
            self.root / "session.jsonl",
            [
                _message("avant", "2026-01-01T10:00:00Z", output=10),
                _message("apres", "2026-02-02T10:00:00Z", output=20),
            ],
        )
        rapport = build_report("demo", self.db_path, transcripts=[journal], since="2026-02-01")
        self.assertIn("1 message", rapport)
        self.assertIn("sortie 20", rapport)
        self.assertNotIn("sortie 30", rapport)

    def _deux_tickets_fusionnes(self) -> None:
        self.db.upsert_ticket("demo", "T-1", "Premier ticket de test", state="MERGED")
        self.db.upsert_ticket("demo", "T-2", "Second ticket de test", state="MERGED")
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO events (project_id, ticket_id, kind, created_at) VALUES "
                "('demo', 'T-1', 'CLAIMED', '2026-03-01 10:00:00'),"
                "('demo', 'T-1', 'MERGED', '2026-03-01 12:00:00'),"
                "('demo', 'T-2', 'CLAIMED', '2026-03-01 11:00:00'),"
                "('demo', 'T-2', 'MERGED', '2026-03-01 13:00:00')"
            )

    def test_repartition_parts_egales_si_chevauchement(self) -> None:
        self._deux_tickets_fusionnes()
        journal = _ecrit_journal(
            self.root / "session.jsonl",
            [_message("msg-1", "2026-03-01T11:30:00Z", output=100, cache_read=50)],
        )
        rapport = build_report("demo", self.db_path, transcripts=[journal])
        lignes = [ln for ln in rapport.splitlines() if ln.startswith("T-")]
        self.assertEqual(len(lignes), 2)
        for ligne in lignes:
            self.assertIn("sortie 50", ligne)
            self.assertIn("cache relu 25", ligne)

    def test_message_hors_fenetre_dans_hors_ticket(self) -> None:
        self._deux_tickets_fusionnes()
        journal = _ecrit_journal(
            self.root / "session.jsonl",
            [_message("msg-1", "2026-03-01T14:00:00Z", output=100, cache_read=50)],
        )
        rapport = build_report("demo", self.db_path, transcripts=[journal])
        self.assertIn("hors ticket (pilotage, discussion) : sortie 100 · cache relu 50", rapport)

    def test_moyenne_par_ticket_fusionne(self) -> None:
        self._deux_tickets_fusionnes()
        journal = _ecrit_journal(
            self.root / "session.jsonl",
            [_message("msg-1", "2026-03-01T10:30:00Z", output=100)],
        )
        rapport = build_report("demo", self.db_path, transcripts=[journal])
        self.assertIn("MOYENNE tokens de sortie de l'architecte par ticket fusionné : 50", rapport)

    def test_aucun_transcript_sans_erreur(self) -> None:
        rapport = self._rapport()
        self.assertIn("ARCHITECTE : aucun journal de session trouvé", rapport)
        self.assertLessEqual(len(rapport.splitlines()), 60)


if __name__ == "__main__":
    unittest.main()
