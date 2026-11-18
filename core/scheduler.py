"""Ordonnanceur de lots de tickets sans conflit de fichiers (T-004)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.pmo_db import CockpitDB


def plan_batch(db: CockpitDB, project_id: str, slots: int = 2) -> list[dict[str, Any]]:
    """Sélectionne jusqu'à `slots` tickets READY sans conflit de fichiers ni double session agy."""
    if slots <= 0:
        return []

    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT file_path, ticket_id FROM file_locks WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()

    locks_by_file: dict[str, set[str]] = {}
    for row in rows:
        locks_by_file.setdefault(row["file_path"], set()).add(row["ticket_id"])

    queue = db.get_prioritized_queue(project_id)
    batch: list[dict[str, Any]] = []
    batch_files: set[str] = set()
    agy_taken = False

    for ticket in queue:
        if len(batch) >= slots:
            break

        if ticket.get("state") != "READY":
            continue

        raw_files = ticket.get("files_owned")
        if raw_files is None:
            files_json = ticket.get("files_owned_json") or "[]"
            try:
                files = list(json.loads(files_json))
            except (TypeError, ValueError):
                files = []
        else:
            files = list(raw_files)

        files = [f for f in files if f]
        if not files:
            continue

        ticket_id = ticket["id"]
        conflict = False
        for file_path in files:
            if file_path in batch_files:
                conflict = True
                break
            holders = locks_by_file.get(file_path, set())
            if any(holder != ticket_id for holder in holders):
                conflict = True
                break
        if conflict:
            continue

        lane_info = db.select_optimal_lane(
            ticket.get("complexity", "M"),
            ticket.get("priority", "P2"),
            int(ticket.get("fail_count") or 0),
        )
        lane = lane_info["lane"]

        if lane.startswith("agy_"):
            if agy_taken:
                continue
            agy_taken = True

        batch.append({"id": ticket_id, "lane": lane, "files": files})
        batch_files.update(files)

    return batch


def should_stop(
    done: int,
    escalations_in_a_row: int,
    max_tickets: int = 6,
    max_escalations: int = 2,
) -> str | None:
    """Détermine si la boucle d'exécution doit s'arrêter."""
    if done >= max_tickets:
        return "max_tickets"
    if escalations_in_a_row >= max_escalations:
        return "escalades"
    return None
