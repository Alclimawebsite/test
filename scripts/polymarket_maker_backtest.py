#!/usr/bin/env python
"""Simulation MAKER historique sur les trades preneurs des marchés Polymarket « Up or Down »
BTC 5m / 15m (04/09–24/09/2026) : un ordre au repos est-il exécuté, et gagne-t-il ?

Simulation papier sur données publiques : aucun ordre, aucune clé. Moteur d'exécution et
stratégies dans :mod:`tradebot.polymarket_maker` (modèle documenté et testé) ; données :

* trades preneurs (``data-api /v2/trades``, cache ``data/cache/polymarket/wallets/trades/``,
  collectés par ``scripts/polymarket_collect_taker_trades.py``) ;
* marchés, issue officielle et barème de frais (``PolymarketClient``, cache) ; prédictions par
  marché de ``reports/polymarket/modeles_vs_marche/marches_predictions.csv`` (``hgb_ind1s``,
  ``logit_tw``, prix du marché à S−30 s, ``tw_gap30``) ; σ TimesFM / EWMA par marché de
  ``reports/timesfm_amplitude/sigma_par_marche.csv`` ; ``priceToBeat`` (cache
  ``timesfm_amplitude/event_meta.parquet``) ; Binance 1 s (zips journaliers, cache
  ``data/cache/pm_maker/binance_1s/``) et TWAP60(S) (agrégats ``pm_backtest/agg1s``).

Protocole : paramètres choisis sur la 1re moitié (04/09–13/09), évalués sur la 2e (14/09–24/09) ;
IC bootstrap groupés par créneau de 15 min ; correction de Bonferroni sur le nombre de cellules.

Sorties dans ``reports/polymarket/maker/`` : ``README.md`` (français), CSV et PNG.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import polymarket as pm
from tradebot import polymarket_backtest as pb
from tradebot import polymarket_maker as mk
from tradebot.config import CACHE_DIR, REPORTS_DIR
from tradebot.report import BG, BLUE, ORANGE, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes, to_markdown

ROOT = Path(__file__).resolve().parents[1]
OUT_DEFAULT = REPORTS_DIR / "polymarket" / "maker"
MVM_CSV = REPORTS_DIR / "polymarket" / "modeles_vs_marche" / "marches_predictions.csv"
SIGMA_CSV = REPORTS_DIR / "timesfm_amplitude" / "sigma_par_marche.csv"
EVENT_META = CACHE_DIR / "timesfm_amplitude" / "event_meta.parquet"
AGG1S_DIR = CACHE_DIR / "pm_backtest" / "agg1s"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}
AQUA, YELLOW, VIOLET = "#1baf7a", "#eda100", "#4a3aa7"
HALFLIFE_GRID = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0)
MODELS = {"hgb_m0": ("p_hgb_ind1s", 0.0), "hgb_m2": ("p_hgb_ind1s", 0.02), "hgb_m5": ("p_hgb_ind1s", 0.05),
          "gap_m30": ("p_gap", 0.0)}
MODEL_SHORT = {"hgb_m0": "hgb_ind1s", "hgb_m2": "hgb_ind1s m2c", "hgb_m5": "hgb_ind1s m5c", "gap_m30": "gap_m30"}
MODEL_LABELS = {"hgb_m0": "hgb_ind1s (Up ssi p ≥ 0,5)", "hgb_m2": "hgb_ind1s, marge 2 c (|p − 0,5| ≥ 0,02)",
                "hgb_m5": "hgb_ind1s, marge 5 c", "gap_m30": "gap_m30 (signe du TWAP partiel)"}
Q_VARIANTS = ("measured", "0", "500", "2000", "5000")
Q_LABELS = {"measured": "mesurée", "0": "0", "500": "500", "2000": "2 000", "5000": "5 000"}
RULE_LABELS = {0: "(i)+(ii)", 1: "(i) seule"}
FV_MAIN = ("ewma1s", "timesfm_S", "ewma_S")
FV_LABELS = {"ewma1s": "σ EWMA 1 s (demi-vie choisie sur train)", "timesfm_S": "σ TimesFM prévu à S",
             "ewma_S": "σ EWMA 1 min à S (comparateur)"}
# (nom, variante σ, Q_ahead, règle, marge, garder les ordres)
FV_CONFIGS = [("ewma1s", "ewma1s", mk.Q_AHEAD_IN_WINDOW, 0, 0.01, True),
              ("timesfm_S", "timesfm_S", mk.Q_AHEAD_IN_WINDOW, 0, 0.01, True),
              ("ewma_S", "ewma_S", mk.Q_AHEAD_IN_WINDOW, 0, 0.01, True),
              ("ewma1s|q0", "ewma1s", 0.0, 0, 0.01, False), ("ewma1s|q500", "ewma1s", 500.0, 0, 0.01, False),
              ("ewma1s|q2000", "ewma1s", 2000.0, 0, 0.01, False), ("ewma1s|q5000", "ewma1s", 5000.0, 0, 0.01, False),
              ("ewma1s|(i)", "ewma1s", mk.Q_AHEAD_IN_WINDOW, 1, 0.01, False),
              ("ewma1s|m2c", "ewma1s", mk.Q_AHEAD_IN_WINDOW, 0, 0.02, False)]
TAKER_MARGINS = {"hgb_ind1s": ("p_hgb_ind1s", 0.0275), "logit_tw": ("p_logit_tw", 0.005)}   # marges de modeles_vs_marche

log = logging.getLogger("maker_backtest")


class Runtime:
    def __init__(self):
        self.steps: list[tuple[str, float]] = []
        self.t0 = time.time()

    def __call__(self, step: str):
        rt = self

        class _Ctx:
            def __enter__(self_):
                self_.t = time.time()
                print(f"… {step}", file=sys.stderr, flush=True)

            def __exit__(self_, *a):
                rt.steps.append((step, time.time() - self_.t))
                print(f"  {step} : {time.time() - self_.t:.1f} s", file=sys.stderr, flush=True)

        return _Ctx()

    def total(self) -> float:
        return time.time() - self.t0


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------
def fr(v, d: int = 1, pct: bool = False, signed: bool = False) -> str:
    from tradebot.report import fmt_number

    return fmt_number(v, d, pct=pct, signed=signed)


def cents(v, d: int = 2) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{fr(v, d, signed=True)} c"


def ic(lo, hi, d: int = 2, unit: str = " c") -> str:
    if lo is None or hi is None or not (np.isfinite(lo) and np.isfinite(hi)):
        return "IC —"
    return f"IC {fr(lo, d, signed=True)}{unit} ; {fr(hi, d, signed=True)}{unit}"


def n_(v) -> str:
    return fr(v, 0)


def pct(v, d: int = 1) -> str:
    return fr(v, d, pct=True)


# ---------------------------------------------------------------------------
# 1. Marchés
# ---------------------------------------------------------------------------
def load_markets(args, rt: Runtime) -> tuple[pd.DataFrame, dict]:
    client = pm.PolymarketClient()
    with rt("1a. marchés, issue, barème (PolymarketClient, cache)"):
        M = pb.market_table(client, args.assets, args.durations, args.start, args.end, max_workers=4)
        n_found = len(M)
        M = M[np.isfinite(M["y"])].reset_index(drop=True)
    with rt("1b. prédictions modeles_vs_marche, σ TimesFM, priceToBeat"):
        mvm = pd.read_csv(MVM_CSV, usecols=["slug", "p_pre", "p_pre_age_s", "tw_gap30", "p_hgb_ind1s", "p_logit_tw", "sigma_1m"])
        M = M.merge(mvm, on="slug", how="left")
        # milieu à S−60 s pour la règle du milieu de (b), posée à S−60 s (p_pre est le prix à S−30 s :
        # l'utiliser à S−60 s serait un regard en avant de 30 s qui écarte surtout les ordres du côté
        # vers lequel le carnet est parti, c'est-à-dire ceux qui auraient été exécutés à perte)
        pre60 = pb.fetch_pre_open_prices(client, M, workers=8, offset_s=-mk.TWO_SIDED_PLACE_OFFSET_S)
        M = M.merge(pre60.rename(columns={"p_pre": "p_pre60", "p_pre_age_s": "p_pre60_age_s"})[["slug", "p_pre60", "p_pre60_age_s"]],
                    on="slug", how="left")
        if SIGMA_CSV.exists():
            sg = pd.read_csv(SIGMA_CSV, usecols=["slug", "sigma_timesfm_bp", "sigma_rv_bp", "amplitude_quantile_train", "quintile_train"])
            M = M.merge(sg, on="slug", how="left")
        else:
            for c in ("sigma_timesfm_bp", "sigma_rv_bp", "amplitude_quantile_train", "quintile_train"):
                M[c] = np.nan
        if EVENT_META.exists():
            em = pd.read_parquet(EVENT_META)
            M = M.merge(em[["slug", "price_to_beat", "final_price"]], on="slug", how="left")
        else:
            M["price_to_beat"] = np.nan
            M["final_price"] = np.nan
    with rt("1c. TWAP60(S) Binance (agrégats 1 s pm_backtest)"):
        M["twap60_S"] = np.nan
        for asset in args.assets:
            agg = pb.load_second_aggregates(SYMBOL[asset], args.start - pd.Timedelta(days=1), args.end, AGG1S_DIR, workers=2)
            m = (M["asset"] == asset).to_numpy()
            M.loc[m, "twap60_S"] = pb.bar_lookup(agg, M.loc[m, "start_ts"].to_numpy(dtype="int64") - 60, "twap60")
    M["slot"] = M["start_ts"] // pb.SLOT_S
    M["train"] = M["start_ts"] < int(args.mid.timestamp())
    M["half"] = np.where(M["train"], "1re moitié", "2e moitié")
    M["p_gap"] = np.where(M["tw_gap30"] > 0, 1.0, np.where(M["tw_gap30"] < 0, 0.0, np.nan))
    for name, (col, margin) in MODELS.items():
        M[f"side_{name}"] = [mk.model_side(p, margin) for p in M[col].to_numpy(dtype="float64")]
    with np.errstate(invalid="ignore", divide="ignore"):
        M["level_gap_bp"] = np.log(M["twap60_S"] / M["price_to_beat"]) * 1e4
        M["move_bp"] = np.log(M["final_price"] / M["price_to_beat"]) * 1e4
    M["sigma_timesfm_1s"] = M["sigma_timesfm_bp"] * 1e-4 / np.sqrt(M["duration_s"] + 60.0)
    M["sigma_ewmaS_1s"] = M["sigma_rv_bp"] * 1e-4 / np.sqrt(M["duration_s"] + 60.0)
    if args.max_markets and args.max_markets < len(M):
        M = M.sample(args.max_markets, random_state=args.seed).sort_values(["start_ts", "asset", "duration"]).reset_index(drop=True)
    meta = {"n_found": n_found, "n_resolved": int(len(M)),
            "n_expected": sum(len(pm.slot_starts(d, args.start, args.end)) for d in args.durations) * len(args.assets)}
    return M, meta


# ---------------------------------------------------------------------------
# 2. Binance 1 s et juste valeur
# ---------------------------------------------------------------------------
def load_binance_1s(args, symbol: str) -> tuple[np.ndarray, np.ndarray, dict[float, np.ndarray]]:
    days = pd.date_range(args.start - pd.Timedelta(days=1), args.end - pd.Timedelta(days=1), freq="D")
    df = mk.fetch_binance_1s_closes(symbol, days)
    sec = df["sec"].to_numpy(dtype="int64")
    close = df["close"].to_numpy(dtype="float64")
    sig = {hl: mk.ewma_sigma_1s(close, hl) for hl in HALFLIFE_GRID}
    return sec, close, sig


def fv_inputs(row, sec: np.ndarray, close: np.ndarray, sig_hl: dict[float, np.ndarray], hl: float | None) -> dict:
    """Instants de cotation, spot et P(Up) par variante de σ pour un marché (NaN si TWAP60(S) absent)."""
    S, E, K = int(row["start_ts"]), int(row["end_ts"]), row["twap60_S"]
    tq = mk.quote_times(S, E)
    # information close à tq : la bougie 1 s ouverte à tq − 1 (close à tq) ; la bougie ouverte à tq
    # n'est close qu'à tq + 1 (la prendre serait un regard en avant d'une seconde)
    idx = np.searchsorted(sec, tq - 1)
    ok = (idx < len(sec)) & (sec[np.minimum(idx, len(sec) - 1)] == tq - 1)
    spot = np.where(ok, close[np.minimum(idx, len(sec) - 1)], np.nan)
    out = {"tq": tq, "spot": spot}
    if not np.isfinite(K) or K <= 0:
        for v in FV_MAIN:
            out[v] = np.full(len(tq), np.nan)
        return out
    if hl is not None:
        out["ewma1s"] = mk.fair_value_probs(spot, K, sig_hl[hl][np.minimum(idx, len(sec) - 1)], tq, E)
    out["timesfm_S"] = mk.fair_value_probs(spot, K, float(row["sigma_timesfm_1s"]) if np.isfinite(row["sigma_timesfm_1s"]) else np.nan, tq, E)
    out["ewma_S"] = mk.fair_value_probs(spot, K, float(row["sigma_ewmaS_1s"]) if np.isfinite(row["sigma_ewmaS_1s"]) else np.nan, tq, E)
    return out


def fair_value_quality(M: pd.DataFrame, sec, close, sig_hl, rt: Runtime) -> tuple[float, pd.DataFrame]:
    """Log-loss et Brier de Φ(d/σ) aux instants de cotation contre l'issue, par variante de σ et
    moitié ; la demi-vie EWMA 1 s est choisie sur la 1re moitié (log-loss)."""
    rows = []
    with rt("2b. juste valeur aux instants de cotation : choix de la demi-vie EWMA (train)"):
        per = {}      # (variant, slug) -> (logloss, brier, n)
        for _, r in M.iterrows():
            if not np.isfinite(r["twap60_S"]):
                continue
            S, E, K = int(r["start_ts"]), int(r["end_ts"]), r["twap60_S"]
            tq = mk.quote_times(S, E)
            idx = np.searchsorted(sec, tq - 1)          # bougie ouverte à tq − 1, close à tq
            ok = (idx < len(sec)) & (sec[np.minimum(idx, len(sec) - 1)] == tq - 1)
            if not ok.all():
                continue
            spot = close[idx]
            y = float(r["y"])
            variants = {f"ewma1s_h{int(hl)}": sig_hl[hl][idx] for hl in HALFLIFE_GRID}
            variants["timesfm_S"] = r["sigma_timesfm_1s"]
            variants["ewma_S"] = r["sigma_ewmaS_1s"]
            for v, sg in variants.items():
                if np.ndim(sg) == 0 and not np.isfinite(sg):
                    continue
                p = mk.fair_value_probs(spot, K, sg, tq, E)
                pc = np.clip(p, 1e-4, 1 - 1e-4)
                ll = -(y * np.log(pc) + (1 - y) * np.log(1 - pc))
                per[(v, r["slug"])] = (float(np.nanmean(ll)), float(np.nanmean((p - y) ** 2)), int(np.isfinite(p).sum()))
        tab = pd.DataFrame([{"variant": k[0], "slug": k[1], "log_loss": v[0], "brier": v[1], "n_quotes": v[2]} for k, v in per.items()])
        tab = tab.merge(M[["slug", "train", "duration", "slot"]], on="slug", how="left")
        for (v, tr, d), g in tab.groupby(["variant", "train", "duration"]):
            rows.append({"variant": v, "half": "1re moitié" if tr else "2e moitié", "duration": d, "n_markets": len(g),
                         "log_loss": g["log_loss"].mean(), "brier": g["brier"].mean()})
        for (v, tr), g in tab.groupby(["variant", "train"]):
            rows.append({"variant": v, "half": "1re moitié" if tr else "2e moitié", "duration": "toutes", "n_markets": len(g),
                         "log_loss": g["log_loss"].mean(), "brier": g["brier"].mean()})
        q = pd.DataFrame(rows)
        tr = q[(q["half"] == "1re moitié") & (q["duration"] == "toutes") & q["variant"].str.startswith("ewma1s_h")]
        best = tr.sort_values("log_loss").iloc[0]["variant"] if len(tr) else "ewma1s_h60"
        hl = float(best.split("_h")[1])
    return hl, q


# ---------------------------------------------------------------------------
# 3. Simulation par marché (fonction de travail, exécutée dans un pool de processus)
# ---------------------------------------------------------------------------
def build_payload(row, mt: mk.MarketTrades, fv: dict, args) -> dict:
    return {"slug": row["slug"], "start_ts": int(row["start_ts"]), "end_ts": int(row["end_ts"]), "p_pre": float(row["p_pre"]),
            "p_pre60": float(row["p_pre60"]) if "p_pre60" in row else float(row["p_pre"]),
            "y": float(row["y"]), "fee": {"rate": float(row["fee_rate"]), "exponent": float(row["fee_exponent"])},
            "size": float(args.size), "delay_s": float(args.delay), "lookback_s": float(args.lookback),
            "arrays": (mt.t, mt.price, mt.size, mt.cp_up, mt.cp_dn, mt.ap_up, mt.ap_dn), "fv": fv, "skip_fv": bool(args.skip_fv)}


def process_market(p: dict) -> dict:
    S, E = p["start_ts"], p["end_ts"]
    t, price, size, cp_up, cp_dn, ap_up, ap_dn = p["arrays"]
    mt = mk.MarketTrades(p["slug"], S, E, t, price, size, cp_up, cp_dn, ap_up, ap_dn)
    fees_total = mt.taker_fees_total(p["fee"])
    Q = p["size"]
    # (a) et (b) : ordres de base, annulation à E, toutes files et règles (les annulations plus
    # tôt s'en déduisent : exécuté ssi t_fill < t_annulation + délai)
    rows = []
    for strategy, offset, prices in (("signal", mk.SIGNAL_PLACE_OFFSET_S, mk.SIGNAL_PRICES),
                                     ("two_sided", mk.TWO_SIDED_PLACE_OFFSET_S, mk.TWO_SIDED_PRICES)):
        for side in ("up", "down"):
            for L in prices:
                # règle du milieu avec le milieu connu à la pose : S−30 s pour (a), S−60 s pour (b)
                cross = mk._mid_crossing(side, L, p["p_pre"] if strategy == "signal" else p.get("p_pre60", p["p_pre"]))
                for qv in Q_VARIANTS:
                    q = mk.measured_q_ahead(side, L, "pre") if qv == "measured" else float(qv)
                    for rule in (0, 1):
                        rows.append({"strategy": strategy, "side": side, "price": L, "t_place": float(S + offset), "t_cancel": float(E),
                                     "q_ahead": q, "size": Q, "rule": rule, "crossing": cross, "q_variant": qv})
    base = mk.simulate_orders(mt, pd.DataFrame(rows), delay_s=p["delay_s"], cross_lookback_s=p["lookback_s"])
    base["slug"] = p["slug"]
    base = base[["slug", "strategy", "side", "price", "t_place", "q_variant", "rule", "q_ahead", "crossing", "placed",
                 "t_fill", "fill_reason", "consumed"]]
    # (c) juste valeur
    fv_orders, fv_agg = [], []
    if not p["skip_fv"]:
        for name, variant, q, rule, margin, keep in FV_CONFIGS:
            pu = p["fv"].get(variant)
            if pu is None or not np.isfinite(pu).any():
                continue
            o = mk.orders_fair_value(p["fv"]["tq"], pu, E - mk.FV_END_BEFORE_S, margin=margin, size=Q, q_ahead=q, label=name)
            if not len(o):
                continue
            o["rule"] = rule
            r = mk.simulate_orders(mt, o, delay_s=p["delay_s"], cross_lookback_s=p["lookback_s"])
            r = mk.score_orders(r, p["y"], p["fee"], fees_total)
            placed = r[r["placed"]]
            ex = placed[placed["executed"]]
            nx = placed[~placed["executed"]]
            fv_agg.append({"slug": p["slug"], "config": name, "n_quotes": int(o["n_quotes"].sum()), "n_orders": int(len(placed)),
                           "n_crossing": int(r["crossing"].sum()), "n_exec": int(len(ex)),
                           "n_trav": int((ex["fill_reason"] == "traversal").sum()), "pnl_usd": float(placed["pnl_order"].sum()),
                           "rebate_usd": float((ex["rebate_per_share"] * ex["size"]).sum()), "win_exec": float(ex["would_win"].sum()),
                           "n_not_exec": int(len(nx)), "win_not_exec": float(nx["would_win"].sum()),
                           "delay_sum": float(ex["delay_fill_s"].sum()),
                           "net_up": float((ex.loc[ex["side"] == "up", "size"]).sum()), "net_down": float((ex.loc[ex["side"] == "down", "size"]).sum()),
                           "size": Q})
            if keep:
                r["slug"] = p["slug"]
                r["config"] = name
                fv_orders.append(r[["slug", "config", "side", "price", "t_place", "t_cancel", "n_quotes", "p_up", "crossing", "placed",
                                    "executed", "t_fill", "delay_fill_s", "fill_reason", "would_win", "pnl_per_share",
                                    "pnl_per_placed_share", "pnl_order", "rebate_per_share", "size"]])
    stats = {"slug": p["slug"], "n_trades": mt.n, "taker_shares": mt.taker_shares_total(), "taker_fees_total": fees_total,
             "taker_notional": float((price * size).sum()) if mt.n else 0.0,
             "n_pre60": int(((t >= S - 60) & (t < S)).sum()), "n_post60": int(((t >= S) & (t < S + 60)).sum()),
             "shares_pre60": float(size[(t >= S - 60) & (t < S)].sum()), "shares_post60": float(size[(t >= S) & (t < S + 60)].sum())}
    return {"base": base, "fv": pd.concat(fv_orders, ignore_index=True) if fv_orders else None,
            "fv_agg": pd.DataFrame(fv_agg) if fv_agg else None, "stats": stats}


def run_simulation(M: pd.DataFrame, args, sec, close, sig_hl, hl: float, rt: Runtime) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    bases, fvs, aggs, stats = [], [], [], []
    with rt("3. simulation des ordres au repos sur les trades preneurs (pool de processus)"):
        ctx = mp.get_context("fork")
        n_done, t0 = 0, time.time()
        batch: list[dict] = []

        def flush(ex):
            nonlocal n_done
            if not batch:
                return
            for res in ex.map(process_market, batch, chunksize=4):
                bases.append(res["base"])
                if res["fv"] is not None:
                    fvs.append(res["fv"])
                if res["fv_agg"] is not None:
                    aggs.append(res["fv_agg"])
                stats.append(res["stats"])
            n_done += len(batch)
            batch.clear()
            print(f"  marchés {n_done}/{len(M)}  {time.time() - t0:6.0f} s", file=sys.stderr, flush=True)

        with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as ex:
            for row, mt in mk.iter_market_trades(M):
                fv = fv_inputs(row, sec, close, sig_hl, hl) if not args.skip_fv else {}
                batch.append(build_payload(row, mt, fv, args))
                if len(batch) >= 64:
                    flush(ex)
            flush(ex)
    base = pd.concat(bases, ignore_index=True)
    fv = pd.concat(fvs, ignore_index=True) if fvs else pd.DataFrame()
    agg = pd.concat(aggs, ignore_index=True) if aggs else pd.DataFrame()
    return base, fv, agg, pd.DataFrame(stats)


# ---------------------------------------------------------------------------
# 4. Analyse
# ---------------------------------------------------------------------------
MKT_COLS = ["slug", "asset", "duration", "start_ts", "end_ts", "y", "fee_rate", "fee_exponent", "slot", "train", "half",
            "quintile_train", "taker_fees_total"] + [f"side_{m}" for m in MODELS]


def attach(df: pd.DataFrame, M: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in MKT_COLS if c in M.columns and c not in df.columns or c == "slug"]
    return df.merge(M[cols], on="slug", how="left")


def expand_cancels(base: pd.DataFrame, cancels, delay: float) -> pd.DataFrame:
    """Ordres de base (annulation à E) -> une ligne par annulation ; exécuté ssi t_fill < t_c + délai."""
    parts = []
    for c in cancels:
        d = base.copy()
        t_c = d["end_ts"].to_numpy(dtype="float64") if c == "E" else d["start_ts"].to_numpy(dtype="float64") + int(c)
        ex = d["placed"].to_numpy(dtype=bool) & np.isfinite(d["t_fill"].to_numpy(dtype="float64")) & (d["t_fill"].to_numpy(dtype="float64") < t_c + delay)
        d["cancel"] = mk._cancel_label(c)
        d["t_cancel"] = t_c
        d["executed"] = ex
        d["t_fill"] = np.where(ex, d["t_fill"], np.nan)
        d["delay_fill_s"] = d["t_fill"] - d["t_place"]
        d["fill_reason"] = np.where(ex, d["fill_reason"], "")
        d["label"] = d["price"].map(lambda x: f"{x:.2f}") + "/" + d["cancel"]
        parts.append(d)
    return pd.concat(parts, ignore_index=True)


def signal_table(base: pd.DataFrame, size: float, delay: float, models=MODELS, q_variant: str = "measured", rule: int = 0) -> pd.DataFrame:
    """(a) : pour chaque modèle, les ordres du côté qu'il favorise, toutes annulations, scorés."""
    b = base[(base["strategy"] == "signal") & (base["q_variant"] == q_variant) & (base["rule"] == rule)]
    parts = []
    for name in models:
        d = b[b["side"] == b[f"side_{name}"]].copy()
        d["model"] = name
        parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    d["size"] = size
    return mk.score_frame(expand_cancels(d, mk.SIGNAL_CANCELS, delay))


