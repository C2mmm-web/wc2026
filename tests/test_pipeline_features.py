import os
import sys
import unittest
import math

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "wc2026_pro"))


def fixture(home, away, hg, ag, status="FT", date="2026-06-14T13:00:00+00:00"):
    return {
        "fixture": {"date": date, "status": {"short": status}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": hg, "away": ag},
    }


class FetchResultsFeatureTests(unittest.TestCase):
    def test_openfootball_history_payload_becomes_training_rows(self):
        from fetch_results import openfootball_history_from_payload

        payload = {
            "name": "World Cup 2022",
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2022-11-29",
                    "team1": "USA",
                    "team2": "Iran",
                    "score": {"ft": [1, 0]},
                },
                {
                    "round": "Final",
                    "date": "2022-12-18",
                    "team1": "Argentina",
                    "team2": "France",
                    "score": {"ft": [3, 3]},
                },
                {
                    "round": "Matchday 1",
                    "date": "2022-11-20",
                    "team1": "Qatar",
                    "team2": "Ecuador",
                    "score": {"ft": [0, 2]},
                },
            ],
        }

        rows, status = openfootball_history_from_payload(payload, 2022, "unit://history.json")

        self.assertEqual(status["status"], "success")
        self.assertIn(("United States", "IR Iran", 1, 0), {
            (r["home"], r["away"], r["home_goals"], r["away_goals"]) for r in rows
        })
        self.assertIn(("Argentina", "France", 3, 3), {
            (r["home"], r["away"], r["home_goals"], r["away_goals"]) for r in rows
        })
        self.assertTrue(all(r["source"] == "openfootball_worldcup" for r in rows))

    def test_openfootball_payload_becomes_keyless_current_results(self):
        from fetch_results import openfootball_current_from_payload

        payload = {
            "name": "World Cup 2026",
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-06-11",
                    "time": "13:00 UTC-6",
                    "team1": "Mexico",
                    "team2": "South Africa",
                    "score": {"ft": [2, 0], "ht": [1, 0]},
                },
                {
                    "round": "Matchday 2",
                    "date": "2026-06-12",
                    "time": "15:00 UTC-4",
                    "team1": "Canada",
                    "team2": "Bosnia & Herzegovina",
                    "score": {"ft": [1, 1]},
                },
                {
                    "round": "Matchday 4",
                    "date": "2026-06-14",
                    "time": "18:00 UTC-5",
                    "team1": "USA",
                    "team2": "Turkey",
                    "score": {"ft": [4, 1]},
                },
                {
                    "round": "Matchday 8",
                    "date": "2026-06-18",
                    "time": "12:00 UTC-4",
                    "team1": "Czech Republic",
                    "team2": "South Africa",
                },
                {
                    "round": "Round of 32",
                    "date": "2026-06-28",
                    "team1": "W73",
                    "team2": "3A/B/C/D/F",
                },
            ],
        }

        out = openfootball_current_from_payload(payload, "unit://worldcup.json")

        self.assertEqual(out["finished"]["Mexico|South Africa"], [2, 0])
        self.assertEqual(out["finished"]["Canada|Bosnia and Herzegovina"], [1, 1])
        self.assertEqual(out["finished"]["United States|Türkiye"], [4, 1])
        self.assertEqual(out["upcoming"][0]["home"], "Czechia")
        self.assertEqual(out["upcoming"][0]["away"], "South Africa")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["skipped_placeholders"], 1)
        self.assertEqual(out["unknown_names"], [])

    def test_build_fetch_outputs_records_status_history_and_fresh_results(self):
        from fetch_results import build_fetch_outputs

        current = {
            "errors": [],
            "response": [
                fixture("Mexico", "South Africa", 2, 0, "FT"),
                fixture("Brazil", "Morocco", None, None, "NS"),
            ],
        }
        history_payloads = [
            {
                "league": 1,
                "season": 2022,
                "data": {
                    "errors": [],
                    "response": [
                        fixture("Brazil", "Morocco", 1, 0, "FT", "2022-12-05T18:00:00+00:00"),
                        fixture("USA", "Iran", 1, 0, "FT", "2022-11-29T19:00:00+00:00"),
                    ],
                },
            }
        ]
        previous = {"Mexico|South Africa": [1, 0]}

        out = build_fetch_outputs(
            current,
            history_payloads,
            previous_results=previous,
            checked_at="2026-06-14T13:00:00Z",
        )

        self.assertEqual(out["finished"]["Mexico|South Africa"], [2, 0])
        self.assertEqual(out["fresh_results"]["Mexico|South Africa"], [2, 0])
        self.assertEqual(out["upcoming"][0]["home"], "Brazil")
        self.assertEqual(out["upcoming"][0]["away"], "Morocco")
        history_keys = {(r["home"], r["away"]) for r in out["historical_results"]}
        self.assertIn(("Brazil", "Morocco"), history_keys)
        self.assertIn(("United States", "IR Iran"), history_keys)
        self.assertEqual(out["status"]["current_results"]["status"], "success")
        self.assertEqual(out["status"]["history"]["status"], "success")
        self.assertEqual(out["status"]["history"]["matches"], 2)

    def test_build_fetch_outputs_uses_free_fallback_when_api_current_errors(self):
        from fetch_results import build_fetch_outputs

        fallback = {
            "status": "success",
            "source": "upbound-web/worldcup-live.json",
            "url": "unit://worldcup.json",
            "finished": {"Australia|Türkiye": [2, 0]},
            "upcoming": [{"home": "Germany", "away": "Curaçao", "status": "NS", "date": "2026-06-14"}],
            "unknown_names": [],
            "skipped_placeholders": 12,
        }

        out = build_fetch_outputs(
            {"errors": {"plan": "Free plans do not have access to this season"}, "response": []},
            [],
            previous_results={},
            checked_at="2026-06-14T13:00:00Z",
            fallback_current=fallback,
        )

        self.assertEqual(out["finished"]["Australia|Türkiye"], [2, 0])
        self.assertEqual(out["status"]["current_results"]["status"], "fallback_success")
        self.assertEqual(out["status"]["current_results"]["fallback_source"]["added_finished"], 1)
        self.assertEqual(out["status"]["current_results"]["fallback_source"]["skipped_placeholders"], 12)

    def test_build_fetch_outputs_merges_free_historical_rows(self):
        from fetch_results import build_fetch_outputs

        out = build_fetch_outputs(
            {"errors": {"plan": "blocked"}, "response": []},
            [],
            checked_at="2026-06-14T13:00:00Z",
            fallback_history={
                "status": "success",
                "source": "openfootball/worldcup.json",
                "seasons": [2018, 2022],
                "rows": [
                    {
                        "date": "2018-07-15",
                        "season": 2018,
                        "home": "France",
                        "away": "Croatia",
                        "home_goals": 4,
                        "away_goals": 2,
                        "neutral": True,
                        "source": "openfootball_worldcup",
                    }
                ],
                "errors": [],
            },
        )

        self.assertEqual(out["historical_results"][0]["home"], "France")
        self.assertEqual(out["status"]["history"]["status"], "success")
        self.assertEqual(out["status"]["history"]["free_source"]["rows"], 1)

    def test_build_fetch_outputs_default_timestamp_is_utc_iso(self):
        from fetch_results import build_fetch_outputs

        out = build_fetch_outputs({"errors": [], "response": []}, [])

        self.assertTrue(out["status"]["checked_at"].endswith("Z"))


