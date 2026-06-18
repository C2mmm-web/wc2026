"""
wc2026_pro.main — P8 orchestration & single source of truth.
Runs the whole pipeline, emits predictions.json / ratings.json / backtest_report.json /
prediction_log.csv / prediction_archive.json, and rebuilds the webpage embedding the generated predictions.
"""
import json, os, math, csv, datetime
import numpy as np
from data import (GROUPS, TEAMS, ELO_PRIOR, HOSTS, MARKET_TITLE, MARKET_MATCH,
                  PLAYED, INJURIES, MODEL_VERSION, LIVE_RESULT_KEYS,
                  FRESH_RESULT_KEYS, HISTORICAL_RESULTS, UPDATE_STATUS)
from engine import (elo_lambdas, context_mult, availability_mult, summarize,
                    american_to_prob, devig, _tau, _pois, _goal_exp)
from backtest import run_backtest
from tournament import Predictor, simulate
from form import attack_mult, build_combined_adjustments
from calibration import apply_temperature
from scorelines import calibrate_scoreline_grid, scoreline_summary, top_scorelines
from audit import save_audit_artifacts
from tempo import build_tempo_adjustment

HERE = os.path.dirname(__file__)
TEMPLATE_MD = {1: [(0,1),(2,3)], 2: [(0,2),(3,1)], 3: [(3,0),(1,2)]}

def fixtures():
    out = []
    for g, t in GROUPS.items():
        for md, pairs in TEMPLATE_MD.items():
            for i, j in pairs:
                out.append({"group": g, "md": md, "home": t[i], "away": t[j]})
    return out

def _result_key(home, away):
    return f"{home}|{away}"

def _points_for(team, row):
    if row["home"] == team:
        gf, ga = int(row["home_goals"]), int(row["away_goals"])
    elif row["away"] == team:
        gf, ga = int(row["away_goals"]), int(row["home_goals"])
    else:
        return None
    return 3 if gf > ga else 1 if gf == ga else 0

def recent_ppg(team, historical_rows, limit=8):
    rows = [r for r in historical_rows or [] if r.get("home") == team or r.get("away") == team]
    rows.sort(key=lambda r: r.get("date") or "")
    rows = rows[-limit:]
    if not rows:
        return None
    pts = [_points_for(team, r) for r in rows]
    pts = [p for p in pts if p is not None]
    return round(sum(pts) / len(pts), 2) if pts else None

def match_metadata(home, away, played, update_status, fresh_keys, historical_rows, form_adjustments=None, tempo_adjustment=None):
    key = _result_key(home, away)
    fresh = (home, away) in fresh_keys or key in fresh_keys
    api_played = (home, away) in LIVE_RESULT_KEYS or key in LIVE_RESULT_KEYS or fresh
    home_ppg = recent_ppg(home, historical_rows)
    away_ppg = recent_ppg(away, historical_rows)
    signals = [f"Elo差 {ELO_PRIOR[home] - ELO_PRIOR[away]:+d}"]
    if home in HOSTS and away not in HOSTS:
        signals.append(f"{home}东道主")
    if home_ppg is not None and away_ppg is not None:
        signals.append(f"最近战绩 {home} {home_ppg} PPG / {away} {away_ppg} PPG")
    elif historical_rows:
        signals.append(f"历史样本 {len(historical_rows)} 场")
    if (home, away) in MARKET_MATCH:
        signals.append("含公开盘口校准")
    for team in (home, away):
        note = (form_adjustments or {}).get(team, {}).get("note")
        if note:
            signals.append(f"{team} {note}")
    tempo_note = (tempo_adjustment or {}).get("note")
    if tempo_note:
        signals.append(tempo_note)
    if played and api_played:
        current_status = (update_status or {}).get("current_results", {}).get("status")
        source = "免费比分源" if current_status == "fallback_success" else "API-Football"
        signals.append(f"比分来自 {source}")
    return {
        "fresh": bool(fresh),
        "played_source": "api" if api_played else ("manual" if played else None),
        "public_signals": signals,
        "update_status": update_status,
    }

def market_fit(odds):
    pw, pd, pl = devig(*odds)
    best = None
    for a in range(10, 360, 6):
        for b in range(10, 360, 6):
            lh, la = a/100, b/100
            w=d=l=0.0
            for i in range(8):
                for j in range(8):
                    pr=_pois(i,lh)*_pois(j,la)
                    if i>j:w+=pr
                    elif i==j:d+=pr
                    else:l+=pr
            e=(w-pw)**2+(l-pl)**2
            if best is None or e<best[0]: best=(e,lh,la)
    return best[1], best[2]

_rng = np.random.default_rng(7)
_KMAX = 8
def _pvec(l):
    out = np.empty(_KMAX); p = math.exp(-l)
    for k in range(_KMAX): out[k] = p; p *= l/(k+1)
    return out
def _wdl(lh, la):
    M = np.outer(_pvec(lh), _pvec(la)); M /= M.sum()
    return float(np.tril(M,-1).sum()), float(np.trace(M)), float(np.triu(M,1).sum())

def _calibrate_summary_wdl(summary, temperature):
    if not temperature or abs(float(temperature) - 1.0) < 1e-9:
        return summary
    out = dict(summary)
    out["w"], out["d"], out["l"] = apply_temperature(
        [summary["w"], summary["d"], summary["l"]],
        temperature,
    )
    return out

def _calibrate_summary_scorelines(summary, rho):
    from engine import grid as _grid
    raw_grid = _grid(summary["lh"], summary["la"], rho, 8)
    calibrated_grid, scoreline_model = calibrate_scoreline_grid(
        raw_grid,
        summary["lh"],
        summary["la"],
        summary["w"],
        summary["d"],
        summary["l"],
    )
    out = dict(summary)
    out["top"] = top_scorelines(calibrated_grid)
    out["scoreline_model"] = scoreline_model
    grid6 = [[round(float(calibrated_grid[i][j]), 4) for j in range(6)] for i in range(6)]
    return out, grid6

def _result_code(hg, ag):
    return "H" if hg > ag else "D" if hg == ag else "A"

def _result_label(code, home, away):
    return {"H": home, "D": "Draw", "A": away}[code]

def _handicap_line(lh, la):
    diff = lh - la
    if diff >= 1.25:
        return -2
    if diff >= 0.55:
        return -1
    if diff <= -1.25:
        return 2
    if diff <= -0.55:
        return 1
    return 0

