"""Tests hors-ligne de ``tradebot.timesfm_amplitude`` (aucun modèle, aucun réseau).

1. σ depuis les quantiles : déciles gaussiens -> σ retrouvé (q10/q90 et IQR), tri, NaN, unités.
2. Alignement temporel : le contexte se termine à l'origine, rien après n'entre dans la
   prévision (modification de toutes les barres postérieures -> quantiles identiques) ; trous.
3. Φ(d/σ) : d = 0 -> 0,5, antisymétrie, monotonie, σ = 0, bornes, σ_restant en √(τ − 0,5).
4. QLIKE : minimum en σ² = r², invariance d'échelle, plancher, NaN ; MZ ; couverture.
5. Volatilité réalisée causale (EWMA, fenêtres roulantes) ; choix de λ ; encompassing.
6. Quintiles (bornes sur l'apprentissage), ECDF, prix du marché à S+k, origine Polymarket.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradebot import timesfm_amplitude as ta
from tradebot.config import TimesFMConfig
from tradebot.forecaster import QUANTILE_LEVELS, TimesFMForecaster

Z9 = np.array([-1.2815516, -0.8416212, -0.5244005, -0.2533471, 0.0, 0.2533471, 0.5244005, 0.8416212, 1.2815516])


class FakeTimesFM3:
    """Quantiles = pente × (moyenne des 5 derniers points centrés) + σ_1 √pas × z ; enregistre
    les contextes reçus."""

    def __init__(self, sigma1: float = 5e-4, drift: float = 0.3):
        self.sigma1, self.drift = sigma1, drift
        self.calls: list[list[np.ndarray]] = []

    def predict_batch(self, contexts, horizon, past_only_covariates=None, return_quantiles=False,
                      use_symmetric_averaging=False, **kw):
        self.calls.append([np.array(c) for c in contexts])
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        for c in contexts:
            c = np.asarray(c, dtype=np.float64)
            off = self.drift * float(np.mean(c[-5:]))
            q = off + self.sigma1 * steps * Z9[None, :]
            yield SimpleNamespace(ts_id=None, forecast=q[:, 4].copy(), quantiles=q if return_quantiles else None)


@pytest.fixture
def series():
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-03-01", periods=3000, freq="1min", tz="UTC")
    lp = np.cumsum(rng.normal(0, 3e-4, len(idx)))
    close = pd.Series(100.0 * np.exp(lp), index=idx)
    bars = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "close": close})
    bars["high"] = np.maximum(bars["open"], bars["close"]) * (1 + rng.uniform(0, 2e-4, len(idx)))
    bars["low"] = np.minimum(bars["open"], bars["close"]) * (1 - rng.uniform(0, 2e-4, len(idx)))
    return bars


def unix(ts) -> int:
    return int(pd.Timestamp(ts).timestamp())


# -----------------------------------------------------------------------------
# 1. σ depuis les quantiles
# -----------------------------------------------------------------------------
def test_sigma_from_gaussian_quantiles():
    sig = np.array([3.0, 12.5])                         # pb
    q = sig[:, None] * Z9[None, :]
    np.testing.assert_allclose(ta.sigma_from_quantiles(q), sig, rtol=1e-6)
    np.testing.assert_allclose(ta.sigma_from_quantiles(q, "iqr"), sig, rtol=2e-2)   # q25/q75 interpolés
    # quantiles en log-prix -> pb avec scale=BP ; décalage de niveau sans effet
    np.testing.assert_allclose(ta.sigma_from_quantiles(q / ta.BP + 11.3, scale=ta.BP), sig, rtol=1e-6)
    # ordre des quantiles non trié, 1D, NaN
    assert ta.sigma_from_quantiles(q[0][::-1]) == pytest.approx(3.0, rel=1e-6)
    bad = q.copy()
    bad[1, 3] = np.nan
    out = ta.sigma_from_quantiles(bad)
    assert out[0] == pytest.approx(3.0, rel=1e-6) and np.isnan(out[1])
    with pytest.raises(ValueError):
        ta.sigma_from_quantiles(q, "median")
    with pytest.raises(ValueError):
        ta.sigma_from_quantiles(q[:, :5])


def test_sigma_per_minute_and_remaining():
    assert ta.sigma_per_minute(10.0, 4) == pytest.approx(5.0)
    # τ = 4,5 min restantes -> √(4,5 − 0,5) = 2
    np.testing.assert_allclose(ta.sigma_remaining(np.array([3.0, 3.0]), np.array([4.5, 0.5])), [6.0, np.nan])
    assert np.isnan(ta.sigma_remaining(3.0, 0.25))


# -----------------------------------------------------------------------------
# 2. Alignement temporel : rien après l'origine
# -----------------------------------------------------------------------------
def test_context_ends_at_origin_and_ignores_future(series):
    close = series["close"]
    fake = FakeTimesFM3()
    fc = TimesFMForecaster(TimesFMConfig(), model=fake)
    o = np.array([unix(close.index[700]), unix(close.index[2500])])
    q = ta.forecast_quantiles(fc, close, o, horizon=16, context_len=512, chunk=1)
    assert set(q["h"]) == set(range(1, 17)) and q["origin_s"].nunique() == 2
    # le contexte reçu par le modèle = log(close) des 512 barres finissant à l'origine, centré
    ctx = fake.calls[0][0]
    expect = np.log(close.iloc[700 - 511: 701].to_numpy())
    np.testing.assert_allclose(ctx, expect - expect[-1], atol=1e-12)
    assert ctx.size == 512
    # toutes les barres après l'origine sont modifiées : prévisions identiques
    c2 = close.copy()
    c2.iloc[701:] = c2.iloc[701:] * 5.0
    q2 = ta.forecast_quantiles(fc, c2, o[:1], horizon=16, context_len=512)
    a = q[q["origin_s"] == o[0]].sort_values("h")[["q10", "q50", "q90"]].to_numpy()
    b = q2.sort_values("h")[["q10", "q50", "q90"]].to_numpy()
    np.testing.assert_allclose(a, b, atol=1e-9)
    # une barre AVANT l'origine modifiée : la prévision change
    c3 = close.copy()
    c3.iloc[699] = c3.iloc[699] * 1.01
    q3 = ta.forecast_quantiles(fc, c3, o[:1], horizon=16, context_len=512)
    assert not np.allclose(a, q3.sort_values("h")[["q10", "q50", "q90"]].to_numpy())
    # sortie en pb de log-rendement depuis la dernière valeur : q50 du pas 1 = pente × moyenne
    row = q[(q["origin_s"] == o[0]) & (q["h"] == 1)].iloc[0]
    assert row["q90"] - row["q10"] == pytest.approx(5e-4 * 2 * 1.2815516 * ta.BP, rel=1e-6)
    assert row["last_close"] == pytest.approx(close.iloc[700])


def test_context_windows_gaps_and_short_history(series):
    close = series["close"]
    o = np.array([unix(close.index[600]), unix(close.index[100]), unix(close.index[600]) + 30])
    wins, ok = ta.context_windows(close, o, 512)
    assert ok.tolist() == [True, False, False] and len(wins) == 1 and wins[0].size == 512
    # trou de 2 barres : interpolé ; trou de 5 barres : écarté ; dernière barre absente : écarté
    c_small = close.drop(close.index[550:552])
    c_big = close.drop(close.index[550:555])
    c_last = close.drop(close.index[600])
    assert ta.context_windows(c_small, o[:1], 512, max_gap_bars=3)[1][0]
    assert not ta.context_windows(c_big, o[:1], 512, max_gap_bars=3)[1][0]
    assert not ta.context_windows(c_last, o[:1], 512)[1][0]
    w = ta.context_windows(c_small, o[:1], 512)[0][0]
    assert np.isfinite(w).all()
    assert ta._max_nan_run(np.array([True, False, False, True, False])) == 2
    assert ta._max_nan_run(np.array([True, True])) == 0


def test_future_log_return_and_realized_variance(series):
    close = series["close"]
    o = np.array([unix(close.index[10])])
    r = ta.future_log_return(close, o, 5)
    assert r[0] == pytest.approx(math.log(close.iloc[15] / close.iloc[10]) * ta.BP)
    assert np.isnan(ta.future_log_return(close, np.array([unix(close.index[-3])]), 5))[0]
    r2 = (np.log(close).diff() * ta.BP) ** 2
    rv = ta.realized_variance_h(r2, o, 5)
    assert rv[0] == pytest.approx(float(r2.iloc[11:16].sum()))
    assert np.isnan(ta.realized_variance_h(r2.drop(r2.index[13]), o, 5))[0]


# -----------------------------------------------------------------------------
# 3. Φ(d/σ)
# -----------------------------------------------------------------------------
def test_prob_up_gaussian_properties():
    assert ta.prob_up_gaussian(0.0, 5.0) == pytest.approx(0.5)
    p = ta.prob_up_gaussian(np.array([5.0, -5.0, 5.0]), np.array([5.0, 5.0, 10.0]))
    assert p[0] == pytest.approx(0.8413447, rel=1e-6)
    assert p[0] + p[1] == pytest.approx(1.0)
    assert p[2] < p[0]                                 # σ plus grande -> plus proche de 0,5
    assert ta.prob_up_gaussian(100.0, 1.0) == 0.99 and ta.prob_up_gaussian(-100.0, 1.0) == 0.01
    np.testing.assert_allclose(ta.prob_up_gaussian(np.array([1.0, -1.0, 0.0]), 0.0), [0.99, 0.01, 0.5])
    out = ta.prob_up_gaussian(np.array([1.0, np.nan, 1.0]), np.array([np.nan, 1.0, -1.0]))
    assert np.isnan(out).all()


def test_probability_scores():
    y = np.array([1.0, 0.0, 1.0, np.nan])
    p = np.array([0.8, 0.3, 0.4, 0.5])
    np.testing.assert_allclose(ta.brier(y[:3], p[:3]), [0.04, 0.09, 0.36])
    np.testing.assert_allclose(ta.accuracy(y, p), [1.0, 1.0, 0.0, np.nan])
    assert ta.log_loss(np.array([1.0]), np.array([0.5]))[0] == pytest.approx(math.log(2))


# -----------------------------------------------------------------------------
# 4. QLIKE, MZ, couverture
# -----------------------------------------------------------------------------
def test_qlike_minimum_scale_floor():
    r2 = np.array([4.0, 9.0])
    assert np.allclose(ta.qlike(r2, r2), 0.0)
    assert (ta.qlike(r2, r2 * 2) > 0).all() and (ta.qlike(r2, r2 / 2) > 0).all()
    np.testing.assert_allclose(ta.qlike(r2, r2 * 3), ta.qlike(r2 * 10, r2 * 30))    # invariance d'échelle
    # plancher : r² = 0 donne une valeur finie, égale à celle du plancher
    assert ta.qlike(np.array([0.0]), np.array([1.0]))[0] == pytest.approx(ta.qlike(np.array([ta.R2_FLOOR_BP2]), np.array([1.0]))[0])
    assert np.isnan(ta.qlike(np.array([1.0, 1.0]), np.array([0.0, np.nan]))).all()
    assert np.isnan(ta.qlike(np.array([np.nan]), np.array([1.0])))[0]


def test_mincer_zarnowitz_and_coverage():
    rng = np.random.default_rng(1)
    s2 = rng.uniform(1, 10, 500)
    r2 = 2.0 + 1.5 * s2 + rng.normal(0, 0.1, 500)
    mz = ta.mincer_zarnowitz(r2, s2)
    assert mz["a"] == pytest.approx(2.0, abs=0.05) and mz["b"] == pytest.approx(1.5, abs=0.02) and mz["r2"] > 0.99
    assert np.isnan(ta.mincer_zarnowitz(r2[:5], s2[:5])["b"])
    np.testing.assert_allclose(ta.coverage(np.array([0.0, 2.0, -3.0, np.nan]), -1.0, 1.0), [1.0, 0.0, 0.0, np.nan])
    # |r| ~ |N(0, 3)| : l'espérance σ√(2/π) est sans biais ; la MAE autour vaut ≈ 1,44 (< 2)
    r_abs = np.abs(rng.normal(0, 3, 20000))
    assert abs(r_abs.mean() - 3.0 * math.sqrt(2 / math.pi)) < 0.05
    assert np.nanmean(ta.mae_abs(r_abs, 3.0)) < 2.0
    assert ta.mse(np.array([1.0]), np.array([3.0]))[0] == 4.0


# -----------------------------------------------------------------------------
# 5. Volatilité réalisée causale, λ, encompassing
# -----------------------------------------------------------------------------
def test_ewma_causal_and_nan():
    r2 = np.array([1.0, np.nan, 4.0, 0.0])
    v = ta.ewma_variance(r2, 0.5)
    np.testing.assert_allclose(v, [1.0, 1.0, 2.5, 1.25])
    with pytest.raises(ValueError):
        ta.ewma_variance(r2, 1.0)


def test_realized_vol_estimators_causal(series):
    rv = ta.realized_vol_estimators(series, lam=0.94)
    for c in ("ret", "r2", "ewma", "rstd60", "rstd240", "park60", "gk60"):
        assert c in rv.columns
    t = 1000
    bumped = series.copy()
    bumped.iloc[t + 1:, :] = bumped.iloc[t + 1:, :] * 3.0
    rv2 = ta.realized_vol_estimators(bumped, lam=0.94)
    pd.testing.assert_frame_equal(rv.iloc[: t + 1], rv2.iloc[: t + 1])
    assert (rv.iloc[t + 1:]["ewma"] != rv2.iloc[t + 1:]["ewma"]).any()
    assert rv["ret"].iloc[5] == pytest.approx(math.log(series["close"].iloc[5] / series["close"].iloc[4]) * ta.BP)
    # rendement enjambant un trou de grille : NaN ; Parkinson ~ variance des rendements
    gap = series.drop(series.index[200:203])
    rvg = ta.realized_vol_estimators(gap, lam=0.94)
    assert np.isnan(rvg["ret"].loc[series.index[203]])
    assert 0.2 < rv["park60"].iloc[-1] / rv["rstd60"].iloc[-1] < 5.0


def test_choose_lambda_and_encompassing():
    rng = np.random.default_rng(3)
    n = 4000
    # variance à mémoire longue -> λ élevé préféré ; à mémoire courte -> λ bas
    v = np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    r2 = v * rng.chisquare(1, n)
    pos = np.arange(600, n - 20, 7)
    lam_long = ta.choose_ewma_lambda(r2, {1: (pos, r2[pos + 1])}, grid=(0.5, 0.9, 0.99))
    assert lam_long == 0.99
    # régimes de variance par blocs de 100 barres (400 puis 25 pb², loin du plancher) : une
    # mémoire courte (λ = 0,9, ≈ 10 barres) suit les blocs, λ = 0,99 (≈ 100 barres) les moyenne
    blocks = np.where((np.arange(n) // 100) % 2 == 0, 400.0, 25.0)
    r2_short = blocks * rng.chisquare(1, n)
    lam_short = ta.choose_ewma_lambda(r2_short, {1: (pos, r2_short[pos + 1])}, grid=(0.5, 0.9, 0.99))
    assert lam_short < 0.99
    s_a = rng.uniform(1, 5, 2000)
    s_b = rng.uniform(1, 5, 2000)
    y = 1.0 + 2.0 * s_a + 0.0 * s_b + rng.normal(0, 0.5, 2000)
    fit = ta.encompassing_fit(y, [s_a, s_b])
    assert fit["coef"][1] == pytest.approx(2.0, abs=0.05) and abs(fit["coef"][2]) < 0.05
    assert fit["t"][1] > 20 and abs(fit["t"][2]) < 3
    pred = ta.encompassing_predict(fit["coef"], [s_a, s_b])
    assert pred.shape == (2000,) and (pred > 0).all()
    # plancher : combinaison négative ramenée à 5 % de la moyenne des σ²
    assert ta.encompassing_predict(np.array([-100.0, 0.0, 0.0]), [np.array([2.0]), np.array([4.0])])[0] == pytest.approx(0.15)
    # combinaison QLIKE-optimale : r² ~ σ²_a × χ²(1) -> tout le poids sur σ²_a, ≈ 1
    s2_a = rng.uniform(1, 9, 3000)
    s2_b = rng.uniform(1, 9, 3000)
    r2 = s2_a * rng.chisquare(1, 3000)
    wq = ta.qlike_combination_fit(r2, [s2_a, s2_b])
    assert wq[0] == pytest.approx(1.0, abs=0.15) and wq[1] < 0.1
    with pytest.raises(ValueError):
        ta.qlike_combination_fit(r2[:5], [s2_a[:5], s2_b[:5]])


# -----------------------------------------------------------------------------
# 6. Quintiles, ECDF, Polymarket
# -----------------------------------------------------------------------------
def test_quintiles_and_ecdf():
    train = np.arange(1, 101, dtype=float)
    edges = ta.quintile_edges(train)
    assert edges.size == 4 and edges[0] == pytest.approx(20.8)
    q = ta.quintile_of(np.array([0.0, 21.0, 50.0, 100.0, 1e9, np.nan]), edges)
    assert q.tolist() == [1, 2, 3, 5, 5, -1]
    e = ta.train_ecdf(train, np.array([0.0, 50.0, 100.0, 200.0, np.nan]))
    np.testing.assert_allclose(e[:4], [0.0, 0.5, 1.0, 1.0])
    assert np.isnan(e[4])
    with pytest.raises(ValueError):
        ta.quintile_edges(np.array([1.0, 2.0]))


def test_market_price_at_and_origin():
    idx = pd.to_datetime([100, 160, 230], unit="s", utc=True)
    hist = pd.Series([0.5, 0.6, 0.7], index=idx)
    assert ta.market_price_at(hist, 165, 90) == (0.6, 5.0)
    assert ta.market_price_at(hist, 229, 90) == (0.6, 69.0)
    p, age = ta.market_price_at(hist, 300, 60)
    assert np.isnan(p) and age == 70.0                 # trop ancien
    assert np.isnan(ta.market_price_at(hist, 50, 90)[0])
    assert np.isnan(ta.market_price_at(None, 50, 90)[0])
    np.testing.assert_array_equal(ta.polymarket_origin(np.array([1_000_000_200])), [1_000_000_080])
    np.testing.assert_array_equal(ta.slot_of(np.array([900, 1799, 1800])), [1, 1, 2])
