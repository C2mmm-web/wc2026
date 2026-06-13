"""
Config for wc2026_pro. The API key is read from the environment / a local
untracked file — it is NEVER hard-coded here, so this file is safe to push to a
public GitHub repo.

  • On GitHub Actions: set a repo Secret named API_FOOTBALL_KEY.
  • Locally (optional): `export API_FOOTBALL_KEY=xxxx`  OR put the key in a file
    named `.api_key` in this folder (it is git-ignored).
If no key is found, fetch_results.py simply skips the API and the pipeline falls
back to manually/searched data — nothing breaks.
"""
import os

def _load_key():
    k = os.environ.get("API_FOOTBALL_KEY")
    if k:
        return k.strip()
    p = os.path.join(os.path.dirname(__file__), ".api_key")
    if os.path.exists(p):
        return open(p).read().strip()
    return None

API_FOOTBALL_KEY = _load_key()
API_FOOTBALL_HOST = "v3.football.api-sports.io"
WC_LEAGUE_ID = 1       # FIFA World Cup
WC_SEASON = 2026
