"""Tests hors-ligne de l'étude du temps de réaction (tradebot.latency).

Alignement temporel « à la date » sur l'horloge locale, reconstruction de la série Binance 1 s et des
moyennes TWAP-60 depuis un bookTicker, formule exacte à points 1 s (égalité avec
``polymarket_formula`` aux secondes entières, causalité), σ EWMA causal, lecture des flux bruts,
carnet sur l'horloge locale (côté Down = 1 − bid Up), réaction du carnet, détection des
opportunités (durée de vie, origine), exécution à t + ℓ sur un carnet construit, P&L et frais,
bootstrap et latence critique.
"""

from __future__ import annotations

import gzip
import json
import math

import numpy as np
import pandas as pd
import pytest

from tradebot import latency as lt
from tradebot.polymarket_book import LiveMarket
from tradebot.polymarket_formula import twap_diff_moments

S = 1_790_000_000
E = S + 300


# ---------------------------------------------------------------------------
# Alignement temporel
# ---------------------------------------------------------------------------
def test_asof_values_includes_exact_timestamp_and_nan_before():
    t = np.array([10.0, 11.0, 12.5])
    v = np.array([1.0, 2.0, 3.0])
    q = np.array([9.99, 10.0, 10.5, 11.0, 12.49, 12.5, 100.0])
    out = lt.asof_values(t, v, q)
    assert np.isnan(out[0])
    np.testing.assert_allclose(out[1:], [1, 1, 2, 2, 3, 3])


def test_price_series_at_is_log_of_last_received():
    ps = lt.PriceSeries([1.0, 2.0], [100.0, 110.0])
    np.testing.assert_allclose(ps.at([1.5, 2.0, 3.0]), np.log([100, 110, 110]))
    assert np.isnan(ps.at(0.5))
    assert ps.max_gap(0.0, 5.0) == pytest.approx(3.0)


def test_binance_1s_and_twap_from_bookticker():
    # bookTicker : le milieu change à des instants irréguliers
    rx = np.array([S - 70.4, S - 59.7, S - 30.2, S - 30.0, S - 0.001, S + 0.5])
    mid = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 999.0])
    ps = lt.PriceSeries(rx, mid)
    secs, x = lt.seconds_asof(ps, S - 59, S)
    assert secs[0] == S - 59 and secs[-1] == S and secs.size == 60
    expected = np.empty(60)
    for i, s in enumerate(secs):
        expected[i] = mid[np.searchsorted(rx, s, side="right") - 1]       # dernier point reçu <= s
    np.testing.assert_allclose(np.exp(x), expected)
    # TWAP-60 à S = moyenne des points S−59 … S ; le point S+0,5 (après S) n'entre pas
    assert lt.twap_1s(ps, S) == pytest.approx(expected.mean())
    assert 999.0 not in np.exp(x)
    # à la seconde S − 30 exactement, le point reçu à S − 30,0 est pris (≤)
    assert np.exp(x[list(secs).index(S - 30)]) == pytest.approx(103.0)


# ---------------------------------------------------------------------------
# Formule
# ---------------------------------------------------------------------------
def _random_series(seed=0, sigma=1e-4):
    rng = np.random.default_rng(seed)
    t = np.arange(S - 200, E + 10, 0.1)
    x = np.log(80_000) + np.cumsum(rng.normal(0, sigma, t.size))
    return lt.PriceSeries(t, np.exp(x))


@pytest.mark.parametrize("t", [S - 100, S - 60, S - 59, S - 30, S - 1, S, S + 1, S + 150, E - 60, E - 59, E - 20,
                               E - 1, E])
