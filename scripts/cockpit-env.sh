#!/usr/bin/env bash
COCKPIT_HOME="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
_COCKPIT_CFG_DIR="${COCKPIT_CONFIG_DIR:-$HOME/.config/agentic-cockpit}"
if [ -z "${PROJECTS_TOML:-}" ]; then
  if [ -f "$_COCKPIT_CFG_DIR/projects.toml" ]; then
    PROJECTS_TOML="$_COCKPIT_CFG_DIR/projects.toml"
  elif [ -f "$COCKPIT_HOME/config/projects.toml" ]; then
    PROJECTS_TOML="$COCKPIT_HOME/config/projects.toml"
  else
    PROJECTS_TOML="$COCKPIT_HOME/config/projects.example.toml"
  fi
fi
PROJECT="${PROJECT:-cockpit}"

if [ -f "$PROJECTS_TOML" ] && command -v python3 >/dev/null 2>&1; then
  eval "$(PROJECT="$PROJECT" PROJECTS_TOML="$PROJECTS_TOML" COCKPIT_HOME="$COCKPIT_HOME" python3 - << 'PY'
import os, shlex, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["COCKPIT_HOME"]).expanduser() / "core"))
from pmo_db import parse_toml_text

p_id = os.environ["PROJECT"]
toml_path = Path(os.environ["PROJECTS_TOML"]).expanduser()
try:
    data = parse_toml_text(toml_path.read_text(encoding="utf-8"))
    cfg = data.get("projects", {}).get(p_id, {})
except Exception:
    cfg = {}

def exp(val: str, default: str) -> str:
    v = val if val else default
    return str(Path(v).expanduser()) if v.startswith("~") else v

name = cfg.get("name", p_id.capitalize())
root = exp(cfg.get("root", ""), os.path.expanduser(f"~/projets/{p_id}"))
wt_sess = exp(cfg.get("worktrees_sessions_dir", ""), f"{root}-worktrees")
wt_tick = exp(cfg.get("worktrees_tickets_dir", ""), f"{root}-worktrees")
branch = cfg.get("default_branch", "main")
mirror = cfg.get("mirror_dir", "")
ports = cfg.get("ports", {})
sidecar_port = str(ports.get("sidecar", 8765))

print(f"export PROJECT_NAME={shlex.quote(name)}")
print(f"export PROJECT_ROOT={shlex.quote(root)}")
print(f"export WT_SESSIONS_DIR={shlex.quote(wt_sess)}")
print(f"export WT_TICKETS_DIR={shlex.quote(wt_tick)}")
print(f"export DEFAULT_BRANCH={shlex.quote(branch)}")
print(f"export MIRROR_DIR={shlex.quote(mirror)}")
print(f"export SIDECAR_PORT={shlex.quote(sidecar_port)}")
PY
)"
else
  export PROJECT_NAME="${PROJECT_NAME:-$PROJECT}"
  export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/projets/$PROJECT}"
  export WT_SESSIONS_DIR="${WT_SESSIONS_DIR:-$HOME/projets/$PROJECT-worktrees}"
  export WT_TICKETS_DIR="${WT_TICKETS_DIR:-$HOME/projets/$PROJECT-worktrees}"
  export DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
  export MIRROR_DIR="${MIRROR_DIR:-}"
  export SIDECAR_PORT="${SIDECAR_PORT:-8765}"
fi
