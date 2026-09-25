"""Données de marché : OHLCV crypto (Binance) et actions (Yahoo), dérivés, cache parquet.

Crypto (prioritaire)
====================
Paires spot Binance (``BTCUSDT``, ``SOLUSDT``…), trois sources complémentaires :

1. **Zips mensuels** ``data.binance.vision/data/spot/monthly/klines/…`` pour les mois
   révolus (publiés 0,3 à 12 jours après la fin du mois ; sinon on passe aux journaliers) ;
2. **Zips journaliers** ``…/spot/daily/klines/…`` pour le mois en cours et les mois partiels
   (publiés ≈ 2 h après minuit UTC : un fichier absent (404) = pas encore publié, pas une erreur) ;
3. **API** ``data-api.binance.vision/api/v3/klines`` pour les dernières heures
   (1000 barres par requête, poids 2, plafond 6000/min partagé par l'IP de sortie).

Pièges de format gérés : les fichiers *spot* n'ont pas d'en-tête et sont horodatés en
**microsecondes** (16 chiffres) depuis 2025, contre des millisecondes (13 chiffres) avant
2025 et dans l'API ; les fichiers *futures* ont un en-tête. L'unité est détectée valeur par
valeur d'après la magnitude, l'en-tête d'après la première ligne.

Format rendu (contrat ``docs/ARCHITECTURE.md``) : index ``open_time`` (UTC, ns, trié, sans
doublon, horodaté à l'**ouverture** de la barre), colonnes float64 ``open, high, low, close,
volume, quote_volume, trades, taker_buy_volume``. La barre en cours (non clôturée) est
toujours supprimée. Aucune barre n'est inventée : un trou de l'exchange reste un trou
(``targets.future_log_return`` rend alors la cible NaN).

Dérivés (futures USDⓈ-M, optionnels)
====================================
* ``metrics`` journaliers (pas de 5 min) : open interest, ratios long/short, ratio taker ;
* ``fundingRate`` mensuels (toutes les 8 h ; le mois en cours n'existe jamais en bulk,
  donc ``funding_rate`` est NaN sur les barres du mois courant).
L'index de :func:`fetch_derivatives` est l'horodatage de **disponibilité** de la mesure ;
:func:`attach_derivatives` les aligne de façon causale (voir sa docstring).

Actions (secondaire)
====================
Yahoo Finance via ``yfinance`` : 1 min limité aux 30 derniers jours, par tranches de 7 jours,
``auto_adjust=True``, ``prepost=False`` ; index dans le fuseau de la place (New York, Paris).

Cache
=====
``data/cache/{ticker}_{interval}.parquet`` et ``data/cache/{symbol}_derivs.parquet``. Chaque
fichier mémorise dans ses métadonnées parquet les intervalles de temps **déjà traités** : un
appel ne télécharge que ce qui manque (début et/ou fin), puis fusionne. Les trous réels de
l'exchange ne provoquent donc pas de nouveaux téléchargements.

Aucun appel réseau n'est fait ailleurs que dans ce module (et le chargement des poids TimesFM).
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from .config import CACHE_DIR, is_crypto, market_of

log = logging.getLogger(__name__)

__all__ = [
    "fetch_ohlcv",
    "fetch_derivatives",
    "attach_derivatives",
    "clean_ohlcv",
    "add_session",
    "load_universe",
    "klines_to_frame",
    "interval_to_timedelta",
    "DataSourceError",
    "OHLCV_COLUMNS",
    "CRYPTO_COLUMNS",
    "DERIVATIVE_COLUMNS",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

BULK_URL = "https://data.binance.vision/data"
API_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
API_MAX_LIMIT = 1000
# Plafond REQUEST_WEIGHT de data-api : 6000 / min / IP. Le compteur est partagé par tous les
# processus qui sortent par la même IP : on fait une pause dès 80 % du plafond.
API_WEIGHT_PAUSE = 4800

HTTP_TIMEOUT = (10.0, 90.0)  # (connexion, lecture) en secondes
HTTP_RETRIES = 5
DOWNLOAD_WORKERS = 6
# Un mois révolu dont on veut au moins ce nombre de jours est lu dans le zip mensuel
# (un seul fichier) plutôt que dans les zips journaliers.
MONTHLY_MIN_DAYS = 10
# Un fichier bulk encore absent ce délai après la fin de sa période n'existera pas
# (symbole pas encore coté…) : la période est alors considérée comme traitée.
PERMANENT_ABSENCE_DELAY = pd.Timedelta(days=7)

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
CRYPTO_COLUMNS: tuple[str, ...] = OHLCV_COLUMNS + ("quote_volume", "trades", "taker_buy_volume")

# Les 12 champs d'une kline Binance (bulk spot, bulk futures et API), par position.
KLINE_FIELDS: tuple[str, ...] = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)

# ----- CSV `metrics` des futures USDⓈ-M ------------------------------------------------
# Un fichier par jour UTC, 288 lignes (pas de 5 min), en-tête présent, lignes NON triées,
# quelques doublons en 2021. Sémantique des colonnes (vérifiée le 25/09/2026 sur BTCUSDT) :
#
# * ``create_time`` — chaîne "YYYY-MM-DD HH:MM:SS" (UTC) = **début** T de la fenêtre de
#   5 min [T, T + 5 min) décrite par la ligne. Le fichier du jour D couvre T = D 00:00 → 23:55.
#   Preuves : (a) ``sum_taker_long_short_vol_ratio`` en T égale taker_buy / taker_sell des
#   klines futures 1 min sur [T, T + 5 min) (erreur relative médiane 0,02 %, corrélation
#   0,9993) et non sur [T − 5 min, T) (corrélation 0,17) ; (b) le prix implicite
#   ``sum_open_interest_value / sum_open_interest`` égale la clôture de la barre T + 4 min,
#   donc le prix en T + 5 min (écart médian 0,2 pb, contre 5 pb avec le prix en T).
#   ⇒ La ligne n'est complète qu'en **T + 5 min** : disponibilité = create_time + 5 min.
# * ``symbol`` — le contrat perpétuel (même nom que la paire spot pour BTC, SOL, ETH…).
# * ``sum_open_interest`` — open interest total (somme sur tous les comptes), en unités de
#   l'actif de base (BTC pour BTCUSDT), instantané en T + 5 min            → ``open_interest``
# * ``sum_open_interest_value`` — le même open interest valorisé en USDT (OI × prix)
#                                                                        → ``open_interest_value``
# * ``count_toptrader_long_short_ratio`` — top traders (les 20 % de comptes au plus gros
#   solde de marge), ratio en **nombre de comptes** nets longs / nets courts (endpoint API
#   ``topLongShortAccountRatio``)                                          → ``ls_ratio_top_accounts``
# * ``sum_toptrader_long_short_ratio`` — top traders, ratio en **taille de positions**
#   (somme des positions longues / courtes ; ``topLongShortPositionRatio``) → ``ls_ratio_top``
# * ``count_long_short_ratio`` — **tous les comptes**, ratio en nombre de comptes nets
#   longs / nets courts (``globalLongShortAccountRatio``)                  → ``ls_ratio_accounts``
# * ``sum_taker_long_short_vol_ratio`` — volume acheteur taker / volume vendeur taker sur
#   [T, T + 5 min) (``takerlongshortRatio``, champ buySellRatio)           → ``taker_ls_ratio``
#
# « count » = compté en comptes, « sum » = somme de quantités : déduit des noms et des ordres
# de grandeur (BTC : ≈ 1,1 contre ≈ 1,9), Binance ne documente pas ces fichiers. Colonnes
# ``*toptrader*`` vides presque toute l'année 2022, taker vide de 12/2021 à 05/2022.
METRICS_CSV_FIELDS: tuple[str, ...] = (
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
)
METRICS_RENAME: dict[str, str] = {
    "sum_open_interest": "open_interest",
    "sum_open_interest_value": "open_interest_value",
    "sum_toptrader_long_short_ratio": "ls_ratio_top",
    "count_long_short_ratio": "ls_ratio_accounts",
    "count_toptrader_long_short_ratio": "ls_ratio_top_accounts",
    "sum_taker_long_short_vol_ratio": "taker_ls_ratio",
}
METRICS_AVAILABILITY_LAG = pd.Timedelta(minutes=5)

# ----- CSV `fundingRate` (mensuel) ------------------------------------------------------
# ``calc_time`` (ms, peut dévier de ±1 ms) = instant de règlement du funding ;
# ``funding_interval_hours`` (8 pour les 6 grosses paires) ; ``last_funding_rate`` = taux
# réglé à cet instant, connu au plus tard à ``calc_time`` → disponibilité = calc_time
# (arrondi à la seconde supérieure, par prudence, pour absorber la dérive de +1 ms).
FUNDING_CSV_FIELDS: tuple[str, ...] = ("calc_time", "funding_interval_hours", "last_funding_rate")

METRIC_COLUMNS: tuple[str, ...] = (
    "open_interest", "open_interest_value", "ls_ratio_top", "ls_ratio_accounts",
    "ls_ratio_top_accounts", "taker_ls_ratio",
)
FUNDING_COLUMNS: tuple[str, ...] = ("funding_rate",)
DERIVATIVE_COLUMNS: tuple[str, ...] = FUNDING_COLUMNS + METRIC_COLUMNS
# Ancienneté maximale d'une mesure attachée à une barre (au-delà : NaN).
METRICS_TOLERANCE = pd.Timedelta(hours=1)
FUNDING_TOLERANCE = pd.Timedelta(hours=9)

_BINANCE_INTERVALS: dict[str, pd.Timedelta] = {
    "1s": pd.Timedelta(seconds=1), "1m": pd.Timedelta(minutes=1), "3m": pd.Timedelta(minutes=3),
    "5m": pd.Timedelta(minutes=5), "15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1), "2h": pd.Timedelta(hours=2), "4h": pd.Timedelta(hours=4),
    "6h": pd.Timedelta(hours=6), "8h": pd.Timedelta(hours=8), "12h": pd.Timedelta(hours=12),
    "1d": pd.Timedelta(days=1),
}
_YAHOO_INTERVALS: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1), "2m": pd.Timedelta(minutes=2), "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30),
    "60m": pd.Timedelta(hours=1), "90m": pd.Timedelta(minutes=90), "1h": pd.Timedelta(hours=1),
    "1d": pd.Timedelta(days=1),
}
# Profondeur d'historique Yahoo par intervalle et taille maximale d'une requête.
_YAHOO_LOOKBACK: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(days=29, hours=23),
    "2m": pd.Timedelta(days=59), "5m": pd.Timedelta(days=59), "15m": pd.Timedelta(days=59),
    "30m": pd.Timedelta(days=59), "90m": pd.Timedelta(days=59),
    "60m": pd.Timedelta(days=729), "1h": pd.Timedelta(days=729),
    "1d": pd.Timedelta(days=365 * 50),
}
_YAHOO_CHUNK: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(days=7),
    "2m": pd.Timedelta(days=59), "5m": pd.Timedelta(days=59), "15m": pd.Timedelta(days=59),
    "30m": pd.Timedelta(days=59), "90m": pd.Timedelta(days=59),
    "60m": pd.Timedelta(days=365), "1h": pd.Timedelta(days=365),
    "1d": pd.Timedelta(days=365 * 50),
}
_MARKET_TZ: dict[str, str] = {"US": "America/New_York", "FR": "Europe/Paris", "CRYPTO": "UTC"}

_CACHE_META_KEY = b"tradebot"
_CACHE_VERSION = 1


class DataSourceError(RuntimeError):
    """Échec d'une source de données (HTTP, format inattendu, symbole inconnu…)."""