def advanced_markets(lh, la, rho, home, away):
    from engine import grid as _grid
    ft = _grid(lh, la, rho, 11)
    total_probs = {"0-1": 0.0, "2-3": 0.0, "4+": 0.0}
    ou25_over = 0.0
    hcap = _handicap_line(lh, la)
    hcap_probs = {"H": 0.0, "D": 0.0, "A": 0.0}
    for hg in range(ft.shape[0]):
        for ag in range(ft.shape[1]):
            prob = float(ft[hg][ag])
            total = hg + ag
            if total <= 1:
                total_probs["0-1"] += prob
            elif total <= 3:
                total_probs["2-3"] += prob
            else:
                total_probs["4+"] += prob
            if total > 2.5:
                ou25_over += prob
            adj_home = hg + hcap
            hcap_probs[_result_code(adj_home, ag)] += prob

    h1 = _grid(lh * 0.44, la * 0.44, rho, 7)
    h2 = _grid(lh * 0.56, la * 0.56, rho, 7)
    htft = {ht: {ftc: 0.0 for ftc in ("H", "D", "A")} for ht in ("H", "D", "A")}
    for hh in range(h1.shape[0]):
        for ha in range(h1.shape[1]):
            hprob = float(h1[hh][ha])
            ht_code = _result_code(hh, ha)
            for sh in range(h2.shape[0]):
                for sa in range(h2.shape[1]):
                    prob = hprob * float(h2[sh][sa])
                    ft_code = _result_code(hh + sh, ha + sa)
                    htft[ht_code][ft_code] += prob

    rows = []
    for ht in ("H", "D", "A"):
        rows.append({
            "ht": ht,
            "label": _result_label(ht, home, away),
            "cells": {
                ftc: {"label": _result_label(ftc, home, away), "prob": round(htft[ht][ftc], 4)}
                for ftc in ("H", "D", "A")
            },
        })
    return {
        "totals": {
            "xg_total": round(lh + la, 2),
            "buckets": [
                {"label": "TG 0-1", "prob": round(total_probs["0-1"], 4)},
                {"label": "TG 2-3", "prob": round(total_probs["2-3"], 4)},
                {"label": "TG 4+", "prob": round(total_probs["4+"], 4)},
            ],
            "over_under": [
                {"label": "Over 2.5", "line": 2.5, "prob": round(ou25_over, 4)},
                {"label": "Under 2.5", "line": 2.5, "prob": round(1 - ou25_over, 4)},
            ],
        },
        "handicap": {
            "type": "3-way",
            "home_hcap": hcap,
            "label": f"{home} {hcap:+d}",
            "outcomes": [
                {"code": "H", "label": "Hcap H", "prob": round(hcap_probs["H"], 4)},
                {"code": "D", "label": "Hcap D", "prob": round(hcap_probs["D"], 4)},
                {"code": "A", "label": "Hcap A", "prob": round(hcap_probs["A"], 4)},
            ],
        },
        "htft": {"rows": rows},
    }

def predict_match(elo, dc, w, home, away, B=300, form_adjustments=None, wdl_temperature=1.0, tempo_mult=1.0):
    host = (home in HOSTS) and (away not in HOSTS)
    neutral = not host
    le_h, le_a = elo_lambdas(elo.r, home, away, neutral=neutral)
    ld_h, ld_a = dc.lambdas(home, away, neutral=neutral)
    lh = w*ld_h + (1-w)*le_h; la = w*ld_a + (1-w)*le_a
    lh = min(8.0, max(0.05, lh)); la = min(8.0, max(0.05, la))
    src = "model"; mkt = None; mh = ma = None
    if (home, away) in MARKET_MATCH:
        mh, ma = market_fit(MARKET_MATCH[(home, away)])
        mkt = [round(x,4) for x in devig(*MARKET_MATCH[(home, away)])]
        lh = 0.5*lh + 0.5*mh; la = 0.5*la + 0.5*ma; src = "market"
    tempo_mult = float(tempo_mult or 1.0)
    lh *= tempo_mult
    la *= tempo_mult
    fm_h = attack_mult(form_adjustments, home)
    fm_a = attack_mult(form_adjustments, away)
    ch, ca = context_mult(home, away)
    av_h, av_a, note = availability_mult(home, away)
    base = _calibrate_summary_wdl(summarize(lh*fm_h*ch, la*fm_a*ca, dc.rho), wdl_temperature)
    judg = _calibrate_summary_wdl(summarize(lh*fm_h*ch*av_h, la*fm_a*ca*av_a, dc.rho), wdl_temperature)
    base, _base_grid6 = _calibrate_summary_scorelines(base, dc.rho)
    judg, grid6 = _calibrate_summary_scorelines(judg, dc.rho)

    # P5 -> per-match confidence interval via parameter-uncertainty bootstrap
    ih, ia = dc.idx[home], dc.idx[away]; g = 0.0 if neutral else dc.gamma
    ws = np.empty(B); ds = np.empty(B); ls = np.empty(B)
    for b in range(B):
        eh = elo.r[home]+_rng.normal(0,28); ea = elo.r[away]+_rng.normal(0,28)
        leh, lea = elo_lambdas({home:eh, away:ea}, home, away, neutral=neutral)
        ah = dc.att[ih]+_rng.normal(0,0.05); aa = dc.att[ia]+_rng.normal(0,0.05)
        dh = dc.dff[ih]+_rng.normal(0,0.05); da = dc.dff[ia]+_rng.normal(0,0.05)
        ldh = _goal_exp(ah-da+g); lda = _goal_exp(aa-dh)
        bh = w*ldh+(1-w)*leh; bla = w*lda+(1-w)*lea
        if mkt: bh = 0.5*bh+0.5*mh; bla = 0.5*bla+0.5*ma
        bh *= tempo_mult
        bla *= tempo_mult
        bh = min(8.0, max(0.05, bh)); bla = min(8.0, max(0.05, bla))
        wb, db, lb = _wdl(bh*fm_h*ch*av_h, bla*fm_a*ca*av_a)
        ws[b], ds[b], ls[b] = apply_temperature([wb, db, lb], wdl_temperature)
    ci = {"w":[round(float(np.percentile(ws,5)),3), round(float(np.percentile(ws,95)),3)],
          "d":[round(float(np.percentile(ds,5)),3), round(float(np.percentile(ds,95)),3)],
          "l":[round(float(np.percentile(ls,5)),3), round(float(np.percentile(ls,95)),3)]}
    adv = advanced_markets(lh*fm_h*ch*av_h, la*fm_a*ca*av_a, dc.rho, home, away)
    return base, judg, src, note, mkt, ci, grid6, adv

