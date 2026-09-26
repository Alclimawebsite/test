"""Tests hors-ligne du simulateur maker historique sur trades preneurs
(:mod:`tradebot.polymarket_maker`).

Cas construits : exécution par traversée (i), par cumul de file (ii), complémentarité Up/Down
(un achat preneur de Down consomme un bid Up), annulation, délai on-chain, ordres croisants,
P&L et remise sur des cas connus, absence de fuite (l'ordre ne voit rien après t0 sauf les
trades de sa fenêtre), juste valeur Φ(d/σ), re-cotation de (c), résumés et paires (b), lecture
du cache par série-jour.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket_maker as mk
from tradebot.polymarket_backtest import SlotBootstrap

S = 1_790_000_000
E = S + 300
FEES = {"rate": 0.07, "exponent": 1}


def trades(rows):
    """rows : (t_rel, side, outcome, price, size)."""
    df = pd.DataFrame(rows, columns=["t_rel", "side", "outcome", "price", "size"])
    df["ts"] = pd.to_datetime(S + df["t_rel"], unit="s", utc=True)
    df["seq"] = np.arange(len(df))
    return df


def market(rows, slug="btc-updown-5m-1790000000"):
    return mk.MarketTrades.from_frame(trades(rows), slug, S, E)


def order(side, price, t_place, t_cancel, q_ahead=0.0, size=10.0, rule=mk.RULE_BOTH, crossing=False):
    return pd.DataFrame([{"side": side, "price": price, "t_place": float(S + t_place), "t_cancel": float(S + t_cancel),
                          "q_ahead": q_ahead, "size": size, "rule": rule, "crossing": crossing}])


def run(mt, o, **kw):
    kw.setdefault("delay_s", 0)
    kw.setdefault("check_cross", False)
    return mk.simulate_orders(mt, o, **kw).iloc[0]


# ---------------------------------------------------------------------------
# Modèle d'exécution
# ---------------------------------------------------------------------------
def test_clearance_prices_and_complementarity():
    mt = market([(-20, "SELL", "Up", 0.49, 5), (-19, "BUY", "Down", 0.52, 7), (-18, "BUY", "Up", 0.51, 3), (-17, "SELL", "Down", 0.48, 2)])
    # vente preneur de Up à 0,49 -> dégage les bids Up <= 0,49 ; achat preneur de Down à 0,52 -> bids Up <= 0,48
    assert mt.cp_up.tolist() == pytest.approx([0.49, 0.48, math.inf, math.inf])
    # côté Down : vente Down à 0,48 et achat Up à 0,51 (= 1 − 0,49)
    assert mt.cp_dn.tolist() == pytest.approx([math.inf, math.inf, 0.49, 0.48])
    # preuve d'ask : achat preneur de Up à 0,51 -> ask Up <= 0,51 ; vente Down à 0,48 -> ask Up <= 0,52
    assert mt.ap_up.tolist() == pytest.approx([math.inf, math.inf, 0.51, 0.52])
    assert mt.ap_dn.tolist() == pytest.approx([0.51, 0.52, math.inf, math.inf])
    assert mt.taker_shares_total() == 17
    assert mt.taker_fees_total(FEES) == pytest.approx(sum(s * 0.07 * p * (1 - p) for p, s in [(0.49, 5), (0.52, 7), (0.51, 3), (0.48, 2)]))


def test_sorted_by_block_time_then_seq():
    df = trades([(-10, "SELL", "Up", 0.40, 1), (-20, "SELL", "Up", 0.45, 1), (-20, "SELL", "Up", 0.44, 1)])
    df.loc[1, "seq"], df.loc[2, "seq"] = 5, 2       # même seconde : l'ordre on-chain est seq 2 puis 5
    mt = mk.MarketTrades.from_frame(df, "x", S, E)
    assert mt.t.tolist() == [S - 20, S - 20, S - 10]
    assert mt.price.tolist() == pytest.approx([0.44, 0.45, 0.40])


def test_fill_by_traversal():
    # bid Up à 0,49 : un SELL Up à 0,485 (< 0,49) traverse le niveau -> exécuté même avec une grande file
    mt = market([(-25, "SELL", "Up", 0.49, 3), (-15, "SELL", "Up", 0.485, 2), (-5, "SELL", "Up", 0.40, 100)])
    r = run(mt, order("up", 0.49, -30, 0, q_ahead=5000))
    assert r["executed"] and r["fill_reason"] == "traversal" and r["t_fill"] == S - 15 and r["delay_fill_s"] == 15
    assert r["consumed"] == pytest.approx(5.0)          # 3 au niveau + le trade traversant
    # règle (ii) seule : le cumul (105) ne dépasse jamais 5 000 + 10 -> non exécuté
    r2 = run(mt, order("up", 0.49, -30, 0, q_ahead=5000, rule=mk.RULE_QUEUE))
    assert not r2["executed"] and r2["fill_reason"] == "" and np.isnan(r2["t_fill"]) and r2["consumed"] == pytest.approx(105.0)


def test_fill_by_queue_with_complementary_trades():
    # bid Up à 0,49, file 100, taille 10 : SELL Up à 0,49 (60) + BUY Down à 0,51 (45) = 105 < 110 ; puis BUY Down à 0,51 (5) -> 110
    mt = market([(-20, "SELL", "Up", 0.49, 60), (-15, "BUY", "Down", 0.51, 45), (-12, "BUY", "Down", 0.51, 5), (-8, "SELL", "Up", 0.49, 50)])
    r = run(mt, order("up", 0.49, -30, 0, q_ahead=100))
    assert r["executed"] and r["fill_reason"] == "queue" and r["t_fill"] == S - 12 and r["consumed"] == pytest.approx(110.0)
    # règle (i) seule : aucun trade ne dépasse strictement 0,49 -> non exécuté
    r1 = run(mt, order("up", 0.49, -30, 0, q_ahead=100, rule=mk.RULE_TRAVERSAL))
    assert not r1["executed"]
    # variante optimiste Q_ahead = 0 : le premier trade (60 >= 10) suffit
    r0 = run(mt, order("up", 0.49, -30, 0, q_ahead=0))
    assert r0["executed"] and r0["t_fill"] == S - 20 and r0["fill_reason"] == "queue"
    # trades au-dessus du niveau (SELL Up à 0,50, BUY Down à 0,50) ne consomment pas un bid Up à 0,49
    mt2 = market([(-20, "SELL", "Up", 0.50, 500), (-15, "BUY", "Down", 0.50, 500)])
    assert not run(mt2, order("up", 0.49, -30, 0, q_ahead=0))["executed"]


def test_down_side_symmetry():
    # bid Down à 0,48 : consommé par SELL Down <= 0,48 et BUY Up >= 0,52 ; traversé par BUY Up à 0,53
    mt = market([(-20, "BUY", "Up", 0.52, 8), (-10, "BUY", "Up", 0.53, 1)])
    r = run(mt, order("down", 0.48, -30, 0, q_ahead=0, size=10))
    assert r["executed"] and r["fill_reason"] == "traversal" and r["t_fill"] == S - 10
    r2 = run(mt, order("down", 0.48, -30, 0, q_ahead=0, size=8))
    assert r2["executed"] and r2["fill_reason"] == "queue" and r2["t_fill"] == S - 20


def test_all_or_nothing_and_cancel():
    mt = market([(-20, "SELL", "Up", 0.49, 9), (10, "SELL", "Up", 0.49, 9), (40, "SELL", "Up", 0.40, 1)])
    # 9 puis 9 : cumul 18 >= 10 à S+10 (tout ou rien : pas d'exécution partielle à S−20)
    r = run(mt, order("up", 0.49, -30, 30, q_ahead=0, size=10))
    assert r["executed"] and r["t_fill"] == S + 10 and r["consumed"] == pytest.approx(18.0)
    # annulation à S : seul le trade de S−20 est vu -> non exécuté ; la traversée de S+40 n'est jamais vue
    r2 = run(mt, order("up", 0.49, -30, 0, q_ahead=0, size=10))
    assert not r2["executed"] and r2["consumed"] == pytest.approx(9.0)
    r3 = run(mt, order("up", 0.49, -30, 30, q_ahead=0, size=20))
    assert not r3["executed"]
    # un ordre annulé avant sa pose n'est jamais exécuté
    r4 = run(mt, order("up", 0.49, 0, -10, q_ahead=0, size=1))
    assert not r4["executed"] and r4["placed"]


def test_onchain_delay_shifts_the_window():
    # trade au bloc S−29 : avec 3 s de délai, l'ordre posé à S−30 (visible à S−27) ne le voit pas
    mt = market([(-29, "SELL", "Up", 0.40, 50), (-26, "SELL", "Up", 0.40, 50)])
    r = run(mt, order("up", 0.49, -30, 0, q_ahead=0), delay_s=3)
    assert r["executed"] and r["t_fill"] == S - 26 and r["delay_fill_s"] == 4
    # annulation à S−27 : fenêtre [S−27, S−24) avec le délai -> voit S−26
    r2 = run(mt, order("up", 0.49, -30, -27, q_ahead=0), delay_s=3)
    assert r2["executed"]
    # annulation à S−29 : fenêtre [S−27, S−26) -> aucun trade vu (S−26 exclu)
    r3 = run(mt, order("up", 0.49, -30, -29, q_ahead=0), delay_s=3)
    assert not r3["executed"]


def test_crossing_detection():
    # achat preneur de Up à 0,49 dans les 10 s précédentes : l'ask Up était <= 0,49 -> un bid Up à 0,49 croiserait
    mt = market([(-35, "BUY", "Up", 0.49, 5), (-20, "SELL", "Up", 0.40, 100)])
    r = mk.simulate_orders(mt, order("up", 0.49, -30, 0, q_ahead=0), delay_s=0, check_cross=True).iloc[0]
    assert r["crossing"] and not r["placed"] and not r["executed"]
    # à 0,48 : pas de preuve (0,49 > 0,48) -> posé et exécuté
    r2 = mk.simulate_orders(mt, order("up", 0.48, -30, 0, q_ahead=0), delay_s=0, check_cross=True).iloc[0]
    assert not r2["crossing"] and r2["executed"]
    # preuve trop ancienne (hors des 10 s)
    mt3 = market([(-45, "BUY", "Up", 0.49, 5), (-20, "SELL", "Up", 0.40, 100)])
    assert not mk.simulate_orders(mt3, order("up", 0.49, -30, 0, q_ahead=0), delay_s=0, check_cross=True).iloc[0]["crossing"]
    # vente preneur de Down à 0,51 = ask Up <= 0,49 : même preuve par complémentarité
    mt4 = market([(-32, "SELL", "Down", 0.51, 5), (-20, "SELL", "Up", 0.40, 100)])
    assert mk.simulate_orders(mt4, order("up", 0.49, -30, 0, q_ahead=0), delay_s=0, check_cross=True).iloc[0]["crossing"]
    # règle du milieu : bid Up à 0,50 avec milieu 0,495 (ask ≈ 0,50) -> croisant ; à 0,49 non ; côté Down symétrique
    o = mk.orders_signal(S, E, "up", p_pre=0.495)
    assert o.loc[o["price"] == 0.50, "crossing"].all() and not o.loc[o["price"] == 0.49, "crossing"].any()
    od = mk.orders_signal(S, E, "down", p_pre=0.495)
    assert not od["crossing"].any()          # milieu Down 0,505 : bid Down à 0,50 < 0,51


def test_no_leak_only_window_trades_matter():
    base = [(-20, "SELL", "Up", 0.49, 60), (-12, "BUY", "Down", 0.51, 50)]
    o = order("up", 0.49, -30, 0, q_ahead=100, size=10)
    ref = run(market(base), o)
    assert ref["executed"] and ref["t_fill"] == S - 12
    # tout ce qui est après l'annulation (t >= S) ou avant la pose (t < S−30) est modifié : rien ne change
    noise = [(-31, "SELL", "Up", 0.10, 10_000), (0, "SELL", "Up", 0.10, 10_000), (100, "BUY", "Down", 0.99, 10_000)]
    alt = run(market(base + noise), o)
    for c in ("executed", "t_fill", "fill_reason", "consumed"):
        assert alt[c] == ref[c]
    # l'issue et le barème n'entrent qu'au P&L, jamais dans l'exécution
    r_up = mk.score_orders(mk.simulate_orders(market(base), o, delay_s=0, check_cross=False), True, FEES)
    r_dn = mk.score_orders(mk.simulate_orders(market(base), o, delay_s=0, check_cross=False), False, FEES)
    assert r_up.loc[0, "t_fill"] == r_dn.loc[0, "t_fill"]
    assert r_up.loc[0, "pnl_per_share"] == pytest.approx(0.51) and r_dn.loc[0, "pnl_per_share"] == pytest.approx(-0.49)


# ---------------------------------------------------------------------------
# P&L, remise, résumés
# ---------------------------------------------------------------------------
def test_score_orders_pnl_and_rebate():
    mt = market([(-20, "SELL", "Up", 0.49, 100)])          # 100 parts au niveau : notre ordre de 100 est servi
    o = pd.concat([order("up", 0.49, -30, 0, q_ahead=0, size=100), order("down", 0.49, -30, 0, q_ahead=0, size=100),
                   order("up", 0.49, -30, 0, q_ahead=0, size=100, crossing=True)], ignore_index=True)
    r = mk.simulate_orders(mt, o, delay_s=0, check_cross=False)
    assert r["executed"].tolist() == [True, False, False] and r["placed"].tolist() == [True, True, False]
    s = mk.score_orders(r, True, FEES, taker_fees_total=mt.taker_fees_total(FEES))
    assert s.loc[0, "pnl_per_share"] == pytest.approx(0.51) and s.loc[0, "pnl_order"] == pytest.approx(51.0)
    assert s.loc[0, "pnl_per_placed_share"] == pytest.approx(0.51)
    assert np.isnan(s.loc[1, "pnl_per_share"]) and s.loc[1, "pnl_per_placed_share"] == 0.0 and s.loc[1, "pnl_order"] == 0.0
    assert np.isnan(s.loc[2, "pnl_per_placed_share"]) and np.isnan(s.loc[2, "pnl_order"])
    assert s["would_win"].tolist() == [1.0, 0.0, 1.0]
    # remise : 0,2 × 0,07 × 0,49 × 0,51 par part ; le plafond (20 % des frais preneurs du marché,
    # ici 100 × 0,07 × 0,49 × 0,51 -> 20 % = exactement notre remise) n'est pas atteint
    full = 0.2 * 0.07 * 0.49 * 0.51
    assert s.loc[0, "rebate_per_share"] == pytest.approx(full)
    s2 = mk.score_orders(r, True, FEES, taker_fees_total=0.5)     # plafond 0,10 $ pour 100 parts -> 0,001 par part
    assert s2.loc[0, "rebate_per_share"] == pytest.approx(0.001)
    # sans barème : pas de remise ; issue inconnue : P&L NaN mais exécution inchangée
    assert mk.score_orders(r, True, None).loc[0, "rebate_per_share"] == 0.0
    s3 = mk.score_orders(r, None, FEES)
    assert np.isnan(s3.loc[0, "pnl_per_share"]) and s3.loc[0, "executed"]


def test_summarize_and_adverse_selection():
    rows = []
    for k, (ex, win, slot) in enumerate([(True, 1, 0), (True, 0, 0), (False, 1, 1), (False, 1, 1), (True, 1, 2)]):
        rows.append({"strategy": "signal", "label": "0.49/S+0", "slug": f"m{k}", "slot": slot, "placed": True, "crossing": False,
                     "executed": ex, "fill_reason": "queue" if ex else "", "delay_fill_s": 5.0 if ex else np.nan,
                     "would_win": float(win), "pnl_per_share": (win - 0.49) if ex else np.nan,
                     "pnl_per_placed_share": (win - 0.49) if ex else 0.0, "pnl_order": 100 * (win - 0.49) if ex else 0.0,
                     "rebate_per_share": 0.0035 if ex else np.nan, "size": 100.0})
    rows.append({**rows[0], "slug": "m9", "placed": False, "crossing": True, "executed": False, "pnl_per_placed_share": np.nan, "pnl_order": np.nan})
    df = pd.DataFrame(rows)
    boot = SlotBootstrap(df["slot"].to_numpy(), B=200, seed=0)
    s = mk.summarize(df, boot=boot, boot_adj=SlotBootstrap(df["slot"].to_numpy(), B=200, seed=0, alpha=0.05 / 10)).iloc[0]
    assert s["n_orders"] == 5 and s["n_crossing"] == 1 and s["fill_rate"] == pytest.approx(0.6)
    assert s["pnl_per_placed_c"] == pytest.approx(100 * (0.51 - 0.49 + 0.51) / 5)
    assert s["pnl_per_executed_c"] == pytest.approx(100 * (0.51 - 0.49 + 0.51) / 3)
    assert s["win_rate_executed"] == pytest.approx(2 / 3) and s["win_rate_not_executed"] == 1.0
    assert s["adverse_selection_pts"] == pytest.approx(100 * (2 / 3 - 1))
    assert s["pnl_per_placed_ic_lo"] <= s["pnl_per_placed_c"] <= s["pnl_per_placed_ic_hi"]
    assert s["pnl_per_placed_ic_lo_adj"] <= s["pnl_per_placed_ic_lo"] and s["pnl_per_placed_ic_hi_adj"] >= s["pnl_per_placed_ic_hi"]
    assert s["delay_median_s"] == 5.0 and s["share_queue"] == 1.0


def test_score_frame_matches_score_orders():
    mt = market([(-20, "SELL", "Up", 0.49, 60), (-18, "BUY", "Up", 0.53, 30)])
    o = pd.concat([order("up", 0.49, -30, 0, q_ahead=0, size=50), order("down", 0.47, -30, 0, q_ahead=0, size=30)], ignore_index=True)
    r = mk.simulate_orders(mt, o, delay_s=0, check_cross=False)
    ref = mk.score_orders(r, False, FEES, taker_fees_total=mt.taker_fees_total(FEES))
    v = r.assign(slug="m", y=0.0, fee_rate=0.07, fee_exponent=1.0, taker_fees_total=mt.taker_fees_total(FEES))
    got = mk.score_frame(v)
    for c in ("would_win", "pnl_per_share", "pnl_per_placed_share", "pnl_order", "rebate_per_share"):
        assert np.allclose(got[c].to_numpy(dtype=float), ref[c].to_numpy(dtype=float), equal_nan=True), c
    assert got["executed"].tolist() == [True, True] and got.loc[1, "pnl_per_share"] == pytest.approx(0.53)


def test_two_sided_pairs():
    # marché 1 : les deux exécutés (écart de 2 c capté) ; marché 2 : un seul, perdant ; marché 3 : un seul, non posable (exclu)
    def rows(slug, ex_up, ex_dn, win_up, slot, placed_dn=True):
        out = []
        for side, ex, placed in (("up", ex_up, True), ("down", ex_dn, placed_dn)):
            win = float(win_up if side == "up" else 1 - win_up)
            out.append({"strategy": "two_sided", "label": "0.49/S+0", "pair": "0.49/S+0", "slug": slug, "slot": slot,
                        "placed": placed, "crossing": not placed, "executed": ex and placed, "size": 100.0,
                        "pnl_order": 100 * (win - 0.49) if (ex and placed) else (0.0 if placed else np.nan)})
        return out
    df = pd.DataFrame(rows("a", True, True, 1, 0) + rows("b", False, True, 1, 1) + rows("c", True, False, 1, 2, placed_dn=False))
    p = mk.pair_summary(df).iloc[0]
    assert p["n_pairs"] == 2 and p["share_both"] == 0.5 and p["share_one"] == 0.5 and p["share_none"] == 0.0
    assert p["pnl_per_pair_c"] == pytest.approx(100 * ((0.51 - 0.49) + (-0.49)) / 2)


# ---------------------------------------------------------------------------
# Stratégies
# ---------------------------------------------------------------------------
def test_model_side_and_signal_orders():
    assert mk.model_side(0.55) == "up" and mk.model_side(0.45) == "down" and mk.model_side(0.5) == "up"
    assert mk.model_side(0.51, 0.02) == "" and mk.model_side(0.53, 0.02) == "up" and mk.model_side(0.47, 0.02) == "down"
    assert mk.model_side(math.nan) == ""
    o = mk.orders_signal(S, E, "up", p_pre=0.505)
    assert len(o) == 16 and set(o["price"]) == {0.47, 0.48, 0.49, 0.50}
    assert o["t_place"].eq(S - 30).all()
    assert sorted(set(o["t_cancel"])) == [S, S + 30, S + 60, E]
    assert o.loc[(o["price"] == 0.49) & (o["cancel"] == "E"), "label"].iloc[0] == "0.49/E"
    assert o.loc[o["price"] == 0.49, "q_ahead"].iloc[0] == mk.measured_q_ahead("up", 0.49, "pre") == 70.0
    assert mk.orders_signal(S, E, "", p_pre=0.5).empty
    o2 = mk.orders_signal(S, E, "down", q_ahead=500.0, prices=(0.48,), cancels=(0,))
    assert len(o2) == 1 and o2.loc[0, "q_ahead"] == 500.0 and o2.loc[0, "side"] == "down"
    ts = mk.orders_two_sided(S, E)
    assert len(ts) == 8 and ts["t_place"].eq(S - 60).all() and set(ts["side"]) == {"up", "down"}
    assert ts.groupby("pair").size().eq(2).all()


def test_fair_value_probs_and_quotes():
    K = 100.0
    t = np.array([S + 30, S + 120, E - 60])
    p = mk.fair_value_probs(np.array([K, K, K]), K, 1e-4, t, E)
    assert np.allclose(p, 0.5)
    p_up = mk.fair_value_probs(np.array([K * 1.001]), K, 1e-4, np.array([S + 30]), E)[0]
    p_dn = mk.fair_value_probs(np.array([K / 1.001]), K, 1e-4, np.array([S + 30]), E)[0]
    assert p_up > 0.5 and p_up + p_dn == pytest.approx(1.0, abs=1e-9)
    # même écart, plus tard dans la fenêtre : moins de variance restante -> probabilité plus tranchée
    later = mk.fair_value_probs(np.array([K * 1.001]), K, 1e-4, np.array([E - 60]), E)[0]
    assert later > p_up
    # variance restante à E−60 = σ² × 20 (moyenne des 60 dernières secondes)
    from scipy.stats import norm

    assert later == pytest.approx(float(norm.cdf(math.log(1.001) / (1e-4 * math.sqrt(20)))))
    # σ = 0 -> 0,99 / 0,01 ; bornes
    assert mk.fair_value_probs(np.array([K * 1.001]), K, 0.0, np.array([S + 30]), E)[0] == 0.99
    assert np.isnan(mk.fair_value_probs(np.array([np.nan]), K, 1e-4, np.array([S + 30]), E)[0])
    tq = mk.quote_times(S, E)
    assert tq[0] == S + 30 and tq[-1] == E - 70 and np.all(np.diff(tq) == 10) and len(tq) == 21
    assert len(mk.quote_times(S, S + 900)) == 81


def test_ewma_sigma_causal():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 1e-4, 2000)))
    s = mk.ewma_sigma_1s(c, 60.0)
    assert s.shape == c.shape and np.all(s >= mk.SIGMA_FLOOR_1S)
    c2 = c.copy()
    c2[1500:] *= 1.5                      # tout modifier après 1500 ne change rien avant
    assert np.allclose(mk.ewma_sigma_1s(c2, 60.0)[:1500], s[:1500])
    assert abs(np.median(s[500:]) / 1e-4 - 1) < 0.3


def test_orders_fair_value_requote_and_merge():
    tq = mk.quote_times(S, E)                                  # 21 instants
    p = np.full(len(tq), 0.60)
    p[5:] = 0.70                                               # changement de niveau au 6e instant
    p[10] = np.nan                                             # pas de cotation
    o = mk.orders_fair_value(tq, p, E - 60, margin=0.01, size=100, q_ahead=100)
    up = o[o["side"] == "up"].reset_index(drop=True)
    dn = o[o["side"] == "down"].reset_index(drop=True)
    # Up : 0,59 sur [t0, t5) ; 0,69 sur [t5, t10) ; 0,69 sur [t11, E−60)
    assert up["price"].tolist() == pytest.approx([0.59, 0.69, 0.69])
    assert up["t_place"].tolist() == [tq[0], tq[5], tq[11]] and up["t_cancel"].tolist() == [tq[5], tq[10], E - 60]
    assert up["n_quotes"].tolist() == [5, 5, 10]
    # Down : (1 − 0,60) − 0,01 = 0,39 ; (1 − 0,70) − 0,01 = 0,29
    assert dn["price"].tolist() == pytest.approx([0.39, 0.29, 0.29])
    assert (o["q_ahead"] == 100).all() and (o["size"] == 100).all()
    # niveaux hors [0,05 ; 0,95] : pas d'ordre
    o2 = mk.orders_fair_value(tq, np.full(len(tq), 0.99), E - 60)
    assert o2.empty                                             # 0,98 > 0,95 et 0,00 < 0,05


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def test_load_taker_trades_merges_taker_and_all(tmp_path):
    base = tmp_path / "trades"
    base.mkdir()

    def write(stem, conds, rows, mode):
        df = pd.DataFrame(rows, columns=["ts", "side", "outcome", "price", "size", "seq", "slug", "condition_id", "role"])
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        df.to_parquet(base / f"{stem}.parquet")
        (base / f"{stem}.json").write_text(json.dumps({"version": 1, "mode": mode, "conditions": {c: 1 for c in conds}}))

    write("btc_5m_2026-09-23_taker", ["c1"], [(S, "BUY", "Up", 0.5, 1, 0, "m1", "c1", "taker"),
                                             (S, "BUY", "Up", 0.5, 1, 1, "m2", "c2", "taker")], "taker")
    write("btc_5m_2026-09-23_all", ["c1", "c2"], [(S, "SELL", "Up", 0.4, 9, 0, "m1", "c1", "taker"),
                                                  (S, "BUY", "Down", 0.5, 2, 0, "m2", "c2", "taker"),
                                                  (S, "SELL", "Down", 0.5, 2, 0, "m2", "c2", "maker")], "all")
    df, covered = mk.load_taker_trades("btc", "5m", "2026-09-23", tmp_path)
    assert covered == {"c1", "c2"}
    # c1 vient du fichier taker (1 ligne, m1 à 0,5), c2 du fichier all (preneur seulement)
    assert df[df["slug"] == "m1"]["price"].tolist() == [0.5]
    assert df[df["slug"] == "m2"]["side"].tolist() == ["BUY"] and df[df["slug"] == "m2"]["size"].tolist() == [2]
    assert "role" not in df.columns
    empty, cov0 = mk.load_taker_trades("btc", "5m", "2026-01-01", tmp_path)
    assert empty.empty and cov0 == set()
    markets = pd.DataFrame({"slug": ["m1", "m2", "m3"], "asset": "btc", "duration": "5m",
                            "start_ts": [S, S, S], "end_ts": [E, E, E]})
    got = {row["slug"]: mt for row, mt in mk.iter_market_trades(markets, tmp_path,
                                                                 loader=lambda a, d, day, cd: mk.load_taker_trades(a, d, "2026-09-23", cd))}
    assert got["m1"].n == 1 and got["m2"].n == 1 and got["m3"].n == 0