def test_formula_matches_reference_at_integer_seconds(t):
    ps = _random_series()
    f = lt.TwapFormula(S, E).set_realized(ps)
    x = float(ps.at(float(t)))
    L = 60
    kw = {}
    if S - L < t <= S:
        kw["start_avg"] = float(np.mean(ps.at(np.arange(S - L + 1, t + 1, dtype=float))))
    if t > S:
        kw["price_to_beat"] = float(np.mean(ps.at(np.arange(S - L + 1, S + 1, dtype=float))))
    if t > E - L:
        kw["end_partial_sum"] = float(np.sum(ps.at(np.arange(E - L + 1, t + 1, dtype=float)))) / L
    m_ref, sd_ref, phase = twap_diff_moments(float(t), S, E, x, 1.0, dt=1.0, **kw)
    assert float(f.mean(t, x)) == pytest.approx(m_ref, abs=1e-12)
    assert math.sqrt(float(f.var_factor(t))) == pytest.approx(sd_ref, rel=1e-9, abs=1e-12)
    assert int(f.phase(t)) == phase


def test_formula_phase1_is_half_and_after_end_is_certain():
    ps = _random_series(1)
    f = lt.TwapFormula(S, E).set_realized(ps)
    assert float(f.prob(S - 100.3, ps.at(S - 100.3), 1e-4)) == pytest.approx(0.5)
    fk = float(f.mean(E + 1, np.nan))
    assert float(f.var_factor(E + 0.5)) == 0.0
    assert float(f.prob(E + 1, np.nan, 1e-4)) == (1.0 if fk >= 0 else 0.0)


def test_formula_variance_continuous_between_seconds_and_decreasing():
    f = lt.TwapFormula(S, E)
    t = np.arange(S - 70, E + 1, 0.05)
    v = f.var_factor(t)
    assert np.all(np.diff(v) <= 1e-9)
    assert np.max(np.abs(np.diff(v))) < 0.06        # pas de saut entre deux secondes


def test_formula_is_causal():
    ps = _random_series(2)
    f = lt.TwapFormula(S, E, lag_s=0.8).set_realized(ps)
    t = np.array([S - 40.35, S + 12.7, E - 30.2])
    p0 = f.prob(t, ps.at(t), 1.5e-4)
    for tc in t:
        # modifier tous les prix postérieurs à tc ne change pas P(tc)
        price2 = ps.price.copy()
        price2[ps.t > tc] *= 1.01
        ps2 = lt.PriceSeries(ps.t, price2)
        f2 = lt.TwapFormula(S, E, lag_s=0.8).set_realized(ps2)
        assert float(f2.prob(tc, ps2.at(tc), 1.5e-4)) == pytest.approx(float(p0[list(t).index(tc)]), abs=1e-12)


def test_formula_lag_shifts_information_time():
    ps = _random_series(3)
    f0 = lt.TwapFormula(S, E, lag_s=0.0).set_realized(ps)
    # série reçue 1 s plus tard (ps2(u) = ps(u − 1)) : Chainlink(s) = ps2(s + 1), soit lag = −1 s ;
    # vue à t + 1, elle donne exactement la formule de la série d'origine à t
    ps2 = lt.PriceSeries(ps.t + 1.0, ps.price)
    f1 = lt.TwapFormula(S, E, lag_s=-1.0).set_realized(ps2)
    t = np.array([S - 20.0, S + 30.0, E - 10.0])
    np.testing.assert_allclose(f1.prob(t + 1.0, ps2.at(t + 1.0), 1e-4), f0.prob(t, ps.at(t), 1e-4), atol=1e-12)
    # et un retard positif avance l'information : P(t) avec lag = L_c utilise Binance jusqu'à s − L_c
    f2 = lt.TwapFormula(S, E, lag_s=2.0).set_realized(ps)
    assert float(f2.var_factor(S + 10.0)) == pytest.approx(float(f0.var_factor(S + 12.0)))


def test_extra_sd_moderates_certainty():
    ps = _random_series(4)
    f = lt.TwapFormula(S, E).set_realized(ps)
    p_hard = float(f.prob(E - 0.5, ps.at(E - 0.5), 1e-4))
    p_soft = float(f.prob(E - 0.5, ps.at(E - 0.5), 1e-4, extra_sd=5e-4))
    assert abs(p_soft - 0.5) < abs(p_hard - 0.5)


