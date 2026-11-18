#!/usr/bin/env python3
"""
Agentic Cockpit V2 — Pipeline d'exécution d'un ticket (0 token hors worker et Porte 1 locale).

    worktree -> brief compilé -> worker headless -> Porte 0 (périmètre, secrets, lint, tests)
             -> Porte 1 (audit du diff par le NAS) -> commit -> VALIDATED (ou GATE2)

puis, sur décision humaine ou de l'architecte :

    merge : intégration de la branche par défaut dans le ticket, re-test, fusion, nettoyage, roadmap.

Tout ce qui est déterministe (git, verrous, tests, tri des logs, états) est fait ici, sans LLM.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parent
COCKPIT_HOME = Path(os.environ.get("COCKPIT_HOME", CORE.parent))
sys.path.insert(0, str(CORE))

import config_paths  # noqa: E402
import costs  # noqa: E402
import judge  # noqa: E402
import judge_calibrate  # noqa: E402
import scheduler  # noqa: E402
import usage  # noqa: E402
from context_compiler import compile_ticket_brief  # noqa: E402
from gatekeeper import (  # noqa: E402
    build_nas_gate1_payload,
    check_tests_present,
    sieve_test_output,
    verify_worktree_scope,
)
from pmo_db import CockpitDB, normalize_rel_path  # noqa: E402

DB_PATH = Path(os.environ.get("DB_PATH", COCKPIT_HOME / "db" / "cockpit_pmo.db"))
BRIEF_NAME = ".nexus_brief.md"
RUNNABLE_STATES = {"READY", "ASSIGNED", "GATE0", "GATE1"}
ACTIVE_STATES = {"ASSIGNED", "GATE0", "GATE1", "GATE2", "VALIDATED"}
INTERACTIVE_OWNERS = ("claude", "agy", "deepseek")


class PipelineError(Exception):
    """Erreur bloquante du pipeline : le message est affiché tel quel."""


# ───────────────────────────── utilitaires ─────────────────────────────


def say(msg: str) -> None:
    print(f"[cockpit] {msg}", flush=True)


def sh(args: list[str], cwd: Path, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise PipelineError(f"Commande en échec ({' '.join(args)}) : {(res.stderr or res.stdout).strip()[:400]}")
    return res


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_runners() -> dict[str, Any]:
    path = config_paths.config_file("runners.toml", COCKPIT_HOME)
    cfg = load_toml(path)
    if "runners" not in cfg:
        raise PipelineError(f"{path} introuvable ou sans section [runners].")
    return cfg


def project_commands(project_id: str, root: Path, workdir: Path) -> tuple[str, str]:
    """Retourne (lint_cmd, test_cmd) : .nexus/project.toml du projet, puis projects.toml, puis make."""
    local = load_toml(workdir / ".nexus" / "project.toml") or load_toml(root / ".nexus" / "project.toml")
    central = load_toml(config_paths.config_file("projects.toml", COCKPIT_HOME)).get("projects", {}).get(project_id, {})
    has_make = (workdir / "Makefile").exists()
    lint = local.get("lint_cmd") or central.get("lint_cmd") or ("make lint" if has_make else "")
    test = local.get("test_cmd") or central.get("test_unit_cmd") or ("make test" if has_make else "")
    return lint, test


def run_shell(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        res = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, executable="/bin/bash"
        )
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT après {timeout}s : {cmd}"


def changed_files(wt: Path, base: str) -> list[str]:
    """Fichiers touchés depuis `base` : commits du worker + modifications non commitées + non suivis."""
    files: set[str] = set()
    committed = sh(["git", "diff", "--name-only", f"{base}..HEAD"], wt).stdout.splitlines()
    files.update(f.strip() for f in committed if f.strip())
    status = sh(["git", "status", "--porcelain", "--untracked-files=all"], wt).stdout.splitlines()
    for line in status:
        path = line[3:].strip()
        if " -> " in path:
            old, path = path.split(" -> ", 1)
            files.add(old.strip().strip('"'))
        files.add(path.strip('"'))
    files.discard(BRIEF_NAME)
    files.discard(".agents")
    return sorted(files)


# ───────────────────────────── worktree & brief ─────────────────────────────

_GIT_WORKTREE_LOCK = threading.Lock()


def private_text_hits(text: str) -> list[str]:
    """Numéros de ligne des motifs de la liste privée (forbidden-words.txt) présents dans le texte."""
    path = config_paths.config_dir() / "forbidden-words.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    hits = []
    for idx, raw in enumerate(lines, start=1):
        pat = raw.strip()
        if pat and not pat.startswith("#") and re.search(pat, text, re.IGNORECASE):
            hits.append(f"mot-interdit#{idx}")
    return hits


ALLOWED_COMMIT_EMAIL_DOMAINS = frozenset(
    {"users.noreply.github.com", "noreply.github.com", "example.com", "example.org", "localhost", "anthropic.com"}
)
ALLOWED_COMMIT_EMAILS = frozenset({"noreply@github.com"})


def commit_identity_leaks(repo: Path, ref: str) -> list[str]:
    """Champs auteur/committer du commit `ref` dont l'e-mail n'est pas autorisé ou qui citent un motif privé."""
    out = sh(["git", "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", ref], repo, check=False).stdout.strip()
    parts = out.split("\x00")
    if len(parts) != 4:
        return []
    leaks = []
    for label, value in zip(("author", "author-email", "committer", "committer-email"), parts):
        if label.endswith("email") and (
            value.lower() not in ALLOWED_COMMIT_EMAILS
            and value.rsplit("@", 1)[-1].lower() not in ALLOWED_COMMIT_EMAIL_DOMAINS
        ):
            leaks.append(f"{label}: email-non-autorise")
        leaks += [f"{label}: {h}" for h in private_text_hits(value)]
    return leaks


def stage_paths(wt: Path, paths: list[str]) -> None:
    """Indexe ajouts, modifications et suppressions ; tolère une suppression déjà indexée (`git rm`)."""
    present = [p for p in paths if (wt / p).exists()]
    missing = [p for p in paths if not (wt / p).exists()]
    if present:
        sh(["git", "add", "-A", "--", *present], wt)
    if missing:
        sh(["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", *missing], wt)


def ensure_worktree(proj: dict[str, Any], ticket_id: str, parent_id: str = "") -> Path:
    root = Path(proj["root_path"])
    wt = Path(proj["wt_tickets_dir"]) / ticket_id
    branch = f"feat/{ticket_id}"
    parent_id = (parent_id or "").strip()
    with _GIT_WORKTREE_LOCK:
        if wt.exists():
            return wt
        wt.parent.mkdir(parents=True, exist_ok=True)
        if parent_id:
            parent_branch = f"feat/{parent_id}"
            has_parent = (
                sh(["git", "rev-parse", "--verify", "--quiet", parent_branch], root, check=False).returncode == 0
            )
            if not has_parent:
                raise PipelineError(
                    f"le chantier {parent_id} doit d'abord être pris (cockpit claim) ou exécuté"
                )
        has_branch = sh(["git", "rev-parse", "--verify", "--quiet", branch], root, check=False).returncode == 0
        if has_branch:
            args = ["git", "worktree", "add", str(wt), branch]
        elif parent_id:
            args = ["git", "worktree", "add", "-b", branch, str(wt), f"feat/{parent_id}"]
        else:
            args = ["git", "worktree", "add", str(wt), "-b", branch]
        sh(args, root)
        agents = root / ".agents"
        if agents.exists() and not (wt / ".agents").exists():
            (wt / ".agents").symlink_to(agents)
        # Le brief ne doit jamais être commité ni compté comme modification du worker.
        exclude = root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [p for p in (BRIEF_NAME, ".agents") if p not in current.splitlines()]
        if missing:
            exclude.write_text(current.rstrip("\n") + "\n" + "\n".join(missing) + "\n", encoding="utf-8")
        return wt


def _build_neighbors_section(db: CockpitDB, ticket: dict[str, Any]) -> str:
    """Construit la section « Travaux voisins » (0 token) ou chaîne vide s'il n'y a aucun voisin."""
    project_id = str(ticket.get("project_id") or "")
    cur_id = str(ticket.get("id") or "")
    cur_parent = (ticket.get("parent_id") or "").strip()
    cur_readonly = {
        normalize_rel_path(f)
        for f in json.loads(ticket.get("readonly_json") or "[]")
        if normalize_rel_path(f)
    }

    neighbors: list[dict[str, Any]] = []
    for cand in db.get_prioritized_queue(project_id):
        cid = str(cand.get("id") or "")
        if not cid or cid == cur_id or cand.get("state") not in ACTIVE_STATES:
            continue
        cand_parent = (cand.get("parent_id") or "").strip()
        is_sibling = bool(cur_parent and cand_parent == cur_parent)
        is_parent = bool(cur_parent and cid == cur_parent)
        cand_files = [
            normalize_rel_path(f)
            for f in json.loads(cand.get("files_owned_json") or "[]")
            if normalize_rel_path(f)
        ]
        touches_readonly = bool(cur_readonly and cur_readonly.intersection(cand_files))
        if is_sibling or is_parent or touches_readonly:
            neighbors.append(cand)
            if len(neighbors) >= 8:
                break

    if not neighbors:
        return ""

    lines = ["", "## Travaux voisins (ne pas modifier leurs fichiers)"]
    for n in neighbors:
        n_files = [
            normalize_rel_path(f)
            for f in json.loads(n.get("files_owned_json") or "[]")
            if normalize_rel_path(f)
        ]
        files_str = ", ".join(n_files[:4]) + (f", +{len(n_files) - 4}" if len(n_files) > 4 else "")
        owner = n.get("owner") or "auto"
        title = (n.get("title") or "")[:60]
        lines.append(f"- {n['id']} {n['state']} {owner} {title} ({files_str})")

    neighbor_ids = [n["id"] for n in neighbors]
    placeholders = ",".join("?" for _ in neighbor_ids)
    with db._connect() as conn:
        note_rows = conn.execute(
            f"SELECT ticket_id, detail, created_at FROM events "
            f"WHERE project_id = ? AND kind = 'NOTE' AND ticket_id IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 3",
            [project_id, *neighbor_ids],
        ).fetchall()
    for row in reversed(note_rows):
        lines.append(f"- NOTE {row['ticket_id']} : {(row['detail'] or '')[:200]}")

    return "\n".join(lines) + "\n"


def write_brief(ticket: dict[str, Any], wt: Path, db: CockpitDB | None = None) -> Path:
    readonly = json.loads(ticket.get("readonly_json") or "[]")
    brief = compile_ticket_brief(wt, ticket, wt / ".nexus" / "rules", readonly or None)
    if db is not None:
        brief += _build_neighbors_section(db, ticket)
    path = wt / BRIEF_NAME
    path.write_text(brief, encoding="utf-8")
    return path


# ───────────────────────────── worker headless ─────────────────────────────


def worker_prompt(feedback: str) -> str:
    base = (COCKPIT_HOME / "prompts" / "sys_coder_tdd.md").read_text(encoding="utf-8").strip()
    rules = (
        " Lis uniquement ./.nexus_brief.md (chemin relatif). N'accède à aucun chemin hors du répertoire courant."
        " Crée ou modifie uniquement les fichiers de FILES_OWNED. Ne commite pas, ne pousse pas."
    )
    if feedback:
        return (
            f"{base}{rules}\n\nUne tentative précédente a été refusée par les portes de qualité."
            f" Corrige uniquement ce qui suit, sans rien réécrire d'autre :\n{feedback}"
        )
    return base + rules


def run_worker(argv: list[str], wt: Path, log_path: Path, timeout: int,
               extra_env: dict[str, str] | None = None) -> tuple[int, float]:
    """Lance le worker, recopie sa sortie à l'écran et dans le journal, borne sa durée."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which(argv[0]) is None:
        raise PipelineError(f"CLI introuvable pour cette voie : {argv[0]}")
    start = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        # PWD doit suivre cwd : certains CLI (opencode) se fient à la variable et non au répertoire réel.
        proc = subprocess.Popen(
            argv, cwd=str(wt), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", env=dict(os.environ, PWD=str(wt), **(extra_env or {})),
        )

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = 124
            log.write(f"\n[cockpit] worker arrêté après {timeout}s\n")
        reader.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
    return code, time.time() - start


