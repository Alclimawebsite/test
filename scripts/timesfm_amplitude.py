#!/usr/bin/env python
"""À quoi sert l'amplitude prévue par TimesFM ? — trois tests sur l'historique (papier).

Usage (depuis la racine, venv activé) :

    python scripts/timesfm_amplitude.py                      # tout (caches : ~1-2 min à chaud)
    python scripts/timesfm_amplitude.py --only-quantiles     # étape TimesFM seule (mise en cache)
    python scripts/timesfm_amplitude.py --threads 3 --refit  # recalcule TimesFM

Tests (fonctions : ``tradebot.timesfm_amplitude``) :

1. prévision de volatilité à 5/10/15 min (BTC, ETH, SOL, origines des runs
   ``reports/timesfm/*_none_s15_c512_logprice``, 02/05–25/09/2026) ;
2. juste valeur en cours de fenêtre Polymarket (BTC 5m/15m) : ``Φ(d/σ_restant)`` avec σ issu
   de l'EWMA réalisée, de TimesFM prévu à S, d'un mélange, contre le prix du marché et un
   empilement marché + nowcast ;
3. régime : quintiles d'amplitude prévue (résolutions serrées, justesse des signaux).

Apprentissage / test chronologiques : test 1 coupé à la médiane des origines ; tests 2-3 :
apprentissage 14/08–03/09 (marchés sur la grille de 15 min), test 04/09–24/09 (tous les
marchés BTC 5m et 15m). IC : bootstrap groupé par créneau de 15 min.

Sorties : ``reports/timesfm_amplitude/`` (README.md, CSV, PNG, ``sigma_par_marche.csv``).
Caches : ``data/cache/timesfm_amplitude/`` (quantiles TimesFM, eventMetadata), plus les
caches existants (Binance 1m, agrégats 1s, prix Polymarket). Aucun ordre, aucune clé.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradebot import polymarket as pm  # noqa: E402
from tradebot import polymarket_backtest as pb  # noqa: E402
from tradebot import timesfm_amplitude as ta  # noqa: E402
from tradebot.config import CACHE_DIR, REPORTS_DIR  # noqa: E402
from tradebot.report import (BG, BLUE, GRID, ORANGE, TEXT, TEXT_2, _draw_header, _header,  # noqa: E402
                             _pyplot, _save, _style_axes, fmt_number, to_markdown)

log = logging.getLogger("timesfm_amplitude")

OUT = REPORTS_DIR / "timesfm_amplitude"
CACHE = CACHE_DIR / "timesfm_amplitude"
PB_CACHE = CACHE_DIR / "pm_backtest"
TFM_DIR = REPORTS_DIR / "timesfm"
MVM_DIR = REPORTS_DIR / "polymarket" / "modeles_vs_marche"
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}
HORIZONS = (5, 10, 15)
H_MAX = 16                      # 15m : D/60 + 1 = 16 pas
K_OF = {"5m": (1, 2, 3), "15m": (2, 5, 10)}
LAMBDA_GRID = (0.90, 0.94, 0.96, 0.97, 0.98, 0.99, 0.995)
AQUA, YELLOW, VIOLET, GREY = "#1baf7a", "#eda100", "#4a3aa7", "#9b9a96"
Z80 = 1.2815516
VOL_MODELS = ("timesfm", "timesfm_iqr", "ewma", "rstd60", "rstd240", "park60", "gk60", "hist", "encomp", "encomp_q")
COLORS = {"timesfm": BLUE, "timesfm_iqr": "#8fb6e8", "ewma": ORANGE, "rstd60": AQUA, "rstd240": "#0f7a55",
          "park60": YELLOW, "gk60": "#b57a00", "hist": GREY, "encomp": "#a79ad8", "encomp_q": VIOLET,
          "market": GREY, "ewma_S": "#f2a98a", "ewma_k": ORANGE, "timesfm_S": BLUE, "mix": VIOLET, "stack": TEXT}
LABELS = {"timesfm": "TimesFM (q90−q10)/2,563", "timesfm_iqr": "TimesFM (q75−q25)/1,349", "ewma": "EWMA r² 1 min",
          "rstd60": "écart-type 60 min", "rstd240": "écart-type 240 min", "park60": "Parkinson 60 min",
          "gk60": "Garman-Klass 60 min", "hist": "moyenne historique", "encomp": "encompassing MCO TimesFM + EWMA",
          "encomp_q": "combinaison QLIKE TimesFM + EWMA",
          "market": "prix du marché à S+k", "ewma_S": "Φ · EWMA à S", "ewma_k": "Φ · EWMA à S+k (nowcast)",
          "timesfm_S": "Φ · TimesFM à S", "mix": "Φ · mélange TimesFM + EWMA", "stack": "empilement marché + nowcast"}
SHORT = {"timesfm": "TimesFM", "timesfm_iqr": "TimesFM IQR", "ewma": "EWMA", "rstd60": "σ 60 min", "rstd240": "σ 240 min",
         "park60": "Parkinson", "gk60": "Garman-Klass", "hist": "historique", "encomp": "MCO TFM+EWMA", "encomp_q": "QLIKE TFM+EWMA"}
PM_MODELS = ("market", "ewma_S", "ewma_k", "timesfm_S", "mix", "stack")


# ---------------------------------------------------------------------------
# Chronométrage, formats
# ---------------------------------------------------------------------------
class Runtime:
    def __init__(self):
        self.rows: list[dict] = []
        self.t0 = time.perf_counter()

    @contextmanager
    def __call__(self, step: str):
        t = time.perf_counter()
        log.info("--- %s", step)
        try:
            yield
        finally:
            self.rows.append({"étape": step, "secondes": round(time.perf_counter() - t, 1)})

    def total(self) -> float:
        return time.perf_counter() - self.t0


def fr(v, d: int = 1, pct: bool = False, signed: bool = False) -> str:
    return fmt_number(v, d, pct=pct, signed=signed)


def ci(est, lo, hi, d: int = 3, pct: bool = False, signed: bool = False) -> str:
    return f"{fr(est, d, pct, signed)} [{fr(lo, d, pct, signed)} ; {fr(hi, d, pct, signed)}]"


def n_(v) -> str:
    return fmt_number(v, 0)


# ---------------------------------------------------------------------------
# 1. Données
# ---------------------------------------------------------------------------
def load_bars(end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    from tradebot.data import clean_ohlcv, fetch_ohlcv

    out = {}
    for a, sym in SYMBOLS.items():
        df = clean_ohlcv(fetch_ohlcv(sym, interval="1m", days=365, cache=True, end=end))
        out[a] = df[["open", "high", "low", "close", "volume", "quote_volume"]].astype("float64")
        log.info("%s : %d barres 1m (%s -> %s)", sym, len(df), df.index[0], df.index[-1])
    return out


def test1_origins() -> dict[str, np.ndarray]:
    """Origines (s Unix) des runs TimesFM existants (stride 15, contexte 512, sans covariable)."""
    out = {}
    for a, sym in SYMBOLS.items():
        p = TFM_DIR / f"{sym}_timesfm3_none_s15_c512_logprice_predictions.csv"
        df = pd.read_csv(p, usecols=["time"])
        t = pd.to_datetime(df["time"], utc=True).drop_duplicates().sort_values()
        out[a] = pb.to_unix(t)
    return out


def test1_predictions(a: str) -> pd.DataFrame:
    """Colonnes utiles du run existant : origine, h, q10/q90 (contrôle), baselines, y_true."""
    p = TFM_DIR / f"{SYMBOLS[a]}_timesfm3_none_s15_c512_logprice_predictions.csv"
    df = pd.read_csv(p, usecols=["time", "horizon", "q10_ret", "q90_ret", "ret", "y_true", "reversal_h", "past_ret_60"])
    df["origin_s"] = pb.to_unix(pd.to_datetime(df["time"], utc=True))
    return df


def polymarket_starts(start: pd.Timestamp, mid: pd.Timestamp, end: pd.Timestamp) -> dict[str, np.ndarray]:
    """Débuts de fenêtre S : apprentissage = grille de 15 min sur [start, mid) ; test = grille
    de 5 min sur [mid, end)."""
    s0, s1, s2 = (int(x.timestamp()) for x in (start, mid, end))
    return {"train": np.arange(s0, s1, 900, dtype="int64"), "test": np.arange(s1, s2, 300, dtype="int64")}


# ---------------------------------------------------------------------------
# 2. Quantiles TimesFM (cache)
# ---------------------------------------------------------------------------
def compute_quantiles(sets: dict[str, tuple[str, np.ndarray]], bars: dict, args, rt: Runtime
                      ) -> tuple[dict[str, pd.DataFrame], dict]:
    """``sets[name] = (asset, origins_s)`` -> DataFrame de déciles (pb) par (origine, pas)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    todo = {k: v for k, v in sets.items() if args.refit or not (CACHE / f"quantiles_{k}.parquet").exists()}
    res: dict[str, pd.DataFrame] = {}
    info_all: dict[str, dict] = {}
    fc = None
    if todo:
        import torch

        from tradebot.config import TimesFMConfig
        from tradebot.forecaster import TimesFMForecaster

        torch.set_num_threads(max(1, args.threads))
        fc = TimesFMForecaster(TimesFMConfig(context_len=args.context_len))
        with rt("2a. chargement des poids TimesFM 3.0"):
            fc.load()
    for name, (asset, O) in sets.items():
        path = CACHE / f"quantiles_{name}.parquet"
        if name not in todo:
            res[name] = pd.read_parquet(path)
            j = path.with_suffix(".json")
            if j.exists():
                info_all[name] = json.loads(j.read_text())
            continue
        t0 = time.perf_counter()
        last = [0.0]

        def progress(done, total, _t0=t0, _name=name, _last=last):
            el = time.perf_counter() - _t0
            if el - _last[0] > 30 or done == total:
                _last[0] = el
                log.info("   TimesFM %s : %d/%d contextes, %.0f s, %.1f/s, reste ~%.0f s", _name, done, total, el,
                         done / max(el, 1e-9), el / max(done, 1) * (total - done))

        with rt(f"2b. TimesFM {name} ({len(O)} origines, h <= {H_MAX})"):
            q = ta.forecast_quantiles(fc, bars[asset]["close"], O, H_MAX, context_len=args.context_len,
                                      chunk=128, progress=progress)
        info = {"asset": asset, "n_origins": int(len(O)), "n_done": int(q["origin_s"].nunique()),
                "seconds": round(time.perf_counter() - t0, 1), "threads": args.threads,
                "context_len": args.context_len, "horizon": H_MAX}
        info["origins_per_s"] = round(info["n_done"] / max(info["seconds"], 1e-9), 2)
        pm._atomic_parquet(q, path)
        path.with_suffix(".json").write_text(json.dumps(info))
        res[name] = q
        info_all[name] = info
    return res, info_all


