"""Tests du catalogue d'indicateurs (src/tradebot/indicators.py).

1. Causalité, pour chaque indicateur du registre : calculer sur les données
   tronquées après t doit redonner exactement les valeurs du calcul complet
   jusqu'à t inclus.
2. Aucune colonne entièrement NaN après la période d'initialisation.
3. Bornes plausibles des oscillateurs et des ratios.
4. Indicateurs ignorés quand leurs colonnes sont absentes, entièrement NaN ou
   quand le volume est nul partout.
5. Toutes les familles sont présentes dans list_indicators().
S'y ajoutent des contrôles de formules (Supertrend contre une boucle de
référence, VWAP de séance, calendrier avec heure d'été, alignement de la
référence BTC).
"""

from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
import pytest

from tradebot import indicators as ind
from tradebot.indicators import FAMILIES, REGISTRY, compute_indicators, list_indicators

RTOL = 1e-9
ATOL = 1e-15


# -----------------------------------------------------------------------------
# Données synthétiques
# -----------------------------------------------------------------------------


def make_synthetic(n_days: float = 3.0, seed: int = 7, start: str = "2026-03-06 00:00",
                   gap: bool = True) -> pd.DataFrame:
    """Barres 1 minute UTC synthétiques au format du contrat (crypto + dérivés + référence).

    Marche aléatoire à queues épaisses, ~1 % de barres plates (dont certaines à volume
    nul), volume log-normal, volume taker acheteur, nombre de trades, close de la
    référence corrélé, colonnes de dérivés en escalier toutes les 5 minutes (NaN
    pendant les 7 premières minutes) et, si ``gap``, une panne de 10 minutes.
    Le 06/03/2026 est un vendredi : les données couvrent semaine et week-end.
    """
    rng = np.random.default_rng(seed)
    n = int(round(n_days * 1440))
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    r = rng.standard_t(4, n) * 4e-4
    flat = rng.random(n) < 0.01
    r[flat] = 0.0
    close = 30_000.0 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0.0, 2e-4, (2, n)))
    high = np.maximum(open_, close) * (1.0 + wick[0])
    low = np.minimum(open_, close) * (1.0 - wick[1])
    high[flat] = close[flat]
    low[flat] = close[flat]
    volume = rng.lognormal(2.0, 0.8, n)
    volume[flat & (rng.random(n) < 0.3)] = 0.0
    taker = volume * rng.beta(4.0, 4.0, n)
    trades = np.where(volume > 0, np.maximum(1, rng.poisson(volume * 5.0)), 0).astype(float)
    bench = 60_000.0 * np.exp(np.cumsum(0.6 * r + rng.standard_t(4, n) * 3e-4))

    k = n // 5 + 1

    def steps(values: np.ndarray) -> np.ndarray:
        out = np.repeat(values, 5)[:n].astype(float)
        out[:7] = np.nan  # publication différée
        return out

    oi = 1e5 * np.exp(np.cumsum(rng.normal(0.0, 1e-3, k)))
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": volume * (high + low + close) / 3.0,
            "trades": trades,
            "taker_buy_volume": taker,
            "bench_close": bench,
            "funding_rate": steps(rng.normal(1e-4, 5e-5, k)),
            "open_interest": steps(oi),
            "open_interest_value": steps(oi * 30_000.0),
            "ls_ratio_top": steps(rng.lognormal(0.3, 0.1, k)),
            "ls_ratio_accounts": steps(rng.lognormal(0.6, 0.1, k)),
            "taker_ls_ratio": steps(rng.lognormal(0.0, 0.2, k)),
        },
        index=idx,
    )
    df["session"] = idx.date  # datetime.date (UTC), comme data.add_session
    if gap and n > 2600:
        df = df.drop(df.index[2500:2510])  # panne de l'exchange : 10 barres manquantes
    return df


def _cut_points(n: int) -> list[int]:
    pts = [30, 700, 1439, 1440, 2499, 2500, 2881, 3600, n - 2, n - 1]
    return sorted({p for p in pts if 0 <= p < n})


@pytest.fixture(scope="module")
def synth() -> pd.DataFrame:
    return make_synthetic()


@pytest.fixture(scope="module")
def full(synth) -> pd.DataFrame:
    return compute_indicators(synth)


