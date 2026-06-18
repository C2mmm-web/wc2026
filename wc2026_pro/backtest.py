"""
wc2026_pro.backtest — P3: validation, calibration, ensemble-weight optimisation.

When fetch_results.py can reach API-Football, this module trains and validates on
real historical World Cup fixtures. If that feed is unavailable or too thin, it
falls back to a realistic synthetic history so the page still builds.
"""
import numpy as np
from data import TEAMS, ELO_PRIOR
from engine import EloModel, DixonColes, elo_lambdas, _pois, grid as score_grid
from calibration import apply_temperature, fit_temperature
from scorelines import top_scorelines

rng = np.random.default_rng(2026)

def true_params():
    s = {t: (ELO_PRIOR[t] - 1800) / 175.0 for t in TEAMS}
    att = {t: 0.20 * s[t] for t in TEAMS}
    dff = {t: 0.20 * s[t] for t in TEAMS}
    return att, dff, 0.25, -0.06            # gamma, rho

def gen_history(n_matches=13000):
    att, dff, gamma, rho = true_params()
    drift = {t: 0.0 for t in TEAMS}
    rows = []
    for k in range(n_matches):
        date = k / n_matches                # 0..1 (older->newer)
        h, a = rng.choice(TEAMS, 2, replace=False)
        neutral = rng.random() < 0.35
        for t in (h, a):                    # slow random-walk form drift
            drift[t] += rng.normal(0, 0.0022)
        g = 0.0 if neutral else gamma
        lh = np.exp(att[h] + drift[h] - dff[a] + g + rng.normal(0, 0.15))
        la = np.exp(att[a] + drift[a] - dff[h] + rng.normal(0, 0.15))
        lh = min(lh, 8); la = min(la, 8)
        hg = rng.poisson(lh); ag = rng.poisson(la)
        if hg <= 1 and ag <= 1 and rng.random() < 0.06:  # mild draw inflation (DC effect)
            ag = hg
        rows.append((h, a, int(hg), int(ag), neutral, date))
    return rows

def rows_from_history(history_rows):
    rows = []
    for r in history_rows or []:
        h, a = r.get("home"), r.get("away")
        if h not in TEAMS or a not in TEAMS or h == a:
            continue
        try:
            hg = int(r.get("home_goals"))
            ag = int(r.get("away_goals"))
        except (TypeError, ValueError):
            continue
        rows.append((r.get("date") or "", h, a, hg, ag, bool(r.get("neutral", True))))
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    out = []
    for i, (_date, h, a, hg, ag, neutral) in enumerate(rows):
        date_norm = 1.0 if n <= 1 else i / (n - 1)
        out.append((h, a, hg, ag, neutral, date_norm))
    return out

def decay_weight(date, halflife=0.25):
    return 0.5 ** ((1 - date) / halflife)

# ---------- metrics ----------
def log_loss(p, y):       # p=[ph,pd,pa], y in {0:home,1:draw,2:away}
    return -np.log(max(p[y], 1e-12))

def brier(p, y):
    o = [0, 0, 0]; o[y] = 1
    return sum((p[i] - o[i])**2 for i in range(3))

def rps(p, y):            # ordered home>draw>away
    o = [0, 0, 0]; o[y] = 1
    c = 0.0
    cp = co = 0.0
    for i in range(2):
        cp += p[i]; co += o[i]; c += (cp - co)**2
    return c / 2

def probs_from_lambdas(lh, la, rho=-0.05, maxg=10):
    from engine import _tau
    w = d = l = 0.0
    for i in range(maxg):
        for j in range(maxg):
            pr = _pois(i, lh) * _pois(j, la)
            if i <= 1 and j <= 1: pr *= _tau(i, j, lh, la, rho)
            if i > j: w += pr
            elif i == j: d += pr
            else: l += pr
    s = w + d + l
    return [w/s, d/s, l/s]

