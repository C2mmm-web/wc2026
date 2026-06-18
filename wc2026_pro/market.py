import datetime
import json
import math
import os
import urllib.request
import urllib.parse
import unicodedata


ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
DEFAULT_ODDS_SPORT_KEY = "soccer_fifa_world_cup"
API_FOOTBALL_ODDS_URL = "https://{host}/odds"


def _norm(value):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _resolver(teams):
    aliases = {
        "usa": "United States",
        "unitedstates": "United States",
        "korearepublic": "South Korea",
        "southkorea": "South Korea",
        "iran": "IR Iran",
        "iriran": "IR Iran",
        "ivorycoast": "Côte d'Ivoire",
        "cotedivoire": "Côte d'Ivoire",
        "czechrepublic": "Czechia",
        "czechia": "Czechia",
        "drcongo": "Congo DR",
        "congodr": "Congo DR",
        "capeverde": "Cabo Verde",
        "caboverde": "Cabo Verde",
        "curacao": "Curaçao",
        "turkey": "Türkiye",
        "turkiye": "Türkiye",
        "bosniaherzegovina": "Bosnia and Herzegovina",
        "bosniaandherzegovina": "Bosnia and Herzegovina",
    }
    canon = {_norm(team): team for team in teams}

    def resolve(name):
        n = _norm(name)
        return canon.get(n) or aliases.get(n)

    return resolve


def _american_to_prob(odds):
    odds = float(odds)
    return 100 / (odds + 100) if odds > 0 else (-odds) / (-odds + 100)


def _decimal_to_prob(odds):
    odds = float(odds)
    if odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return 1 / odds


def _devig(probs):
    total = sum(probs)
    return [p / total for p in probs] if total > 0 else [1 / len(probs)] * len(probs)


def _round_probs(probs):
    return [round(float(p), 4) for p in probs]


def _avg_prob_vectors(vectors):
    if not vectors:
        return None
    n = len(vectors)
    return [sum(row[i] for row in vectors) / n for i in range(3)]


def parse_the_odds_payload(payload, teams, checked_at=None, source="the-odds-api"):
    resolve = _resolver(teams)
    checked_at = checked_at or datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    matches = {}
    unknown = set()
    for event in payload or []:
        home = resolve(event.get("home_team"))
        away = resolve(event.get("away_team"))
        if not home:
            unknown.add(event.get("home_team"))
        if not away:
            unknown.add(event.get("away_team"))
        if not home or not away:
            continue

        vectors = []
        bookmaker_names = []
        last_update = None
        for bookmaker in event.get("bookmakers", []):
            markets = bookmaker.get("markets", [])
            h2h = next((market for market in markets if market.get("key") == "h2h"), None)
            if not h2h:
                continue
            prices = {}
            for outcome in h2h.get("outcomes", []):
                name = outcome.get("name")
                if name == "Draw":
                    prices["D"] = outcome.get("price")
                elif resolve(name) == home:
                    prices["H"] = outcome.get("price")
                elif resolve(name) == away:
                    prices["A"] = outcome.get("price")
            if all(key in prices for key in ("H", "D", "A")):
                vectors.append(_devig([_american_to_prob(prices["H"]), _american_to_prob(prices["D"]), _american_to_prob(prices["A"])]))
                bookmaker_names.append(bookmaker.get("title") or bookmaker.get("key") or "bookmaker")
                last_update = bookmaker.get("last_update") or last_update

        avg = _avg_prob_vectors(vectors)
        if avg:
            matches[f"{home}|{away}"] = {
                "home": home,
                "away": away,
                "probs": _round_probs(avg),
                "bookmakers": len(vectors),
                "bookmaker_names": bookmaker_names[:8],
                "commence_time": event.get("commence_time"),
                "last_update": last_update,
                "source": source,
            }

    return {
        "status": "success" if matches else "empty",
        "source": source,
        "checked_at": checked_at,
        "matches": matches,
        "unknown_names": sorted(name for name in unknown if name),
    }


