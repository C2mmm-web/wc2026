"""
wc2026_pro.fetch_results — pull finished WC2026 results, upcoming fixtures, and
historical World Cup results. API-Football is used when available; a keyless
openfootball/upbound JSON source is used as the current-results fallback.
The pipeline writes small JSON sidecar files that data.py/main.py can consume
without ever storing the API key.

    python3 fetch_results.py

Designed to run wherever there is outbound network access (your computer / cron).
Fails gracefully (non-zero exit, clear message) if the API can't be reached, so a
caller can fall back to another source.
"""
import datetime
import json
import os
import re
import sys
import urllib.request
import unicodedata
from config import (
    API_FOOTBALL_KEY,
    API_FOOTBALL_HOST,
    ODDS_API_KEY,
    ODDS_API_SPORT_KEY,
    WC_LEAGUE_ID,
    WC_SEASON,
    HISTORY_SEASONS,
)
from data import TEAMS
from market import fetch_api_football_odds, fetch_the_odds_api

HERE = os.path.dirname(__file__)
OPENFOOTBALL_CURRENT_URL = (
    "https://raw.githubusercontent.com/upbound-web/worldcup-live.json/"
    "master/2026/worldcup.json"
)
OPENFOOTBALL_HISTORY_SEASONS = (2014, 2018, 2022)
OPENFOOTBALL_HISTORY_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/{season}/worldcup.json"

# --- map API-Football country names -> our canonical team names ---
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())
_CANON = {norm(t): t for t in TEAMS}
_ALIASES = {
    "usa": "United States", "unitedstates": "United States",
    "korearepublic": "South Korea", "southkorea": "South Korea",
    "iran": "IR Iran", "iriran": "IR Iran",
    "ivorycoast": "Côte d'Ivoire", "cotedivoire": "Côte d'Ivoire",
    "czechrepublic": "Czechia", "czechia": "Czechia",
    "drcongo": "Congo DR", "congodr": "Congo DR",
    "democraticrepublicofcongo": "Congo DR",
    "capeverde": "Cabo Verde", "capeverdeislands": "Cabo Verde",
    "caboverde": "Cabo Verde", "curacao": "Curaçao",
    "turkey": "Türkiye", "turkiye": "Türkiye",
    "bosnia": "Bosnia and Herzegovina", "bosniaherzegovina": "Bosnia and Herzegovina",
    "bosniaandherzegovina": "Bosnia and Herzegovina",
}
def resolve(api_name):
    n = norm(api_name)
    return _CANON.get(n) or _ALIASES.get(n)

def api(path):
    req = urllib.request.Request("https://" + API_FOOTBALL_HOST + "/" + path,
                                 headers={"x-apisports-key": API_FOOTBALL_KEY})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def result_key(home, away):
    return f"{home}|{away}"

def _match_from_fixture(fx):
    th = fx["teams"]["home"]["name"]; ta = fx["teams"]["away"]["name"]
    h, a = resolve(th), resolve(ta)
    unknown = []
    if not h: unknown.append(th)
    if not a: unknown.append(ta)
    return h, a, unknown

def previous_results_from_payload(payload):
    out = {}
    for m in payload.get("matches", []):
        if m.get("played"):
            out[result_key(m["home"], m["away"])] = [int(m["played"][0]), int(m["played"][1])]
    return out

def previous_results_from_pages():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return {}, {"status": "skipped", "reason": "GITHUB_REPOSITORY not set"}
    owner, name = repo.split("/", 1)
    url = f"https://{owner.lower()}.github.io/{name}/predictions.json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.load(r)
        return previous_results_from_payload(payload), {"status": "success", "url": url}
    except Exception as e:
        return {}, {"status": "unavailable", "url": url, "reason": str(e)[:180]}

def fetch_openfootball_current(url=OPENFOOTBALL_CURRENT_URL):
    with urllib.request.urlopen(url, timeout=25) as r:
        return openfootball_current_from_payload(json.load(r), url)

def _openfootball_date(match):
    date = match.get("date") or ""
    time = match.get("time")
    return f"{date} {time}" if time else date

