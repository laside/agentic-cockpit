#!/usr/bin/env python3
"""
Lecture des coûts opencode : parsing de `opencode stats`, snapshots avant/après et deltas.
Aucun appel réseau ni LLM ; stdlib uniquement.
"""
from __future__ import annotations

import re
import subprocess

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

NOMBRE = r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([KMkm]?)"

MOTIF_LIGNE = re.compile(
    r"^\s*[│|]?\s*(?P<label>[A-Za-z][A-Za-z ]*?)\s+" + NOMBRE + r"\s*[│|]?\s*$"
)

MULTIPLICATEURS = {"": 1, "k": 1_000, "m": 1_000_000}

CHAMPS = ("total_cost_usd", "input_tokens", "output_tokens", "cache_read_tokens")

ZEROS = {
    "total_cost_usd": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
}


def _valeur(nombre: str, suffixe: str, entier: bool) -> float | int:
    valeur = float(nombre) * MULTIPLICATEURS[suffixe.lower()]
    return int(round(valeur)) if entier else round(valeur, 6)


def parse_opencode_stats(text: str) -> dict:
    """Parse la sortie de `opencode stats` et retourne les quatre métriques imposées."""
    resultat = dict(ZEROS)
    if not text:
        return resultat

    texte = ANSI_RE.sub("", text)

    for ligne in texte.splitlines():
        correspondance = MOTIF_LIGNE.match(ligne)
        if not correspondance:
            continue
        label = correspondance.group("label").strip().lower()
        nombre = correspondance.group(2)
        suffixe = correspondance.group(3)

        if label == "total cost":
            resultat["total_cost_usd"] = _valeur(nombre, suffixe, entier=False)
        elif label == "input":
            resultat["input_tokens"] = _valeur(nombre, suffixe, entier=True)
        elif label == "output":
            resultat["output_tokens"] = _valeur(nombre, suffixe, entier=True)
        elif label == "cache read":
            resultat["cache_read_tokens"] = _valeur(nombre, suffixe, entier=True)

    return resultat


def snapshot_opencode_stats(timeout: int = 30) -> dict | None:
    """Exécute `opencode stats` et retourne le parsing, ou None en cas d'échec."""
    try:
        acheve = subprocess.run(
            ["opencode", "stats"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if acheve.returncode != 0:
        return None

    return parse_opencode_stats(acheve.stdout or "")


def delta(before: dict | None, after: dict | None) -> dict:
    """Différence après - avant, jamais négative ; quatre zéros si un côté est None."""
    if before is None or after is None:
        return dict(ZEROS)

    resultat = {}
    for champ in CHAMPS:
        difference = after.get(champ, 0) - before.get(champ, 0)
        if isinstance(difference, float):
            resultat[champ] = max(difference, 0.0)
        else:
            resultat[champ] = max(int(difference), 0)
    return resultat
