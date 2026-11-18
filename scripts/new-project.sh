#!/usr/bin/env bash
# Agentic Cockpit V2 — Création d'un nouveau projet standardisé (T-105 + Mitigations T-104)
set -euo pipefail

HOME="${HOME:-/tmp}"
PROJ_ID="${1:-}"
[ -n "$PROJ_ID" ] || { echo "Usage: new-project.sh <id_projet> [chemin_racine] [port_sidecar]" >&2; exit 1; }
[[ "$PROJ_ID" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "Identifiant de projet invalide : $PROJ_ID (attendu: ^[a-z][a-z0-9-]*$)" >&2; exit 1; }

COCKPIT_HOME="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
COCKPIT_CONFIG_DIR="${COCKPIT_CONFIG_DIR:-$HOME/.config/agentic-cockpit}"
if [ -z "${PROJECTS_TOML:-}" ]; then
  mkdir -p "$COCKPIT_CONFIG_DIR"
  PROJECTS_TOML="$COCKPIT_CONFIG_DIR/projects.toml"
  if [ ! -f "$PROJECTS_TOML" ] && [ -f "$COCKPIT_HOME/config/projects.example.toml" ]; then
    cp "$COCKPIT_HOME/config/projects.example.toml" "$PROJECTS_TOML"
  fi
fi
PROJ_ROOT="${2:-$HOME/projets/$PROJ_ID}"
SIDECAR_PORT="${3:-8790}"
if ! [[ "$SIDECAR_PORT" =~ ^[0-9]+$ ]] || [ "$SIDECAR_PORT" -lt 1024 ] || [ "$SIDECAR_PORT" -gt 65535 ]; then
  echo "Port sidecar invalide : $SIDECAR_PORT (attendu: entier de 1024 à 65535)" >&2
  exit 1
fi
WT_SESS="${PROJ_ROOT}-worktrees"
WT_TICK="${PROJ_ROOT}-worktrees"
MIRROR_DIR="${MIRROR_DIR:-}"
DB_PATH="${DB_PATH:-$COCKPIT_HOME/db/cockpit_pmo.db}"

COCKPIT_HOME="$COCKPIT_HOME" PROJECTS_TOML="$PROJECTS_TOML" PROJ_ID="$PROJ_ID" \
PROJ_ROOT="$PROJ_ROOT" WT_SESS="$WT_SESS" WT_TICK="$WT_TICK" SIDECAR_PORT="$SIDECAR_PORT" \
python3 - << 'PY'
import os
import sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["COCKPIT_HOME"], "core"))
from pmo_db import validate_projects_ports

toml_path = Path(os.environ["PROJECTS_TOML"])
proj_id = os.environ["PROJ_ID"]
proj_root = os.environ["PROJ_ROOT"]
wt_sess = os.environ["WT_SESS"]
wt_tick = os.environ["WT_TICK"]
sidecar_port = os.environ["SIDECAR_PORT"]
original = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
header = f"[projects.{proj_id}]"
added = False
if header not in original:
    block = (
        f"\n[projects.{proj_id}]\n"
        f'name = "{proj_id.capitalize()}"\n'
        f'root = "{proj_root}"\n'
        f'worktrees_sessions_dir = "{wt_sess}"\n'
        f'worktrees_tickets_dir = "{wt_tick}"\n'
        f'default_branch = "main"\n'
        f'milestones = ["M1", "M2", "M3"]\n'
        f'lint_cmd = "make lint"\n'
        f'test_unit_cmd = "make test"\n'
        f"\n[projects.{proj_id}.ports]\n"
        f"sidecar = {sidecar_port}\n"
    )
    sep = "" if (not original or original.endswith("\n")) else "\n"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(original + sep + block, encoding="utf-8")
    added = True

if toml_path.exists():
    collisions = validate_projects_ports(toml_path)
    if collisions:
        if added:
            toml_path.write_text(original, encoding="utf-8")
        print("Collision de ports détectée : " + "; ".join(collisions), file=sys.stderr)
        sys.exit(1)
PY

mkdir -p "$PROJ_ROOT" "$WT_SESS" "$WT_TICK"
mkdir -p "$PROJ_ROOT/.nexus/rules"

HAS_GIT=0
if command -v git >/dev/null 2>&1; then
  HAS_GIT=1
fi

if [ "$HAS_GIT" -eq 1 ] && [ ! -d "$PROJ_ROOT/.git" ]; then
  git -C "$PROJ_ROOT" init -b main >/dev/null
  _GIT_NAME="${GIT_AUTHOR_NAME:-$(git config --global user.name 2>/dev/null || true)}"
  _GIT_EMAIL="${GIT_AUTHOR_EMAIL:-$(git config --global user.email 2>/dev/null || true)}"
  git -C "$PROJ_ROOT" config user.name "${_GIT_NAME:-cockpit}"
  git -C "$PROJ_ROOT" config user.email "${_GIT_EMAIL:-cockpit@localhost}"