# ---------------------------------------------------------------------------
# Outils temps / intervalles
# ---------------------------------------------------------------------------

def _utcnow() -> pd.Timestamp:
    """Instant présent en UTC (fonction isolée pour pouvoir la figer dans les tests)."""
    return pd.Timestamp.now(tz="UTC")


def _to_utc(ts) -> pd.Timestamp:
    """Timestamp UTC ; une date ou un horodatage sans fuseau est supposé en UTC."""
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def interval_to_timedelta(interval: str, crypto: bool = True) -> pd.Timedelta:
    """Durée d'une barre pour un intervalle Binance (``crypto=True``) ou Yahoo."""
    table = _BINANCE_INTERVALS if crypto else _YAHOO_INTERVALS
    try:
        return table[interval]
    except KeyError:
        raise ValueError(
            f"intervalle {interval!r} non pris en charge ({'Binance' if crypto else 'Yahoo'} : "
            f"{', '.join(table)})") from None


def _floor(ts: pd.Timestamp, step: pd.Timedelta) -> pd.Timestamp:
    """Arrondi inférieur d'un instant UTC à un multiple de ``step`` depuis l'époque Unix."""
    step_ns = step.value
    return pd.Timestamp(ts.value - ts.value % step_ns, unit="ns", tz="UTC")


