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
from tradebot.report import (BG, BLUE, ORANGE, TEXT, TEXT_2, _draw_header, _header,  # noqa: E402
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
            rows.append({**row, "statut": "issue pas encore publiée"})
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
        ts, mid = fb.mid_path(m)
        ok_mid = np.flatnonzero(np.isfinite(mid))
        if ok_mid.size == 0:
            continue
        g0 = max((S - 45) * 1000, int(ts[ok_mid[0]]) + 1000)       # pas d'événement avant le premier carnet
        grid = np.arange(g0, (E - 2) * 1000, 20, dtype="int64")
        if grid.size < 100:
            continue
        pf = fb.formula_path_ms(P, grid, agg["T"].to_numpy(), agg["p"].to_numpy(), S, E, k_live * sig,
                                lag_s=H["lag"], extra_sd=H["basis_sd"])
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
# ---------------------------------------------------------------------------
# 5. Graphiques
# ---------------------------------------------------------------------------
def _fig(W: float, body_h: float, title: str, sub: str, nrows: int = 1, ncols: int = 1, left: float = 0.08, **kw):
    plt = _pyplot()
    t, s_, hh = _header(W, title, sub)
    H = hh + body_h
    fig, axes = plt.subplots(nrows, ncols, figsize=(W, H), facecolor=BG, **kw)
    fig.subplots_adjust(left=left, right=0.97, top=1 - (hh + 0.12) / H, bottom=0.72 / H)
    _draw_header(fig, t, s_)
    return fig, axes


def fig_calibration(H: dict, path: Path) -> str:
    cal = H["calibration"]
    sp = H["scores_phase"]
    sp = sp[(sp["duree"] == "tous") & (sp["periode"] == "2e moitié")].set_index(["variante", "phase"])["pente_calibration"]
    sl_b = [sp.loc[("brute", p)] for p in (2, 3, 4)]
    sl_c = [sp.loc[("corrigée", p)] for p in (2, 3, 4)]
    title = (f"Brute, la formule est trop sûre d'elle (pente de calibration {f_(min(sl_b), 2)} à {f_(max(sl_b), 2)}, 1 = parfait) ; "
             f"avec σ × {f_(H['k_val'], 2)} et le décalage Chainlink de {H['lag']} s, elle suit la diagonale "
             f"(pente {f_(min(sl_c), 2)} à {f_(max(sl_c), 2)})")
    sub = ("Courbe de fiabilité : fréquence réelle de « Up » (issue officielle) par décile de la probabilité annoncée, "
           "2e moitié (14/09–24/09/2026), BTC 5m + 15m, tous les instants de la phase. Barres : ± 2 erreurs-types binomiales. "
           f"Brute = formule telle quelle, σ EWMA 1 s (demi-vie {H['hl']} s) ; corrigée = σ × {f_(H['k_val'], 2)}, Binance décalé de "
           f"{H['lag']} s, bruit de source {f_(H['basis_sd'] * 1e4, 2)} pb (tout choisi sur la 1re moitié).")
    fig, axes = _fig(12.0, 4.6, title, sub, 1, 3, sharey=True, gridspec_kw={"wspace": 0.08})
    names = {2: "S−45 … S (TWAP de départ en cours)", 3: "S+2 … E−60 (K connu)", 4: "E−30, E−10 (moyenne finale en cours)"}
    for ax, ph in zip(axes, (2, 3, 4)):
        _style_axes(ax, ygrid=True, xgrid=True)
        ax.plot([0, 1], [0, 1], color=GREY, lw=1.0, ls=(0, (3, 3)))
        for v, col in (("brute", ORANGE), ("corrigée", BLUE)):
            c = cal[(cal["duree"] == "tous") & (cal["phase"] == ph) & (cal["variante"] == v)]
            ax.errorbar(c["p_mean"], c["up_rate"], yerr=2 * c["se"], color=col, lw=1.8, marker="o", ms=4, capsize=0,
                        elinewidth=1.0, label=v)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_title(names[ph], color=TEXT, fontsize=9.5, loc="left")
        ax.set_xlabel("P(Up) annoncée (moyenne du décile)", color=TEXT_2, fontsize=9)
    axes[0].set_ylabel("fréquence réelle de Up", color=TEXT_2, fontsize=9)
    axes[0].legend(loc="upper left", frameon=False, fontsize=9, labelcolor=TEXT)
    _save(fig, path)
    return title


def fig_brier(H: dict, path: Path) -> str:
    sc = H["scores"]
    sc = sc[sc["duree"] == "tous"].set_index(["variante", "instant"])
    st = H["stack"].set_index(["variante", "instant"])
    x = np.arange(len(LABELS))
    worse = [lab for lab in LABELS if sc.loc[("corrigée", lab), "d_brier_lo"] > 0]
    better = [lab for lab in LABELS if sc.loc[("corrigée", lab), "d_brier_hi"] < 0]
    title = (f"Au même instant, la formule ne prévoit pas mieux que le prix des trades : écart de Brier nul avant S, "
             f"significativement en faveur du marché à {', '.join(PRETTY[l_] for l_ in worse)}"
             + (f", en faveur de la formule à {', '.join(PRETTY[l_] for l_ in better)}" if better else "")
             + " ; l'empilement formule + marché ne gagne presque rien")
    sub = ("Brier(formule) − Brier(marché) × 1 000 (négatif = la formule fait mieux), 2e moitié (14/09–24/09), BTC 5m + 15m ; "
           "marché = dernier trade preneur (jeton Up, ou 1 − prix du Down) horodaté ≤ t. IC 95 % bootstrap groupé par créneau de 15 min. "
           "Empilement : logistique sur logit(formule corrigée) et logit(marché), apprise sur la 1re moitié.")
    fig, ax = _fig(12.0, 4.4, title, sub)
    _style_axes(ax)
    ax.axhline(0, color=TEXT_2, lw=1.0)
    for off, (v, col, lab) in zip((-0.22, 0.0, 0.22), (("brute", ORANGE, "formule brute"), ("corrigée", BLUE, "formule corrigée"),
                                                        ("stack", AQUA, "empilement formule + marché"))):
        if v == "stack":
            e = np.array([st.loc[("corrigée", l_), "d_brier_emp_marche"] for l_ in LABELS])
            lo = np.array([st.loc[("corrigée", l_), "lo"] for l_ in LABELS])
            hi = np.array([st.loc[("corrigée", l_), "hi"] for l_ in LABELS])
        else:
            e = np.array([sc.loc[(v, l_), "d_brier"] for l_ in LABELS])
            lo = np.array([sc.loc[(v, l_), "d_brier_lo"] for l_ in LABELS])
            hi = np.array([sc.loc[(v, l_), "d_brier_hi"] for l_ in LABELS])
        ax.errorbar(x + off, 1e3 * e, yerr=[1e3 * (e - lo), 1e3 * (hi - e)], fmt="o", color=col, ms=4.5, elinewidth=1.4,
                    capsize=0, label=lab)
    ax.set_xticks(x)
    ax.set_xticklabels([PRETTY[l_] for l_ in LABELS], fontsize=8.5)
    ax.set_ylabel("écart de Brier × 1 000", color=TEXT_2, fontsize=9)
    ax.set_xlabel("instant de décision (S = ouverture, E = clôture)", color=TEXT_2, fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=TEXT)
    _save(fig, path)
    return title


def fig_pnl_latency(H: dict, lag_block: float, path: Path) -> str:
    pn = H["pnl"]
    g = pn[(pn["groupe"] == "tous les instants") & (pn["delta_s"] >= 0)]
    c = g[g["variante"] == "corrigée"].set_index("delta_s")
    b = g[g["variante"] == "brute"].set_index("delta_s")
    title = (f"On ne gagne qu'en achetant au prix d'avant la décision : formule corrigée {c_(c.loc[0, 'pnl_par_part'])} par part "
             f"au premier trade horodaté ≥ t (apparié ≈ {f_(lag_block, 1)} s plus tôt), {c_(c.loc[2, 'pnl_par_part'])} "
             f"(IC {c_(c.loc[2, 'lo'])} ; {c_(c.loc[2, 'hi'])}) à δ = 2 s, soit une exécution à t, négatif au-delà")
    sub = ("P&L par part (issue officielle − prix payé − frais 0,07·p·(1 − p)), 2e moitié (14/09–24/09), BTC 5m + 15m, tous instants ; "
           "achat si P − coût(ask estimé à t) > marge (choisie sur la 1re moitié : "
           f"{c_(H['margins']['corrigée'], 0)} corrigée, {c_(H['margins']['brute'], 0)} brute) ; prix = 1er achat preneur du même jeton dont le bloc est "
           f"dans [t + δ, t + δ + 2 s]. Les blocs arrivent ≈ {f_(lag_block, 1)} s après l'appariement : δ = 2 s ≈ latence réelle nulle. "
           "Bande : IC 95 % bootstrap groupé par créneau de 15 min.")
    fig, ax = _fig(11.0, 4.4, title, sub)
    _style_axes(ax)
    ax.axhline(0, color=TEXT_2, lw=1.0)
    for d_, col, lab in ((c, BLUE, "formule corrigée"), (b, ORANGE, "formule brute")):
        xs = d_.index.to_numpy(dtype=float)
        ax.fill_between(xs, 100 * d_["lo"], 100 * d_["hi"], color=col, alpha=0.12, linewidth=0)
        ax.plot(xs, 100 * d_["pnl_par_part"], color=col, lw=2.0, marker="o", ms=4)
        ax.text(xs[-1] + 0.15, 100 * d_["pnl_par_part"].iloc[-1], lab, color=TEXT, fontsize=8.5, va="center")
    ax.axvline(lag_block, color=TEXT_2, lw=0.9, ls=(0, (3, 3)))
    ax.text(lag_block + 0.08, ax.get_ylim()[1] * 0.92, "exécution à t\n(latence réelle 0)", color=TEXT_2, fontsize=8, va="top")
    ax.set_xlim(-0.3, 12.2)
    ax.set_xticks(list(fb.HIST_DELAYS))
    ax.set_xlabel("δ : délai entre la décision t et le bloc du trade exécuté (s)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("P&L par part (cents)", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def fig_pnl_heat(H: dict, path: Path) -> str:
    pn = H["pnl"]
    d = pn[(pn["variante"] == "corrigée") & (pn["groupe"].isin(LABELS)) & (pn["delta_s"] >= 0)]
    val = d.pivot(index="groupe", columns="delta_s", values="pnl_par_part").reindex(LABELS)
    lo = d.pivot(index="groupe", columns="delta_s", values="lo").reindex(LABELS)
    hi = d.pivot(index="groupe", columns="delta_s", values="hi").reindex(LABELS)
    nn = d.pivot(index="groupe", columns="delta_s", values="executees").reindex(LABELS)
    cells = H["cells"].set_index("variante").loc["corrigée"]
    title = (f"Par instant, {int(cells['pos'])} cases sur {int(cells['n'])} ont un P&L à IC positif et {int(cells['neg'])} à IC négatif, "
             f"≈ {f_(0.025 * cells['n'], 0)} attendues de chaque côté par hasard : aucun instant n'est rentable de façon fiable dès δ ≥ 2 s")
    sub = ("P&L par part (cents), formule corrigée, 2e moitié ; gras = IC 95 % qui exclut 0 (sans correction pour tests multiples) ; "
           "case vide = moins de 30 positions exécutées. Colonnes : δ (bloc − décision, s).")
    from matplotlib.colors import LinearSegmentedColormap

    fig, ax = _fig(9.0, 6.4, title, sub, left=0.2)
    ax.set_facecolor(BG)
    for sp_ in ax.spines.values():
        sp_.set_visible(False)
    cmap = LinearSegmentedColormap.from_list("div", [ORANGE, "#fbe3d8", BG, "#d6e6f8", BLUE])
    v = 100 * val.to_numpy(dtype=float)
    v[nn.to_numpy(dtype=float) < 30] = np.nan
    lim = 15.0
    ax.imshow(np.clip(v, -lim, lim), cmap=cmap, vmin=-lim, vmax=lim, aspect="auto")
    for i in range(v.shape[0]):
        for j in range(v.shape[1]):
            if not np.isfinite(v[i, j]):
                continue
            sig = (lo.iloc[i, j] > 0) or (hi.iloc[i, j] < 0)
            ax.text(j, i, fmt_number(v[i, j], 1, signed=True), ha="center", va="center", fontsize=8.5, color=TEXT,
                    fontweight="bold" if sig else "normal")
    ax.set_xticks(range(v.shape[1]))
    ax.set_xticklabels([f"δ = {int(c)} s" for c in val.columns], fontsize=8.5, color=TEXT_2)
    ax.set_yticks(range(v.shape[0]))
    ax.set_yticklabels([f"{PRETTY[l_]} (n ≈ {n_(nn.loc[l_].max())})" for l_ in LABELS], fontsize=8.5, color=TEXT_2)
    ax.tick_params(length=0)
    _save(fig, path)
    return title


def fig_reaction(R: pd.DataFrame, path: Path) -> str:
    ok = R[~R["censored"]]
    d = ok["delay_ms"].to_numpy()
    med = float(np.median(d))
    q1, q3 = np.quantile(d, [0.25, 0.75])
    title = (f"Quand Binance fait bouger la formule de plus de 5 points, le milieu du carnet fait la moitié du chemin en "
             f"{n_(med)} ms (médiane ; quartiles {n_(q1)}–{n_(q3)} ms) ; {fmt_number(R['censored'].mean(), 0, pct=True)} des "
             "mouvements ne sont jamais suivis")
    sub = (f"{n_(len(R))} mouvements (|ΔP| > 0,05 en 1 s, formule corrigée sur les trades agrégés Binance à la ms) dans "
           f"{R['slug'].nunique()} marchés BTC/ETH du 26/09/2026 ; délai = instant où le milieu du carnet a parcouru la moitié de ΔP − "
           "instant où la formule l'a fait (grille de 20 ms ; négatif = le carnet a bougé avant). Non suivi = pas de réaction dans les 20 s. "
           "Trait pointillé : 347 ms (OpenMarket, arXiv 2607.26245).")
    fig, ax = _fig(10.0, 4.2, title, sub)
    _style_axes(ax)
    bins = np.arange(-1000, 3050, 50)
    ax.hist(np.clip(d, -1000, 3000), bins=bins, color=BLUE, edgecolor=BG, linewidth=0.5)
    ax.axvline(med, color=TEXT, lw=1.2)
    ax.text(med - 30, ax.get_ylim()[1] * 0.97, f"médiane {n_(med)} ms", color=TEXT, fontsize=8.5, va="top", ha="right")
    ax.axvline(347, color=TEXT_2, lw=1.0, ls=(0, (3, 3)))
    ax.text(377, ax.get_ylim()[1] * 0.97, "347 ms (OpenMarket)", color=TEXT_2, fontsize=8.5, va="top", ha="left")
    ax.set_xlabel("délai de réaction du carnet (ms ; > 3 000 regroupés à 3 000)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("nombre de mouvements", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def fig_live_pnl(LP: pd.DataFrame, path: Path, n_mk: int) -> str:
    q = LP[LP["qte"] == 10]
    b = q[q["variante"] == "brute"].set_index("delta_s")
    c = q[q["variante"] == "corrigée"].set_index("delta_s")
    title = (f"Sur le carnet réel, même sans latence, acheter quand la formule s'écarte de l'ask ne rapporte rien : "
             f"{c_(b.loc[0, 'pnl_par_part'])} par part (brute, IC {c_(b.loc[0, 'lo'])} ; {c_(b.loc[0, 'hi'])}), "
             f"{c_(c.loc[0, 'pnl_par_part'])} (corrigée) ; n petit, rien n'est démontré")
    sub = (f"P&L par part, 10 parts achetées au meilleur ask réel (carnet reconstruit à la ms) à t + δ, décision à t sur l'ask réel à t "
           f"(marges de l'historique) ; {int(b['decisions'].iloc[0])} décisions brute / {int(c['decisions'].iloc[0])} corrigée sur "
           f"{n_mk} marchés BTC/ETH du 26/09/2026. Bande : IC 95 % bootstrap groupé par créneau de 15 min.")
    fig, ax = _fig(10.0, 4.2, title, sub)
    _style_axes(ax)
    ax.axhline(0, color=TEXT_2, lw=1.0)
    for d_, col, lab in ((c, BLUE, "formule corrigée"), (b, ORANGE, "formule brute")):
        xs = d_.index.to_numpy(dtype=float)
        ax.fill_between(xs, 100 * d_["lo"], 100 * d_["hi"], color=col, alpha=0.12, linewidth=0)
        ax.plot(xs, 100 * d_["pnl_par_part"], color=col, lw=2.0, marker="o", ms=4, label=lab)
    ax.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=TEXT)
    ax.set_xscale("symlog", linthresh=0.1)
    ax.set_xticks(list(fb.LIVE_DELAYS))
    ax.set_xticklabels([fmt_number(x, 0 if x in (0, 1, 2, 5) else 1) for x in fb.LIVE_DELAYS])
    ax.set_xlim(-0.01, 7.5)
    ax.set_xlabel("δ : latence entre la décision et l'exécution au meilleur ask (s)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("P&L par part (cents)", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def fig_vr(H: dict, path: Path) -> str:
    vr = H["vr"]
    v60 = float(vr.set_index("horizon_s").loc[60, "ratio_variance"])
    ac1 = float(H["ac"].set_index("retard_s").loc[1, "autocorrelation"])
    title = (f"Pourquoi la formule brute est trop sûre d'elle : les rendements Binance 1 s sont autocorrélés (+{f_(ac1, 2)} d'une seconde "
             f"à l'autre), donc la variance sur 1 min vaut {f_(v60, 2)} × la somme des variances 1 s (σ × {f_(math.sqrt(v60), 2)})")
    sub = ("Ratio de variance Var(r sur h s) / (h · Var(r sur 1 s)), log-closes Binance BTCUSDT 1 s, 1re moitié (04/09–13/09/2026). "
           f"Une marche aléatoire donnerait 1. Le facteur appris sur la 1re moitié par log-loss vaut {f_(H['k_val'], 2)} pour σ EWMA 1 s.")
    fig, ax = _fig(9.0, 3.8, title, sub)
    _style_axes(ax)
    ax.axhline(1, color=GREY, lw=1.0, ls=(0, (3, 3)))
    ax.plot(vr["horizon_s"], vr["ratio_variance"], color=BLUE, lw=2.0, marker="o", ms=4)
    ax.set_xscale("log")
    ax.set_xticks(vr["horizon_s"])
    ax.set_xticklabels([str(int(h)) for h in vr["horizon_s"]])
    ax.set_ylim(0.9, max(1.8, float(vr["ratio_variance"].max()) * 1.05))
    ax.set_xlabel("horizon h (s)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("ratio de variance", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def _pick(df: pd.DataFrame, **kw) -> pd.Series:
    m = np.ones(len(df), bool)
    for k, v in kw.items():
        m &= (df[k] == v).to_numpy()
    return df[m].iloc[0]


def write_csvs(H: dict, Lv: dict, rt: Runtime) -> list[tuple[str, str]]:
    files = []

    def w(df: pd.DataFrame, name: str, desc: str) -> None:
        if df is None or len(df) == 0:
            return
        df.to_csv(OUT / name, index=False, float_format="%.6g")
        files.append((name, desc))

    w(H["scores"], "scores_par_instant.csv", "formule (brute, corrigée) contre marché par durée × instant, 2e moitié : Brier, log-loss, AUC, écarts et IC, pentes de calibration")
    w(H["scores_sigma"], "scores_par_sigma.csv", "formule selon l'estimateur de σ (bruts, × k, corrigée) par durée × instant, 2e moitié")
    w(H["scores_phase"], "scores_par_phase.csv", "formule par durée × phase, 2e moitié et période entière")
    w(H["calibration"], "calibration.csv", "courbes de fiabilité par déciles (durée × phase × variante), 2e moitié")
    w(H["tails"], "queues.csv", "queues : quand la formule annonce < 2 % ou > 98 %, fréquence réelle de l'improbable")
    w(H["gap_dist"], "ecart_formule_marche.csv", "distribution de l'écart P_formule − p_marché par instant, 2e moitié")
    w(H["stack"], "empilement.csv", "empilement formule + marché (2e moitié) et pente de l'écart sur l'issue")
    w(H["stack_coefs"], "empilement_coefficients.csv", "coefficients de l'empilement appris sur la 1re moitié")
    w(H["pnl"], "pnl_latence.csv", "P&L preneur par variante × groupe d'instants × δ (δ = −1 : ask estimé à t), 2e moitié")
    w(H["margin_tab"], "marge_par_moitie.csv", "P&L par marge × moitié × δ (choix de la marge sur la 1re moitié)")
    w(H["sig_sel"], "choix_sigma.csv", "log-loss 1re moitié de chaque σ, brut et × k")
    w(H["hl_tab"], "choix_demi_vie_ewma.csv", "log-loss 1re moitié selon la demi-vie EWMA")
    w(H["k_tab"], "choix_facteur_sigma.csv", "log-loss 1re moitié selon le facteur k appliqué à σ")
    w(H["align"], "alignement_chainlink.csv", "Binance contre priceToBeat / finalPrice selon le décalage (écart de niveau, erreur sur F − K, accord d'issue)")
    w(pd.concat([H["vr"], H["ac"]], ignore_index=True), "ratio_variance.csv", "ratio de variance et autocorrélations des rendements Binance 1 s (1re moitié)")
    if Lv:
        w(Lv.get("mtab"), "carnet_marches.csv", "marchés du collecteur : statut, issue, couverture")
        w(Lv.get("PX"), "carnet_prix.csv", "carnet réel : formule, milieu, écart, tailles au meilleur niveau par marché × instant")
        w(Lv.get("EX"), "carnet_executions.csv", "carnet réel : décisions et exécutions à t + δ (10 et 100 parts)")
        w(Lv.get("live_scores"), "carnet_scores.csv", "carnet réel : Brier / log-loss formule contre milieu du carnet")
        w(Lv.get("live_pnl"), "carnet_pnl.csv", "carnet réel : P&L par variante × δ × quantité, IC")
        w(Lv.get("live_book"), "carnet_profondeur.csv", "carnet réel : écart et tailles médianes au meilleur ask par instant")
        w(Lv.get("reaction"), "carnet_reaction.csv", "mouvements de la formule > 5 points et délai de réaction du carnet (ms)")
        bl = Lv.get("block_lag")
        if bl is not None and len(bl):
            q = bl["retard_s"].quantile([0.05, 0.25, 0.5, 0.75, 0.95])
            w(pd.DataFrame([{"n": len(bl), "marches": bl["slug"].nunique(), "moyenne_s": bl["retard_s"].mean(),
                             **{f"q{int(100 * k)}_s": v for k, v in q.items()}}]),
              "retard_blocs.csv", "retard horodatage de bloc (data-api) − appariement (WebSocket), par trade apparié")
    w(pd.DataFrame(rt.rows), "runtime.csv", "temps de calcul par étape (s)")
    return files


def write_report(H: dict, Lv: dict, rt: Runtime, args) -> None:
    files = write_csvs(H, Lv, rt)
    figs = {}
    lag_block = float(Lv["block_lag"]["retard_s"].median()) if Lv and len(Lv.get("block_lag", [])) else 2.0
    figs["calibration"] = fig_calibration(H, OUT / "calibration.png")
    figs["brier"] = fig_brier(H, OUT / "brier_vs_marche.png")
    figs["pnl"] = fig_pnl_latency(H, lag_block, OUT / "pnl_latence.png")
    figs["heat"] = fig_pnl_heat(H, OUT / "pnl_par_instant.png")
    figs["vr"] = fig_vr(H, OUT / "ratio_variance.png")
    has_live = bool(Lv) and len(Lv.get("live_pnl", [])) > 0
    if has_live:
        n_mk = int((Lv["mtab"]["statut"] == "exploitable").sum())
        figs["live"] = fig_live_pnl(Lv["live_pnl"], OUT / "pnl_carnet_reel.png", n_mk)
        if len(Lv.get("reaction", [])):
            figs["reaction"] = fig_reaction(Lv["reaction"], OUT / "reaction_carnet.png")
    text = readme(H, Lv, rt, figs, files, lag_block)
    (OUT / "README.md").write_text(text, encoding="utf-8")


def readme(H: dict, Lv: dict, rt: Runtime, figs: dict, files: list, lag_block: float) -> str:
    L_: list[str] = []
    w = L_.append
    sc, sp, st, pn = H["scores"], H["scores_phase"], H["stack"], H["pnl"]
    scT = sc[sc["duree"] == "tous"].set_index(["variante", "instant"])
    spT = sp[(sp["duree"] == "tous") & (sp["periode"] == "2e moitié")].set_index(["variante", "phase"])
    stC = st[st["variante"] == "corrigée"].set_index("instant")
    tails = H["tails"]
    tb = tails[(tails["duree"] == "tous") & (tails["phase"] == 3) & (tails["variante"] == "brute") & (tails["zone"].str.startswith("P <"))].iloc[0]
    tc = tails[(tails["duree"] == "tous") & (tails["phase"] == 3) & (tails["variante"] == "corrigée") & (tails["zone"].str.startswith("P <"))].iloc[0]
    sl_b = [spT.loc[("brute", p), "pente_calibration"] for p in (2, 3, 4)]
    sl_c = [spT.loc[("corrigée", p), "pente_calibration"] for p in (2, 3, 4)]
    v60 = float(H["vr"].set_index("horizon_s").loc[60, "ratio_variance"])
    ac1 = float(H["ac"].set_index("retard_s").loc[1, "autocorrelation"])
    dB = scT.xs("corrigée")["d_brier"]
    worse = [l_ for l_ in LABELS if scT.loc[("corrigée", l_), "d_brier_lo"] > 0]
    better = [l_ for l_ in LABELS if scT.loc[("corrigée", l_), "d_brier_hi"] < 0]
    pnlC = pn[(pn["variante"] == "corrigée") & (pn["groupe"] == "tous les instants")].set_index("delta_s")
    pnlB = pn[(pn["variante"] == "brute") & (pn["groupe"] == "tous les instants")].set_index("delta_s")
    ex = H["example"]
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    tot = rt.rows[-1]["secondes"] if rt.rows else math.nan
    n_live = int((Lv["mtab"]["statut"] == "exploitable").sum()) if Lv and "mtab" in Lv else 0
    near_open = ["S-10", "S-5", "S-2", "S", "S+2"]
    slopes_open = [stC.loc[l_, "pente_ecart"] for l_ in near_open]
    sig_open = sum(stC.loc[l_, "pente_lo"] > 0 for l_ in near_open)
    stack_sig = [l_ for l_ in LABELS if stC.loc[l_, "hi"] < 0]
    stack_bad = [l_ for l_ in LABELS if stC.loc[l_, "lo"] > 0]
    mt = H["margin_tab"]
    tr2 = mt[(mt["moitie"] == "1re moitié") & (mt["delta_s"] == 2)]
    all_neg2 = bool((tr2["pnl_par_part"] < 0).all())

    w("# La formule exacte P(Up) contre le marché — Polymarket « Up or Down » BTC 5m / 15m")
    w("")
    w(f"*Généré le {now} par `scripts/polymarket_formula_backtest.py` (temps de calcul : {fmt_number(tot, 0)} s, détail § 8). "
      f"Historique : {n_(H['n_markets'])} marchés BTC 5m et 15m du 04/09 au 24/09/2026 (tous). "
      f"Carnet réel : {n_live} marchés BTC 5m, BTC 15m et ETH 5m enregistrés à la milliseconde le 26/09/2026.*")
    w("")
    w("> Simulation papier sur données publiques : aucun ordre, aucune clé. La formule est celle de "
      "`src/tradebot/polymarket_formula.py` (vérifiée par Monte-Carlo dans `tests/test_polymarket_formula.py`).")
    w("")
    w("## 0. Réponse courte")
    w("")
    w(f"* **La formule est juste, à condition de mesurer σ à la bonne échelle.** Telle quelle (σ = EWMA des rendements Binance 1 s, "
      f"demi-vie {H['hl']} s, le meilleur des trois σ sur la 1re moitié), elle classe bien mais elle est **trop sûre d'elle** : pente de "
      f"calibration {f_(min(sl_b), 2)} à {f_(max(sl_b), 2)} selon la phase (1 = parfait). Quand elle annonce moins de 2 % (K connu), "
      f"l'improbable arrive {fmt_number(tb['observe'], 1, pct=True)} du temps au lieu de {fmt_number(tb['attendu'], 1, pct=True)}. "
      f"La cause est mesurée : les rendements Binance 1 s sont autocorrélés (+{f_(ac1, 2)} d'une seconde à l'autre) et la variance sur "
      f"une minute vaut {f_(v60, 2)} × la somme des variances 1 s. Avec σ × {f_(H['k_val'], 2)}, Binance décalé de {H['lag']} s "
      f"(Chainlink est en retard) et un bruit de source de {f_(H['basis_sd'] * 1e4, 2)} pb, tous trois fixés sur la 1re moitié, la pente "
      f"passe à {f_(min(sl_c), 2)}–{f_(max(sl_c), 2)} sur la 2e moitié : la formule « corrigée » est calibrée, à ± 0,2 près.")
    w(f"* **Elle ne bat pas le marché.** Au même instant, le prix du dernier trade preneur prévoit aussi bien ou mieux. L'écart de Brier "
      f"(formule corrigée − marché) va de {f_(1e3 * dB.min(), 2, True)} à {f_(1e3 * dB.max(), 2, True)} × 10⁻³. L'IC est en faveur du "
      f"marché à {', '.join(PRETTY[l_] for l_ in worse) if worse else 'aucun instant'} ({len(worse)} instants sur 17), et "
      + (f"en faveur de la formule à {', '.join(PRETTY[l_] for l_ in better)}." if better else "jamais en faveur de la formule.")
      + f" Les AUC sont identiques (à S : formule {f_(scT.loc[('corrigée', 'S'), 'auc_formule'], 3)}, marché "
      f"{f_(scT.loc[('corrigée', 'S'), 'auc_marche'], 3)}).")
    w(f"* **Son désaccord avec le marché contient un peu d'information, autour de l'ouverture seulement.** La pente de l'issue sur l'écart "
      f"formule − marché vaut {f_(min(slopes_open), 2)} à {f_(max(slopes_open), 2)} de S−10 à S+2 (IC qui exclut 0 à {sig_open} de ces "
      f"5 instants ; 1 voudrait dire « la formule a raison, le marché tort »). Mais l'empilement formule + marché appris sur la 1re moitié "
      f"n'améliore le Brier du marché de façon significative qu'à {', '.join(PRETTY[l_] for l_ in stack_sig) if stack_sig else 'aucun instant'}"
      + (f" ({f_(1e3 * stC.loc[stack_sig[0], 'd_brier_emp_marche'], 2, True)} × 10⁻³, sur un Brier de {f_(stC.loc[stack_sig[0], 'brier_marche'], 3)})" if stack_sig else "")
      + f", et le dégrade à {', '.join(PRETTY[l_] for l_ in stack_bad) if stack_bad else 'aucun instant'} : l'information en plus est minuscule.")
    w(f"* **Pour gagner, il faut acheter au prix d'avant la décision.** Règle : acheter si P − coût(ask estimé) > marge (marge fixée sur "
      f"la 1re moitié : {c_(H['margins']['corrigée'], 0)} pour la formule corrigée). P&L par part, frais inclus, 2e moitié : "
      f"{ci_c(pnlC.loc[0, 'pnl_par_part'], pnlC.loc[0, 'lo'], pnlC.loc[0, 'hi'])} au prix du premier trade dont le bloc est ≥ t, "
      f"{ci_c(pnlC.loc[1, 'pnl_par_part'], pnlC.loc[1, 'lo'], pnlC.loc[1, 'hi'])} à δ = 1 s, "
      f"{ci_c(pnlC.loc[2, 'pnl_par_part'], pnlC.loc[2, 'lo'], pnlC.loc[2, 'hi'])} à δ = 2 s, puis négatif "
      f"({c_(pnlC.loc[10, 'pnl_par_part'])} à 10 s). La formule brute fait {c_(pnlB.loc[0, 'pnl_par_part'])} à δ = 0 et "
      f"{c_(pnlB.loc[2, 'pnl_par_part'])} à δ = 2 s.")
    if Lv and len(Lv.get("block_lag", [])):
        bl = Lv["block_lag"]["retard_s"]
        w(f"* **Or un bloc arrive {f_(bl.median(), 1)} s après l'appariement** (médiane mesurée sur {n_(len(bl))} trades vus à la fois "
          f"dans le WebSocket et dans data-api ; quartiles {f_(bl.quantile(0.25), 1)}–{f_(bl.quantile(0.75), 1)} s). « δ = 0 » est donc un "
          f"prix apparié ≈ {f_(bl.median(), 1)} s **avant** la décision, inaccessible ; δ = 2 s correspond à une exécution à t. "
          "À latence réelle nulle, il ne reste rien de significatif.")
    if Lv and len(Lv.get("reaction", [])):
        R = Lv["reaction"]
        ok = R[~R["censored"]]["delay_ms"]
        w(f"* **Le carnet réel suit Binance en ≈ {n_(ok.median())} ms** (médiane ; quartiles {n_(ok.quantile(0.25))}–"
          f"{n_(ok.quantile(0.75))} ms ; {n_(len(R))} mouvements de la formule de plus de 5 points en 1 s). "
          f"{fmt_number(R['censored'].mean(), 0, pct=True)} des mouvements ne sont jamais suivis : le marché n'y croit pas.")
    if Lv and len(Lv.get("live_pnl", [])):
        LP = Lv["live_pnl"]
        b0 = _pick(LP, variante="brute", delta_s=0.0, qte=10)
        c0 = _pick(LP, variante="corrigée", delta_s=0.0, qte=10)
        b100 = _pick(LP, variante="brute", delta_s=0.0, qte=100)
        w(f"* **Sur le carnet réel, même sans latence, rien n'est gagné** (n petit : {n_live} marchés, {int(b0['creneaux'])} créneaux de "
          f"15 min ; les IC contiennent 0). En achetant au meilleur ask affiché à t : formule brute {ci_c(b0['pnl_par_part'], b0['lo'], b0['hi'])} "
          f"par part ({int(b0['decisions'])} décisions), corrigée {ci_c(c0['pnl_par_part'], c0['lo'], c0['hi'])} "
          f"({int(c0['decisions'])} décisions) ; aucune latence de 0 à 5 s ne donne un P&L positif. La profondeur n'est pas la contrainte : "
          f"{n_(b0['taille_meilleur_ask_mediane'])} parts au meilleur ask (médiane), et 100 parts coûtent "
          f"{f_(100 * (b100['prix_moyen'] - b0['prix_moyen']), 1)} c de plus que 10.")
    w(f"* **Verdict : la formule est exacte, mais le marché la connaît et l'applique en un quart de seconde.** Le gain historique "
      f"({c_(pnlC.loc[1, 'pnl_par_part'], 0)} à {c_(pnlC.loc[0, 'pnl_par_part'], 0)} par part, sur {n_(pnlC.loc[0, 'executees'])} achats "
      "en 11 jours) n'existe que contre des prix vieux d'une à deux secondes ; or le carnet se remet à jour ≈ 250 ms après Binance, et ces "
      "prix ont disparu quand un preneur arrive. Pour gagner, il faudrait voir Binance et frapper le carnet en moins de ≈ 250 ms (vitesse "
      "d'un teneur de marché), et l'avantage serait alors inférieur à cette borne de quelques cents. Avec une latence d'une seconde ou plus "
      f"(un robot qui lit Binance à la seconde), il ne reste rien après frais : {ci_c(pnlC.loc[2, 'pnl_par_part'], pnlC.loc[2, 'lo'], pnlC.loc[2, 'hi'])} "
      "par part à exécution immédiate, négatif ensuite.")
    w("")

    # 1. la formule en clair
    w("## 1. La formule en clair")
    w("")
    w("Règle officielle (TWAP-60, vérifiée sur 8 063 marchés) : on note p le log-prix Chainlink publié chaque seconde, S l'ouverture, "
      "E la clôture, D = E − S (300 s ou 900 s).")
    w("")
    w("```text")
    w("K = moyenne de p sur (S − 60, S]      (le « prix à battre », 60 points)")
    w("F = moyenne de p sur (E − 60, E]      (le prix final)")
    w("« Up »  si et seulement si  F ≥ K")
    w("")
    w("Si p suit une marche aléatoire de volatilité σ (par √seconde) :")
    w("")
    w("    P(Up | ce qu'on sait à t) = Φ( m(t) / s(t) )        Φ = loi normale")
    w("")
    w("Phase 1   t ≤ S − 60        m = 0                                   → P = 0,5")
    w("Phase 2   S − 60 < t ≤ S    m = (a / 60) · (p_t − Ā)                a = t − (S − 60), Ā = moyenne réalisée sur (S − 60, t]")
    w("                            s² = σ² · (D − 40 + r − r²/60 + r³/10 800),   r = S − t")
    w("Phase 3   S < t ≤ E − 60    m = p_t − K                             s² = σ² · (E − t − 40)")
    w("Phase 4   E − 60 < t ≤ E    m = B + (τ / 60) · p_t − K              B = (somme réalisée sur (E − 60, t]) / 60, τ = E − t")
    w("                            s² = σ² · τ³ / 10 800")
    w("```")
    w("")
    w("Intuition : avant S, le prix à battre n'est pas encore fixé ; si le prix actuel est au-dessus de la moyenne en cours, K finira sous "
      "le prix actuel, d'où un avantage à « Up » (le « TWAP partiel »). Après S, c'est l'écart au prix à battre, rapporté à la volatilité "
      "qui reste. Le code utilise la version exacte à la seconde (sommes discrètes), qui coïncide avec ces formules à < 1 % près. "
      "Décision preneur : acheter « Up » à l'ask a coûte a + 0,07 · a · (1 − a) ; l'espérance par part est P − a − 0,07 · a · (1 − a) "
      "(symétrique pour « Down »).")
    w("")
    diff = ex["diff_bp"]
    w(f"**Exemple chiffré à S−10 s** (`{ex['slug']}`, ouverture {pd.Timestamp(ex['S'], unit='s', tz='UTC'):%d/%m/%Y %H:%M} UTC, 2e moitié) :")
    w("")
    w(f"* Binance à t = S−10 s : {fmt_number(ex['price'], 2)} $ ; moyenne des 50 closes 1 s de (S−60, S−10] : "
      f"{fmt_number(ex['avg_price'], 2)} $ ; p_t − Ā = {f_(diff, 2, True)} pb ; a = 50 s ⇒ m = 50/60 × ({f_(diff, 2, True)}) = "
      f"{f_(ex['m'] * 1e4, 2, True)} pb.")
    w(f"* σ ({SIG_NAMES[ex['sigma_name']]}) = {f_(ex['sigma'] * 1e4, 3)} pb/√s ; facteur de variance exact (r = 10 s, D = 300 s) = "
      f"{f_(ex['vf'], 1)} (formule continue : 300 − 40 + 10 − 100/60 + 1 000/10 800 = {f_(300 - 40 + 10 - 100 / 60 + 1000 / 10800, 1)}) ; "
      f"s = {f_(ex['sigma'] * 1e4, 3)} × √{f_(ex['vf'], 1)} = {f_(ex['sd'] * 1e4, 2)} pb.")
    w(f"* m / s = {f_(ex['m'] / ex['sd'], 3, True)} ⇒ **P(Up) = Φ({f_(ex['m'] / ex['sd'], 3, True)}) = {f_(ex['p'], 3)}** "
      f"(formule brute). Corrigée (σ × {f_(H['k_val'], 2)} = {f_(ex['sigma_c'] * 1e4, 3)} pb/√s, Binance décalé de {H['lag']} s, "
      f"bruit de source) : m = {f_(ex['m_c'] * 1e4, 2, True)} pb, s = {f_(ex['sd_c'] * 1e4, 2)} pb, P(Up) = {f_(ex['p_c'], 3)}.")
    au, ad = fb.estimated_asks(np.array([ex["p_mkt"]]))
    cost_dn = float(ad[0] + fb.taker_fee(ad[0]))
    w(f"* Marché au même instant : dernier trade = {f_(ex['p_mkt'], 3)} (pour Up). Acheter « Down » coûterait ≈ {f_(ad[0], 3)} + frais "
      f"{f_(float(fb.taker_fee(ad[0])), 4)} = {f_(cost_dn, 3)} ; espérance selon la formule corrigée : {f_(1 - ex['p_c'], 3)} − "
      f"{f_(cost_dn, 3)} = {c_(1 - ex['p_c'] - cost_dn)} par part, sous la marge de {c_(H['margins']['corrigée'], 0)} : pas d'achat. "
      f"Issue officielle : **{'Up' if ex['y'] > 0.5 else 'Down'}**.")
    w("")

    # 2. données
    w("## 2. Données")
    w("")
    bc = H["by_cell"].set_index("duration")
    ts_ = H["trade_sources"]
    w(f"* **Marchés** : {n_(H['n_markets'])} marchés BTC du 04/09 au 24/09/2026 ({n_(bc.loc['5m', 'n'])} en 5m, taux de Up "
      f"{fmt_number(bc.loc['5m', 'up'], 1, pct=True)} ; {n_(bc.loc['15m', 'n'])} en 15m, {fmt_number(bc.loc['15m', 'up'], 1, pct=True)}), "
      "issue officielle (`PolymarketClient`, cache), `priceToBeat`/`finalPrice` (caches `event_meta` et `markets.csv`). Choix des "
      "paramètres : 04/09–13/09 (1re moitié) ; évaluation : 14/09–24/09 (2e moitié).")
    w("* **Binance** : closes 1 s BTCUSDT (cache `pm_maker/binance_1s`, 03/09–24/09) ; bougies 1 m (cache `BTCUSDT_1m`) pour Parkinson ; "
      "σ TimesFM par marché (`reports/timesfm_amplitude/sigma_par_marche.csv`, prévu à S−120 s donc connu à tous les instants).")
    w(f"* **Trades preneurs** : {n_(H['n_trades'])} trades de {n_(H['n_with_trades'])} marchés, horodatés au bloc Polygon (data-api, "
      f"cache `wallets/trades`) : {n_(ts_['taker'])} marchés depuis les fichiers `*_taker`, {n_(ts_['all'])} (une partie du 23/09 et le "
      "24/09, absents de ces fichiers) depuis les jambes `role = taker` des fichiers `*_all`.")
    if Lv and "mtab" in Lv:
        mtab = Lv["mtab"]
        stc = mtab["statut"].value_counts()
        w(f"* **Carnet réel** : fichiers du collecteur (`data/cache/polymarket/live/`, carnet reconstruit par `tradebot.polymarket_book`) : "
          f"{len(mtab)} marchés enregistrés, **{n_live} exploitables** ; écartés : "
          + ", ".join(f"{k} ({v})" for k, v in stc.items() if k != "exploitable")
          + ". Binance 1 s (BTC, ETH) et trades agrégés (ms) via `data-api.binance.vision`, cache `data/cache/pm_formula/`.")
    w("")

    # 3. méthode
    al = H["align"]
    a1 = al[al["moitie"] == "1re moitié"].set_index("decalage_s")
    a2 = al[al["moitie"] == "2e moitié"].set_index("decalage_s")
    w("## 3. Méthode")
    w("")
    w("**Alignement (aucune donnée après t).** La bougie Binance 1 s ouverte à u − 1 clôt à u : son close est « le prix à u », "
      "connu à u. À l'instant t, la formule n'utilise que les prix aux instants ≤ t : p_t = close de la bougie ouverte à t − 1 ; "
      "Ā = moyenne sur (S − 60, t] ; K = moyenne sur (S − 60, S] ; B = somme sur (E − 60, t] / 60. Les σ sont calculés avant t : "
      "Parkinson sur les 60 dernières bougies 1 m **closes** à t ; EWMA des rendements 1 s jusqu'à t ; TimesFM prévu à S − 120 s. "
      "Des tests unitaires modifient toutes les données postérieures à t et vérifient que rien ne change "
      "(`tests/test_polymarket_formula_backtest.py`).")
    w("")
    w(f"**K vient de Binance, pas du `priceToBeat`.** Vérification sur les {n_(H['n_markets'])} marchés : la moyenne Binance sur "
      f"(S − 60, S] dépasse le `priceToBeat` Chainlink de {f_(a1.loc[0, 'ecart_niveau_moyen_pb'], 1)} pb en moyenne sur la 1re moitié "
      f"et de {f_(a2.loc[0, 'ecart_niveau_moyen_pb'], 1)} pb sur la 2e (écart-type {f_(a1.loc[0, 'ecart_niveau_sd_pb'], 1)} à "
      f"{f_(a2.loc[0, 'ecart_niveau_sd_pb'], 1)} pb) : l'écart de niveau n'est pas constant d'un jour à l'autre. En revanche il "
      f"s'annule dans F − K : l'erreur quadratique sur F − K n'est que de {f_(a1.loc[0, 'rmse_F_moins_K_pb'], 2)} pb. Comparer le spot "
      f"Binance au `priceToBeat` introduirait un biais de 2 à 4 pb, énorme en fin de fenêtre (s ≈ 1 pb à E−10 s). Décaler Binance de "
      f"{H['lag']} s vers le passé réduit cette erreur à {f_(a1.loc[H['lag'], 'rmse_F_moins_K_pb'], 2)} pb (1re moitié ; "
      f"{f_(a2.loc[H['lag'], 'rmse_F_moins_K_pb'], 2)} pb sur la 2e) : Chainlink est en retard d'environ {H['lag']} s sur Binance. "
      f"Même avec le bon K, l'issue calculée sur Binance ne coïncide avec l'issue officielle que dans "
      f"{fmt_number(a2.loc[H['lag'], 'accord_issue'], 1, pct=True)} des cas (2e moitié) : c'est un plancher d'erreur en fin de fenêtre.")
    w("")
    w("**Trois σ, tous connus avant t** : (i) Parkinson « hauts/bas » sur les bougies 1 m des 60 dernières minutes ; (ii) EWMA des carrés "
      f"des rendements 1 s, demi-vie choisie sur la 1re moitié parmi {', '.join(str(h) for h in HL_GRID)} s (log-loss) : **{H['hl']} s** ; "
      "(iii) TimesFM : σ prévu à l'horizon de h = D/60 + 1 bougies 1 m, divisé par √(60 h) pour l'avoir par √s. L'estimateur principal est choisi sur la 1re moitié par "
      f"log-loss : **{SIG_NAMES[H['sig_best']]}**.")
    w("")
    ss = H["sig_sel"].copy()
    ss["sigma"] = ss["sigma"].map(SIG_NAMES)
    w(to_markdown(ss.rename(columns={"sigma": "σ", "logloss_1re_moitie": "log-loss brut", "brier_1re_moitie": "Brier brut",
                                     "k": "facteur k choisi", "logloss_1re_moitie_k": "log-loss avec σ × k", "n": "n (marché × instant)"}),
                  {"log-loss brut": 4, "Brier brut": 4, "facteur k choisi": 2, "log-loss avec σ × k": 4}))
    w("")
    w(f"**Deux variantes.** *Brute* : la formule telle quelle avec {SIG_NAMES[H['sig_best']]} (ce que demande l'énoncé). *Corrigée* : "
      f"σ × k (k choisi sur la 1re moitié par log-loss, grille 0,80–2,20 ; retenu : {SIG_NAMES[H['sig_best_k']]} × {f_(H['k_val'], 2)}), "
      f"Binance décalé de {H['lag']} s (décalage qui minimise l'erreur sur F − K contre `priceToBeat`/`finalPrice` sur la 1re moitié ; "
      f"la formule est alors évaluée à τ = t + {H['lag']} s avec les données Binance ≤ t) et un bruit de source de "
      f"{f_(H['basis_sd'] * 1e4, 2)} pb ajouté à s (l'erreur résiduelle sur F − K, 1re moitié). Aucun de ces trois paramètres n'utilise "
      "les issues de la 2e moitié.")
    w("")
    w("**Prix du marché au même instant** : dernier trade preneur de bloc ≤ t, ramené au jeton Up (un trade sur Down au prix p compte "
      f"1 − p), ignoré s'il a plus de {MAX_AGE_S} s ; variante : VWAP des trades de bloc dans [t − 3 s, t] (dans `scores_par_instant.csv`).")
    w("")
    w(f"**P&L preneur** : à t, ask estimé = prix de référence (VWAP 3 s, sinon dernier trade) + 0,005 pour Up et 1 − référence + 0,005 "
      f"pour Down ; on achète le côté dont l'espérance P − (ask + 0,07·ask·(1 − ask)) dépasse la marge. Prix payé : **premier achat preneur "
      f"du même jeton dont le bloc est dans [t + δ, t + δ + 2 s]**, δ ∈ {{{', '.join(str(int(d)) for d in fb.HIST_DELAYS)}}} s ; sans "
      "trade dans la fenêtre, pas d'exécution (comptée à part). Frais 0,07·p·(1 − p), gain 1 si le côté acheté gagne (issue officielle). "
      f"Marge choisie sur la 1re moitié dans {{{', '.join(fmt_number(100 * m, 1) for m in MARGIN_GRID)}}} c : P&L total maximal à δ = 0 s "
      f"(cas le plus favorable), au moins 200 achats exécutés ⇒ **{c_(H['margins']['corrigée'], 0)}** (corrigée), "
      f"**{c_(H['margins']['brute'], 0)}** (brute). "
      + ("À δ = 2 s, **aucune** marge de la grille n'est rentable sur la 1re moitié (voir `marge_par_moitie.csv`) : la règle stricte "
         "serait de ne jamais acheter." if all_neg2 else ""))
    w("")
    w("**Biais des horodatages de bloc** : un trade est horodaté au bloc Polygon qui l'inclut, "
      + (f"en moyenne {f_(Lv['block_lag']['retard_s'].mean(), 2)} s (médiane {f_(lag_block, 2)} s) " if Lv and len(Lv.get('block_lag', [])) else "≈ 2 s ")
      + "après l'appariement dans le carnet (mesuré ici en appariant par hash de transaction les trades du WebSocket, à la ms, et ceux de "
      "data-api). Le « premier trade de bloc ≥ t + δ » a donc été apparié vers t + δ − 2,2 s : **le P&L historique à un δ donné est "
      "optimiste** (le prix reflète un état du carnet plus ancien, d'avant que le marché intègre le mouvement de Binance). Lire δ − 2,2 s "
      "comme la latence réelle. Le même retard vieillit le prix du marché « à t » (il a ≈ 2 s de plus que son horodatage) : la comparaison "
      "du § 4b est donc biaisée **en faveur de la formule**, qui ne bat pourtant pas le marché.")
    w("")
    w("**Incertitude** : IC à 95 % par bootstrap groupé par créneau de 15 min (les 5m et 15m d'un même créneau sont tirés ensemble ; "
      f"{args_boot(rt)} tirages). **Tests multiples** : 17 instants × 2 durées × 2 variantes pour les scores, 17 instants × 6 δ pour le "
      "P&L ; aucun IC n'est corrigé : à 95 %, environ 1 case sur 40 sort « significative » de chaque côté par hasard. Les conclusions "
      "reposent sur les agrégats et les motifs réguliers, pas sur une case isolée.")
    w("")

    # 4a calibration
    w("## 4. Historique (BTC 5m + 15m, 2e moitié : 14/09–24/09/2026)")
    w("")
    w("### 4a. La formule est-elle juste ? (calibration contre l'issue officielle)")
    w("")
    w(f"![{figs['calibration']}](calibration.png)")
    w("")
    w(f"![{figs['vr']}](ratio_variance.png)")
    w("")
    ph = sp[(sp["periode"] == "2e moitié")].copy()
    ph["phase"] = ph["phase"].map({2: "2 (S−45…S)", 3: "3 (S+2…E−60)", 4: "4 (E−30, E−10)"})
    w("Par phase et par durée (2e moitié ; pente de calibration : logistique de l'issue sur logit(P), 1 = parfaitement calibrée, "
      "< 1 = trop sûre d'elle) :")
    w("")
    w(to_markdown(ph[["duree", "phase", "variante", "n", "brier", "logloss", "auc", "accuracy", "pente_calibration"]].rename(
        columns={"duree": "durée", "accuracy": "justesse", "pente_calibration": "pente de calibration", "logloss": "log-loss"}),
        {"brier": 4, "log-loss": 4, "auc": 3, "justesse": "1%", "pente de calibration": 2}))
    w("")
    tl = tails[(tails["duree"] == "tous")].copy()
    tl["phase"] = tl["phase"].map({2: "2", 3: "3", 4: "4"})
    tl = tl[tl["n"] > 0]
    w("Queues (2e moitié, 5m + 15m) : quand la formule annonce moins de 2 % (ou plus de 98 %), fréquence réelle de l'improbable :")
    w("")
    tl["zone"] = tl["zone"].str.replace("0.02", "0,02").str.replace("0.98", "0,98")
    w(to_markdown(tl.drop(columns="duree").rename(columns={"attendu": "attendu (moyenne de P)", "observe": "observé", "surprises": "surprises",
                                                           "n": "annonces"}),
                  {"attendu (moyenne de P)": "2%", "observé": "2%"}))
    w("")
    w(f"Lecture : la formule brute sous-estime la variance (queues trop fines : en phase 3, {n_(tb['surprises'])} surprises sur "
      f"{n_(tb['n'])} annonces < 2 % contre {f_(tb['n'] * tb['attendu'], 0)} attendues). La correction de σ ramène la phase 3 à "
      f"{fmt_number(tc['observe'], 1, pct=True)} observés pour {fmt_number(tc['attendu'], 1, pct=True)} attendus, et le bruit de "
      "source supprime les excès de confiance de fin de fenêtre (phase 4). Le Brier par instant et par σ est dans `scores_par_sigma.csv` "
      "(TimesFM, déjà à l'échelle de la minute, est le meilleur σ brut de S−20 à S+120 ; une fois multipliés par leur k, les trois σ se valent).")
    w("")

    # 4b vs marché
    w("### 4b. Bat-elle le marché au même instant ?")
    w("")
    w(f"![{figs['brier']}](brier_vs_marche.png)")
    w("")
    tab = sc[(sc["duree"] == "tous") & (sc["variante"] == "corrigée")].copy()
    tab = tab.merge(st[st["variante"] == "corrigée"][["instant", "d_brier_emp_marche", "lo", "hi", "pente_ecart", "pente_lo", "pente_hi"]],
                    on="instant")
    tab["instant"] = tab["instant"].map(PRETTY)
    tab["ΔBrier ×10³ [IC]"] = [f"{f_(1e3 * a, 2, True)} [{f_(1e3 * b, 2, True)} ; {f_(1e3 * c, 2, True)}]"
                               for a, b, c in zip(tab["d_brier"], tab["d_brier_lo"], tab["d_brier_hi"])]
    tab["empilement − marché ×10³ [IC]"] = [f"{f_(1e3 * a, 2, True)} [{f_(1e3 * b, 2, True)} ; {f_(1e3 * c, 2, True)}]"
                                            for a, b, c in zip(tab["d_brier_emp_marche"], tab["lo"], tab["hi"])]
    tab["pente de l'écart [IC]"] = [f"{f_(a, 2)} [{f_(b, 2)} ; {f_(c, 2)}]" for a, b, c in zip(tab["pente_ecart"], tab["pente_lo"], tab["pente_hi"])]
    w("Formule corrigée contre dernier trade preneur, 5m + 15m. « Pente de l'écart » : régression sans constante de (issue − p_marché) sur "
      "(P_formule − p_marché) ; 0 = l'écart n'apprend rien au-delà du marché, 1 = la formule a entièrement raison quand ils divergent.")
    w("")
    w(to_markdown(tab[["instant", "n", "brier_formule", "brier_marche", "ΔBrier ×10³ [IC]", "auc_formule", "auc_marche",
                       "pente_calibration_formule", "pente_calibration_marche", "empilement − marché ×10³ [IC]", "pente de l'écart [IC]"]].rename(
        columns={"brier_formule": "Brier formule", "brier_marche": "Brier marché", "auc_formule": "AUC formule", "auc_marche": "AUC marché",
                 "pente_calibration_formule": "pente calib. formule", "pente_calibration_marche": "pente calib. marché"}),
        {"Brier formule": 4, "Brier marché": 4, "AUC formule": 3, "AUC marché": 3, "pente calib. formule": 2, "pente calib. marché": 2}))
    w("")
    gd = H["gap_dist"]
    gd = gd[gd["variante"] == "corrigée"].copy()
    gd["instant"] = gd["instant"].map(PRETTY)
    w("Distribution de l'écart P_formule (corrigée) − p_marché (2e moitié) :")
    w("")
    w(to_markdown(gd[["instant", "n", "moyenne", "ecart_type", "p05", "mediane", "p95", "part_abs_sup_5pts", "part_abs_sup_10pts"]].rename(
        columns={"ecart_type": "écart-type", "mediane": "médiane", "part_abs_sup_5pts": "|écart| > 5 pts", "part_abs_sup_10pts": "|écart| > 10 pts"}),
        {"moyenne": "+3", "écart-type": 3, "p05": "+3", "médiane": "+3", "p95": "+3", "|écart| > 5 pts": "1%", "|écart| > 10 pts": "1%"}))
    w("")
    w("Lecture : avant S, formule et marché sont presque toujours à moins de 5 points l'un de l'autre ; l'écart s'élargit ensuite "
      "(plus d'information, prix plus extrêmes). Le marché est aussi bien calibré (pentes ≈ 1) et fait mieux en fin de fenêtre, où il "
      "connaît Chainlink et où l'issue Binance diffère parfois de l'issue officielle.")
    w("")

    # 4c P&L
    w("### 4c. Peut-on gagner, et à quelle vitesse faut-il agir ?")
    w("")
    w(f"![{figs['pnl']}](pnl_latence.png)")
    w("")
    rows = []
    for v in ("corrigée", "brute"):
        for g in ("tous les instants", "avant S (S−45…S−2)", "ouverture (S…S+10)", "milieu (S+30…E−60)", "fin (E−30, E−10)"):
            d = pn[(pn["variante"] == v) & (pn["groupe"] == g)].set_index("delta_s")
            row = {"variante": v, "instants": g, "achats": int(d.loc[0, "decisions"])}
            row["ask estimé à t"] = c_(d.loc[-1, "pnl_par_part"])
            for dd in fb.HIST_DELAYS:
                r_ = d.loc[dd]
                sig = "**" if (r_["lo"] > 0 or r_["hi"] < 0) else ""
                row[f"δ = {int(dd)} s"] = f"{sig}{c_(r_['pnl_par_part'])}{sig}"
            row["exécutés à δ = 2 s"] = fmt_number(d.loc[2, "taux_execution"], 0, pct=True)
            row["surcoût à δ = 2 s"] = c_(d.loc[2, "surcout_vs_ask_estime"])
            rows.append(row)
    w("P&L par part (cents, frais inclus), 2e moitié ; **gras** = IC 95 % qui exclut 0. « Ask estimé à t » : exécution supposée au prix "
      "de référence + 0,5 c (hypothèse des études précédentes). « Surcoût » : prix réellement payé − ask estimé.")
    w("")
    w(to_markdown(pd.DataFrame(rows)))
    w("")
    w(f"![{figs['heat']}](pnl_par_instant.png)")
    w("")
    cellsC = pn[(pn["variante"] == "corrigée") & (pn["groupe"].isin(LABELS)) & (pn["delta_s"] >= 0) & (pn["executees"] >= 30)]
    posC = cellsC[cellsC["lo"] > 0]
    pos_late = posC[posC["delta_s"] >= 2]
    best_late = cellsC[cellsC["delta_s"] == 2].sort_values("pnl_par_part", ascending=False).head(2)
    w("Lecture : le gain « papier » (ask estimé) et le gain à δ = 0–1 s viennent de prix appariés **avant** la décision. À δ = 2 s "
      "(exécution au moment de la décision, compte tenu des ≈ 2,2 s de bloc), le surcoût payé mange l'avantage estimé : les vendeurs ont "
      "déjà déplacé leurs prix. "
      + (f"Les {len(posC)} cases à IC positif de la carte sont toutes à δ ≤ 1 s. " if len(pos_late) == 0 else
         f"{len(pos_late)} case(s) à IC positif à δ ≥ 2 s : {', '.join(PRETTY[g] + ' δ=' + str(int(d)) for g, d in zip(pos_late['groupe'], pos_late['delta_s']))}. ")
      + "À δ = 2 s, les meilleurs instants sont "
      + " et ".join(f"{PRETTY[r['groupe']]} ({c_(r['pnl_par_part'])}, IC {c_(r['lo'])} ; {c_(r['hi'])}, {n_(r['executees'])} achats)"
                    for _, r in best_late.iterrows())
      + " : choisis a posteriori parmi 17, sur quelques dizaines d'achats, ce ne sont pas des résultats ; à revalider sur d'autres jours "
      "avant d'y croire.")
    w("")
    mt2 = mt[(mt["moitie"] == "1re moitié") & (mt["delta_s"].isin([0, 2]))].pivot_table(
        index=["variante", "marge"], columns="delta_s", values=["pnl_par_part", "executees"]).reset_index()
    mt2.columns = ["variante", "marge", "achats δ = 0", "achats δ = 2 s", "P&L/part δ = 0", "P&L/part δ = 2 s"]
    mt2["achats δ = 0"] = mt2["achats δ = 0"].astype(int)
    mt2["achats δ = 2 s"] = mt2["achats δ = 2 s"].astype(int)
    mt2["marge"] = [c_(x, 1) for x in mt2["marge"]]
    mt2["P&L/part δ = 0"] = [c_(x) for x in mt2["P&L/part δ = 0"]]
    mt2["P&L/part δ = 2 s"] = [c_(x) for x in mt2["P&L/part δ = 2 s"]]
    w("<details><summary>Choix de la marge sur la 1re moitié (P&L par part selon la marge, δ = 0 et δ = 2 s)</summary>")
    w("")
    w(to_markdown(mt2[["variante", "marge", "achats δ = 0", "P&L/part δ = 0", "achats δ = 2 s", "P&L/part δ = 2 s"]]))
    w("")
    w("</details>")
    w("")

    # 5. carnet réel
    if Lv and "mtab" in Lv:
        w("## 5. Carnet réel (collecteur WebSocket, milliseconde)")
        w("")
        w(f"**n est petit** : {n_live} marchés BTC 5m / BTC 15m / ETH 5m du 26/09/2026 (04:30–06:05 et à partir de 10:00 UTC), "
          "quelques créneaux de 15 min. Paramètres (demi-vie, k, décalage, bruit de source, marges) repris de l'historique BTC, y compris "
          f"pour ETH. σ : {SIG_NAMES[Lv['sig_raw']]} (brute), {SIG_NAMES[Lv['sig_k']]} × {f_(Lv['k_live'], 2)} (corrigée) ; TimesFM n'est "
          "pas disponible en direct. Décision à t sur le **meilleur ask réel** à t ; exécution au meilleur ask réel à t + δ "
          f"(δ ∈ {{{', '.join(fmt_number(d, 1) for d in fb.LIVE_DELAYS)}}} s), en parcourant le carnet pour 10 et 100 parts.")
        w("")
        if len(Lv.get("live_book", [])):
            lb = Lv["live_book"].copy()
            lb["instant"] = lb["instant"].map(PRETTY)
            lb["ecart_median"] = 100 * lb["ecart_median"]
            w("Carnet aux instants de décision (médianes) :")
            w("")
            w(to_markdown(lb.rename(columns={"n": "marchés", "ecart_median": "écart bid–ask (c)", "taille_ask_up_mediane": "parts au meilleur ask Up",
                                             "taille_ask_down_mediane": "parts au meilleur ask Down"}),
                          {"écart bid–ask (c)": 1, "parts au meilleur ask Up": 0, "parts au meilleur ask Down": 0}))
            w("")
        ls = Lv.get("live_scores")
        if ls is not None and len(ls):
            w("Formule contre milieu du carnet au même instant (Brier ; plus bas = mieux) :")
            w("")
            w(to_markdown(ls[["groupe", "n", "marches", "brier_p_f", "brier_p_corr", "brier_mid", "logloss_p_corr", "logloss_mid"]].rename(
                columns={"marches": "marchés", "brier_p_f": "Brier brute", "brier_p_corr": "Brier corrigée", "brier_mid": "Brier milieu du carnet",
                         "logloss_p_corr": "log-loss corrigée", "logloss_mid": "log-loss milieu"}), 4))
            w("")
        if "live" in figs:
            w(f"![{figs['live']}](pnl_carnet_reel.png)")
            w("")
            LP = Lv["live_pnl"].copy()
            a0 = LP[LP["delta_s"] == 0].copy()
            a0["P&L par part à δ = 0 [IC]"] = [ci_c(a, b, c) for a, b, c in zip(a0["pnl_par_part"], a0["lo"], a0["hi"])]
            w(to_markdown(a0[["variante", "qte", "decisions", "marches", "creneaux", "taille_meilleur_ask_mediane", "part_remplie",
                              "prix_moyen", "taux_gain", "P&L par part à δ = 0 [IC]"]].rename(
                columns={"qte": "parts", "decisions": "décisions", "marches": "marchés", "creneaux": "créneaux",
                         "part_remplie": "entièrement rempli", "taille_meilleur_ask_mediane": "taille au meilleur ask (méd.)",
                         "prix_moyen": "prix moyen payé", "taux_gain": "taux de gain"}),
                {"entièrement rempli": "0%", "taille au meilleur ask (méd.)": 0, "prix moyen payé": 3, "taux de gain": "0%"}))
            w("")
            rows = []
            for (v, q), g in LP.groupby(["variante", "qte"]):
                g = g.set_index("delta_s")
                row = {"variante": v, "parts": int(q)}
                for d in fb.LIVE_DELAYS:
                    r_ = g.loc[d]
                    sig = "**" if (r_["lo"] > 0 or r_["hi"] < 0) else ""
                    row[f"δ = {fmt_number(d, 0 if d in (0, 1, 2, 5) else 1)} s"] = f"{sig}{c_(r_['pnl_par_part'])}{sig}"
                rows.append(row)
            w("P&L par part selon la latence (cents ; gras = IC qui exclut 0 ; IC complets dans `carnet_pnl.csv`) :")
            w("")
            w(to_markdown(pd.DataFrame(rows)))
            w("")
            EX = Lv["EX"]
            e0 = EX[(EX["delta_s"] == 0) & (EX["qte"] == 10)]
            ec = e0[e0["variante"] == "corrigée"]
            if len(ec):
                late = ec["instant"].isin(["S+60", "S+120", "E-90", "E-60", "E-30", "E-10"]).mean()
                w(f"Lecture : la variante corrigée n'achète que lorsque l'écart dépasse sa marge ({c_(H['margins']['corrigée'], 0)}) : "
                  f"{fmt_number(late, 0, pct=True)} de ses {len(ec)} achats ont lieu à S+60 s ou après, à un ask médian de "
                  f"{f_(ec['meilleur_ask'].median(), 2)} (des « outsiders » que le marché juge peu probables) ; elle en gagne "
                  f"{int(np.sum(np.where(ec['cote'] == 1, ec['y'], 1 - ec['y'])))}. Le carnet avait raison contre la formule. "
                  "La brute achète plus souvent, près de 0,50, et gagne moins souvent que le prix payé ne l'exige.")
                w("")
        if "reaction" in figs:
            R = Lv["reaction"]
            w("### Délai de réaction du carnet")
            w("")
            w(f"![{figs['reaction']}](reaction_carnet.png)")
            w("")
            rr = []
            for name, d in [("tous", R)] + [(f"phase {p}", R[R["phase"] == p]) for p in (2, 3, 4)] + \
                    [(f"{a} {du}", g) for (a, du), g in R.groupby(["asset", "duree"])]:
                ok = d[~d["censored"]]["delay_ms"]
                rr.append({"groupe": name, "mouvements": len(d), "non suivis": float(d["censored"].mean()) if len(d) else math.nan,
                           "médiane (ms)": float(ok.median()) if len(ok) else math.nan,
                           "quartile 1 (ms)": float(ok.quantile(0.25)) if len(ok) else math.nan,
                           "quartile 3 (ms)": float(ok.quantile(0.75)) if len(ok) else math.nan,
                           "carnet avant la formule": float((ok <= 0).mean()) if len(ok) else math.nan,
                           "< 347 ms": float((ok < 347).mean()) if len(ok) else math.nan})
            w("Méthode : formule corrigée recalculée toutes les 20 ms avec le dernier trade agrégé Binance (ms) et σ figé à S−45 s ; "
              "mouvement = |P(g) − P(g − 1 s)| > 0,05, puis 3 s sans nouveau mouvement ; délai = (premier instant où le milieu du carnet a "
              "parcouru la moitié de ΔP depuis sa valeur à g − 1 s) − (instant où la formule a parcouru la moitié de ΔP). Horloges : "
              "horodatage serveur Polymarket (ms) contre horodatage de trade Binance (ms).")
            w("")
            w(to_markdown(pd.DataFrame(rr), {"non suivis": "0%", "médiane (ms)": 0, "quartile 1 (ms)": 0, "quartile 3 (ms)": 0,
                                             "carnet avant la formule": "0%", "< 347 ms": "0%"}))
            w("")
            w("Lecture : les teneurs de marché déplacent leurs prix environ un quart de seconde après Binance comptant, un peu plus vite "
              "que les 347 ms rapportés par OpenMarket ; dans une partie des cas le carnet bouge même avant (ils suivent sans doute les "
              "contrats à terme, qui mènent le comptant). Les mouvements de fin de fenêtre (phase 4) sont rarement suivis : près de "
              "l'échéance, la moyenne finale est en grande partie déjà fixée et le carnet, souvent collé à 0,01 ou 0,99, ne suit plus "
              "les petits mouvements du comptant (explication probable, non vérifiée ici). "
              "Un preneur qui lit les bougies Binance 1 s (≈ 0,5 s de retard en moyenne sur le dernier trade) puis envoie un ordre arrive "
              "après eux.")
            w("")

    # 6. limites
    w("## 6. Limites")
    w("")
    w("* **Prix d'exécution historiques** : premier achat preneur du même jeton dans la fenêtre ; un autre preneur l'a obtenu, rien ne "
      "garantit qu'il restait de la quantité. Les trades sont horodatés au bloc (≈ 2,2 s après l'appariement) : biais optimiste, voir § 3.")
    w("* **Ask estimé à t** (décision historique) : prix de référence + 0,5 c ; l'écart réel est souvent de 1 c et s'ouvre à 4–5 c à S.")
    w("* **Chainlink** : le flux officiel n'est pas disponible ; Binance décalé de 3–4 s en tient lieu. 1,6 à 2,7 % des marchés se "
      "résolvent autrement que Binance ne l'indique : plancher d'erreur en fin de fenêtre.")
    w("* **Carnet réel** : quelques dizaines de marchés sur une matinée, paramètres BTC appliqués à ETH ; conclusions à confirmer quand "
      "la collecte aura couvert plusieurs jours (le collecteur continue).")
    w("* **Tests multiples** : nombreux instants, durées, variantes et latences ; aucune correction dans les IC affichés.")
    w("")

    # 7. fichiers
    w("## 7. Fichiers")
    w("")
    w("| fichier | contenu |")
    w("|---|---|")
    for name, desc in files:
        w(f"| `{name}` | {desc} |")
    for png, key in (("calibration.png", "calibration"), ("ratio_variance.png", "vr"), ("brier_vs_marche.png", "brier"),
                     ("pnl_latence.png", "pnl"), ("pnl_par_instant.png", "heat"), ("pnl_carnet_reel.png", "live"),
                     ("reaction_carnet.png", "reaction")):
        if key in figs:
            w(f"| `{png}` | {figs[key]} |")
    w("")
    w("Code : `src/tradebot/polymarket_formula_backtest.py` (fonctions testées), `scripts/polymarket_formula_backtest.py` (ce rapport), "
      "`tests/test_polymarket_formula_backtest.py`. Relancer : `python scripts/polymarket_formula_backtest.py` (options `--skip-live`, "
      "`--boot N`, `--report-only`).")
    w("")
    w("## 8. Temps d'exécution")
    w("")
    w(to_markdown(pd.DataFrame(rt.rows), {"secondes": 1}))
    w("")
    return "\n".join(L_)


def args_boot(rt: Runtime) -> str:
    return fmt_number(getattr(rt, "boot", 2000), 0)


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
        rt.boot = args.boot
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
