BASELINE_TOTAL_GOALS = 2.62
MIN_TEMPO_MULT = 0.92
MAX_TEMPO_MULT = 1.08
MAX_LIVE_TEMPO_MULT = 1.12
TEMPO_BLEND = 0.35
LIVE_TEMPO_BLEND_DENOM = 36


def _clip(value, lo, hi):
    return max(lo, min(hi, value))


def _goal_totals(rows):
    totals = []
    for row in rows or []:
        try:
            totals.append(float(row["home_goals"]) + float(row["away_goals"]))
        except (KeyError, TypeError, ValueError):
            continue
    return totals


def build_tempo_adjustment(history_rows, min_matches=40, baseline=BASELINE_TOTAL_GOALS):
    totals = _goal_totals(history_rows)
    if len(totals) < min_matches:
        return {
            "source": "neutral",
            "matches": len(totals),
            "avg_goals": None,
            "goal_mult": 1.0,
            "note": "赛事节奏中性：真实历史样本不足，未调整总进球。",
        }

    avg_goals = sum(totals) / len(totals)
    raw_ratio = avg_goals / baseline if baseline else 1.0
    goal_mult = _clip(1.0 + TEMPO_BLEND * (raw_ratio - 1.0), MIN_TEMPO_MULT, MAX_TEMPO_MULT)
    direction = "上调" if goal_mult >= 1.002 else "下调" if goal_mult <= 0.998 else "持平"
    return {
        "source": "real_history",
        "matches": len(totals),
        "avg_goals": round(avg_goals, 3),
        "goal_mult": round(goal_mult, 4),
        "note": f"赛事节奏{direction} {(abs(goal_mult - 1.0) * 100):.1f}%（真实世界杯历史均值 {avg_goals:.2f} 球，封顶 ±8%）。",
    }


def _played_goal_totals(played):
    totals = []
    seen = set()
    for pair, score in (played or {}).items():
        try:
            home, away = pair
            dedupe = tuple(sorted((home, away)))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            totals.append(float(score[0]) + float(score[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return totals


def build_live_tempo_adjustment(played, base_adjustment=None, min_matches=12, baseline=BASELINE_TOTAL_GOALS):
    base_adjustment = base_adjustment or build_tempo_adjustment([])
    base_mult = float(base_adjustment.get("goal_mult", 1.0))
    totals = _played_goal_totals(played)
    if len(totals) < min_matches:
        out = dict(base_adjustment)
        out["live_matches"] = len(totals)
        out["note"] = (
            base_adjustment.get("note", "")
            + f" 赛内节奏未启用：已完赛 {len(totals)} 场，低于 {min_matches} 场门槛。"
        ).strip()
        return out

    avg_goals = sum(totals) / len(totals)
    raw_ratio = avg_goals / baseline if baseline else 1.0
    shrink = len(totals) / (len(totals) + LIVE_TEMPO_BLEND_DENOM)
    live_mult = 1.0 + shrink * (raw_ratio - 1.0)
    combined = _clip(base_mult * live_mult, MIN_TEMPO_MULT, MAX_LIVE_TEMPO_MULT)
    direction = "上调" if combined >= 1.002 else "下调" if combined <= 0.998 else "持平"
    return {
        "source": "history_plus_live_tournament",
        "matches": len(totals),
        "history_goal_mult": round(base_mult, 4),
        "live_avg_goals": round(avg_goals, 3),
        "live_shrink": round(shrink, 3),
        "avg_goals": round(avg_goals, 3),
        "goal_mult": round(combined, 4),
        "note": (
            f"赛事节奏{direction} {(abs(combined - 1.0) * 100):.1f}%"
            f"（历史基准 x{base_mult:.3f}；赛内节奏 {len(totals)} 场均值 {avg_goals:.2f} 球，"
            f"按 shrink={shrink:.2f} 保守吸收，封顶 +12%）。"
        ),
    }