def test_ewma_is_causal_and_uses_prior():
    rng = np.random.default_rng(5)
    x = np.cumsum(rng.normal(0, 1e-4, 2000))
    v = lt.ewma_var_1s(x, 600.0, 120)
    assert np.isnan(v[119]) and np.isfinite(v[120])
    x2 = x.copy()
    x2[1500:] += 0.01
    v2 = lt.ewma_var_1s(x2, 600.0, 120)
    np.testing.assert_allclose(v[:1500], v2[:1500])
    vp = lt.ewma_var_1s(np.full(50, 1.0), 600.0, 120, v_init=4e-8)
    assert vp[0] == pytest.approx(4e-8)
    lam = 0.5 ** (1 / 600.0)
    assert vp[10] == pytest.approx(4e-8 * lam ** 10)          # rendements nuls : décroissance pure
    sig = lt.SigmaSeries(np.arange(10, dtype="int64"), np.full(10, 1e-8))
    assert float(sig.at(5.9)) == pytest.approx(1e-4)


# ---------------------------------------------------------------------------
# Lecture des flux bruts
# ---------------------------------------------------------------------------
def _write(path, lines):
    with gzip.open(path, "wt") as fh:
        for rx, msg in lines:
            fh.write(json.dumps({"rx": rx, "msg": json.dumps(msg)}) + "\n")


def test_loaders(tmp_path):
    bn = tmp_path / "binance_2026092610.jsonl.gz"
    _write(bn, [(1_000_000_000_000, {"stream": "btcusdt@bookTicker", "data": {"s": "BTCUSDT", "b": "100", "B": "1",
                                                                              "a": "100.02", "A": "2", "u": 1}}),
                (1_000_050_000_000, {"stream": "btcusdt@aggTrade", "data": {"s": "BTCUSDT", "E": 1_000_040, "T": 1_000_039,
                                                                            "p": "100.01", "q": "0.5", "m": True}})])
    d = lt.load_binance([bn])["BTCUSDT"]
    assert d["book"]["mid"].iloc[0] == pytest.approx(100.01)
    assert d["book"]["rx"].iloc[0] == pytest.approx(1000.0)
    assert (d["trades"]["rx"] - d["trades"]["E"]).iloc[0] == pytest.approx(0.01)
    cb = tmp_path / "coinbase_2026092610.jsonl.gz"
    _write(cb, [(1_790_000_000_500_000_000, {"type": "ticker", "product_id": "BTC-USD", "price": "1", "best_bid": "1",
                                             "best_ask": "1.1", "time": "2026-09-21T14:13:20.250000Z"})])
    c = lt.load_coinbase([cb])["BTC-USD"]
    assert c["time"].iloc[0] == pytest.approx(1_790_000_000.25)
    rt = tmp_path / "rtds_2026092610.jsonl.gz"
    _write(rt, [(2_000_000_000_000, {"topic": "crypto_prices", "type": "subscribe",
                                     "payload": {"symbol": "btc/usd", "data": [{"timestamp": 1_998_000, "value": 5.0},
                                                                               {"timestamp": 1_999_000, "value": 6.0}]}}),
                (2_001_000_000_000, {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 2_000_500,
                                     "payload": {"symbol": "btc/usd", "timestamp": 1_999_000, "value": 6.5}}),
                (2_002_000_000_000, {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 2_001_400,
                                     "payload": {"symbol": "btc/usd", "timestamp": 2_000_000, "value": 7.0}})])
    r = lt.load_rtds([rt])["btc/usd"]
    assert list(r["ts"]) == [1998.0, 1999.0, 2000.0]
    assert r.loc[r["ts"] == 2000.0, "msg_ts"].iloc[0] == pytest.approx(2001.4)
    # doublon (1999) : la mise à jour temps réel (avec heure de publication) est préférée à l'historique
    assert r["snapshot"].tolist() == [True, False, False]
    assert r.loc[r["ts"] == 1999.0, "value"].iloc[0] == 6.5


# ---------------------------------------------------------------------------
# Carnet sur l'horloge locale
# ---------------------------------------------------------------------------
def _market(events):
    meta = {"slug": "btc-updown-5m-x", "asset": "btc", "duration": "5m",
            "start": pd.Timestamp(S, unit="s", tz="UTC").isoformat(),
            "end": pd.Timestamp(E, unit="s", tz="UTC").isoformat(), "token_up": "1", "token_down": "2"}
    return LiveMarket(slug="btc-updown-5m-x", meta=meta, events=events)


