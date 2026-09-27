"""pump.fun : courbe, variables sans information future, étiquettes censurées, P&L, signaux sociaux."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import pumpfun as pf
from tradebot import pumpfun_runners as pr
from tradebot import pumpfun_social as ps

T0 = 1_790_000_000_000
CREATOR = "DEV"


def _coin(**kw):
    c = {"mint": "MINTpump", "symbol": "TEST", "name": "Test", "creator": CREATOR, "created_timestamp": T0,
         "twitter": "https://x.com/testcoin", "telegram": "", "website": "", "description": "gm",
         "image_uri": "https://ipfs.io/ipfs/x", "quote_mint": pf.SOL_MINT, "mayhem_state": None,
         "virtual_sol_reserves": 30_000_000_000, "virtual_token_reserves": 1_073_000_000_000_000}
    c.update(kw)
    return c


def _trades(events):
    """events : (secondes après T0, côté, SOL, utilisateur) ; prix déduit de la courbe."""
    rows, sold = [], 0.0
    for k, (s, side, sol, user) in enumerate(events):
        p = float(pf.curve_price(sold / pf.REAL_TOKEN0))
        tok = sol / p
        sold += tok if side == "buy" else -tok
        p_after = float(pf.curve_price(max(sold, 0) / pf.REAL_TOKEN0))
        rows.append({"ts": T0 + int(s * 1000), "slot": f"{k:022d}", "side": side, "program": "pump",
                     "price_sol": p_after, "price_usd": p_after * 120, "sol": sol, "usd": sol * 120,
                     "tokens": tok, "user": user, "tx": f"tx{k}"})
    return pd.DataFrame(rows)


def _candles(prices, start=T0, dur=60_000):
    p = np.asarray(prices, float)
    return pd.DataFrame({"ts": start + dur * np.arange(p.size), "open": p, "high": p, "low": p, "close": p,
                         "volume": 1.0, "dur": dur})


# -- courbe ------------------------------------------------------------------
def test_curve_round_trip_and_graduation_price():
    x = np.linspace(0, 1, 11)
    assert np.allclose(pf.curve_progress(pf.curve_price(x)), x)
    assert pf.curve_price(0.0) == pytest.approx(30 / 1.073e9)
    assert pf.GRAD_PRICE_SOL * pf.TOTAL_SUPPLY == pytest.approx(410.9, rel=1e-3)    # ≈ 411 SOL de market cap


def test_curve_buy_sell_costs_money():
    p = float(pf.curve_price(0.3))
    avg = pf.curve_buy_price(p, 1.0, fee=0.0)
    assert avg > p                                              # le glissement renchérit l'achat
    back = pf.curve_sell_value(p * 1.0000001, 1.0 / avg, fee=0.0)
    assert back < 1.0
    assert pf.curve_buy_price(p, 1.0, fee=0.01) > avg


def test_standard_curve_excludes_mayhem_and_other_quotes():
    assert pf.is_standard_curve(_coin())
    assert not pf.is_standard_curve(_coin(mayhem_state="active"))
    assert not pf.is_standard_curve(_coin(quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
    assert not pf.is_standard_curve(_coin(virtual_sol_reserves=25_000_000_000))
    c = _coin()
    del c["mayhem_state"]
    assert not pf.is_standard_curve(c)                        # instantané ancien : type inconnu


def test_parse_trades_orders_and_dedups():
    raw = [{"timestamp": "2026-09-27T06:18:02.000Z", "slotIndexId": "2", "type": "sell", "program": "pump",
            "priceSol": "3e-8", "priceUsd": "3e-6", "amountSol": "0.1", "amountUsd": "12", "baseAmount": "1",
            "userAddress": "B", "tx": "t2"},
           {"timestamp": "2026-09-27T06:18:00.000Z", "slotIndexId": "1", "type": "buy", "program": "pump",
            "priceSol": "2.9e-8", "priceUsd": "3e-6", "amountSol": "0.2", "amountUsd": "24", "baseAmount": "2",
            "userAddress": "A", "tx": "t1"}]
    df = pf.parse_trades(raw + raw[:1])
    assert list(df["tx"]) == ["t1", "t2"] and df["price_sol"].dtype == float


# -- variables point-in-time -------------------------------------------------
def _events():
    ev = [(0, "buy", 1.0, CREATOR), (0, "buy", 0.5, "S1"), (1, "buy", 0.4, "S2"), (20, "buy", 0.2, "A"),
          (40, "sell", 0.1, "S1"), (70, "buy", 2.0, "B"), (200, "buy", 0.3, "C"), (400, "sell", 0.5, CREATOR)]
    return ev


def test_features_ignore_trades_at_or_after_t():
    tr = _trades(_events())
    t = T0 + 60_000
    f = pr.features_at("launch_1m", t, _coin(), tr, _candles([3e-8] * 10))
    assert f["n_trades"] == 5 and f["n_buyers"] == 4
    assert f["dev_buy_sol"] == pytest.approx(1.0) and f["dev_sold"] == 0
    assert f["snipe_n0"] == 1 and f["snipe_n3"] == 2           # le créateur n'est pas un sniper
    # changer tout ce qui suit t ne change aucune variable
    tr2 = tr.copy()
    late = tr2["ts"] >= t
    tr2.loc[late, "sol"] *= 50
    tr2.loc[late, "user"] = "X"
    f2 = pr.features_at("launch_1m", t, _coin(), tr2, _candles([3e-8] * 10))
    assert f == f2


def test_top_holder_share_and_dev_hold():
    tr = _trades(_events())
    f = pr.wallet_features(tr, T0 + 1_000_000, CREATOR, T0)
    assert 0 < f["top1_share"] <= f["top10_share"] <= 1
    assert f["dev_sold"] == 1 and f["buyers_who_sold"] > 0


def test_social_link_parsing():
    assert ps.twitter_kind("https://x.com/abc") == "profile"
    assert ps.twitter_kind("https://twitter.com/abc/status/123") == "status"
    assert ps.twitter_kind("https://x.com/i/communities/1789") == "community"
    assert ps.twitter_kind("https://x.com/search?q=%24ABC") == "search"
    assert ps.twitter_kind("") == "none"
    f = ps.link_features(_coin(symbol="TESTCOIN", twitter="x.com/testcoin", website="https://usepaid.app/t/x"))
    assert f["tw_profile"] == 1 and f["tw_handle_matches"] == 1 and f["website_launchpad"] == 1


def test_dex_features_are_point_in_time():
    o = pd.DataFrame({"kind": ["tokenProfile", "boost"], "status": ["approved"] * 2, "amount": [None, 50],
                      "paid_ms": [T0 + 100, T0 + 10_000]})
    assert ps.dex_features(o, T0 + 50) == {"dex_paid_profile": 0, "dex_boost_amount": 0.0, "dex_n_orders": 0}
    assert ps.dex_features(o, T0 + 200)["dex_paid_profile"] == 1
    assert ps.dex_features(o, T0 + 20_000)["dex_boost_amount"] == 50


# -- étiquettes et P&L -------------------------------------------------------
def test_labels_censored_until_horizon_elapsed():
    c = _candles(np.r_[np.full(30, 3e-8), np.full(100, 4e-7)])
    t = T0 + 5 * 60_000
    lab = pr.labels_at(t, now_ms=t + 30 * 60_000, p_ref=3e-8, sol_usd=120, candles=c, grad_ms=math.nan, moment="launch_5m")
    assert math.isnan(lab["x10_1h"]) and math.isnan(lab["grad_1h"])
    lab = pr.labels_at(t, now_ms=t + 2 * 3_600_000, p_ref=3e-8, sol_usd=120, candles=c, grad_ms=T0 + 40 * 60_000,
                       moment="launch_5m")
    assert lab["x10_1h"] == 1.0 and lab["x3_1h"] == 1.0 and lab["grad_1h"] == 1.0
    assert lab["mcap100k_1h"] == 0.0                     # 4e-7 × 1e9 × 120 $ = 48 k$
    assert math.isnan(lab["x10_6h"])


def test_labels_ignore_candle_containing_t():
    """Le plus haut de la bougie en cours à t (atteint avant t) ne compte pas."""
    c = _candles([3e-8, 9e-7, 3e-8, 3e-8])
    lab = pr.labels_at(T0 + 90_000, T0 + 10 * 3_600_000, 3e-8, 120, c, math.nan, "launch_1m")
    assert lab["maxmult_1h"] == pytest.approx(1.0)


def test_pnl_take_profit_uses_closes_and_costs():
    p0 = float(pf.curve_price(0.05))
    c = _candles(np.r_[p0, p0 * 3.5, np.full(60, p0 * 0.2)])
    costs = pr.Costs(size_sol=0.1, curve_fee=0.0125, priority_sol=0.0)
    out = pr.pnl_at(T0 + 1, T0 + 2 * 3_600_000, p0, c, costs)
    assert out["pnl_tp3_1h"] == pytest.approx(3.5 * 0.975, rel=0.03)       # vendu à la clôture ×3,5, frais des deux côtés
    assert out["pnl_hold_1h"] < 0.25
    assert out["pnl_tp5_1h"] == out["pnl_hold_1h"]                           # objectif jamais atteint
    assert pr.pnl_at(T0, T0 + 1000, p0, c, costs) == {}                     # horizon pas écoulé


def test_splice_candles_prefers_finer_resolution():
    fine = _candles([2.0, 3.0], start=T0 + 600_000)
    coarse = _candles([1.0, 1.5, 9.0], start=T0, dur=300_000)
    s = pr.splice_candles([fine, coarse])
    assert list(s["ts"]) == [T0, T0 + 300_000, T0 + 600_000, T0 + 660_000]
    assert 9.0 not in set(s["close"])                           # la bougie 5 min recouverte par le 1 min est retirée


def test_token_rows_skip_launch_moments_after_graduation_and_future():
    tr = _trades(_events())
    c = _candles(np.full(20, float(pf.curve_price(0.1))))
    td = pr.TokenData(coin=_coin(), candles=c, trades={"launch": tr}, grad_ms=T0 + 3 * 60_000)
    rows = pr.token_rows(td, now_ms=T0 + 4 * 60_000, moments=pr.MOMENTS)
    assert [r["moment"] for r in rows] == ["launch_1m"]


def test_score_logit_matches_sklearn():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler
    rng = np.random.default_rng(0)
    X = rng.lognormal(size=(300, 3))
    X[rng.random(X.shape) < 0.1] = np.nan
    y = (np.nan_to_num(X[:, 0]) + rng.normal(0, 1, 300) > 1.5).astype(int)
    m = make_pipeline(SimpleImputer(strategy="median"), FunctionTransformer(lambda a: np.sign(a) * np.log1p(np.abs(a))),
                      StandardScaler(), LogisticRegression()).fit(X, y)
    imp, _, sc, lr = (s[1] for s in m.steps)
    model = {"features": ["a", "b", "c"], "median": imp.statistics_.tolist(), "mean": sc.mean_.tolist(),
             "scale": sc.scale_.tolist(), "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}
    for row in X[:20]:
        got = pr.score_logit(model, dict(zip("abc", row)))
        assert got == pytest.approx(m.predict_proba(row[None])[0, 1], rel=1e-9)
