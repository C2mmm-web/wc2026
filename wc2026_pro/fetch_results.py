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
from config import API_FOOTBALL_KEY, API_FOOTBALL_HOST, WC_LEAGUE_ID, WC_SEASON, HISTORY_SEASONS
from data import TEAMS

HERE = os.path.dirname(__file__)
OPENFOOTBALL_CURRENT_URL = (
    "https://raw.githubusercontent.com/upbound-web/worldcup-live.json/"
    "master/2026/worldcup.json"
)

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

def build_fetch_outputs(current_data, history_payloads, previous_results=None, checked_at=None, fallback_current=None):
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
            dedupe = (season, fx["fixture"].get("date"), h, a)
            if dedupe in seen_history:
                continue
            seen_history.add(dedupe)
            historical.append({
                "date": fx["fixture"].get("date"),
                "season": season,
                "league": payload.get("league"),
                "home": h,
                "away": a,
                "home_goals": int(gh),
                "away_goals": int(ga),
                "neutral": True,
            })
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
            },
            "unknown_api_names": sorted(unknown),
        },
    }

def write_outputs(outputs):
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
    if not API_FOOTBALL_KEY:
        outputs = build_fetch_outputs(
            {"errors": {"auth": "API_FOOTBALL_KEY is not set"}, "response": []},
            [],
            previous_results=previous,
            fallback_current=fallback_current,
        )
        outputs["status"]["previous_site"] = previous_status
        if outputs["status"]["current_results"]["status"] != "fallback_success":
            outputs["status"]["current_results"]["status"] = "no_key"
        outputs["status"]["history"]["status"] = "no_key"
        write_outputs(outputs)
        print("[fetch_results] no API key set — wrote keyless current results from fallback source")
        return
    try:
        data = api(f"fixtures?league={WC_LEAGUE_ID}&season={WC_SEASON}")
    except Exception as e:
        print("[fetch_results] API unreachable:", e)
        data = {"errors": {"request": str(e)}, "response": []}
    if data.get("errors"):
        print("[fetch_results] API error:", data["errors"])

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
    outputs = build_fetch_outputs(data, history_payloads, previous_results=previous, fallback_current=fallback_current)
    outputs["status"]["previous_site"] = previous_status

    write_outputs(outputs)
    print(f"[fetch_results] finished={len(outputs['finished'])} upcoming={len(outputs['upcoming'])} "
          f"fresh={len(outputs['fresh_results'])} history={len(outputs['historical_results'])}")
    if outputs["status"]["unknown_api_names"]:
        print("[fetch_results] UNMAPPED names (add to _ALIASES):", outputs["status"]["unknown_api_names"])

if __name__ == "__main__":
    main()
