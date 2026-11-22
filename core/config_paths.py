#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Résolution des chemins de configuration (surcouche hors dépôt).

Ordre de résolution pour `config_file(name, cockpit_home)` :
1. `config_dir()/name` s'il existe ;
2. `<cockpit_home>/config/name` s'il existe (compatibilité) ;
3. `<cockpit_home>/config/<stem>.example<suffix>`.
"""
from __future__ import annotations

import os
from pathlib import Path

def config_dir() -> Path:
    """Retourne `$COCKPIT_CONFIG_DIR` s'il est défini, sinon `~/.config/agentic-cockpit`."""
    raw = os.environ.get("COCKPIT_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "agentic-cockpit"


def _default_cockpit_home() -> Path:
    raw = os.environ.get("COCKPIT_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "agentic-cockpit"


def config_file(name: str, cockpit_home: Path | None = None) -> Path:
    """Résout un fichier de configuration depuis la surcouche, puis le dépôt, puis l'exemple."""
    home = Path(cockpit_home).expanduser() if cockpit_home is not None else _default_cockpit_home()

    overlay_candidate = config_dir() / name
    if overlay_candidate.exists():
        return overlay_candidate

    repo_candidate = home / "config" / name
    if repo_candidate.exists():
        return repo_candidate

    rel = Path(name)
    example_name = f"{rel.stem}.example{rel.suffix}"
    return home / "config" / rel.parent / example_name


def forbidden_words_path(projet: str | None = None) -> Path:
    """Liste de mots interdits : celle du projet si elle existe, sinon la liste générale."""
    if projet:
        specifique = config_dir() / f"forbidden-words.{projet}.txt"
        if specifique.is_file():
            return specifique
    return config_dir() / "forbidden-words.txt"
