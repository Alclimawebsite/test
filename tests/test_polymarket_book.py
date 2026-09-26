"""Tests hors-ligne de la reconstruction du carnet (tradebot.polymarket_book).

Fichiers synthétiques dans les deux formats du collecteur (messages bruts v1, lignes compactes v2),
symétrie Up/Down, instantanés puis deltas, séries sur grille, trades avec niveau consommé et état
« avant », contrôle ``best_bid_ask`` (émis avant le ``price_change``), fichier tronqué, segments
qui se recouvrent (trades dédoublonnés).
"""

from __future__ import annotations

import gzip
import json
import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket_book as bk

UP_ID = "111"
DOWN_ID = "222"
S = 1_790_000_000           # début de fenêtre (s)
META = {"slug": "btc-updown-5m-1790000000", "asset": "btc", "duration": "5m",
        "start": pd.Timestamp(S, unit="s", tz="UTC").isoformat(),
        "end": pd.Timestamp(S + 300, unit="s", tz="UTC").isoformat(),
        "token_up": UP_ID, "token_down": DOWN_ID, "fee_schedule": {"rate": 0.07, "exponent": 1},
        "resolved_up": None}


def ms(t_rel: float) -> int:
    return int((S + t_rel) * 1000)


def raw_book(asset: str, ts: int, bids: list, asks: list) -> str:
    return json.dumps({"event_type": "book", "asset_id": asset, "market": "0xm", "timestamp": str(ts),
                       "bids": [{"price": f"{p:.2f}", "size": f"{s}"} for p, s in bids],
                       "asks": [{"price": f"{p:.2f}", "size": f"{s}"} for p, s in asks]})


def raw_pc(ts: int, changes: list) -> str:
    """changes : [(asset, price, size, side, bb, ba)]"""
    return json.dumps({"event_type": "price_change", "market": "0xm", "timestamp": str(ts),
                       "price_changes": [{"asset_id": a, "price": f"{p:.2f}", "size": f"{s}", "side": side,
                                          "best_bid": f"{bb:.2f}", "best_ask": f"{ba:.2f}", "hash": "h"}
                                         for a, p, s, side, bb, ba in changes]})


def raw_trade(asset: str, ts: int, price: float, size: float, side: str, tx: str) -> str:
    return json.dumps({"event_type": "last_trade_price", "asset_id": asset, "market": "0xm", "timestamp": str(ts),
                       "price": f"{price:.2f}", "size": f"{size}", "side": side, "fee_rate_bps": "0",
                       "transaction_hash": tx})


def raw_bba(asset: str, ts: int, bb: float, ba: float) -> str:
    return json.dumps({"event_type": "best_bid_ask", "asset_id": asset, "market": "0xm", "timestamp": str(ts),
                       "best_bid": f"{bb:.2f}", "best_ask": f"{ba:.2f}", "spread": f"{ba - bb:.2f}"})


def write_v1(path, msgs: list[tuple[int, str]]) -> None:
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"rx": msgs[0][0] * 10**6, "ev": "connect", "n": 0}) + "\n")
        for rx_ms, raw in msgs:
            fh.write(json.dumps({"rx": rx_ms * 10**6, "msg": raw}) + "\n")


def write_v2(path, lines: list[dict]) -> None:
    with gzip.open(path, "wt") as fh:
        for d in lines:
            fh.write(json.dumps(d, separators=(",", ":")) + "\n")


@pytest.fixture
def live_dir(tmp_path):
    (tmp_path / f"{META['slug']}.meta.json").write_text(json.dumps(META))
    return tmp_path


def scenario_v1() -> list[tuple[int, str]]:
    """Carnet Up 0,49/0,51 à S−100 s ; le Down publie le miroir ; puis deltas et trades."""
    msgs = [
        (ms(-100), raw_book(UP_ID, ms(-100), bids=[(0.49, 100), (0.48, 200), (0.40, 50)],
                            asks=[(0.51, 120), (0.52, 300)])),
        # instantané Down = miroir exact (bids Down à 0,49 = asks Up à 0,51)
        (ms(-99.9), raw_book(DOWN_ID, ms(-99.9), bids=[(0.49, 120), (0.48, 300)],
                             asks=[(0.51, 100), (0.52, 200), (0.60, 50)])),
        # à S−60 : un bid Up à 0,50 apparaît (délivré aussi comme ask Down à 0,50)
        (ms(-60), raw_pc(ms(-60), [(UP_ID, 0.50, 40, "BUY", 0.50, 0.51), (DOWN_ID, 0.50, 40, "SELL", 0.49, 0.50)])),
        # à S−30 : best_bid_ask émis AVANT le price_change qui vide 0,50 (ordre annulé)
        (ms(-30), raw_bba(UP_ID, ms(-30), 0.49, 0.51)),
        (ms(-30), raw_pc(ms(-30), [(UP_ID, 0.50, 0, "BUY", 0.49, 0.51), (DOWN_ID, 0.50, 0, "SELL", 0.49, 0.51)])),
        # à S−10 : preneur achète Down à 0,51 (= consomme les bids Up à 0,49), 30 parts
        (ms(-10), raw_trade(DOWN_ID, ms(-10), 0.51, 30, "BUY", "0xaaa")),
        (ms(-10), raw_pc(ms(-10), [(UP_ID, 0.49, 70, "BUY", 0.49, 0.51), (DOWN_ID, 0.51, 70, "SELL", 0.49, 0.51)])),
        # à S+5 : preneur achète Up à 0,52 alors que le meilleur ask est 0,51 : traverse un niveau
        (ms(5), raw_trade(UP_ID, ms(5), 0.52, 150, "BUY", "0xbbb")),
        (ms(5), raw_pc(ms(5), [(UP_ID, 0.51, 0, "SELL", 0.49, 0.52), (UP_ID, 0.52, 270, "SELL", 0.49, 0.52),
                              (DOWN_ID, 0.49, 0, "BUY", 0.48, 0.51), (DOWN_ID, 0.48, 270, "BUY", 0.48, 0.51)])),
    ]
    return msgs


