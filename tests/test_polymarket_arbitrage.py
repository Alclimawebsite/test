"""Tests hors-ligne de la recherche d'arbitrages entre marchés liés (tradebot.polymarket_arbitrage).

Cas construits : énumération des issues possibles selon les niveaux connus, sommets des
portefeuilles couvrants, programme linéaire (violation évidente -> arbitrage trouvé ; prix
cohérents -> aucun ; frais qui annulent une petite violation ; tailles et niveaux limités ;
jeton indisponible), égalité entre le coût minimal vectorisé et l'optimum du programme linéaire,
branchement de la 4h, épisodes, prix issus des trades, vérification de la chaîne K/F, états
conjoints des carnets.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd
import pytest

from tradebot import polymarket_arbitrage as pa

L = pa.GROUP_LEGS
LEG15, LEG1, LEG2, LEG3 = L


def outcomes_set(legs, known):
    return {tuple(bool(x) for x in row) for row in pa.possible_outcomes(legs, known)}


# ---------------------------------------------------------------------------
# Issues possibles
# ---------------------------------------------------------------------------
def test_outcomes_nothing_known_excludes_only_impossible_patterns():
    got = outcomes_set(L, {})
    assert len(got) == 14
    # (15m, 5m#1, 5m#2, 5m#3)
    assert (False, True, True, True) not in got     # trois 5m Up => 15m Up
    assert (True, False, False, False) not in got   # trois 5m Down => 15m Down
    assert (True, True, True, True) in got and (False, False, False, False) in got
    assert outcomes_set(L, {0: 100.0}) == got       # V0 seul ne contraint rien


def test_outcomes_after_first_close():
    up1 = outcomes_set(L, {0: 100.0, 1: 101.0})
    assert len(up1) == 7
    assert all(o[1] for o in up1)                   # 5m#1 fixée à Up
    assert (False, True, True, True) not in up1     # #2 et #3 Up => 15m Up
    assert (True, True, False, False) in up1        # la 15m peut rester Up malgré deux baisses
    dn1 = outcomes_set(L, {0: 100.0, 1: 99.0})
    assert len(dn1) == 7 and not any(o[1] for o in dn1)
    assert (True, False, False, False) not in dn1   # #2 et #3 Down => 15m Down


def test_outcomes_after_second_close_nested_digitals():
    legs = (LEG15, LEG3)
    above = outcomes_set(legs, {0: 100.0, 1: 101.0, 2: 100.5})     # V2 >= V0
    assert above == {(True, True), (True, False), (False, False)}   # 5m#3 Up => 15m Up
    below = outcomes_set(legs, {0: 100.0, 1: 101.0, 2: 99.5})      # V2 < V0
    assert below == {(True, True), (False, True), (False, False)}   # 5m#3 Down => 15m Down
    tie = outcomes_set(legs, {0: 100.0, 1: 101.0, 2: 100.0})       # égalité : mêmes événements
    assert tie == {(True, True), (False, False)}


def test_outcomes_all_known_and_ties_go_up():
    assert outcomes_set(L, {0: 1.0, 1: 2.0, 2: 2.0, 3: 0.5}) == {(False, True, True, False)}
    assert outcomes_set((LEG1,), {0: 5.0, 1: 5.0}) == {(True,)}


def test_outcomes_consecutive_5m_are_unconstrained():
    assert outcomes_set((LEG1, LEG2), {}) == set(itertools.product([True, False], repeat=2))
    assert outcomes_set((LEG1, LEG2), {0: 1.0}) == set(itertools.product([True, False], repeat=2))


def test_outcomes_4h_joins_last_quarter():
    legs = (LEG15, LEG3, pa.Leg("4h", pa.K4H, 3))
    # V2 connu, K4h < V0 <= V2 : 5m#3 Up => 15m Up => 4h Up (seuils croissants)
    got = outcomes_set(legs, {0: 100.0, 1: 100.2, 2: 100.4, pa.K4H: 99.0})
    assert got == {(True, True, True), (True, False, True), (False, False, True), (False, False, False)}


def test_payoff_matrix_columns():
    A = pa.payoff_matrix(np.array([[True, False]]))
    assert A.tolist() == [[1.0, 0.0, 0.0, 1.0]]
    assert pa.token_names((LEG15, LEG3)) == ["15m Up", "15m Down", "5m#3 Up", "5m#3 Down"]


# ---------------------------------------------------------------------------
# Frais
# ---------------------------------------------------------------------------
def test_fee_matches_client_formula():
    from tradebot.polymarket import taker_fee

    for p in (0.01, 0.3, 0.5, 0.97):
        assert pa.fee_per_share(p) == pytest.approx(taker_fee(p, 1.0, {"rate": 0.07, "exponent": 1}, decimals=None))
    assert pa.taker_cost(0.5) == pytest.approx(0.5175)


# ---------------------------------------------------------------------------
# Programme linéaire
# ---------------------------------------------------------------------------
def nested_payoff(v2_above=True):
    return pa.payoff_matrix(pa.possible_outcomes((LEG15, LEG3), {0: 100.0, 1: 101.0, 2: 100.5 if v2_above else 99.5}))


def test_obvious_violation_is_found():
    A = nested_payoff(True)
    # 15m Up à 0,40 et 5m#3 Down à 0,47 : 0,87 + frais < 1 alors que l'un des deux paie toujours
    r = pa.solve_arbitrage(A, [0.40, 0.62, 0.55, 0.47])
    assert r.is_arbitrage
    assert r.shares.tolist() == pytest.approx([1.0, 0.0, 0.0, 1.0])
    expected = 1.0 - pa.taker_cost(0.40) - pa.taker_cost(0.47)
    assert r.profit == pytest.approx(expected) and r.margin == pytest.approx(expected)


def test_direction_depends_on_known_level():
    # mêmes prix, V2 < V0 : c'est 15m Down + 5m#3 Up qui couvre, trop cher ici
    A = nested_payoff(False)
    assert not pa.solve_arbitrage(A, [0.40, 0.62, 0.55, 0.47]).is_arbitrage
    r = pa.solve_arbitrage(A, [0.60, 0.42, 0.50, 0.52])
    assert r.is_arbitrage and r.shares.tolist() == pytest.approx([0.0, 1.0, 1.0, 0.0])


def test_coherent_prices_no_arbitrage():
    A = pa.payoff_matrix(pa.possible_outcomes(L, {0: 100.0}))
    prices = [0.52, 0.49, 0.51, 0.50, 0.50, 0.51, 0.51, 0.50]   # Up + Down = 1,01 partout
    assert not pa.solve_arbitrage(A, prices).is_arbitrage
    A2 = nested_payoff(True)
    assert not pa.solve_arbitrage(A2, [0.80, 0.21, 0.55, 0.46]).is_arbitrage   # 0,80 + 0,46 > 1


def test_fees_cancel_small_violation():
    A = nested_payoff(True)
    # 0,50 + 0,49 = 0,99 < 1 mais frais 0,0175 + 0,0175 => coût 1,025 : pas d'arbitrage
    assert 0.50 + 0.49 < 1.0
    assert not pa.solve_arbitrage(A, [0.50, 0.51, 0.52, 0.49]).is_arbitrage
    # sans frais la même violation serait un arbitrage d'un centime
    r = pa.solve_arbitrage(A, [0.50, 0.51, 0.52, 0.49], fee_rate=0.0)
    assert r.profit == pytest.approx(0.01)
    # aux extrêmes les frais sont faibles : 0,95 + 0,04 reste un arbitrage
    assert pa.solve_arbitrage(A, [0.95, 0.06, 0.97, 0.04]).is_arbitrage


def test_sizes_levels_and_cap():
    A = nested_payoff(True)
    r = pa.solve_arbitrage(A, [0.40, 0.62, 0.55, 0.47], [20, 50, 50, 30], max_sets=100)
    assert r.payout == pytest.approx(20.0)                      # limité par la jambe la plus mince
    r2 = pa.solve_arbitrage(A, [[0.40, 0.45], math.nan, 0.55, [0.47, 0.50]], [[20, 100], 0, 50, [30, 100]],
                            max_sets=100)
    assert r2.payout == pytest.approx(100.0)
    assert r2.avg_price[0] == pytest.approx((20 * 0.40 + 80 * 0.45) / 100)
    assert r2.max_price[3] == pytest.approx(0.50)
    # un niveau trop cher n'est pas utilisé : 0,45 + 0,60 + frais > 1
    r3 = pa.solve_arbitrage(A, [[0.40, 0.45], math.nan, 0.55, [0.47, 0.60]], [[20, 100], 0, 50, [30, 100]],
                            max_sets=100)
    assert r3.payout == pytest.approx(30.0)


def test_unavailable_token_blocks_cover():
    A = nested_payoff(True)
    assert not pa.solve_arbitrage(A, [0.40, 0.62, 0.55, math.nan]).is_arbitrage


def test_four_market_cover_before_first_close():
    A = pa.payoff_matrix(pa.possible_outcomes(L, {0: 100.0}))
    # 15m Up + trois 5m Down paie toujours au moins 1 : si la 15m finit Down, au moins une 5m est Down
    prices = [0.10, 0.91, 0.75, 0.26, 0.75, 0.26, 0.75, 0.26]
    r = pa.solve_arbitrage(A, prices)
    assert r.is_arbitrage
    assert r.shares.tolist() == pytest.approx([1, 0, 0, 1, 0, 1, 0, 1])


def test_4h_ladder_arbitrage():
    legs = (LEG15, LEG3, pa.Leg("4h", pa.K4H, 3))
    A = pa.payoff_matrix(pa.possible_outcomes(legs, {0: 100.0, 1: 100.2, 2: 100.4, pa.K4H: 99.0}))
    # 4h Up (seuil le plus bas) doit valoir au moins 15m Up : ici 4h Up à 0,30 et 15m Down à 0,40
    r = pa.solve_arbitrage(A, [0.61, 0.40, 0.45, 0.56, 0.30, 0.71])
    assert r.is_arbitrage and r.shares[1] == pytest.approx(1.0) and r.shares[4] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Sommets et coût minimal vectorisé
# ---------------------------------------------------------------------------
def test_cover_vertices_initial_phase():
    A = pa.payoff_matrix(pa.possible_outcomes(L, {}))
    V = pa.cover_vertices(A)
    got = {tuple(np.flatnonzero(v)) for v in V}
    assert got == {(0, 1), (2, 3), (4, 5), (6, 7), (0, 3, 5, 7), (1, 2, 4, 6)}
    assert np.all((A @ V.T) >= 1 - 1e-9)


@pytest.mark.parametrize("known,legs", [
    ({}, L), ({0: 100.0, 1: 101.0}, (LEG15, LEG2, LEG3)), ({0: 100.0, 1: 99.0}, (LEG15, LEG2, LEG3)),
    ({0: 100.0, 1: 101.0, 2: 100.5}, (LEG15, LEG3)), ({0: 100.0, 1: 101.0, 2: 99.0}, (LEG15, LEG3)),
])
def test_min_cover_cost_equals_lp(known, legs):
    A = pa.payoff_matrix(pa.possible_outcomes(legs, known))
    V = pa.cover_vertices(A)
    rng = np.random.default_rng(0)
    n = A.shape[1]
    P = rng.uniform(0.02, 0.98, size=(40, n))
    P[rng.random(P.shape) < 0.15] = np.nan                 # jetons indisponibles
    C = np.where(np.isfinite(P), pa.taker_cost(np.nan_to_num(P, nan=0.5)), np.inf)
    best, idx = pa.min_cover_cost(C, V)
    for k in range(len(P)):
        r = pa.solve_arbitrage(A, list(P[k]))
        lp_margin = r.profit if r.is_arbitrage else 0.0
        vert_margin = max(0.0, 1.0 - best[k]) if np.isfinite(best[k]) else 0.0
        assert lp_margin == pytest.approx(vert_margin, abs=1e-7)


def test_min_cover_cost_no_vertex_available():
    A = pa.payoff_matrix(pa.possible_outcomes((LEG15, LEG3), {0: 1.0, 1: 2.0, 2: 1.5}))
    V = pa.cover_vertices(A)
    best, idx = pa.min_cover_cost(np.array([[0.5, np.inf, np.inf, np.inf]]), V)
    assert not np.isfinite(best[0]) and idx[0] == -1


# ---------------------------------------------------------------------------
# État d'un groupe, épisodes, trades
# ---------------------------------------------------------------------------
def test_group_state_phases():
    vals = {0: 100.0, 1: 101.0, 2: 99.0, 3: 98.0}
    act, kn = pa.group_state(-10, L, vals)
    assert act == (0, 1, 2, 3) and kn == {}
    act, kn = pa.group_state(0, L, vals)
    assert act == (0, 1, 2, 3) and kn == {0: 100.0}
    act, kn = pa.group_state(300, L, vals)
    assert act == (0, 2, 3) and kn == {0: 100.0, 1: 101.0}
    act, kn = pa.group_state(299.5, L, vals, known_delay_s=0.0)
    assert act == (0, 1, 2, 3) and 1 not in kn
    act, kn = pa.group_state(650, pa.GROUP_LEGS_4H, {**vals, pa.K4H: 97.0})
    assert act == (0, 3, 4) and kn[pa.K4H] == 97.0
    assert pa.phase_label(-1) == "avant S" and pa.phase_label(450) == "entre #1 et #2"


def test_episodes_merge_and_split():
    t = np.array([0, 1, 2, 3, 4, 5, 6, 10, 11])
    f = np.array([1, 1, 0, 1, 1, 1, 0, 1, 1], dtype=bool)
    assert pa.episodes(t, f, max_gap=1) == [(0, 1), (3, 5), (7, 8)]
    keys = np.array([0, 0, 0, 1, 2, 2, 0, 3, 3])
    assert pa.episodes(t, f, max_gap=1, keys=keys) == [(0, 1), (3, 3), (4, 5), (7, 8)]
    assert pa.episodes(t, np.zeros(9, dtype=bool), 1) == []


def test_last_trade_grid_age_and_level_size():
    ts = np.array([10, 10, 10, 14])
    price = np.array([0.50, 0.52, 0.52, 0.55])
    size = np.array([5.0, 3.0, 4.0, 2.0])
    grid = np.array([9, 10, 13, 14, 18, 19, 25])
    p, s, a = pa.last_trade_grid(ts, price, size, grid, max_age=5)
    assert np.isnan(p[0])
    assert p[1] == 0.52 and s[1] == 7.0 and a[1] == 0          # 3 + 4 au même prix, même seconde
    assert p[2] == 0.52 and a[2] == 3
    assert p[3] == 0.55 and s[3] == 2.0
    assert p[4] == 0.55 and a[4] == 4
    assert np.isnan(p[5]) and np.isnan(p[6])                    # âge 5 : exclu


def test_last_trade_grid_block_max_ignores_order_within_block():
    # même bloc (seconde 10) : 0,90 puis 0,53 dans l'ordre fourni ; l'ordre réel est inconnu
    ts = np.array([10, 10, 10, 12])
    price = np.array([0.51, 0.90, 0.53, 0.60])
    size = np.array([5.0, 3.0, 4.0, 2.0])
    p, s, a = pa.last_trade_grid(ts, price, size, np.array([10, 11, 12]), max_age=5, mode="block_max")
    assert p.tolist() == [0.90, 0.90, 0.60] and s.tolist() == [3.0, 3.0, 2.0] and a.tolist() == [0, 1, 0]
    p2, _, _ = pa.last_trade_grid(ts, price, size, np.array([10]), max_age=5, mode="last")
    assert p2[0] == 0.53
    with pytest.raises(ValueError):
        pa.last_trade_grid(ts, price, size, np.array([10]), max_age=5, mode="first")


# ---------------------------------------------------------------------------
# Chaîne K/F
# ---------------------------------------------------------------------------
def test_chain_checks_detects_break():
    S0 = 1_790_000_100 - (1_790_000_100 % 900)
    rows = []
    lv = [100.0, 101.0, 102.5, 101.5, 103.0, 104.0, 102.0]
    for k in range(6):
        rows.append({"slug": f"btc-updown-5m-{S0 + 300 * k}", "price_to_beat": lv[k], "final_price": lv[k + 1]})
    rows.append({"slug": f"btc-updown-15m-{S0}", "price_to_beat": lv[0], "final_price": lv[3]})
    rows.append({"slug": f"btc-updown-15m-{S0 + 900}", "price_to_beat": lv[3], "final_price": lv[6] + 0.01})
    out = pa.chain_checks(pd.DataFrame(rows)).set_index("relation")
    assert out.loc["K(5m#k) == F(5m#k−1) (5m consécutives)", "n"] == 5
    assert out.loc["K(5m#k) == F(5m#k−1) (5m consécutives)", "taux_egal"] == 1.0
    assert out.loc["K(15m) == K(5m#1) (même début)", "egal"] == 2
    f = out.loc["F(15m) == F(5m#3) (même fin)"]
    assert f["n"] == 2 and f["egal"] == 1 and f["ecart_max"] == pytest.approx(0.01)
    assert out.loc["K(15m) == F(15m précédente)", "taux_egal"] == 1.0


# ---------------------------------------------------------------------------
# Carnets conjoints
# ---------------------------------------------------------------------------
class _FakeMarket:
    def __init__(self, events):
        self.events = events


def test_live_group_states_unified_book():
    # marché 0 : instantané Up (bids 0,40 ; asks 0,45 / 0,47) ; marché 1 : instantané Down
    m0 = _FakeMarket([(10, 1000, "book", 0, ([(400, 10.0)], [(450, 5.0), (470, 8.0)]))])
    m1 = _FakeMarket([(5, 900, "book", 1, ([(300, 7.0)], [(650, 2.0)])),
                      (20, 1100, "pc", -1, [(1, 310, 4.0, "B", 0.31, 0.65)])])
    st = pa.live_group_states([m0, m1], depth=2)
    assert st["rx"].tolist() == [5, 10, 20]
    # après l'événement 0 (marché 1, bids Down 0,30 / asks Down 0,65), repère Up :
    # asks Up = 1 − bids Down = 0,70 ; bids Up = 1 − asks Down = 0,35, donc ask Down = 0,65
    px, sz = st["px"], st["sz"]
    assert px[0, 2, 0] == pytest.approx(0.70) and sz[0, 2, 0] == 7.0        # ask Up marché 1
    assert px[0, 3, 0] == pytest.approx(0.65) and sz[0, 3, 0] == 2.0        # ask Down = 1 − bid Up (0,35)
    assert np.isnan(px[0, 0, 0])                                             # marché 0 pas encore reçu
    assert px[1, 0].tolist() == pytest.approx([0.45, 0.47]) and px[1, 1, 0] == pytest.approx(0.60)
    # le price_change ajoute un bid Down à 0,31 => ask Up 0,69
    assert px[2, 2, 0] == pytest.approx(0.69) and sz[2, 2, 0] == 4.0
    assert st["fresh"].tolist() == [[False, True], [True, True], [True, True]]
    assert not st["crossed"].any()
