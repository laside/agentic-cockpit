#!/usr/bin/env python3
"""Moteur cockpit lessons : analyse des échecs récurrents et section bornée.

Bibliothèque standard uniquement, compatible Python 3.12.
Aucun accès réseau, aucun LLM, aucune écriture dans la base réelle.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

FAILURE_KINDS = ("GATE0_FAIL", "GATE1_FAIL", "INTEGRATION_FAIL", "PR_PRIVACY_FAIL")
MIN_OCCURRENCES = 3
MAX_RULES = 20
SECTION_BEGIN = "<!-- BEGIN COCKPIT LESSONS -->"
SECTION_END = "<!-- END COCKPIT LESSONS -->"

_EMAIL_RE = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_AFFECTATION_RE = re.compile(
    r"\b(?:token|secret|password|passwd|api[_-]?key|bearer)\s*[=:]\s*\S+",
    re.IGNORECASE,
)
_SECRET_PREFIXE_RE = re.compile(r"\b(?:sk|ghp|gho|ghs|github_pat|xox[baprs])[-_][a-z0-9_-]{6,}", re.IGNORECASE)
_DUREE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|millisecondes?|s|sec(?:ondes?)?|min(?:utes?)?|m|h|heures?)\b",
    re.IGNORECASE,
)
_HEURE_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_HASH_HEX_RE = re.compile(r"\b[0-9a-f]{7,}\b", re.IGNORECASE)
_CHIFFRES_RE = re.compile(r"\d+")
_ESPACES_RE = re.compile(r"\s+")


def normalize_motif(detail: str) -> str:
    """Normalise un détail d'échec en motif stable, sans données sensibles."""
    texte = str(detail or "").lower()
    texte = _EMAIL_RE.sub(" ", texte)
    texte = _IPV4_RE.sub(" ", texte)
    texte = _SECRET_AFFECTATION_RE.sub(" ", texte)
    texte = _SECRET_PREFIXE_RE.sub(" ", texte)
    texte = _DUREE_RE.sub(" ", texte)
    texte = _HEURE_RE.sub(" ", texte)
    texte = _HASH_HEX_RE.sub(" ", texte)
    texte = _CHIFFRES_RE.sub(" ", texte)
    texte = _ESPACES_RE.sub(" ", texte).strip()
    return texte[:120].strip()