def _ns(t):
    return int(round(t * 1e9))


def test_book_timeline_and_down_side():
    ev = [
        (_ns(S + 1.0), (S + 1) * 1000 - 20, "book", 0, ([(480, 10.0), (470, 5.0)], [(500, 7.0), (510, 3.0)])),
        (_ns(S + 2.0), (S + 2) * 1000 - 20, "pc", -1, [(0, 500, 0.0, "S", 0.48, 0.51)]),     # ask 0,50 vidé
        (_ns(S + 3.0), (S + 3) * 1000 - 20, "pc", -1, [(1, 530, 8.0, "B", 0.53, 0.55)]),     # bid Down 0,53 = ask Up 0,47 ?
        (_ns(S + 3.5), (S + 3.5) * 1000, "trade", 0, (0.51, 3.0, "BUY", "0xa")),
    ]
    tl = lt.book_timeline(_market(ev))
    assert tl.rx.tolist() == [S + 1.0, S + 2.0, S + 3.0]
    np.testing.assert_allclose(tl.ask, [0.50, 0.51, 0.47])
    np.testing.assert_allclose(tl.bid, [0.48, 0.48, 0.48])
    a_dn, s_dn = tl.side_at(-1, [S + 0.5, S + 1.5, S + 3.2])
    assert np.isnan(a_dn[0]) and s_dn[0] == 0
    np.testing.assert_allclose(a_dn[1:], [0.52, 0.52])          # ask Down = 1 − meilleur bid Up
    np.testing.assert_allclose(s_dn[1:], [10.0, 10.0])
    a_up, s_up = tl.side_at(1, S + 3.2)
    assert float(a_up) == pytest.approx(0.47) and float(s_up) == 8.0
    assert tl.trades["consumes"].iloc[0] == "ask" and tl.trades["level_up"].iloc[0] == pytest.approx(0.51)
    lad = lt.ladders_at(_market(ev), [S + 1.5])[0]
    assert lad["up"][0] == (0.5, 7.0) and lad["down"][0] == (0.52, 10.0)


def test_book_timeline_consolidates_same_server_timestamp():
    ev = [
        (_ns(S + 1.0), (S + 1) * 1000, "book", 0, ([(480, 10.0)], [(500, 7.0)])),
        (_ns(S + 2.0), (S + 2) * 1000, "pc", -1, [(0, 500, 0.0, "S", 0.48, 0.52)]),        # ask 0,50 annulé…
        (_ns(S + 2.00005), (S + 2) * 1000, "pc", -1, [(0, 490, 4.0, "S", 0.48, 0.49)]),    # … remplacé à 0,49 (même ts)
        (_ns(S + 3.0), (S + 3) * 1000, "pc", -1, [(0, 470, 2.0, "B", 0.47, 0.49)]),
    ]
    tl = lt.book_timeline(_market(ev))
    assert tl.rx.tolist() == pytest.approx([S + 1.0, S + 2.00005, S + 3.0])
    np.testing.assert_allclose(tl.ask, [0.50, 0.49, 0.49])       # l'état transitoire « pas d'ask » disparaît
    raw = lt.book_timeline(_market(ev), consolidate=False)
    assert raw.rx.size == 4 and np.isnan(raw.ask[1])


def _timeline(rx, bid, ask, bid_sz=None, ask_sz=None, trades=None):
    n = len(rx)
    tl = lt.BookTimeline(np.asarray(rx, float), np.asarray(rx, float), np.asarray(bid, float), np.asarray(ask, float),
                         np.asarray(bid_sz if bid_sz is not None else [100.0] * n, float),
                         np.asarray(ask_sz if ask_sz is not None else [100.0] * n, float))
    tl.trades = trades if trades is not None else pd.DataFrame(columns=["rx", "ts", "consumes", "level_up", "size"])
    return tl


