#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Dashboard TUI PMO (Roadmap J1..J5 + Chemin Critique + Gouverneur Quotas).
Respecte le correctif de fluidité T-140 : zéro écriture stdout si l'état SQLite n'a pas changé.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from pmo_db import CockpitDB

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_MAGENTA = "\033[35m"
C_BLUE = "\033[34m"


def render_bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    return "█" * filled + "░" * (width - filled)


def render_pmo_screen(db: CockpitDB, project_id: str, width: int = 105) -> str:
    with db._connect() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        milestones = conn.execute(
            "SELECT * FROM milestones WHERE project_id = ? ORDER BY rank", (project_id,)
        ).fetchall()
        all_tickets = conn.execute(
            "SELECT * FROM tickets WHERE project_id = ?", (project_id,)
        ).fetchall()
        quotas = {
            r["provider"]: dict(r)
            for r in conn.execute("SELECT * FROM quota_state").fetchall()
        }
        runs_du_jour = conn.execute(
            "SELECT provider, COUNT(*) AS nb_runs, SUM(cost_eur) AS cout_total, "
            "SUM(tokens_in) AS tokens_in_total, SUM(tokens_out) AS tokens_out_total "
            "FROM runs WHERE project_id = ? AND date(created_at) = date('now') "
            "GROUP BY provider ORDER BY provider",
            (project_id,),
        ).fetchall()

    proj_name = proj["name"] if proj else project_id.upper()
    lines: list[str] = [
        f"{C_BOLD}{C_CYAN}══ AGENTIC COCKPIT V2 — PROJET : {proj_name} (SQLite WAL • 0 Token) ══{C_RESET}",
    ]

    agy = quotas.get("agy", {})
    claude = quotas.get("claude", {})
    ds = quotas.get("deepseek", {})
    ms = quotas.get("mistral", {})
    ms_status = (
        f"{ms.get('monthly_spent_eur', 0):.2f}€/{ms.get('monthly_limit_eur', 25):.0f}€ (ACTIF)"
        if ms.get("api_key_configured")
        else "EN VEILLE (Clé absente -> Route DeepSeek)"
    )

    lines.append(
        f"{C_DIM}Quotas 5h :{C_RESET} AGY Pro [{render_bar(agy.get('window_used_pct', 0), 8)}] {agy.get('window_used_pct', 0):.0f}% (Argon Prêt) │ "
        f"Claude [{render_bar(claude.get('window_used_pct', 0), 8)}] {claude.get('window_used_pct', 0):.0f}%"
    )
    lines.append(
        f"{C_DIM}Crédits API :{C_RESET} DeepSeek {C_GREEN}{ds.get('monthly_spent_eur', 0):.2f}€/{ds.get('monthly_limit_eur', 20):.0f}€{C_RESET} "
        f"(Cible {ds.get('daily_target_eur', 0.65):.2f}€/j) │ Mistral 25€ : {C_YELLOW}{ms_status}{C_RESET}"
    )
    lines.append(
        f"{C_DIM}GPU Locaux (0€) :{C_RESET} NAS 1080Ti (192.168.0.10) {C_GREEN}Gemma4-ctx:32k (208 tok/s){C_RESET} │ "
        f"PC RTX 3070 {C_GREEN}Qwen2.5-Coder:7b (Fixer){C_RESET}"
    )
    lines.append("")

    lines.append(f"{C_BOLD}{C_BLUE}── 1. ROADMAP & AVANCEMENT DES JALONS ──{C_RESET}")
    by_ms: dict[str, list[sqlite3.Row]] = {}
    for t in all_tickets:
        by_ms.setdefault(t["milestone_id"] or "Hors roadmap", []).append(t)

    for ms_row in milestones:
        m_id = ms_row["id"]
        t_list = by_ms.get(m_id, [])
        tot = len(t_list)
        merged = sum(1 for t in t_list if t["state"] == "MERGED")
        pct = (merged / tot * 100.0) if tot else 0.0
        bar = render_bar(pct, 14)
        lines.append(
            f"  {C_BOLD}{m_id:<4}{C_RESET} {ms_row['title']:<28} [{bar}] {pct:5.1f}% ({merged}/{tot} fusionnés) [{ms_row['status']}]"
        )

    lines.append("")
    queue = db.get_prioritized_queue(project_id)
    lines.append(
        f"{C_BOLD}{C_MAGENTA}── 2. FILE D'EXÉCUTION PAR CHEMIN CRITIQUE & PRIORITÉ ({len(queue)} actifs) ──{C_RESET}"
    )
    lines.append(
        f"{C_DIM}{'ID':<7} {'JALON':<6} {'PRIO':<5} {'TAILLE':<6} {'ÉTAT':<10} {'DÉBLOQUE':<9} {'VOIE RECOMMANDÉE':<20} {'TITRE'}{C_RESET}"
    )
    for item in queue[:18]:
        rec = db.select_optimal_lane(item["complexity"], item["priority"], item["fail_count"])
        lane = item.get("lane") or rec["lane"]
        unb = f"+{item['unblocks_count']} aval" if item["unblocks_count"] else "-"
        title_trunc = item["title"][: max(20, width - 78)]
        lines.append(
            f"{C_BOLD}{item['id']:<7}{C_RESET} {str(item['milestone_id'] or '-'):<6} "
            f"{item['priority']:<5} [{item['complexity']:<2}]   {item['state']:<10} "
            f"{unb:<9} {lane:<20} {title_trunc}"
        )

    events = db.recent_events(project_id, 5)
    if events:
        lines.append("")
        lines.append(f"{C_BOLD}{C_BLUE}── 3. DERNIERS ÉVÉNEMENTS ──{C_RESET}")
        for ev in events:
            ts = (ev.get("created_at") or "")[-8:]
            ticket = ev.get("ticket_id") or "-"
            kind = ev.get("kind") or ""
            detail = (ev.get("detail") or "")[: max(10, width - 40)]
            lines.append(f"  {C_DIM}{ts}{C_RESET} {ticket:<7} {kind:<12} {detail}")

    if runs_du_jour:
        lines.append("")
        lines.append(f"{C_BOLD}{C_BLUE}── 4. COÛTS ET RUNS DU JOUR ──{C_RESET}")
        for run in runs_du_jour:
            nb = run["nb_runs"]
            pluriel = "run" if nb == 1 else "runs"
            lines.append(
                f"  {C_BOLD}{run['provider']:<10}{C_RESET} {nb} {pluriel} │ "
                f"{run['cout_total'] or 0:.2f}€ │ "
                f"in {run['tokens_in_total'] or 0} tok │ out {run['tokens_out_total'] or 0} tok"
            )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("PROJECT", "mon-app"))
    parser.add_argument("--db", default=str(Path.home() / "agentic-cockpit/db/cockpit_pmo.db"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    db = CockpitDB(Path(args.db))
    w = shutil.get_terminal_size((105, 30)).columns
    out = render_pmo_screen(db, args.project, width=w)
    if args.once:
        print(out)
        return

    last_out = ""
    while True:
        w = shutil.get_terminal_size((105, 30)).columns
        out = render_pmo_screen(db, args.project, width=w)
        if out != last_out:
            sys.stdout.write("\033[2J\033[H" + out + "\n")
            sys.stdout.flush()
            last_out = out
        time.sleep(10)


if __name__ == "__main__":
    main()
