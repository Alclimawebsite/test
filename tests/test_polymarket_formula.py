"""Vérifie la formule exacte P(Up) par simulation de marches aléatoires seconde par seconde."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradebot.polymarket_formula import (
    fair_prob_up,
    parkinson_sigma_per_s,
    taker_edge,
    twap_diff_moments,
)

L, D = 60, 300
S, E = 1000.0, 1300.0
SIG = 1e-4  # par √s


def _simulate(n_paths: int, seed: int):
    """Chemins de log-prix à la seconde de S − L − 30 à E (moyennes (t − L, t] discrètes)."""
    rng = np.random.default_rng(seed)
    t0 = int(S - L - 30)
    times = np.arange(t0, int(E) + 1)
    steps = rng.normal(0, SIG, size=(n_paths, times.size - 1))
    paths = np.concatenate([np.zeros((n_paths, 1)), np.cumsum(steps, axis=1)], axis=1)
    idx = lambda t: int(t) - t0  # noqa: E731
    K = paths[:, idx(S - L) + 1: idx(S) + 1].mean(axis=1)
    F = paths[:, idx(E - L) + 1: idx(E) + 1].mean(axis=1)
    return times, paths, idx, K, F


@pytest.mark.parametrize("t", [S - 45, S - 30, S - 10, S, S + 100, E - 90, E - 30, E - 10])
def test_moments_match_monte_carlo(t):
    times, paths, idx, K, F = _simulate(40_000, seed=int(t))
    diff = F - K
    lp = paths[:, idx(t)]
    kw = {}
    if S - L < t <= S:
        kw["start_avg"] = paths[:, idx(S - L) + 1: idx(t) + 1].mean(axis=1)
    if t > S:
        kw["price_to_beat"] = K
    if t > E - L:
        kw["end_partial_sum"] = paths[:, idx(E - L) + 1: idx(t) + 1].sum(axis=1) / L
    # moyenne conditionnelle : résidu diff − m(t) de moyenne nulle, écart-type s(t)
    means, sds = [], []
    for i in range(2000):
        m, s, _ = twap_diff_moments(t, S, E, lp[i], SIG,
                                    **{k: (v[i] if hasattr(v, "__len__") else v) for k, v in kw.items()})
        means.append(m)
        sds.append(s)
    resid = diff[:2000] - np.array(means)
    assert abs(resid.mean()) < 4 * sds[0] / math.sqrt(2000)
    assert resid.std() == pytest.approx(sds[0], rel=0.08)


def test_probability_is_calibrated_before_open():
    """À S − 30 s, P(Up) prévue par la formule = fréquence simulée, par déciles."""
    t = S - 30
    times, paths, idx, K, F = _simulate(60_000, seed=7)
    up = F >= K
    start_avg = paths[:, idx(S - L) + 1: idx(t) + 1].mean(axis=1)
    lp = paths[:, idx(t)]
    p = np.array([fair_prob_up(t, S, E, lp[i], SIG, start_avg=start_avg[i]).prob_up for i in range(lp.size)])
    bins = np.quantile(p, np.linspace(0, 1, 11))
    k = np.clip(np.searchsorted(bins, p, side="right") - 1, 0, 9)
    for b in range(10):
        sel = k == b
        assert abs(p[sel].mean() - up[sel].mean()) < 0.02


def test_phase_one_is_coin_flip():
    fv = fair_prob_up(S - 120, S, E, 0.0, SIG)
    assert fv.phase == 1 and fv.prob_up == 0.5


def test_parkinson_on_brownian_bars():
    rng = np.random.default_rng(1)
    x = np.cumsum(rng.normal(0, SIG, size=(5000, 60)), axis=1)
    x = np.concatenate([np.zeros((5000, 1)), x], axis=1)
    est = parkinson_sigma_per_s(np.exp(x.max(axis=1)), np.exp(x.min(axis=1)))
    assert est == pytest.approx(SIG, rel=0.1)  # biais de discrétisation du vrai max/min


def test_taker_edge():
    e = taker_edge(0.60, ask_up=0.52, ask_down=0.49)
    assert e["side"] == "up"
    assert e["ev_up"] == pytest.approx(0.60 - 0.52 - 0.07 * 0.52 * 0.48)
    assert taker_edge(0.505, 0.51, 0.50)["side"] is None


@pytest.mark.parametrize("t", [S - 45, S - 10, S + 100, E - 30, E - 5])
def test_discrete_tends_to_continuous(t):
    kw = dict(start_avg=0.0, price_to_beat=0.0, end_partial_sum=0.0)
    _, s_cont, _ = twap_diff_moments(t, S, E, 0.0, SIG, dt=None, **kw)
    _, s_fine, _ = twap_diff_moments(t, S, E, 0.0, SIG, dt=0.01, **kw)
    assert s_fine == pytest.approx(s_cont, rel=0.01)
