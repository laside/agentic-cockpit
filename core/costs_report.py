#!/usr/bin/env python3
"""
Rapport cockpit costs — workers et architecte, par ticket, sans mélange d'unités.
Bibliothèque standard uniquement, aucun réseau. Compatible Python 3.12.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

MAX_LINES = 60
TYPES_MESURES = ("measured", "api_equivalent", "tokens_only")


def _utc() -> datetime.timezone:
    return datetime.timezone.utc


def _parse_iso(texte: str) -> datetime.datetime | None:
    if not texte:
        return None
    valeur = texte.strip()
    if valeur.endswith("Z"):
        valeur = valeur[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(valeur)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_utc())
    return dt.astimezone(_utc())


def _parse_since(since: str | datetime.datetime | None) -> datetime.datetime | None:
    if since is None:
        return None
    if isinstance(since, datetime.datetime):
        dt = since
        return dt.replace(tzinfo=_utc()) if dt.tzinfo is None else dt.astimezone(_utc())
    return _parse_iso(since) or _parse_iso(since + "T00:00:00")


def _lire_runs(db_path: Path, project_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _valeur(row: dict[str, Any], cle: str, defaut: Any) -> Any:
    valeur = row.get(cle, defaut)
    return defaut if valeur is None else valeur


def _entier(valeur: Any) -> int:
    try:
        return int(float(valeur))
    except (TypeError, ValueError):
        return 0


def _montant(valeur: Any, symbole: str) -> str:
    try:
        nombre = float(valeur)
    except (TypeError, ValueError):
        nombre = 0.0
    return f"{nombre:.2f} {symbole}".replace(".", ",")


def _nombre(valeur: float) -> str:
    if abs(round(valeur) - valeur) < 1e-9:
        return str(int(round(valeur)))
    return f"{valeur:.1f}".replace(".", ",")


def _resume_workers(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int, int, float]:
    voies: dict[str, dict[str, Any]] = {}
    total_in = total_out = total_cache = 0
    total_mesure = 0.0
    for row in rows:
        lane = str(_valeur(row, "lane", "") or "inconnue")
        voie = voies.setdefault(
            lane,
            {"lane": lane, "runs": 0, "in": 0, "out": 0, "cache": 0, "kinds": {}},
        )
        entree = _entier(_valeur(row, "tokens_in", 0))
        sortie = _entier(_valeur(row, "tokens_out", 0))
        cache = _entier(_valeur(row, "tokens_cache", 0))
        voie["runs"] += 1
        voie["in"] += entree
        voie["out"] += sortie
        voie["cache"] += cache
        total_in += entree
        total_out += sortie
        total_cache += cache

        kind = str(_valeur(row, "cost_kind", "") or "").strip()
        if kind == "measured":
            cout = float(_valeur(row, "cost_eur", 0.0) or 0.0)
            total_mesure += cout
            voie["kinds"]["measured"] = voie["kinds"].get("measured", 0.0) + cout
        elif kind == "api_equivalent":
            cout = float(
                _valeur(row, "cost_usd", _valeur(row, "cost_eur", 0.0)) or 0.0
            )
            voie["kinds"]["api_equivalent"] = voie["kinds"].get("api_equivalent", 0.0) + cout
        elif kind == "tokens_only":
            voie["kinds"]["tokens_only"] = voie["kinds"].get("tokens_only", 0) + 1
        else:
            voie["kinds"]["non_mesure"] = voie["kinds"].get("non_mesure", 0) + 1
    return list(voies.values()), total_in, total_out, total_cache, total_mesure


def _cout_voie(kinds: dict[str, Any]) -> str:
    morceaux: list[str] = []
    if "measured" in kinds:
        morceaux.append(_montant(kinds["measured"], "€") + " mesuré")
    if "api_equivalent" in kinds:
        morceaux.append(_montant(kinds["api_equivalent"], "$") + " équivalent API (abonnement)")
    if "tokens_only" in kinds:
        morceaux.append("tokens seulement")
    if "non_mesure" in kinds:
        morceaux.append("non mesuré")
    return " + ".join(morceaux) if morceaux else "non mesuré"


def _lire_journal(path: Path) -> list[dict[str, Any]]:
    """Lit un journal JSONL et dédoublonne par message.id (à défaut requestId)."""
    uniques: dict[str, dict[str, Any]] = {}
    try:
        lignes = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for numero, ligne in enumerate(lignes):
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            obj = json.loads(ligne)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        timestamp = _parse_iso(str(obj.get("timestamp") or ""))
        if timestamp is None:
            continue
        ident = msg.get("id") or obj.get("requestId") or f"{path}:{numero}"
        sortie = _entier(usage.get("output_tokens", 0))
        existant = uniques.get(str(ident))
        if existant is not None and existant["sortie"] >= sortie:
            continue
        uniques[str(ident)] = {
            "id": str(ident),
            "timestamp": timestamp,
            "model": str(msg.get("model") or "inconnu"),
            "entree": _entier(usage.get("input_tokens", 0)),
            "sortie": sortie,
            "cache_creation": _entier(usage.get("cache_creation_input_tokens", 0)),
            "cache_read": _entier(usage.get("cache_read_input_tokens", 0)),
        }
    return list(uniques.values())


def _transcripts_par_defaut(db_path: Path, project_id: str) -> list[Path]:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT wt_sessions_dir FROM projects WHERE id = ?", (project_id,)).fetchone()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if not row or not row["wt_sessions_dir"]:
        return []
    worktree = Path(str(row["wt_sessions_dir"])) / "wt-claude"
    slug = str(worktree).replace("/", "-")
    base = Path.home() / ".claude" / "projects" / slug
    if not base.is_dir():
        return []
    return sorted(base.glob("*.jsonl"))


def _fenetres_tickets(db_path: Path, project_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        tickets = conn.execute(
            "SELECT id, title FROM tickets WHERE project_id = ? AND state = 'MERGED' ORDER BY id",
            (project_id,),
        ).fetchall()
        fenetres: list[dict[str, Any]] = []
        for ticket in tickets:
            premier = conn.execute(
                "SELECT MIN(created_at) AS t FROM events WHERE project_id = ? AND ticket_id = ?",
                (project_id, ticket["id"]),
            ).fetchone()
            fusion = conn.execute(
                "SELECT MAX(created_at) AS t FROM events WHERE project_id = ? AND ticket_id = ? AND kind = 'MERGED'",
                (project_id, ticket["id"]),
            ).fetchone()
            debut = _parse_iso(str(premier["t"])) if premier and premier["t"] else None
            fin = _parse_iso(str(fusion["t"])) if fusion and fusion["t"] else None
            if debut is None or fin is None or fin < debut:
                continue
            fenetres.append({"id": ticket["id"], "title": ticket["title"], "debut": debut, "fin": fin})
        return fenetres
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _attribuer(messages: list[dict[str, Any]], fenetres: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    par_ticket: dict[str, dict[str, Any]] = {
        f["id"]: {"id": f["id"], "title": f["title"], "sortie": 0.0, "cache_read": 0.0}
        for f in fenetres
    }
    hors = {"sortie": 0.0, "cache_read": 0.0}
    for message in messages:
        touches = [f for f in fenetres if f["debut"] <= message["timestamp"] <= f["fin"]]
        if touches:
            part = 1.0 / len(touches)
            for fenetre in touches:
                par_ticket[fenetre["id"]]["sortie"] += message["sortie"] * part
                par_ticket[fenetre["id"]]["cache_read"] += message["cache_read"] * part
        else:
            hors["sortie"] += message["sortie"]
            hors["cache_read"] += message["cache_read"]
    return par_ticket, hors


def build_report(
    project_id: str,
    db_path: Path | str,
    since: str | datetime.datetime | None = None,
    transcripts: list[Path | str] | None = None,
) -> str:
    """Construit le rapport texte (60 lignes au plus)."""
    db_path = Path(db_path)
    lignes: list[str] = [f"COCKPIT COSTS — projet {project_id}", f"source DB : {db_path}", "", "WORKERS"]

    rows = _lire_runs(db_path, project_id)
    voies, total_in, total_out, total_cache, total_mesure = _resume_workers(rows)
    if not voies:
        lignes.append("aucun run enregistré")
    for voie in voies:
        lignes.append(
            f"voie {voie['lane']} : {voie['runs']} run(s) · entrée {voie['in']} · "
            f"sortie {voie['out']} · cache {voie['cache']} · {_cout_voie(voie['kinds'])}"
        )
    lignes.append(
        f"TOTAL workers : entrée {total_in} · sortie {total_out} · cache {total_cache} · "
        f"entrée+sortie {total_in + total_out}"
    )
    lignes.append(f"TOTAL coût mesuré : {_montant(total_mesure, '€')}")
    lignes.append("")

    if transcripts is None:
        chemins = _transcripts_par_defaut(db_path, project_id)
    else:
        chemins = [Path(p) for p in transcripts]

    messages: list[dict[str, Any]] = []
    for chemin in chemins:
        messages.extend(_lire_journal(chemin))

    seuil = _parse_since(since)
    if seuil is not None:
        messages = [m for m in messages if m["timestamp"] >= seuil]

    lignes.append("ARCHITECTE")
    if not messages:
        lignes.append("ARCHITECTE : aucun journal de session trouvé")
        lignes.append("")
        lignes.append("Le cache relu domine la facture de l'architecte (relu à chaque tour) : "
                      "l'économie passe par moins de tours de Claude (boucle auto, sous-agents).")
        lignes.append("Les % d'abonnement ne sont pas accessibles par script.")
        return "\n".join(lignes[:MAX_LINES])

    par_modele: dict[str, dict[str, Any]] = {}
    for message in messages:
        modele = par_modele.setdefault(
            message["model"],
            {"messages": 0, "entree": 0, "sortie": 0, "cache_creation": 0, "cache_read": 0},
        )
        modele["messages"] += 1
        modele["entree"] += message["entree"]
        modele["sortie"] += message["sortie"]
        modele["cache_creation"] += message["cache_creation"]
        modele["cache_read"] += message["cache_read"]
    for nom, donnees in par_modele.items():
        lignes.append(
            f"modèle {nom} : {donnees['messages']} message(s) · entrée {donnees['entree']} · "
            f"sortie {donnees['sortie']} · cache créé {donnees['cache_creation']} · "
            f"cache relu {donnees['cache_read']}"
        )

    fenetres = _fenetres_tickets(db_path, project_id)
    par_ticket, hors = _attribuer(messages, fenetres)
    lignes.append("Tickets fusionnés (architecte)")
    classes = sorted(par_ticket.values(), key=lambda t: (-t["sortie"], t["id"]))[:10]
    for ticket in classes:
        titre = str(ticket["title"] or "")[:40]
        lignes.append(
            f"{ticket['id']} {titre} : sortie {_nombre(ticket['sortie'])} · "
            f"cache relu {_nombre(ticket['cache_read'])}"
        )
    lignes.append(
        f"hors ticket (pilotage, discussion) : sortie {_nombre(hors['sortie'])} · "
        f"cache relu {_nombre(hors['cache_read'])}"
    )
    avec_donnees = [t for t in par_ticket.values() if t["sortie"] > 0]
    total_attribue = sum(t["sortie"] for t in avec_donnees)
    moyenne = total_attribue / len(avec_donnees) if avec_donnees else 0.0
    mot = "ticket" if len(avec_donnees) == 1 else "tickets"
    lignes.append(
        f"MOYENNE tokens de sortie de l'architecte par ticket fusionné : "
        f"{_nombre(moyenne)} (sur {len(avec_donnees)} {mot} avec données)"
    )
    lignes.append("")
    lignes.append("Le cache relu domine la facture de l'architecte (relu à chaque tour) : "
                  "l'économie passe par moins de tours de Claude (boucle auto, sous-agents).")
    lignes.append("Les % d'abonnement ne sont pas accessibles par script.")
    return "\n".join(lignes[:MAX_LINES])


def _db_par_defaut() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    cockpit = Path(os.environ.get("COCKPIT_HOME") or (Path.home() / "agentic-cockpit"))
    return str(cockpit / "db" / "cockpit_pmo.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cockpit costs", description="Rapport des coûts workers et architecte.")
    parser.add_argument("project", help="identifiant du projet")
    parser.add_argument("--since", default=None, help="date UTC AAAA-MM-JJ (défaut : tout)")
    parser.add_argument("--transcript", action="append", default=[], help="journal JSONL de session Claude (répétable)")
    parser.add_argument("--db", default=None, help="chemin de la base SQLite")
    args = parser.parse_args(argv)
    texte = build_report(
        args.project,
        args.db or _db_par_defaut(),
        since=args.since,
        transcripts=args.transcript if args.transcript else None,
    )
    print(texte)
    return 0


if __name__ == "__main__":
    sys.exit(main())
