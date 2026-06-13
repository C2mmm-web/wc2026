"""
wc2026_pro.fetch_results — pull finished WC2026 results, upcoming fixtures, and
historical World Cup results from API-Football. The pipeline writes small JSON
sidecar files that data.py/main.py can consume without ever storing the API key.

    python3 fetch_results.py

Designed to run wherever there is outbound network access (your computer / cron).
Fails gracefully (non-zero exit, clear message) if the API can't be reached, so a
caller can fall back to another source.
"""
import datetime
import json
import os
import sys
import urllib.request
import unicodedata
from config import API_FOOTBALL_KEY, API_FOOTBALL_HOST, WC_LEAGUE_ID, WC_SEASON, HISTORY_SEASONS
from data import TEAMS

HERE = os.path.dirname(__file__)

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
    "bosnia": "Bosnia and Herzegovina",
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

def build_fetch_outputs(current_data, history_payloads, previous_results=None, checked_at=None):
    checked_at = checked_at or datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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

    current_status = "success" if not current_errors else "error"
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

def main():
    if not API_FOOTBALL_KEY:
        print("[fetch_results] no API key set (env API_FOOTBALL_KEY or .api_key) — skipping API import"); sys.exit(1)
    try:
        data = api(f"fixtures?league={WC_LEAGUE_ID}&season={WC_SEASON}")
    except Exception as e:
        print("[fetch_results] API unreachable:", e); sys.exit(2)
    if data.get("errors"):
        print("[fetch_results] API error:", data["errors"]); sys.exit(3)

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

    previous, previous_status = previous_results_from_pages()
    outputs = build_fetch_outputs(data, history_payloads, previous_results=previous)
    outputs["status"]["previous_site"] = previous_status

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
    print(f"[fetch_results] finished={len(outputs['finished'])} upcoming={len(outputs['upcoming'])} "
          f"fresh={len(outputs['fresh_results'])} history={len(outputs['historical_results'])}")
    if outputs["status"]["unknown_api_names"]:
        print("[fetch_results] UNMAPPED names (add to _ALIASES):", outputs["status"]["unknown_api_names"])

if __name__ == "__main__":
    main()
