#!/usr/bin/env python3
"""Tests du vote majoritaire du juge de revue (T-060) : aucun réseau, aucun LLM."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import judge  # noqa: E402
import judge_calibrate  # noqa: E402


def _ligne_claude(
    result_str: str,
    *,
    tokens_in: int = 100,
    tokens_out: int = 10,
    cost: float = 0.001,
) -> str:
    """Une ligne JSON `claude --output-format json` portant le verdict."""
    payload = {
        "type": "result",
        "subtype": "success",
        "result": result_str,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 5,
        },
    }
    return json.dumps(payload) + "\n"


def _verdict(verdict: str, defects: list[str] | None = None, **kw) -> str:
    return _ligne_claude(json.dumps({"verdict": verdict, "defects": defects or []}), **kw)


def _make_faux_claude_compteur(bin_dir: Path, sorties: list[str], compteur: Path) -> None:
    """Faux exécutable `claude` : une sortie par appel, compteur d'appels dans `compteur`."""
    script = bin_dir / "claude"
    lignes = [
        "#!/usr/bin/env python3",
        "import sys",
        f"compteur = {str(compteur)!r}",
        "try:",
        "    n = int(open(compteur, encoding='utf-8').read())",
        "except Exception:",
        "    n = 0",
        "open(compteur, 'w', encoding='utf-8').write(str(n + 1))",
        f"sorties = {list(sorties)!r}",
        "sys.stdout.write(sorties[min(n, len(sorties) - 1)])",
        "sys.stdout.flush()",
        "sys.exit(0)",
    ]
    script.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _nb_appels(compteur: Path) -> int:
    if not compteur.exists():
        return 0
    return int(compteur.read_text(encoding="utf-8") or 0)


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

    def faux_juge(brief: str, diff: str, cfg: dict):
        nom = diff.strip()
        sequence = plan[nom]
        index = appels.get(nom, 0)
        verdict = sequence[min(index, len(sequence) - 1)]
        appels[nom] = index + 1
        return verdict, [], {}

    return faux_juge


class TestVerdictMajoritaire(unittest.TestCase):
    """Fonction partagée `judge.verdict_majoritaire` (judge_diff et calibrate)."""

    def test_majorite_fail_avec_defauts_des_votes_fail(self) -> None:
        votes = [("FAIL", ["d1"]), ("PASS", []), ("FAIL", ["d2", "d3"])]
        verdict, defects = judge.verdict_majoritaire(votes, 3)
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(defects, ["d1", "d2", "d3"])

    def test_majorite_pass(self) -> None:
        votes = [("PASS", []), ("FAIL", ["d1"]), ("PASS", ["remarque"])]
        verdict, defects = judge.verdict_majoritaire(votes, 3)
        self.assertEqual(verdict, "PASS")
        self.assertEqual(defects, ["remarque"])

    def test_skipped_si_aucune_majorite_parmi_les_votes_valides(self) -> None:
        votes = [("PASS", []), ("FAIL", ["d1"]), ("SKIPPED", ["timeout"])]
        verdict, defects = judge.verdict_majoritaire(votes, 3)
        self.assertEqual(verdict, "SKIPPED")
        self.assertEqual(defects, ["timeout"])

    def test_tous_skipped_donne_skipped(self) -> None:
        votes = [("SKIPPED", ["a"]), ("SKIPPED", ["b"]), ("SKIPPED", [])]
        verdict, defects = judge.verdict_majoritaire(votes, 3)
        self.assertEqual(verdict, "SKIPPED")
        self.assertEqual(defects, ["a", "b"])

    def test_skipped_exclu_du_decompte_majoritaire(self) -> None:
        # Le SKIPPED n'est jamais compté comme PASS, et un PASS isolé ne suffit pas :
        # il faut une majorité absolue (2 sur 3) pour PASS comme pour FAIL.
        verdict, _ = judge.verdict_majoritaire(
            [("PASS", []), ("SKIPPED", []), ("SKIPPED", [])], 3
        )
        self.assertEqual(verdict, "SKIPPED")
        verdict, _ = judge.verdict_majoritaire(
            [("PASS", []), ("PASS", []), ("SKIPPED", [])], 3
        )
        self.assertEqual(verdict, "PASS")
        verdict, _ = judge.verdict_majoritaire(
            [("FAIL", ["d"]), ("SKIPPED", []), ("SKIPPED", [])], 3
        )
        self.assertEqual(verdict, "SKIPPED")

    def test_nb_votes_1_exige_l_unanimite(self) -> None:
        self.assertEqual(judge.verdict_majoritaire([("FAIL", ["a"])], 1), ("FAIL", ["a"]))
        self.assertEqual(judge.verdict_majoritaire([("PASS", [])], 1), ("PASS", []))
        verdict, _ = judge.verdict_majoritaire([("FAIL", []), ("PASS", [])], 1)
        self.assertEqual(verdict, "SKIPPED")


