#!/usr/bin/env python3
"""Actions de l'onglet Commande : argv construits (jamais de shell), actions personnalisées, lancement détaché."""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CORE = Path(__file__).resolve().parents[2] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import config_paths  # noqa: E402
from pmo_db import parse_toml_text  # noqa: E402

LOG_DIR = Path.home() / ".cache" / "agentic-cockpit" / "ui-logs"
CLI = "cockpit"


@dataclass
class Action:
    id: str
    libelle: str
    confirmation: bool = False
    longue: bool = False
    argv: list[str] = field(default_factory=list)


def actions_projet(projet: str) -> list[Action]:
    """Actions toujours disponibles au niveau du projet."""
    return [
        Action("sync_projet", "Synchroniser", False, False, [CLI, "sync", projet]),
        Action("status", "État", False, False, [CLI, "status", projet]),
        Action("brief", "Brief", False, False, [CLI, "brief", projet]),
        Action("costs", "Coûts", False, False, [CLI, "costs", projet]),
        Action("auto_dry", "Simulation de la boucle", False, False,
               [CLI, "auto", projet, "--slots", "1", "--dry-run"]),
    ]


def actions_for(
    projet: str,
    ticket: dict[str, Any] | None,
    voie_choisie: str = "",
    worktree: str | None = None,
    branche_defaut: str = "main",
) -> list[Action]:
    """Actions du ticket selon son état, suivies des actions du projet."""
    res: list[Action] = []
    if ticket:
        tid = ticket["id"]
        etat = ticket.get("state") or ticket.get("etat") or ""
        voie = ticket.get("lane") or ticket.get("voie") or ""
        abandon = Action("abandon", "Abandonner", True, False,
                         [CLI, "abandon", tid, projet, "--reason", "abandonné depuis l'onglet Commande"])
        if etat == "DRAFT":
            res += [Action("promote", "Promouvoir", False, False, [CLI, "ticket", "promote", projet, tid]), abandon]
        elif etat == "READY":
            res += [
                Action("run", "Lancer", False, True,
                       [CLI, "run", tid, projet, "--lane", voie_choisie, "--no-gate1"]),
                Action("claim", "Prendre en main", False, False, [CLI, "claim", tid, projet, "--as", "claude"]),
                abandon,
            ]
        elif etat == "BLOCKED":
            res += [Action("events", "Voir les événements", False, False, [CLI, "events", projet]), abandon]
        elif etat == "ASSIGNED" and voie == "interactive":
            res.append(Action("submit", "Soumettre", False, True, [CLI, "submit", tid, projet]))
        elif etat == "GATE2":
            if worktree:
                res.append(Action("diff", "Voir le diff", False, False,
                                  ["git", "-C", worktree, "diff", f"origin/{branche_defaut}...HEAD"]))
            res.append(Action("approve", "Approuver", True, True, [CLI, "merge", tid, projet, "--approve"]))
        elif etat == "PR_OPEN":
            res.append(Action("sync", "Synchroniser", False, False, [CLI, "sync", projet]))
    return res + actions_projet(projet)


def _config_actions(cockpit_home: Path | None) -> list[dict[str, Any]]:
    try:
        path = config_paths.config_file("actions.toml", cockpit_home)
        data = parse_toml_text(path.read_text(encoding="utf-8"))
        items = data.get("action", [])
        return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []
    except Exception:  # noqa: BLE001 - absent ou invalide : aucune action personnalisée
        return []


def actions_personnalisees(
    projet: str,
    ticket: dict[str, Any] | None,
    worktree: str | None = None,
    cockpit_home: Path | None = None,
) -> list[Action]:
    """Actions de actions.toml filtrées par état et par projet, jetons remplacés."""
    home = Path(cockpit_home).expanduser() if cockpit_home else Path.home() / "agentic-cockpit"
    t = ticket or {}
    etat = t.get("state") or t.get("etat") or ""
    jetons = {
        "{ticket}": t.get("id", ""),
        "{projet}": projet,
        "{worktree}": worktree or "",
        "{cockpit_home}": str(home),
    }
    res: list[Action] = []
    for a in _config_actions(home):
        try:
            argv = a["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                continue
            etats = a.get("etats") or []
            projets = a.get("projets") or []
            if etats and etat not in etats:
                continue
            if projets and projet not in projets:
                continue
            for k, v in jetons.items():
                argv = [x.replace(k, v) for x in argv]
            res.append(Action(str(a["id"]), str(a["libelle"]), bool(a.get("confirmation", False)),
                              bool(a.get("longue", False)), argv))
        except (KeyError, TypeError):
            continue
    return res


def run_action(action: Action, log_dir: Path | None = None) -> Path:
    """Lance argv détaché (nouvelle session) ; sortie dans un journal horodaté ; renvoie son chemin."""
    d = Path(log_dir) if log_dir else LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    journal = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}-{action.id}.log"
    with open(journal, "wb") as out:
        subprocess.Popen(
            action.argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return journal