# -----------------------------------------------------------------------------
# 1. Causalité
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(REGISTRY))
def test_causality_truncation(name, synth, full):
    """Tronquer après t ne change aucune valeur jusqu'à t (donc en particulier en t)."""
    assert name in full.columns, f"{name} non calculé sur les données synthétiques complètes"
    ref = full[name].to_numpy()
    for t in _cut_points(len(synth)):
        part = compute_indicators(synth.iloc[: t + 1], names=[name])
        np.testing.assert_allclose(
            part[name].to_numpy(), ref[: t + 1], rtol=RTOL, atol=ATOL, equal_nan=True,
            err_msg=f"{name} : la valeur jusqu'à t={t} dépend des barres postérieures",
        )


def test_future_perturbation_does_not_leak(synth, full):
    """Modifier fortement toutes les colonnes après t laisse le passé intact."""
    t = 2881
    future = synth.index[t + 1:]
    pert = synth.copy()
    for col in ("open", "high", "low", "close", "bench_close"):
        pert.loc[future, col] = pert.loc[future, col] * 1.3
    for col in ("volume", "quote_volume", "taker_buy_volume", "trades", "open_interest",
                "open_interest_value", "ls_ratio_top", "ls_ratio_accounts", "taker_ls_ratio"):
        pert.loc[future, col] = pert.loc[future, col] * 3.0
    pert.loc[future, "funding_rate"] = -5e-3
    out = compute_indicators(pert)
    np.testing.assert_allclose(out.to_numpy()[: t + 1], full.to_numpy()[: t + 1],
                               rtol=RTOL, atol=ATOL, equal_nan=True)
    assert not np.allclose(out.to_numpy()[t + 1:], full.to_numpy()[t + 1:], equal_nan=True)


# -----------------------------------------------------------------------------
# 2. Couverture après initialisation, types
# -----------------------------------------------------------------------------


def test_every_indicator_computed_and_not_all_nan_after_warmup(full):
    assert list(full.columns) == list(REGISTRY)
    assert full.attrs["skipped"] == {}
    last_day = full.iloc[-1440:]  # après 2 jours d'historique
    coverage = last_day.notna().mean()
    assert not last_day.isna().all().any(), list(coverage[coverage == 0].index)
    assert coverage.min() >= 0.9, coverage[coverage < 0.9].to_dict()


def test_output_is_float64_without_inf(full, synth):
    assert full.index.equals(synth.index)
    assert (full.dtypes == "float64").all()
    assert not np.isinf(full.to_numpy()).any()


def test_input_is_not_modified(synth):
    before = synth.copy()
    compute_indicators(synth, benchmark=synth["bench_close"] * 2.0)
    pd.testing.assert_frame_equal(synth, before)


# -----------------------------------------------------------------------------
# 3. Bornes plausibles
# -----------------------------------------------------------------------------

_SQRT19 = math.sqrt(19.0)
BOUNDS: dict[str, tuple[float, float]] = {
    r"rsi_\d+": (-1.0, 1.0),
    r"stoch_[kd]_.*": (-1.0, 1.0),
    r"williams_r_14": (-1.0, 0.0),
    r"cci_20": (-20.0 / 3.0, 20.0 / 3.0),  # |CCI| <= n / (2 × 0,015), divisé par 100
    r"tsi_25_13": (-1.0, 1.0),
    r"ultimate_osc": (-1.0, 1.0),
    r"mfi_14": (-1.0, 1.0),
    r"cmf_20": (-1.0, 1.0),
    r"adx_14": (0.0, 1.0),
    r"di_diff_14": (-1.0, 1.0),
    r"aroon_osc_25": (-1.0, 1.0),
    r"efficiency_ratio_\d+": (0.0, 1.0),
    r"donchian_pos_\d+": (-0.5, 0.5),
    r"bb_pctb_20_2": (0.5 - _SQRT19 / 4.0, 0.5 + _SQRT19 / 4.0),  # |z| <= √(n−1) (écart-type population)
    r"zscore_20": (-19 / math.sqrt(20), 19 / math.sqrt(20)),  # |z| <= (n−1)/√n
    r"zscore_60": (-59 / math.sqrt(60), 59 / math.sqrt(60)),
    r"zscore_240": (-239 / math.sqrt(240), 239 / math.sqrt(240)),
    r"taker_imb_\d+": (-1.0, 1.0),
    r"tick_imb_\d+": (-1.0, 1.0),
    r"obv_slope_\d+": (-1.0, 1.0),
    r"clv(_\d+)?": (-1.0, 1.0),
    r"body_ratio": (-1.0, 1.0),
    r"(upper|lower)_wick": (0.0, 1.0),
    r"acf1_\d+": (-1.0, 1.0),
    r"corr_bench_240": (-1.0, 1.0),
    r"jump_ratio_240": (0.0, 1.0),
    r"zero_ret_frac_60": (0.0, 1.0),
    r"(hour|dow)_(sin|cos)": (-1.0, 1.0),
    r"is_weekend|session_.*|us_open_window": (0.0, 1.0),
    r"min_since_funding": (0.0, 479.0),
    r"min_to_funding": (1.0, 480.0),
    r"supertrend_dir": (-1.0, 1.0),
    r"rv_\d+|parkinson_\d+|garman_klass_\d+|atr_pct_14|bb_width_20_2|bb_width_rel_20": (0.0, np.inf),
    r"roll_spread_\d+|cs_spread_60|rel_volume_.*|trades_rel_60|avg_trade_size_rel_60": (0.0, np.inf),
    r"rv_ratio_.*|range_atr_14|vr_5_240": (0.0, np.inf),
}