class TestJudgeVotesDiff(unittest.TestCase):
    """`judge_diff` fait `judge_votes` appels et rend le verdict majoritaire."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}:{self._path}"
        self.compteur = self.base / "appels.txt"

    def tearDown(self) -> None:
        os.environ["PATH"] = self._path
        self._tmp.cleanup()

    def test_trois_votes_majorite_fail_et_defauts_des_votes_fail(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [
                _verdict("FAIL", ["critère manquant"]),
                _verdict("PASS"),
                _verdict("FAIL", ["signature erronée"]),
            ],
            self.compteur,
        )
        verdict, defects, _ = judge.judge_diff(
            "brief", "diff", {"judge_model": "haiku", "judge_votes": 3}
        )
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(defects, ["critère manquant", "signature erronée"])
        self.assertEqual(_nb_appels(self.compteur), 3)

    def test_trois_votes_majorite_pass(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("PASS"), _verdict("FAIL", ["d"]), _verdict("PASS")],
            self.compteur,
        )
        verdict, defects, _ = judge.judge_diff(
            "brief", "diff", {"judge_votes": 3}
        )
        self.assertEqual(verdict, "PASS")
        self.assertEqual(defects, [])
        self.assertEqual(_nb_appels(self.compteur), 3)

    def test_skipped_jamais_pass_sans_majorite_parmi_les_valides(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("PASS"), _verdict("FAIL", ["d"]), _verdict("MAYBE")],
            self.compteur,
        )
        verdict, defects, _ = judge.judge_diff(
            "brief", "diff", {"judge_votes": 3}
        )
        self.assertEqual(verdict, "SKIPPED")
        self.assertEqual(_nb_appels(self.compteur), 3)
        self.assertTrue(any("MAYBE" in d for d in defects))

    def test_trois_votes_skipped_verdict_skipped(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir, [_verdict("MAYBE")] * 3, self.compteur
        )
        verdict, _, _ = judge.judge_diff("brief", "diff", {"judge_votes": 3})
        self.assertEqual(verdict, "SKIPPED")
        self.assertEqual(_nb_appels(self.compteur), 3)

    def test_arret_anticipe_des_majorite_acquise(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("PASS"), _verdict("PASS"), _verdict("FAIL", ["d"])],
            self.compteur,
        )
        verdict, _, _ = judge.judge_diff("brief", "diff", {"judge_votes": 3})
        self.assertEqual(verdict, "PASS")
        self.assertEqual(_nb_appels(self.compteur), 2)  # 3e appel non lancé

    def test_arret_anticipe_des_majorite_fail(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("FAIL", ["d1"]), _verdict("FAIL", ["d2"]), _verdict("PASS")],
            self.compteur,
        )
        verdict, defects, _ = judge.judge_diff("brief", "diff", {"judge_votes": 3})
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(defects, ["d1", "d2"])
        self.assertEqual(_nb_appels(self.compteur), 2)

    def test_judge_votes_absent_un_seul_appel(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("PASS"), _verdict("FAIL", ["d"]), _verdict("FAIL", ["e"])],
            self.compteur,
        )
        verdict, _, _ = judge.judge_diff("brief", "diff", {"judge_model": "haiku"})
        self.assertEqual(verdict, "PASS")
        self.assertEqual(_nb_appels(self.compteur), 1)

    def test_judge_votes_1_un_seul_appel(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [_verdict("FAIL", ["d"]), _verdict("PASS"), _verdict("PASS")],
            self.compteur,
        )
        verdict, defects, _ = judge.judge_diff(
            "brief", "diff", {"judge_votes": 1}
        )
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(defects, ["d"])
        self.assertEqual(_nb_appels(self.compteur), 1)

    def test_info_somme_des_seuls_appels_lances(self) -> None:
        _make_faux_claude_compteur(
            self.bin_dir,
            [
                _verdict("PASS", tokens_in=100, cost=0.001),
                _verdict("PASS", tokens_in=200, cost=0.002),
                _verdict("FAIL", ["d"], tokens_in=999, cost=0.5),
            ],
            self.compteur,
        )
        verdict, _, info = judge.judge_diff("brief", "diff", {"judge_votes": 3})
        self.assertEqual(verdict, "PASS")
        self.assertEqual(_nb_appels(self.compteur), 2)
        self.assertEqual(info["tokens_in"], 300)
        self.assertAlmostEqual(info["cost_usd"], 0.003)


class TestCalibrateVotesMajoritaires(unittest.TestCase):
    """`calibrate` applique la même règle de majorité par cas (fonction partagée)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_majorite_par_cas_et_rapport_complet(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("bon", "PASS"), ("mauvais", "FAIL")])
        cfg = {"judge_model": "sonnet", "judge_votes": 3}
        juge = _juge_par_plan(
            {"bon": ["PASS", "FAIL", "PASS"], "mauvais": ["FAIL", "PASS", "FAIL"]}
        )

        resultat = judge_calibrate.calibrate(cas_dir, cfg, runs=3, judge=juge)

        self.assertTrue(resultat["passed"])
        self.assertEqual(resultat["detected"], 1)
        self.assertEqual(resultat["bad_total"], 1)
        self.assertEqual(resultat["false_positives"], 0)
        self.assertEqual(resultat["good_total"], 1)
        self.assertEqual(resultat["skipped"], 0)
        self.assertEqual(resultat["judge_model"], "sonnet")
        self.assertEqual(resultat["judge_votes"], 3)
        cas_bon = resultat["cases"][0]
        self.assertEqual(cas_bon["verdicts"], ["PASS", "FAIL", "PASS"])
        self.assertEqual(cas_bon["verdict"], "PASS")
        self.assertTrue(cas_bon["ok"])
        cas_mauvais = resultat["cases"][1]
        self.assertEqual(cas_mauvais["verdicts"], ["FAIL", "PASS", "FAIL"])
        self.assertEqual(cas_mauvais["verdict"], "FAIL")
        self.assertTrue(cas_mauvais["ok"])

    def test_sans_majorite_parmi_votes_valides_verdict_skipped(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("mauvais", "FAIL")])
        juge = _juge_par_plan({"mauvais": ["PASS", "FAIL", "SKIPPED"]})

        resultat = judge_calibrate.calibrate(
            cas_dir, {"judge_votes": 3}, runs=3, judge=juge
        )

        cas = resultat["cases"][0]
        self.assertEqual(cas["verdicts"], ["PASS", "FAIL", "SKIPPED"])
        self.assertEqual(cas["verdict"], "SKIPPED")
        self.assertFalse(cas["ok"])
        self.assertEqual(resultat["detected"], 0)
        self.assertEqual(resultat["skipped"], 1)
        self.assertFalse(resultat["passed"])

    def test_judge_votes_1_conserve_l_unanimite(self) -> None:
        cas_dir = _ecrire_cas(self.base, [("mauvais", "FAIL")])
        juge = _juge_par_plan({"mauvais": ["FAIL", "PASS", "FAIL"]})

        resultat = judge_calibrate.calibrate(cas_dir, {}, runs=3, judge=juge)

        cas = resultat["cases"][0]
        self.assertEqual(cas["verdicts"], ["FAIL", "PASS", "FAIL"])
        self.assertEqual(cas["verdict"], "SKIPPED")  # pas d'unanimité
        self.assertFalse(cas["ok"])
        self.assertEqual(resultat["detected"], 0)
        self.assertFalse(resultat["passed"])
        self.assertEqual(resultat["judge_votes"], 1)