def two_sided_table(base: pd.DataFrame, size: float, delay: float, q_variant: str = "measured", rule: int = 0) -> pd.DataFrame:
    b = base[(base["strategy"] == "two_sided") & (base["q_variant"] == q_variant) & (base["rule"] == rule)].copy()
    b["size"] = size
    d = mk.score_frame(expand_cancels(b, mk.TWO_SIDED_CANCELS, delay))
    d["pair"] = d["label"]
    return d


def summarize_halves(df: pd.DataFrame, by, B: int, seed: int, alpha_adj: float | None = None) -> pd.DataFrame:
    out = []
    for half, g in df.groupby("half", sort=True):
        g = g.reset_index(drop=True)
        boot = pb.SlotBootstrap(g["slot"].to_numpy(), B=B, seed=seed)
        boot_adj = pb.SlotBootstrap(g["slot"].to_numpy(), B=B, seed=seed, alpha=alpha_adj) if alpha_adj else None
        s = mk.summarize(g, by=by, boot=boot, boot_adj=boot_adj)
        s.insert(0, "half", half)
        out.append(s)
    return pd.concat(out, ignore_index=True)


def choose_config(summary: pd.DataFrame, by, min_orders: int = 200) -> dict:
    """Règle fixée d'avance : meilleur P&L par part placée sur la 1re moitié (>= min_orders ordres)."""
    tr = summary[(summary["half"] == "1re moitié") & (summary["n_orders"] >= min_orders)]
    if not len(tr):
        tr = summary[summary["half"] == "1re moitié"]
    best = tr.sort_values("pnl_per_placed_c", ascending=False).iloc[0]
    return {k: best[k] for k in by}


