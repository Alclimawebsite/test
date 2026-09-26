"""Moteur de rotation memecoins : pas d'information future, coûts, retraits de la cote, Sharpe dégonflé."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import memecoins as mc


def _frames(closes: dict[str, np.ndarray], start="2024-01-01", qvol=1e7) -> dict[str, pd.DataFrame]:
    out = {}
    for s, c in closes.items():
        c = np.asarray(c, float)
        d = pd.date_range(start, periods=c.size, freq="D")
        o = np.r_[c[0], c[:-1]]                      # ouverture = clôture de la veille (marché 24 h/24)
        out[s] = pd.DataFrame({"date": d, "open": o, "high": np.maximum(o, c), "low": np.minimum(o, c),
                               "close": c, "volume": 1.0, "qvol": np.where(np.isfinite(c), qvol, 0.0), "n_trades": 1})
    return out


def _random_panel(n_sym=8, n_days=500, seed=0, vol=0.08):
    rng = np.random.default_rng(seed)
    closes = {f"S{j}USDT": 100 * np.exp(np.cumsum(rng.normal(0, vol, n_days))) for j in range(n_sym)}
    return mc.build_panel(_frames(closes))


def test_no_lookahead_decisions_do_not_depend_on_future():
    """Modifier les prix après le jour d ne change aucune décision prise jusqu'à d."""
    panel = _random_panel()
    el = mc.eligibility(panel, min_age=30, min_qvol=0)
    p = mc.RotationParams(mult=1.5, low_win=10, range_win=20, bottom=0.3, k=2)
    base = mc.simulate_rotation(panel, el, p)
    cut = panel.dates[300]
    shocked = mc.Panel(**{k: getattr(panel, k).copy() for k in ("open", "high", "low", "close", "qvol", "active")})
    for k in ("open", "high", "low", "close"):
        getattr(shocked, k).loc[panel.dates > cut] *= np.random.default_rng(9).uniform(0.2, 5, (int((panel.dates > cut).sum()), 8))
    alt = mc.simulate_rotation(shocked, mc.eligibility(shocked, min_age=30, min_qvol=0), p)
    tb = base.trades[base.trades["date"] <= cut].reset_index(drop=True)
    ta = alt.trades[alt.trades["date"] <= cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(tb[["date", "slot", "sell", "buy", "why"]], ta[["date", "slot", "sell", "buy", "why"]])
    np.testing.assert_allclose(base.returns.loc[:cut].to_numpy(), alt.returns.loc[:cut].to_numpy())


def test_signals_are_trailing():
    c = pd.DataFrame({"A": [1.0, 2, 3, 4, 5, 1]})
    np.testing.assert_allclose(mc.rolling_low(c, 3)["A"].to_numpy()[2:], [1, 2, 3, 1])
    rp = mc.range_position(c, 3)["A"].to_numpy()
    assert rp[4] == 1.0 and rp[5] == 0.0


def test_costs_charged_on_each_swap():
    """Prix en paliers : A chute, est achetée au plus bas, fait 4x, est vendue pour B (en bas de sa
    fourchette). Gain exact 4 × (1 − c)³ : achat de A, vente de A, achat de B."""
    n = 150
    a = np.r_[np.full(40, 10.0), np.full(50, 2.5), np.full(60, 10.0)]
    b = np.r_[np.full(60, 5.0), np.full(90, 1.0)]
    panel = mc.build_panel(_frames({"AUSDT": a, "BUSDT": b}))
    el = mc.eligibility(panel, min_age=1, min_qvol=0)
    p = mc.RotationParams(mult=3.0, low_win=30, range_win=60, bottom=0.2, k=1, cost=0.01)
    res = mc.simulate_rotation(panel, el, p, start=panel.dates[60])
    assert list(res.trades["why"]) == ["entrée", "rotation"]
    assert list(res.trades["buy"]) == ["AUSDT", "BUSDT"] and res.trades.iloc[1]["sell"] == "AUSDT"
    assert res.trades.iloc[1]["date"] == panel.dates[91]          # décidé à la clôture du jour 90, exécuté le 91
    assert float((1 + res.returns).prod()) == pytest.approx(4.0 * (1 - 0.01) ** 3, rel=1e-9)


def test_delisted_coin_is_sold_at_last_traded_close():
    n = 100
    a = np.full(n, 10.0)
    a[20] = 12.0
    a[70:] = np.nan                                  # retiré de la cote après le jour 69
    b = np.full(n, 2.0)
    b[20] = 3.0
    panel = mc.build_panel(_frames({"AUSDT": a, "BUSDT": b}))
    assert not panel.active["AUSDT"].iloc[75]
    el = mc.eligibility(panel, min_age=1, min_qvol=0)
    p = mc.RotationParams(k=2, cost=0.0, delist_haircut=0.5)
    res = mc.simulate_rotation(panel, el, p, start=panel.dates[30])
    assert list(res.trades["why"]) == ["entrée", "entrée", "retrait"]
    assert res.trades.iloc[2]["date"] == panel.dates[70]
    assert float((1 + res.returns).prod()) == pytest.approx(0.5 * 0.5 + 0.5, rel=1e-9)


def test_basket_matches_equal_weight_without_costs():
    panel = _random_panel(n_sym=4, n_days=200)
    el = mc.eligibility(panel, min_age=1, min_qvol=0)
    res = mc.simulate_basket(panel, el, cost=0.0, rebalance_days=1, start=panel.dates[20])
    r = panel.close.pct_change().loc[res.returns.index].mean(axis=1)
    np.testing.assert_allclose(res.returns.to_numpy(), r.to_numpy(), rtol=1e-9)


def test_random_walks_give_no_rotation_edge_on_average():
    """Sans retour à la moyenne, la règle « vendre le 3x, acheter le bas » n'a pas d'espérance de gain
    sur la détention (moyenne de 30 univers simulés, sans frais)."""
    diffs = []
    for seed in range(30):
        panel = _random_panel(n_sym=10, n_days=400, seed=seed, vol=0.10)
        el = mc.eligibility(panel, min_age=60, min_qvol=0)
        p = mc.RotationParams(mult=2.0, cost=0.0)
        rot = mc.simulate_rotation(panel, el, p, start=panel.dates[60])
        hold = mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=panel.dates[60])
        diffs.append(np.log1p(rot.returns).sum() - np.log1p(hold.returns).sum())
    d = np.asarray(diffs)
    assert abs(d.mean()) < 3 * d.std(ddof=1) / math.sqrt(d.size)


