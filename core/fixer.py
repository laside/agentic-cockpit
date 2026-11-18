#!/usr/bin/env python3
"""
Sprint Fixer : corrections lint/typage proposées par un modèle local Ollama.
Module non branché : aucun appel réseau au chargement, stdlib uniquement.
"""
from __future__ import annotations

import ast
import json
import re
import urllib.request
from pathlib import Path

URL_OLLAMA = "http://127.0.0.1:11434/api/generate"
DELAI_TIMEOUT = 60

MOTIF_RUFF = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)$"
)
MOTIF_MYPY = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\s*error:\s*(?P<message>.+)$"
)
MOTIF_BLOC = re.compile(r"```[^\n]*\n(?P<code>.*?)```", re.DOTALL)


def extract_lint_errors(output: str) -> list[dict]:
    """Extrait les erreurs ruff et mypy ; ignore silencieusement les autres lignes."""
    erreurs: list[dict] = []
    if not output:
        return erreurs

    for ligne in output.splitlines():
        correspondance = MOTIF_RUFF.match(ligne)
        if correspondance:
            erreurs.append(
                {
                    "path": correspondance.group("path"),
                    "line": int(correspondance.group("line")),
                    "message": correspondance.group("message").strip(),
                }
            )
            continue

        correspondance = MOTIF_MYPY.match(ligne)
        if correspondance:
            erreurs.append(
                {
                    "path": correspondance.group("path"),
                    "line": int(correspondance.group("line")),
                    "message": correspondance.group("message").strip(),
                }
            )

    return erreurs


def build_fixer_payload(
    source: str,
    errors: list[dict],
    system_prompt: str,
    model: str = "qwen2.5-coder-16k",
    num_ctx: int = 16384,
) -> dict:
    """Construit la charge utile Ollama /api/generate."""
    morceaux = [system_prompt]
    if errors:
        morceaux.append("Erreurs à corriger :")
        for erreur in errors:
            morceaux.append(
                "- {path}:{line}: {message}".format(
                    path=erreur.get("path", ""),
                    line=erreur.get("line", 0),
                    message=erreur.get("message", ""),
                )
            )
    morceaux.append("Source :")
    morceaux.append(source)

    return {
        "model": model,
        "prompt": "\n".join(morceaux),
        "stream": False,
        "keep_alive": "10m",
        "options": {"num_ctx": num_ctx, "temperature": 0.0},
    }


def apply_fix(response_text: str, source: str) -> str | None:
    """Extrait le premier bloc de code valide, ou None si le correctif est inutilisable."""
    if not response_text:
        return None

    correspondance = MOTIF_BLOC.search(response_text)
    if not correspondance:
        return None

    code = correspondance.group("code").strip()
    if not code:
        return None

    try:
        ast.parse(code)
    except SyntaxError:
        return None

    if abs(len(code) - len(source)) > 0.5 * len(source):
        return None

    return code


def _call_ollama(payload: dict) -> str:
    """POST réel vers Ollama ; lève une exception réseau en cas d'échec."""
    requete = urllib.request.Request(
        URL_OLLAMA,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(requete, timeout=DELAI_TIMEOUT) as reponse:
        donnees = json.loads(reponse.read().decode("utf-8"))
    return donnees.get("response", "")


def fix_file(
    path,
    errors,
    call=None,
    system_prompt: str = "",
) -> bool:
    """Réécrit le fichier si le correctif est valide ; False sinon, sans le modifier."""
    chemin = Path(path)
    source = chemin.read_text(encoding="utf-8")
    payload = build_fixer_payload(source, errors, system_prompt)
    appel = call if call is not None else _call_ollama

    try:
        reponse = appel(payload)
    except Exception:
        return False

    if not isinstance(reponse, str):
        return False

    corrige = apply_fix(reponse, source)
    if corrige is None:
        return False

    chemin.write_text(corrige, encoding="utf-8")
    return True