# ---------------------------------------------------------------------------
# Réaction du carnet
# ---------------------------------------------------------------------------
def test_reaction_events_delay():
    grid = np.round(np.arange(0.0, 20.0, 0.1), 3)
    P = np.where(grid >= 10.0, 0.62, 0.50)                   # saut de 12 points à t = 10 s
    tl = _timeline([0.0, 10.15, 10.3, 11.0], [0.49, 0.49, 0.55, 0.60], [0.51, 0.53, 0.57, 0.62])
    r = lt.reaction_events(grid, P, tl, lambda t: np.where(np.asarray(t) >= 10.0, 0.62, 0.50), np.array([9.95, 10.0]),
                           jump=0.05)
    assert len(r) == 1
    row = r.iloc[0]
    assert row["t0"] == pytest.approx(10.0)
    assert row["delay_first_ms"] == pytest.approx(150.0, abs=1e-6)     # l'ask Up monte à 10,15
    assert row["delay_50_ms"] == pytest.approx(300.0, abs=1e-6)        # milieu 0,56 >= 0,50 + 0,06
    assert row["delay_90_ms"] == pytest.approx(1000.0, abs=1e-6)       # milieu 0,61 >= 0,50 + 0,108
    assert np.isnan(row["delay_placebo_ms"])          # aucune retouche entre 7 s et 10 s


# ---------------------------------------------------------------------------
# Opportunités et exécution
# ---------------------------------------------------------------------------
def test_taker_cost_and_fee():
    assert float(lt.taker_cost(0.5)) == pytest.approx(0.5175)
    assert float(lt.taker_cost(0.9)) == pytest.approx(0.9063)


def test_find_opportunities_life_and_trigger():
    T = np.round(np.arange(0.0, 10.0, 0.1), 3)
    P = np.where(T >= 2.0, 0.70, 0.55)                            # saut de la formule à 2 s
    ask_up = np.where(T < 2.35, 0.56, 0.72)                        # le teneur relève son ask à 2,35 s
    ask_up = np.where((T >= 2.2) & (T < 2.35), 0.55, ask_up)       # ask amélioré entre-temps : même opportunité
    bid_up = np.full(T.size, 0.53)
    ask_dn = np.round(1 - bid_up, 3)
    o = lt.find_opportunities(T, P, ask_up, np.full(T.size, 30.0), ask_dn, np.full(T.size, 40.0), margin=0.0)
    first = o.iloc[0]
    assert first["side"] == "up" and first["t_start"] == pytest.approx(2.0) and first["ask0"] == pytest.approx(0.56)
    assert first["life_book_ms"] == pytest.approx(400.0, abs=1e-6)        # jusqu'à ce que l'ask dépasse 0,56 (2,4 s)
    assert first["edge0"] == pytest.approx(0.70 - 0.56 - 0.07 * 0.56 * 0.44)
    assert first["trigger"] == "saut"
    # le niveau 0,55 (apparu à 2,2 s, avantage déjà là 1 s plus tôt ? non : P = 0,55 à 1,2 s) -> saut aussi
    second = o.iloc[1]
    assert second["ask0"] == pytest.approx(0.55) and second["t_start"] == pytest.approx(2.2)
    assert not o["censored"].iloc[:2].any()
    # marge : 16 c n'est pas atteint (avantage ≈ 12 c)
    assert lt.find_opportunities(T, P, ask_up, np.full(T.size, 30.0), ask_dn, np.full(T.size, 40.0),
                                 margin=0.16).empty
    # asks hors de l'intervalle retenu : aucune opportunité
    assert lt.find_opportunities(T, P, ask_up, np.full(T.size, 30.0), ask_dn, np.full(T.size, 40.0),
                                 ask_range=(0.60, 0.95)).empty


