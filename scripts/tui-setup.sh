#!/usr/bin/env bash
# Crée l'environnement virtuel dédié à l'écran Textual et y installe requirements-tui.txt.
# Aucune installation système ; idempotent (relançable sans effet de bord).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${TUI_VENV:-${HOME}/.local/share/agentic-cockpit/tui-venv}"

REQS="${REPO_ROOT}/requirements-tui.txt"
AVEC_UV=0

if [ ! -x "${VENV}/bin/python" ]; then
    mkdir -p "$(dirname "${VENV}")"
    if ! python3 -m venv "${VENV}" 2>/dev/null; then
        # ensurepip absent (paquet python3-venv non installé) : venv à moitié créé, on nettoie.
        rm -rf "${VENV}"
        if command -v uv >/dev/null 2>&1; then
            echo "python3 -m venv indisponible (ensurepip absent) : utilisation de uv."
            uv venv "${VENV}"
            AVEC_UV=1
        else
            echo "Erreur : python3 -m venv a échoué (ensurepip absent) et uv est introuvable." >&2
            echo "Deux solutions :" >&2
            echo "  1. installer le paquet venv de Python (ex. python3-venv), puis relancer ce script ;" >&2
            echo "  2. installer uv dans votre compte (https://docs.astral.sh/uv/), puis relancer ce script." >&2
            exit 1
        fi
    fi
elif ! "${VENV}/bin/python" -m pip --version >/dev/null 2>&1; then
    AVEC_UV=1  # venv créé par uv, sans pip
fi

if [ "${AVEC_UV}" -eq 1 ]; then
    if ! command -v uv >/dev/null 2>&1; then
        echo "Erreur : ${VENV} n'a pas pip et uv est introuvable ; supprimez-le ou installez uv." >&2
        exit 1
    fi
    uv pip install --quiet --python "${VENV}/bin/python" -r "${REQS}"
else
    "${VENV}/bin/python" -m pip install --quiet -r "${REQS}"
fi

echo "Environnement prêt : ${VENV}"
echo "Lancement : cd ${REPO_ROOT} && ${VENV}/bin/python -m tui.commande.app <projet>"
