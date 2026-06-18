import json
import os
import urllib.request


HERE = os.path.dirname(__file__)
ARCHIVE_FILENAME = "prediction_archive.json"
REPORT_FILENAME = "audit_report.json"


def _match_key(home, away):
    return f"{home}|{away}"


def _snapshot_id(generated_at, home, away):
    return f"{generated_at}|{home}|{away}"


def _round(value, digits=4):
    return round(float(value), digits)


def _score_entry(item):
    if isinstance(item, dict):
        return {
            "score": str(item["score"]),
            "prob": _round(item.get("prob", 0.0)),
        }
    if len(item) >= 3:
        return {"score": f"{int(item[0])}-{int(item[1])}", "prob": _round(item[2])}
    score, prob = item
    return {"score": f"{int(score[0])}-{int(score[1])}", "prob": _round(prob)}


def _top3(match):
    scoreline = match.get("scoreline") or {}
    items = scoreline.get("top3") or (match.get("judgment") or {}).get("top") or []
    return [_score_entry(item) for item in items[:3]]


def _result_code(home_goals, away_goals):
    return "H" if home_goals > away_goals else "D" if home_goals == away_goals else "A"


def _pred_result(record):
    probs = {
        "H": float(record.get("p_home", 0.0)),
        "D": float(record.get("p_draw", 0.0)),
        "A": float(record.get("p_away", 0.0)),
    }
    return max(probs, key=probs.get)


def _rate(hit, total):
    return round(hit / total, 3) if total else None


def prediction_snapshot(match, payload, commit_sha=None):
    generated_at = payload.get("generated_at") or payload.get("generated")
    judgment = match.get("judgment") or {}
    top3 = _top3(match)
    advanced = match.get("advanced") or {}
    totals = advanced.get("totals") or {}
    xg_home = _round(judgment.get("lh", 0.0), 2)
    xg_away = _round(judgment.get("la", 0.0), 2)
    xg_total = totals.get("xg_total")
    if xg_total is None:
        xg_total = xg_home + xg_away
    record = {
        "snapshot_id": _snapshot_id(generated_at, match["home"], match["away"]),
        "generated": payload.get("generated"),
        "generated_at": generated_at,
        "version": payload.get("version"),
        "commit": commit_sha,
        "group": match.get("group"),
        "md": match.get("md"),
        "home": match["home"],
        "away": match["away"],
        "p_home": _round(judgment.get("w", 0.0)),
        "p_draw": _round(judgment.get("d", 0.0)),
        "p_away": _round(judgment.get("l", 0.0)),
        "xg_home": xg_home,
        "xg_away": xg_away,
        "xg_total": _round(xg_total, 2),
        "pred_score": top3[0]["score"] if top3 else None,
        "top3": top3,
    }
    record["pred_result"] = _pred_result(record)
    return record


def _active_matchday(matches):
    matchdays = [
        int(match["md"])
        for match in matches
        if not match.get("played") and match.get("md") is not None
    ]
    return min(matchdays) if matchdays else None


def append_prediction_archive(existing, payload, commit_sha=None):
    archive = [dict(row) for row in (existing or []) if isinstance(row, dict)]
    seen = {
        row.get("snapshot_id")
        or _snapshot_id(row.get("generated_at") or row.get("generated"), row.get("home"), row.get("away"))
        for row in archive
    }
    matches = payload.get("matches", [])
    active_md = _active_matchday(matches)
    for match in matches:
        if match.get("played"):
            continue
        if active_md is not None and match.get("md") != active_md:
            continue
        record = prediction_snapshot(match, payload, commit_sha=commit_sha)
        if record["snapshot_id"] in seen:
            continue
        archive.append(record)
        seen.add(record["snapshot_id"])
    return archive