def _month_start(ts: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(year=ts.year, month=ts.month, day=1, tz="UTC")


def _next_month(ts: pd.Timestamp) -> pd.Timestamp:
    return _month_start(ts) + pd.offsets.MonthBegin(1)


def _epoch_to_datetime(values) -> pd.DatetimeIndex:
    """Horodatages epoch -> DatetimeIndex UTC (ns), unité détectée par la magnitude.

    s (10 chiffres), ms (13 chiffres, API et bulk avant 2025), µs (16 chiffres, bulk spot
    depuis 2025) ou ns (19 chiffres) ; la détection est faite valeur par valeur, ce qui
    tolère un mélange d'unités après concaténation.
    """
    v = np.asarray(pd.to_numeric(pd.Series(values)).to_numpy(), dtype="int64")
    ns = np.where(v >= 10**17, v,
                  np.where(v >= 10**14, v * 1_000,
                           np.where(v >= 10**11, v * 1_000_000, v * 1_000_000_000)))
    return pd.DatetimeIndex(pd.to_datetime(ns, unit="ns", utc=True))


Interval = tuple[pd.Timestamp, pd.Timestamp]  # [début, fin)


def _ivl_union(ivls: Iterable[Interval]) -> list[Interval]:
    """Union d'intervalles semi-ouverts, triée, sans chevauchement ni contiguïté."""
    out: list[Interval] = []
    for s, e in sorted((s, e) for s, e in ivls if s < e):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _ivl_subtract(target: Interval, covered: Iterable[Interval]) -> list[Interval]:
    """Parties de ``target`` non couvertes par ``covered``."""
    s, e = target
    missing: list[Interval] = []
    cur = s
    for cs, ce in _ivl_union(covered):
        if ce <= cur:
            continue
        if cs >= e:
            break
        if cs > cur:
            missing.append((cur, min(cs, e)))
        cur = max(cur, ce)
        if cur >= e:
            break
    if cur < e:
        missing.append((cur, e))
    return missing


def _ivl_to_json(ivls: Iterable[Interval]) -> list[list[str]]:
    return [[s.isoformat(), e.isoformat()] for s, e in _ivl_union(ivls)]


def _ivl_from_json(raw) -> list[Interval]:
    return _ivl_union((_to_utc(s), _to_utc(e)) for s, e in (raw or []))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_tls = threading.local()


def _session() -> requests.Session:
    """Une session HTTP par thread (connexions réutilisées, pas de partage entre threads)."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "tradebot/0.1 (recherche ; phase 1)"
        _tls.session = s
    return s


def _http_get(url: str, params: dict | None = None, allow_404: bool = True):
    """GET avec retentatives (backoff exponentiel + aléa) et délais d'attente.

    Renvoie la réponse (statut 200), ou ``None`` si 404 et ``allow_404`` (fichier bulk pas
    encore publié ou inexistant). 429 : attend ``Retry-After`` puis réessaie ; 418 (IP
    bannie par Binance), 403 et 451 (restriction géographique) : erreur immédiate.
    """
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = _session().get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_error = exc
            log.debug("GET %s : %s (tentative %d/%d)", url, exc, attempt, HTTP_RETRIES)
        else:
            if r.status_code == 200:
                return r
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code in (403, 418, 451):
                raise DataSourceError(
                    f"HTTP {r.status_code} pour {url} (accès refusé / IP bannie / restriction "
                    f"géographique) : {r.text[:200]}")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = DataSourceError(f"HTTP {r.status_code} pour {url}")
                retry_after = r.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait = min(float(retry_after), 120.0)
                    except ValueError:
                        wait = delay
                    log.warning("HTTP %d sur %s : pause de %.0f s", r.status_code, url, wait)
                    time.sleep(wait)
                    continue
            else:
                raise DataSourceError(f"HTTP {r.status_code} pour {url} : {r.text[:200]}")
        if attempt < HTTP_RETRIES:
            time.sleep(delay + random.uniform(0.0, 0.5))
            delay = min(delay * 2.0, 30.0)
    raise DataSourceError(f"échec après {HTTP_RETRIES} tentatives : {url}") from last_error


def _download_zip(url: str) -> bytes | None:
    """Contenu d'un zip bulk, ou None s'il n'est pas (encore) publié."""
    r = _http_get(url)
    return None if r is None else r.content


def _csv_bytes_from_zip(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
        if not names:
            raise DataSourceError("zip vide")
        return zf.read(names[0])


def _has_header(raw: bytes) -> bool:
    """Vrai si la première ligne du CSV est un en-tête (premier champ non numérique)."""
    first = raw.lstrip()[:128].split(b"\n", 1)[0].split(b",", 1)[0].strip().strip(b'"')
    if not first:
        return False
    try:
        float(first)
    except ValueError:
        return True
    return False


def _read_csv(raw: bytes, fields: Sequence[str], by_header: bool = False) -> pd.DataFrame:
    """Lit un CSV Binance avec ou sans ligne d'en-tête.

    ``by_header=False`` : colonnes nommées par position selon ``fields`` (klines : l'en-tête
    des futures dit ``count`` là où l'on veut ``trades``). ``by_header=True`` : colonnes lues
    par leur nom dans l'en-tête s'il existe (robuste à un réordonnancement), sinon par position.
    """
    if not raw.strip():
        return pd.DataFrame(columns=list(fields))
    header = _has_header(raw)
    if header and by_header:
        return pd.read_csv(io.BytesIO(raw))
    return pd.read_csv(io.BytesIO(raw), header=None, names=list(fields),
                       skiprows=1 if header else 0)


# ---------------------------------------------------------------------------
# Klines Binance
# ---------------------------------------------------------------------------

def _empty_frame(columns: Sequence[str], tz: str = "UTC") -> pd.DataFrame:
    idx = pd.DatetimeIndex([], dtype=f"datetime64[ns, {tz}]", name="open_time")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in columns}, index=idx)