class PredictionAuditFeatureTests(unittest.TestCase):
    def test_archive_snapshots_unplayed_matches_once(self):
        from audit import append_prediction_archive

        payload = {
            "version": "unit-v1",
            "generated": "2026-06-18",
            "generated_at": "2026-06-18T00:00:00Z",
            "matches": [
                {
                    "group": "A",
                    "md": 2,
                    "home": "Mexico",
                    "away": "Canada",
                    "played": None,
                    "judgment": {
                        "w": 0.52,
                        "d": 0.25,
                        "l": 0.23,
                        "lh": 1.45,
                        "la": 1.05,
                        "top": [[2, 1, 0.112], [1, 1, 0.105], [1, 0, 0.097]],
                    },
                    "scoreline": {
                        "top3": [
                            {"score": "2-1", "prob": 0.112},
                            {"score": "1-1", "prob": 0.105},
                            {"score": "1-0", "prob": 0.097},
                        ]
                    },
                    "advanced": {"totals": {"xg_total": 2.5}},
                },
                {
                    "group": "B",
                    "md": 2,
                    "home": "United States",
                    "away": "Brazil",
                    "played": [1, 3],
                    "judgment": {
                        "w": 0.21,
                        "d": 0.23,
                        "l": 0.56,
                        "lh": 0.9,
                        "la": 1.8,
                        "top": [[1, 2, 0.12]],
                    },
                },
                {
                    "group": "C",
                    "md": 3,
                    "home": "France",
                    "away": "Japan",
                    "played": None,
                    "judgment": {
                        "w": 0.6,
                        "d": 0.22,
                        "l": 0.18,
                        "lh": 1.8,
                        "la": 0.8,
                        "top": [[2, 0, 0.13]],
                    },
                },
            ],
        }
        archive = append_prediction_archive([], payload, commit_sha="abc123")
        archive = append_prediction_archive(archive, payload, commit_sha="abc123")

        mexico_records = [r for r in archive if r["home"] == "Mexico" and r["away"] == "Canada"]
        self.assertEqual(len(mexico_records), 1)
        self.assertFalse(any(r["home"] == "United States" for r in archive))
        self.assertFalse(any(r["home"] == "France" for r in archive))
        record = mexico_records[0]
        self.assertEqual(record["pred_score"], "2-1")
        self.assertEqual(record["top3"][0], {"score": "2-1", "prob": 0.112})
        self.assertEqual(record["xg_home"], 1.45)
        self.assertEqual(record["xg_away"], 1.05)
        self.assertEqual(record["xg_total"], 2.5)
        self.assertEqual(record["commit"], "abc123")

    def test_audit_report_uses_earliest_archived_prediction(self):
        from audit import build_audit_report

        archive = [
            {
                "generated_at": "2026-06-15T00:00:00Z",
                "home": "Mexico",
                "away": "Canada",
                "p_home": 0.5,
                "p_draw": 0.25,
                "p_away": 0.25,
                "pred_score": "1-0",
                "top3": [
                    {"score": "1-0", "prob": 0.12},
                    {"score": "1-1", "prob": 0.11},
                    {"score": "2-1", "prob": 0.09},
                ],
            },
            {
                "generated_at": "2026-06-17T00:00:00Z",
                "home": "Mexico",
                "away": "Canada",
                "p_home": 0.33,
                "p_draw": 0.41,
                "p_away": 0.26,
                "pred_score": "1-1",
                "top3": [{"score": "1-1", "prob": 0.2}],
            },
        ]
        payload = {
            "generated_at": "2026-06-18T00:00:00Z",
            "matches": [
                {"home": "Mexico", "away": "Canada", "played": [1, 1]},
                {"home": "United States", "away": "Brazil", "played": [0, 2]},
                {"home": "France", "away": "Japan", "played": None},
            ],
        }

        report = build_audit_report(archive, payload)

        self.assertEqual(report["finished_matches"], 2)
        self.assertEqual(report["audited_matches"], 1)
        self.assertEqual(report["missing_prematch"], 1)
        self.assertEqual(report["exact_top1_hits"], 0)
        self.assertEqual(report["exact_top3_hits"], 1)
        self.assertEqual(report["wdl_hits"], 0)
        self.assertEqual(report["rates"]["exact_top1"], 0.0)
        self.assertEqual(report["rates"]["exact_top3"], 1.0)
        self.assertEqual(report["rates"]["wdl"], 0.0)
        row = report["rows"][0]
        self.assertEqual(row["prediction_generated_at"], "2026-06-15T00:00:00Z")
        self.assertEqual(row["pred_score"], "1-0")
        self.assertEqual(row["actual_score"], "1-1")
        self.assertFalse(row["exact_top1"])
        self.assertTrue(row["exact_top3"])
        self.assertEqual(report["missing"][0]["home"], "United States")


