#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Compilateur de Contexte Déterministe (0 token LLM).
Remplace la lecture manuelle de AGENTS.md (12 Ko), backlog.md (229 Ko) et coordination.md (22 Ko)
par un fichier unique `.nexus_brief.md` (< 4 Ko) ordonné pour le Prompt Caching (-90% coût Cloud & KV Cache q8_0).
Extrait uniquement les signatures AST Python/TypeScript des fichiers en lecture seule.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


def extract_python_ast_skeleton(source_code: str) -> str:
    """Extrait classes, signatures de fonctions et docstrings courtes sans le corps d'implémentation."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return "# [Erreur de syntaxe lors de l'extraction AST]"

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            lines.append(f"class {node.name}{base_str}:")
            doc = ast.get_docstring(node)
            if doc:
                first_line = doc.strip().splitlines()[0]
                lines.append(f'    """{first_line}"""')
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                    ret = f" -> {ast.unparse(sub.returns)}" if sub.returns else ""
                    lines.append(f"    {prefix} {sub.name}({ast.unparse(sub.args)}){ret}: ...")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            lines.append(f"{prefix} {node.name}({ast.unparse(node.args)}){ret}: ...")
    return "\n".join(lines) if lines else "# [Aucune interface publique détectée]"


def compile_ticket_brief(
    project_root: Path,
    ticket: dict[str, Any],
    rules_dir: Path,
    readonly_files: list[str] | None = None,
) -> str:
    """
    Construit le brief selon l'ordre canonique de Prompt Caching :
    1. Bloc Statique Projet (Règles modulaires immuables)
    2. Squelettes AST des dépendances (Lecture seule)
    3. Spécification dynamique du Ticket (Périmètre strict + Critères TDD)
    """
    files_owned = json.loads(ticket.get("files_owned_json", "[]"))
    acceptance = json.loads(ticket.get("acceptance_json", "[]"))

    sections: list[str] = [
        "<!-- BLOC 1 : PRÉFIXE STATIQUE CACHEABLE (NE PAS MODIFIER) -->",
        "# RÈGLES D'INGÉNIERIE & INVARIANTS DU PROJET",
    ]

    if rules_dir.exists():
        core_rule = rules_dir / "core.md"
        if core_rule.exists():
            sections.append(core_rule.read_text(encoding="utf-8").strip())
        touches_backend = any(f.startswith("backend/") for f in files_owned)
        touches_frontend = any(f.startswith("frontend/") for f in files_owned)
        if touches_backend and (rules_dir / "data_privacy.md").exists():
            sections.append((rules_dir / "data_privacy.md").read_text(encoding="utf-8").strip())
        if touches_frontend and (rules_dir / "frontend_ui.md").exists():
            sections.append((rules_dir / "frontend_ui.md").read_text(encoding="utf-8").strip())

    if readonly_files:
        sections.append("\n<!-- BLOC 2 : CONTRATS D'INTERFACES (AST LECTURE SEULE) -->")
        for rel in readonly_files:
            fp = project_root / rel
            if fp.exists() and fp.suffix == ".py":
                skel = extract_python_ast_skeleton(fp.read_text(encoding="utf-8", errors="replace"))
                sections.append(f"## Interface `{rel}`\n```python\n{skel}\n```")
            elif fp.exists() and fp.is_file():
                head = fp.read_text(encoding="utf-8", errors="replace")[:1500]
                sections.append(f"## Extrait `{rel}` (lecture seule, tronqué)\n```\n{head}\n```")

    sections.extend([
        "\n<!-- BLOC 3 : SPÉCIFICATION ATOMIQUE DU TICKET -->",
        f"# TICKET {ticket['id']} — {ticket['title']}",
        f"- **Jalon** : `{ticket.get('milestone_id') or 'Hors roadmap'}`",
        f"- **Priorité / Taille** : `{ticket.get('priority', 'P2')}` / `[{ticket.get('complexity', 'M')}]`",
        "",
        "## Périmètre d'écriture strict (`FILES_OWNED`)",
        "Tu n'es autorisé à créer ou modifier QUE les fichiers suivants :",
    ])
    for f in files_owned:
        sections.append(f"- `{f}`")

    sections.append("\n## Critères d'acceptation (TDD : Tests Rouges -> Code Minimal -> Tests Verts)")
    for idx, crit in enumerate(acceptance, start=1):
        sections.append(f"{idx}. {crit}")

    contract = (ticket.get("contract") or "").strip()
    if contract:
        sections.append("\n## Contrat d'interface (partagé avec d'autres tickets : à respecter à la lettre)")
        sections.append(
            "Les noms de champs, routes, types et codes ci-dessous sont imposés. "
            "Ne les renomme pas, ne les traduis pas, n'en invente pas d'autres."
        )
        sections.append(contract)

    test_cmd = (ticket.get("test_cmd") or "").strip()
    if test_cmd:
        sections.append("\n## Commande de validation (exécutée par la Porte 0)")
        sections.append(f"`{test_cmd}`")

    return "\n".join(sections) + "\n"