def klines_to_frame(raw, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Klines Binance brutes -> OHLCV au format du contrat.

    ``raw`` : DataFrame aux 12 colonnes ``KLINE_FIELDS`` (CSV bulk) ou liste de listes
    (réponse JSON de l'API). Horodatages en ms ou µs (détection automatique). Si ``now`` est
    donné, supprime les barres non clôturées (``close_time >= now``).
    """
    if not isinstance(raw, pd.DataFrame):
        rows = list(raw)
        if not rows:
            return _empty_frame(CRYPTO_COLUMNS)
        raw = pd.DataFrame(rows, columns=list(KLINE_FIELDS[: len(rows[0])]))
    if raw.empty:
        return _empty_frame(CRYPTO_COLUMNS)
    open_time = _epoch_to_datetime(raw["open_time"]).rename("open_time")
    data = {c: pd.to_numeric(raw[c], errors="coerce").astype("float64").to_numpy()
            for c in CRYPTO_COLUMNS}
    out = pd.DataFrame(data, index=open_time)
    if now is not None:
        close_time = _epoch_to_datetime(raw["close_time"])
        out = out[np.asarray(close_time < _to_utc(now))]
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def _kline_url(symbol: str, interval: str, period: str, daily: bool) -> str:
    freq = "daily" if daily else "monthly"
    return f"{BULK_URL}/spot/{freq}/klines/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"


def _download_kline_zip(url: str) -> pd.DataFrame | None:
    content = _download_zip(url)
    if content is None:
        return None
    return klines_to_frame(_read_csv(_csv_bytes_from_zip(content), KLINE_FIELDS))


def _api_klines_page(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Une page de l'API klines (<= 1000 barres) ; pause si le poids consommé approche du plafond."""
    params = {"symbol": symbol, "interval": interval, "startTime": int(start_ms),
              "endTime": int(end_ms), "limit": API_MAX_LIMIT}
    r = _http_get(API_KLINES_URL, params=params, allow_404=False)
    used = r.headers.get("x-mbx-used-weight-1m") or r.headers.get("x-mbx-used-weight")
    try:
        used_weight = int(used) if used is not None else 0
    except ValueError:
        used_weight = 0
    if used_weight >= API_WEIGHT_PAUSE:
        wait = 61.0 - time.time() % 60.0
        log.warning("poids API Binance %d/6000 : pause de %.0f s", used_weight, wait)
        time.sleep(wait)
    return r.json()


def _fetch_api_klines(symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp,
                      now: pd.Timestamp) -> pd.DataFrame:
    """Barres clôturées d'ouverture dans [start, end) via l'API (pagination par 1000)."""
    bar = interval_to_timedelta(interval)
    start_ms = start.value // 1_000_000
    end_ms = end.value // 1_000_000 - 1
    rows: list = []
    cur = start_ms
    while cur <= end_ms:
        page = _api_klines_page(symbol, interval, cur, end_ms)
        if not page:
            break
        rows.extend(page)
        last_open = _epoch_to_datetime([page[-1][0]])[0]
        nxt = (last_open + bar).value // 1_000_000
        if len(page) < API_MAX_LIMIT or nxt <= cur:
            break
        cur = nxt
    df = klines_to_frame(rows, now=now)
    return df[(df.index >= start) & (df.index < end)]


def _run_parallel(func: Callable, items: Sequence) -> list:
    if not items:
        return []
    if len(items) == 1:
        return [func(items[0])]
    with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(items))) as pool:
        return list(pool.map(func, items))


def _fetch_binance_range(symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp,
                         now: pd.Timestamp) -> pd.DataFrame:
    """Barres clôturées d'ouverture dans [start, end) : zips bulk puis API pour la fin."""
    bar = interval_to_timedelta(interval)
    one_day = pd.Timedelta(days=1)
    days = [d for d in pd.date_range(start.floor("D"), (end - pd.Timedelta(1, "ns")).floor("D"),
                                     freq="D")
            if d + one_day <= now]  # seuls les jours révolus peuvent avoir un zip
    by_month: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    for d in days:
        by_month.setdefault(_month_start(d), []).append(d)

    monthly: list[tuple[pd.Timestamp, list[pd.Timestamp]]] = []
    daily: list[pd.Timestamp] = []
    for m0, ds in by_month.items():
        month_over = _next_month(m0) <= now
        if month_over and len(ds) >= MONTHLY_MIN_DAYS:
            monthly.append((m0, ds))
        else:
            daily.extend(ds)

    frames: list[pd.DataFrame] = []
    m_urls = [_kline_url(symbol, interval, f"{m0:%Y-%m}", daily=False) for m0, _ in monthly]
    for (m0, ds), df in zip(monthly, _run_parallel(_download_kline_zip, m_urls)):
        if df is None:  # zip mensuel pas encore publié -> zips journaliers
            log.info("%s %s : zip mensuel %s absent, repli sur les journaliers",
                     symbol, interval, f"{m0:%Y-%m}")
            daily.extend(ds)
        else:
            frames.append(df)
    daily.sort()
    d_urls = [_kline_url(symbol, interval, f"{d:%Y-%m-%d}", daily=True) for d in daily]
    for d, df in zip(daily, _run_parallel(_download_kline_zip, d_urls)):
        if df is None:
            log.debug("%s %s : zip journalier %s absent", symbol, interval, f"{d:%Y-%m-%d}")
        else:
            frames.append(df)

    bulk = pd.concat(frames) if frames else _empty_frame(CRYPTO_COLUMNS)
    bulk = bulk[(bulk.index >= start) & (bulk.index < end)]
    # Complément API : après la dernière barre bulk (jours pas encore publiés, jour courant).
    api_start = bulk.index.max() + bar if len(bulk) else start
    if api_start < end:
        log.info("%s %s : API klines de %s à %s", symbol, interval, api_start, end)
        api = _fetch_api_klines(symbol, interval, api_start, end, now)
        bulk = pd.concat([bulk, api]) if len(bulk) else api
    bulk = bulk[~bulk.index.duplicated(keep="last")].sort_index()
    bulk.index = bulk.index.as_unit("ns").rename("open_time")
    return bulk