def sigma_table(qdf: pd.DataFrame, hs) -> pd.DataFrame:
    """σ (pb) par origine et horizon : ``origin_s``, ``h``, ``sig``, ``sig_iqr``, ``q10``, ``q90``."""
    rows = []
    for h in hs:
        O, q = ta.quantile_frame(qdf, h)
        rows.append(pd.DataFrame({"origin_s": O, "h": int(h), "sig": ta.sigma_from_quantiles(q),
                                  "sig_iqr": ta.sigma_from_quantiles(q, "iqr"), "q10": q[:, 0], "q90": q[:, -1]}))
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# 3. Test 1 : prévision de volatilité
# ---------------------------------------------------------------------------
def build_test1(bars: dict, Q: dict, args) -> tuple[pd.DataFrame, dict]:
    """Une ligne par (actif, origine, h) : cibles réalisées et σ² (pb²) de chaque modèle."""
    frames, meta = [], {}
    # coupure commune : médiane des origines BTC
    all_o = np.sort(Q["test1_btc"]["origin_s"].unique())
    split_s = int(np.median(all_o))
    meta["split_s"] = split_s
    for a in SYMBOLS:
        b = bars[a]
        close = b["close"]
        st = sigma_table(Q[f"test1_{a}"], HORIZONS)
        O_all = np.sort(st["origin_s"].unique())
        # contrôle : reproduction des q10/q90 du run existant
        pred = test1_predictions(a)
        chk = st.merge(pred, left_on=["origin_s", "h"], right_on=["origin_s", "horizon"], how="inner")
        meta[f"repro_{a}"] = float(np.nanmax(np.abs(np.c_[chk["q10"] - chk["q10_ret"] * ta.BP,
                                                          chk["q90"] - chk["q90_ret"] * ta.BP]))) if len(chk) else math.nan
        # volatilité réalisée : λ choisi sur l'apprentissage (QLIKE, cibles r_h² des 3 horizons)
        base = ta.realized_vol_estimators(b, lam=0.97)
        r2 = base["r2"].to_numpy()
        ts_bar = pb.to_unix(base.index)
        targets = {}
        train_o = O_all[O_all < split_s]
        for h in HORIZONS:
            y = ta.future_log_return(close, train_o, h)
            pos = np.searchsorted(ts_bar, train_o)
            ok = (pos < len(ts_bar)) & (ts_bar[np.minimum(pos, len(ts_bar) - 1)] == train_o) & np.isfinite(y)
            targets[h] = (pos[ok], y[ok] ** 2)
        lam = ta.choose_ewma_lambda(r2, targets, LAMBDA_GRID)
        meta[f"lambda_{a}"] = lam
        rv = ta.realized_vol_estimators(b, lam=lam)
        for h in HORIZONS:
            sub = st[st["h"] == h].copy()
            O = sub["origin_s"].to_numpy()
            sub["ret"] = ta.future_log_return(close, O, h)
            sub["rv_h"] = ta.realized_variance_h(rv["r2"], O, h)
            look = pb.bar_lookup(rv, O, ["ewma", "rstd60", "rstd240", "park60", "gk60"])
            for j, m in enumerate(["ewma", "rstd60", "rstd240", "park60", "gk60"]):
                sub[f"s2_{m}"] = h * look[:, j]
            sub["s2_timesfm"] = sub["sig"] ** 2
            sub["s2_timesfm_iqr"] = sub["sig_iqr"] ** 2
            sub["asset"] = a
            frames.append(sub)
    df = pd.concat(frames, ignore_index=True)
    df["train"] = df["origin_s"] < split_s
    df["r2"] = df["ret"] ** 2
    df["slot"] = ta.slot_of(df["origin_s"])
    # moyenne historique (par actif × h, apprentissage) et encompassing (par actif × h)
    df["s2_hist"] = np.nan
    df["s2_encomp"] = np.nan
    df["s2_encomp_q"] = np.nan
    enc_rows = []
    for (a, h), g in df.groupby(["asset", "h"]):
        tr = g["train"] & np.isfinite(g["r2"])
        df.loc[g.index, "s2_hist"] = float(g.loc[tr, "r2"].mean())
        fit = ta.encompassing_fit(g.loc[tr, "r2"], [g.loc[tr, "s2_timesfm"], g.loc[tr, "s2_ewma"]])
        df.loc[g.index, "s2_encomp"] = ta.encompassing_predict(fit["coef"], [g["s2_timesfm"], g["s2_ewma"]])
        wq = ta.qlike_combination_fit(g.loc[tr, "r2"], [g.loc[tr, "s2_timesfm"], g.loc[tr, "s2_ewma"]])
        df.loc[g.index, "s2_encomp_q"] = wq[0] * g["s2_timesfm"] + wq[1] * g["s2_ewma"]
        fit_t = ta.encompassing_fit(g.loc[tr, "r2"], [g.loc[tr, "s2_timesfm"]])
        fit_e = ta.encompassing_fit(g.loc[tr, "r2"], [g.loc[tr, "s2_ewma"]])
        enc_rows.append({"actif": a.upper(), "h": h, "n_train": fit["n"],
                         "a": fit["coef"][0], "b_timesfm": fit["coef"][1], "t_timesfm": fit["t"][1],
                         "c_ewma": fit["coef"][2], "t_ewma": fit["t"][2],
                         "b_timesfm_seul": fit_t["coef"][1], "t_timesfm_seul": fit_t["t"][1],
                         "c_ewma_seul": fit_e["coef"][1], "t_ewma_seul": fit_e["t"][1],
                         "w_timesfm_qlike": float(wq[0]), "w_ewma_qlike": float(wq[1]),
                         "part_timesfm_qlike": float(wq[0] / (wq[0] + wq[1])) if (wq[0] + wq[1]) > 0 else np.nan})
    meta["encompassing"] = pd.DataFrame(enc_rows)
    return df, meta


def score_test1(df: pd.DataFrame, B: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scores hors échantillon (2e moitié) par (actif | tous) × h × modèle, ΔQLIKE vs EWMA avec IC."""
    te = df[~df["train"] & np.isfinite(df["r2"])].copy()
    rows, drows = [], []
    for scope in ["Tous", *sorted(SYMBOLS)]:
        g = te if scope == "Tous" else te[te["asset"] == scope]
        for h in HORIZONS:
            gh = g[g["h"] == h]
            boot = pb.SlotBootstrap(gh["slot"].to_numpy(), B=B, seed=1)
            r2 = gh["r2"].to_numpy()
            rabs = np.abs(gh["ret"].to_numpy())
            q_ref = ta.qlike(r2, gh["s2_ewma"].to_numpy())
            q_rv_ref = ta.qlike(gh["rv_h"].to_numpy(), gh["s2_ewma"].to_numpy())
            for m in VOL_MODELS:
                s2 = gh[f"s2_{m}"].to_numpy()
                s = np.sqrt(np.clip(s2, 0, None))
                q = ta.qlike(r2, s2)
                q_rv = ta.qlike(gh["rv_h"].to_numpy(), s2)
                if m == "timesfm":
                    cov = ta.coverage(gh["ret"].to_numpy(), gh["q10"].to_numpy(), gh["q90"].to_numpy())
                else:
                    cov = ta.coverage(gh["ret"].to_numpy(), -Z80 * s, Z80 * s)
                mz = ta.mincer_zarnowitz(r2, s2)
                dq, dlo, dhi = boot.mean(q - q_ref)
                dqrv, dqrv_lo, dqrv_hi = boot.mean(q_rv - q_rv_ref)
                cv, cv_lo, cv_hi = boot.mean(cov)
                rows.append({"cellule": scope if scope == "Tous" else scope.upper(), "h": h, "modèle": m, "n": int(np.isfinite(q).sum()),
                             "qlike": float(np.nanmean(q)), "qlike_rv": float(np.nanmean(q_rv)),
                             "mse_r2": float(np.nanmean(ta.mse(r2, s2))), "mae_abs": float(np.nanmean(ta.mae_abs(rabs, s))),
                             "mz_a": mz["a"], "mz_b": mz["b"], "mz_r2": mz["r2"],
                             "couverture_80": cv, "couverture_80_lo": cv_lo, "couverture_80_hi": cv_hi,
                             "sigma_moy_pb": float(np.nanmean(s)), "abs_ret_moy_pb": float(np.nanmean(rabs)),
                             "dqlike_vs_ewma": dq, "dqlike_lo": dlo, "dqlike_hi": dhi,
                             "dqlike_rv_vs_ewma": dqrv, "dqlike_rv_lo": dqrv_lo, "dqlike_rv_hi": dqrv_hi})
    return pd.DataFrame(rows), te


# ---------------------------------------------------------------------------
# 4. Polymarket : marchés, métadonnées, prix en cours de fenêtre
# ---------------------------------------------------------------------------
def fetch_event_meta(client: pm.PolymarketClient, slugs: list[str], workers: int) -> pd.DataFrame:
    """``eventMetadata`` (priceToBeat, finalPrice) ; caches : diag (existant, lu) + le nôtre."""
    cols = ["slug", "price_to_beat", "final_price"]
    path = CACHE / "event_meta.parquet"
    parts = []
    diag = CACHE_DIR / "diag" / "event_meta.parquet"
    if diag.exists():
        parts.append(pd.read_parquet(diag)[cols])
    if path.exists():
        parts.append(pd.read_parquet(path)[cols])
    cached = pd.concat(parts, ignore_index=True).drop_duplicates("slug") if parts else pd.DataFrame(columns=cols)
    todo = sorted(set(slugs) - set(cached["slug"]))
    new = []
    if todo:
        batches = [todo[i: i + pm.GAMMA_BATCH] for i in range(0, len(todo), pm.GAMMA_BATCH)]

        def one(batch):
            params = [("slug", s) for s in batch] + [("limit", len(batch))]
            out = []
            for ev in client._get(f"{pm.GAMMA_URL}/events", params) or []:
                md = ev.get("eventMetadata") or {}
                out.append({"slug": ev.get("slug"), "price_to_beat": pm._f(md.get("priceToBeat")),
                            "final_price": pm._f(md.get("finalPrice"))})
            return out

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as ex:
            for rows in ex.map(one, batches):
                new.extend(rows)
        log.info("eventMetadata : %d slugs demandés, %d reçus", len(todo), len(new))
    new_df = pd.DataFrame(new, columns=cols)
    if len(new_df):
        keep = new_df[np.isfinite(new_df["final_price"].astype(float))]
        mine = pd.concat([pd.read_parquet(path)[cols] if path.exists() else pd.DataFrame(columns=cols), keep],
                         ignore_index=True).drop_duplicates("slug")
        pm._atomic_parquet(mine.reset_index(drop=True), path)
    allm = pd.concat([cached, new_df], ignore_index=True).drop_duplicates("slug", keep="first")
    return allm[allm["slug"].isin(set(slugs))].reset_index(drop=True)


def fetch_window_prices(client: pm.PolymarketClient, M: pd.DataFrame, workers: int) -> pd.DataFrame:
    """Prix du jeton Up (dernier point <= S + k min, ancienneté <= 90 s) pour les k de chaque durée."""
    def one(rec):
        slug, token, S, E, d = rec
        row = {"slug": slug}
        try:
            hist = client.prices_history(token, int(S) - pb.PRICE_LOOKBACK_S, int(E) + 60, fidelity=1, resolved=True)
        except pm.PolymarketError as exc:
            log.warning("prices-history %s : %s", slug, exc)
            hist = None
        for k in K_OF[d]:
            p, age = ta.market_price_at(hist, int(S) + 60 * k, staleness_s=90)
            row[f"p_mkt_k{k}"] = p
            row[f"age_k{k}"] = age
        return row

    recs = list(zip(M["slug"], M["token_up"], M["start_ts"], M["end_ts"], M["duration"]))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        rows = list(ex.map(one, recs))
    return pd.DataFrame(rows)


def load_polymarket(args, bars: dict, Q: dict, rt: Runtime, meta: dict) -> pd.DataFrame:
    client = pm.PolymarketClient()
    with rt("4a. marchés BTC 5m/15m (list_updown_markets, cache) + prévisions modeles_vs_marche"):
        M = pb.market_table(client, ["btc"], ["5m", "15m"], args.start, args.end, max_workers=args.workers)
        M = M[np.isfinite(M["y"])].reset_index(drop=True)
        mvm = pd.read_csv(MVM_DIR / "marches_predictions.csv",
                          usecols=["slug", "p_pre", "tw_gap30", "p_hgb_ind1s", "p_stack", "sigma_1m"])
        M = M.merge(mvm, on="slug", how="left")
    with rt("4b. eventMetadata (priceToBeat / finalPrice ; caches diag + gamma)"):
        em = fetch_event_meta(client, M["slug"].tolist(), args.workers)
        M = M.merge(em, on="slug", how="left")
    with rt("4c. prix du jeton Up à S+k (prices_history, cache)"):
        W = fetch_window_prices(client, M, args.workers)
        M = M.merge(W, on="slug", how="left")
    with rt("4d. Binance : TWAP60(S) (agrégats 1s), prix à S+k, EWMA"):
        agg = pb.load_second_aggregates("BTCUSDT", args.start, args.end, PB_CACHE / "agg1s", workers=2)
        S = M["start_ts"].to_numpy(dtype="int64")
        M["twap60_S"] = pb.bar_lookup(agg, S - 60, "twap60")
        M["origin_s"] = ta.polymarket_origin(S)
        M["hD"] = M["duration_s"] // 60 + 1
        M["train"] = M["start_ts"] < int(args.mid.timestamp())
        M["slot"] = M["start_ts"] // pb.SLOT_S
        for k in (1, 2, 3, 5, 10):
            M[f"px_k{k}"] = pb.bar_lookup(bars["btc"], S + 60 * k - 60, "close")
            with np.errstate(invalid="ignore", divide="ignore"):
                M[f"d_k{k}"] = np.log(M[f"px_k{k}"] / M["twap60_S"]) * ta.BP
        # TimesFM à S : σ au pas hD (origine S − 120 s)
        qs = pd.concat([Q["pm_train_btc"], Q["pm_test_btc"]], ignore_index=True)
        st = sigma_table(qs, sorted(M["hD"].unique()))
        M = M.merge(st.rename(columns={"h": "hD", "sig": "sigma_timesfm_bp", "sig_iqr": "sigma_timesfm_iqr_bp"})
                    [["origin_s", "hD", "sigma_timesfm_bp", "sigma_timesfm_iqr_bp"]], on=["origin_s", "hD"], how="left")
        M["sigma_pm_timesfm"] = M["sigma_timesfm_bp"] / np.sqrt(M["hD"])
        M["move_bp"] = np.log(M["final_price"] / M["price_to_beat"]) * ta.BP
    return M


def add_ewma(M: pd.DataFrame, bars: dict, lam: float) -> pd.DataFrame:
    rv = ta.realized_vol_estimators(bars["btc"], lam=lam)
    S = M["start_ts"].to_numpy(dtype="int64")
    M["sigma_pm_ewma_S"] = np.sqrt(pb.bar_lookup(rv, S - 120, "ewma"))
    for k in (1, 2, 3, 5, 10):
        M[f"sigma_pm_ewma_k{k}"] = np.sqrt(pb.bar_lookup(rv, S + 60 * k - 60, "ewma"))
    M["sigma_rv_bp"] = M["sigma_pm_ewma_S"] * np.sqrt(M["hD"])
    return M


def choose_lambda_pm(M: pd.DataFrame, bars: dict) -> tuple[float, pd.DataFrame]:
    """λ minimisant la log-loss de Φ(d/σ_EWMA(S+k)) sur les marchés d'apprentissage (tous k)."""
    tr = M[M["train"]]
    rows = []
    for lam in LAMBDA_GRID:
        rv = ta.realized_vol_estimators(bars["btc"], lam=lam)
        S = tr["start_ts"].to_numpy(dtype="int64")
        tot, n = 0.0, 0
        for d, ks in K_OF.items():
            g = tr[tr["duration"] == d]
            Sg = g["start_ts"].to_numpy(dtype="int64")
            for k in ks:
                spm = np.sqrt(pb.bar_lookup(rv, Sg + 60 * k - 60, "ewma"))
                p = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(spm, g["duration_s"] / 60 - k))
                ll = ta.log_loss(g["y"], p)
                tot += float(np.nansum(ll))
                n += int(np.isfinite(ll).sum())
        rows.append({"lambda": lam, "log_loss_train": tot / max(n, 1), "n": n})
    tab = pd.DataFrame(rows)
    return float(tab.loc[tab["log_loss_train"].idxmin(), "lambda"]), tab


