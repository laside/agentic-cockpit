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
PLAN_MIN_TICKETS = 2
PLAN_MAX_TICKETS = 4


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


def _plan_limit(max_tickets: int) -> int:
    try:
        limit = int(max_tickets)
    except (TypeError, ValueError):
        limit = PLAN_MAX_TICKETS
    return max(PLAN_MIN_TICKETS, min(limit, PLAN_MAX_TICKETS))


def validate_plan(plan: Any, wt: Path, max_tickets: int = PLAN_MAX_TICKETS) -> str | None:
    """Valide un plan multi-tickets : 2..N tickets, règles du mode draft, dépendances acycliques."""
    if not isinstance(plan, dict):
        return "plan.json doit contenir un objet JSON."
    tickets = plan.get("tickets")
    if not isinstance(tickets, list):
        return "La clé `tickets` doit être une liste."
    limit = _plan_limit(max_tickets)
    if not (PLAN_MIN_TICKETS <= len(tickets) <= limit):
        return (
            f"Le plan doit contenir entre {PLAN_MIN_TICKETS} et {limit} tickets "
            f"(reçu {len(tickets)})."
        )

    for index, item in enumerate(tickets):
        refusal = validate_draft(item, wt)
        if refusal:
            return f"Ticket {index} : {refusal}"

    graph: list[list[int]] = []
    for index, item in enumerate(tickets):
        raw = item.get("depends_on")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            return f"Ticket {index} : `depends_on` doit être une liste d'indices."
        deps: list[int] = []
        for dep in raw:
            if isinstance(dep, bool) or not isinstance(dep, int):
                return f"Ticket {index} : indice de dépendance invalide ({dep!r})."
            if dep < 0 or dep >= len(tickets):
                return f"Ticket {index} : dépendance hors bornes ({dep})."
            deps.append(dep)
        graph.append(deps)

    colors = [0] * len(tickets)

    def _has_cycle(node: int) -> bool:
        if colors[node] == 1:
            return True
        if colors[node] == 2:
            return False
        colors[node] = 1
        for dep in graph[node]:
            if _has_cycle(dep):
                return True
        colors[node] = 2
        return False

    for index in range(len(tickets)):
        if _has_cycle(index):
            return "Le plan contient un cycle de dépendances entre tickets."
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


def _clean_plan_ticket(draft: dict[str, Any]) -> dict[str, Any]:
    clean = _clean_draft(draft)
    clean["depends_on"] = [int(dep) for dep in (draft.get("depends_on") or [])]
    return clean


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


