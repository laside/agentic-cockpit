#!/usr/bin/env python3
"""
Tests de dépersonnalisation du lanceur, du tableau de bord et du cœur.

Vérifie l'absence de toute adresse IPv4 privée (RFC1918) dans les fichiers
ciblés et le comportement de la sous-commande d'aide de `bin/cockpit`.
Aucun réseau, aucun LLM, aucune base réelle.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

FICHIERS_CIBLES = (
    "bin/cockpit",
    "core/pmo_db.py",
    "core/gatekeeper.py",
    "tui/pmo_dashboard.py",
    "mk/agentic.mk",
)

# Plages IPv4 privées RFC1918 : 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16.  # privacy-check: allow
IPV4_PRIVEE_RE = re.compile(
    r"\b(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r")\b"
)


class TestDepersonalize(unittest.TestCase):
    """Garantit qu'aucune IP locale ne subsiste et que l'aide reste utilisable."""

    def test_aucune_ipv4_privee_dans_les_fichiers_cibles(self) -> None:
        """Chaque fichier ciblé ne doit contenir aucun littéral d'IPv4 privée."""
        for relatif in FICHIERS_CIBLES:
            chemin = ROOT_DIR / relatif
            with self.subTest(fichier=relatif):
                self.assertTrue(chemin.is_file(), f"Fichier introuvable : {relatif}")
                contenu = chemin.read_text(encoding="utf-8")
                trouvees = IPV4_PRIVEE_RE.findall(contenu)
                self.assertEqual(
                    trouvees,
                    [],
                    f"Adresse(s) IPv4 privée(s) détectée(s) dans {relatif} : {trouvees}",
                )

    def test_help_sort_en_zero_et_liste_privacy_check(self) -> None:
        """`bash bin/cockpit help` sort en 0, affiche privacy-check et ne parle pas de migrate."""
        cockpit_bin = ROOT_DIR / "bin" / "cockpit"
        env = {**os.environ, "COCKPIT_HOME": str(ROOT_DIR)}
        resultat = subprocess.run(
            ["bash", str(cockpit_bin), "help"],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            resultat.returncode,
            0,
            f"`cockpit help` doit sortir en 0 (code {resultat.returncode}) : {resultat.stderr}",
        )
        sortie = resultat.stdout + resultat.stderr
        self.assertIn(
            "privacy-check",
            sortie,
            "La sortie de `cockpit help` doit mentionner privacy-check.",
        )
        self.assertNotIn(
            "migrate",
            sortie,
            "La sortie de `cockpit help` ne doit plus mentionner migrate.",
        )


if __name__ == "__main__":
    unittest.main()
