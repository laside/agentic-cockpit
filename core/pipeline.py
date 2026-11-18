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
import json
import os
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

import costs  # noqa: E402
import scheduler  # noqa: E402
from context_compiler import compile_ticket_brief  # noqa: E402
from gatekeeper import build_nas_gate1_payload, sieve_test_output, verify_worktree_scope  # noqa: E402
from pmo_db import CockpitDB  # noqa: E402

DB_PATH = Path(os.environ.get("DB_PATH", COCKPIT_HOME / "db" / "cockpit_pmo.db"))
BRIEF_NAME = ".nexus_brief.md"
RUNNABLE_STATES = {"READY", "ASSIGNED", "GATE0", "GATE1"}


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
    cfg = load_toml(COCKPIT_HOME / "config" / "runners.toml")
    if "runners" not in cfg:
        raise PipelineError("config/runners.toml introuvable ou sans section [runners].")
    return cfg


def project_commands(project_id: str, root: Path, workdir: Path) -> tuple[str, str]:
    """Retourne (lint_cmd, test_cmd) : .nexus/project.toml du projet, puis projects.toml, puis make."""
    local = load_toml(workdir / ".nexus" / "project.toml") or load_toml(root / ".nexus" / "project.toml")
    central = load_toml(COCKPIT_HOME / "config" / "projects.toml").get("projects", {}).get(project_id, {})
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


def ensure_worktree(proj: dict[str, Any], ticket_id: str) -> Path:
    root = Path(proj["root_path"])
    wt = Path(proj["wt_tickets_dir"]) / ticket_id
    branch = f"feat/{ticket_id}"
    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    has_branch = sh(["git", "rev-parse", "--verify", "--quiet", branch], root, check=False).returncode == 0
    args = ["git", "worktree", "add", str(wt)] + ([branch] if has_branch else ["-b", branch])
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