# ───────────────────────────── portes de qualité ─────────────────────────────


def gate0(ticket: dict[str, Any], proj: dict[str, Any], wt: Path, base: str, timeout: int,
          artefacts: set[str] | None = None) -> tuple[str, str, list[str]]:
    """Retourne (verdict, retour_pour_le_worker, fichiers_modifiés). Verdicts : PASS | FAIL | SECRET.

    `artefacts` mémorise les fichiers créés par lint/tests eux-mêmes (caches, rapports) : ils ne sont
    pas imputés au worker lors des tentatives suivantes.
    """
    artefacts = artefacts if artefacts is not None else set()
    allowed = json.loads(ticket.get("files_owned_json") or "[]")
    changed = [f for f in changed_files(wt, base) if f not in artefacts]
    if not changed:
        return "FAIL", "Aucun fichier n'a été créé ni modifié. Implémente les fichiers de FILES_OWNED.", changed
    ok, unauthorized, secrets = verify_worktree_scope(changed, allowed)
    if secrets:
        return "SECRET", "Fichiers secrets touchés : " + ", ".join(secrets), changed
    if not ok:
        return (
            "FAIL",
            "Fichiers hors périmètre modifiés : " + ", ".join(unauthorized)
            + ". Annule ces modifications (supprime les fichiers créés, restaure les autres)"
            " et ne touche qu'à FILES_OWNED : " + ", ".join(allowed),
            changed,
        )
    lint_cmd, default_test = project_commands(ticket["project_id"], Path(proj["root_path"]), wt)
    test_cmd = (ticket.get("test_cmd") or "").strip() or default_test
    for label, cmd in (("lint", lint_cmd), ("tests", test_cmd)):
        if not cmd:
            continue
        code, out = run_shell(cmd, wt, timeout)
        artefacts.update(set(changed_files(wt, base)) - set(changed))
        if code != 0:
            return "FAIL", f"Échec {label} (`{cmd}`, code {code}) :\n{sieve_test_output(out, 25)}", changed
    return "PASS", "", changed


