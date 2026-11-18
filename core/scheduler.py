"""Ordonnanceur de lots de tickets sans conflit de fichiers (T-004)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

try:
    from core.pmo_db import normalize_rel_path
except ImportError:
    from pmo_db import normalize_rel_path  # type: ignore[no-redef]

if TYPE_CHECKING:
    from core.pmo_db import CockpitDB


def plan_batch(
    db: CockpitDB,
    project_id: str,
    slots: int = 2,
    provider_limits: dict[str, int] | None = None,
    lane_providers: dict[str, str] | None = None,
    parent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Sélectionne jusqu'à `slots` tickets READY sans conflit de fichiers ni dépassement de limite provider."""
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
        locks_by_file.setdefault(normalize_rel_path(row["file_path"]), set()).add(row["ticket_id"])

    queue = db.get_prioritized_queue(project_id)
    lp = lane_providers or {}
    active_by_provider: dict[str, int] = {}
    if provider_limits is not None:
        for t in queue:
            if t.get("state") in ("ASSIGNED", "GATE0", "GATE1"):
                t_lane = t.get("lane") or db.select_optimal_lane(
                    t.get("complexity", "M"),
                    t.get("priority", "P2"),
                    int(t.get("fail_count") or 0),
                )["lane"]
                t_prov = lp.get(t_lane, t_lane)
                active_by_provider[t_prov] = active_by_provider.get(t_prov, 0) + 1

    batch: list[dict[str, Any]] = []
    batch_files: set[str] = set()
    batch_by_provider: dict[str, int] = {}
    agy_taken = False

    for ticket in queue:
        if len(batch) >= slots:
            break

        t_parent = (ticket.get("parent_id") or "").strip()
        if parent_id is None:
            if t_parent:
                continue
        else:
            if t_parent != parent_id:
                continue

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

        files = [normalize_rel_path(f) for f in files if f and normalize_rel_path(f)]
        if not files:
            continue

        ticket_id = ticket["id"]
        allowed_holders = {ticket_id, parent_id} if parent_id else {ticket_id}
        conflict = False
        for file_path in files:
            if file_path in batch_files:
                conflict = True
                break
            holders = locks_by_file.get(file_path, set())
            if any(holder not in allowed_holders for holder in holders):
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

        if provider_limits is None:
            if lane.startswith("agy_"):
                if agy_taken:
                    continue
                agy_taken = True
        else:
            provider = lp.get(lane, lane)
            if provider in provider_limits:
                if (
                    active_by_provider.get(provider, 0) + batch_by_provider.get(provider, 0)
                    >= provider_limits[provider]
                ):
                    continue
            batch_by_provider[provider] = batch_by_provider.get(provider, 0) + 1

        batch.append({"id": ticket_id, "lane": lane, "files": files})
        batch_files.update(files)

    return batch


def should_stop(
    done: int,
    escalations_in_a_row: int,
    max_tickets: int = 6,
    max_escalations: int = 2,
    spent_eur: float = 0.0,
    max_cost_eur: float | None = None,
) -> str | None:
    """Détermine si la boucle d'exécution doit s'arrêter."""
    if done >= max_tickets:
        return "max_tickets"
    if escalations_in_a_row >= max_escalations:
        return "escalades"
    if max_cost_eur is not None and spent_eur >= max_cost_eur:
        return "budget"
    return None