def test_oscillator_bounds(full):
    eps = 1e-9
    matched_patterns = set()
    for name in full.columns:
        for pattern, (lo, hi) in BOUNDS.items():
            if re.fullmatch(pattern, name):
                matched_patterns.add(pattern)
                x = full[name].dropna()
                assert len(x) > 0, name
                assert x.min() >= lo - eps and x.max() <= hi + eps, (name, x.min(), x.max(), lo, hi)
    assert matched_patterns == set(BOUNDS), set(BOUNDS) - matched_patterns


def test_discrete_and_normalized_values(full):
    for name in ("is_weekend", "session_asia", "session_europe", "session_us", "us_open_window"):
        assert set(full[name].dropna().unique()) <= {0.0, 1.0}, name
    assert set(full["supertrend_dir"].dropna().unique()) == {-1.0, 1.0}
    # Les oscillateurs varient vraiment (pas une constante)
    for name in ("rsi_14", "stoch_k_14", "cci_20", "mfi_14", "adx_14", "aroon_osc_25"):
        assert full[name].std() > 0.05, name
    # Rendements en unités de volatilité et z-scores : dispersion de l'ordre de 1
    for name in ("ret_vol_1", "ret_vol_15", "mom_z_15", "taker_imb_z_15", "ls_ratio_top_z_1440"):
        assert 0.5 < full[name].std() < 2.0, (name, full[name].std())


# -----------------------------------------------------------------------------
# 4. Colonnes absentes -> indicateurs ignorés
# -----------------------------------------------------------------------------


def _names(pred) -> set[str]:
    return {n for n, s in REGISTRY.items() if pred(s)}


def test_skips_indicators_when_columns_missing(synth):
    base = synth[["open", "high", "low", "close", "volume", "session"]]
    out = compute_indicators(base)
    expected = _names(lambda s: not s.needs_benchmark and not s.needs_columns)
    assert set(out.columns) == expected
    assert set(out.attrs["skipped"]) == set(REGISTRY) - expected
    assert _names(lambda s: s.family == "flux" and s.needs_columns).isdisjoint(out.columns)
    assert _names(lambda s: s.family in ("derives", "marche")).isdisjoint(out.columns)

    # Noms explicites : un indicateur non calculable est ignoré sans erreur.
    out = compute_indicators(base, names=["taker_imb_5", "rsi_14", "bench_ret_5"])
    assert list(out.columns) == ["rsi_14"]
    assert set(out.attrs["skipped"]) == {"taker_imb_5", "bench_ret_5"}


def test_skips_when_columns_entirely_nan(synth):
    nan_cols = synth.assign(taker_buy_volume=np.nan, funding_rate=np.nan, bench_close=np.nan)
    out = compute_indicators(nan_cols)
    for name, spec in REGISTRY.items():
        blocked = spec.needs_benchmark or bool({"taker_buy_volume", "funding_rate"} & set(spec.needs_columns))
        assert (name in out.columns) == (not blocked), name
    assert "oi_chg_15" in out.columns and "trades_rel_60" in out.columns


def test_skips_volume_indicators_when_volume_zero_or_absent(synth):
    zero_vol = synth.assign(volume=0.0)
    out = compute_indicators(zero_vol)
    assert _names(lambda s: s.needs_volume).isdisjoint(out.columns)
    assert "rsi_14" in out.columns and "funding_rate_bp" in out.columns

    ohlc = synth[["open", "high", "low", "close"]]
    out = compute_indicators(ohlc)
    assert set(out.columns) == _names(
        lambda s: not s.needs_volume and not s.needs_benchmark and not s.needs_columns)
    # Sans colonne session, la séance est la date UTC de l'index : mêmes résultats.
    ref = compute_indicators(synth, names=["ret_since_day_open", "ret_since_day_open_vol"])
    pd.testing.assert_frame_equal(out[ref.columns], ref, check_exact=False, rtol=RTOL)