def test_community_flag_is_point_in_time():
    c = pd.DataFrame({"A": [10, 12, 3, 2, 5, 3, 1]}, dtype=float)
    f = mc.community_flag(c)["A"].tolist()
    assert f == [False, False, False, False, True, True, True]   # −70 % au jour 3 (2 <= 0,3 × 12), ×2 au jour 4


def test_deflated_sharpe_rejects_pure_noise():
    rng = np.random.default_rng(0)
    rejects = 0
    for rep in range(40):
        trials = rng.normal(0, 0.02, size=(365 * 3, 50))
        sr = trials.mean(0) / trials.std(0, ddof=1)
        best = trials[:, int(np.argmax(sr))]
        rejects += mc.deflated_sharpe(best, sr)["dsr"] <= 0.95
    assert rejects >= 36                               # ≈ 5 % de faux positifs au plus


def test_deflated_sharpe_accepts_real_edge():
    rng = np.random.default_rng(1)
    trials = rng.normal(0, 0.02, size=(365 * 3, 50))
    trials[:, 0] += 0.004                              # Sharpe ≈ 3,8 annualisé
    sr = trials.mean(0) / trials.std(0, ddof=1)
    assert mc.deflated_sharpe(trials[:, 0], sr)["dsr"] > 0.95


def test_forward_stats_freeze_after_delisting():
    a = np.array([1, 2, 4, np.nan, np.nan, np.nan], float)
    panel = mc.build_panel(_frames({"AUSDT": a}))
    fw = mc.forward_stats(panel, 2)
    assert fw["ret"]["AUSDT"].iloc[1] == pytest.approx(1.0)      # 2 -> figé à 4
    assert fw["max"]["AUSDT"].iloc[0] == pytest.approx(3.0)


def test_newey_west_t_reduces_to_iid_without_lags():
    x = pd.Series(np.random.default_rng(2).normal(0.1, 1, 2000))
    t0 = mc.newey_west_t(x, 0)
    assert t0 == pytest.approx(x.mean() / (x.std(ddof=0) / math.sqrt(x.size)), rel=1e-6)