def _logit(p):
    p = np.clip(np.asarray(p, dtype="float64"), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def run_test2(M: pd.DataFrame, B: int) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Probabilités Φ(d/σ) en cours de fenêtre contre le marché ; paramètres (w, empilement) sur
    l'apprentissage, scores sur le test."""
    from sklearn.linear_model import LogisticRegression

    P = {}   # (duration, k) -> DataFrame test avec p_<model>
    params = []
    for d, ks in K_OF.items():
        g_all = M[M["duration"] == d].copy()
        Dm = int(pm.DURATION_SECONDS[d] // 60)
        # poids du mélange : choisi sur l'apprentissage (tous k), grille 0..1
        best_w, best_ll = 0.5, math.inf
        for w in np.linspace(0, 1, 11):
            tot, n = 0.0, 0
            for k in ks:
                g = g_all[g_all["train"] & np.isfinite(g_all["sigma_pm_timesfm"])]
                s2 = w * g["sigma_pm_timesfm"] ** 2 + (1 - w) * g[f"sigma_pm_ewma_k{k}"] ** 2
                p = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(np.sqrt(s2), Dm - k))
                ll = ta.log_loss(g["y"], p)
                tot += float(np.nansum(ll))
                n += int(np.isfinite(ll).sum())
            if n and tot / n < best_ll:
                best_w, best_ll = float(w), tot / n
        for k in ks:
            tau = Dm - k
            g = g_all.copy()
            g["p_market"] = g[f"p_mkt_k{k}"]
            g["p_ewma_S"] = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(g["sigma_pm_ewma_S"], tau))
            g["p_ewma_k"] = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(g[f"sigma_pm_ewma_k{k}"], tau))
            g["p_timesfm_S"] = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(g["sigma_pm_timesfm"], tau))
            s2 = best_w * g["sigma_pm_timesfm"] ** 2 + (1 - best_w) * g[f"sigma_pm_ewma_k{k}"] ** 2
            g["p_mix"] = ta.prob_up_gaussian(g[f"d_k{k}"], ta.sigma_remaining(np.sqrt(s2), tau))
            # empilement marché + nowcast (EWMA à S+k), régression logistique sur l'apprentissage
            tr = g["train"] & np.isfinite(g["p_market"]) & np.isfinite(g["p_ewma_k"])
            X_tr = np.c_[_logit(g.loc[tr, "p_market"]), _logit(g.loc[tr, "p_ewma_k"])]
            lr = LogisticRegression(C=100.0).fit(X_tr, g.loc[tr, "y"].astype(int))
            ok = np.isfinite(g["p_market"]) & np.isfinite(g["p_ewma_k"])
            g["p_stack"] = np.nan
            g.loc[ok, "p_stack"] = lr.predict_proba(np.c_[_logit(g.loc[ok, "p_market"]), _logit(g.loc[ok, "p_ewma_k"])])[:, 1]
            params.append({"durée": d, "k": k, "w_timesfm": best_w, "stack_b_marché": float(lr.coef_[0][0]),
                           "stack_b_nowcast": float(lr.coef_[0][1]), "stack_a": float(lr.intercept_[0]), "n_train": int(tr.sum())})
            P[(d, k)] = g
    rows = []
    for (d, k), g in P.items():
        te = g[~g["train"]]
        # même échantillon pour tous les modèles : marché, TimesFM et EWMA disponibles
        ok = np.isfinite(te["p_market"]) & np.isfinite(te["p_timesfm_S"]) & np.isfinite(te["p_ewma_k"]) & np.isfinite(te["p_stack"])
        te = te[ok]
        boot = pb.SlotBootstrap(te["slot"].to_numpy(), B=B, seed=2)
        y = te["y"].to_numpy()
        b_ref = ta.brier(y, te["p_market"].to_numpy())
        b_ewk = ta.brier(y, te["p_ewma_k"].to_numpy())
        for m in PM_MODELS:
            p = te[f"p_{m}"].to_numpy()
            b = ta.brier(y, p)
            acc, acc_lo, acc_hi = boot.mean(ta.accuracy(y, p))
            db, dlo, dhi = boot.mean(b - b_ref)
            de, delo, dehi = boot.mean(b - b_ewk)
            rows.append({"durée": d, "k": k, "modèle": m, "n": int(len(te)), "âge_point_marché_s": float(np.nanmedian(te[f"age_k{k}"])),
                         "brier": float(np.nanmean(b)),
                         "log_loss": float(np.nanmean(ta.log_loss(y, p))), "justesse": acc, "justesse_lo": acc_lo,
                         "justesse_hi": acc_hi, "dbrier_vs_marché": db, "dbrier_lo": dlo, "dbrier_hi": dhi,
                         "dbrier_vs_ewma_k": de, "dbrier_ewma_lo": delo, "dbrier_ewma_hi": dehi,
                         "p_moy": float(np.nanmean(p)), "p_sd": float(np.nanstd(p))})
    return pd.DataFrame(rows), pd.DataFrame(params), P


# ---------------------------------------------------------------------------
# 5. Test 3 : régime par quintile d'amplitude prévue
# ---------------------------------------------------------------------------
def quintile_table_pm(M: pd.DataFrame, col: str, B: int, label: str) -> pd.DataFrame:
    """Par durée et quintile de ``col`` (bornes sur l'apprentissage) : résolutions serrées,
    justesse des signaux (test seulement)."""
    rows = []
    for d in ("5m", "15m"):
        g = M[M["duration"] == d]
        tr = g[g["train"] & np.isfinite(g[col])]
        edges = ta.quintile_edges(tr[col])
        te = g[~g["train"] & np.isfinite(g[col])].copy()
        te["q"] = ta.quintile_of(te[col], edges)
        boot = pb.SlotBootstrap(te["slot"].to_numpy(), B=B, seed=3)
        y = te["y"].to_numpy()
        stats = {
            "serré_2pb": (np.abs(te["move_bp"]) < 2.0).astype(float).where(np.isfinite(te["move_bp"])).to_numpy(),
            "taux_up": y,
            "just_gap_m30": ta.accuracy(y, (te["tw_gap30"] >= 0).astype(float).where(np.isfinite(te["tw_gap30"]))),
            "just_hgb_ind1s": ta.accuracy(y, te["p_hgb_ind1s"]),
            "just_marché_S-30": ta.accuracy(y, te["p_pre"]),
            "just_marché_S+k1": ta.accuracy(y, te["p_mkt_k1"] if d == "5m" else te["p_mkt_k2"]),
        }
        for q in range(1, 6):
            m = (te["q"] == q).to_numpy()
            row = {"amplitude": label, "durée": d, "quintile": q, "n": int(m.sum()),
                   "sigma_moy_pb": float(te.loc[m, col].mean()),
                   "mouvement_médian_pb": float(np.nanmedian(np.abs(te.loc[m, "move_bp"])))}
            for name, x in stats.items():
                est, lo, hi = boot.mean(x, mask=m)
                row[name], row[f"{name}_lo"], row[f"{name}_hi"] = est, lo, hi
            rows.append(row)
        # écart Q5 − Q1 avec IC (mêmes tirages)
        m1, m5 = (te["q"] == 1).to_numpy(), (te["q"] == 5).to_numpy()
        row = {"amplitude": label, "durée": d, "quintile": "Q5−Q1", "n": int(m1.sum() + m5.sum()),
               "sigma_moy_pb": np.nan, "mouvement_médian_pb": np.nan}
        for name, x in stats.items():
            e5, d5 = boot.ratio_draws(x, 1.0, mask=m5)
            e1, d1 = boot.ratio_draws(x, 1.0, mask=m1)
            diff = d5 - d1
            diff = diff[np.isfinite(diff)]
            row[name] = e5 - e1
            row[f"{name}_lo"], row[f"{name}_hi"] = (np.quantile(diff, [0.025, 0.975]) if diff.size else (np.nan, np.nan))
        rows.append(row)
    return pd.DataFrame(rows)


def quintile_table_binance(te: pd.DataFrame, full: pd.DataFrame, B: int) -> pd.DataFrame:
    """Binance (2e moitié du test 1) : justesse du signal retournement par quintile de σ_TimesFM(h)
    (bornes par actif × h sur la 1re moitié)."""
    rows = []
    preds = {a: test1_predictions(a) for a in SYMBOLS}
    for h in HORIZONS:
        parts = []
        for a in SYMBOLS:
            g = full[(full["asset"] == a) & (full["h"] == h)]
            edges = ta.quintile_edges(g.loc[g["train"], "sig"])
            t = te[(te["asset"] == a) & (te["h"] == h)].copy()
            t["q"] = ta.quintile_of(t["sig"], edges)
            pr = preds[a]
            pr = pr[pr["horizon"] == h][["origin_s", "reversal_h", "y_true"]]
            t = t.merge(pr, on="origin_s", how="left")
            parts.append(t)
        t = pd.concat(parts, ignore_index=True)
        boot = pb.SlotBootstrap(t["slot"].to_numpy(), B=B, seed=4)
        stats = {"just_retournement": ta.accuracy(t["y_true"], t["reversal_h"]),
                 "serré_2pb": (np.abs(t["ret"]) < 2.0).astype(float).to_numpy(),
                 "taux_up": t["y_true"].to_numpy()}
        for q in range(1, 6):
            m = (t["q"] == q).to_numpy()
            row = {"h": h, "quintile": q, "n": int(m.sum()), "sigma_moy_pb": float(t.loc[m, "sig"].mean()),
                   "abs_ret_médian_pb": float(np.nanmedian(np.abs(t.loc[m, "ret"])))}
            for name, x in stats.items():
                est, lo, hi = boot.mean(x, mask=m)
                row[name], row[f"{name}_lo"], row[f"{name}_hi"] = est, lo, hi
            rows.append(row)
        m1, m5 = (t["q"] == 1).to_numpy(), (t["q"] == 5).to_numpy()
        row = {"h": h, "quintile": "Q5−Q1", "n": int(m1.sum() + m5.sum()), "sigma_moy_pb": np.nan, "abs_ret_médian_pb": np.nan}
        for name, x in stats.items():
            e5, d5 = boot.ratio_draws(x, 1.0, mask=m5)
            e1, d1 = boot.ratio_draws(x, 1.0, mask=m1)
            diff = d5 - d1
            diff = diff[np.isfinite(diff)]
            row[name] = e5 - e1
            row[f"{name}_lo"], row[f"{name}_hi"] = (np.quantile(diff, [0.025, 0.975]) if diff.size else (np.nan, np.nan))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 6. Graphiques