def _is_openfootball_placeholder(name):
    return bool(re.fullmatch(r"(?:[WL]?\d+[A-Z]?|3[A-Z](?:/[A-Z])*)", name or ""))

def openfootball_current_from_payload(payload, source_url=OPENFOOTBALL_CURRENT_URL):
    finished, upcoming, unknown = {}, [], set()
    skipped_placeholders = 0
    for match in payload.get("matches", []):
        raw_home = match.get("team1")
        raw_away = match.get("team2")
        home = resolve(raw_home or "")
        away = resolve(raw_away or "")
        if not home or not away:
            skipped_placeholders += 1
            if raw_home and not home and not _is_openfootball_placeholder(raw_home):
                unknown.add(raw_home)
            if raw_away and not away and not _is_openfootball_placeholder(raw_away):
                unknown.add(raw_away)
            continue
        score = match.get("score") or {}
        ft = score.get("ft")
        if isinstance(ft, list) and len(ft) >= 2 and ft[0] is not None and ft[1] is not None:
            finished[result_key(home, away)] = [int(ft[0]), int(ft[1])]
        else:
            upcoming.append({
                "home": home,
                "away": away,
                "status": "NS",
                "date": _openfootball_date(match),
                "round": match.get("round"),
                "ground": match.get("ground"),
            })
    return {
        "status": "success",
        "source": "upbound-web/worldcup-live.json",
        "url": source_url,
        "finished": finished,
        "upcoming": upcoming,
        "unknown_names": sorted(unknown),
        "skipped_placeholders": skipped_placeholders,
    }

def openfootball_history_from_payload(payload, season, source_url):
    rows, unknown = [], set()
    for match in payload.get("matches", []):
        raw_home = match.get("team1")
        raw_away = match.get("team2")
        home = resolve(raw_home or "")
        away = resolve(raw_away or "")
        if not home or not away:
            if raw_home and not home and not _is_openfootball_placeholder(raw_home):
                unknown.add(raw_home)
            if raw_away and not away and not _is_openfootball_placeholder(raw_away):
                unknown.add(raw_away)
            continue
        score = match.get("score") or {}
        ft = score.get("ft")
        if not (isinstance(ft, list) and len(ft) >= 2 and ft[0] is not None and ft[1] is not None):
            continue
        rows.append({
            "date": match.get("date"),
            "season": season,
            "home": home,
            "away": away,
            "home_goals": int(ft[0]),
            "away_goals": int(ft[1]),
            "neutral": True,
            "source": "openfootball_worldcup",
        })
    return rows, {
        "status": "success",
        "season": season,
        "url": source_url,
        "rows": len(rows),
        "unknown_names": sorted(unknown),
    }

def fetch_openfootball_history(seasons=OPENFOOTBALL_HISTORY_SEASONS):
    all_rows, statuses, errors = [], [], []
    for season in seasons:
        url = OPENFOOTBALL_HISTORY_URL.format(season=season)
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                rows, status = openfootball_history_from_payload(json.load(r), season, url)
            all_rows.extend(rows)
            statuses.append(status)
        except Exception as e:
            errors.append({"season": season, "url": url, "error": str(e)[:180]})
    return {
        "status": "success" if all_rows else ("error" if errors else "empty"),
        "source": "openfootball/worldcup.json",
        "seasons": list(seasons),
        "rows": all_rows,
        "season_status": statuses,
        "errors": errors,
    }

def _merge_fallback_current(finished, upcoming, unknown, fallback_current):
    if not fallback_current or fallback_current.get("status") != "success":
        return 0, 0
    added_finished = 0
    added_upcoming = 0
    for key, score in fallback_current.get("finished", {}).items():
        if key not in finished:
            finished[key] = score
            added_finished += 1
    seen_upcoming = {(m.get("home"), m.get("away")) for m in upcoming}
    seen_finished = set(finished.keys())
    for match in fallback_current.get("upcoming", []):
        pair = (match.get("home"), match.get("away"))
        if pair in seen_upcoming or result_key(*pair) in seen_finished:
            continue
        upcoming.append(match)
        seen_upcoming.add(pair)
        added_upcoming += 1
    unknown.update(fallback_current.get("unknown_names", []))
    return added_finished, added_upcoming

