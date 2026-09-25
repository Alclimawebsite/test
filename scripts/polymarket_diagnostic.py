#!/usr/bin/env python
"""Diagnostic empirique des marchés Polymarket crypto « Up or Down » (5 min / 15 min).

Usage (reproductible) ::

    . .venv/bin/activate
    python scripts/polymarket_diagnostic.py                 # 7 derniers jours complets, btc/eth/sol, 5m/15m
    python scripts/polymarket_diagnostic.py --days 3        # plus rapide
    python scripts/polymarket_diagnostic.py --end 2026-09-25 --days 7   # période figée

Pour chaque marché résolu de la période :

* issue officielle (``outcomePrices``, via :mod:`tradebot.polymarket`) et niveaux Chainlink
  ``eventMetadata.priceToBeat`` / ``finalPrice`` (lus directement sur gamma, car le client ne
  conserve pas l'événement) ;
* prix du jeton Up (``/prices-history``, ≈ 1 point/min) juste avant l'ouverture (dernier point
  <= début − 30 s) et à début + 1, 2, 5 (et 10) min ; volume ;
* proxies Binance (klines 1m ET 1s, zips journaliers data.binance.vision) de l'issue ;
* baselines causales calculées sur Binance AVANT l'ouverture et P&L théorique (in-sample).

Sorties : ``reports/polymarket/diagnostic.md`` et ``reports/polymarket/*.csv``.
Caches : ``data/cache/polymarket`` (client) et ``data/cache/diag`` (Binance, métadonnées).
Lecture seule : aucune fonction d'ordre, aucune clé.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from scipy import stats
from sklearn.metrics import roc_auc_score

from tradebot import polymarket as pm
from tradebot.config import CACHE_DIR, REPORTS_DIR

log = logging.getLogger("polymarket_diagnostic")

DIAG_CACHE = CACHE_DIR / "diag"
OUT_DIR = REPORTS_DIR / "polymarket"
EPOCH = pd.Timestamp(0, tz="UTC")

BINANCE_BULK = "https://data.binance.vision/data/spot/daily/klines/{sym}/{iv}/{sym}-{iv}-{day}.zip"
BINANCE_API = "https://data-api.binance.vision/api/v3/klines"
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
    "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]

PRE_OFFSET_S = 30                      # prix « avant l'ouverture » : dernier point <= début − 30 s
PRE_STALENESS = pd.Timedelta("5min")   # avant l'ouverture le prix est plat (≈ 0,505)
IN_STALENESS = pd.Timedelta("90s")     # pendant la fenêtre : points ≈ toutes les 60 s
HORIZONS_MIN = {"5m": (1, 2, 5), "15m": (1, 2, 5, 10)}
PRICE_LOOKBACK_S = 900                 # historique demandé : [début − 15 min, fin + 1 min]
SHARES = 100                           # taille d'un ordre simulé (parts)
ASK_MARKUP = 0.005                     # ask ≈ milieu + 0,005 (carnet 0,50 / 0,51)
LAG_SCAN = range(-10, 11)              # décalage (s) testé entre Binance et Chainlink
# Fenêtres (s, relatives à S, [a, b)) où l'on relève les prix payés par les preneurs (transactions)
TRADE_WINDOWS = {"pre30": (-30, 0), "open5": (0, 6)}

# (nom, résolution, description, colonne fin, colonne début) — issue proxy : fin >= début
PROXIES = [
    ("a_close_vs_open_1m", "1m", "(a) close de la bougie 1m finissant à la fin ≥ open de la bougie 1m commençant au début",
     "bn_close1m_E", "bn_open1m_S"),
    ("a2_close_vs_close_1m", "1m", "(a') close 1m à la fin ≥ close 1m de la bougie précédant l'ouverture (= label r_h actuel)",
     "bn_close1m_E", "bn_close1m_S"),
    ("b_vwap_last_vs_first_1m", "1m", "(b) VWAP 1m de la dernière minute ≥ VWAP 1m de la PREMIÈRE minute de la fenêtre",
     "bn_vwap1m_E", "bn_vwap1m_first"),
    ("b_ohlc4_last_vs_first_1m", "1m", "(b) (O+H+L+C)/4 dernière minute ≥ idem première minute de la fenêtre",
     "bn_ohlc4_E", "bn_ohlc4_first"),
    ("b_vwap_last_vs_prev_1m", "1m", "(b*) VWAP 1m de la dernière minute ≥ VWAP 1m de la minute PRÉCÉDANT l'ouverture",
     "bn_vwap1m_E", "bn_vwap1m_S"),
    ("b_ohlc4_last_vs_prev_1m", "1m", "(b*) (O+H+L+C)/4 dernière minute ≥ idem minute précédant l'ouverture",
     "bn_ohlc4_E", "bn_ohlc4_S"),
    ("b_twap60_last_vs_first_1s", "1s", "(b) moyenne des closes 1s sur (E−60, E] ≥ moyenne sur (S, S+60] (première minute)",
     "bn_twap60_E", "bn_first60_S"),
    ("b_twap60_1s", "1s", "(b*) TWAP60 exact : moyenne des closes 1s sur (E−60, E] ≥ moyenne sur (S−60, S]",
     "bn_twap60_E", "bn_twap60_S"),
    ("b_vwap60_1s", "1s", "(b*) VWAP 60 s (1s) sur (E−60, E] ≥ VWAP 60 s sur (S−60, S]",
     "bn_vwap60_E", "bn_vwap60_S"),
    ("b_twap30_1s", "1s", "(contrôle) TWAP 30 s (1s) fin ≥ début — pour vérifier la fenêtre de 60 s",
     "bn_twap30_E", "bn_twap30_S"),
    ("x_twap60_1s_lag4", "1s", "(exploratoire) TWAP60 1s décalé de 4 s : moyenne sur (t−64, t−4] — décalage choisi sur ces données",
     "bn_twap60m4_E", "bn_twap60m4_S"),
    ("c_window_twap_vs_open_1m", "1m", "(c) moyenne des closes 1m de toute la fenêtre ≥ open 1m au début",
     "bn_win1m", "bn_open1m_S"),
    ("c_window_twap_vs_open_1s", "1s", "(c) moyenne des closes 1s de toute la fenêtre ≥ open 1s au début",
     "bn_win1s", "bn_open1s_S"),
]

# Niveaux Binance comparés aux niveaux Chainlink (priceToBeat à S, finalPrice à E)
LEVELS = [
    ("spot_1s", "dernier prix (close 1s) avant t"),
    ("twap60_1s", "moyenne des closes 1s sur (t−60, t]"),
    ("twap60_open_1s", "moyenne des opens 1s sur [t−60, t)"),
    ("vwap60_1s", "VWAP 60 s (1s)"),
    ("twap30_1s", "moyenne des closes 1s sur (t−30, t]"),
    ("vwap_1m", "VWAP de la bougie 1m [t−1m, t)"),
    ("ohlc4_1m", "(O+H+L+C)/4 de la bougie 1m [t−1m, t)"),
    ("close_1m", "close de la bougie 1m [t−1m, t)"),
]

# Baselines pré-ouverture : (nom, coupure d'information, description)
FEATURES = [
    ("mom5_m60", "S−60s (1m)", "rendement des 5 dernières min, bougies 1m closes à S−60 s"),
    ("mom15_m60", "S−60s (1m)", "rendement des 15 dernières min, bougies 1m closes à S−60 s"),
    ("mom5_m30", "S−30s (1s)", "rendement des 5 dernières min, prix 1s à S−30 s"),
    ("mom15_m30", "S−30s (1s)", "rendement des 15 dernières min, prix 1s à S−30 s"),
    ("gap_m30", "S−30s (1s)", "prix à S−30 s − moyenne 1s sur (S−60, S−30] (TWAP partiel)"),
    ("mom5_m0", "S (1m) *", "rendement des 5 dernières min, bougies 1m closes à S"),
    ("mom15_m0", "S (1m) *", "rendement des 15 dernières min, bougies 1m closes à S"),
    ("gap_m0_1m", "S (1m) *", "close 1m − VWAP 1m de la bougie [S−1m, S)"),
    ("gap_m0_1s", "S (1s) *", "spot(S) − TWAP60(S) Binance (écart spot − TWAP)"),
]
CLEAN_CUTOFFS = ("S−60s (1m)", "S−30s (1s)")   # compatibles avec un prix de marché pris à S−30 s


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


def make_session(pool: int = 32) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "tradebot/0.1 (read-only research; polymarket_diagnostic)"})
    return s


def _get_retry(session: requests.Session, url: str, params=None, timeout=120, tries=4) -> requests.Response:
    last = None
    for k in range(tries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code in (429,) or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code} {url}")
                time.sleep(1.5 * 2**k)
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
            time.sleep(1.5 * 2**k)
    raise RuntimeError(f"échec {url}") from last


def to_unix(s: pd.Series) -> np.ndarray:
    return ((s - EPOCH) // pd.Timedelta("1s")).to_numpy().astype("int64")


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def binom_p(k: int, n: int) -> float:
    return float(stats.binomtest(int(k), int(n), 0.5).pvalue) if n else math.nan


def auc_hanley(y: np.ndarray, s: np.ndarray) -> tuple[float, float, float]:
    """AUC et IC 95 % (Hanley & McNeil 1982)."""
    ok = np.isfinite(y) & np.isfinite(s)
    y, s = y[ok].astype(int), s[ok]
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0 or np.unique(s).size < 2:
        return (0.5 if n1 and n0 else math.nan), math.nan, math.nan
    a = float(roc_auc_score(y, s))
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    se = math.sqrt(max(a * (1 - a) + (n1 - 1) * (q1 - a * a) + (n0 - 1) * (q2 - a * a), 0) / (n1 * n0))
    return a, a - 1.96 * se, a + 1.96 * se


def cluster_mean_ci(x: np.ndarray, groups: np.ndarray) -> tuple[float, float, float]:
    """Moyenne et IC 95 % robuste aux corrélations intra-groupe (même créneau de 15 min)."""
    ok = np.isfinite(x)
    x, g = x[ok], groups[ok]
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else math.nan), math.nan, math.nan
    m = x.mean()
    sums = pd.Series(x - m).groupby(g).sum().to_numpy()
    se = math.sqrt((sums**2).sum()) / n
    return float(m), float(m - 1.96 * se), float(m + 1.96 * se)


# -- mise en forme française -------------------------------------------------
def fr(x, nd: int = 2) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and not np.isfinite(x)):
        return "—"
    if isinstance(x, (bool, np.bool_)):
        return "oui" if x else "non"
    if isinstance(x, (int, np.integer)):
        return f"{int(x):,}".replace(",", " ")
    return f"{float(x):,.{nd}f}".replace(",", " ").replace(".", ",")


def pct(x, nd: int = 1) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return fr(100 * float(x), nd) + " %"


def md_table(df: pd.DataFrame, formats: dict | None = None) -> str:
    formats = formats or {}
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            f = formats.get(c)
            if f is not None:
                cells.append(f(v))
            elif isinstance(v, str):
                cells.append(v)
            elif isinstance(v, (int, np.integer)):
                cells.append(fr(v))
            elif isinstance(v, (float, np.floating)):
                cells.append(fr(v, 3))
            else:
                cells.append("—" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def cell_name(asset: str, duration: str) -> str:
    return f"{asset.upper()} {duration}"


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
def fetch_event_meta(client: pm.PolymarketClient, slugs: list[str], workers: int, use_cache: bool) -> pd.DataFrame:
    """``eventMetadata`` (priceToBeat, finalPrice) des événements — absent de UpDownMarket.

    Contournement : le client ne garde que ``event["markets"][0]`` ; on relit ``/events`` avec
    son ``_get`` (limiteur de débit + retentatives). Cache : data/cache/diag/event_meta.parquet
    (événements clos avec finalPrice uniquement).
    """
    path = DIAG_CACHE / "event_meta.parquet"
    cols = ["slug", "price_to_beat", "final_price", "event_closed", "resolution_source", "series_slug"]
    cached = pd.read_parquet(path) if (use_cache and path.exists()) else pd.DataFrame(columns=cols)
    have = set(cached["slug"])
    todo = [s for s in slugs if s not in have]

    def one(batch: list[str]) -> list[dict]:
        params = [("slug", s) for s in batch] + [("limit", len(batch))]
        rows = []
        for ev in client._get(f"{pm.GAMMA_URL}/events", params) or []:
            md = ev.get("eventMetadata") or {}
            rows.append({
                "slug": ev.get("slug"),
                "price_to_beat": pm._f(md.get("priceToBeat")),
                "final_price": pm._f(md.get("finalPrice")),
                "event_closed": bool(ev.get("closed")),
                "resolution_source": ev.get("resolutionSource"),
                "series_slug": ev.get("seriesSlug"),
            })
        return rows

    new: list[dict] = []
    if todo:
        batches = [todo[i: i + pm.GAMMA_BATCH] for i in range(0, len(todo), pm.GAMMA_BATCH)]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as ex:
            for rows in ex.map(one, batches):
                new.extend(rows)
    new_df = pd.DataFrame(new, columns=cols)
    out = pd.concat([cached, new_df], ignore_index=True).drop_duplicates("slug", keep="last")
    if use_cache and len(new_df):
        keep = out[out["event_closed"].astype(bool) & np.isfinite(out["final_price"].astype(float))]
        DIAG_CACHE.mkdir(parents=True, exist_ok=True)
        pm._atomic_parquet(keep.reset_index(drop=True), path)
    return out[out["slug"].isin(set(slugs))].reset_index(drop=True)


def point_at(hist: pd.Series, ts: pd.Timestamp, staleness: pd.Timedelta) -> tuple[float, pd.Timestamp | None]:
    """Dernier point <= ts (prix, horodatage) ; NaN si trop ancien — même logique que pm.up_price_at."""
    if hist is None or len(hist) == 0:
        return math.nan, None
    i = hist.index.searchsorted(ts, side="right") - 1
    if i < 0:
        return math.nan, None
    t = hist.index[i]
    if ts - t > staleness:
        return math.nan, None
    return float(hist.iloc[i]), t


def fetch_price_points(client: pm.PolymarketClient, markets: list[pm.UpDownMarket], workers: int) -> tuple[pd.DataFrame, dict]:
    """Prix du jeton Up aux instants d'intérêt pour chaque marché (historique CLOB fidelity=1)."""
    checks = {"up_price_at_mismatch": 0, "no_history": 0, "errors": 0}

    def one(m: pm.UpDownMarket) -> dict:
        S, E = int(m.start.timestamp()), int(m.end.timestamp())
        row: dict = {"slug": m.slug}
        try:
            hist = client.prices_history(m.token_up, S - PRICE_LOOKBACK_S, E + 60, fidelity=1, resolved=True)
        except pm.PolymarketError as exc:
            log.warning("prices-history %s : %s", m.slug, exc)
            row["hist_error"] = True
            return row
        row["n_hist_points"] = len(hist)
        t_pre = m.start - pd.Timedelta(seconds=PRE_OFFSET_S)
        p, t = point_at(hist, t_pre, PRE_STALENESS)
        ref = pm.up_price_at(hist, t_pre, max_staleness=PRE_STALENESS)
        row["_mismatch"] = not ((np.isnan(p) and np.isnan(ref)) or p == ref)
        row["p_pre"] = p
        row["p_pre_age_s"] = (t_pre - t).total_seconds() if t is not None else math.nan
        p, t = point_at(hist, m.start, IN_STALENESS)
        row["p_S"] = p
        row["p_S_age_s"] = (m.start - t).total_seconds() if t is not None else math.nan
        for k in HORIZONS_MIN[m.duration]:
            ts = m.start + pd.Timedelta(minutes=k)
            p, t = point_at(hist, ts, IN_STALENESS)
            row[f"p_{k}m"] = p
            row[f"t_{k}m"] = int(t.timestamp()) if t is not None else np.nan
        return row

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, markets))
    df = pd.DataFrame(rows)
    checks["up_price_at_mismatch"] = int(df.get("_mismatch", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    checks["errors"] = int(df.get("hist_error", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    checks["no_history"] = int((df.get("n_hist_points", pd.Series(dtype=float)).fillna(0) == 0).sum())
    return df.drop(columns=[c for c in ("_mismatch", "hist_error") if c in df.columns]), checks


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------
def _klines_frame(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw[pd.to_numeric(raw["open_time"], errors="coerce").notna()]  # en-tête éventuel
    ot = pd.to_numeric(raw["open_time"]).astype("int64").to_numpy()
    unit = "us" if ot.max() > 10**14 else "ms"   # zips spot 2025+ : microsecondes
    idx = pd.to_datetime(ot, unit=unit, utc=True).as_unit("ns")
    out = pd.DataFrame(
        {c: pd.to_numeric(raw[c]).astype("float64").to_numpy() for c in ("open", "high", "low", "close", "volume", "quote_volume")},
        index=pd.DatetimeIndex(idx, name="time"),
    )
    return out[~out.index.duplicated(keep="last")].sort_index()


def _api_klines(session, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    step = 1000 if interval == "1s" else 60_000
    t, end_ms, rows = int(start.timestamp() * 1000), int(end.timestamp() * 1000), []
    while t < end_ms:
        r = _get_retry(session, BINANCE_API, {"symbol": symbol, "interval": interval, "startTime": t,
                                               "endTime": end_ms - 1, "limit": 1000}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        t = int(data[-1][0]) + step
    return _klines_frame(pd.DataFrame(rows, columns=KLINE_COLS)) if rows else pd.DataFrame()


def binance_day(session, symbol: str, interval: str, day: pd.Timestamp, use_cache: bool) -> pd.DataFrame:
    path = DIAG_CACHE / "binance" / f"{symbol}_{interval}_{day:%Y-%m-%d}.parquet"
    if use_cache and path.exists():
        return pd.read_parquet(path)
    r = _get_retry(session, BINANCE_BULK.format(sym=symbol, iv=interval, day=f"{day:%Y-%m-%d}"))
    if r.status_code == 200:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        df = _klines_frame(pd.read_csv(z.open(z.namelist()[0]), header=None, names=KLINE_COLS))
        source = "bulk"
    else:  # jour pas encore publié : API klines
        df = _api_klines(session, symbol, interval, day, day + pd.Timedelta(days=1))
        source = "api"
    expected = 86400 if interval == "1s" else 1440
    log.debug("%s %s %s : %d barres (%s)", symbol, interval, day.date(), len(df), source)
    if use_cache and len(df) == expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        pm._atomic_parquet(df.reset_index(), path)
    return df


def load_binance(session, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp,
                 use_cache: bool, workers: int = 6) -> pd.DataFrame:
    days = pd.date_range(start.floor("D"), (end - pd.Timedelta("1s")).floor("D"), freq="D")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        parts = list(ex.map(lambda d: binance_day(session, symbol, interval, d, use_cache), days))
    parts = [p.set_index("time") if "time" in p.columns else p for p in parts if len(p)]
    df = pd.concat(parts).sort_index()
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC").as_unit("ns")
    return df[~df.index.duplicated(keep="last")]


class Bars:
    """Bougies régulières (pas ``step`` s) adressées par leur horodatage d'OUVERTURE en s Unix.

    Les secondes/minutes manquantes sont comblées (close reporté, volume nul) et comptées.
    Toute lecture hors de la plage renvoie NaN.
    """

    def __init__(self, df: pd.DataFrame, step: int):
        t = ((df.index - EPOCH) // pd.Timedelta("1s")).to_numpy().astype("int64")
        self.step = step
        self.t0 = int(t.min())
        self.n = int((t.max() - self.t0) // step + 1)
        pos = (t - self.t0) // step

        def arr(col: str) -> np.ndarray:
            a = np.full(self.n, np.nan)
            a[pos] = df[col].to_numpy(dtype="float64")
            return a

        close = arr("close")
        self.n_missing = int(np.isnan(close).sum())
        close = pd.Series(close).ffill().to_numpy()
        o, h, lo = arr("open"), arr("high"), arr("low")
        miss = np.isnan(o)
        o[miss], h[miss], lo[miss] = close[miss], close[miss], close[miss]
        self.cols = {
            "open": o, "high": h, "low": lo, "close": close,
            "volume": np.nan_to_num(arr("volume")), "quote_volume": np.nan_to_num(arr("quote_volume")),
        }
        self.cols["ohlc4"] = (o + h + lo + close) / 4
        self._cum: dict[str, np.ndarray] = {}

    def _idx(self, t) -> np.ndarray:
        return (np.asarray(t, dtype="int64") - self.t0) // self.step

    def _take(self, a: np.ndarray, i: np.ndarray) -> np.ndarray:
        ok = (i >= 0) & (i < self.n)
        out = np.full(i.shape, np.nan)
        out[ok] = a[i[ok]]
        return out

    def at(self, col: str, t) -> np.ndarray:
        """Valeur de la bougie qui s'OUVRE à t."""
        return self._take(self.cols[col], self._idx(t))

    def window(self, col: str, a, length: int) -> np.ndarray:
        """Matrice (n, length/step) des bougies qui s'ouvrent dans [a, a + length)."""
        k = int(length // self.step)
        i = self._idx(a)[:, None] + np.arange(k)[None, :]
        return self._take(self.cols[col], i)

    def mean(self, col: str, a, length: int) -> np.ndarray:
        return self.window(col, a, length).mean(axis=1)

    def vwap(self, a, length: int) -> np.ndarray:
        q = self.window("quote_volume", a, length).sum(axis=1)
        v = self.window("volume", a, length).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = q / v
        return np.where(v > 0, out, self.mean("close", a, length))

    def range_sum(self, col: str, a, b) -> np.ndarray:
        """Somme de ``col`` sur les bougies qui s'ouvrent dans [a, b) (longueurs variables)."""
        if col not in self._cum:
            self._cum[col] = np.concatenate([[0.0], np.cumsum(self.cols[col])])
        c = self._cum[col]
        ia, ib = self._idx(a), self._idx(b)
        ok = (ia >= 0) & (ib <= self.n) & (ib >= ia)
        out = np.full(ia.shape, np.nan)
        out[ok] = c[ib[ok]] - c[ia[ok]]
        return out


# ---------------------------------------------------------------------------
# Variables Binance par marché
# ---------------------------------------------------------------------------
def binance_features(g: pd.DataFrame, b1s: Bars, b1m: Bars, dur_s: int) -> pd.DataFrame:
    S, E = g["start_ts"].to_numpy(), g["end_ts"].to_numpy()
    f: dict[str, np.ndarray] = {}
    # -- niveaux pour les proxies de l'issue (utilisent des données APRÈS l'ouverture : étiquettes)
    f["bn_open1m_S"] = b1m.at("open", S)
    f["bn_close1m_S"] = b1m.at("close", S - 60)      # bougie [S−1m, S)
    f["bn_close1m_E"] = b1m.at("close", E - 60)      # bougie [E−1m, E)
    f["bn_vwap1m_S"] = b1m.vwap(S - 60, 60)
    f["bn_vwap1m_first"] = b1m.vwap(S, 60)
    f["bn_vwap1m_E"] = b1m.vwap(E - 60, 60)
    f["bn_ohlc4_S"] = b1m.at("ohlc4", S - 60)
    f["bn_ohlc4_first"] = b1m.at("ohlc4", S)
    f["bn_ohlc4_E"] = b1m.at("ohlc4", E - 60)
    f["bn_win1m"] = b1m.mean("close", S, dur_s)
    f["bn_open1s_S"] = b1s.at("open", S)
    f["bn_win1s"] = b1s.mean("close", S, dur_s)
    for tag, T in (("S", S), ("E", E)):
        f[f"bn_twap60_{tag}"] = b1s.mean("close", T - 60, 60)     # closes des bougies [T−60, T) = prix sur (T−60, T]
        f[f"bn_vwap60_{tag}"] = b1s.vwap(T - 60, 60)
        f[f"bn_twap30_{tag}"] = b1s.mean("close", T - 30, 30)
        f[f"bn_twap60m4_{tag}"] = b1s.mean("close", T - 64, 60)
    f["bn_first60_S"] = b1s.mean("close", S, 60)
    # -- niveaux candidats à S et E pour la comparaison avec Chainlink
    for tag, T in (("S", S), ("E", E)):
        f[f"lvl_spot_1s_{tag}"] = b1s.at("close", T - 1)
        f[f"lvl_twap60_1s_{tag}"] = f[f"bn_twap60_{tag}"]
        f[f"lvl_twap60_open_1s_{tag}"] = b1s.mean("open", T - 60, 60)
        f[f"lvl_vwap60_1s_{tag}"] = f[f"bn_vwap60_{tag}"]
        f[f"lvl_twap30_1s_{tag}"] = f[f"bn_twap30_{tag}"]
        f[f"lvl_vwap_1m_{tag}"] = b1m.vwap(T - 60, 60)
        f[f"lvl_ohlc4_1m_{tag}"] = b1m.at("ohlc4", T - 60)
        f[f"lvl_close_1m_{tag}"] = b1m.at("close", T - 60)
    # -- baselines pré-ouverture (STRICTEMENT causales)
    c1m = lambda t: b1m.at("close", t)  # noqa: E731  close de la bougie qui s'ouvre à t (connu à t+60)
    c1s = lambda t: b1s.at("close", t)  # noqa: E731  close de la bougie 1s qui s'ouvre à t (connu à t+1)
    with np.errstate(invalid="ignore", divide="ignore"):
        # coupure S−60 s : dernière bougie 1m close = celle qui s'ouvre à S−120
        f["mom5_m60"] = np.log(c1m(S - 120) / c1m(S - 420))
        f["mom15_m60"] = np.log(c1m(S - 120) / c1m(S - 1020))
        # coupure S−30 s : dernière bougie 1s close = celle qui s'ouvre à S−31
        spot30 = c1s(S - 31)
        f["mom5_m30"] = np.log(spot30 / c1s(S - 331))
        f["mom15_m30"] = np.log(spot30 / c1s(S - 931))
        f["gap_m30"] = np.log(spot30 / b1s.mean("close", S - 60, 30))
        # coupure S (optimiste pour un prix de marché à S−30 s)
        f["mom5_m0"] = np.log(c1m(S - 60) / c1m(S - 360))
        f["mom15_m0"] = np.log(c1m(S - 60) / c1m(S - 960))
        f["gap_m0_1m"] = np.log(c1m(S - 60) / b1m.vwap(S - 60, 60))
        f["gap_m0_1s"] = np.log(c1s(S - 1) / f["bn_twap60_S"])
        # volatilité par seconde (1m, 60 min avant S) pour le nowcast en cours de fenêtre
        w = b1m.window("close", S - 3660, 3660)
        f["sigma_s"] = np.nanstd(np.diff(np.log(w), axis=1), axis=1, ddof=1) / math.sqrt(60)
    return pd.DataFrame(f, index=g.index)


def nowcast(b1s: Bars, S: np.ndarray, E: np.ndarray, tau: np.ndarray, twap_s: np.ndarray, sigma_s: np.ndarray) -> np.ndarray:
    """P(Up) « Binance seul » à l'instant tau (s) : marche aléatoire sans dérive.

    Up ⇔ TWAP60(E) >= TWAP60(S). À tau (S <= tau <= E) on connaît TWAP60(S) et le prix spot ;
    si tau > E − 60, une partie du TWAP final est déjà connue.
    """
    p = np.full(len(S), np.nan)
    tau = np.asarray(tau, dtype=float)
    ok = np.isfinite(tau) & np.isfinite(twap_s) & np.isfinite(sigma_s) & (sigma_s > 0)
    ok &= (np.nan_to_num(tau, nan=-1) >= S) & (np.nan_to_num(tau, nan=np.inf) <= E)
    if not ok.any():
        return p
    t = tau[ok].astype("int64")
    s_, e_, sig = S[ok], E[ok], sigma_s[ok]
    spot = b1s.at("close", t - 1)
    u = (e_ - t).astype(float)
    in_last = t > e_ - 60
    known = b1s.range_sum("close", np.where(in_last, e_ - 60, t), np.where(in_last, t, t))
    known = np.where(in_last, known, 0.0)
    mean_level = np.where(in_last, (known + spot * u) / 60.0, spot)
    var = np.where(in_last, sig**2 * u**3 / (3 * 3600.0), sig**2 * (e_ - t - 40))
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (np.log(mean_level) - np.log(twap_s[ok])) / np.sqrt(var)
    z = np.where(var > 0, z, np.where(np.log(mean_level) >= np.log(twap_s[ok]), np.inf, -np.inf))
    p[ok] = np.clip(stats.norm.cdf(z), 0.001, 0.999)
    return p


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------
def cells(D: pd.DataFrame):
    for (a, d), g in D.groupby(["asset", "duration"], sort=False):
        yield cell_name(a, d), g
    for d, g in D.groupby("duration", sort=False):
        yield f"Tous {d}", g
    yield "Tous", D


def base_rates(D: pd.DataFrame, expected: dict) -> pd.DataFrame:
    rows = []
    for name, g in cells(D):
        y = g["y"].dropna()
        k, n = int(y.sum()), len(y)
        lo, hi = wilson(k, n)
        ties = g["tie_cl"].sum()
        exp = sum(v for (a, d), v in expected.items() if cell_name(a, d) == name or name == "Tous"
                  or name == f"Tous {d}")
        rows.append({
            "cellule": name, "créneaux attendus": exp, "marchés": len(g), "résolus": n, "up": k,
            "taux_up": k / n if n else math.nan, "ic95_bas": lo, "ic95_haut": hi, "p_binom": binom_p(k, n),
            "égalités_chainlink": int(ties), "avec_eventMetadata": int(np.isfinite(g["final_price"]).sum()),
            "mouvement_médian_pb": float(np.nanmedian(np.abs(g["cl_move_bps"]))),
            "volume_médian_usd": float(g["volume"].median()), "volume_moyen_usd": float(g["volume"].mean()),
            "volume_total_usd": float(g["volume"].sum()),
        })
    return pd.DataFrame(rows)


def proxy_agreement(D: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, g in cells(D):
        for pname, res, desc, c_end, c_start in PROXIES:
            x_end, x_start = g[c_end].to_numpy(), g[c_start].to_numpy()
            ok = np.isfinite(x_end) & np.isfinite(x_start) & g["y"].notna().to_numpy()
            pred = (x_end >= x_start)[ok]
            y = g["y"].to_numpy()[ok].astype(bool)
            n, k = len(y), int((pred == y).sum())
            lo, hi = wilson(k, n)
            mv = np.abs(g["cl_move_bps"].to_numpy()[ok])
            dis = mv[(pred != y)]
            dis, mv = dis[np.isfinite(dis)], mv[np.isfinite(mv)]
            rows.append({
                "cellule": name, "proxy": pname, "résolution": res, "n": n, "accord": k / n if n else math.nan,
                "ic95_bas": lo, "ic95_haut": hi, "désaccords": n - k,
                "égalités_binance": int((x_end == x_start)[ok].sum()),
                "mouvement_cl_médian_désaccords_pb": float(np.median(dis)) if len(dis) else math.nan,
                "mouvement_cl_médian_tous_pb": float(np.median(mv)) if len(mv) else math.nan,
                "description": desc,
            })
    return pd.DataFrame(rows)


def level_check(D: pd.DataFrame) -> pd.DataFrame:
    """Quel niveau Binance reproduit le mieux les niveaux Chainlink (priceToBeat / finalPrice) ?"""
    rows = []
    for name, g in cells(D):
        cl_s, cl_e = g["price_to_beat"].to_numpy(), g["final_price"].to_numpy()
        d_cl = 1e4 * np.log(cl_e / cl_s)
        for lv, desc in LEVELS:
            bs, be = g[f"lvl_{lv}_S"].to_numpy(), g[f"lvl_{lv}_E"].to_numpy()
            ok = np.isfinite(bs) & np.isfinite(be) & np.isfinite(cl_s) & np.isfinite(cl_e)
            basis = 1e4 * np.log(np.concatenate([bs[ok] / cl_s[ok], be[ok] / cl_e[ok]]))
            d_bn = 1e4 * np.log(be[ok] / bs[ok])
            err = d_bn - d_cl[ok]
            rows.append({
                "cellule": name, "niveau": lv, "n": int(ok.sum()),
                "écart_moyen_pb": float(basis.mean()), "écart_sd_pb": float(basis.std(ddof=1)),
                "rmse_variation_pb": float(np.sqrt(np.mean(err**2))),
                "corr_variation": float(np.corrcoef(d_bn, d_cl[ok])[0, 1]),
                "accord_signe": float(np.mean((d_bn >= 0) == (d_cl[ok] >= 0))),
                "description": desc,
            })
    return pd.DataFrame(rows)


def lag_scan(D: pd.DataFrame, bars1s: dict[str, Bars]) -> pd.DataFrame:
    rows = []
    for asset, g in D.groupby("asset", sort=False):
        b = bars1s[asset]
        S, E = g["start_ts"].to_numpy(), g["end_ts"].to_numpy()
        cl_s, cl_e = g["price_to_beat"].to_numpy(), g["final_price"].to_numpy()
        d_cl = 1e4 * np.log(cl_e / cl_s)
        y = g["y"].to_numpy()
        for lag in LAG_SCAN:
            bs, be = b.mean("close", S - 60 + lag, 60), b.mean("close", E - 60 + lag, 60)
            ok = np.isfinite(bs) & np.isfinite(be) & np.isfinite(d_cl) & np.isfinite(y)
            d_bn = 1e4 * np.log(be[ok] / bs[ok])
            rows.append({
                "actif": asset.upper(), "décalage_s": lag, "n": int(ok.sum()),
                "rmse_variation_pb": float(np.sqrt(np.mean((d_bn - d_cl[ok]) ** 2))),
                "sd_écart_niveau_pb": float(np.std(1e4 * np.log(bs[ok] / cl_s[ok]), ddof=1)),
                "accord_issue": float(np.mean((d_bn >= 0) == y[ok].astype(bool))),
            })
    return pd.DataFrame(rows)


def market_power(D: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pouvoir prédictif du prix du jeton Up avant l'ouverture et pendant la fenêtre."""
    rows, calib = [], []
    for name, g in cells(D):
        horizons = [("pre", "S−30 s (avant ouverture)"), ("S", "S (dernier point ≤ S)")]
        durs = g["duration"].unique()
        ks = sorted(set(k for d in durs for k in HORIZONS_MIN[d]))
        horizons += [(f"{k}m", f"S+{k} min") for k in ks]
        for key, label in horizons:
            col = f"p_{key}"
            if col not in g.columns:
                continue
            p, y = g[col].to_numpy(dtype=float), g["y"].to_numpy(dtype=float)
            ok = np.isfinite(p) & np.isfinite(y)
            if ok.sum() == 0:
                continue
            p, y = p[ok], y[ok]
            auc, alo, ahi = auc_hanley(y, p)
            brier = float(np.mean((p - y) ** 2))
            ybar = y.mean()
            pc = np.clip(p, 1e-4, 1 - 1e-4)
            rows.append({
                "cellule": name, "instant": label, "n": int(ok.sum()),
                "prix_moyen": float(p.mean()), "prix_sd": float(p.std(ddof=1)) if len(p) > 1 else math.nan,
                "part_0.45_0.55": float(np.mean((p >= 0.45) & (p <= 0.55))),
                "part_0.505": float(np.mean(np.abs(p - 0.505) < 1e-9)),
                "écart_moyen_à_0.5": float(np.mean(np.abs(p - 0.5))),
                "part_décidé_0.1_0.9": float(np.mean((p <= 0.1) | (p >= 0.9))),
                "accuracy": float(np.mean((p >= 0.5) == (y == 1))),
                "brier": brier, "brier_ref_0.25": 0.25,
                "bss_climato": 1 - brier / (ybar * (1 - ybar)) if 0 < ybar < 1 else math.nan,
                "log_loss": float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc))),
                "auc": auc, "auc_ic_bas": alo, "auc_ic_haut": ahi,
            })
            if name.startswith("Tous "):
                bins = np.minimum((p * 10).astype(int), 9)
                for b in range(10):
                    m = bins == b
                    if m.sum() == 0:
                        continue
                    k, n = int(y[m].sum()), int(m.sum())
                    lo, hi = wilson(k, n)
                    calib.append({"cellule": name, "instant": label, "décile": f"{fr(b / 10, 1)}–{fr((b + 1) / 10, 1)}",
                                  "n": n, "prix_moyen": float(p[m].mean()), "freq_up": k / n, "ic95_bas": lo, "ic95_haut": hi})
    return pd.DataFrame(rows), pd.DataFrame(calib)


def nowcast_columns(D: pd.DataFrame, bars1s: dict[str, Bars]) -> pd.DataFrame:
    """Nowcast Binance P(Up) calculé À L'HORODATAGE de chaque point de prix du marché (même information)."""
    parts = []
    for (asset, dur), g in D.groupby(["asset", "duration"], sort=False):
        b = bars1s[asset]
        S, E = g["start_ts"].to_numpy(), g["end_ts"].to_numpy()
        cols = {}
        for k in HORIZONS_MIN[dur]:
            cols[f"nowcast_{k}m"] = nowcast(b, S, E, g[f"t_{k}m"].to_numpy(dtype=float),
                                            g["bn_twap60_S"].to_numpy(), g["sigma_s"].to_numpy())
        parts.append(pd.DataFrame(cols, index=g.index))
    return pd.concat(parts).reindex(D.index)


def nowcast_vs_market(D: pd.DataFrame) -> pd.DataFrame:
    """Le prix du marché contre le nowcast Binance au même instant ; ΔBrier apparié (IC robuste)."""
    rows = []
    for name, g in cells(D):
        ks = sorted(set(k for d in g["duration"].unique() for k in HORIZONS_MIN[d]))
        for k in ks:
            if f"nowcast_{k}m" not in g.columns:
                continue
            pn, pmk = g[f"nowcast_{k}m"].to_numpy(dtype=float), g[f"p_{k}m"].to_numpy(dtype=float)
            y, tau = g["y"].to_numpy(dtype=float), g[f"t_{k}m"].to_numpy(dtype=float)
            ok = np.isfinite(pn) & np.isfinite(pmk) & np.isfinite(y)
            if not ok.any():
                continue
            pn, pmk, y, tau, S = pn[ok], pmk[ok], y[ok], tau[ok], g["start_ts"].to_numpy()[ok]
            d_brier = (pn - y) ** 2 - (pmk - y) ** 2
            m, lo, hi = cluster_mean_ci(d_brier, S // 900)
            rows.append({
                "cellule": name, "instant": f"S+{k} min", "n": int(ok.sum()),
                "âge_moyen_point_s": float(np.mean(S + 60 * k - tau)),
                "acc_marché": float(np.mean((pmk >= 0.5) == (y == 1))),
                "acc_nowcast": float(np.mean((pn >= 0.5) == (y == 1))),
                "brier_marché": float(np.mean((pmk - y) ** 2)),
                "brier_nowcast": float(np.mean((pn - y) ** 2)),
                "delta_brier": m, "delta_brier_ic_bas": lo, "delta_brier_ic_haut": hi,
                "auc_marché": auc_hanley(y, pmk)[0], "auc_nowcast": auc_hanley(y, pn)[0],
                "corr_marché_nowcast": float(np.corrcoef(pmk, pn)[0, 1]),
            })
    return pd.DataFrame(rows)


NOWCAST_LAGS = (0, 10, 20, 30, 45, 60)


def nowcast_lag_table(D: pd.DataFrame, bars1s: dict[str, Bars]) -> pd.DataFrame:
    """Robustesse : nowcast calculé Δ secondes AVANT l'horodatage du point de marché.

    Si le point ``prices-history`` reflétait un état antérieur à son horodatage (agrégation par
    minute), le nowcast décalé devrait rejoindre le marché (ΔBrier -> 0, corrélation maximale).
    Mêmes marchés pour tous les Δ (lignes valides pour chaque décalage).
    """
    rows = []
    for dur in D["duration"].unique():
        for k in HORIZONS_MIN[dur]:
            if k < 2:
                continue   # à S+1 min, τ − 60 s tombe souvent avant S
            per_lag = {lag: [] for lag in NOWCAST_LAGS}
            pm_all, y_all, cl_all = [], [], []
            for asset, g in D[D["duration"] == dur].groupby("asset", sort=False):
                S, E = g["start_ts"].to_numpy(), g["end_ts"].to_numpy()
                tau = g[f"t_{k}m"].to_numpy(dtype=float)
                for lag in NOWCAST_LAGS:
                    per_lag[lag].append(nowcast(bars1s[asset], S, E, tau - lag, g["bn_twap60_S"].to_numpy(),
                                                g["sigma_s"].to_numpy()))
                pm_all.append(g[f"p_{k}m"].to_numpy(dtype=float))
                y_all.append(g["y"].to_numpy(dtype=float))
                cl_all.append(S // 900)
            pmk, y, cl = np.concatenate(pm_all), np.concatenate(y_all), np.concatenate(cl_all)
            pns = {lag: np.concatenate(v) for lag, v in per_lag.items()}
            ok = np.isfinite(pmk) & np.isfinite(y)
            for v in pns.values():
                ok &= np.isfinite(v)
            for lag, pn in pns.items():
                d = (pn[ok] - y[ok]) ** 2 - (pmk[ok] - y[ok]) ** 2
                m, lo, hi = cluster_mean_ci(d, cl[ok])
                rows.append({"durée": dur, "instant": f"S+{k} min", "décalage_s": lag, "n": int(ok.sum()),
                             "brier_nowcast": float(np.mean((pn[ok] - y[ok]) ** 2)),
                             "brier_marché": float(np.mean((pmk[ok] - y[ok]) ** 2)),
                             "delta_brier": m, "delta_brier_ic_bas": lo, "delta_brier_ic_haut": hi,
                             "corr_marché_nowcast": float(np.corrcoef(pmk[ok], pn[ok])[0, 1])})
    return pd.DataFrame(rows)


def strategy_predictions(D: pd.DataFrame) -> dict[str, tuple[str, str, np.ndarray]]:
    """nom -> (coupure, description, prédiction Up (1.0) / Down (0.0) / NaN)."""
    out: dict[str, tuple[str, str, np.ndarray]] = {}
    n = len(D)
    out["always_up"] = ("—", "toujours Up", np.ones(n))
    out["always_down"] = ("—", "toujours Down", np.zeros(n))
    p = D["p_pre"].to_numpy(dtype=float)
    out["market_pre"] = ("S−30s (marché)", "prix du marché à S−30 s > 0,5 → Up (égalité → Up)",
                         np.where(np.isfinite(p), (p >= 0.5).astype(float), np.nan))
    for fname, cutoff, desc in FEATURES:
        x = D[fname].to_numpy(dtype=float)
        fin = np.isfinite(x)
        mom = np.where(fin, (x >= 0).astype(float), np.nan)        # égalité → Up
        rev = np.where(fin, (x <= 0).astype(float), np.nan)        # égalité → Up
        out[f"{fname}:momentum"] = (cutoff, f"momentum — {desc}", mom)
        out[f"{fname}:retournement"] = (cutoff, f"retournement — {desc}", rev)
    return out


def baseline_table(D: pd.DataFrame, preds: dict) -> pd.DataFrame:
    rows = []
    y_all = D["y"].to_numpy(dtype=float)
    feat_of = {f"{f}:{r}": f for f, _, _ in FEATURES for r in ("momentum", "retournement")}
    for name, gidx in [(n, g.index) for n, g in cells(D)]:
        pos = D.index.get_indexer(gidx)
        y = y_all[pos]
        groups = (D["start_ts"].to_numpy()[pos] // 900)
        for sname, (cutoff, desc, pred) in preds.items():
            pr = pred[pos]
            ok = np.isfinite(pr) & np.isfinite(y)
            hit = (pr[ok] == y[ok]).astype(float)
            n, k = int(ok.sum()), int(hit.sum())
            lo, hi = wilson(k, n)
            clo, chi = cluster_mean_ci(hit, groups[ok])[1:]
            row = {"cellule": name, "stratégie": sname, "coupure": cutoff, "n": n,
                   "accuracy": k / n if n else math.nan, "ic95_bas": lo, "ic95_haut": hi,
                   "ic95_cluster_bas": clo, "ic95_cluster_haut": chi, "p_binom": binom_p(k, n),
                   "part_up_prédit": float(np.mean(pr[ok])) if n else math.nan}
            f = feat_of.get(sname)
            if f is not None:
                x = D[f].to_numpy(dtype=float)[pos]
                a, _, _ = auc_hanley(y, x if sname.endswith("momentum") else -x)
                row["auc_variable"] = a
                row["part_variable_nulle"] = float(np.mean(x[np.isfinite(x)] == 0))
            elif sname == "market_pre":
                row["auc_variable"] = auc_hanley(y, D["p_pre"].to_numpy(dtype=float)[pos])[0]
            row["description"] = desc
            rows.append(row)
    return pd.DataFrame(rows)


def pnl_rows(D: pd.DataFrame, pred: np.ndarray, fee_by_slug: dict) -> pd.DataFrame:
    """P&L par marché : achat de SHARES parts du côté prédit au prix ask ≈ milieu + 0,005, frais preneur."""
    p = D["p_pre"].to_numpy(dtype=float)
    y = D["y"].to_numpy(dtype=float)
    ok = np.isfinite(pred) & np.isfinite(p) & np.isfinite(y)
    side_up = pred == 1.0
    mid = np.where(side_up, p, 1 - p)
    ask = np.clip(mid + ASK_MARKUP, 0.001, 0.999)
    fees = np.array([
        pm.taker_fee(a, SHARES, fee_by_slug.get(s)) if o else math.nan
        for a, s, o in zip(ask, D["slug"], ok)
    ])
    win = np.where(side_up, y == 1, y == 0).astype(float)
    pnl = SHARES * (win - ask) - fees
    return pd.DataFrame({
        "slug": D["slug"].to_numpy(), "ok": ok, "side_up": side_up, "ask": ask, "fee": fees, "win": win,
        "pnl": np.where(ok, pnl, np.nan), "cost": SHARES * ask + fees, "cluster": D["start_ts"].to_numpy() // 900,
        "asset": D["asset"].to_numpy(), "duration": D["duration"].to_numpy(), "start_ts": D["start_ts"].to_numpy(),
    })


def pnl_summary(P: pd.DataFrame, label: str, strategy: str, sample: str) -> dict:
    Q = P[P["ok"]]
    n = len(Q)
    if n == 0:
        return {"stratégie": strategy, "échantillon": sample, "cellule": label, "n_trades": 0}
    per_share = Q["pnl"].to_numpy() / SHARES
    m, lo, hi = cluster_mean_ci(per_share, Q["cluster"].to_numpy())
    sd = per_share.std(ddof=1) if n > 1 else math.nan
    return {
        "stratégie": strategy, "échantillon": sample, "cellule": label, "n_trades": n,
        "taux_gain": float(Q["win"].mean()), "ask_moyen": float(Q["ask"].mean()),
        "frais_moyens_par_part": float((Q["fee"] / SHARES).mean()),
        "seuil_rentabilité": float(((Q["ask"] * SHARES + Q["fee"]) / SHARES).mean()),
        "pnl_moyen_par_part": m, "ic95_bas": lo, "ic95_haut": hi,
        "t_stat_iid": m / (sd / math.sqrt(n)) if n > 1 and sd > 0 else math.nan,
        f"pnl_total_usd_{SHARES}_parts": float(Q["pnl"].sum()), "capital_engagé_usd": float(Q["cost"].sum()),
        "roi": float(Q["pnl"].sum() / Q["cost"].sum()),
    }


def fetch_trade_windows(client_tr: pm.PolymarketClient, sub: pd.DataFrame, workers: int, use_cache: bool) -> pd.DataFrame:
    """Prix payés par les preneurs dans les fenêtres TRADE_WINDOWS (sous-échantillon).

    Chaque transaction preneuse donne une exposition : BUY X au prix p -> côté X payé p ;
    SELL X au prix p -> côté opposé payé 1 − p (carnets complémentaires). On calcule le VWAP
    du coût par côté (Up / Down). Cache : data/cache/diag/trade_windows.parquet (marchés résolus).
    """
    path = DIAG_CACHE / "trade_windows.parquet"
    cached = pd.read_parquet(path) if (use_cache and path.exists()) else pd.DataFrame(columns=["slug"])
    have = set(cached["slug"])
    todo = sub[~sub["slug"].isin(have)]

    def one(args) -> dict:
        slug, cond, S = args
        try:
            tr = client_tr.trades(cond, limit=50_000)
        except pm.PolymarketError as exc:
            log.warning("trades %s : %s", slug, exc)
            return {"slug": slug, "trades_error": True}
        row: dict = {"slug": slug, "n_trades": len(tr), "trades_error": False}
        if tr.empty:
            return row
        t = to_unix(tr["time"]) - int(S)
        buy = tr["side"].astype(str).str.upper().eq("BUY").to_numpy()
        up = tr["outcome"].astype(str).str.lower().eq("up").to_numpy()
        price, size = tr["price"].to_numpy(dtype=float), tr["size"].to_numpy(dtype=float)
        exp_up = np.where(buy, up, ~up)
        cost = np.where(buy, price, 1 - price)
        row["first_trade_dt_s"] = float(t.min())
        for w, (a, b) in TRADE_WINDOWS.items():
            m = (t >= a) & (t < b)
            row[f"{w}_n"] = int(m.sum())
            row[f"{w}_usd"] = float((price[m] * size[m]).sum())
            for side, sel in (("up", exp_up), ("down", ~exp_up)):
                mm = m & sel
                row[f"{w}_{side}_n"] = int(mm.sum())
                row[f"{w}_{side}_vwap"] = float((cost[mm] * size[mm]).sum() / size[mm].sum()) if mm.any() else math.nan
        return row

    rows = []
    if len(todo):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(one, zip(todo["slug"], todo["condition_id"], todo["start_ts"])))
    new = pd.DataFrame(rows)
    out = pd.concat([cached, new], ignore_index=True) if len(new) else cached
    if use_cache and len(new):
        good = out[~out.get("trades_error", pd.Series(False, index=out.index)).fillna(False).astype(bool)]
        pm._atomic_parquet(good.reset_index(drop=True), path)
    return out[out["slug"].isin(set(sub["slug"]))].drop_duplicates("slug", keep="last").reset_index(drop=True)


def execution_check(sub: pd.DataFrame, preds: dict, D: pd.DataFrame, checks: list[tuple[str, str]], fee_by_slug: dict) -> pd.DataFrame:
    """P&L d'une stratégie au prix RÉELLEMENT payé par les preneurs dans une fenêtre, contre l'hypothèse ask ≈ milieu + 0,005."""
    rows = []
    pos = D.index.get_indexer(sub.index)
    for strat, w in checks:
        pred = preds[strat][2][pos]
        y = sub["y"].to_numpy(dtype=float)
        side_up = pred == 1.0
        px = np.where(side_up, sub[f"{w}_up_vwap"].to_numpy(dtype=float), sub[f"{w}_down_vwap"].to_numpy(dtype=float))
        p_pre = sub["p_pre"].to_numpy(dtype=float)
        assumed = np.clip(np.where(side_up, p_pre, 1 - p_pre) + ASK_MARKUP, 0.001, 0.999)
        ok = np.isfinite(pred) & np.isfinite(y) & np.isfinite(px)
        win = np.where(side_up, y == 1, y == 0).astype(float)
        fee_px = np.array([pm.taker_fee(a, SHARES, fee_by_slug.get(s)) / SHARES if o else math.nan
                           for a, s, o in zip(px, sub["slug"], ok)])
        fee_as = np.array([pm.taker_fee(a, SHARES, fee_by_slug.get(s)) / SHARES if o else math.nan
                           for a, s, o in zip(assumed, sub["slug"], ok)])
        pnl_px = win - px - fee_px
        pnl_as = win - assumed - fee_as
        cl = sub["start_ts"].to_numpy() // 900
        base = np.isfinite(pred) & np.isfinite(y)
        dur = sub["duration"].to_numpy()
        groups = [("Tous", np.ones(len(sub), dtype=bool))] + [(f"Tous {d}", dur == d) for d in pd.unique(dur)]
        for name, gm in groups:
            m = ok & gm
            if m.sum() == 0:
                continue
            mp_, lo, hi = cluster_mean_ci(pnl_px[m], cl[m])
            ma, lo_a, hi_a = cluster_mean_ci(pnl_as[m], cl[m])
            rows.append({
                "stratégie": strat, "fenêtre_exécution": w, "cellule": name,
                "n_échantillon": int((base & gm).sum()), "n_avec_transaction": int(m.sum()),
                "taux_gain": float(win[m].mean()),
                "prix_payé_moyen": float(px[m].mean()), "ask_supposé_moyen": float(assumed[m].mean()),
                "pnl_par_part_prix_payé": mp_, "ic95_bas": lo, "ic95_haut": hi,
                "pnl_par_part_ask_supposé": ma, "ic95_bas_supposé": lo_a, "ic95_haut_supposé": hi_a,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------
def cell_pivot(df: pd.DataFrame, row_key: str, value: str, order_cells: list[str], fmt=pct, n_col: str | None = None) -> pd.DataFrame:
    piv = df.pivot_table(index=row_key, columns="cellule", values=value, aggfunc="first", sort=False)
    piv = piv[[c for c in order_cells if c in piv.columns]]
    out = piv.reset_index()
    for c in piv.columns:
        out[c] = out[c].map(fmt)
    return out


def gap_exec_sentence(e: pd.Series) -> str:
    """Phrase conditionnelle (selon l'IC) sur l'exécution de l'écart spot − TWAP entre S et S+5 s."""
    base = (f"Sur le sous-échantillon de transactions, les preneurs ont payé en moyenne **{fr(e['prix_payé_moyen'], 3)}** "
            f"pour ce côté entre S et S+5 s (contre {fr(e['ask_supposé_moyen'], 3)} supposé à S−30 s), pour un taux de gain de "
            f"{pct(e['taux_gain'])}. Le P&L au prix payé est de {fr(100 * e['pnl_par_part_prix_payé'], 2)} c par part (IC "
            f"{fr(100 * e['ic95_bas'], 2)} – {fr(100 * e['ic95_haut'], 2)} c, n = {fr(int(e['n_avec_transaction']))}) : ")
    if e["ic95_bas"] > 0:
        return base + ("il reste **positif et significatif** sur cet échantillon. C'est à confirmer hors échantillon, avec "
                       "notre latence réelle et la profondeur du carnet, car le prix payé par d'autres preneurs est optimiste (§ 6 bis).")
    if e["ic95_haut"] < 0:
        return base + "l'avantage est **plus qu'absorbé** par le prix payé (§ 6 bis)."
    return base + ("il n'est **pas significativement différent de zéro**. Une bonne partie de l'avantage est payée dès "
                   "l'ouverture (§ 6 bis).")


def write_report(ctx: dict, out_dir: Path) -> Path:
    D = ctx["D"]
    br, pa, lv, lag, mp, cal, nv, bl, pnl_all, pnl_best, checks, rt = (
        ctx[k] for k in ("base", "proxy", "level", "lag", "market", "calib", "nowcast", "baselines", "pnl_all",
                         "pnl_best", "checks", "runtime"))
    order = [cell_name(a, d) for d in ctx["durations"] for a in ctx["assets"]] + [f"Tous {d}" for d in ctx["durations"]] + ["Tous"]
    L: list[str] = []
    w = L.append
    period = f"du {ctx['start']:%d/%m/%Y %H:%M} au {ctx['end']:%d/%m/%Y %H:%M} UTC"
    n_res = int(D["y"].notna().sum())

    best_proxy = ctx["best_proxy"]
    best_1m = ctx["best_proxy_1m"]
    best_strat = ctx["best_strategy"]
    tot = pa[(pa["cellule"] == "Tous")].set_index("proxy")
    br_all = br.set_index("cellule")

    w("# Diagnostic empirique — marchés Polymarket crypto « Up or Down » (5 min, 15 min)")
    w("")
    w(f"*Généré le {pd.Timestamp.now(tz='UTC'):%d/%m/%Y %H:%M} UTC par `scripts/polymarket_diagnostic.py` "
      f"(temps d'exécution total : {fr(ctx['total_runtime'], 0)} s). Période : {period} "
      f"({ctx['days']} jours complets) ; actifs : {', '.join(a.upper() for a in ctx['assets'])} ; "
      f"durées : {', '.join(ctx['durations'])}.*")
    w("")
    w("> Lecture seule de données publiques. Aucun ordre, aucune clé. Depuis la France, Polymarket est "
      "en « close-only » et ces marchés sont `restricted` : ce diagnostic sert uniquement à évaluer "
      "nos prévisions contre un marché (simulation papier).")
    w("")
    # ---------------------------------------------------------------- résumé
    w("## 0. Résumé")
    w("")
    a_all = br_all.loc["Tous"]
    w(f"* **{fr(n_res)} marchés résolus** analysés ({fr(int(a_all['créneaux attendus']))} créneaux attendus). "
      f"Taux de « Up » global : **{pct(a_all['taux_up'])}** (IC 95 % {pct(a_all['ic95_bas'])} – {pct(a_all['ic95_haut'])}). "
      f"Égalités Chainlink (`finalPrice == priceToBeat`) : **{fr(int(a_all['égalités_chainlink']))}**."
      + (lambda r: f" IC robuste (créneaux de 15 min, actifs corrélés) : {pct(r['ic95_cluster_bas'])} – "
                   f"{pct(r['ic95_cluster_haut'])}"
                   + (" : pas de biais haussier démontré." if r["ic95_cluster_bas"] <= 0.5 else " : léger biais haussier.")
         )(bl[(bl["cellule"] == "Tous") & (bl["stratégie"] == "always_up")].iloc[0]))
    w(f"* Contrôle de la règle : l'issue officielle est égale à `finalPrice >= priceToBeat` dans "
      f"{fr(checks['rule_ok'])} / {fr(checks['rule_n'])} cas, et le `finalPrice` d'une fenêtre est égal au "
      f"`priceToBeat` de la suivante dans {fr(checks['chain_ok'])} / {fr(checks['chain_n'])} cas. "
      f"L'issue compare donc **TWAP60(fin) à TWAP60(début)**, où TWAP60(t) est la moyenne sur (t−60 s, t] : "
      "on compare la minute qui PRÉCÈDE l'ouverture, et non la première minute de la fenêtre.")
    bp = tot.loc[best_proxy]
    b1 = tot.loc[best_1m]
    a_ = tot.loc["a_close_vs_open_1m"]
    w(f"* **Meilleur proxy Binance** : `{best_proxy}` ({bp['description']}) : accord de **{pct(bp['accord'])}** "
      f"(IC {pct(bp['ic95_bas'])} – {pct(bp['ic95_haut'])}, n = {fr(int(bp['n']))}). "
      f"Avec des bougies 1m seulement : `{best_1m}`, **{pct(b1['accord'])}**. La comparaison close/open 1m "
      f"(a) n'atteint que {pct(a_['accord'])}."
      + (f" Exploratoire : en décalant la fenêtre Binance de 4 s vers le passé, l'erreur sur la variation baisse "
         f"(RMSE {fr(lag[lag['décalage_s'] == -4]['rmse_variation_pb'].mean(), 2)} pb contre "
         f"{fr(lag[lag['décalage_s'] == 0]['rmse_variation_pb'].mean(), 2)} pb) et l'accord passe à "
         f"{pct(tot.loc['x_twap60_1s_lag4', 'accord'])}. Le flux Chainlink semble donc en retard d'environ 4 s sur Binance "
         "(décalage choisi sur ces données)." if "x_twap60_1s_lag4" in tot.index and len(lag) else ""))
    pre = mp[(mp["cellule"] == "Tous") & (mp["instant"].str.startswith("S−30"))]
    if len(pre):
        r = pre.iloc[0]
        head = ("le marché n'a pas d'avis mesurable" if r["auc_ic_bas"] <= 0.5 <= r["auc_ic_haut"]
                else "le marché a un pouvoir prédictif faible mais mesurable")
        w(f"* **Avant l'ouverture (S−30 s), {head}** : prix moyen du jeton Up {fr(r['prix_moyen'], 3)} "
          f"(écart-type {fr(r['prix_sd'], 3)}), {pct(r['part_0.45_0.55'])} des prix dans [0,45 ; 0,55], "
          f"{pct(r['part_0.505'])} exactement à 0,505 ; Brier {fr(r['brier'], 4)} (pièce : 0,25) ; "
          f"AUC {fr(r['auc'], 3)} (IC {fr(r['auc_ic_bas'], 3)} – {fr(r['auc_ic_haut'], 3)}).")
    conv = mp[(mp["cellule"].isin([f"Tous {d}" for d in ctx["durations"]])) & (mp["instant"].str.startswith("S+"))]
    if len(conv):
        parts = [f"{r['cellule'].replace('Tous ', '')} {r['instant']} : {pct(r['accuracy'])} (Brier {fr(r['brier'], 3)})"
                 for _, r in conv.iterrows()]
        w("* **Une fois la fenêtre ouverte, le prix converge vite** (justesse du signe p ≥ 0,5) : " + " ; ".join(parts) + ".")
    nl = ctx.get("nowcast_lag")
    n1 = nv[(nv["cellule"] == "Tous") & (nv["instant"] == "S+1 min")] if len(nv) else nv
    if len(n1) and nl is not None and len(nl):
        r1 = n1.iloc[0]
        z = nl[(nl["durée"] == ctx["durations"][0]) & (nl["instant"] == "S+2 min")]
        txt = (f"* **Nowcast Binance contre prix du marché au même horodatage** : le nowcast sans paramètre fait mieux que le "
               f"point `prices-history` (S+1 min, toutes cellules : justesse {pct(r1['acc_nowcast'])} contre "
               f"{pct(r1['acc_marché'])}, ΔBrier {fr(r1['delta_brier'], 4)}, IC {fr(r1['delta_brier_ic_bas'], 4)} ; "
               f"{fr(r1['delta_brier_ic_haut'], 4)}).")
        if len(z):
            zb = z.loc[z["corr_marché_nowcast"].idxmax()]
            z0 = z[z["décalage_s"] == 0].iloc[0]
            txt += (f" Sur le {ctx['durations'][0]} à S+2 min, la corrélation avec le marché est maximale quand le nowcast est "
                    f"calculé {int(zb['décalage_s'])} s avant l'horodatage du point ({fr(zb['corr_marché_nowcast'], 3)} contre "
                    f"{fr(z0['corr_marché_nowcast'], 3)} à 0 s). ")
            if zb["décalage_s"] > 0:
                zd = zb["delta_brier"]
                txt += (f"À ce décalage, ΔBrier vaut {fr(zd, 4)} (IC {fr(zb['delta_brier_ic_bas'], 4)} ; "
                        f"{fr(zb['delta_brier_ic_haut'], 4)}). L'avance du nowcast équivaut donc à environ "
                        f"{int(zb['décalage_s'])} s d'information. Deux explications sont possibles, et on ne peut pas "
                        "les départager ici : (i) le point `prices-history` reflète un état du carnet plus ancien que son "
                        "horodatage ; (ii) le milieu de fourchette réagit avec ce retard sur Binance. Seul un enregistrement "
                        "du carnet en direct (WebSocket) permettrait de trancher. Même dans le cas (ii), il faudrait payer "
                        "l'ask réel, et le § 6 bis montre que le côté informé se paie vite.")
            else:
                txt += "Aucun retard apparent du point : l'avance du nowcast est à confirmer avec le carnet enregistré en direct."
        w(txt)
    bs = bl[(bl["cellule"] == "Tous") & (bl["stratégie"] == best_strat)].iloc[0]
    w(f"* **Meilleure baseline pré-ouverture « propre »** (information ≤ S−30 s, choisie in-sample) : "
      f"`{best_strat}`, justesse {pct(bs['accuracy'])} (IC robuste {pct(bs['ic95_cluster_bas'])} – "
      f"{pct(bs['ic95_cluster_haut'])}, n = {fr(int(bs['n']))}).")
    opt = bl[(bl["cellule"] == "Tous") & (bl["stratégie"] == "gap_m0_1s:momentum")]
    if len(opt):
        o = opt.iloc[0]
        txt = (f"* À titre de comparaison, l'écart spot − TWAP60 **à S** (`gap_m0_1s`, information jusqu'à S : non "
               f"exécutable au prix de S−30 s) atteint {pct(o['accuracy'])} de justesse (IC robuste "
               f"{pct(o['ic95_cluster_bas'])} – {pct(o['ic95_cluster_haut'])}) : c'est l'information « mécanique » créée par la règle TWAP.")
        ex = ctx.get("exec")
        if ex is not None and len(ex):
            e = ex[(ex["stratégie"] == "gap_m0_1s:momentum") & (ex["cellule"] == "Tous")]
            if len(e):
                e = e.iloc[0]
                txt += " " + gap_exec_sentence(e)
        w(txt)
    pb = pnl_best[(pnl_best["échantillon"] == "in-sample") & (pnl_best["cellule"] == "Tous")]
    if len(pb):
        r = pb.iloc[0]
        w(f"* **P&L théorique** (achat preneur à S−30 s au prix milieu + 0,005, frais `crypto_fees_v2` inclus) "
          f"de `{best_strat}` : **{fr(100 * r['pnl_moyen_par_part'], 2)} c par part** (IC robuste "
          f"{fr(100 * r['ic95_bas'], 2)} – {fr(100 * r['ic95_haut'], 2)} c), pour un seuil de rentabilité de "
          f"{pct(r['seuil_rentabilité'])} de réussite contre {pct(r['taux_gain'])} obtenu. ROI {pct(r['roi'], 2)}. "
          + ("L'IC contient zéro : **non significatif**. " if r["ic95_bas"] <= 0 <= r["ic95_haut"] else
             ("L'IC exclut zéro. " if r["ic95_bas"] > 0 else "L'IC est entièrement négatif. "))
          + "Résultat **in-sample** : la stratégie a été choisie sur ces mêmes données.")
    ex0 = ctx.get("exec")
    if ex0 is not None and len(ex0):
        e = ex0[(ex0["stratégie"] == best_strat) & (ex0["fenêtre_exécution"] == "pre30") & (ex0["cellule"] == "Tous")]
        if len(e):
            e = e.iloc[0]
            w(f"* **Hypothèse d'exécution** : dans [S−30 s, S), les preneurs du côté choisi par `{best_strat}` ont réellement "
              f"payé {fr(e['prix_payé_moyen'], 3)} en moyenne, contre {fr(e['ask_supposé_moyen'], 3)} supposé (milieu + 0,005). "
              f"L'hypothèse est donc optimiste d'environ {fr(100 * (e['prix_payé_moyen'] - e['ask_supposé_moyen']), 1)} c par part. "
              f"Au prix payé, le P&L est de {fr(100 * e['pnl_par_part_prix_payé'], 2)} c par part (IC {fr(100 * e['ic95_bas'], 2)} – "
              f"{fr(100 * e['ic95_haut'], 2)} c, n = {fr(int(e['n_avec_transaction']))}).")
    oos = pnl_best[(pnl_best["échantillon"].str.startswith("2e moitié")) & (pnl_best["cellule"] == "Tous")]
    if len(oos):
        r = oos.iloc[0]
        w(f"* Contrôle pseudo hors échantillon (choix sur la 1re moitié de la période, test sur la 2e) : "
          f"`{r['stratégie']}`, {fr(100 * r['pnl_moyen_par_part'], 2)} c par part (IC {fr(100 * r['ic95_bas'], 2)} – "
          f"{fr(100 * r['ic95_haut'], 2)} c, n = {fr(int(r['n_trades']))}).")
    w("")
    # ---------------------------------------------------------------- données
    w("## 1. Données et méthode")
    w("")
    w(f"* **Marchés** : `PolymarketClient.list_updown_markets` (slugs déterministes `{{asset}}-updown-{{5m|15m}}-{{début}}`), "
      f"issue = `outcomePrices` (`resolved_up`). Niveaux Chainlink : `event.eventMetadata.priceToBeat` / `finalPrice` "
      "(lus directement sur `gamma-api /events`, car `UpDownMarket` ne les conserve pas).")
    w(f"* **Prix du jeton Up** : `PolymarketClient.prices_history(token_up, S−{PRICE_LOOKBACK_S} s, E+60 s, fidelity=1)`. "
      "C'est le **milieu de fourchette**, échantillonné environ une fois par minute à des secondes irrégulières. "
      f"Prix « avant l'ouverture » = dernier point ≤ S−{PRE_OFFSET_S} s (ancienneté ≤ 5 min) ; prix à S+k min = dernier point "
      "≤ S+k min (ancienneté ≤ 90 s). Contrôle : ces prix sont identiques à ceux de `pm.up_price_at` "
      f"({fr(checks['up_price_at_mismatch'])} écart).")
    w("* **Binance** (spot USDT) : zips journaliers `data.binance.vision` en **1m et 1s** (repli automatique sur "
      "`data-api.binance.vision/api/v3/klines`). Le 1s est utilisé **sur tout l'échantillon**, pas seulement sur un "
      f"sous-échantillon. Secondes manquantes comblées : {ctx['missing_1s']}.")
    w("* **Causalité des baselines** : trois coupures d'information. `S−60s (1m)` = bougies 1m closes au plus tard à S−60 s ; "
      "`S−30s (1s)` = bougies 1s closes au plus tard à S−30 s ; `S (…) *` = bougies closes au plus tard à S. Seules les deux "
      "premières sont comparables au prix du marché pris à S−30 s, et elles seules servent au P&L. Dans les 30 s qui "
      "précèdent l'ouverture, les preneurs font déjà bouger le prix (§ 6 bis).")
    w("* **Intervalles** : Wilson à 95 % par cellule. « IC robuste » = IC d'une moyenne en groupant par créneau de 15 min, "
      "car BTC, ETH et SOL, ainsi que les marchés 5m et 15m d'un même créneau, sont corrélés. Les AUC ont un IC de Hanley-McNeil.")
    w("")
    w("### Contrôles de cohérence")
    w("")
    ck = pd.DataFrame([
        ("créneaux attendus / marchés trouvés / résolus", f"{fr(checks['expected'])} / {fr(checks['found'])} / {fr(checks['resolved'])}"),
        ("issue officielle == (finalPrice >= priceToBeat)", f"{fr(checks['rule_ok'])} / {fr(checks['rule_n'])}"),
        ("finalPrice(n) == priceToBeat(n+1) (fenêtres consécutives)", f"{fr(checks['chain_ok'])} / {fr(checks['chain_n'])}"),
        ("priceToBeat 5m == priceToBeat 15m au même début", f"{fr(checks['same_ptb_ok'])} / {fr(checks['same_ptb_n'])}"),
        ("règle de résolution (client) / fenêtre TWAP", checks["rules"]),
        ("barème de frais (fee_type, rate, exposant)", checks["fees"]),
        ("marchés résolus sans `finalPrice` (exclus des comparaisons Chainlink)", checks["no_final"]),
        ("marchés sans historique de prix / erreurs", f"{fr(checks['no_history'])} / {fr(checks['errors'])}"),
        ("marchés avec prix à S−30 s", f"{fr(checks['with_pre'])}"),
        ("ancienneté médiane du point S−30 s (s)", fr(checks["pre_age_median"], 0)),
        ("`restricted` = true", f"{fr(checks['restricted'])} / {fr(checks['found'])}"),
    ], columns=["contrôle", "résultat"])
    w(md_table(ck))
    w("")
    # ---------------------------------------------------------------- 1. base rate
    w("## 2. Taux de « Up » (base rate) et égalités")
    w("")
    t = br[["cellule", "résolus", "up", "taux_up", "ic95_bas", "ic95_haut", "p_binom", "égalités_chainlink",
            "mouvement_médian_pb", "volume_médian_usd", "volume_total_usd"]].copy()
    t = t.set_index("cellule").loc[[c for c in order if c in set(t["cellule"])]].reset_index()
    w(md_table(t, {"taux_up": pct, "ic95_bas": pct, "ic95_haut": pct, "p_binom": lambda x: fr(x, 3),
                   "mouvement_médian_pb": lambda x: fr(x, 1), "volume_médian_usd": lambda x: fr(x, 0),
                   "volume_total_usd": lambda x: fr(x, 0)}))
    w("")
    w("*`mouvement_médian_pb` : |log(finalPrice / priceToBeat)| médian en points de base. Les égalités Chainlink "
      "sont comptées à l'égalité exacte des flottants ; elles iraient à « Up ».*")
    w("")
    # ---------------------------------------------------------------- 2. proxies
    w("## 3. Proxies Binance de la résolution officielle")
    w("")
    w("Pour chaque proxy, on prédit « Up » si niveau(fin) ≥ niveau(début), puis on mesure le taux d'accord avec l'issue officielle.")
    w("")
    pv = cell_pivot(pa, "proxy", "accord", order)
    desc = pa.drop_duplicates("proxy").set_index("proxy")["description"]
    pv.insert(1, "définition", pv["proxy"].map(desc))
    w(md_table(pv))
    w("")
    dis = pa[pa["cellule"] == "Tous"][["proxy", "n", "désaccords", "égalités_binance", "mouvement_cl_médian_désaccords_pb",
                                        "mouvement_cl_médian_tous_pb"]]
    w("Désaccords (toutes cellules) : taille du mouvement Chainlink dans les cas de désaccord.")
    w("")
    w(md_table(dis, {"mouvement_cl_médian_désaccords_pb": lambda x: fr(x, 2), "mouvement_cl_médian_tous_pb": lambda x: fr(x, 2)}))
    w("")
    w("### Quel niveau Binance colle aux niveaux Chainlink ?")
    w("")
    w("Écart de niveau log(Binance / Chainlink), en pb, sur les points S (priceToBeat) et E (finalPrice). "
      "`rmse_variation_pb` mesure l'erreur sur la variation fin − début, qui seule compte pour l'issue.")
    w("")
    lt = lv[lv["cellule"].isin([cell_name(a, ctx["durations"][0]) for a in ctx["assets"]] + ["Tous"])]
    lt = lt[["cellule", "niveau", "n", "écart_moyen_pb", "écart_sd_pb", "rmse_variation_pb", "corr_variation", "accord_signe"]]
    w(md_table(lt, {"écart_moyen_pb": lambda x: fr(x, 2), "écart_sd_pb": lambda x: fr(x, 2),
                    "rmse_variation_pb": lambda x: fr(x, 2), "corr_variation": lambda x: fr(x, 4), "accord_signe": pct}))
    w("")
    if len(lag):
        best_lag = lag.loc[lag.groupby("actif")["rmse_variation_pb"].idxmin()]
        w("Décalage temporel : on déplace la fenêtre TWAP60 Binance de −10 à +10 s. Le meilleur décalage par actif "
          "(RMSE de la variation minimale) est le suivant (détail dans `level_lag_scan.csv`) :")
        w("")
        z = lag[lag["décalage_s"] == 0][["actif", "rmse_variation_pb", "accord_issue"]].rename(
            columns={"rmse_variation_pb": "rmse_décalage_0", "accord_issue": "accord_décalage_0"})
        z = best_lag[["actif", "décalage_s", "rmse_variation_pb", "accord_issue"]].merge(z, on="actif")
        w(md_table(z, {"rmse_variation_pb": lambda x: fr(x, 2), "rmse_décalage_0": lambda x: fr(x, 2),
                       "accord_issue": pct, "accord_décalage_0": pct}))
        w("")
    # ---------------------------------------------------------------- 3. prix du marché
    w("## 4. Le prix du marché : avant l'ouverture et pendant la fenêtre")
    w("")
    mt = mp[mp["cellule"].isin(order)].copy()
    mt["o"] = mt["cellule"].map({c: i for i, c in enumerate(order)})
    mt = mt.sort_values(["o"], kind="stable")
    cols = ["cellule", "instant", "n", "prix_moyen", "prix_sd", "part_0.45_0.55", "écart_moyen_à_0.5", "part_décidé_0.1_0.9",
            "accuracy", "brier", "auc", "auc_ic_bas", "auc_ic_haut"]
    w(md_table(mt[cols], {"prix_moyen": lambda x: fr(x, 3), "prix_sd": lambda x: fr(x, 3), "part_0.45_0.55": pct,
                          "écart_moyen_à_0.5": lambda x: fr(x, 3), "part_décidé_0.1_0.9": pct, "accuracy": pct,
                          "brier": lambda x: fr(x, 4), "auc": lambda x: fr(x, 3), "auc_ic_bas": lambda x: fr(x, 3),
                          "auc_ic_haut": lambda x: fr(x, 3)}))
    w("")
    w("*`part_décidé_0.1_0.9` : part des prix ≤ 0,1 ou ≥ 0,9. `S` = dernier point ≤ S (ancienneté ≤ 90 s). "
      "Pour le 5m, « S+5 min » tombe à la clôture de la fenêtre, et le prix est alors presque certain.*")
    w("")
    w("### Calibration par déciles (toutes cellules d'une même durée)")
    w("")
    ct = cal[cal["instant"].str.startswith("S+")].copy()
    ct["txt"] = ct.apply(lambda r: f"{fr(r['freq_up'], 2)} ({fr(int(r['n']))})", axis=1)
    piv = ct.pivot_table(index="décile", columns=["cellule", "instant"], values="txt", aggfunc="first")
    col_order = [(f"Tous {d}", f"S+{k} min") for d in ctx["durations"] for k in HORIZONS_MIN[d]]
    piv = piv[[c for c in col_order if c in piv.columns]]
    piv.columns = [f"{c[0].replace('Tous ', '')} {c[1]}" for c in piv.columns]
    piv = piv.reset_index()
    w("Fréquence réalisée de « Up » par décile de prix (effectif entre parenthèses) :")
    w("")
    w(md_table(piv.fillna("—")))
    w("")
    w("### Le marché contre un nowcast Binance au même instant")
    w("")
    w("Au moment précis de chaque point de prix (horodatage du point, donc même information), on calcule "
      "P(Up) = Φ((log m − log TWAP60_Binance(S)) / σ√·). Ici m est le prix spot Binance, corrigé de la partie "
      "déjà connue du TWAP final, et σ la volatilité 1m de l'heure précédente. Ce modèle utilise Binance seul, "
      "sans paramètre ajusté.")
    w("")
    nt = nv[nv["cellule"].str.startswith("Tous")] if len(nv) else nv
    if len(nt):
        nt = nt.copy()
        nt["ΔBrier (IC robuste)"] = nt.apply(lambda r: f"{fr(r['delta_brier'], 4)} ({fr(r['delta_brier_ic_bas'], 4)} ; "
                                                      f"{fr(r['delta_brier_ic_haut'], 4)})", axis=1)
        w(md_table(nt[["cellule", "instant", "n", "âge_moyen_point_s", "acc_marché", "acc_nowcast", "brier_marché",
                       "brier_nowcast", "ΔBrier (IC robuste)", "auc_marché", "auc_nowcast", "corr_marché_nowcast"]],
                   {"âge_moyen_point_s": lambda x: fr(x, 0), "acc_marché": pct, "acc_nowcast": pct,
                    "brier_marché": lambda x: fr(x, 4), "brier_nowcast": lambda x: fr(x, 4), "auc_marché": lambda x: fr(x, 3),
                    "auc_nowcast": lambda x: fr(x, 3), "corr_marché_nowcast": lambda x: fr(x, 3)}))
        w("")
        w("*ΔBrier = Brier(nowcast) − Brier(marché), apparié marché par marché ; négatif = le nowcast Binance fait mieux. "
          "`âge_moyen_point_s` : S+k min − horodatage du point. Le prix `prices-history` est un milieu de fourchette, "
          "pas un prix exécutable. Un nowcast meilleur que le marché au même instant ne prouve donc pas qu'un gain "
          "soit exploitable.*")
        w("")
        nl = ctx.get("nowcast_lag")
        if nl is not None and len(nl):
            w("**Robustesse : le point `prices-history` est-il en retard sur son horodatage ?** On recalcule le nowcast "
              "Δ secondes AVANT l'horodatage du point, sur les mêmes marchés. Si le nowcast décalé rejoint le marché "
              "(ΔBrier ≈ 0, corrélation maximale), le point reflète un état antérieur à son horodatage, et l'avance du "
              "nowcast vient de là, pas d'une inefficience.")
            w("")
            t = nl.copy()
            t["ΔBrier (IC)"] = t.apply(lambda r: f"{fr(r['delta_brier'], 4)} ({fr(r['delta_brier_ic_bas'], 4)} ; "
                                                f"{fr(r['delta_brier_ic_haut'], 4)})", axis=1)
            t["cellule"] = t["durée"] + " " + t["instant"]
            t["décalage_s"] = t["décalage_s"].astype(int).astype(str)
            piv_d = t.pivot_table(index="décalage_s", columns="cellule", values="ΔBrier (IC)", aggfunc="first", sort=False)
            piv_c = t.pivot_table(index="décalage_s", columns="cellule", values="corr_marché_nowcast", aggfunc="first", sort=False)
            w("ΔBrier (nowcast décalé − marché), IC robuste :")
            w("")
            w(md_table(piv_d.reset_index().rename(columns={"décalage_s": "Δ (s)"})))
            w("")
            w("Corrélation marché / nowcast décalé :")
            w("")
            w(md_table(piv_c.reset_index().rename(columns={"décalage_s": "Δ (s)"}),
                       {c: (lambda x: fr(x, 3)) for c in piv_c.columns}))
            w("")
    # ---------------------------------------------------------------- 4. baselines
    w("## 5. Baselines Binance calculées AVANT l'ouverture")
    w("")
    w("Momentum : « Up » si le rendement (ou l'écart) est ≥ 0. Retournement : l'inverse. En cas d'égalité, on prédit « Up ». "
      "Les stratégies marquées `*` utilisent l'information jusqu'à S, donc 30 s de plus que le prix du marché retenu.")
    w("")
    bt = bl.copy()
    bt["label"] = bt["stratégie"] + " [" + bt["coupure"] + "]"
    pv = cell_pivot(bt, "label", "accuracy", order)
    tot_b = bt[bt["cellule"] == "Tous"].set_index("label")
    pv["IC robuste (Tous)"] = pv["label"].map(lambda s: f"{pct(tot_b.loc[s, 'ic95_cluster_bas'])} – {pct(tot_b.loc[s, 'ic95_cluster_haut'])}")
    pv["AUC (Tous)"] = pv["label"].map(lambda s: fr(tot_b.loc[s].get("auc_variable", math.nan), 3))
    w(md_table(pv))
    w("")
    w(f"*n par cellule : voir `baselines.csv`. Plus de {len(bt['stratégie'].unique())} règles sont testées sur 6 cellules : "
      "sans correction, quelques p < 0,05 sont attendus par hasard (tests multiples).*")
    w("")
    # ---------------------------------------------------------------- 5. P&L
    w("## 6. P&L théorique de la stratégie naïve (in-sample)")
    w("")
    w(f"Règle : à S−30 s, acheter {SHARES} parts du côté prédit (Up ou Down) au prix ask ≈ milieu + {ASK_MARKUP} "
      "(milieu = dernier point `prices-history` ≤ S−30 s ; Down = 1 − milieu). Les frais preneur sont calculés avec "
      "`pm.taker_fee(ask, parts, feeSchedule du marché)`, soit `crypto_fees_v2` : 0,07 × p × (1 − p) par part. "
      "On garde la position jusqu'à la résolution. Seules les stratégies dont l'information s'arrête au plus tard à "
      "S−30 s sont évaluées. Le carnet réel avant l'ouverture (≈ 0,50/0,51) limite la taille à quelques centaines de "
      "parts, et la profondeur n'est pas modélisée.")
    w("")
    pt = pnl_all[pnl_all["cellule"] == "Tous"].sort_values("pnl_moyen_par_part", ascending=False)
    cols = ["stratégie", "n_trades", "taux_gain", "seuil_rentabilité", "pnl_moyen_par_part", "ic95_bas", "ic95_haut", "roi"]
    cents = lambda x: fr(100 * x, 2) + " c"  # noqa: E731
    w(md_table(pt[cols], {"taux_gain": pct, "seuil_rentabilité": pct, "pnl_moyen_par_part": cents, "ic95_bas": cents,
                          "ic95_haut": cents, "roi": lambda x: pct(x, 2)}))
    w("")
    w(f"Détail par cellule pour la meilleure baseline (`{best_strat}`) :")
    w("")
    pb = pnl_best.copy()
    w(md_table(pb[["échantillon", "cellule", "n_trades", "taux_gain", "seuil_rentabilité", "pnl_moyen_par_part", "ic95_bas",
                   "ic95_haut", f"pnl_total_usd_{SHARES}_parts", "roi"]],
               {"taux_gain": pct, "seuil_rentabilité": pct, "pnl_moyen_par_part": cents, "ic95_bas": cents, "ic95_haut": cents,
                f"pnl_total_usd_{SHARES}_parts": lambda x: fr(x, 0) + " $", "roi": lambda x: pct(x, 2)}))
    w("")
    ex = ctx.get("exec")
    if ex is not None and len(ex):
        w("## 6 bis. Contrôle d'exécution avec les transactions réelles (sous-échantillon)")
        w("")
        w(f"Sous-échantillon aléatoire (graine fixe) de {fr(ctx['trades_sample'])} marchés par cellule, soit "
          f"{fr(ctx['n_sub'])} marchés. On lit toutes les transactions preneuses avec `PolymarketClient.trades`. Une "
          "transaction BUY X au prix p donne une exposition X payée p ; une transaction SELL X au prix p donne le côté "
          "opposé payé 1 − p. Le « prix payé » est le VWAP de ce coût, pour le côté choisi par la stratégie, dans la "
          "fenêtre d'exécution : `pre30` = [S−30 s, S), `open5` = [S, S+5 s]. Horodatage à la seconde (bloc). On compare "
          "au P&L calculé avec l'hypothèse ask ≈ milieu(S−30 s) + 0,005, sur les MÊMES marchés.")
        w("")
        w(md_table(ex[["stratégie", "fenêtre_exécution", "cellule", "n_échantillon", "n_avec_transaction", "taux_gain",
                       "prix_payé_moyen", "ask_supposé_moyen", "pnl_par_part_prix_payé", "ic95_bas", "ic95_haut",
                       "pnl_par_part_ask_supposé"]],
                   {"taux_gain": pct, "prix_payé_moyen": lambda x: fr(x, 3), "ask_supposé_moyen": lambda x: fr(x, 3),
                    "pnl_par_part_prix_payé": cents, "ic95_bas": cents, "ic95_haut": cents, "pnl_par_part_ask_supposé": cents}))
        w("")
        w("*Le prix payé par d'autres preneurs est un proxy optimiste de notre propre exécution : il ne compte ni la "
          "latence, ni la file d'attente, ni l'impact de notre ordre. Si aucune transaction n'a eu lieu du côté voulu "
          "dans la fenêtre, le marché est exclu (voir `n_avec_transaction`).*")
        w("")
    # ---------------------------------------------------------------- conclusions
    w("## 7. Conséquences pour nos backtests")
    w("")
    w(f"1. **Étiquette** : utiliser `{best_proxy}` (klines 1s), soit {pct(bp['accord'])} d'accord. Si l'on n'a que du 1m : "
      f"`{best_1m}` ({pct(b1['accord'])}). Il ne faut PAS utiliser close[t+h]/close[t], qui ne donne que "
      f"{pct(tot.loc['a2_close_vs_close_1m', 'accord'])} d'accord : c'est le label `r_h` actuel de `targets.py`."
      + (f" Variante exploratoire : fenêtre décalée de 4 s, soit {pct(tot.loc['x_twap60_1s_lag4', 'accord'])}, à valider "
         "sur une autre période." if "x_twap60_1s_lag4" in tot.index else ""))
    w("2. **Origine** : l'origine d'une prévision est le début de fenêtre S (multiple de 300 ou 900 s en UTC). La référence "
      "est TWAP60(S), une moyenne sur (S−60 s, S] : elle est fixée à S et en partie connue dès S−30 s.")
    pre_all = mp[(mp["cellule"] == "Tous") & mp["instant"].str.startswith("S−30")]
    lo_hi = (f"{fr(pre_all.iloc[0]['prix_moyen'], 3)} en moyenne, entre 0,45 et 0,55 dans "
             f"{pct(pre_all.iloc[0]['part_0.45_0.55'])} des cas") if len(pre_all) else "≈ 0,50"
    gap_shift = ""
    if ex is not None and len(ex):
        e = ex[(ex["stratégie"] == "gap_m0_1s:momentum") & (ex["cellule"] == "Tous")]
        if len(e):
            e = e.iloc[0]
            gap_shift = (f" En revanche, pour le côté indiqué par l'écart spot − TWAP à S, le prix payé passe de "
                         f"{fr(e['ask_supposé_moyen'], 3)} (ask supposé à S−30 s) à {fr(e['prix_payé_moyen'], 3)} "
                         "dès [S, S+5 s] (§ 6 bis).")
    w(f"3. **Comparaison au marché** : avant l'ouverture, le prix du marché est plat ({lo_hi}). Il suffit en théorie de "
      "dépasser le seuil ask + frais (≈ 52,75 % pour acheter Up à 0,51)." + gap_shift + " Il faut toujours comparer "
      "le modèle au prix **au même instant d'information**, et jamais un modèle qui a vu la bougie [S−1m, S) à un "
      "prix de S−30 s.")
    n5 = int(D[D["duration"] == "5m"]["y"].notna().sum()) if "5m" in ctx["durations"] else 0
    n_cell = int(D.groupby(["asset", "duration"])["y"].count().min())
    w(f"4. **Taille d'échantillon** : l'IC à 95 % d'une justesse autour de 50 % vaut ±{fr(100 * 1.96 * math.sqrt(0.25 / n_res), 1)} "
      f"points sur les {fr(n_res)} marchés, ±{fr(100 * 1.96 * math.sqrt(0.25 / max(n5, 1)), 1)} points sur les {fr(n5)} marchés 5m, "
      f"et ±{fr(100 * 1.96 * math.sqrt(0.25 / max(n_cell, 1)), 1)} points pour la plus petite cellule ({fr(n_cell)}). Les actifs "
      "étant corrélés, l'IC robuste est plus large. Pour démontrer un avantage de 2 à 3 points, il faut plusieurs semaines "
      "de données et une sélection de stratégie faite hors échantillon.")
    w("")
    # ---------------------------------------------------------------- limites
    w("## 8. Limites et remarques sur le client `tradebot.polymarket`")
    w("")
    for note in ctx["client_notes"]:
        w(f"* {note}")
    w("")
    # ---------------------------------------------------------------- runtime
    w("## 9. Temps d'exécution")
    w("")
    w("Caches disque **désactivés** (`--no-cache`) : exécution à froid, toutes les données ont été retéléchargées." if not ctx["use_cache"]
      else "Caches disque activés (`data/cache/polymarket`, `data/cache/diag`) : une exécution à froid est bien plus longue.")
    w("")
    w(md_table(rt, {"secondes": lambda x: fr(x, 1)}))
    w("")
    w("## 10. Fichiers")
    w("")
    for f, d in ctx["files_desc"]:
        w(f"* `reports/polymarket/{f}` : {d}")
    w("")
    w("Reproduire : `. .venv/bin/activate && python scripts/polymarket_diagnostic.py --end "
      f"{ctx['end']:%Y-%m-%d} --days {ctx['days']}` (les caches rendent la 2e exécution rapide).")
    path = out_dir / "diagnostic.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Diagnostic empirique des marchés Polymarket crypto Up/Down")
    p.add_argument("--days", type=int, default=7, help="nombre de jours complets (défaut 7)")
    p.add_argument("--end", default=None, help="fin EXCLUSIVE, date UTC AAAA-MM-JJ (défaut : aujourd'hui 00:00 UTC)")
    p.add_argument("--assets", default="btc,eth,sol")
    p.add_argument("--durations", default="5m,15m")
    p.add_argument("--max-rps", type=float, default=40.0, help="requêtes/s max vers Polymarket")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--out", default=str(OUT_DIR))
    p.add_argument("--trades-sample", type=int, default=100,
                   help="marchés par cellule pour le contrôle d'exécution par les transactions (0 = désactivé)")
    p.add_argument("--trades-rps", type=float, default=15.0, help="requêtes/s max vers data-api /trades")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-cache", action="store_true", help="désactive les caches disque")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    t_all = time.perf_counter()
    rt = Runtime()
    use_cache = not args.no_cache
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC").floor("D")
    start = end - pd.Timedelta(days=args.days)
    assets = [a.strip().lower() for a in args.assets.split(",") if a.strip()]
    durations = [d.strip() for d in args.durations.split(",") if d.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    DIAG_CACHE.mkdir(parents=True, exist_ok=True)
    log.info("période [%s, %s) — %s × %s", start, end, assets, durations)

    session = make_session()
    client = pm.PolymarketClient(session=session, max_rps=args.max_rps, use_cache=use_cache)

    # 1. marchés -----------------------------------------------------------
    with rt("1. liste des marchés (client.list_updown_markets)"):
        by_cell: dict[tuple[str, str], list[pm.UpDownMarket]] = {}
        expected: dict[tuple[str, str], int] = {}
        for a in assets:
            for d in durations:
                expected[(a, d)] = len(pm.slot_starts(d, start, end))
                by_cell[(a, d)] = client.list_updown_markets(a, d, start, end, max_workers=8)
                log.info("   %s %s : %d / %d marchés", a, d, len(by_cell[(a, d)]), expected[(a, d)])
    markets = [m for v in by_cell.values() for m in v]
    fee_by_slug = {m.slug: m.fee_schedule for m in markets}
    found_slugs = {m.slug for m in markets}
    missing_slugs = [pm.updown_slug(a, d, ts) for a in assets for d in durations
                     for ts in pm.slot_starts(d, start, end) if pm.updown_slug(a, d, ts) not in found_slugs]
    confirmed = []
    for sl in missing_slugs[:20]:   # contre-vérification directe sur gamma (/events et /markets)
        ev = client._get(f"{pm.GAMMA_URL}/events", {"slug": sl}) or []
        mk = client._get(f"{pm.GAMMA_URL}/markets", {"slug": sl}) or []
        if not ev and not mk:
            confirmed.append(sl)
    if missing_slugs:
        missing_note = (f"Créneaux absents : {len(missing_slugs)} ({', '.join(missing_slugs[:20])}). Contre-vérification "
                        f"directe sur gamma (`/events` et `/markets`) : {len(confirmed)} / {min(len(missing_slugs), 20)} "
                        "sont bien inconnus de Polymarket. Ce n'est donc pas un bug du client.")
    else:
        missing_note = "Aucun créneau absent sur la période."
    mf = pm.markets_to_frame(markets)

    # 2. métadonnées Chainlink ------------------------------------------------
    with rt("2. eventMetadata (priceToBeat / finalPrice)"):
        meta = fetch_event_meta(client, mf["slug"].tolist(), workers=8, use_cache=use_cache)
    mf = mf.merge(meta, on="slug", how="left")
    for c in ("price_to_beat", "final_price"):
        mf[c] = pd.to_numeric(mf[c], errors="coerce").astype("float64")

    # 3. prix du jeton Up ---------------------------------------------------------
    resolved = [m for m in markets if m.is_resolved and m.resolved_up is not None]
    with rt(f"3. historique de prix du jeton Up ({len(resolved)} marchés)"):
        prices, price_checks = fetch_price_points(client, resolved, workers=args.workers)
    mf = mf.merge(prices, on="slug", how="left")

    # 4. Binance ------------------------------------------------------------------
    with rt("4. klines Binance 1m + 1s (zips journaliers)"):
        b_start, b_end = start - pd.Timedelta(days=1), end
        bars1s, bars1m, missing = {}, {}, {}
        for a in assets:
            sym = pm.ASSET_TO_SYMBOL[a]
            bars1m[a] = Bars(load_binance(session, sym, "1m", b_start, b_end, use_cache), 60)
            bars1s[a] = Bars(load_binance(session, sym, "1s", b_start, b_end, use_cache), 1)
            missing[a] = (bars1s[a].n_missing, bars1m[a].n_missing)
            log.info("   %s : 1s %d barres (%d manquantes), 1m %d barres (%d manquantes)", sym, bars1s[a].n,
                     bars1s[a].n_missing, bars1m[a].n, bars1m[a].n_missing)

    # 5. table par marché -----------------------------------------------------------
    with rt("5. variables et analyses"):
        D = mf[mf["closed"] & mf["resolved_up"].notna()].copy().reset_index(drop=True)
        D["y"] = D["up_final"].astype(float)
        D["start_ts"], D["end_ts"] = to_unix(D["start"]), to_unix(D["end"])
        D["cl_up"] = (D["final_price"] >= D["price_to_beat"]).where(np.isfinite(D["final_price"]))
        D["tie_cl"] = (D["final_price"] == D["price_to_beat"]) & np.isfinite(D["final_price"])
        D["cl_move_bps"] = 1e4 * np.log(D["final_price"] / D["price_to_beat"])
        feats = []
        for (a, d), g in D.groupby(["asset", "duration"], sort=False):
            feats.append(binance_features(g, bars1s[a], bars1m[a], pm.DURATION_SECONDS[d]))
        D = D.join(pd.concat(feats))

        # contrôles
        chk = dict(price_checks)
        ok = np.isfinite(D["final_price"]) & np.isfinite(D["price_to_beat"])
        chk["rule_n"] = int(ok.sum())
        chk["rule_ok"] = int((D.loc[ok, "cl_up"].astype(bool) == D.loc[ok, "y"].astype(bool)).sum())
        chain_ok = chain_n = 0
        for _, g in D.sort_values("start_ts").groupby(["asset", "duration"]):
            g = g.sort_values("start_ts")
            nxt = g["start_ts"].shift(-1) == g["end_ts"]
            fp, ptb = g["final_price"].to_numpy(), g["price_to_beat"].shift(-1).to_numpy()
            m = nxt.to_numpy() & np.isfinite(fp) & np.isfinite(ptb)
            chain_n += int(m.sum())
            chain_ok += int((fp[m] == ptb[m]).sum())
        chk["chain_ok"], chk["chain_n"] = chain_ok, chain_n
        same_ok = same_n = 0
        if {"5m", "15m"} <= set(durations):
            p5 = D[D["duration"] == "5m"].set_index(["asset", "start_ts"])["price_to_beat"]
            p15 = D[D["duration"] == "15m"].set_index(["asset", "start_ts"])["price_to_beat"]
            j = pd.concat([p5.rename("a"), p15.rename("b")], axis=1, join="inner").dropna()
            same_n, same_ok = len(j), int((j["a"] == j["b"]).sum())
        chk["same_ptb_ok"], chk["same_ptb_n"] = same_ok, same_n
        chk["expected"] = int(sum(expected.values()))
        chk["found"] = len(mf)
        chk["resolved"] = int(D["y"].notna().sum())
        rules = D.groupby(["resolution_rule", "twap_lookback_s"], dropna=False).size()
        chk["rules"] = ", ".join(f"{r} / {fr(lb) if pd.notna(lb) else '—'} s : {fr(int(n))}" for (r, lb), n in rules.items())
        fees = D.groupby(["fee_type", "fee_rate", "fee_exponent"], dropna=False).size()
        chk["fees"] = ", ".join(f"{t} ({fr(r, 2)}, {fr(e, 0)}) : {fr(int(n))}" for (t, r, e), n in fees.items())
        nofp = D[~np.isfinite(D["final_price"])]
        chk["no_final"] = (f"{len(nofp)}" + (f" ({', '.join(sorted(nofp['slug']))})" if 0 < len(nofp) <= 8 else ""))
        chk["with_pre"] = int(np.isfinite(D["p_pre"]).sum())
        chk["pre_age_median"] = float(np.nanmedian(D["p_pre_age_s"]))
        chk["restricted"] = int(mf["restricted"].sum())

        base = base_rates(D, expected)
        proxy = proxy_agreement(D)
        level = level_check(D)
        lags = lag_scan(D, bars1s)
        market, calib = market_power(D)
        D = D.join(nowcast_columns(D, bars1s))
        nv = nowcast_vs_market(D)
        nv_lag = nowcast_lag_table(D, bars1s)

        preds = strategy_predictions(D)
        baselines = baseline_table(D, preds)

        # meilleurs proxies
        tot = proxy[proxy["cellule"] == "Tous"].set_index("proxy")
        std = tot[~tot.index.str.startswith("x_")]          # hors proxies exploratoires
        best_proxy = std["accord"].idxmax()
        best_proxy_1m = std[std["résolution"] == "1m"]["accord"].idxmax()

        # meilleure baseline « propre » (in-sample, toutes cellules)
        feat_strats = [s for s, (c, _, _) in preds.items() if c in CLEAN_CUTOFFS]
        allb = baselines[(baselines["cellule"] == "Tous") & baselines["stratégie"].isin(feat_strats)]
        best_strategy = allb.set_index("stratégie")["accuracy"].idxmax()

        # P&L : toutes les stratégies propres + références
        pnl_list = []
        pnl_strats = feat_strats + ["always_up", "always_down"]
        for s in pnl_strats:
            P = pnl_rows(D, preds[s][2], fee_by_slug)
            pnl_list.append(pnl_summary(P, "Tous", s, "in-sample"))
        pnl_all = pd.DataFrame(pnl_list)
        pnl_all["cellule"] = "Tous"
        best_rows = []
        P = pnl_rows(D, preds[best_strategy][2], fee_by_slug)
        for name, g in cells(D):
            best_rows.append(pnl_summary(P.loc[g.index], name, best_strategy, "in-sample"))
        # pseudo hors échantillon : choix sur la 1re moitié, test sur la 2e
        mid_ts = int((start + (end - start) / 2).timestamp())
        first = D["start_ts"] < mid_ts
        acc1 = {}
        for s in feat_strats:
            pr = preds[s][2]
            okm = first.to_numpy() & np.isfinite(pr) & D["y"].notna().to_numpy()
            acc1[s] = float(np.mean(pr[okm] == D["y"].to_numpy()[okm]))
        best_first = max(acc1, key=acc1.get)
        P2 = pnl_rows(D, preds[best_first][2], fee_by_slug)
        second = ~first
        for name, g in cells(D[second]):
            best_rows.append(pnl_summary(P2.loc[g.index], name, best_first, "2e moitié (choix sur la 1re)"))
        pnl_best = pd.DataFrame(best_rows)

    # 5b. contrôle d'exécution par les transactions (sous-échantillon) ----------------------
    exec_df, sub = pd.DataFrame(), pd.DataFrame()
    exec_checks = [(best_strategy, "pre30"), ("gap_m0_1s:momentum", "open5"), (best_strategy, "open5")]
    if args.trades_sample > 0:
        rng = np.random.default_rng(args.seed)
        idx: list[int] = []
        for _, g in D.groupby(["asset", "duration"], sort=False):
            idx += list(rng.choice(g.index.to_numpy(), size=min(args.trades_sample, len(g)), replace=False))
        sub = D.loc[sorted(idx)]
        with rt(f"5b. transactions preneuses autour de S ({len(sub)} marchés)"):
            client_tr = pm.PolymarketClient(session=session, max_rps=args.trades_rps, use_cache=use_cache)
            tw = fetch_trade_windows(client_tr, sub, workers=8, use_cache=use_cache)
            sub = sub.join(tw.set_index("slug"), on="slug")
            exec_df = execution_check(sub, preds, D, exec_checks, fee_by_slug)

    # 6. écriture -------------------------------------------------------------------
    with rt("6. écriture des CSV et du rapport"):
        keep = ["slug", "asset", "duration", "start", "end", "y", "price_to_beat", "final_price", "cl_move_bps", "tie_cl",
                "volume", "liquidity", "fee_type", "fee_rate", "fee_exponent", "twap_lookback_s", "resolution_rule",
                "n_hist_points", "p_pre", "p_pre_age_s", "p_S", "p_S_age_s"]
        keep += [c for c in D.columns if re.fullmatch(r"[pt]_\d+m", c)]
        keep += [c for c in D.columns if c.startswith("nowcast_")]
        keep += [c for c in D.columns if c.startswith("bn_")]
        keep += [f for f, _, _ in FEATURES] + ["sigma_s"]
        seen, cols = set(), []
        for c in keep:
            if c in D.columns and c not in seen:
                seen.add(c)
                cols.append(c)
        for pname, _, _, c_end, c_start in PROXIES:
            D[f"proxy_{pname}"] = (D[c_end] >= D[c_start]).astype(float).where(np.isfinite(D[c_end]) & np.isfinite(D[c_start]))
        cols += [f"proxy_{p[0]}" for p in PROXIES]
        files = [
            ("markets.csv", "une ligne par marché résolu : issue, niveaux Chainlink, prix du jeton Up, niveaux et variables Binance, proxies"),
            ("base_rates.csv", "taux de Up, IC de Wilson, égalités, volumes par cellule"),
            ("proxy_agreement.csv", "taux d'accord de chaque proxy Binance avec l'issue officielle"),
            ("level_check.csv", "niveaux Binance contre niveaux Chainlink (écart, RMSE de la variation)"),
            ("level_lag_scan.csv", "décalage temporel Binance/Chainlink de −10 à +10 s"),
            ("market_price_power.csv", "pouvoir prédictif du prix du jeton Up (S−30 s, S, S+k min)"),
            ("calibration.csv", "calibration par déciles du prix du jeton Up"),
            ("nowcast_vs_market.csv", "marché contre nowcast Binance au même instant"),
            ("nowcast_lag.csv", "robustesse : nowcast calculé 0 à 60 s avant l'horodatage du point de marché"),
            ("baselines.csv", "justesse des baselines pré-ouverture (toutes cellules)"),
            ("pnl.csv", "P&L théorique des baselines propres (in-sample) et détail de la meilleure (+ pseudo hors échantillon)"),
            ("runtime.csv", "temps d'exécution par étape"),
        ]
        if len(exec_df):
            files[-1:-1] = [
                ("trades_windows.csv", "sous-échantillon : prix payés par les preneurs dans [S−30 s, S) et [S, S+5 s] par côté"),
                ("execution_check.csv", "P&L au prix réellement payé par les preneurs contre l'hypothèse ask ≈ milieu + 0,005"),
            ]
            tw_cols = ["slug", "asset", "duration", "start", "y", "p_pre", "gap_m0_1s", best_strategy.split(":")[0]]
            tw_cols += [c for c in sub.columns if c.startswith(tuple(TRADE_WINDOWS)) or c in ("n_trades", "first_trade_dt_s")]
            sub[list(dict.fromkeys(tw_cols))].to_csv(out_dir / "trades_windows.csv", index=False, float_format="%.10g")
            exec_df.to_csv(out_dir / "execution_check.csv", index=False)
        D[cols].to_csv(out_dir / "markets.csv", index=False, float_format="%.10g")
        base.to_csv(out_dir / "base_rates.csv", index=False)
        proxy.to_csv(out_dir / "proxy_agreement.csv", index=False)
        level.to_csv(out_dir / "level_check.csv", index=False)
        lags.to_csv(out_dir / "level_lag_scan.csv", index=False)
        market.to_csv(out_dir / "market_price_power.csv", index=False)
        calib.to_csv(out_dir / "calibration.csv", index=False)
        nv.to_csv(out_dir / "nowcast_vs_market.csv", index=False)
        nv_lag.to_csv(out_dir / "nowcast_lag.csv", index=False)
        baselines.to_csv(out_dir / "baselines.csv", index=False)
        pd.concat([pnl_all.assign(section="toutes_strategies"), pnl_best.assign(section="meilleure_baseline")],
                  ignore_index=True).to_csv(out_dir / "pnl.csv", index=False)

        client_notes = [
            "`UpDownMarket` ne conserve pas `event.eventMetadata` (`priceToBeat`, `finalPrice`), c'est-à-dire les niveaux "
            "Chainlink exacts qui décident de l'issue. Le script les relit avec `client._get` sur `/events`. Ce n'est pas "
            "bloquant, mais cela vaudrait un champ `price_to_beat` / `final_price` dans le client.",
            "`prices-history` renvoie un milieu de fourchette, environ un point par minute : ce n'est pas un prix "
            "exécutable. L'ask est approché par milieu + 0,005, ce qui correspond au carnet 0,50/0,51 observé avant l'ouverture.",
            "`resolution_rule` ne distingue pas le TWAP-30 du TWAP-60. Vérifié sur btc 5m : TWAP-30 du 07/08 au "
            "13/08/2026 23:55 UTC (`btc-5m-twap-30`), TWAP-60 à partir du 14/08 00:00. La docstring du module annonce "
            "« TWAP 60 s depuis le ~7 août » pour toutes les durées, ce qui est inexact pour le 5m. `twap_lookback_s` "
            "est correct (30 ou 60) : c'est lui qu'il faut utiliser pour filtrer. Sur la période étudiée, tous les "
            "marchés sont en TWAP-60 (voir les contrôles).",
            missing_note,
            f"Marchés résolus sans `finalPrice` dans `eventMetadata` : {chk['no_final']}. Ils sont exclus des "
            "comparaisons de niveaux Chainlink, mais gardés pour l'issue officielle.",
            "Les points `prices-history` sont en retard d'environ 10 s sur l'information Binance (voir la robustesse "
            "du nowcast). Pour comparer un modèle au marché à la seconde près, il faut le carnet (`order_book`, "
            "WebSocket) ou les transactions (`trades`), pas `prices_history`.",
            "Aucun bug bloquant trouvé : `list_updown_markets`, `prices_history`, `up_price_at`, `taker_fee` et "
            "`markets_to_frame` ont été utilisés tels quels, avec un contrôle croisé pour `up_price_at`.",
        ]
        runtime_df = pd.DataFrame(rt.rows, columns=["étape", "secondes"])
        ctx = {
            "D": D, "base": base, "proxy": proxy, "level": level, "lag": lags, "market": market, "calib": calib,
            "nowcast": nv, "baselines": baselines, "pnl_all": pnl_all, "pnl_best": pnl_best, "checks": chk,
            "runtime": runtime_df, "assets": assets, "durations": durations, "start": start, "end": end,
            "days": args.days, "best_proxy": best_proxy, "best_proxy_1m": best_proxy_1m,
            "best_strategy": best_strategy, "missing_1s": ", ".join(f"{a.upper()} {m[0]}" for a, m in missing.items()),
            "client_notes": client_notes, "files_desc": files, "total_runtime": time.perf_counter() - t_all,
            "exec": exec_df, "trades_sample": args.trades_sample, "n_sub": len(sub), "nowcast_lag": nv_lag,
            "use_cache": use_cache,
        }
        write_report(ctx, out_dir)
    total = time.perf_counter() - t_all
    runtime_df = pd.DataFrame(rt.rows + [("total", total)], columns=["étape", "secondes"])
    runtime_df.to_csv(out_dir / "runtime.csv", index=False)
    # réécrit le rapport avec le temps total définitif
    ctx["runtime"], ctx["total_runtime"] = runtime_df, total
    write_report(ctx, out_dir)
    log.info("terminé en %.1f s — %s", total, out_dir / "diagnostic.md")
    summary = {
        "n_resolved": int(D["y"].notna().sum()), "best_proxy": best_proxy,
        "best_proxy_agreement": float(tot.loc[best_proxy, "accord"]), "best_proxy_1m": best_proxy_1m,
        "best_strategy": best_strategy, "runtime_s": round(total, 1),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