def test_v1_reconstruction_and_series(live_dir):
    write_v1(live_dir / f"{META['slug']}.jsonl.gz", scenario_v1())
    m = bk.load_market(META["slug"], live_dir)
    assert m.n_events("book") == 2 and m.n_events("pc") == 4 and m.n_events("trade") == 2
    assert m.start_ts == S and m.duration_s == 300 and m.resolved_up is None
    ser = bk.book_series(m, step_s=1.0, t0_rel=-120, t1_rel=10)
    at = ser.set_index("t_rel_s")
    assert not at.loc[-120.0, "fresh"] and np.isnan(at.loc[-120.0, "best_bid"])
    r = at.loc[-90.0]
    assert r["best_bid"] == 0.49 and r["best_ask"] == 0.51 and math.isclose(r["spread"], 0.02)
    assert r["bid_up_049"] == 100 and r["bid_up_048"] == 200 and r["bid_dn_049"] == 120 and r["bid_dn_048"] == 300
    assert r["depth_best_bid"] == 100 and r["depth_best_ask"] == 120
    # delta à S−60 : nouveau bid Up à 0,50 -> meilleur bid 0,50 ; supprimé à S−30
    assert at.loc[-45.0, "best_bid"] == 0.50 and at.loc[-45.0, "bid_up_050"] == 40
    assert at.loc[-30.0, "best_bid"] == 0.49 and at.loc[-30.0, "bid_up_050"] == 0
    # après le trade à S−10 : 70 restent à 0,49
    assert at.loc[-5.0, "bid_up_049"] == 70
    # après S+5 : ask 0,51 vidé, meilleur ask 0,52 ; bids Down 0,48 = asks Up 0,52 = 270
    assert at.loc[6.0, "best_ask"] == 0.52 and at.loc[6.0, "bid_dn_048"] == 270 and at.loc[6.0, "bid_dn_049"] == 0
    # profondeur cumulée à ± 0,10 du milieu (0,505 à S+6) côté bid : 0,49 (70) + 0,48 (200) ; 0,40 exclu
    assert math.isclose(at.loc[6.0, "depth_bid_10c"], 270.0)


def test_trades_frame_levels_and_traversal(live_dir):
    write_v1(live_dir / f"{META['slug']}.jsonl.gz", scenario_v1())
    m = bk.load_market(META["slug"], live_dir)
    tr = bk.trades_frame(m)
    assert list(tr["tx"]) == ["0xaaa", "0xbbb"]
    a = tr.iloc[0]
    assert a["asset"] == "down" and a["side"] == "BUY" and a["consumes"] == "bid" and a["level_up"] == 0.49
    assert a["best_bid_pre"] == 0.49 and a["best_ask_pre"] == 0.51 and a["depth_level_pre"] == 100
    assert not a["traverses"] and not a["exceeds_depth"]
    b = tr.iloc[1]
    assert b["consumes"] == "ask" and b["level_up"] == 0.52 and b["best_ask_pre"] == 0.51
    assert b["traverses"] and b["exceeds_depth"] and b["depth_best_pre"] == 120
    assert math.isclose(b["t_rel_s"], 5.0)


def test_consumed_level_complementarity():
    assert bk.consumed_level(bk.UP, "BUY", 0.53) == ("ask", 0.53)
    assert bk.consumed_level(bk.UP, "SELL", 0.48) == ("bid", 0.48)
    assert bk.consumed_level(bk.DOWN, "BUY", 0.52) == ("bid", 0.48)
    assert bk.consumed_level(bk.DOWN, "SELL", 0.47) == ("ask", 0.53)


