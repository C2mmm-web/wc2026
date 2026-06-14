import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "wc2026_pro"))


def fixture(home, away, hg, ag, status="FT", date="2026-06-14T13:00:00+00:00"):
    return {
        "fixture": {"date": date, "status": {"short": status}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": hg, "away": ag},
    }


class FetchResultsFeatureTests(unittest.TestCase):
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

    def test_build_fetch_outputs_default_timestamp_is_utc_iso(self):
        from fetch_results import build_fetch_outputs

        out = build_fetch_outputs({"errors": [], "response": []}, [])

        self.assertTrue(out["status"]["checked_at"].endswith("Z"))


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

        self.assertEqual(report["data_source"], "api_football_history")
        self.assertEqual(report["n_history"], len(rows))
        self.assertIn("model", report["test"])
        self.assertGreaterEqual(weight, 0.0)
        self.assertLessEqual(weight, 1.0)


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


if __name__ == "__main__":
    unittest.main()
