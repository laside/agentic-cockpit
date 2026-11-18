#!/usr/bin/env bash
# Lance le pipeline d'un ticket (cockpit run) dans le panneau Zellij de la voie choisie par le Gouverneur.
# Le pipeline crée le worktree, compile le brief, lance le worker headless et passe les portes : tout est visible.
set -euo pipefail

COCKPIT_HOME="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
DB_PATH="${DB_PATH:-$COCKPIT_HOME/db/cockpit_pmo.db}"
TICKET="${1:-}"
PROJECT="${2:-${PROJECT:-}}"
[ -n "$TICKET" ] && [ -n "$PROJECT" ] || { echo "Usage: cockpit dispatch T-xxx <projet>" >&2; exit 1; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "Identifiant de ticket invalide : $TICKET (attendu: ^T-[0-9]+$)" >&2; exit 1; }
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "Identifiant de projet invalide : $PROJECT (attendu: ^[a-z][a-z0-9-]*$)" >&2; exit 1; }
SESSION="${ZELLIJ_SESSION_NAME:-cockpit-$PROJECT}"

# Voie recommandée -> préfixe du titre du panneau. Séparateur tabulation : les titres contiennent des espaces.
IFS=$'\t' read -r LANE PANE_PREFIX <<< "$(COCKPIT_HOME="$COCKPIT_HOME" DB_PATH="$DB_PATH" P="$PROJECT" T="$TICKET" python3 - << 'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["COCKPIT_HOME"], "core"))
from pmo_db import CockpitDB

db = CockpitDB(Path(os.environ["DB_PATH"]))
t = db.get_ticket(os.environ["P"], os.environ["T"])
if not t:
    raise SystemExit("Ticket introuvable")
lane = db.select_optimal_lane(t["complexity"], t["priority"], t["fail_count"])["lane"]
print(f"{lane}\t{'AGY' if lane.startswith('agy') else 'DeepSeek / Mistral'}")
PY
)"
[ -n "$LANE" ] || exit 1
RUN_CMD="$COCKPIT_HOME/bin/cockpit run $TICKET $PROJECT"

PANES_JSON="$(zellij --session "$SESSION" action list-panes --json 2>/dev/null || echo "[]")"
PANE_ID="$(printf '%s\n' "$PANES_JSON" | PFX="$PANE_PREFIX" python3 -c '
import json, os, sys
panes = json.loads(sys.stdin.read() or "[]")
pfx = os.environ["PFX"]
for p in panes:
    if isinstance(p, dict) and not p.get("is_plugin") and str(p.get("title", "")).startswith(pfx):
        print(p["id"]); break
')"
[ -n "$PANE_ID" ] || { echo "Panneau '$PANE_PREFIX' introuvable dans la session $SESSION. Lancer à la main : $RUN_CMD" >&2; exit 1; }

zellij --session "$SESSION" action write-chars --pane-id "terminal_$PANE_ID" "$RUN_CMD"
sleep 0.3
zellij --session "$SESSION" action send-keys --pane-id "terminal_$PANE_ID" Enter
echo "OK : $TICKET lancé sur la voie '$LANE' dans le panneau '$PANE_PREFIX' (terminal_$PANE_ID)."
