def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _as_float_grid(grid):
    return [[float(cell) for cell in row] for row in grid]


def _result_bucket(home_goals, away_goals):
    if home_goals > away_goals:
        return "H"
    if home_goals == away_goals:
        return "D"
    return "A"


def _shape_weight(home_goals, away_goals, lh, la):
    total = home_goals + away_goals
    margin = abs(home_goals - away_goals)
    total_xg = lh + la
    diff = lh - la
    pace = _clamp((total_xg - 2.15) / 0.9, 0.0, 1.0)
    balance = _clamp(1.0 - abs(diff) / 0.75, 0.0, 1.0)
    favorite_strength = _clamp(abs(diff) / 1.0, 0.0, 1.0)
    home_favorite = diff >= 0
    favorite_wins = (home_favorite and home_goals > away_goals) or (
        (not home_favorite) and away_goals > home_goals
    )

    weight = 1.0
    if total == 0:
        weight *= 1.0 - 0.08 * pace
    elif total == 1:
        weight *= 1.0 - 0.32 * pace
    elif total == 2:
        weight *= 1.0 + 0.12 * pace
    elif total == 3:
        weight *= 1.0 + 0.10 * pace
    else:
        weight *= 1.0 + 0.12 * pace

    if balance:
        if home_goals == away_goals and total == 2:
            weight *= 1.0 + 0.22 * balance
        elif home_goals == away_goals and total == 0:
            weight *= 1.0 + 0.04 * balance
        elif margin == 1 and total == 1:
            weight *= 1.0 - 0.10 * balance

    if favorite_strength and favorite_wins:
        if margin == 1 and total == 1:
            weight *= 1.0 - 0.28 * favorite_strength
        elif margin >= 2 and 2 <= total <= 4:
            weight *= 1.0 + 0.22 * favorite_strength
        elif margin == 1 and total >= 3:
            weight *= 1.0 + 0.08 * favorite_strength

    return max(0.05, weight)


def calibrate_scoreline_grid(grid, lh, la, home_win, draw, away_win):
    raw = _as_float_grid(grid)
    targets = {"H": float(home_win), "D": float(draw), "A": float(away_win)}
    weighted = []
    bucket_sums = {"H": 0.0, "D": 0.0, "A": 0.0}
    for i, row in enumerate(raw):
        out_row = []
        for j, prob in enumerate(row):
            value = prob * _shape_weight(i, j, lh, la)
            out_row.append(value)
            bucket_sums[_result_bucket(i, j)] += value
        weighted.append(out_row)

    calibrated = []
    for i, row in enumerate(weighted):
        out_row = []
        for j, value in enumerate(row):
            bucket = _result_bucket(i, j)
            scale = targets[bucket] / bucket_sums[bucket] if bucket_sums[bucket] else 0.0
            out_row.append(value * scale)
        calibrated.append(out_row)

    total = sum(sum(row) for row in calibrated)
    if total:
        calibrated = [[value / total for value in row] for row in calibrated]

    return calibrated, {
        "model": "tempo_overdispersion",
        "xg_total": round(float(lh + la), 3),
        "xg_diff": round(float(lh - la), 3),
        "preserves": "1x2_result_probabilities",
    }


def exact_scoreline_grid(raw_grid, calibrated_grid, raw_weight=1.0):
    raw = _as_float_grid(raw_grid)
    calibrated = _as_float_grid(calibrated_grid)
    weight = _clamp(float(raw_weight), 0.0, 1.0)
    out = []
    for i, row in enumerate(raw):
        out_row = []
        for j, raw_value in enumerate(row):
            calibrated_value = calibrated[i][j] if i < len(calibrated) and j < len(calibrated[i]) else 0.0
            out_row.append(weight * raw_value + (1.0 - weight) * calibrated_value)
        out.append(out_row)
    total = sum(sum(row) for row in out)
    if total:
        out = [[value / total for value in row] for row in out]
    return out, {
        "model": "goal_shape_exact_score",
        "raw_weight": round(weight, 3),
        "preserves": "goal_distribution_shape" if weight >= 0.999 else "blended_goal_shape_and_1x2",
    }


def top_scorelines(grid, limit=4, max_goals=6):
    rows = []
    upper = min(max_goals, len(grid))
    for i in range(upper):
        row = grid[i]
        for j in range(min(max_goals, len(row))):
            rows.append(((i, j), float(row[j])))
    rows.sort(key=lambda item: -item[1])
    return rows[:limit]


def scoreline_summary(top, model_info=None):
    rows = []
    for item in top[:4]:
        if len(item) == 2:
            (hg, ag), prob = item
        else:
            hg, ag, prob = item
        rows.append({
            "home_goals": int(hg),
            "away_goals": int(ag),
            "score": f"{int(hg)}-{int(ag)}",
            "prob": round(float(prob), 4),
        })
    rows.sort(key=lambda item: item["prob"], reverse=True)
    mode_prob = rows[0]["prob"] if rows else 0.0
    mode_gap = mode_prob - (rows[1]["prob"] if len(rows) > 1 else 0.0)
    if mode_prob >= 0.22 and mode_gap >= 0.06:
        concentration, label = "high", "高集中度"
    elif mode_prob >= 0.16 or mode_gap >= 0.035:
        concentration, label = "medium", "中集中度"
    else:
        concentration, label = "low", "低集中度"
    single_pick = mode_prob >= 0.16 and mode_gap >= 0.035
    out = {
        "top3": rows[:3],
        "mode_prob": round(mode_prob, 4),
        "mode_gap": round(mode_gap, 4),
        "concentration": concentration,
        "concentration_label": label,
        "single_pick": single_pick,
        "primary_label": rows[0]["score"] if single_pick and rows else "无单一精确比分优势",
    }
    if model_info:
        out["model"] = dict(model_info)
    return out
