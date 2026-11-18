#!/usr/bin/env python3
"""
Calibration du juge de revue (T-027) : mesure honnête du taux de détection des
mauvais diffs et du taux de faux positifs. Stdlib uniquement, aucun réseau.

    python3 core/judge_calibrate.py [--runs N] [--cases DIR] [--json CHEMIN]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parent
COCKPIT_HOME = Path(os.environ.get("COCKPIT_HOME", CORE.parent))

try:  # import packagé (core.judge_calibrate) ou exécution directe du script
    from . import config_paths
    from . import judge as judge_mod
except ImportError:  # pragma: no cover - dépend du mode d'import
    import config_paths  # type: ignore[no-redef]
    import judge as judge_mod  # type: ignore[no-redef]

RUNNERS_PATH = config_paths.config_file("runners.toml")
DEFAULT_CASES_DIR = COCKPIT_HOME / "tests" / "judge_cases"

PROMPT_PATH = getattr(
    judge_mod, "SYS_PROMPT_PATH", COCKPIT_HOME / "prompts" / "sys_judge_claude.md"
)


def _prompt_sha256() -> str:
    """Hachage SHA-256 du prompt système du juge (chaîne vide si illisible)."""
    try:
        contenu = Path(PROMPT_PATH).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(contenu).hexdigest()


def _verdict_appele(
    juge, brief: str, diff: str, cfg: dict[str, Any]
) -> tuple[str, list[str]]:
    """Appelle le juge une fois et normalise le verdict en (PASS | FAIL | SKIPPED, defects)."""
    try:
        resultat = juge(brief, diff, cfg)
    except Exception as exc:  # un juge en échec ne doit jamais faire passer un cas
        return "SKIPPED", [f"exception : {exc}"]
    defects: list[str] = []
    if isinstance(resultat, str):
        verdict = resultat
    elif isinstance(resultat, (tuple, list)) and resultat:
        verdict = resultat[0]
        if len(resultat) >= 2 and isinstance(resultat[1], (list, tuple)):
            defects = [str(d) for d in resultat[1]]
    else:
        verdict = "SKIPPED"
    verdict_norm = verdict if verdict in ("PASS", "FAIL") else "SKIPPED"
    return verdict_norm, defects


def calibrate(
    cases_dir,
    cfg: dict[str, Any],
    runs: int = 1,
    judge=None,
) -> dict[str, Any]:
    """Évalue le juge sur les cas de `cases_dir` et retourne les statistiques."""
    base = Path(cases_dir)
    entrees = json.loads((base / "cases.json").read_text(encoding="utf-8"))
    nb_runs = max(int(runs), 1)
    juge = judge if judge is not None else judge_mod.judge_diff

    cas_resultats: list[dict[str, Any]] = []
    detected = 0
    bad_total = 0
    false_positives = 0
    good_total = 0
    skipped = 0

    for entree in entrees:
        nom = str(entree.get("name", ""))
        attendu = str(entree.get("expected", "")).upper()
        brief = (base / entree["brief"]).read_text(encoding="utf-8")
        diff = (base / entree["diff"]).read_text(encoding="utf-8")

        verdicts: list[str] = []
        defects_par_passe: list[list[str]] = []
        debut = time.monotonic()
        for _ in range(nb_runs):
            verdict, motifs = _verdict_appele(juge, brief, diff, cfg)
            verdicts.append(verdict)
            defects_par_passe.append(motifs)
        secondes = time.monotonic() - debut

        if attendu == "FAIL":
            bad_total += 1
            ok = all(v == "FAIL" for v in verdicts)
            if ok:
                detected += 1
        else:
            good_total += 1
            ok = all(v == "PASS" for v in verdicts)
            if any(v == "FAIL" for v in verdicts):
                false_positives += 1

        if any(v == "SKIPPED" for v in verdicts):
            skipped += 1

        cas_resultats.append(
            {
                "name": nom,
                "expected": attendu,
                "verdicts": verdicts,
                "defects": defects_par_passe,
                "ok": ok,
                "seconds": secondes,
            }
        )

    passed = (
        detected == bad_total
        and false_positives == 0
        and skipped == 0
    )

    return {
        "cases": cas_resultats,
        "detected": detected,
        "bad_total": bad_total,
        "false_positives": false_positives,
        "good_total": good_total,
        "skipped": skipped,
        "passed": passed,
        "prompt_sha256": _prompt_sha256(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": nb_runs,
        "judge_model": str(cfg.get("judge_model", "")),
    }


def calibration_status(
    report_path=None,
    max_age_days: int = 14,
    now=None,
) -> tuple[bool, str]:
    """Vérifie si la calibration du juge est valide, récente et conforme au prompt courant."""
    chemin = (
        Path(report_path)
        if report_path is not None
        else COCKPIT_HOME / "logs" / "judge-calibration.json"
    )
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, "aucun rapport de calibration"
    if not isinstance(data, dict):
        return False, "aucun rapport de calibration"

    if not data.get("passed"):
        detected = int(data.get("detected") or 0)
        bad_total = int(data.get("bad_total") or 0)
        false_pos = int(data.get("false_positives") or 0)
        return (
            False,
            f"juge non fiable (détectés {detected}/{bad_total}, faux positifs {false_pos})",
        )

    actuel_sha = _prompt_sha256()
    if not actuel_sha or data.get("prompt_sha256") != actuel_sha:
        return False, "le prompt a changé depuis la calibration"

    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)):
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    elif isinstance(now, str):
        now_dt = datetime.fromisoformat(now)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

    raw_gen = data.get("generated_at")
    if not isinstance(raw_gen, str) or not raw_gen.strip():
        return False, "calibration trop ancienne (inconnue jours)"
    try:
        gen_dt = datetime.fromisoformat(raw_gen)
    except ValueError:
        return False, "calibration trop ancienne (inconnue jours)"
    if gen_dt.tzinfo is None:
        gen_dt = gen_dt.replace(tzinfo=timezone.utc)

    age_seconds = (now_dt - gen_dt).total_seconds()
    age_days = int(age_seconds // 86400)
    if age_seconds >= max_age_days * 86400:
        return False, f"calibration trop ancienne ({age_days} jours)"

    runs = int(data.get("runs") or 0)
    bad_total = int(data.get("bad_total") or 0)
    if runs < 3 or bad_total < 10:
        return False, f"calibration trop légère (runs={runs}, cas={bad_total})"

    return True, ""


def _charger_gate1(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data.get("gate1", {})


def _afficher(resultat: dict[str, Any]) -> None:
    print(f"{'Cas':<24} {'Attendu':<8} {'Obtenu':<22} Durée")
    for cas in resultat["cases"]:
        obtenu = ",".join(cas["verdicts"])
        print(
            f"{cas['name']:<24} {cas['expected']:<8} {obtenu:<22} "
            f"{cas['seconds']:.2f}s"
        )
        if not cas["ok"]:
            motif = next(
                (str(m) for passe in cas.get("defects", []) for m in passe if str(m).strip()),
                "",
            )[:200]
            print(f"  motif : {motif}")
    fiabilite = "JUGE FIABLE" if resultat["passed"] else "JUGE NON FIABLE"
    print(
        f"Détectés {resultat['detected']}/{resultat['bad_total']} — "
        f"faux positifs {resultat['false_positives']}/{resultat['good_total']} — "
        f"ignorés {resultat['skipped']} — VERDICT : {fiabilite}"
    )


def main(argv: list[str]) -> int:
    """Entrée CLI `cockpit judge-calibrate` : code 0 seulement si le juge est fiable."""
    parser = argparse.ArgumentParser(
        prog="cockpit judge-calibrate",
        description="Calibre le juge de revue sur un jeu de cas étiquetés.",
    )
    parser.add_argument("--runs", type=int, default=1, help="appels du juge par cas")
    parser.add_argument(
        "--cases", default=str(DEFAULT_CASES_DIR), help="répertoire des cas"
    )
    parser.add_argument("--json", dest="json_path", default=None, help="écrit le rapport JSON")
    args = parser.parse_args(argv)

    cfg = _charger_gate1(RUNNERS_PATH)
    resultat = calibrate(args.cases, cfg, runs=args.runs)
    _afficher(resultat)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(resultat, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return 0 if resultat["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