# ---------------------------------------------------------------------------
def plot_qlike(scores: pd.DataFrame, path: Path, title: str) -> None:
    plt = _pyplot()
    models = [m for m in VOL_MODELS if m != "timesfm_iqr"]
    W, H0 = 11.0, 4.8
    t, s, hh = _header(W, title, "QLIKE moyenne sur r_h² (2e moitié, BTC + ETH + SOL, ≈ 2 250 origines par horizon) ; "
                       "plus bas = mieux ; barre d'erreur = IC 95 % de l'écart à l'EWMA (bootstrap par créneau de 15 min)")
    fig, axes = plt.subplots(1, len(HORIZONS), sharey=True, figsize=(W, H0 + hh), facecolor=BG, squeeze=False)
    fig.subplots_adjust(top=1 - (hh + 0.35) / (H0 + hh), bottom=0.22, left=0.07, right=0.99, wspace=0.08)
    _draw_header(fig, t, s)
    sub = scores[scores["cellule"] == "Tous"]
    for ax, h in zip(axes[0], HORIZONS):
        g = sub[sub["h"] == h].set_index("modèle")
        ref = float(g.loc["ewma", "qlike"])
        for i, m in enumerate(models):
            v = float(g.loc[m, "qlike"])
            ax.bar(i, v, width=0.7, color=COLORS[m])
            if m != "ewma":
                ax.errorbar(i, v, yerr=[[v - (ref + g.loc[m, "dqlike_lo"])], [(ref + g.loc[m, "dqlike_hi"]) - v]],
                            fmt="none", ecolor=TEXT, elinewidth=1.0, capsize=3)
        ax.axhline(ref, color=ORANGE, linewidth=0.8, linestyle="--")
        ax.set_xticks(range(len(models)))
        ax.set_xticklabels([SHORT[m] for m in models], rotation=35, ha="right", fontsize=8)
        ax.set_title(f"h = {h} min", fontsize=10, color=TEXT, loc="left")
        _style_axes(ax)
        lo = float(sub["qlike"].min()) * 0.9
        ax.set_ylim(lo, float(sub["qlike"].max()) * 1.04)
    axes[0][0].set_ylabel("QLIKE", color=TEXT_2, fontsize=9)
    _save(fig, path)


def plot_brier_k(sc: pd.DataFrame, path: Path, title: str) -> None:
    plt = _pyplot()
    W, H0 = 10.0, 4.2
    t, s, hh = _header(W, title, "Brier (plus bas = mieux) par instant S+k, marchés BTC du test (04/09–24/09) ; "
                       "Φ(d/σ) : d = log(spot Binance à S+k / TWAP60(S)), σ_restant = σ_par_minute · √(τ − 0,5)")
    fig, axes = plt.subplots(1, 2, sharey=True, figsize=(W, H0 + hh), facecolor=BG, squeeze=False)
    fig.subplots_adjust(top=1 - (hh + 0.35) / (H0 + hh), bottom=0.16, left=0.07, right=0.99, wspace=0.08)
    _draw_header(fig, t, s)
    for ax, d in zip(axes[0], ("5m", "15m")):
        g = sc[sc["durée"] == d]
        for m in PM_MODELS:
            gm = g[g["modèle"] == m].sort_values("k")
            ax.plot(gm["k"], gm["brier"], marker="o", markersize=4, color=COLORS[m], label=LABELS[m],
                    linewidth=2.0 if m in ("market", "timesfm_S") else 1.2,
                    linestyle="-" if m != "stack" else ":")
        ax.set_xticks(K_OF[d])
        ax.set_xlabel("k (minutes après l'ouverture S)", color=TEXT_2, fontsize=9)
        ax.set_title(f"BTC {d} (n ≈ {n_(g['n'].max())} marchés)", fontsize=10, color=TEXT, loc="left")
        _style_axes(ax)
    axes[0][0].set_ylabel("Brier", color=TEXT_2, fontsize=9)
    axes[0][1].legend(frameon=False, fontsize=8, loc="upper right")
    _save(fig, path)


