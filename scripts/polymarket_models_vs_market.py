#!/usr/bin/env python
"""Backtest « nos modèles contre le marché » — marchés Polymarket crypto « Up or Down » 5m / 15m.

Simulation papier uniquement (aucune clé, aucun ordre). Usage (reproductible) ::

    . .venv/bin/activate
    python scripts/polymarket_models_vs_market.py                     # tout (caches réutilisés)
    python scripts/polymarket_models_vs_market.py --stage data        # collecte seule
    python scripts/polymarket_models_vs_market.py --timesfm 0         # sans TimesFM

Étapes : (1) marchés + issue officielle + prix du jeton Up à S − 30 s (Polymarket, cache) ;
(2) Binance 1m (1 an, cache ``data/cache``) et agrégats 1s par minute (cache
``data/cache/pm_backtest/agg1s``) ; (3) variables à S − 30 s et étiquette proxy VWAP ;
(4) entraînement (période antérieure au 14/08/2026, purgée) ; (5) TimesFM (optionnel) ;
(6) évaluation hors échantillon sur l'issue officielle ; (7) P&L ; (8) rapport
``reports/polymarket/modeles_vs_marche/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

from tradebot import polymarket as pm
from tradebot import polymarket_backtest as pb
from tradebot.config import CACHE_DIR, REPORTS_DIR

log = logging.getLogger("pm_models_vs_market")

PB_CACHE = CACHE_DIR / "pm_backtest"
OUT_DIR = REPORTS_DIR / "polymarket" / "modeles_vs_marche"
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
class Runtime:
    def __init__(self):
        self.rows: list[tuple[str, float]] = []

    @contextmanager
    def __call__(self, name: str):
        t = time.perf_counter()
        log.info("… %s", name)
        try:
            yield
        finally:
            dt = time.perf_counter() - t
            self.rows.append((name, dt))
            log.info("   %s : %.1f s", name, dt)


class CountingSession(requests.Session):
    """Session HTTP qui compte les codes de statut (pour surveiller les 429)."""

    def __init__(self, pool: int = 32):
        super().__init__()
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
        self.mount("https://", adapter)
        self.headers.update({"User-Agent": "tradebot/0.1 (read-only research; models_vs_market)"})
        self.status = Counter()
        self._lock = threading.Lock()

    def request(self, *args, **kwargs):
        r = super().request(*args, **kwargs)
        with self._lock:
            self.status[r.status_code] += 1
        return r


COLD_PATH = PB_CACHE / "cold_runtime.json"


def record_cold(key: str, seconds: float, note: str = "") -> None:
    """Mémorise la durée d'une étape exécutée SANS cache (1re exécution, à froid)."""
    try:
        d = json.loads(COLD_PATH.read_text()) if COLD_PATH.exists() else {}
    except ValueError:
        d = {}
    d[key] = {"secondes": round(float(seconds), 1), "note": note, "date": f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M}"}
    PB_CACHE.mkdir(parents=True, exist_ok=True)
    pm._atomic_write_text(COLD_PATH, json.dumps(d, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# 1. Polymarket
# ---------------------------------------------------------------------------
def priority_order(mk: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Groupe A (collecté d'abord) : tous les 15m + les 5m sur la grille de 10 min (1 sur 2) ;
    groupe B : les autres 5m. Si le budget de temps est dépassé, seul A est gardé."""
    a = (mk["duration"] == "15m") | ((mk["duration"] == "5m") & (mk["start_ts"] % 600 == 0))
    return mk[a], mk[~a]


def collect_polymarket(args, rt: Runtime) -> tuple[pd.DataFrame, dict]:
    session = CountingSession(pool=max(32, args.workers + 8))
    client = pm.PolymarketClient(session=session, max_rps=args.max_rps, use_cache=True, timeout=20.0,
                                 retries=4)
    info: dict = {}
    with rt("1a. liste des marchés + issue officielle (list_updown_markets)"):
        mk = pb.market_table(client, args.assets, args.durations, args.start, args.end, max_workers=8)
    expected = {(a, d): len(pm.slot_starts(d, args.start, args.end)) for a in args.assets for d in args.durations}
    info["expected"] = int(sum(expected.values()))
    info["found"] = len(mk)
    info["regimes"] = mk.groupby(["twap_lookback_s", "fee_type", "fee_rate", "fee_exponent"], dropna=False).size().to_dict()
    res = mk[mk["closed"] & mk["y"].notna()]
    info["resolved"] = len(res)

    cache_path = PB_CACHE / "pre_prices.parquet"
    cached = pd.read_parquet(cache_path) if cache_path.exists() else pd.DataFrame(
        columns=["slug", "p_pre", "p_pre_age_s", "n_hist", "error"])
    cached = cached[~cached["error"].astype(bool)]
    have = set(cached["slug"])
    todo = res[~res["slug"].isin(have)]
    if args.no_fetch:
        log.warning("--no-fetch : %d marchés sans prix en cache sont ignorés", len(todo))
        todo = todo.iloc[:0]
    grp_a, grp_b = priority_order(todo)
    log.info("prix S−30 s : %d en cache, %d à collecter (A=%d, B=%d)", len(have & set(res["slug"])),
             len(todo), len(grp_a), len(grp_b))
    t0 = time.perf_counter()
    budget_s = args.budget_min * 60
    new_parts = []
    state = {"stopped": False}

    chunk_t0 = [time.perf_counter()]

    def progress(k, n, label):
        if k % 1000 == 0 or k == n:
            el = time.perf_counter() - chunk_t0[0]
            rate = k / max(el, 1e-9)
            log.info("   [%s] %d/%d (%.1f marchés/s, reste ~%.0f s) statuts HTTP %s", label, k, n, rate,
                     (n - k) / max(rate, 1e-9), dict(session.status))

    def save(parts):
        if not parts:
            return
        allp = pd.concat([cached] + parts, ignore_index=True).drop_duplicates("slug", keep="last")
        PB_CACHE.mkdir(parents=True, exist_ok=True)
        pm._atomic_parquet(allp.reset_index(drop=True), cache_path)

    with rt(f"1b. prix du jeton Up à S−30 s (prices_history, {len(todo)} marchés hors cache)"):
        for label, grp in (("A", grp_a), ("B", grp_b)):
            if grp.empty:
                continue
            el = time.perf_counter() - t0
            if label == "B" and new_parts:
                rate = sum(len(p) for p in new_parts) / max(el, 1e-9)
                eta = len(grp) / max(rate, 1e-9)
                if el + eta > budget_s:
                    log.warning("budget de %d min dépassé (écoulé %.0f s, reste estimé %.0f s) : "
                                "échantillonnage — groupe B (%d marchés 5m) ignoré", args.budget_min, el, eta, len(grp))
                    state["stopped"] = True
                    info["sampled_out"] = grp["slug"].tolist()
                    break
            # sauvegarde par tranches pour pouvoir reprendre
            for i in range(0, len(grp), 4000):
                chunk = grp.iloc[i:i + 4000]
                chunk_t0[0] = time.perf_counter()
                part = pb.fetch_pre_open_prices(client, chunk, workers=args.workers,
                                                progress=lambda k, n, lb=f"{label}{i // 4000}": progress(k, n, lb))
                new_parts.append(part)
                save(new_parts)
    info["http_status"] = dict(session.status)
    info["fetch_seconds"] = time.perf_counter() - t0
    if len(todo) >= 1000:
        record_cold("prix Polymarket S−30 s", info["fetch_seconds"], f"{len(todo)} marchés à {args.max_rps:g} req/s max")
    allp = pd.read_parquet(cache_path) if cache_path.exists() else cached
    mk = mk.merge(allp.drop(columns=["error"]), on="slug", how="left")
    info["sampled"] = bool(state["stopped"])
    return mk, info


# ---------------------------------------------------------------------------
# 2. Binance
# ---------------------------------------------------------------------------
def load_binance(args, rt: Runtime) -> tuple[dict, dict]:
    from tradebot.data import load_universe

    with rt("2a. Binance 1m + dérivés (tradebot.data.load_universe, 1 an, cache)"):
        u = load_universe([SYMBOLS[a] for a in args.assets], interval="1m", days=365, cache=True)
        bars = {a: u[SYMBOLS[a]] for a in args.assets}
    with rt(f"2b. Binance 1s -> agrégats par minute ({args.s1_start:%Y-%m-%d} -> {args.end:%Y-%m-%d}, zips journaliers, cache)"):
        t1 = time.perf_counter()
        n_missing = sum(not (PB_CACHE / "agg1s" / f"{SYMBOLS[a]}_{d:%Y-%m-%d}.parquet").exists()
                        for a in args.assets for d in pd.date_range(args.s1_start, args.end - pd.Timedelta("1s"), freq="D"))
        agg = {a: pb.load_second_aggregates(SYMBOLS[a], args.s1_start, args.end, PB_CACHE / "agg1s", workers=4)
               for a in args.assets}
        if n_missing >= 30:
            record_cold("bougies 1s -> agrégats", time.perf_counter() - t1, f"{n_missing} jours × actif téléchargés")
    return bars, agg


def indicator_tables(args, bars: dict, rt: Runtime) -> dict[str, pd.DataFrame]:
    """Indicateurs causaux (tradebot.indicators, benchmark BTC pour eth/sol) lus à S − 120 s
    pour S sur la grille de ``step_min`` minutes (cache parquet par actif)."""
    from tradebot.indicators import compute_indicators

    step = 60 * args.step_min
    s0 = int((args.end - pd.Timedelta(days=360)).timestamp())
    s0 = -(-s0 // step) * step
    grid = np.arange(s0, int(args.end.timestamp()), step, dtype="int64")
    out = {}
    with rt(f"3a. indicateurs 1m (compute_indicators) à S − 2 min, grille {args.step_min} min"):
        for a in args.assets:
            path = PB_CACHE / f"ind_{SYMBOLS[a]}_{grid[0]}_{grid[-1]}_{step}.parquet"
            if path.exists() and not args.refit:
                out[a] = pd.read_parquet(path)
                continue
            df = bars[a]
            first_ok = int(df.index[0].timestamp()) + 2 * 86400
            if first_ok > grid[0]:
                log.warning("%s : historique 1m plus court que prévu (début %s)", a, df.index[0])
            bench = None if a == "btc" else bars["btc"]["close"]
            t0 = time.perf_counter()
            ind = compute_indicators(df, benchmark=bench)
            rows = pb.indicator_rows(ind, grid)
            rows.insert(0, "S", grid)
            del ind
            PB_CACHE.mkdir(parents=True, exist_ok=True)
            pm._atomic_parquet(rows, path)
            out[a] = rows
            log.info("   %s : %d lignes × %d indicateurs (%.1f s)", a, len(rows), rows.shape[1] - 1,
                     time.perf_counter() - t0)
            record_cold(f"indicateurs {a}", time.perf_counter() - t0, f"{len(rows)} lignes")
    return out


TW_COLS = ["tw_gap30", "tw_gap30_z", "tw_ret30", "tw_ret90", "tw_ret330", "tw_slope30", "tw_vwap_gap30",
           "tw_range30", "tw_taker30", "tw_vol30_rel", "tw_prev_gap", "sigma_1m"]
BASE_COLS = ["mom15_m60", "mom5_m60"]
CAL_COLS = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]


def assemble(D: int, args, bars: dict, agg: dict, ind_tabs: dict) -> tuple[pd.DataFrame, list[str]]:
    """Table (actif, S) : indicateurs à S − 2 min, TWAP partiel 1s à S − 30 s, baselines,
    indicatrices d'actif, étiquette proxy VWAP ``y`` et étiquette TWAP60 1s ``y1s`` (contrôle)."""
    parts = []
    ind_cols: list[str] = []
    for a in args.assets:
        t = ind_tabs[a]
        S = t["S"].to_numpy()
        ind_cols += [c for c in t.columns if c != "S" and c not in ind_cols]
        tw = pb.twap_partial_features(agg[a], bars[a], S, D)
        bf = pb.baseline_features(bars[a], S).astype("float32")
        y = pb.vwap_label(bars[a], S, D)
        t0 = pb.bar_lookup(agg[a], S - 60, "twap60")
        t1 = pb.bar_lookup(agg[a], S + D - 60, "twap60")
        y1s = np.where(np.isfinite(t0) & np.isfinite(t1), (t1 >= t0).astype(float), np.nan)
        meta = pd.DataFrame({"asset": a, "S": S, "y": y, "y1s": y1s})
        dums = pd.DataFrame({f"is_{b}": np.float32(a == b) for b in args.assets}, index=meta.index)
        parts.append(pd.concat([meta, t.drop(columns="S"), tw, bf, dums], axis=1))
    df = pd.concat(parts, ignore_index=True)
    for c in ind_cols:
        df[c] = df[c].astype("float32")
    return df, ind_cols


FRACS = (0.75, 0.10, 0.15)   # apprentissage / arrêt précoce / calibration isotonique (dans le temps)
MODEL_SPECS = {
    # nom : (algorithme, jeu de variables, période d'apprentissage)
    "logit_ind": ("logit", "ind", "full"),
    "hgb_ind": ("hgb", "ind", "full"),
    "logit_ind1s": ("logit", "ind1s", "s1"),
    "hgb_ind1s": ("hgb", "ind1s", "s1"),
    "logit_tw": ("logit", "tw", "s1"),
    "hgb_tw": ("hgb", "tw", "s1"),
}
MODEL_LABELS = {
    "market": "Marché (prix Up à S−30 s)",
    "logit_ind": "logit · indicateurs 1m",
    "hgb_ind": "HGB · indicateurs 1m",
    "logit_ind1s": "logit · indicateurs + TWAP partiel 1s",
    "hgb_ind1s": "HGB · indicateurs + TWAP partiel 1s",
    "logit_tw": "logit · TWAP partiel 1s (compact)",
    "hgb_tw": "HGB · TWAP partiel 1s (compact)",
    "stack": "empilement marché + meilleur modèle",
    "timesfm": "TimesFM (brut)",
    "timesfm_cal": "TimesFM recalibré",
    "gap_m30": "baseline : écart spot − TWAP partiel (momentum)",
    "rev15": "baseline : retournement 15 min",
    "always_up": "baseline : toujours Up",
    "mkt_gt_05": "baseline : marché > 0,5",
}


def feature_sets(ind_cols: list[str], assets: list[str]) -> dict[str, list[str]]:
    dums = [f"is_{a}" for a in assets]
    tw = TW_COLS + BASE_COLS + [c for c in CAL_COLS if c in ind_cols] + dums
    ind = ind_cols + BASE_COLS + dums
    return {"ind": ind, "ind1s": ind + TW_COLS, "tw": tw}


def fit_all(D: int, df: pd.DataFrame, ind_cols: list[str], args, rt: Runtime) -> tuple[pd.DataFrame, list[dict]]:
    """Entraîne les modèles pour la durée D ; renvoie les prévisions sur la période de test
    (toutes les origines S de la grille, 3 actifs) et une ligne d'info par modèle."""
    from threadpoolctl import threadpool_limits

    S = df["S"].to_numpy()
    y = df["y"].to_numpy()
    test_start, test_end = int(args.start.timestamp()), int(args.end.timestamp())
    te = (S >= test_start) & (S < test_end)
    fsets = feature_sets(ind_cols, args.assets)
    starts = {"full": int(S.min()), "s1": int(args.s1_start.timestamp()) + 3600}
    keep = ["asset", "S", "y", "y1s", "tw_gap30", "tw_gap30_z", "mom15_m60", "mom5_m60", "sigma_1m"]
    out = df.loc[te, keep].reset_index(drop=True)
    infos = []
    for name, (kind, fset, period) in MODEL_SPECS.items():
        t0 = time.perf_counter()
        cols = fsets[fset]
        per = (S >= starts[period]) & np.isfinite(y)
        if period == "s1":
            per &= np.isfinite(df["tw_gap30"].to_numpy())
        idx = np.flatnonzero(per)
        seg = pb.train_segments(S[idx], test_start, D, fracs=FRACS, train_start_s=starts[period])
        i_fit, i_es, i_cal = idx[seg["fit"]], idx[seg["es"]], idx[seg["cal"]]
        X = df[cols].to_numpy(dtype="float32")
        model = pb.ProbModel(kind=kind, max_iter=500 if kind == "hgb" else 1000, random_state=args.seed)
        with threadpool_limits(limits=args.threads):
            if kind == "hgb":
                model.fit(X[i_fit], y[i_fit], X[i_es], y[i_es])
            else:
                i_tr = np.concatenate([i_fit, i_es])
                model.fit(X[i_tr], y[i_tr])
            raw_cal = model.predict_raw(X[i_cal])
            model.calibrate(X[i_cal], y[i_cal])
            out[f"p_{name}"] = model.predict(X[te])
            out[f"praw_{name}"] = model.predict_raw(X[te])
        m_cal = pb.prob_metrics(y[i_cal], raw_cal)
        m_te = pb.prob_metrics(out["y"].to_numpy(), out[f"p_{name}"].to_numpy())
        b = seg["bounds"]
        ts = lambda v: pd.Timestamp(int(v), unit="s", tz="UTC")  # noqa: E731
        infos.append({
            "duration": f"{D // 60}m", "model": name, "algo": kind, "variables": fset, "n_variables": len(cols),
            "période": period, "fit_début": ts(b[0]), "es_début": ts(b[1]), "cal_début": ts(b[2]), "cal_fin": ts(b[3]),
            "n_fit": len(i_fit), "n_es": len(i_es), "n_cal": len(i_cal), "n_iter_hgb": model.n_iter_,
            "auc_cal_brut": m_cal["auc"], "acc_cal_brut": m_cal["accuracy"], "brier_cal_brut": m_cal["brier"],
            "auc_test_proxy": m_te["auc"], "acc_test_proxy": m_te["accuracy"], "brier_test_proxy": m_te["brier"],
            "p_test_sd": float(np.nanstd(out[f"p_{name}"])), "secondes": time.perf_counter() - t0,
        })
        log.info("   %s %s : fit %d / es %d / cal %d, AUC cal %.4f, AUC test (proxy) %.4f, %.1f s",
                 f"{D // 60}m", name, len(i_fit), len(i_es), len(i_cal), m_cal["auc"], m_te["auc"],
                 time.perf_counter() - t0)
        del X
    out["duration"] = f"{D // 60}m"
    return out, infos


# ---------------------------------------------------------------------------
# 5. TimesFM (optionnel)
# ---------------------------------------------------------------------------
def run_timesfm(args, bars: dict, M: pd.DataFrame, rt: Runtime) -> tuple[pd.DataFrame, dict]:
    """P(close à E >= close à S − 1 min) par TimesFM (contexte : 512 closes 1m finissant à la
    bougie close à S − 1 min), prob_up(seuil = dernière valeur), recalibrée par isotonique sur
    des origines BTC antérieures au test (étiquette proxy VWAP). Sous-échantillon régulier de
    marchés BTC 5m / 15m résolus (``M`` : tous les marchés résolus, avec ou sans prix S − 30 s)."""
    info = {"status": "sauté", "n": 0}
    if args.timesfm <= 0:
        return pd.DataFrame(), info
    path = PB_CACHE / f"timesfm_{args.timesfm}_{args.start:%Y%m%d}_{args.end:%Y%m%d}.parquet"
    if path.exists() and not args.refit:
        df = pd.read_parquet(path)
        info.update(status="cache", n=int(df["p_timesfm"].notna().sum()))
        return df, info
    import torch

    from tradebot.config import TimesFMConfig
    from tradebot.forecaster import TimesFMForecaster, calibrate_isotonic, prob_up, transform_context

    torch.set_num_threads(max(1, args.threads))
    cfg = TimesFMConfig()
    ctx_len = cfg.context_len
    btc = bars["btc"]
    close = btc["close"]
    ts = pb.to_unix(close.index)
    cvals = close.to_numpy(dtype="float64")

    def contexts(S: np.ndarray) -> tuple[list, np.ndarray]:
        pos = np.searchsorted(ts, S - 120)
        ok = (pos < len(ts)) & (ts[np.minimum(pos, len(ts) - 1)] == S - 120) & (pos >= ctx_len - 1)
        ok &= np.array([ts[p] - ts[p - ctx_len + 1] == 60 * (ctx_len - 1) if o else False for p, o in zip(pos, ok)])
        return [transform_context(cvals[p - ctx_len + 1:p + 1], cfg.context_transform) for p in pos[ok]], ok

    per_d = args.timesfm // 2
    rows = []
    fc = TimesFMForecaster(cfg)
    t_load = time.perf_counter()
    try:
        fc.load()
    except Exception as exc:  # poids absents / moteur indisponible
        info.update(status=f"échec du chargement ({type(exc).__name__}: {exc})")
        log.warning("TimesFM indisponible : %s", exc)
        return pd.DataFrame(), info
    info["load_s"] = time.perf_counter() - t_load
    t0 = time.perf_counter()
    n_done = 0
    total = 0
    plan = []
    for d in args.durations:
        D = pm.DURATION_SECONDS[d]
        # tirage régulier parmi TOUS les marchés BTC résolus (indépendant de la couverture des prix)
        mk = M[(M["asset"] == "btc") & (M["duration"] == d)].sort_values("start_ts")
        sel = mk.iloc[np.unique(np.linspace(0, len(mk) - 1, min(per_d, len(mk))).round().astype(int))]
        cal_lo = int((args.start - pd.Timedelta(days=28)).timestamp())
        cal_hi = int(args.start.timestamp()) - D - 3600
        cal_S = np.arange(-(-cal_lo // D) * D, cal_hi, D)
        cal_S = cal_S[np.unique(np.linspace(0, len(cal_S) - 1, min(per_d, len(cal_S))).round().astype(int))]
        plan.append((d, D, sel, cal_S))
        total += len(sel) + len(cal_S)
    budget = args.timesfm_budget_min * 60
    for d, D, sel, cal_S in plan:
        h = D // 60 + 1        # de la bougie close à S − 1 min jusqu'à la bougie close à E
        res = {}
        for tag, S in (("cal", cal_S), ("test", sel["start_ts"].to_numpy())):
            ctx, ok = contexts(S)
            p = np.full(len(S), np.nan)
            vals = []
            for c0 in range(0, len(ctx), 128):
                fb = fc.forecast(ctx[c0:c0 + 128], h)
                vals.append(prob_up(fb.quantiles[:, h - 1, :], fb.last_value))
                n_done += len(ctx[c0:c0 + 128])
                el = time.perf_counter() - t0
                eta = el / n_done * (total - n_done)
                log.info("   TimesFM %s %s : %d/%d contextes, %.0f s écoulées, reste ~%.0f s", d, tag, n_done, total, el, eta)
                if el + eta > budget:
                    info.update(status=f"interrompu : trop lent ({n_done / el:.2f} contextes/s, reste estimé {eta:.0f} s)")
                    log.warning("TimesFM : %s", info["status"])
                    return pd.DataFrame(), info
            if vals:
                p[ok] = np.concatenate(vals)
            res[tag] = (S, p)
        S_cal, p_cal = res["cal"]
        y_cal = pb.vwap_label(btc, S_cal, D)
        try:
            iso = calibrate_isotonic(p_cal, y_cal)
            p_te_cal = iso(res["test"][1])
        except ValueError as exc:          # trop peu d'origines de calibration
            log.warning("TimesFM %s : calibration impossible (%s)", d, exc)
            p_te_cal = np.full(len(res["test"][1]), np.nan)
        S_te, p_te = res["test"]
        rows.append(pd.DataFrame({"slug": sel["slug"].to_numpy(), "p_timesfm": p_te, "p_timesfm_cal": p_te_cal}))
        info[f"cal_{d}"] = pb.prob_metrics(y_cal, p_cal)
    df = pd.concat(rows, ignore_index=True)
    info.update(status="ok", n=int(df["p_timesfm"].notna().sum()), seconds=time.perf_counter() - t0,
                backend=cfg.backend, context_len=ctx_len)
    record_cold("TimesFM", info["seconds"], f"{total} contextes, {args.threads} fils")
    pm._atomic_parquet(df, path)
    return df, info


# ---------------------------------------------------------------------------
# 6. Évaluation hors échantillon (issue officielle)
# ---------------------------------------------------------------------------
def cells(M: pd.DataFrame):
    """(nom, masque) : Tous, Tous 5m, Tous 15m, puis actif × durée."""
    yield "Tous", np.ones(len(M), bool)
    for d in sorted(M["duration"].unique(), key=lambda x: pm.DURATION_SECONDS[x]):
        yield f"Tous {d}", (M["duration"] == d).to_numpy()
    for d in sorted(M["duration"].unique(), key=lambda x: pm.DURATION_SECONDS[x]):
        for a in sorted(M["asset"].unique()):
            yield f"{a.upper()} {d}", ((M["asset"] == a) & (M["duration"] == d)).to_numpy()


def scopes(M: pd.DataFrame, mid_ts: int):
    yield "période complète", np.ones(len(M), bool)
    yield "1re moitié (validation)", (M["start_ts"] < mid_ts).to_numpy()
    yield "2e moitié (test final)", (M["start_ts"] >= mid_ts).to_numpy()


def eval_table(M: pd.DataFrame, prob_models: list[str], signals: dict[str, np.ndarray], boot: pb.SlotBootstrap,
               mid_ts: int, auc_B: int = 200, cell_filter=None) -> pd.DataFrame:
    """Justesse, AUC, Brier, log-loss (+ IC bootstrap groupé par créneau de 15 min) et
    différences appariées avec le marché. ``prob_models`` : colonnes ``p_*`` (probabilités) ;
    ``signals`` : baselines {nom: score} (Up si score >= 0, AUC sur le score)."""
    y = M["y"].to_numpy(dtype="float64")
    pm_ = M["p_pre"].to_numpy(dtype="float64")
    hit_m = ((pm_ >= 0.5) == (y > 0.5)).astype(float)
    br_m = (pm_ - y) ** 2
    rows = []
    for scope, sm in scopes(M, mid_ts):
        for cell, cm in cells(M):
            if cell_filter is not None and not cell_filter(scope, cell):
                continue
            mask = sm & cm
            entries = [("market", pm_, pm_, None)] + [(m, M[f"p_{m}"].to_numpy(dtype="float64"), None, None)
                                                      for m in prob_models]
            entries += [(k, None, None, s) for k, s in signals.items()]
            for name, p, _, sig in entries:
                if sig is None:
                    ok = mask & np.isfinite(p)
                    hit = ((p >= 0.5) == (y > 0.5)).astype(float)
                    score = p
                else:
                    ok = mask & np.isfinite(sig)
                    hit = ((sig >= 0) == (y > 0.5)).astype(float)
                    score = sig
                n = int(ok.sum())
                if n == 0:
                    continue
                acc = boot.mean(hit, ok)
                dacc = boot.mean(hit - hit_m, ok)
                auc = boot.auc(y, score, ok, B=auc_B) if np.unique(score[ok]).size > 1 else (math.nan,) * 3
                r = {"échantillon": scope, "cellule": cell, "modèle": name, "n": n,
                     "justesse": acc[0], "justesse_ic_bas": acc[1], "justesse_ic_haut": acc[2],
                     "d_justesse_vs_marché": dacc[0], "d_justesse_ic_bas": dacc[1], "d_justesse_ic_haut": dacc[2],
                     "auc": auc[0], "auc_ic_bas": auc[1], "auc_ic_haut": auc[2]}
                if sig is None:
                    br = (p - y) ** 2
                    pc = np.clip(p, 1e-6, 1 - 1e-6)
                    ll = -(y * np.log(pc) + (1 - y) * np.log(1 - pc))
                    b = boot.mean(br, ok)
                    db = boot.mean(br - br_m, ok)
                    r.update({"brier": b[0], "brier_ic_bas": b[1], "brier_ic_haut": b[2],
                              "d_brier_vs_marché": db[0], "d_brier_ic_bas": db[1], "d_brier_ic_haut": db[2],
                              "logloss": float(np.mean(ll[ok])), "p_moyen": float(np.mean(p[ok])),
                              "p_sd": float(np.std(p[ok]))})
                rows.append(r)
    return pd.DataFrame(rows)


def calib_tables(M: pd.DataFrame, models: list[str], mask: np.ndarray) -> pd.DataFrame:
    parts = []
    y = M["y"].to_numpy(dtype="float64")
    for m in ["market"] + models:
        p = M["p_pre"].to_numpy(dtype="float64") if m == "market" else M[f"p_{m}"].to_numpy(dtype="float64")
        ok = mask & np.isfinite(p)
        c = pb.calibration_table(y[ok], p[ok], bins=10, quantile=True)
        c.insert(0, "modèle", m)
        parts.append(c)
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------
# 7. P&L
# ---------------------------------------------------------------------------
MARGINS = np.round(np.arange(0.0, 0.1001, 0.0025), 4)
EXTRA_COST = 0.017   # surcoût observé (diagnostic § 6 bis) : prix payé 0,524 contre 0,507 supposé


def pnl_stage(M: pd.DataFrame, prob_models: list[str], signals: dict[str, np.ndarray], boot: pb.SlotBootstrap,
              mid_ts: int, min_trades: int) -> dict[str, pd.DataFrame]:
    y = M["y"].to_numpy(dtype="float64")
    pmk = M["p_pre"].to_numpy(dtype="float64")
    fr, fe = M["fee_rate"].to_numpy(dtype="float64"), M["fee_exponent"].to_numpy(dtype="float64")
    val = (M["start_ts"] < mid_ts).to_numpy()
    tst = ~val
    curves, summ, trades = [], [], {}

    def summarize(name, mode, margin, t, chosen_on):
        rows = []
        for scope, sm in (("1re moitié (validation)", val), ("2e moitié (test final)", tst)):
            ok = sm & np.isfinite(t["pnl"].to_numpy())
            traded = (t["side"].to_numpy() != 0) & ok
            pnl = t["pnl"].to_numpy()
            n = int(traded.sum())
            per = boot.ratio(np.where(ok, pnl, np.nan), traded.astype(float), ok) if n else (math.nan,) * 3
            tot = boot.ratio(np.where(ok, pnl, np.nan), 1.0, ok)
            n_mk = int(ok.sum())
            rows.append({
                "modèle": name, "mode": mode, "marge": margin, "marge_choisie_sur": chosen_on, "échantillon": scope,
                "n_marchés": n_mk, "n_trades": n, "part_up": float((t["side"].to_numpy()[traded] > 0).mean()) if n else math.nan,
                "taux_gain": float(t["win"].to_numpy()[traded].mean()) if n else math.nan,
                "coût_moyen": float((t["price"] + t["fee"]).to_numpy()[traded].mean()) if n else math.nan,
                "pnl_total_usd": float(np.nansum(pnl[ok])),
                "pnl_total_ic_bas": tot[1] * n_mk, "pnl_total_ic_haut": tot[2] * n_mk,
                "pnl_par_part": per[0], "pnl_par_part_ic_bas": per[1], "pnl_par_part_ic_haut": per[2],
                "pnl_par_part_surcoût_1_7c": per[0] - EXTRA_COST if n else math.nan,
            })
        return rows

    for mode in ("taker", "maker"):
        for m in prob_models:
            p = M[f"p_{m}"].to_numpy(dtype="float64")
            for scope, sm in (("1re moitié (validation)", val), ("2e moitié (test final)", tst)):
                c = pb.pnl_by_margin(p, pmk, y, MARGINS, fr, fe, mode=mode, mask=sm & np.isfinite(p))
                c.insert(0, "échantillon", scope)
                c.insert(0, "mode", mode)
                c.insert(0, "modèle", m)
                curves.append(c)
            cv = curves[-2]
            mg = pb.choose_margin(cv, min_trades=min_trades)
            if not np.isfinite(mg):
                summ.append({"modèle": m, "mode": mode, "marge": math.nan, "marge_choisie_sur": "aucune (< min trades)",
                             "échantillon": "2e moitié (test final)", "n_trades": 0, "pnl_total_usd": 0.0})
                continue
            t = (pb.taker_trades(p, pmk, y, mg, fr, fe) if mode == "taker" else pb.maker_trades(p, pmk, y, mg))
            t.loc[~np.isfinite(p), ["side", "pnl"]] = [0, np.nan]
            summ += summarize(m, mode, mg, t, "1re moitié")
            trades[(m, mode)] = t
        # baselines : on achète toujours le côté prédit (pas de marge)
        for k, s in signals.items():
            p = np.where(np.isfinite(s), (s >= 0).astype(float), np.nan)
            t = (pb.taker_trades(p, pmk, y, -1.0, fr, fe) if mode == "taker" else pb.maker_trades(p, pmk, y, -1.0))
            summ += summarize(k, mode, math.nan, t, "— (toujours en position)")
            trades[(k, mode)] = t
    return {"curves": pd.concat(curves, ignore_index=True), "summary": pd.DataFrame(summ), "trades": trades}


def pnl_cells(M: pd.DataFrame, trades: dict, names: list[str], boot: pb.SlotBootstrap, mid_ts: int) -> pd.DataFrame:
    rows = []
    tst = (M["start_ts"] >= mid_ts).to_numpy()
    for name in names:
        t = trades.get((name, "taker"))
        if t is None:
            continue
        pnl = t["pnl"].to_numpy()
        traded = t["side"].to_numpy() != 0
        for cell, cm in cells(M):
            ok = tst & cm & np.isfinite(pnl)
            tr = ok & traded
            n = int(tr.sum())
            per = boot.ratio(np.where(ok, pnl, np.nan), tr.astype(float), ok) if n else (math.nan,) * 3
            rows.append({"modèle": name, "cellule": cell, "n_marchés": int(ok.sum()), "n_trades": n,
                         "taux_gain": float(t["win"].to_numpy()[tr].mean()) if n else math.nan,
                         "pnl_total_usd": float(np.nansum(pnl[ok])), "pnl_par_part": per[0],
                         "ic_bas": per[1], "ic_haut": per[2]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 8. Sorties : CSV et graphiques (style tradebot.report)
# ---------------------------------------------------------------------------
PERIOD = {"all": "", "h1": "", "h2": ""}   # libellés de période (remplis dans main)
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#4a3aa7"
BASELINE_COLORS = {"gap_m30": C_ORANGE, "rev15": C_YELLOW, "always_up": C_AQUA, "mkt_gt_05": C_VIOLET}


def fr_num(x, nd: int = 2, signed: bool = False, pct: bool = False) -> str:
    from tradebot.report import fmt_number

    return fmt_number(x, nd, signed=signed, pct=pct)


def cents(x, nd: int = 2, signed: bool = True) -> str:
    return "—" if x is None or not np.isfinite(x) else fr_num(100 * x, nd, signed=signed) + " c"


SHORT_LABELS = {
    "logit_ind": "logit · ind. 1m", "hgb_ind": "HGB · ind. 1m", "logit_ind1s": "logit · ind. + TWAP 1s",
    "hgb_ind1s": "HGB · ind. + TWAP 1s", "logit_tw": "logit · TWAP 1s", "hgb_tw": "HGB · TWAP 1s",
    "stack": "empilement marché + modèle", "gap_m30": "écart spot − TWAP partiel", "rev15": "retournement 15 min",
    "always_up": "toujours Up", "mkt_gt_05": "marché > 0,5", "market": "marché",
}


def label(m: str) -> str:
    return MODEL_LABELS.get(m, m)


def short(m: str) -> str:
    return SHORT_LABELS.get(m, m)


def spread_labels(values: list[float], gap: float) -> list[float]:
    """Positions verticales d'étiquettes directes, écartées d'au moins ``gap`` (ordre conservé)."""
    order = np.argsort(values)
    pos = np.array(values, dtype=float)
    last = -np.inf
    for i in order:
        pos[i] = max(pos[i], last + gap)
        last = pos[i]
    # recentre le paquet s'il a trop monté
    shift = (np.max(pos) - np.max(values)) / 2 if len(values) else 0
    return list(pos - max(shift, 0))


def plot_accuracy_brier(ev: pd.DataFrame, models: list[str], signals: list[str], path: Path, n_markets: int) -> str:
    from tradebot.report import BG, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    e = ev[(ev["échantillon"] == "période complète") & (ev["cellule"] == "Tous")].set_index("modèle")
    order = ["market"] + [m for m in models if m in e.index] + [s for s in signals if s in e.index]
    colors = [TEXT_2] + [C_BLUE] * len([m for m in models if m in e.index]) + [C_ORANGE] * len([s for s in signals if s in e.index])
    probs = [m for m in models if m in e.index]
    best = e.loc[probs, "d_brier_vs_marché"].idxmin()
    bb = e.loc[best]
    if bb["d_brier_ic_haut"] < 0:
        title = (f"{label(best)} prévoit un peu mieux que le prix du marché à S−30 s "
                 f"(Brier −{fr_num(-1e3 * bb['d_brier_vs_marché'], 1)} ×10⁻³), mais l'écart reste minuscule")
    else:
        title = "Aucun modèle ne prévoit nettement mieux que le prix du marché à S−30 s"
    sub = (f"Issue officielle Polymarket, {fr_num(n_markets, 0)} marchés BTC/ETH/SOL 5m+15m du {PERIOD['all']} "
           "(hors échantillon). Barres : IC 95 % bootstrap groupé par créneau de 15 min. Bleu : nos modèles ; "
           "orange : baselines ; gris : marché.")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 0.42 * len(order) + 0.9
    fig, axes = plt.subplots(1, 2, figsize=(W, H), sharey=True, facecolor=BG,
                             gridspec_kw={"width_ratios": [1, 1], "wspace": 0.08})
    fig.subplots_adjust(left=0.30, right=0.97, top=1 - hh / H, bottom=0.7 / H)
    _draw_header(fig, t, s)
    ypos = np.arange(len(order))[::-1]
    # panneau 1 : justesse − 50 %
    ax = axes[0]
    _style_axes(ax, ygrid=False, xgrid=True)
    for yv, m, c in zip(ypos, order, colors):
        r = e.loc[m]
        v = 100 * (r["justesse"] - 0.5)
        ax.barh(yv, v, height=0.55, color=c, zorder=2)
        ax.plot([100 * (r["justesse_ic_bas"] - 0.5), 100 * (r["justesse_ic_haut"] - 0.5)], [yv, yv], color=TEXT, lw=1.2, zorder=3)
        ax.text(max(v, 100 * (r["justesse_ic_haut"] - 0.5)) + 0.15, yv, fr_num(v, 1, signed=True), va="center", ha="left",
                fontsize=8.5, color=TEXT_2)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    ax.set_yticks(ypos)
    ax.set_yticklabels([label(m) for m in order], fontsize=9, color=TEXT)
    ax.set_xlabel("justesse − 50 % (points)", color=TEXT_2, fontsize=9)
    lo = min(-1.0, min(100 * (e.loc[m, "justesse_ic_bas"] - 0.5) for m in order) - 0.3)
    hi = max(100 * (e.loc[m, "justesse_ic_haut"] - 0.5) for m in order) + 1.2
    ax.set_xlim(lo, hi)
    # panneau 2 : ΔBrier contre le marché (probabilités seulement)
    ax = axes[1]
    _style_axes(ax, ygrid=False, xgrid=True)
    vals = []
    for yv, m, c in zip(ypos, order, colors):
        r = e.loc[m]
        if m == "market":
            ax.text(0.0, yv, "  référence (0)", va="center", ha="left", fontsize=8.5, color=TEXT_2)
            continue
        if not np.isfinite(r.get("d_brier_vs_marché", np.nan)):
            ax.text(0.0, yv, "  — (pas de probabilité)", va="center", ha="left", fontsize=8.5, color=TEXT_2)
            continue
        v = 1e3 * r["d_brier_vs_marché"]
        lo_, hi_ = 1e3 * r["d_brier_ic_bas"], 1e3 * r["d_brier_ic_haut"]
        vals += [lo_, hi_]
        ax.barh(yv, v, height=0.55, color=c, zorder=2)
        ax.plot([lo_, hi_], [yv, yv], color=TEXT, lw=1.2, zorder=3)
        ax.text(hi_ + 0.05, yv, fr_num(v, 2, signed=True), va="center", ha="left", fontsize=8.5, color=TEXT_2)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    if vals:
        ax.set_xlim(min(min(vals) - 0.3, -0.5), max(max(vals) + 1.2, 2.5))
    ax.set_xlabel("Brier modèle − Brier marché (×10⁻³), < 0 = mieux", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def plot_pnl_margin(curves: pd.DataFrame, summ: pd.DataFrame, names: list[str], path: Path) -> str:
    from tradebot.report import BG, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    cols = [C_BLUE, C_ORANGE, C_AQUA, C_VIOLET, C_YELLOW]
    c = curves[curves["mode"] == "taker"]
    chosen = summ[(summ["mode"] == "taker") & (summ["échantillon"] == "2e moitié (test final)")].set_index("modèle")
    best = names[0]
    mg = chosen.loc[best, "marge"] if best in chosen.index else math.nan
    pnl_te = chosen.loc[best, "pnl_total_usd"] if best in chosen.index else math.nan
    if np.isfinite(mg):
        verb = "gagne" if pnl_te > 0 else "perd"
        title = (f"La marge choisie sur la 1re moitié ({fr_num(100 * mg, 2)} c) : {label(best)} {verb} "
                 f"{fr_num(abs(pnl_te), 1)} $ sur la 2e moitié (1 part par marché)")
    else:
        title = f"{label(best)} : aucune marge ne garde assez de positions"
    sub = ("P&L total en $ pour 1 part par marché, preneur au prix ask ≈ milieu(S−30 s) + 0,005, frais crypto_fees_v2 "
           f"inclus. Gauche : 1re moitié ({PERIOD['h1']}), où la marge est choisie (EN ÉCHANTILLON) ; droite : "
           f"2e moitié ({PERIOD['h2']}), hors échantillon. Point : marge retenue.")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 4.6
    fig, axes = plt.subplots(1, 2, figsize=(W, H), sharey=True, facecolor=BG, gridspec_kw={"wspace": 0.06})
    fig.subplots_adjust(left=0.07, right=0.78, top=1 - (hh + 0.35) / H, bottom=0.75 / H)
    _draw_header(fig, t, s)
    for ax, scope, ttl in zip(axes, ("1re moitié (validation)", "2e moitié (test final)"),
                              ("1re moitié — choix de la marge (en échantillon)", "2e moitié — test final (hors échantillon)")):
        _style_axes(ax)
        ax.axhline(0, color=TEXT_2, lw=0.8)
        for m, col in zip(names, cols):
            d = c[(c["modèle"] == m) & (c["échantillon"] == scope)]
            ax.plot(100 * d["margin"], d["pnl_total"], color=col, lw=2, zorder=3)
            if m in chosen.index and np.isfinite(chosen.loc[m, "marge"]):
                mm = chosen.loc[m, "marge"]
                yy = d.loc[np.isclose(d["margin"], mm), "pnl_total"]
                if len(yy):
                    ax.plot([100 * mm], [yy.iloc[0]], "o", ms=7, color=col, mec=BG, mew=2, zorder=4)
        ax.set_title(ttl, fontsize=9.5, color=TEXT_2, loc="left")
        ax.set_xlabel("marge exigée au-delà de ask + frais (cents de probabilité)", color=TEXT_2, fontsize=9)
    axes[0].set_ylabel("P&L total ($)", color=TEXT_2, fontsize=9)
    ax = axes[1]
    ends = []
    for m, col in zip(names, cols):
        d = c[(c["modèle"] == m) & (c["échantillon"] == "2e moitié (test final)")]
        if len(d):
            ends.append((float(d["pnl_total"].iloc[-1]), m, float(100 * d["margin"].iloc[-1]), col))
    ylo, yhi = ax.get_ylim()
    pos = spread_labels([e[0] for e in ends], 0.06 * (yhi - ylo))
    for (v, m, x, col), yv in zip(ends, pos):
        ax.annotate(short(m), xy=(x, v), xytext=(x + 0.6, yv), fontsize=8.5, color=TEXT, va="center", ha="left",
                    annotation_clip=False, arrowprops=dict(arrowstyle="-", color=col, lw=1.2))
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], color=col, lw=2) for _, _, _, col in ends]
    leg = axes[0].legend(handles, [short(m) for _, m, _, _ in ends], loc="lower left", frameon=True, fontsize=8,
                         facecolor=BG, edgecolor=BG, framealpha=0.9)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    _save(fig, path)
    return title


def plot_cum_pnl(M: pd.DataFrame, trades: dict, best: str, mid_ts: int, path: Path, maker_ok: bool) -> str:
    from tradebot.report import BG, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    tst = (M["start_ts"] >= mid_ts).to_numpy()
    order = np.argsort(M["start_ts"].to_numpy()[tst], kind="stable")
    times = pd.to_datetime(M["start_ts"].to_numpy()[tst][order], unit="s", utc=True)
    series = [(best, "taker", C_BLUE, "-", f"{short(best)} (preneur)")]
    if maker_ok:
        series.append((best, "maker", C_BLUE, "--", f"{short(best)}, maker optimiste"))
    series += [(k, "taker", BASELINE_COLORS[k], "-", short(k)) for k in BASELINE_COLORS]
    finals = {}
    W = 11.0
    t_best = trades.get((best, "taker"))
    fin_best = float(np.nansum(t_best["pnl"].to_numpy()[tst])) if t_best is not None else math.nan
    base_fin = {k: float(np.nansum(trades[(k, "taker")]["pnl"].to_numpy()[tst])) for k in BASELINE_COLORS if (k, "taker") in trades}
    worst = min(base_fin, key=base_fin.get) if base_fin else None
    title = (f"Test final ({PERIOD['h2']}) : {label(best)} finit à {fr_num(fin_best, 1, signed=True)} $ ; "
             f"les baselines toujours en position perdent jusqu'à {fr_num(-base_fin[worst], 0)} $") if worst else "P&L cumulé"
    sub = ("P&L cumulé ($) pour 1 part par marché, 2e moitié de la période, preneur au prix ask ≈ milieu(S−30 s) + 0,005, "
           "frais inclus. Les baselines achètent le côté prédit à chaque marché ; le modèle ne prend position que si son "
           "avantage estimé dépasse la marge. Pointillés : exécution maker supposée certaine (borne haute).")
    t, s, hh = _header(W, title, sub)
    H = hh + 4.4
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.08, right=0.70, top=1 - hh / H, bottom=0.6 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    ax.axhline(0, color=TEXT_2, lw=0.8)
    ends = []
    for name, mode, col, ls, lab in series:
        tt = trades.get((name, mode))
        if tt is None:
            continue
        pnl = np.nan_to_num(tt["pnl"].to_numpy()[tst][order])
        cum = np.cumsum(pnl)
        ax.plot(times, cum, color=col, lw=2, ls=ls, zorder=3)
        ends.append([cum[-1], lab, col, ls])
        finals[lab] = cum[-1]
    # étiquettes directes à droite, écartées pour éviter les chevauchements
    ylo, yhi = ax.get_ylim()
    pos = spread_labels([e[0] for e in ends], 0.06 * (yhi - ylo))
    for (v, lab, col, ls), yv in zip(ends, pos):
        ax.annotate(f"{lab} : {fr_num(v, 1, signed=True)} $", xy=(times[-1], v), xytext=(times[-1] + pd.Timedelta(hours=10), yv),
                    fontsize=8.5, color=TEXT, va="center", ha="left", annotation_clip=False,
                    arrowprops=dict(arrowstyle="-", color=col, lw=1.2))
    from matplotlib.lines import Line2D

    leg = ax.legend([Line2D([], [], color=col, lw=2, ls=ls) for _, _, col, ls in ends], [e[1] for e in ends],
                    loc="lower left", frameon=True, fontsize=8, facecolor=BG, edgecolor=BG, framealpha=0.9)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    import matplotlib.dates as mdates

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.set_ylabel("P&L cumulé ($, 1 part par marché)", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def plot_calibration(calib: pd.DataFrame, best: str, path: Path) -> str:
    from tradebot.report import BG, GRID, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    W = 7.5
    b = calib[calib["modèle"] == best]
    wts = b["n"].to_numpy(dtype=float)
    xm = np.average(b["p_mean"], weights=wts)
    slope = float(np.sum(wts * (b["p_mean"] - xm) * (b["up_rate"] - np.average(b["up_rate"], weights=wts)))
                  / np.sum(wts * (b["p_mean"] - xm) ** 2))
    verdict = ("à peu près calibrées" if 0.7 <= slope <= 1.3 else
               "trop dispersées (surconfiance)" if slope < 0.7 else "trop prudentes")
    title = f"{short(best)} : probabilités {verdict}, pente de calibration {fr_num(slope, 2)}"
    sub = (f"Fréquence réalisée de « Up » (issue officielle) par décile de probabilité prévue (déciles de "
           f"{fr_num(b['p_mean'].min(), 3)} à {fr_num(b['p_mean'].max(), 3)}), {PERIOD['all']}. Diagonale : calibration "
           "parfaite ; pente < 1 = probabilités trop dispersées.")
    t, s, hh = _header(W, title, sub)
    H = hh + 5.0
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.12, right=0.95, top=1 - hh / H, bottom=0.65 / H)
    _draw_header(fig, t, s)
    _style_axes(ax, xgrid=True)
    both = calib[calib["modèle"].isin(["market", best])]
    lo = min(both["p_mean"].min(), both["up_rate"].min()) - 0.01
    hi = max(both["p_mean"].max(), both["up_rate"].max()) + 0.01
    ax.plot([lo, hi], [lo, hi], color=GRID, lw=1.2, zorder=1)
    for m, col, lab in (("market", TEXT_2, "marché (prix Up à S−30 s)"), (best, C_BLUE, label(best))):
        d = calib[calib["modèle"] == m]
        ax.plot(d["p_mean"], d["up_rate"], "-o", color=col, lw=2, ms=6, mec=BG, mew=1.5, zorder=3, label=lab)
    leg = ax.legend(loc="upper left", frameon=False, fontsize=8.5)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("probabilité prévue de « Up » (moyenne du décile)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("fréquence réalisée de « Up »", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def write_outputs(ctx: dict) -> None:
    out: Path = ctx["out_dir"]
    M, models = ctx["M"], ctx["models"]
    keep = ["slug", "asset", "duration", "start", "slot", "y", "y_proxy", "y1s", "p_pre", "p_pre_age_s", "n_hist",
            "fee_type", "fee_rate", "fee_exponent", "volume", "tw_gap30", "tw_gap30_z", "mom15_m60", "sigma_1m"]
    keep += [f"p_{m}" for m in models] + ["p_stack"] + [c for c in ("p_timesfm", "p_timesfm_cal") if c in M.columns]
    M[[c for c in keep if c in M.columns]].to_csv(out / "marches_predictions.csv", index=False, float_format="%.6g")
    ctx["ev"].to_csv(out / "metriques_hors_echantillon.csv", index=False, float_format="%.6g")
    if len(ctx["ev_tfm"]):
        ctx["ev_tfm"].to_csv(out / "metriques_timesfm.csv", index=False, float_format="%.6g")
    ctx["calib"].to_csv(out / "calibration.csv", index=False, float_format="%.6g")
    pnl = ctx["pnl"]
    pnl["curves"].to_csv(out / "pnl_vs_marge.csv", index=False, float_format="%.6g")
    pnl["summary"].to_csv(out / "pnl_resume.csv", index=False, float_format="%.6g")
    ctx["cellp"].to_csv(out / "pnl_par_cellule.csv", index=False, float_format="%.6g")
    ctx["train_info"].to_csv(out / "apprentissage.csv", index=False, float_format="%.6g")
    # P&L cumulé journalier (2e moitié)
    tst = (M["start_ts"] >= ctx["mid_ts"]).to_numpy()
    day = pd.to_datetime(M["start_ts"], unit="s", utc=True).dt.floor("D").to_numpy()[tst]
    rows = {}
    for (name, mode), t in pnl["trades"].items():
        rows[f"{name}:{mode}"] = pd.Series(np.nan_to_num(t["pnl"].to_numpy()[tst])).groupby(day).sum()
    daily = pd.DataFrame(rows).sort_index().cumsum()
    daily.index = pd.DatetimeIndex(daily.index).strftime("%Y-%m-%d")
    daily.rename_axis("jour").to_csv(out / "pnl_cumule_journalier.csv", float_format="%.6g")
    # graphiques
    titles = {}
    titles["acc"] = plot_accuracy_brier(ctx["ev"], models, list(ctx["signals"]), out / "justesse_brier_vs_marche.png", len(M))
    summ = pnl["summary"]
    tv = summ[(summ["mode"] == "taker") & summ["modèle"].isin(models) & (summ["échantillon"] == "1re moitié (validation)")]
    top = list(tv.sort_values("pnl_total_usd", ascending=False)["modèle"].head(3))
    best = ctx["best_pnl"]
    names = [best] + [m for m in top if m != best][:2] + ["stack"]
    titles["margin"] = plot_pnl_margin(pnl["curves"], summ, names, out / "pnl_vs_marge.png")
    maker_ok = (best, "maker") in pnl["trades"]
    titles["cum"] = plot_cum_pnl(M, pnl["trades"], best, ctx["mid_ts"], out / "pnl_cumule_test.png", maker_ok)
    titles["calib"] = plot_calibration(ctx["calib"], ctx["best_ll"], out / "calibration.png")
    ctx["titles"] = titles


def make_verdict(ctx: dict) -> str:
    """Verdict en une phrase, choisi à partir des IC (P&L hors échantillon et Brier)."""
    summ, ev = ctx["pnl"]["summary"], ctx["ev"]
    best = ctx["best_pnl"]
    r = summ[(summ["modèle"] == best) & (summ["mode"] == "taker") & (summ["échantillon"] == "2e moitié (test final)")]
    e = ev[(ev["échantillon"] == "période complète") & (ev["cellule"] == "Tous")].set_index("modèle")
    probs = [m for m in ctx["models"] if m in e.index]
    better = [m for m in probs if e.loc[m, "d_brier_ic_haut"] < 0]
    txt = ""
    if len(r) and np.isfinite(r.iloc[0].get("pnl_par_part_ic_bas", np.nan)) and r.iloc[0]["pnl_par_part_ic_bas"] > 0:
        rr = r.iloc[0]
        txt = (f"`{best}` est rentable sur la 2e moitié ({cents(rr['pnl_par_part'])} par part, IC > 0, "
               f"{fr_num(rr['n_trades'], 0)} positions), mais avec un prix d'exécution optimiste : il faut le confirmer en "
               "papier sur le carnet réel avant toute conclusion.")
    else:
        txt = ("rien ne bat le marché de façon exploitable après frais, hors échantillon. ")
        if better:
            txt += (f"En probabilité, {', '.join('`' + m + '`' for m in better)} font significativement mieux que le prix "
                    "à S−30 s (Brier), mais l'écart est de l'ordre de 10⁻³ : il ne couvre pas le coût d'entrée (ask ≈ 0,505 + "
                    "frais 0,0175, soit un seuil ≈ 52,3 %). ")
        txt += ("Le peu d'information disponible à S−30 s vient du TWAP partiel 1s (mécanique de la règle TWAP), pas des "
                "indicateurs 1m.")
    return txt


def make_limits(ctx: dict) -> list[str]:
    chk = ctx["chk"]
    return [
        "**Prix d'exécution approché** : le point `prices-history` est un milieu de fourchette (ancienneté médiane "
        f"{fr_num(chk['p_pre_age_médian_s'], 0)} s) ; ask = milieu + 0,005 suppose le carnet 0,50 / 0,51. Le diagnostic "
        "(§ 6 bis) a mesuré un prix réellement payé plus élevé d'environ 1,7 c dans [S−30 s, S). La profondeur n'est pas "
        "modélisée (1 part par marché ; quelques centaines de parts au plus au meilleur prix).",
        "**Étiquette d'entraînement proxy** (VWAP 1m, ≈ 96,5 % d'accord avec l'issue officielle) : les modèles et leur "
        "calibration isotonique visent le proxy, pas l'issue Chainlink ; l'évaluation, elle, porte sur l'issue officielle.",
        "**Période de test courte et unique** (6 semaines, un seul régime TWAP-60) ; les IC bootstrap groupés tiennent "
        "compte de la corrélation entre actifs et durées d'un même créneau, pas d'un éventuel changement de régime.",
        "**Sélection** : 6 modèles, l'empilement et 4 baselines sont comparés. Le choix du « meilleur » modèle et de sa "
        "marge est fait sur la 1re moitié seulement, ce qui limite le biais. En revanche, les métriques de précision sur "
        "la période complète comparent plusieurs modèles sans correction pour tests multiples.",
        "**Modèles 1s** appris sur ≈ 3 mois seulement (bougies 1s téléchargées depuis le "
        f"{ctx['args'].s1_start:%d/%m/%Y}) ; leur calibration isotonique repose sur ≈ 13 jours (≈ 11 700 origines).",
        "**Instant S−30 s** : les 30 dernières secondes de TWAP60(S) et la bougie [S−1 min, S) ne sont pas utilisées. Or "
        "c'est là que se crée l'information « mécanique » (écart spot − TWAP à S : 56 % de justesse dans le diagnostic). "
        "Le marché la paie dès S…S+5 s (0,565 en moyenne pour le côté indiqué).",
        "**Variante maker** : borne haute irréaliste (ni file d'attente, ni sélection adverse) ; aucune remise maker comptée.",
        "**TimesFM** : sous-échantillon BTC seulement, contexte 1m, horizon arrondi à la bougie ; licence non commerciale "
        "(TimesFM 3.0), usage de recherche uniquement.",
    ]


def write_readme(ctx: dict) -> None:
    from tradebot.report import to_markdown

    args, chk, info, M, ev = ctx["args"], ctx["chk"], ctx["info"], ctx["M"], ctx["ev"]
    models, pnl, out = ctx["models"], ctx["pnl"], ctx["out_dir"]
    summ = pnl["summary"]
    best, best_ll = ctx["best_pnl"], ctx["best_ll"]
    pct = lambda x, nd=1: fr_num(x, nd, pct=True)  # noqa: E731
    n = lambda x: fr_num(x, 0)  # noqa: E731

    def E(model, scope="période complète", cell="Tous"):
        r = ev[(ev["modèle"] == model) & (ev["échantillon"] == scope) & (ev["cellule"] == cell)]
        return r.iloc[0] if len(r) else None

    def ci(r, k, f=pct):
        return f"{f(r[k])} (IC {f(r[k + '_ic_bas'])} ; {f(r[k + '_ic_haut'])})"

    def S(model, mode="taker", scope="2e moitié (test final)"):
        r = summ[(summ["modèle"] == model) & (summ["mode"] == mode) & (summ["échantillon"] == scope)]
        return r.iloc[0] if len(r) else None

    mk_ = E("market")
    probs = [m for m in models if E(m) is not None]
    best_brier = min(probs, key=lambda m: E(m)["d_brier_vs_marché"])
    rb = E(best_brier)
    ind_best = max(["logit_ind", "hgb_ind"], key=lambda m: E(m)["auc"])
    ri = E(ind_best)
    gap = E("gap_m30")
    rst = E("stack", "2e moitié (test final)")
    mk2 = E("market", "2e moitié (test final)")
    sb_v, sb_t = S(best, scope="1re moitié (validation)"), S(best)
    mb_t = S(best, "maker")
    base_t = {k: S(k) for k in ("gap_m30", "rev15", "always_up", "mkt_gt_05")}
    rt_df = pd.DataFrame(ctx["rt"].rows, columns=["étape", "secondes"])
    total_s = float(rt_df.loc[rt_df["étape"] == "total", "secondes"].iloc[0]) if (rt_df["étape"] == "total").any() \
        else time.perf_counter() - ctx["t_all"]
    sig = lambda lo, hi: "significatif" if (lo > 0 or hi < 0) else "non significatif (l'IC contient 0)"  # noqa: E731

    L: list[str] = []
    w = L.append
    w("# Nos modèles contre le marché — Polymarket « Up or Down » crypto 5m / 15m")
    w("")
    w(f"*Généré le {pd.Timestamp.now(tz='UTC'):%d/%m/%Y %H:%M} UTC par `scripts/polymarket_models_vs_market.py` "
      f"(temps total : {n(total_s)} s, caches compris — voir § 8). Marchés : BTC, ETH, SOL × 5m, 15m ; fenêtres du "
      f"{args.start:%d/%m/%Y} au {(args.end - pd.Timedelta('1s')):%d/%m/%Y %H:%M} UTC (régime TWAP-60 homogène).*")
    w("")
    w("> Simulation papier sur données publiques : aucune clé, aucun ordre. Depuis la France, ces marchés sont "
      "`restricted` (close-only) : ce test mesure seulement si nos prévisions battent le prix du marché.")
    w("")
    # ------------------------------------------------------------------ résumé
    w("## 0. Résumé")
    w("")
    w(f"* **Données** : {n(chk['évalués'])} marchés résolus évalués sur {n(chk['attendus'])} créneaux attendus "
      f"({n(chk['sans_prix_pré'])} sans prix du jeton Up à S−30 s), issue **officielle** Polymarket. "
      + ("Tous les marchés 5m ont été collectés (pas d'échantillonnage)." if not info.get("sampled") else
         "**Échantillonnage** : seuls les 15m et 1 marché 5m sur 2 ont été collectés (budget de temps dépassé)."))
    w(f"* **Le prix du marché à S−30 s n'a presque aucune information** : justesse {ci(mk_, 'justesse')}, AUC "
      f"{fr_num(mk_['auc'], 3)} (IC {fr_num(mk_['auc_ic_bas'], 3)} ; {fr_num(mk_['auc_ic_haut'], 3)}), Brier "
      f"{fr_num(mk_['brier'], 4)} (pièce : 0,2500).")
    rl2, mk2b = E(best_ll, "2e moitié (test final)"), E("market", "2e moitié (test final)")
    w(f"* **Modèle choisi sans regarder le test : `{best_ll}`** ({label(best_ll)}, meilleure log-loss sur la 1re moitié). "
      f"Sur la 2e moitié : justesse {ci(rl2, 'justesse')} contre {pct(mk2b['justesse'])} pour le marché, AUC {fr_num(rl2['auc'], 3)} "
      f"(IC {fr_num(rl2['auc_ic_bas'], 3)} ; {fr_num(rl2['auc_ic_haut'], 3)}) contre {fr_num(mk2b['auc'], 3)}, écart de Brier "
      f"{fr_num(1e3 * rl2['d_brier_vs_marché'], 2, signed=True)} ×10⁻³ (IC {fr_num(1e3 * rl2['d_brier_ic_bas'], 2, signed=True)} ; "
      f"{fr_num(1e3 * rl2['d_brier_ic_haut'], 2, signed=True)}) : {sig(rl2['d_brier_ic_bas'], rl2['d_brier_ic_haut'])}.")
    w(f"* **Meilleur modèle a posteriori (Brier, période complète) : `{best_brier}`** ({label(best_brier)}) : justesse {ci(rb, 'justesse')}, AUC "
      f"{fr_num(rb['auc'], 3)} (IC {fr_num(rb['auc_ic_bas'], 3)} ; {fr_num(rb['auc_ic_haut'], 3)}), écart de Brier au marché "
      f"{fr_num(1e3 * rb['d_brier_vs_marché'], 2, signed=True)} ×10⁻³ (IC {fr_num(1e3 * rb['d_brier_ic_bas'], 2, signed=True)} ; "
      f"{fr_num(1e3 * rb['d_brier_ic_haut'], 2, signed=True)}) : {sig(rb['d_brier_ic_bas'], rb['d_brier_ic_haut'])}. "
      f"Écart de justesse au marché : {fr_num(100 * rb['d_justesse_vs_marché'], 1, signed=True)} points "
      f"(IC {fr_num(100 * rb['d_justesse_ic_bas'], 1, signed=True)} ; {fr_num(100 * rb['d_justesse_ic_haut'], 1, signed=True)}).")
    w(f"* **Les indicateurs 1m seuls (1 an d'apprentissage) font à peine mieux qu'une pièce** : `{ind_best}` justesse "
      f"{ci(ri, 'justesse')}, AUC {fr_num(ri['auc'], 3)} ; l'information utile vient surtout du **TWAP partiel 1s** "
      f"(écart spot − moyenne des 30 dernières secondes, connu à S−30 s) : la règle seule `gap_m30` fait "
      f"{ci(gap, 'justesse')}, AUC {fr_num(gap['auc'], 3)}.")
    if rst is not None:
        w(f"* **Marché + modèle (empilement appris sur la 1re moitié, testé sur la 2e)** : Brier {fr_num(rst['brier'], 4)} contre "
          f"{fr_num(mk2['brier'], 4)} pour le marché seul (écart {fr_num(1e3 * rst['d_brier_vs_marché'], 2, signed=True)} ×10⁻³, IC "
          f"{fr_num(1e3 * rst['d_brier_ic_bas'], 2, signed=True)} ; {fr_num(1e3 * rst['d_brier_ic_haut'], 2, signed=True)}), "
          f"justesse {ci(rst, 'justesse')}, n = {n(rst['n'])}.")
    if sb_t is not None and np.isfinite(sb_t.get("marge", np.nan)):
        w(f"* **P&L preneur (1 part par marché, frais inclus)** : meilleur modèle sur la 1re moitié = `{best}`, marge retenue "
          f"{fr_num(100 * sb_t['marge'], 2)} c. 1re moitié (en échantillon pour la marge) : {fr_num(sb_v['pnl_total_usd'], 1, signed=True)} $ "
          f"sur {n(sb_v['n_trades'])} positions ; **2e moitié (hors échantillon) : {fr_num(sb_t['pnl_total_usd'], 1, signed=True)} $ sur "
          f"{n(sb_t['n_trades'])} positions, soit {cents(sb_t['pnl_par_part'])} par part (IC {cents(sb_t['pnl_par_part_ic_bas'])} ; "
          f"{cents(sb_t['pnl_par_part_ic_haut'])})**, {sig(sb_t['pnl_par_part_ic_bas'], sb_t['pnl_par_part_ic_haut'])}. "
          f"Avec le surcoût d'exécution mesuré dans le diagnostic (+1,7 c par part) : {cents(sb_t['pnl_par_part_surcoût_1_7c'])} par part.")
    btxt = "; ".join(f"`{k}` {cents(r['pnl_par_part'])} (IC {cents(r['pnl_par_part_ic_bas'])} ; {cents(r['pnl_par_part_ic_haut'])})"
                     for k, r in base_t.items() if r is not None)
    w(f"* **Baselines toujours en position (2e moitié, par part)** : {btxt}. Aucune n'est rentable après frais.")
    if mb_t is not None and np.isfinite(mb_t.get("marge", np.nan)):
        w(f"* **Borne haute maker (achat au bid, sans frais, exécution SUPPOSÉE certaine)** : `{best}` "
          f"{fr_num(mb_t['pnl_total_usd'], 1, signed=True)} $ sur {n(mb_t['n_trades'])} positions, {cents(mb_t['pnl_par_part'])} par part "
          f"(IC {cents(mb_t['pnl_par_part_ic_bas'])} ; {cents(mb_t['pnl_par_part_ic_haut'])}). Ce n'est pas un résultat atteignable "
          "tel quel : un ordre au repos n'est exécuté que si un preneur vient le chercher, c'est-à-dire surtout quand il a tort (sélection adverse).")
    ti = ctx["tfm_info"]
    if ti.get("status") in ("ok", "cache") and len(ctx["ev_tfm"]):
        et = ctx["ev_tfm"]
        g = lambda m: et[(et["modèle"] == m) & (et["échantillon"] == "période complète") & (et["cellule"] == "Tous")].iloc[0]  # noqa: E731
        t1, t0 = g("timesfm_cal"), g("market")
        w(f"* **TimesFM** (sous-échantillon régulier de {n(t1['n'])} marchés BTC) : justesse {ci(t1, 'justesse')}, AUC "
          f"{fr_num(t1['auc'], 3)}, Brier {fr_num(t1['brier'], 4)} contre {fr_num(t0['brier'], 4)} pour le marché sur les mêmes marchés "
          f"(écart {fr_num(1e3 * t1['d_brier_vs_marché'], 2, signed=True)} ×10⁻³, IC {fr_num(1e3 * t1['d_brier_ic_bas'], 2, signed=True)} ; "
          f"{fr_num(1e3 * t1['d_brier_ic_haut'], 2, signed=True)}).")
    else:
        w(f"* **TimesFM** : {ti.get('status', 'sauté')}.")
    w(f"* **Verdict** : {ctx.get('verdict', '')}")
    w("")
    # ------------------------------------------------------------------ données
    w("## 1. Données")
    w("")
    cnt = M.groupby(["asset", "duration"]).agg(marchés=("slug", "size"), taux_up=("y", "mean"),
                                               p_pre_moyen=("p_pre", "mean"), p_pre_sd=("p_pre", "std"),
                                               âge_médian_s=("p_pre_age_s", "median")).reset_index()
    cnt = cnt.merge(chk["couverture"], on=["asset", "duration"], how="left")
    cnt["couverture_prix"] = cnt["avec_prix"] / cnt["résolus"]
    cnt["cellule"] = cnt["asset"].str.upper() + " " + cnt["duration"]
    cnt = cnt.sort_values(["duration", "asset"], key=lambda x: x.map(pm.DURATION_SECONDS) if x.name == "duration" else x)
    w(f"* **Marchés** : `PolymarketClient.list_updown_markets` (slugs déterministes, cache disque), issue officielle "
      f"`outcomePrices`. Créneaux attendus {n(chk['attendus'])}, trouvés {n(chk['trouvés'])}, résolus {n(chk['résolus'])}, "
      f"en TWAP-60 {n(chk['twap60'])} ; frais : " + ", ".join(f"{k[0]} (taux {fr_num(k[1], 2)}, exposant {fr_num(k[2], 0)}) : {n(v)}"
                                                           for k, v in chk["frais"].items()) + ".")
    w(f"* **Prix d'exécution de référence** : dernier point `prices_history` (fidelity = 1, milieu de fourchette) <= S−30 s, "
      f"ancienneté <= 5 min ; ancienneté médiane {fr_num(chk['p_pre_age_médian_s'], 0)} s ; {pct(chk['p_pre_part_0.45_0.55'])} des prix "
      "dans [0,45 ; 0,55]. Hypothèse (documentée dans le diagnostic, carnet ≈ 0,50 / 0,51 avant l'ouverture) : "
      "**ask ≈ milieu + 0,005**, **bid ≈ milieu − 0,005**. Le diagnostic a mesuré que les preneurs ont en réalité payé "
      "≈ 1,7 c de plus dans [S−30 s, S) : l'hypothèse est optimiste (voir la colonne « surcoût 1,7 c »).")
    w(f"* **Collecte** : {n(info.get('found', 0))} marchés ; historiques de prix à {fr_num(args.max_rps, 0)} requêtes/s max, "
      f"{args.workers} fils ; codes HTTP de cette exécution : {info.get('http_status', {})} ; durée de la collecte des prix : "
      f"{n(info.get('fetch_seconds', 0))} s dans cette exécution (≈ 0 quand tout est en cache) ; 1re collecte, à froid : "
      f"{ctx.get('cold_fetch', '—')}.")
    w(f"* **Binance** : bougies 1m (1 an, `tradebot.data.load_universe`, dérivés alignés) et bougies 1s du "
      f"{args.s1_start:%d/%m/%Y} au {(args.end - pd.Timedelta('1s')):%d/%m/%Y} (zips journaliers, agrégées par demi-minute). "
      f"Contrôle de l'étiquette proxy sur les marchés testés : `y_proxy` (VWAP 1m) = issue officielle dans "
      f"{pct(chk['accord_y_proxy'])} des cas (n = {n(chk['n_y_proxy'])}) ; TWAP60 1s exact : {pct(chk['accord_y1s'])}.")
    w("")
    w(to_markdown(cnt[["cellule", "résolus", "avec_prix", "couverture_prix", "marchés", "taux_up", "p_pre_moyen", "p_pre_sd",
                       "âge_médian_s"]].rename(columns={"marchés": "évalués", "avec_prix": "avec prix S−30 s",
                                                        "couverture_prix": "couverture", "taux_up": "taux de Up",
                                                        "p_pre_moyen": "prix Up moyen", "p_pre_sd": "écart-type prix",
                                                        "âge_médian_s": "ancienneté médiane (s)"}),
                  {"couverture": "1%", "taux de Up": "1%", "prix Up moyen": 3, "écart-type prix": 3,
                   "ancienneté médiane (s)": 0}))
    w("")
    # ------------------------------------------------------------------ méthode
    w("## 2. Méthode")
    w("")
    w("**Instant d'information.** Tout est mesuré à **S−30 s** : le prix du marché est le dernier point <= S−30 s, et les "
      "variables n'utilisent que ce qui est connu à S−30 s. Pour les bougies 1m, c'est la dernière bougie close, ouverte à "
      "S−2 min et close à S−1 min (la bougie [S−1 min, S) n'est close qu'à S). Pour les bougies 1s, ce sont les bougies closes "
      "au plus tard à S−30 s. Un test unitaire modifie toutes les données postérieures et vérifie que les variables ne "
      "changent pas (`tests/test_polymarket_backtest.py`).")
    w("")
    w("**Variables.**")
    w("")
    w("* `ind` : les indicateurs causaux de `tradebot.indicators` (158 pour BTC, 167 pour ETH et SOL avec le benchmark BTC), "
      "lus à la bougie ouverte à S−2 min, plus `mom15_m60`, `mom5_m60` et une indicatrice d'actif.")
    w("* `tw` (« TWAP partiel », bougies 1s) : `tw_gap30` = log(spot(S−30 s) / moyenne des closes 1s sur (S−60, S−30]), soit "
      "l'écart spot − TWAP partiel. La référence TWAP60(S) est à moitié connue à S−30 s. S'y ajoutent sa version réduite par σ, "
      "les rendements sur 30 s, 90 s et 330 s, la pente sur 30 s, l'écart au VWAP, l'étendue, la part acheteuse des preneurs, "
      "le volume relatif, l'écart TWAP de la minute précédente, σ 1m, l'heure et le jour (sin/cos) et l'actif.")
    w("* `ind1s` = `ind` + `tw`.")
    w("")
    w("**Étiquette d'entraînement (Binance)** : y = 1 si VWAP(bougie [S+D−1m]) >= VWAP(bougie [S−1m]), avec VWAP = "
      "quote_volume / volume (proxy à 96,5 % du diagnostic). Les origines S suivent une grille de "
      f"{args.step_min} min, pour les 3 actifs. On entraîne **un modèle par durée** (5m, 15m), commun aux trois actifs.")
    w("")
    w(f"**Apprentissage, purge, calibration.** Seules les origines antérieures au {args.start:%d/%m/%Y %H:%M} moins D moins 1 h sont "
      "utilisées. Cette période est découpée dans le temps en trois segments : apprentissage (75 %), arrêt précoce (10 %) et "
      "calibration isotonique (15 %, `forecaster.calibrate_isotonic`, scores bruts winsorisés à 1 % / 99 % pour que les "
      "blocs extrêmes ne reposent pas sur quelques points). Entre deux segments, on retire D + 1 h d'origines "
      "(purge). Les modèles `*_ind` apprennent sur ~10,5 mois de 1m ; les modèles `*_ind1s` et `*_tw` sur la période couverte "
      f"par le 1s (depuis le {args.s1_start:%d/%m/%Y}, ~3 mois). Algorithmes : `logit` (régression logistique L2, C = 0,1, "
      "variables standardisées) et `hgb` (HistGradientBoosting, profondeur 3, taux 0,05, 200 observations minimum par feuille, "
      "arrêt précoce sur le segment chronologique). Les valeurs manquantes sont remplacées par la médiane de l'apprentissage. "
      "Détail : `apprentissage.csv`.")
    w("")
    w("**Baselines** (diagnostic) : `gap_m30` (momentum de l'écart spot − TWAP partiel), `rev15` (retournement des 15 dernières "
      "minutes, bougies 1m closes à S−1 min), « toujours Up », « marché > 0,5 ». Le prix du marché lui-même sert de "
      "probabilité de référence.")
    w("")
    w(f"**Évaluation hors échantillon** sur l'issue officielle, {PERIOD['all']}. Mesures : justesse (Up si p >= 0,5), AUC, "
      "Brier et log-loss, écarts appariés au marché, calibration par déciles. Les IC à 95 % viennent d'un **bootstrap groupé par "
      f"créneau de 15 min** ({args.bootstrap} tirages ; 200 pour l'AUC). Les 3 actifs et les 2 durées d'un même créneau sont "
      f"corrélés et sont donc tirés ensemble. La période est coupée en deux moitiés : la 1re ({PERIOD['h1']}) sert de "
      f"**validation** (choix de la marge, apprentissage de l'empilement), la 2e ({PERIOD['h2']}) de **test final**.")
    w("")
    w("**P&L (1 part par marché).** Coût d'achat de Up : ask_Up = p_Up(S−30 s) + 0,005, plus les frais `taker_fee` du "
      "`feeSchedule` du marché (0,07·p·(1−p)). Pour Down : ask_Down = 1 − p_Up(S−30 s) + 0,005. On achète Up si "
      "p_modèle − (ask_Up + frais) > marge, et Down si (1 − p_modèle) − (ask_Down + frais) > marge. La position est gardée "
      "jusqu'à la résolution. La marge est choisie sur la 1re moitié : c'est celle qui maximise le P&L total avec au moins "
      f"{args.min_trades} positions, sur une grille de 0 à 10 c par pas de 0,25 c. Elle est ensuite appliquée telle quelle "
      "à la 2e moitié. La variante **maker optimiste** achète au bid (milieu − 0,005), sans frais, et suppose que l'ordre "
      "est toujours exécuté. C'est une **borne haute**, pas un résultat atteignable.")
    w("")
    # ------------------------------------------------------------------ précision
    w("## 3. Précision : nos modèles contre le prix du marché au même instant")
    w("")
    w(f"![{ctx['titles']['acc']}](justesse_brier_vs_marche.png)")
    w("")
    rows = []
    for m in ["market"] + probs + ["gap_m30", "rev15", "always_up", "mkt_gt_05"]:
        r = E(m)
        if r is None:
            continue
        rows.append({"modèle": m, "description": label(m), "n": r["n"],
                     "justesse": f"{pct(r['justesse'])} [{pct(r['justesse_ic_bas'])} ; {pct(r['justesse_ic_haut'])}]",
                     "AUC": f"{fr_num(r['auc'], 3)} [{fr_num(r['auc_ic_bas'], 3)} ; {fr_num(r['auc_ic_haut'], 3)}]" if np.isfinite(r["auc"]) else "—",
                     "Brier": fr_num(r.get("brier", np.nan), 4), "log-loss": fr_num(r.get("logloss", np.nan), 4),
                     "ΔBrier vs marché (×10⁻³)": (f"{fr_num(1e3 * r['d_brier_vs_marché'], 2, signed=True)} "
                                                 f"[{fr_num(1e3 * r['d_brier_ic_bas'], 2, signed=True)} ; {fr_num(1e3 * r['d_brier_ic_haut'], 2, signed=True)}]")
                     if m != "market" and np.isfinite(r.get("d_brier_vs_marché", np.nan)) else "—",
                     "Δjustesse vs marché (pts)": (f"{fr_num(100 * r['d_justesse_vs_marché'], 1, signed=True)} "
                                                   f"[{fr_num(100 * r['d_justesse_ic_bas'], 1, signed=True)} ; {fr_num(100 * r['d_justesse_ic_haut'], 1, signed=True)}]")
                     if m != "market" else "—"})
    w(f"Période complète ({PERIOD['all']}), tous marchés ; [IC 95 %] :")
    w("")
    w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    w("")
    if rst is not None:
        w(f"Empilement marché + `{ctx['stack_info']['base']}` (régression logistique sur logit(p_modèle), logit(p_marché) et "
          f"l'indicatrice 15m, apprise sur la 1re moitié ; coefficients {', '.join(fr_num(c, 3) for c in ctx['stack_info']['coef'])}) — "
          f"2e moitié seulement : justesse {ci(rst, 'justesse')}, AUC {fr_num(rst['auc'], 3)}, Brier {fr_num(rst['brier'], 4)} "
          f"(marché : {fr_num(mk2['brier'], 4)}).")
        w("")
    # par cellule
    w("Par actif et durée (période complète) : justesse et écart de Brier au marché (×10⁻³) du meilleur modèle en probabilité, "
      f"`{best_brier}`, et de la baseline `gap_m30` :")
    w("")
    rows = []
    for cell in [c for c in ev["cellule"].unique() if c != "Tous"]:
        rm, rbm, rg = E("market", cell=cell), E(best_brier, cell=cell), E("gap_m30", cell=cell)
        if rm is None or rbm is None:
            continue
        rows.append({"cellule": cell, "n": rm["n"], "marché : justesse": pct(rm["justesse"]), "marché : AUC": fr_num(rm["auc"], 3),
                     f"{best_brier} : justesse": f"{pct(rbm['justesse'])} [{pct(rbm['justesse_ic_bas'])} ; {pct(rbm['justesse_ic_haut'])}]",
                     f"{best_brier} : AUC": fr_num(rbm["auc"], 3),
                     f"{best_brier} : ΔBrier": f"{fr_num(1e3 * rbm['d_brier_vs_marché'], 2, signed=True)} [{fr_num(1e3 * rbm['d_brier_ic_bas'], 2, signed=True)} ; {fr_num(1e3 * rbm['d_brier_ic_haut'], 2, signed=True)}]",
                     "gap_m30 : justesse": pct(rg["justesse"]) if rg is not None else "—"})
    w(to_markdown(pd.DataFrame(rows)))
    w("")
    w("Stabilité dans le temps (tous marchés) : 1re moitié contre 2e moitié.")
    w("")
    rows = []
    for m in ["market"] + probs + ["gap_m30"]:
        r1, r2 = E(m, "1re moitié (validation)"), E(m, "2e moitié (test final)")
        rows.append({"modèle": m, "justesse 1re moitié": pct(r1["justesse"]), "justesse 2e moitié": pct(r2["justesse"]),
                     "AUC 1re": fr_num(r1["auc"], 3), "AUC 2e": fr_num(r2["auc"], 3),
                     "ΔBrier 1re (×10⁻³)": fr_num(1e3 * r1.get("d_brier_vs_marché", np.nan), 2, signed=True) if m not in ("market", "gap_m30") else "—",
                     "ΔBrier 2e (×10⁻³)": fr_num(1e3 * r2.get("d_brier_vs_marché", np.nan), 2, signed=True) if m not in ("market", "gap_m30") else "—"})
    w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    w("")
    # ------------------------------------------------------------------ calibration
    w("## 4. Calibration")
    w("")
    w(f"![{ctx['titles']['calib']}](calibration.png)")
    w("")
    cb = ctx["calib"]
    tb = cb[cb["modèle"].isin(["market", best_ll])].copy()
    tb["décile"] = tb["bin"] + 1
    piv = tb.pivot_table(index="décile", columns="modèle", values=["p_mean", "up_rate", "n"], aggfunc="first")
    rows = []
    for dcl in piv.index:
        rows.append({"décile": int(dcl),
                     "marché : p moyen": fr_num(piv.loc[dcl, ("p_mean", "market")], 3),
                     "marché : fréquence Up": pct(piv.loc[dcl, ("up_rate", "market")]),
                     f"{best_ll} : p moyen": fr_num(piv.loc[dcl, ("p_mean", best_ll)], 3),
                     f"{best_ll} : fréquence Up": pct(piv.loc[dcl, ("up_rate", best_ll)]),
                     "n": n(piv.loc[dcl, ("n", best_ll)])})
    w(f"Déciles (quantiles) de probabilité, période complète — marché et `{best_ll}` (meilleure log-loss sur la 1re moitié) :")
    w("")
    w(to_markdown(pd.DataFrame(rows)))
    w("")
    rows = []
    yv = M["y"].to_numpy(dtype="float64")
    bm = float(np.mean((M["p_pre"].to_numpy() - yv) ** 2))
    for m in probs:
        if f"praw_{m}" not in M.columns:
            continue
        pr, pc = M[f"praw_{m}"].to_numpy(dtype="float64"), M[f"p_{m}"].to_numpy(dtype="float64")
        rows.append({"modèle": m, "Brier brut": fr_num(np.mean((pr - yv) ** 2), 4), "Brier isotonique": fr_num(np.mean((pc - yv) ** 2), 4),
                     "Brier marché": fr_num(bm, 4), "écart-type p brut": fr_num(np.std(pr), 3),
                     "écart-type p isotonique": fr_num(np.std(pc), 3),
                     "p isotonique min – max": f"{fr_num(np.min(pc), 3)} – {fr_num(np.max(pc), 3)}"})
    w("Effet de la calibration isotonique (période complète). Les probabilités « brutes » sont la sortie directe du "
      "classifieur ; toutes les métriques et le P&L ci-dessus utilisent la version isotonique :")
    w("")
    w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    w("")
    # ------------------------------------------------------------------ P&L
    w("## 5. P&L simulé")
    w("")
    w(f"![{ctx['titles']['margin']}](pnl_vs_marge.png)")
    w("")
    w(f"![{ctx['titles']['cum']}](pnl_cumule_test.png)")
    w("")
    w("Tous les modèles et toutes les baselines. La marge est choisie sur la 1re moitié ; le P&L est donné sur les deux moitiés "
      "(la 1re est **en échantillon** pour la marge). Preneur, 1 part par marché, frais inclus :")
    w("")
    rows = []
    for m in probs + ["stack", "gap_m30", "rev15", "always_up", "mkt_gt_05"]:
        r1, r2 = S(m, scope="1re moitié (validation)"), S(m)
        if r2 is None or not np.isfinite(r2.get("n_marchés", np.nan)):
            rows.append({"modèle": m, "marge": "aucune (< min. de positions)", "1re : positions": "—", "1re : P&L $": "—",
                         "2e : positions": "—", "2e : taux de gain": "—", "2e : coût moyen": "—", "2e : P&L $": "—",
                         "2e : P&L par part [IC 95 %]": "—", "2e : P&L/part si +1,7 c": "—"})
            continue
        rows.append({"modèle": m, "marge": cents(r2["marge"], 2, signed=False) if np.isfinite(r2["marge"]) else "toujours",
                     "1re : positions": n(r1["n_trades"]), "1re : P&L $": fr_num(r1["pnl_total_usd"], 1, signed=True),
                     "2e : positions": n(r2["n_trades"]), "2e : taux de gain": pct(r2["taux_gain"]),
                     "2e : coût moyen": fr_num(r2["coût_moyen"], 3), "2e : P&L $": fr_num(r2["pnl_total_usd"], 1, signed=True),
                     "2e : P&L par part [IC 95 %]": f"{cents(r2['pnl_par_part'])} [{cents(r2['pnl_par_part_ic_bas'])} ; {cents(r2['pnl_par_part_ic_haut'])}]",
                     "2e : P&L/part si +1,7 c": cents(r2["pnl_par_part_surcoût_1_7c"])})
    w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    w("")
    w("*`stack` : sur la 1re moitié, ses probabilités sont en échantillon (l'empilement y est appris), donc la marge est "
      "choisie en échantillon. Le coût moyen comprend le prix et les frais. Le taux de gain est la part des positions "
      "gagnantes.*")
    w("")
    w(f"Par actif et durée, 2e moitié, preneur : `{best}` à sa marge et les baselines.")
    w("")
    cp = ctx["cellp"].copy()
    rows = []
    for _, r in cp.iterrows():
        rows.append({"modèle": r["modèle"], "cellule": r["cellule"], "marchés": n(r["n_marchés"]), "positions": n(r["n_trades"]),
                     "taux de gain": pct(r["taux_gain"]), "P&L $": fr_num(r["pnl_total_usd"], 1, signed=True),
                     "P&L par part [IC 95 %]": f"{cents(r['pnl_par_part'])} [{cents(r['ic_bas'])} ; {cents(r['ic_haut'])}]" if r["n_trades"] else "—"})
    cpt = pd.DataFrame(rows)
    w(to_markdown(cpt[cpt["modèle"].isin([best, "stack", "gap_m30"])], code_columns=["modèle"]))
    w("")
    w("Variante **maker optimiste** (borne haute : achat au bid, sans frais, exécution supposée certaine), marge choisie de la "
      "même façon :")
    w("")
    rows = []
    for m in probs + ["gap_m30", "always_up"]:
        r1, r2 = S(m, "maker", "1re moitié (validation)"), S(m, "maker")
        if r2 is None or not np.isfinite(r2.get("n_marchés", np.nan)):
            continue
        rows.append({"modèle": m, "marge": cents(r2["marge"], 2, signed=False) if np.isfinite(r2["marge"]) else "toujours",
                     "1re : P&L $": fr_num(r1["pnl_total_usd"], 1, signed=True), "2e : positions": n(r2["n_trades"]),
                     "2e : taux de gain": pct(r2["taux_gain"]), "2e : P&L $": fr_num(r2["pnl_total_usd"], 1, signed=True),
                     "2e : P&L par part [IC 95 %]": f"{cents(r2['pnl_par_part'])} [{cents(r2['pnl_par_part_ic_bas'])} ; {cents(r2['pnl_par_part_ic_haut'])}]"})
    w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    w("")
    # ------------------------------------------------------------------ TimesFM
    w("## 6. TimesFM")
    w("")
    if ti.get("status") in ("ok", "cache") and len(ctx["ev_tfm"]):
        et = ctx["ev_tfm"]
        w(f"On estime P(close à E >= close à S−1 min) avec `TimesFMForecaster` (backend `{ti.get('backend', 'timesfm3')}`, "
          "poids non commerciaux, usage de recherche). Le contexte est formé des 512 closes 1m qui finissent à la bougie close à "
          "S−1 min. On applique `prob_up` avec comme seuil la dernière valeur, à l'horizon D/1 min + 1 bougie. Le résultat est "
          f"recalibré par régression isotonique sur des origines BTC des 4 semaines précédant le {args.start:%d/%m} (étiquette proxy VWAP). "
          f"Le sous-échantillon est un tirage régulier de marchés BTC 5m et 15m ; temps de calcul : {n(ti.get('seconds', 0))} s.")
        w("")
        rows = []
        for m in ["market", "timesfm", "timesfm_cal", best_brier]:
            for cell in ("Tous", "BTC 5m", "BTC 15m"):
                r = et[(et["modèle"] == m) & (et["échantillon"] == "période complète") & (et["cellule"] == cell)]
                if not len(r):
                    continue
                r = r.iloc[0]
                rows.append({"modèle": m, "cellule": cell, "n": n(r["n"]),
                             "justesse [IC]": f"{pct(r['justesse'])} [{pct(r['justesse_ic_bas'])} ; {pct(r['justesse_ic_haut'])}]",
                             "AUC": fr_num(r["auc"], 3), "Brier": fr_num(r["brier"], 4),
                             "ΔBrier vs marché (×10⁻³) [IC]": (f"{fr_num(1e3 * r['d_brier_vs_marché'], 2, signed=True)} "
                                                              f"[{fr_num(1e3 * r['d_brier_ic_bas'], 2, signed=True)} ; {fr_num(1e3 * r['d_brier_ic_haut'], 2, signed=True)}]")
                             if m != "market" else "—"})
        w(to_markdown(pd.DataFrame(rows), code_columns=["modèle"]))
    else:
        w(f"Non exécuté : {ti.get('status')}.")
    w("")
    # ------------------------------------------------------------------ limites
    w("## 7. Limites")
    w("")
    for t in ctx.get("limits", []):
        w(f"* {t}")
    w("")
    # ------------------------------------------------------------------ temps
    w("## 8. Temps d'exécution")
    w("")
    w(to_markdown(rt_df, {"secondes": 1}))
    w("")
    w("*Les étapes réseau et d'apprentissage utilisent des caches disque (`data/cache/polymarket`, `data/cache/pm_backtest`). "
      "Mesures de la 1re exécution, à froid : " + ctx.get("cold_times", "voir le journal") + ".*")
    w("")
    # ------------------------------------------------------------------ fichiers
    w("## 9. Fichiers")
    w("")
    for f, d in [
        ("marches_predictions.csv", "une ligne par marché évalué : issue officielle, étiquettes proxy, prix S−30 s, frais, variables clés, prévisions de chaque modèle"),
        ("metriques_hors_echantillon.csv", "justesse / AUC / Brier / log-loss et écarts au marché avec IC, par modèle × échantillon (période, moitiés) × cellule"),
        ("metriques_timesfm.csv", "idem sur le sous-échantillon TimesFM"),
        ("calibration.csv", "calibration par déciles (marché et modèles)"),
        ("pnl_vs_marge.csv", "courbes P&L contre marge (preneur et maker), par modèle et moitié"),
        ("pnl_resume.csv", "P&L à la marge choisie (modèles) et des baselines, par moitié, avec IC"),
        ("pnl_par_cellule.csv", "P&L de la 2e moitié par actif × durée"),
        ("pnl_cumule_journalier.csv", "P&L cumulé jour par jour (2e moitié)"),
        ("apprentissage.csv", "périodes, tailles, itérations et AUC de validation de chaque modèle"),
        ("runtime.csv", "temps d'exécution par étape"),
        ("justesse_brier_vs_marche.png, calibration.png, pnl_vs_marge.png, pnl_cumule_test.png", "graphiques"),
    ]:
        w(f"* `{f}` : {d}")
    w("")
    w("Reproduire : `. .venv/bin/activate && python scripts/polymarket_models_vs_market.py --end "
      f"{args.end:%Y-%m-%d}` ; tests : `python -m pytest tests/test_polymarket_backtest.py`.")
    (out / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest nos modèles contre le marché Polymarket (papier)")
    p.add_argument("--start", default=str(pb.TWAP60_START.date()), help="début (UTC) des fenêtres testées")
    p.add_argument("--end", default=None, help="fin EXCLUSIVE (UTC, défaut : aujourd'hui 00:00 UTC)")
    p.add_argument("--assets", default="btc,eth,sol")
    p.add_argument("--durations", default="5m,15m")
    p.add_argument("--max-rps", type=float, default=20.0, help="requêtes/s max vers Polymarket")
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--budget-min", type=float, default=60.0,
                   help="budget (min) de collecte des prix : au-delà, 1 marché 5m sur 2 seulement")
    p.add_argument("--s1-start", default="2026-05-15", help="début du téléchargement des bougies 1s")
    p.add_argument("--step-min", type=int, default=5, help="pas (min) de la grille d'entraînement")
    p.add_argument("--threads", type=int, default=2, help="fils de calcul (sklearn, torch)")
    p.add_argument("--timesfm", type=int, default=1500, help="marchés BTC du sous-échantillon TimesFM (0 = sauté)")
    p.add_argument("--timesfm-budget-min", type=float, default=30.0, help="au-delà, TimesFM est abandonné")
    p.add_argument("--min-trades", type=int, default=100, help="positions minimales pour retenir une marge")
    p.add_argument("--bootstrap", type=int, default=2000, help="tirages du bootstrap groupé")
    p.add_argument("--stage", default="all", choices=["all", "data", "fit"])
    p.add_argument("--refit", action="store_true", help="ignore les caches d'indicateurs / prévisions / TimesFM")
    p.add_argument("--no-fetch", action="store_true", help="n'utilise que les prix Polymarket déjà en cache")
    p.add_argument("--split", default=None, help="coupure validation / test final (UTC ; défaut : milieu de la période)")
    p.add_argument("--out", default=str(OUT_DIR))
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    a.start = pd.Timestamp(a.start, tz="UTC")
    a.end = pd.Timestamp(a.end, tz="UTC") if a.end else pd.Timestamp.now(tz="UTC").floor("D")
    a.assets = [x.strip().lower() for x in a.assets.split(",") if x.strip()]
    a.durations = [x.strip() for x in a.durations.split(",") if x.strip()]
    a.s1_start = pd.Timestamp(a.s1_start, tz="UTC")
    a.split = pd.Timestamp(a.split, tz="UTC") if a.split else None
    return a


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype="float64"), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    rt = Runtime()
    t_all = time.perf_counter()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.stage != "fit":
        mk, info = collect_polymarket(args, rt)
        log.info("marchés : %s", json.dumps({k: v for k, v in info.items() if k not in ("sampled_out", "regimes")},
                                            default=str))
        if args.stage == "data":
            log.info("collecte terminée en %.0f s", time.perf_counter() - t_all)
            return 0

    bars, agg = load_binance(args, rt)
    ind_tabs = indicator_tables(args, bars, rt)
    preds, infos = [], []
    with rt(f"4. apprentissage ({len(MODEL_SPECS)} modèles × {len(args.durations)} durées, calibration isotonique)"):
        for d in args.durations:
            D = pm.DURATION_SECONDS[d]
            tag = f"{d}_{args.start:%Y%m%d}_{args.end:%Y%m%d}_{args.s1_start:%Y%m%d}_{args.step_min}_v2"
            p_path, i_path = PB_CACHE / f"preds_{tag}.parquet", PB_CACHE / f"train_{tag}.json"
            if p_path.exists() and i_path.exists() and not args.refit:
                preds.append(pd.read_parquet(p_path))
                infos += json.loads(i_path.read_text())
                continue
            t_fit = time.perf_counter()
            df, ind_cols = assemble(D, args, bars, agg, ind_tabs)
            out, inf = fit_all(D, df, ind_cols, args, rt)
            record_cold(f"apprentissage {d}", time.perf_counter() - t_fit, f"{len(MODEL_SPECS)} modèles, {args.threads} fils")
            del df
            pm._atomic_parquet(out, p_path)
            pm._atomic_write_text(i_path, json.dumps(inf, default=str))
            preds.append(out)
            infos += json.loads(json.dumps(inf, default=str))
    train_info = pd.DataFrame(infos)
    P = pd.concat(preds, ignore_index=True)
    if args.stage == "fit":
        log.info("apprentissage terminé en %.0f s", time.perf_counter() - t_all)
        return 0
    models = list(MODEL_SPECS)

    # --- table des marchés évalués ------------------------------------------------
    with rt("6a. jointure marchés / prévisions, contrôles"):
        chk: dict = {"attendus": info["expected"], "trouvés": info["found"], "résolus": info["resolved"]}
        base = mk[mk["closed"] & mk["y"].notna()].copy()
        chk["twap60"] = int((base["twap_lookback_s"] == 60).sum())
        base = base[base["twap_lookback_s"] == 60]
        chk["sans_prix_pré"] = int((~np.isfinite(base["p_pre"])).sum())
        chk["couverture"] = base.assign(avec=np.isfinite(base["p_pre"])).groupby(["asset", "duration"]).agg(
            résolus=("slug", "size"), avec_prix=("avec", "sum")).reset_index()
        M = base.merge(P, left_on=["asset", "duration", "start_ts"], right_on=["asset", "duration", "S"],
                       how="left", suffixes=("", "_bn"))
        M = M.rename(columns={"y_bn": "y_proxy"})
        chk["sans_prévision"] = int((~np.isfinite(M[f"p_{models[0]}"])).sum())
        M = M[np.isfinite(M["p_pre"]) & np.isfinite(M[f"p_{models[0]}"])].reset_index(drop=True)
        chk["évalués"] = len(M)
        yo = M["y"].to_numpy()
        for c in ("y_proxy", "y1s"):
            ok = np.isfinite(M[c].to_numpy())
            chk[f"accord_{c}"] = float(np.mean(M[c].to_numpy()[ok] == yo[ok])) if ok.any() else math.nan
            chk[f"n_{c}"] = int(ok.sum())
        chk["p_pre_age_médian_s"] = float(M["p_pre_age_s"].median())
        chk["p_pre_part_0.45_0.55"] = float(M["p_pre"].between(0.45, 0.55).mean())
        chk["frais"] = M.groupby(["fee_type", "fee_rate", "fee_exponent"], dropna=False).size().to_dict()
        mid = args.split if args.split is not None else args.start + (args.end - args.start) / 2
        mid_ts = int(mid.timestamp())
        chk["milieu"] = mid
        last = args.end - pd.Timedelta("1s")
        PERIOD.update(all=f"{args.start:%d/%m}–{last:%d/%m/%Y}", h1=f"{args.start:%d/%m}–{mid - pd.Timedelta('1s'):%d/%m}",
                      h2=f"{mid:%d/%m}–{last:%d/%m}")

    # --- TimesFM ------------------------------------------------------------------
    with rt(f"5. TimesFM (sous-échantillon {args.timesfm} marchés BTC)"):
        tfm, tfm_info = run_timesfm(args, bars, base, rt)
    if len(tfm):
        M = M.merge(tfm, on="slug", how="left")

    # --- empilement marché + meilleur modèle (appris sur la 1re moitié) ----------------
    y = M["y"].to_numpy(dtype="float64")
    val = (M["start_ts"] < mid_ts).to_numpy()
    ll_val = {}
    for m in models:
        p = np.clip(M[f"p_{m}"].to_numpy(), 1e-6, 1 - 1e-6)
        ll_val[m] = float(-np.mean(y[val] * np.log(p[val]) + (1 - y[val]) * np.log(1 - p[val])))
    best_ll = min(ll_val, key=ll_val.get)
    from sklearn.linear_model import LogisticRegression

    Xs = np.column_stack([logit(M[f"p_{best_ll}"]), logit(M["p_pre"]), (M["duration"] == "15m").astype(float)])
    stk = LogisticRegression(C=1.0).fit(Xs[val], y[val])
    p_stack = stk.predict_proba(Xs)[:, 1]
    M["p_stack"] = np.where(val, np.nan, p_stack)          # hors échantillon : 2e moitié seulement
    M["p_stack_pnl"] = p_stack                              # 1re moitié en échantillon (choix de la marge)
    stack_info = {"base": best_ll, "coef": stk.coef_.ravel().tolist(), "intercept": float(stk.intercept_[0]),
                  "ll_val": ll_val}

    signals = {
        "gap_m30": M["tw_gap30"].to_numpy(dtype="float64"),
        "rev15": -M["mom15_m60"].to_numpy(dtype="float64"),
        "always_up": np.ones(len(M)),
        "mkt_gt_05": np.where(M["p_pre"] > 0.5, 1.0, -1.0) * np.maximum(np.abs(M["p_pre"] - 0.5), 1e-9),
    }
    boot = pb.SlotBootstrap(M["slot"].to_numpy(), B=args.bootstrap, seed=args.seed)

    with rt("6b. métriques hors échantillon (bootstrap groupé par créneau de 15 min)"):
        ev = eval_table(M, models, signals, boot, mid_ts)
        ev_stack = eval_table(M, ["stack"], {}, boot, mid_ts,
                              cell_filter=lambda sc, ce: sc.startswith("2e"))
        ev_stack = ev_stack[ev_stack["modèle"] == "stack"] if len(ev_stack) else ev_stack
        ev = pd.concat([ev, ev_stack], ignore_index=True)
        ev_tfm = pd.DataFrame()
        if len(tfm):
            sub = M[np.isfinite(M["p_timesfm"])].reset_index(drop=True)
            boot_s = pb.SlotBootstrap(sub["slot"].to_numpy(), B=args.bootstrap, seed=args.seed)
            ev_tfm = eval_table(sub, ["timesfm", "timesfm_cal"] + models, {}, boot_s, mid_ts,
                                cell_filter=lambda sc, ce: ce.startswith("Tous") or ce.startswith("BTC"))
        calib = calib_tables(M, models, np.ones(len(M), bool))

    with rt("7. P&L (marge choisie sur la 1re moitié, évaluée sur la 2e)"):
        pnl = pnl_stage(M.assign(p_stack=M["p_stack_pnl"]), models + ["stack"], signals, boot, mid_ts,
                        args.min_trades)
        summ = pnl["summary"]
        tv = summ[(summ["mode"] == "taker") & summ["modèle"].isin(models) &
                  (summ["échantillon"] == "1re moitié (validation)")]
        best_pnl = tv.set_index("modèle")["pnl_total_usd"].idxmax() if len(tv) else models[0]
        cellp = pnl_cells(M, pnl["trades"], [best_pnl, "stack", "gap_m30", "rev15", "always_up", "mkt_gt_05"],
                          boot, mid_ts)

    ctx = dict(args=args, info=info, chk=chk, M=M, ev=ev, ev_tfm=ev_tfm, tfm_info=tfm_info, calib=calib,
               pnl=pnl, cellp=cellp, best_pnl=best_pnl, best_ll=best_ll, stack_info=stack_info,
               train_info=train_info, mid=mid, mid_ts=mid_ts, models=models, signals=signals, rt=rt,
               t_all=t_all, out_dir=out_dir)
    ctx["verdict"] = make_verdict(ctx)
    ctx["limits"] = make_limits(ctx)
    try:
        cold = json.loads(COLD_PATH.read_text()) if COLD_PATH.exists() else {}
    except ValueError:
        cold = {}
    cf = cold.get("prix Polymarket S−30 s")
    ctx["cold_fetch"] = f"{fr_num(cf['secondes'], 0)} s ({cf['note']})" if cf else "—"
    ctx["cold_times"] = "; ".join(f"{k} {fr_num(v['secondes'], 0)} s ({v['note']})" for k, v in cold.items()) or "—"
    with rt("8. CSV, graphiques et README"):
        write_outputs(ctx)
    total = time.perf_counter() - t_all
    rt.rows.append(("total", total))
    pd.DataFrame(rt.rows, columns=["étape", "secondes"]).to_csv(out_dir / "runtime.csv", index=False)
    write_readme(ctx)
    log.info("terminé en %.0f s — %s", total, out_dir / "README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
