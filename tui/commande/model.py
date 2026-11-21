#!/usr/bin/env python3
"""Modèle de l'onglet Commande : lecture seule de la base du cockpit (sans Textual)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

_CORE = Path(__file__).resolve().parents[2] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import config_paths  # noqa: E402
from pmo_db import parse_toml_text  # noqa: E402

FILTRES: dict[str, tuple[str, ...] | None] = {
    "Tous": None,
    "À faire": ("DRAFT", "READY", "BLOCKED"),
    "En cours": ("ASSIGNED", "GATE0", "GATE1"),
    "À relire": ("GATE2",),
    "PR": ("PR_OPEN",),
    "Terminés": ("MERGED", "ABANDONED"),
}
ETATS_TERMINES = ("MERGED", "ABANDONED")
CONTRAT_MAX = 1500


def cockpit_home() -> Path:
    raw = os.environ.get("COCKPIT_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "agentic-cockpit"


def db_path(home: Path | None = None) -> Path:
    return (home or cockpit_home()) / "db" / "cockpit_pmo.db"


def _load_toml(name: str, home: Path | None) -> dict[str, Any]:
    try:
        path = config_paths.config_file(name, home)
        return parse_toml_text(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - fichier absent ou invalide : configuration vide
        return {}


def projets(home: Path | None = None) -> dict[str, dict[str, Any]]:
    """Projets déclarés dans projects.toml (résolution de configuration du cockpit)."""
    return dict(_load_toml("projects.toml", home).get("projects", {}))


def voies(home: Path | None = None) -> list[str]:
    """Voies disponibles : clés de [runners] de runners.toml."""
    return list(_load_toml("runners.toml", home).get("runners", {}).keys())


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


class CommandeModel:
    """Accès en lecture seule à la base (sqlite mode=ro + PRAGMA query_only)."""

    def __init__(self, path: Path | None = None, home: Path | None = None) -> None:
        self.home = Path(home).expanduser() if home else cockpit_home()
        self.path = Path(path) if path else db_path(self.home)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def projets(self) -> dict[str, dict[str, Any]]:
        return projets(self.home)

    def voies(self) -> list[str]:
        return voies(self.home)

    def list_tickets(self, projet: str, filtre: str = "Tous") -> list[dict[str, Any]]:
        if filtre not in FILTRES:
            raise ValueError(f"Filtre inconnu : {filtre}")
        etats = FILTRES[filtre]
        rows = [dict(r) for r in self._query("SELECT * FROM tickets WHERE project_id = ?", (projet,))]
        if etats is not None:
            rows = [r for r in rows if r["state"] in etats]
        # états actifs d'abord, puis priorité, puis id
        rows.sort(key=lambda r: (r["state"] in ETATS_TERMINES, r.get("priority") or "P9", r["id"]))
        return rows

    def ticket_detail(self, projet: str, ticket_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM tickets WHERE project_id = ? AND id = ?", (projet, ticket_id))
        if not rows:
            return None
        t = dict(rows[0])
        runs = self._query(
            "SELECT * FROM runs WHERE project_id = ? AND ticket_id = ? ORDER BY id DESC LIMIT 10",
            (projet, ticket_id),
        )
        events = self._query(
            "SELECT * FROM events WHERE project_id = ? AND ticket_id = ? ORDER BY id DESC LIMIT 15",
            (projet, ticket_id),
        )
        return {
            "id": t["id"],
            "titre": t["title"],
            "etat": t["state"],
            "priorite": t["priority"],
            "complexite": t["complexity"],
            "voie": t.get("lane") or "",
            "sensible": bool(t["touches_sensitive"]),
            "fichiers": _json_list(t["files_owned_json"]),
            "criteres": _json_list(t["acceptance_json"]),
            "contrat": (t.get("contract") or "")[:CONTRAT_MAX],
            "dependances": _json_list(t["depends_on_json"]),
            "runs": [dict(r) for r in runs],
            "evenements": [dict(r) for r in events],
        }

    def worktree_of(self, projet: str, ticket_id: str) -> str | None:
        """Chemin du worktree du ticket d'après `git worktree list` (jamais le dossier de projects.toml)."""
        racine = self.projets().get(projet, {}).get("root")
        if not racine:
            rows = self._query("SELECT root_path FROM projects WHERE id = ?", (projet,))
            racine = rows[0]["root_path"] if rows else None
        if not racine:
            return None
        try:
            res = subprocess.run(
                ["git", "-C", str(Path(racine).expanduser()), "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        for ligne in res.stdout.splitlines():
            if ligne.startswith("worktree "):
                chemin = ligne[len("worktree "):]
                if chemin.endswith("/" + ticket_id):
                    return chemin
        return None