def write_brief(ticket: dict[str, Any], wt: Path) -> Path:
    readonly = json.loads(ticket.get("readonly_json") or "[]")
    brief = compile_ticket_brief(wt, ticket, wt / ".nexus" / "rules", readonly or None)
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
    """Audit du diff par le modèle local. Verdicts : PASS | FAIL | SKIPPED (NAS injoignable ou réponse illisible)."""
    system = (COCKPIT_HOME / "prompts" / "sys_gate1_nas32k.md").read_text(encoding="utf-8")
    payload = build_nas_gate1_payload(ticket_brief, diff, system, model=cfg.get("model", "Gemma4-ctx:32k"))
    # Sans réflexion, l'audit répond en quelques secondes ; avec, il peut dépasser plusieurs minutes.
    payload["think"] = bool(cfg.get("think", False))
    req = urllib.request.Request(
        cfg.get("url", "http://192.168.0.10:11434").rstrip("/") + "/api/generate",
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


def run_ticket(db: CockpitDB, project_id: str, ticket_id: str, lane_override: str = "",
               max_attempts: int = 0, use_gate1: bool = True) -> int:
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj:
        raise PipelineError(f"Projet inconnu : {project_id}")
    if not ticket:
        raise PipelineError(f"Ticket inconnu : {ticket_id} (projet {project_id})")
    if ticket["state"] not in RUNNABLE_STATES:
        raise PipelineError(f"{ticket_id} est à l'état {ticket['state']} : exécution refusée.")

    cfg = load_runners()
    defaults = cfg.get("defaults", {})
    max_attempts = max_attempts or int(defaults.get("max_attempts", 3))
    files_owned = json.loads(ticket.get("files_owned_json") or "[]")
    if not files_owned:
        raise PipelineError(f"{ticket_id} n'a aucun fichier dans files_owned : périmètre non vérifiable.")

    ok, conflicts = db.acquire_locks(project_id, ticket_id, files_owned)
    if not ok:
        raise PipelineError("Fichiers déjà verrouillés : " + ", ".join(conflicts))

    wt = ensure_worktree(proj, ticket_id)
    brief_path = write_brief(ticket, wt)
    base = sh(["git", "merge-base", "HEAD", proj["default_branch"]], wt).stdout.strip()
    log_dir = COCKPIT_HOME / "logs" / project_id / ticket_id
    say(f"{ticket_id} : worktree {wt}, brief {brief_path.stat().st_size} octets, base {base[:7]}")

    feedback = ""
    artefacts: set[str] = set()
    gate1_reworks = 0
    gate1_status = "SKIPPED"
    gate1_notes: list[str] = []

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
        before = costs.snapshot_opencode_stats() if runner.get("cost_source") == "opencode_stats" else None
        code, duration = run_worker(argv, wt, log_dir / f"attempt-{attempt}.log",
                                    int(runner.get("timeout_s", defaults.get("timeout_s", 900))), extra_env)
        after = costs.snapshot_opencode_stats() if before is not None else None
        say(f"{ticket_id} : worker terminé (code {code}, {duration:.0f}s)")

        db.update_ticket(project_id, ticket_id, state="GATE0")
        g0, g0_feedback, changed = gate0(
            ticket, proj, wt, base, int(defaults.get("gate_timeout_s", 600)), artefacts
        )
        run_fields: dict[str, Any] = dict(attempt=attempt, lane=lane, provider=runner.get("provider", ""),
                                          exit_code=code, duration_s=round(duration, 1), gate0=g0)
        if before is not None and after is not None:
            d = costs.delta(before, after)
            usd_to_eur = float(defaults.get("usd_to_eur", 0.92))
            cost_eur = round(d["total_cost_usd"] * usd_to_eur, 4)
            run_fields["tokens_in"] = d["input_tokens"]
            run_fields["tokens_out"] = d["output_tokens"]
            run_fields["cost_eur"] = cost_eur
            if cost_eur > 0:
                db.update_quota_usage(runner["provider"], add_spent_eur=cost_eur)
            say(f"{ticket_id} : coût {cost_eur:.4f} € (tokens in={d['input_tokens']}, out={d['output_tokens']})")
        if g0 == "SECRET":
            db.record_run(project_id, ticket_id, **run_fields)
            db.update_ticket(project_id, ticket_id, state="GATE2")
            db.add_event(project_id, ticket_id, "GATE0_SECRET", g0_feedback)
            say(f"{ticket_id} : ARRÊT — {g0_feedback}. Intervention humaine requise.")
            return 2
        if g0 == "FAIL" and code != 0 and not changed:
            # Le CLI lui-même a échoué (modèle inconnu, quota, authentification) : ce n'est pas un échec du ticket.
            run_fields["gate0"] = "WORKER_ERROR"
            db.record_run(project_id, ticket_id, **run_fields)
            db.update_ticket(project_id, ticket_id, state="READY")
            db.add_event(project_id, ticket_id, "WORKER_ERROR", f"voie {lane} code {code}")
            say(f"{ticket_id} : le worker de la voie {lane} a échoué (code {code}) sans rien produire."
                f" Ticket remis à READY. Journal : {log_dir / f'attempt-{attempt}.log'}")
            return 3
        if g0 == "FAIL":
            db.record_run(project_id, ticket_id, **run_fields)
            db.update_ticket(project_id, ticket_id, fail_count=ticket["fail_count"] + 1)
            db.add_event(project_id, ticket_id, "GATE0_FAIL", g0_feedback[:300])
            say(f"{ticket_id} : Porte 0 refusée —\n{g0_feedback}")
            feedback = g0_feedback
            continue
        say(f"{ticket_id} : Porte 0 OK ({len(changed)} fichier(s) : {', '.join(changed)})")

        # Indexe uniquement les fichiers du périmètre, puis audite le diff complet depuis la base.
        sh(["git", "add", "--", *changed], wt)
        diff = sh(["git", "diff", "--cached", base], wt).stdout
        if use_gate1:
            db.update_ticket(project_id, ticket_id, state="GATE1")
            say(f"{ticket_id} : Porte 1 — audit du diff ({len(diff)} caractères) par le modèle local…")
            gate1_status, gate1_notes, _ = gate1(
                brief_path.read_text(encoding="utf-8"), diff, cfg.get("gate1", {}),
                int(defaults.get("gate1_timeout_s", 300)),
            )
            say(f"{ticket_id} : Porte 1 {gate1_status}" + (f" — {'; '.join(gate1_notes)[:400]}" if gate1_notes else ""))
        run_fields["gate1"] = gate1_status
        db.record_run(project_id, ticket_id, **run_fields)

        max_reworks = int(cfg.get("gate1", {}).get("max_reworks", 1))
        if gate1_status == "FAIL" and gate1_reworks < max_reworks and attempt < max_attempts:
            gate1_reworks += 1
            db.add_event(project_id, ticket_id, "GATE1_FAIL", "; ".join(gate1_notes)[:300])
            feedback = "L'audit du diff signale :\n- " + "\n- ".join(gate1_notes)
            continue

        # Commit déterministe du travail validé par la Porte 0.
        if sh(["git", "diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
            sh(["git", "commit", "-m", f"feat({ticket_id}): {ticket['title'][:72]}"], wt)
        needs_arch = bool(ticket.get("touches_sensitive")) or gate1_status != "PASS"
        state = "GATE2" if needs_arch else "VALIDATED"
        db.update_ticket(project_id, ticket_id, state=state)
        db.add_event(project_id, ticket_id, state, f"gate0=PASS gate1={gate1_status} tentative={attempt}")
        write_report(log_dir, ticket, lane, attempt, changed, gate1_status, gate1_notes, state)
        say(f"{ticket_id} : {state}" + (" (revue architecte requise)" if needs_arch else " — prêt à fusionner"))
        return 0

    db.update_ticket(project_id, ticket_id, state="GATE2")
    db.add_event(project_id, ticket_id, "ESCALATED", f"{max_attempts} tentatives sans succès")
    say(f"{ticket_id} : {max_attempts} tentatives sans succès — escalade (état GATE2). Dernier retour :\n{feedback}")
    return 1


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


def merge_ticket(db: CockpitDB, project_id: str, ticket_id: str, approve: bool = False) -> int:
    proj = db.get_project(project_id)
    ticket = db.get_ticket(project_id, ticket_id)
    if not proj or not ticket:
        raise PipelineError(f"Projet ou ticket inconnu : {project_id} / {ticket_id}")
    if ticket["state"] == "GATE2" and not approve:
        raise PipelineError(f"{ticket_id} attend une revue (GATE2). Relire le diff puis relancer avec --approve.")
    if ticket["state"] not in ("VALIDATED", "GATE2"):
        raise PipelineError(f"{ticket_id} est à l'état {ticket['state']} : fusion refusée.")

    root = Path(proj["root_path"])
    wt = Path(proj["wt_tickets_dir"]) / ticket_id
    branch, default = f"feat/{ticket_id}", proj["default_branch"]
    if sh(["git", "symbolic-ref", "--short", "HEAD"], root).stdout.strip() != default:
        raise PipelineError(f"Le dépôt racine doit être sur {default}.")
    if not wt.exists():
        raise PipelineError(f"Worktree introuvable : {wt}")
    if sh(["git", "status", "--porcelain", "--untracked-files=no"], wt).stdout.strip():
        raise PipelineError(f"{ticket_id} a des modifications non commitées dans son worktree.")

    # Porte d'intégration : on ramène la branche par défaut dans le ticket et on re-teste AVANT de fusionner.
    res = sh(["git", "merge", "--no-edit", default], wt, check=False)
    if res.returncode != 0:
        sh(["git", "merge", "--abort"], wt, check=False)
        db.add_event(project_id, ticket_id, "MERGE_CONFLICT", res.stdout[-300:])
        raise PipelineError(f"Conflit entre {branch} et {default} : fusion annulée, rien n'a changé.")
    lint_cmd, default_test = project_commands(project_id, root, wt)
    timeout = int(load_runners().get("defaults", {}).get("gate_timeout_s", 600))
    for label, cmd in (("lint", lint_cmd), ("tests", default_test), ("test du ticket", ticket.get("test_cmd") or "")):
        if not cmd:
            continue
        code, out = run_shell(cmd, wt, timeout)
        if code != 0:
            db.add_event(project_id, ticket_id, "INTEGRATION_FAIL", f"{label}: {cmd}")
            raise PipelineError(f"Porte d'intégration refusée ({label}, `{cmd}`) :\n{sieve_test_output(out, 25)}")
    say(f"{ticket_id} : porte d'intégration OK ({default} intégré, lint et tests verts)")

    sh(["git", "merge", "--no-ff", branch, "-m", f"merge({ticket_id}): {ticket['title'][:72]}"], root)
    sh(["git", "worktree", "remove", "--force", str(wt)], root, check=False)
    sh(["git", "branch", "-D", branch], root, check=False)
    unblocked = db.complete_and_merge_ticket(project_id, ticket_id)
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


# ───────────────────────────── tickets & vues ─────────────────────────────


def next_ticket_id(db: CockpitDB, project_id: str) -> str:
    with db._connect() as conn:
        rows = conn.execute("SELECT id FROM tickets WHERE project_id = ?", (project_id,)).fetchall()
    nums = [int(r["id"][2:]) for r in rows if r["id"].startswith("T-") and r["id"][2:].isdigit()]
    return f"T-{(max(nums) + 1) if nums else 1:03d}"


def cmd_ticket(db: CockpitDB, args: argparse.Namespace) -> int:
    if args.action == "add":
        if not db.get_project(args.project):
            raise PipelineError(f"Projet inconnu : {args.project}")
        tid = args.id or next_ticket_id(db, args.project)
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
        )
        t = db.get_ticket(args.project, tid) or {}
        say(f"{tid} créé ({t.get('state')}) dans {args.project}")
        return 0
    if args.action == "show":
        t = db.get_ticket(args.project, args.id)
        if not t:
            raise PipelineError(f"Ticket inconnu : {args.id}")
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0
    for t in db.get_prioritized_queue(args.project):
        lane = t.get("lane") or db.select_optimal_lane(t["complexity"], t["priority"], t["fail_count"])["lane"]
        print(f"{t['id']:<7} {t['milestone_id'] or '-':<5} {t['priority']:<3} [{t['complexity']:<2}] "
              f"{t['state']:<10} {lane:<16} {t['title'][:70]}")
    return 0


def cmd_events(db: CockpitDB, project_id: str, limit: int) -> int:
    for e in reversed(db.recent_events(project_id, limit)):
        print(f"{e['created_at']}  {e['ticket_id'] or '-':<7} {e['kind']:<16} {e['detail']}")
    return 0


def cmd_auto(db: CockpitDB, args: argparse.Namespace) -> int:
    projet = args.project
    done = 0
    escalations_in_a_row = 0
    while True:
        batch = scheduler.plan_batch(db, projet, args.slots)
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
            except PipelineError as exc:
                say(str(exc))
                results[tid] = 1

        threads = [threading.Thread(target=_worker, args=(item["id"],)) for item in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for item in batch:
            code = results.get(item["id"], 1)
            if code == 0:
                done += 1
                escalations_in_a_row = 0
            else:
                escalations_in_a_row += 1

        if args.auto_merge:
            for item in batch:
                t_info = db.get_ticket(projet, item["id"])
                if t_info and t_info.get("state") == "VALIDATED":
                    try:
                        merge_ticket(db, projet, item["id"], approve=False)
                    except PipelineError as exc:
                        say(str(exc))
                        escalations_in_a_row += 1

        reason = scheduler.should_stop(done, escalations_in_a_row, args.max_tickets, args.max_escalations)
        if reason is not None:
            db.add_event(projet, None, "AUTO_STOP", reason)
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
    p_auto.add_argument("--max-attempts", type=int, default=2)
    p_auto.add_argument("--gate1", action="store_true")
    p_auto.add_argument("--auto-merge", action="store_true")
    p_auto.add_argument("--dry-run", action="store_true")

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

    p_events = sub.add_parser("events", help="Derniers événements du pipeline")
    p_events.add_argument("project", nargs="?", default=default_project)
    p_events.add_argument("--limit", type=int, default=15)

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
