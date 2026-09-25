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
    grp_a, grp_b = priority_order(todo)
    log.info("prix S−30 s : %d en cache, %d à collecter (A=%d, B=%d)", len(have & set(res["slug"])),
             len(todo), len(grp_a), len(grp_b))
    t0 = time.perf_counter()
    budget_s = args.budget_min * 60
    new_parts = []
    state = {"stopped": False}

    def progress(k, n, label):
        if k % 1000 == 0 or k == n:
            el = time.perf_counter() - t0
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
                part = pb.fetch_pre_open_prices(client, chunk, workers=args.workers,
                                                progress=lambda k, n, lb=f"{label}{i // 4000}": progress(k, n, lb))
                new_parts.append(part)
                save(new_parts)
    info["http_status"] = dict(session.status)
    info["fetch_seconds"] = time.perf_counter() - t0
    allp = pd.read_parquet(cache_path) if cache_path.exists() else cached
    mk = mk.merge(allp.drop(columns=["error"]), on="slug", how="left")
    info["sampled"] = bool(state["stopped"])
    return mk, info


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
    p.add_argument("--timesfm", type=int, default=1500, help="marchés BTC du sous-échantillon TimesFM (0 = sauté)")
    p.add_argument("--stage", default="all", choices=["all", "data"])
    p.add_argument("--out", default=str(OUT_DIR))
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    a.start = pd.Timestamp(a.start, tz="UTC")
    a.end = pd.Timestamp(a.end, tz="UTC") if a.end else pd.Timestamp.now(tz="UTC").floor("D")
    a.assets = [x.strip().lower() for x in a.assets.split(",") if x.strip()]
    a.durations = [x.strip() for x in a.durations.split(",") if x.strip()]
    a.s1_start = pd.Timestamp(a.s1_start, tz="UTC")
    return a


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    rt = Runtime()
    t_all = time.perf_counter()
    mk, info = collect_polymarket(args, rt)
    log.info("marchés : %s", json.dumps({k: v for k, v in info.items() if k not in ("sampled_out", "regimes")}, default=str))
    if args.stage == "data":
        log.info("collecte terminée en %.0f s", time.perf_counter() - t_all)
        return 0
    raise SystemExit("étapes suivantes : à venir")


if __name__ == "__main__":
    sys.exit(main())