class BacktestFeatureTests(unittest.TestCase):
    def test_run_backtest_uses_real_history_when_available(self):
        from backtest import run_backtest
        from data import TEAMS

        rows = []
        for i in range(72):
            home = TEAMS[i % len(TEAMS)]
            away = TEAMS[(i * 7 + 5) % len(TEAMS)]
            if home == away:
                away = TEAMS[(i + 1) % len(TEAMS)]
            rows.append(
                {
                    "home": home,
                    "away": away,
                    "home_goals": i % 4,
                    "away_goals": (i + 1) % 3,
                    "neutral": True,
                    "date": f"202{i % 4}-01-01T00:00:00+00:00",
                }
            )

        report, _elo, _dc, weight = run_backtest(
            history_rows=rows,
            min_real_history_matches=40,
        )

        self.assertEqual(report["data_source"], "real_worldcup_history")
        self.assertEqual(report["n_history"], len(rows))
        self.assertIn("model", report["test"])
        self.assertIn("calibration", report)
        self.assertIn("wdl_temperature", report["calibration"])
        self.assertIn("scoreline", report["test"])
        self.assertEqual(report["test"]["scoreline"]["n"], report["n_test"])
        self.assertGreaterEqual(report["test"]["scoreline"]["exact_top1"], 0.0)
        self.assertLessEqual(report["test"]["scoreline"]["exact_top1"], 1.0)
        self.assertGreaterEqual(
            report["test"]["scoreline"]["exact_top3"],
            report["test"]["scoreline"]["exact_top1"],
        )
        self.assertGreaterEqual(weight, 0.0)
        self.assertLessEqual(weight, 1.0)

    def test_temperature_calibration_keeps_probabilities_normalized(self):
        from calibration import apply_temperature, fit_temperature
        from backtest import log_loss

        probs = [
            [0.85, 0.10, 0.05],
            [0.80, 0.15, 0.05],
            [0.75, 0.15, 0.10],
            [0.70, 0.20, 0.10],
        ]
        ys = [1, 0, 2, 1]

        temp = fit_temperature(probs, ys)
        calibrated = [apply_temperature(p, temp) for p in probs]

        self.assertAlmostEqual(sum(calibrated[0]), 1.0, places=7)
        self.assertGreater(temp, 1.0)
        self.assertLess(
            sum(log_loss(p, y) for p, y in zip(calibrated, ys)),
            sum(log_loss(p, y) for p, y in zip(probs, ys)),
        )