def build_audit_report(archive, payload):
    by_match = {}
    for record in archive or []:
        if not isinstance(record, dict):
            continue
        key = _match_key(record.get("home"), record.get("away"))
        by_match.setdefault(key, []).append(record)
    for records in by_match.values():
        records.sort(key=lambda row: row.get("generated_at") or row.get("generated") or "")

    rows = []
    missing = []
    finished = 0
    exact_top1_hits = 0
    exact_top3_hits = 0
    wdl_hits = 0

    for match in payload.get("matches", []):
        played = match.get("played")
        if not played:
            continue
        finished += 1
        home, away = match["home"], match["away"]
        actual_score = f"{int(played[0])}-{int(played[1])}"
        records = by_match.get(_match_key(home, away), [])
        if not records:
            missing.append({"home": home, "away": away, "actual_score": actual_score})
            continue

        record = records[0]
        top3_scores = [item.get("score") for item in record.get("top3", [])]
        pred_score = record.get("pred_score")
        actual_result = _result_code(int(played[0]), int(played[1]))
        pred_result = record.get("pred_result") or _pred_result(record)
        exact_top1 = pred_score == actual_score
        exact_top3 = actual_score in top3_scores
        wdl_hit = pred_result == actual_result
        exact_top1_hits += int(exact_top1)
        exact_top3_hits += int(exact_top3)
        wdl_hits += int(wdl_hit)
        rows.append({
            "home": home,
            "away": away,
            "prediction_generated_at": record.get("generated_at") or record.get("generated"),
            "version": record.get("version"),
            "commit": record.get("commit"),
            "pred_score": pred_score,
            "top3": record.get("top3", []),
            "actual_score": actual_score,
            "pred_result": pred_result,
            "actual_result": actual_result,
            "exact_top1": exact_top1,
            "exact_top3": exact_top3,
            "wdl_hit": wdl_hit,
            "p_home": record.get("p_home"),
            "p_draw": record.get("p_draw"),
            "p_away": record.get("p_away"),
            "xg_total": record.get("xg_total"),
        })

    audited = len(rows)
    return {
        "generated_at": payload.get("generated_at") or payload.get("generated"),
        "note": "Only archived pre-match predictions are scored; post-match rebuilds are excluded.",
        "finished_matches": finished,
        "audited_matches": audited,
        "missing_prematch": len(missing),
        "archive_records": len(archive or []),
        "exact_top1_hits": exact_top1_hits,
        "exact_top3_hits": exact_top3_hits,
        "wdl_hits": wdl_hits,
        "rates": {
            "exact_top1": _rate(exact_top1_hits, audited),
            "exact_top3": _rate(exact_top3_hits, audited),
            "wdl": _rate(wdl_hits, audited),
        },
        "rows": rows,
        "missing": missing,
    }


def _load_json(path):
    if not path or not os.path.exists(path):
        return []
    with open(path) as fp:
        data = json.load(fp)
    if isinstance(data, dict):
        return data.get("records", [])
    return data if isinstance(data, list) else []


def _pages_base_url():
    explicit = os.environ.get("PAGES_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}"


def _load_remote_archive(base_url):
    if not base_url:
        return []
    url = base_url.rstrip("/") + "/" + ARCHIVE_FILENAME
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, dict):
            return data.get("records", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_previous_archive(local_path=None, base_url=None):
    local = _load_json(local_path or os.path.join(HERE, ARCHIVE_FILENAME))
    if local:
        return local
    return _load_remote_archive(base_url or _pages_base_url())


def save_audit_artifacts(payload, here=HERE, previous_archive=None, commit_sha=None):
    if previous_archive is None:
        previous_archive = load_previous_archive(os.path.join(here, ARCHIVE_FILENAME))
    archive = append_prediction_archive(previous_archive, payload, commit_sha=commit_sha)
    report = build_audit_report(archive, payload)
    payload["audit"] = report
    with open(os.path.join(here, ARCHIVE_FILENAME), "w") as fp:
        json.dump(archive, fp, ensure_ascii=False, indent=1)
    with open(os.path.join(here, REPORT_FILENAME), "w") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)
    return archive, report
