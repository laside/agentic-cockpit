#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Rédaction de tickets par un worker (T-043).

Le worker explore un worktree jetable détaché de `origin/<branche par défaut>` et écrit
`draft.json`. La validation est mécanique (0 token) : chemins, nombre de fichiers, taille,
priorité, longueur du contrat, critères d'acceptation et confidentialité. Un brouillon valide
devient un ticket `DRAFT` que `cockpit ticket promote` seul peut rendre `READY` (ou `BLOCKED`).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

try:
    import usage
    from pmo_db import CockpitDB, is_secret_or_forbidden_path, normalize_rel_path
except ImportError:  # import via le paquet `core`
    from core import usage  # type: ignore[no-redef]
    from core.pmo_db import CockpitDB, is_secret_or_forbidden_path, normalize_rel_path  # type: ignore[no-redef]

DEFAULT_LANE = "deepseek_coder"
ALLOWED_SIZES = ("XS", "S", "M", "L")
ALLOWED_PRIOS = ("P0", "P1", "P2", "P3")
MAX_FILES = 8
MAX_CONTRACT_CHARS = 1500
MAX_ATTEMPTS = 2


def _pipeline():
    """Import tardif pour éviter le cycle pipeline -> drafter (importé dans une commande)."""
    import sys

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "run_worker"):
        return main_module
    try:
        import pipeline
    except ImportError:  # import via le paquet `core`
        from core import pipeline  # type: ignore[no-redef]

    return pipeline


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _is_safe_relative(path: str) -> bool:
    raw = str(path).strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return False
    norm = normalize_rel_path(raw)
    return ".." not in norm.split("/")


def _forbidden_path_reason(path: str) -> str:
    raw = str(path).strip().replace("\\", "/")
    norm = normalize_rel_path(raw)
    if not raw or raw.startswith("/") or ".." in norm.split("/"):
        return "hors dépôt"
    if ".git" in norm.split("/"):
        return ".git interdit"
    if is_secret_or_forbidden_path(norm):
        return ".env ou fichier secret interdit"
    return ""


def validate_draft(draft: Any, wt: Path) -> str | None:
    """Valide un brouillon dans l'ordre imposé. Retourne le premier refus ou None si valide."""
    if not isinstance(draft, dict):
        return "draft.json doit contenir un objet JSON."

    title = str(draft.get("title") or "").strip()
    if not title:
        return "Le titre est vide."

    raw_files = draft.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return "La liste `files` est vide ou absente."
    files = [normalize_rel_path(str(f)) for f in raw_files]
    if any(not f for f in files):
        return "Un chemin de fichier est vide."

    # 1. Chaque fichier existe ou son dossier parent existe (sans jamais lire hors dépôt).
    for path in files:
        if not _is_safe_relative(path):
            continue
        target = wt / path
        if not target.exists() and not target.parent.exists():
            return f"Le fichier {path} n'existe pas et son dossier parent non plus."

    # 2. Aucun chemin hors dépôt, .git ou .env.
    for path in files:
        reason = _forbidden_path_reason(path)
        if reason:
            return f"Chemin interdit ({path}) : {reason}."

    # 3. Huit fichiers maximum.
    if len(files) > MAX_FILES:
        return f"Trop de fichiers : {len(files)} (maximum {MAX_FILES})."

    # 4. Taille autorisée.
    size = str(draft.get("size") or "").strip().upper()
    if size not in ALLOWED_SIZES:
        return f"Taille invalide ({size or 'absente'}) : attendu XS, S, M ou L."

    # 5. Priorité autorisée.
    prio = str(draft.get("prio") or "").strip().upper()
    if prio not in ALLOWED_PRIOS:
        return f"Priorité invalide ({prio or 'absente'}) : attendu P0, P1, P2 ou P3."

    # 6. Contrat court.
    contract = str(draft.get("contract") or "").strip()
    if len(contract) > MAX_CONTRACT_CHARS:
        return f"Contrat trop long : {len(contract)} caractères (maximum {MAX_CONTRACT_CHARS})."

    # 7. Critères d'acceptation non vides.
    accept = _normalize_list(draft.get("accept"))
    if not accept:
        return "Les critères d'acceptation sont vides."

    # 8. Confidentialité sur le titre, le contrat et l'acceptation.
    text = "\n".join([title, contract, *accept])
    hits = _pipeline().private_text_hits(text)
    if hits:
        return "Données privées détectées : " + ", ".join(hits) + "."

    return None


def _clean_draft(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(draft.get("title") or "").strip(),
        "size": str(draft.get("size") or "").strip().upper(),
        "prio": str(draft.get("prio") or "").strip().upper(),
        "files": [normalize_rel_path(str(f)) for f in draft.get("files") or []],
        "contract": str(draft.get("contract") or "").strip(),
        "accept": _normalize_list(draft.get("accept")),
    }


