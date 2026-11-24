#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Moteur PMO SQLite WAL, Gouverneur de Quotas & Mitigations T-104.
Compatible Python 3.10, 3.11 et 3.12+ (bibliothèque standard uniquement, 0 dépendance externe).
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore[assignment]

FORBIDDEN_SECRET_PATTERNS = (
    r"(^|/)\.env($|\.)",
    r"(^|/)secrets(/|$)",
    r"(^|/)exclusion\.key$",
    r"(^|/)id_rsa",
    r"\.pem$",
)


def parse_toml_text(text: str) -> dict[str, Any]:
    """Parseur TOML compatible Python 3.10+ (utilise tomllib si dispo, sinon parseur stdlib intégré)."""
    if tomllib is not None:
        return tomllib.loads(text)
    root: dict[str, Any] = {}
    cur = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].split("#", 1)[0].strip()
        i += 1
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            sec = raw[1:-1].strip()
            cur = root
            for part in sec.split("."):
                cur = cur.setdefault(part, {})
            continue
        if "=" in raw:
            k, v = [x.strip() for x in raw.split("=", 1)]
            while v.startswith("[") and not v.endswith("]") and i < len(lines):
                v += " " + lines[i].split("#", 1)[0].strip()
                i += 1
            try:
                cur[k] = ast.literal_eval(v)
            except Exception:
                cur[k] = v.strip('"').strip("'")
    return root


def normalize_rel_path(path: str) -> str:
    """Normalise un chemin relatif : strip, « \\ » remplacé par « / », préfixe « ./ » retiré."""
    return path.strip().replace("\\", "/").removeprefix("./")


def is_secret_or_forbidden_path(rel_path: str) -> bool:
    """Mitigation #1 (T-104) : Bloque strictement .env (sauf .env.example), secrets/, exclusion.key, chemins absolus et '..'."""
    raw = rel_path.strip().replace("\\", "/")
    if raw.startswith("/"):
        return True
    norm = normalize_rel_path(raw)
    if ".." in norm.split("/"):
        return True
    if norm.endswith(".env.example"):
        return False
    for pat in FORBIDDEN_SECRET_PATTERNS:
        if re.search(pat, norm):
            return True
    return False


def validate_projects_ports(projects_toml_path: Path) -> list[str]:
    """Mitigation #3 (T-104) : Détecte toute collision de ports entre projets dans projects.toml."""
    data = parse_toml_text(projects_toml_path.read_text(encoding="utf-8"))
    projects = data.get("projects", {})
    seen_ports: dict[int, str] = {}
    collisions: list[str] = []
    shared_keys = {"litellm", "backend_dev"}
    for proj_id, cfg in projects.items():
        ports = cfg.get("ports", {})
        for port_name, port_val in ports.items():
            if port_name in shared_keys:
                continue
            p = int(port_val)
            owner = f"{proj_id}.{port_name}"
            if p in seen_ports:
                collisions.append(f"Collision port {p} entre {seen_ports[p]} et {owner}")
            else:
                seen_ports[p] = owner
    return collisions