def test_unknown_name_and_bad_input_raise(synth):
    with pytest.raises(KeyError):
        compute_indicators(synth, names=["pas_un_indicateur"])
    with pytest.raises(ValueError):
        compute_indicators(synth.iloc[::-1])
    with pytest.raises(ValueError):
        compute_indicators(synth.drop(columns=["high"]))
    with pytest.raises(TypeError):
        compute_indicators(synth.reset_index(drop=True))


# -----------------------------------------------------------------------------
# 5. Catalogue
# -----------------------------------------------------------------------------


def test_list_indicators_covers_all_families():
    cat = list_indicators()
    assert list(cat.columns) == ["name", "family", "description", "needs_volume",
                                 "needs_benchmark", "needs_columns"]
    assert len(cat) == len(REGISTRY) >= 70
    assert cat["name"].is_unique
    assert set(cat["family"]) == set(FAMILIES)
    assert cat.groupby("family").size().min() >= 5
    assert cat["name"].str.fullmatch(r"[a-z][a-z0-9_]*").all()
    assert (cat["description"].str.len() > 15).all()
    for name, spec in REGISTRY.items():
        assert spec.name == name and spec.family in FAMILIES
        assert isinstance(spec.needs_columns, tuple)
        if spec.family == "marche":
            assert spec.needs_benchmark
        if spec.family == "derives":
            assert spec.needs_columns


# -----------------------------------------------------------------------------
# Contrôles de formules
# -----------------------------------------------------------------------------


def test_simple_formulas(synth, full):
    c = synth["close"]
    # ln(a) − ln(b) et ln(a / b) diffèrent d'un arrondi (~1e-15 en absolu).
    np.testing.assert_allclose(full["ret_5"], np.log(c / c.shift(5)), rtol=1e-12, atol=1e-13, equal_nan=True)
    v, b = synth["volume"], synth["taker_buy_volume"]
    np.testing.assert_allclose(full["taker_imb_1"], (2 * b / v - 1).where(v > 0), rtol=1e-12, equal_nan=True)
    imb15 = 2 * b.rolling(15).sum() / v.rolling(15).sum() - 1
    np.testing.assert_allclose(full["taker_imb_15"], imb15, rtol=1e-9, equal_nan=True)
    rb = np.log(synth["bench_close"])
    np.testing.assert_allclose(full["rel_ret_15"], np.log(c).diff(15) - rb.diff(15), rtol=1e-9, atol=1e-15,
                               equal_nan=True)
    np.testing.assert_allclose(full["funding_rate_bp"], synth["funding_rate"] * 1e4, rtol=1e-12, equal_nan=True)
    oi = synth["open_interest"]
    np.testing.assert_allclose(full["oi_chg_15"], 100 * (oi / oi.shift(15) - 1), rtol=1e-9, atol=1e-12,
                               equal_nan=True)


def test_rsi_matches_wilder_definition(synth, full):
    d = synth["close"].diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-d).clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    np.testing.assert_allclose(full["rsi_14"], (rsi - 50) / 50, rtol=1e-9, atol=1e-12, equal_nan=True)


def test_session_vwap_resets_at_midnight_utc(synth, full):
    sigma = np.sqrt((np.log(synth["close"]).diff() ** 2).rolling(60).mean())
    day_start = synth.index[(synth.index.hour == 0) & (synth.index.minute == 0)][1:]  # σ60 connu
    assert len(day_start) == 2
    for ts in day_start:
        row = synth.loc[ts]
        expected = (np.log(row["close"]) - np.log(row["quote_volume"] / row["volume"])) / sigma.loc[ts]
        assert full.loc[ts, "vwap_dev_session"] == pytest.approx(expected, rel=1e-9)
        assert full.loc[ts, "ret_since_day_open"] == pytest.approx(np.log(row["close"] / row["open"]), abs=1e-15)


