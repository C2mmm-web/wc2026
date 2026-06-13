"""
wc2026_pro.fetch_results — pull finished WC2026 results + today's fixtures from
API-Football, and write them to live_results.json / live_fixtures.json, which
data.py merges automatically. Run this BEFORE main.py to auto-import scores.

    python3 fetch_results.py

Designed to run wherever there is outbound network access (your computer / cron).
Fails gracefully (non-zero exit, clear message) if the API can't be reached, so a
caller can fall back to another source.
"""
import json, os, sys, urllib.request, unicodedata
from config import API_FOOTBALL_KEY, API_FOOTBALL_HOST, WC_LEAGUE_ID, WC_SEASON
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

def main():
    if not API_FOOTBALL_KEY:
        print("[fetch_results] no API key set (env API_FOOTBALL_KEY or .api_key) — skipping API import"); sys.exit(1)
    try:
        data = api(f"fixtures?league={WC_LEAGUE_ID}&season={WC_SEASON}")
    except Exception as e:
        print("[fetch_results] API unreachable:", e); sys.exit(2)
    if data.get("errors"):
        print("[fetch_results] API error:", data["errors"]); sys.exit(3)

    finished, upcoming, unknown = {}, [], set()
    FT = {"FT", "AET", "PEN"}
    for fx in data.get("response", []):
        th = fx["teams"]["home"]["name"]; ta = fx["teams"]["away"]["name"]
        h, a = resolve(th), resolve(ta)
        if not h: unknown.add(th)
        if not a: unknown.add(ta)
        if not h or not a: continue
        status = fx["fixture"]["status"]["short"]
        if status in FT:
            gh = fx["goals"]["home"]; ga = fx["goals"]["away"]
            if gh is not None and ga is not None:
                finished[f"{h}|{a}"] = [int(gh), int(ga)]
        elif status in ("NS", "TBD", "1H", "2H", "HT", "LIVE"):
            upcoming.append({"home": h, "away": a, "status": status,
                             "date": fx["fixture"]["date"]})

    json.dump(finished, open(os.path.join(HERE, "live_results.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(upcoming, open(os.path.join(HERE, "live_fixtures.json"), "w"),
              ensure_ascii=False, indent=1)
    print(f"[fetch_results] finished={len(finished)} upcoming={len(upcoming)}")
    if unknown:
        print("[fetch_results] UNMAPPED names (add to _ALIASES):", sorted(unknown))

if __name__ == "__main__":
    main()