def fetch_the_odds_api(api_key, teams, sport_key=None, regions="us,eu,uk", markets="h2h"):
    checked_at = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    if not api_key:
        return {
            "status": "no_key",
            "source": "the-odds-api",
            "checked_at": checked_at,
            "matches": {},
            "unknown_names": [],
        }
    sport_key = sport_key or DEFAULT_ODDS_SPORT_KEY
    query = (
        f"?apiKey={api_key}&regions={regions}&markets={markets}"
        "&oddsFormat=american&dateFormat=iso"
    )
    url = ODDS_API_URL.format(sport=sport_key) + query
    with urllib.request.urlopen(url, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    out = parse_the_odds_payload(payload, teams, checked_at=checked_at)
    out["sport_key"] = sport_key
    out["url"] = ODDS_API_URL.format(sport=sport_key)
    return out


def _fixture_teams_from_api_football_item(item, teams, fixture_lookup=None):
    resolve = _resolver(teams)
    fixture = item.get("fixture") or {}
    fixture_id = str(fixture.get("id") or fixture.get("fixture_id") or item.get("fixture_id") or "")
    if fixture_lookup and fixture_id and fixture_id in fixture_lookup:
        row = fixture_lookup[fixture_id]
        return row.get("home"), row.get("away")

    team_data = item.get("teams") or {}
    home_raw = ((team_data.get("home") or {}).get("name")
                or item.get("home")
                or item.get("home_team"))
    away_raw = ((team_data.get("away") or {}).get("name")
                or item.get("away")
                or item.get("away_team"))
    return resolve(home_raw) if home_raw else None, resolve(away_raw) if away_raw else None


def _api_football_value_side(value):
    raw = str(value or "").strip().lower()
    if raw in ("home", "1", "team 1", "localteam"):
        return "H"
    if raw in ("draw", "x", "tie"):
        return "D"
    if raw in ("away", "2", "team 2", "visitorteam"):
        return "A"
    return None


def parse_api_football_odds_payload(payload, teams, fixture_lookup=None, checked_at=None):
    checked_at = checked_at or datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    matches = {}
    unknown = set()
    response = payload.get("response", []) if isinstance(payload, dict) else payload or []
    for item in response:
        home, away = _fixture_teams_from_api_football_item(item, teams, fixture_lookup)
        if not home:
            unknown.add(((item.get("teams") or {}).get("home") or {}).get("name") or item.get("home"))
        if not away:
            unknown.add(((item.get("teams") or {}).get("away") or {}).get("name") or item.get("away"))
        if not home or not away:
            continue

        vectors = []
        bookmaker_names = []
        for bookmaker in item.get("bookmakers", []):
            bets = bookmaker.get("bets", [])
            match_winner = next(
                (
                    bet for bet in bets
                    if bet.get("id") == 1
                    or str(bet.get("name", "")).strip().lower()
                    in ("match winner", "winner", "1x2", "fulltime result", "full time result")
                ),
                None,
            )
            if not match_winner:
                continue

            prices = {}
            for outcome in match_winner.get("values", []):
                side = _api_football_value_side(outcome.get("value"))
                if side:
                    prices[side] = outcome.get("odd")
            if all(key in prices for key in ("H", "D", "A")):
                try:
                    vectors.append(_devig([
                        _decimal_to_prob(prices["H"]),
                        _decimal_to_prob(prices["D"]),
                        _decimal_to_prob(prices["A"]),
                    ]))
                except (TypeError, ValueError):
                    continue
                bookmaker_names.append(bookmaker.get("name") or str(bookmaker.get("id")) or "bookmaker")

        avg = _avg_prob_vectors(vectors)
        if avg:
            fixture = item.get("fixture") or {}
            matches[f"{home}|{away}"] = {
                "home": home,
                "away": away,
                "probs": _round_probs(avg),
                "bookmakers": len(vectors),
                "bookmaker_names": bookmaker_names[:8],
                "commence_time": fixture.get("date"),
                "last_update": item.get("update"),
                "source": "api-football-odds",
            }

    return {
        "status": "success" if matches else "empty",
        "source": "api-football-odds",
        "checked_at": checked_at,
        "matches": matches,
        "unknown_names": sorted(name for name in unknown if name),
    }


def fetch_api_football_odds(api_key, teams, fixture_lookup=None, host="v3.football.api-sports.io", league=1, season=2026):
    checked_at = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    if not api_key:
        return {
            "status": "no_key",
            "source": "api-football-odds",
            "checked_at": checked_at,
            "matches": {},
            "unknown_names": [],
        }
    query = urllib.parse.urlencode({"league": league, "season": season})
    url = f"{API_FOOTBALL_ODDS_URL.format(host=host)}?{query}"
    req = urllib.request.Request(url, headers={"x-apisports-key": api_key})
    with urllib.request.urlopen(req, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        return {
            "status": "error",
            "source": "api-football-odds",
            "checked_at": checked_at,
            "matches": {},
            "unknown_names": [],
            "error": str(errors)[:180],
            "url": API_FOOTBALL_ODDS_URL.format(host=host),
        }
    out = parse_api_football_odds_payload(
        payload,
        teams,
        fixture_lookup=fixture_lookup,
        checked_at=checked_at,
    )
    out["url"] = API_FOOTBALL_ODDS_URL.format(host=host)
    return out


def load_market_sidecar(path):
    if not path or not os.path.exists(path):
        return {"status": "not_run", "source": "none", "matches": {}}
    try:
        with open(path) as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            data.setdefault("matches", {})
            return data
    except Exception as exc:
        return {"status": "error", "source": "market_odds.json", "matches": {}, "error": str(exc)[:180]}
    return {"status": "invalid", "source": "market_odds.json", "matches": {}}


def market_probabilities(line):
    if isinstance(line, dict):
        probs = line.get("probs")
        if isinstance(probs, list) and len(probs) >= 3:
            return _devig([float(probs[0]), float(probs[1]), float(probs[2])])
    if isinstance(line, (list, tuple)) and len(line) >= 3:
        return _devig([_american_to_prob(line[0]), _american_to_prob(line[1]), _american_to_prob(line[2])])
    return None