def collect_failures(
    db_path: str | Path,
    project_id: str,
    kinds: tuple[str, ...] | list[str] = FAILURE_KINDS,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Lit les événements d'échec en lecture seule, du plus ancien au plus récent."""
    types = tuple(kinds)
    if not types:
        return []
    chemin = Path(db_path)
    if not chemin.exists():
        return []
    try:
        uri = f"{chemin.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        marques = ", ".join("?" for _ in types)
        lignes = conn.execute(
            f"SELECT id, ticket_id, kind, detail FROM events "
            f"WHERE project_id = ? AND kind IN ({marques}) ORDER BY id ASC LIMIT ?",
            (project_id, *types, int(limit)),
        ).fetchall()
        return [dict(ligne) for ligne in lignes]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def group_recurring(
    failures: list[dict[str, Any]],
    min_occurrences: int = MIN_OCCURRENCES,
) -> list[dict[str, Any]]:
    """Regroupe les échecs par motif équivalent, triés par count décroissant puis motif."""
    groupes: dict[str, dict[str, Any]] = {}
    ordre: list[str] = []
    for echec in failures:
        motif = normalize_motif(str(echec.get("detail", "")))
        if not motif:
            motif = normalize_motif(str(echec.get("kind", ""))) or "motif vide"
        groupe = groupes.get(motif)
        if groupe is None:
            groupe = {"motif": motif, "count": 0, "kinds": set(), "examples": []}
            groupes[motif] = groupe
            ordre.append(motif)
        groupe["count"] += 1
        kind = str(echec.get("kind", "") or "")
        if kind:
            groupe["kinds"].add(kind)
        detail = str(echec.get("detail", "") or "")
        if detail and detail not in groupe["examples"] and len(groupe["examples"]) < 2:
            groupe["examples"].append(detail)
    resultat: list[dict[str, Any]] = []
    for motif in ordre:
        groupe = groupes[motif]
        if groupe["count"] < min_occurrences:
            continue
        resultat.append(
            {
                "motif": motif,
                "count": groupe["count"],
                "kinds": sorted(groupe["kinds"]),
                "examples": list(groupe["examples"]),
            }
        )
    resultat.sort(key=lambda g: (-g["count"], g["motif"]))
    return resultat


def propose_rules(groupes: list[dict[str, Any]]) -> list[str]:
    """Propose des règles courtes, déterministes et dédupliquées (au plus MAX_RULES)."""
    regles: list[str] = []
    vues: set[str] = set()
    for groupe in groupes:
        motif = str(groupe.get("motif", "") or "").strip()
        if not motif:
            continue
        count = int(groupe.get("count", 0) or 0)
        kinds = ", ".join(str(k) for k in (groupe.get("kinds") or []))
        suffixe = f", {kinds}" if kinds else ""
        regle = f"Prévenir la récurrence « {motif} » ({count} occurrences{suffixe})"
        cle = " ".join(regle.split()).lower()
        if cle in vues:
            continue
        vues.add(cle)
        regles.append(regle)
        if len(regles) >= MAX_RULES:
            break
    return regles


def render_section(rules: list[str]) -> str:
    """Produit un unique bloc délimité, une règle par ligne préfixée de « - »."""
    lignes = [SECTION_BEGIN]
    lignes.extend(f"- {regle}" for regle in rules)
    lignes.append(SECTION_END)
    return "\n".join(lignes)


def _dedupliquer(rules: list[str], max_rules: int) -> list[str]:
    propres: list[str] = []
    vues: set[str] = set()
    for regle in rules:
        texte = " ".join(str(regle).split()).strip()
        if not texte:
            continue
        cle = texte.lower()
        if cle in vues:
            continue
        vues.add(cle)
        propres.append(texte)
        if len(propres) >= max_rules:
            break
    return propres


def apply_rules(target: str | Path, rules: list[str], max_rules: int = MAX_RULES) -> list[str]:
    """Remplace le bloc délimité (ou l'ajoute en fin) de façon idempotente et bornée."""
    chemin = Path(target)
    propres = _dedupliquer(rules, max_rules)
    section = render_section(propres)
    try:
        texte = chemin.read_text(encoding="utf-8")
    except FileNotFoundError:
        texte = ""
    marqueurs = re.compile(f"{re.escape(SECTION_BEGIN)}|{re.escape(SECTION_END)}")
    occurrences = list(marqueurs.finditer(texte))
    if not occurrences:
        if texte and not texte.endswith("\n"):
            texte += "\n"
        nouveau = texte + section + "\n"
    else:
        morceaux: list[str] = []
        position = 0
        index = 0
        while index < len(occurrences):
            occurrence = occurrences[index]
            if occurrence.group() == SECTION_BEGIN:
                suivant = index + 1
                while (suivant < len(occurrences)
                       and occurrences[suivant].group() != SECTION_END):
                    suivant += 1
                morceaux.append(texte[position:occurrence.start()])
                if suivant < len(occurrences):
                    position = occurrences[suivant].end()
                    index = suivant + 1
                else:
                    position = occurrence.end()
                    index += 1
            else:
                morceaux.append(texte[position:occurrence.start()])
                position = occurrence.end()
                index += 1
        morceaux.append(texte[position:])
        nouveau = morceaux[0] + section + "".join(morceaux[1:])
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(nouveau, encoding="utf-8")
    return propres


def _db_par_defaut() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    cockpit = Path(os.environ.get("COCKPIT_HOME") or (Path.home() / "agentic-cockpit"))
    return str(cockpit / "db" / "cockpit_pmo.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cockpit lessons",
        description="Analyse les échecs récurrents et met à jour une section bornée.",
    )
    parser.add_argument("project", help="identifiant du projet")
    parser.add_argument("--apply", action="store_true", help="écrire les règles dans le fichier cible")
    parser.add_argument("--db", default=None, help="chemin de la base SQLite")
    parser.add_argument("--rules", default="AGENTS.md", help="fichier de règles cible")
    parser.add_argument("--min", type=int, default=MIN_OCCURRENCES, dest="min_occurrences",
                        help="nombre minimal d'occurrences")
    parser.add_argument("--json", action="store_true", help="afficher les groupes et règles en JSON")
    args = parser.parse_args(argv)

    failures = collect_failures(args.db or _db_par_defaut(), args.project)
    groupes = group_recurring(failures, min_occurrences=args.min_occurrences)
    regles = propose_rules(groupes)

    if args.json:
        print(json.dumps(
            {"project": args.project, "groups": groupes, "rules": regles},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(render_section(regles))

    if args.apply:
        apply_rules(args.rules, regles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
