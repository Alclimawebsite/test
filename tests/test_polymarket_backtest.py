"""Tests hors-ligne du backtest « nos modèles contre le marché » (tradebot.polymarket_backtest).

Couvre : l'alignement temporel des variables (rien après S − 30 s : on modifie les données
après S − 30 s et les variables ne doivent pas bouger), l'étiquette proxy VWAP, la purge
apprentissage / test, les frais et le P&L sur des cas construits, le bootstrap groupé.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket as pm
from tradebot import polymarket_backtest as pb
from tradebot.indicators import compute_indicators

T0 = pd.Timestamp("2026-08-20 00:00", tz="UTC")


# ---------------------------------------------------------------------------
# Données synthétiques
# ---------------------------------------------------------------------------
def make_1s(n_sec: int, seed: int = 0, start: pd.Timestamp = T0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 2e-4, n_sec)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 1e-4, n_sec))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 1e-4, n_sec))
    vol = rng.uniform(0.1, 2.0, n_sec)
    idx = pd.date_range(start, periods=n_sec, freq="1s", tz="UTC")
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": vol,
        "quote_volume": vol * close * (1 + rng.normal(0, 1e-5, n_sec)),
        "trades": rng.integers(1, 20, n_sec).astype(float), "taker_buy_volume": vol * rng.uniform(0, 1, n_sec),
    }, index=idx)


def resample_1m(df1s: pd.DataFrame) -> pd.DataFrame:
    g = df1s.resample("1min")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "quote_volume": g["quote_volume"].sum(), "trades": g["trades"].sum(),
        "taker_buy_volume": g["taker_buy_volume"].sum(),
    })
    return out


def perturb_after(df: pd.DataFrame, t_open: pd.Timestamp, factor: float = 1.07) -> pd.DataFrame:
    """Multiplie prix et volumes de toutes les bougies ouvertes à partir de ``t_open``."""
    out = df.copy()
    m = out.index >= t_open
    for c in ("open", "high", "low", "close"):
        out.loc[m, c] = out.loc[m, c] * factor
    for c in ("volume", "quote_volume", "taker_buy_volume"):
        out.loc[m, c] = out.loc[m, c] * 3.0
    return out


@pytest.fixture(scope="module")
def synth():
    df1s = make_1s(6 * 3600)                      # 6 heures de bougies 1s
    df1m = resample_1m(df1s)
    S = int((T0 + pd.Timedelta(hours=5)).timestamp())     # multiple de 300 s
    return df1s, df1m, S


# ---------------------------------------------------------------------------
# 1. Alignement temporel : rien après S − 30 s
# ---------------------------------------------------------------------------
def test_minute_aggregates_first_half_only(synth):
    df1s, _, S = synth
    agg = pb.minute_aggregates_1s(df1s)
    m = pd.Timestamp(S - 60, unit="s", tz="UTC")
    sec = df1s.loc[m: m + pd.Timedelta(seconds=59)]
    row = agg.loc[m]
    assert row["a_close"] == pytest.approx(sec["close"].iloc[29])
    assert row["a_twap"] == pytest.approx(sec["close"].iloc[:30].mean())
    assert row["b_twap"] == pytest.approx(sec["close"].iloc[30:].mean())
    assert row["twap60"] == pytest.approx(sec["close"].mean())
    assert row["a_vwap"] == pytest.approx(sec["quote_volume"].iloc[:30].sum() / sec["volume"].iloc[:30].sum())
    assert row["a_open"] == pytest.approx(sec["open"].iloc[0])


def test_twap_partial_features_ignore_data_after_s_minus_30(synth):
    df1s, df1m, S = synth
    base = pb.twap_partial_features(pb.minute_aggregates_1s(df1s), df1m, np.array([S]), 300)
    # on modifie TOUT ce qui n'est pas connu à S − 30 s : bougies 1s ouvertes à partir de S − 30 s,
    # bougies 1m ouvertes à partir de S − 60 s (close à S)
    s1 = perturb_after(df1s, pd.Timestamp(S - 30, unit="s", tz="UTC"))
    m1 = perturb_after(df1m, pd.Timestamp(S - 60, unit="s", tz="UTC"))
    pert = pb.twap_partial_features(pb.minute_aggregates_1s(s1), m1, np.array([S]), 300)
    pd.testing.assert_frame_equal(base, pert)
    assert np.isfinite(base.to_numpy()).all()
    # contrôle de sensibilité : la bougie 1s ouverte à S − 31 s (close à S − 30 s) est connue et compte
    s2 = perturb_after(df1s, pd.Timestamp(S - 31, unit="s", tz="UTC"))
    pert2 = pb.twap_partial_features(pb.minute_aggregates_1s(s2), m1, np.array([S]), 300)
    assert pert2["tw_gap30"].iloc[0] != pytest.approx(base["tw_gap30"].iloc[0])


def test_gap30_matches_diagnostic_definition(synth):
    df1s, df1m, S = synth
    f = pb.twap_partial_features(pb.minute_aggregates_1s(df1s), df1m, np.array([S]), 300)
    spot30 = df1s["close"].loc[pd.Timestamp(S - 31, unit="s", tz="UTC")]
    twap_part = df1s["close"].loc[pd.Timestamp(S - 60, unit="s", tz="UTC"): pd.Timestamp(S - 31, unit="s", tz="UTC")].mean()
    assert f["tw_gap30"].iloc[0] == pytest.approx(math.log(spot30 / twap_part), rel=1e-5)


def test_indicator_rows_ignore_data_after_s_minus_60(synth):
    _, df1m, S = synth
    names = ["ret_1", "ret_5", "rsi_14", "ema_dist_30", "vwap_dev_60", "rv_60", "bb_pctb_20_2", "force_index_13"]
    names = [n for n in names if n in compute_indicators(df1m.iloc[:200], names=None).columns]
    ind = compute_indicators(df1m, names=names)
    base = pb.indicator_rows(ind, np.array([S]))
    pert_df = perturb_after(df1m, pd.Timestamp(S - 60, unit="s", tz="UTC"))
    pert = pb.indicator_rows(compute_indicators(pert_df, names=names), np.array([S]))
    pd.testing.assert_frame_equal(base, pert)
    assert np.isfinite(base.to_numpy()).all()
    # la ligne lue est celle de la bougie ouverte à S − 120 s
    exp = ind.loc[pd.Timestamp(S - 120, unit="s", tz="UTC")].to_numpy(dtype="float32")
    np.testing.assert_array_equal(base.iloc[0].to_numpy(), exp)
    # sensibilité : modifier la bougie ouverte à S − 120 s change les variables
    pert2 = pb.indicator_rows(compute_indicators(perturb_after(df1m, pd.Timestamp(S - 120, unit="s", tz="UTC")),
                                                 names=names), np.array([S]))
    assert not np.allclose(base.to_numpy(), pert2.to_numpy())


def test_baseline_features_causal(synth):
    _, df1m, S = synth
    base = pb.baseline_features(df1m, np.array([S]))
    pert = pb.baseline_features(perturb_after(df1m, pd.Timestamp(S - 60, unit="s", tz="UTC")), np.array([S]))
    pd.testing.assert_frame_equal(base, pert)
    c = df1m["close"]
    ts = lambda s: pd.Timestamp(s, unit="s", tz="UTC")  # noqa: E731
    assert base["mom15_m60"].iloc[0] == pytest.approx(math.log(c[ts(S - 120)] / c[ts(S - 1020)]))


def test_bar_lookup_exact_no_fill():
    idx = pd.date_range(T0, periods=5, freq="1min", tz="UTC")
    df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx).drop(idx[2])
    t = pb.to_unix(idx)
    out = pb.bar_lookup(df, t, "x")
    np.testing.assert_array_equal(np.isnan(out), [False, False, True, False, False])
    assert out[3] == 4.0


# ---------------------------------------------------------------------------
# 2. Étiquette proxy VWAP
# ---------------------------------------------------------------------------
def _bars(times_s, vwap, close, volume=1.0):
    idx = pd.to_datetime(np.asarray(times_s), unit="s", utc=True)
    v = np.full(len(times_s), volume, dtype=float)
    df = pd.DataFrame({"close": close, "volume": v, "quote_volume": np.asarray(vwap) * v}, index=idx)
    return df.sort_index()


def test_vwap_label_uses_vwap_of_last_minutes():
    S, D = 3000, 300
    # bougie [S−1m] VWAP 100 (close 50), bougie [E−1m] VWAP 100 (close 10) -> Up (égalité), malgré les closes
    bars = _bars([S - 60, S + D - 60], [100.0, 100.0], [50.0, 10.0])
    assert pb.vwap_label(bars, np.array([S]), D)[0] == 1.0
    bars = _bars([S - 60, S + D - 60], [100.0, 99.99], [10.0, 50.0])
    assert pb.vwap_label(bars, np.array([S]), D)[0] == 0.0
    # 15 min : bougie [S+15m−1m]
    bars = _bars([S - 60, S + 900 - 60, S + 240], [100.0, 101.0, 1.0], [0, 0, 0])
    assert pb.vwap_label(bars, np.array([S]), 900)[0] == 1.0
    assert pb.vwap_label(bars, np.array([S]), 300)[0] == 0.0


def test_vwap_label_missing_or_zero_volume_is_nan():
    S, D = 3000, 300
    bars = _bars([S - 60], [100.0], [100.0])
    assert np.isnan(pb.vwap_label(bars, np.array([S]), D)[0])
    bars = _bars([S - 60, S + D - 60], [100.0, 101.0], [100.0, 100.0], volume=0.0)
    assert np.isnan(pb.vwap_label(bars, np.array([S]), D)[0])


def test_vwap_label_matches_1s_vwap(synth):
    df1s, df1m, S = synth
    y = pb.vwap_label(df1m, np.array([S]), 300)[0]
    sec = df1s[["quote_volume", "volume"]]
    w = lambda a: sec.loc[pd.Timestamp(a, unit="s", tz="UTC"): pd.Timestamp(a + 59, unit="s", tz="UTC")].sum()  # noqa: E731
    v0, v1 = w(S - 60), w(S + 240)
    assert y == float(v1["quote_volume"] / v1["volume"] >= v0["quote_volume"] / v0["volume"])


# ---------------------------------------------------------------------------
# 3. Purge apprentissage / test
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("D", [300, 900])
def test_train_segments_purged(D):
    test_start = int(pb.TWAP60_START.timestamp())
    S = np.arange(test_start - 60 * 86400, test_start + 5 * 86400, 300)
    seg = pb.train_segments(S, test_start, D, gap_s=3600)
    fit, es, cal = S[seg["fit"]], S[seg["es"]], S[seg["cal"]]
    assert len(fit) > len(es) > 0 and len(cal) > 0
    # aucune origine d'apprentissage dont l'étiquette [S − 60, S + D] atteint le test
    for part in (fit, es, cal):
        assert part.max() + D + 3600 <= test_start
    # segments disjoints, ordonnés, séparés par au moins D + marge
    assert fit.max() + D + 3600 < es.min()
    assert es.max() + D + 3600 < cal.min()
    # proportions ≈ 80 / 10 / 10 (valeur par défaut)
    n = len(fit) + len(es) + len(cal)
    assert 0.75 < len(fit) / n < 0.82
    assert not (seg["fit"] & seg["es"]).any() and not (seg["es"] & seg["cal"]).any()


def test_train_segments_respects_start():
    test_start = 10_000_000
    S = np.arange(test_start - 10 * 86400, test_start, 300)
    seg = pb.train_segments(S, test_start, 300, train_start_s=test_start - 5 * 86400)
    used = seg["fit"] | seg["es"] | seg["cal"]
    assert S[used].min() >= test_start - 5 * 86400


# ---------------------------------------------------------------------------
# 4. Frais et P&L sur des cas construits
# ---------------------------------------------------------------------------
def test_taker_trades_up_down_and_fees():
    p_model = np.array([0.60, 0.40, 0.53, 0.60, 0.60])
    p_mkt = np.array([0.50, 0.50, 0.50, 0.50, np.nan])
    y = np.array([1.0, 1.0, 1.0, 0.0, 1.0])
    t = pb.taker_trades(p_model, p_mkt, y, margin=0.05, fee_rate=0.07, fee_exponent=1.0)
    fee = pm.taker_fee(0.505, 1.0, {"rate": 0.07, "exponent": 1})
    assert fee == pytest.approx(0.0175)                      # 0,07 × 0,505 × 0,495 arrondi à 5 décimales
    # 1) achat Up : 1 − 0,505 − 0,0175
    assert t["side"][0] == 1 and t["pnl"][0] == pytest.approx(1 - 0.505 - fee)
    assert t["edge"][0] == pytest.approx(0.60 - 0.505 - fee)
    # 2) achat Down à 1 − 0,5 + 0,005, perdu
    assert t["side"][1] == -1 and t["price"][1] == pytest.approx(0.505)
    assert t["pnl"][1] == pytest.approx(-(0.505 + fee))
    # 3) avantage 0,53 − 0,5225 = 0,0075 < marge 0,05 : pas de position
    assert t["side"][2] == 0 and t["pnl"][2] == 0.0
    # 4) Up perdu
    assert t["pnl"][3] == pytest.approx(-(0.505 + fee))
    # 5) pas de prix de marché : exclu
    assert t["side"][4] == 0 and np.isnan(t["pnl"][4])
    # avec marge nulle, le cas 3 devient une position Up (0,0075 > 0)
    t0 = pb.taker_trades(p_model, p_mkt, y, margin=0.0)
    assert t0["side"][2] == 1


def test_taker_breakeven_consistent_with_client():
    # seuil de rentabilité : p_modèle doit dépasser ask + frais(ask) = breakeven_probability(ask)
    ask = 0.51
    be = pm.breakeven_probability(ask, {"rate": 0.07, "exponent": 1})
    t = pb.taker_trades(np.array([be + 1e-4, be - 1e-4]), np.array([0.505, 0.505]), np.array([1.0, 1.0]), 0.0)
    assert list(t["side"]) == [1, 0]


def test_taker_fee_schedule_per_market():
    # ancien barème crypto_fees (0,25, exposant 2) sur la 2e ligne, pas de frais sur la 3e
    t = pb.taker_trades(np.array([0.9, 0.9, 0.9]), np.array([0.5, 0.5, 0.5]), np.ones(3), 0.0,
                        fee_rate=np.array([0.07, 0.25, 0.0]), fee_exponent=np.array([1.0, 2.0, 1.0]))
    assert t["fee"][0] == pytest.approx(0.0175)
    assert t["fee"][1] == pytest.approx(round(0.25 * (0.505 * 0.495) ** 2, 5))
    assert t["fee"][2] == 0.0


def test_maker_trades_optimistic_bound():
    t = pb.maker_trades(np.array([0.6, 0.4, 0.5]), np.array([0.5, 0.5, 0.5]), np.array([1.0, 0.0, 1.0]), 0.02)
    assert t["side"][0] == 1 and t["price"][0] == pytest.approx(0.495) and t["fee"][0] == 0
    assert t["pnl"][0] == pytest.approx(0.505)
    assert t["side"][1] == -1 and t["pnl"][1] == pytest.approx(1 - 0.495)
    assert t["side"][2] == 0      # 0,5 − 0,495 = 0,005 < 0,02


def test_pnl_by_margin_and_choice():
    p = np.array([0.60, 0.58, 0.56, 0.54, 0.45])
    m = np.full(5, 0.50)
    y = np.array([1.0, 0.0, 1.0, 1.0, 0.0])
    cost = 0.505 + 0.0175                       # ask + frais, identique pour Up et Down à p_mkt = 0,5
    # avantages : Up 0,0775 / 0,0575 / 0,0375 / 0,0175 ; Down (p = 0,45) 0,55 − 0,5225 = 0,0275
    c = pb.pnl_by_margin(p, m, y, [0.0, 0.02, 0.05, 0.2])
    assert list(c["n_trades"]) == [5, 4, 2, 0]
    assert c["pnl_total"][0] == pytest.approx(4 * (1 - cost) - cost)      # 4 gagnés, 1 perdu
    assert c["pnl_total"][1] == pytest.approx(3 * (1 - cost) - cost)      # 0,54 (0,0175) exclu
    assert c["pnl_total"][2] == pytest.approx((1 - cost) - cost)          # 0,60 gagné, 0,58 perdu
    assert c["pnl_total"][3] == 0.0 and math.isnan(c["pnl_per_trade"][3])
    assert c["win_rate"][0] == pytest.approx(0.8)
    assert c["share_up"][0] == pytest.approx(0.8)
    # choix : P&L total maximal parmi les marges avec assez de positions
    assert pb.choose_margin(c, min_trades=1) == 0.0
    assert pb.choose_margin(c, min_trades=3) == 0.0
    assert math.isnan(pb.choose_margin(c, min_trades=100))
    c2 = pd.DataFrame({"margin": [0.0, 0.01, 0.02], "n_trades": [500, 300, 150], "pnl_total": [-3.0, 2.0, 2.0]})
    assert pb.choose_margin(c2, min_trades=100) == 0.01                   # égalité : la plus petite marge
    assert pb.choose_margin(c2, min_trades=200) == 0.01


# ---------------------------------------------------------------------------
# 5. Métriques et bootstrap groupé
# ---------------------------------------------------------------------------
def test_weighted_auc_matches_sklearn_with_ties():
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, 500).astype(float)
    s = np.round(rng.normal(size=500) + 0.3 * y, 1)          # beaucoup d'égalités
    w = rng.integers(0, 3, 500).astype(float)
    assert pb.weighted_auc(y, s, np.ones((1, 500)))[0] == pytest.approx(roc_auc_score(y, s))
    assert pb.weighted_auc(y, s, w[None, :])[0] == pytest.approx(roc_auc_score(y, s, sample_weight=w))


def test_slot_bootstrap_ci_and_clusters():
    rng = np.random.default_rng(2)
    g = np.repeat(np.arange(200), 6)                          # 6 marchés corrélés par créneau
    x = np.repeat(rng.normal(size=200), 6) + 0.1 * rng.normal(size=1200)
    bs = pb.SlotBootstrap(g, B=500, seed=0)
    est, lo, hi = bs.mean(x)
    assert lo < est < hi and est == pytest.approx(x.mean())
    # IC groupé plus large que l'IC naïf (i.i.d.) : ≈ √6 fois
    naive = 1.96 * x.std() / math.sqrt(len(x))
    assert (hi - lo) / 2 > 1.8 * naive
    r = bs.ratio(np.ones(1200), np.full(1200, 2.0))
    assert r[0] == pytest.approx(0.5) and r[1] == pytest.approx(0.5)


def test_prob_metrics_basic():
    y = np.array([1, 0, 1, 0], dtype=float)
    p = np.array([0.8, 0.2, 0.6, 0.4])
    m = pb.prob_metrics(y, p)
    assert m["accuracy"] == 1.0 and m["auc"] == 1.0
    assert m["brier"] == pytest.approx(np.mean((p - y) ** 2))
    m2 = pb.prob_metrics(y, np.full(4, np.nan), score=np.array([1.0, -1.0, -1.0, 1.0]))
    assert m2["accuracy"] == 0.5 and math.isnan(m2["brier"])


def test_calibration_table_bins():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.3, 0.7, 5000)
    y = (rng.uniform(size=5000) < p).astype(float)
    c = pb.calibration_table(y, p, bins=10)
    assert len(c) == 10 and c["n"].sum() == 5000
    assert np.all(np.abs(c["up_rate"] - c["p_mean"]) < 0.07)


# ---------------------------------------------------------------------------
# 6. Prix avant l'ouverture, collecte et modèles
# ---------------------------------------------------------------------------
def _hist(points: dict[int, float]) -> pd.Series:
    return pm._history_series(list(points), list(points.values()))


def test_pre_open_price_last_point_before_s_minus_30():
    S = 1_790_000_100
    h = _hist({S - 200: 0.49, S - 40: 0.51, S - 29: 0.70, S + 30: 0.9})
    p, age = pb.pre_open_price(h, S)
    assert p == 0.51 and age == 10.0
    assert np.isnan(pb.pre_open_price(_hist({S - 400: 0.5}), S)[0])      # > 5 min
    assert np.isnan(pb.pre_open_price(_hist({S - 10: 0.5}), S)[0])       # seulement après S − 30 s


def test_fetch_pre_open_prices_with_fake_client():
    S = 1_790_000_100

    class FakeClient:
        def prices_history(self, token, a, b, fidelity=1, resolved=None):
            assert a == S - 900 and b == S + 300 + 60 and fidelity == 1
            if token == "bad":
                raise pm.PolymarketError("boom")
            return _hist({S - 45: 0.52, S + 10: 0.8})

    mk = pd.DataFrame({"slug": ["a", "b"], "token_up": ["ok", "bad"], "start_ts": [S, S], "end_ts": [S + 300] * 2})
    out = pb.fetch_pre_open_prices(FakeClient(), mk, workers=2).set_index("slug")
    assert out.loc["a", "p_pre"] == 0.52 and out.loc["a", "p_pre_age_s"] == 15
    assert bool(out.loc["b", "error"]) and np.isnan(out.loc["b", "p_pre"])


def test_load_second_aggregates_cache(tmp_path):
    calls = []

    def fake_day(symbol, day):
        calls.append(day)
        return make_1s(86400, seed=int(day.day), start=day)

    a = pb.load_second_aggregates("XUSDT", T0, T0 + pd.Timedelta(days=2), tmp_path, workers=1, fetch_day=fake_day)
    assert len(a) == 2 * 1440 and len(calls) == 2
    b = pb.load_second_aggregates("XUSDT", T0, T0 + pd.Timedelta(days=2), tmp_path, workers=1, fetch_day=fake_day)
    assert len(calls) == 2                                            # relu depuis le cache
    pd.testing.assert_frame_equal(a, b)


@pytest.mark.parametrize("kind", ["logit", "hgb"])
def test_prob_model_fit_calibrate(kind):
    rng = np.random.default_rng(4)
    X = rng.normal(size=(6000, 3))
    X[rng.uniform(size=X.shape) < 0.05] = np.nan
    y = (rng.uniform(size=6000) < 1 / (1 + np.exp(-np.nan_to_num(X[:, 0])))).astype(float)
    m = pb.ProbModel(kind=kind, max_iter=100)
    m.fit(X[:4000], y[:4000], X[4000:5000], y[4000:5000])
    m.calibrate(X[4000:5000], y[4000:5000])
    p = m.predict(X[5000:])
    assert p.shape == (1000,) and np.isfinite(p).all() and ((p > 0) & (p < 1)).all()
    assert pb.prob_metrics(y[5000:], p)["auc"] > 0.65
    # isotonique sur scores winsorisés : bornes = quantiles 1 % / 99 % du segment de calibration
    raw_cal = m.predict_raw(X[4000:5000])
    assert m.clip_ == pytest.approx(tuple(np.quantile(raw_cal, [0.01, 0.99])))
    extreme = m.predict_raw(X[5000:]) > m.clip_[1]
    if extreme.any():
        assert np.allclose(p[extreme], m.iso_(np.array([m.clip_[1]]))[0])