def _default_ref(root: Path, default_branch: str) -> str:
    pipe = _pipeline()
    for ref in (f"origin/{default_branch}", default_branch, "HEAD"):
        res = pipe.sh(["git", "rev-parse", "--verify", "--quiet", ref], root, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return ref
    raise pipe.PipelineError("Impossible de résoudre la branche de départ du brouillon.")


def _create_worktree(proj: dict[str, Any]) -> tuple[Path, Path]:
    pipe = _pipeline()
    root = Path(proj["root_path"])
    default_branch = str(proj.get("default_branch") or "main")
    ref = _default_ref(root, default_branch)
    wt_root = Path(proj["wt_tickets_dir"])
    wt_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    wt = wt_root / f"_draft-{stamp}"
    suffix = 0
    while wt.exists():
        suffix += 1
        wt = wt_root / f"_draft-{stamp}-{suffix}"
    pipe.sh(["git", "worktree", "add", "--detach", str(wt), ref], root)
    return root, wt


def _remove_worktree(root: Path, wt: Path) -> None:
    pipe = _pipeline()
    if wt.exists():
        pipe.sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    pipe.sh(["git", "worktree", "prune"], root, check=False)


def build_prompt(objective: str, error: str = "") -> str:
    """Construit le prompt du rédacteur, en réinjectant l'erreur de validation sur relance."""
    pipe = _pipeline()
    path = pipe.COCKPIT_HOME / "prompts" / "sys_drafter.md"
    try:
        base = path.read_text(encoding="utf-8").strip()
    except OSError:
        base = (
            "Tu es un rédacteur de tickets. Explore le dépôt et écris uniquement `draft.json` "
            "avec les clés title, size, prio, files, contract, accept."
        )
    prompt = f"{base}\n\nObjectif : {objective.strip()}"
    if error:
        prompt += (
            "\n\nUne tentative précédente a été refusée. Corrige uniquement ce point :\n"
            f"{error}\nRéécris entièrement draft.json puis arrête-toi."
        )
    return prompt


def _metrics(runner: dict[str, Any], defaults: dict[str, Any], log_path: Path,
             before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Métriques de consommation du run, via core/usage.py quand c'est possible."""
    metrics: dict[str, Any] = {}
    parsed = None
    if runner.get("usage_format") and log_path.exists():
        parsed = usage.parse_usage(
            str(runner["usage_format"]), log_path.read_text(encoding="utf-8", errors="replace")
        )
    if parsed is not None:
        usd_to_eur = float(defaults.get("usd_to_eur", 0.92))
        cost_usd = float(parsed["cost_usd"]) if parsed["cost_usd"] is not None else 0.0
        cost_kind = str(parsed["cost_kind"])
        metrics.update(
            tokens_in=int(parsed["tokens_in"]),
            tokens_out=int(parsed["tokens_out"]),
            tokens_cache=int(parsed["tokens_cache"]),
            cost_usd=cost_usd,
            cost_kind=cost_kind,
            cost_eur=round(cost_usd * usd_to_eur, 4) if cost_kind == "measured" else 0.0,
        )
    elif before is not None and after is not None:
        import costs

        delta = costs.delta(before, after)
        usd_to_eur = float(defaults.get("usd_to_eur", 0.92))
        metrics.update(
            tokens_in=int(delta["input_tokens"]),
            tokens_out=int(delta["output_tokens"]),
            cost_eur=round(float(delta["total_cost_usd"]) * usd_to_eur, 4),
        )
    elif runner.get("cost_source") == "estimate":
        metrics.update(
            tokens_in=int(runner.get("estimated_tokens_in", 0)),
            tokens_out=int(runner.get("estimated_tokens_out", 0)),
            cost_eur=float(runner.get("estimated_cost_eur", 0.0)),
        )
    return metrics


def _fresh_ticket_id(db: CockpitDB, project_id: str) -> str:
    pipe = _pipeline()
    ticket_id = pipe.next_ticket_id(db, project_id)
    with db._connect() as conn:
        existing = {r["id"] for r in conn.execute(
            "SELECT id FROM tickets WHERE project_id = ?", (project_id,)
        ).fetchall()}
    while ticket_id in existing:
        number = int(ticket_id[2:]) + 1
        ticket_id = f"T-{number:03d}"
    return ticket_id


def _print_summary(ticket: dict[str, Any]) -> None:
    pipe = _pipeline()
    files = json.loads(ticket.get("files_owned_json") or "[]")
    accept = json.loads(ticket.get("acceptance_json") or "[]")
    contract = str(ticket.get("contract") or "")
    pipe.say(f"Ticket {ticket['id']} créé à l'état DRAFT")
    pipe.say(f"  titre    : {ticket['title']}")
    pipe.say(f"  taille   : {ticket['complexity']}  priorité : {ticket['priority']}")
    pipe.say(f"  fichiers : {', '.join(files)}")
    for item in accept[:5]:
        pipe.say(f"  accept   : {item}")
    if len(accept) > 5:
        pipe.say(f"  accept   : (+{len(accept) - 5} autre(s))")
    pipe.say(f"  contrat  : {contract[:300]}")


def draft_ticket(
    db: CockpitDB,
    project_id: str,
    objective: str,
    lane: str = DEFAULT_LANE,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Fait rédiger un brouillon, le valide, puis crée un ticket DRAFT. Aucun ticket si échec."""
    pipe = _pipeline()
    proj = db.get_project(project_id)
    if not proj:
        raise pipe.PipelineError(f"Projet inconnu : {project_id}")
    runners_cfg = pipe.load_runners()
    runner = runners_cfg.get("runners", {}).get(lane)
    if not runner:
        raise pipe.PipelineError(f"Voie '{lane}' absente de config/runners.toml.")
    argv_template = runner.get("argv") or []
    if not argv_template:
        raise pipe.PipelineError(f"Voie '{lane}' sans argv dans config/runners.toml.")
    defaults = runners_cfg.get("defaults", {})
    timeout = int(runner.get("timeout_s", defaults.get("timeout_s", 900)))

    root, wt = _create_worktree(proj)
    last_error = ""
    try:
        for attempt in range(1, max(1, max_attempts) + 1):
            prompt = build_prompt(objective, last_error)
            argv = [str(a).replace("{prompt}", prompt) for a in argv_template]
            log_path = pipe.COCKPIT_HOME / "logs" / project_id / wt.name / f"attempt-{attempt}.log"
            extra_env = {
                k: str(v).replace("{cockpit}", str(pipe.COCKPIT_HOME))
                for k, v in (runner.get("env") or {}).items()
            }
            import costs

            use_stats = runner.get("cost_source") == "opencode_stats" and not runner.get("usage_format")
            before = costs.snapshot_opencode_stats() if use_stats else None
            code, duration = pipe.run_worker(argv, wt, log_path, timeout, extra_env)
            after = costs.snapshot_opencode_stats() if before is not None else None

            if code != 0:
                last_error = (
                    f"Le worker a échoué (code {code}). Écris draft.json à la racine du répertoire courant."
                )
                continue

            draft_path = wt / "draft.json"
            if not draft_path.exists():
                last_error = "draft.json est absent. Écris ce fichier à la racine du répertoire courant."
                continue
            try:
                draft = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last_error = "draft.json n'est pas un JSON valide."
                continue
            refusal = validate_draft(draft, wt)
            if refusal:
                last_error = refusal
                continue

            clean = _clean_draft(draft)
            ticket_id = _fresh_ticket_id(db, project_id)
            db.upsert_ticket(
                project_id,
                ticket_id,
                clean["title"],
                milestone_id=None,
                priority=clean["prio"],
                complexity=clean["size"],
                state="DRAFT",
                owner="drafter",
                files_owned=clean["files"],
                acceptance=clean["accept"],
                contract=clean["contract"],
            )
            run_fields: dict[str, Any] = {
                "attempt": attempt,
                "lane": lane,
                "provider": runner.get("provider", ""),
                "exit_code": code,
                "duration_s": round(duration, 1),
                "gate0": "DRAFT",
            }
            run_fields.update(_metrics(runner, defaults, log_path, before, after))
            db.record_run(project_id, ticket_id, **run_fields)
            db.add_event(project_id, ticket_id, "DRAFT_CREATED", f"objectif : {objective[:200]}")
            ticket = db.get_ticket(project_id, ticket_id) or {}
            _print_summary(ticket)
            return ticket

        raise pipe.PipelineError(f"Aucun ticket créé : {last_error or 'brouillon invalide'}")
    finally:
        _remove_worktree(root, wt)


def promote_ticket(db: CockpitDB, project_id: str, ticket_id: str) -> str:
    """DRAFT -> READY, ou BLOCKED si une dépendance n'est pas MERGED. Refus sinon."""
    pipe = _pipeline()
    if not db.get_project(project_id):
        raise pipe.PipelineError(f"Projet inconnu : {project_id}")
    ticket = db.get_ticket(project_id, ticket_id)
    if not ticket:
        raise pipe.PipelineError(f"Ticket inconnu : {ticket_id}")
    if ticket.get("state") != "DRAFT":
        raise pipe.PipelineError(
            f"{ticket_id} est à l'état {ticket.get('state')} (attendu : DRAFT) : promotion refusée."
        )
    depends = json.loads(ticket.get("depends_on_json") or "[]")
    new_state = "READY"
    if depends:
        with db._connect() as conn:
            placeholders = ",".join("?" for _ in depends)
            rows = conn.execute(
                f"SELECT id, state FROM tickets WHERE project_id = ? AND id IN ({placeholders})",
                [project_id, *depends],
            ).fetchall()
        merged = {r["id"] for r in rows if r["state"] == "MERGED"}
        if any(dep not in merged for dep in depends):
            new_state = "BLOCKED"
    db.update_ticket(project_id, ticket_id, state=new_state)
    db.add_event(project_id, ticket_id, "PROMOTED", f"{ticket_id} : DRAFT -> {new_state}")
    pipe.say(f"{ticket_id} : DRAFT -> {new_state}")
    return new_state