def run_backtest(history_rows=None, min_real_history_matches=80):
    real_rows = rows_from_history(history_rows)
    use_real_history = len(real_rows) >= min_real_history_matches
    real_sources = sorted({r.get("source", "unknown") for r in history_rows or [] if r.get("source")})
    rows = real_rows if use_real_history else gen_history()
    n_all = len(rows)
    i1, i2 = int(n_all*0.6), int(n_all*0.8)
    train, val, test = rows[:i1], rows[i1:i2], rows[i2:]   # 60/20/20, no leakage

    def fit_models(data):
        prior = ELO_PRIOR if use_real_history else {t: 1500 for t in TEAMS}
        e = EloModel(prior=prior, k=30, home_adv=60)
        for h, a, hg, ag, neu, date in data:
            e.update(h, a, hg, ag, neutral=neu, weight=1.0)
        d = DixonColes(TEAMS)
        d.fit([(h, a, hg, ag, neu, decay_weight(date)) for h, a, hg, ag, neu, date in data])
        return e, d

    def pred_probs(e, d, match, w):
        h, a, _hg, _ag, neu, _date = match
        le_h, le_a = elo_lambdas(e.r, h, a, neutral=neu)
        ld_h, ld_a = d.lambdas(h, a, neutral=neu)
        lh = w*ld_h + (1-w)*le_h; la = w*ld_a + (1-w)*le_a
        lh = float(np.clip(lh, 0.05, 8.0)); la = float(np.clip(la, 0.05, 8.0))
        return probs_from_lambdas(lh, la, d.rho)

    def eval_w(e, d, matches, w, temperature=1.0):
        LL = BR = RP = 0.0
        for match in matches:
            _h, _a, hg, ag, _neu, _date = match
            p = apply_temperature(pred_probs(e, d, match, w), temperature)
            y = 0 if hg > ag else 1 if hg == ag else 2
            LL += log_loss(p, y); BR += brier(p, y); RP += rps(p, y)
        n = len(matches)
        return LL/n, BR/n, RP/n

    def eval_scorelines(e, d, matches, w):
        exact_top1 = exact_top3 = 0
        top1_prob = 0.0
        for match in matches:
            h, a, hg, ag, neu, _date = match
            le_h, le_a = elo_lambdas(e.r, h, a, neutral=neu)
            ld_h, ld_a = d.lambdas(h, a, neutral=neu)
            lh = float(np.clip(w * ld_h + (1 - w) * le_h, 0.05, 8.0))
            la = float(np.clip(w * ld_a + (1 - w) * le_a, 0.05, 8.0))
            top = top_scorelines(score_grid(lh, la, d.rho, 8), limit=3, max_goals=8)
            scores = [score for score, _prob in top]
            actual = (int(hg), int(ag))
            exact_top1 += int(bool(scores) and scores[0] == actual)
            exact_top3 += int(actual in scores)
            top1_prob += float(top[0][1]) if top else 0.0
        n = len(matches)
        return {
            "n": n,
            "exact_top1": round(exact_top1 / n, 3) if n else None,
            "exact_top3": round(exact_top3 / n, 3) if n else None,
            "avg_top1_prob": round(top1_prob / n, 3) if n else None,
        }

    # 1) select ensemble weight on VAL using models fit on TRAIN ONLY (no leakage)
    e_sel, d_sel = fit_models(train)
    ws = np.linspace(0, 1, 21)
    w_star = float(ws[int(np.argmin([eval_w(e_sel, d_sel, val, w)[2] for w in ws]))])
    val_probs = [pred_probs(e_sel, d_sel, m, w_star) for m in val]
    val_y = [0 if hg > ag else 1 if hg == ag else 2 for _h, _a, hg, ag, _neu, _date in val]
    wdl_temperature = fit_temperature(val_probs, val_y)

    # 2) refit on TRAIN+VAL for production-quality params, evaluate on untouched TEST
    elo, dc = fit_models(train + val)

    ys = [0 if hg > ag else 1 if hg == ag else 2 for h, a, hg, ag, neu, date in train]
    base = [ys.count(0)/len(ys), ys.count(1)/len(ys), ys.count(2)/len(ys)]

    # test metrics: model vs baselines vs sharp-market proxy
    ll_raw, br_raw, rp_raw = eval_w(elo, dc, test, w_star)
    ll_m, br_m, rp_m = eval_w(elo, dc, test, w_star, temperature=wdl_temperature)
    ll_e, br_e, rp_e = eval_w(elo, dc, test, 0.0)     # Elo-only
    ll_d, br_d, rp_d = eval_w(elo, dc, test, 1.0)     # DC-only
    scoreline_metrics = eval_scorelines(elo, dc, test, w_star)
    LLb = BRb = RPb = 0.0
    LLk = BRk = RPk = 0.0                      # market proxy
    att, dff, gamma, rho = true_params()
    for h, a, hg, ag, neu, date in test:
        y = 0 if hg > ag else 1 if hg == ag else 2
        LLb += log_loss(base, y); BRb += brier(base, y); RPb += rps(base, y)
        g = 0.0 if neu else gamma
        lh = np.exp(att[h]-dff[a]+g); la = np.exp(att[a]-dff[h])
        pt = probs_from_lambdas(lh, la, rho)
        pm = [(x+0.02) for x in pt]; ssum=sum(pm); pm=[x/ssum for x in pm]  # +vig/noise
        LLk += log_loss(pm, y); BRk += brier(pm, y); RPk += rps(pm, y)
    n = len(test)

    # calibration: P(home win) deciles
    bins = [[] for _ in range(10)]
    for h, a, hg, ag, neu, date in test:
        le_h, le_a = elo_lambdas(elo.r, h, a, neutral=neu)
        ld_h, ld_a = dc.lambdas(h, a, neutral=neu)
        lh = w_star*ld_h+(1-w_star)*le_h; la = w_star*ld_a+(1-w_star)*le_a
        p = probs_from_lambdas(lh, la, dc.rho)
        b = min(9, int(p[0]*10)); bins[b].append(1 if hg > ag else 0)
    calib = [(f"{i*10}-{i*10+10}%", round(100*np.mean(b),1) if b else None, len(b))
             for i, b in enumerate(bins)]

    report = {
        "n_train": len(train)+len(val), "n_test": n,
        "n_history": len(real_rows),
        "data_source": "real_worldcup_history" if use_real_history else "synthetic_fallback",
        "history_sources": real_sources if use_real_history else [],
        "ensemble_weight_DC": w_star,
        "test": {
            "model":      dict(logloss=round(ll_m,4), brier=round(br_m,4), rps=round(rp_m,4)),
            "raw_model":  dict(logloss=round(ll_raw,4), brier=round(br_raw,4), rps=round(rp_raw,4)),
            "elo_only":   dict(logloss=round(ll_e,4), brier=round(br_e,4), rps=round(rp_e,4)),
            "dc_only":    dict(logloss=round(ll_d,4), brier=round(br_d,4), rps=round(rp_d,4)),
            "baseline":   dict(logloss=round(LLb/n,4), brier=round(BRb/n,4), rps=round(RPb/n,4)),
            "sharp_market_proxy": dict(logloss=round(LLk/n,4), brier=round(BRk/n,4), rps=round(RPk/n,4)),
            "scoreline": scoreline_metrics,
        },
        "calibration": {
            "method": "wdl_temperature_scaling",
            "wdl_temperature": round(wdl_temperature, 3),
        },
        "calibration_home_win": calib,
        "dc_gamma": round(dc.gamma,3), "dc_rho": round(dc.rho,3),
    }
    return report, elo, dc, w_star