def test_bba_control_accepts_message_before_delta(live_dir):
    write_v1(live_dir / f"{META['slug']}.jsonl.gz", scenario_v1())
    m = bk.load_market(META["slug"], live_dir)
    ctl = bk.bba_control(m)
    assert ctl["n"] == 1 and ctl["agree"] == 1 and ctl["agree_immediate"] == 0
    assert ctl["agree_rate"] == 1.0


def test_book_mirror_and_changes():
    b = bk.Book()
    b.apply_snapshot(bk.DOWN, bids=[(490, 10.0)], asks=[(520, 5.0), (530, 0.0)])
    assert b.asks == {510: 10.0} and b.bids == {480: 5.0}
    b.apply_change(bk.DOWN, 500, 3.0, "B")          # bid Down 0,50 -> ask Up 0,50
    assert b.asks[500] == 3.0 and b.best_ask() == 0.50
    b.apply_change(bk.UP, 500, 0.0, "S")            # vidé côté Up
    assert 500 not in b.asks
    b.apply_change(bk.UP, 485, 7.5, "B")            # pas de 0,001 (tick réduit)
    assert b.size("bid", 0.485) == 7.5 and b.best_bid() == 0.485
    assert b.size_down_bid(0.49) == 10.0            # bids Down 0,49 = asks Up 0,51
    c = b.copy()
    c.apply_change(bk.UP, 485, 0.0, "B")
    assert b.size("bid", 0.485) == 7.5


def test_v2_format_and_truncated_segment(live_dir):
    slug = META["slug"]
    lines = [
        {"rx": 1, "e": "start", "fmt": 2, "up": UP_ID, "dn": DOWN_ID},
        {"rx": 2, "e": "connect", "n": 0},
        {"rx": 3, "e": "book", "a": 0, "ts": ms(-100), "b": [["0.49", "100"], ["0.48", "50"]], "k": [["0.51", "80"]]},
        {"rx": 4, "e": "pc", "ts": ms(-50), "c": [[1, "0.50", "25", "B", "0.49", "0.50"], [0, "0.50", "25", "S", "0.50", "0.51"]]},
        {"rx": 5, "e": "bba", "a": 1, "ts": ms(-50), "bb": "0.50", "ba": "0.51"},
        {"rx": 6, "e": "trade", "a": 0, "ts": ms(-20), "p": "0.50", "s": "10", "side": "BUY", "tx": "0xccc"},
        {"rx": 7, "e": "tick", "a": 0, "ts": ms(100), "old": "0.01", "new": "0.001"},
        {"rx": 8, "e": "resolved", "ts": ms(320), "win": "Down"},
    ]
    write_v2(live_dir / f"{slug}.jsonl.gz", lines)
    # segment tronqué : on coupe le gzip au milieu (collecteur tué)
    seg = live_dir / f"{slug}.s1.jsonl.gz"
    write_v2(seg, [{"rx": 6, "e": "trade", "a": 0, "ts": ms(-20), "p": "0.50", "s": "10", "side": "BUY", "tx": "0xccc"},
                   {"rx": 9, "e": "trade", "a": 1, "ts": ms(30), "p": "0.60", "s": "5", "side": "SELL", "tx": "0xddd"}] * 40)
    data = seg.read_bytes()
    seg.write_bytes(data[: len(data) - 40])
    m = bk.load_market(slug, live_dir)
    assert [p.name for p in m.files] == [f"{slug}.jsonl.gz", f"{slug}.s1.jsonl.gz"]
    assert m.resolved_up is False                               # issue lue dans le flux WebSocket
    tr = bk.trades_frame(m)
    assert list(tr["tx"]) == ["0xccc", "0xddd"]                # dédoublonnés malgré 40 copies
    assert tr.iloc[1]["consumes"] == "ask" and tr.iloc[1]["level_up"] == 0.40
    ser = bk.book_series(m, step_s=10.0, t0_rel=-100, t1_rel=0)
    at = ser.set_index("t_rel_s")
    assert at.loc[-60.0, "best_ask"] == 0.51 and at.loc[-40.0, "best_ask"] == 0.50
    assert at.loc[-40.0, "bid_dn_050"] == 25 and at.loc[-40.0, "bid_up_049"] == 100
    ctl = bk.bba_control(m)
    assert ctl["n"] == 1 and ctl["agree"] == 1
    assert m.price_to_beat != m.price_to_beat                   # NaN sans eventMetadata


def test_market_files_order_and_listing(live_dir):
    slug = META["slug"]
    for name in (f"{slug}.s10.jsonl.gz", f"{slug}.s2.jsonl.gz", f"{slug}.jsonl.gz"):
        write_v2(live_dir / name, [{"rx": 1, "e": "connect", "n": 0}])
    names = [p.name for p in bk.market_files(slug, live_dir)]
    assert names == [f"{slug}.jsonl.gz", f"{slug}.s2.jsonl.gz", f"{slug}.s10.jsonl.gz"]
    assert bk.list_markets(live_dir) == [slug]
