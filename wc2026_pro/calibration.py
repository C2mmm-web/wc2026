"""
Post-hoc probability calibration helpers.

Temperature scaling keeps ranking/order intact but softens or sharpens class
probabilities using validation-set log loss.
"""
import math


def _normalize(probs):
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(probs)] * len(probs)
    return [p / total for p in probs]


def apply_temperature(probs, temperature):
    t = max(0.25, min(4.0, float(temperature or 1.0)))
    clipped = [max(1e-12, float(p)) for p in probs]
    scaled = [math.exp(math.log(p) / t) for p in clipped]
    return _normalize(scaled)


def fit_temperature(prob_vectors, outcomes, candidates=None):
    candidates = candidates or [0.55 + 0.05 * i for i in range(70)]
    best = (float("inf"), 1.0)
    for temp in candidates:
        loss = 0.0
        for probs, y in zip(prob_vectors, outcomes):
            calibrated = apply_temperature(probs, temp)
            loss -= math.log(max(calibrated[int(y)], 1e-12))
        if loss < best[0]:
            best = (loss, float(temp))
    return best[1]