def test_persistent_trigger_and_live_level_not_duplicated():
    T = np.round(np.arange(0.0, 5.0, 0.1), 3)
    P = np.full(T.size, 0.30)                                   # Down vaut 0,70
    bid_up = np.where((T >= 1.0) & (T < 1.2), 0.40, 0.45)       # ask Down 0,55 puis 0,60 pendant 0,2 s puis 0,55
    ask_dn = np.round(1 - bid_up, 3)
    ask_up = np.full(T.size, 0.47)
    o = lt.find_opportunities(T, P, ask_up, np.full(T.size, 10.0), ask_dn, np.full(T.size, 20.0), margin=0.0)
    assert (o["side"] == "down").all()
    # 0,55 à 0 s (fin à 1,0 s : l'ask passe à 0,60), 0,60 à 1,0 s, 0,55 de nouveau à 1,2 s
    assert o["ask0"].tolist() == [0.55, 0.6, 0.55]
    assert o["trigger"].tolist()[1:] == ["persistante", "persistante"]
    assert o["life_book_ms"].iloc[0] == pytest.approx(1000.0, abs=1e-6)


def test_simulate_fills_at_latency_and_pnl():
    # carnet : ask Up 0,56 × 30 jusqu'à 2,25 s (vu d'ici), puis 0,62 × 80, puis 0,75
    tl = _timeline([0.0, 2.25, 3.0], [0.50, 0.55, 0.70], [0.56, 0.62, 0.75], ask_sz=[30.0, 80.0, 5.0])
    opps = pd.DataFrame({"side": ["up"], "t_start": [2.0], "p_side": [0.70]})
    f = lt.simulate_fills(opps, tl, latencies_ms=(0, 100, 250, 500, 1500), margin=0.0, outcome_up=True,
                          markout_s=10.0)
    f = f.set_index("latency_ms")
    # ℓ = 0 et 100 ms : ask 0,56, 30 parts ; P&L = 30 × (1 − 0,56 − frais)
    for lat in (0, 100):
        assert f.loc[lat, "filled"] and f.loc[lat, "fill_ask"] == pytest.approx(0.56)
        assert f.loc[lat, "qty"] == 30.0
        assert f.loc[lat, "pnl"] == pytest.approx(30 * (1 - 0.56 - 0.07 * 0.56 * 0.44))
    # ℓ = 250 et 500 ms : ask 0,62 ; 0,70 − 0,62 − frais > 0 -> achat plafonné à 50 parts
    assert f.loc[250, "fill_ask"] == pytest.approx(0.62) and f.loc[500, "qty"] == 50.0
    # ℓ = 1,5 s : ask 0,75 > P(t) − frais -> pas d'achat
    assert not f.loc[1500, "filled"] and f.loc[1500, "pnl"] == 0.0
    # valorisation : milieu 10 s plus tard = (0,70 + 0,75)/2
    assert f.loc[0, "markout"] == pytest.approx(30 * ((0.70 + 0.75) / 2 - lt.taker_cost(0.56)))
    # clôture dépassée : valorisation à l'issue
    f2 = lt.simulate_fills(opps, tl, latencies_ms=(0,), outcome_up=False, markout_s=10.0, end_s=5.0)
    assert f2["markout"].iloc[0] == pytest.approx(30 * (0 - lt.taker_cost(0.56)))
    assert f2["pnl"].iloc[0] == pytest.approx(f2["markout"].iloc[0])


def test_simulate_fills_down_side_uses_one_minus_bid():
    tl = _timeline([0.0], [0.40], [0.42], bid_sz=[12.0])
    opps = pd.DataFrame({"side": ["down"], "t_start": [1.0], "p_side": [0.75]})
    f = lt.simulate_fills(opps, tl, latencies_ms=(0,), outcome_up=False)
    assert f["fill_ask"].iloc[0] == pytest.approx(0.60) and f["qty"].iloc[0] == 12.0
    assert f["pnl"].iloc[0] == pytest.approx(12 * (1 - 0.60 - 0.07 * 0.6 * 0.4))
    f_margin = lt.simulate_fills(opps, tl, latencies_ms=(0,), margin=0.20, outcome_up=False)
    assert not f_margin["filled"].iloc[0]                 # 0,75 − 0,6168 < 0,20


def test_removal_cause():
    trades = pd.DataFrame({"rx": [2.3], "ts": [2.3], "consumes": ["ask"], "level_up": [0.56], "size": [30.0]})
    opps = pd.DataFrame({"side": ["up", "down"], "t_start": [2.0, 5.0], "ask0": [0.56, 0.60], "t_end_book": [2.35, 5.2],
                         "censored": [False, False]})
    r = lt.removal_cause(opps, trades)
    assert r["removed_by"].tolist() == ["preneur", "retrait"]
    assert r["taken_size"].iloc[0] == 30.0


