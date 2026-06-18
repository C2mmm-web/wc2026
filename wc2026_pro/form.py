"""
wc2026_pro.form — conservative in-tournament form adjustments.

The goal is to let finished matches nudge later predictions without letting one
result overpower the long-run Elo / Dixon-Coles priors.
"""
from data import ELO_PRIOR

MIN_ATTACK_MULT = 0.97
MAX_ATTACK_MULT = 1.03
MIN_GROUP_CONTEXT_MULT = 0.982
MAX_GROUP_CONTEXT_MULT = 1.018
MIN_COMBINED_ATTACK_MULT = 0.96
MAX_COMBINED_ATTACK_MULT = 1.04


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


def _unique_group_results(groups, played):
    group_for = {}
    for group, teams in groups.items():
        for team in teams:
            group_for[team] = group

    rows = []
    seen = set()
    for (home, away), score in played.items():
        if home not in group_for or away not in group_for:
            continue
        if group_for[home] != group_for[away]:
            continue
        dedupe = tuple(sorted((home, away)))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        rows.append((group_for[home], home, away, int(score[0]), int(score[1])))
    return rows


def build_group_context_adjustments(groups, played):
    rows = _unique_group_results(groups, played)
    stats = {
        team: {"pts": 0, "gf": 0, "ga": 0, "played": 0, "group_matches": 0}
        for teams in groups.values()
        for team in teams
    }
    group_matches = {group: 0 for group in groups}
    for group, home, away, hg, ag in rows:
        group_matches[group] += 1
        stats[home]["played"] += 1
        stats[away]["played"] += 1
        stats[home]["gf"] += hg
        stats[home]["ga"] += ag
        stats[away]["gf"] += ag
        stats[away]["ga"] += hg
        if hg > ag:
            stats[home]["pts"] += 3
        elif ag > hg:
            stats[away]["pts"] += 3
        else:
            stats[home]["pts"] += 1
            stats[away]["pts"] += 1

    out = {}
    for group, teams in groups.items():
        played_in_group = group_matches.get(group, 0)
        if played_in_group <= 0:
            continue
        for team in teams:
            row = stats[team]
            if row["played"] <= 0:
                continue
            gd = row["gf"] - row["ga"]
            signal = 0.003 * (row["pts"] - row["played"])
            signal += 0.002 * _clip(gd, -2, 2)
            signal += 0.001 * _clip(row["gf"], 0, 3)
            attack_mult = _clip(1.0 + signal, MIN_GROUP_CONTEXT_MULT, MAX_GROUP_CONTEXT_MULT)
            direction = "上调" if attack_mult >= 1.0005 else "下调" if attack_mult <= 0.9995 else "持平"
            pct = abs(attack_mult - 1.0) * 100
            out[team] = {
                "attack_mult": round(attack_mult, 4),
                "matches": row["played"],
                "group_matches": played_in_group,
                "note": f"小组形势微调{direction} {pct:.1f}%（基于 {played_in_group} 场已完赛，单层封顶 ±1.8%）",
            }
    return out


def build_combined_adjustments(groups, played):
    form = build_form_adjustments(played)
    context = build_group_context_adjustments(groups, played)
    out = {}
    teams = set(form) | set(context)
    for team in teams:
        form_mult = float(form.get(team, {}).get("attack_mult", 1.0))
        context_mult = float(context.get(team, {}).get("attack_mult", 1.0))
        attack_mult = _clip(form_mult * context_mult, MIN_COMBINED_ATTACK_MULT, MAX_COMBINED_ATTACK_MULT)
        direction = "上调" if attack_mult >= 1.0005 else "下调" if attack_mult <= 0.9995 else "持平"
        pct = abs(attack_mult - 1.0) * 100
        parts = []
        if team in form:
            parts.append(form[team]["note"])
        if team in context:
            parts.append(context[team]["note"])
        out[team] = {
            "attack_mult": round(attack_mult, 4),
            "matches": max(form.get(team, {}).get("matches", 0), context.get(team, {}).get("matches", 0)),
            "form_mult": form_mult,
            "group_context_mult": context_mult,
            "note": f"综合微调{direction} {pct:.1f}%（总封顶 ±4%；" + "；".join(parts) + "）",
        }
    return out


def attack_mult(adjustments, team):
    return float((adjustments or {}).get(team, {}).get("attack_mult", 1.0))
