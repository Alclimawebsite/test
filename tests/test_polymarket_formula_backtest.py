"""Tests du backtest de la formule : alignement (rien après t), moyennes TWAP depuis des séries 1 s
construites, prix d'exécution à t + δ, P&L et frais, carnet et délai de réaction."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket_formula_backtest as fb
from tradebot.polymarket_formula import ewma_sigma_per_s, fair_prob_up, parkinson_sigma_per_s

S, E = 10_000, 10_300          # marché 5 min
L = 60


def _series(n0: int = S - 400, n1: int = E + 50, seed: int = 0):
    """Bougies 1 s ouvertes de n0 à n1 − 1, prix connu à u = close de la bougie u − 1."""
    rng = np.random.default_rng(seed)
    sec = np.arange(n0, n1)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 1e-4, sec.size)))
    return sec, close


# ---------------------------------------------------------------------------
# Série 1 s et entrées de la formule
# ---------------------------------------------------------------------------
def test_price_at_u_is_close_of_bar_opened_u_minus_1():
    sec, close = _series()
    P = fb.SecondPrices(sec, close)
    u = S - 10
    assert P.at(u) == pytest.approx(math.log(close[sec == u - 1][0]))
    assert np.isnan(P.at(sec[0]))                         # pas encore connu
    assert np.isnan(P.at(sec[-1] + 2))                    # après la fin


def test_window_means_from_constructed_series():
    """Prix construits = instant connu (log-prix = u) : les moyennes sont des moyennes d'entiers."""
    sec = np.arange(S - 200, E + 10)
    close = np.exp((sec + 1).astype(float) / 1e6)         # x(u) = u / 1e6
    P = fb.SecondPrices(sec, close)
    t = S - 20
    inp = fb.formula_inputs(P, t, S, E)
    # start_avg = moyenne de u ∈ (S − 60, S − 20] = (S − 59 + S − 20) / 2
    assert inp["start_avg"] == pytest.approx(((S - 59) + (S - 20)) / 2 / 1e6)
    assert np.isnan(inp["price_to_beat"]) and np.isnan(inp["end_partial_sum"])
    inp = fb.formula_inputs(P, S + 5, S, E)
    assert inp["price_to_beat"] == pytest.approx(((S - 59) + S) / 2 / 1e6)     # K sur (S − 60, S]
    assert np.isnan(inp["start_avg"])
    inp = fb.formula_inputs(P, E - 20, S, E)
    # somme sur (E − 60, E − 20] / 60 : 40 termes
    expect = sum(range(E - 59, E - 19)) / 1e6 / 60
    assert inp["end_partial_sum"] == pytest.approx(expect)
    assert inp["log_price"] == pytest.approx((E - 20) / 1e6)


def test_lagged_inputs_shift_binance_windows():
    sec = np.arange(S - 200, E + 10)
    close = np.exp((sec + 1).astype(float) / 1e6)
    P = fb.SecondPrices(sec, close)
    c = 3
    inp = fb.formula_inputs(P, S - 1, S, E, lag_s=c)      # τ = S + 2 > S : K connu
    assert inp["tau"] == S + 2
    assert inp["price_to_beat"] == pytest.approx(((S - 59 - c) + (S - c)) / 2 / 1e6)
    assert inp["log_price"] == pytest.approx((S - 1) / 1e6)


@pytest.mark.parametrize("t", [S - 45, S - 10, S, S + 30, E - 90, E - 30, E - 10])
@pytest.mark.parametrize("lag", [0, 3])
def test_no_data_after_t(t, lag):
    """Modifier tous les prix connus après t ne change ni les entrées ni la probabilité."""
    sec, close = _series(seed=3)
    P1 = fb.SecondPrices(sec, close)
    close2 = close.copy()
    close2[sec + 1 > t] *= 1.05                       # prix des instants u > t
    P2 = fb.SecondPrices(sec, close2)
    a = fb.formula_inputs(P1, t, S, E, lag_s=lag)
    b = fb.formula_inputs(P2, t, S, E, lag_s=lag)
    for k in a:
        np.testing.assert_array_equal(a[k], b[k])
    pa = fb.formula_probs(a, S, E, 5e-5)["p"]
    pb = fb.formula_probs(b, S, E, 5e-5)["p"]
    assert pa == pb
    # et les estimateurs de σ
    v1 = fb.ewma_var_series(P1, 60.0)
    v2 = fb.ewma_var_series(P2, 60.0)
    k = t - P1.t0
    assert v1[k] == v2[k]


