#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Gatekeeper (Porte 0 Déterministe + Tamis de Logs + Porte 1 NAS Gemma4-ctx:32k).
Garantit 0 token gaspillé sur les vérifications Git, les fuites de secrets et les logs de test.
"""
from __future__ import annotations

import re
from typing import Any

from pmo_db import is_secret_or_forbidden_path


def sieve_test_output(raw_output: str, max_lines: int = 25) -> str:
    """
    Tamis de logs (Log Sieve) :
    Supprime les centaines de lignes de tests réussis et n'extrait que les blocs d'erreur/stacktrace.
    """
    lines = raw_output.splitlines()
    if len(lines) <= max_lines:
        return raw_output.strip()

    error_indices: list[int] = []
    patterns = (r"FAILED", r"ERROR", r"AssertionError", r"TypeError", r"ValueError", r"error:", r"E   ")
    for idx, line in enumerate(lines):
        if any(re.search(p, line) for p in patterns):
            error_indices.append(idx)

    if not error_indices:
        return "\n".join(lines[-max_lines:]).strip()

    selected: list[str] = []
    seen: set[int] = set()
    for center in error_indices:
        for i in range(max(0, center - 2), min(len(lines), center + 4)):
            if i not in seen:
                seen.add(i)
                selected.append(lines[i])
        if len(selected) >= max_lines:
            break

    return "\n".join(selected[:max_lines]).strip()


def verify_worktree_scope(
    modified_files: list[str],
    allowed_files: list[str],
) -> tuple[bool, list[str], list[str]]:
    """
    Porte 0 (Périmètre & Sécurité) :
    Vérifie qu'aucun fichier hors `files_owned` ni aucun fichier secret (.env, secrets/) n'a été touché.
    """
    unauthorized: list[str] = []
    secret_violations: list[str] = []
    allowed_set = {f.strip().removeprefix("./") for f in allowed_files}

    for mod in modified_files:
        norm = mod.strip().removeprefix("./")
        if is_secret_or_forbidden_path(norm):
            secret_violations.append(norm)
        elif allowed_set and norm not in allowed_set:
            matched = False
            for pat in allowed_set:
                if "*" in pat:
                    regex = "^" + re.escape(pat).replace(r"\*", ".*") + "$"
                    if re.match(regex, norm):
                        matched = True
                        break
            if not matched:
                unauthorized.append(norm)

    ok = (len(unauthorized) == 0) and (len(secret_violations) == 0)
    return ok, unauthorized, secret_violations


def build_nas_gate1_payload(
    ticket_brief: str,
    git_diff: str,
    system_prompt: str,
    model: str = "Gemma4-ctx:32k",
) -> dict[str, Any]:
    """
    Construit la requête Ollama pour la GTX 1080 Ti (192.168.0.10:11434) avec :
    - KV Cache épinglé (`keep_alive: -1`)
    - Fenêtre de 32 768 tokens validée à 208 tok/s
    - Sortie JSON stricte
    """
    prompt = (
        f"{system_prompt.strip()}\n\n"
        f"=== BRIEF DU TICKET ===\n{ticket_brief.strip()}\n\n"
        f"=== DIFF GIT À AUDITER ===\n{git_diff[:45000].strip()}\n"
    )
    return {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "keep_alive": -1,
        "options": {
            "num_ctx": 32768,
            "temperature": 0.0,
        },
    }


_TEST_EXTENSIONS = (".test.", ".spec.")
_SOURCE_EXTENSIONS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".sh",
    ".go",
    ".rs",
    ".java",
    ".rb",
)


def is_test_file(path: str) -> bool:
    """Vrai si le chemin désigne un fichier de test (dossier tests/ ou nom explicite)."""
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith(("tests/", "test/")):
        return True
    name = normalized.rsplit("/", 1)[-1]
    if name.startswith("test_"):
        return True
    return any(marker in name for marker in _TEST_EXTENSIONS)


def is_source_file(path: str) -> bool:
    """Vrai si le chemin est un fichier source (extension connue et pas un test)."""
    normalized = path.strip().replace("\\", "/")
    if is_test_file(normalized):
        return False
    return normalized.lower().endswith(_SOURCE_EXTENSIONS)


def check_tests_present(
    changed_files: list[str],
    files_owned: list[str],
) -> tuple[bool, str]:
    """
    Porte 1 statique (0 token) :
    Un diff qui touche du code source doit aussi toucher un test.
    """
    if not any(is_source_file(f) for f in files_owned):
        return True, ""
    if any(is_test_file(f) for f in changed_files):
        return True, ""
    message = (
        "Aucun test n'a été ajouté ou modifié. "
        "Écris le test d'acceptation avant le code."
    )
    return False, message