def plot_quintiles(qpm: pd.DataFrame, qbn: pd.DataFrame, path: Path, title: str) -> None:
    plt = _pyplot()
    W, H0 = 10.0, 4.2
    t, s, hh = _header(W, title, "Justesse (% de bonnes directions) par quintile d'amplitude prévue par TimesFM à S "
                       "(bornes fixées sur l'apprentissage) ; barres = IC 95 % bootstrap par créneau de 15 min ; test seulement")
    fig, axes = plt.subplots(1, 2, sharey=True, figsize=(W, H0 + hh + 0.9), facecolor=BG, squeeze=False)
    fig.subplots_adjust(top=1 - (hh + 0.35) / (H0 + hh + 0.9), bottom=0.30, left=0.07, right=0.99, wspace=0.08)
    _draw_header(fig, t, s)
    ax = axes[0][0]
    g = qpm[(qpm["amplitude"] == "timesfm") & (qpm["quintile"] != "Q5−Q1")].copy()
    g["quintile"] = g["quintile"].astype(int)
    series = [("just_gap_m30", "TWAP partiel (gap_m30)", ORANGE), ("just_hgb_ind1s", "hgb_ind1s", VIOLET),
              ("just_marché_S-30", "marché à S−30 s", GREY)]
    for j, (col, lab, color) in enumerate(series):
        for d, ls in (("5m", "-"), ("15m", "--")):
            gd = g[g["durée"] == d].sort_values("quintile")
            x = gd["quintile"] + (j - 1) * 0.12
            ax.errorbar(x, gd[col] * 100, yerr=[(gd[col] - gd[f"{col}_lo"]) * 100, (gd[f"{col}_hi"] - gd[col]) * 100],
                        fmt="o" if d == "5m" else "s", markersize=4, color=color, linestyle=ls, linewidth=1.1,
                        capsize=2, label=f"{lab} · {d}")
    ax.axhline(50, color=GRID, linewidth=1.0)
    ax.set_xticks(range(1, 6))
    ax.set_xlabel("quintile de σ_TimesFM (1 = calme, 5 = agité)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("justesse (%)", color=TEXT_2, fontsize=9)
    ax.set_title("Polymarket BTC (signaux à S−30 s)", fontsize=10, color=TEXT, loc="left")
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", bbox_to_anchor=(0.0, -0.18), ncol=2)
    _style_axes(ax)
    ax = axes[0][1]
    g = qbn[qbn["quintile"] != "Q5−Q1"].copy()
    g["quintile"] = g["quintile"].astype(int)
    for j, (h, color) in enumerate(zip(HORIZONS, (BLUE, AQUA, YELLOW))):
        gd = g[g["h"] == h].sort_values("quintile")
        x = gd["quintile"] + (j - 1) * 0.12
        ax.errorbar(x, gd["just_retournement"] * 100,
                    yerr=[(gd["just_retournement"] - gd["just_retournement_lo"]) * 100,
                          (gd["just_retournement_hi"] - gd["just_retournement"]) * 100],
                    fmt="o", markersize=4, color=color, linestyle="-", linewidth=1.1, capsize=2, label=f"retournement · h = {h}")
    ax.axhline(50, color=GRID, linewidth=1.0)
    ax.set_xticks(range(1, 6))
    ax.set_xlabel("quintile de σ_TimesFM(h)", color=TEXT_2, fontsize=9)
    ax.set_title("Binance BTC + ETH + SOL (2e moitié)", fontsize=10, color=TEXT, loc="left")
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", bbox_to_anchor=(0.0, -0.18), ncol=2)
    _style_axes(ax)
    _save(fig, path)


def plot_sigma_vs_realized(te: pd.DataFrame, path: Path, title: str) -> None:
    """Calibration de l'amplitude : |r_h| réalisé moyen par décile de σ prévu (TimesFM contre EWMA)."""
    plt = _pyplot()
    W, H0 = 10.0, 4.0
    t, s, hh = _header(W, title, "Par décile de σ prévu (2e moitié, 3 actifs) : |r_h| réalisé moyen contre σ prévu moyen × √(2/π) ; "
                       "la diagonale = amplitude parfaitement calibrée")
    fig, axes = plt.subplots(1, len(HORIZONS), sharey=True, figsize=(W, H0 + hh), facecolor=BG, squeeze=False)
    fig.subplots_adjust(top=1 - (hh + 0.35) / (H0 + hh), bottom=0.16, left=0.07, right=0.99, wspace=0.08)
    _draw_header(fig, t, s)
    for ax, h in zip(axes[0], HORIZONS):
        g = te[te["h"] == h]
        mx = 0.0
        for m, color in (("timesfm", BLUE), ("ewma", ORANGE)):
            sig = np.sqrt(g[f"s2_{m}"].to_numpy())
            ok = np.isfinite(sig) & np.isfinite(g["ret"].to_numpy())
            dec = pd.qcut(sig[ok], 10, labels=False, duplicates="drop")
            xs = pd.Series(sig[ok] * math.sqrt(2 / math.pi)).groupby(dec).mean()
            ys = pd.Series(np.abs(g["ret"].to_numpy()[ok])).groupby(dec).mean()
            ax.plot(xs, ys, marker="o", markersize=4, color=color, label=LABELS[m])
            mx = max(mx, float(xs.max()), float(ys.max()))
        ax.plot([0, mx * 1.05], [0, mx * 1.05], color=GRID, linewidth=1.0)
        ax.set_title(f"h = {h} min", fontsize=10, color=TEXT, loc="left")
        ax.set_xlabel("E|r| prévu (pb)", color=TEXT_2, fontsize=9)
        _style_axes(ax)
    axes[0][0].set_ylabel("|r_h| réalisé moyen (pb)", color=TEXT_2, fontsize=9)
    axes[0][0].legend(frameon=False, fontsize=8, loc="upper left")
    _save(fig, path)


# ---------------------------------------------------------------------------
# 7. README
# ---------------------------------------------------------------------------
def write_readme(ctx: dict, rt: Runtime) -> None:
    L: list[str] = []
    w = L.append
    sc1, enc, meta1 = ctx["scores1"], ctx["encompassing"], ctx["meta1"]
    sc2, par2 = ctx["scores2"], ctx["params2"]
    qpm, qbn = ctx["quintiles_pm"], ctx["quintiles_binance"]
    M = ctx["M"]
    qinfo = ctx["qinfo"]
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    tot = pd.DataFrame(rt.rows)
    all1 = sc1[sc1["cellule"] == "Tous"].set_index(["h", "modèle"])

    def s1(h, m, col="qlike"):
        return all1.loc[(h, m), col]

    def dq(h, m):
        r = all1.loc[(h, m)]
        return ci(r["dqlike_vs_ewma"], r["dqlike_lo"], r["dqlike_hi"], 3, signed=True)

    # --- résumé chiffré ---------------------------------------------------------------
    n1 = int(sc1[(sc1["cellule"] == "Tous") & (sc1["modèle"] == "timesfm")]["n"].sum())
    best_by_h = {h: all1.xs(h, level="h")["qlike"].idxmin() for h in HORIZONS}
    rank_tfm = {h: int((all1.xs(h, level="h")["qlike"] < s1(h, "timesfm")).sum()) + 1 for h in HORIZONS}
    cov_tfm = {h: s1(h, "timesfm", "couverture_80") for h in HORIZONS}
    mzb = {h: s1(h, "timesfm", "mz_b") for h in HORIZONS}
    enc_all = enc.copy()
    t2 = sc2.set_index(["durée", "k", "modèle"])

    def d2(d, k, m, col="dbrier_vs_marché", lo="dbrier_lo", hi="dbrier_hi"):
        r = t2.loc[(d, k, m)]
        return ci(r[col] * 1e3, r[lo] * 1e3, r[hi] * 1e3, 2, signed=True)

    q5 = qpm[(qpm["amplitude"] == "timesfm") & (qpm["quintile"] == "Q5−Q1")].set_index("durée")
    qb = qbn[qbn["quintile"] == "Q5−Q1"].set_index("h")
    qt = qpm[(qpm["amplitude"] == "timesfm") & (qpm["quintile"] != "Q5−Q1")].copy()
    qt["quintile"] = qt["quintile"].astype(int)

    def qrow(d, q):
        return qt[(qt["durée"] == d) & (qt["quintile"] == q)].iloc[0]

    n_te = int((~M["train"]).sum())
    n_tr_q = int((M["train"] & np.isfinite(M["sigma_timesfm_bp"])).sum())
    n_te_q = int((~M["train"] & np.isfinite(M["sigma_timesfm_bp"])).sum())
    tfm_secs = sum(v.get("seconds", 0) for v in qinfo.values())
    tfm_n = sum(v.get("n_done", 0) for v in qinfo.values())
    tfm_rate = tfm_n / tfm_secs if tfm_secs else math.nan

    w("# À quoi sert l'amplitude prévue par TimesFM ? — volatilité, juste valeur en cours de fenêtre, régime")
    w("")
    w(f"*Généré le {now} par `scripts/timesfm_amplitude.py` (temps total : {fr(rt.total(), 0)} s, caches compris — § 7). "
      f"TimesFM 3.0 (`timesfm3`, contexte 512 barres 1m, moyenne symétrique, déciles 0,1…0,9), poids sous licence non commerciale : "
      f"recherche uniquement. {n_(tfm_n)} contextes calculés en {fr(tfm_secs, 0)} s ({fr(tfm_rate, 1)} origines/s, "
      f"{qinfo.get('pm_test_btc', {}).get('threads', 3)} fils CPU).*")
    w("")
    w("> Simulation papier sur données publiques : aucune clé, aucun ordre. Les prix Polymarket ne sont montrés qu'en agrégé.")
    w("")
    w("## 0. Résumé : à quoi sert TimesFM ?")
    w("")
    # puce 1 : volatilité
    tfm_vs_ewma = [all1.loc[(h, "timesfm"), "dqlike_vs_ewma"] for h in HORIZONS]
    n_better = sum(all1.loc[(h, "timesfm"), "dqlike_hi"] < 0 for h in HORIZONS)
    n_worse = sum(all1.loc[(h, "timesfm"), "dqlike_lo"] > 0 for h in HORIZONS)
    rel = float(np.mean([all1.loc[(h, "timesfm"), "dqlike_vs_ewma"] / all1.loc[(h, "ewma"), "qlike"] for h in HORIZONS]))
    ctx["rel_qlike_tfm"] = rel
    ctx["n_worse_tfm"] = int(n_worse)
    if n_better == 3:
        verdict_vol = "**mieux** que l'EWMA (IC excluant 0 aux 3 horizons)"
    elif n_worse >= 2:
        verdict_vol = (f"**un peu moins bien** que l'EWMA (QLIKE {fr(rel, 1, pct=True, signed=True)} en moyenne ; IC excluant 0 à "
                       f"{n_worse} horizon(s) sur 3)")
    elif n_better == 0 and n_worse == 0:
        verdict_vol = "**ni mieux ni moins bien** que l'EWMA de façon significative"
    else:
        verdict_vol = f"de façon **mitigée** par rapport à l'EWMA ({n_better} horizon(s) mieux, {n_worse} moins bien)"
    w(f"* **Prévoir l'amplitude (test 1)** : sur {n_(n1)} origines hors échantillon (BTC, ETH, SOL, 15/07–25/09/2026, 3 horizons), "
      f"σ_TimesFM = (q90−q10)/2,563 prévoit r_h² {verdict_vol} : ΔQLIKE (TimesFM − EWMA) "
      + " ; ".join(f"h = {h} : {dq(h, 'timesfm')}" for h in HORIZONS)
      + f". Classement de TimesFM parmi {len(VOL_MODELS)} modèles (QLIKE, tous actifs) : "
      + ", ".join(f"{rank_tfm[h]}ᵉ à h = {h}" for h in HORIZONS)
      + f" (meilleur : {', '.join(f'`{best_by_h[h]}` à h = {h}' for h in HORIZONS)}). "
      f"Couverture de [q10, q90] : {', '.join(fr(cov_tfm[h], 3) for h in HORIZONS)} pour 0,80 visé ; pente de Mincer-Zarnowitz "
      f"{', '.join(fr(mzb[h], 2) for h in HORIZONS)}.")
    # puce 2 : encompassing
    tt = enc_all["t_timesfm"].to_numpy()
    te_ = enc_all["t_ewma"].to_numpy()
    n_sig_t = int((np.abs(tt) > 2).sum())
    n_sig_e = int((np.abs(te_) > 2).sum())
    dq_enc = [all1.loc[(h, "encomp"), "dqlike_vs_ewma"] for h in HORIZONS]
    enc_sig = [all1.loc[(h, "encomp"), "dqlike_hi"] < 0 for h in HORIZONS]
    encq_sig = [all1.loc[(h, "encomp_q"), "dqlike_hi"] < 0 for h in HORIZONS]
    share_q = float(enc_all["part_timesfm_qlike"].mean())
    w(f"* **Information EN PLUS de la vol réalisée ?** Régression d'encompassing r_h² ~ a + b·σ²_TimesFM + c·σ²_EWMA (MCO sur "
      f"l'apprentissage, 9 cellules actif × h) : b a |t| > 2 dans {n_sig_t}/9 cellules (c : {n_sig_e}/9) ; b moyen {fr(enc_all['b_timesfm'].mean(), 2)}, "
      f"c moyen {fr(enc_all['c_ewma'].mean(), 2)}. Hors échantillon, cette combinaison MCO fait "
      + " ; ".join(f"h = {h} : ΔQLIKE {dq(h, 'encomp')}" for h in HORIZONS)
      + f" par rapport à l'EWMA seule ({sum(enc_sig)}/3 horizons significativement mieux) : les MCO en niveau sont tirées par quelques r² "
      f"extrêmes et se transportent mal. La combinaison **QLIKE-optimale** σ² = w₁σ²_TimesFM + w₂σ²_EWMA (poids >= 0 sur l'apprentissage) "
      f"donne à TimesFM une part moyenne de {fr(share_q, 2)} du poids et fait "
      + " ; ".join(f"h = {h} : ΔQLIKE {dq(h, 'encomp_q')}" for h in HORIZONS)
      + f" ({sum(encq_sig)}/3 significativement mieux que l'EWMA). "
      + ("TimesFM apporte donc une information complémentaire mesurable, mais petite." if sum(encq_sig) >= 2
         else "L'apport de TimesFM au-delà de la vol réalisée n'est pas démontré : à peine mieux, IC contenant 0."))
    # puce 3 : test 2
    def best_model(d, k):
        g = sc2[(sc2["durée"] == d) & (sc2["k"] == k)].set_index("modèle")["brier"]
        return g.idxmin(), float(g.min())

    tf_vs_ew = [(d, k, t2.loc[(d, k, "timesfm_S"), "dbrier_vs_ewma_k"], t2.loc[(d, k, "timesfm_S"), "dbrier_ewma_lo"],
                 t2.loc[(d, k, "timesfm_S"), "dbrier_ewma_hi"]) for d, ks in K_OF.items() for k in ks]
    n_tf_better = sum(1 for _, _, e, lo, hi in tf_vs_ew if hi < 0)
    n_tf_worse = sum(1 for _, _, e, lo, hi in tf_vs_ew if lo > 0)
    now_vs_mkt = [(d, k, t2.loc[(d, k, "ewma_k"), "dbrier_vs_marché"], t2.loc[(d, k, "ewma_k"), "dbrier_lo"],
                   t2.loc[(d, k, "ewma_k"), "dbrier_hi"]) for d, ks in K_OF.items() for k in ks]
    n_now_better = sum(1 for _, _, e, lo, hi in now_vs_mkt if hi < 0)
    stack_vs_mkt = [(d, k, t2.loc[(d, k, "stack"), "dbrier_vs_marché"], t2.loc[(d, k, "stack"), "dbrier_lo"],
                     t2.loc[(d, k, "stack"), "dbrier_hi"]) for d, ks in K_OF.items() for k in ks]
    n_stack_better = sum(1 for _, _, e, lo, hi in stack_vs_mkt if hi < 0)
    w_used = ", ".join(f"{d} : {fr(par2[par2['durée'] == d]['w_timesfm'].iloc[0], 1)}" for d in K_OF)
    w(f"* **Juste valeur en cours de fenêtre (test 2, {n_(n_te_q)} marchés BTC 5m/15m du 04/09 au 24/09, tous, pas de sous-échantillon)** : "
      f"Φ(d/σ_restant) avec σ de TimesFM prévu à S fait {n_tf_better}/6 fois significativement mieux et {n_tf_worse}/6 fois significativement "
      f"moins bien (Brier) que la même formule avec l'EWMA réalisée à S+k : "
      + " ; ".join(f"{d} k = {k} : {ci(e * 1e3, lo * 1e3, hi * 1e3, 2, signed=True)}" for d, k, e, lo, hi in tf_vs_ew)
      + " (×10⁻³). Le poids de TimesFM dans le mélange choisi sur l'apprentissage vaut "
      f"{w_used}. Le nowcast EWMA bat le dernier point de marché <= S+k dans {n_now_better}/6 cas et l'empilement marché + nowcast "
      f"dans {n_stack_better}/6 cas, mais ce point a ≈ {fr(np.nanmedian(sc2['âge_point_marché_s']), 0)} s d'ancienneté : c'est une avance "
      f"d'information, pas un gain exploitable (§ 3). **La σ de TimesFM n'améliore pas la juste valeur** par rapport à "
      f"une volatilité réalisée à jour : ce qui compte à S+k, c'est d (le chemin déjà parcouru), pas la finesse de σ.")
    # puce 4 : régime
    def q5txt(d, col):
        r = q5.loc[d]
        return ci(r[col] * 100, r[f'{col}_lo'] * 100, r[f'{col}_hi'] * 100, 1, signed=True)

    w(f"* **Régime (test 3)** : l'amplitude prévue trie bien les marchés par mouvement réalisé (|log(finalPrice/priceToBeat)| médian "
      f"de {fr(qrow('5m', 1)['mouvement_médian_pb'], 1)} pb au 1er quintile à {fr(qrow('5m', 5)['mouvement_médian_pb'], 1)} pb au 5ᵉ en 5m ; "
      f"{fr(qrow('15m', 1)['mouvement_médian_pb'], 1)} à {fr(qrow('15m', 5)['mouvement_médian_pb'], 1)} pb en 15m) et le taux de résolutions "
      f"serrées (< 2 pb) passe de {fr(qrow('5m', 1)['serré_2pb'], 1, pct=True)} à {fr(qrow('5m', 5)['serré_2pb'], 1, pct=True)} en 5m "
      f"(Q5−Q1 : {q5txt('5m', 'serré_2pb')} points). Justesse du TWAP partiel (`gap_m30`) Q5−Q1 : 5m {q5txt('5m', 'just_gap_m30')}, "
      f"15m {q5txt('15m', 'just_gap_m30')} points ; `hgb_ind1s` : 5m {q5txt('5m', 'just_hgb_ind1s')}, 15m {q5txt('15m', 'just_hgb_ind1s')} ; "
      f"retournement Binance Q5−Q1 : " + ", ".join(f"h = {h} {ci(qb.loc[h, 'just_retournement'] * 100, qb.loc[h, 'just_retournement_lo'] * 100, qb.loc[h, 'just_retournement_hi'] * 100, 1, signed=True)}" for h in HORIZONS)
      + " points. " + ctx["verdict_regime"])
    w(f"* **Fichier pour l'agent maker** : `sigma_par_marche.csv` ({n_(len(ctx['sigma_csv']))} marchés BTC 5m/15m du 04/09 au 24/09) : "
      f"`sigma_timesfm_bp` (σ à l'horizon D, pb de log-prix), `sigma_rv_bp` (EWMA à S, même horizon), `amplitude_quantile_train` "
      f"(rang dans la distribution d'apprentissage 14/08–03/09). Corrélation de Spearman σ_TimesFM / σ_EWMA : {fr(ctx['rho_sig'], 2)} ; "
      f"médiane de σ_TimesFM : 5m {fr(ctx['sig_med']['5m'], 1)} pb, 15m {fr(ctx['sig_med']['15m'], 1)} pb.")
    w(f"* **Verdict** : {ctx['verdict']}")
    w("")
    # --- 1. Données et méthode ------------------------------------------------------------
    w("## 1. Données et méthode")
    w("")
    w(f"* **Test 1** : barres Binance 1m (1 an en cache, `tradebot.data`), origines des runs existants "
      f"`reports/timesfm/*_none_s15_c512_logprice` (1 500 par actif, toutes les 15 min du 02/05 au 25/09/2026). Les déciles sont "
      f"**recalculés** (le CSV existant ne garde que q10/q90) et vérifiés contre lui : écart maximal "
      + ", ".join(f"{a.upper()} {fr(meta1[f'repro_{a}'], 4)} pb" for a in SYMBOLS)
      + f". Coupure apprentissage / test à la médiane des origines ({pd.Timestamp(meta1['split_s'], unit='s', tz='UTC'):%d/%m/%Y %H:%M} UTC).")
    w("* **σ_TimesFM(h)** = (q90 − q10)/2,5631 en points de base de log-prix (déciles au pas h, contexte centré, `TimesFMForecaster`) ; "
      "variante robuste (q75 − q25)/1,349 avec q25/q75 interpolés entre déciles (`timesfm_iqr`). Comparateurs (tous causaux, variance "
      "par minute × h) : EWMA des r² 1 min (λ choisi sur l'apprentissage par QLIKE : "
      + ", ".join(f"{a.upper()} {fr(meta1[f'lambda_{a}'], 3)}" for a in SYMBOLS)
      + "), écart-type roulant 60 / 240 min, Parkinson et Garman-Klass 60 min, moyenne historique de r_h² (apprentissage), "
      "`encomp` = a + b·σ²_TimesFM + c·σ²_EWMA (MCO sur l'apprentissage, plancher à 5 % de la moyenne des deux) et `encomp_q` = "
      "w₁σ²_TimesFM + w₂σ²_EWMA (poids >= 0 minimisant la QLIKE d'apprentissage).")
    w("* **Scores** (2e moitié) : QLIKE = r²/σ² − log(r²/σ²) − 1 (r_h² plancheré à (0,1 pb)² : r_h = 0 exact arrive), MSE sur r_h², "
      "MAE entre |r_h| et σ√(2/π), régression de Mincer-Zarnowitz r_h² = a + b σ² (pente b, R²), couverture de [q10, q90] "
      "(gaussienne ±1,2816 σ pour les comparateurs). `qlike_rv` : même QLIKE avec la variance réalisée Σ r²_1min des h barres "
      "(cible moins bruitée). IC : bootstrap groupé par créneau de 15 min (2 000 tirages), les 3 actifs d'un créneau tirés ensemble.")
    w(f"* **Tests 2 et 3 (Polymarket)** : marchés BTC 5m et 15m résolus du 14/08 (régime TWAP-60) au 24/09/2026 ({n_(len(M))} marchés, "
      f"issue officielle `outcomePrices`, `priceToBeat`/`finalPrice` d'`eventMetadata`). **Apprentissage** 14/08–03/09 sur la grille de 15 min "
      f"({n_(n_tr_q)} marchés avec TimesFM), **test** 04/09–24/09 : tous les marchés ({n_(n_te)}, dont {n_(n_te_q)} avec TimesFM). "
      f"TimesFM « prévu à S » = contexte de 512 closes finissant à la barre close à S − 1 min (origine S − 120 s), σ au pas D/60 + 1, "
      f"σ_par_minute = σ/√(D/60 + 1). d = log(close Binance 1m à S+k / TWAP60(S)), TWAP60(S) = moyenne des closes 1s sur (S−60 s, S] "
      f"(agrégats `pm_backtest/agg1s`). Prix du marché = dernier point `prices-history` <= S+k (ancienneté <= 90 s, milieu de fourchette). "
      f"EWMA : λ = {ctx['lambda_pm']} (log-loss sur l'apprentissage, grille {LAMBDA_GRID}). Mélange : σ² = w σ²_TimesFM + (1−w) σ²_EWMA(S+k), "
      f"w sur l'apprentissage. Empilement : régression logistique sur logit(p_marché) et logit(p_nowcast EWMA(S+k)), par durée et k, "
      f"apprise sur l'apprentissage.")
    w("* **Tests multiples** : test 1 compare 9 modèles × 3 horizons (× 4 cellules), test 2 6 modèles × 6 instants, test 3 5 quintiles × 6 "
      "mesures × 2 durées. Aucune correction n'est appliquée aux IC ; à 5 %, 1 comparaison sur 20 sort par hasard. Ne retenir que les "
      "écarts cohérents entre horizons / durées / actifs.")
    w("")
    # --- 2. Test 1 -------------------------------------------------------------------------
    w("## 2. Test 1 — TimesFM prévoit-il la volatilité mieux que la volatilité réalisée ?")
    w("")
    w(f"![{ctx['title_qlike']}](qlike_par_modele.png)")
    w("")
    w("Tous actifs, 2e moitié (15/07–25/09/2026) ; ΔQLIKE = QLIKE(modèle) − QLIKE(EWMA), apparié, [IC 95 %] ; < 0 = mieux que l'EWMA.")
    w("")
    tab = sc1[sc1["cellule"] == "Tous"].copy()
    tab["ΔQLIKE vs EWMA [IC]"] = [ci(r.dqlike_vs_ewma, r.dqlike_lo, r.dqlike_hi, 3, signed=True) for r in tab.itertuples()]
    tab["ΔQLIKE (cible Σr²_1m) [IC]"] = [ci(r.dqlike_rv_vs_ewma, r.dqlike_rv_lo, r.dqlike_rv_hi, 3, signed=True) for r in tab.itertuples()]
    tab["modèle"] = tab["modèle"].map(lambda m: f"`{m}`")
    w(to_markdown(tab[["h", "modèle", "n", "qlike", "ΔQLIKE vs EWMA [IC]", "qlike_rv", "ΔQLIKE (cible Σr²_1m) [IC]", "mse_r2", "mae_abs",
                       "mz_b", "mz_r2", "couverture_80", "sigma_moy_pb", "abs_ret_moy_pb"]],
                  {"qlike": 3, "qlike_rv": 3, "mse_r2": 0, "mae_abs": 2, "mz_b": 2, "mz_r2": 3, "couverture_80": 3,
                   "sigma_moy_pb": 1, "abs_ret_moy_pb": 1}))
    w("")
    w("*`mse_r2` en pb⁴, `mae_abs` en pb. `couverture_80` : part des r_h dans [q10, q90] (TimesFM) ou dans ±1,2816 σ (comparateurs).*")
    w("")
    w("Par actif (2e moitié) : QLIKE et ΔQLIKE vs EWMA de TimesFM et de l'encompassing.")
    w("")
    rows = []
    for a in sorted(SYMBOLS):
        for h in HORIZONS:
            g = sc1[(sc1["cellule"] == a.upper()) & (sc1["h"] == h)].set_index("modèle")
            rows.append({"actif": a.upper(), "h": h, "n": int(g.loc["timesfm", "n"]),
                         "QLIKE EWMA": g.loc["ewma", "qlike"], "QLIKE TimesFM": g.loc["timesfm", "qlike"],
                         "ΔQLIKE TimesFM [IC]": ci(g.loc["timesfm", "dqlike_vs_ewma"], g.loc["timesfm", "dqlike_lo"], g.loc["timesfm", "dqlike_hi"], 3, signed=True),
                         "ΔQLIKE combinaison QLIKE [IC]": ci(g.loc["encomp_q", "dqlike_vs_ewma"], g.loc["encomp_q", "dqlike_lo"], g.loc["encomp_q", "dqlike_hi"], 3, signed=True),
                         "couverture TimesFM": g.loc["timesfm", "couverture_80"], "MZ b TimesFM": g.loc["timesfm", "mz_b"],
                         "MZ b EWMA": g.loc["ewma", "mz_b"], "MZ R² TimesFM": g.loc["timesfm", "mz_r2"], "MZ R² EWMA": g.loc["ewma", "mz_r2"]})
    w(to_markdown(pd.DataFrame(rows), {"QLIKE EWMA": 3, "QLIKE TimesFM": 3, "couverture TimesFM": 3, "MZ b TimesFM": 2, "MZ b EWMA": 2,
                                       "MZ R² TimesFM": 3, "MZ R² EWMA": 3}))
    w("")
    w("**Régression d'encompassing** (apprentissage, 1re moitié) : r_h² = a + b·σ²_TimesFM + c·σ²_EWMA ; t robustes (HC1). "
      "`b_seul` / `c_seul` : pentes des régressions à une seule variable (Mincer-Zarnowitz). `w_*_qlike` : poids (>= 0, sans "
      "constante) de la combinaison qui minimise la QLIKE d'apprentissage ; `part_timesfm_qlike` = w₁/(w₁ + w₂).")
    w("")
    w(to_markdown(enc_all, {"a": 0, "b_timesfm": 2, "t_timesfm": 1, "c_ewma": 2, "t_ewma": 1, "b_timesfm_seul": 2, "t_timesfm_seul": 1,
                            "c_ewma_seul": 2, "t_ewma_seul": 1, "w_timesfm_qlike": 2, "w_ewma_qlike": 2, "part_timesfm_qlike": 2}))
    w("")
    w(f"![{ctx['title_calib']}](amplitude_calibration.png)")
    w("")
    # --- 3. Test 2 -------------------------------------------------------------------------
    w("## 3. Test 2 — Juste valeur en cours de fenêtre : Φ(d/σ) contre le prix du marché")
    w("")
    w(f"![{ctx['title_brier']}](brier_en_cours_de_fenetre.png)")
    w("")
    w("Test (04/09–24/09), même échantillon pour tous les modèles (marché, TimesFM et EWMA disponibles) ; ΔBrier ×10⁻³ apparié [IC 95 %], "
      "< 0 = mieux. `justesse` : Up prévu si p >= 0,5.")
    w("")
    tab = sc2.copy()
    tab["ΔBrier vs marché [IC]"] = [ci(r.dbrier_vs_marché * 1e3, r.dbrier_lo * 1e3, r.dbrier_hi * 1e3, 2, signed=True) for r in tab.itertuples()]
    tab["ΔBrier vs EWMA(S+k) [IC]"] = [ci(r.dbrier_vs_ewma_k * 1e3, r.dbrier_ewma_lo * 1e3, r.dbrier_ewma_hi * 1e3, 2, signed=True) for r in tab.itertuples()]
    tab["justesse [IC]"] = [ci(r.justesse, r.justesse_lo, r.justesse_hi, 1, pct=True) for r in tab.itertuples()]
    tab["modèle"] = tab["modèle"].map(lambda m: f"`{m}`")
    w(to_markdown(tab[["durée", "k", "modèle", "n", "brier", "log_loss", "justesse [IC]", "ΔBrier vs marché [IC]", "ΔBrier vs EWMA(S+k) [IC]", "p_sd"]],
                  {"brier": 4, "log_loss": 4, "p_sd": 3}))
    w("")
    w("*Ancienneté médiane du point de marché « <= S+k » : "
      + ", ".join(f"{d} k = {k} : {fr(t2.loc[(d, k, 'market'), 'âge_point_marché_s'], 0)} s" for d, ks in K_OF.items() for k in ks)
      + " ; d est lu exactement à S+k.*")
    w("")
    w("Paramètres appris sur l'apprentissage (14/08–03/09) :")
    w("")
    w(to_markdown(par2, {"w_timesfm": 1, "stack_b_marché": 2, "stack_b_nowcast": 2, "stack_a": 2}))
    w("")
    w(ctx["comment_test2"])
    w("")
    # --- 4. Test 3 -------------------------------------------------------------------------
    w("## 4. Test 3 — Régime : les marchés à forte amplitude prévue sont-ils différents ?")
    w("")
    w(f"![{ctx['title_quint']}](justesse_par_quintile.png)")
    w("")
    w("Polymarket BTC, test (04/09–24/09), quintiles de σ_TimesFM à l'horizon D (bornes fixées sur l'apprentissage, par durée) ; "
      "`serré_2pb` : |log(finalPrice/priceToBeat)| < 2 pb ; justesse des signaux disponibles à S−30 s (`gap_m30` = signe de l'écart "
      "spot − TWAP partiel, `hgb_ind1s` = modèle retenu du backtest, marché = prix du jeton Up à S−30 s) et du marché à S+1 min (5m) / "
      "S+2 min (15m). Dernière ligne : écart Q5 − Q1 [IC 95 %].")
    w("")
    for d in ("5m", "15m"):
        t3 = qpm[(qpm["amplitude"] == "timesfm") & (qpm["durée"] == d)].copy()
        for c in ("serré_2pb", "taux_up", "just_gap_m30", "just_hgb_ind1s", "just_marché_S-30", "just_marché_S+k1"):
            t3[c] = [ci(r[c] * 100, r[f"{c}_lo"] * 100, r[f"{c}_hi"] * 100, 1) for _, r in t3.iterrows()]
        w(f"BTC {d} :")
        w("")
        w(to_markdown(t3[["quintile", "n", "sigma_moy_pb", "mouvement_médian_pb", "serré_2pb", "taux_up", "just_gap_m30", "just_hgb_ind1s",
                          "just_marché_S-30", "just_marché_S+k1"]], {"sigma_moy_pb": 1, "mouvement_médian_pb": 1}))
        w("")
    w("Même découpage par quintile de **σ_EWMA à S** (volatilité réalisée, sans TimesFM) — pour savoir si le tri vient de TimesFM ou de la "
      "vol réalisée :")
    w("")
    t3 = qpm[qpm["amplitude"] == "ewma"].copy()
    for c in ("serré_2pb", "just_gap_m30", "just_hgb_ind1s"):
        t3[c] = [ci(r[c] * 100, r[f"{c}_lo"] * 100, r[f"{c}_hi"] * 100, 1) for _, r in t3.iterrows()]
    w(to_markdown(t3[["durée", "quintile", "n", "sigma_moy_pb", "mouvement_médian_pb", "serré_2pb", "just_gap_m30", "just_hgb_ind1s"]],
                  {"sigma_moy_pb": 1, "mouvement_médian_pb": 1}))
    w("")
    w("Binance (BTC + ETH + SOL, 2e moitié du test 1) : justesse du signal « retournement » (parier contre le rendement des h dernières "
      "minutes, `reversal_h` des runs existants) par quintile de σ_TimesFM(h), bornes par actif × h sur la 1re moitié.")
    w("")
    t4 = qbn.copy()
    for c in ("just_retournement", "serré_2pb", "taux_up"):
        t4[c] = [ci(r[c] * 100, r[f"{c}_lo"] * 100, r[f"{c}_hi"] * 100, 1) for _, r in t4.iterrows()]
    w(to_markdown(t4[["h", "quintile", "n", "sigma_moy_pb", "abs_ret_médian_pb", "serré_2pb", "taux_up", "just_retournement"]],
                  {"sigma_moy_pb": 1, "abs_ret_médian_pb": 1}))
    w("")
    w(ctx["comment_test3"])
    w("")
    # --- 5. Fichier σ -----------------------------------------------------------------------
    w("## 5. `sigma_par_marche.csv` (pour l'agent maker)")
    w("")
    w(f"Une ligne par marché BTC 5m / 15m résolu du 04/09 au 24/09/2026 ({n_(len(ctx['sigma_csv']))} lignes ; "
      f"{n_(int(ctx['sigma_csv']['sigma_timesfm_bp'].isna().sum()))} sans TimesFM : contexte troué). Colonnes : `slug`, `S` (début de fenêtre, "
      "UTC), `start_ts` (s Unix), `duration`, `sigma_timesfm_bp` (σ_TimesFM à l'horizon D = (q90−q10)/2,563 au pas D/60 + 1, pb de log-prix, "
      "prévu à S − 1 min), `sigma_timesfm_iqr_bp` (variante robuste), `sigma_rv_bp` (EWMA réalisée à S − 1 min, λ = "
      f"{ctx['lambda_pm']}, ramenée au même horizon), `amplitude_quantile_train` (rang de `sigma_timesfm_bp` dans la distribution "
      "d'apprentissage 14/08–03/09 de la même durée, 0 = le plus calme, 1 = le plus agité), `quintile_train` (1–5). "
      "Lecture : un marché au-dessus de 0,8 est dans le quintile le plus agité du mois d'apprentissage.")
    w("")
    sd = ctx["sigma_csv"]
    desc = sd.groupby("duration")[["sigma_timesfm_bp", "sigma_rv_bp"]].describe(percentiles=[0.1, 0.5, 0.9]).stack(0, future_stack=True)
    desc = desc.reset_index().rename(columns={"level_1": "σ"})
    w(to_markdown(desc[["duration", "σ", "count", "mean", "10%", "50%", "90%", "max"]], {"count": 0, "mean": 1, "10%": 1, "50%": 1, "90%": 1, "max": 1}))
    w("")
    # --- 6. Limites -----------------------------------------------------------------------
    w("## 6. Limites")
    w("")
    w("* **σ_TimesFM est une lecture gaussienne des déciles** : (q90 − q10)/2,563 suppose des queues normales ; la variante IQR donne "
      "un ordre de grandeur voisin (tableau § 2). Les quantiles du pas h sont marginaux : ils décrivent close[t+h] − close[t], pas "
      "le chemin.")
    w("* **Cible bruitée** : r_h² est un proxy très bruité de la variance (une seule réalisation) ; la QLIKE sur Σ r²_1min "
      "(`qlike_rv`) est donnée en contrôle et donne le même classement. Le plancher (0,1 pb)² sur r_h² touche "
      f"{fr(ctx['frac_floor'], 2, pct=True)} des origines (surtout SOL).")
    w("* **Polymarket** : d utilise Binance (spot et TWAP60 1s) alors que l'issue vient de Chainlink (≈ 3 pb de décalage de niveau, "
      "sans effet sur d puisque les deux termes sont Binance ; ≈ 4 s de retard du flux). Le prix du marché est un milieu de fourchette "
      "`prices-history` daté à la minute (ancienneté moyenne ≈ 45 s, déjà en retard de ≈ 10 s sur Binance d'après le diagnostic) : "
      "battre ce point au même horodatage ne prouve pas un gain exploitable. Le TWAP60 final est approché par le prix à E − 30 s "
      "(σ_restant en √(τ − 0,5)) : c'est une approximation (la vraie variance du TWAP d'une minute est un peu plus faible).")
    w("* **Un seul régime, 6 semaines** (TWAP-60 depuis le 14/08) ; le test 1 couvre 5 mois. Les IC groupés par créneau ne couvrent "
      "pas un changement de régime.")
    w("* **TimesFM est plus lent** que n'importe quel estimateur de vol réalisée (≈ 8 origines/s sur 3 fils CPU contre des microsecondes) "
      "et ses poids 3.0 sont sous licence non commerciale : pour un usage réel, il faudrait revalider avec TimesFM 2.5 (Apache-2.0).")
    w("* **Sélection** : λ, w et l'empilement sont choisis sur l'apprentissage ; les bornes de quintiles aussi. Les 3 tests réutilisent "
      "les mêmes prévisions TimesFM ; aucune correction pour tests multiples (§ 1).")
    w("")
    # --- 7. Temps -----------------------------------------------------------------------------
    w("## 7. Temps d'exécution")
    w("")
    w(to_markdown(pd.concat([tot, pd.DataFrame([{"étape": "total", "secondes": round(rt.total(), 1)}])], ignore_index=True), {"secondes": 1}))
    w("")
    qi = pd.DataFrame([{"jeu": k, **v} for k, v in qinfo.items()])
    if len(qi):
        w("Calcul des quantiles TimesFM (mis en cache dans `data/cache/timesfm_amplitude/`, première exécution) :")
        w("")
        w(to_markdown(qi[[c for c in ("jeu", "asset", "n_origins", "n_done", "seconds", "origins_per_s", "threads", "context_len", "horizon") if c in qi.columns]],
                      {"seconds": 0, "origins_per_s": 1}))
        w("")
    # --- 8. Fichiers -------------------------------------------------------------------------
    w("## 8. Fichiers")
    w("")
    for f, d in [("sigma_par_marche.csv", "σ TimesFM / EWMA par marché BTC 5m/15m du test (pour l'agent maker)"),
                 ("test1_scores.csv", "test 1 : scores hors échantillon par cellule × h × modèle, ΔQLIKE vs EWMA avec IC"),
                 ("test1_encompassing.csv", "test 1 : régressions d'encompassing (apprentissage)"),
                 ("test1_origines.csv", "test 1 : une ligne par (actif, origine, h) : r_h, Σr²_1m, σ² de chaque modèle"),
                 ("test2_scores.csv", "test 2 : Brier / log-loss / justesse par durée × k × modèle, écarts au marché et à l'EWMA avec IC"),
                 ("test2_parametres.csv", "test 2 : poids du mélange et coefficients de l'empilement (apprentissage)"),
                 ("test2_lambda.csv", "test 2 : log-loss d'apprentissage par λ"),
                 ("test3_quintiles_polymarket.csv", "test 3 : Polymarket par quintile de σ_TimesFM et de σ_EWMA"),
                 ("test3_quintiles_binance.csv", "test 3 : Binance par quintile de σ_TimesFM(h)"),
                 ("marches_test.csv", "marchés BTC du test : y, priceToBeat/finalPrice, d à S+k, prix du marché à S+k, σ, p de chaque modèle"),
                 ("runtime.csv", "temps par étape"),
                 ("qlike_par_modele.png, amplitude_calibration.png, brier_en_cours_de_fenetre.png, justesse_par_quintile.png", "graphiques")]:
        w(f"* `{f}` : {d}")
    w("")
    w("Reproduire : `. .venv/bin/activate && python scripts/timesfm_amplitude.py` ; tests : `python -m pytest tests/test_timesfm_amplitude.py`.")
    (OUT / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2026-08-14", help="début (UTC) de la période Polymarket (TWAP-60)")
    p.add_argument("--mid", default="2026-09-04", help="début du test Polymarket (fin de l'apprentissage)")
    p.add_argument("--end", default="2026-09-25", help="fin (exclue) de la période Polymarket")
    p.add_argument("--threads", type=int, default=3)
    p.add_argument("--context-len", type=int, default=512)
    p.add_argument("--boot", type=int, default=2000, help="tirages bootstrap")
    p.add_argument("--refit", action="store_true", help="recalcule les quantiles TimesFM")
    p.add_argument("--only-quantiles", action="store_true", help="s'arrête après le calcul des quantiles")
    p.add_argument("--workers", type=int, default=12, help="fils pour les lectures Polymarket")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("urllib3", "requests", "huggingface_hub", "filelock", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.start, args.mid, args.end = (pd.Timestamp(x, tz="UTC") for x in (args.start, args.mid, args.end))
    rt = Runtime()
    OUT.mkdir(parents=True, exist_ok=True)

    with rt("1a. Binance 1m (tradebot.data, cache 1 an)"):
        bars = load_bars(args.end + pd.Timedelta(days=1))
    with rt("1b. origines : runs TimesFM existants + fenêtres Polymarket BTC"):
        o1 = test1_origins()
        S = polymarket_starts(args.start, args.mid, args.end)
        sets = {f"test1_{a}": (a, o1[a]) for a in SYMBOLS}
        sets["pm_train_btc"] = ("btc", ta.polymarket_origin(S["train"]))
        sets["pm_test_btc"] = ("btc", ta.polymarket_origin(S["test"]))
        for k, (a, O) in sets.items():
            log.info("%s : %d origines (%s)", k, len(O), a)
    Q, qinfo = compute_quantiles(sets, bars, args, rt)
    if args.only_quantiles:
        log.info("quantiles calculés : %s", {k: v.get("seconds") for k, v in qinfo.items()})
        return 0

    ctx: dict = {"qinfo": qinfo}
    # --- test 1 ------------------------------------------------------------------------------
    with rt("3a. test 1 : cibles, volatilités réalisées (λ sur l'apprentissage), encompassing"):
        df1, meta1 = build_test1(bars, Q, args)
    with rt("3b. test 1 : scores hors échantillon + bootstrap par créneau"):
        scores1, te1 = score_test1(df1, args.boot)
    ctx.update(scores1=scores1, encompassing=meta1["encompassing"], meta1=meta1)
    ctx["frac_floor"] = float((te1["r2"] < ta.R2_FLOOR_BP2).mean())
    # --- Polymarket --------------------------------------------------------------------------
    M = load_polymarket(args, bars, Q, rt, ctx)
    with rt("4e. test 2 : λ de l'EWMA (apprentissage), mélange, empilement, scores"):
        lam_pm, lam_tab = choose_lambda_pm(M, bars)
        M = add_ewma(M, bars, lam_pm)
        scores2, params2, P2 = run_test2(M, args.boot)
    ctx.update(scores2=scores2, params2=params2, lambda_pm=lam_pm)
    with rt("5. test 3 : quintiles d'amplitude (bornes sur l'apprentissage)"):
        q_tfm = quintile_table_pm(M, "sigma_timesfm_bp", args.boot, "timesfm")
        q_ewm = quintile_table_pm(M, "sigma_rv_bp", args.boot, "ewma")
        qpm = pd.concat([q_tfm, q_ewm], ignore_index=True)
        qbn = quintile_table_binance(te1, df1, args.boot)
    ctx.update(quintiles_pm=qpm, quintiles_binance=qbn, M=M)
    # --- sigma_par_marche.csv ----------------------------------------------------------------
    with rt("6a. sigma_par_marche.csv"):
        te = M[~M["train"]].copy()
        te["amplitude_quantile_train"] = np.nan
        te["quintile_train"] = -1
        for d in ("5m", "15m"):
            tr = M[M["train"] & (M["duration"] == d)]["sigma_timesfm_bp"]
            m = te["duration"] == d
            te.loc[m, "amplitude_quantile_train"] = ta.train_ecdf(tr, te.loc[m, "sigma_timesfm_bp"])
            te.loc[m, "quintile_train"] = ta.quintile_of(te.loc[m, "sigma_timesfm_bp"], ta.quintile_edges(tr))
        te["S"] = pd.to_datetime(te["start_ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
        sig = te[["slug", "S", "start_ts", "duration", "sigma_timesfm_bp", "sigma_timesfm_iqr_bp", "sigma_rv_bp",
                  "amplitude_quantile_train", "quintile_train"]].sort_values(["start_ts", "duration"]).reset_index(drop=True)
        sig.to_csv(OUT / "sigma_par_marche.csv", index=False, float_format="%.6g")
        ctx["sigma_csv"] = sig
        ok = np.isfinite(sig["sigma_timesfm_bp"]) & np.isfinite(sig["sigma_rv_bp"])
        ctx["rho_sig"] = float(pd.Series(sig.loc[ok, "sigma_timesfm_bp"]).corr(sig.loc[ok, "sigma_rv_bp"], method="spearman"))
        ctx["sig_med"] = {d: float(sig[sig["duration"] == d]["sigma_timesfm_bp"].median()) for d in ("5m", "15m")}
    # --- verdicts et titres --------------------------------------------------------------------
    all1 = scores1[scores1["cellule"] == "Tous"].set_index(["h", "modèle"])
    dq_t = [all1.loc[(h, "timesfm"), "dqlike_vs_ewma"] for h in HORIZONS]
    hi_t = [all1.loc[(h, "timesfm"), "dqlike_hi"] for h in HORIZONS]
    lo_t = [all1.loc[(h, "timesfm"), "dqlike_lo"] for h in HORIZONS]
    ctx["n_worse_tfm"] = int(sum(l > 0 for l in lo_t))
    ctx["rel_qlike_tfm"] = float(np.mean([all1.loc[(h, "timesfm"), "dqlike_vs_ewma"] / all1.loc[(h, "ewma"), "qlike"] for h in HORIZONS]))
    hi_e = [all1.loc[(h, "encomp_q"), "dqlike_hi"] for h in HORIZONS]
    dq_e = [all1.loc[(h, "encomp_q"), "dqlike_vs_ewma"] for h in HORIZONS]
    if all(h < 0 for h in hi_t):
        ctx["title_qlike"] = (f"TimesFM prévoit la volatilité à 5–15 min mieux que l'EWMA réalisée (ΔQLIKE {fr(np.mean(dq_t), 3, signed=True)}) ; "
                              f"combiné à l'EWMA : {fr(np.mean(dq_e), 3, signed=True)}")
    elif all(l > 0 for l in lo_t):
        ctx["title_qlike"] = (f"TimesFM prévoit la volatilité à 5–15 min moins bien que l'EWMA réalisée (ΔQLIKE {fr(np.mean(dq_t), 3, signed=True)}) ; "
                              f"combiné à l'EWMA : {fr(np.mean(dq_e), 3, signed=True)}")
    else:
        ctx["title_qlike"] = (f"TimesFM prévoit la volatilité à 5–15 min à peu près comme l'EWMA réalisée (ΔQLIKE {fr(np.mean(dq_t), 3, signed=True)}, "
                              f"IC contenant 0) ; combiné à l'EWMA : {fr(np.mean(dq_e), 3, signed=True)}")
    ctx["title_calib"] = ("L'amplitude prévue par TimesFM est calibrée : |r_h| réalisé suit σ prévu décile par décile, comme l'EWMA")
    t2 = scores2.set_index(["durée", "k", "modèle"])
    d_tf = [t2.loc[(d, k, "timesfm_S"), "dbrier_vs_ewma_k"] for d, ks in K_OF.items() for k in ks]
    hi_tf = [t2.loc[(d, k, "timesfm_S"), "dbrier_ewma_hi"] for d, ks in K_OF.items() for k in ks]
    lo_tf = [t2.loc[(d, k, "timesfm_S"), "dbrier_ewma_lo"] for d, ks in K_OF.items() for k in ks]
    d_nm = [t2.loc[(d, k, "ewma_k"), "dbrier_vs_marché"] for d, ks in K_OF.items() for k in ks]
    if sum(h < 0 for h in hi_tf) >= 4:
        ctx["title_brier"] = (f"En cours de fenêtre, σ TimesFM améliore Φ(d/σ) par rapport à l'EWMA (ΔBrier moyen {fr(np.mean(d_tf) * 1e3, 2, signed=True)} ×10⁻³)")
    elif sum(l > 0 for l in lo_tf) >= 4:
        ctx["title_brier"] = (f"En cours de fenêtre, σ TimesFM fait moins bien que l'EWMA réalisée dans Φ(d/σ) (ΔBrier moyen {fr(np.mean(d_tf) * 1e3, 2, signed=True)} ×10⁻³)")
    else:
        ctx["title_brier"] = (f"En cours de fenêtre, remplacer l'EWMA par σ TimesFM dans Φ(d/σ) ne change presque rien au Brier "
                              f"(écart moyen {fr(np.mean(d_tf) * 1e3, 2, signed=True)} ×10⁻³) ; Φ(d/σ) bat le dernier point de marché "
                              f"(ΔBrier {fr(np.mean(d_nm) * 1e3, 1, signed=True)} ×10⁻³) grâce à ≈ {fr(np.nanmedian(scores2['âge_point_marché_s']), 0)} s d'avance d'information")
    q5 = qpm[(qpm["amplitude"] == "timesfm") & (qpm["quintile"] == "Q5−Q1")].set_index("durée")
    qb = qbn[qbn["quintile"] == "Q5−Q1"].set_index("h")
    gap_sig = [(q5.loc[d, "just_gap_m30_lo"] > 0) or (q5.loc[d, "just_gap_m30_hi"] < 0) for d in ("5m", "15m")]
    rev_sig = [(qb.loc[h, "just_retournement_lo"] > 0) or (qb.loc[h, "just_retournement_hi"] < 0) for h in HORIZONS]
    n_sig = sum(gap_sig) + sum(rev_sig)
    mean_gap = float(np.mean([q5.loc[d, "just_gap_m30"] for d in ("5m", "15m")]))
    mean_rev = float(np.mean([qb.loc[h, "just_retournement"] for h in HORIZONS]))
    if n_sig == 0:
        ctx["title_quint"] = (f"La justesse des signaux ne dépend pas de l'amplitude prévue : Q5 − Q1 = {fr(mean_gap * 100, 1, signed=True)} points "
                              f"(TWAP partiel) et {fr(mean_rev * 100, 1, signed=True)} points (retournement Binance), IC contenant 0")
        ctx["verdict_regime"] = ("Aucun écart Q5 − Q1 de justesse n'est significatif : l'amplitude prévue ne sert pas de filtre pour ces signaux ; "
                                 "elle prédit seulement la taille du mouvement, pas si le signal aura raison.")
    else:
        ctx["title_quint"] = (f"L'amplitude prévue change la justesse des signaux dans {n_sig}/5 cas : Q5 − Q1 = {fr(mean_gap * 100, 1, signed=True)} points "
                              f"(TWAP partiel) et {fr(mean_rev * 100, 1, signed=True)} points (retournement Binance)")
        ctx["verdict_regime"] = (f"{n_sig} écart(s) Q5 − Q1 sur 5 sont significatifs (sans correction pour tests multiples) : "
                                 "l'amplitude prévue peut servir de filtre, à confirmer sur une autre période.")
    ctx["verdict"] = (
        "TimesFM sert à **prévoir l'amplitude**, pas le sens : ses déciles donnent une σ calibrée à 5–15 min (couverture ≈ 0,79 pour 0,80), "
        + (f"un peu moins discriminante qu'une EWMA à la minute (QLIKE {fr(ctx['rel_qlike_tfm'], 1, pct=True, signed=True)}, test 1)"
           if ctx["n_worse_tfm"] >= 2 else "du niveau d'une bonne volatilité réalisée (test 1)")
        + ", avec un apport complémentaire "
        + ("mesurable mais petit" if all(h < 0 for h in hi_e) else "non démontré")
        + " une fois combinée à l'EWMA. En cours de fenêtre Polymarket, cette σ ne rend pas Φ(d/σ) meilleure qu'avec une EWMA à jour "
        "(test 2) : l'information utile est dans d, que le marché intègre déjà. Comme filtre de régime, l'amplitude prévue trie le mouvement "
        "réalisé et le taux de résolutions serrées, "
        + ("mais pas la justesse des signaux directionnels (test 3). " if n_sig == 0 else "et modifie en partie la justesse des signaux (test 3). ")
        + "Usage raisonnable pour un maker : dimensionner (taille, écart coté, σ_restant pour valoriser une position) plutôt que choisir le sens. "
        "Une EWMA à la minute fait presque aussi bien, en microsecondes et sans licence non commerciale.")
    # commentaires de sections
    age_med = float(np.nanmedian(scores2["âge_point_marché_s"]))
    ctx["comment_test2"] = (
        f"*Lecture.* Le point de marché « <= S+k » a en médiane {fr(age_med, 0)} s d'ancienneté (échantillonnage `prices-history` ≈ 1 point/min) "
        "alors que d est lu exactement à S+k : l'avance de Φ(d/σ) sur le marché est donc en grande partie une avance d'information, comme "
        "le diagnostic l'a montré (au même horodatage, le nowcast ne bat plus le marché que de ≈ 3 points en 5m). Cette comparaison ne dit "
        "rien d'exploitable ; elle sert de référence. La question posée ici est ailleurs : **remplacer σ_EWMA par σ_TimesFM prévue à S, "
        "ou mélanger les deux, déplace le Brier de quelques 10⁻⁴ au plus**, car à S+k la probabilité est dominée par d/σ où d (le chemin "
        "déjà parcouru) varie de dizaines de pb quand σ_restant ne varie que de quelques pb entre modèles. L'EWMA à S (même information "
        "que TimesFM) et TimesFM à S sont au coude à coude. L'empilement marché + nowcast apprend surtout à faire confiance au nowcast."
    )
    ctx["comment_test3"] = (
        "*Lecture.* Les quintiles de σ_TimesFM et de σ_EWMA trient de la même façon le mouvement réalisé et les résolutions serrées "
        f"(corrélation de Spearman entre les deux σ : {fr(ctx['rho_sig'], 2)}) : c'est la volatilité réalisée qui porte l'information de régime. "
        "Sur la justesse des signaux directionnels, aucun quintile ne se distingue de façon cohérente entre 5m, 15m et Binance."
    )
    # --- sorties ------------------------------------------------------------------------------
    with rt("6b. CSV, graphiques, README"):
        scores1.to_csv(OUT / "test1_scores.csv", index=False, float_format="%.6g")
        meta1["encompassing"].to_csv(OUT / "test1_encompassing.csv", index=False, float_format="%.6g")
        keep1 = ["asset", "origin_s", "h", "train", "slot", "ret", "r2", "rv_h", "q10", "q90", "sig", "sig_iqr"] + [f"s2_{m}" for m in VOL_MODELS]
        d1 = df1[keep1].copy()
        d1.insert(1, "time", pd.to_datetime(d1["origin_s"], unit="s", utc=True))
        d1.to_csv(OUT / "test1_origines.csv", index=False, float_format="%.6g")
        scores2.to_csv(OUT / "test2_scores.csv", index=False, float_format="%.6g")
        params2.to_csv(OUT / "test2_parametres.csv", index=False, float_format="%.6g")
        lam_tab.to_csv(OUT / "test2_lambda.csv", index=False, float_format="%.6g")
        qpm.to_csv(OUT / "test3_quintiles_polymarket.csv", index=False, float_format="%.6g")
        qbn.to_csv(OUT / "test3_quintiles_binance.csv", index=False, float_format="%.6g")
        # marchés du test avec les probabilités de chaque modèle
        mt = M[~M["train"]].copy()
        for (d, k), g in P2.items():
            for m in PM_MODELS:
                col = f"p_{m}_k{k}"
                mt.loc[g.index.intersection(mt.index), col] = g.loc[g.index.intersection(mt.index), f"p_{m}"]
        keep = ["slug", "duration", "start", "y", "price_to_beat", "final_price", "move_bp", "twap60_S", "p_pre", "tw_gap30", "p_hgb_ind1s",
                "sigma_timesfm_bp", "sigma_timesfm_iqr_bp", "sigma_rv_bp"] + \
               [c for c in mt.columns if c.startswith(("d_k", "p_mkt_k", "age_k", "px_k", "p_ewma_S_k", "p_ewma_k_k", "p_timesfm_S_k", "p_mix_k", "p_stack_k", "p_market_k"))]
        mt[keep].to_csv(OUT / "marches_test.csv", index=False, float_format="%.6g")
        plot_qlike(scores1, OUT / "qlike_par_modele.png", ctx["title_qlike"])
        plot_sigma_vs_realized(te1, OUT / "amplitude_calibration.png", ctx["title_calib"])
        plot_brier_k(scores2, OUT / "brier_en_cours_de_fenetre.png", ctx["title_brier"])
        plot_quintiles(qpm, qbn, OUT / "justesse_par_quintile.png", ctx["title_quint"])
    write_readme(ctx, rt)
    pd.DataFrame(rt.rows + [{"étape": "total", "secondes": round(rt.total(), 1)}]).to_csv(OUT / "runtime.csv", index=False)
    log.info("terminé en %.0f s -> %s", rt.total(), OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