# ---------------------------------------------------------------------------
# Yahoo (actions)
# ---------------------------------------------------------------------------

def _normalize_yahoo(raw: pd.DataFrame | None, ticker: str) -> pd.DataFrame:
    """Sortie de ``yf.download`` -> OHLCV (colonnes simples, fuseau de la place)."""
    tz = _MARKET_TZ[market_of(ticker)]
    if raw is None or raw.empty:
        return _empty_frame(OHLCV_COLUMNS, tz)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        level = next((i for i in range(df.columns.nlevels)
                      if "Close" in set(df.columns.get_level_values(i))), 0)
        if df.columns.nlevels > 1:
            other = [i for i in range(df.columns.nlevels) if i != level]
            tick_vals = set(df.columns.get_level_values(other[0]))
            if ticker in tick_vals:
                df = df.xs(ticker, axis=1, level=other[0])
            else:
                df.columns = df.columns.get_level_values(level)
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise DataSourceError(f"{ticker} : colonnes Yahoo manquantes {missing}")
    df = df[list(OHLCV_COLUMNS)].astype("float64")
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(tz).as_unit("ns").rename("open_time")
    return df


def _fetch_yahoo_range(ticker: str, interval: str, start: pd.Timestamp, end: pd.Timestamp,
                       now: pd.Timestamp) -> pd.DataFrame:
    """Barres Yahoo clôturées dans [start, end), par tranches (7 jours en 1 min)."""
    import yfinance as yf  # import paresseux : lent et inutile pour la crypto

    bar = interval_to_timedelta(interval, crypto=False)
    chunk = _YAHOO_CHUNK[interval]
    frames = []
    cur = start
    while cur < end:
        nxt = min(cur + chunk, end)
        raw = None
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                raw = yf.download(ticker, start=cur.to_pydatetime(), end=nxt.to_pydatetime(),
                                  interval=interval, auto_adjust=True, prepost=False,
                                  progress=False, threads=False)
                break
            except Exception as exc:  # yfinance lève des erreurs variées
                if attempt == HTTP_RETRIES:
                    raise DataSourceError(f"Yahoo {ticker} : {exc}") from exc
                time.sleep(2.0 ** attempt)
        frames.append(_normalize_yahoo(raw, ticker))
        cur = nxt
    df = pd.concat(frames) if frames else _empty_frame(OHLCV_COLUMNS, _MARKET_TZ[market_of(ticker)])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= start) & (df.index < end) & (df.index + bar <= now)]
    if df.empty and end - start >= pd.Timedelta(days=5):
        raise DataSourceError(f"Yahoo : aucune barre {interval} pour {ticker} entre {start} et {end}")
    return df


# ---------------------------------------------------------------------------
# Cache parquet
# ---------------------------------------------------------------------------

def _cache_path(name: str) -> Path:
    safe = name.replace("/", "_").replace("^", "")
    return Path(CACHE_DIR) / f"{safe}.parquet"


def _read_cache(path: Path) -> tuple[pd.DataFrame | None, dict]:
    if not path.exists():
        return None, {}
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(path)
    except Exception as exc:  # fichier corrompu / écrit à moitié : on repart de zéro
        log.warning("cache illisible %s (%s) : ignoré", path, exc)
        return None, {}
    raw_meta = (table.schema.metadata or {}).get(_CACHE_META_KEY)
    meta = json.loads(raw_meta) if raw_meta else {}
    df = table.to_pandas()
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.as_unit("ns")
    return df, meta


