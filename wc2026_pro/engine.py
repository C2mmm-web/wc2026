"""
wc2026_pro.engine — the model core.

Solves:
  P1  results-driven Elo that updates per match (EloModel)
  P2  Dixon-Coles bivariate-Poisson with per-team attack/defense, home adv, rho
  P5  ensemble of (Elo-mapping) + (Dixon-Coles), weight set by backtest
  P6  contextual multipliers: rest, altitude/heat, motivation
  P7  systematic, quantified availability (injury) adjustments
"""
import math
import numpy as np
from scipy.optimize import minimize
from data import ELO_PRIOR, HOSTS, VENUE, DEFAULT_VENUE, INJURIES

# ---------------- odds helpers ----------------
def american_to_prob(o):
    return 100 / (o + 100) if o > 0 else (-o) / (-o + 100)

def devig(home, draw, away):
    p = [american_to_prob(home), american_to_prob(draw), american_to_prob(away)]
    s = sum(p)
    return [x / s for x in p]

# ================= P1: results-driven Elo =================
class EloModel:
    """Online Elo updated per match: home advantage + margin-of-victory multiplier."""
    def __init__(self, prior=None, k=32, home_adv=60):
        self.k = k
        self.home_adv = home_adv
        self.r = dict(prior) if prior else {}

    def get(self, t):
        return self.r.get(t, 1500.0)

    def expected(self, home, away, neutral=False):
        ha = 0 if neutral else self.home_adv
        return 1 / (1 + 10 ** (-((self.get(home) + ha) - self.get(away)) / 400))

    def update(self, home, away, hg, ag, neutral=False, weight=1.0):
        exp_h = self.expected(home, away, neutral)
        res = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        gd = abs(hg - ag)
        mov = math.log(max(gd, 1) + 1) * (2.2 / ((abs(self.get(home) - self.get(away)) * 0.001) + 2.2))
        delta = self.k * weight * mov * (res - exp_h)
        self.r[home] = self.get(home) + delta
        self.r[away] = self.get(away) - delta

    def fit(self, matches):
        """matches: iterable of (home, away, hg, ag, neutral, weight)."""
        for m in matches:
            self.update(*m)
        return self

# ================= P2: Dixon-Coles =================
def _tau(x, y, lh, la, rho):
    if x == 0 and y == 0: return 1 - lh * la * rho
    if x == 0 and y == 1: return 1 + lh * rho
    if x == 1 and y == 0: return 1 + la * rho
    if x == 1 and y == 1: return 1 - rho
    return 1.0

class DixonColes:
    def __init__(self, teams):
        self.teams = list(teams)
        self.idx = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)
        self.att = np.zeros(n); self.dff = np.zeros(n)
        self.gamma = 0.25   # log home advantage
        self.rho = -0.05    # low-score correction

    def _unpack(self, theta):
        n = len(self.teams)
        att = theta[:n]; dff = theta[n:2*n]; gamma = theta[2*n]; rho = theta[2*n+1]
        att = att - att.mean()            # identifiability: mean-zero attack
        dff = dff - dff.mean()
        return att, dff, gamma, rho

    def _nll(self, theta, H, A, HG, AG, W):
        att, dff, gamma, rho = self._unpack(theta)
        lh = np.exp(att[H] - dff[A] + gamma)
        la = np.exp(att[A] - dff[H])
        lh = np.clip(lh, 1e-3, 12); la = np.clip(la, 1e-3, 12)
        ll = HG*np.log(lh) - lh - AG*np.log(la) - la            # Poisson logpmf (drop const)
        # DC tau correction on low scores
        tau = np.ones_like(lh)
        for k in range(len(H)):
            tau[k] = _tau(min(HG[k],1), min(AG[k],1),
                          lh[k] if HG[k] <= 1 else 1, la[k] if AG[k] <= 1 else 1, rho) \
                     if (HG[k] <= 1 and AG[k] <= 1) else 1.0
        ll = ll + np.log(np.clip(tau, 1e-6, None))
        return -np.sum(W * ll)

    def fit(self, matches):
        """matches: list of (home, away, hg, ag, neutral, weight). Neutral folds home adv via gamma*(1-neutral)."""
        H, A, HG, AG, W, NEU = [], [], [], [], [], []
        for h, a, hg, ag, neu, w in matches:
            if h not in self.idx or a not in self.idx: continue
            H.append(self.idx[h]); A.append(self.idx[a]); HG.append(hg); AG.append(ag)
            W.append(w); NEU.append(0.0 if neu else 1.0)
        H=np.array(H); A=np.array(A); HG=np.array(HG); AG=np.array(AG); W=np.array(W)
        n = len(self.teams)
        theta0 = np.concatenate([self.att, self.dff, [self.gamma, self.rho]])
        # neutral handled by scaling gamma per match — approximate by including only home matches' gamma:
        # we bake neutrality into a per-match home term via a closure
        self._NEU = np.array(NEU)
        def nll(theta):
            att, dff, gamma, rho = self._unpack(theta)
            lh = np.exp(att[H] - dff[A] + gamma*self._NEU)
            la = np.exp(att[A] - dff[H])
            lh = np.clip(lh,1e-3,12); la=np.clip(la,1e-3,12)
            ll = HG*np.log(lh)-lh - 0 + AG*np.log(la)-la
            tau = np.ones_like(lh)
            mask = (HG<=1)&(AG<=1)
            for k in np.where(mask)[0]:
                tau[k] = _tau(HG[k],AG[k],lh[k],la[k],rho)
            ll = ll + np.log(np.clip(tau,1e-6,None))
            return -np.sum(W*ll)
        res = minimize(nll, theta0, method="L-BFGS-B",
                       options={"maxiter": 400, "maxfun": 40000})
        att, dff, gamma, rho = self._unpack(res.x)
        self.att, self.dff, self.gamma, self.rho = att, dff, gamma, max(min(rho,0.2),-0.2)
        return self

    def lambdas(self, home, away, neutral=True):
        ah = self.att[self.idx[home]]; dh = self.dff[self.idx[home]]
        aa = self.att[self.idx[away]]; da = self.dff[self.idx[away]]
        g = 0.0 if neutral else self.gamma
        lh = math.exp(ah - da + g)
        la = math.exp(aa - dh)
        return lh, la