def build_plan_prompt(objective: str, max_tickets: int = PLAN_MAX_TICKETS, error: str = "") -> str:
    """Construit le prompt du plan multi-tickets, en réinjectant l'erreur sur relance."""
    pipe = _pipeline()
    path = pipe.COCKPIT_HOME / "prompts" / "sys_drafter_plan.md"
    try:
        base = path.read_text(encoding="utf-8").strip()
    except OSError:
        base = (
            "Tu es un rédacteur de plans. Explore le dépôt et écris uniquement `plan.json` "
            "avec une clé tickets, chaque ticket ayant title, size, prio, files, contract, "
            "accept, depends_on (indices 0-based)."
        )
    limit = _plan_limit(max_tickets)
    prompt = (
        f"{base}\n\nObjectif : {objective.strip()}\n\n"
        f"Produis entre {PLAN_MIN_TICKETS} et {limit} tickets."
    )
    if error:
        prompt += (
            "\n\nUne tentative précédente a été refusée. Corrige uniquement ce point :\n"
            f"{error}\nRéécris entièrement plan.json puis arrête-toi."
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


def _rollback_plan(db: CockpitDB, project_id: str, ticket_ids: list[str]) -> None:
    """Supprime les tickets d'un plan dont la création a échoué en cours de route."""
    if not ticket_ids:
        return
    placeholders = ",".join("?" for _ in ticket_ids)
    with db._connect() as conn:
        conn.execute(
            f"DELETE FROM runs WHERE project_id = ? AND ticket_id IN ({placeholders})",
            [project_id, *ticket_ids],
        )
        conn.execute(
            f"DELETE FROM events WHERE project_id = ? AND ticket_id IN ({placeholders})",
            [project_id, *ticket_ids],
        )
        conn.execute(
            f"DELETE FROM tickets WHERE project_id = ? AND id IN ({placeholders})",
            [project_id, *ticket_ids],
        )


def _print_plan_summary(ids: list[str], clean: list[dict[str, Any]]) -> None:
    """Résumé compact : une ligne par ticket, le total, puis les chevauchements de fichiers."""
    pipe = _pipeline()
    for ticket_id, item in zip(ids, clean):
        deps = [ids[index] for index in item["depends_on"]]
        suffix = f"(dépend de {', '.join(deps)})" if deps else "(aucune dépendance)"
        pipe.say(f"{ticket_id} [{item['size']}/{item['prio']}] {item['title']} {suffix}")
    pipe.say(f"Total : {len(ids)} tickets DRAFT")

    file_sets = [
        {normalize_rel_path(str(path)) for path in item["files"] if normalize_rel_path(str(path))}
        for item in clean
    ]
    for left in range(len(ids)):
        for right in range(left + 1, len(ids)):
            common = sorted(file_sets[left].intersection(file_sets[right]))
            if common:
                pipe.say(
                    f"Chevauchement de fichiers : {ids[left]}/{ids[right]} → {', '.join(common)}"
                )


def plan_tickets(
    db: CockpitDB,
    project_id: str,
    objective: str,
    lane: str = DEFAULT_LANE,
    max_tickets: int = PLAN_MAX_TICKETS,
) -> list[dict[str, Any]]:
    """Fait rédiger un plan, le valide en bloc, puis crée tous les tickets DRAFT d'un coup."""
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
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = build_plan_prompt(objective, max_tickets, last_error)
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
                    f"Le worker a échoué (code {code}). Écris plan.json à la racine du répertoire courant."
                )
                continue

            plan_path = wt / "plan.json"
            if not plan_path.exists():
                last_error = "plan.json est absent. Écris ce fichier à la racine du répertoire courant."
                continue
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last_error = "plan.json n'est pas un JSON valide."
                continue
            refusal = validate_plan(plan, wt, max_tickets)
            if refusal:
                last_error = refusal
                continue

            clean = [_clean_plan_ticket(item) for item in plan["tickets"]]
            start = int(pipe.next_ticket_id(db, project_id)[2:])
            ids = [f"T-{start + index:03d}" for index in range(len(clean))]
            metrics = _metrics(runner, defaults, log_path, before, after)
            created: list[str] = []
            try:
                for index, item in enumerate(clean):
                    ticket_id = ids[index]
                    depends_on = [ids[dep] for dep in item["depends_on"]]
                    db.upsert_ticket(
                        project_id,
                        ticket_id,
                        item["title"],
                        milestone_id=None,
                        priority=item["prio"],
                        complexity=item["size"],
                        state="DRAFT",
                        owner="drafter",
                        files_owned=item["files"],
                        acceptance=item["accept"],
                        contract=item["contract"],
                        depends_on=depends_on,
                    )
                    created.append(ticket_id)
                    run_fields: dict[str, Any] = {
                        "attempt": attempt,
                        "lane": lane,
                        "provider": runner.get("provider", ""),
                        "exit_code": code,
                        "duration_s": round(duration, 1),
                        "gate0": "DRAFT",
                    }
                    run_fields.update(metrics)
                    db.record_run(project_id, ticket_id, **run_fields)
                    db.add_event(
                        project_id, ticket_id, "PLAN_CREATED", f"objectif : {objective[:200]}"
                    )
            except Exception as exc:  # création atomique : rien ne doit rester à moitié créé
                _rollback_plan(db, project_id, created)
                raise pipe.PipelineError(f"Création du plan impossible : {exc}") from exc

            _print_plan_summary(ids, clean)
            return [db.get_ticket(project_id, ticket_id) or {} for ticket_id in ids]

        raise pipe.PipelineError(f"Aucun ticket créé : {last_error or 'plan invalide'}")
    finally:
        _remove_worktree(root, wt)


def promote_ticket(db: CockpitDB, project_id: str, ticket_id: str) -> str:
    """DRAFT -> READY, ou BLOCKED si une dépendance n'est pas MERGED. Refus sinon."""
    results = promote_tickets(db, project_id, [ticket_id])
    return results[0][1]


def promote_tickets(db: CockpitDB, project_id: str, ticket_ids: list[str]) -> list[tuple[str, str]]:
    """Valide tous les ids (DRAFT) puis les promeut ; un seul refus annule toute la promotion."""
    pipe = _pipeline()
    if not db.get_project(project_id):
        raise pipe.PipelineError(f"Projet inconnu : {project_id}")
    unique: list[str] = []
    for raw in ticket_ids:
        ticket_id = (raw or "").strip()
        if ticket_id and ticket_id not in unique:
            unique.append(ticket_id)
    if not unique:
        raise pipe.PipelineError("Aucun identifiant de ticket fourni pour la promotion.")

    tickets: list[dict[str, Any]] = []
    for ticket_id in unique:
        ticket = db.get_ticket(project_id, ticket_id)
        if not ticket:
            raise pipe.PipelineError(f"Ticket inconnu : {ticket_id}")
        if ticket.get("state") != "DRAFT":
            raise pipe.PipelineError(
                f"{ticket_id} est à l'état {ticket.get('state')} (attendu : DRAFT) : promotion refusée."
            )
        tickets.append(ticket)

    results: list[tuple[str, str]] = []
    for ticket in tickets:
        ticket_id = ticket["id"]
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
        results.append((ticket_id, new_state))
    return results
