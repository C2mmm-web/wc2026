BASELINE_TOTAL_GOALS = 2.62
MIN_TEMPO_MULT = 0.92
MAX_TEMPO_MULT = 1.08
TEMPO_BLEND = 0.35


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
