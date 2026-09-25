"""Tests du walk-forward TimesFM et des métriques (src/tradebot/backtest.py).

Toujours avec un FAUX modèle injecté (aucun téléchargement) :

1. Grille des origines (``stride``, ``start``/``end``, ``max_origins``) et un seul appel
   au modèle par paquet, à ``horizon = max(horizons)``.
2. Alignement : dernière valeur du contexte = close[t], dernière colonne des
   covariables = barre t ; ``ret`` = ``targets.future_log_return``.
3. Absence de fuite : modifier les données après t ne change rien aux prévisions en t.
4. Trous : origines dont le contexte contient un trou > ``max_gap_bars`` écartées et
   comptées ; petits trous interpolés.
5. Métriques : prédicteur parfait -> accuracy 1 et PT très significatif ; PT codé à la
   main ≈ χ² d'indépendance ; cas dégénérés.
6. ``toy_strategy`` : trades non chevauchants, frais, côté short, filtres.
7. Baselines : momentum / reversal / always_up / majority_prev_day (causal).
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from tradebot.backtest import (BASELINE_P, PRED_COLUMNS, baseline_predictions, direction_metrics,
                               metrics_by_horizon, pesaran_timmermann, toy_strategy,
                               walk_forward_timesfm)
from tradebot.config import TimesFMConfig
from tradebot.forecaster import TimesFMForecaster, prepare_covariates
from tradebot.targets import future_log_return

Z9 = np.array([-1.2816, -0.8416, -0.5244, -0.2533, 0.0, 0.2533, 0.5244, 0.8416, 1.2816])


class FakeTimesFM3:
    """Faux ``predict_batch`` 3.0 : la sortie dépend du contexte et des covariables."""

    def __init__(self, drift: float = -0.5, cov_weight: float = 1e-4, scale: float = 5e-4):
        self.drift, self.cov_weight, self.scale = drift, cov_weight, scale
        self.calls: list[dict] = []

    def predict_batch(self, contexts, horizon, past_only_covariates=None, return_quantiles=False,
                      use_symmetric_averaging=False, **kwargs):
        self.calls.append(dict(contexts=[np.array(c) for c in contexts], horizon=horizon,
                               covariates=None if past_only_covariates is None
                               else [np.array(c) for c in past_only_covariates]))
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        for i, c in enumerate(contexts):
            off = self.drift * float(np.mean(np.asarray(c)[-5:]))
            if past_only_covariates is not None:
                off += self.cov_weight * float(np.mean(past_only_covariates[i][:, -1]))
            q = off + self.scale * steps * Z9[None, :]
            yield SimpleNamespace(forecast=q[:, 4], quantiles=q)


def make_df(n: int = 3000, seed: int = 0, start: str = "2026-01-01 00:00") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = 30_000.0 * np.exp(np.cumsum(rng.normal(0.0, 5e-4, n)))
    return pd.DataFrame({"close": close, "volume": rng.lognormal(1, 0.5, n)}, index=idx)


def make_forecaster(kind: str = "logprice", **fake_kw) -> tuple[TimesFMForecaster, FakeTimesFM3]:
    fake = FakeTimesFM3(**fake_kw)
    return TimesFMForecaster(TimesFMConfig(context_transform=kind), model=fake), fake


def flat_calls(fake: FakeTimesFM3, key: str) -> list[np.ndarray]:
    return [x for call in fake.calls for x in call[key]]


# -----------------------------------------------------------------------------
# Grille des origines
# -----------------------------------------------------------------------------


def test_origin_grid_stride_and_bounds():
    df = make_df(3000)
    fc, fake = make_forecaster()
    out = walk_forward_timesfm(df, fc, horizons=(5, 10, 15), context_len=256, stride=15)
    assert list(out.columns) == list(PRED_COLUMNS)
    times = pd.DatetimeIndex(out["time"].unique())
    # positions 255, 270, …, 2985 : minute multiple de 15 et au moins 256 barres d'historique
    assert len(times) == (2999 - 255) // 15 + 1 == out.attrs["n_origins"]
    assert times[0] == df.index[255]
    assert np.all(times.minute % 15 == 0)
    assert (times[1:] - times[:-1] == pd.Timedelta("15min")).all()
    assert len(out) == 3 * len(times)
    assert (out.groupby("time")["horizon"].apply(tuple) == (5, 10, 15)).all()
    # un seul appel par paquet, horizon = max(horizons)
    assert all(call["horizon"] == 15 for call in fake.calls)
    assert out.attrs["n_excluded_gaps"] == 0 and out.attrs["stride"] == 15

    fc2, _ = make_forecaster()
    sub = walk_forward_timesfm(df, fc2, context_len=256, stride=15,
                               start="2026-01-01 10:00", end=pd.Timestamp("2026-01-01 20:00", tz="UTC"))
    t = pd.DatetimeIndex(sub["time"].unique())
    assert t[0] == pd.Timestamp("2026-01-01 10:00", tz="UTC")
    assert t[-1] == pd.Timestamp("2026-01-01 20:00", tz="UTC")
    assert len(t) == 41


def test_max_origins_and_chunks():
    df = make_df(3000)
    fc, fake = make_forecaster()
    out = walk_forward_timesfm(df, fc, context_len=256, stride=5, max_origins=50, chunk_size=16)
    t = pd.DatetimeIndex(out["time"].unique())
    assert len(t) == 50 == out.attrs["n_origins"]
    assert t[0] == df.index[255] and t[-1] == df.index[2995]        # extrémités conservées
    assert (t[1:] - t[:-1] >= pd.Timedelta("5min")).all()
    assert len(fake.calls) == math.ceil(50 / 16)
    assert out.attrs["n_candidates"] == (2995 - 255) // 5 + 1


def test_no_origin_returns_empty_frame():
    df = make_df(200)
    fc, fake = make_forecaster()
    out = walk_forward_timesfm(df, fc, context_len=512)
    assert out.empty and list(out.columns) == list(PRED_COLUMNS)
    assert out.attrs["n_origins"] == 0 and not fake.calls


# -----------------------------------------------------------------------------
# Alignement
# -----------------------------------------------------------------------------


def test_alignment_context_covariates_and_targets():
    df = make_df(2000, seed=1)
    df.iloc[1500:1520, df.columns.get_loc("close")] = df["close"].iloc[1500]   # plat -> égalités
    rng = np.random.default_rng(2)
    cov = pd.DataFrame({"ramp": np.arange(len(df), dtype=float),
                        "noise": rng.normal(size=len(df))}, index=df.index)
    cov.iloc[:300, 1] = np.nan                                      # période d'initialisation
    L = 128
    fc, fake = make_forecaster(kind="price")
    out = walk_forward_timesfm(df, fc, horizons=(5, 15), context_len=L, stride=10, covariates=cov)
    times = pd.DatetimeIndex(out["time"].unique())
    ctxs, covs = flat_calls(fake, "contexts"), flat_calls(fake, "covariates")
    assert len(ctxs) == len(times) == len(covs)
    close = df["close"].to_numpy()
    for t, c, cv in zip(times, ctxs, covs):
        p = df.index.get_loc(t)
        # contexte = L dernières clôtures jusqu'à t inclus, centré sur close[t]
        np.testing.assert_array_equal(c, close[p - L + 1:p + 1] - close[p])
        # covariables jusqu'à t inclus : dernière colonne = barre t
        raw = cov.to_numpy().T
        np.testing.assert_array_equal(cv, prepare_covariates(raw[:, p - L + 1:p + 1]))
        if p - L + 1 >= 301 and p + 1 < len(df):      # bruit disponible : décalage détectable
            assert not np.allclose(cv, prepare_covariates(raw[:, p - L + 2:p + 2]))
            assert not np.allclose(cv, prepare_covariates(raw[:, p - L:p]))
    # lots homogènes : toutes les séries d'un appel ont leurs covariables
    for call in fake.calls:
        assert all(x is not None and x.shape == (2, L) for x in call["covariates"])

    idx = out.set_index(["time", "horizon"])
    for h in (5, 15):
        sub = idx.xs(h, level="horizon")
        np.testing.assert_array_equal(sub["last_close"].to_numpy(), df.loc[sub.index, "close"].to_numpy())
        fut = future_log_return(df, h).reindex(sub.index)
        np.testing.assert_allclose(sub["ret"].to_numpy(), fut.to_numpy(), rtol=0, atol=0)
        exp_y = np.where(fut > 0, 1.0, np.where(fut < 0, 0.0, np.nan))
        np.testing.assert_array_equal(sub["y_true"].to_numpy(), exp_y)
        if h == 15:                                                  # 1990 + 15 > 1999 : fin des données
            assert np.isnan(sub["ret"].iloc[-1])
        # médiane en prix cohérente avec le log-rendement prévu
        np.testing.assert_allclose(sub["pred_median"], sub["last_close"] * np.exp(sub["pred_ret"]))
        assert np.all(sub["q10_ret"] < sub["pred_ret"]) and np.all(sub["pred_ret"] < sub["q90_ret"])
    flat_origins = [t for t in times if 1500 <= df.index.get_loc(t) < 1505]
    assert flat_origins and all(np.isnan(idx.loc[(t, 5), "y_true"]) for t in flat_origins)


# -----------------------------------------------------------------------------
# Absence de fuite
# -----------------------------------------------------------------------------


def test_no_lookahead_leakage():
    df = make_df(2500, seed=3)
    rng = np.random.default_rng(4)
    cov = pd.DataFrame({"x": rng.normal(size=len(df)), "y": rng.normal(size=len(df))},
                       index=df.index)
    kw = dict(horizons=(5, 10, 15), context_len=256, stride=5, covariates=None)
    T = df.index[1800]

    df2, cov2 = df.copy(), cov.copy()
    after = df.index > T
    df2.loc[after, "close"] *= np.exp(rng.normal(0, 0.01, after.sum()))
    cov2.loc[after] = rng.normal(size=(after.sum(), 2)) * 5

    for covs in ((None, None), (cov, cov2)):
        kw["covariates"] = covs[0]
        a = walk_forward_timesfm(df, make_forecaster()[0], **kw)
        kw["covariates"] = covs[1]
        b = walk_forward_timesfm(df2, make_forecaster()[0], **kw)
        kw["covariates"] = None if covs[1] is None else covs[1].loc[:T]
        c = walk_forward_timesfm(df2.loc[:T], make_forecaster()[0], **kw)   # données tronquées
        cols = ["last_close", "pred_median", "p_up", "pred_ret", "q10_ret", "q90_ret"]
        ma, mb = a[a["time"] <= T], b[b["time"] <= T]
        pd.testing.assert_frame_equal(ma[["time", "horizon"] + cols].reset_index(drop=True),
                                      mb[["time", "horizon"] + cols].reset_index(drop=True))
        pd.testing.assert_frame_equal(ma[["time", "horizon"] + cols].reset_index(drop=True),
                                      c[["time", "horizon"] + cols].reset_index(drop=True))
        # la cible, elle, dépend du futur : identique seulement si t + h <= T
        known = (ma["time"] + pd.to_timedelta(ma["horizon"], unit="min")) <= T
        np.testing.assert_array_equal(ma.loc[known, "ret"].to_numpy(), mb.loc[known, "ret"].to_numpy())
        # contrôle de puissance : les prévisions après T, elles, ont changé
        assert not np.allclose(a.loc[a["time"] > T, "p_up"].to_numpy(),
                               b.loc[b["time"] > T, "p_up"].to_numpy())


# -----------------------------------------------------------------------------
# Trous de données
# -----------------------------------------------------------------------------


def test_gap_exclusion_and_small_gap_interpolation():
    df_full = make_df(3000, seed=5)
    big = df_full.index[1500:1510]                                    # trou de 10 barres
    small = df_full.index[2500:2502]                                  # trou de 2 barres
    df = df_full.drop(big.union(small))
    L, stride = 128, 5
    fc, fake = make_forecaster(kind="price")
    out = walk_forward_timesfm(df, fc, horizons=(5,), context_len=L, stride=stride, max_gap_bars=3)
    times = pd.DatetimeIndex(out["time"].unique())
    pos = np.array([df_full.index.get_loc(t) for t in times])        # positions sur la grille
    # origines dont la fenêtre [p-L+1, p] touche le grand trou : écartées et comptées
    touching = [p for p in range(1510, 1510 + L - 1) if p % stride == 0]
    assert out.attrs["n_excluded_gaps"] == len(touching) > 0
    assert not np.any((pos >= 1500) & (pos <= 1509 + L - 1))
    # petit trou : origines conservées, contexte complet et interpolé (sans NaN)
    kept = [i for i, p in enumerate(pos) if p - L + 1 <= 2500 and p >= 2502]
    assert kept
    ctxs = flat_calls(fake, "contexts")
    close = df_full["close"].to_numpy()
    for i in kept:
        c = ctxs[i]
        p = pos[i]
        assert c.shape == (L,) and np.isfinite(c).all()
        j = 2500 - (p - L + 1)                                        # indice du trou dans le contexte
        a, b = close[2499] - close[p], close[2502] - close[p]
        np.testing.assert_allclose(c[j:j + 2], [a + (b - a) / 3, a + 2 * (b - a) / 3])
    # la cible traverse le trou -> NaN
    t_cross = df_full.index[2495]
    assert np.isnan(out.loc[out["time"] == t_cross, "ret"]).all()

    out2 = walk_forward_timesfm(df, make_forecaster()[0], horizons=(5,), context_len=L,
                                stride=stride, max_gap_bars=1)
    assert out2.attrs["n_excluded_gaps"] > out.attrs["n_excluded_gaps"]


# -----------------------------------------------------------------------------
# Métriques
# -----------------------------------------------------------------------------


def test_direction_metrics_perfect_predictor():
    rng = np.random.default_rng(0)
    y = (rng.uniform(size=2000) < 0.5).astype(float)
    p = 0.1 + 0.8 * y
    m = direction_metrics(y, p, h=5)
    assert m["n"] == 2000 and m["n_eff"] == 400
    assert m["accuracy"] == 1.0 and m["balanced_accuracy"] == 1.0 and m["auc"] == 1.0
    assert m["brier"] == pytest.approx(0.01)
    assert m["log_loss"] == pytest.approx(-math.log(0.9))
    assert m["pt_stat"] > 40 and m["pt_pvalue"] < 1e-12
    assert m["p_binom"] < 1e-100
    assert m["excess_accuracy"] == pytest.approx(1 - max(y.mean(), 1 - y.mean()))
    for key in ("n", "accuracy", "balanced_accuracy", "auc", "brier", "log_loss", "p_binom",
                "pt_stat", "pt_pvalue"):
        assert key in m


def test_pesaran_timmermann_matches_chi2_independence():
    rng = np.random.default_rng(1)
    n = 20000
    y = (rng.uniform(size=n) < 0.55).astype(float)
    x = np.where(rng.uniform(size=n) < 0.6, y, 1 - y)               # prédicteur informatif
    pt, pval = pesaran_timmermann(y, x)
    table = np.array([[np.sum((x == a) & (y == b)) for b in (0, 1)] for a in (0, 1)])
    chi2 = stats.chi2_contingency(table, correction=False)[0]
    assert pt > 0 and pt ** 2 == pytest.approx(chi2, rel=1e-3)
    assert pval == pytest.approx(stats.norm.sf(pt))
    # prévision constante : test non défini
    assert all(np.isnan(v) for v in pesaran_timmermann(y, np.ones(n)))


def test_direction_metrics_random_constant_and_nan():
    rng = np.random.default_rng(2)
    y = (rng.uniform(size=3000) < 0.5).astype(float)
    m = direction_metrics(y, rng.uniform(size=3000), h=1)
    assert abs(m["accuracy"] - 0.5) < 0.04 and abs(m["pt_stat"]) < 3.5 and abs(m["auc"] - 0.5) < 0.04
    c = direction_metrics(y, np.full(3000, 0.6), h=15)
    assert c["balanced_accuracy"] == 0.5 and c["auc"] == 0.5 and np.isnan(c["pt_stat"])
    assert c["excess_accuracy"] <= 0
    yn = y.copy()
    yn[:100] = np.nan
    pn = np.full(3000, 0.7)
    pn[100:150] = np.nan
    assert direction_metrics(yn, pn, h=1)["n"] == 2850
    e = direction_metrics([], [], h=5)
    assert e["n"] == 0 and np.isnan(e["accuracy"])
    one = direction_metrics(np.ones(50), np.full(50, 0.7), h=1)
    assert one["accuracy"] == 1.0 and np.isnan(one["auc"])
    with pytest.raises(ValueError):
        direction_metrics([0.0, 2.0], [0.5, 0.5], h=1)


def test_metrics_by_horizon_uses_effective_overlap():
    df = make_df(3000, seed=7)
    out = walk_forward_timesfm(df, make_forecaster()[0], context_len=256, stride=5)
    res = metrics_by_horizon(out)
    assert list(res["horizon"]) == [5, 10, 15] and list(res["h_eff"]) == [1, 2, 3]
    assert (res["n_eff"] == res["n"] // res["h_eff"]).all()


# -----------------------------------------------------------------------------
# Mini-stratégie
# -----------------------------------------------------------------------------


def _preds(p, ret, h=5, start="2026-01-01", freq="1min"):
    t = pd.date_range(start, periods=len(p), freq=freq, tz="UTC")
    return pd.DataFrame({"time": t, "horizon": h, "p_up": p, "ret": ret})


def test_toy_strategy_non_overlapping_and_costs():
    n = 100
    long_ = toy_strategy(_preds(np.full(n, 0.9), np.full(n, 1e-3)), h=5, cost_bps=10.0)
    assert long_["n_trades"] == 20 and long_["n_long"] == 20 and long_["n_short"] == 0
    assert long_["n_signals"] == 100
    assert long_["gross_bps_mean"] == pytest.approx(10.0)
    assert long_["net_bps_mean"] == pytest.approx(0.0, abs=1e-9)
    assert long_["hit_rate"] == 1.0 and long_["net_bps_sum"] == pytest.approx(0.0, abs=1e-9)

    short = toy_strategy(_preds(np.full(n, 0.1), np.full(n, 1e-3)), h=5, cost_bps=4.0)
    assert short["n_short"] == 20 and short["gross_bps_mean"] == pytest.approx(-10.0)
    assert short["net_bps_mean"] == pytest.approx(-14.0) and short["hit_rate"] == 0.0

    none = toy_strategy(_preds(np.full(n, 0.54), np.full(n, 1e-3)), h=5, threshold=0.55)
    assert none["n_trades"] == 0 and np.isnan(none["net_bps_mean"])


def test_toy_strategy_skips_overlaps_nan_and_other_horizons():
    p = np.array([0.9, 0.5, 0.5, 0.2, 0.5, 0.1, 0.9, 0.9, 0.5, 0.5, 0.5, 0.8])
    r = np.array([2e-3, 0, 0, 1e-3, 0, -1e-3, np.nan, 5e-4, 0, 0, 0, 1e-3])
    pr = _preds(p, r)
    # minute 0 long (tenu jusqu'à 5) ; 3 ignorée (chevauche) ; 5 short ; 6 NaN ; 7 ignorée ;
    # 11 long (>= 10)
    other = _preds(np.full(12, 0.9), np.full(12, 1.0), h=15)          # autre horizon : ignoré
    res = toy_strategy(pd.concat([pr, other]), h=5, cost_bps=0.0)
    assert res["n_trades"] == 3 and res["n_long"] == 2 and res["n_short"] == 1
    assert res["gross_bps_sum"] == pytest.approx(20.0 + 10.0 + 10.0)
    # barres de 5 minutes : la détention dure 25 minutes
    p5 = _preds(np.full(10, 0.9), np.full(10, 1e-3), freq="5min")
    assert toy_strategy(p5, h=5, bar=pd.Timedelta("5min"))["n_trades"] == 2
    p5.attrs["bar"] = pd.Timedelta("5min")
    assert toy_strategy(p5, h=5)["n_trades"] == 2
    # colonne de probabilité alternative (ex. P recalibrée ou baseline)
    pr["p_cal"] = 0.1
    assert toy_strategy(pr, h=5, p_col="p_cal")["n_short"] > 0
    with pytest.raises(ValueError):
        toy_strategy(pr, h=5, threshold=0.4)


# -----------------------------------------------------------------------------
# Baselines
# -----------------------------------------------------------------------------


def test_baselines_momentum_reversal_always_up():
    df = make_df(3000, seed=8)
    origins = df.index[300:3000:7]
    b = baseline_predictions(df, origins, horizons=(5, 15))
    assert len(b) == 2 * len(origins)
    close = df["close"]
    for h in (5, 15):
        sub = b[b["horizon"] == h].set_index("time")
        past = np.log(close / close.shift(h)).reindex(sub.index)
        exp_mom = np.where(past > 0, BASELINE_P, np.where(past < 0, 1 - BASELINE_P, 0.5))
        np.testing.assert_allclose(sub["momentum_h"], exp_mom)
        np.testing.assert_allclose(sub["reversal_h"], 1 - exp_mom)
        assert (sub["always_up"] == BASELINE_P).all()
        np.testing.assert_allclose(sub["ret"], future_log_return(df, h).reindex(sub.index))
    # même format que le walk-forward : fusion directe sur (time, horizon)
    wf = walk_forward_timesfm(df, make_forecaster()[0], horizons=(5, 15), context_len=256, stride=15)
    merged = wf.merge(baseline_predictions(df, wf["time"].unique(), (5, 15)),
                      on=["time", "horizon"], suffixes=("", "_b"))
    assert len(merged) == len(wf)
    np.testing.assert_array_equal(merged["y_true"].to_numpy(), merged["y_true_b"].to_numpy())


def test_majority_prev_day_direction_and_causality():
    n = 3 * 1440
    idx = pd.date_range("2026-02-01", periods=n, freq="1min", tz="UTC")
    steps = np.r_[np.full(1440, 1e-4), np.full(1440, -1e-4), np.full(1440, 1e-4)]
    df = pd.DataFrame({"close": 100 * np.exp(np.cumsum(steps))}, index=idx)
    origins = idx[::60]
    b = baseline_predictions(df, origins, horizons=(5,)).set_index("time")
    day = b.index.floor("D")
    assert (b.loc[day == idx[0], "majority_prev_day"] == 0.5).all()          # pas de veille
    assert (b.loc[day == idx[1440], "majority_prev_day"] == BASELINE_P).all()      # veille en hausse
    assert (b.loc[day == idx[2880], "majority_prev_day"] == 1 - BASELINE_P).all()  # veille en baisse


def test_majority_prev_day_ignores_labels_crossing_midnight():
    """Veille plate (labels tous NaN) mais saut à minuit : les labels de fin de veille qui
    traversent minuit ne doivent pas servir (ils ne sont connus qu'après minuit)."""
    idx = pd.date_range("2026-02-01", periods=2 * 1440, freq="1min", tz="UTC")
    close = np.r_[np.full(1440, 100.0), 101.0 + np.arange(1440) * 1e-3]
    df = pd.DataFrame({"close": close}, index=idx)
    b = baseline_predictions(df, idx[1440::30], horizons=(15,))
    assert (b["majority_prev_day"] == 0.5).all()
    # et modifier le jour J après l'origine ne change pas la baseline du jour J
    df2 = df.copy()
    df2.iloc[1500:, 0] *= 0.9
    b2 = baseline_predictions(df2, idx[1440:1500:5], horizons=(15,))
    b1 = baseline_predictions(df, idx[1440:1500:5], horizons=(15,))
    np.testing.assert_array_equal(b1["majority_prev_day"], b2["majority_prev_day"])
    np.testing.assert_array_equal(b1["momentum_h"], b2["momentum_h"])


def test_pt_hac_needs_both_predicted_classes():
    """Deux « hausse » justes sur 98 origines ne doivent pas donner un t HAC géant
    (cas réel : accuracy 46 %, t ≈ 10 avant la garde PT_HAC_MIN_CLASS)."""
    from tradebot.backtest import PT_HAC_MIN_CLASS

    rng = np.random.default_rng(3)
    y = (rng.uniform(size=98) < 0.55).astype(float)
    p = np.full(98, 0.45)
    up = np.flatnonzero(y == 1.0)[:2]
    p[up] = 0.8                                    # 2 prévisions « hausse », toutes deux justes
    m = direction_metrics(y, p, 1)
    assert np.isnan(m["pt_hac_t"]) and np.isnan(m["pt_hac_pvalue"])
    assert np.isfinite(m["accuracy"])
    # au-delà du seuil, le t HAC est calculé
    p2 = np.where(rng.uniform(size=98) < 0.5, 0.6, 0.4)
    assert min((p2 > 0.5).sum(), (p2 <= 0.5).sum()) >= PT_HAC_MIN_CLASS
    assert np.isfinite(direction_metrics(y, p2, 1)["pt_hac_t"])