def _write_cache(path: Path, df: pd.DataFrame, meta: dict) -> None:
    """Écriture atomique (fichier temporaire puis renommage) avec métadonnées."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=True)
    md = dict(table.schema.metadata or {})
    md[_CACHE_META_KEY] = json.dumps({"version": _CACHE_VERSION, **meta}).encode()
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}_{threading.get_ident()}")
    pq.write_table(table.replace_schema_metadata(md), tmp, compression="zstd")
    os.replace(tmp, path)


def _merge_frames(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or old.empty:
        out = new
    elif new.empty:
        out = old
    else:
        out = pd.concat([old, new])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _cached_range_fetch(path: Path, target: Interval, fetch: Callable[[pd.Timestamp, pd.Timestamp],
                        pd.DataFrame], use_cache: bool, empty: pd.DataFrame, label: str
                        ) -> pd.DataFrame:
    """Données couvrant ``target`` en ne téléchargeant que les intervalles absents du cache."""
    a, b = target
    if a >= b:
        cached = _read_cache(path)[0] if use_cache else None
        return cached if cached is not None else empty
    if not use_cache:
        return fetch(a, b)
    cached, meta = _read_cache(path)
    covered = _ivl_from_json(meta.get("covered"))
    if cached is not None and len(cached) and not covered:
        # cache sans métadonnées (ancien format) : on suppose [premier jour, dernière barre]
        covered = [(cached.index.min().tz_convert("UTC").floor("D"),
                    cached.index.max().tz_convert("UTC") + pd.Timedelta(1, "ns"))]
    missing = _ivl_subtract((a, b), covered)
    for s, e in missing:
        log.info("%s : téléchargement de %s à %s", label, s, e)
        new = fetch(s, e)
        cached = _merge_frames(cached, new)
        covered = _ivl_union(covered + [(s, e)])
        _write_cache(path, cached, {"covered": _ivl_to_json(covered)})
    return cached if cached is not None else empty


# ---------------------------------------------------------------------------
# API publique : OHLCV
# ---------------------------------------------------------------------------

def fetch_ohlcv(ticker: str, interval: str = "1m", days: int = 365,
                cache: bool = True, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """OHLCV des ``days`` derniers jours jusqu'à ``end`` (défaut : maintenant), barres clôturées.

    Crypto (``config.is_crypto``) : zips mensuels pour les mois révolus, zips journaliers
    pour le mois en cours (jusqu'à hier), API klines pour les dernières heures ; colonnes
    ``open, high, low, close, volume, quote_volume, trades, taker_buy_volume`` ; index UTC.
    Actions : Yahoo (1 min limité aux 30 derniers jours) ; colonnes OHLCV ; index dans le
    fuseau de la place.

    Le cache ``data/cache/{ticker}_{interval}.parquet`` est fusionné de façon incrémentale :
    seuls les intervalles jamais traités sont téléchargés. Le cache crypto couvre des jours
    UTC entiers (plus la journée en cours jusqu'à la dernière barre clôturée).
    """
    if days <= 0:
        raise ValueError("days doit être > 0")
    crypto = is_crypto(ticker)
    now = _utcnow()
    end_ts = now if end is None else min(_to_utc(end), now)
    start_ts = end_ts - pd.Timedelta(days=days)
    bar = interval_to_timedelta(interval, crypto=crypto)

    if crypto:
        symbol = ticker.strip().upper()
        a = start_ts.floor("D")
        b = min(end_ts.ceil("D"), _floor(now, bar))
        fetch = lambda s, e: _fetch_binance_range(symbol, interval, s, e, now)  # noqa: E731
        empty = _empty_frame(CRYPTO_COLUMNS)
        name = f"{symbol}_{interval}"
    else:
        symbol = ticker.strip()
        if interval not in _YAHOO_LOOKBACK:
            raise ValueError(f"intervalle Yahoo non pris en charge : {interval!r}")
        earliest = now - _YAHOO_LOOKBACK[interval]
        if start_ts < earliest:
            log.warning("%s : Yahoo ne fournit le %s que sur %s ; début ramené à %s",
                        symbol, interval, _YAHOO_LOOKBACK[interval], earliest.floor("min"))
        a = max(start_ts, earliest).floor("min")
        b = _floor(now, bar) if end is None else min(end_ts, _floor(now, bar))
        fetch = lambda s, e: _fetch_yahoo_range(symbol, interval, s, e, now)  # noqa: E731
        empty = _empty_frame(OHLCV_COLUMNS, _MARKET_TZ[market_of(symbol)])
        name = f"{symbol}_{interval}"

    df = _cached_range_fetch(_cache_path(name), (a, b), fetch, cache, empty, name)
    out = df[(df.index >= start_ts) & (df.index < end_ts) & (df.index + bar <= now)]
    return out.copy()


# ---------------------------------------------------------------------------
# API publique : dérivés
# ---------------------------------------------------------------------------

def _metrics_to_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """CSV ``metrics`` -> index = disponibilité (create_time + 5 min), colonnes renommées."""
    cols = list(METRICS_RENAME.values())
    if raw.empty:
        out = _empty_frame(cols)
        out.index.name = "available_time"
        return out
    ct = raw["create_time"]
    if pd.api.types.is_numeric_dtype(ct):
        created = _epoch_to_datetime(ct)
    else:
        created = pd.DatetimeIndex(pd.to_datetime(ct, utc=True, format="mixed")).as_unit("ns")
    data = {new: pd.to_numeric(raw[old], errors="coerce").astype("float64").to_numpy()
            if old in raw.columns else np.full(len(raw), np.nan)
            for old, new in METRICS_RENAME.items()}
    out = pd.DataFrame(data, index=(created + METRICS_AVAILABILITY_LAG).rename("available_time"))
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]


def _funding_to_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """CSV ``fundingRate`` -> index = disponibilité (calc_time arrondi à la seconde sup.)."""
    if raw.empty:
        out = _empty_frame(FUNDING_COLUMNS)
        out.index.name = "available_time"
        return out
    avail = _epoch_to_datetime(raw["calc_time"]).ceil("s")
    out = pd.DataFrame(
        {"funding_rate": pd.to_numeric(raw["last_funding_rate"], errors="coerce")
         .astype("float64").to_numpy()},
        index=avail.rename("available_time"))
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]


def _metrics_url(symbol: str, day: pd.Timestamp) -> str:
    return f"{BULK_URL}/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day:%Y-%m-%d}.zip"


def _funding_url(symbol: str, month: pd.Timestamp) -> str:
    return f"{BULK_URL}/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month:%Y-%m}.zip"


def _download_metrics(url: str) -> pd.DataFrame | None:
    content = _download_zip(url)
    if content is None:
        return None
    return _metrics_to_frame(_read_csv(_csv_bytes_from_zip(content), METRICS_CSV_FIELDS,
                                       by_header=True))


def _download_funding(url: str) -> pd.DataFrame | None:
    content = _download_zip(url)
    if content is None:
        return None
    return _funding_to_frame(_read_csv(_csv_bytes_from_zip(content), FUNDING_CSV_FIELDS,
                                       by_header=True))


def _combine_derivs(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Réunit lignes metrics et funding (horodatages distincts) ; colonnes du contrat."""
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        out = _empty_frame(DERIVATIVE_COLUMNS)
        out.index.name = "available_time"
        return out
    df = pd.concat(frames)
    # une même seconde peut porter une ligne metrics et une ligne funding : on les fusionne
    # (``last`` ignore les NaN, colonne par colonne)
    if df.index.has_duplicates:
        df = df.groupby(level=0, sort=True).last()
    df = df.reindex(columns=list(DERIVATIVE_COLUMNS)).astype("float64").sort_index()
    df.index = pd.DatetimeIndex(df.index).as_unit("ns").rename("available_time")
    return df


def _fetch_periods(symbol: str, periods: list[pd.Timestamp], url_of: Callable,
                   loader: Callable, period_end: Callable, now: pd.Timestamp
                   ) -> tuple[list[pd.DataFrame], list[Interval]]:
    """Télécharge des fichiers bulk périodiques ; renvoie les données et les périodes traitées.

    Une période est « traitée » si son fichier existe, ou s'il manque encore
    ``PERMANENT_ABSENCE_DELAY`` après sa fin (il n'existera pas). Un fichier récent absent
    (pas encore publié) laisse la période non traitée : elle sera retentée au prochain appel.
    """
    results = _run_parallel(loader, [url_of(symbol, p) for p in periods])
    frames, done = [], []
    for p, df in zip(periods, results):
        if df is not None:
            frames.append(df)
            done.append((p, period_end(p)))
        elif period_end(p) + PERMANENT_ABSENCE_DELAY <= now:
            done.append((p, period_end(p)))
    return frames, done


def fetch_derivatives(symbol: str, start, end, cache: bool = True) -> pd.DataFrame:
    """Dérivés futures USDⓈ-M de ``symbol`` utiles aux barres de [start, end).

    Index UTC = horodatage de **disponibilité** de la mesure (``available_time``) :
    ``create_time + 5 min`` pour les ``metrics`` (fin de la fenêtre de 5 min qu'elles
    décrivent), ``calc_time`` (arrondi à la seconde supérieure) pour le funding.
    Colonnes : ``funding_rate, open_interest, open_interest_value, ls_ratio_top,
    ls_ratio_accounts, ls_ratio_top_accounts, taker_ls_ratio`` (sémantique détaillée en tête
    de module, voir ``METRICS_RENAME``). Les lignes metrics ont ``funding_rate`` NaN et
    inversement. Le frame inclut les mesures disponibles jusqu'à 9 h avant ``start``
    (nécessaires à l'alignement as-of des premières barres).

    Limites : ``metrics`` publiés ≈ 7 h après la fin du jour UTC (le jour en cours et souvent
    la veille manquent) ; ``fundingRate`` n'existe qu'en fichiers mensuels (mois en cours
    absent). Cache ``data/cache/{symbol}_derivs.parquet``, incrémental par jour / par mois.
    """
    symbol = symbol.strip().upper()
    now = _utcnow()
    start_ts = _to_utc(start)
    end_ts = min(_to_utc(end), now)
    one_day = pd.Timedelta(days=1)
    if start_ts >= end_ts:
        return _combine_derivs([])

    # jours metrics dont une mesure peut servir sur [start, end) : create_time dans
    # [start - tolérance - 5 min, end - 5 min) ; seuls les jours révolus ont un fichier.
    m_a = (start_ts - METRICS_TOLERANCE - METRICS_AVAILABILITY_LAG).floor("D")
    m_b = min((end_ts - METRICS_AVAILABILITY_LAG).floor("D") + one_day, now.floor("D"))
    # mois de funding : calc_time dans [start - 9 h, end) ; seuls les mois révolus existent.
    f_a = _month_start(start_ts - FUNDING_TOLERANCE)
    f_b = min(_next_month(end_ts - pd.Timedelta(1, "ns")), _month_start(now))

    path = _cache_path(f"{symbol}_derivs")
    cached, meta = _read_cache(path) if cache else (None, {})
    cov_m = _ivl_from_json(meta.get("metrics"))
    cov_f = _ivl_from_json(meta.get("funding"))

    new_frames: list[pd.DataFrame] = []
    changed = False
    if m_a < m_b:
        days = [d for s, e in _ivl_subtract((m_a, m_b), cov_m)
                for d in pd.date_range(s, e - one_day, freq="D")]
        if days:
            log.info("%s : %d fichiers metrics à télécharger (%s → %s)", symbol, len(days),
                     f"{days[0]:%Y-%m-%d}", f"{days[-1]:%Y-%m-%d}")
            frames, done = _fetch_periods(symbol, days, _metrics_url, _download_metrics,
                                          lambda d: d + one_day, now)
            new_frames += frames
            cov_m = _ivl_union(cov_m + done)
            changed = True
    if f_a < f_b:
        months = [m for s, e in _ivl_subtract((f_a, f_b), cov_f)
                  for m in pd.date_range(s, e - pd.Timedelta(1, "ns"), freq="MS")]
        if months:
            log.info("%s : %d fichiers fundingRate à télécharger", symbol, len(months))
            frames, done = _fetch_periods(symbol, months, _funding_url, _download_funding,
                                          _next_month, now)
            new_frames += frames
            cov_f = _ivl_union(cov_f + done)
            changed = True

    new = _combine_derivs(new_frames)
    if cache:
        deriv = _combine_derivs([cached, new]) if cached is not None else new
        if changed:
            _write_cache(path, deriv, {"metrics": _ivl_to_json(cov_m),
                                       "funding": _ivl_to_json(cov_f)})
    else:
        deriv = new
    lo = start_ts - max(METRICS_TOLERANCE, FUNDING_TOLERANCE)
    return deriv[(deriv.index >= lo) & (deriv.index < end_ts)].copy()


def attach_derivatives(df: pd.DataFrame, deriv: pd.DataFrame | None) -> pd.DataFrame:
    """Ajoute les colonnes dérivés à ``df`` par un merge *as-of* strictement causal.

    Convention (la plus prudente) : la barre d'ouverture ``t`` reçoit la dernière mesure
    dont l'horodatage de **disponibilité** ``A`` vérifie ``A <= t`` — c'est-à-dire
    disponible **au plus tard à l'ouverture** de la barre. Comme la barre ``t`` n'est
    exploitée qu'à sa clôture (``t + 1 min``), cela laisse une minute de marge. Pour les
    ``metrics`` (A = create_time + 5 min = fin de leur fenêtre), une mesure de la fenêtre
    [T, T + 5 min) sert à partir de la barre qui s'ouvre à T + 5 min, conformément au
    contrat ; le funding réglé à ``calc_time`` sert à partir de la barre qui s'ouvre à
    ``calc_time`` (arrondi à la seconde supérieure).

    Ancienneté maximale : 1 h pour les metrics, 9 h pour le funding (au-delà : NaN). Les
    colonnes metrics et funding sont alignées séparément (horodatages distincts). Une mesure
    présente mais NaN reste NaN : aucune valeur plus ancienne n'est propagée.
    Colonnes absentes de ``deriv`` (ou ``deriv`` vide / None) -> NaN.
    """
    out = df.drop(columns=[c for c in DERIVATIVE_COLUMNS if c in df.columns])
    if not out.index.is_monotonic_increasing:
        out = out.sort_index()
    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    left = pd.DataFrame({"_t": idx.tz_convert("UTC").as_unit("ns")})
    new_cols: dict[str, np.ndarray] = {}
    for cols, tol in ((FUNDING_COLUMNS, FUNDING_TOLERANCE), (METRIC_COLUMNS, METRICS_TOLERANCE)):
        present = [c for c in cols if deriv is not None and c in deriv.columns]
        for c in cols:
            new_cols[c] = np.full(len(out), np.nan)
        if not present or len(out) == 0:
            continue
        right = deriv[present].dropna(how="all")
        if right.empty:
            continue
        ridx = pd.DatetimeIndex(right.index)
        ridx = ridx.tz_localize("UTC") if ridx.tz is None else ridx
        right = right.set_axis(ridx.tz_convert("UTC").as_unit("ns").rename("_t"), axis=0)
        right = right[~right.index.duplicated(keep="last")].sort_index().reset_index()
        merged = pd.merge_asof(left, right, on="_t", direction="backward",
                               allow_exact_matches=True, tolerance=tol)
        for c in present:
            new_cols[c] = merged[c].to_numpy(dtype="float64")
    return out.assign(**new_cols)


# ---------------------------------------------------------------------------
# API publique : nettoyage, sessions, univers
# ---------------------------------------------------------------------------

def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Nettoyage minimal et sans invention de données.

    Index tz-aware (UTC si absent), trié, sans doublon (dernière occurrence gardée) ;
    colonnes numériques connues en float64 ; lignes à prix manquant ou <= 0 supprimées ;
    ``high``/``low`` rendus cohérents avec ``open``/``close`` ; volumes négatifs -> NaN.
    Les trous ne sont PAS comblés (les cibles les détectent). Autres colonnes conservées.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("l'index doit être un DatetimeIndex")
    out = df.copy()
    idx = out.index if out.index.tz is not None else out.index.tz_localize("UTC")
    out.index = idx.as_unit("ns").rename(df.index.name or "open_time")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    num = [c for c in CRYPTO_COLUMNS if c in out.columns]
    for c in num:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    price = [c for c in ("open", "high", "low", "close") if c in out.columns]
    if price:
        p = out[price]
        bad = p.isna().any(axis=1) | (p <= 0).any(axis=1)
        out = out[~bad.to_numpy()]
        if {"open", "high", "low", "close"} <= set(out.columns):
            p = out[["open", "high", "low", "close"]]
            out["high"] = p.max(axis=1)
            out["low"] = p.min(axis=1)
    for c in ("volume", "quote_volume", "trades", "taker_buy_volume"):
        if c in out.columns:
            out[c] = out[c].where(~(out[c] < 0))
    return out


def add_session(df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame:
    """Colonne ``session`` (``datetime.date``) : date UTC pour la crypto, date locale de la
    place pour les actions. Sans ``ticker`` : date dans le fuseau de l'index (UTC si absent).
    """
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    if ticker is not None:
        tz = _MARKET_TZ[market_of(ticker)]
    else:
        tz = idx.tz
    out = df.copy()
    out["session"] = idx.tz_convert(tz).date
    return out


def load_universe(tickers, interval: str = "1m", days: int = 365, cache: bool = True,
                  with_derivatives: bool = True) -> dict[str, pd.DataFrame]:
    """Charge plusieurs tickers : OHLCV nettoyé + session (+ dérivés alignés pour la crypto).

    Une erreur sur un ticker est journalisée et n'empêche pas les autres ; une erreur sur
    les seuls dérivés laisse le ticker avec des colonnes dérivés NaN. Renvoie
    ``{ticker: DataFrame}`` pour les tickers chargés avec succès.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        t0 = time.monotonic()
        try:
            df = clean_ohlcv(fetch_ohlcv(ticker, interval=interval, days=days, cache=cache))
            if df.empty:
                log.warning("%s : aucune barre %s sur %d jours", ticker, interval, days)
                continue
            if with_derivatives and is_crypto(ticker):
                deriv = None
                try:
                    bar = interval_to_timedelta(interval)
                    deriv = fetch_derivatives(ticker, df.index[0], df.index[-1] + bar, cache=cache)
                except Exception as exc:
                    log.warning("%s : dérivés indisponibles (%s) ; colonnes NaN", ticker, exc,
                                exc_info=True)
                df = attach_derivatives(df, deriv)
            out[ticker] = add_session(df, ticker)
            log.info("%s : %d barres %s chargées en %.1f s", ticker, len(df), interval,
                     time.monotonic() - t0)
        except Exception as exc:
            log.error("%s : échec du chargement (%s)", ticker, exc, exc_info=True)
    return out