# ================= Elo -> lambda mapping (model #1) =================
BASE_GOALS = 2.62
def elo_lambdas(elo, home, away, neutral=True, home_adv=60):
    ha = 0 if neutral else home_adv
    diff = (elo.get(home) + (ha if home in HOSTS or not neutral else 0)) - elo.get(away)
    sup = 0.0044 * diff
    return max(0.12, BASE_GOALS/2 + sup/2), max(0.12, BASE_GOALS/2 - sup/2)

# ================= P6: contextual multipliers =================
def context_mult(home, away, rest_home=3, rest_away=3):
    """Return (mult_home, mult_away) on expected goals from rest/altitude/heat."""
    v = VENUE.get((home, away), DEFAULT_VENUE)
    mh = ma = 1.0
    # rest: each day of rest deficit vs opponent ~1.5% on goals
    mh *= 1 + 0.015 * (rest_home - rest_away)
    ma *= 1 + 0.015 * (rest_away - rest_home)
    # altitude favours the acclimatised host; visitors fade -> fewer away goals at high alt
    if v["alt"] > 1500:
        if away not in HOSTS: ma *= 0.94
        if home in HOSTS: mh *= 1.03
    # heat compresses both teams' output slightly (more cagey)
    heat = v.get("heat", 0.4)
    mh *= 1 - 0.05*heat; ma *= 1 - 0.05*heat
    return mh, ma

# ================= P7: quantified availability / judgment =================
def availability_mult(home, away):
    """Convert structured injury input into expected-goals multipliers + note."""
    info = INJURIES.get((home, away))
    if not info:
        return 1.0, 1.0, ""
    mh = ma = 1.0
    for _, imp, kind in info.get("home_out", []):
        if kind in ("attacking", "creative"): mh *= (1 - imp)          # lose own attack
        elif kind == "defensive": ma *= (1 + 0.6*imp)                   # opp scores more
    for _, imp, kind in info.get("away_out", []):
        if kind in ("attacking", "creative"): ma *= (1 - imp)
        elif kind == "defensive": mh *= (1 + 0.6*imp)
    return mh, ma, info.get("note", "")

# ================= scoreline grid with DC correction =================
def _pois(k, l): return math.exp(-l) * l**k / math.factorial(k)

def grid(lh, la, rho=-0.05, maxg=11):
    g = np.zeros((maxg, maxg))
    for i in range(maxg):
        for j in range(maxg):
            p = _pois(i, lh) * _pois(j, la)
            if i <= 1 and j <= 1:
                p *= _tau(i, j, lh, la, rho)
            g[i, j] = p
    g /= g.sum()
    return g

def summarize(lh, la, rho=-0.05):
    g = grid(lh, la, rho)
    w = np.tril(g, -1).sum(); d = np.trace(g); l = np.triu(g, 1).sum()
    btts = g[1:, 1:].sum()
    over = sum(g[i, j] for i in range(g.shape[0]) for j in range(g.shape[1]) if i+j > 2.5)
    flat = [((i, j), g[i, j]) for i in range(6) for j in range(6)]
    flat.sort(key=lambda x: -x[1]); top = flat[:4]
    # halves 44/56
    gh = grid(lh*0.44, la*0.44, rho, 7)
    hw = np.tril(gh, -1).sum(); hd = np.trace(gh); hl = np.triu(gh, 1).sum()
    return dict(lh=lh, la=la, w=w, d=d, l=l, btts=btts, over=over, top=top,
                hw=hw, hd=hd, hl=hl, h1=lh*0.44+la*0.44, h2=lh*0.56+la*0.56,
                h1g=1-math.exp(-(lh+la)*0.44), h2g=1-math.exp(-(lh+la)*0.56))
