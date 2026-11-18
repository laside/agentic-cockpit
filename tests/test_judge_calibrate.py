#!/usr/bin/env python3
"""Tests de la calibration du juge (T-027), juge factice injecté, aucun réseau."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import judge_calibrate  # noqa: E402

CASES_REELLES = ROOT / "tests" / "judge_cases"


def _ecrire_cas(base: Path, specs: list[tuple[str, str]]) -> Path:
    """Écrit un cases.json temporaire et ses briefs/diffs (diff = nom du cas)."""
    cas_dir = base / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    (cas_dir / "brief.md").write_text("brief de test", encoding="utf-8")
    entrees = []
    for nom, attendu in specs:
        (cas_dir / f"{nom}.diff").write_text(nom, encoding="utf-8")
        entrees.append(
            {"name": nom, "brief": "brief.md", "diff": f"{nom}.diff", "expected": attendu}
        )
    (cas_dir / "cases.json").write_text(
        json.dumps(entrees, ensure_ascii=False), encoding="utf-8"
    )
    return cas_dir


def _juge_par_plan(plan: dict[str, list[str]]):
    """Juge factice : renvoie les verdicts prévus pour chaque diff (nom du cas)."""
    appels: dict[str, int] = {}

    def faux_juge(brief: str, diff: str, cfg: dict) -> tuple[str, list[str], dict]:
        nom = diff.strip()
        sequence = plan[nom]
        index = appels.get(nom, 0)
        verdict = sequence[min(index, len(sequence) - 1)]
        appels[nom] = index + 1
        return verdict, [], {}

    return faux_juge


def _juge_fixe(verdict: str):
    def faux_juge(brief: str, diff: str, cfg: dict) -> tuple[str, list[str], dict]:
        return verdict, [], {}

    return faux_juge


class TestCalibrate(unittest.TestCase):
    """Vérifie le contrat de `core.judge_calibrate.calibrate`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_juge_parfait_est_fiable(self) -> None:
        cas_dir = _ecrire_cas(
            self.base,
            [
                ("bon", "PASS"),
                ("mauvais_1", "FAIL"),
                ("mauvais_2", "FAIL"),
            ],
        )
        juge = _juge_par_plan(
            {"bon": ["PASS"], "mauvais_1": ["FAIL"], "mauvais_2": ["FAIL"]}
        )

        resultat = judge_calibrate.calibrate(cas_dir, {}, judge=juge)

        self.assertTrue(resultat["passed"])
        self.assertEqual(resultat["detected"], 2)
        self.assertEqual(resultat["bad_total"], 2)
        self.assertEqual(resultat["false_positives"], 0)
        self.assertEqual(resultat["good_total"], 1)
        self.assertEqual(resultat["skipped"], 0)
        self.assertEqual([c["name"] for c in resultat["cases"]], ["bon", "mauvais_1", "mauvais_2"])
        self.assertTrue(all(c["ok"] for c in resultat["cases"]))
        self.assertEqual(len(resultat["prompt_sha256"]), 64)

    def test_mauvais_diff_juge_pass_nest_pas_detecte(self) -> None:
        cas_dir = _ecrire_cas(
            self.base, [("bon", "PASS"), ("mauvais_1", "FAIL"), ("mauvais_2", "FAIL")]
        )
        juge = _juge_par_plan(
            {"bon": ["PASS"], "mauvais_1": ["PASS"], "mauvais_2": ["FAIL"]}
        )

        resultat = judge_calibrate.calibrate(cas_dir, {}, judge=juge)

        self.assertEqual(resultat["detected"], 1)
        self.assertEqual(resultat["bad_total"], 2)
        self.assertLess(resultat["detected"], resultat["bad_total"])
        self.assertFalse(resultat["passed"])
        self.assertFalse(next(c for c in resultat["cases"] if c["name"] == "mauvais_1")["ok"])

    def test_bon_diff_juge_fail_est_faux_positif(self) -> None:
        cas_dir = _ecrire_cas(
            self.base, [("bon", "PASS"), ("mauvais", "FAIL")]
        )
        juge = _juge_par_plan({"bon": ["FAIL"], "mauvais": ["FAIL"]})

        resultat = judge_calibrate.calibrate(cas_dir, {}, judge=juge)

        self.assertEqual(resultat["false_positives"], 1)
        self.assertEqual(resultat["good_total"], 1)
        self.assertFalse(resultat["passed"])

    def test_skipped_compte_comme_ratage_sans_faux_positif(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS"), ("mauvais", "FAIL")])
        juge = _juge_par_plan({"bon": ["PASS"], "mauvais": ["SKIPPED"]})

        resultat = judge_calibrate.calibrate(cas_dir, {}, judge=juge)

        self.assertEqual(resultat["skipped"], 1)
        self.assertEqual(resultat["false_positives"], 0)
        self.assertEqual(resultat["detected"], 0)
        self.assertFalse(resultat["passed"])
        self.assertFalse(next(c for c in resultat["cases"] if c["name"] == "mauvais")["ok"])

    def test_runs_trois_verdict_instable_non_detecte(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("mauvais", "FAIL")])
        juge = _juge_par_plan({"mauvais": ["FAIL", "PASS", "FAIL"]})

        resultat = judge_calibrate.calibrate(cas_dir, {}, runs=3, judge=juge)

        cas = resultat["cases"][0]
        self.assertEqual(cas["verdicts"], ["FAIL", "PASS", "FAIL"])
        self.assertFalse(cas["ok"])
        self.assertEqual(resultat["detected"], 0)
        self.assertFalse(resultat["passed"])

    def test_hachage_change_si_le_prompt_change(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS")])
        prompt = self.base / "sys_judge_claude.md"
        prompt.write_text("version 1", encoding="utf-8")
        juge = _juge_par_plan({"bon": ["PASS"]})

        with mock.patch.object(judge_calibrate, "PROMPT_PATH", prompt):
            h1 = judge_calibrate.calibrate(cas_dir, {}, judge=juge)["prompt_sha256"]
            prompt.write_text("version 2", encoding="utf-8")
            h2 = judge_calibrate.calibrate(cas_dir, {}, judge=juge)["prompt_sha256"]

        self.assertNotEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_main_json_ecrit_le_fichier_et_code_sortie_zero(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS"), ("mauvais", "FAIL")])
        config = self.base / "runners.toml"
        config.write_text('[gate1]\njudge_model = "haiku"\n', encoding="utf-8")
        sortie_json = self.base / "resultat.json"
        juge = _juge_par_plan({"bon": ["PASS"], "mauvais": ["FAIL"]})

        with (
            mock.patch.object(judge_calibrate, "RUNNERS_PATH", config),
            mock.patch.object(judge_calibrate.judge_mod, "judge_diff", juge),
        ):
            tampon = io.StringIO()
            with redirect_stdout(tampon):
                code = judge_calibrate.main(
                    ["--cases", str(cas_dir), "--json", str(sortie_json)]
                )

        self.assertEqual(code, 0)
        self.assertTrue(sortie_json.exists())
        contenu = json.loads(sortie_json.read_text(encoding="utf-8"))
        self.assertTrue(contenu["passed"])
        self.assertIn("JUGE FIABLE", tampon.getvalue())
        self.assertIn("Détectés 1/1", tampon.getvalue())

    def test_main_non_fiable_code_sortie_un(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS"), ("mauvais", "FAIL")])
        config = self.base / "runners.toml"
        config.write_text("[gate1]\n", encoding="utf-8")
        juge = _juge_par_plan({"bon": ["PASS"], "mauvais": ["PASS"]})

        with (
            mock.patch.object(judge_calibrate, "RUNNERS_PATH", config),
            mock.patch.object(judge_calibrate.judge_mod, "judge_diff", juge),
        ):
            tampon = io.StringIO()
            with redirect_stdout(tampon):
                code = judge_calibrate.main(["--cases", str(cas_dir)])

        self.assertEqual(code, 1)
        self.assertIn("JUGE NON FIABLE", tampon.getvalue())

    def test_jeu_reel_se_charge_et_fichiers_existent(self) -> None:
        entrees = json.loads((CASES_REELLES / "cases.json").read_text(encoding="utf-8"))
        self.assertTrue(entrees)
        for entree in entrees:
            self.assertTrue((CASES_REELLES / entree["brief"]).is_file(), entree["brief"])
            self.assertTrue((CASES_REELLES / entree["diff"]).is_file(), entree["diff"])

        resultat = judge_calibrate.calibrate(CASES_REELLES, {}, judge=_juge_fixe("PASS"))
        attendus_fail = sum(1 for e in entrees if e["expected"] == "FAIL")
        attendus_pass = sum(1 for e in entrees if e["expected"] == "PASS")
        self.assertEqual(len(resultat["cases"]), len(entrees))
        self.assertGreaterEqual(resultat["bad_total"], 10)
        self.assertEqual(resultat["bad_total"], attendus_fail)
        self.assertEqual(resultat["good_total"], attendus_pass)
        self.assertEqual(resultat["prompt_sha256"], judge_calibrate._prompt_sha256())

    def test_defects_presents_par_passe_et_affiches(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS"), ("mauvais", "FAIL")])
        config = self.base / "runners.toml"
        config.write_text('[gate1]\njudge_model = "haiku"\n', encoding="utf-8")
        sortie_json = self.base / "rapport.json"
        appels = 0

        def juge_avec_motifs(brief: str, diff: str, cfg: dict):
            nonlocal appels
            if diff.strip() == "bon":
                return "PASS", [], {}
            appels += 1
            if appels == 1:
                return "PASS", [], {}
            return "FAIL", ["Fuite de secret détectée dans config"], {}

        with (
            mock.patch.object(judge_calibrate, "RUNNERS_PATH", config),
            mock.patch.object(judge_calibrate.judge_mod, "judge_diff", juge_avec_motifs),
        ):
            tampon = io.StringIO()
            with redirect_stdout(tampon):
                code = judge_calibrate.main(
                    ["--runs", "2", "--cases", str(cas_dir), "--json", str(sortie_json)]
                )

        self.assertEqual(code, 1)
        out = tampon.getvalue()
        self.assertIn("motif : Fuite de secret détectée dans config", out)
        rapport = json.loads(sortie_json.read_text(encoding="utf-8"))
        self.assertEqual(rapport["runs"], 2)
        self.assertEqual(rapport["judge_model"], "haiku")
        self.assertIn("generated_at", rapport)
        datetime.fromisoformat(rapport["generated_at"])
        cas_mauvais = next(c for c in rapport["cases"] if c["name"] == "mauvais")
        self.assertEqual(
            cas_mauvais["defects"],
            [[], ["Fuite de secret détectée dans config"]],
        )

    def test_exception_du_juge_donne_skipped_et_motif(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("mauvais", "FAIL")])

        def juge_plante(brief: str, diff: str, cfg: dict):
            raise RuntimeError("panne réseau simulée")

        resultat = judge_calibrate.calibrate(cas_dir, {}, runs=1, judge=juge_plante)
        self.assertFalse(resultat["passed"])
        self.assertEqual(resultat["skipped"], 1)
        cas = resultat["cases"][0]
        self.assertEqual(cas["verdicts"], ["SKIPPED"])
        self.assertEqual(cas["defects"], [["exception : panne réseau simulée"]])

    def test_calibration_status_tous_les_cas(self) -> None:
        maintenant = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        prompt_file = self.base / "sys_judge_claude.md"
        prompt_file.write_text("prompt officiel", encoding="utf-8")
        rapport_path = self.base / "judge-calibration.json"

        with mock.patch.object(judge_calibrate, "PROMPT_PATH", prompt_file):
            sha_actuel = judge_calibrate._prompt_sha256()

            # 1. Rapport absent
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertFalse(ok)
            self.assertEqual(raison, "aucun rapport de calibration")

            def ecrire_rapport(**surcharges) -> None:
                base_rapport = {
                    "passed": True,
                    "detected": 10,
                    "bad_total": 10,
                    "false_positives": 0,
                    "good_total": 2,
                    "skipped": 0,
                    "runs": 3,
                    "prompt_sha256": sha_actuel,
                    "generated_at": (maintenant - timedelta(days=2)).isoformat(),
                    "judge_model": "haiku",
                    "cases": [],
                }
                base_rapport.update(surcharges)
                rapport_path.write_text(json.dumps(base_rapport), encoding="utf-8")

            # 2. passed faux
            ecrire_rapport(passed=False, detected=8, bad_total=10, false_positives=1)
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertFalse(ok)
            self.assertEqual(raison, "juge non fiable (détectés 8/10, faux positifs 1)")

            # 3. prompt changé
            ecrire_rapport(prompt_sha256="0" * 64)
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertFalse(ok)
            self.assertEqual(raison, "le prompt a changé depuis la calibration")

            # 4. trop ancien
            ecrire_rapport(generated_at=(maintenant - timedelta(days=15)).isoformat())
            ok, raison = judge_calibrate.calibration_status(
                rapport_path, max_age_days=14, now=maintenant
            )
            self.assertFalse(ok)
            self.assertEqual(raison, "calibration trop ancienne (15 jours)")

            # 5. runs=1
            ecrire_rapport(runs=1)
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertFalse(ok)
            self.assertIn("calibration trop légère", raison)

            # 6. bad_total=5
            ecrire_rapport(bad_total=5, detected=5)
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertFalse(ok)
            self.assertIn("calibration trop légère", raison)

            # 7. tout bon -> (True, "")
            ecrire_rapport()
            ok, raison = judge_calibrate.calibration_status(rapport_path, now=maintenant)
            self.assertTrue(ok)
            self.assertEqual(raison, "")


if __name__ == "__main__":
    unittest.main()