def agg_summary(agg: pd.DataFrame, M: pd.DataFrame, by, B: int, seed: int) -> pd.DataFrame:
    """Résumé (c) à partir des agrégats par marché (variantes de sensibilité) : ratios de sommes, IC par créneau."""
    a = agg.merge(M[["slug", "slot", "train", "half", "duration"]], on="slug", how="left")
    rows = []
    for key, g in a.groupby(list(by), sort=False):
        key = key if isinstance(key, tuple) else (key,)
        boot = pb.SlotBootstrap(g["slot"].to_numpy(), B=B, seed=seed)
        n_orders = g["n_orders"].to_numpy(dtype=float)
        n_exec = g["n_exec"].to_numpy(dtype=float)
        size = g["size"].to_numpy(dtype=float)
        pnl = g["pnl_usd"].to_numpy(dtype=float)
        row = dict(zip(by, key))
        row["n_markets"] = int(len(g))
        row["n_orders"] = int(n_orders.sum())
        row["n_crossing"] = int(g["n_crossing"].sum())
        row["fill_rate"], row["fill_rate_ic_lo"], row["fill_rate_ic_hi"] = boot.ratio(n_exec, n_orders)
        e, lo, hi = boot.ratio(100 * pnl, size * n_orders)
        row.update(pnl_per_placed_c=e, pnl_per_placed_ic_lo=lo, pnl_per_placed_ic_hi=hi)
        e2, lo2, hi2 = boot.ratio(100 * pnl, size * n_exec)
        row.update(pnl_per_executed_c=e2, pnl_per_executed_ic_lo=lo2, pnl_per_executed_ic_hi=hi2)
        row["rebate_per_executed_c"] = 100 * g["rebate_usd"].sum() / max(1.0, (size * n_exec).sum())
        row["share_traversal"] = g["n_trav"].sum() / max(1.0, n_exec.sum())
        row["win_rate_executed"] = g["win_exec"].sum() / max(1.0, n_exec.sum())
        row["win_rate_not_executed"] = g["win_not_exec"].sum() / max(1.0, g["n_not_exec"].sum())
        row["adverse_selection_pts"] = 100 * (row["win_rate_executed"] - row["win_rate_not_executed"])
        row["delay_mean_s"] = g["delay_sum"].sum() / max(1.0, n_exec.sum())
        row["pnl_total_usd"] = float(pnl.sum())
        row["pnl_per_market_usd"] = float(pnl.mean())
        _, plo, phi = boot.mean(pnl)
        row.update(pnl_per_market_ic_lo=plo, pnl_per_market_ic_hi=phi)
        rows.append(row)
    return pd.DataFrame(rows)


def quintile_table(df: pd.DataFrame, B: int, seed: int, label: str) -> pd.DataFrame:
    """(d) : P&L par part placée, exécution, sélection adverse par quintile d'amplitude TimesFM (2e moitié) ; Q5 − Q1 avec IC."""
    g = df[(df["half"] == "2e moitié") & np.isfinite(df["quintile_train"])].reset_index(drop=True)
    if not len(g):
        return pd.DataFrame()
    boot = pb.SlotBootstrap(g["slot"].to_numpy(), B=B, seed=seed)
    s = mk.summarize(g, by=["quintile_train"], boot=boot)
    s["quintile_train"] = s["quintile_train"].astype(int)
    s = s.sort_values("quintile_train")
    x = 100 * g["pnl_per_placed_share"].to_numpy(dtype=float)
    m_scored = np.isfinite(x)
    q = g["quintile_train"].to_numpy()
    _, d5 = boot.ratio_draws(x, 1.0, m_scored & (q == 5))
    _, d1 = boot.ratio_draws(x, 1.0, m_scored & (q == 1))
    dd = d5 - d1
    dd = dd[np.isfinite(dd)]
    e5 = np.nanmean(x[m_scored & (q == 5)]) if (m_scored & (q == 5)).any() else np.nan
    e1 = np.nanmean(x[m_scored & (q == 1)]) if (m_scored & (q == 1)).any() else np.nan
    s["strategy_label"] = label
    s["q5_minus_q1_c"] = e5 - e1
    s["q5_minus_q1_ic_lo"] = float(np.quantile(dd, 0.025)) if dd.size else np.nan
    s["q5_minus_q1_ic_hi"] = float(np.quantile(dd, 0.975)) if dd.size else np.nan
    return s


def taker_comparison(M: pd.DataFrame, size: float, B: int, seed: int) -> pd.DataFrame:
    """Preneur (modeles_vs_marche, marges choisies là-bas) et borne haute maker sur les mêmes marchés (2e moitié)."""
    te = M[~M["train"]].reset_index(drop=True)
    boot = pb.SlotBootstrap(te["slot"].to_numpy(), B=B, seed=seed)
    rows = []
    y = te["y"].to_numpy(dtype=float)
    pm_ = te["p_pre"].to_numpy(dtype=float)
    fr_, fe_ = te["fee_rate"].to_numpy(dtype=float), te["fee_exponent"].to_numpy(dtype=float)
    specs = [(f"preneur {m} (marge {100 * mg:.2f} c)", te[col].to_numpy(dtype=float), mg, "taker") for m, (col, mg) in TAKER_MARGINS.items()]
    specs.append(("preneur gap_m30 (toujours en position)", te["p_gap"].to_numpy(dtype=float), -1.0, "taker"))
    specs.append(("borne haute maker hgb_ind1s (achat au bid, exécution supposée certaine)", te["p_hgb_ind1s"].to_numpy(dtype=float), 0.0, "maker"))
    for name, p, mg, mode in specs:
        t = pb.taker_trades(p, pm_, y, mg, fr_, fe_) if mode == "taker" else pb.maker_trades(p, pm_, y, mg)
        pos = (t["side"] != 0).to_numpy()
        pnl = t["pnl"].to_numpy(dtype=float)
        e, lo, hi = boot.mean(100 * pnl, pos)
        rows.append({"strategie": name, "n_markets": int(len(te)), "n_positions": int(pos.sum()), "win_rate": float(t.loc[pos, "win"].mean()) if pos.any() else np.nan,
                     "pnl_per_share_c": e, "pnl_ic_lo": lo, "pnl_ic_hi": hi, "pnl_total_usd": float(np.nansum(pnl) * size),
                     "pnl_series": pnl * size})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. Graphiques
# ---------------------------------------------------------------------------
STRAT_COLORS = {"signal": BLUE, "two_sided": ORANGE, "fair_value": AQUA}
STRAT_LABELS = {"signal": "(a) signal maker à S−30 s", "two_sided": "(b) deux côtés avant l'ouverture", "fair_value": "(c) juste valeur ± 1 c"}


def plot_configs(rows: pd.DataFrame, all_cells: pd.DataFrame, path: Path, n_markets: int, model_label: str) -> str:
    plt = _pyplot()
    b = rows.reset_index(drop=True)
    a = all_cells
    pos = a[np.isfinite(a["pnl_per_placed_ic_lo_adj"]) & (a["pnl_per_placed_ic_lo_adj"] > 0)]
    pos_raw = a[np.isfinite(a["pnl_per_placed_ic_lo"]) & (a["pnl_per_placed_ic_lo"] > 0)]
    best = a.sort_values("pnl_per_placed_c", ascending=False).iloc[0]
    if len(pos):
        title = (f"{len(pos)} configuration(s) sur {len(a)} gardent un P&L maker positif après Bonferroni sur la 2e moitié "
                 f"(au mieux {best['name']} : {cents(best['pnl_per_placed_c'])} par part placée)")
    elif len(pos_raw):
        title = (f"{len(pos_raw)} configuration(s) sur {len(a)} ont un IC 95 % positif sur la 2e moitié, aucune après Bonferroni "
                 f"(au mieux {best['name']} : {cents(best['pnl_per_placed_c'])} par part placée)")
    else:
        title = (f"Aucune des {len(a)} configurations maker n'a un P&L par part placée significativement positif sur la 2e moitié "
                 f"({n_(n_markets)} marchés ; au mieux {best['name']} : {cents(best['pnl_per_placed_c'])}, {ic(best['pnl_per_placed_ic_lo'], best['pnl_per_placed_ic_hi'])})")
    sub = (f"(a) : modèle choisi sur la 1re moitié ({model_label}) ; les autres modèles sont dans le README. "
           "P&L à la résolution par part placée (exécutée ou non), sans frais ni remise, ordres croisants écartés ; 100 parts par ordre. "
           "Trait épais : IC 95 % bootstrap par créneau de 15 min ; trait fin : IC corrigé de Bonferroni. "
           "À droite : taux d'exécution et sélection adverse (gain si exécuté − gain sinon).")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 0.30 * len(b) + 1.3
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.30, right=0.78, top=1 - hh / H, bottom=1.0 / H)
    _draw_header(fig, t, s)
    _style_axes(ax, ygrid=False, xgrid=True)
    y = np.arange(len(b))[::-1]
    for yv, (_, r) in zip(y, b.iterrows()):
        c = STRAT_COLORS[r["strategy"]]
        ax.barh(yv, r["pnl_per_placed_c"], height=0.55, color=c, zorder=2)
        lo, hi = r["pnl_per_placed_ic_lo"], r["pnl_per_placed_ic_hi"]
        if np.isfinite(lo) and np.isfinite(hi):
            ax.plot([lo, hi], [yv, yv], color=TEXT, lw=1.6, zorder=3)
        lo2, hi2 = r.get("pnl_per_placed_ic_lo_adj", np.nan), r.get("pnl_per_placed_ic_hi_adj", np.nan)
        if np.isfinite(lo2) and np.isfinite(hi2):
            ax.plot([lo2, hi2], [yv, yv], color=TEXT_2, lw=0.6, zorder=3)
        txt = f"exéc. {pct(r['fill_rate'], 0)} · n = {n_(r['n_orders'])}"
        if np.isfinite(r["adverse_selection_pts"]):
            txt += f" · sél. adv. {fr(r['adverse_selection_pts'], 0, signed=True)} pt"
        ax.text(1.01, yv, txt, transform=ax.get_yaxis_transform(), va="center", ha="left", fontsize=7.5, color=TEXT_2)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(b["name"].tolist(), fontsize=8, color=TEXT)
    ax.set_xlabel("P&L par part placée, 2e moitié (cents)", color=TEXT_2, fontsize=9)
    vals = np.r_[b["pnl_per_placed_c"].to_numpy(), b["pnl_per_placed_ic_lo"].to_numpy(), b["pnl_per_placed_ic_hi"].to_numpy()]
    vals = vals[np.isfinite(vals)]
    if len(vals):
        ax.set_xlim(min(vals.min() - 1, -3), max(vals.max() + 1, 3))
    from matplotlib.lines import Line2D

    fig.legend(handles=[Line2D([0], [0], color=c, lw=6, label=STRAT_LABELS[k]) for k, c in STRAT_COLORS.items()],
               loc="lower center", bbox_to_anchor=(0.5, 0.005), ncol=3, frameon=False, fontsize=8, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_cumulative(series: dict[str, pd.Series], path: Path, size: float) -> str:
    plt = _pyplot()
    finals = {k: float(v.iloc[-1]) for k, v in series.items() if len(v)}
    keys = list(series)
    best = max(finals, key=finals.get)
    title = (f"2e moitié, {n_(size)} parts par ordre : {best} finit au plus haut ({fr(finals[best], 0, signed=True)} $) ; "
             + " ; ".join(f"{k} {fr(v, 0, signed=True)} $" for k, v in finals.items() if k != best))
    sub = ("P&L cumulé à la résolution, par marché dans l'ordre chronologique (BTC 5m et 15m mêlés) ; maker : sans frais ni remise, "
           "ordres non exécutés = 0 ; preneur : frais inclus, ask = milieu + 0,005 (sans le surcoût d'exécution mesuré de 1,3 à 3,1 c).")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 4.2
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - hh / H, bottom=0.9 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    colors = [BLUE, ORANGE, AQUA, VIOLET, YELLOW]
    for k, c in zip(keys, colors):
        v = series[k]
        if len(v):
            ax.plot(v.index, v.to_numpy(), color=c, lw=1.6, label=k)
    ax.axhline(0, color=TEXT_2, lw=0.8)
    ax.set_ylabel("P&L cumulé ($)", color=TEXT_2, fontsize=9)
    import matplotlib.dates as mdates

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="lower left")
    _save(fig, path)
    return title


