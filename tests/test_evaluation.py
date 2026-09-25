"""Tests de src/tradebot/evaluation.py.

1. Signal = rendement futur bruité : IC fortement positif, hit-rate OOS > 0,6.
2. Bruit pur : rien de significatif ; taille du test Newey-West sous chevauchement.
3. Signal anti-corrélé : train_sign = -1 et hit-rate OOS > 0,5.
4. Exactitude : IC, IC par jour, IC apprentissage / test, AUC, hit-rate et t de
   Newey-West comparés à scipy / sklearn / statsmodels (rangs calculés « à la main »).
5. Purge : split chronologique de ``indicator_scores`` et plis du walk-forward.
6. Benjamini-Hochberg (q >= p, égal à statsmodels), Stouffer, agrégation entre actifs.
7. Modèles combinés (logit, hgb) et ``direction_summary``.
8. Cas dégénérés : NaN, jamais d'exception.
9. Performance : 200 000 lignes x 10 indicateurs x 3 horizons en moins de 20 s.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
import pytest
import statsmodels.api as sm
from scipy import stats
from sklearn.metrics import balanced_accuracy_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from statsmodels.stats.multitest import multipletests

from tradebot import evaluation as ev
from tradebot.evaluation import (
    AGGREGATE_COLUMNS,
    SCORE_COLUMNS,
    SUMMARY_COLUMNS,
    aggregate_scores,
    benjamini_hochberg,
    combined_model_walkforward,
    direction_summary,
    indicator_scores,
    newey_west_tstat,
    purged_walk_forward_splits,
    stouffer,
)
from tradebot.targets import make_targets

HORIZONS = (5, 10, 15)


# -----------------------------------------------------------------------------
# Données synthétiques
# -----------------------------------------------------------------------------


def make_market(n: int, seed: int = 0, start: str = "2026-03-02 00:00", gap: bool = False,
                flat_frac: float = 0.01) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Barres 1 minute UTC (close seul) et cibles ret_h / dir_h.

    ``flat_frac`` de barres plates crée des rendements nuls (dir_h NaN, ex-aequo) ;
    ``gap`` retire 10 minutes en milieu de série (cibles NaN autour du trou).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    r = rng.normal(0.0, 1e-3, n)
    r[rng.random(n) < flat_frac] = 0.0
    df = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(r))}, index=idx)
    if gap:
        df = df.drop(df.index[n // 2: n // 2 + 10])
    return df, make_targets(df, HORIZONS)


def make_features(tg: pd.DataFrame, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(tg)
    other = np.cumsum(rng.normal(0.0, 1e-3, n))          # marche aléatoire indépendante
    partial = tg["ret_5"].to_numpy() + rng.normal(0.0, 2e-3, n)
    partial[:500] = np.nan
    partial[rng.random(n) < 0.05] = np.nan
    return pd.DataFrame({
        "signal": tg["ret_5"].to_numpy() + rng.normal(0.0, 2e-3, n),
        "anti": -tg["ret_10"].to_numpy() + rng.normal(0.0, 3e-3, n),
        "noise": rng.normal(size=n),
        "mom_other": other - np.r_[np.full(15, np.nan), other[:-15]],   # chevauchant, inerte
        "disc": np.round(rng.normal(size=n)),                        # beaucoup d'ex-aequo
        "partial": partial,
    }, index=tg.index)


@pytest.fixture(scope="module")
def market():
    df, tg = make_market(12 * 1440, seed=0, gap=True)
    feat = make_features(tg, seed=1)
    return df, tg, feat, indicator_scores(feat, tg, HORIZONS, 0.6)


def _row(scores: pd.DataFrame, name: str, h: int) -> pd.Series:
    sel = scores[(scores["indicator"] == name) & (scores["horizon"] == h)]
    assert len(sel) == 1
    return sel.iloc[0]


# -----------------------------------------------------------------------------
# 1-3. Signal, bruit, anti-corrélation
# -----------------------------------------------------------------------------


def test_output_shape_and_columns(market):
    _, _, feat, s = market
    assert list(s.columns) == list(SCORE_COLUMNS)
    assert len(s) == feat.shape[1] * len(HORIZONS)
    assert list(s["indicator"].unique()) == list(feat.columns)
    assert s["horizon"].tolist() == list(HORIZONS) * feat.shape[1]
    np.testing.assert_allclose(s["n_eff_oos"], s["n_oos"] / s["horizon"])


def test_signal_detected(market):
    _, _, _, s = market
    for h in HORIZONS:
        r = _row(s, "signal", h)
        assert r["ic_spearman"] > 0.3
        assert r["ic_daily_mean"] > 0.3 and r["ic_daily_t"] > 10
        assert r["n_days"] >= 10
        assert r["frac_months_same_sign"] == 1.0
        assert r["ic_nw_t"] > 10 and r["ic_nw_t_2h"] > 10 and r["t_cons"] > 10
        assert r["p_value"] < 1e-10
        assert r["ic_train"] > 0.3 and r["ic_test"] > 0.3
        assert r["train_sign"] == 1.0
        assert r["hit_rate_oos"] > 0.6
        assert r["auc_oos"] > 0.6
        assert r["p_hit"] < 1e-6
    # la force décroît quand l'horizon s'éloigne de celui du signal (h = 5)
    assert _row(s, "signal", 5)["ic_spearman"] > _row(s, "signal", 15)["ic_spearman"]
    assert _row(s, "partial", 5)["ic_spearman"] > 0.5


def test_noise_not_significant(market):
    _, _, _, s = market
    for name in ("noise", "mom_other", "disc"):
        for h in HORIZONS:
            r = _row(s, name, h)
            assert abs(r["ic_spearman"]) < 0.03
            assert abs(r["ic_nw_t"]) < 3.0 and r["p_value"] > 1e-3
            assert abs(r["t_cons"]) < 3.0
            assert 0.45 < r["hit_rate_oos"] < 0.55
            assert 0.45 < r["auc_oos"] < 0.55
            assert r["p_hit"] > 1e-3


def test_anti_correlated_signal(market):
    _, _, _, s = market
    for h in HORIZONS:
        r = _row(s, "anti", h)
        assert r["ic_spearman"] < -0.3 and r["ic_nw_t"] < -10
        assert r["train_sign"] == -1.0
        assert r["hit_rate_oos"] > 0.6          # le signe appris retourne l'indicateur
        assert r["auc_oos"] > 0.6
        assert r["p_hit"] < 1e-6


def test_newey_west_size_under_overlap():
    """Sous H0, avec un indicateur et une cible chevauchants, le test NW(lags=h) garde
    un taux de rejet proche de 5 % alors que le t naïf (ic * sqrt(n)) rejette bien plus."""
    h, n, sims = 10, 3000, 150
    rej_nw = rej_naive = 0
    for k in range(sims):
        df, tg = make_market(n, seed=1000 + k, flat_frac=0.0)
        rng = np.random.default_rng(5000 + k)
        other = np.cumsum(rng.normal(size=n))
        mom = pd.Series(other - np.r_[np.full(h, np.nan), other[:-h]], index=tg.index)
        s = indicator_scores(pd.DataFrame({"mom": mom}), tg, (h,), 0.6)
        r = s.iloc[0]
        rej_nw += abs(r["ic_nw_t"]) > 1.96
        rej_naive += abs(r["ic_spearman"]) * math.sqrt(r["n"]) > 1.96
    assert rej_nw / sims < 0.12
    assert rej_naive / sims > 0.2


# -----------------------------------------------------------------------------
# 4. Exactitude contre les implémentations de référence
# -----------------------------------------------------------------------------


def _std_ranks(v: np.ndarray) -> np.ndarray:
    r = stats.rankdata(v)
    return (r - r.mean()) / r.std()


@pytest.mark.parametrize("name", ["signal", "disc", "partial", "anti"])
@pytest.mark.parametrize("h", [5, 15])
def test_matches_reference(market, name, h):
    _, tg, feat, s = market
    got = _row(s, name, h)
    x = feat[name].to_numpy()
    r = tg[f"ret_{h}"].to_numpy()
    d = tg[f"dir_{h}"].to_numpy()
    n = len(feat)
    pos = np.arange(n)
    V = np.isfinite(x) & np.isfinite(r)
    assert got["n"] == V.sum()

    # IC global et Newey-West (statsmodels sur les rangs standardisés)
    np.testing.assert_allclose(got["ic_spearman"], stats.spearmanr(x[V], r[V]).statistic,
                               rtol=1e-10, atol=1e-12)
    za, zb = _std_ranks(x[V]), _std_ranks(r[V])
    for col, lags in (("ic_nw_t", h), ("ic_nw_t_2h", 2 * h)):
        res = sm.OLS(zb, za).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
        np.testing.assert_allclose(got[col], res.tvalues[0], rtol=1e-6)
        np.testing.assert_allclose(res.params[0], got["ic_spearman"], rtol=1e-9)
    np.testing.assert_allclose(got["p_value"], 2 * stats.norm.sf(abs(got["ic_nw_t"])), rtol=1e-12)

    # IC par jour UTC (jours d'au moins 100 observations)
    frame = pd.DataFrame({"x": x, "r": r}, index=feat.index)[V]
    daily = frame.groupby(frame.index.normalize()).apply(
        lambda g: g["x"].corr(g["r"], method="spearman") if len(g) >= 100 else np.nan).dropna()
    assert got["n_days"] == len(daily)
    np.testing.assert_allclose(got["ic_daily_mean"], daily.mean(), rtol=1e-10)
    np.testing.assert_allclose(got["ic_daily_t"],
                               daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))),
                               rtol=1e-9)

    # split chronologique avec purge de h lignes
    split = int(round(0.6 * n))
    tr = V & (pos < split - h)
    te = V & (pos >= split)
    np.testing.assert_allclose(got["ic_train"], stats.spearmanr(x[tr], r[tr]).statistic,
                               rtol=1e-10)
    np.testing.assert_allclose(got["ic_test"], stats.spearmanr(x[te], r[te]).statistic,
                               rtol=1e-10)
    med = np.median(x[tr])
    assert got["median_train"] == pytest.approx(med)
    sign = np.sign(got["ic_train"])
    assert got["train_sign"] == sign
    oo = te & np.isfinite(d)
    assert got["n_oos"] == oo.sum()
    pred_up = (x[oo] - med) * sign > 0
    hit = np.mean(pred_up == (d[oo] == 1))
    np.testing.assert_allclose(got["hit_rate_oos"], hit, rtol=1e-12)
    np.testing.assert_allclose(got["auc_oos"], roc_auc_score(d[oo], sign * x[oo]), rtol=1e-10)
    n_eff = int(oo.sum() // h)
    p_hit = stats.binomtest(int(round(hit * n_eff)), n_eff, 0.5, alternative="greater").pvalue
    np.testing.assert_allclose(got["p_hit"], p_hit, rtol=1e-9)
    assert got["up_rate_oos"] == pytest.approx(np.mean(d[oo] == 1))


def test_newey_west_matches_statsmodels():
    rng = np.random.default_rng(3)
    n = 4000
    e = rng.normal(size=(n, 2))
    x = np.empty(n)
    y = np.empty(n)
    x[0], y[0] = e[0]
    for t in range(1, n):          # AR(1) : erreurs autocorrélées
        x[t] = 0.7 * x[t - 1] + e[t, 0]
        y[t] = 0.1 * x[t] + 0.6 * y[t - 1] + e[t, 1]
    for lags in (0, 1, 5, 15, 30):
        ref = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": lags}).tvalues[0]
        assert newey_west_tstat(y, x, lags) == pytest.approx(ref, rel=1e-6, abs=1e-6)
    # paires non finies retirées, cas dégénérés -> NaN
    xn = x.copy()
    xn[:10] = np.nan
    ref = sm.OLS(y[10:], x[10:]).fit(cov_type="HAC", cov_kwds={"maxlags": 5}).tvalues[0]
    assert newey_west_tstat(y, xn, 5) == pytest.approx(ref, rel=1e-6)
    assert np.isnan(newey_west_tstat(y, np.zeros(n), 5))
    assert np.isnan(newey_west_tstat([1.0], [1.0], 5))


def test_session_column_defines_days():
    df, tg = make_market(5 * 1440, seed=4)
    feat = make_features(tg, seed=5)[["signal"]]
    # jours décalés de 12 h : 6 « sessions » dont 2 demi-journées
    sess = (feat.index - pd.Timedelta(hours=12)).date
    s_idx = indicator_scores(feat, tg, (5,))
    s_ses = indicator_scores(feat.assign(session=sess), tg, (5,))
    assert s_idx.iloc[0]["n_days"] == 5
    assert s_ses.iloc[0]["n_days"] == 6
    assert list(s_ses["indicator"]) == ["signal"]           # « session » n'est pas un indicateur
    s_tg = indicator_scores(feat, tg.assign(session=sess), (5,))
    assert s_tg.iloc[0]["ic_daily_t"] == pytest.approx(s_ses.iloc[0]["ic_daily_t"])


# -----------------------------------------------------------------------------
# 5. Purge
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("h", [5, 15])
def test_indicator_scores_purges_last_h_train_rows(h):
    df, tg = make_market(3000, seed=6, flat_frac=0.0)
    n = len(tg)
    split = int(round(0.6 * n))
    x = pd.DataFrame({"sig": tg[f"ret_{h}"] + np.random.default_rng(0).normal(0, 1e-3, n)},
                     index=tg.index)
    # Cibles valides seulement dans la zone purgée [split - h, n) : l'apprentissage est vide.
    tg1 = tg.copy()
    tg1.iloc[: split - h] = np.nan
    r = indicator_scores(x, tg1, (h,)).iloc[0]
    assert np.isnan(r["ic_train"]) and np.isnan(r["train_sign"]) and np.isnan(r["hit_rate_oos"])
    assert r["ic_test"] > 0.3 and r["n_oos"] > 0
    assert indicator_scores(x, tg1, (h,)).attrs["split_row"] == split
    # Trois lignes de plus avant la zone purgée : l'apprentissage en compte exactement trois.
    tg2 = tg.copy()
    tg2.iloc[: split - h - 3] = np.nan
    r2 = indicator_scores(x, tg2, (h,)).iloc[0]
    assert np.isfinite(r2["ic_train"])
    xv = x["sig"].to_numpy()[split - h - 3: split - h]
    assert r2["median_train"] == pytest.approx(np.median(xv))


@pytest.mark.parametrize("row_step", [1, 3])
@pytest.mark.parametrize("h", [5, 15])
def test_purged_walk_forward_splits(row_step, h):
    n, k = 1000, 5
    folds = list(purged_walk_forward_splits(n, k, h, row_step))
    assert len(folds) == k
    prev_size = -1
    tests = []
    for tr, te in folds:
        assert tr.size > prev_size                      # fenêtre croissante
        prev_size = tr.size
        assert tr[0] == 0 and (np.diff(te) == row_step).all()
        assert tr.max() + h < te.min()                  # purge : pas de cible chevauchante
        assert te.min() - h - row_step <= tr.max()      # ... et pas davantage que nécessaire
        assert (tr % row_step == 0).all() and (te % row_step == 0).all()
        tests.append(te)
    allte = np.concatenate(tests)
    assert (np.diff(allte) == row_step).all()           # plis de test contigus
    assert allte[-1] >= n - row_step
    if row_step == 1:
        ref = list(TimeSeriesSplit(n_splits=k, gap=h).split(np.arange(n)))
        for (tr, te), (rtr, rte) in zip(folds, ref):
            np.testing.assert_array_equal(tr, rtr)
            np.testing.assert_array_equal(te, rte)


# -----------------------------------------------------------------------------
# 6. Tests multiples, Stouffer, agrégation
# -----------------------------------------------------------------------------


def test_benjamini_hochberg():
    rng = np.random.default_rng(7)
    p = np.r_[rng.uniform(size=200), rng.uniform(0, 1e-4, 20), [np.nan, np.nan]]
    q = benjamini_hochberg(p)
    ok = np.isfinite(p)
    assert np.isnan(q[~ok]).all()
    assert (q[ok] >= p[ok] - 1e-15).all() and (q[ok] <= 1).all()
    np.testing.assert_allclose(q[ok], multipletests(p[ok], method="fdr_bh")[1], rtol=1e-12)
    qby = benjamini_hochberg(p, "by")
    np.testing.assert_allclose(qby[ok], multipletests(p[ok], method="fdr_by")[1], rtol=1e-12)
    assert (qby[ok] >= q[ok]).all()
    order = np.argsort(p[ok])
    assert (np.diff(q[ok][order]) >= -1e-15).all()      # monotone en p
    assert np.isnan(benjamini_hochberg([np.nan])).all()
    assert benjamini_hochberg([]).size == 0
    with pytest.raises(ValueError):
        benjamini_hochberg([0.1], method="xx")


def test_stouffer():
    z, p = stouffer([2.0, 2.0, np.nan, 2.0])
    assert z == pytest.approx(6 / math.sqrt(3))
    assert p == pytest.approx(2 * stats.norm.sf(6 / math.sqrt(3)))
    z, p = stouffer([3.0, -3.0])
    assert z == 0 and p == pytest.approx(1.0)
    assert all(np.isnan(stouffer([np.nan])))


@pytest.fixture(scope="module")
def per_ticker():
    out = {}
    for k, t in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")):
        _, tg = make_market(6 * 1440, seed=20 + k)
        feat = make_features(tg, seed=40 + k).rename(columns={"disc": "rsi_14"})
        out[t] = indicator_scores(feat, tg, HORIZONS)
    return out


def test_aggregate_scores(per_ticker):
    fam = {"signal": "test", "anti": "test"}
    agg = aggregate_scores(per_ticker, families=fam)
    assert list(agg.columns) == list(AGGREGATE_COLUMNS)
    assert len(agg) == 6 * len(HORIZONS)
    assert (agg["n_tickers"] == 3).all()
    # tri par |ic_mean| décroissant
    assert (np.diff(agg["ic_mean"].abs().to_numpy()) <= 1e-15).all()
    assert agg.iloc[0]["indicator"] in ("signal", "anti", "partial")
    # q >= p, BY >= BH
    assert (agg["q_value"] >= agg["p_combined"] - 1e-15).all()
    assert (agg["q_value_by"] >= agg["q_value"] - 1e-15).all()
    sig = agg[(agg["indicator"] == "signal") & (agg["horizon"] == 5)].iloc[0]
    anti = agg[(agg["indicator"] == "anti") & (agg["horizon"] == 10)].iloc[0]
    assert sig["frac_same_sign"] == 1.0 and anti["frac_same_sign"] == 1.0
    assert sig["ic_mean"] > 0.3 and anti["ic_mean"] < -0.3 and sig["q_value"] < 1e-10
    assert sig["family"] == "test" and pd.isna(
        agg[agg["indicator"] == "noise"].iloc[0]["family"])
    # valeurs recalculées à la main
    rows = pd.concat([df.assign(t=t) for t, df in per_ticker.items()])
    one = rows[(rows["indicator"] == "noise") & (rows["horizon"] == 10)]
    got = agg[(agg["indicator"] == "noise") & (agg["horizon"] == 10)].iloc[0]
    ic = one["ic_spearman"].to_numpy()
    assert got["ic_mean"] == pytest.approx(ic.mean())
    assert got["ic_std"] == pytest.approx(ic.std(ddof=1))
    assert got["ic_t_cross"] == pytest.approx(ic.mean() / (ic.std(ddof=1) / math.sqrt(3)))
    assert got["frac_same_sign"] == pytest.approx(np.mean(np.sign(ic) == np.sign(ic.mean())))
    for src, dst in (("ic_train", "ic_train_mean"), ("ic_test", "ic_test_mean"),
                     ("hit_rate_oos", "hit_rate_oos_mean"), ("auc_oos", "auc_oos_mean"),
                     ("ic_daily_t", "ic_daily_t_mean")):
        assert got[dst] == pytest.approx(one[src].mean())
    z = one["t_cons"].sum() / math.sqrt(3)
    assert got["z_combined"] == pytest.approx(z)
    assert got["p_combined"] == pytest.approx(2 * stats.norm.sf(abs(z)))
    np.testing.assert_allclose(agg["q_value"], multipletests(agg["p_combined"], method="fdr_bh")[1])
    # famille par défaut : registre des indicateurs
    agg_default = aggregate_scores(per_ticker)
    assert agg_default.set_index(["indicator", "horizon"]).loc[("rsi_14", 5), "family"] == "momentum"


def test_aggregate_degenerate(per_ticker):
    empty = aggregate_scores({})
    assert list(empty.columns) == list(AGGREGATE_COLUMNS) and len(empty) == 0
    assert len(aggregate_scores({"X": pd.DataFrame(columns=list(SCORE_COLUMNS))})) == 0
    one = aggregate_scores({"AAAUSDT": per_ticker["AAAUSDT"]})
    assert (one["n_tickers"] == 1).all()
    assert one["ic_std"].isna().all() and one["ic_t_cross"].isna().all()
    # colonne entièrement NaN chez tous les actifs
    bad = {t: df.assign(ic_spearman=np.nan, t_cons=np.nan, ic_nw_t=np.nan)
           for t, df in per_ticker.items()}
    agg = aggregate_scores(bad)
    assert (agg["n_tickers"] == 0).all() and agg["p_combined"].isna().all()
    assert agg["q_value"].isna().all() and agg["frac_same_sign"].isna().all()


# -----------------------------------------------------------------------------
# 7. Modèles combinés
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_market():
    df, tg = make_market(8000, seed=11, gap=True)
    rng = np.random.default_rng(12)
    n = len(tg)
    feat = pd.DataFrame({
        "sig5": tg["ret_5"].to_numpy() + rng.normal(0, 2e-3, n),
        "sig15": tg["ret_15"].to_numpy() + rng.normal(0, 4e-3, n),
        "noise": rng.normal(size=n),
        "half_nan": np.where(rng.random(n) < 0.3, np.nan, rng.normal(size=n)),
        "late": np.r_[np.full(n // 2, np.nan), rng.normal(size=n - n // 2)],
    }, index=tg.index)
    return tg, feat


@pytest.mark.parametrize("model,min_acc", [("logit", 0.62), ("hgb", 0.58)])
def test_combined_model_walkforward(small_market, model, min_acc):
    tg, feat = small_market
    oos = combined_model_walkforward(feat, tg, HORIZONS, n_splits=5, model=model)
    n = len(feat)
    assert list(oos.columns) == ["fold"] + [f"{k}_{h}" for h in HORIZONS for k in ("p_up", "y", "ret")]
    # index OOS = dernières n_splits/(n_splits+1) lignes, contiguës, dans l'ordre
    ref = list(TimeSeriesSplit(n_splits=5).split(np.arange(n)))
    te_all = np.concatenate([te for _, te in ref])
    assert oos.index.equals(feat.index[te_all])
    assert sorted(oos["fold"].unique()) == list(range(5))
    for h in HORIZONS:
        p = oos[f"p_up_{h}"]
        assert p.notna().all() and ((p >= 0) & (p <= 1)).all()
        np.testing.assert_array_equal(oos[f"y_{h}"].to_numpy(), tg[f"dir_{h}"].to_numpy()[te_all])
    # purge vérifiée sur l'index : la dernière origine d'apprentissage + h < début du test
    folds = oos.attrs["folds"]
    assert len(folds) == 5 * len(HORIZONS)
    for f in folds:
        end = feat.index.get_loc(f["train_end"])
        start = feat.index.get_loc(f["test_start"])
        assert end == f["train_end_row"] and start == f["test_start_row"]
        assert end + f["horizon"] < start
        assert f["n_train"] > 0
    summ = direction_summary(oos, HORIZONS)
    assert list(summ.columns) == list(SUMMARY_COLUMNS)
    r5 = summ.set_index("horizon").loc[5]
    assert r5["accuracy"] > min_acc and r5["auc"] > min_acc and r5["balanced_accuracy"] > min_acc - 0.01
    assert r5["brier"] < 0.25 and r5["bss"] > 0 and r5["p_binom"] < 1e-3
    assert r5["n_eff"] == pytest.approx(r5["n"] / 5)
    assert r5["acc_w"] > 0.6


def test_combined_model_subsampling(small_market):
    tg, feat = small_market
    oos = combined_model_walkforward(feat, tg, (5, 15), n_splits=4, model="logit",
                                     max_train_rows=500, row_step=2)
    assert oos.attrs["row_step"] == 2
    steps = np.diff(feat.index.get_indexer(oos.index))
    assert (steps == 2).all()
    for f in oos.attrs["folds"]:
        assert 0 < f["n_train"] <= 500
        assert f["train_end_row"] % 2 == 0
        assert f["train_end_row"] + f["horizon"] < f["test_start_row"]
    summ = direction_summary(oos, (5, 15)).set_index("horizon")
    assert summ.loc[5, "n_eff"] == pytest.approx(summ.loc[5, "n"] / 3)     # ceil(5/2)
    assert summ.loc[15, "n_eff"] == pytest.approx(summ.loc[15, "n"] / 8)   # ceil(15/2)
    assert summ.loc[5, "accuracy"] > 0.6
    with pytest.raises(ValueError):
        combined_model_walkforward(feat, tg, (5,), model="rf")


def test_direction_summary_reference():
    rng = np.random.default_rng(13)
    n = 500
    y = (rng.random(n) < 0.45).astype(float)
    p = np.clip(0.5 + 0.2 * (y - 0.5) + rng.normal(0, 0.15, n), 0.01, 0.99)
    ret = np.where(y == 1, 1, -1) * rng.exponential(1e-3, n)
    y[:5] = np.nan
    df = pd.DataFrame({"p_up_5": p, "y_5": y, "ret_5": ret})
    got = direction_summary(df, (5,)).iloc[0]
    ok = np.isfinite(y)
    yy, pp = y[ok], p[ok]
    assert got["n"] == ok.sum() and got["n_eff"] == pytest.approx(ok.sum() / 5)
    assert got["accuracy"] == pytest.approx(np.mean((pp > 0.5) == (yy == 1)))
    assert got["balanced_accuracy"] == pytest.approx(balanced_accuracy_score(yy, pp > 0.5))
    assert got["auc"] == pytest.approx(roc_auc_score(yy, pp))
    assert got["brier"] == pytest.approx(brier_score_loss(yy, pp))
    assert got["up_rate"] == pytest.approx(yy.mean())
    assert got["bss"] == pytest.approx(1 - got["brier"] / (yy.mean() * (1 - yy.mean())))
    rr = ret[ok]
    right = (pp > 0.5) == (rr > 0)
    assert got["acc_w"] == pytest.approx(np.abs(rr)[right].sum() / np.abs(rr).sum())
    # une seule classe, colonnes absentes, cadre vide
    one = direction_summary(pd.DataFrame({"p_up_5": [0.6, 0.7], "y_5": [1.0, 1.0]}), (5,)).iloc[0]
    assert one["accuracy"] == 1.0 and np.isnan(one["auc"]) and np.isnan(one["balanced_accuracy"])
    miss = direction_summary(pd.DataFrame({"x": [1.0]}), (5, 10))
    assert (miss["n"] == 0).all() and miss["accuracy"].isna().all()


# -----------------------------------------------------------------------------
# 8. Cas dégénérés
# -----------------------------------------------------------------------------


def test_degenerate_columns_give_nan():
    df, tg = make_market(3 * 1440, seed=14)
    n = len(tg)
    feat = pd.DataFrame({
        "const": np.ones(n),
        "all_nan": np.full(n, np.nan),
        "inf": np.where(np.arange(n) % 2 == 0, np.inf, 1.0),
        "few": np.r_[np.full(n - 2, np.nan), [1.0, 2.0]],
        "binary": (np.arange(n) % 7 == 0).astype(float),
        "flag": np.arange(n) % 3 == 0,                       # booléen -> 0/1
        "label": ["a"] * n,                                  # non numérique : ignoré
    }, index=tg.index)
    s = indicator_scores(feat, tg, HORIZONS)
    assert set(s["indicator"]) == {"const", "all_nan", "inf", "few", "binary", "flag"}
    for name in ("const", "all_nan", "inf", "few"):
        sub = s[s["indicator"] == name]
        for col in ("ic_spearman", "ic_daily_mean", "ic_nw_t", "p_value", "ic_train",
                    "ic_test", "train_sign", "hit_rate_oos", "auc_oos", "p_hit"):
            assert sub[col].isna().all(), (name, col)
    assert (s.loc[s["indicator"] == "all_nan", "n"] == 0).all()
    assert np.isfinite(s.loc[s["indicator"] == "binary", "ic_spearman"]).all()
    assert np.isfinite(s.loc[s["indicator"] == "flag", "ic_spearman"]).all()


def test_degenerate_frames():
    df, tg = make_market(50, seed=15)
    feat = pd.DataFrame({"a": np.arange(50.0)}, index=tg.index)
    # très peu de lignes : pas d'IC journalier (jours < 100), mais pas d'exception
    s = indicator_scores(feat, tg, HORIZONS)
    assert (s["n_days"] == 0).all() and s["ic_daily_mean"].isna().all()
    # cadre vide : une ligne NaN par (indicateur, horizon) ; sans colonne numérique / sans horizon
    s0 = indicator_scores(feat.iloc[:0], tg.iloc[:0], HORIZONS)
    assert len(s0) == 3 and (s0["n"] == 0).all() and s0["ic_spearman"].isna().all()
    none = indicator_scores(pd.DataFrame({"s": ["x"] * 50}, index=tg.index), tg)
    assert list(none.columns) == list(SCORE_COLUMNS) and len(none) == 0
    assert len(indicator_scores(feat, tg, ())) == 0
    # aucune ligne commune (cibles toutes NaN)
    s = indicator_scores(feat, tg * np.nan, HORIZONS)
    assert (s["n"] == 0).all() and s["ic_spearman"].isna().all()
    # index non temporel : pas d'IC journalier, le reste fonctionne
    _, tg2 = make_market(3000, seed=16)
    f2 = pd.DataFrame({"sig": tg2["ret_5"].to_numpy()}).reset_index(drop=True)
    s2 = indicator_scores(f2, tg2.reset_index(drop=True), (5,))
    assert s2.iloc[0]["n_days"] == 0 and s2.iloc[0]["ic_spearman"] > 0.9
    # index désaligné : targets réalignées sur features
    s3 = indicator_scores(feat.iloc[10:], tg, (5,))
    assert s3.iloc[0]["n"] == np.isfinite(tg["ret_5"].iloc[10:]).sum()
    # dir_h absente : déduite du signe de ret_h
    s4 = indicator_scores(feat, tg[["ret_5"]], (5,))
    assert s4.iloc[0]["n_oos"] > 0
    # erreurs d'usage explicites
    with pytest.raises(KeyError):
        indicator_scores(feat, tg[["dir_5"]], (5,))
    with pytest.raises(ValueError):
        indicator_scores(feat, tg, (5,), train_frac=1.0)


def test_degenerate_combined_model():
    _, tg = make_market(8, seed=17)
    feat = pd.DataFrame({"a": np.arange(8.0)}, index=tg.index)
    oos = combined_model_walkforward(feat, tg, HORIZONS, n_splits=10)
    assert len(oos) == 0 and "p_up_5" in oos.columns
    summ = direction_summary(oos, HORIZONS)
    assert (summ["n"] == 0).all()
    # une seule classe à l'apprentissage -> NaN, pas d'exception
    _, tg = make_market(300, seed=18)
    tg1 = tg.copy()
    tg1["dir_5"] = np.where(np.arange(len(tg)) < 200, 1.0, tg["dir_5"])
    f = pd.DataFrame({"a": np.random.default_rng(0).normal(size=len(tg)),
                      "nan": np.nan}, index=tg.index)
    oos = combined_model_walkforward(f, tg1, (5,), n_splits=5, model="hgb")
    assert oos["p_up_5"].isna().any()
    assert np.isfinite(oos["p_up_5"].iloc[-1])


# -----------------------------------------------------------------------------
# 9. Performance
# -----------------------------------------------------------------------------


def test_performance_200k_rows():
    n = 200_000
    df, tg = make_market(n, seed=19)
    rng = np.random.default_rng(20)
    cols = {f"f{j}": tg["ret_5"].to_numpy() * (j % 3) + rng.normal(0, 2e-3, n) for j in range(8)}
    cols["disc"] = np.round(rng.normal(size=n) * 3)
    cols["warm"] = np.r_[np.full(1440, np.nan), rng.normal(size=n - 1440)]
    feat = pd.DataFrame(cols, index=tg.index)
    t0 = time.perf_counter()
    s = indicator_scores(feat, tg, HORIZONS)
    elapsed = time.perf_counter() - t0
    assert len(s) == 10 * 3
    assert elapsed < 20.0, f"indicator_scores trop lent : {elapsed:.1f} s"