class FormAdjustmentFeatureTests(unittest.TestCase):
    def test_form_adjustments_are_capped_after_extreme_single_match(self):
        from form import build_form_adjustments

        adjustments = build_form_adjustments({
            ("Haiti", "Spain"): (6, 0),
            ("Spain", "Haiti"): (0, 6),
        })

        self.assertLessEqual(adjustments["Haiti"]["attack_mult"], 1.03)
        self.assertGreaterEqual(adjustments["Spain"]["attack_mult"], 0.97)
        self.assertEqual(adjustments["Haiti"]["matches"], 1)
        self.assertIn("状态微调", adjustments["Haiti"]["note"])

    def test_form_adjustments_include_defensive_vulnerability(self):
        from form import build_form_adjustments

        adjustments = build_form_adjustments({
            ("Spain", "Haiti"): (0, 5),
        })

        self.assertGreater(adjustments["Spain"]["defense_mult"], 1.0)
        self.assertLess(adjustments["Haiti"]["defense_mult"], 1.0)
        self.assertIn("防守", adjustments["Spain"]["note"])

    def test_predictor_applies_form_as_small_goal_multiplier(self):
        import numpy as np
        from tournament import Predictor
        from data import TEAMS, ELO_PRIOR

        class FakeElo:
            r = dict(ELO_PRIOR)

        class FakeDc:
            teams = list(TEAMS)
            idx = {team: i for i, team in enumerate(teams)}
            att = np.zeros(len(teams))
            dff = np.zeros(len(teams))
            gamma = 0.0

        plain = Predictor(FakeElo(), FakeDc(), w=0.0, rating_sigma=0, param_sigma=0)
        adjusted = Predictor(
            FakeElo(),
            FakeDc(),
            w=0.0,
            rating_sigma=0,
            param_sigma=0,
            form_adjustments={"Mexico": {"attack_mult": 1.03}, "South Africa": {"attack_mult": 0.97}},
        )

        base_home, base_away = plain.lambdas("Mexico", "South Africa")
        adj_home, adj_away = adjusted.lambdas("Mexico", "South Africa")

        self.assertAlmostEqual(adj_home / base_home, 1.03, places=6)
        self.assertAlmostEqual(adj_away / base_away, 0.97, places=6)

    def test_predictor_applies_opponent_defensive_vulnerability(self):
        import numpy as np
        from tournament import Predictor
        from data import TEAMS, ELO_PRIOR

        class FakeElo:
            r = dict(ELO_PRIOR)

        class FakeDc:
            teams = list(TEAMS)
            idx = {team: i for i, team in enumerate(teams)}
            att = np.zeros(len(teams))
            dff = np.zeros(len(teams))
            gamma = 0.0

        plain = Predictor(FakeElo(), FakeDc(), w=0.0, rating_sigma=0, param_sigma=0)
        adjusted = Predictor(
            FakeElo(),
            FakeDc(),
            w=0.0,
            rating_sigma=0,
            param_sigma=0,
            form_adjustments={
                "Mexico": {"attack_mult": 1.0, "defense_mult": 0.98},
                "South Africa": {"attack_mult": 1.0, "defense_mult": 1.04},
            },
        )

        base_home, base_away = plain.lambdas("Mexico", "South Africa")
        adj_home, adj_away = adjusted.lambdas("Mexico", "South Africa")

        self.assertAlmostEqual(adj_home / base_home, 1.04, places=6)
        self.assertAlmostEqual(adj_away / base_away, 0.98, places=6)

    def test_group_context_adjustments_are_small_and_stage_aware(self):
        from form import build_group_context_adjustments

        adjustments = build_group_context_adjustments(
            {"A": ["Mexico", "South Africa", "South Korea", "Czechia"]},
            {
                ("Mexico", "South Africa"): (5, 0),
                ("South Korea", "Czechia"): (0, 0),
            },
        )

        self.assertGreater(adjustments["Mexico"]["attack_mult"], 1.0)
        self.assertLessEqual(adjustments["Mexico"]["attack_mult"], 1.018)
        self.assertGreaterEqual(adjustments["South Africa"]["attack_mult"], 0.982)
        self.assertIn("小组形势", adjustments["Mexico"]["note"])
        self.assertIn("2 场", adjustments["Mexico"]["note"])

    def test_combined_adjustments_stay_conservative_after_single_match(self):
        from form import build_combined_adjustments

        adjustments = build_combined_adjustments(
            {"A": ["Mexico", "South Africa", "South Korea", "Czechia"]},
            {
                ("Mexico", "South Africa"): (6, 0),
                ("South Africa", "Mexico"): (0, 6),
            },
        )

        self.assertLessEqual(adjustments["Mexico"]["attack_mult"], 1.04)
        self.assertGreaterEqual(adjustments["South Africa"]["attack_mult"], 0.96)
        self.assertIn("综合微调", adjustments["Mexico"]["note"])

    def test_played_results_update_elo_but_cap_total_movement(self):
        from engine import EloModel
        from form import apply_played_elo_updates

        elo = EloModel(prior={"Favorite": 1900, "Underdog": 1500}, k=32, home_adv=0)

        report = apply_played_elo_updates(
            elo,
            {("Favorite", "Underdog"): (0, 5)},
            k=40,
            max_shift=12,
        )

        self.assertLess(elo.r["Favorite"], 1900)
        self.assertGreater(elo.r["Underdog"], 1500)
        self.assertLessEqual(abs(elo.r["Favorite"] - 1900), 12)
        self.assertLessEqual(abs(elo.r["Underdog"] - 1500), 12)
        self.assertEqual(report["matches"], 1)
        self.assertIn("Elo", report["note"])


