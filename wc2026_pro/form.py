"""
wc2026_pro.form — conservative in-tournament form adjustments.

The goal is to let finished matches nudge later predictions without letting one
result overpower the long-run Elo / Dixon-Coles priors.
"""
from data import ELO_PRIOR

MIN_ATTACK_MULT = 0.97
MAX_ATTACK_MULT = 1.03


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _points(gf, ga):
    return 3 if gf > ga else 1 if gf == ga else 0


def _expected_points(team, opp):
    diff = ELO_PRIOR[team] - ELO_PRIOR[opp]
    elo_win = 1 / (1 + 10 ** (-diff / 400))
    draw = _clip(0.28 - min(abs(diff), 450) * 0.00016, 0.20, 0.28)
    win = (1 - draw) * elo_win
    return 3 * win + draw


def _team_signal(team, opp, gf, ga):
    pts_surprise = _points(gf, ga) - _expected_points(team, opp)
    gd = _clip(gf - ga, -2, 2)
    return 0.006 * pts_surprise + 0.004 * gd


def build_form_adjustments(played):
    raw = {team: {"signal": 0.0, "matches": 0} for team in ELO_PRIOR}
    seen = set()
    for (home, away), score in played.items():
        if home not in ELO_PRIOR or away not in ELO_PRIOR:
            continue
        dedupe = tuple(sorted((home, away)))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        hg, ag = int(score[0]), int(score[1])
        raw[home]["signal"] += _team_signal(home, away, hg, ag)
        raw[away]["signal"] += _team_signal(away, home, ag, hg)
        raw[home]["matches"] += 1
        raw[away]["matches"] += 1

    out = {}
    for team, row in raw.items():
        if row["matches"] <= 0:
            continue
        attack_mult = _clip(1.0 + row["signal"], MIN_ATTACK_MULT, MAX_ATTACK_MULT)
        direction = "上调" if attack_mult >= 1.0005 else "下调" if attack_mult <= 0.9995 else "持平"
        pct = abs(attack_mult - 1.0) * 100
        out[team] = {
            "attack_mult": round(attack_mult, 4),
            "matches": row["matches"],
            "note": f"状态微调{direction} {pct:.1f}%（封顶 ±3%，避免单场过度反应）",
        }
    return out


def attack_mult(adjustments, team):
    return float((adjustments or {}).get(team, {}).get("attack_mult", 1.0))
