#!/usr/bin/env bash
# Crée l'environnement virtuel dédié à l'écran Textual et y installe requirements-tui.txt.
# Aucune installation système ; idempotent (relançable sans effet de bord).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${TUI_VENV:-${HOME}/.local/share/agentic-cockpit/tui-venv}"

if [ ! -x "${VENV}/bin/python" ]; then
    mkdir -p "$(dirname "${VENV}")"
    python3 -m venv "${VENV}"
fi

"${VENV}/bin/python" -m pip install --quiet -r "${REPO_ROOT}/requirements-tui.txt"

echo "Environnement prêt : ${VENV}"
echo "Lancement : cd ${REPO_ROOT} && ${VENV}/bin/python -m tui.commande.app <projet>"