def test_calendar_utc_and_daylight_saving():
    # 06/03/2026 (vendredi, heure d'hiver à New York) et 09/03/2026 (lundi, heure d'été).
    idx = pd.DatetimeIndex(
        ["2026-03-06 14:29", "2026-03-06 14:30", "2026-03-06 20:59", "2026-03-06 21:00",
         "2026-03-07 15:00", "2026-03-09 13:29", "2026-03-09 13:30", "2026-03-09 19:59",
         "2026-03-09 20:00", "2026-03-10 07:59", "2026-03-10 08:00"], tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx)
    out = compute_indicators(df, names=["session_us", "is_weekend", "min_to_funding", "min_since_funding",
                                        "session_asia", "hour_sin", "hour_cos"])
    assert out["session_us"].tolist() == [0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0]
    assert out["is_weekend"].tolist() == [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    assert out.loc["2026-03-10 07:59", "min_to_funding"].item() == 1
    assert out.loc["2026-03-10 08:00", "min_since_funding"].item() == 0
    assert out.loc["2026-03-10 07:59", "session_asia"].item() == 0
    np.testing.assert_allclose(out["hour_sin"] ** 2 + out["hour_cos"] ** 2, 1.0)


def test_benchmark_argument_alignment(synth):
    market = sorted(_names(lambda s: s.needs_benchmark))
    base = synth.drop(columns=["bench_close"])
    bench = synth["bench_close"]
    ref = compute_indicators(synth, names=market)
    # Référence passée en argument, avec une autre unité de temps et un autre fuseau.
    other = bench.set_axis(bench.index.as_unit("ms").tz_convert("Europe/Paris"))
    got = compute_indicators(base, names=market, benchmark=other)
    pd.testing.assert_frame_equal(got, ref)
    # Causalité avec la référence complète en argument.
    t = 3000
    part = compute_indicators(base.iloc[: t + 1], names=market, benchmark=bench)
    np.testing.assert_allclose(part.to_numpy(), ref.to_numpy()[: t + 1], rtol=RTOL, atol=ATOL, equal_nan=True)
    # Report de la dernière valeur limité à 5 barres.
    holes = bench.drop(bench.index[3000:3010])
    out = compute_indicators(base, names=["bench_ret_1"], benchmark=holes)["bench_ret_1"].to_numpy()
    assert np.all(out[3000:3005] == 0.0)
    assert np.isnan(out[3005:3011]).all()
    assert np.isfinite(out[3011])


def _supertrend_reference(df: pd.DataFrame, n: int = 10, mult: float = 3.0):
    """Supertrend (convention TradingView) par une boucle explicite, pour comparaison."""
    h, lo, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - lo, np.fmax(np.abs(h - pc), np.abs(lo - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()
    hl2 = (h + lo) / 2
    dist = np.full(len(c), np.nan)
    direction = np.full(len(c), np.nan)
    up_prev = dn_prev = prev_close = None
    trend = 1
    for i in range(len(c)):
        if not np.isfinite(atr[i]):
            continue
        up, dn = hl2[i] - mult * atr[i], hl2[i] + mult * atr[i]
        if up_prev is not None:
            if prev_close > up_prev:
                up = max(up, up_prev)
            if prev_close < dn_prev:
                dn = min(dn, dn_prev)
            if trend == -1 and c[i] > dn_prev:
                trend = 1
            elif trend == 1 and c[i] < up_prev:
                trend = -1
        dist[i] = (c[i] - (up if trend == 1 else dn)) / atr[i]
        direction[i] = trend
        up_prev, dn_prev, prev_close = up, dn, c[i]
    return dist, direction


@pytest.mark.parametrize("mult", [3.0, 1.0, 0.2])
def test_supertrend_matches_reference_loop(synth, mult):
    dist, direction = ind._supertrend(synth, n=10, mult=mult)
    ref_dist, ref_dir = _supertrend_reference(synth, n=10, mult=mult)
    np.testing.assert_array_equal(direction.to_numpy(), ref_dir)
    np.testing.assert_allclose(dist.to_numpy(), ref_dist, rtol=1e-12, atol=1e-12, equal_nan=True)
    assert np.nanmin(ref_dir) == -1 and np.nanmax(ref_dir) == 1


def test_prev_ge_index_bruteforce():
    rng = np.random.default_rng(3)
    for size in (1, 2, 7, 64, 333):
        values = rng.integers(0, 20, size).astype(float)  # nombreuses égalités
        queries = rng.integers(0, 22, size).astype(float)
        got = ind._prev_ge_index(values, queries)
        for k in range(size):
            hits = [j for j in range(k + 1) if values[j] >= queries[k]]
            assert got[k] == (hits[-1] if hits else -1)
