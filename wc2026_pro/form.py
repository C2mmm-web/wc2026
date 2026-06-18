"""
wc2026_pro.form — conservative in-tournament form adjustments.

The goal is to let finished matches nudge later predictions without letting one
result overpower the long-run Elo / Dixon-Coles priors.
"""
from data import ELO_PRIOR

MIN_ATTACK_MULT = 0.97
MAX_ATTACK_MULT = 1.03
MIN_DEFENSE_MULT = 0.96
MAX_DEFENSE_MULT = 1.04
MIN_GROUP_CONTEXT_MULT = 0.982
MAX_GROUP_CONTEXT_MULT = 1.018
MIN_COMBINED_ATTACK_MULT = 0.96
MAX_COMBINED_ATTACK_MULT = 1.04
MIN_COMBINED_DEFENSE_MULT = 0.94
MAX_COMBINED_DEFENSE_MULT = 1.06


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


def _expected_goals_against(team, opp):
    diff = ELO_PRIOR[team] - ELO_PRIOR[opp]
    return _clip(1.31 - 0.0022 * diff, 0.35, 3.2)


def _defense_signal(team, opp, ga):
    expected_ga = _expected_goals_against(team, opp)
    return 0.012 * _clip(float(ga) - expected_ga, -3.0, 3.0)


def build_form_adjustments(played):
    raw = {team: {"signal": 0.0, "def_signal": 0.0, "matches": 0} for team in ELO_PRIOR}
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
        raw[home]["def_signal"] += _defense_signal(home, away, ag)
        raw[away]["def_signal"] += _defense_signal(away, home, hg)
        raw[home]["matches"] += 1
        raw[away]["matches"] += 1

    out = {}
    for team, row in raw.items():
        if row["matches"] <= 0:
            continue
        attack_mult = _clip(1.0 + row["signal"], MIN_ATTACK_MULT, MAX_ATTACK_MULT)
        defense_mult = _clip(1.0 + row["def_signal"], MIN_DEFENSE_MULT, MAX_DEFENSE_MULT)
        direction = "上调" if attack_mult >= 1.0005 else "下调" if attack_mult <= 0.9995 else "持平"
        defense_direction = "变脆弱" if defense_mult >= 1.0005 else "变稳固" if defense_mult <= 0.9995 else "持平"
        pct = abs(attack_mult - 1.0) * 100
        defense_pct = abs(defense_mult - 1.0) * 100
        out[team] = {
            "attack_mult": round(attack_mult, 4),
            "defense_mult": round(defense_mult, 4),
            "matches": row["matches"],
            "note": (
                f"状态微调{direction} {pct:.1f}%（进攻封顶 ±3%）；"
                f"防守{defense_direction} {defense_pct:.1f}%（对手 xG 乘数，封顶 ±4%）"
            ),
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
                "defense_mult": 1.0,
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
        form_defense_mult = float(form.get(team, {}).get("defense_mult", 1.0))
        context_defense_mult = float(context.get(team, {}).get("defense_mult", 1.0))
        attack_mult = _clip(form_mult * context_mult, MIN_COMBINED_ATTACK_MULT, MAX_COMBINED_ATTACK_MULT)
        defense_mult = _clip(
            form_defense_mult * context_defense_mult,
            MIN_COMBINED_DEFENSE_MULT,
            MAX_COMBINED_DEFENSE_MULT,
        )
        direction = "上调" if attack_mult >= 1.0005 else "下调" if attack_mult <= 0.9995 else "持平"
        defense_direction = "变脆弱" if defense_mult >= 1.0005 else "变稳固" if defense_mult <= 0.9995 else "持平"
        pct = abs(attack_mult - 1.0) * 100
        defense_pct = abs(defense_mult - 1.0) * 100
        parts = []
        if team in form:
            parts.append(form[team]["note"])
        if team in context:
            parts.append(context[team]["note"])
        out[team] = {
            "attack_mult": round(attack_mult, 4),
            "defense_mult": round(defense_mult, 4),
            "matches": max(form.get(team, {}).get("matches", 0), context.get(team, {}).get("matches", 0)),
            "form_mult": form_mult,
            "group_context_mult": context_mult,
            "form_defense_mult": form_defense_mult,
            "group_context_defense_mult": context_defense_mult,
            "note": (
                f"综合微调{direction} {pct:.1f}% / 防守{defense_direction} {defense_pct:.1f}%"
                "（进攻总封顶 ±4%，防守总封顶 ±6%；" + "；".join(parts) + "）"
            ),
        }
    return out


def attack_mult(adjustments, team):
    return float((adjustments or {}).get(team, {}).get("attack_mult", 1.0))


def defense_mult(adjustments, team):
    return float((adjustments or {}).get(team, {}).get("defense_mult", 1.0))


def apply_played_elo_updates(elo, played, k=18, max_shift=28):
    """Apply finished tournament results to the fitted Elo object, with a hard cap."""
    if not played:
        return {
            "matches": 0,
            "teams": 0,
            "max_abs_shift": 0.0,
            "avg_abs_shift": 0.0,
            "note": "赛内 Elo 未调整：暂无已完赛样本。",
        }

    start = dict(elo.r)
    old_k = getattr(elo, "k", k)
    seen = set()
    matches = 0
    elo.k = k
    try:
        for (home, away), score in sorted(played.items()):
            if home not in elo.r or away not in elo.r:
                continue
            dedupe = tuple(sorted((home, away)))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            hg, ag = int(score[0]), int(score[1])
            elo.update(home, away, hg, ag, neutral=True, weight=1.0)
            matches += 1
    finally:
        elo.k = old_k

    shifts = []
    for team, base_rating in start.items():
        raw_shift = elo.r.get(team, base_rating) - base_rating
        capped_shift = _clip(raw_shift, -max_shift, max_shift)
        elo.r[team] = base_rating + capped_shift
        if abs(capped_shift) > 1e-9:
            shifts.append(abs(capped_shift))

    max_abs = max(shifts) if shifts else 0.0
    avg_abs = sum(shifts) / len(shifts) if shifts else 0.0
    return {
        "matches": matches,
        "teams": len(shifts),
        "k": k,
        "max_shift_cap": max_shift,
        "max_abs_shift": round(max_abs, 2),
        "avg_abs_shift": round(avg_abs, 2),
        "note": (
            f"赛内 Elo 已根据 {matches} 场已完赛更新"
            f"（K={k}，单队总位移封顶 ±{max_shift}），用于后续比赛预测。"
        ),
    }