class TempoAdjustmentFeatureTests(unittest.TestCase):
    def test_tournament_tempo_uses_real_history_with_tight_bounds(self):
        from tempo import build_tempo_adjustment

        high_scoring_rows = [
            {"home_goals": 3, "away_goals": 2, "source": "openfootball_worldcup"}
            for _ in range(50)
        ]

        tempo = build_tempo_adjustment(high_scoring_rows, min_matches=20)

        self.assertEqual(tempo["source"], "real_history")
        self.assertGreater(tempo["goal_mult"], 1.0)
        self.assertLessEqual(tempo["goal_mult"], 1.08)
        self.assertIn("赛事节奏", tempo["note"])

    def test_tournament_tempo_falls_back_without_real_history(self):
        from tempo import build_tempo_adjustment

        tempo = build_tempo_adjustment([], min_matches=20)

        self.assertEqual(tempo["source"], "neutral")
        self.assertEqual(tempo["goal_mult"], 1.0)

    def test_live_tournament_tempo_uses_current_goals_conservatively(self):
        from tempo import build_live_tempo_adjustment

        played = {
            (f"Home{i}", f"Away{i}"): (3, 2)
            for i in range(24)
        }
        base = {"goal_mult": 1.01, "note": "历史节奏"}

        tempo = build_live_tempo_adjustment(played, base, min_matches=12)

        self.assertEqual(tempo["source"], "history_plus_live_tournament")
        self.assertEqual(tempo["matches"], 24)
        self.assertGreater(tempo["goal_mult"], 1.01)
        self.assertLessEqual(tempo["goal_mult"], 1.12)
        self.assertIn("赛内节奏", tempo["note"])

    def test_predictor_applies_tempo_symmetrically(self):
        import numpy as np
        from tournament import Predictor
        from data import TEAMS, ELO_PRIOR

        class FakeElo:
            r = dict(ELO_PRIOR)

        class FakeDc:
            teams = list(TEAMS)
            idx = {team: i for i, team in enumerate(teams)}
            att = np.zeros(len(teams))
            dff = np.zeros(len(teams))
            gamma = 0.0

        plain = Predictor(FakeElo(), FakeDc(), w=0.0, rating_sigma=0, param_sigma=0)
        tempo = Predictor(FakeElo(), FakeDc(), w=0.0, rating_sigma=0, param_sigma=0, tempo_mult=1.04)

        base_home, base_away = plain.lambdas("France", "Senegal")
        tempo_home, tempo_away = tempo.lambdas("France", "Senegal")

        self.assertAlmostEqual(tempo_home / base_home, 1.04, places=6)
        self.assertAlmostEqual(tempo_away / base_away, 1.04, places=6)