@pytest.mark.parametrize("t", [S - 45, S - 10, S - 2, S, S + 2, S + 120, E - 60, E - 30, E - 10])
def test_vectorized_probs_match_scalar_formula(t):
    sec, close = _series(seed=5)
    P = fb.SecondPrices(sec, close)
    inp = fb.formula_inputs(P, t, S, E)
    sig = 6e-5
    out = fb.formula_probs(inp, S, E, sig)
    kw = {}
    if S - L < t <= S:
        kw["start_avg"] = float(inp["start_avg"])
    if t > S:
        kw["price_to_beat"] = float(inp["price_to_beat"])
    if t > E - L:
        kw["end_partial_sum"] = float(inp["end_partial_sum"])
    fv = fair_prob_up(t, S, E, float(inp["log_price"]), sig, **kw)
    assert float(out["p"]) == pytest.approx(fv.prob_up, abs=1e-12)
    assert float(out["sd"]) == pytest.approx(fv.sd, rel=1e-12)
    assert int(out["phase"]) == fv.phase


def test_extra_sd_widens_distribution():
    sec, close = _series(seed=5)
    P = fb.SecondPrices(sec, close)
    inp = fb.formula_inputs(P, E - 10, S, E)
    a = fb.formula_probs(inp, S, E, 6e-5)
    b = fb.formula_probs(inp, S, E, 6e-5, extra_sd=5e-5)
    assert float(b["sd"]) == pytest.approx(math.hypot(float(a["sd"]), 5e-5))
    assert abs(float(b["p"]) - 0.5) <= abs(float(a["p"]) - 0.5)


def test_decision_times_grid():
    T = fb.decision_times(np.array([S]), np.array([E]))
    labels = [d[0] for d in fb.DECISIONS]
    assert T[0, labels.index("S-45")] == S - 45
    assert T[0, labels.index("E-10")] == E - 10
    assert T.shape == (1, len(fb.DECISIONS))


# ---------------------------------------------------------------------------
# σ
# ---------------------------------------------------------------------------
def test_ewma_matches_module_estimator():
    sec, close = _series(n0=0, n1=20_000, seed=9)
    P = fb.SecondPrices(sec, close)
    v = fb.ewma_var_series(P, 300.0)
    k = 15_000
    ref = ewma_sigma_per_s(P.x[: k + 1], halflife_s=300.0)
    assert math.sqrt(v[k]) == pytest.approx(ref, rel=1e-3)


def test_parkinson_series_is_causal_and_matches_module():
    rng = np.random.default_rng(2)
    n = 200
    idx = pd.date_range("2026-09-01", periods=n, freq="1min", tz="UTC")
    h = 100 * np.exp(np.abs(rng.normal(0, 1e-3, n)))
    lo = 100 * np.exp(-np.abs(rng.normal(0, 1e-3, n)))
    bars = pd.DataFrame({"high": h, "low": lo}, index=idx)
    s = fb.parkinson_sigma_series(bars, n=60)
    open_s = (idx - pd.Timestamp(0, tz="UTC")) // pd.Timedelta("1s")
    t = int(open_s[120]) + 15                     # 15 s dans la bougie 120 (non close)
    got = float(fb.bar_sigma_at(s, np.array([t]))[0])
    ref = parkinson_sigma_per_s(h[60:120], lo[60:120])   # bougies 60..119 : closes à t
    assert got == pytest.approx(ref, rel=1e-9)
    # une bougie qui se ferme exactement à t est incluse
    t2 = int(open_s[120])
    assert float(fb.bar_sigma_at(s, np.array([t2]))[0]) == pytest.approx(ref, rel=1e-9)


