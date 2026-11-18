#!/usr/bin/env python3
"""Tests du moteur cockpit lessons (core/lessons.py).

Base SQLite temporaire et fichiers temporaires uniquement : aucun accès réseau,
aucun LLM, aucun accès à la base réelle db/cockpit_pmo.db.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

COCKPIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COCKPIT_ROOT))

from core.lessons import (  # noqa: E402
    CATEGORIES,
    FAILURE_KINDS,
    MAX_RULES,
    MIN_OCCURRENCES,
    SECTION_BEGIN,
    SECTION_END,
    apply_rules,
    classify_failures,
    collect_failures,
    group_recurring,
    main,
    normalize_motif,
    propose_rules,
    render_section,
)

REGLE_TEST_CONTRAT = "Écris un test d'acceptation couvrant un comportement du contrat."
REGLE_PERIMETRE = "Ne modifie que FILES_OWNED ; annule le reste."
REGLE_AUCUN_FICHIER = "Crée ou modifie au moins un fichier de FILES_OWNED ; pas de diff vide."
REGLE_DONNEE_PRIVEE = "Données factices (example.com, 192.0.2.x) uniquement ; aucun secret."
REGLE_PORTE_INTEGRATION = "Ramène la branche principale, résous les conflits, rejoue lint/tests."

DETAILS_REPRESENTATIFS = {
    "test_contrat": "Il manque un test d'acceptation du contrat",
    "perimetre_files_owned": "Un fichier hors perimetre a ete modifie",
    "aucun_fichier_modifie": "Aucune modification : le diff vide est refuse",
    "donnee_privee": "Le detail contient une donnee privee a masquer",
    "porte_integration": "Conflit de fusion pendant le merge",
}


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


def _echec(kind: str, detail: str, ticket: str = "T-1") -> dict[str, object]:
    return {"ticket_id": ticket, "kind": kind, "detail": detail}


def _echecs(kind: str, detail: str, nombre: int) -> list[dict[str, object]]:
    return [_echec(kind, detail, f"T-{index + 1}") for index in range(nombre)]


class TestCategoriesEtClassification(unittest.TestCase):
    def test_categories_exactes_et_ordonnees(self) -> None:
        self.assertIsInstance(CATEGORIES, tuple)
        identifiants = [categorie["id"] for categorie in CATEGORIES]
        self.assertEqual(
            identifiants,
            [
                "test_contrat",
                "perimetre_files_owned",
                "aucun_fichier_modifie",
                "donnee_privee",
                "porte_integration",
            ],
        )
        regles = {categorie["id"]: categorie["rule"] for categorie in CATEGORIES}
        self.assertEqual(regles["test_contrat"], REGLE_TEST_CONTRAT)
        self.assertEqual(regles["perimetre_files_owned"], REGLE_PERIMETRE)
        self.assertEqual(regles["aucun_fichier_modifie"], REGLE_AUCUN_FICHIER)
        self.assertEqual(regles["donnee_privee"], REGLE_DONNEE_PRIVEE)
        self.assertEqual(regles["porte_integration"], REGLE_PORTE_INTEGRATION)
        for categorie in CATEGORIES:
            self.assertTrue(categorie["keywords"])
            self.assertIsInstance(categorie["keywords"], tuple)

    def test_un_detail_representatif_par_categorie(self) -> None:
        echecs = [
            _echec("GATE0_FAIL", detail, f"T-{index}")
            for index, detail in enumerate(DETAILS_REPRESENTATIFS.values(), start=1)
        ]
        groupes = classify_failures(echecs, min_occurrences=1)
        par_categorie = {groupe["category"]: groupe for groupe in groupes}
        self.assertEqual(set(par_categorie), set(DETAILS_REPRESENTATIFS))
        for identifiant, detail in DETAILS_REPRESENTATIFS.items():
            self.assertEqual(par_categorie[identifiant]["category"], identifiant)
            self.assertEqual(
                par_categorie[identifiant]["rule"],
                next(c["rule"] for c in CATEGORIES if c["id"] == identifiant),
            )
            self.assertIn(detail, par_categorie[identifiant]["examples"])

    def test_premiere_correspondance_par_ordre_de_priorite(self) -> None:
        detail = "un test du contrat malgre un conflit de merge"
        groupes = classify_failures([_echec("GATE0_FAIL", detail)], min_occurrences=1)
        self.assertEqual(groupes[0]["category"], "test_contrat")

    def test_repli_par_kind_privacy_et_integration(self) -> None:
        echecs = [
            _echec("PR_PRIVACY_FAIL", "xyz"),
            _echec("INTEGRATION_FAIL", "xyz"),
        ]
        groupes = classify_failures(echecs, min_occurrences=1)
        categories = {groupe["category"] for groupe in groupes}
        self.assertIn("donnee_privee", categories)
        self.assertIn("porte_integration", categories)

    def test_detail_sans_mot_cle_et_kind_non_mappe_est_ignore(self) -> None:
        echecs = _echecs("GATE1_FAIL", "erreur de compilation isolee", MIN_OCCURRENCES)
        groupes = classify_failures(echecs)
        self.assertEqual(groupes, [])
        self.assertEqual(propose_rules(groupes), [])

    def test_seuil_inclusif_deux_absent_trois_present_et_min_deux(self) -> None:
        detail = "Il manque un test de contrat"
        deux = classify_failures(_echecs("GATE0_FAIL", detail, 2))
        self.assertEqual(deux, [])

        groupes = classify_failures(_echecs("GATE0_FAIL", detail, 3))
        self.assertEqual(len(groupes), 1)
        self.assertEqual(groupes[0]["category"], "test_contrat")
        self.assertEqual(groupes[0]["count"], 3)

        abaisses = classify_failures(_echecs("GATE0_FAIL", detail, 2), min_occurrences=2)
        self.assertEqual(len(abaisses), 1)
        self.assertEqual(abaisses[0]["category"], "test_contrat")
        self.assertEqual(abaisses[0]["count"], 2)

    def test_regle_emise_sans_interpolation_du_motif_ni_du_detail(self) -> None:
        detail = "test contrat ticket T-051 chemin /tmp/exemple"
        groupes = classify_failures(_echecs("GATE0_FAIL", detail, 3))
        self.assertEqual(len(groupes), 1)
        self.assertEqual(groupes[0]["rule"], REGLE_TEST_CONTRAT)
        self.assertNotIn("T-051", groupes[0]["rule"])
        self.assertNotIn(detail, groupes[0]["rule"])
        self.assertEqual(propose_rules(groupes), [REGLE_TEST_CONTRAT])

    def test_kinds_examples_et_tri(self) -> None:
        echecs = [
            _echec("GATE0_FAIL", "test contrat un", "T-1"),
            _echec("GATE1_FAIL", "test contrat deux", "T-2"),
            _echec("GATE0_FAIL", "test contrat trois", "T-3"),
            _echec("GATE0_FAIL", "test contrat un", "T-4"),
            _echec("GATE0_FAIL", "donnee privee", "T-5"),
            _echec("GATE0_FAIL", "donnee privee", "T-6"),
            _echec("GATE0_FAIL", "donnee privee", "T-7"),
            _echec("GATE0_FAIL", "donnee privee", "T-8"),
        ]
        groupes = classify_failures(echecs)
        self.assertEqual([g["category"] for g in groupes], ["donnee_privee", "test_contrat"])
        contrat = next(g for g in groupes if g["category"] == "test_contrat")
        self.assertEqual(contrat["count"], 4)
        self.assertEqual(contrat["kinds"], ["GATE0_FAIL", "GATE1_FAIL"])
        self.assertLessEqual(len(contrat["examples"]), 2)
        self.assertEqual(len(contrat["examples"]), len(set(contrat["examples"])))

    def test_tri_a_count_egal_par_categorie(self) -> None:
        echecs = []
        for detail in DETAILS_EGALITE:
            echecs.extend(_echecs("GATE0_FAIL", detail, 3))
        groupes = classify_failures(echecs)
        self.assertEqual(
            [g["category"] for g in groupes],
            sorted(g["category"] for g in groupes),
        )


DETAILS_EGALITE = [
    "un test de contrat",
    "une donnee privee",
    "un conflit de fusion",
]


class TestPropositionEtRendu(unittest.TestCase):
    def test_propose_rules_deduit_casse_et_espaces(self) -> None:
        categories = [
            {"rule": "Règle A"},
            {"rule": "  règle   a  "},
            {"rule": "Règle B"},
        ]
        self.assertEqual(propose_rules(categories), ["Règle A", "Règle B"])

    def test_propose_rules_plafonne_a_max_rules(self) -> None:
        categories = [{"rule": f"Règle {index}"} for index in range(MAX_RULES + 5)]
        regles = propose_rules(categories)
        self.assertEqual(len(regles), MAX_RULES)
        self.assertEqual(len(regles), len({regle.lower() for regle in regles}))

    def test_propose_rules_omet_les_regles_avec_motif_prive(self) -> None:
        categories = [
            {"rule": REGLE_DONNEE_PRIVEE},
            {"rule": "Règle publique"},
        ]
        with mock.patch(
            "core.lessons._private_hits",
            side_effect=lambda texte: ["mot-interdit#1"] if "Données factices" in texte else [],
        ):
            regles = propose_rules(categories)
        self.assertEqual(regles, ["Règle publique"])

    def test_render_section_un_seul_bloc(self) -> None:
        texte = render_section(["Première règle", "Deuxième règle"])
        self.assertTrue(texte.startswith(SECTION_BEGIN))
        self.assertTrue(texte.endswith(SECTION_END))
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertEqual(texte.count(SECTION_END), 1)
        lignes = texte.splitlines()
        self.assertIn("- Première règle", lignes)
        self.assertIn("- Deuxième règle", lignes)


class TestCollecte(unittest.TestCase):
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
        categories = [{"rule": "Règle A"}, {"rule": "  règle   a  "}]
        categories.extend({"rule": f"Règle {index}"} for index in range(MAX_RULES + 5))
        regles = propose_rules(categories)
        self.assertLessEqual(len(regles), MAX_RULES)
        self.assertEqual(len(regles), len({regle.strip().lower() for regle in regles}))
        self.assertEqual(regles[0], "Règle A")

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


class TestApplication(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

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
        self.assertEqual(sum(1 for ligne in texte.splitlines() if ligne.startswith("- ")), MAX_RULES)
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
        detail = "Il manque un test d'acceptation du contrat"
        _cree_base(
            self.db,
            [
                ("T-1", "GATE0_FAIL", detail),
                ("T-2", "GATE0_FAIL", detail),
                ("T-3", "GATE0_FAIL", detail),
            ],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_main_sans_apply_n_ecrit_aucun_fichier(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        cible.write_text("SENTINELLE\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            code = main(["demo", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertEqual(cible.read_text(encoding="utf-8"), "SENTINELLE\n")

    def test_main_sans_apply_ne_cree_pas_le_fichier(self) -> None:
        cible = self.tmp_path / "absent.md"
        with redirect_stdout(io.StringIO()):
            code = main(["demo", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertFalse(cible.exists())

    def test_main_apply_ecrit_dans_le_chemin_donne(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        with redirect_stdout(io.StringIO()):
            code = main(["demo", "--apply", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        texte = cible.read_text(encoding="utf-8")
        self.assertEqual(texte.count(SECTION_BEGIN), 1)
        self.assertIn(f"- {REGLE_TEST_CONTRAT}", texte)

    def test_main_min_ajuste_le_seuil(self) -> None:
        db = self.tmp_path / "deux.db"
        detail = "Il manque un test de contrat"
        _cree_base(
            db,
            [("T-1", "GATE0_FAIL", detail), ("T-2", "GATE0_FAIL", detail)],
        )
        cible = self.tmp_path / "AGENTS.md"
        with redirect_stdout(io.StringIO()):
            code = main(["demo", "--apply", "--min", "2", "--db", str(db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertIn(f"- {REGLE_TEST_CONTRAT}", cible.read_text(encoding="utf-8"))

    def test_main_json_expose_groupes_classes_et_regles(self) -> None:
        cible = self.tmp_path / "AGENTS.md"
        sortie = io.StringIO()
        with redirect_stdout(sortie):
            code = main(["demo", "--json", "--db", str(self.db), "--rules", str(cible)])
        self.assertEqual(code, 0)
        self.assertFalse(cible.exists())
        donnees = json.loads(sortie.getvalue())
        self.assertEqual(donnees["project"], "demo")
        self.assertEqual(donnees["groups"][0]["category"], "test_contrat")
        self.assertEqual(donnees["groups"][0]["count"], 3)
        self.assertEqual(donnees["rules"], [REGLE_TEST_CONTRAT])


if __name__ == "__main__":
    unittest.main()