else
  mkdir -p "$PROJ_ROOT/.git/info"
fi

EXCL="$PROJ_ROOT/.git/info/exclude"
mkdir -p "$(dirname "$EXCL")"
for pattern in ".agents" ".nexus_brief.md" ".agent-lane" ".handoff/"; do
  grep -qxF "$pattern" "$EXCL" 2>/dev/null || echo "$pattern" >> "$EXCL"
done

mkdir -p "$PROJ_ROOT/.githooks"
cp "$COCKPIT_HOME/templates/pre-commit" "$PROJ_ROOT/.githooks/pre-commit"
chmod +x "$PROJ_ROOT/.githooks/pre-commit"
if [ "$HAS_GIT" -eq 1 ]; then
  git -C "$PROJ_ROOT" config core.hooksPath .githooks || true
fi

sed "s|__PROJECT_ID__|$PROJ_ID|g; s|__PROJECT_ROOT__|$PROJ_ROOT|g" \
  "$COCKPIT_HOME/templates/AGENTS.common.md" > "$PROJ_ROOT/AGENTS.md"

cat << EOF > "$PROJ_ROOT/Makefile"
PROJECT := $PROJ_ID
COCKPIT_HOME ?= $COCKPIT_HOME
include \$(COCKPIT_HOME)/mk/agentic.mk

.PHONY: test lint run

test: ## Lance les tests unitaires du projet
	@if [ -d tests ]; then python3 -B -m unittest discover -s tests; else echo "Aucun test défini"; exit 0; fi

lint: ## Lance l'analyse statique du projet
	@echo "Aucun linter configuré"
EOF

cat << 'EOF' > "$PROJ_ROOT/.gitignore"
.env
.env.*
!.env.example
secrets/
*.key
.aider*
node_modules/
__pycache__/
.venv/
EOF

cp "$COCKPIT_HOME/templates/core.md" "$PROJ_ROOT/.nexus/rules/core.md"

cat << 'EOF' > "$PROJ_ROOT/.nexus/project.toml"
lint_cmd = "make lint"
test_cmd = "make test"
EOF

if [ "$HAS_GIT" -eq 1 ]; then
  ALLOW_MAIN=1 git -C "$PROJ_ROOT" add AGENTS.md Makefile .gitignore .githooks/pre-commit .nexus/rules/core.md .nexus/project.toml
  ALLOW_MAIN=1 git -C "$PROJ_ROOT" commit -m "chore($PROJ_ID): initialisation projet Agentic Cockpit V2" >/dev/null 2>&1 || true
fi

for agent in claude agy deepseek; do
  if [ ! -d "$WT_SESS/wt-$agent" ]; then
    if [ "$HAS_GIT" -eq 1 ]; then
      git -C "$PROJ_ROOT" worktree add "$WT_SESS/wt-$agent" -b "agent/$agent" >/dev/null 2>&1 || mkdir -p "$WT_SESS/wt-$agent"
    else
      mkdir -p "$WT_SESS/wt-$agent"
    fi
    ln -sf "$PROJ_ROOT/.agents" "$WT_SESS/wt-$agent/.agents" 2>/dev/null || true
  fi
done

COCKPIT_HOME="$COCKPIT_HOME" DB_PATH="$DB_PATH" PROJ_ID="$PROJ_ID" PROJ_ROOT="$PROJ_ROOT" \
WT_SESS="$WT_SESS" WT_TICK="$WT_TICK" MIRROR_DIR="$MIRROR_DIR" SIDECAR_PORT="$SIDECAR_PORT" \
python3 - << 'PY'
import os
import sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["COCKPIT_HOME"], "core"))
from pmo_db import CockpitDB

proj_id = os.environ["PROJ_ID"]
proj_root = os.environ["PROJ_ROOT"]
db = CockpitDB(Path(os.environ["DB_PATH"]))
db.register_project(
    proj_id=proj_id,
    name=proj_id.capitalize(),
    root_path=proj_root,
    wt_sessions_dir=os.environ["WT_SESS"],
    wt_tickets_dir=os.environ["WT_TICK"],
    default_branch="main",
    mirror_dir=os.environ["MIRROR_DIR"],
    sidecar_port=int(os.environ["SIDECAR_PORT"]),
    milestones=[("M1", "Fondations & Socle"), ("M2", "Fonctionnalités Cœur"), ("M3", "Stabilisation & Prod")],
)
db.export_markdown_roadmap(proj_id, Path(proj_root) / "docs" / "ROADMAP.md")
PY

echo "OK : Nouveau projet '$PROJ_ID' initialisé dans $PROJ_ROOT (worktrees + SQLite PMO prêts)."
