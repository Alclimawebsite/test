"""Backtest « nos modèles contre le marché » sur les marchés Polymarket crypto « Up or Down ».

Simulation papier uniquement : aucune clé, aucun ordre. Ce module regroupe les fonctions
réutilisables ; l'exécution complète (données, entraînement, rapport) est dans
``scripts/polymarket_models_vs_market.py``.

Règle de résolution (régime TWAP-60, depuis le 14/08/2026 00:00 UTC, vérifiée dans
``reports/polymarket/diagnostic.md``) : « Up » ssi ``TWAP60(E) >= TWAP60(S)``, où
``TWAP60(t)`` est la moyenne du prix Chainlink sur ``(t − 60 s, t]``, ``S`` le début et
``E = S + D`` la fin de la fenêtre.

Instant de décision et causalité
--------------------------------
On décide à ``S − 30 s`` (:data:`PRE_OFFSET_S`) et on compare au prix du jeton Up à ce même
instant (dernier point ``prices-history`` <= S − 30 s, ancienneté <= 5 min). Toute variable
explicative n'utilise que l'information disponible à ``S − 30 s`` :

* bougies Binance **1m** : la dernière bougie close est celle qui s'ouvre à ``S − 2 min``
  (close à ``S − 1 min``) — :func:`indicator_rows` lit la ligne d'horodatage d'ouverture
  ``S − 120 s`` d'indicateurs causaux (``tradebot.indicators``) ;
* bougies Binance **1s** : bougies closes au plus tard à ``S − 30 s``, c'est-à-dire ouvertes
  dans ``[S − 60 s, S − 30 s)`` pour la minute en cours (:func:`minute_aggregates_1s` agrège
  chaque minute en deux moitiés ; seule la première moitié de la minute ``S − 60 s`` sert).

Étiquette d'entraînement (Binance, proxy 1m à 96,5 % d'accord avec l'issue officielle) :
``y = 1{VWAP(bougie [S+D−1m]) >= VWAP(bougie [S−1m])}`` avec ``VWAP = quote_volume / volume``
(:func:`vwap_label`). L'évaluation, elle, se fait sur l'issue **officielle** Polymarket.

Exécution simulée (preneur) : ``ask_Up = p_Up(S−30 s) + 0,005`` et
``ask_Down = 1 − p_Up(S−30 s) + 0,005`` (carnet ≈ 0,50 / 0,51 avant l'ouverture ; le point
``prices-history`` est un milieu de fourchette), frais ``taker_fee`` du ``feeSchedule`` du
marché, une part par marché (:func:`taker_trades`). Variante maker **optimiste** (borne
haute) : achat au bid ``p − 0,005`` sans frais, en supposant l'ordre exécuté.
"""

from __future__ import annotations

import io
import logging
import math
import threading
import time
import warnings
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import polymarket as pm

log = logging.getLogger(__name__)

