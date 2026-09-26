"""Tests hors-ligne du simulateur maker sur carnet réel (tradebot.polymarket_maker_live).

Scénarios construits : file d'attente consommée par les trades du côté opposé (avec la
complémentarité Up/Down), traversée du niveau, annulation, exécution partielle, latence, borne
« file ≤ taille affichée », ordres non posables sans instantané ; signal TWAP partiel ; juste valeur
Φ(d/σ) ; re-cotation de la stratégie (c) ; résumé et P&L à la résolution.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket_book as bk
from tradebot import polymarket_maker_live as mk

S = 1_790_000_000
E = S + 300


def ev_book(t_rel: float, bids: dict, asks: dict, asset: int = bk.UP):
    ts = int((S + t_rel) * 1000)
    return (ts * 10**6, ts, "book", asset, ([(int(round(p * 1000)), s) for p, s in bids.items()],
                                           [(int(round(p * 1000)), s) for p, s in asks.items()]))


def ev_pc(t_rel: float, changes: list):
    """changes : [(asset, price, size, side)] dans le repère du jeton ``asset``."""
    ts = int((S + t_rel) * 1000)
    return (ts * 10**6, ts, "pc", -1, [(a, int(round(p * 1000)), s, side, math.nan, math.nan) for a, p, s, side in changes])


def ev_trade(t_rel: float, asset: int, side: str, price: float, size: float, tx: str = "0x"):
    ts = int((S + t_rel) * 1000)
    return (ts * 10**6, ts, "trade", asset, (price, size, side, tx))


def make_market(events, resolved_up=True, duration_s: int = 300, price_to_beat=None) -> bk.LiveMarket:
    meta = {"slug": "btc-updown-5m-1790000000", "asset": "btc", "duration": "5m",
            "start": pd.Timestamp(S, unit="s", tz="UTC").isoformat(),
            "end": pd.Timestamp(S + duration_s, unit="s", tz="UTC").isoformat(),
            "token_up": "1", "token_down": "2", "fee_schedule": {"rate": 0.07, "exponent": 1, "rebateRate": 0.2},
            "resolved_up": resolved_up, "event_metadata": {"priceToBeat": price_to_beat} if price_to_beat else None}
    return bk.LiveMarket(slug=meta["slug"], meta=meta, events=list(events))


BASE = [ev_book(-100, bids={0.49: 100.0, 0.48: 50.0}, asks={0.51: 80.0, 0.52: 40.0})]


def test_queue_then_fill_with_complementary_trades():
    # bid Up à 0,49 posé à S−30 (latence 0) : 100 devant. Deux trades preneurs consomment 0,49 :
    # un SELL Up à 0,49 (60) puis un BUY Down à 0,51 (50) -> file 100 épuisée, puis 10 pour nous.
    events = BASE + [ev_trade(-20, bk.UP, "SELL", 0.49, 60.0, "a"),
                     ev_trade(-15, bk.DOWN, "BUY", 0.51, 50.0, "b"),
                     ev_trade(-10, bk.DOWN, "BUY", 0.51, 5.0, "c")]
    m = make_market(events)
    o = mk.Order("t", "up", 0.49, S - 30, S, size=10.0)
    (r,) = mk.simulate(m, [o], latency_ms=0)
    assert r.q_ahead0 == 100.0
    assert r.status == "filled" and r.filled == 10.0 and r.fill_reason == "queue" and not r.fill_by_traversal
    assert math.isclose(r.t_fill, S - 15) and math.isclose(r.t_full, S - 15)
    assert r.traded_at_level == 110.0 and r.q_ahead_end == 0.0
    df = mk.results_frame([r], m)
    assert df.loc[0, "executed"] and df.loc[0, "pnl_per_share"] == pytest.approx(1 - 0.49)
    assert df.loc[0, "delay_s"] == pytest.approx(15.0)
    assert df.loc[0, "rebate_per_share"] == pytest.approx(0.2 * 0.07 * 0.49 * 0.51)


def test_partial_fill_then_cancel():
    events = BASE + [ev_trade(-20, bk.UP, "SELL", 0.49, 104.0, "a")]      # 100 pour la file, 4 pour nous
    m = make_market(events, resolved_up=False)
    o = mk.Order("t", "up", 0.49, S - 30, S, size=10.0)
    (r,) = mk.simulate(m, [o], latency_ms=0)
    assert r.status == "partial" and r.filled == 4.0 and math.isclose(r.t_fill, S - 20) and np.isnan(r.t_full)
    df = mk.results_frame([r], m)
    assert df.loc[0, "pnl_per_share"] == pytest.approx(-0.49)
    assert df.loc[0, "pnl_per_placed_share"] == pytest.approx(-0.49 * 0.4)


def test_traversal_fills_entirely_and_other_side_ignored():
    # ordre Down à 0,49 = ask Up à 0,51 ; un BUY Up à 0,52 traverse 0,51 -> exécution totale.
    events = BASE + [ev_trade(-20, bk.UP, "SELL", 0.49, 500.0, "x"),      # côté bid : sans effet
                     ev_trade(-10, bk.UP, "BUY", 0.52, 5.0, "y")]
    m = make_market(events, resolved_up=True)
    o = mk.Order("t", "down", 0.49, S - 30, S, size=10.0)
    (r,) = mk.simulate(m, [o], latency_ms=0)
    assert r.q_ahead0 == 80.0 and r.status == "filled" and r.fill_by_traversal and math.isclose(r.t_fill, S - 10)
    assert r.fill_reason == "traversal"
    df = mk.results_frame([r], m)
    assert df.loc[0, "pnl_per_share"] == pytest.approx(-0.49)         # Up a gagné, on a acheté Down


def test_cancellation_ahead_reduces_queue_via_displayed_size():
    # 100 devant nous ; le niveau tombe à 30 (annulations) -> file 30 ; un trade de 35 nous exécute 5.
    events = BASE + [ev_pc(-25, [(bk.UP, 0.49, 30.0, "B"), (bk.DOWN, 0.51, 30.0, "S")]),
                     ev_trade(-20, bk.UP, "SELL", 0.49, 35.0, "a")]
    m = make_market(events)
    (r,) = mk.simulate(m, [mk.Order("t", "up", 0.49, S - 30, S, size=10.0)], latency_ms=0)
    assert r.status == "partial" and r.filled == 5.0 and r.q_ahead_end == 0.0


def test_cancel_time_latency_and_no_book():
    events = BASE + [ev_trade(5, bk.UP, "SELL", 0.49, 200.0, "late")]
    m = make_market(events)
    o1 = mk.Order("t", "up", 0.49, S - 30, S, size=10.0)            # annulé à S, avant le trade
    o2 = mk.Order("t", "up", 0.49, S - 30, S + 30, size=10.0)       # vivant à S+5 -> exécuté
    o3 = mk.Order("t", "up", 0.49, S - 200, S - 150, size=10.0)     # avant tout instantané
    r1, r2, r3 = mk.simulate(m, [o1, o2, o3], latency_ms=500)
    assert r1.status == "cancelled" and r1.filled == 0
    assert r2.status == "filled" and math.isclose(r2.t_placed, S - 29.5)
    assert r3.status == "no_book" and np.isnan(r3.q_ahead0)
    df = mk.results_frame([r1, r2, r3], m)
    assert list(df["status"]) == ["cancelled", "filled", "no_book"]
    assert df.loc[0, "pnl_per_placed_share"] == 0.0 and np.isnan(df.loc[0, "delay_s"])


def test_crossing_orders_and_cross_fill():
    # bid Up à 0,51 alors que le meilleur ask est 0,51 : ordre preneur, pas maker -> "crossing".
    m = make_market(BASE + [ev_trade(-25, bk.UP, "BUY", 0.51, 1.0, "z")])
    (r,) = mk.simulate(m, [mk.Order("t", "up", 0.51, S - 30, S, size=10.0)], latency_ms=0)
    assert r.status == "crossing" and np.isnan(r.q_ahead0) and math.isclose(r.t_placed, S - 30)
    # achat Down à 0,48 = ask Up à 0,52 : ne croise pas (meilleur bid 0,49) ; puis un bid Up à 0,52
    # apparaît dans le carnet : il nous croise -> exécution "cross".
    events = BASE + [ev_pc(-20, [(bk.UP, 0.52, 15.0, "B"), (bk.DOWN, 0.48, 15.0, "S")])]
    m2 = make_market(events, resolved_up=False)
    (r2,) = mk.simulate(m2, [mk.Order("t", "down", 0.48, S - 30, S, size=10.0)], latency_ms=0)
    assert r2.status == "filled" and r2.fill_reason == "cross" and math.isclose(r2.t_fill, S - 20)
    df = mk.results_frame([r, r2], m2)
    assert list(df["placed"]) == [False, True] and list(df["status"]) == ["crossing", "filled"]
    df["slot"] = 1
    summ = mk.summarize(df, by=["strategy"]).iloc[0]
    assert summ["n_orders"] == 1 and summ["n_crossing"] == 1 and summ["share_by_cross"] == 1.0


def test_orders_fair_value_capped_by_bbo():
    m = make_market(BASE)
    t = np.arange(S, E + 1)
    fv = pd.DataFrame({"t": t, "t_rel_s": t - S, "spot": 1.0, "d": 0.0, "sd": 1.0, "p_up": 0.80})
    bbo = pd.DataFrame({"t_rel_s": np.arange(0, 301, 10.0), "best_bid": 0.60, "best_ask": 0.61})
    orders = mk.orders_fair_value(m, fv, step_s=10, bbo=bbo)
    ups = [o for o in orders if o.side == "up"]
    dns = [o for o in orders if o.side == "down"]
    assert len(ups) == 1 and ups[0].price == pytest.approx(0.60)      # 0,79 plafonné à ask − 1 c
    assert len(dns) == 1 and dns[0].price == pytest.approx(0.19)      # (1 − 0,80) − 0,01, sous le plafond 0,39


def test_partial_twap_gap_and_fair_value():
    idx = pd.date_range(pd.Timestamp(S - 120, unit="s", tz="UTC"), periods=430, freq="1s")
    close = np.full(430, 100.0)
    # hausse régulière dans [S−60, S−30) : spot(S−30) > moyenne -> gap > 0
    close[60:90] = 100.0 + np.arange(30) * 0.01
    close[90:] = close[89]
    bars = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)
    gap = mk.partial_twap_gap(bars, S)
    assert gap > 0 and math.isclose(gap, math.log(close[89] / close[60:90].mean()))
    assert np.isnan(mk.partial_twap_gap(bars.iloc[:70], S))          # fenêtre incomplète
    # juste valeur : spot au-dessus de K -> p_up > 0,5 et croissante vers 1 quand E approche
    fv = mk.fair_value_series(bars, S, E, price_to_beat=100.0)
    assert len(fv) == 301 and (fv["p_up"] > 0.5).all()
    assert fv["p_up"].iloc[-1] >= fv["p_up"].iloc[0]
    assert fv["p_up"].iloc[-1] == pytest.approx(1.0)                 # TWAP final connu, > K
    fv2 = mk.fair_value_series(bars, S, E, price_to_beat=200.0)
    assert (fv2["p_up"] < 0.5).all()
    fv3 = mk.fair_value_series(bars, S, E)                          # K = TWAP60(S) Binance
    assert fv3["p_up"].iloc[0] > 0.5


def test_orders_fair_value_requote_only_on_level_change():
    m = make_market(BASE)
    t = np.arange(S, E + 1)
    p = np.where(t < S + 100, 0.55, 0.70)                            # saut de juste valeur à S+100
    fv = pd.DataFrame({"t": t, "t_rel_s": t - S, "spot": 1.0, "d": 0.0, "sd": 1.0, "p_up": p})
    orders = mk.orders_fair_value(m, fv, step_s=10, margin=0.01, start_offset_s=10, end_offset_s=10)
    ups = sorted([o for o in orders if o.side == "up"], key=lambda o: o.t_place)
    dns = sorted([o for o in orders if o.side == "down"], key=lambda o: o.t_place)
    assert [o.price for o in ups] == [0.54, 0.69] and [o.price for o in dns] == [0.44, 0.29]
    assert ups[0].t_place == S + 10 and ups[0].t_cancel == S + 100 and ups[1].t_place == S + 100
    assert ups[1].t_cancel == E - 10


def test_signal_and_two_sided_orders_and_summary():
    m = make_market(BASE + [ev_trade(-5, bk.UP, "SELL", 0.49, 150.0, "a")], resolved_up=True)
    sig = mk.orders_signal(m, gap=-1e-4)
    assert len(sig) == 9 and {o.side for o in sig} == {"down"} and {o.price for o in sig} == {0.48, 0.49, 0.50}
    assert mk.orders_signal(m, gap=float("nan")) == []
    two = mk.orders_two_sided(m)
    assert len(two) == 6 and {o.side for o in two} == {"up", "down"}
    res = mk.simulate(m, sig + two, latency_ms=0)
    df = mk.results_frame(res, m)
    df["slot"] = 1
    summ = mk.summarize(df).set_index(["strategy", "label"])
    # signal Down à 0,49 = ask Up à 0,51 : aucun trade côté ask -> non exécuté ; two_sided Up à 0,49 exécuté
    assert summ.loc[("signal", "0.49/S+0"), "fill_rate"] == 0.0
    ts_ = summ.loc[("two_sided", "0.49/S+0")]
    assert ts_["n_orders"] == 2 and ts_["fill_rate"] == 0.5
    assert ts_["pnl_per_executed_c"] == pytest.approx(51.0) and ts_["pnl_per_placed_c"] == pytest.approx(25.5)
    assert ts_["win_rate_executed"] == 1.0 and ts_["win_rate_not_executed"] == 0.0
    assert ts_["adverse_selection_pts"] == pytest.approx(100.0)
