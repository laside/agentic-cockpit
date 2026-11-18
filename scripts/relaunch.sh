#!/usr/bin/env bash
# Relance différée du cockpit après une mise à jour : minuteur, puis reprise de la dernière session.
#
#   relaunch.sh [options]
#     --project P       projet (défaut : $PROJECT ou mon-app)
#     --in SECONDES     minuteur avant action (défaut : 60)
#     --dashboard       redémarre le panneau « PMO Roadmap » (prend en compte tui/pmo_dashboard.py à jour)
#     --resume          relance `claude --continue` dans le panneau Architecte, SANS jamais écrire
#                       dans un panneau où claude tourne encore (attend qu'il ait quitté)
#     --cmd "CMD"       commande de reprise à la place de `claude --continue`
#     --wait SECONDES   attente maximale de la sortie de claude pour --resume (défaut : 900)
#     --foreground      ne pas se détacher (par défaut le script passe en arrière-plan)
#
# Fonctionnement attendu : j'applique la mise à jour, je lance ce script avec un minuteur, puis je quitte
# claude (/exit). Le script retape `claude --continue` : la conversation reprend là où elle s'était arrêtée.
# Journal : ~/agentic-cockpit/logs/relaunch.log
set -euo pipefail

COCKPIT_HOME="${COCKPIT_HOME:-$HOME/agentic-cockpit}"
PROJECT="${PROJECT:-mon-app}"
DELAY=60
DO_DASH=0
DO_RESUME=0
RESUME_CMD="claude --continue"
WAIT_MAX=900
FOREGROUND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --in) DELAY="$2"; shift 2 ;;
    --dashboard) DO_DASH=1; shift ;;
    --resume) DO_RESUME=1; shift ;;
    --cmd) RESUME_CMD="$2"; DO_RESUME=1; shift 2 ;;
    --wait) WAIT_MAX="$2"; shift 2 ;;
    --foreground) FOREGROUND=1; shift ;;
    *) echo "Option inconnue : $1" >&2; exit 1 ;;
  esac
done
[[ "$DELAY" =~ ^[0-9]+$ && "$WAIT_MAX" =~ ^[0-9]+$ ]] || { echo "--in et --wait attendent un nombre de secondes" >&2; exit 1; }

LOG="$COCKPIT_HOME/logs/relaunch.log"
mkdir -p "$(dirname "$LOG")"

if [ "$FOREGROUND" -eq 0 ]; then
  # Détaché de la session appelante : le minuteur survit à la fin de la conversation.
  child=("$0" --foreground --project "$PROJECT" --in "$DELAY" --wait "$WAIT_MAX")
  [ "$DO_DASH" -eq 1 ] && child+=(--dashboard)
  [ "$DO_RESUME" -eq 1 ] && child+=(--cmd "$RESUME_CMD")
  setsid nohup "${child[@]}" >> "$LOG" 2>&1 < /dev/null &
  echo "Relance programmée dans ${DELAY}s (projet $PROJECT). Journal : $LOG"
  exit 0
fi

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

source "$COCKPIT_HOME/scripts/cockpit-env.sh" 2>/dev/null || true
export PROJECT
# Variable d'environnement : la substitution est faite par bash, pas par le script Python.
SESSION="${ZELLIJ_SESSION_NAME:-cockpit-$PROJECT}"
WT_CLAUDE="${WT_SESSIONS_DIR:-$HOME/projets/$PROJECT-worktrees}/wt-claude"

panes_json() { zellij --session "$SESSION" action list-panes --json 2>/dev/null || echo "[]"; }

# pane_field TITRE_PREFIX CHAMP -> valeur du premier panneau terminal dont le titre commence par le préfixe
pane_field() {
  panes_json | PFX="$1" FIELD="$2" python3 -c '
import json, os, sys
for p in json.loads(sys.stdin.read() or "[]"):
    if isinstance(p, dict) and not p.get("is_plugin") and str(p.get("title", "")).startswith(os.environ["PFX"]):
        print(p.get(os.environ["FIELD"], "")); break
'
}

log "relance programmée : projet=$PROJECT session=$SESSION délai=${DELAY}s dashboard=$DO_DASH reprise=$DO_RESUME"
sleep "$DELAY"

if ! zellij list-sessions --no-formatting 2>/dev/null | grep "^$SESSION " | grep -vq EXITED; then
  log "ÉCHEC : session $SESSION absente ou terminée. Relancer à la main : $COCKPIT_HOME/bin/cockpit open $PROJECT"
  exit 1
fi

if [ "$DO_DASH" -eq 1 ]; then
  id="$(pane_field 'PMO Roadmap' id)"
  if [ -n "$id" ]; then
    # Ctrl+C arrête le tableau de bord ; Entrée relance la commande du panneau avec le code à jour.
    zellij --session "$SESSION" action send-keys --pane-id "terminal_$id" "Ctrl c"
    sleep 1
    zellij --session "$SESSION" action send-keys --pane-id "terminal_$id" Enter
    log "tableau de bord redémarré (terminal_$id)"
  else
    log "panneau « PMO Roadmap » introuvable"
  fi
fi

if [ "$DO_RESUME" -eq 1 ]; then
  id="$(pane_field 'Claude' id)"
  [ -n "$id" ] || { log "ÉCHEC : panneau Claude introuvable"; exit 1; }
  waited=0
  # Sécurité : ne jamais taper dans un panneau où claude tourne encore (le texte partirait comme message).
  while :; do
    cmd="$(pane_field 'Claude' pane_command)"
    case "$cmd" in
      claude*|*/claude*|node*) ;;   # claude (ou son interpréteur) occupe encore le panneau
      *) break ;;
    esac
    if [ "$waited" -ge "$WAIT_MAX" ]; then
      log "ABANDON : claude toujours actif après ${WAIT_MAX}s, rien n'a été tapé"
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  zellij --session "$SESSION" action write-chars --pane-id "terminal_$id" "cd '$WT_CLAUDE' && $RESUME_CMD"
  sleep 0.3
  zellij --session "$SESSION" action send-keys --pane-id "terminal_$id" Enter
  log "reprise envoyée au panneau terminal_$id : $RESUME_CMD (attente ${waited}s)"
fi
log "terminé"
