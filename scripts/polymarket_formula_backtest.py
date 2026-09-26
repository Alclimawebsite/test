#!/usr/bin/env python
"""La formule exacte P(Up) marche-t-elle, et peut-on gagner avec ? — backtest papier.

Usage (depuis la racine, venv activé) :

    python scripts/polymarket_formula_backtest.py              # historique + carnet réel + README
    python scripts/polymarket_formula_backtest.py --skip-live  # historique seul
    python scripts/polymarket_formula_backtest.py --boot 500   # bootstrap plus court

1. Historique BTC 5m/15m (04/09–24/09/2026, tous les marchés) : formule
   (``tradebot.polymarket_formula``) à 17 instants de décision avec Binance 1 s, 3 estimateurs de σ
   (Parkinson 1 m, EWMA 1 s, TimesFM) ; calibration contre l'issue officielle ; comparaison au prix
   des trades preneurs au même instant ; P&L preneur au prix réellement payé à t + δ.
2. Carnet réel (collecteur WebSocket, ms) : mêmes calculs au meilleur ask reconstruit à t + δ, et
   délai de réaction du carnet aux mouvements de la formule (trades agrégés Binance, ms).
3. ``reports/polymarket/formule/`` : README.md, CSV, PNG.

Choix (demi-vie EWMA, estimateur principal, marge, décalage Chainlink) : 04/09–13/09 ; évaluation :
14/09–24/09. IC : bootstrap groupé par créneau de 15 min. Aucun ordre, aucune clé.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradebot import polymarket as pm  # noqa: E402
from tradebot import polymarket_backtest as pb  # noqa: E402
from tradebot import polymarket_formula_backtest as fb  # noqa: E402
from tradebot.config import CACHE_DIR, REPORTS_DIR  # noqa: E402
from tradebot.report import (BG, BLUE, GRID, ORANGE, TEXT, TEXT_2, _draw_header, _header,  # noqa: E402
                             _pyplot, _save, _style_axes, fmt_number, to_markdown)

log = logging.getLogger("formula_backtest")

OUT = REPORTS_DIR / "polymarket" / "formule"
CACHE = CACHE_DIR / "pm_formula"
BN1S_DIR = CACHE_DIR / "pm_maker" / "binance_1s"
TRADES_DIR = CACHE_DIR / "polymarket" / "wallets" / "trades"
SIGMA_CSV = REPORTS_DIR / "timesfm_amplitude" / "sigma_par_marche.csv"
AQUA, YELLOW, VIOLET, GREY = "#1baf7a", "#eda100", "#4a3aa7", "#9b9a96"
START = pd.Timestamp("2026-09-04", tz="UTC")
MID = pd.Timestamp("2026-09-14", tz="UTC")
END = pd.Timestamp("2026-09-25", tz="UTC")
LABELS = [d[0] for d in fb.DECISIONS]
PRETTY = {lab: lab.replace("-", "−") for lab in LABELS}
HL_GRID = (15, 30, 60, 120, 300, 600, 1800)
MARGIN_GRID = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.25, 0.30)
K_GRID = tuple(np.round(np.arange(0.8, 2.21, 0.05), 2))
SIG_NAMES = {"park": "Parkinson 1 m (60 min)", "ewma": "EWMA 1 s", "tfm": "TimesFM"}
LIVE_QTY = (10, 100)
MARGIN_DELTA = 0
MAX_AGE_S = 300


class Runtime:
    def __init__(self):
        self.rows: list[dict] = []

    @contextmanager
    def __call__(self, name: str):
        t0 = time.perf_counter()
        log.info("… %s", name)
        yield
        dt = time.perf_counter() - t0
        self.rows.append({"etape": name, "secondes": round(dt, 2)})
        log.info("   %s : %.1f s", name, dt)


def c_(x, d: int = 1) -> str:
    """Centimes signés."""
    return fmt_number(100 * x, d, signed=True) + " c" if np.isfinite(x) else "—"


def ci_c(est, lo, hi, d: int = 1) -> str:
    return f"{c_(est, d)} (IC {c_(lo, d)} ; {c_(hi, d)})"


def n_(x) -> str:
    return fmt_number(x, 0)


def f_(x, d: int = 3, signed: bool = False) -> str:
    return fmt_number(x, d, signed=signed)


# ---------------------------------------------------------------------------
# 1. Chargement
# ---------------------------------------------------------------------------
def load_markets() -> pd.DataFrame:
    client = pm.PolymarketClient()
    M = pb.market_table(client, ["btc"], ["5m", "15m"], START, END, max_workers=8)
    M = M[np.isfinite(M["y"])].reset_index(drop=True)
    em = pd.read_parquet(CACHE_DIR / "timesfm_amplitude" / "event_meta.parquet")
    mc = pd.read_csv(REPORTS_DIR / "polymarket" / "markets.csv", usecols=["slug", "price_to_beat", "final_price"])
    em = pd.concat([em, mc], ignore_index=True).drop_duplicates("slug")
    M = M.merge(em, on="slug", how="left")
    sig = pd.read_csv(SIGMA_CSV, usecols=["slug", "sigma_timesfm_bp"])
    M = M.merge(sig, on="slug", how="left")
    M["hD"] = M["duration_s"] // 60 + 1
    M["sigma_tfm"] = M["sigma_timesfm_bp"] * 1e-4 / np.sqrt(M["hD"] * 60.0)
    M["train"] = M["start_ts"] < int(MID.timestamp())
    return M


def load_binance_hist() -> fb.SecondPrices:
    days = pd.date_range(START - pd.Timedelta(days=1), END - pd.Timedelta(days=1), freq="D")
    parts = [pd.read_parquet(BN1S_DIR / f"BTCUSDT_{d:%Y-%m-%d}.parquet") for d in days]
    df = pd.concat(parts, ignore_index=True).drop_duplicates("sec").sort_values("sec")
    return fb.SecondPrices(df["sec"].to_numpy(), df["close"].to_numpy())


def load_bars_1m() -> pd.DataFrame:
    b = pd.read_parquet(CACHE_DIR / "BTCUSDT_1m.parquet", columns=["open", "high", "low", "close"])
    return b[(b.index >= START - pd.Timedelta(days=1)) & (b.index < END + pd.Timedelta(hours=1))]


def load_trades() -> tuple[dict[str, fb.MarketTrades], dict]:
    """Trades preneurs : fichiers ``*_taker`` ; pour les marchés absents de ces fichiers (23/09 en
    partie, 24/09), jambes ``role == "taker"`` des fichiers ``*_all`` (même collecte data-api)."""
    out: dict[str, fb.MarketTrades] = {}
    cols = ["ts", "side", "outcome", "price", "size", "slug", "seq"]
    src = {"taker": 0, "all": 0}
    for d in pd.date_range(START, END - pd.Timedelta(days=1), freq="D"):
        for dur in ("5m", "15m"):
            path = TRADES_DIR / f"btc_{dur}_{d:%Y-%m-%d}_taker.parquet"
            if path.exists():
                got = fb.prepare_trades(pd.read_parquet(path, columns=cols))
                out.update(got)
                src["taker"] += len(got)
            path = TRADES_DIR / f"btc_{dur}_{d:%Y-%m-%d}_all.parquet"
            if path.exists():
                a = pd.read_parquet(path, columns=cols + ["role"])
                a = a[(a["role"].astype(str) == "taker") & ~a["slug"].astype(str).isin(out.keys())]
                got = fb.prepare_trades(a.drop(columns="role"))
                out.update(got)
                src["all"] += len(got)
    return out, src


# ---------------------------------------------------------------------------
# 2. Grille (marché × instant)
# ---------------------------------------------------------------------------
def build_grid(M: pd.DataFrame, P: fb.SecondPrices, bars: pd.DataFrame, trades: dict, rt: Runtime) -> tuple[pd.DataFrame, dict]:
    n, K = len(M), len(fb.DECISIONS)
    S = M["start_ts"].to_numpy("int64")
    E = M["end_ts"].to_numpy("int64")
    T = fb.decision_times(S, E)
    G = pd.DataFrame({
        "i": np.repeat(np.arange(n), K), "k": np.tile(np.arange(K), n), "t": T.ravel(),
        "S": np.repeat(S, K), "E": np.repeat(E, K),
    })
    G["label"] = np.array(LABELS)[G["k"]]
    for c in ("slug", "duration", "y", "train", "slot", "sigma_tfm", "price_to_beat", "final_price"):
        G[c] = np.repeat(M[c].to_numpy(), K)
    ctx: dict = {}
    with rt("2a. entrées de la formule (Binance 1 s) et σ"):
        inp0 = fb.formula_inputs(P, G["t"].to_numpy(), G["S"].to_numpy(), G["E"].to_numpy())
        for k_, v in inp0.items():
            G[f"in_{k_}"] = v
        park = fb.parkinson_sigma_series(bars, n=60)
        G["sigma_park"] = fb.bar_sigma_at(park, G["t"].to_numpy())
        kk = G["t"].to_numpy() - P.t0
        ev = {}
        for hl in HL_GRID:
            v = fb.ewma_var_series(P, hl)
            ev[hl] = np.sqrt(v[kk])
        ctx["ewma_by_hl"] = ev
    return G, ctx


def probs(G: pd.DataFrame, sigma: np.ndarray, lag_inputs: dict | None = None, extra_sd: float = 0.0) -> np.ndarray:
    inp = lag_inputs if lag_inputs is not None else {k[3:]: G[k].to_numpy() for k in G.columns if k.startswith("in_")}
    return fb.formula_probs(inp, G["S"].to_numpy(), G["E"].to_numpy(), sigma, extra_sd=extra_sd)["p"]


def chainlink_alignment(M: pd.DataFrame, P: fb.SecondPrices, lags=range(0, 8)) -> pd.DataFrame:
    """Binance contre priceToBeat / finalPrice (Chainlink) : écart de niveau et erreur sur F − K
    selon le décalage c (moyennes Binance sur (a − c, b − c]), par moitié."""
    S = M["start_ts"].to_numpy("int64")
    E = M["end_ts"].to_numpy("int64")
    rows = []
    for c in lags:
        K = P.window_mean(S - 60 - c, S - c)
        F = P.window_mean(E - 60 - c, E - c)
        lvl = (K - np.log(M["price_to_beat"].to_numpy())) * 1e4
        mv = ((F - K) - np.log(M["final_price"].to_numpy() / M["price_to_beat"].to_numpy())) * 1e4
        agree = ((F >= K) == (M["y"].to_numpy() > 0.5))
        for half, m in (("1re moitié", M["train"].to_numpy()), ("2e moitié", ~M["train"].to_numpy())):
            ok = m & np.isfinite(mv)
            rows.append({"decalage_s": c, "moitie": half, "n": int(ok.sum()), "ecart_niveau_moyen_pb": float(np.mean(lvl[ok])),
                         "ecart_niveau_sd_pb": float(np.std(lvl[ok])), "rmse_F_moins_K_pb": float(np.sqrt(np.mean(mv[ok] ** 2))),
                         "accord_issue": float(np.mean(agree[ok]))})
    return pd.DataFrame(rows)


def market_and_exec(G: pd.DataFrame, M: pd.DataFrame, trades: dict) -> dict[str, np.ndarray]:
    """Prix du marché à t (dernier trade, VWAP 3 s) et premiers achats preneurs de chaque jeton
    dans [t + δ, t + δ + 2] pour tous les δ."""
    n, K = len(M), len(fb.DECISIONS)
    T = G["t"].to_numpy().reshape(n, K)
    out = {k: np.full((n, K), np.nan) for k in ("last", "age", "vwap", "n_win")}
    for d in fb.HIST_DELAYS:
        for tok in ("up", "dn"):
            out[f"x_{tok}_{d:g}"] = np.full((n, K), np.nan)
            out[f"xt_{tok}_{d:g}"] = np.full((n, K), np.nan)
    for i, slug in enumerate(M["slug"]):
        tr = trades.get(slug)
        if tr is None or tr.ts.size == 0:
            continue
        mp = fb.market_price_at(tr, T[i], window_s=3)
        for k in ("last", "age", "vwap", "n_win"):
            out[k][i] = mp[k]
        for tok, code in (("up", 0), ("dn", 1)):
            m = (tr.token == code) & tr.buy
            ts, px = tr.ts[m], tr.price[m]
            for d in fb.HIST_DELAYS:
                p_, at = fb.first_trade_in(ts, px, T[i] + d, T[i] + d + 2)
                out[f"x_{tok}_{d:g}"][i] = p_
                out[f"xt_{tok}_{d:g}"][i] = at
    return {k: v.ravel() for k, v in out.items()}


# ---------------------------------------------------------------------------
# 3. Évaluation
# ---------------------------------------------------------------------------
def prob_scores(y, p) -> dict:
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    ok = np.isfinite(y) & np.isfinite(p)
    if ok.sum() == 0:
        return {"n": 0, "brier": math.nan, "logloss": math.nan, "auc": math.nan, "accuracy": math.nan}
    y, p = y[ok], p[ok]
    auc = float(pb.weighted_auc(y, p, np.ones((1, y.size)))[0]) if np.unique(y).size == 2 else math.nan
    return {"n": int(ok.sum()), "brier": float(np.mean(fb.brier(y, p))), "logloss": float(np.mean(fb.log_loss(y, p))),
            "auc": auc, "accuracy": float(np.mean((p >= 0.5) == (y > 0.5)))}


def calib_slope(y, p) -> float:
    """Pente de calibration : logistique y ~ a + b·logit(p) (1 = calibrée, < 1 = trop sûre d'elle)."""
    from sklearn.linear_model import LogisticRegression

    y = np.asarray(y, float)
    x = fb.logit(p)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 50 or np.unique(y[ok]).size < 2:
        return math.nan
    lr = LogisticRegression(C=1e4, max_iter=1000).fit(x[ok, None], y[ok].astype(int))
    return float(lr.coef_[0][0])


def phase_of(G: pd.DataFrame) -> np.ndarray:
    t, S, E = G["t"].to_numpy(), G["S"].to_numpy(), G["E"].to_numpy()
    return np.select([t <= S - 60, t <= S, t <= E - 60], [1, 2, 3], 4)


PHASE_NAMES = {2: "phase 2 (S−60 < t ≤ S : TWAP de départ en cours)", 3: "phase 3 (S < t ≤ E−60 : K connu)",
               4: "phase 4 (t > E−60 : moyenne finale en cours)"}


def run_history(args, rt: Runtime) -> dict:
    ctx: dict = {}
    with rt("1a. marchés BTC 5m/15m (cache), issue officielle, priceToBeat/finalPrice, σ TimesFM"):
        M = load_markets()
    with rt("1b. Binance 1 s (cache pm_maker) et 1 m (cache)"):
        P = load_binance_hist()
        bars = load_bars_1m()
    with rt("1c. trades preneurs (cache wallets/trades)"):
        trades, src = load_trades()
    ctx["trade_sources"] = src
    ctx["n_markets"] = len(M)
    ctx["n_trades"] = int(sum(tr.ts.size for tr in trades.values()))
    ctx["n_with_trades"] = int(sum(1 for s in M["slug"] if s in trades))
    ctx["by_cell"] = M.groupby("duration").agg(n=("slug", "size"), up=("y", "mean")).reset_index()

    G, gctx = build_grid(M, P, bars, trades, rt)
    train = G["train"].to_numpy()
    y = G["y"].to_numpy()

    with rt("2b. alignement Binance / Chainlink (priceToBeat, finalPrice)"):
        align = chainlink_alignment(M, P)
        a1 = align[align["moitie"] == "1re moitié"].set_index("decalage_s")
        c_best = int(a1["rmse_F_moins_K_pb"].idxmin())
        b_sd = float(a1.loc[c_best, "rmse_F_moins_K_pb"]) * 1e-4
        ctx.update(align=align, lag=c_best, basis_sd=b_sd, basis_sd0=float(a1.loc[0, "rmse_F_moins_K_pb"]) * 1e-4)

    with rt("2c. choix de la demi-vie EWMA (1re moitié, log-loss)"):
        hl_rows = []
        for hl, sg in gctx["ewma_by_hl"].items():
            p = probs(G, sg)
            m = train & np.isfinite(p)
            hl_rows.append({"demi_vie_s": hl, "logloss_1re_moitie": float(np.mean(fb.log_loss(y[m], p[m]))),
                            "brier_1re_moitie": float(np.mean(fb.brier(y[m], p[m]))), "n": int(m.sum())})
        hl_tab = pd.DataFrame(hl_rows)
        hl_best = int(hl_tab.loc[hl_tab["logloss_1re_moitie"].idxmin(), "demi_vie_s"])
        G["sigma_ewma"] = gctx["ewma_by_hl"][hl_best]
        ctx.update(hl_tab=hl_tab, hl=hl_best)

    with rt("2d. probabilités de la formule (3 σ bruts, σ × k, variante corrigée)"):
        base_inp = {k[3:]: G[k].to_numpy() for k in G.columns if k.startswith("in_")}
        for s in ("park", "ewma", "tfm"):
            G[f"p_{s}"] = probs(G, G[f"sigma_{s}"].to_numpy())
        common = train & np.isfinite(G[["p_park", "p_ewma", "p_tfm"]]).all(axis=1).to_numpy()
        sel_rows, k_rows = [], []
        for s in ("park", "ewma", "tfm"):
            sig = G[f"sigma_{s}"].to_numpy()
            ll = {}
            for kf in K_GRID:
                p = probs(G, kf * sig)
                ll[kf] = float(np.mean(fb.log_loss(y[common], p[common])))
                k_rows.append({"sigma": s, "k": kf, "logloss_1re_moitie": ll[kf]})
            k_best = min(ll, key=ll.get)
            G[f"p_{s}_k"] = probs(G, k_best * sig)
            p0 = G[f"p_{s}"].to_numpy()
            sel_rows.append({"sigma": s, "logloss_1re_moitie": float(np.mean(fb.log_loss(y[common], p0[common]))),
                             "brier_1re_moitie": float(np.mean(fb.brier(y[common], p0[common]))),
                             "k": k_best, "logloss_1re_moitie_k": ll[k_best], "n": int(common.sum())})
        sel = pd.DataFrame(sel_rows)
        best = str(sel.loc[sel["logloss_1re_moitie"].idxmin(), "sigma"])
        best_k = str(sel.loc[sel["logloss_1re_moitie_k"].idxmin(), "sigma"])
        k_val = float(sel.set_index("sigma").loc[best_k, "k"])
        ctx.update(sig_sel=sel, sig_best=best, sig_best_k=best_k, k_val=k_val, k_tab=pd.DataFrame(k_rows))
        G["p_f"] = G[f"p_{best}"]
        inp_c = fb.formula_inputs(P, G["t"].to_numpy(), G["S"].to_numpy(), G["E"].to_numpy(), lag_s=c_best)
        sig_c = k_val * G[f"sigma_{best_k}"].to_numpy()
        G["p_corr"] = probs(G, sig_c, lag_inputs=inp_c, extra_sd=b_sd)
        fo = fb.formula_probs(base_inp, G["S"].to_numpy(), G["E"].to_numpy(), G[f"sigma_{best}"].to_numpy())
        G["m"], G["sd"] = fo["m"], fo["sd"]
        fc = fb.formula_probs(inp_c, G["S"].to_numpy(), G["E"].to_numpy(), sig_c, extra_sd=b_sd)
        G["m_corr"], G["sd_corr"] = fc["m"], fc["sd"]
        G["phase"] = phase_of(G)
        # ratio de variance des rendements Binance 1 s (1re moitié) : pourquoi σ doit être agrandi
        tr_end = int(MID.timestamp()) - P.t0
        x = P.x[: tr_end]
        r1 = np.diff(x)
        v1 = float(np.nanvar(r1))
        ctx["vr"] = pd.DataFrame([{"horizon_s": h, "ratio_variance": float(np.nanvar(x[h:] - x[:-h]) / (h * v1))}
                                  for h in (1, 2, 5, 10, 30, 60, 120, 300, 900)])
        ctx["ac"] = pd.DataFrame([{"retard_s": l_, "autocorrelation": float(np.corrcoef(r1[:-l_], r1[l_:])[0, 1])}
                                  for l_ in (1, 2, 3, 5, 10, 30)])
        ctx["zero_ret"] = float(np.mean(r1 == 0))

    with rt("2e. prix du marché à t et exécutions à t + δ (trades preneurs)"):
        mx = market_and_exec(G, M, trades)
        for k, v in mx.items():
            G[k] = v
        G["p_mkt"] = np.where(G["age"] <= MAX_AGE_S, G["last"], np.nan)
        G["p_ref"] = np.where(G["n_win"] > 0, G["vwap"], G["p_mkt"])
    ctx["G"] = G
    ctx["M"] = M

    with rt("3a. calibration et scores par instant / phase / durée"):
        evaluate(G, ctx, args)
    with rt("3b. empilement et information au-delà du marché"):
        stacking(G, ctx, args)
    with rt("3c. P&L preneur selon la latence δ"):
        pnl_latency(G, ctx, args)
    with rt("3d. exemple chiffré à S−10 s"):
        ctx["example"] = example_row(G, ctx)
    return ctx


VARIANTS = {"f": "brute", "corr": "corrigée"}


def evaluate(G: pd.DataFrame, ctx: dict, args) -> None:
    test = ~G["train"].to_numpy()
    y = G["y"].to_numpy()
    boot = pb.SlotBootstrap(G["slot"].to_numpy(), B=args.boot, seed=1)
    rows, rows_sig = [], []
    pm_ = G["p_mkt"].to_numpy()
    pv = G["vwap"].to_numpy()
    for dur in ("5m", "15m", "tous"):
        md = np.ones(len(G), bool) if dur == "tous" else (G["duration"] == dur).to_numpy()
        for k, lab in enumerate(LABELS):
            mk = md & (G["k"] == k).to_numpy()
            for s in ("park", "ewma", "tfm", "park_k", "ewma_k", "tfm_k", "corr"):
                p = G[f"p_{s}"].to_numpy()
                sc = prob_scores(y[mk & test], p[mk & test])
                rows_sig.append({"duree": dur, "instant": lab, "sigma": s, **sc,
                                 "pente_calibration": calib_slope(y[mk & test], p[mk & test])})
            for v, vname in VARIANTS.items():
                pf = G[f"p_{v}"].to_numpy()
                m = mk & test & np.isfinite(pf) & np.isfinite(pm_)
                sf, sm = prob_scores(y[m], pf[m]), prob_scores(y[m], pm_[m])
                e_b, lo_b, hi_b = boot.mean(fb.brier(y, pf) - fb.brier(y, pm_), m)
                e_l, lo_l, hi_l = boot.mean(fb.log_loss(y, pf) - fb.log_loss(y, pm_), m)
                mv = mk & test & np.isfinite(pv) & np.isfinite(pf)
                rows.append({"duree": dur, "instant": lab, "variante": vname, "n": sm["n"],
                             "brier_formule": sf["brier"], "brier_marche": sm["brier"],
                             "d_brier": e_b, "d_brier_lo": lo_b, "d_brier_hi": hi_b,
                             "logloss_formule": sf["logloss"], "logloss_marche": sm["logloss"], "d_logloss": e_l,
                             "d_logloss_lo": lo_l, "d_logloss_hi": hi_l, "auc_formule": sf["auc"], "auc_marche": sm["auc"],
                             "justesse_formule": sf["accuracy"], "justesse_marche": sm["accuracy"],
                             "n_vwap": int(mv.sum()), "brier_formule_vwap": prob_scores(y[mv], pf[mv])["brier"],
                             "brier_marche_vwap": prob_scores(y[mv], pv[mv])["brier"],
                             "age_median_s": float(np.nanmedian(G["age"].to_numpy()[m])) if m.any() else math.nan,
                             "pente_calibration_formule": calib_slope(y[m], pf[m]),
                             "pente_calibration_marche": calib_slope(y[m], pm_[m])})
    ctx["scores"] = pd.DataFrame(rows)
    ctx["scores_sigma"] = pd.DataFrame(rows_sig)
    # par phase × durée (2e moitié, + toute la période)
    ph = G["phase"].to_numpy()
    prow, cal_rows, tails = [], [], []
    for dur in ("5m", "15m", "tous"):
        md = np.ones(len(G), bool) if dur == "tous" else (G["duration"] == dur).to_numpy()
        for p_ in (2, 3, 4):
            for per, mper in (("2e moitié", test), ("toute la période", np.ones(len(G), bool))):
                m = md & (ph == p_) & mper
                for v, vname in VARIANTS.items():
                    pf = G[f"p_{v}"].to_numpy()
                    sc = prob_scores(y[m], pf[m])
                    prow.append({"duree": dur, "phase": p_, "periode": per, "variante": vname, **sc,
                                 "pente_calibration": calib_slope(y[m], pf[m])})
            m = md & (ph == p_) & test
            for v, vname in VARIANTS.items():
                cal = fb.calibration_deciles(y[m], G[f"p_{v}"].to_numpy()[m])
                cal.insert(0, "variante", vname)
                cal.insert(0, "phase", p_)
                cal.insert(0, "duree", dur)
                cal["se"] = np.sqrt(cal["up_rate"] * (1 - cal["up_rate"]) / cal["n"])
                cal_rows.append(cal)
                tc = fb.tail_check(y[m], G[f"p_{v}"].to_numpy()[m], lo=0.02)
                tc.insert(0, "variante", vname)
                tc.insert(0, "phase", p_)
                tc.insert(0, "duree", dur)
                tails.append(tc)
    ctx["scores_phase"] = pd.DataFrame(prow)
    ctx["calibration"] = pd.concat(cal_rows, ignore_index=True)
    ctx["tails"] = pd.concat(tails, ignore_index=True)
    # distribution de l'écart formule − marché par instant (2e moitié)
    drows = []
    for v, vname in VARIANTS.items():
        gap = G[f"p_{v}"].to_numpy() - pm_
        for k, lab in enumerate(LABELS):
            m = test & (G["k"] == k).to_numpy() & np.isfinite(gap)
            q = np.quantile(gap[m], [0.05, 0.25, 0.5, 0.75, 0.95]) if m.any() else [math.nan] * 5
            drows.append({"variante": vname, "instant": lab, "n": int(m.sum()), "moyenne": float(np.mean(gap[m])),
                          "ecart_type": float(np.std(gap[m])), "p05": q[0], "p25": q[1], "mediane": q[2], "p75": q[3],
                          "p95": q[4], "part_abs_sup_5pts": float(np.mean(np.abs(gap[m]) > 0.05)),
                          "part_abs_sup_10pts": float(np.mean(np.abs(gap[m]) > 0.10))})
    ctx["gap_dist"] = pd.DataFrame(drows)


def stacking(G: pd.DataFrame, ctx: dict, args) -> None:
    """Empilement logistique (logit formule corrigée, logit marché) appris sur la 1re moitié par
    durée × instant ; pente de l'écart : y − p_marché ~ b·(p_formule − p_marché), sans constante."""
    train = G["train"].to_numpy()
    test = ~train
    y = G["y"].to_numpy()
    pm_ = G["p_mkt"].to_numpy()
    boot = pb.SlotBootstrap(G["slot"].to_numpy(), B=args.boot, seed=2)
    rows, out = [], []
    for v, vname in VARIANTS.items():
        pf = G[f"p_{v}"].to_numpy()
        ps = np.full(len(G), np.nan)
        for dur in ("5m", "15m"):
            for k, lab in enumerate(LABELS):
                mk = (G["duration"] == dur).to_numpy() & (G["k"] == k).to_numpy() & np.isfinite(pf) & np.isfinite(pm_)
                coef = fb.fit_stack(y[mk & train], pf[mk & train], pm_[mk & train])
                mt = mk & test
                ps[mt] = fb.predict_stack(coef, pf[mt], pm_[mt])
                rows.append({"variante": vname, "duree": dur, "instant": lab, "a": coef[0], "b_formule": coef[1],
                             "c_marche": coef[2], "n_apprentissage": int((mk & train).sum())})
        G[f"p_stack_{v}"] = ps
        gap = pf - pm_
        resid = y - pm_
        for k, lab in enumerate(LABELS):
            m = test & (G["k"] == k).to_numpy() & np.isfinite(ps) & np.isfinite(pf) & np.isfinite(pm_)
            e, lo, hi = boot.mean(fb.brier(y, ps) - fb.brier(y, pm_), m)
            el, lol, hil = boot.mean(fb.log_loss(y, ps) - fb.log_loss(y, pm_), m)
            sl, sl_lo, sl_hi = boot.ratio(gap * resid, gap * gap, m)
            out.append({"variante": vname, "instant": lab, "n": int(m.sum()),
                        "brier_empilement": float(np.mean(fb.brier(y[m], ps[m]))),
                        "brier_marche": float(np.mean(fb.brier(y[m], pm_[m]))), "d_brier_emp_marche": e, "lo": lo, "hi": hi,
                        "d_logloss_emp_marche": el, "lo_ll": lol, "hi_ll": hil,
                        "pente_ecart": sl, "pente_lo": sl_lo, "pente_hi": sl_hi})
    ctx["stack"] = pd.DataFrame(out)
    ctx["stack_coefs"] = pd.DataFrame(rows)


def decide(G: pd.DataFrame, margin: float, pcol: str = "p_f") -> tuple[np.ndarray, np.ndarray]:
    au, ad = fb.estimated_asks(G["p_ref"].to_numpy())
    return fb.choose_side(G[pcol].to_numpy(), au, ad, margin)


def pnl_at(G: pd.DataFrame, side: np.ndarray, delta: float) -> tuple[np.ndarray, np.ndarray]:
    px = np.where(side == 1, G[f"x_up_{delta:g}"].to_numpy(), np.where(side == -1, G[f"x_dn_{delta:g}"].to_numpy(), np.nan))
    return fb.pnl_per_share(side, px, G["y"].to_numpy()), px


def pnl_groups(G: pd.DataFrame) -> dict[str, np.ndarray]:
    ph = G["phase"].to_numpy()
    groups = {"tous les instants": np.ones(len(G), bool),
              "avant S (S−45…S−2)": (G["t"] < G["S"]).to_numpy(),
              "ouverture (S…S+10)": ((G["t"] >= G["S"]) & (G["t"] <= G["S"] + 10)).to_numpy(),
              "milieu (S+30…E−60)": ((G["t"] >= G["S"] + 30) & (ph == 3)).to_numpy(),
              "fin (E−30, E−10)": (ph == 4)}
    for k, lab in enumerate(LABELS):
        groups[lab] = (G["k"] == k).to_numpy()
    return groups


def pnl_latency(G: pd.DataFrame, ctx: dict, args) -> None:
    """Marge choisie sur la 1re moitié pour chaque variante : P&L total maximal à δ = 0 s (cas le
    plus favorable : prix du premier trade de bloc >= t), au moins 200 positions exécutées ; puis
    P&L par part sur la 2e moitié selon δ. (À δ = 2 s, aucune marge n'est rentable sur la 1re
    moitié : voir ``marge_1re_moitie.csv``.)"""
    train = G["train"].to_numpy()
    test = ~train
    y = G["y"].to_numpy()
    boot = pb.SlotBootstrap(G["slot"].to_numpy(), B=args.boot, seed=3)
    groups = pnl_groups(G)
    au, ad = fb.estimated_asks(G["p_ref"].to_numpy())
    mrows, rows, margins = [], [], {}
    for v, vname in VARIANTS.items():
        for mg in MARGIN_GRID:
            side, _ = decide(G, mg, f"p_{v}")
            for half, mh in (("1re moitié", train), ("2e moitié", test)):
                for d in fb.HIST_DELAYS:
                    pnl, _ = pnl_at(G, side, d)
                    m = mh & (side != 0)
                    ex = m & np.isfinite(pnl)
                    mrows.append({"variante": vname, "marge": mg, "moitie": half, "delta_s": d, "decisions": int(m.sum()),
                                  "executees": int(ex.sum()), "pnl_total": float(np.nansum(pnl[ex])),
                                  "pnl_par_part": float(np.mean(pnl[ex])) if ex.any() else math.nan})
        mt = pd.DataFrame(mrows)
        cand = mt[(mt["variante"] == vname) & (mt["moitie"] == "1re moitié") & (mt["delta_s"] == MARGIN_DELTA) & (mt["executees"] >= 200)]
        margin = float(cand.loc[cand["pnl_total"].idxmax(), "marge"]) if len(cand) else 0.02
        margins[vname] = margin
        side, edge = decide(G, margin, f"p_{v}")
        G[f"side_{v}"] = side
        ask_est = np.where(side == 1, au, np.where(side == -1, ad, np.nan))
        pnl_est = fb.pnl_per_share(side, ask_est, y)
        for gname, gm in groups.items():
            for d in list(fb.HIST_DELAYS) + [-1]:
                if d >= 0:
                    pnl, px = pnl_at(G, side, d)
                else:                                   # exécution supposée à l'ask estimé à t
                    pnl, px = pnl_est, ask_est
                m = test & gm & (side != 0)
                ex = m & np.isfinite(pnl)
                e, lo, hi = boot.mean(pnl, ex)
                win = np.where(side == 1, y, 1 - y)
                rows.append({"variante": vname, "groupe": gname, "delta_s": d, "decisions": int(m.sum()),
                             "executees": int(ex.sum()), "taux_execution": float(ex.sum() / m.sum()) if m.sum() else math.nan,
                             "pnl_par_part": e, "lo": lo, "hi": hi, "pnl_total": float(np.nansum(pnl[ex])),
                             "prix_moyen": float(np.mean(px[ex])) if ex.any() else math.nan,
                             "surcout_vs_ask_estime": float(np.mean((px - ask_est)[ex])) if ex.any() else math.nan,
                             "avantage_estime": float(np.mean(edge[ex])) if ex.any() else math.nan,
                             "taux_gain": float(np.mean(win[ex])) if ex.any() else math.nan})
    ctx["margin_tab"] = pd.DataFrame(mrows)
    ctx["margins"] = margins
    ctx["pnl"] = pd.DataFrame(rows)
    # tests multiples : cellules instant × δ (variante corrigée et brute)
    P_ = ctx["pnl"]
    cells = P_[(P_["groupe"].isin(LABELS)) & (P_["delta_s"] >= 0) & (P_["executees"] >= 30)]
    ctx["cells"] = cells.groupby("variante").agg(n=("lo", "size"), pos=("lo", lambda x: int((x > 0).sum())),
                                                 neg=("hi", lambda x: int((x < 0).sum()))).reset_index()


def example_row(G: pd.DataFrame, ctx: dict) -> dict:
    """Un marché 5m de la 2e moitié à S−10 s, avec un écart TWAP partiel net (médian parmi ceux-ci)."""
    m = (~G["train"]) & (G["label"] == "S-10") & (G["duration"] == "5m") & np.isfinite(G["p_mkt"]) & \
        (np.abs(G["p_f"] - 0.5) > 0.08) & (np.abs(G["p_f"] - 0.5) < 0.2)
    r = G[m].iloc[len(G[m]) // 2]
    best, kb = ctx["sig_best"], ctx["sig_best_k"]
    return {"slug": r["slug"], "S": int(r["S"]), "t": int(r["t"]), "price": math.exp(float(r["in_log_price"])),
            "avg_price": math.exp(float(r["in_start_avg"])),
            "diff_bp": (float(r["in_log_price"]) - float(r["in_start_avg"])) * 1e4, "a": 50,
            "m": float(r["m"]), "vf": fb.variance_factor(-10, 300), "sigma": float(r[f"sigma_{best}"]), "sd": float(r["sd"]),
            "p": float(r["p_f"]), "sigma_c": ctx["k_val"] * float(r[f"sigma_{kb}"]), "m_c": float(r["m_corr"]),
            "sd_c": float(r["sd_corr"]), "p_c": float(r["p_corr"]), "p_mkt": float(r["p_mkt"]), "y": float(r["y"]),
            "sigma_name": best, "sigma_name_c": kb}


# ---------------------------------------------------------------------------
# 4. Carnet réel (fichiers du collecteur)
# ---------------------------------------------------------------------------
SYMBOL_OF = {"btc": "BTCUSDT", "eth": "ETHUSDT"}


def load_live_markets(now: float) -> tuple[list, pd.DataFrame]:
    from tradebot import polymarket_book as pbk

    kept, rows = [], []
    for slug in pbk.list_markets():
        meta = json.loads((pbk.LIVE_DIR / f"{slug}.meta.json").read_text())
        S = int(pd.Timestamp(meta["start"]).timestamp())
        E = int(pd.Timestamp(meta["end"]).timestamp())
        row = {"slug": slug, "asset": meta.get("asset"), "duree": meta.get("duration"), "S": S, "E": E,
               "debut_collecte_rel_S": round(float(meta.get("collector_started", math.nan)) - S, 1)}
        if E > now - 30:
            rows.append({**row, "statut": "fenêtre en cours"})
            continue
        m = pbk.load_market(slug)
        y = m.resolved_up
        if y is None:
            rows.append({**row, "statut": "issue inconnue (collecte interrompue)"})
            continue
        first, last = m.coverage()
        row.update(y=float(y), premier_evt_rel_S=round(first - S, 1), dernier_evt_rel_E=round(last - E, 1),
                   n_evenements=len(m.events))
        if not np.isfinite(last) or last < E - 15:
            rows.append({**row, "statut": "carnet incomplet (fin manquante)"})
            continue
        rows.append({**row, "statut": "exploitable"})
        kept.append(m)
    return kept, pd.DataFrame(rows)


def block_lag(markets: list, cache: Path) -> pd.DataFrame:
    """Retard horodatage de bloc (data-api) − horodatage d'appariement (WebSocket, ms) par trade,
    appariés par hash de transaction et prix."""
    from tradebot import polymarket_wallets as pw

    cids = sorted({m.meta["condition_id"] for m in markets})
    key = f"trades_live_{len(cids)}_{cids[0][:10]}_{cids[-1][:10]}.parquet"
    path = cache / key
    if path.exists():
        df = pd.read_parquet(path)
    else:
        df = pw.fetch_market_trades(pm.PolymarketClient(), cids, taker_only=True)
        df = df[["ts", "tx_hash", "price", "size", "side", "outcome", "condition_id"]].copy()
        df.to_parquet(path)
    blk = df.assign(block_s=pd.DatetimeIndex(df["ts"]).as_unit("ns").asi8 // 1_000_000_000)
    blk = blk.groupby("tx_hash")["block_s"].min()
    rows = []
    for m in markets:
        for e in m.events:
            if e[2] != "trade":
                continue
            tx = str(e[4][3])
            if tx in blk.index:
                rows.append({"slug": m.slug, "ws_ms": int(e[1]), "bloc_s": int(blk.loc[tx]),
                             "retard_s": int(blk.loc[tx]) - e[1] / 1000.0})
    return pd.DataFrame(rows)


def run_live(args, rt: Runtime, H: dict) -> dict:
    ctx: dict = {}
    now = time.time()
    with rt("4a. marchés du collecteur (carnet reconstruit, issue)"):
        markets, mtab = load_live_markets(now)
    ctx["mtab"] = mtab
    if not markets:
        return ctx
    live_cache = CACHE
    params = {"hl": H["hl"], "lag": H["lag"], "basis_sd": H["basis_sd"], "k": H["k_val"]}
    sig_raw = H["sig_best"] if H["sig_best"] != "tfm" else "ewma"
    sig_k = H["sig_best_k"] if H["sig_best_k"] != "tfm" else "ewma"
    k_live = H["k_val"] if H["sig_best_k"] != "tfm" else float(H["sig_sel"].set_index("sigma").loc["ewma", "k"])
    ctx.update(sig_raw=sig_raw, sig_k=sig_k, k_live=k_live, params=params)
    with rt("4b. Binance 1 s (data-api, cache pm_formula) et bougies 1 m"):
        series = {}
        for a_ in sorted({m.asset for m in markets}):
            sym = SYMBOL_OF[a_]
            S0 = min(m.start_ts for m in markets if m.asset == a_) - 3720
            E1 = max(m.end_ts for m in markets if m.asset == a_) + 5
            df = fb.fetch_binance_1s(sym, S0, E1, live_cache / "binance_1s")
            P = fb.SecondPrices(df["sec"].to_numpy(), df["close"].to_numpy())
            park = fb.parkinson_sigma_series(fb.bars_1m_from_1s(df), n=60)
            ev = np.sqrt(fb.ewma_var_series(P, H["hl"]))
            series[a_] = (P, park, ev)
    margins = H["margins"]
    rows_px, rows_ex = [], []
    with rt("4c. formule, carnet à t + δ, exécutions (10 et 100 parts)"):
        for m in markets:
            P, park, ev = series[m.asset]
            S, E = m.start_ts, m.end_ts
            T = fb.decision_times(np.array([S]), np.array([E]))[0]
            y = float(m.resolved_up)
            sig = {"park": fb.bar_sigma_at(park, T), "ewma": ev[np.clip(T - P.t0, 0, ev.size - 1)]}
            inp0 = fb.formula_inputs(P, T, S, E)
            inpc = fb.formula_inputs(P, T, S, E, lag_s=H["lag"])
            pv = {"f": fb.formula_probs(inp0, S, E, sig[sig_raw])["p"],
                  "corr": fb.formula_probs(inpc, S, E, k_live * sig[sig_k], extra_sd=H["basis_sd"])["p"]}
            times = [int(t * 1000 + d * 1000) for t in T for d in fb.LIVE_DELAYS]
            states = fb.book_states_at(m, times)
            nd = len(fb.LIVE_DELAYS)
            for k, t in enumerate(T):
                st0 = states[k * nd]
                if not st0["fresh"] or not st0["asks"] or not st0["bids"]:
                    continue
                ask_up, bid_up = st0["asks"][0][0], st0["bids"][0][0]
                mid = (ask_up + bid_up) / 2
                rows_px.append({"slug": m.slug, "asset": m.asset, "duree": m.duration, "S": S, "instant": LABELS[k], "t": int(t),
                                "y": y, "p_f": float(pv["f"][k]), "p_corr": float(pv["corr"][k]), "mid": mid,
                                "ecart_bid_ask": ask_up - bid_up, "taille_ask_up": st0["asks"][0][1],
                                "taille_ask_down": st0["bids"][0][1]})
                for v, vname in VARIANTS.items():
                    side, edge = fb.choose_side(np.array([pv[v][k]]), np.array([ask_up]), np.array([1.0 - bid_up]), margins[vname])
                    side, edge = int(side[0]), float(edge[0])
                    if side == 0:
                        continue
                    for j, d in enumerate(fb.LIVE_DELAYS):
                        st = states[k * nd + j]
                        for q in LIVE_QTY:
                            best, sz, avg, got = fb.fill_price(st, side, q)
                            pnl = float(fb.pnl_per_share(np.array([side]), np.array([avg]), np.array([y]))[0]) if got > 0 else math.nan
                            rows_ex.append({"slug": m.slug, "asset": m.asset, "duree": m.duration, "S": S, "instant": LABELS[k],
                                            "variante": vname, "cote": side, "avantage_estime": edge, "delta_s": d, "qte": q,
                                            "meilleur_ask": best, "taille_meilleur_ask": sz, "prix_moyen": avg, "qte_obtenue": got,
                                            "pnl_par_part": pnl, "y": y})
    PX = pd.DataFrame(rows_px)
    EX = pd.DataFrame(rows_ex)
    ctx.update(PX=PX, EX=EX, markets=markets)
    with rt("4d. scores formule contre milieu du carnet ; P&L par δ"):
        live_scores(PX, ctx)
        live_pnl(EX, ctx, args)
    with rt("4e. délai de réaction du carnet (trades agrégés Binance, ms)"):
        ctx["reaction"] = reaction(markets, series, H, sig_k, k_live, live_cache)
    with rt("4f. retard bloc − appariement (data-api contre WebSocket)"):
        try:
            ctx["block_lag"] = block_lag(markets, live_cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("retard des blocs non mesuré : %r", exc)
            ctx["block_lag"] = pd.DataFrame()
    return ctx


def live_scores(PX: pd.DataFrame, ctx: dict) -> None:
    rows = []
    if PX.empty:
        ctx["live_scores"] = pd.DataFrame()
        return
    ph_group = {lab: ("avant S" if lab.startswith("S-") else "S…S+120" if lab.startswith("S") else "fin (E−90…E−10)")
                for lab in LABELS}
    PX["groupe"] = PX["instant"].map(ph_group)
    for g, d in list(PX.groupby("groupe")) + [("tous", PX)]:
        y = d["y"].to_numpy()
        row = {"groupe": g, "n": len(d), "marches": d["slug"].nunique()}
        for c in ("p_f", "p_corr", "mid"):
            sc = prob_scores(y, d[c].to_numpy())
            row[f"brier_{c}"] = sc["brier"]
            row[f"logloss_{c}"] = sc["logloss"]
        rows.append(row)
    ctx["live_scores"] = pd.DataFrame(rows)
    ctx["live_book"] = PX.groupby("instant").agg(n=("slug", "size"), ecart_median=("ecart_bid_ask", "median"),
                                                 taille_ask_up_mediane=("taille_ask_up", "median"),
                                                 taille_ask_down_mediane=("taille_ask_down", "median")).reindex(LABELS).dropna(how="all").reset_index()


def live_pnl(EX: pd.DataFrame, ctx: dict, args) -> None:
    rows = []
    if EX.empty:
        ctx["live_pnl"] = pd.DataFrame()
        return
    for (v, d, q), g in EX.groupby(["variante", "delta_s", "qte"]):
        boot = pb.SlotBootstrap(fb.slot_of(g["S"].to_numpy()), B=args.boot, seed=4)
        pnl = g["pnl_par_part"].to_numpy()
        full = (g["qte_obtenue"] >= q - 1e-9).to_numpy()
        e, lo, hi = boot.mean(pnl, np.isfinite(pnl))
        rows.append({"variante": v, "delta_s": d, "qte": q, "decisions": len(g), "marches": g["slug"].nunique(),
                     "creneaux": int(np.unique(g["S"].to_numpy() // 900).size),
                     "part_remplie": float(np.mean(full)), "taille_meilleur_ask_mediane": float(np.nanmedian(g["taille_meilleur_ask"])),
                     "prix_moyen": float(np.nanmean(g["prix_moyen"])), "pnl_par_part": e, "lo": lo, "hi": hi,
                     "taux_gain": float(np.mean(np.where(g["cote"] == 1, g["y"], 1 - g["y"])))})
    ctx["live_pnl"] = pd.DataFrame(rows)


def reaction(markets: list, series: dict, H: dict, sig_k: str, k_live: float, cache: Path) -> pd.DataFrame:
    """Événements « la formule bouge de plus de 5 points en 1 s » et délai avant que le milieu du
    carnet fasse la moitié du chemin (grille de 20 ms, variante corrigée, σ figé à S−45 s)."""
    out = []
    for m in markets:
        P, park, ev = series[m.asset]
        S, E = m.start_ts, m.end_ts
        agg = fb.fetch_agg_trades(SYMBOL_OF[m.asset], (S - 70) * 1000, E * 1000, cache / "aggtrades")
        if agg.empty:
            continue
        t_sig = S - 45
        sig = float(fb.bar_sigma_at(park, np.array([t_sig]))[0]) if sig_k == "park" else float(ev[t_sig - P.t0])
        grid = np.arange((S - 45) * 1000, (E - 2) * 1000, 20, dtype="int64")
        pf = fb.formula_path_ms(P, grid, agg["T"].to_numpy(), agg["p"].to_numpy(), S, E, k_live * sig,
                                lag_s=H["lag"], extra_sd=H["basis_sd"])
        ts, mid = fb.mid_path(m)
        if ts.size == 0:
            continue
        mg = fb.step_values(ts, mid, grid)
        evs = fb.reaction_delays(grid, pf, mg, jump=0.05, window_ms=1000, quiet_ms=3000, horizon_ms=20_000)
        if evs.empty:
            continue
        evs.insert(0, "slug", m.slug)
        evs.insert(1, "asset", m.asset)
        evs.insert(2, "duree", m.duration)
        evs["t_rel_S_s"] = evs["t_event"] / 1000.0 - S
        evs["phase"] = np.select([evs["t_rel_S_s"] <= 0, evs["t_rel_S_s"] <= E - S - 60], [2, 3], 4)
        out.append(evs)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ---------------------------------------------------------------------------
# main (partie historique ; carnet réel et README plus bas)
# ---------------------------------------------------------------------------
def write_report(H: dict, Lv: dict, rt: Runtime, args) -> None:
    pn = H["pnl"]
    print(H["margins"])
    print(pn[pn["groupe"] == "tous les instants"].to_string())
    for k in ("mtab", "live_scores", "live_pnl", "live_book"):
        if k in Lv:
            print(Lv[k].to_string())
    if "reaction" in Lv and len(Lv["reaction"]):
        r = Lv["reaction"]
        print(len(r), r["censored"].mean(), r["delay_ms"].describe())
    if "block_lag" in Lv and len(Lv["block_lag"]):
        print(Lv["block_lag"]["retard_s"].describe())
    print(pd.DataFrame(rt.rows).to_string())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-live", action="store_true", help="historique seul")
    ap.add_argument("--boot", type=int, default=2000, help="tirages bootstrap (défaut 2000)")
    ap.add_argument("--report-only", action="store_true", help="réécrire le rapport depuis le dernier calcul (cache)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    import pickle

    t0 = time.perf_counter()
    CACHE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    state = CACHE / "last_run.pkl"
    if args.report_only:
        with open(state, "rb") as fh:
            H, Lv, rt = pickle.load(fh)
    else:
        rt = Runtime()
        H = run_history(args, rt)
        H.pop("M", None)
        Lv = {} if args.skip_live else run_live(args, rt, H)
        Lv.pop("markets", None)
        rt.rows.append({"etape": "total calcul", "secondes": round(time.perf_counter() - t0, 1)})
        with open(state, "wb") as fh:
            pickle.dump((H, Lv, rt), fh)
    t1 = time.perf_counter()
    write_report(H, Lv, rt, args)
    print(f"rapport : {OUT / 'README.md'} ({time.perf_counter() - t1:.1f} s ; calcul {rt.rows[-1]['secondes']} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