LEVELS = [(220,"实力差距悬殊"),(120,"明显优势"),(45,"略占上风"),(0,"几乎五五开")]
def analysis_zh(home, away, judg, ci, mkt, note, host):
    eh, ea = ELO_PRIOR[home], ELO_PRIOR[away]
    gap = abs(eh-ea); lead = home if eh>=ea else away
    level = next(t for th,t in LEVELS if gap>=th)
    fav, fp = (home, judg["w"]) if judg["w"]>=judg["l"] else (away, judg["l"])
    lo, hi = (ci["w"] if fav==home else ci["l"])
    width = hi-lo
    conf = "把握较高" if (fp>0.58 and width<0.20) else ("把握中等" if fp>0.45 else "胜负难料")
    tg = judg["lh"]+judg["la"]
    s = f"Elo 上{lead}领先 {gap} 分，属于「{level}」。"
    s += f"模型给出{fav}胜 {fp*100:.0f}%（90% 区间 {lo*100:.0f}–{hi*100:.0f}%），{conf}。"
    s += f"平局概率 {judg['d']*100:.0f}%，" + ("两队接近、需防冷平。" if judg["d"]>0.27 else "分出胜负的可能较大。")
    s += "预计" + ("场面开放、进球较多。" if tg>2.9 else "比赛偏闷、机会不多。" if tg<2.3 else "进球数中等。")
    s += f"下半场更可能出球（破门概率 {judg['h2g']*100:.0f}% vs 上半场 {judg['h1g']*100:.0f}%），"
    s += f"{fav}更可能在下半场拉开而非开局速胜。"
    sl = scoreline_summary(judg["top"], judg.get("scoreline_model"))
    if sl["concentration"] == "low":
        top3 = " / ".join(f"{item['score']} {item['prob']*100:.0f}%" for item in sl["top3"])
        s += f"精确比分属于低集中度分布，Top 3（{top3}）比单一比分更有参考价值。"
    if host: s += f"{home}坐拥东道主之利。"
    if mkt:
        mfp = mkt[0] if fav==home else mkt[2]
        diff = fp - mfp
        if abs(diff) >= 0.05:
            s += f"与盘口相比，模型对{fav}的信心{'更高' if diff>0 else '更低'}"
            s += f"（盘口 {mfp*100:.0f}% vs 模型 {fp*100:.0f}%），此处存在分歧。"
        else:
            s += "模型判断与博彩盘口基本一致。"
    if note: s += " " + note
    return s

def title_blend(model_counts, n):
    imp = {t: american_to_prob(o) for t, o in MARKET_TITLE.items()}
    s = sum(imp.values()); mk = {t: v/s*0.62 for t, v in imp.items()}
    mp = {t: model_counts[t]/n for t in TEAMS}
    bl = {t: (0.5*mp[t] + 0.5*mk[t]) if t in mk else mp[t] for t in TEAMS}
    z = sum(bl.values()); bl = {t: v/z for t, v in bl.items()}
    return mp, bl