def build_fetch_outputs(current_data, history_payloads, previous_results=None, checked_at=None, fallback_current=None, fallback_history=None):
    checked_at = checked_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    previous_results = previous_results or {}
    finished, upcoming, unknown = {}, [], set()
    FT = {"FT", "AET", "PEN"}
    current_errors = current_data.get("errors")

    for fx in current_data.get("response", []):
        h, a, names = _match_from_fixture(fx)
        unknown.update(names)
        if not h or not a:
            continue
        status = fx["fixture"]["status"]["short"]
        if status in FT:
            gh = fx["goals"]["home"]; ga = fx["goals"]["away"]
            if gh is not None and ga is not None:
                finished[result_key(h, a)] = [int(gh), int(ga)]
        elif status in ("NS", "TBD", "1H", "2H", "HT", "LIVE"):
            upcoming.append({"home": h, "away": a, "status": status,
                             "date": fx["fixture"]["date"]})

    added_finished, added_upcoming = _merge_fallback_current(finished, upcoming, unknown, fallback_current)

    fresh_results = {
        k: v for k, v in finished.items()
        if previous_results.get(k) != v
    }

    historical = []
    history_errors = []
    seen_history = set()
    def add_history_row(row):
        try:
            season = row.get("season")
            h, a = row.get("home"), row.get("away")
            hg, ag = int(row.get("home_goals")), int(row.get("away_goals"))
        except (TypeError, ValueError):
            return False
        dedupe = (season, row.get("date"), h, a)
        if not h or not a or dedupe in seen_history:
            return False
        seen_history.add(dedupe)
        historical.append({
            "date": row.get("date"),
            "season": season,
            "league": row.get("league"),
            "home": h,
            "away": a,
            "home_goals": hg,
            "away_goals": ag,
            "neutral": bool(row.get("neutral", True)),
            "source": row.get("source", "api_football"),
        })
        return True

    for payload in history_payloads:
        season = payload.get("season")
        data = payload.get("data", {})
        if data.get("errors"):
            history_errors.append({"season": season, "errors": data.get("errors")})
            continue
        for fx in data.get("response", []):
            h, a, names = _match_from_fixture(fx)
            unknown.update(names)
            if not h or not a:
                continue
            status = fx["fixture"]["status"]["short"]
            gh = fx["goals"]["home"]; ga = fx["goals"]["away"]
            if status not in FT or gh is None or ga is None:
                continue
            add_history_row({
                "date": fx["fixture"].get("date"),
                "season": season,
                "league": payload.get("league"),
                "home": h,
                "away": a,
                "home_goals": int(gh),
                "away_goals": int(ga),
                "neutral": True,
                "source": "api_football",
            })
    free_history_rows = 0
    if fallback_history and fallback_history.get("status") == "success":
        for row in fallback_history.get("rows", []):
            if add_history_row(row):
                free_history_rows += 1
    historical.sort(key=lambda r: r.get("date") or "")

    if current_errors and fallback_current and fallback_current.get("status") == "success":
        current_status = "fallback_success"
    elif not current_errors:
        current_status = "success"
    else:
        current_status = "error"
    history_status = "success" if historical else ("error" if history_errors else "empty")
    return {
        "finished": finished,
        "upcoming": upcoming,
        "fresh_results": fresh_results,
        "historical_results": historical,
        "status": {
            "checked_at": checked_at,
            "current_results": {
                "status": current_status,
                "finished": len(finished),
                "upcoming": len(upcoming),
                "errors": current_errors or None,
                "fallback_source": {
                    "status": fallback_current.get("status"),
                    "source": fallback_current.get("source"),
                    "url": fallback_current.get("url"),
                    "added_finished": added_finished,
                    "added_upcoming": added_upcoming,
                    "source_finished": len(fallback_current.get("finished", {})),
                    "source_upcoming": len(fallback_current.get("upcoming", [])),
                    "skipped_placeholders": fallback_current.get("skipped_placeholders", 0),
                } if fallback_current else None,
            },
            "history": {
                "status": history_status,
                "matches": len(historical),
                "seasons": [p.get("season") for p in history_payloads],
                "errors": history_errors or None,
                "free_source": {
                    "status": fallback_history.get("status"),
                    "source": fallback_history.get("source"),
                    "seasons": fallback_history.get("seasons"),
                    "rows": free_history_rows,
                    "errors": fallback_history.get("errors") or None,
                } if fallback_history else None,
            },
            "unknown_api_names": sorted(unknown),
        },
    }