def gate1(ticket_brief: str, diff: str, cfg: dict[str, Any], timeout: int) -> tuple[str, list[str], dict[str, Any]]:
    """Audit du diff par le modèle local ou Claude. Verdicts : PASS | FAIL | SKIPPED (NAS injoignable ou réponse illisible)."""
    if cfg.get("backend") == "claude":
        return judge.judge_diff(ticket_brief, diff, cfg, int(cfg.get("judge_timeout_s", 120)))
    system = (COCKPIT_HOME / "prompts" / "sys_gate1_nas32k.md").read_text(encoding="utf-8")
    payload = build_nas_gate1_payload(ticket_brief, diff, system, model=cfg.get("model", "Gemma4-ctx:32k"))
    # Sans réflexion, l'audit répond en quelques secondes ; avec, il peut dépasser plusieurs minutes.
    payload["think"] = bool(cfg.get("think", False))
    req = urllib.request.Request(
        cfg.get("url", "http://127.0.0.1:11434").rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        verdict_obj = json.loads(raw.get("response", "") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "SKIPPED", [f"Porte 1 indisponible : {exc}"], {}
    if not isinstance(verdict_obj, dict):
        return "SKIPPED", ["Porte 1 : réponse JSON inattendue"], {}
    verdict = str(verdict_obj.get("verdict", "")).upper()
    defects = [str(d) for d in (verdict_obj.get("defects") or [])]
    if verdict == "PASS":
        return "PASS", defects, verdict_obj
    if verdict == "FAIL":
        return "FAIL", defects or ["Verdict FAIL sans détail"], verdict_obj
    return "SKIPPED", ["Porte 1 : verdict absent ou illisible"], verdict_obj


# ───────────────────────────── exécution d'un ticket ─────────────────────────────


def _post_worker_gates(
    db: CockpitDB,
    proj: dict[str, Any],
    ticket: dict[str, Any],
    wt: Path,
    base: str,
    log_dir: Path,
    cfg: dict[str, Any],
    run_fields: dict[str, Any],
    artefacts: set[str],
    *,
    use_gate1: bool = False,
    allow_gate1_rework: bool = False,
    worker_failed: bool = False,
    success_event: str | None = None,
    keep_assigned_on_fail: bool = False,
) -> tuple[str, str]:
    """Partie « après worker » : Porte 0, Porte 1 statique (require_tests), indexation, Porte 1 NAS, commit, GATE2."""
    project_id = ticket["project_id"]
    ticket_id = ticket["id"]
    defaults = cfg.get("defaults", {})
    files_owned = json.loads(ticket.get("files_owned_json") or "[]")
    lane = str(run_fields.get("lane", ""))
    attempt = int(run_fields.get("attempt", 1))

    db.update_ticket(project_id, ticket_id, state="GATE0")
    g0, g0_feedback, changed = gate0(
        ticket, proj, wt, base, int(defaults.get("gate_timeout_s", 600)), artefacts
    )
    run_fields["gate0"] = g0

    if g0 == "SECRET":
        db.record_run(project_id, ticket_id, **run_fields)
        db.update_ticket(project_id, ticket_id, state="GATE2")
        db.add_event(project_id, ticket_id, "GATE0_SECRET", g0_feedback)
        say(f"{ticket_id} : ARRÊT — {g0_feedback}. Intervention humaine requise.")
        return "SECRET", g0_feedback
    if g0 == "FAIL" and worker_failed and not changed:
        # Le CLI lui-même a échoué (modèle inconnu, quota, authentification) : ce n'est pas un échec du ticket.
        run_fields["gate0"] = "WORKER_ERROR"
        db.record_run(project_id, ticket_id, **run_fields)
        db.update_ticket(project_id, ticket_id, state="READY")
        code = run_fields.get("exit_code", 1)
        db.add_event(project_id, ticket_id, "WORKER_ERROR", f"voie {lane} code {code}")
        say(f"{ticket_id} : le worker de la voie {lane} a échoué (code {code}) sans rien produire."
            f" Ticket remis à READY. Journal : {log_dir / f'attempt-{attempt}.log'}")
        return "WORKER_ERROR", g0_feedback
    if g0 == "FAIL":
        db.record_run(project_id, ticket_id, **run_fields)
        fail_updates: dict[str, Any] = {"fail_count": ticket["fail_count"] + 1}
        if keep_assigned_on_fail:
            fail_updates["state"] = "ASSIGNED"
        db.update_ticket(project_id, ticket_id, **fail_updates)
        db.add_event(project_id, ticket_id, "GATE0_FAIL", g0_feedback[:300])
        say(f"{ticket_id} : Porte 0 refusée —\n{g0_feedback}")
        return "FAIL", g0_feedback
    say(f"{ticket_id} : Porte 0 OK ({len(changed)} fichier(s) : {', '.join(changed)})")

    # Porte 1 statique (0 token) : un ticket qui touche du code source doit aussi toucher un test.
    if defaults.get("require_tests", False):
        ok, msg = check_tests_present(changed, files_owned)
        if not ok:
            run_fields["gate1"] = "STATIC_FAIL"
            db.record_run(project_id, ticket_id, **run_fields)
            static_updates: dict[str, Any] = {"fail_count": ticket["fail_count"] + 1}
            if keep_assigned_on_fail:
                static_updates["state"] = "ASSIGNED"
            db.update_ticket(project_id, ticket_id, **static_updates)
            db.add_event(project_id, ticket_id, "GATE1_STATIC_FAIL", msg[:300])
            say(f"{ticket_id} : Porte 1 statique refusée — {msg}")
            return "FAIL", msg

    # Indexe uniquement les fichiers du périmètre, puis audite le diff complet depuis la base.
    stage_paths(wt, changed)
    diff = sh(["git", "diff", "--cached", base], wt).stdout
    gate1_status = "SKIPPED"
    gate1_notes: list[str] = []
    if use_gate1:
        db.update_ticket(project_id, ticket_id, state="GATE1")
        say(f"{ticket_id} : Porte 1 — audit du diff ({len(diff)} caractères) par le modèle local…")
        gate1_status, gate1_notes, info = gate1(
            (wt / BRIEF_NAME).read_text(encoding="utf-8"), diff, cfg.get("gate1", {}),
            int(defaults.get("gate1_timeout_s", 300)),
        )
        if isinstance(info, dict) and "tokens_in" in info:
            db.add_event(
                project_id,
                ticket_id,
                "JUDGE",
                f"{gate1_status} tokens in={info['tokens_in']} out={info['tokens_out']} "
                f"cache={info['tokens_cache']} équivalent API {float(info.get('cost_usd', 0.0)):.4f} $",
            )
        say(f"{ticket_id} : Porte 1 {gate1_status}" + (f" — {'; '.join(gate1_notes)[:400]}" if gate1_notes else ""))
    run_fields["gate1"] = gate1_status
    db.record_run(project_id, ticket_id, **run_fields)

    if gate1_status == "FAIL" and allow_gate1_rework:
        db.add_event(project_id, ticket_id, "GATE1_FAIL", "; ".join(gate1_notes)[:300])
        return "GATE1_REWORK", "L'audit du diff signale :\n- " + "\n- ".join(gate1_notes)

    # Commit déterministe du travail validé par la Porte 0.
    if sh(["git", "diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
        sh(["git", "commit", "-m", f"feat({ticket_id}): {ticket['title'][:72]}"], wt)
    needs_arch = bool(ticket.get("touches_sensitive")) or gate1_status != "PASS"
    state = "GATE2" if needs_arch else "VALIDATED"
    db.update_ticket(project_id, ticket_id, state=state)
    db.add_event(project_id, ticket_id, success_event or state, f"gate0=PASS gate1={gate1_status} tentative={attempt}")
    write_report(log_dir, ticket, lane, attempt, changed, gate1_status, gate1_notes, state)
    say(f"{ticket_id} : {state}" + (" (revue architecte requise)" if needs_arch else " — prêt à fusionner"))
    return "PASS", ""


def run_ticket(db: CockpitDB, project_id: str, ticket_id: str, lane_override: str = "",
               max_attempts: int = 0, use_gate1: bool = True) -> int:
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    claimed, reason = db.claim_ticket(project_id, ticket_id, os.getpid())
    if not claimed:
        raise PipelineError(f"{ticket_id} ne peut pas être exécuté ({reason}) : exécution refusée.")

    try:
        cfg = load_runners()
        defaults = cfg.get("defaults", {})
        max_attempts = max_attempts or int(defaults.get("max_attempts", 3))
        files_owned = json.loads(ticket.get("files_owned_json") or "[]")
        if not files_owned:
            raise PipelineError(f"{ticket_id} n'a aucun fichier dans files_owned : périmètre non vérifiable.")

        ok, conflicts = db.acquire_locks(project_id, ticket_id, files_owned)
        if not ok:
            raise PipelineError("Fichiers déjà verrouillés : " + ", ".join(conflicts))

        parent_id = (ticket.get("parent_id") or "").strip()
        wt = ensure_worktree(proj, ticket_id, parent_id)
        brief_path = write_brief(ticket, wt, db)
        base_ref = f"feat/{parent_id}" if parent_id else proj["default_branch"]
        base = sh(["git", "merge-base", "HEAD", base_ref], wt).stdout.strip()
        log_dir = COCKPIT_HOME / "logs" / project_id / ticket_id
        say(f"{ticket_id} : worktree {wt}, brief {brief_path.stat().st_size} octets, base {base[:7]}")

        feedback = ""
        artefacts: set[str] = set()
        gate1_reworks = 0
        max_reworks = int(cfg.get("gate1", {}).get("max_reworks", 1))

        for attempt in range(1, max_attempts + 1):
            ticket = db.get_ticket(project_id, ticket_id) or ticket
            lane = lane_override or db.select_optimal_lane(
                ticket["complexity"], ticket["priority"], ticket["fail_count"]
            )["lane"]
            runner = cfg["runners"].get(lane)
            if not runner:
                raise PipelineError(f"Voie '{lane}' absente de config/runners.toml.")
            argv = [a.replace("{prompt}", worker_prompt(feedback)) for a in runner["argv"]]
            db.update_ticket(project_id, ticket_id, state="ASSIGNED", owner=runner.get("agent", lane), lane=lane)
            db.add_event(project_id, ticket_id, "WORKER_START", f"tentative {attempt}/{max_attempts} voie {lane}")
            say(f"{ticket_id} : tentative {attempt}/{max_attempts} sur la voie {lane} ({runner['argv'][0]})")

            extra_env = {k: v.replace("{cockpit}", str(COCKPIT_HOME)) for k, v in runner.get("env", {}).items()}
            log_path = log_dir / f"attempt-{attempt}.log"
            use_opencode_stats = runner.get("cost_source") == "opencode_stats" and not runner.get("usage_format")
            before = costs.snapshot_opencode_stats() if use_opencode_stats else None
            code, duration = run_worker(argv, wt, log_path,
                                        int(runner.get("timeout_s", defaults.get("timeout_s", 900))), extra_env)
            after = costs.snapshot_opencode_stats() if before is not None else None
            say(f"{ticket_id} : worker terminé (code {code}, {duration:.0f}s)")

            run_fields: dict[str, Any] = dict(attempt=attempt, lane=lane, provider=runner.get("provider", ""),
                                              exit_code=code, duration_s=round(duration, 1))
            parsed_usage = None
            if runner.get("usage_format") and log_path.exists():
                parsed_usage = usage.parse_usage(
                    str(runner["usage_format"]),
                    log_path.read_text(encoding="utf-8", errors="replace"),
                )
            if parsed_usage is not None:
                usd_to_eur = float(defaults.get("usd_to_eur", 0.92))
                raw_usd = parsed_usage["cost_usd"]
                cost_usd = float(raw_usd) if raw_usd is not None else 0.0
                cost_kind = str(parsed_usage["cost_kind"])
                cost_eur = round(cost_usd * usd_to_eur, 4) if cost_kind == "measured" else 0.0
                run_fields["tokens_in"] = int(parsed_usage["tokens_in"])
                run_fields["tokens_out"] = int(parsed_usage["tokens_out"])
                run_fields["tokens_cache"] = int(parsed_usage["tokens_cache"])
                run_fields["cost_usd"] = cost_usd
                run_fields["cost_kind"] = cost_kind
                run_fields["cost_eur"] = cost_eur
                if cost_eur > 0:
                    db.update_quota_usage(runner.get("provider", ""), add_spent_eur=cost_eur)
                if cost_kind == "measured":
                    cost_msg = f"coût mesuré {cost_eur:.4f} €"
                elif cost_kind == "api_equivalent":
                    cost_msg = f"équivalent API {cost_usd:.4f} $ (abonnement, pas de l'argent)"
                else:
                    cost_msg = "tokens seulement"
                say(
                    f"{ticket_id} : tokens entrée={run_fields['tokens_in']}, "
                    f"sortie={run_fields['tokens_out']}, cache={run_fields['tokens_cache']} — {cost_msg}"
                )
            elif before is not None and after is not None:
                d = costs.delta(before, after)
                usd_to_eur = float(defaults.get("usd_to_eur", 0.92))
                cost_eur = round(d["total_cost_usd"] * usd_to_eur, 4)
                run_fields["tokens_in"] = d["input_tokens"]
                run_fields["tokens_out"] = d["output_tokens"]
                run_fields["cost_eur"] = cost_eur
                if cost_eur > 0:
                    db.update_quota_usage(runner["provider"], add_spent_eur=cost_eur)
                say(f"{ticket_id} : coût {cost_eur:.4f} € (tokens in={d['input_tokens']}, out={d['output_tokens']})")

            if runner.get("cost_source") == "estimate":
                provider = runner.get("provider", "")
                window_hours = float(runner.get("window_hours", 5.0))
                recent = db.recent_runs_by_lane(provider, window_hours)
                pct = 0.0
                for other_lane, other_cfg in cfg["runners"].items():
                    if other_cfg.get("provider") == provider and other_cfg.get("cost_source") == "estimate":
                        pct += recent.get(other_lane, 0) * float(other_cfg.get("window_pct_per_run", 0.0))
                # Le run en cours n'est pas encore enregistré : on l'ajoute à la jauge glissante.
                pct = min(pct + float(runner.get("window_pct_per_run", 0.0)), 100.0)
                db.update_quota_usage(provider, window_used_pct=pct)
                if parsed_usage is None:
                    estimated_cost = float(runner.get("estimated_cost_eur", 0.0))
                    run_fields["tokens_in"] = int(runner.get("estimated_tokens_in", 0))
                    run_fields["tokens_out"] = int(runner.get("estimated_tokens_out", 0))
                    run_fields["cost_eur"] = estimated_cost
                    if estimated_cost > 0:
                        db.update_quota_usage(provider, add_spent_eur=estimated_cost)
                    say(f"{ticket_id} : coût estimé {estimated_cost:.4f} € (estimation, fenêtre {window_hours:g} h"
                        f" à {pct:.1f} %), tokens in={run_fields['tokens_in']}, out={run_fields['tokens_out']}")

            verdict, msg = _post_worker_gates(
                db, proj, ticket, wt, base, log_dir, cfg, run_fields, artefacts,
                use_gate1=use_gate1,
                allow_gate1_rework=(gate1_reworks < max_reworks and attempt < max_attempts),
                worker_failed=(code != 0),
            )
            if verdict == "SECRET":
                return 2
            if verdict == "WORKER_ERROR":
                return 3
            if verdict == "FAIL":
                feedback = msg
                continue
            if verdict == "GATE1_REWORK":
                gate1_reworks += 1
                feedback = msg
                continue
            return 0

        db.update_ticket(project_id, ticket_id, state="GATE2")
        db.add_event(project_id, ticket_id, "ESCALATED", f"{max_attempts} tentatives sans succès")
        say(f"{ticket_id} : {max_attempts} tentatives sans succès — escalade (état GATE2). Dernier retour :\n{feedback}")
        return 1
    finally:
        db.unclaim_ticket(project_id, ticket_id)


def claim_ticket(db: CockpitDB, project_id: str, ticket_id: str, owner: str) -> int:
    """Mode chantier interactif : verrouille les fichiers, passe à ASSIGNED (lane=interactive), prépare worktree et brief."""
    if owner not in INTERACTIVE_OWNERS:
        raise PipelineError(f"Agent interactif invalide : {owner} (attendu : {', '.join(INTERACTIVE_OWNERS)})")
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    if ticket["state"] != "READY":
        raise PipelineError(f"{ticket_id} est à l'état {ticket['state']} (attendu : READY) : claim refusé.")
    files_owned = json.loads(ticket.get("files_owned_json") or "[]")
    if not files_owned:
        raise PipelineError(f"{ticket_id} n'a aucun fichier dans files_owned : périmètre non vérifiable.")

    ok, conflicts = db.acquire_locks(project_id, ticket_id, files_owned)
    if not ok:
        raise PipelineError("Fichiers déjà verrouillés : " + ", ".join(conflicts))

    conn = db._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE tickets SET state = 'ASSIGNED', owner = ?, lane = 'interactive', "
            "claimed_pid = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE project_id = ? AND id = ? AND state = 'READY'",
            (owner, project_id, ticket_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise PipelineError(f"{ticket_id} n'est plus à l'état READY : claim refusé.")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    ticket = db.get_ticket(project_id, ticket_id) or ticket
    parent_id = (ticket.get("parent_id") or "").strip()
    wt = ensure_worktree(proj, ticket_id, parent_id)
    write_brief(ticket, wt, db)
    db.add_event(project_id, ticket_id, "CLAIMED", f"owner={owner} lane=interactive")
    say(f"{ticket_id} pris en charge par {owner} — lire {BRIEF_NAME} avant toute modification.")
    print(f"WORKTREE={wt.resolve()}", flush=True)
    return 0


def submit_ticket(db: CockpitDB, project_id: str, ticket_id: str) -> int:
    """Soumet le worktree d'un ticket interactif aux portes de qualité (une seule passe, sans worker)."""
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    if ticket["state"] != "ASSIGNED" or ticket.get("lane") != "interactive":
        raise PipelineError(
            f"{ticket_id} doit être à l'état ASSIGNED sur la voie 'interactive'"
            f" (actuel : state={ticket['state']}, lane={ticket.get('lane')!r})."
        )
    files_owned = json.loads(ticket.get("files_owned_json") or "[]")
    if not files_owned:
        raise PipelineError(f"{ticket_id} n'a aucun fichier dans files_owned : périmètre non vérifiable.")

    wt = Path(proj["wt_tickets_dir"]) / ticket_id
    if not wt.exists():
        raise PipelineError(f"Worktree introuvable : {wt}")

    cfg = load_runners()
    parent_id = (ticket.get("parent_id") or "").strip()
    base_ref = f"feat/{parent_id}" if parent_id else proj["default_branch"]
    base = sh(["git", "merge-base", "HEAD", base_ref], wt).stdout.strip()
    log_dir = COCKPIT_HOME / "logs" / project_id / ticket_id
    attempt = int(ticket.get("fail_count") or 0) + 1
    run_fields: dict[str, Any] = dict(
        attempt=attempt,
        lane="interactive",
        provider=str(ticket.get("owner") or ""),
        exit_code=0,
        duration_s=0.0,
    )
    verdict, _ = _post_worker_gates(
        db, proj, ticket, wt, base, log_dir, cfg, run_fields, set(),
        use_gate1=False,
        allow_gate1_rework=False,
        worker_failed=False,
        success_event="SUBMITTED",
        keep_assigned_on_fail=True,
    )
    if verdict == "SECRET":
        return 2
    if verdict == "FAIL":
        return 1
    return 0


def release_ticket(db: CockpitDB, project_id: str, ticket_id: str) -> int:
    """Relâche un ticket interactif ASSIGNED : remet à READY, libère les verrous et conserve le worktree."""
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    if ticket["state"] != "ASSIGNED" or ticket.get("lane") != "interactive":
        raise PipelineError(
            f"{ticket_id} doit être à l'état ASSIGNED sur la voie 'interactive'"
            f" (actuel : state={ticket['state']}, lane={ticket.get('lane')!r})."
        )
    db.update_ticket(project_id, ticket_id, state="READY", owner="auto", lane="")
    db.unclaim_ticket(project_id, ticket_id)
    db.release_locks(project_id, ticket_id)
    db.add_event(project_id, ticket_id, "RELEASED", f"rendu par {ticket.get('owner') or 'interactive'}")
    say(f"{ticket_id} relâché : état READY, verrous libérés (worktree conservé).")
    return 0


def write_report(log_dir: Path, ticket: dict[str, Any], lane: str, attempt: int, changed: list[str],
                 gate1_status: str, gate1_notes: list[str], state: str) -> None:
    """Rapport de ticket généré sans LLM."""
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Rapport {ticket['id']} — {ticket['title']}",
        f"- État : {state}",
        f"- Voie : {lane} (tentative {attempt})",
        f"- Fichiers : {', '.join(changed)}",
        "- Porte 0 : PASS (périmètre, secrets, lint, tests)",
        f"- Porte 1 : {gate1_status}",
    ]
    lines += [f"  - {n}" for n in gate1_notes]
    (log_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ───────────────────────────── fusion ─────────────────────────────


def _child_ready_for_integration(db: CockpitDB, project_id: str, ticket: dict[str, Any]) -> bool:
    state = ticket.get("state")
    if state == "VALIDATED":
        return True
    if state == "GATE2":
        last = db.last_run(project_id, ticket["id"])
        return bool(last and last.get("gate0") == "PASS" and last.get("gate1") != "STATIC_FAIL")
    return False


def _is_pid_alive(pid: Any) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def abandon_ticket(db: CockpitDB, project_id: str, ticket_id: str, reason: str = "") -> list[str]:
    """Abandonne proprement un ticket ou un chantier (enfants puis parent) sans toucher à la branche principale."""
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    if ticket["state"] in ("MERGED", "ABANDONED"):
        raise PipelineError(f"{ticket_id} est déjà à l'état {ticket['state']} : abandon refusé.")

    children = db.children(project_id, ticket_id)
    targets = [*children, ticket]
    for t in targets:
        if _is_pid_alive(t.get("claimed_pid")):
            raise PipelineError(
                f"{t['id']} est réservé par un processus actif (pid {t['claimed_pid']}) : abandon refusé."
            )

    root = Path(proj["root_path"])
    wt_base = Path(proj["wt_tickets_dir"])
    motif = (reason or "").strip() or "abandon demandé"
    abandoned: list[str] = []

    for t in targets:
        tid = t["id"]
        wt = wt_base / tid
        branch = f"feat/{tid}"
        with _GIT_WORKTREE_LOCK:
            if wt.exists():
                sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
        sh(["git", "branch", "-D", branch], root, check=False)
        db.release_locks(project_id, tid)
        db.unclaim_ticket(project_id, tid)
        db.update_ticket(project_id, tid, state="ABANDONED")
        db.add_event(project_id, tid, "ABANDONED", motif)
        abandoned.append(tid)

    say(f"Abandon ({project_id}) : {', '.join(abandoned)} ({motif})")
    return abandoned


def _pr_only_body(
    db: CockpitDB, project_id: str, ticket_id: str, contract: str, wt: Path, base_ref: str
) -> str:
    """Corps de PR généré sans LLM : contrat, fichiers, diffstat, portes et coût du dernier run."""
    modified = changed_files(wt, base_ref)
    diffstat = sh(["git", "diff", "--stat", f"{base_ref}..HEAD"], wt, check=False).stdout.strip()
    run = db.last_run(project_id, ticket_id) or {}
    gate0 = str(run.get("gate0") or "inconnu")
    gate1 = str(run.get("gate1") or "inconnu")
    cost_eur = float(run.get("cost_eur") or 0.0)
    cost_usd = float(run.get("cost_usd") or 0.0)
    return "\n\n".join([
        contract,
        "Fichiers modifiés :\n" + ("\n".join(f"- {f}" for f in modified) or "- (aucun)"),
        "Diffstat :\n" + (diffstat or "(aucune modification)"),
        f"Porte 0 : {gate0}\nPorte 1 : {gate1}",
        f"Coût du run : {cost_eur:.4f} € ({cost_usd:.4f} $)",
    ])


def merge_ticket(db: CockpitDB, project_id: str, ticket_id: str, approve: bool = False) -> int:
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj or not ticket:
        raise PipelineError(f"Projet ou ticket inconnu : {project_id} / {ticket_id}")

    unmerged_children = [
        c["id"] for c in db.children(project_id, ticket_id) if c.get("state") not in ("MERGED", "ABANDONED")
    ]
    if unmerged_children:
        raise PipelineError(
            f"{ticket_id} a des sous-tickets non MERGED ({', '.join(unmerged_children)}) : fusion refusée."
        )

    parent_id = (ticket.get("parent_id") or "").strip()
    if parent_id:
        if not _child_ready_for_integration(db, project_id, ticket):
            raise PipelineError(
                f"{ticket_id} n'est pas intégrable dans {parent_id} (état {ticket['state']}, Porte 0/1 non validée)."
            )
    else:
        if ticket["state"] == "GATE2" and not approve:
            raise PipelineError(f"{ticket_id} attend une revue (GATE2). Relire le diff puis relancer avec --approve.")
        if ticket["state"] not in ("VALIDATED", "GATE2"):
            raise PipelineError(f"{ticket_id} est à l'état {ticket['state']} : fusion refusée.")

    root = Path(proj["root_path"])
    wt = Path(proj["wt_tickets_dir"]) / ticket_id
    branch, default = f"feat/{ticket_id}", proj["default_branch"]
    target_branch = f"feat/{parent_id}" if parent_id else default
    target_wt = (Path(proj["wt_tickets_dir"]) / parent_id) if parent_id else root
    central = load_toml(config_paths.config_file("projects.toml", COCKPIT_HOME)).get("projects", {}).get(
        project_id, {}
    )
    merge_mode = str(central.get("merge_mode", "local")).strip() or "local"
    remote = str(central.get("remote", "origin")).strip() or "origin"
    github_mode = merge_mode == "github" and not parent_id
    github_pr_mode = merge_mode == "github_pr" and not parent_id
    github_like = github_mode or github_pr_mode
    lock_path = root / ".git" / "cockpit-merge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        if parent_id:
            if not target_wt.exists():
                raise PipelineError(
                    f"Worktree du chantier {parent_id} introuvable ({target_wt}) : commiter le chantier d'abord."
                )
            if sh(["git", "status", "--porcelain", "--untracked-files=no"], target_wt).stdout.strip():
                raise PipelineError(
                    f"Le chantier {parent_id} a des modifications suivies non commitées : commiter le chantier d'abord."
                )
        else:
            if sh(["git", "symbolic-ref", "--short", "HEAD"], root).stdout.strip() != default:
                raise PipelineError(f"Le dépôt racine doit être sur {default}.")
        if not wt.exists():
            raise PipelineError(f"Worktree introuvable : {wt}")
        if sh(["git", "status", "--porcelain", "--untracked-files=no"], wt).stdout.strip():
            raise PipelineError(f"{ticket_id} a des modifications non commitées dans son worktree.")

        # Porte d'intégration : on ramène la branche cible dans le ticket et on re-teste AVANT de fusionner.
        integration_ref = f"{remote}/{default}" if github_like else target_branch
        if github_like:
            sh(["git", "fetch", remote], wt)
        res = sh(["git", "merge", "--no-edit", integration_ref], wt, check=False)
        if res.returncode != 0:
            sh(["git", "merge", "--abort"], wt, check=False)
            db.add_event(project_id, ticket_id, "MERGE_CONFLICT", res.stdout[-300:])
            raise PipelineError(f"Conflit entre {branch} et {integration_ref} : fusion annulée, rien n'a changé.")
        lint_cmd, default_test = project_commands(project_id, root, wt)
        timeout = int(load_runners().get("defaults", {}).get("gate_timeout_s", 600))
        for label, cmd in (("lint", lint_cmd), ("tests", default_test), ("test du ticket", ticket.get("test_cmd") or "")):
            if not cmd:
                continue
            code, out = run_shell(cmd, wt, timeout)
            if code != 0:
                db.add_event(project_id, ticket_id, "INTEGRATION_FAIL", f"{label}: {cmd}")
                raise PipelineError(f"Porte d'intégration refusée ({label}, `{cmd}`) :\n{sieve_test_output(out, 25)}")
        say(f"{ticket_id} : porte d'intégration OK ({integration_ref} intégré, lint et tests verts)")

        if github_like:
            gh = os.environ.get("COCKPIT_GH", "gh")
            contract = (ticket.get("contract") or "").strip()[:4000]
            title = f"{ticket_id}: {ticket['title']}"
            if github_pr_mode:
                body = _pr_only_body(db, project_id, ticket_id, contract, wt, integration_ref)
            else:
                modified = changed_files(wt, integration_ref)
                body = contract + "\n\nFichiers modifiés :\n" + "\n".join(f"- {f}" for f in modified)
            hits = private_text_hits(title + "\n" + body)
            if hits:
                db.add_event(project_id, ticket_id, "PR_PRIVACY_FAIL", ", ".join(hits))
                raise PipelineError(
                    f"{ticket_id} : le titre ou le contrat contient un motif privé ({', '.join(hits)}) : "
                    "rien n'est poussé. Reformuler le ticket."
                )
            sh(["git", "push", "-u", remote, branch], wt)
            view = sh([gh, "pr", "view", branch, "--json", "number,url,state"], wt, check=False, timeout=60)
            try:
                view_data = json.loads(view.stdout) if view.returncode == 0 else {}
            except (ValueError, TypeError):
                view_data = {}
            if str(view_data.get("state", "OPEN")).upper() != "OPEN":
                view_data = {}  # PR fermée ou déjà fusionnée : en ouvrir une nouvelle
            if view_data:
                pr_label = str(view_data.get("url") or "").strip()
                if not pr_label:
                    number = view_data.get("number")
                    pr_label = f"PR #{number}" if number not in (None, "") else branch
            else:
                created = sh(
                    [gh, "pr", "create", "--base", default, "--head", branch, "--title", title, "--body", body],
                    wt,
                )
                pr_label = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else branch
            if github_pr_mode:
                db.update_ticket(project_id, ticket_id, state="PR_OPEN")
                db.add_event(project_id, ticket_id, "PR_OPENED", f"{pr_label} — en attente de fusion humaine")
                say(f"{ticket_id} : PR ouverte ({pr_label}), ticket en PR_OPEN (aucune fusion automatique).")
                return 0
            try:
                # Juste après le push, GitHub peut ne pas avoir encore enregistré les checks.
                for _ in range(12):
                    checks = sh([gh, "pr", "checks", branch, "--watch", "--fail-fast"], wt, check=False, timeout=timeout)
                    if "no checks reported" not in (checks.stdout + (checks.stderr or "")).lower():
                        break
                    time.sleep(10)
            except subprocess.TimeoutExpired:
                db.add_event(project_id, ticket_id, "PR_CHECKS_FAIL", f"délai {timeout}s dépassé")
                raise PipelineError(f"{ticket_id} : gh pr checks a dépassé {timeout}s, rien n'est fusionné.")
            if checks.returncode != 0:
                detail = (checks.stdout or checks.stderr).strip()[-300:]
                db.add_event(project_id, ticket_id, "PR_CHECKS_FAIL", detail)
                raise PipelineError(f"{ticket_id} : checks GitHub en échec, rien n'est fusionné.\n{detail}")
            sh([gh, "pr", "merge", branch, "--squash", "--delete-branch"], wt)
            sh(["git", "fetch", remote], root)
            sh(["git", "merge", "--ff-only", f"{remote}/{default}"], root)
            # Le commit de squash est créé par GitHub avec l'adresse du compte : la vérifier.
            leaks = commit_identity_leaks(root, f"{remote}/{default}")
            if leaks:
                db.add_event(project_id, ticket_id, "PRIVACY_LEAK", ", ".join(leaks))
                say(f"{ticket_id} : ATTENTION, identité de commit non autorisée sur {default} ({', '.join(leaks)}). "
                    "Activer « Keep my email addresses private » dans les réglages GitHub.")
            sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
            sh(["git", "branch", "-D", branch], root, check=False)
            unblocked = db.complete_and_merge_ticket(project_id, ticket_id)
            db.add_event(project_id, ticket_id, "MERGED", f"{pr_label} — débloque : " + (", ".join(unblocked) or "rien"))
            # Branche principale protégée : la roadmap est régénérée mais jamais commitée.
            db.export_markdown_roadmap(project_id, root / "docs" / "ROADMAP.md")
            say(f"{ticket_id} : fusionné dans {default} via GitHub ({pr_label}). Débloqués : "
                f"{', '.join(unblocked) or 'aucun'}")
            return 0

        sh(["git", "merge", "--no-ff", branch, "-m", f"merge({ticket_id}): {ticket['title'][:72]}"], target_wt)
        sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
        sh(["git", "branch", "-D", branch], root, check=False)
        unblocked = db.complete_and_merge_ticket(project_id, ticket_id)
        if parent_id:
            db.add_event(project_id, ticket_id, "MERGED", f"intégré dans {parent_id}")
            say(f"{ticket_id} : intégré dans {parent_id}")
            return 0
        db.add_event(project_id, ticket_id, "MERGED", "débloque : " + (", ".join(unblocked) or "rien"))

        roadmap = root / "docs" / "ROADMAP.md"
        db.export_markdown_roadmap(project_id, roadmap)
        sh(["git", "add", "--", "docs/ROADMAP.md"], root, check=False)
        if sh(["git", "diff", "--cached", "--quiet"], root, check=False).returncode != 0:
            env = dict(os.environ, ALLOW_MAIN="1")
            subprocess.run(["git", "commit", "-q", "-m", f"docs(roadmap): après fusion de {ticket_id}", "--", "docs/ROADMAP.md"],
                           cwd=str(root), env=env, capture_output=True, text=True)
        say(f"{ticket_id} : fusionné dans {default}. Débloqués : {', '.join(unblocked) or 'aucun'}")
        return 0
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _gh_pr_state(gh: str, branch: str, cwd: Path) -> tuple[str, str, bool]:
    """Interroge `gh pr view` : (state, mergedAt, ok). ok=False si gh absent ou erreur réseau."""
    try:
        res = sh([gh, "pr", "view", branch, "--json", "state,mergedAt"], cwd, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "", "", False
    if res.returncode != 0:
        return "", "", False
    try:
        data = json.loads(res.stdout or "{}")
    except (ValueError, TypeError):
        return "", "", False
    if not isinstance(data, dict):
        return "", "", False
    state = str(data.get("state") or "").upper()
    merged_at = str(data.get("mergedAt") or "")
    return state, merged_at, bool(state)


def sync_project(db: CockpitDB, project_id: str) -> int:
    """Clôt les tickets PR_OPEN dont la PR GitHub est fusionnée (0 token)."""
    proj = db.get_project(project_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    pr_tickets = [t for t in db.get_prioritized_queue(project_id) if t.get("state") == "PR_OPEN"]
    if not pr_tickets:
        return 0

    root = Path(proj["root_path"])
    default = proj["default_branch"]
    central = load_toml(config_paths.config_file("projects.toml", COCKPIT_HOME)).get("projects", {}).get(
        project_id, {}
    )
    remote = str(central.get("remote", "origin")).strip() or "origin"
    gh = os.environ.get("COCKPIT_GH", "gh")
    errors = 0

    for ticket in pr_tickets:
        ticket_id = ticket["id"]
        branch = f"feat/{ticket_id}"
        state, merged_at, ok = _gh_pr_state(gh, branch, root)
        if not ok:
            say(f"{ticket_id} : GitHub injoignable ou gh absent — état inchangé.")
            errors += 1
            continue
        if state == "OPEN":
            continue
        if state == "CLOSED" and not merged_at:
            db.add_event(project_id, ticket_id, "PR_CLOSED", f"{branch} fermée sans fusion")
            say(f"{ticket_id} : PR fermée sans fusion — relancer le ticket ou l'abandonner (état inchangé).")
            continue
        if state != "MERGED" and not merged_at:
            continue

        if sh(["git", "symbolic-ref", "--short", "HEAD"], root, check=False).stdout.strip() != default:
            raise PipelineError(f"Le dépôt racine doit être sur {default} pour synchroniser {ticket_id}.")
        if sh(["git", "status", "--porcelain", "--untracked-files=no"], root).stdout.strip():
            raise PipelineError(
                f"Le dépôt racine a des modifications suivies non commitées : synchronisation refusée."
            )
        sh(["git", "fetch", remote], root)
        merge = sh(["git", "merge", "--ff-only", f"{remote}/{default}"], root, check=False)
        if merge.returncode != 0:
            raise PipelineError(
                f"{ticket_id} : fusion fast-forward impossible depuis {remote}/{default} : "
                f"{(merge.stderr or merge.stdout).strip()[:300]}"
            )
        leaks = commit_identity_leaks(root, f"{remote}/{default}")
        if leaks:
            db.add_event(project_id, ticket_id, "PRIVACY_LEAK", ", ".join(leaks))
            say(f"{ticket_id} : ATTENTION, identité de commit non autorisée sur {default} ({', '.join(leaks)}).")
        wt = Path(proj["wt_tickets_dir"]) / ticket_id
        sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
        sh(["git", "branch", "-D", branch], root, check=False)
        unblocked = db.complete_and_merge_ticket(project_id, ticket_id)
        db.add_event(
            project_id, ticket_id, "MERGED", f"PR fusionnée — débloque : " + (", ".join(unblocked) or "rien")
        )
        say(f"{ticket_id} : PR fusionnée, ticket MERGED. Débloqués : {', '.join(unblocked) or 'aucun'}")

    return 1 if errors else 0


# ───────────────────────────── tickets & vues ─────────────────────────────


def next_ticket_id(db: CockpitDB, project_id: str) -> str:
    with db._connect() as conn:
        rows = conn.execute("SELECT id FROM tickets WHERE project_id = ?", (project_id,)).fetchall()
    nums = [int(r["id"][2:]) for r in rows if r["id"].startswith("T-") and r["id"][2:].isdigit()]
    return f"T-{(max(nums) + 1) if nums else 1:03d}"


def _format_ticket_line(db: CockpitDB, t: dict[str, Any], indent: str = "") -> str:
    lane = t.get("lane") or db.select_optimal_lane(t["complexity"], t["priority"], t["fail_count"])["lane"]
    return (
        f"{indent}{t['id']:<7} {t['milestone_id'] or '-':<5} {t['priority']:<3} [{t['complexity']:<2}] "
        f"{t['state']:<10} {lane:<16} {t['title'][:70]}"
    )


def cmd_ticket(db: CockpitDB, args: argparse.Namespace) -> int:
    if args.action == "add":
        if not db.get_project(args.project):
            raise PipelineError(f"Projet inconnu : {args.project}")
        tid = args.id or next_ticket_id(db, args.project)
        parent_id = (getattr(args, "parent", "") or "").strip()
        if parent_id:
            try:
                runners_cfg = load_runners()
            except PipelineError:
                runners_cfg = {}
            max_children = int(runners_cfg.get("defaults", {}).get("max_children", 4))
            active_children = [
                c
                for c in db.children(args.project, parent_id)
                if c.get("state") not in ("MERGED", "ABANDONED") and c.get("id") != tid
            ]
            if len(active_children) >= max_children:
                raise PipelineError(
                    f"Le ticket parent {parent_id} a déjà {len(active_children)} sous-tickets non MERGED (limite max_children={max_children})."
                )
        contract = args.contract or ""
        if args.contract_file:
            contract = Path(args.contract_file).read_text(encoding="utf-8")
        db.upsert_ticket(
            args.project, tid, args.title, milestone_id=args.milestone, priority=args.prio, complexity=args.size,
            files_owned=[f for f in (args.files or "").split(",") if f],
            files_readonly=[f for f in (args.readonly or "").split(",") if f],
            depends_on=[d for d in (args.depends or "").split(",") if d],
            acceptance=args.accept or [], contract=contract, test_cmd=args.test_cmd or "",
            touches_sensitive=args.sensitive,
            parent_id=parent_id,
        )
        t = db.get_ticket(args.project, tid) or {}
        say(f"{tid} créé ({t.get('state')}) dans {args.project}")
        return 0
    if args.action == "show":
        t = db.get_ticket(args.project, args.id)
        if not t:
            raise PipelineError(f"Ticket inconnu : {args.id}")
        out = dict(t)
        if not out.get("parent_id"):
            out.pop("parent_id", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    queue = db.get_prioritized_queue(args.project)
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for t in queue:
        pid = (t.get("parent_id") or "").strip()
        if pid:
            children_by_parent.setdefault(pid, []).append(t)
    top_ids = {t["id"] for t in queue if not (t.get("parent_id") or "").strip()}
    for t in queue:
        if (t.get("parent_id") or "").strip():
            continue
        print(_format_ticket_line(db, t))
        for child in children_by_parent.get(t["id"], []):
            print(_format_ticket_line(db, child, indent="  "))
    for pid, orphans in children_by_parent.items():
        if pid not in top_ids:
            for child in orphans:
                print(_format_ticket_line(db, child, indent="  "))
    return 0


def cmd_events(db: CockpitDB, project_id: str, limit: int) -> int:
    for e in reversed(db.recent_events(project_id, limit)):
        print(f"{e['created_at']}  {e['ticket_id'] or '-':<7} {e['kind']:<16} {e['detail']}")
    return 0


def _format_status_ticket(t: dict[str, Any], indent: str = "") -> str:
    owner = t.get("owner") or "auto"
    lane = t.get("lane") or "-"
    title = (t.get("title") or "")[:50]
    return f"{indent}{t['id']} {t['state']} {owner} {lane} {title}"


def build_status(db: CockpitDB, project_id: str, max_events: int = 8) -> str:
    """État synthétique du projet en texte brut (<= 40 lignes, 0 token, sections vides omises)."""
    queue = db.get_prioritized_queue(project_id)

    active = [t for t in queue if t.get("state") in ACTIVE_STATES]
    pr_open_lines = [_format_status_ticket(t) for t in queue if t.get("state") == "PR_OPEN"]
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for t in active:
        pid = (t.get("parent_id") or "").strip()
        if pid:
            children_by_parent.setdefault(pid, []).append(t)

    work_lines: list[str] = []
    top_active_ids = {t["id"] for t in active if not (t.get("parent_id") or "").strip()}
    for t in active:
        if (t.get("parent_id") or "").strip():
            continue
        work_lines.append(_format_status_ticket(t))
        for child in children_by_parent.get(t["id"], []):
            work_lines.append(_format_status_ticket(child, indent="  "))
    for pid, orphans in children_by_parent.items():
        if pid not in top_active_ids:
            for child in orphans:
                work_lines.append(_format_status_ticket(child, indent="  "))

    with db._connect() as conn:
        lock_rows = conn.execute(
            "SELECT ticket_id, file_path FROM file_locks WHERE project_id = ? ORDER BY ticket_id, file_path",
            (project_id,),
        ).fetchall()
    locks_by_ticket: dict[str, list[str]] = {}
    for row in lock_rows:
        locks_by_ticket.setdefault(row["ticket_id"], []).append(row["file_path"])
    lock_lines: list[str] = []
    for tid, files in locks_by_ticket.items():
        if len(files) <= 6:
            lock_lines.append(f"{tid} : {', '.join(files)}")
        else:
            lock_lines.append(f"{tid} : {', '.join(files[:6])}, +{len(files) - 6}")

    ready_ids = [t["id"] for t in queue if t.get("state") == "READY"]

    ev_limit = max(0, min(int(max_events), 8))
    events = db.recent_events(project_id, limit=ev_limit) if ev_limit > 0 else []
    event_lines: list[str] = []
    for e in events:
        ts = str(e.get("created_at") or "")
        hhmm = ts[11:16] if len(ts) >= 16 else (ts[:5] or "00:00")
        tid = e.get("ticket_id") or "-"
        kind = e.get("kind") or ""
        detail = str(e.get("detail") or "").replace("\n", " ")[:60]
        event_lines.append(f"{hhmm} {tid} {kind} {detail}".rstrip())

    if not work_lines and not lock_lines and not ready_ids and not event_lines and not pr_open_lines:
        return "Aucune activité."

    def _cap(items: list[str], budget: int) -> list[str]:
        if len(items) <= budget:
            return items
        if budget <= 1:
            return [f"... +{len(items)} autres"]
        kept = budget - 1
        return items[:kept] + [f"... +{len(items) - kept} autres"]

    fixed_lines = (2 if ready_ids else 0) + ((1 + len(event_lines)) if event_lines else 0)
    fixed_lines += (1 + len(pr_open_lines)) if pr_open_lines else 0
    rem = max(4, 40 - fixed_lines)

    if work_lines and lock_lines:
        avail = max(2, rem - 2)
        if len(work_lines) + len(lock_lines) > avail:
            lock_budget = min(len(lock_lines), max(3, avail // 3))
            work_budget = max(1, avail - lock_budget)
            work_lines = _cap(work_lines, work_budget)
            lock_lines = _cap(lock_lines, lock_budget)
    elif work_lines:
        work_lines = _cap(work_lines, max(1, rem - 1))
    elif lock_lines:
        lock_lines = _cap(lock_lines, max(1, rem - 1))

    out_lines: list[str] = []
    if work_lines:
        out_lines.append("TRAVAUX EN COURS")
        out_lines.extend(work_lines)
    if pr_open_lines:
        out_lines.append("PR OUVERTES")
        out_lines.extend(_cap(pr_open_lines, max(1, 39 - len(out_lines))))
    if lock_lines:
        out_lines.append("FICHIERS VERROUILLÉS")
        out_lines.extend(lock_lines)
    if ready_ids:
        out_lines.append("PRÊTS")
        out_lines.append(", ".join(ready_ids))
    if event_lines:
        out_lines.append("DERNIERS ÉVÉNEMENTS")
        out_lines.extend(event_lines)

    return "\n".join(out_lines[:40])


def add_note(db: CockpitDB, project_id: str, ticket_id: str, text: str) -> int:
    """Enregistre une note inter-agents sur un ticket (0 token)."""
    clean = (text or "").strip()
    if not clean:
        raise PipelineError("Texte de note vide refusé.")
    if not db.get_ticket(project_id, ticket_id):
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    db.add_event(project_id, ticket_id, "NOTE", clean[:200])
    say(f"Note enregistrée sur {ticket_id} ({project_id})")
    return 0


def cmd_auto(db: CockpitDB, args: argparse.Namespace) -> int:
    projet = args.project
    parent_id = (getattr(args, "parent", "") or "").strip() or None
    cfg_all = load_runners()
    if getattr(args, "auto_merge", False) and not getattr(args, "dry_run", False) and not parent_id:
        gate1_cfg = cfg_all.get("gate1", {})
        if not getattr(args, "gate1", False) or gate1_cfg.get("backend") != "claude":
            say("--auto-merge exige --gate1 avec le juge Claude")
            return 2
        ok_cal, raison = judge_calibrate.calibration_status()
        if not ok_cal:
            say(
                f"--auto-merge refusé : {raison}. "
                "Lancez : cockpit judge-calibrate --runs 3 --json logs/judge-calibration.json"
            )
            return 2
    done = 0
    escalations_in_a_row = 0
    runners_cfg = cfg_all.get("runners", {})
    lane_providers: dict[str, str] = {}
    limits: dict[str, int] = {}
    for lane_name, r_cfg in runners_cfg.items():
        prov = str(r_cfg.get("provider") or lane_name)
        lane_providers[lane_name] = prov
        if "max_parallel" in r_cfg and r_cfg["max_parallel"] is not None:
            lim = int(r_cfg["max_parallel"])
            limits[prov] = min(limits[prov], lim) if prov in limits else lim
    provider_limits: dict[str, int] | None = limits if limits else None
    max_cost_eur = float(getattr(args, "max_cost_eur", 0.0) or 0.0) or None
    with db._connect() as conn:
        run_marker = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM runs WHERE project_id = ?", (projet,)
            ).fetchone()[0]
        )

    while True:
        slots = min(args.slots, args.max_tickets - done)
        plan_kwargs: dict[str, Any] = {
            "provider_limits": provider_limits,
            "lane_providers": lane_providers if provider_limits is not None else None,
        }
        if parent_id is not None:
            plan_kwargs["parent_id"] = parent_id
        batch = scheduler.plan_batch(
            db,
            projet,
            slots,
            **plan_kwargs,
        )
        if not batch:
            say("Plus de ticket exécutable")
            return 0
        if args.dry_run:
            for item in batch:
                files = ", ".join(item["files"]) if isinstance(item["files"], list) else str(item["files"])
                print(f"{item['id']}  {item['lane']}  {files}")
            return 0

        results: dict[str, int] = {}

        def _worker(tid: str) -> None:
            try:
                results[tid] = run_ticket(
                    db, projet, tid, max_attempts=args.max_attempts, use_gate1=bool(args.gate1)
                )
            except Exception as exc:
                say(f"Erreur dans le worker du ticket {tid} : {exc}")
                try:
                    db.add_event(projet, tid, "WORKER_CRASH", str(exc)[:300])
                    t_cur = db.get_ticket(projet, tid)
                    if t_cur and t_cur.get("state") in ("ASSIGNED", "GATE0", "GATE1"):
                        db.update_ticket(projet, tid, state="READY")
                except Exception:
                    pass
                results[tid] = 1

        threads = [threading.Thread(target=_worker, args=(item["id"],)) for item in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = sum(1 for item in batch if results.get(item["id"], 1) == 0)
        failures = scheduler.count_failures(db, projet, batch, results)
        done += successes
        if failures > 0:
            escalations_in_a_row += failures
        else:
            escalations_in_a_row = 0

        if args.auto_merge:
            for item in batch:
                t_info = db.get_ticket(projet, item["id"])
                if not t_info:
                    continue
                can_merge = (
                    _child_ready_for_integration(db, projet, t_info)
                    if parent_id
                    else t_info.get("state") == "VALIDATED"
                )
                if can_merge:
                    try:
                        merge_ticket(db, projet, item["id"], approve=False)
                    except PipelineError as exc:
                        say(str(exc))
                        escalations_in_a_row += 1

        with db._connect() as conn:
            spent_eur = float(
                conn.execute(
                    "SELECT COALESCE(SUM(cost_eur), 0) FROM runs WHERE project_id = ? AND id > ?",
                    (projet, run_marker),
                ).fetchone()[0]
            )
        reason = scheduler.should_stop(
            done, escalations_in_a_row, args.max_tickets, args.max_escalations,
            spent_eur=spent_eur, max_cost_eur=max_cost_eur,
        )
        if reason is not None:
            db.add_event(projet, None, "AUTO_STOP", reason)
            if reason == "budget":
                say(f"Arrêt : budget ({spent_eur:.2f} € dépensés)")
            else:
                say(f"Arrêt : {reason}")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cockpit", description="Pipeline Agentic Cockpit V2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    default_project = os.environ.get("PROJECT", "")

    p_run = sub.add_parser("run", help="Exécute un ticket : worker, Porte 0, Porte 1, commit")
    p_run.add_argument("ticket")
    p_run.add_argument("project", nargs="?", default=default_project)
    p_run.add_argument("--lane", default="")
    p_run.add_argument("--max-attempts", type=int, default=0)
    p_run.add_argument("--no-gate1", action="store_true")

    p_merge = sub.add_parser("merge", help="Porte d'intégration puis fusion et nettoyage")
    p_merge.add_argument("ticket")
    p_merge.add_argument("project", nargs="?", default=default_project)
    p_merge.add_argument("--approve", action="store_true", help="Valide un ticket en attente de revue (GATE2)")

    p_auto = sub.add_parser("auto", help="Boucle autonome sur la file de tickets")
    p_auto.add_argument("project", nargs="?", default=default_project)
    p_auto.add_argument("--slots", type=int, default=2)
    p_auto.add_argument("--max-tickets", type=int, default=6)
    p_auto.add_argument("--max-escalations", type=int, default=2)
    p_auto.add_argument("--max-cost-eur", type=float, default=0.0)
    p_auto.add_argument("--max-attempts", type=int, default=2)
    p_auto.add_argument("--gate1", action="store_true")
    p_auto.add_argument("--auto-merge", action="store_true")
    p_auto.add_argument("--dry-run", action="store_true")
    p_auto.add_argument("--parent", default="")

    p_ticket = sub.add_parser("ticket", help="Gestion des tickets (0 token)")
    p_ticket.add_argument("action", choices=["add", "list", "show"])
    p_ticket.add_argument("project")
    p_ticket.add_argument("title", nargs="?", default="")
    p_ticket.add_argument("--id", default="")
    p_ticket.add_argument("--milestone", default=None)
    p_ticket.add_argument("--prio", default="P2", choices=["P0", "P1", "P2", "P3"])
    p_ticket.add_argument("--size", default="S", choices=["XS", "S", "M", "L"])
    p_ticket.add_argument("--files", default="")
    p_ticket.add_argument("--readonly", default="")
    p_ticket.add_argument("--depends", default="")
    p_ticket.add_argument("--accept", action="append")
    p_ticket.add_argument("--contract", default="")
    p_ticket.add_argument("--contract-file", default="")
    p_ticket.add_argument("--test-cmd", default="")
    p_ticket.add_argument("--sensitive", action="store_true")
    p_ticket.add_argument("--parent", default="")

    p_events = sub.add_parser("events", help="Derniers événements du pipeline")
    p_events.add_argument("project", nargs="?", default=default_project)
    p_events.add_argument("--limit", type=int, default=15)

    p_claim = sub.add_parser("claim", help="Prend un ticket en mode chantier interactif")
    p_claim.add_argument("ticket")
    p_claim.add_argument("project", nargs="?", default=default_project)
    p_claim.add_argument("--as", dest="owner", required=True, choices=list(INTERACTIVE_OWNERS))

    p_submit = sub.add_parser("submit", help="Soumet le worktree d'un ticket interactif aux portes")
    p_submit.add_argument("ticket")
    p_submit.add_argument("project", nargs="?", default=default_project)

    p_release = sub.add_parser("release", help="Relâche un ticket interactif vers READY")
    p_release.add_argument("ticket")
    p_release.add_argument("project", nargs="?", default=default_project)

    p_sync = sub.add_parser("sync", help="Clôt les tickets PR_OPEN dont la PR est fusionnée (0 token)")
    p_sync.add_argument("project", nargs="?", default=default_project)

    p_status = sub.add_parser("status", help="État synthétique du projet (0 token)")
    p_status.add_argument("project", nargs="?", default=default_project)

    p_note = sub.add_parser("note", help="Enregistre une note utile aux autres agents (0 token)")
    p_note.add_argument("ticket")
    p_note.add_argument("project", nargs="?", default=default_project)
    p_note.add_argument("text", nargs="?", default="")

    p_abandon = sub.add_parser("abandon", help="Abandonne proprement un ticket ou un chantier")
    p_abandon.add_argument("ticket")
    p_abandon.add_argument("project", nargs="?", default=default_project)
    p_abandon.add_argument("--reason", default="")

    args = parser.parse_args(argv)
    db = CockpitDB(DB_PATH)
    try:
        if args.cmd == "run":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit run T-xxx <projet>")
            return run_ticket(db, args.project, args.ticket, args.lane, args.max_attempts, not args.no_gate1)
        if args.cmd == "merge":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit merge T-xxx <projet>")
            return merge_ticket(db, args.project, args.ticket, args.approve)
        if args.cmd == "auto":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit auto <projet>")
            return cmd_auto(db, args)
        if args.cmd == "claim":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit claim T-xxx <projet> --as <nom>")
            return claim_ticket(db, args.project, args.ticket, args.owner)
        if args.cmd == "submit":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit submit T-xxx <projet>")
            return submit_ticket(db, args.project, args.ticket)
        if args.cmd == "release":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit release T-xxx <projet>")
            return release_ticket(db, args.project, args.ticket)
        if args.cmd == "sync":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit sync <projet>")
            return sync_project(db, args.project)
        if args.cmd == "status":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit status <projet>")
            try:
                sync_project(db, args.project)
            except PipelineError as exc:
                say(f"sync : {exc}")
            print(build_status(db, args.project))
            return 0
        if args.cmd == "note":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit note T-xxx <projet> \"texte\"")
            return add_note(db, args.project, args.ticket, args.text)
        if args.cmd == "abandon":
            if not args.project:
                raise PipelineError("Projet manquant : cockpit abandon T-xxx <projet>")
            abandon_ticket(db, args.project, args.ticket, args.reason)
            return 0
        if args.cmd == "ticket":
            if args.action == "add" and not args.title:
                raise PipelineError("Titre manquant : cockpit ticket add <projet> \"titre\" --files a,b")
            if args.action == "show" and not args.id:
                raise PipelineError("Identifiant manquant : cockpit ticket show <projet> --id T-xxx")
            return cmd_ticket(db, args)
        if args.cmd == "events":
            return cmd_events(db, args.project, args.limit)
    except PipelineError as exc:
        print(f"REFUS : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