class AdvancedMarketFeatureTests(unittest.TestCase):
    def _poisson_grid(self, lh, la, maxg=8):
        grid = []
        total = 0.0
        for i in range(maxg):
            row = []
            for j in range(maxg):
                prob = math.exp(-lh) * lh**i / math.factorial(i)
                prob *= math.exp(-la) * la**j / math.factorial(j)
                row.append(prob)
                total += prob
            grid.append(row)
        return [[prob / total for prob in row] for row in grid]

    def _result_totals(self, grid):
        home = draw = away = 0.0
        for i, row in enumerate(grid):
            for j, prob in enumerate(row):
                if i > j:
                    home += prob
                elif i == j:
                    draw += prob
                else:
                    away += prob
        return home, draw, away

    def test_advanced_markets_are_derived_from_goal_distribution(self):
        from main import advanced_markets

        markets = advanced_markets(1.6, 1.1, -0.05, "Home", "Away")

        self.assertIn("totals", markets)
        self.assertIn("handicap", markets)
        self.assertIn("htft", markets)
        self.assertAlmostEqual(
            sum(item["prob"] for item in markets["totals"]["buckets"]),
            1.0,
            places=3,
        )
        self.assertAlmostEqual(
            sum(item["prob"] for item in markets["handicap"]["outcomes"]),
            1.0,
            places=3,
        )
        self.assertAlmostEqual(
            sum(sum(row["cells"][col]["prob"] for col in ("H", "D", "A")) for row in markets["htft"]["rows"]),
            1.0,
            places=3,
        )
        self.assertEqual(markets["totals"]["over_under"][0]["line"], 2.5)
        self.assertIn(markets["handicap"]["home_hcap"], (-2, -1, 0, 1, 2))

    def test_scoreline_summary_marks_close_modes_as_low_concentration(self):
        from scorelines import scoreline_summary

        summary = scoreline_summary([
            ((1, 0), 0.1267),
            ((1, 1), 0.1112),
            ((2, 1), 0.0950),
            ((2, 0), 0.0949),
        ])

        self.assertEqual(summary["top3"][0]["score"], "1-0")
        self.assertEqual([item["score"] for item in summary["top3"]], ["1-0", "1-1", "2-1"])
        self.assertEqual(summary["concentration"], "low")
        self.assertEqual(summary["concentration_label"], "低集中度")
        self.assertLess(summary["mode_gap"], 0.025)
        self.assertFalse(summary["single_pick"])
        self.assertIn("无单一", summary["primary_label"])

    def test_scoreline_summary_only_promotes_clear_single_pick(self):
        from scorelines import scoreline_summary

        clear = scoreline_summary([
            ((2, 0), 0.201),
            ((1, 1), 0.112),
            ((2, 1), 0.091),
        ])
        noisy = scoreline_summary([
            ((1, 1), 0.136),
            ((2, 1), 0.094),
            ((1, 2), 0.091),
        ])

        self.assertTrue(clear["single_pick"])
        self.assertEqual(clear["primary_label"], "2-0")
        self.assertFalse(noisy["single_pick"])
        self.assertIn("无单一", noisy["primary_label"])

    def test_scoreline_calibration_preserves_result_probabilities(self):
        from scorelines import calibrate_scoreline_grid

        raw = self._poisson_grid(1.6, 1.0)
        targets = self._result_totals(raw)

        calibrated, meta = calibrate_scoreline_grid(raw, 1.6, 1.0, *targets)
        totals = self._result_totals(calibrated)

        self.assertEqual(meta["model"], "tempo_overdispersion")
        self.assertAlmostEqual(sum(sum(row) for row in calibrated), 1.0, places=7)
        for actual, expected in zip(totals, targets):
            self.assertAlmostEqual(actual, expected, places=7)

    def test_scoreline_calibration_reduces_mechanical_one_nil_mode(self):
        from scorelines import calibrate_scoreline_grid, scoreline_summary, top_scorelines

        raw = self._poisson_grid(1.6, 1.0)
        raw_summary = scoreline_summary(top_scorelines(raw))
        targets = self._result_totals(raw)

        calibrated, _meta = calibrate_scoreline_grid(raw, 1.6, 1.0, *targets)
        calibrated_summary = scoreline_summary(top_scorelines(calibrated))

        self.assertEqual(raw_summary["top3"][0]["score"], "1-0")
        self.assertLess(calibrated[1][0], raw[1][0])
        self.assertNotEqual(calibrated_summary["top3"][0]["score"], "1-0")
        self.assertIn(calibrated_summary["top3"][0]["score"], {"1-1", "2-0", "2-1"})