class TestCalibrationStatusConfigCourante(unittest.TestCase):
    """`calibration_status` refuse une calibration faite avec un autre juge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.maintenant = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
        self.prompt = self.base / "sys_judge_claude.md"
        self.prompt.write_text("prompt officiel", encoding="utf-8")
        self.config = self.base / "runners.toml"
        self.config.write_text(
            '[gate1]\njudge_model = "sonnet"\njudge_votes = 3\n', encoding="utf-8"
        )
        self.rapport_path = self.base / "judge-calibration.json"
        with mock.patch.object(judge_calibrate, "PROMPT_PATH", self.prompt):
            self.sha = judge_calibrate._prompt_sha256()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rapport(self, **surcharges) -> dict:
        base = {
            "passed": True,
            "detected": 13,
            "bad_total": 13,
            "false_positives": 0,
            "good_total": 4,
            "skipped": 0,
            "runs": 3,
            "prompt_sha256": self.sha,
            "generated_at": (self.maintenant - timedelta(days=1)).isoformat(),
            "judge_model": "sonnet",
            "judge_votes": 3,
            "cases": [],
        }
        base.update(surcharges)
        return base

    def _status(self, rapport: dict) -> tuple[bool, str]:
        self.rapport_path.write_text(json.dumps(rapport), encoding="utf-8")
        with (
            mock.patch.object(judge_calibrate, "PROMPT_PATH", self.prompt),
            mock.patch.object(judge_calibrate, "RUNNERS_PATH", self.config),
        ):
            return judge_calibrate.calibration_status(
                self.rapport_path, now=self.maintenant
            )

    def test_accepte_calibration_sur_la_config_courante(self) -> None:
        ok, raison = self._status(self._rapport())
        self.assertTrue(ok)
        self.assertEqual(raison, "")

    def test_refuse_un_autre_judge_model(self) -> None:
        ok, raison = self._status(self._rapport(judge_model="haiku"))
        self.assertFalse(ok)
        self.assertIn("judge_model", raison)
        self.assertIn("haiku", raison)

    def test_refuse_un_autre_judge_votes(self) -> None:
        ok, raison = self._status(self._rapport(judge_votes=1))
        self.assertFalse(ok)
        self.assertIn("judge_votes", raison)
        self.assertIn("1", raison)

    def test_rapport_sans_judge_votes_est_refuse(self) -> None:
        # Critère 4 : un rapport sans la clé judge_votes (avant T-060) vaut
        # judge_votes=1 (un seul appel) ; la config courante exige 3 votes,
        # la calibration doit être refusée explicitement.
        rapport = self._rapport()
        del rapport["judge_votes"]
        ok, raison = self._status(rapport)
        self.assertFalse(ok)
        self.assertIn("judge_votes", raison)

    def test_rapport_sans_judge_model_est_refuse(self) -> None:
        rapport = self._rapport()
        del rapport["judge_model"]
        ok, raison = self._status(rapport)
        self.assertFalse(ok)
        self.assertIn("judge_model", raison)


class TestRunnersExampleConfig(unittest.TestCase):
    """La clé [gate1] judge_votes vaut 3 (entier impair) dans l'exemple livré."""

    def test_gate1_judge_votes_vaut_3(self) -> None:
        cfg = tomllib.loads(
            (ROOT / "config" / "runners.example.toml").read_text(encoding="utf-8")
        )
        votes = cfg["gate1"]["judge_votes"]
        self.assertEqual(votes, 3)
        self.assertEqual(votes % 2, 1)


if __name__ == "__main__":
    unittest.main()
