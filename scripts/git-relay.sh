#!/usr/bin/env bash
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR

COCKPIT_HOME="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
source "$COCKPIT_HOME/scripts/cockpit-env.sh"

refuse() { echo "REFUS : $*" >&2; exit 1; }

SUBCMD="${1:-}"
shift || true
M=""; F=""; T=""
for arg in "$@"; do
  case "$arg" in
    M=*) M="${arg#M=}" ;;
    F=*) F="${arg#F=}" ;;
    T=*) T="${arg#T=}" ;;
    T-[0-9]*) T="$arg" ;;
  esac
done

TOP="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$TOP" ] || refuse "hors d'un dépôt git."
cd "$TOP"

is_forbidden_file() {
  local p="${1#./}"
  case "$p" in
    .env.example|*/.env.example) return 1 ;;
    .env|*/.env|.env.*|*/.env.*|secrets|secrets/*|*/secrets/*|exclusion.key|*/exclusion.key|*.pem)
      return 0 ;;
  esac
  git check-ignore -q --no-index -- "$p" 2>/dev/null && return 0
  return 1
}

case "$SUBCMD" in
  commit)
    if [ -n "$F" ]; then
      for file in $F; do
        is_forbidden_file "$file" && refuse "fichier interdit ou ignoré : $file"
        git add -- "$file"
      done
    else
      git add -u
    fi
    staged="$(git diff --cached --name-only)"
    [ -n "$staged" ] || refuse "aucun fichier indexé."
    while IFS= read -r f; do
      [ -n "$f" ] && is_forbidden_file "$f" && refuse "fichier secret détecté dans l'index : $f"
    done <<< "$staged"

    if [ -z "$M" ]; then
      branch="$(git symbolic-ref --short -q HEAD || echo wip)"
      M="feat($branch): mise à jour validée par Porte 0"
    fi
    git commit -m "$M" >/dev/null
    echo "OK commit : $(git rev-parse --short HEAD) ($M)"
    ;;
  land)
    [ -n "$T" ] || refuse "usage : git-relay.sh land T=T-xxx"
    [ "$(realpath "$TOP")" = "$(realpath "$PROJECT_ROOT")" ] || refuse "land se lance depuis $PROJECT_ROOT"
    cur_branch="$(git symbolic-ref --short -q HEAD || true)"
    [ "$cur_branch" = "$DEFAULT_BRANCH" ] || refuse "land exige la branche $DEFAULT_BRANCH (actuelle: $cur_branch)"
    wt_path="$WT_TICKETS_DIR/$T"
    if [ -d "$wt_path" ]; then
      git worktree remove --force "$wt_path"
    fi
    git branch -D "feat/$T" >/dev/null 2>&1 || true
    COCKPIT_HOME="$COCKPIT_HOME" PROJECT="$PROJECT" T="$T" PROJECT_ROOT="$PROJECT_ROOT" python3 - << 'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ["COCKPIT_HOME"], "core"))
from pmo_db import CockpitDB

db = CockpitDB(Path(os.environ["COCKPIT_HOME"]) / "db" / "cockpit_pmo.db")
db.complete_and_merge_ticket(os.environ["PROJECT"], os.environ["T"])
db.export_markdown_roadmap(os.environ["PROJECT"], Path(os.environ["PROJECT_ROOT"]) / "docs" / "ROADMAP.md")
PY
    echo "OK land : $T nettoyé et dépendants débloqués dans SQLite."
    ;;
  *)
    refuse "sous-commande inconnue : $SUBCMD (attendu: commit | land)"
    ;;
esac
