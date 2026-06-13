"""
wc2026_pro.data — SINGLE SOURCE OF TRUTH for all inputs (solves P8).
Every other module imports from here. No constant is duplicated elsewhere.
"""
MODEL_VERSION = "2.0.0"

# ---- 12 groups, 48 teams (official 2026 draw) ----
GROUPS = {
    "A": ["Mexico", "South Africa", "South Korea", "Czechia"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["United States", "Paraguay", "Australia", "Türkiye"],
    "E": ["Germany", "Curaçao", "Côte d'Ivoire", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "IR Iran", "New Zealand"],
    "H": ["Spain", "Cabo Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "Congo DR", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}
TEAMS = [t for g in GROUPS.values() for t in g]

# ---- REAL strength prior: World Football Elo (June 2026) ----
# Anchored to published values (Spain 2157, Argentina 2115, France 2063, Brazil 1991).
ELO_PRIOR = {
    "Spain": 2157, "Argentina": 2115, "France": 2063, "England": 2008,
    "Portugal": 1998, "Brazil": 1991, "Netherlands": 1976, "Germany": 1962,
    "Belgium": 1938, "Croatia": 1899, "Colombia": 1884, "Uruguay": 1880,
    "Morocco": 1868, "Switzerland": 1842, "Senegal": 1836, "Japan": 1828,
    "United States": 1818, "Ecuador": 1800, "Norway": 1804, "Austria": 1792,
    "Mexico": 1808, "Sweden": 1778, "Türkiye": 1776, "IR Iran": 1762,
    "Egypt": 1758, "Côte d'Ivoire": 1754, "Canada": 1758, "South Korea": 1752,
    "Algeria": 1744, "Australia": 1742, "Bosnia and Herzegovina": 1736,
    "Czechia": 1734, "Paraguay": 1728, "Scotland": 1718, "Tunisia": 1710,
    "Congo DR": 1702, "Ghana": 1700, "Panama": 1690, "Qatar": 1682,
    "Uzbekistan": 1678, "Saudi Arabia": 1668, "South Africa": 1652,
    "Iraq": 1640, "Jordan": 1630, "Cabo Verde": 1618, "Curaçao": 1576,
    "Haiti": 1558, "New Zealand": 1548,
}

HOSTS = {"United States", "Canada", "Mexico"}

# ---- REAL bookmaker title odds (American) ----
MARKET_TITLE = {
    "Spain": 450, "France": 475, "England": 750, "Portugal": 800,
    "Brazil": 900, "Argentina": 900, "Germany": 1400, "Netherlands": 2000,
    "Norway": 3000, "Colombia": 4000, "United States": 6000, "Senegal": 9000,
}

# ---- REAL match results already played (home|away -> (hg, ag)) ----
PLAYED = {
    ("Mexico", "South Africa"): (2, 0),
    ("South Korea", "Czechia"): (2, 1),
    ("United States", "Paraguay"): (4, 1),
    ("Canada", "Bosnia and Herzegovina"): (1, 1),
    ("Qatar", "Switzerland"): (1, 1),   # 13 Jun: Khoukhi equalises late
}

# Auto-merge results pulled by fetch_results.py (API-Football). Stored both
# orientations so lookups work everywhere. Live data overrides hand entries.
import os as _os, json as _json
LIVE_RESULT_KEYS = set()
_LR = _os.path.join(_os.path.dirname(__file__), "live_results.json")
if _os.path.exists(_LR):
    try:
        with open(_LR) as _fp:
            _live_results = _json.load(_fp)
        for _k, _v in _live_results.items():
            _h, _a = _k.split("|"); _hg, _ag = int(_v[0]), int(_v[1])
            LIVE_RESULT_KEYS.add((_h, _a))
            PLAYED[(_h, _a)] = (_hg, _ag); PLAYED[(_a, _h)] = (_ag, _hg)
    except Exception as _e:
        print("[data] could not merge live_results.json:", _e)

def _load_json_sidecar(_name, _default):
    _path = _os.path.join(_os.path.dirname(__file__), _name)
    if not _os.path.exists(_path):
        return _default
    try:
        with open(_path) as _fp:
            return _json.load(_fp)
    except Exception as _e:
        print(f"[data] could not load {_name}:", _e)
        return _default

FRESH_RESULT_KEYS = set()
for _k in _load_json_sidecar("fresh_results.json", {}).keys():
    try:
        _h, _a = _k.split("|")
        FRESH_RESULT_KEYS.add((_h, _a))
    except ValueError:
        pass

HISTORICAL_RESULTS = _load_json_sidecar("historical_results.json", [])
UPDATE_STATUS = _load_json_sidecar("update_status.json", {
    "checked_at": None,
    "current_results": {"status": "not_run", "finished": 0, "upcoming": 0},
    "history": {"status": "not_run", "matches": 0},
})

# ---- REAL live bookmaker match lines: (home, draw, away) American, fixture orientation ----
MARKET_MATCH = {
    ("Brazil", "Morocco"): (-175, 300, 425),
    ("Haiti", "Scotland"): (260, 250, -110),
    ("Australia", "Türkiye"): (200, 235, 125),
    # 14 Jun fixtures
    ("Germany", "Curaçao"): (-2000, 1900, 5000),
    ("Netherlands", "Japan"): (100, 260, 290),
    ("Sweden", "Tunisia"): (-111, 255, 350),
}

# ---- Venue context: altitude (m) and a heat index 0..1 for host summer venues ----
# (P6) Affects fatigue/finishing; only a few venues are extreme.
VENUE = {
    ("Mexico", "South Africa"): {"alt": 2240, "heat": 0.3},   # Mexico City
    ("South Korea", "Czechia"): {"alt": 1566, "heat": 0.4},   # Guadalajara
    ("Qatar", "Switzerland"): {"alt": 5, "heat": 0.2},        # Bay Area, mild
    ("Brazil", "Morocco"): {"alt": 5, "heat": 0.4},           # NJ
}
DEFAULT_VENUE = {"alt": 50, "heat": 0.45}  # NA summer baseline

# ---- Rest days before each upcoming match (P6); matchday 1 = ~equal rest ----
# Filled per matchday in schedule; here defaults handled in context.py.

# ---- Structured availability / judgment input (P7) ----
# Each entry: list of (player_importance, side) where side in {home, away}.
# importance ~ share of team's attacking/defensive output the player represents
# (0.04 squad rotation .. 0.18 talismanic). Out = subtract; returning = ignore.
INJURIES = {
    ("Brazil", "Morocco"): {
        "home_out": [("Neymar", 0.05, "creative")],          # not nailed-on
        "away_out": [("Nayef Aguerd", 0.14, "defensive"),    # 1st-choice CB
                     ("Abde Ezzalzouli", 0.06, "attacking")],
        "note": "Morocco lose 1st-choice CB Aguerd (groin) + winger Abde; their "
                "low-block is their main asset vs Brazil, so it is downgraded. "
                "Neymar (calf) out for Brazil but not a guaranteed starter."
    },
    ("Qatar", "Switzerland"): {
        "home_out": [], "away_out": [],
        "note": "Switzerland at full strength (Embolo cleared). Qatar no fresh "
                "injuries but limited attacking ceiling; expect a low block."
    },
}