def test_pnl_curve_bootstrap_breakeven():
    rows = []
    for m in range(6):
        for lat, v in ((0, 2.0), (100, 1.0), (200, -1.0)):
            rows.append({"market": f"m{m}", "latency_ms": lat, "filled": True, "qty": 10.0, "pnl": v + 0.1 * m,
                         "opp_id": 0})
    f = pd.DataFrame(rows)
    c = lt.pnl_curve(f)
    assert c["pnl_per_opp"].tolist() == pytest.approx([2.25, 1.25, -0.75])
    bs = lt.bootstrap_by_group(f, n_boot=200)
    assert bs["reps"].shape == (200, 3)
    np.testing.assert_allclose(bs["point"], [2.25, 1.25, -0.75])
    assert lt.breakeven_latency(bs["latencies"], bs["point"]) == pytest.approx(100 + 100 * 1.25 / 2.0)
    assert lt.breakeven_latency([0, 100], [-1.0, 2.0]) == 0.0
    assert lt.breakeven_latency([0, 100], [1.0, 2.0]) == math.inf
    bs_h = lt.bootstrap_by_group(f, n_boot=0, denom=2.0)
    np.testing.assert_allclose(bs_h["point"], [13.5 / 2, 7.5 / 2, -4.5 / 2])


def test_isotonic_and_significance():
    np.testing.assert_allclose(lt.isotonic_decreasing([3.0, 1.0, 2.0, -1.0, 0.0]), [3.0, 1.5, 1.5, -0.5, -0.5])
    np.testing.assert_allclose(lt.isotonic_decreasing([1.0, 2.0, 3.0]), [2.0, 2.0, 2.0])
    lats = [0, 100, 200, 300]
    # courbe brute : positive, négative, re-positive -> ℓ* brut 150, isotone : moyenne des blocs
    raw = [2.0, -1.0, 1.0, -2.0]
    assert lt.breakeven_latency(lats, raw) == pytest.approx(100 * 2 / 3)
    iso = lt.isotonic_decreasing(raw)
    np.testing.assert_allclose(iso, [2.0, 0.0, 0.0, -2.0])
    assert lt.breakeven_latency(lats, raw, monotone=True) == pytest.approx(100.0)
    assert lt.last_significant_latency(lats, [0.5, 0.1, -0.2, 0.3]) == 100
    assert math.isnan(lt.last_significant_latency(lats, [-0.5, 0.1, 0.2, 0.3]))
    assert lt.last_significant_latency(lats, [1, 1, 1, 1]) == 300


def test_lag_scan_recovers_known_lag():
    rng = np.random.default_rng(9)
    t = np.arange(0.0, 2000.0, 0.05)
    x = np.cumsum(rng.normal(0, 1e-4, t.size))
    bn = lt.PriceSeries(t, np.exp(x))
    ts = np.arange(100.0, 1900.0, 1.0)
    ref = bn.at(ts - 1.3)                                       # Chainlink = Binance il y a 1,3 s
    sc = lt.lag_scan(ts, ref, bn, np.round(np.arange(0.0, 3.01, 0.1), 3))
    assert sc.loc[sc["corr"].idxmax(), "lag_ms"] == 1300


def test_cex_files_filters_hours(tmp_path):
    for h in ("2026092609", "2026092610", "2026092612"):
        (tmp_path / f"binance_{h}.jsonl.gz").write_bytes(b"")
    (tmp_path / "coinbase_2026092610.jsonl.gz").write_bytes(b"")
    t10 = pd.Timestamp("2026-09-26 10:30", tz="UTC").timestamp()
    names = [p.name for p in lt.cex_files("binance", t10, t10 + 60, cex_dir=tmp_path)]
    assert names == ["binance_2026092610.jsonl.gz"]
    assert len(lt.cex_files("binance", cex_dir=tmp_path)) == 3
