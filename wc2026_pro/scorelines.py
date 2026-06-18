def scoreline_summary(top):
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
    return {
        "top3": rows[:3],
        "mode_prob": round(mode_prob, 4),
        "mode_gap": round(mode_gap, 4),
        "concentration": concentration,
        "concentration_label": label,
    }