class MainPayloadFeatureTests(unittest.TestCase):
    def test_match_payload_exposes_update_and_public_signal_metadata(self):
        from main import match_metadata

        status = {
            "checked_at": "2026-06-14T13:00:00Z",
            "current_results": {"status": "success", "finished": 6, "upcoming": 2},
            "history": {"status": "success", "matches": 72},
        }
        historical_rows = [
            {"home": "Mexico", "away": "South Africa", "home_goals": 2, "away_goals": 0},
            {"home": "Mexico", "away": "Canada", "home_goals": 1, "away_goals": 1},
            {"home": "Brazil", "away": "South Africa", "home_goals": 3, "away_goals": 1},
        ]

        meta = match_metadata(
            "Mexico",
            "South Africa",
            played=(2, 0),
            update_status=status,
            fresh_keys={"Mexico|South Africa"},
            historical_rows=historical_rows,
        )

        self.assertTrue(meta["fresh"])
        self.assertEqual(meta["played_source"], "api")
        self.assertIn("最近战绩", " ".join(meta["public_signals"]))
        self.assertEqual(meta["update_status"]["current_results"]["status"], "success")

    def test_match_payload_names_free_fallback_source_for_played_scores(self):
        from main import match_metadata

        status = {
            "checked_at": "2026-06-14T13:00:00Z",
            "current_results": {"status": "fallback_success", "finished": 8, "upcoming": 96},
            "history": {"status": "no_key", "matches": 0},
        }

        meta = match_metadata(
            "Australia",
            "Türkiye",
            played=(2, 0),
            update_status=status,
            fresh_keys={"Australia|Türkiye"},
            historical_rows=[],
        )

        self.assertIn("比分来自 免费比分源", meta["public_signals"])

    def test_match_payload_exposes_conservative_form_adjustments(self):
        from main import match_metadata

        meta = match_metadata(
            "Mexico",
            "South Korea",
            played=None,
            update_status={},
            fresh_keys=set(),
            historical_rows=[],
            form_adjustments={
                "Mexico": {"attack_mult": 1.018, "note": "状态微调上调 1.8%（封顶 ±3%）"},
                "South Korea": {"attack_mult": 0.991, "note": "状态微调下调 0.9%（封顶 ±3%）"},
            },
        )

        joined = " ".join(meta["public_signals"])
        self.assertIn("Mexico 状态微调上调 1.8%", joined)
        self.assertIn("South Korea 状态微调下调 0.9%", joined)


if __name__ == "__main__":
    unittest.main()