def plot_sensitivity(sens: pd.DataFrame, path: Path) -> str:
    plt = _pyplot()
    strategies = [s for s in ("signal", "two_sided", "fair_value") if (sens["strategy"] == s).any()]
    xcat = ["0", "measured", "500", "2000", "5000"]
    xlab = ["0", "mesurée", "500", "2 000", "5 000"]
    d = sens[(sens["rule"] == 0) & (sens["q_variant"] == "measured")]
    d5 = sens[(sens["rule"] == 0) & (sens["q_variant"] == "5000")]
    spans = []
    for s in strategies:
        a = d[d["strategy"] == s]
        b = d5[d5["strategy"] == s]
        if len(a) and len(b):
            spans.append(f"{STRAT_LABELS[s].split(')')[0]}) exéc. {pct(a['fill_rate'].iloc[0], 0)} -> {pct(b['fill_rate'].iloc[0], 0)}, "
                         f"P&L {cents(a['pnl_per_placed_c'].iloc[0], 1)} -> {cents(b['pnl_per_placed_c'].iloc[0], 1)}")
    title = "File d'attente de 0 à 5 000 parts (règle (i)+(ii)) : " + " ; ".join(spans)
    sub = ("P&L par part placée (2e moitié) selon la file devant l'ordre (Q_ahead) et la règle d'exécution : (i)+(ii) = traversée ou cumul ; "
           "(i) seule = traversée seulement (conservatrice). Barres : IC 95 % par créneau de 15 min.")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 4.6
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - hh / H, bottom=1.55 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    x = np.arange(len(xcat))
    k = 0
    for s in strategies:
        for rule, ls in ((0, "-"), (1, "--")):
            g = sens[(sens["strategy"] == s) & (sens["rule"] == rule)].set_index("q_variant")
            if not len(g):
                continue
            yv = np.array([g["pnl_per_placed_c"].get(c, np.nan) for c in xcat])
            lo = np.array([g["pnl_per_placed_ic_lo"].get(c, np.nan) for c in xcat])
            hi = np.array([g["pnl_per_placed_ic_hi"].get(c, np.nan) for c in xcat])
            off = (k - 2.5) * 0.06
            ax.errorbar(x + off, yv, yerr=[yv - lo, hi - yv], color=STRAT_COLORS[s], ls=ls, marker="o", ms=4, lw=1.4, capsize=2,
                        label=f"{STRAT_LABELS[s]} — {RULE_LABELS[rule]}")
            k += 1
    ax.axhline(0, color=TEXT_2, lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(xlab)
    ax.set_xlabel("file devant l'ordre à la pose, Q_ahead (parts)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("P&L par part placée (cents)", color=TEXT_2, fontsize=9)
    fig.legend(frameon=False, fontsize=7.5, labelcolor=TEXT, ncol=2, loc="lower center", bbox_to_anchor=(0.5, 0.005))
    _save(fig, path)
    return title


def plot_quintiles(qt: pd.DataFrame, path: Path) -> str:
    plt = _pyplot()
    labs = list(dict.fromkeys(qt["strategy_label"]))
    parts = []
    for lab in labs:
        g = qt[qt["strategy_label"] == lab]
        parts.append(f"{lab} : Q5 − Q1 {cents(g['q5_minus_q1_c'].iloc[0], 1)} ({ic(g['q5_minus_q1_ic_lo'].iloc[0], g['q5_minus_q1_ic_hi'].iloc[0], 1)})")
    sig = [lab for lab in labs if (qt.loc[qt["strategy_label"] == lab, "q5_minus_q1_ic_lo"].iloc[0] > 0) or (qt.loc[qt["strategy_label"] == lab, "q5_minus_q1_ic_hi"].iloc[0] < 0)]
    head = ("L'amplitude prévue par TimesFM ne change pas le P&L maker (aucun écart Q5 − Q1 significatif)" if not sig
            else f"L'amplitude prévue par TimesFM change le P&L maker pour : {', '.join(sig)}")
    title = head + " — " + " ; ".join(parts)
    sub = ("(d) P&L par part placée sur la 2e moitié, par quintile de σ_TimesFM (bornes fixées sur l'apprentissage 14/08–03/09 de "
           "timesfm_amplitude) ; Q1 = amplitude prévue la plus faible. Barres : IC 95 % par créneau de 15 min.")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 3.8
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - hh / H, bottom=1.0 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    x = np.arange(1, 6)
    w = 0.8 / max(1, len(labs))
    colors = [BLUE, ORANGE, AQUA, VIOLET]
    for i, lab in enumerate(labs):
        g = qt[qt["strategy_label"] == lab].set_index("quintile_train")
        yv = np.array([g["pnl_per_placed_c"].get(q, np.nan) for q in x])
        lo = np.array([g["pnl_per_placed_ic_lo"].get(q, np.nan) for q in x])
        hi = np.array([g["pnl_per_placed_ic_hi"].get(q, np.nan) for q in x])
        xx = x + (i - (len(labs) - 1) / 2) * w
        ax.bar(xx, yv, width=w * 0.9, color=colors[i % len(colors)], label=lab, zorder=2)
        ax.errorbar(xx, yv, yerr=[yv - lo, hi - yv], fmt="none", ecolor=TEXT, elinewidth=1.0, capsize=2, zorder=3)
    ax.axhline(0, color=TEXT_2, lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([f"Q{q}" for q in x])
    ax.set_xlabel("quintile d'amplitude prévue par TimesFM (Q1 = faible, Q5 = forte)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("P&L par part placée (cents)", color=TEXT_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_delays(delays: dict[str, np.ndarray], path: Path) -> str:
    plt = _pyplot()
    meds = {k: float(np.median(v)) for k, v in delays.items() if len(v)}
    title = "Délai entre la pose et l'exécution : " + " ; ".join(f"{k} médiane {fr(v, 0)} s" for k, v in meds.items())
    sub = "Fonction de répartition du délai (s) des ordres exécutés, 2e moitié ; l'horodatage est celui du bloc Polygon (≈ 1,5 s de résolution)."
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 3.6
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - hh / H, bottom=1.0 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    colors = [BLUE, ORANGE, AQUA, VIOLET]
    for (k, v), c in zip(delays.items(), colors):
        if not len(v):
            continue
        v = np.sort(v)
        ax.plot(v, np.arange(1, len(v) + 1) / len(v), color=c, lw=1.6, label=f"{k} (n = {n_(len(v))})")
    ax.set_xscale("log")
    from matplotlib.ticker import FixedLocator, NullFormatter, ScalarFormatter

    ticks = [3, 5, 10, 20, 30, 60, 120, 300, 600, 900]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("délai pose -> exécution (s, échelle log)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("part des ordres exécutés", color=TEXT_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="lower right")
    _save(fig, path)
    return title


# ---------------------------------------------------------------------------
# 6. README
# ---------------------------------------------------------------------------
def md(df: pd.DataFrame, cols: dict[str, str], fmts: dict) -> str:
    d = df[list(cols)].rename(columns=cols)
    return to_markdown(d, {cols.get(k, k): v for k, v in fmts.items()})


SUMMARY_COLS = {"n_orders": "ordres", "n_markets": "marchés", "n_crossing": "croisants", "fill_rate": "exécutés",
                "share_traversal": "via (i)", "delay_median_s": "délai médian (s)", "pnl_per_placed_c": "P&L / part placée (c)",
                "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut", "pnl_per_executed_c": "P&L / part exécutée (c)",
                "rebate_per_executed_c": "remise (c)", "win_rate_executed": "gain si exécuté", "win_rate_not_executed": "gain sinon",
                "adverse_selection_pts": "sél. adverse (pt)"}
SUMMARY_FMT = {"fill_rate": "0%", "share_traversal": "0%", "delay_median_s": 1, "pnl_per_placed_c": "+2", "pnl_per_placed_ic_lo": "+2",
               "pnl_per_placed_ic_hi": "+2", "pnl_per_executed_c": "+2", "rebate_per_executed_c": 2, "win_rate_executed": "0%",
               "win_rate_not_executed": "0%", "adverse_selection_pts": "+0"}


def write_readme(ctx: dict, out: Path) -> None:
    A, Bt, C, D, S, T = ctx["sum_a"], ctx["sum_b"], ctx["sum_c"], ctx["quint"], ctx["sens"], ctx["taker"]
    ch = ctx["chosen_a"]
    meta = ctx["meta"]
    n_te = int((~ctx["M"]["train"]).sum())
    n_tr = int(ctx["M"]["train"].sum())
    a_ch = A[(A["model"] == ch["model"]) & (A["label"] == ch["label"])].set_index("half")
    a_te = a_ch.loc["2e moitié"] if "2e moitié" in a_ch.index else None
    a_trn = a_ch.loc["1re moitié"] if "1re moitié" in a_ch.index else None
    b_te = Bt[Bt["half"] == "2e moitié"].set_index("label")
    b_ch = ctx["chosen_b"]
    b_row = b_te.loc[b_ch] if b_ch in b_te.index else None
    pairs = ctx["pairs"]
    c_te = C[(C["half"] == "2e moitié") & (C["duration"] == "toutes")].set_index("config") if len(C) else pd.DataFrame()
    c_main = c_te.loc["ewma1s"] if "ewma1s" in c_te.index else None
    cells = ctx["cells"]
    n_cells = len(cells)
    n_pos_raw = int(((cells["pnl_per_placed_ic_lo"] > 0)).sum())
    n_pos_adj = int(((cells["pnl_per_placed_ic_lo_adj"] > 0)).sum())
    n_neg_adj = int(((cells["pnl_per_placed_ic_hi_adj"] < 0)).sum())
    best = cells.sort_values("pnl_per_placed_c", ascending=False).iloc[0]
    tk = T.set_index("strategie")
    tk_hgb = tk.iloc[0]
    L = []
    L.append("# Le maker gagne-t-il ? — simulation historique sur les trades preneurs, Polymarket « Up or Down » BTC 5m / 15m\n")
    L.append(f"*Généré le {pd.Timestamp.now(tz='UTC'):%d/%m/%Y %H:%M} UTC par `scripts/polymarket_maker_backtest.py` "
             f"(temps total : {ctx['runtime']:.0f} s, caches compris — § 10). Marchés BTC 5m et 15m du "
             f"{ctx['args'].start:%d/%m/%Y} au {(ctx['args'].end - pd.Timedelta(seconds=1)):%d/%m/%Y} UTC : "
             f"{n_(meta['n_resolved'])} marchés résolus sur {n_(meta['n_expected'])} créneaux attendus ; "
             f"{n_(ctx['n_trades'])} trades preneurs ({fr(ctx['taker_volume'] / 1e6, 1)} M$). "
             f"1re moitié (paramètres) : {n_(n_tr)} marchés jusqu'au {(ctx['args'].mid - pd.Timedelta(seconds=1)):%d/%m} ; "
             f"2e moitié (test) : {n_(n_te)} marchés.*\n")
    L.append("> Simulation papier sur données publiques : aucun ordre, aucune clé. Nos ordres simulés ne modifient pas l'historique "
             "(pas de réaction des autres participants) ; les exécutions sont déduites des trades preneurs réels par le modèle du § 2.\n")
    # --- Résumé
    L.append("## 0. Résumé : le maker gagne-t-il ?\n")
    verdict = ("**Non, pas de façon démontrable.**" if n_pos_adj == 0 else f"**Peut-être : {n_pos_adj} configuration(s) résistent à Bonferroni.**")
    L.append(f"* {verdict} Sur les {n_(n_cells)} configurations testées ((a) {ctx['n_cells_a']}, (b) {ctx['n_cells_b']}, (c) {ctx['n_cells_c']}), "
             f"{n_pos_raw} ont un IC 95 % (par créneau de 15 min) entièrement positif sur la 2e moitié, {n_pos_adj} après correction de Bonferroni "
             f"(α = 5 %/{n_cells}) ; {n_neg_adj} sont significativement **perdantes** après correction. La meilleure a posteriori, {best['name']}, "
             f"fait {cents(best['pnl_per_placed_c'])} par part placée ({ic(best['pnl_per_placed_ic_lo'], best['pnl_per_placed_ic_hi'])}).")
    if a_te is not None:
        L.append(f"* **(a) Signal maker à S−30 s**, règle fixée d'avance (meilleur P&L par part placée sur la 1re moitié parmi 4 modèles × 4 prix × 4 annulations) : "
                 f"**{MODEL_LABELS[ch['model']]}, {ch['label']}**. 1re moitié : {cents(a_trn['pnl_per_placed_c'])} par part placée "
                 f"({n_(a_trn['n_orders'])} ordres, exécutés {pct(a_trn['fill_rate'], 0)}). **2e moitié : {cents(a_te['pnl_per_placed_c'])} par part placée "
                 f"({ic(a_te['pnl_per_placed_ic_lo'], a_te['pnl_per_placed_ic_hi'])}), {cents(a_te['pnl_per_executed_c'])} par part exécutée, "
                 f"exécutés {pct(a_te['fill_rate'], 0)}** ({n_(a_te['n_orders'])} ordres, {n_(a_te['n_crossing'])} croisants écartés), délai médian "
                 f"{fr(a_te['delay_median_s'], 0)} s, remise ≈ {fr(a_te['rebate_per_executed_c'], 2)} c. Sélection adverse : gain {pct(a_te['win_rate_executed'], 0)} si exécuté "
                 f"contre {pct(a_te['win_rate_not_executed'], 0)} sinon ({fr(a_te['adverse_selection_pts'], 0, signed=True)} points, "
                 f"{ic(a_te.get('adverse_selection_ic_lo'), a_te.get('adverse_selection_ic_hi'), 0, ' pt')}).")
    if b_row is not None:
        pr = pairs[pairs["label"] == b_ch].iloc[0] if (pairs["label"] == b_ch).any() else None
        L.append(f"* **(b) Deux côtés avant l'ouverture** ({b_ch}, choisi sur la 1re moitié) : 2e moitié {cents(b_row['pnl_per_placed_c'])} par part placée "
                 f"({ic(b_row['pnl_per_placed_ic_lo'], b_row['pnl_per_placed_ic_hi'])}), exécutés {pct(b_row['fill_rate'], 0)}, sélection adverse "
                 f"{fr(b_row['adverse_selection_pts'], 0, signed=True)} points"
                 + (f" ; par paire Up + Down posable : les deux exécutés {pct(pr['share_both'], 0)}, un seul {pct(pr['share_one'], 0)}, "
                    f"P&L {cents(pr['pnl_per_pair_c'])} par paire ({ic(pr['pnl_per_pair_ic_lo'], pr['pnl_per_pair_ic_hi'])})." if pr is not None else "."))
    if c_main is not None:
        c_t = c_te.loc["timesfm_S"] if "timesfm_S" in c_te.index else None
        c_e = c_te.loc["ewma_S"] if "ewma_S" in c_te.index else None
        L.append(f"* **(c) Juste valeur Φ(d/σ) ± 1 c en cours de fenêtre** (σ EWMA 1 s, demi-vie {fr(ctx['hl'], 0)} s choisie sur la 1re moitié) : "
                 f"2e moitié {cents(c_main['pnl_per_placed_c'])} par part placée ({ic(c_main['pnl_per_placed_ic_lo'], c_main['pnl_per_placed_ic_hi'])}), "
                 f"{cents(c_main['pnl_per_executed_c'])} par part exécutée, exécutés {pct(c_main['fill_rate'], 0)} ({n_(c_main['n_orders'])} ordres, "
                 f"{n_(c_main['n_crossing'])} croisants écartés), sélection adverse {fr(c_main['adverse_selection_pts'], 0, signed=True)} points ; "
                 f"P&L {fr(ctx['c_per_market']['ewma1s'], 2, signed=True)} $ par marché ({n_(ctx['args'].size)} parts par ordre)."
                 + (f" Avec **σ TimesFM** (prévu à S) : {cents(c_t['pnl_per_placed_c'])} ({ic(c_t['pnl_per_placed_ic_lo'], c_t['pnl_per_placed_ic_hi'])}) ; "
                    f"avec σ EWMA 1 min à S : {cents(c_e['pnl_per_placed_c'])}." if c_t is not None and c_e is not None else ""))
    if len(D):
        dl = []
        for lab in dict.fromkeys(D["strategy_label"]):
            g = D[D["strategy_label"] == lab].iloc[0]
            dl.append(f"{lab} : Q5 − Q1 {cents(g['q5_minus_q1_c'], 1)} ({ic(g['q5_minus_q1_ic_lo'], g['q5_minus_q1_ic_hi'], 1)})")
        L.append("* **(d) Filtre d'amplitude TimesFM** (quintiles fixés sur l'apprentissage de `timesfm_amplitude`) : " + " ; ".join(dl)
                 + ". " + ("Aucun écart significatif : l'amplitude prévue ne rend pas le maker gagnant, ni sur les marchés calmes ni sur les agités."
                           if not ctx["quint_sig"] else f"Écart significatif pour : {', '.join(ctx['quint_sig'])}."))
    if len(S):
        sa = S[(S["strategy"] == "signal")]
        s0 = sa[(sa["q_variant"] == "0") & (sa["rule"] == 0)]
        s5 = sa[(sa["q_variant"] == "5000") & (sa["rule"] == 0)]
        si = sa[(sa["q_variant"] == "measured") & (sa["rule"] == 1)]
        sm = sa[(sa["q_variant"] == "measured") & (sa["rule"] == 0)]
        if len(s0) and len(s5) and len(si) and len(sm):
            L.append(f"* **Sensibilité à la file et à la règle** ((a) choisie, 2e moitié) : Q_ahead = 0 (optimiste) -> exécutés {pct(s0['fill_rate'].iloc[0], 0)}, "
                     f"{cents(s0['pnl_per_placed_c'].iloc[0])} par part placée ; file mesurée -> {pct(sm['fill_rate'].iloc[0], 0)}, {cents(sm['pnl_per_placed_c'].iloc[0])} ; "
                     f"5 000 parts -> {pct(s5['fill_rate'].iloc[0], 0)}, {cents(s5['pnl_per_placed_c'].iloc[0])} ; règle (i) seule (traversée, conservatrice) -> "
                     f"{pct(si['fill_rate'].iloc[0], 0)}, {cents(si['pnl_per_placed_c'].iloc[0])}. "
                     f"{'Le signe ne dépend pas de la file : ' if (np.sign(s0['pnl_per_placed_c'].iloc[0]) == np.sign(s5['pnl_per_placed_c'].iloc[0])) else 'Le signe dépend de la file : '}"
                     f"un ordre au repos est surtout exécuté par **traversée** ({pct(sm['share_traversal'].iloc[0], 0)} des exécutions), c'est-à-dire quand un preneur "
                     f"pressé va au-delà de notre niveau — le moment où le signal a le plus de chances d'être faux.")
    L.append(f"* **Comparaison avec le preneur** (mêmes marchés, 2e moitié, 100 parts) : preneur `hgb_ind1s` à sa marge de `modeles_vs_marche` "
             f"{cents(tk_hgb['pnl_per_share_c'])} par part ({ic(tk_hgb['pnl_ic_lo'], tk_hgb['pnl_ic_hi'])}, {n_(tk_hgb['n_positions'])} positions, "
             f"avant le surcoût d'exécution de 1,3 à 3,1 c mesuré dans le diagnostic) ; borne haute maker (exécution supposée certaine) "
             f"{cents(tk.iloc[-1]['pnl_per_share_c'])} ({ic(tk.iloc[-1]['pnl_ic_lo'], tk.iloc[-1]['pnl_ic_hi'])}). "
             + (f"Le maker réel (a) fait {cents(a_te['pnl_per_placed_c'])} par part placée : la borne haute est inatteignable parce que l'ordre "
                f"n'est exécuté que {pct(a_te['fill_rate'], 0)} du temps, et surtout quand il a tort." if a_te is not None else ""))
    L.append(f"* **Ce que vaut le modèle d'exécution** : {n_(ctx['n_trades'])} trades preneurs réels, ordre chronologique on-chain, délai bloc de {ctx['args'].delay:.0f} s ; "
             f"la file d'attente vient des mesures sur le carnet réel (`maker_live`, 18 marchés) et l'exécution est tout ou rien. "
             f"Les traversées (règle (i)) ne dépendent pas de la file : la variante conservatrice « (i) seule » donne la borne la plus sûre ; "
             f"la variante optimiste (Q_ahead = 0) la borne haute. Les deux concluent dans le même sens (§ 8).")
    # --- Données
    L.append("\n## 1. Données\n")
    st = ctx["stats"]
    L.append(f"* **Trades preneurs** : cache `data/cache/polymarket/wallets/trades/` (`scripts/polymarket_collect_taker_trades.py`, "
             f"`/v2/trades` avec `taker_only=true` : une ligne par transaction, côté preneur, prix moyen d'exécution, horodatage du bloc Polygon à la seconde, "
             f"`seq` = ordre on-chain). {n_(len(st))} marchés, {n_(ctx['n_trades'])} trades, {fr(ctx['taker_volume'] / 1e6, 1)} M$ de notionnel preneur ; "
             f"par marché (médiane) : 5m {n_(ctx['med_trades']['5m'])} trades, 15m {n_(ctx['med_trades']['15m'])} ; "
             f"dans [S−60 s, S) : {n_(ctx['med_pre']['5m'])} / {n_(ctx['med_pre']['15m'])} trades ; dans [S, S+60 s) : {n_(ctx['med_post']['5m'])} / {n_(ctx['med_post']['15m'])}.")
    L.append(f"* **Marchés** : `PolymarketClient.list_updown_markets` (cache), issue officielle `outcomePrices`, barème `feeSchedule` "
             f"({ctx['fee_desc']}) ; `priceToBeat`/`finalPrice` du cache `timesfm_amplitude/event_meta.parquet` ; prédictions par marché de "
             f"`modeles_vs_marche/marches_predictions.csv` (`p_hgb_ind1s`, `p_logit_tw`, `tw_gap30`, prix Up à S−30 s `p_pre`) et prix Up à S−60 s "
             f"`p_pre60` (`prices-history`, pour la règle du milieu de (b)) ; "
             f"σ TimesFM et EWMA à S de `timesfm_amplitude/sigma_par_marche.csv` ({n_(ctx['n_sigma'])} marchés avec σ TimesFM).")
    L.append(f"* **Binance 1 s** (BTCUSDT, zips journaliers `data.binance.vision`, cache `data/cache/pm_maker/binance_1s/`) pour la juste valeur (c) ; "
             f"TWAP60(S) = moyenne des closes 1 s sur (S−60 s, S] (agrégats `pm_backtest/agg1s`). Écart de niveau log(TWAP60_Binance(S) / priceToBeat) : "
             f"moyenne {fr(ctx['level_gap_mean'], 1)} pb, écart-type {fr(ctx['level_gap_sd'], 1)} pb : on prend K = TWAP60_Binance(S) comme référence de d "
             f"(l'écart Binance–Chainlink est le même à S et à E, cf. diagnostic), pas le `priceToBeat` brut.")
    L.append(f"* **Périodes** : 1re moitié = fenêtres du {ctx['args'].start:%d/%m} au {(ctx['args'].mid - pd.Timedelta(seconds=1)):%d/%m} "
             f"({n_(n_tr)} marchés : choix des paramètres) ; 2e moitié = du {ctx['args'].mid:%d/%m} au {(ctx['args'].end - pd.Timedelta(seconds=1)):%d/%m} ({n_(n_te)} marchés : test). "
             f"IC 95 % : bootstrap groupé par créneau de 15 min (les marchés 5m et 15m d'un même créneau sont tirés ensemble), {n_(ctx['args'].B)} tirages.")
    # --- Modèle d'exécution
    L.append("\n## 2. Modèle d'exécution (documenté et testé)\n")
    L.append("Un ordre au repos « acheter Q parts de X à L » posé à t0 (annulé à t1) est exécuté, dans l'ordre chronologique des trades preneurs "
             "(horodatage du bloc puis `seq`), à la première des deux conditions :\n")
    L.append("* **(i) traversée** : un trade preneur dépasse strictement le niveau — vente preneur de X à un prix < L, ou achat preneur de l'autre jeton Y "
             "à un prix > 1 − L (complémentarité du CLOB : un bid X à L est un ask Y à 1 − L, servi par appariement « mint ») ; par priorité de prix, "
             "notre ordre est servi avant que le preneur n'aille plus loin ;")
    L.append("* **(ii) file** : le cumul depuis t0 des ventes preneurs de X à un prix ≤ L et des achats preneurs de Y à un prix ≥ 1 − L dépasse "
             "Q_ahead + Q (Q_ahead = file devant nous à la pose).\n")
    L.append(f"Variante conservatrice : (i) seule ; variante optimiste : Q_ahead = 0. Exécution **tout ou rien**. **Délai on-chain** : l'ordre n'est visible "
             f"qu'après ≈ 0,3 s et un trade n'est daté que par son bloc (≈ 2,2 s après l'appariement en médiane) : l'ordre ne voit que les trades dont "
             f"le bloc est dans [t0 + {ctx['args'].delay:.0f} s, t1 + {ctx['args'].delay:.0f} s). **Ordres croisants** (sans carnet historique) : un achat preneur de X à un "
             f"prix ≤ L (ou une vente preneur de Y à ≥ 1 − L) dans les {ctx['args'].lookback:.0f} s précédant la pose prouve que l'ask de X était ≤ L : l'ordre serait un "
             f"ordre preneur (frais, exécution immédiate) et il est écarté ; avant l'ouverture s'y ajoute la règle du milieu `prices-history` "
             f"(L ≥ milieu_X + 0,005, milieu connu à la pose : S−30 s pour (a), S−60 s pour (b)). **Résolution** : 1 $ par part si X gagne ; P&L par part = 1{{gagné}} − L, sans frais. **Remise maker** estimée à part : "
             f"0,2 × frais preneur au prix L (≈ 0,35 c à 0,50), plafonnée à 20 % des frais preneurs du marché.\n")
    L.append("**File d'attente Q_ahead** (parts) : médiane de la taille affichée au niveau quand il est présent, mesurée sur le carnet réel "
             "(`reports/polymarket/maker_live/README.md` § 3, 18 marchés du 26/09) ; 0,47 non mesuré = valeur de 0,48 ; en cours de fenêtre (c) : 100 parts.\n")
    qa = pd.DataFrame([{"phase": ph, "côté": sd, **{f"{lv:.2f}": mk.MEASURED_Q_AHEAD[(ph, sd, lv)] for lv in mk.SIGNAL_PRICES}}
                       for ph in ("pre", "post") for sd in ("up", "down")])
    qa["phase"] = qa["phase"].map({"pre": "avant S", "post": "après S"})
    L.append(to_markdown(qa, 0) + "\n")
    L.append("Tests unitaires (`tests/test_polymarket_maker.py`) : traversée, cumul, complémentarité Up/Down, annulation, délai, croisement, tout ou rien, "
             "P&L et remise, absence de fuite (l'ordre ne voit rien après t0 sauf les trades de sa fenêtre ; l'issue n'entre qu'au P&L), juste valeur, re-cotation, cache.\n")
    # --- Stratégies
    L.append("\n## 3. Stratégies et protocole\n")
    L.append(f"* **(a) signal** : à S−30 s, côté favorisé par le modèle (`hgb_ind1s` : Up ssi p ≥ 0,5 + m, Down ssi p ≤ 0,5 − m, m ∈ {{0, 2 c, 5 c}} ; "
             f"`gap_m30` : signe du TWAP partiel), prix L ∈ {{0,47 ; 0,48 ; 0,49 ; 0,50}}, annulation à S, S+30 s, S+60 s ou E, Q = {n_(ctx['args'].size)} parts. "
             f"Règle fixée d'avance : la configuration (modèle, L, annulation) au meilleur P&L par part placée sur la 1re moitié (≥ 200 ordres) est évaluée sur la 2e.")
    L.append("* **(b) deux côtés** : achat Up et achat Down au même prix (0,49 ou 0,48) posés à S−60 s, annulés à S ou S+30 s ; P&L par paire = écart capté "
             "(2 c à 0,49) si les deux sont exécutés, position directionnelle sinon.")
    L.append(f"* **(c) juste valeur** : de S+30 s à E−60 s, toutes les 10 s, p̂ = Φ(d/σ_restant) avec d = log(spot Binance 1 s / TWAP60(S)) (close de la bougie 1 s close à l'instant de cotation) et "
             f"var_restant = σ_1s² × ((E − 60 − t) + 20) ; bid Up à p̂ − 1 c et bid Down à (1 − p̂) − 1 c (cent inférieur, niveaux dans [0,05 ; 0,95]), ordre conservé "
             f"tant que le niveau ne change pas, sinon remplacé ; positions cumulées réglées à la résolution. σ : EWMA des rendements 1 s (demi-vie choisie sur la 1re "
             f"moitié parmi {{30, 60, 120, 300, 600, 1 200, 1 800, 3 600}} s par log-loss de p̂ contre l'issue : **{fr(ctx['hl'], 0)} s**), ou σ TimesFM prévu à S "
             f"(σ_1s = σ_TimesFM / √(D + 60 s)), ou σ EWMA 1 min à S (le comparateur de `timesfm_amplitude`).")
    L.append("* **(d) filtre d'amplitude** : (a) et (b) choisies, restreintes aux quintiles de σ_TimesFM (bornes de l'apprentissage 14/08–03/09).")
    L.append(f"* **Tests multiples** : {n_cells} cellules évaluées sur la 2e moitié ; IC corrigés de Bonferroni (α = 5 %/{n_cells}) donnés en plus des IC bruts. "
             "Les résumés « 1re moitié » sont en échantillon pour les choix de paramètres.\n")
    # --- (a)
    L.append("\n## 4. (a) Signal maker à S−30 s\n")
    L.append(f"![{ctx['fig_titles']['configs']}](pnl_par_configuration.png)\n")
    L.append(f"Configuration choisie sur la 1re moitié : **{MODEL_LABELS[ch['model']]}, {ch['label']}** (file mesurée, règle (i)+(ii)).\n")
    if a_trn is not None and a_te is not None:
        L.append(md(a_ch.reset_index(), {"half": "moitié", **SUMMARY_COLS}, SUMMARY_FMT) + "\n")
    L.append("Toutes les configurations (a), 2e moitié, par modèle (`resume_configurations.csv` pour les deux moitiés) : P&L par part placée et IC bruts / corrigés.\n")
    at = A[A["half"] == "2e moitié"].copy()
    at["modèle"] = at["model"].map(MODEL_LABELS)
    L.append(md(at, {"modèle": "modèle", "label": "config.", "n_orders": "ordres", "n_crossing": "croisants", "fill_rate": "exécutés",
                     "pnl_per_placed_c": "P&L / part placée (c)", "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut",
                     "pnl_per_placed_ic_lo_adj": "IC bas (Bonf.)", "pnl_per_placed_ic_hi_adj": "IC haut (Bonf.)", "pnl_per_executed_c": "P&L / part exécutée (c)",
                     "adverse_selection_pts": "sél. adverse (pt)"},
               {"fill_rate": "0%", "pnl_per_placed_c": "+2", "pnl_per_placed_ic_lo": "+2", "pnl_per_placed_ic_hi": "+2", "pnl_per_placed_ic_lo_adj": "+2",
                "pnl_per_placed_ic_hi_adj": "+2", "pnl_per_executed_c": "+2", "adverse_selection_pts": "+0"}) + "\n")
    L.append("Configuration choisie par durée et par côté (2e moitié) :\n")
    L.append(md(ctx["a_cells"], {"cellule": "cellule", **SUMMARY_COLS}, SUMMARY_FMT) + "\n")
    # --- (b)
    L.append("\n## 5. (b) Deux côtés avant l'ouverture\n")
    L.append(md(Bt, {"half": "moitié", "label": "config.", **SUMMARY_COLS}, SUMMARY_FMT) + "\n")
    L.append("Par **paire** Up + Down (marchés où les deux ordres sont posables), 2e moitié :\n")
    L.append(md(pairs, {"label": "config.", "n_pairs": "paires", "share_both": "les deux exécutés", "share_one": "un seul", "share_none": "aucun",
                        "pnl_per_pair_c": "P&L / paire (c)", "pnl_per_pair_ic_lo": "IC bas", "pnl_per_pair_ic_hi": "IC haut"},
               {"share_both": "0%", "share_one": "0%", "share_none": "0%", "pnl_per_pair_c": "+2", "pnl_per_pair_ic_lo": "+2", "pnl_per_pair_ic_hi": "+2"}) + "\n")
    L.append("Par côté acheté (2e moitié) :\n")
    L.append(md(ctx["b_sides"], {"label": "config.", "side": "côté", **SUMMARY_COLS}, SUMMARY_FMT) + "\n")
    # --- (c)
    L.append("\n## 6. (c) Juste valeur Φ(d/σ) ± 1 c en cours de fenêtre\n")
    if not len(C):
        L.append("Stratégie (c) non simulée (`--skip-fv`).\n")
    L.append("Qualité de la juste valeur aux instants de cotation (log-loss et Brier de p̂ contre l'issue, moyenne par marché ; « 1re moitié » sert au choix de la demi-vie) :\n")
    fq = ctx["fv_quality"]
    if len(fq):
        fq = fq[fq["duration"] == "toutes"].pivot(index="variant", columns="half", values=["log_loss", "brier"])
        fq.columns = [f"{a} {b}" for a, b in fq.columns]
        fq = fq.reset_index()
        L.append(to_markdown(fq, 4) + "\n")
    if len(C):
        L.append("P&L des cotations (toutes durées puis par durée) ; `fair_value_par_marche.csv` donne le détail par marché :\n")
        L.append(md(C.assign(config=C["config"].map(lambda v: FV_LABELS.get(v, v))), {"half": "moitié", "config": "σ", "duration": "durée", **SUMMARY_COLS}, SUMMARY_FMT) + "\n")
    if c_main is not None:
        L.append(f"Exposition : sur la 2e moitié, un marché reçoit en médiane {n_(ctx['c_exposure']['orders'])} ordres (c) et {n_(ctx['c_exposure']['exec'])} exécutions "
             f"({n_(ctx['args'].size)} parts chacune) ; position nette médiane |Up − Down| = {n_(ctx['c_exposure']['net'])} parts ; "
             f"P&L par marché : moyenne {fr(ctx['c_per_market']['ewma1s'], 2, signed=True)} $, p10 {fr(ctx['c_exposure']['p10'], 1, signed=True)} $, p90 {fr(ctx['c_exposure']['p90'], 1, signed=True)} $.\n")
    # --- (d)
    L.append("\n## 7. (d) Filtre d'amplitude TimesFM\n")
    if len(D):
        L.append(f"![{ctx['fig_titles']['quint']}](amplitude_quintiles.png)\n")
        L.append(md(D, {"strategy_label": "stratégie", "quintile_train": "quintile", "n_orders": "ordres", "fill_rate": "exécutés", "pnl_per_placed_c": "P&L / part placée (c)",
                        "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut", "adverse_selection_pts": "sél. adverse (pt)", "q5_minus_q1_c": "Q5 − Q1 (c)",
                        "q5_minus_q1_ic_lo": "IC bas", "q5_minus_q1_ic_hi": "IC haut"},
                   {"fill_rate": "0%", "pnl_per_placed_c": "+2", "pnl_per_placed_ic_lo": "+2", "pnl_per_placed_ic_hi": "+2", "adverse_selection_pts": "+0",
                    "q5_minus_q1_c": "+2", "q5_minus_q1_ic_lo": "+2", "q5_minus_q1_ic_hi": "+2"}) + "\n")
        if not ctx["quint_sig"]:
            L.append("Lecture : l'amplitude prévue trie le mouvement réalisé (`timesfm_amplitude`, test 3) mais pas la justesse des signaux ; côté maker, elle change "
                     "le taux d'exécution (plus de traversées quand ça bouge) et donc l'exposition, pas le signe du P&L : aucun écart Q5 − Q1 n'est significatif.\n")
        else:
            L.append(f"Lecture : l'écart Q5 − Q1 est significatif pour {', '.join(ctx['quint_sig'])} (2 stratégies × 1 contraste, sans correction) ; "
                     "l'amplitude prévue change le taux d'exécution (plus de traversées quand ça bouge) et donc l'exposition. À confirmer sur une autre période avant d'en faire un filtre.\n")
    else:
        L.append("σ TimesFM absente : filtre non évalué.\n")
    # --- Sensibilité
    L.append("\n## 8. Sensibilité : file d'attente, règle d'exécution, délai\n")
    L.append(f"![{ctx['fig_titles']['sens']}](sensibilite_file.png)\n")
    Sx = S.copy()
    Sx["stratégie"] = Sx["strategy"].map(lambda s: STRAT_LABELS[s].split(")")[0] + ")")
    Sx["file"] = Sx["q_variant"].map(Q_LABELS)
    Sx["règle"] = Sx["rule"].map(RULE_LABELS)
    L.append(md(Sx, {"stratégie": "stratégie", "file": "Q_ahead", "règle": "règle", "n_orders": "ordres", "fill_rate": "exécutés", "share_traversal": "via (i)",
                     "pnl_per_placed_c": "P&L / part placée (c)", "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut",
                     "pnl_per_executed_c": "P&L / part exécutée (c)", "adverse_selection_pts": "sél. adverse (pt)"},
               {"fill_rate": "0%", "share_traversal": "0%", "pnl_per_placed_c": "+2", "pnl_per_placed_ic_lo": "+2", "pnl_per_placed_ic_hi": "+2",
                "pnl_per_executed_c": "+2", "adverse_selection_pts": "+0"}) + "\n")
    L.append(f"![{ctx['fig_titles']['delays']}](delai_execution.png)\n")
    L.append(f"Délais (2e moitié, ordres exécutés) : {ctx['delay_text']}. Le délai on-chain de {ctx['args'].delay:.0f} s est fixé (pas de re-simulation à 0 ou 6 s : "
             "les fenêtres [t0 + δ, t1 + δ) se décalent d'autant, ce qui ne change que les exécutions à moins de 3 s de la pose ou de l'annulation).\n")
    # --- preneur
    L.append("\n## 9. Comparaison avec le preneur (mêmes marchés, 2e moitié)\n")
    L.append(f"![{ctx['fig_titles']['cum']}](pnl_cumule_test.png)\n")
    L.append(md(T, {"strategie": "stratégie", "n_positions": "positions", "win_rate": "taux de gain", "pnl_per_share_c": "P&L / part (c)", "pnl_ic_lo": "IC bas",
                    "pnl_ic_hi": "IC haut", "pnl_total_usd": f"P&L total ($, {n_(ctx['args'].size)} parts)"},
               {"win_rate": "1%", "pnl_per_share_c": "+2", "pnl_ic_lo": "+2", "pnl_ic_hi": "+2", "pnl_total_usd": 0}) + "\n")
    L.append(md(ctx["maker_vs_taker"], {"strategie": "stratégie", "n_positions": "positions / ordres", "pnl_per_share_c": "P&L / part placée (c)", "pnl_ic_lo": "IC bas",
                                        "pnl_ic_hi": "IC haut", "pnl_total_usd": f"P&L total ($, {n_(ctx['args'].size)} parts)"},
               {"pnl_per_share_c": "+2", "pnl_ic_lo": "+2", "pnl_ic_hi": "+2", "pnl_total_usd": 0}) + "\n")
    L.append("Le preneur paie les frais (≈ 1,75 c à 0,50) et, en réalité, 1,3 à 3,1 c de plus que l'ask supposé (diagnostic) ; le maker ne paie rien mais n'est exécuté "
             "que si un preneur vient le chercher — surtout par traversée, quand le carnet part dans l'autre sens. La « borne haute maker » de `modeles_vs_marche` "
             "(exécution certaine au bid) mesurait le demi-écart capté ; ici, l'exécution réelle le rend au marché.\n")
    # --- limites
    L.append("\n## 10. Limites\n")
    L.append("* **Tout ou rien** : pas d'exécution partielle ; un ordre de 100 parts est soit entièrement servi, soit pas du tout (les vrais ordres sont souvent servis par morceaux).")
    L.append("* **File estimée** : Q_ahead vient de 18 marchés d'une seule matinée (`maker_live`), appliquée uniformément ; les annulations devant nous ne sont pas vues "
             "(borne haute de la file), la règle (i) seule et Q_ahead = 0 encadrent le résultat.")
    L.append("* **Pas de réaction des autres participants** à notre présence (ni des makers concurrents, ni des preneurs) ; nos ordres n'altèrent pas l'historique.")
    L.append("* **Carnet inconnu** : les ordres croisants ne sont détectés que par les trades (preuve d'ask) et par le milieu `prices-history` (ancienneté médiane 17 s) ; "
             "une partie des « traversées » sont sans doute des ordres qui auraient été preneurs (exécution immédiate avec frais).")
    L.append("* **Horodatage au bloc** (≈ 1,5 s de résolution, ≈ 2,2 s après l'appariement) et délai fixe de 3 s ; le prix d'un trade est le prix moyen du preneur.")
    L.append("* **Juste valeur (c)** : gaussienne sans saut, K = TWAP60 Binance (≈ 3 pb au-dessus de Chainlink, corrigé par construction), Binance en avance de ≈ 4 s sur Chainlink non modélisée.")
    L.append("* **Tests multiples** : 71 cellules ; la meilleure a posteriori est optimiste, seule la règle fixée d'avance (§ 4) compte. Une seule période de 21 jours, un seul actif.\n")
    # --- fichiers
    L.append("\n## 11. Fichiers, temps d'exécution et relance\n")
    files = [("marches.csv", "un marché par ligne : issue, prix S−30 s, côtés par modèle, σ TimesFM/EWMA, quintile, TWAP60(S), priceToBeat, trades preneurs (total, [S−60, S), [S, S+60))"),
             ("resume_configurations.csv", "toutes les cellules (a), (b), (c) par moitié : exécution, P&L, IC bruts et Bonferroni, sélection adverse, délais"),
             ("resume_par_cellule.csv", "configuration (a) choisie par durée et par côté"),
             ("paires_two_sided.csv", "(b) par paire Up + Down"), ("resume_sensibilite.csv", "file × règle pour (a), (b), (c)"),
             ("resume_quintiles.csv", "(d) par quintile d'amplitude TimesFM"), ("juste_valeur_qualite.csv", "log-loss / Brier de p̂ par variante de σ"),
             ("fair_value_par_marche.csv", "(c) agrégats par marché et configuration"), ("comparaison_preneur.csv", "preneur / borne haute maker sur les mêmes marchés"),
             ("pnl_cumule.csv", "séries cumulées de la figure"), ("ordres_choisis.csv", "ordres (a) et (b) des configurations choisies, un par ligne"),
             ("runtime.csv", "temps par étape")]
    L.append(to_markdown(pd.DataFrame(files, columns=["fichier", "contenu"])) + "\n")
    L.append(to_markdown(ctx["runtime_df"], {"secondes": 1}) + "\n")
    L.append("```bash\n. .venv/bin/activate\npython scripts/polymarket_maker_backtest.py            # --workers 3 --B 2000 par défaut ; --skip-fv pour sauter (c) ; --max-markets 400 pour un essai\npython -m pytest tests/test_polymarket_maker.py -q\n```\n")
    (out / "README.md").write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2026-09-04", help="début (UTC) des fenêtres")
    ap.add_argument("--end", default="2026-09-25", help="fin exclue (UTC)")
    ap.add_argument("--mid", default="2026-09-14", help="début de la 2e moitié (test)")
    ap.add_argument("--assets", default="btc")
    ap.add_argument("--durations", default="5m,15m")
    ap.add_argument("--size", type=float, default=mk.DEFAULT_SIZE, help="parts par ordre")
    ap.add_argument("--delay", type=float, default=mk.ONCHAIN_DELAY_S, help="délai on-chain (s)")
    ap.add_argument("--lookback", type=float, default=mk.CROSS_LOOKBACK_S, help="fenêtre de détection des ordres croisants (s)")
    ap.add_argument("--B", type=int, default=2000, help="tirages bootstrap")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-markets", type=int, default=0, help="sous-échantillon régulier (essai)")
    ap.add_argument("--skip-fv", action="store_true", help="sauter la stratégie (c)")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--sim-cache", default="", help="répertoire parquet des résultats de simulation (relus s'ils existent)")
    a = ap.parse_args(argv)
    a.start = pd.Timestamp(a.start, tz="UTC")
    a.end = pd.Timestamp(a.end, tz="UTC")
    a.mid = pd.Timestamp(a.mid, tz="UTC")
    a.assets = [s.strip() for s in a.assets.split(",") if s.strip()]
    a.durations = [s.strip() for s in a.durations.split(",") if s.strip()]
    return a


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rt = Runtime()
    M, meta = load_markets(args, rt)
    with rt("2a. Binance 1 s (zips journaliers, cache) et EWMA"):
        sec, close, sig_hl = load_binance_1s(args, SYMBOL[args.assets[0]])
    hl, fv_quality = fair_value_quality(M, sec, close, sig_hl, rt) if not args.skip_fv else (60.0, pd.DataFrame())
    cache = Path(args.sim_cache) if args.sim_cache else None
    if cache is not None and (cache / "base.parquet").exists():
        with rt("3. simulation : relecture du cache parquet (--sim-cache)"):
            base = pd.read_parquet(cache / "base.parquet")
            fv = pd.read_parquet(cache / "fv.parquet") if (cache / "fv.parquet").exists() else pd.DataFrame()
            fv_agg = pd.read_parquet(cache / "fv_agg.parquet") if (cache / "fv_agg.parquet").exists() else pd.DataFrame()
            stats = pd.read_parquet(cache / "stats.parquet")
            M = M[M["slug"].isin(stats["slug"])].reset_index(drop=True)
    else:
        base, fv, fv_agg, stats = run_simulation(M, args, sec, close, sig_hl, hl, rt)
        if cache is not None:
            cache.mkdir(parents=True, exist_ok=True)
            base.to_parquet(cache / "base.parquet")
            stats.to_parquet(cache / "stats.parquet")
            if len(fv):
                fv.to_parquet(cache / "fv.parquet")
            if len(fv_agg):
                fv_agg.to_parquet(cache / "fv_agg.parquet")

    with rt("4. analyse : expansion, P&L, bootstrap"):
        M = M.merge(stats, on="slug", how="left")
        base = attach(base, M)
        A_df = signal_table(base, args.size, args.delay)
        B_df = two_sided_table(base, args.size, args.delay)
        n_cells_a = len(MODELS) * len(mk.SIGNAL_PRICES) * len(mk.SIGNAL_CANCELS)
        n_cells_b = len(mk.TWO_SIDED_PRICES) * len(mk.TWO_SIDED_CANCELS)
        n_cells_c = len(FV_MAIN) if len(fv) else 0
        n_cells = n_cells_a + n_cells_b + n_cells_c
        alpha_adj = 0.05 / n_cells
        sum_a = summarize_halves(A_df, ["model", "label"], args.B, args.seed, alpha_adj)
        sum_b = summarize_halves(B_df, ["label"], args.B, args.seed, alpha_adj)
        chosen_a = choose_config(sum_a, ["model", "label"])
        chosen_b = choose_config(sum_b, ["label"])["label"]
        A_ch = A_df[(A_df["model"] == chosen_a["model"]) & (A_df["label"] == chosen_a["label"])].reset_index(drop=True)
        B_ch = B_df[B_df["label"] == chosen_b].reset_index(drop=True)
        # (a) choisie par durée / côté
        te = A_ch[A_ch["half"] == "2e moitié"].reset_index(drop=True)
        cells = []
        for name, mask in (("Tous", np.ones(len(te), bool)), ("BTC 5m", (te["duration"] == "5m").to_numpy()), ("BTC 15m", (te["duration"] == "15m").to_numpy()),
                           ("côté Up", (te["side"] == "up").to_numpy()), ("côté Down", (te["side"] == "down").to_numpy())):
            g = te[mask].reset_index(drop=True)
            if not len(g):
                continue
            s = mk.summarize(g, by=["strategy"], boot=pb.SlotBootstrap(g["slot"].to_numpy(), B=args.B, seed=args.seed))
            s.insert(0, "cellule", name)
            cells.append(s)
        a_cells = pd.concat(cells, ignore_index=True)
        # (b) paires et côtés
        Bte = B_df[B_df["half"] == "2e moitié"].reset_index(drop=True)
        boot_b = pb.SlotBootstrap(Bte["slot"].to_numpy(), B=args.B, seed=args.seed)
        pairs = mk.pair_summary(Bte, by=["label"], boot=boot_b)
        b_sides = mk.summarize(Bte, by=["label", "side"], boot=boot_b)
        # (c)
        if len(fv):
            fvm = attach(fv, M)
            sum_c_all = summarize_halves(fvm, ["config"], args.B, args.seed, alpha_adj)
            sum_c_all["duration"] = "toutes"
            sum_c_dur = summarize_halves(fvm, ["config", "duration"], args.B, args.seed)
            sum_c = pd.concat([sum_c_all, sum_c_dur], ignore_index=True)
            sum_c["config"] = pd.Categorical(sum_c["config"], categories=list(FV_MAIN), ordered=True)
            sum_c = sum_c.sort_values(["half", "duration", "config"]).reset_index(drop=True)
            sum_c["config"] = sum_c["config"].astype(str)
            agg_te = fv_agg.merge(M[["slug", "train"]], on="slug").query("not train")
            c_per_market = agg_te.groupby("config")["pnl_usd"].mean().to_dict()
            ge = agg_te[agg_te["config"] == "ewma1s"]
            c_exposure = {"orders": ge["n_orders"].median(), "exec": ge["n_exec"].median(), "net": (ge["net_up"] - ge["net_down"]).abs().median(),
                          "p10": ge["pnl_usd"].quantile(0.1), "p90": ge["pnl_usd"].quantile(0.9)}
        else:
            fvm, sum_c, c_per_market, c_exposure = pd.DataFrame(), pd.DataFrame(), {}, {}
        # sensibilité (2e moitié)
        sens = []
        for qv in Q_VARIANTS:
            for rule in (0, 1):
                a_s = signal_table(base, args.size, args.delay, models={chosen_a["model"]: MODELS[chosen_a["model"]]}, q_variant=qv, rule=rule)
                a_s = a_s[(a_s["label"] == chosen_a["label"]) & (a_s["half"] == "2e moitié")].reset_index(drop=True)
                b_s = two_sided_table(base, args.size, args.delay, q_variant=qv, rule=rule)
                b_s = b_s[(b_s["label"] == chosen_b) & (b_s["half"] == "2e moitié")].reset_index(drop=True)
                for d in (a_s, b_s):
                    if len(d):
                        s = mk.summarize(d, by=["strategy"], boot=pb.SlotBootstrap(d["slot"].to_numpy(), B=args.B, seed=args.seed))
                        s["q_variant"], s["rule"] = qv, rule
                        sens.append(s)
        if len(fv_agg):
            cfg_of = {"ewma1s|q0": ("0", 0), "ewma1s": ("measured", 0), "ewma1s|q500": ("500", 0), "ewma1s|q2000": ("2000", 0),
                      "ewma1s|q5000": ("5000", 0), "ewma1s|(i)": ("measured", 1)}
            ag = fv_agg.merge(M[["slug", "train"]], on="slug").query("not train")
            cs = agg_summary(ag[ag["config"].isin(cfg_of)], M, ["config"], args.B, args.seed)
            cs["strategy"] = "fair_value"
            cs["q_variant"] = cs["config"].map(lambda c: cfg_of[c][0])
            cs["rule"] = cs["config"].map(lambda c: cfg_of[c][1])
            sens.append(cs.drop(columns=["config"]))
            m2 = agg_summary(ag[ag["config"] == "ewma1s|m2c"], M, ["config"], args.B, args.seed)
        else:
            m2 = pd.DataFrame()
        sens = pd.concat(sens, ignore_index=True)
        # (d)
        quint = pd.concat([quintile_table(A_ch, args.B, args.seed, f"(a) {chosen_a['label']}"), quintile_table(B_ch, args.B, args.seed, f"(b) {chosen_b}")],
                          ignore_index=True)
        quint_sig = [lab for lab in dict.fromkeys(quint["strategy_label"]) if (quint.loc[quint["strategy_label"] == lab, "q5_minus_q1_ic_lo"].iloc[0] > 0)
                     or (quint.loc[quint["strategy_label"] == lab, "q5_minus_q1_ic_hi"].iloc[0] < 0)] if len(quint) else []
        # preneur
        taker = taker_comparison(M, args.size, args.B, args.seed)
        # toutes les cellules (2e moitié) pour la figure et le bilan
        ca = sum_a[sum_a["half"] == "2e moitié"].copy()
        ca["name"] = "(a) " + ca["model"].map(MODEL_SHORT) + " " + ca["label"]
        ca["strategy"] = "signal"
        cb = sum_b[sum_b["half"] == "2e moitié"].copy()
        cb["name"] = "(b) " + cb["label"]
        cb["strategy"] = "two_sided"
        parts = [ca, cb]
        if len(sum_c):
            cc = sum_c[(sum_c["half"] == "2e moitié") & (sum_c["duration"] == "toutes")].copy()
            cc["name"] = "(c) " + cc["config"].map(lambda v: FV_LABELS.get(v, v))
            cc["strategy"] = "fair_value"
            parts.append(cc)
        cells_all = pd.concat(parts, ignore_index=True)
        for c in ("pnl_per_placed_ic_lo_adj", "pnl_per_placed_ic_hi_adj"):
            if c not in cells_all.columns:
                cells_all[c] = np.nan
        # cumul dans le temps
        te_M = M[~M["train"]].sort_values("start_ts")
        idx_t = pd.to_datetime(te_M["start_ts"], unit="s", utc=True)
        series = {}
        a_te_m = A_ch[A_ch["half"] == "2e moitié"].groupby("slug")["pnl_order"].sum()
        series[f"(a) {MODEL_SHORT[chosen_a['model']]} {chosen_a['label']}"] = pd.Series(te_M["slug"].map(a_te_m).fillna(0.0).cumsum().to_numpy(), index=idx_t)
        b_te_m = B_ch[B_ch["half"] == "2e moitié"].groupby("slug")["pnl_order"].sum()
        series[f"(b) {chosen_b}"] = pd.Series(te_M["slug"].map(b_te_m).fillna(0.0).cumsum().to_numpy(), index=idx_t)
        if len(fv_agg):
            c_m = fv_agg[fv_agg["config"] == "ewma1s"].set_index("slug")["pnl_usd"]
            series["(c) juste valeur ± 1 c"] = pd.Series(te_M["slug"].map(c_m).fillna(0.0).cumsum().to_numpy(), index=idx_t)
        tk0 = taker.iloc[0]
        tk_series = pd.Series(tk0["pnl_series"], index=M[~M["train"]].index).reindex(te_M.index).fillna(0.0)
        series[f"preneur hgb_ind1s (marge {100 * TAKER_MARGINS['hgb_ind1s'][1]:.2f} c)"] = pd.Series(tk_series.cumsum().to_numpy(), index=idx_t)
        # délais
        delays = {f"(a) {chosen_a['label']}": A_ch.loc[(A_ch["half"] == "2e moitié") & A_ch["executed"], "delay_fill_s"].to_numpy(dtype=float),
                  f"(b) {chosen_b}": B_ch.loc[(B_ch["half"] == "2e moitié") & B_ch["executed"], "delay_fill_s"].to_numpy(dtype=float)}
        if len(fvm):
            delays["(c) juste valeur"] = fvm.loc[(fvm["half"] == "2e moitié") & fvm["executed"] & (fvm["config"] == "ewma1s"), "delay_fill_s"].to_numpy(dtype=float)
        delay_text = " ; ".join(f"{k} : médiane {fr(np.median(v), 0)} s, p90 {fr(np.quantile(v, 0.9), 0)} s (n = {n_(len(v))})" for k, v in delays.items() if len(v))
        # maker vs preneur, tableau commun
        mvt = []
        a_te_row = sum_a[(sum_a["half"] == "2e moitié") & (sum_a["model"] == chosen_a["model"]) & (sum_a["label"] == chosen_a["label"])].iloc[0]
        mvt.append({"strategie": f"maker (a) {MODEL_SHORT[chosen_a['model']]} {chosen_a['label']}", "n_positions": a_te_row["n_orders"], "pnl_per_share_c": a_te_row["pnl_per_placed_c"],
                    "pnl_ic_lo": a_te_row["pnl_per_placed_ic_lo"], "pnl_ic_hi": a_te_row["pnl_per_placed_ic_hi"], "pnl_total_usd": a_te_row["pnl_total_usd"]})
        b_te_row = sum_b[(sum_b["half"] == "2e moitié") & (sum_b["label"] == chosen_b)].iloc[0]
        mvt.append({"strategie": f"maker (b) {chosen_b}", "n_positions": b_te_row["n_orders"], "pnl_per_share_c": b_te_row["pnl_per_placed_c"],
                    "pnl_ic_lo": b_te_row["pnl_per_placed_ic_lo"], "pnl_ic_hi": b_te_row["pnl_per_placed_ic_hi"], "pnl_total_usd": b_te_row["pnl_total_usd"]})
        if len(sum_c):
            c_row = sum_c[(sum_c["half"] == "2e moitié") & (sum_c["duration"] == "toutes") & (sum_c["config"] == "ewma1s")].iloc[0]
            mvt.append({"strategie": "maker (c) juste valeur ± 1 c (σ EWMA 1 s)", "n_positions": c_row["n_orders"], "pnl_per_share_c": c_row["pnl_per_placed_c"],
                        "pnl_ic_lo": c_row["pnl_per_placed_ic_lo"], "pnl_ic_hi": c_row["pnl_per_placed_ic_hi"], "pnl_total_usd": c_row["pnl_total_usd"]})
        for _, r in taker.iterrows():
            mvt.append({"strategie": r["strategie"], "n_positions": r["n_positions"], "pnl_per_share_c": r["pnl_per_share_c"], "pnl_ic_lo": r["pnl_ic_lo"],
                        "pnl_ic_hi": r["pnl_ic_hi"], "pnl_total_usd": r["pnl_total_usd"]})
        maker_vs_taker = pd.DataFrame(mvt)

    with rt("5. figures"):
        rows_plot = cells_all[(cells_all["strategy"] != "signal") | (cells_all["model"] == chosen_a["model"])]
        fig_titles = {"configs": plot_configs(rows_plot, cells_all, out / "pnl_par_configuration.png", int((~M["train"]).sum()), MODEL_LABELS[chosen_a["model"]]),
                      "cum": plot_cumulative(series, out / "pnl_cumule_test.png", args.size),
                      "sens": plot_sensitivity(sens, out / "sensibilite_file.png"),
                      "quint": plot_quintiles(quint, out / "amplitude_quintiles.png") if len(quint) else "",
                      "delays": plot_delays(delays, out / "delai_execution.png")}

    with rt("6. CSV et README"):
        mcols = ["slug", "asset", "duration", "start_ts", "end_ts", "y", "p_pre", "p_pre_age_s", "p_pre60", "p_pre60_age_s", "tw_gap30", "p_hgb_ind1s", "p_logit_tw", "twap60_S",
                 "price_to_beat", "final_price", "level_gap_bp", "move_bp", "sigma_timesfm_bp", "sigma_rv_bp", "quintile_train", "train", "slot",
                 "n_trades", "taker_shares", "taker_fees_total", "n_pre60", "shares_pre60", "n_post60", "shares_post60"] + [f"side_{m}" for m in MODELS]
        M[[c for c in mcols if c in M.columns]].to_csv(out / "marches.csv", index=False)
        pd.concat([sum_a.assign(strategy="signal"), sum_b.assign(strategy="two_sided"), sum_c.assign(strategy="fair_value") if len(sum_c) else pd.DataFrame()],
                  ignore_index=True).to_csv(out / "resume_configurations.csv", index=False)
        a_cells.to_csv(out / "resume_par_cellule.csv", index=False)
        pairs.to_csv(out / "paires_two_sided.csv", index=False)
        pd.concat([sens, m2.assign(strategy="fair_value", q_variant="measured", rule=0, note="marge 2 c")], ignore_index=True).to_csv(out / "resume_sensibilite.csv", index=False)
        quint.to_csv(out / "resume_quintiles.csv", index=False)
        fv_quality.to_csv(out / "juste_valeur_qualite.csv", index=False)
        if len(fv_agg):
            fv_agg.to_csv(out / "fair_value_par_marche.csv", index=False)
        taker.drop(columns=["pnl_series"]).to_csv(out / "comparaison_preneur.csv", index=False)
        pd.DataFrame(series).rename_axis("start").to_csv(out / "pnl_cumule.csv")
        keep = ["slug", "duration", "half", "strategy", "label", "side", "price", "t_place", "t_cancel", "q_ahead", "crossing", "placed", "executed", "t_fill",
                "delay_fill_s", "fill_reason", "y", "would_win", "pnl_per_share", "pnl_per_placed_share", "pnl_order", "rebate_per_share"]
        pd.concat([A_ch.assign(model=chosen_a["model"]), B_ch], ignore_index=True)[[c for c in keep if c in A_ch.columns] + ["model"]].to_csv(out / "ordres_choisis.csv", index=False)
        runtime_df = pd.DataFrame(rt.steps + [("total", rt.total())], columns=["étape", "secondes"])
        runtime_df.to_csv(out / "runtime.csv", index=False)
        fee_desc = ", ".join(f"{k[0]} (taux {k[1]}, exposant {k[2]:.0f}) : {n_(v)} marchés" for k, v in M.groupby(["fee_type", "fee_rate", "fee_exponent"]).size().items())
        ctx = {"args": args, "M": M, "meta": meta, "stats": stats, "sum_a": sum_a, "sum_b": sum_b, "sum_c": sum_c, "quint": quint, "quint_sig": quint_sig,
               "sens": sens, "taker": taker.drop(columns=["pnl_series"]), "chosen_a": chosen_a, "chosen_b": chosen_b, "pairs": pairs, "b_sides": b_sides,
               "a_cells": a_cells, "cells": cells_all, "n_cells_a": n_cells_a, "n_cells_b": n_cells_b, "n_cells_c": n_cells_c, "hl": hl,
               "fv_quality": fv_quality, "c_per_market": c_per_market, "c_exposure": c_exposure, "fig_titles": fig_titles, "delay_text": delay_text,
               "maker_vs_taker": maker_vs_taker, "runtime": rt.total(), "runtime_df": runtime_df, "fee_desc": fee_desc,
               "n_trades": int(stats["n_trades"].sum()), "taker_volume": float(stats["taker_notional"].sum()),
               "n_sigma": int(np.isfinite(M["sigma_timesfm_bp"]).sum()), "level_gap_mean": float(np.nanmean(M["level_gap_bp"])),
               "level_gap_sd": float(np.nanstd(M["level_gap_bp"])),
               "med_trades": {d: float(M.loc[M["duration"] == d, "n_trades"].median()) for d in args.durations},
               "med_pre": {d: float(M.loc[M["duration"] == d, "n_pre60"].median()) for d in args.durations},
               "med_post": {d: float(M.loc[M["duration"] == d, "n_post60"].median()) for d in args.durations}}
        write_readme(ctx, out)
    print(f"OK : {out} ({rt.total():.0f} s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