__all__ = [
    "TWAP60_START", "PRE_OFFSET_S", "PRE_STALENESS_S", "HALF_SPREAD", "SLOT_S",
    "market_table", "pre_open_price", "fetch_pre_open_prices",
    "minute_aggregates_1s", "load_second_aggregates",
    "bar_lookup", "vwap_label", "indicator_rows", "twap_partial_features", "baseline_features",
    "train_segments", "ProbModel", "prob_metrics", "calibration_table",
    "SlotBootstrap", "weighted_auc", "taker_trades", "maker_trades", "pnl_by_margin",
    "choose_margin",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
TWAP60_START = pd.Timestamp("2026-08-14 00:00", tz="UTC")   # TWAP-60 partout (5m, 15m)
PRE_OFFSET_S = 30          # décision / prix de référence à S − 30 s
PRE_STALENESS_S = 300      # ancienneté maximale du point prices-history retenu
PRICE_LOOKBACK_S = 900     # historique demandé [S − 15 min, E + 1 min] (même clé de cache
                           # que scripts/polymarket_diagnostic.py)
HALF_SPREAD = 0.005        # ask ≈ milieu + 0,005, bid ≈ milieu − 0,005
SLOT_S = 900               # grappes du bootstrap : créneaux de 15 min (actifs/durées corrélés)
EPOCH = pd.Timestamp(0, tz="UTC")


def to_unix(values) -> np.ndarray:
    """Horodatages (Series / DatetimeIndex UTC) -> secondes Unix int64."""
    idx = pd.DatetimeIndex(values)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return ((idx - EPOCH) // pd.Timedelta("1s")).to_numpy().astype("int64")


# ---------------------------------------------------------------------------
# Polymarket : marchés et prix avant l'ouverture
# ---------------------------------------------------------------------------
def market_table(client: pm.PolymarketClient, assets: Iterable[str], durations: Iterable[str],
                 start: pd.Timestamp, end: pd.Timestamp, max_workers: int = 8) -> pd.DataFrame:
    """Marchés ``asset × duration`` dont la fenêtre commence dans ``[start, end)``.

    Une ligne par marché (colonnes de :func:`tradebot.polymarket.markets_to_frame`) plus
    ``start_ts`` / ``end_ts`` (s Unix), ``duration_s``, ``y`` (issue officielle 1/0, NaN si non
    résolue) et ``slot`` (créneau de 15 min, pour le bootstrap groupé).
    """
    frames = []
    for a in assets:
        for d in durations:
            ms = client.list_updown_markets(a, d, start, end, max_workers=max_workers)
            log.info("%s %s : %d marchés", a, d, len(ms))
            frames.append(pm.markets_to_frame(ms))
    df = pd.concat(frames, ignore_index=True) if frames else pm.markets_to_frame([])
    df["start_ts"] = to_unix(df["start"])
    df["end_ts"] = to_unix(df["end"])
    df["duration_s"] = df["duration"].map(pm.DURATION_SECONDS).astype("int64")
    df["y"] = df["up_final"].astype("float64")
    df["slot"] = df["start_ts"] // SLOT_S
    return df.sort_values(["start_ts", "asset", "duration"], kind="stable").reset_index(drop=True)


def pre_open_price(hist: pd.Series, start_ts: int, offset_s: int = PRE_OFFSET_S,
                   staleness_s: int = PRE_STALENESS_S) -> tuple[float, float]:
    """(prix, ancienneté en s) du dernier point <= ``start_ts − offset_s`` ; NaN si trop ancien.

    Même règle que :func:`tradebot.polymarket.up_price_at` (qui donne le prix).
    """
    t = pd.Timestamp(int(start_ts) - int(offset_s), unit="s", tz="UTC")
    p = pm.up_price_at(hist, t, max_staleness=pd.Timedelta(seconds=staleness_s))
    if not np.isfinite(p):
        return math.nan, math.nan
    idx = hist.index
    i = idx.searchsorted(t, side="right") - 1
    return float(p), float((t - idx[i]).total_seconds())


def fetch_pre_open_prices(client: pm.PolymarketClient, markets: pd.DataFrame, workers: int = 16,
                          offset_s: int = PRE_OFFSET_S, staleness_s: int = PRE_STALENESS_S,
                          progress: Callable[[int, int], None] | None = None,
                          stop: Callable[[], bool] | None = None) -> pd.DataFrame:
    """Prix du jeton Up à ``S − offset_s`` pour chaque marché (``prices_history``, fidelity=1).

    ``markets`` : colonnes ``slug``, ``token_up``, ``start_ts``, ``end_ts``. Historique demandé :
    ``[S − 900 s, E + 60 s]`` (mis en cache disque par le client pour les marchés résolus).
    Retour : ``slug``, ``p_pre``, ``p_pre_age_s``, ``n_hist``, ``error``. ``stop()`` vrai ->
    les marchés restants sont ignorés (absents du résultat).
    """
    rows: list[dict] = []
    lock = threading.Lock()
    done = [0]
    n = len(markets)

    def one(rec) -> dict | None:
        if stop is not None and stop():
            return None
        slug, token, S, E = rec
        row = {"slug": slug, "p_pre": math.nan, "p_pre_age_s": math.nan, "n_hist": 0, "error": False}
        try:
            hist = client.prices_history(token, int(S) - PRICE_LOOKBACK_S, int(E) + 60, fidelity=1,
                                         resolved=True)
            row["n_hist"] = len(hist)
            row["p_pre"], row["p_pre_age_s"] = pre_open_price(hist, int(S), offset_s, staleness_s)
        except pm.PolymarketError as exc:
            log.warning("prices-history %s : %s", slug, exc)
            row["error"] = True
        with lock:
            done[0] += 1
            if progress is not None:
                progress(done[0], n)
        return row

    recs = list(zip(markets["slug"], markets["token_up"], markets["start_ts"], markets["end_ts"]))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(one, recs):
            if r is not None:
                rows.append(r)
    return pd.DataFrame(rows, columns=["slug", "p_pre", "p_pre_age_s", "n_hist", "error"])


# ---------------------------------------------------------------------------
# Binance 1s : agrégats par minute (moitiés de minute)
# ---------------------------------------------------------------------------
AGG_1S_COLUMNS = (
    "a_open", "a_close", "a_twap", "a_vwap", "a_high", "a_low", "a_volume", "a_taker", "a_trades",
    "a_slope", "b_twap", "twap60",
)


def minute_aggregates_1s(df1s: pd.DataFrame) -> pd.DataFrame:
    """Agrège des bougies 1s en minutes, chaque minute ``[t, t+60)`` coupée en deux moitiés.

    Première moitié ``a`` = bougies 1s ouvertes dans ``[t, t+30)`` (information connue à
    ``t + 30 s``) ; seconde moitié ``b`` = ``[t+30, t+60)``. Colonnes (index = ouverture de la
    minute, UTC) :

    * ``a_open`` : open de la bougie ``t`` ; ``a_close`` : close de la bougie ``t+29`` (prix à
      ``t + 30 s``) ; ``a_twap`` : moyenne des closes de ``a`` (TWAP de ``(t, t+30]``) ;
      ``a_vwap``, ``a_high``, ``a_low``, ``a_volume``, ``a_taker`` (volume acheteur preneur),
      ``a_trades`` ; ``a_slope`` : pente MCO du log-close sur les 30 s (par seconde) ;
    * ``b_twap`` : moyenne des closes de ``b`` ; ``twap60`` : moyenne des 60 closes (TWAP60 à
      ``t + 60 s``, pour l'étiquette exacte / les contrôles).

    Les secondes manquantes sont comblées (close reporté, volume nul). Les minutes sans aucune
    bougie 1s sont absentes.
    """
    if df1s.empty:
        return pd.DataFrame(columns=list(AGG_1S_COLUMNS), index=pd.DatetimeIndex([], tz="UTC"))
    idx = pd.DatetimeIndex(df1s.index)
    t = ((idx - EPOCH) // pd.Timedelta("1s")).to_numpy().astype("int64")
    m0 = int(t.min() // 60 * 60)
    n_min = int((t.max() - m0) // 60 + 1)
    pos = t - m0

    def arr(col: str, fill=np.nan) -> np.ndarray:
        a = np.full(n_min * 60, fill, dtype="float64")
        if col in df1s.columns:
            a[pos] = df1s[col].to_numpy(dtype="float64")
        return a

    close = arr("close")
    present = ~np.isnan(close)
    close = pd.Series(close).ffill().to_numpy()
    o, h, lo = arr("open"), arr("high"), arr("low")
    miss = np.isnan(o)
    o[miss], h[miss], lo[miss] = close[miss], close[miss], close[miss]
    vol = np.nan_to_num(arr("volume", 0.0))
    qv = np.nan_to_num(arr("quote_volume", 0.0))
    taker = np.nan_to_num(arr("taker_buy_volume", 0.0))
    trades = np.nan_to_num(arr("trades", 0.0))

    C, O, H, L = (x.reshape(n_min, 60) for x in (close, o, h, lo))
    V, Q, T, N = (x.reshape(n_min, 60) for x in (vol, qv, taker, trades))
    P = present.reshape(n_min, 60)
    a = slice(0, 30)
    va, qa = V[:, a].sum(1), Q[:, a].sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap_a = np.where(va > 0, qa / va, C[:, a].mean(1))
        lc = np.log(C[:, a])
    x = np.arange(30, dtype="float64") - 14.5
    slope = (lc * x).sum(1) / (x * x).sum()
    out = pd.DataFrame({
        "a_open": O[:, 0], "a_close": C[:, 29], "a_twap": C[:, a].mean(1), "a_vwap": vwap_a,
        "a_high": H[:, a].max(1), "a_low": L[:, a].min(1), "a_volume": va, "a_taker": T[:, a].sum(1),
        "a_trades": N[:, a].sum(1), "a_slope": slope, "b_twap": C[:, 30:].mean(1), "twap60": C.mean(1),
    }, index=pd.to_datetime(m0 + 60 * np.arange(n_min), unit="s", utc=True).as_unit("ns"))
    # une minute sans aucune bougie 1s n'est pas une observation (close reporté seulement)
    out = out[P.any(axis=1)]
    # la première minute peut commencer avant la première bougie : invalide si incomplète au début
    first_ok = P[:, 0] | (np.arange(n_min) > 0)
    return out[first_ok[P.any(axis=1)]]


def _day_1s(symbol: str, day: pd.Timestamp) -> pd.DataFrame | None:
    """Bougies 1s d'un jour UTC (zip journalier data.binance.vision via ``tradebot.data``)."""
    from . import data as tdata

    url = tdata._kline_url(symbol, "1s", f"{day:%Y-%m-%d}", daily=True)
    return tdata._download_kline_zip(url)


def load_second_aggregates(symbol: str, start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path,
                           workers: int = 6, fetch_day: Callable | None = None) -> pd.DataFrame:
    """Agrégats par minute (:func:`minute_aggregates_1s`) des jours UTC de ``[start, end)``.

    Un parquet par jour dans ``cache_dir`` (``{symbol}_{jour}.parquet``, jours complets
    seulement : 1 440 minutes). ``fetch_day(symbol, day) -> DataFrame 1s`` est injectable
    (tests) ; par défaut, zips journaliers Binance.
    """
    fetch_day = fetch_day or _day_1s
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    days = pd.date_range(pd.Timestamp(start).floor("D"), pd.Timestamp(end) - pd.Timedelta("1ns"),
                         freq="D")

    def one(day: pd.Timestamp) -> pd.DataFrame:
        path = cache_dir / f"{symbol}_{day:%Y-%m-%d}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        raw = fetch_day(symbol, day)
        if raw is None or raw.empty:
            log.warning("%s 1s %s : aucune donnée", symbol, day.date())
            return pd.DataFrame(columns=list(AGG_1S_COLUMNS))
        agg = minute_aggregates_1s(raw)
        agg = agg[(agg.index >= day) & (agg.index < day + pd.Timedelta(days=1))]
        if len(agg) == 1440:
            pm._atomic_parquet(agg.rename_axis("time").reset_index(), path)
            return agg.rename_axis("time").reset_index()
        log.warning("%s 1s %s : %d minutes (jour incomplet, non mis en cache)", symbol, day.date(), len(agg))
        return agg.rename_axis("time").reset_index()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        parts = [p for p in ex.map(one, days) if len(p)]
    if not parts:
        return pd.DataFrame(columns=list(AGG_1S_COLUMNS), index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.concat(parts, ignore_index=True)
    df = df.set_index(pd.DatetimeIndex(df.pop("time")).tz_convert("UTC").as_unit("ns")).sort_index()
    return df[~df.index.duplicated(keep="last")].astype("float64")


# ---------------------------------------------------------------------------
# Étiquettes et variables (information <= S − 30 s)
# ---------------------------------------------------------------------------
def bar_lookup(frame: pd.DataFrame, times_s: np.ndarray, cols: Sequence[str] | str) -> np.ndarray:
    """Valeurs de ``frame`` (index = horodatage d'ouverture) aux instants EXACTS ``times_s``
    (s Unix) ; NaN si la ligne n'existe pas (trou de données : jamais de report)."""
    single = isinstance(cols, str)
    cols = [cols] if single else list(cols)
    idx = to_unix(frame.index)
    pos = np.searchsorted(idx, times_s)
    ok = (pos < len(idx)) & (idx[np.minimum(pos, len(idx) - 1)] == times_s)
    out = np.full((len(times_s), len(cols)), np.nan)
    if ok.any():
        vals = frame[cols].to_numpy(dtype="float64")
        out[ok] = vals[pos[ok]]
    return out[:, 0] if single else out


def _vwap_1m(bars1m: pd.DataFrame, open_s: np.ndarray) -> np.ndarray:
    qv, v, c = bar_lookup(bars1m, open_s, ["quote_volume", "volume", "close"]).T
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(v > 0, qv / v, np.nan)


def vwap_label(bars1m: pd.DataFrame, starts_s: np.ndarray, duration_s: int) -> np.ndarray:
    """Étiquette proxy : ``1{VWAP(bougie [S+D−1m]) >= VWAP(bougie [S−1m])}`` (1.0 / 0.0 / NaN).

    ``bars1m`` : bougies 1m Binance indexées par leur ouverture, colonnes ``quote_volume`` et
    ``volume``. NaN si une des deux bougies manque ou a un volume nul.
    """
    s = np.asarray(starts_s, dtype="int64")
    v0 = _vwap_1m(bars1m, s - 60)
    v1 = _vwap_1m(bars1m, s + int(duration_s) - 60)
    y = (v1 >= v0).astype("float64")
    y[~(np.isfinite(v0) & np.isfinite(v1))] = np.nan
    return y


def indicator_rows(ind: pd.DataFrame, starts_s: np.ndarray) -> pd.DataFrame:
    """Lignes d'indicateurs causaux lues à la dernière bougie 1m CLOSE avant ``S − 30 s`` :
    celle qui s'ouvre à ``S − 120 s`` (close à ``S − 60 s``). NaN si elle manque."""
    s = np.asarray(starts_s, dtype="int64")
    vals = bar_lookup(ind, s - 120, list(ind.columns))
    return pd.DataFrame(vals.astype("float32"), columns=ind.columns)


def _sigma_1m(bars1m: pd.DataFrame, n: int = 60) -> pd.Series:
    """Écart type des rendements log 1m sur ``n`` bougies (causal, valeur à l'ouverture t
    utilisant les closes <= t + 60 s)."""
    r = np.log(bars1m["close"]).diff()
    return r.rolling(n, min_periods=n // 2).std()


def twap_partial_features(agg1s: pd.DataFrame, bars1m: pd.DataFrame, starts_s: np.ndarray,
                          duration_s: int) -> pd.DataFrame:
    """Variables « TWAP partiel » à ``S − 30 s`` à partir des agrégats 1s (:func:`minute_aggregates_1s`).

    La minute ``m = S − 60 s`` n'est lue que dans sa première moitié (bougies 1s ouvertes dans
    ``[S − 60, S − 30)``) ; les minutes antérieures entièrement. ``σ`` = volatilité 1m sur
    60 min finissant à ``S − 60 s``.

    * ``tw_gap30`` = log(spot(S−30) / moyenne des closes 1s sur (S−60, S−30]) : écart spot −
      TWAP partiel (``gap_m30`` du diagnostic) ; ``tw_gap30_z`` = ``tw_gap30 / (2 σ √(D/60))`` ;
    * ``tw_ret30`` (S−60 → S−30), ``tw_ret90`` (S−120 → S−30), ``tw_ret330`` (S−360 → S−30) ;
    * ``tw_slope30`` (pente log-prix, pb/s), ``tw_vwap_gap30``, ``tw_range30`` (log haut/bas),
      ``tw_taker30`` (part acheteuse − 0,5), ``tw_vol30_rel`` (volume des 30 s / volume moyen
      par 30 s sur 60 min), ``tw_prev_gap`` = log(TWAP(S−90, S−60] / TWAP60(S−60)) ;
    * ``sigma_1m`` : σ (par minute).
    """
    s = np.asarray(starts_s, dtype="int64")
    m = s - 60
    cur = bar_lookup(agg1s, m, ["a_open", "a_close", "a_twap", "a_vwap", "a_high", "a_low",
                                "a_volume", "a_taker", "a_slope"])
    a_open, a_close, a_twap, a_vwap, a_hi, a_lo, a_vol, a_tk, a_slope = cur.T
    prev = bar_lookup(agg1s, m - 60, ["a_close", "b_twap", "twap60"])
    p_close, p_btwap, p_twap60 = prev.T
    close_330 = bar_lookup(agg1s, m - 300, "a_close")   # prix à S − 330 s
    sig = bar_lookup(_sigma_1m(bars1m).to_frame("s"), s - 120, "s")
    vol_mean = bar_lookup(bars1m["volume"].rolling(60, min_periods=30).mean().to_frame("v"),
                          s - 120, "v")
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = np.log(a_close / a_twap)
        out = {
            "tw_gap30": gap,
            "tw_gap30_z": gap / (2.0 * sig * math.sqrt(duration_s / 60.0)),
            "tw_ret30": np.log(a_close / a_open),
            "tw_ret90": np.log(a_close / p_close),
            "tw_ret330": np.log(a_close / close_330),
            "tw_slope30": 1e4 * a_slope,
            "tw_vwap_gap30": np.log(a_close / a_vwap),
            "tw_range30": np.log(a_hi / a_lo),
            "tw_taker30": np.where(a_vol > 0, a_tk / a_vol - 0.5, 0.0),
            "tw_vol30_rel": a_vol / (0.5 * vol_mean),
            "tw_prev_gap": np.log(p_btwap / p_twap60),
            "sigma_1m": sig,
        }
    df = pd.DataFrame(out)
    df.loc[~np.isfinite(a_close), :] = np.nan
    return df.astype("float32")


def baseline_features(bars1m: pd.DataFrame, starts_s: np.ndarray) -> pd.DataFrame:
    """Signaux des baselines 1m du diagnostic (coupure S − 60 s) : ``mom15_m60`` =
    log(close(S−60) / close(S−960)), ``mom5_m60`` = log(close(S−60) / close(S−360))."""
    s = np.asarray(starts_s, dtype="int64")
    c = lambda t: bar_lookup(bars1m, t, "close")  # noqa: E731  close de la bougie ouverte à t
    with np.errstate(invalid="ignore", divide="ignore"):
        return pd.DataFrame({
            "mom15_m60": np.log(c(s - 120) / c(s - 1020)),
            "mom5_m60": np.log(c(s - 120) / c(s - 420)),
        })


# ---------------------------------------------------------------------------
# Découpage chronologique purgé
# ---------------------------------------------------------------------------
def train_segments(starts_s: np.ndarray, train_end_s: int, duration_s: int,
                   fracs: tuple[float, float, float] = (0.8, 0.1, 0.1), gap_s: int = 3600,
                   train_start_s: int | None = None) -> dict[str, np.ndarray]:
    """Masques ``fit`` / ``es`` (arrêt précoce) / ``cal`` (calibration) sur la période
    d'apprentissage ``S < train_end_s − D − gap_s`` (purge avant le test), découpée par le temps
    selon ``fracs``. Entre deux segments, on retire les origines dont la fenêtre d'étiquette
    ``[S − 60 s, S + D]`` déborde sur le segment suivant, plus ``gap_s`` de marge.
    """
    s = np.asarray(starts_s, dtype="int64")
    D = int(duration_s)
    lo = int(s.min()) if train_start_s is None else int(train_start_s)
    hi = int(train_end_s) - D - int(gap_s)            # dernière origine admise (purge du test)
    f = np.asarray(fracs, dtype="float64")
    f = f / f.sum()
    b1 = lo + int(f[0] * (hi - lo))
    b2 = lo + int((f[0] + f[1]) * (hi - lo))
    purge = D + int(gap_s)
    fit = (s >= lo) & (s < b1 - purge)
    es = (s >= b1) & (s < b2 - purge)
    cal = (s >= b2) & (s <= hi)
    return {"fit": fit, "es": es, "cal": cal, "bounds": np.array([lo, b1, b2, hi])}


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------
@dataclass
class ProbModel:
    """Classifieur de P(Up) avec imputation par la médiane de l'apprentissage.

    ``kind`` : ``"logit"`` (StandardScaler + régression logistique L2, ``C``) ou ``"hgb"``
    (``HistGradientBoostingClassifier``, arrêt précoce sur un ensemble de validation
    CHRONOLOGIQUE fourni : ``X_val``/``y_val``). Mêmes hyperparamètres que
    ``tradebot.evaluation`` (profondeur 3, taux 0,05) avec plus d'itérations possibles.
    ``calibrate(X, y)`` ajuste une recalibration isotonique
    (:func:`tradebot.forecaster.calibrate_isotonic`) sur une période postérieure à
    l'apprentissage ; ``predict`` la applique si elle existe.
    """

    kind: str = "logit"
    C: float = 0.1
    max_iter: int = 500
    random_state: int = 0
    features: list[str] = field(default_factory=list)
    median_: np.ndarray | None = None
    keep_: np.ndarray | None = None
    est_: object | None = None
    iso_: Callable | None = None
    n_iter_: int | None = None

    def _prep(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype="float64")[:, self.keep_]
        X = np.where(np.isfinite(X), X, np.nan)
        return np.where(np.isnan(X), self.median_[None, :], X)

    def fit(self, X: np.ndarray, y: np.ndarray, X_val: np.ndarray | None = None,
            y_val: np.ndarray | None = None) -> "ProbModel":
        X = np.asarray(X, dtype="float64")
        X = np.where(np.isfinite(X), X, np.nan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(X, axis=0)
        self.keep_ = np.isfinite(med)
        self.median_ = med[self.keep_]
        Xp = self._prep(X)
        y = np.asarray(y, dtype="float64")
        if self.kind == "logit":
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler

            est = make_pipeline(StandardScaler(),
                                LogisticRegression(C=self.C, max_iter=self.max_iter))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                est.fit(Xp, y)
        elif self.kind == "hgb":
            from sklearn.ensemble import HistGradientBoostingClassifier

            est = HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=self.max_iter, min_samples_leaf=200,
                l2_regularization=1.0, early_stopping=X_val is not None, n_iter_no_change=30,
                scoring="loss", validation_fraction=None, random_state=self.random_state)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if X_val is not None:
                    est.fit(Xp, y, X_val=self._prep(X_val), y_val=np.asarray(y_val, dtype="float64"))
                else:
                    est.fit(Xp, y)
            self.n_iter_ = int(est.n_iter_)
        else:
            raise ValueError("kind doit valoir 'logit' ou 'hgb'")
        self.est_ = est
        return self

    def predict_raw(self, X: np.ndarray) -> np.ndarray:
        proba = self.est_.predict_proba(self._prep(X))
        classes = list(self.est_.classes_)
        return proba[:, classes.index(1.0)]

    def calibrate(self, X: np.ndarray, y: np.ndarray) -> "ProbModel":
        from .forecaster import calibrate_isotonic

        self.iso_ = calibrate_isotonic(self.predict_raw(X), y)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        p = self.predict_raw(X)
        return self.iso_(p) if self.iso_ is not None else p


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def prob_metrics(y: np.ndarray, p: np.ndarray, score: np.ndarray | None = None) -> dict:
    """Justesse (Up si p >= 0,5), AUC (sur ``score``, par défaut ``p``), Brier et log-loss.

    ``p`` peut être NaN (signal binaire sans probabilité) : Brier / log-loss NaN, justesse et AUC
    calculées sur ``score``. Lignes où ``y`` ou le score manque ignorées."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    s = p if score is None else np.asarray(score, dtype="float64")
    ok = np.isfinite(y) & np.isfinite(s)
    y, p, s = y[ok], p[ok], s[ok]
    n = int(ok.sum())
    out = {"n": n, "accuracy": math.nan, "auc": math.nan, "brier": math.nan, "logloss": math.nan}
    if n == 0:
        return out
    pred = (s >= 0.5) if score is None else (s >= 0)
    out["accuracy"] = float(np.mean(pred == (y > 0.5)))
    if np.unique(y).size == 2 and np.unique(s).size > 1:
        out["auc"] = float(weighted_auc(y, s, np.ones((1, n)))[0])
    if np.isfinite(p).all():
        pc = np.clip(p, 1e-6, 1 - 1e-6)
        out["brier"] = float(np.mean((p - y) ** 2))
        out["logloss"] = float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)))
    return out


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 10, quantile: bool = True) -> pd.DataFrame:
    """Calibration par déciles de ``p`` (quantiles par défaut) : p moyen, fréquence de Up, n."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if quantile:
        edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    else:
        edges = np.linspace(0, 1, bins + 1)
    k = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    df = pd.DataFrame({"bin": k, "p": p, "y": y}).groupby("bin").agg(
        p_mean=("p", "mean"), up_rate=("y", "mean"), n=("y", "size")).reset_index()
    df["p_lo"] = edges[df["bin"].to_numpy()]
    df["p_hi"] = edges[df["bin"].to_numpy() + 1]
    return df


def weighted_auc(y: np.ndarray, s: np.ndarray, W: np.ndarray) -> np.ndarray:
    """AUC pondérée, vectorisée sur des lignes de poids ``W`` (B, n) : égalités comptées 1/2.

    Sert au bootstrap groupé (poids = nombre de tirages du créneau de chaque ligne)."""
    y = np.asarray(y, dtype="float64")
    s = np.asarray(s, dtype="float64")
    W = np.atleast_2d(np.asarray(W, dtype="float64"))
    order = np.argsort(s, kind="mergesort")
    ss, ys, Ws = s[order], y[order], W[:, order]
    _, first = np.unique(ss, return_index=True)
    gpos = np.add.reduceat(Ws * ys, first, axis=1)
    gneg = np.add.reduceat(Ws * (1 - ys), first, axis=1)
    below = np.cumsum(gneg, axis=1) - gneg
    num = (gpos * (below + 0.5 * gneg)).sum(axis=1)
    den = gpos.sum(axis=1) * gneg.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return num / den


class SlotBootstrap:
    """Bootstrap groupé par créneau (grappe) : on tire les créneaux avec remise.

    Les mêmes tirages (``counts``, B × G) servent à toutes les statistiques : les différences
    appariées (modèle − marché) ont donc des IC cohérents. ``ratio(num, den)`` donne
    ``Σ num / Σ den`` et son IC percentile ; ``mean(x)`` = ``ratio(x, 1)``.
    """

    def __init__(self, groups: np.ndarray, B: int = 2000, seed: int = 0, alpha: float = 0.05):
        g = np.asarray(groups)
        self.codes, self.uniq = pd.factorize(g, sort=True)
        self.G = len(self.uniq)
        self.B = int(B)
        self.alpha = float(alpha)
        rng = np.random.default_rng(seed)
        self.counts = rng.multinomial(self.G, np.full(self.G, 1.0 / self.G), size=self.B).astype("float64")

    def _gsum(self, x: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
        x = np.asarray(x, dtype="float64")
        w = np.isfinite(x) if mask is None else (np.asarray(mask, bool) & np.isfinite(x))
        return np.bincount(self.codes[w], weights=x[w], minlength=self.G)

    def ratio(self, num: np.ndarray, den: np.ndarray | float = 1.0,
              mask: np.ndarray | None = None) -> tuple[float, float, float]:
        num = np.asarray(num, dtype="float64")
        den = np.broadcast_to(np.asarray(den, dtype="float64"), num.shape)
        ok = np.isfinite(num) & np.isfinite(den)
        if mask is not None:
            ok &= np.asarray(mask, bool)
        gn, gd = self._gsum(num, ok), self._gsum(den, ok)
        if gd.sum() == 0:
            return math.nan, math.nan, math.nan
        est = gn.sum() / gd.sum()
        with np.errstate(invalid="ignore", divide="ignore"):
            boots = (self.counts @ gn) / (self.counts @ gd)
        boots = boots[np.isfinite(boots)]
        lo, hi = np.quantile(boots, [self.alpha / 2, 1 - self.alpha / 2]) if boots.size else (math.nan,) * 2
        return float(est), float(lo), float(hi)

    def mean(self, x: np.ndarray, mask: np.ndarray | None = None) -> tuple[float, float, float]:
        return self.ratio(x, 1.0, mask)

    def auc(self, y: np.ndarray, s: np.ndarray, mask: np.ndarray | None = None,
            B: int = 300) -> tuple[float, float, float]:
        y = np.asarray(y, dtype="float64")
        s = np.asarray(s, dtype="float64")
        ok = np.isfinite(y) & np.isfinite(s)
        if mask is not None:
            ok &= np.asarray(mask, bool)
        if ok.sum() < 10 or np.unique(y[ok]).size < 2:
            return math.nan, math.nan, math.nan
        est = float(weighted_auc(y[ok], s[ok], np.ones((1, int(ok.sum()))))[0])
        W = self.counts[: min(B, self.B)][:, self.codes[ok]]
        boots = weighted_auc(y[ok], s[ok], W)
        boots = boots[np.isfinite(boots)]
        lo, hi = np.quantile(boots, [self.alpha / 2, 1 - self.alpha / 2])
        return est, float(lo), float(hi)


# ---------------------------------------------------------------------------
# Exécution simulée et P&L
# ---------------------------------------------------------------------------
def _fee_per_share(price: np.ndarray, fee_rate: np.ndarray, fee_exponent: np.ndarray) -> np.ndarray:
    """Frais preneur par part via :func:`tradebot.polymarket.taker_fee`, barème par marché."""
    price = np.asarray(price, dtype="float64")
    rate = np.broadcast_to(np.asarray(fee_rate, dtype="float64"), price.shape)
    expo = np.broadcast_to(np.asarray(fee_exponent, dtype="float64"), price.shape)
    out = np.zeros(price.shape)
    keys = pd.DataFrame({"r": np.nan_to_num(rate, nan=0.0), "e": np.nan_to_num(expo, nan=1.0)})
    for (r, e), idx in keys.groupby(["r", "e"]).indices.items():
        sched = {"rate": float(r), "exponent": float(e)} if r > 0 else None
        ok = np.isfinite(price[idx])
        fee = np.full(len(idx), np.nan)
        if ok.any():
            fee[ok] = pm.taker_fee(price[idx][ok], 1.0, sched)
        out[idx] = fee
    return out


def taker_trades(p_model: np.ndarray, p_mkt: np.ndarray, y: np.ndarray, margin: float,
                 fee_rate: np.ndarray | float = 0.07, fee_exponent: np.ndarray | float = 1.0,
                 half_spread: float = HALF_SPREAD) -> pd.DataFrame:
    """Décision et P&L (1 part) d'un preneur à ``S − 30 s``.

    ``ask_Up = p_mkt + half_spread`` ; ``ask_Down = 1 − p_mkt + half_spread`` ;
    coût = ask + frais(ask). Achat Up si ``p_model − coût_Up > margin``, achat Down si
    ``(1 − p_model) − coût_Down > margin`` (au plus un côté : ``coût_Up + coût_Down > 1``) ;
    en cas d'égalité improbable, le plus grand avantage l'emporte.
    Colonnes : ``side`` (+1 Up, −1 Down, 0 rien), ``price``, ``fee``, ``edge`` (avantage estimé),
    ``pnl`` (``1{gagné} − prix − frais``, 0 sans position), ``win``.
    """
    p = np.asarray(p_model, dtype="float64")
    m = np.asarray(p_mkt, dtype="float64")
    y = np.asarray(y, dtype="float64")
    ask_up = m + half_spread
    ask_dn = 1.0 - m + half_spread
    fee_up = _fee_per_share(ask_up, fee_rate, fee_exponent)
    fee_dn = _fee_per_share(ask_dn, fee_rate, fee_exponent)
    e_up = p - ask_up - fee_up
    e_dn = (1.0 - p) - ask_dn - fee_dn
    return _book(e_up, e_dn, ask_up, ask_dn, fee_up, fee_dn, y, margin)


def maker_trades(p_model: np.ndarray, p_mkt: np.ndarray, y: np.ndarray, margin: float,
                 half_spread: float = HALF_SPREAD) -> pd.DataFrame:
    """Variante maker OPTIMISTE (borne haute) : achat au bid (``p_mkt − half_spread`` pour Up,
    ``1 − p_mkt − half_spread`` pour Down), sans frais, en supposant l'ordre toujours exécuté
    (ni file d'attente, ni sélection adverse)."""
    p = np.asarray(p_model, dtype="float64")
    m = np.asarray(p_mkt, dtype="float64")
    bid_up = m - half_spread
    bid_dn = 1.0 - m - half_spread
    zero = np.zeros_like(m)
    return _book(p - bid_up, (1.0 - p) - bid_dn, bid_up, bid_dn, zero, zero,
                 np.asarray(y, dtype="float64"), margin)


def _book(e_up, e_dn, px_up, px_dn, fee_up, fee_dn, y, margin) -> pd.DataFrame:
    ok = np.isfinite(e_up) & np.isfinite(e_dn) & np.isfinite(y)
    up = ok & (e_up > margin) & (e_up >= e_dn)
    dn = ok & (e_dn > margin) & ~up
    side = np.where(up, 1, np.where(dn, -1, 0))
    price = np.where(up, px_up, np.where(dn, px_dn, np.nan))
    fee = np.where(up, fee_up, np.where(dn, fee_dn, np.nan))
    win = np.where(up, y, np.where(dn, 1.0 - y, np.nan))
    pnl = np.where(side != 0, win - price - fee, 0.0)
    pnl = np.where(ok, pnl, np.nan)
    edge = np.where(up, e_up, np.where(dn, e_dn, np.nan))
    return pd.DataFrame({"side": side, "price": price, "fee": fee, "edge": edge, "pnl": pnl, "win": win})


def pnl_by_margin(p_model: np.ndarray, p_mkt: np.ndarray, y: np.ndarray, margins: Sequence[float],
                  fee_rate=0.07, fee_exponent=1.0, mode: str = "taker",
                  mask: np.ndarray | None = None) -> pd.DataFrame:
    """Courbe P&L contre marge : ``n_trades``, ``pnl_total`` ($, 1 part par marché),
    ``pnl_per_trade``, ``win_rate``, ``avg_cost`` (prix + frais moyen des positions)."""
    rows = []
    msk = np.ones(len(np.asarray(y)), bool) if mask is None else np.asarray(mask, bool)
    for mg in margins:
        if mode == "taker":
            t = taker_trades(p_model, p_mkt, y, mg, fee_rate, fee_exponent)
        else:
            t = maker_trades(p_model, p_mkt, y, mg)
        t = t[msk]
        tr = t["side"] != 0
        n = int(tr.sum())
        rows.append({
            "margin": float(mg), "n_trades": n, "pnl_total": float(t["pnl"].sum()),
            "pnl_per_trade": float(t.loc[tr, "pnl"].mean()) if n else math.nan,
            "win_rate": float(t.loc[tr, "win"].mean()) if n else math.nan,
            "avg_cost": float((t.loc[tr, "price"] + t.loc[tr, "fee"]).mean()) if n else math.nan,
            "share_up": float((t.loc[tr, "side"] > 0).mean()) if n else math.nan,
        })
    return pd.DataFrame(rows)


def choose_margin(curve: pd.DataFrame, min_trades: int = 100) -> float:
    """Marge qui maximise le P&L total sur la période de validation, parmi celles qui gardent
    au moins ``min_trades`` positions (à égalité : la plus petite marge). NaN si aucune."""
    c = curve[curve["n_trades"] >= min_trades]
    if c.empty:
        return math.nan
    best = c["pnl_total"].max()
    return float(c.loc[c["pnl_total"] == best, "margin"].min())