def write_outputs(outputs, market_odds=None):
    json.dump(outputs["finished"], open(os.path.join(HERE, "live_results.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(outputs["upcoming"], open(os.path.join(HERE, "live_fixtures.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(outputs["fresh_results"], open(os.path.join(HERE, "fresh_results.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(outputs["historical_results"], open(os.path.join(HERE, "historical_results.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(outputs["status"], open(os.path.join(HERE, "update_status.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(market_odds or {"status": "not_run", "matches": {}}, open(os.path.join(HERE, "market_odds.json"), "w"),
              ensure_ascii=False, indent=1)

def _read_json_sidecar(here, name, default):
    path = os.path.join(here, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fp:
            return json.load(fp)
    except Exception:
        return default

def preserve_previous_sidecars(outputs, here=HERE):
    current_status = (outputs.get("status") or {}).get("current_results") or {}
    fallback_status = (current_status.get("fallback_source") or {}).get("status")
    current_unavailable = current_status.get("status") in ("error", "no_key") and fallback_status != "success"
    if current_unavailable and not outputs.get("finished") and not outputs.get("upcoming"):
        previous_finished = _read_json_sidecar(here, "live_results.json", {})
        previous_upcoming = _read_json_sidecar(here, "live_fixtures.json", [])
        if previous_finished or previous_upcoming:
            outputs["finished"] = previous_finished if isinstance(previous_finished, dict) else {}
            outputs["upcoming"] = previous_upcoming if isinstance(previous_upcoming, list) else []
            outputs["fresh_results"] = {}
            current_status["status"] = "preserved"
            current_status["preserved_reason"] = "current sources unavailable; kept previous sidecars"
            current_status["finished"] = len(outputs["finished"])
            current_status["upcoming"] = len(outputs["upcoming"])

    history_status = (outputs.get("status") or {}).get("history") or {}
    if not outputs.get("historical_results") and history_status.get("status") in ("error", "empty", "no_key"):
        previous_history = _read_json_sidecar(here, "historical_results.json", [])
        if isinstance(previous_history, list) and previous_history:
            outputs["historical_results"] = previous_history
            history_status["status"] = "preserved"
            history_status["preserved_reason"] = "history sources unavailable; kept previous sidecar"
            history_status["matches"] = len(previous_history)
    return outputs

def _fixture_lookup_from_api_payload(payload):
    lookup = {}
    for fx in (payload or {}).get("response", []):
        fixture = fx.get("fixture") or {}
        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        home, away, _unknown = _match_from_fixture(fx)
        if home and away:
            lookup[str(fixture_id)] = {
                "home": home,
                "away": away,
                "date": fixture.get("date"),
                "status": ((fixture.get("status") or {}).get("short")),
            }
    return lookup

def fetch_market_odds_safe(fixture_payload=None):
    primary = None
    try:
        primary = fetch_the_odds_api(ODDS_API_KEY, TEAMS, sport_key=ODDS_API_SPORT_KEY)
    except Exception as e:
        primary = {
            "status": "error",
            "source": "the-odds-api",
            "sport_key": ODDS_API_SPORT_KEY,
            "matches": {},
            "unknown_names": [],
            "error": str(e)[:180],
        }
    if primary.get("status") == "success" and primary.get("matches"):
        return primary

    if API_FOOTBALL_KEY:
        try:
            fallback = fetch_api_football_odds(
                API_FOOTBALL_KEY,
                TEAMS,
                fixture_lookup=_fixture_lookup_from_api_payload(fixture_payload),
                host=API_FOOTBALL_HOST,
                league=WC_LEAGUE_ID,
                season=WC_SEASON,
            )
        except Exception as e:
            fallback = {
                "status": "error",
                "source": "api-football-odds",
                "matches": {},
                "unknown_names": [],
                "error": str(e)[:180],
            }
        if fallback.get("status") == "success" and fallback.get("matches"):
            fallback["fallback_from"] = {
                "source": primary.get("source"),
                "status": primary.get("status"),
                "error": primary.get("error"),
            }
            return fallback
        primary["fallback_attempt"] = {
            "source": fallback.get("source"),
            "status": fallback.get("status"),
            "matches": len(fallback.get("matches", {})),
            "error": fallback.get("error"),
            "unknown_names": fallback.get("unknown_names", []),
        }
    return primary

def main():
    previous, previous_status = previous_results_from_pages()
    try:
        fallback_current = fetch_openfootball_current()
    except Exception as e:
        print("[fetch_results] openfootball fallback unavailable:", e)
        fallback_current = {
            "status": "error",
            "source": "upbound-web/worldcup-live.json",
            "url": OPENFOOTBALL_CURRENT_URL,
            "error": str(e)[:180],
            "finished": {},
            "upcoming": [],
            "unknown_names": [],
        }
    try:
        fallback_history = fetch_openfootball_history()
    except Exception as e:
        print("[fetch_results] openfootball history unavailable:", e)
        fallback_history = {
            "status": "error",
            "source": "openfootball/worldcup.json",
            "seasons": list(OPENFOOTBALL_HISTORY_SEASONS),
            "rows": [],
            "errors": [{"error": str(e)[:180]}],
        }
    if not API_FOOTBALL_KEY:
        outputs = build_fetch_outputs(
            {"errors": {"auth": "API_FOOTBALL_KEY is not set"}, "response": []},
            [],
            previous_results=previous,
            fallback_current=fallback_current,
            fallback_history=fallback_history,
        )
        outputs["status"]["previous_site"] = previous_status
        if outputs["status"]["current_results"]["status"] != "fallback_success":
            outputs["status"]["current_results"]["status"] = "no_key"
        if not outputs["historical_results"]:
            outputs["status"]["history"]["status"] = "no_key"
        market_odds = fetch_market_odds_safe()
        write_outputs(preserve_previous_sidecars(outputs), market_odds=market_odds)
        print("[fetch_results] no API key set — wrote keyless current results from fallback source")
        print(f"[fetch_results] market odds status={market_odds.get('status')} matches={len(market_odds.get('matches', {}))}")
        return
    try:
        data = api(f"fixtures?league={WC_LEAGUE_ID}&season={WC_SEASON}")
    except Exception as e:
        print("[fetch_results] API unreachable:", e)
        data = {"errors": {"request": str(e)}, "response": []}
    if data.get("errors"):
        print("[fetch_results] API error:", data["errors"])
    market_odds = fetch_market_odds_safe(data)

    history_payloads = []
    for season in HISTORY_SEASONS:
        try:
            history_payloads.append({
                "league": WC_LEAGUE_ID,
                "season": season,
                "data": api(f"fixtures?league={WC_LEAGUE_ID}&season={season}"),
            })
        except Exception as e:
            history_payloads.append({
                "league": WC_LEAGUE_ID,
                "season": season,
                "data": {"errors": {"request": str(e)}, "response": []},
            })
    outputs = build_fetch_outputs(data, history_payloads, previous_results=previous,
                                  fallback_current=fallback_current, fallback_history=fallback_history)
    outputs["status"]["previous_site"] = previous_status

    outputs = preserve_previous_sidecars(outputs)
    write_outputs(outputs, market_odds=market_odds)
    print(f"[fetch_results] finished={len(outputs['finished'])} upcoming={len(outputs['upcoming'])} "
          f"fresh={len(outputs['fresh_results'])} history={len(outputs['historical_results'])}")
    print(f"[fetch_results] market odds status={market_odds.get('status')} matches={len(market_odds.get('matches', {}))}")
    if outputs["status"]["unknown_api_names"]:
        print("[fetch_results] UNMAPPED names (add to _ALIASES):", outputs["status"]["unknown_api_names"])

if __name__ == "__main__":
    main()