SITE_TEMPLATE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>2026 世界杯预测 · 专业版</title><style>
:root{--bg:#0b1020;--card:#141b30;--c2:#1b2440;--line:#27314f;--txt:#eef2fb;--mut:#9aa6c4;--acc:#5b8cff;--acc2:#22d3a6;--gold:#f5c451;--pur:#cf9bff;--red:#ff7a90}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#16203c,#0b1020 60%);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:30px 18px 80px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.3px}.sub{color:var(--mut);font-size:13px;margin-bottom:14px;line-height:1.6}
.status{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px}.status span{background:#0e1530;border:1px solid var(--line);border-radius:8px;padding:6px 9px;color:#cdd7ee;font-size:12px}.status b{color:var(--acc2)}
.bar2{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.chip{background:var(--c2);border:1px solid var(--line);color:var(--mut);border-radius:20px;padding:6px 13px;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}.chip.gp.on{background:var(--acc2);color:#06121f;border-color:var(--acc2)}
.lab{font-size:12px;color:var(--mut);margin-right:2px}
/* match list rows */
.list{display:flex;flex-direction:column;gap:8px}
.mrow{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 15px;cursor:pointer;transition:.12s}
.mrow.fresh{border-color:#2bbf9d;box-shadow:0 0 0 1px rgba(34,211,166,.18) inset;background:#132335}
.mrow:hover{border-color:var(--acc);background:#172040}
.tag{font-size:10px;color:var(--mut);background:#0e1530;border:1px solid var(--line);border-radius:6px;padding:2px 7px;white-space:nowrap}
.mteams{flex:1;font-size:15px}.mteams .fav{font-weight:700}.vs{color:var(--mut);font-weight:400;margin:0 6px;font-size:13px}
.mscore{font-weight:700;color:var(--mut);font-variant-numeric:tabular-nums}
.pill{font-size:11px;padding:2px 9px;border-radius:20px;white-space:nowrap}
.pill.mkt{background:#2a2412;color:var(--gold)}.pill.mod{background:#14203a;color:var(--acc)}.pill.fin{background:#13322a;color:var(--acc2)}.pill.new{background:#113a32;color:var(--acc2);border:1px solid rgba(34,211,166,.35)}
.jdot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--pur);margin-left:6px;vertical-align:middle}
.headline{font-size:12px;color:var(--mut);min-width:96px;text-align:right}
/* secondary panels */
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-top:18px;padding:0 16px}
summary{cursor:pointer;padding:14px 0;font-size:15px;font-weight:600;list-style:none}summary::-webkit-details-marker{display:none}
summary::before{content:"▸ ";color:var(--mut)}details[open] summary::before{content:"▾ "}
.tbl{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px}.tbl th,.tbl td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line)}
.tbl th{color:var(--mut);font-weight:500;font-size:11px}.tbl td.n{text-align:right;font-variant-numeric:tabular-nums}.best{color:var(--acc2);font-weight:700}
.tbar{height:8px;background:#0e1530;border-radius:5px;overflow:hidden;width:90px;display:inline-block;vertical-align:middle}.tbar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2))}
small{color:var(--mut);font-size:12px;line-height:1.6;display:block}
.metric{display:inline-block;background:#0e1530;border:1px solid var(--line);border-radius:9px;padding:7px 11px;margin:3px 6px 3px 0;font-size:12px}.metric b{color:var(--acc2)}
.auditnote{background:#101a31;border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin:6px 0 12px;font-size:12px;line-height:1.65;color:#cbd6ef}
.auditpill{display:inline-block;border-radius:999px;padding:2px 7px;font-size:11px;border:1px solid var(--line);white-space:nowrap}.auditpill.ok{background:#12362d;color:var(--acc2);border-color:#236b5b}.auditpill.bad{background:#351a24;color:var(--red);border-color:#67404a}.auditpill.wait{background:#241f33;color:var(--pur);border-color:#463c63}
.auditmut{color:var(--mut);font-size:11px;line-height:1.5}.nowrap{white-space:nowrap}
@media(max-width:520px){.mrow{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center}.mteams{grid-column:2/-1;min-width:0;overflow-wrap:anywhere}.headline{grid-column:1/3;min-width:0;text-align:left}.mscore{grid-column:1/2}.pill{justify-self:end}}
/* modal report */
.ov{position:fixed;inset:0;background:rgba(5,9,20,.82);backdrop-filter:blur(4px);display:none;align-items:flex-start;justify-content:center;padding:24px 12px;overflow:auto;z-index:50}.ov.show{display:flex}
.rep{background:#121a30;border:1px solid var(--line);border-radius:18px;max-width:560px;width:100%;padding:24px;position:relative}
.x{position:absolute;top:14px;right:17px;font-size:24px;color:var(--mut);cursor:pointer;line-height:1}.x:hover{color:#fff}
.rh{font-size:22px;font-weight:800;letter-spacing:-.3px}.rs{font-size:12px;color:var(--mut);margin:3px 0 16px}
.fin{text-align:center;background:#0e1530;border:1px solid var(--line);border-radius:12px;padding:10px;margin-bottom:16px;color:var(--acc2)}.fin b{font-size:26px;color:#fff;display:block}
.call{display:flex;align-items:center;justify-content:space-between;background:linear-gradient(120deg,#16223f,#11192f);border:1px solid #2b3a63;border-radius:14px;padding:16px 18px;margin-bottom:18px}
.call .sc{font-size:30px;font-weight:800;letter-spacing:1px}.call .cf{font-size:12px;color:var(--mut);text-align:right}.call .cf b{display:block;font-size:15px;color:var(--gold)}
.call.scorecall{align-items:flex-start;gap:14px}.scorechips{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.scorechip{background:#0e1530;border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:82px}.scorechip b{display:block;color:#fff;font-size:18px;line-height:1}.scorechip span{display:block;color:var(--mut);font-size:11px;margin-top:4px;font-variant-numeric:tabular-nums}.scorehint{font-size:11px;color:var(--mut);margin-top:8px;line-height:1.45;max-width:320px}
.seclab{font-size:11px;color:var(--acc);letter-spacing:1px;margin:18px 0 10px;font-weight:700}
.wrow{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:13px}
.wname{width:96px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.wtrack{flex:1;height:18px;background:#0e1530;border-radius:6px;position:relative;overflow:hidden}
.wfill{height:100%;border-radius:6px}.wci{position:absolute;top:0;height:100%;background:rgba(255,255,255,.16);border-left:1px solid rgba(255,255,255,.5);border-right:1px solid rgba(255,255,255,.5)}
.wval{width:88px;text-align:right;font-variant-numeric:tabular-nums;color:var(--mut)}.wval b{color:#fff}
.cmp{font-size:12px;color:var(--mut);background:#0e1530;border:1px solid var(--line);border-radius:10px;padding:10px 13px;margin-top:10px;line-height:1.6}
/* heatmap */
.heat{display:grid;grid-template-columns:auto repeat(6,1fr);gap:3px;font-size:11px;margin-top:4px}
.heat .hc{aspect-ratio:1;border-radius:5px;display:flex;align-items:center;justify-content:center;color:#dfe7fb;font-variant-numeric:tabular-nums}
.heat .ax{color:var(--mut);display:flex;align-items:center;justify-content:center}
.heat .top{outline:2px solid var(--gold);font-weight:700;color:#fff}
.legend{font-size:11px;color:var(--mut);margin-top:8px}
.hb{background:#0e1530;border:1px solid var(--line);border-radius:12px;padding:13px 15px}.hr{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;color:#cdd7ee}
.flow{display:flex;height:9px;border-radius:5px;overflow:hidden;margin:7px 0 11px}
.note{background:#160f24;border:1px solid #3a2a52;border-radius:12px;padding:14px 16px;font-size:13.5px;line-height:1.75;color:#e2d8f4}
.kv{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mut);margin-top:6px}.kv b{color:#fff}
.adv{display:grid;gap:9px}.advrow{display:grid;grid-template-columns:92px 1fr 54px;align-items:center;gap:9px;font-size:12px;color:#d8e1f4}
.advbar{height:18px;background:#0e1530;border:1px solid var(--line);border-radius:5px;overflow:hidden}.advbar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2))}
.advval{text-align:right;font-variant-numeric:tabular-nums;color:#fff}.hcapline{font-size:12px;color:var(--mut);margin-bottom:8px}.hcapline b{color:var(--gold)}
.htft{display:grid;grid-template-columns:64px repeat(3,1fr);gap:4px;font-size:11px}.htft span{min-height:34px;border-radius:6px;background:#0e1530;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;text-align:center;padding:4px;color:#dfe7fb;font-variant-numeric:tabular-nums}.htft .hd{color:var(--mut);background:transparent;border-color:transparent}.htft .hot{outline:2px solid var(--gold);font-weight:700;color:#fff}
</style></head><body><div class="wrap">
<h1>⚽ 2026 世界杯预测 <span style="font-size:12px;color:var(--pur);font-weight:600">PRO</span></h1>
<div class="sub" id="sub"></div>
<div class="status" id="status"></div>

<div class="bar2" id="mdf"></div><div class="bar2" id="gpf"></div>
<div class="list" id="list"></div>

<details id="titlebox"><summary>🏆 夺冠概率（前 16）</summary><div id="title"></div></details>
<details id="auditbox"><summary>📊 自查与命中率</summary><div id="audit"></div></details>
<details id="cardbox"><summary>🧪 模型说明与回测验证</summary><div id="modelcard"></div></details>

</div><div class="ov" id="ov"><div class="rep" id="rep"></div></div>
<script>const PRED=/*DATA*/;
const GROUPS={};PRED.matches.forEach(m=>{(GROUPS[m.group]=GROUPS[m.group]||1)});const gkeys=Object.keys(GROUPS).sort();
const pct=x=>(x*100).toFixed(0);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ST=PRED.update_status||{},CR=ST.current_results||{},HS=ST.history||{},FB=CR.fallback_source||{};
const checked=ST.checked_at?ST.checked_at.replace("T"," ").replace("Z"," UTC"):"未运行云端拉取";
const dataLabel=CR.status=="fallback_success"?"免费比分源":"API 比分";
const apiReason=CR.errors&&CR.status!="fallback_success"?` · ${esc(JSON.stringify(CR.errors)).slice(0,90)}`:"";
const sourceNote=CR.status=="fallback_success"&&FB.source?` · ${esc(FB.source)}`:"";
document.getElementById("sub").innerHTML=`Dixon-Coles + Elo 集成模型 · 版本 v${PRED.version} · 数据日期 ${PRED.generated}<br>点击任意一场，查看比分概率、置信区间、公开信号和文字解读。`;
document.getElementById("status").innerHTML=
 `<span>最后云端更新 <b>${checked}</b></span><span>${dataLabel} <b>${CR.status||"not_run"}</b> · 完赛 ${CR.finished||0} · 待赛 ${CR.upcoming||0}${sourceNote}${apiReason}</span><span>历史拟合 <b>${HS.status||"not_run"}</b> · 样本 ${HS.matches||0}</span>`;

/* ---- filters + list ---- */
let curMD=1,curGP="all";
function chips(){const md=document.getElementById("mdf");
 md.innerHTML='<span class="lab">轮次</span>'+[1,2,3].map(n=>`<span class="chip md ${n==curMD?'on':''}" data-md="${n}">第 ${n} 轮</span>`).join("");
 const gp=document.getElementById("gpf");
 gp.innerHTML='<span class="lab">小组</span><span class="chip gp '+(curGP=='all'?'on':'')+'" data-gp="all">全部</span>'+gkeys.map(g=>`<span class="chip gp ${curGP==g?'on':''}" data-gp="${g}">${g}</span>`).join("");
 md.querySelectorAll(".md").forEach(c=>c.onclick=()=>{curMD=+c.dataset.md;chips();list()});
 gp.querySelectorAll(".gp").forEach(c=>c.onclick=()=>{curGP=c.dataset.gp;chips();list()});}
function list(){const L=PRED.matches.filter(m=>m.md==curMD&&(curGP=="all"||m.group==curGP));let h="";
 L.forEach((m,i)=>{const r=m.judgment,p=m.played;let fh,fa,pill,head,hs="",as="";
  if(p){fh=p[0]>p[1];fa=p[1]>p[0];hs=p[0];as=p[1];pill=m.fresh?'<span class="pill new">刚更新</span>':'<span class="pill fin">完赛</span>';head="最终比分";}
  else{fh=r.w>=r.l;fa=r.l>r.w;pill=m.src=='market'?'<span class="pill mkt">盘口校准</span>':'<span class="pill mod">模型</span>';
   head=`${fh?m.home:m.away} 胜 ${pct(Math.max(r.w,r.l))}%`;}
  h+=`<div class="mrow ${m.fresh?'fresh':''}" data-i="${i}"><span class="tag">${m.group}组·R${m.md}</span>
   <span class="mteams"><span class="${fh?'fav':''}">${m.home}</span><span class="vs">vs</span><span class="${fa?'fav':''}">${m.away}</span>${m.note?'<span class="jdot" title="含伤停判断"></span>':''}</span>
   ${p?`<span class="mscore">${hs} : ${as}</span>`:`<span class="headline">${head}</span>`}${pill}</div>`});
 const box=document.getElementById("list");box.innerHTML=h||'<small>暂无比赛</small>';
 box.querySelectorAll(".mrow").forEach(c=>c.onclick=()=>report(L[+c.dataset.i]));}

/* ---- single-match report ---- */
function wrow(name,prob,ci,color){
 return `<div class="wrow"><span class="wname">${name}</span>
  <span class="wtrack"><span class="wfill" style="width:${prob*100}%;background:${color}"></span>
  <span class="wci" style="left:${ci[0]*100}%;width:${(ci[1]-ci[0])*100}%"></span></span>
  <span class="wval"><b>${pct(prob)}%</b> [${pct(ci[0])}–${pct(ci[1])}%]</span></div>`;}
function heat(m){const G=m.grid6;let mx=0;G.forEach(r=>r.forEach(v=>mx=Math.max(mx,v)));
 let ti=0,tj=0;G.forEach((r,i)=>r.forEach((v,j)=>{if(v>G[ti][tj]){ti=i;tj=j}}));
 let h='<div class="heat"><span class="ax"></span>';
 for(let j=0;j<6;j++)h+=`<span class="ax">${j}</span>`;
 for(let i=0;i<6;i++){h+=`<span class="ax">${i}</span>`;
  for(let j=0;j<6;j++){const v=G[i][j],a=Math.pow(v/mx,.7);
   h+=`<span class="hc ${i==ti&&j==tj?'top':''}" style="background:rgba(34,211,166,${a.toFixed(3)})" title="${m.home} ${i}-${j} ${m.away}: ${pct(v)}%">${v>=0.04?pct(v):''}</span>`;}}
 h+='</div><div class="legend">纵轴 = '+m.home+' 进球数，横轴 = '+m.away+' 进球数；颜色越亮概率越高，金框为最可能比分。</div>';
 return h;}
function advRows(items,color){
 return `<div class="adv">${items.map(x=>`<div class="advrow"><span>${x.label}</span><span class="advbar"><i style="width:${x.prob*100}%;background:${color||'linear-gradient(90deg,var(--acc),var(--acc2))'}"></i></span><span class="advval">${pct(x.prob)}%</span></div>`).join("")}</div>`;}
function htft(m){const rows=m.advanced.htft.rows,cols=["H","D","A"];let mx=0;
 rows.forEach(r=>cols.forEach(c=>mx=Math.max(mx,r.cells[c].prob)));
 let h='<div class="htft"><span class="hd">HT \\ FT</span><span class="hd">H</span><span class="hd">D</span><span class="hd">A</span>';
 rows.forEach(r=>{h+=`<span class="hd">${r.ht}</span>`;cols.forEach(c=>{const v=r.cells[c].prob,a=Math.max(.08,Math.pow(v/mx,.7));h+=`<span class="${v==mx?'hot':''}" style="background:rgba(34,211,166,${a.toFixed(3)})" title="HT/FT ${r.ht}/${c}: ${pct(v)}%">${r.ht}/${c}<br>${pct(v)}%</span>`})});
 return h+'</div><div class="legend">H = 主胜，D = 平局，A = 客胜；金框为最高概率 HT/FT 组合。</div>';}
function scorelineBlock(m){
 const sl=m.scoreline||{},items=sl.top3||m.judgment.top.slice(0,3).map(x=>({score:`${x[0]}-${x[1]}`,prob:x[2]}));
 return `<div><div style="font-size:11px;color:var(--mut)">Calibrated Exact Score Top 3 · 比分候选</div>
  <div class="scorechips">${items.map((x,i)=>`<span class="scorechip"><b>${esc(x.score)}</b><span>${i==0?'Mode':'Alt'} · ${pct(x.prob)}%</span></span>`).join("")}</div>
  <div class="scorehint">tempo/overdispersion 校准后展示；胜平负总概率保持不变，Top 3 和热力图比只看一个比分更可靠。</div></div>
  <div class="cf">Scoreline<br>concentration<b>${esc(sl.concentration_label||"分布")}</b><span style="font-size:11px">Mode ${pct(sl.mode_prob||items[0].prob)}%</span></div>`;
}
function report(m){const r=m.judgment,ci=m.ci,p=m.played;
 const fav=r.w>=r.l?m.home:m.away, fp=Math.max(r.w,r.l);
 const conf=fp>0.58&&(ci.w[1]-ci.w[0]<0.2||ci.l[1]-ci.l[0]<0.2)?"把握较高":fp>0.45?"把握中等":"胜负难料";
 let cmp="";
 if(m.market){const mk=m.market;
  cmp=`<div class="cmp"><b style="color:var(--gold)">模型 vs 盘口</b><br>
   主胜 模型 ${pct(r.w)}% / 盘口 ${pct(mk[0])}% &nbsp;·&nbsp; 平 模型 ${pct(r.d)}% / 盘口 ${pct(mk[1])}% &nbsp;·&nbsp; 客胜 模型 ${pct(r.l)}% / 盘口 ${pct(mk[2])}%</div>`;}
 let res=p?`<div class="fin">最终比分<b>${m.home} ${p[0]} : ${p[1]} ${m.away}</b></div>`:"";
 const sig=(m.public_signals||[]).map(s=>`<span>${s}</span>`).join("");
 document.getElementById("rep").innerHTML=`<span class="x" onclick="cl()">×</span>
  <div class="rh">${m.home} <span style="color:var(--mut);font-weight:400">vs</span> ${m.away}</div>
  <div class="rs">${m.group} 组 · 第 ${m.md} 轮 · ${m.src=='market'?'已用实时盘口校准':'Elo + Dixon-Coles 模型'}</div>
  ${res}
  <div class="call scorecall">${scorelineBlock(m)}</div>

  <div class="seclab">胜平负 · 含 90% 置信区间</div>
  ${wrow(m.home+" 胜",r.w,ci.w,"var(--acc2)")}
  ${wrow("平局",r.d,ci.d,"#54608a")}
  ${wrow(m.away+" 胜",r.l,ci.l,"var(--gold)")}
  ${cmp}

  <div class="seclab">Totals · O/U 2.5 · Total Goals</div>
  <div class="hb">
    <div class="hr"><span>xG Total</span><span><b style="color:#fff">${m.advanced.totals.xg_total}</b></span></div>
    ${advRows(m.advanced.totals.buckets)}
    <div style="height:8px"></div>
    ${advRows(m.advanced.totals.over_under,'linear-gradient(90deg,var(--gold),var(--red))')}
  </div>

  <div class="seclab">Handicap 3-Way · 让球胜平负</div>
  <div class="hb">
    <div class="hcapline">Line: <b>${m.advanced.handicap.label}</b> · Type: <b>${m.advanced.handicap.type}</b></div>
    ${advRows(m.advanced.handicap.outcomes,'linear-gradient(90deg,var(--pur),var(--acc))')}
  </div>

  <div class="seclab">HT/FT · 半全场</div>
  ${htft(m)}

  <div class="seclab">比分概率分布</div>${heat(m)}

  <div class="seclab">上下半场走势</div>
  <div class="hb">
   <div class="hr"><span>半场领先</span><span>${m.home} ${pct(r.hw)}% · 平 ${pct(r.hd)}% · ${m.away} ${pct(r.hl)}%</span></div>
   <div class="flow"><i style="width:${r.hw*100}%;background:var(--acc2)"></i><i style="width:${r.hd*100}%;background:#54608a"></i><i style="width:${r.hl*100}%;background:var(--gold)"></i></div>
   <div class="hr"><span>上半场预期进球</span><span><b style="color:#fff">${r.h1}</b> （破门 ${pct(r.h1g)}%）</span></div>
   <div class="hr"><span>下半场预期进球</span><span><b style="color:#fff">${r.h2}</b> （破门 ${pct(r.h2g)}%）</span></div>
  </div>

  <div class="seclab">文字解读</div>
  <div class="note">${m.analysis}</div>

  <div class="seclab">关键数据</div>
  <div class="kv"><span>预期进球 <b>${r.lh} : ${r.la}</b></span><span>双方进球 <b>${pct(r.btts)}%</b></span>
   <span>大 2.5 球 <b>${pct(r.over)}%</b></span><span>小 2.5 球 <b>${pct(1-r.over)}%</b></span></div>
  <div class="seclab">免费公开信号</div>
  <div class="kv">${sig}</div>`;
 document.getElementById("ov").classList.add("show");}
function cl(){document.getElementById("ov").classList.remove("show");}
document.getElementById("ov").onclick=e=>{if(e.target.id=="ov")cl();};document.addEventListener("keydown",e=>{if(e.key=="Escape")cl();});

/* ---- title odds + model card (secondary) ---- */
let th=`<table class="tbl"><thead><tr><th>#</th><th>球队</th><th class="n">综合</th><th></th><th class="n">纯模型</th><th class="n">进32强</th><th class="n">进决赛</th></tr></thead><tbody>`;
PRED.title.slice(0,16).forEach((r,i)=>{th+=`<tr><td class="mut">${i+1}</td><td>${r.team}</td><td class="n best">${r.blended}%</td><td><span class="tbar"><i style="width:${Math.min(100,r.blended*4)}%"></i></span></td><td class="n mut">${r.model}%</td><td class="n">${r.advance}%</td><td class="n">${r.finalist}%</td></tr>`});
document.getElementById("title").innerHTML=th+'</tbody></table><small>“综合” = 模型 + 博彩夺冠盘口；“纯模型” = 引擎蒙特卡洛（含参数不确定性）。</small>';
function auditPct(x){return x==null?"--":pct(x)+"%";}
function auditPill(ok,label){return `<span class="auditpill ${ok?'ok':'bad'}">${label}</span>`;}
function renderAudit(){const A=PRED.audit||{},rates=A.rates||{},rows=A.rows||[],missing=A.missing||[];
 const box=document.getElementById("audit");if(!box)return;
 const note=A.audited_matches?`只统计有赛前留档的比赛；赛后重算不会计入。`:`归档从本版本开始建立；之前已经完赛但没有赛前快照的比赛会列为 Missing，不拿来美化命中率。`;
 let h=`<div>
  <span class="metric">已完赛 <b>${A.finished_matches||0}</b></span>
  <span class="metric">可审计样本 <b>${A.audited_matches||0}</b></span>
  <span class="metric">精确比分 Top1 <b>${auditPct(rates.exact_top1)}</b></span>
  <span class="metric">精确比分 Top3 <b>${auditPct(rates.exact_top3)}</b></span>
  <span class="metric">1X2 胜平负 <b>${auditPct(rates.wdl)}</b></span>
  <span class="metric">Missing 赛前留档 <b>${A.missing_prematch||0}</b></span>
  <span class="metric">Archive rows <b>${A.archive_records||0}</b></span>
 </div><div class="auditnote">${note}</div>`;
 if(rows.length){
  h+=`<table class="tbl"><thead><tr><th>比赛</th><th>赛前预测</th><th>实际</th><th class="nowrap">Top1</th><th class="nowrap">Top3</th><th class="nowrap">1X2</th></tr></thead><tbody>`;
  rows.slice(-18).reverse().forEach(r=>{const top=(r.top3||[]).map(x=>`${esc(x.score)} ${pct(x.prob)}%`).join(" / ");
   h+=`<tr><td>${esc(r.home)} <span class="auditmut">vs</span> ${esc(r.away)}<br><span class="auditmut">${esc(r.prediction_generated_at||"")}</span></td>
    <td><b>${esc(r.pred_score||"--")}</b><br><span class="auditmut">${top}</span></td><td class="best">${esc(r.actual_score)}</td>
    <td>${auditPill(r.exact_top1,r.exact_top1?"命中":"未中")}</td><td>${auditPill(r.exact_top3,r.exact_top3?"命中":"未中")}</td><td>${auditPill(r.wdl_hit,r.wdl_hit?"命中":"未中")}</td></tr>`});
  h+=`</tbody></table>`;
 }else{
  h+=`<div class="auditnote">还没有可审计样本。下一场有赛前快照的比赛完赛后，这里会自动显示 Top1 / Top3 / 1X2 命中率。</div>`;
 }
 if(missing.length){
  h+=`<small>Missing 示例：${missing.slice(0,8).map(m=>`${esc(m.home)} ${esc(m.actual_score)} ${esc(m.away)}`).join(" · ")}${missing.length>8?" · ...":""}</small>`;
 }
 box.innerHTML=h;
}
const bt=PRED.backtest,T=bt.test;
const mrow=(l,o,b)=>`<tr><td>${l}</td><td class="n ${b?'best':''}">${o.logloss}</td><td class="n ${b?'best':''}">${o.brier}</td><td class="n ${b?'best':''}">${o.rps}</td></tr>`;
const source=bt.data_source=="real_worldcup_history"||bt.data_source=="api_football_history"?"真实世界杯历史赛果":"拟真回退样本";
const calib=bt.calibration||{};
const tempo=PRED.tempo_adjustment||{goal_mult:1,source:"neutral",matches:0,note:"赛事节奏中性"};
document.getElementById("modelcard").innerHTML=
 `<div><span class="metric">训练来源 <b>${source}</b></span><span class="metric">历史样本 <b>${bt.n_history||0}</b></span><span class="metric">赛事节奏 <b>x${tempo.goal_mult}</b></span><span class="metric">集成权重(Dixon-Coles) <b>${pct(PRED.ensemble_weight_dc)}%</b></span><span class="metric">WDL 校准温度 <b>${calib.wdl_temperature||1}</b></span><span class="metric">主场优势 γ <b>${bt.dc_gamma}</b></span><span class="metric">低比分相关 ρ <b>${bt.dc_rho}</b></span><span class="metric">测试样本 <b>${bt.n_test}</b></span></div>
 <table class="tbl" style="margin-top:10px"><thead><tr><th>模型（留出测试集）</th><th class="n">LogLoss</th><th class="n">Brier</th><th class="n">RPS↓</th></tr></thead><tbody>
 ${mrow("本模型（校准后）",T.model,1)}${T.raw_model?mrow("本模型（未校准）",T.raw_model):""}${mrow("仅 Elo",T.elo_only)}${mrow("仅 Dixon-Coles",T.dc_only)}${mrow("朴素基准",T.baseline)}${mrow("锐利盘口基准",T.sharp_market_proxy)}</tbody></table>
 <small>数值越低越好。Brier / LogLoss / RPS 衡量的是概率准不准，比“精确比分”更有价值；精确比分层使用 tempo/overdispersion 校准并保持胜平负总概率不变。</small>
 <div class="note" style="margin-top:12px">数据说明：云端会优先用 API-Football + openfootball 历史赛果重新拟合；胜平负概率使用验证集 temperature scaling 做校准。${esc(tempo.note)} 若历史样本不足，页面会明确显示为“拟真回退样本”，不会假装成真实历史。</div>`;
renderAudit();chips();list();</script></body></html>"""

def build_site(payload):
    html = SITE_TEMPLATE.replace("/*DATA*/", json.dumps(payload, ensure_ascii=False))
    out = os.path.abspath(os.path.join(HERE, "..", "wc2026_predictor.html"))
    open(out, "w").write(html)
    print("Rebuilt webpage:", out)

def main():
    print("Backtesting / fitting models on history…")
    report, elo, dc, w = run_backtest(history_rows=HISTORICAL_RESULTS, min_real_history_matches=40)
    wdl_temperature = report.get("calibration", {}).get("wdl_temperature", 1.0)
    print(f"  ensemble weight (Dixon-Coles share) = {w:.2f}")
    print(f"  test RPS  model={report['test']['model']['rps']}  "
          f"elo={report['test']['elo_only']['rps']}  dc={report['test']['dc_only']['rps']}  "
          f"baseline={report['test']['baseline']['rps']}  market={report['test']['sharp_market_proxy']['rps']}")

    print("Running tournament Monte-Carlo with parameter uncertainty…")
    form_adjustments = build_combined_adjustments(GROUPS, PLAYED)
    tempo_adjustment = build_tempo_adjustment(HISTORICAL_RESULTS)
    pred = Predictor(
        elo,
        dc,
        w,
        form_adjustments=form_adjustments,
        tempo_mult=tempo_adjustment["goal_mult"],
    )
    sim = simulate(pred, n=20000)
    mp, bl = title_blend(sim["title"], sim["n"])

    # per-match predictions
    fx = fixtures()
    matches = []
    def pack(s):
        return dict(lh=round(s["lh"],2), la=round(s["la"],2),
                    w=round(s["w"],4), d=round(s["d"],4), l=round(s["l"],4),
                    btts=round(s["btts"],3), over=round(s["over"],3),
                    top=[[int(i),int(j),round(p,4)] for (i,j),p in s["top"]],
                    scoreline_model=s.get("scoreline_model"),
                    hw=round(s["hw"],3), hd=round(s["hd"],3), hl=round(s["hl"],3),
                    h1=round(s["h1"],2), h2=round(s["h2"],2),
                    h1g=round(s["h1g"],3), h2g=round(s["h2g"],3))
    for f in fx:
        h,a=f["home"],f["away"]
        played = PLAYED.get((h,a))
        host = (h in HOSTS) and (a not in HOSTS)
        base,judg,src,note,mkt,ci,grid6,advanced = predict_match(
            elo,dc,w,h,a, form_adjustments=form_adjustments, wdl_temperature=wdl_temperature,
            tempo_mult=tempo_adjustment["goal_mult"])
        ana = analysis_zh(h,a,judg,ci,mkt,note,host)
        meta = match_metadata(h, a, played, UPDATE_STATUS, FRESH_RESULT_KEYS, HISTORICAL_RESULTS,
                              form_adjustments=form_adjustments, tempo_adjustment=tempo_adjustment)
        matches.append({**f, "src":src, "note":note,
                        "played": list(played) if played else None,
                        "model": pack(base), "judgment": pack(judg),
                        "scoreline": scoreline_summary(judg["top"], judg.get("scoreline_model")),
                        "ci":ci, "market":mkt, "grid6":grid6, "advanced":advanced, "analysis":ana,
                        "fresh": meta["fresh"], "played_source": meta["played_source"],
                        "public_signals": meta["public_signals"]})

    title=[{"team":t,"model":round(mp[t]*100,2),"blended":round(bl[t]*100,2),
            "group_win":round(sim["group_win"][t]/sim["n"]*100,1),
            "advance":round(sim["advance"][t]/sim["n"]*100,1),
            "finalist":round(sim["finalist"][t]/sim["n"]*100,2)}
           for t in TEAMS]
    title.sort(key=lambda x:-x["blended"])

    ratings={t:ELO_PRIOR[t] for t in TEAMS}   # display real anchored Elo, not synthetic-fit
    payload={"version":MODEL_VERSION,
             "generated":datetime.date.today().isoformat(),
             "generated_at":datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "update_status":UPDATE_STATUS,
             "form_adjustments":form_adjustments,
             "tempo_adjustment":tempo_adjustment,
             "ensemble_weight_dc":round(w,3),
             "backtest":report,"ratings":ratings,
             "matches":matches,"title":title}

    archive, audit_report = save_audit_artifacts(
        payload,
        here=HERE,
        commit_sha=os.environ.get("GITHUB_SHA"),
    )

    json.dump(payload, open(os.path.join(HERE,"predictions.json"),"w"),
              ensure_ascii=False, indent=1)
    json.dump(report, open(os.path.join(HERE,"backtest_report.json"),"w"),
              ensure_ascii=False, indent=2)
    json.dump(ratings, open(os.path.join(HERE,"ratings.json"),"w"),
              ensure_ascii=False, indent=1)

    # P7: log predictions for later accuracy evaluation (dedupe same-date reruns)
    logf=os.path.join(HERE,"prediction_log.csv")
    header=["date","version","home","away","p_home","p_draw","p_away","pred_score","actual"]
    kept=[]
    if os.path.exists(logf):
        rd=list(csv.reader(open(logf)))
        for row in rd[1:]:
            if row and row[0]!=payload["generated"]: kept.append(row)
    with open(logf,"w",newline="") as fp:
        wtr=csv.writer(fp); wtr.writerow(header)
        for row in kept: wtr.writerow(row)
        for m in matches:
            if m["played"]: continue
            j=m["judgment"]; top=j["top"][0]
            wtr.writerow([payload["generated"],MODEL_VERSION,m["home"],m["away"],
                          j["w"],j["d"],j["l"],f"{top[0]}-{top[1]}",""])

    build_site(payload)

    print("\n=== TITLE ODDS (blended) ===")
    for r in title[:12]:
        print(f"  {r['team']:<16} model {r['model']:>5.1f}%  blended {r['blended']:>5.1f}%  "
              f"R32 {r['advance']:>5.1f}%  final {r['finalist']:>4.1f}%")
    print(f"\nAudit archive rows={len(archive)} audited={audit_report['audited_matches']} missing={audit_report['missing_prematch']}")
    print(f"\nWrote predictions.json, ratings.json, backtest_report.json, prediction_log.csv, prediction_archive.json, audit_report.json")
    return payload

if __name__ == "__main__":
    main()