class CockpitDB:
    """Base transactionnelle SQLite WAL pour la gestion multi-projets, Roadmap, DAG et Quotas."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    wt_sessions_dir TEXT NOT NULL,
                    wt_tickets_dir TEXT NOT NULL,
                    default_branch TEXT DEFAULT 'main',
                    mirror_dir TEXT DEFAULT '',
                    sidecar_port INTEGER DEFAULT 8765,
                    active_milestone TEXT DEFAULT 'J1'
                );

                CREATE TABLE IF NOT EXISTS milestones (
                    project_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    rank INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'ACTIVE',
                    PRIMARY KEY (project_id, id)
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    project_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    milestone_id TEXT,
                    title TEXT NOT NULL,
                    priority TEXT DEFAULT 'P2',
                    complexity TEXT DEFAULT 'M',
                    state TEXT DEFAULT 'READY',
                    owner TEXT DEFAULT 'auto',
                    files_owned_json TEXT DEFAULT '[]',
                    acceptance_json TEXT DEFAULT '[]',
                    depends_on_json TEXT DEFAULT '[]',
                    fail_count INTEGER DEFAULT 0,
                    touches_sensitive INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, id)
                );

                CREATE TABLE IF NOT EXISTS file_locks (
                    project_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    locked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, file_path)
                );

                CREATE TABLE IF NOT EXISTS quota_state (
                    provider TEXT PRIMARY KEY,
                    window_used_pct REAL DEFAULT 0.0,
                    monthly_spent_eur REAL DEFAULT 0.0,
                    monthly_limit_eur REAL DEFAULT 0.0,
                    daily_spent_eur REAL DEFAULT 0.0,
                    daily_target_eur REAL DEFAULT 0.0,
                    api_key_configured INTEGER DEFAULT 1,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    ticket_id TEXT,
                    kind TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS runs (
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
                    tokens_cache INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    cost_kind TEXT DEFAULT '',
                    cost_eur REAL DEFAULT 0.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            # Migrations additives (idempotentes) : colonnes ajoutées après la V2 initiale.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()}
            for col, ddl in (
                ("readonly_json", "TEXT DEFAULT '[]'"),
                ("contract", "TEXT DEFAULT ''"),
                ("test_cmd", "TEXT DEFAULT ''"),
                ("lane", "TEXT DEFAULT ''"),
                ("claimed_pid", "INTEGER"),
                ("parent_id", "TEXT DEFAULT ''"),
            ):
                if col not in existing:
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
            existing_runs = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
            for col, ddl in (
                ("tokens_cache", "INTEGER DEFAULT 0"),
                ("cost_usd", "REAL DEFAULT 0.0"),
                ("cost_kind", "TEXT DEFAULT ''"),
            ):
                if col not in existing_runs:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
            defaults = [
                ("claude", 0.0, 0.0, 0.0, 0.0, 0.0, 1),
                ("agy", 0.0, 0.0, 0.0, 0.0, 0.0, 1),
                ("deepseek", 0.0, 0.0, 20.0, 0.0, 0.65, 1),
                ("mistral", 0.0, 0.0, 0.0, 0.0, 0.0, 0),
            ]
            for prov, w_pct, m_spent, m_lim, d_spent, d_targ, key_ok in defaults:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO quota_state
                    (provider, window_used_pct, monthly_spent_eur, monthly_limit_eur, daily_spent_eur, daily_target_eur, api_key_configured)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (prov, w_pct, m_spent, m_lim, d_spent, d_targ, key_ok),
                )

    def register_project(
        self,
        proj_id: str,
        name: str,
        root_path: str,
        wt_sessions_dir: str,
        wt_tickets_dir: str,
        default_branch: str = "main",
        mirror_dir: str = "",
        sidecar_port: int = 8765,
        milestones: list[tuple[str, str]] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO projects
                (id, name, root_path, wt_sessions_dir, wt_tickets_dir, default_branch, mirror_dir, sidecar_port, active_milestone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proj_id,
                    name,
                    root_path,
                    wt_sessions_dir,
                    wt_tickets_dir,
                    default_branch,
                    mirror_dir,
                    sidecar_port,
                    milestones[0][0] if milestones else "M1",
                ),
            )
            if milestones:
                for idx, (m_id, m_title) in enumerate(milestones, start=1):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO milestones (project_id, id, title, rank, status)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (proj_id, m_id, m_title, idx, "ACTIVE" if idx <= 2 else "PLANNED"),
                    )

    def upsert_ticket(
        self,
        project_id: str,
        ticket_id: str,
        title: str,
        milestone_id: str | None = "J1",
        priority: str = "P2",
        complexity: str = "M",
        state: str = "READY",
        owner: str = "auto",
        files_owned: list[str] | None = None,
        acceptance: list[str] | None = None,
        depends_on: list[str] | None = None,
        touches_sensitive: bool = False,
        fail_count: int = 0,
        files_readonly: list[str] | None = None,
        contract: str = "",
        test_cmd: str = "",
        parent_id: str = "",
    ) -> None:
        files_owned = files_owned or []
        for f in files_owned:
            if is_secret_or_forbidden_path(f):
                raise ValueError(f"Sécurité R7 : fichier interdit dans files_owned ({f})")

        parent_id = (parent_id or "").strip()
        depends_on = depends_on or []
        with self._connect() as conn:
            if parent_id:
                parent_row = conn.execute(
                    "SELECT * FROM tickets WHERE project_id = ? AND id = ?",
                    (project_id, parent_id),
                ).fetchone()
                if not parent_row:
                    raise ValueError(f"Le ticket parent {parent_id} n'existe pas dans le projet {project_id}.")
                if parent_row["parent_id"]:
                    raise ValueError(
                        f"Sous-ticket interdit sur {parent_id} : le parent a déjà un parent ({parent_row['parent_id']})."
                    )
                if parent_row["state"] in ("MERGED", "ABANDONED"):
                    raise ValueError(f"Le ticket parent {parent_id} est déjà {parent_row['state']}.")
                parent_files = {
                    normalize_rel_path(pf)
                    for pf in json.loads(parent_row["files_owned_json"] or "[]")
                    if normalize_rel_path(pf)
                }
                for f in files_owned:
                    norm_f = normalize_rel_path(f)
                    if norm_f not in parent_files:
                        raise ValueError(
                            f"Le fichier {norm_f} n'appartient pas aux files_owned du parent {parent_id}."
                        )

            if depends_on and state == "READY":
                placeholders = ",".join("?" for _ in depends_on)
                rows = conn.execute(
                    f"SELECT id, state FROM tickets WHERE project_id = ? AND id IN ({placeholders})",
                    [project_id, *depends_on],
                ).fetchall()
                merged_ids = {r["id"] for r in rows if r["state"] == "MERGED"}
                if not all(dep in merged_ids for dep in depends_on):
                    state = "BLOCKED"

            conn.execute(
                """
                INSERT OR REPLACE INTO tickets
                (project_id, id, milestone_id, title, priority, complexity, state, owner,
                 files_owned_json, acceptance_json, depends_on_json, fail_count, touches_sensitive, updated_at,
                 readonly_json, contract, test_cmd, parent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    ticket_id,
                    milestone_id,
                    title,
                    priority,
                    complexity,
                    state,
                    owner,
                    json.dumps(files_owned, ensure_ascii=False),
                    json.dumps(acceptance or [], ensure_ascii=False),
                    json.dumps(depends_on, ensure_ascii=False),
                    fail_count,
                    1 if touches_sensitive else 0,
                    datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    json.dumps(files_readonly or [], ensure_ascii=False),
                    contract,
                    test_cmd,
                    parent_id,
                ),
            )

    def children(self, project_id: str, parent_id: str) -> list[dict[str, Any]]:
        """Retourne les tickets enfants d'un parent dans le projet, triés par id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE project_id = ? AND parent_id = ? ORDER BY id",
                (project_id, parent_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_ticket(self, project_id: str, ticket_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE project_id = ? AND id = ?", (project_id, ticket_id)
            ).fetchone()
        return dict(row) if row else None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def update_ticket(self, project_id: str, ticket_id: str, **fields: Any) -> None:
        """Met à jour des colonnes simples d'un ticket (state, owner, lane, fail_count...)."""
        allowed = {"state", "owner", "lane", "fail_count", "priority", "complexity", "contract", "test_cmd"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Colonnes non modifiables : {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tickets SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND id = ?",
                [*fields.values(), project_id, ticket_id],
            )

    def release_locks(self, project_id: str, ticket_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM file_locks WHERE project_id = ? AND ticket_id = ?", (project_id, ticket_id)
            )

    def claim_ticket(self, project_id: str, ticket_id: str, pid: int) -> tuple[bool, str]:
        """Prend en charge un ticket de manière atomique (BEGIN IMMEDIATE)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, owner, claimed_pid FROM tickets WHERE project_id = ? AND id = ?",
                (project_id, ticket_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return False, "état inconnu"
            state = row["state"]
            if state == "READY":
                conn.execute(
                    "UPDATE tickets SET state = 'ASSIGNED', claimed_pid = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE project_id = ? AND id = ?",
                    (pid, project_id, ticket_id),
                )
                conn.commit()
                return True, ""
            if state in ("ASSIGNED", "GATE0", "GATE1"):
                claimed_pid = row["claimed_pid"]
                if claimed_pid is None:
                    conn.rollback()
                    return False, f"pris en mode interactif par {row['owner']}"
                try:
                    os.kill(int(claimed_pid), 0)
                except ProcessLookupError:
                    conn.execute(
                        "UPDATE tickets SET claimed_pid = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE project_id = ? AND id = ?",
                        (pid, project_id, ticket_id),
                    )
                    conn.commit()
                    return True, ""
                except PermissionError:
                    pass
                conn.rollback()
                return False, f"déjà en cours d'exécution (pid {claimed_pid})"
            conn.rollback()
            return False, f"état {state}"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def unclaim_ticket(self, project_id: str, ticket_id: str) -> None:
        """Relâche claimed_pid à NULL sans modifier l'état du ticket."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tickets SET claimed_pid = NULL, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND id = ?",
                (project_id, ticket_id),
            )

    def add_event(self, project_id: str, ticket_id: str | None, kind: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (project_id, ticket_id, kind, detail) VALUES (?, ?, ?, ?)",
                (project_id, ticket_id, kind, detail[:2000]),
            )

    def recent_events(self, project_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_runs_by_lane(self, provider: str, hours: float = 5.0) -> dict[str, int]:
        """Nombre de runs par voie pour ce provider sur les `hours` dernières heures."""
        window = f"-{float(hours)} hours"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT lane, COUNT(*) AS n FROM runs WHERE provider = ? "
                "AND created_at >= datetime('now', ?) GROUP BY lane",
                (provider, window),
            ).fetchall()
        return {r["lane"]: int(r["n"]) for r in rows}

    def record_run(self, project_id: str, ticket_id: str, **fields: Any) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO runs (project_id, ticket_id, {cols}) VALUES (?, ?, {marks})",
                [project_id, ticket_id, *fields.values()],
            )

    def last_run(self, project_id: str, ticket_id: str) -> dict[str, Any] | None:
        """Retourne le dernier run enregistré pour ce ticket, ou None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? AND ticket_id = ? ORDER BY id DESC LIMIT 1",
                (project_id, ticket_id),
            ).fetchone()
        return dict(row) if row else None

    def acquire_locks(self, project_id: str, ticket_id: str, files: list[str]) -> tuple[bool, list[str]]:
        norm_files = [normalize_rel_path(f) for f in files]
        conflicts: list[str] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            t_row = conn.execute(
                "SELECT parent_id FROM tickets WHERE project_id = ? AND id = ?",
                (project_id, ticket_id),
            ).fetchone()
            parent_id = (t_row["parent_id"] or "") if t_row else ""
            for f in norm_files:
                row = conn.execute(
                    "SELECT ticket_id FROM file_locks WHERE project_id = ? AND file_path = ?",
                    (project_id, f),
                ).fetchone()
                if row and row["ticket_id"] != ticket_id:
                    if parent_id and row["ticket_id"] == parent_id:
                        continue
                    conflicts.append(f"{f} (verrouillé par {row['ticket_id']})")
            if conflicts:
                conn.rollback()
                return False, conflicts
            for f in norm_files:
                if parent_id:
                    conn.execute(
                        "UPDATE file_locks SET ticket_id = ? WHERE project_id = ? AND file_path = ? AND ticket_id = ?",
                        (ticket_id, project_id, f, parent_id),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO file_locks (project_id, file_path, ticket_id) VALUES (?, ?, ?)",
                    (project_id, f, ticket_id),
                )
            conn.commit()
            return True, []
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def return_locks_to_parent(self, project_id: str, ticket_id: str) -> None:
        """Rend au parent tous les verrous du ticket ; sans effet si le ticket n'a pas de parent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT parent_id FROM tickets WHERE project_id = ? AND id = ?",
                (project_id, ticket_id),
            ).fetchone()
            parent_id = (row["parent_id"] or "") if row else ""
            if not parent_id:
                return
            conn.execute(
                "UPDATE file_locks SET ticket_id = ? WHERE project_id = ? AND ticket_id = ?",
                (parent_id, project_id, ticket_id),
            )

    def complete_and_merge_ticket(self, project_id: str, ticket_id: str) -> list[str]:
        unblocked: list[str] = []
        ticket = self.get_ticket(project_id, ticket_id)
        has_parent = bool(ticket and ticket.get("parent_id"))
        if has_parent:
            self.return_locks_to_parent(project_id, ticket_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE tickets SET state = 'MERGED', updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND id = ?",
                (project_id, ticket_id),
            )
            if not has_parent:
                conn.execute(
                    "DELETE FROM file_locks WHERE project_id = ? AND ticket_id = ?",
                    (project_id, ticket_id),
                )
            blocked_rows = conn.execute(
                "SELECT id, depends_on_json FROM tickets WHERE project_id = ? AND state = 'BLOCKED'",
                (project_id,),
            ).fetchall()
            for r in blocked_rows:
                deps = json.loads(r["depends_on_json"] or "[]")
                if ticket_id in deps:
                    placeholders = ",".join("?" for _ in deps)
                    st_rows = conn.execute(
                        f"SELECT id, state FROM tickets WHERE project_id = ? AND id IN ({placeholders})",
                        [project_id, *deps],
                    ).fetchall()
                    merged_set = {sr["id"] for sr in st_rows if sr["state"] == "MERGED"}
                    if all(d in merged_set for d in deps):
                        conn.execute(
                            "UPDATE tickets SET state = 'READY', updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND id = ?",
                            (project_id, r["id"]),
                        )
                        unblocked.append(r["id"])
        return unblocked

    def set_mistral_key_active(self, active: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE quota_state SET api_key_configured = ?, updated_at = CURRENT_TIMESTAMP WHERE provider = 'mistral'",
                (1 if active else 0,),
            )

    def is_mistral_ready(self) -> bool:
        with self._connect() as conn:
            ms = conn.execute("SELECT * FROM quota_state WHERE provider = 'mistral'").fetchone()
        if not ms or not bool(ms["api_key_configured"]):
            return False
        limit = float(ms["monthly_limit_eur"] or 0.0)
        return limit <= 0.0 or float(ms["monthly_spent_eur"] or 0.0) < limit * 0.95

    def update_quota_usage(
        self,
        provider: str,
        window_used_pct: float | None = None,
        add_spent_eur: float = 0.0,
    ) -> None:
        with self._connect() as conn:
            if window_used_pct is not None:
                conn.execute(
                    "UPDATE quota_state SET window_used_pct = ?, updated_at = CURRENT_TIMESTAMP WHERE provider = ?",
                    (window_used_pct, provider),
                )
            if add_spent_eur > 0:
                conn.execute(
                    """
                    UPDATE quota_state
                    SET daily_spent_eur = daily_spent_eur + ?,
                        monthly_spent_eur = monthly_spent_eur + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE provider = ?
                    """,
                    (add_spent_eur, add_spent_eur, provider),
                )

    def select_optimal_lane(self, complexity: str, priority: str, fail_count: int = 0) -> dict[str, str]:
        with self._connect() as conn:
            q_rows = {
                r["provider"]: dict(r)
                for r in conn.execute("SELECT * FROM quota_state").fetchall()
            }

        agy = q_rows["agy"]
        ds = q_rows["deepseek"]
        ms = q_rows["mistral"]

        ms_limit = float(ms.get("monthly_limit_eur") or 0.0)
        mistral_ready = bool(ms["api_key_configured"]) and (
            ms_limit <= 0.0 or float(ms["monthly_spent_eur"] or 0.0) < ms_limit * 0.95
        )
        deepseek_ready = ds["monthly_spent_eur"] < ds["monthly_limit_eur"] * 0.95
        agy_std_ok = agy["window_used_pct"] < 60.0
        agy_argon_ok = agy["window_used_pct"] < 88.0

        if (fail_count >= 2 or complexity == "L" or priority == "P0") and agy_argon_ok:
            return {
                "lane": "agy_argon",
                "cli": "agy --model 'Gemini 4 Hydrogen (High)'",
                "tdd_prep": "nas-gemma4-32k",
                "gate0_fixer": "local-small (RTX 3070)",
                "gate1_audit": "nas-gemma4-32k",
                "reason": "Escalade Argon (Ticket L/P0 ou reprise sur échec)",
            }

        if complexity == "XS":
            if mistral_ready:
                return {
                    "lane": "mistral_vibe",
                    "cli": "vibe",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": "Mistral Vibe (abonnement forfaitaire, 0 € marginal)",
                }
            if deepseek_ready:
                return {
                    "lane": "deepseek_coder",
                    "cli": "opencode",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": "Mistral en veille -> Repli DeepSeek 20€",
                }

        if complexity == "S":
            if mistral_ready:
                return {
                    "lane": "mistral_vibe",
                    "cli": "vibe",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": "Mistral Vibe (abonnement forfaitaire, prioritaire avant DeepSeek)",
                }
            if deepseek_ready:
                return {
                    "lane": "deepseek_coder",
                    "cli": "opencode",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": "Bouclier quota AGY -> DeepSeek 20€",
                }

        if complexity == "M":
            if agy_std_ok:
                return {
                    "lane": "agy_standard",
                    "cli": "agy",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": f"Antigravity Pro nominal (quota 5h à {agy['window_used_pct']:.0f}%)",
                }
            if mistral_ready:
                return {
                    "lane": "mistral_vibe",
                    "cli": "vibe",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": f"Bouclier AGY ({agy['window_used_pct']:.0f}% >= 60%) -> Mistral Vibe (forfait)",
                }
            if deepseek_ready:
                return {
                    "lane": "deepseek_coder",
                    "cli": "opencode",
                    "tdd_prep": "nas-gemma4-32k",
                    "gate0_fixer": "local-small (RTX 3070)",
                    "gate1_audit": "nas-gemma4-32k",
                    "reason": f"Bouclier AGY ({agy['window_used_pct']:.0f}% >= 60%) -> DeepSeek 20€",
                }

        return {
            "lane": "local_nas_coder",
            "cli": "opencode --model local-coder",
            "tdd_prep": "nas-gemma4-32k",
            "gate0_fixer": "local-small (RTX 3070)",
            "gate1_audit": "nas-gemma4-32k",
            "reason": "Mode autonome local GTX 1080 Ti (0 €)",
        }

    def get_prioritized_queue(self, project_id: str) -> list[dict[str, Any]]:
        prio_weights = {"P0": 1000, "P1": 500, "P2": 200, "P3": 50}
        with self._connect() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            active_ms = proj["active_milestone"] if proj else "J1"
            all_tickets = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM tickets WHERE project_id = ?", (project_id,)
                ).fetchall()
            ]

        dependents_count: dict[str, int] = {t["id"]: 0 for t in all_tickets}
        for t in all_tickets:
            if t["state"] not in ("MERGED", "ABANDONED"):
                for dep in json.loads(t["depends_on_json"] or "[]"):
                    if dep in dependents_count:
                        dependents_count[dep] += 1

        queue: list[dict[str, Any]] = []
        for t in all_tickets:
            if t["state"] in ("MERGED", "ABANDONED"):
                continue
            ms_bonus = 300 if t["milestone_id"] == active_ms else 0
            p_weight = prio_weights.get(t["priority"], 100)
            unblock_bonus = dependents_count.get(t["id"], 0) * 15
            state_bonus = 2000 if t["state"] in ("GATE2", "GATE1", "VALIDATED") else (
                1200 if t["state"] == "ASSIGNED" else 0
            )
            score = state_bonus + ms_bonus + p_weight + unblock_bonus if t["state"] != "BLOCKED" else -100
            t["parent_id"] = t.get("parent_id") or ""
            t["critical_score"] = score
            t["unblocks_count"] = dependents_count.get(t["id"], 0)
            queue.append(t)

        queue.sort(key=lambda x: (-x["critical_score"], x["id"]))
        return queue

    def export_markdown_roadmap(self, project_id: str, output_path: Path) -> str:
        with self._connect() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            milestones = conn.execute(
                "SELECT * FROM milestones WHERE project_id = ? ORDER BY rank", (project_id,)
            ).fetchall()
            tickets = conn.execute(
                "SELECT * FROM tickets WHERE project_id = ?", (project_id,)
            ).fetchall()

        lines = [
            f"# {proj['name'] if proj else project_id} — Roadmap & Pilotage PMO (Généré automatiquement)",
            "",
            "> Fichier généré sans LLM depuis SQLite (`pmo_db.py`). Ne pas éditer à la main.",
            "",
            "## 1. Avancement des Jalons",
            "",
            "| Jalon | Titre | Statut | Progression | Fusionnés / Total |",
            "|---|---|---|---|---|",
        ]

        by_ms: dict[str, list[sqlite3.Row]] = {}
        for t in tickets:
            ms_id = t["milestone_id"] or "Hors roadmap"
            by_ms.setdefault(ms_id, []).append(t)

        for ms in milestones:
            m_id = ms["id"]
            t_list = [t for t in by_ms.get(m_id, []) if t["state"] != "ABANDONED"]
            tot = len(t_list)
            merged = sum(1 for t in t_list if t["state"] == "MERGED")
            pct = round((merged / tot) * 100.0, 1) if tot else 0.0
            filled = int(round(pct / 10.0))
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(f"| **{m_id}** | {ms['title']} | `{ms['status']}` | `{bar}` {pct}% | {merged} / {tot} |")

        lines.extend([
            "",
            "## 2. File active par Chemin Critique et Priorité",
            "",
            "| Ticket | Jalon | Prio | Taille | État | Voie / Agent | Débloque | Titre |",
            "|---|---|---|---|---|---|---|---|",
        ])

        for item in self.get_prioritized_queue(project_id):
            lines.append(
                f"| **{item['id']}** | {item['milestone_id'] or '-'} | `{item['priority']}` | "
                f"`[{item['complexity']}]` | **{item['state']}** | `{item['owner']}` | "
                f"{item['unblocks_count']} ticket(s) | {item['title']} |"
            )

        content = "\n".join(lines) + "\n"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        return content