# ---------------------------------------------------------------------------
# Trades : prix du marché, exécution à t + δ, P&L
# ---------------------------------------------------------------------------
def _trades():
    rows = [  # ts, side, outcome, price, size, seq
        (S - 50, "BUY", "Up", 0.50, 10, 0),
        (S - 3, "SELL", "Up", 0.48, 5, 1),
        (S - 2, "BUY", "Down", 0.53, 10, 2),       # repère Up : 0,47
        (S - 1, "BUY", "Up", 0.52, 30, 3),
        (S + 1, "BUY", "Up", 0.55, 4, 4),
        (S + 1, "BUY", "Up", 0.56, 4, 5),
        (S + 4, "BUY", "Down", 0.40, 7, 6),
        (S + 9, "BUY", "Up", 0.60, 2, 7),
    ]
    df = pd.DataFrame(rows, columns=["ts", "side", "outcome", "price", "size", "seq"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df["slug"] = "m"
    return fb.prepare_trades(df)["m"]


def test_market_price_last_and_vwap():
    tr = _trades()
    out = fb.market_price_at(tr, np.array([S, S - 10, S - 60]))
    assert out["last"][0] == pytest.approx(0.52)
    assert out["age"][0] == 1
    # VWAP [S − 3, S] : 0,48·5 + 0,47·10 + 0,52·30
    assert out["vwap"][0] == pytest.approx((0.48 * 5 + 0.47 * 10 + 0.52 * 30) / 45)
    assert out["last"][1] == pytest.approx(0.50) and np.isnan(out["vwap"][1])
    assert np.isnan(out["last"][2])


def test_execution_price_is_first_buy_of_same_token_in_window():
    tr = _trades()
    up = (tr.token == 0) & tr.buy
    dn = (tr.token == 1) & tr.buy
    t = S
    # fenêtres [t + δ, t + δ + 2] (blocs, bornes incluses)
    for delta, exp_up, exp_dn in [(0, 0.55, np.nan), (1, 0.55, np.nan), (2, np.nan, 0.40), (3, np.nan, 0.40),
                                  (5, np.nan, np.nan), (7, 0.60, np.nan), (8, 0.60, np.nan)]:
        pu, _ = fb.first_trade_in(tr.ts[up], tr.price[up], t + delta, t + delta + 2)
        pd_, _ = fb.first_trade_in(tr.ts[dn], tr.price[dn], t + delta, t + delta + 2)
        assert (np.isnan(pu) and np.isnan(exp_up)) or pu == pytest.approx(exp_up), delta
        assert (np.isnan(pd_) and np.isnan(exp_dn)) or pd_ == pytest.approx(exp_dn), delta
    # les ventes (SELL) ne sont jamais des prix d'achat
    assert not tr.buy[1]


def test_choose_side_and_pnl_with_fees():
    side, edge = fb.choose_side(np.array([0.60, 0.40, 0.505]), np.array([0.52, 0.52, 0.51]),
                                np.array([0.49, 0.49, 0.50]), margin=0.0)
    assert list(side) == [1, -1, 0]
    assert edge[0] == pytest.approx(0.60 - 0.52 - 0.07 * 0.52 * 0.48)
    assert edge[1] == pytest.approx(0.60 - 0.49 - 0.07 * 0.49 * 0.51)
    side2, _ = fb.choose_side(np.array([0.60]), np.array([0.52]), np.array([0.49]), margin=0.10)
    assert side2[0] == 0
    # P&L : achat Up à 0,55 gagnant, Down à 0,40 perdant, sans exécution
    pnl = fb.pnl_per_share(np.array([1, -1, 1, 0]), np.array([0.55, 0.40, np.nan, 0.5]), np.array([1, 1, 1, 1]))
    assert pnl[0] == pytest.approx(1 - 0.55 - 0.07 * 0.55 * 0.45)
    assert pnl[1] == pytest.approx(0 - 0.40 - 0.07 * 0.40 * 0.60)
    assert np.isnan(pnl[2]) and np.isnan(pnl[3])


def test_estimated_asks():
    au, ad = fb.estimated_asks(np.array([0.52]))
    assert au[0] == pytest.approx(0.525) and ad[0] == pytest.approx(0.485)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def test_metrics_and_calibration():
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 20_000)
    y = (rng.uniform(0, 1, p.size) < p).astype(float)
    cal = fb.calibration_deciles(y, p)
    assert len(cal) == 10 and cal["n"].sum() == p.size
    assert np.abs(cal["p_mean"] - cal["up_rate"]).max() < 0.03
    assert fb.log_loss([1], [0.0])[0] == pytest.approx(-math.log(1e-3))
    tc = fb.tail_check(y, p, lo=0.02)
    assert tc["observe"].max() < 0.05


def test_stack_recovers_informative_input():
    rng = np.random.default_rng(1)
    n = 5000
    z = rng.normal(0, 1, n)
    p_true = 1 / (1 + np.exp(-z))
    y = (rng.uniform(0, 1, n) < p_true).astype(float)
    p_noise = np.full(n, 0.5) + rng.normal(0, 0.01, n)
    coef = fb.fit_stack(y, p_true, p_noise)
    assert coef[1] == pytest.approx(1.0, abs=0.15)
    assert abs(coef[2]) < 1.0


# ---------------------------------------------------------------------------
# Carnet réel
# ---------------------------------------------------------------------------
class _FakeMarket:
    def __init__(self, events):
        self.events = events


def test_book_states_and_fill_price():
    # événements : (rx, ts_ms, kind, asset, payload) ; instantané Up puis changement
    ev = [
        (1, 1000, "book", 0, ([(480, 50.0), (470, 100.0)], [(520, 10.0), (530, 40.0), (550, 500.0)])),
        (2, 2000, "pc", -1, [(0, 520, 0.0, "S", math.nan, math.nan)]),     # l'ask 0,52 disparaît
        (3, 3000, "trade", 0, (0.53, 5.0, "BUY", "x")),
    ]
    m = _FakeMarket(ev)
    st = fb.book_states_at(m, [1500, 999, 2000])
    assert not st[1]["fresh"]
    assert st[0]["asks"][0] == (0.52, 10.0)
    assert st[2]["asks"][0] == (0.53, 40.0)
    best, sz, avg, got = fb.fill_price(st[0], 1, 30)
    assert (best, sz, got) == (0.52, 10.0, 30)
    assert avg == pytest.approx((0.52 * 10 + 0.53 * 20) / 30)
    # Down : ask Down = 1 − bid Up
    best, sz, avg, got = fb.fill_price(st[0], -1, 60)
    assert best == pytest.approx(0.52) and sz == 50.0
    assert avg == pytest.approx((0.52 * 50 + 0.53 * 10) / 60)
    ts, mid = fb.mid_path(m)
    assert list(ts) == [1000, 2000] and mid[0] == pytest.approx(0.50) and mid[1] == pytest.approx(0.505)


def test_reaction_delay_on_constructed_paths():
    g = np.arange(0, 20_000, 20)
    P = np.where(g >= 5_000, 0.60, 0.50)                  # saut de la formule à 5,000 s
    mid = np.where(g >= 5_400, 0.56, 0.50)                # le carnet fait 6 points à 5,400 s
    ev = fb.reaction_delays(g, P, mid, jump=0.05)
    assert len(ev) == 1
    assert ev["delay_ms"].iloc[0] == pytest.approx(400)
    mid2 = np.where(g >= 5_400, 0.52, 0.50)               # moins de la moitié : censuré
    ev2 = fb.reaction_delays(g, P, mid2, jump=0.05)
    assert bool(ev2["censored"].iloc[0])
    mid3 = np.where(g >= 4_900, 0.56, 0.50)               # le carnet a bougé avant
    ev3 = fb.reaction_delays(g, P, mid3, jump=0.05)
    assert ev3["delay_ms"].iloc[0] == pytest.approx(-100)


def test_fetch_binance_1s_caches_complete_hours(tmp_path):
    calls = []

    def fake(sym, a, b):
        calls.append((a, b))
        sec = np.arange(a, b)
        return pd.DataFrame({"sec": sec, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0 + 0 * sec})

    h = 1_790_400_000 - 1_790_400_000 % 3600
    df = fb.fetch_binance_1s("BTCUSDT", h + 10, h + 7200 - 5, tmp_path, fetcher=fake, now=h + 7200 + 100)
    assert df["sec"].min() == h + 10 and df["sec"].max() == h + 7200 - 6
    assert len(list(tmp_path.glob("*.parquet"))) == 2
    fb.fetch_binance_1s("BTCUSDT", h + 10, h + 7200 - 5, tmp_path, fetcher=fake, now=h + 7200 + 100)
    assert len(calls) == 2                                  # deuxième appel : cache
    bars = fb.bars_1m_from_1s(df)
    assert len(bars) >= 118


def test_formula_path_ms_uses_only_past_trades():
    sec, close = _series(seed=11)
    P = fb.SecondPrices(sec, close)
    T = np.arange((S - 60) * 1000, (E - 5) * 1000, 250, dtype="int64")
    px = 100.0 * np.exp(np.random.default_rng(0).normal(0, 1e-4, T.size).cumsum())
    grid = np.arange((S - 45) * 1000, (E - 10) * 1000, 20, dtype="int64")
    a = fb.formula_path_ms(P, grid, T, px, S, E, 6e-5)
    cut = (S + 30) * 1000 + 130
    px2 = px.copy()
    px2[T > cut] *= 1.01
    close2 = close.copy()
    close2[sec + 1 > cut // 1000] *= 1.01
    b = fb.formula_path_ms(fb.SecondPrices(sec, close2), grid, T, px2, S, E, 6e-5)
    m = grid <= cut
    np.testing.assert_array_equal(a[m], b[m])
    assert not np.array_equal(a[~m], b[~m])
    # à une seconde entière, avec le dernier trade = close 1 s, on retrouve la formule 1 s
    t = S + 30
    g = np.array([t * 1000])
    lp = float(P.at(t))
    p_ms = fb.formula_path_ms(P, g, np.array([t * 1000 - 1]), np.array([math.exp(lp)]), S, E, 6e-5)
    p_s = fb.formula_probs(fb.formula_inputs(P, t, S, E), S, E, 6e-5)["p"]
    assert float(p_ms[0]) == pytest.approx(float(p_s), abs=1e-12)


def test_step_values():
    v = fb.step_values(np.array([10, 20, 30]), np.array([0.1, 0.2, 0.3]), np.array([5, 10, 25, 99]))
    assert np.isnan(v[0]) and list(v[1:]) == [0.1, 0.2, 0.3]
