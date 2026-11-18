#!/usr/bin/env python3
"""Tests du moteur cockpit lessons (core/lessons.py).

Base SQLite temporaire et fichiers temporaires uniquement : aucun accès réseau,
aucun LLM, aucun accès à la base réelle db/cockpit_pmo.db.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT))

from core.lessons import (  # noqa: E402
    FAILURE_KINDS,
    MAX_RULES,
    MIN_OCCURRENCES,
    SECTION_BEGIN,
    SECTION_END,
    apply_rules,
    collect_failures,
    group_recurring,
    main,
    normalize_motif,
    propose_rules,
    render_section,
)


def _cree_base(db_path: Path, evenements: list[tuple[str, str, str]]) -> None:
    """Crée une base temporaire minimale avec la table events.

    `evenements` contient des triplets (ticket_id, kind, detail) insérés dans l'ordre.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "project_id TEXT NOT NULL,"
            "ticket_id TEXT,"
            "kind TEXT NOT NULL,"
            "detail TEXT DEFAULT '',"
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        for ticket_id, kind, detail in evenements:
            conn.execute(
                "INSERT INTO events (project_id, ticket_id, kind, detail) VALUES (?, ?, ?, ?)",
                ("demo", ticket_id, kind, detail),
            )


class TestCollecteEtRegroupement(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collect_failures_lit_du_plus_ancien_au_plus_recent(self) -> None:
        db = self.tmp_path / "cockpit.db"
        _cree_base(
            db,
            [
                ("T-1", "GATE0_FAIL", "premier"),
                ("T-2", "GATE1_FAIL", "deuxieme"),
                ("T-3", "GATE0_FAIL", "troisieme"),
                ("T-4", "AUTRE", "ignore"),
            ],
        )
        failures = collect_failures(db, "demo", kinds=("GATE0_FAIL",), limit=10)
        self.assertEqual([f["detail"] for f in failures], ["premier", "troisieme"])
        self.assertEqual(failures[0]["ticket_id"], "T-1")
        self.assertEqual(failures[0]["kind"], "GATE0_FAIL")
        self.assertIn("id", failures[0])

    def test_collect_failures_n_ecrit_pas_si_base_absente(self) -> None:
        db = self.tmp_path / "absente.db"
        self.assertEqual(collect_failures(db, "demo"), [])
        self.assertFalse(db.exists())

    def test_trois_occurrences_produisent_une_regle_deux_non(self) -> None:
        motif = "Gate0: échec de compilation du module paiement"
        db_trois = self.tmp_path / "trois.db"
        _cree_base(db_trois, [("T-1", "GATE0_FAIL", motif), ("T-2", "GATE0_FAIL", motif),
                              ("T-3", "GATE0_FAIL", motif)])
        groupes = group_recurring(collect_failures(db_trois, "demo"))
        self.assertEqual(len(groupes), 1)
        self.assertEqual(groupes[0]["count"], 3)
        self.assertGreaterEqual(len(propose_rules(groupes)), 1)

        db_deux = self.tmp_path / "deux.db"
        _cree_base(db_deux, [("T-1", "GATE0_FAIL", motif), ("T-2", "GATE0_FAIL", motif)])
        groupes_deux = group_recurring(collect_failures(db_deux, "demo"))
        self.assertEqual(groupes_deux, [])
        self.assertEqual(propose_rules(groupes_deux), [])

    def test_group_recurring_normalise_et_trie(self) -> None:
        failures = [
            {"id": 1, "ticket_id": "T-1", "kind": "GATE0_FAIL", "detail": "Timeout 30s sur le module auth token abc123def"},
            {"id": 2, "ticket_id": "T-2", "kind": "GATE1_FAIL", "detail": "Timeout  45 s sur le module auth token 9988aabb"},
            {"id": 3, "ticket_id": "T-3", "kind": "GATE0_FAIL", "detail": "Timeout 60s sur le module auth token ffff0000"},
            {"id": 4, "ticket_id": "T-4", "kind": "GATE0_FAIL", "detail": "Erreur réseau isolée"},
            {"id": 5, "ticket_id": "T-5", "kind": "GATE0_FAIL", "detail": "Erreur réseau isolée"},
            {"id": 6, "ticket_id": "T-6", "kind": "GATE0_FAIL", "detail": "Erreur réseau isolée"},
            {"id": 7, "ticket_id": "T-7", "kind": "GATE0_FAIL", "detail": "Erreur réseau isolée"},
        ]
        groupes = group_recurring(failures)
        self.assertEqual(len(groupes), 2)
        self.assertEqual(groupes[0]["count"], 4)
        self.assertEqual(groupes[1]["count"], 3)
        self.assertEqual(groupes[0]["kinds"], ["GATE0_FAIL"])
        self.assertEqual(groupes[1]["kinds"], ["GATE0_FAIL", "GATE1_FAIL"])
        self.assertLessEqual(len(groupes[0]["examples"]), 2)
        self.assertLessEqual(len(groupes[1]["examples"]), 2)

    def test_group_recurring_egalite_triee_par_motif(self) -> None:
        failures = []
        ident = 0
        for motif in ("zebre échec", "alpha échec"):
            for _ in range(MIN_OCCURRENCES):
                ident += 1
                failures.append({"id": ident, "ticket_id": f"T-{ident}", "kind": "GATE0_FAIL",
                                 "detail": motif})
        groupes = group_recurring(failures)
        self.assertEqual([g["motif"] for g in groupes], ["alpha échec", "zebre échec"])

    def test_normalize_motif_supprime_donnees_sensibles(self) -> None:
        texte = "Échec pour user@example.com depuis 192.0.2.10 token=abcdef1234567890 pendant 12.5s"  # privacy-check: allow
        motif = normalize_motif(texte)
        self.assertNotIn("user@example.com", motif)  # privacy-check: allow
        self.assertNotIn("192.0.2.10", motif)  # privacy-check: allow
        self.assertNotIn("abcdef1234567890", motif)
        self.assertNotIn("12.5s", motif)
        self.assertLessEqual(len(motif), 120)
        self.assertNotIn("  ", motif)

    def test_propose_rules_deduit_et_borne(self) -> None:
        groupes = [
            {"motif": f"motif {i}", "count": MIN_OCCURRENCES + i, "kinds": ["GATE0_FAIL"],
             "examples": ["exemple"]}
            for i in range(MAX_RULES + 5)
        ]
        groupes.append(dict(groupes[0]))
        regles = propose_rules(groupes)
        self.assertLessEqual(len(regles), MAX_RULES)
        self.assertEqual(len(regles), len({r.strip().lower() for r in regles}))

    def test_collect_failures_utilise_les_types_publics(self) -> None:
        db = self.tmp_path / "publics.db"
        _cree_base(
            db,
            [
                ("T-1", "GATE0_FAIL", "premier"),
                ("T-2", "GATE1_FAIL", "deuxieme"),
                ("T-3", "INTEGRATION_FAIL", "troisieme"),
                ("T-4", "PR_PRIVACY_FAIL", "quatrieme"),
                ("T-5", "AUTRE", "ignore"),
            ],
        )
        failures = collect_failures(db, "demo")
        self.assertEqual([f["kind"] for f in failures], list(FAILURE_KINDS))
        self.assertEqual(len(failures), len(FAILURE_KINDS))
        self.assertNotIn("AUTRE", [f["kind"] for f in failures])


class TestRenduEtApplication(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_render_section_un_seul_bloc(self) -> None:
        texte = render_section(["Première règle", "Deuxième règle"])
        self.assertTrue(texte.startswith(SECTION_BEGIN))
        self.assertTrue(texte.endswith(SECTION_END))
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertEqual(texte.count(SECTION_END), 1)
        lignes = texte.splitlines()
        self.assertIn(f"- Première règle", lignes)
        self.assertIn(f"- Deuxième règle", lignes)

    def test_apply_rules_idempotent(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text(
            "Avant\n\n" + SECTION_BEGIN + "\n- vieille règle\n" + SECTION_END + "\n\nAprès\n",
            encoding="utf-8",
        )
        regles = ["Règle A", "Règle B"]
        apply_rules(cible, regles)
        premier = cible.read_text(encoding="utf-8")
        apply_rules(cible, regles)
        second = cible.read_text(encoding="utf-8")
        self.assertEqual(premier, second)
        self.assertEqual(second.count(SECTION_BEGIN), 1)
        self.assertEqual(second.count(SECTION_END), 1)
        self.assertEqual(second.count("- Règle A"), 1)
        self.assertTrue(second.startswith("Avant\n"))
        self.assertTrue(second.endswith("\n\nAprès\n"))

    def test_apply_rules_deduit_casse_et_espaces(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        ecrites = apply_rules(cible, ["Règle A", "  règle   a  ", "Règle B"])
        self.assertEqual(ecrites, ["Règle A", "Règle B"])
        texte = cible.read_text(encoding="utf-8")
        self.assertEqual(texte.count("- Règle A"), 1)
        self.assertEqual(texte.count("- Règle B"), 1)

    def test_apply_rules_borne_a_vingt_et_preserve_avant_apres(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        avant = "ENTETE INTACTE\n\n"
        apres = "\n\nQUEUE INTACTE\n"
        cible.write_text(
            avant + SECTION_BEGIN + "\n- ancienne\n" + SECTION_END + apres,
            encoding="utf-8",
        )
        regles = [f"Règle {i}" for i in range(MAX_RULES + 7)]
        ecrites = apply_rules(cible, regles)
        self.assertEqual(len(ecrites), MAX_RULES)
        texte = cible.read_text(encoding="utf-8")
        self.assertTrue(texte.startswith(avant))
        self.assertTrue(texte.endswith(apres))
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertEqual(texte.count(SECTION_END), 1)
        self.assertEqual(sum(1 for l in texte.splitlines() if l.startswith("- ")), MAX_RULES)
        self.assertNotIn("Règle 20", texte)

    def test_apply_rules_ajoute_en_fin_si_absent(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text("Contenu existant sans bloc", encoding="utf-8")
        apply_rules(cible, ["Règle unique"])
        texte = cible.read_text(encoding="utf-8")
        self.assertTrue(texte.startswith("Contenu existant sans bloc"))
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertEqual(texte.count(SECTION_END), 1)
        self.assertIn("- Règle unique", texte)

    def test_apply_rules_preserve_apres_begin_orphelin(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text("Avant\n" + SECTION_BEGIN + "\nTexte à préserver\n", encoding="utf-8")
        apply_rules(cible, ["Règle A"])
        premier = cible.read_text(encoding="utf-8")
        apply_rules(cible, ["Règle A"])
        second = cible.read_text(encoding="utf-8")
        self.assertEqual(premier, second)
        self.assertTrue(second.startswith("Avant\n"))
        self.assertTrue(second.endswith("\nTexte à préserver\n"))
        self.assertEqual(second.count(SECTION_BEGIN), 1)
        self.assertEqual(second.count(SECTION_END), 1)
        self.assertEqual(second.count("- Règle A"), 1)

    def test_apply_rules_end_avant_begin_reste_idempotent(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text(
            "Entete\n" + SECTION_END + "\nMilieu\n" + SECTION_BEGIN + "\nQueue\n",
            encoding="utf-8",
        )
        apply_rules(cible, ["Règle A"])
        premier = cible.read_text(encoding="utf-8")
        apply_rules(cible, ["Règle A"])
        second = cible.read_text(encoding="utf-8")
        self.assertEqual(premier, second)
        self.assertEqual(second.count(SECTION_BEGIN), 1)
        self.assertEqual(second.count(SECTION_END), 1)
        self.assertEqual(second.count("- Règle A"), 1)
        for morceau in ("Entete\n", "Milieu\n", "Queue\n"):
            self.assertIn(morceau, second)


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db = self.tmp_path / "cockpit.db"
        motif = "Gate0: échec de compilation du module paiement"
        _cree_base(
            self.db,
            [("T-1", "GATE0_FAIL", motif), ("T-2", "GATE0_FAIL", motif), ("T-3", "GATE0_FAIL", motif)],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_main_sans_apply_n_ecrit_aucun_fichier(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text("SENTINELLE\n", encoding="utf-8")
        code = main(["demo", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertEqual(cible.read_text(encoding="utf-8"), "SENTINELLE\n")

    def test_main_sans_apply_ne_cree_pas_le_fichier(self) -> None:
        cible = self.tmp_path / "absent.md"
        code = main(["demo", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertFalse(cible.exists())

    def test_main_apply_ecrit_dans_le_chemin_donne(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        code = main(["demo", "--apply", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        texte = cible.read_text(encoding="utf-8")
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertIn("\n- ", texte)

    def test_main_min_ajuste_le_seuil(self) -> None:
        db = self.tmp_path / "deux.db"
        _cree_base(
            db,
            [("T-1", "GATE0_FAIL", "Echec identique"), ("T-2", "GATE0_FAIL", "Echec identique")],
        )
        cible = self.tmp_path / "AGENTS.md"
        self.assertEqual(main(["demo", "--apply", "--min", "2", "--db", str(db), "--rules", str(cible)]), 0)
        self.assertIn("- ", cible.read_text(encoding="utf-8"))

    def test_main_json_n_ecrit_rien(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        code = main(["demo", "--json", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertFalse(cible.exists())


if __name__ == "__main__":
    unittest.main()
