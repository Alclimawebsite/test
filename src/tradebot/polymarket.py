"""Client Polymarket en LECTURE SEULE pour les marchés crypto récurrents « Up or Down ».

Polymarket publie des marchés binaires « Up or Down » sur btc, eth, sol, xrp, bnb, doge,
hype et zec, par fenêtres de 5 min, 15 min et 4 h — exactement nos horizons de prévision.
Ce module sert à les lister, à récupérer leur résolution, l'historique de prix du jeton
« Up », le carnet d'ordres et les transactions, pour comparer nos probabilités de hausse
(indicateurs, TimesFM) aux probabilités implicites du marché.

**Aucune fonction de passage d'ordre, aucune clé privée** : uniquement des GET publics sur
``gamma-api`` (métadonnées), ``clob`` (prix, carnet) et ``data-api`` (transactions).

Faits vérifiés sur les API (septembre 2026)
-------------------------------------------
* Slug d'événement déterministe : ``{asset}-updown-{5m|15m|4h}-{début de fenêtre en s Unix}``.
  ``GET gamma-api/events?slug=...`` renvoie ``[]`` si le marché n'existe pas ; le paramètre
  ``slug`` peut être répété (≤ 100 par requête, penser à ``limit`` : 20 par défaut).
* Alignement : les fenêtres 5m/15m sont des multiples de la durée en UTC. Les fenêtres **4h
  sont alignées sur l'heure de New York** (00h, 04h, 08h, 12h, 16h, 20h ET) : 00h/04h…
  UTC en heure d'été (EDT), 01h/05h… UTC en heure d'hiver (EST). Autour des changements
  d'heure, Polymarket crée parfois des fenêtres en double ou en saute (8 mars 2026, 2 nov. 2025).
* Règle de résolution : « Up » si prix de fin >= prix de début (égalité -> Up). Jusqu'au
  06/08/2026 : dernier prix du flux Chainlink ``X/USD`` ; du 07/08 au 13/08/2026 : TWAP
  Chainlink 30 s pour le 5m et 60 s pour le 15m/4h ; depuis le 14/08/2026 00:00 UTC :
  **TWAP Chainlink 60 s** partout (``cryptoMarketConfig.twapEnabled``,
  ``twapLookbackSeconds``), lu au début et à la fin de la fenêtre — voir
  ``UpDownMarket.resolution_rule`` et docs/research/polymarket.md.
* Frais preneur (``feeSchedule``) : aucun en déc. 2025, ``crypto_15_min`` puis ``crypto_fees``
  (rate 0,25, exposant 2) à partir de janv./févr. 2026, ``crypto_fees_v2`` (rate 0,07,
  exposant 1) depuis le 30-31/03/2026. Toujours lire ``feeSchedule`` marché par marché.
  Voir :func:`taker_fee`.
* ``bestBid``/``bestAsk`` de gamma peuvent être en retard de plusieurs dizaines de secondes :
  pour du temps réel utiliser :meth:`PolymarketClient.order_book` (CLOB).
* Hors de la fenêtre, le prix du jeton Up vaut ≈ 0,505 (milieu d'un carnet 0,50/0,51) : il
  n'est informatif qu'une fois la fenêtre ouverte, puis converge vers 0 ou 1.
* ``restricted: true`` : marché soumis au géoblocage. La France (FR) est en « close-only »
  sur le site ET l'API (docs.polymarket.com/api-reference/geoblock) : impossible d'ouvrir une
  position depuis la France. La lecture des données publiques reste possible.

Contrat de cache : seuls les marchés **résolus** (immuables) sont mis en cache disque
(``CACHE_DIR/polymarket``), ainsi que l'historique de prix de leurs jetons.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .config import CACHE_DIR

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"

# Actifs Polymarket -> paire Binance correspondante (hype et zec n'ont pas d'équivalent
# dans notre univers Binance par défaut).
ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "bnb": "BNBUSDT",
    "doge": "DOGEUSDT",
}
SYMBOL_TO_ASSET: dict[str, str] = {v: k for k, v in ASSET_TO_SYMBOL.items()}
UPDOWN_ASSETS: tuple[str, ...] = ("btc", "eth", "sol", "xrp", "bnb", "doge", "hype", "zec")

DURATION_SECONDS: dict[str, int] = {"5m": 300, "15m": 900, "4h": 14400}
# Fuseau d'alignement des fenêtres 4h (vérifié : 8:00AM-12:00PM ET, etc.).
SLOT_TZ_4H = "America/New_York"

GAMMA_BATCH = 50                 # slugs par requête /events (max API : 100)
PRICES_MAX_SPAN_S = 3 * 86400    # /prices-history refuse les intervalles trop longs (15 j : 400)
MISSING_CONFIRM_S = 6 * 3600     # un slug absent dont la fenêtre a fini depuis 6 h n'existera plus
BOOK_DEPTH_WINDOW = 0.02         # profondeur du carnet mesurée à ±2 cents autour du milieu

_SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]+)-updown-(?P<duration>5m|15m|4h)-(?P<ts>\d+)$")
# Champs gamma conservés dans le cache disque (le reste — description, images, compteurs de
# volume glissants… — est volumineux et inutile pour un marché résolu).
_RAW_KEEP = (
    "id", "question", "conditionId", "questionID", "slug", "resolutionSource", "startDate", "createdAt",
    "eventStartTime", "endDate", "closedTime", "umaEndDate", "outcomes", "outcomePrices", "clobTokenIds",
    "active", "closed", "umaResolutionStatus", "automaticallyResolved", "volume", "volumeNum", "liquidity",
    "liquidityNum", "bestBid", "bestAsk", "spread", "lastTradePrice", "restricted", "acceptingOrders",
    "negRisk", "orderPriceMinTickSize", "orderMinSize", "feesEnabled", "feeType", "feeSchedule",
    "makerBaseFee", "takerBaseFee", "cryptoMarketConfigId", "cryptoMarketConfig",
)


class PolymarketError(RuntimeError):
    """Erreur réseau / API persistante après les retentatives."""


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def _to_utc(ts: Any) -> pd.Timestamp:
    """Timestamp UTC ; les entrées naïves sont supposées en UTC, les entiers en secondes Unix."""
    if isinstance(ts, (int, float, np.integer, np.floating)):
        return pd.Timestamp(int(ts), unit="s", tz="UTC")
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _to_unix(ts: Any) -> int:
    if isinstance(ts, (int, np.integer)):
        return int(ts)
    return int(_to_utc(ts).timestamp())


def _norm_asset(asset: str) -> str:
    a = str(asset).strip()
    if a.upper() in SYMBOL_TO_ASSET:
        return SYMBOL_TO_ASSET[a.upper()]
    return a.lower()


def _check_duration(duration: str) -> int:
    if duration not in DURATION_SECONDS:
        raise ValueError(f"durée inconnue {duration!r} (attendu : {sorted(DURATION_SECONDS)})")
    return DURATION_SECONDS[duration]


def _json_list(value: Any) -> list:
    """Les champs clobTokenIds / outcomes / outcomePrices sont des chaînes JSON."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _f(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out


def _parse_time(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        return _to_utc(value)
    except (ValueError, TypeError):
        return None


def updown_slug(asset: str, duration: str, start_ts: int) -> str:
    """Slug de l'événement « Up or Down » : ``{asset}-updown-{duration}-{start_ts}``.

    ``asset`` accepte ``"btc"`` ou ``"BTCUSDT"`` ; ``start_ts`` est le début de fenêtre en
    secondes Unix (ou un Timestamp).
    """
    dur = _check_duration(duration)
    ts = _to_unix(start_ts)
    step = 3600 if duration == "4h" else dur  # 4h : alignement New York, donc heure pleine UTC
    if ts % step:
        raise ValueError(f"début de fenêtre {ts} non aligné pour {duration}")
    return f"{_norm_asset(asset)}-updown-{duration}-{ts}"


def parse_slug(slug: str) -> tuple[str, str, int]:
    """(asset, duration, start_ts) depuis un slug ``btc-updown-15m-1790350200``."""
    m = _SLUG_RE.match(slug)
    if not m:
        raise ValueError(f"slug Up/Down invalide : {slug!r}")
    return m["asset"], m["duration"], int(m["ts"])


def slot_starts(duration: str, start: pd.Timestamp, end: pd.Timestamp) -> list[int]:
    """Débuts de fenêtres (s Unix) dans ``[start, end)``.

    * 5m / 15m : multiples de la durée en UTC (identique en heure de New York).
    * 4h : heures de New York multiples de 4 (00h, 04h, … ET) — vérifié sur l'API : en
      hiver (EST) les fenêtres commencent à 01h, 05h… UTC, en été (EDT) à 00h, 04h… UTC.
      Les jours de changement d'heure on obtient la grille « canonique » (ex. 8 mars 2026 :
      01h, 05h, 08h, 12h… UTC) ; les éventuels doublons créés par Polymarket sont ignorés.
    """
    dur = _check_duration(duration)
    t0, t1 = _to_utc(start), _to_utc(end)
    if t1 <= t0:
        return []
    if duration != "4h":
        a, b = int(t0.timestamp()), int(t1.timestamp())
        first = -(-a // dur) * dur
        return list(range(first, b, dur))
    hours = pd.date_range(t0.ceil("h"), t1, freq="h", inclusive="left")
    if len(hours) == 0:
        return []
    local = hours.tz_convert(SLOT_TZ_4H)
    keep = hours[(local.hour % 4 == 0) & (local.minute == 0)]
    return [int(t.timestamp()) for t in keep]


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------
@dataclass
class UpDownMarket:
    """Un marché « Up or Down » (un événement = un marché binaire Up / Down)."""

    slug: str
    asset: str
    duration: str
    start: pd.Timestamp                 # début de fenêtre (UTC) — prix de référence
    end: pd.Timestamp                   # fin de fenêtre (UTC)
    condition_id: str
    token_up: str
    token_down: str
    closed: bool
    resolved_up: bool | None            # True = Up gagnant, False = Down, None = non résolu
    outcome_prices: tuple[float, ...]   # (prix Up, prix Down) ; (1, 0) une fois résolu Up
    best_bid: float                     # jeton Up (gamma, peut être en retard)
    best_ask: float
    spread: float
    volume: float                       # volume cumulé en $
    liquidity: float
    fee_schedule: dict | None           # {rate, exponent, takerOnly, rebateRate} ou None
    twap_lookback_s: int | None         # 60 si résolution par TWAP Chainlink, sinon None
    restricted: bool
    accepting_orders: bool
    raw: dict = field(default_factory=dict, repr=False)
    # Champs complémentaires
    question: str = ""
    fee_type: str | None = None         # "crypto_fees" | "crypto_fees_v2" | None
    resolution_source: str | None = None
    tick_size: float = math.nan
    last_trade_price: float = math.nan
    uma_status: str | None = None

    @property
    def symbol(self) -> str | None:
        """Paire Binance correspondante (``BTCUSDT``…) ou None."""
        return ASSET_TO_SYMBOL.get(self.asset)

    @property
    def is_resolved(self) -> bool:
        """Marché fermé et issue connue (ou résolution UMA terminée) : données immuables."""
        return bool(self.closed) and (self.resolved_up is not None or self.uma_status == "resolved")

    @property
    def up_price(self) -> float:
        return self.outcome_prices[0] if self.outcome_prices else math.nan

    @property
    def resolution_rule(self) -> str:
        """``"twap"`` (TWAP Chainlink, depuis ~août 2026) ou ``"last_price"`` (avant)."""
        return "twap" if self.twap_lookback_s else "last_price"


def parse_market(market: dict, event: dict | None = None) -> UpDownMarket:
    """Construit un :class:`UpDownMarket` depuis un marché gamma (``event["markets"][0]``)."""
    event = event or {}
    slug = market.get("slug") or event.get("slug") or ""
    cfg = market.get("cryptoMarketConfig") or {}
    try:
        s_asset, s_duration, s_ts = parse_slug(slug)
    except ValueError:
        s_asset, s_duration, s_ts = cfg.get("asset", ""), cfg.get("duration", ""), None
    asset = (cfg.get("asset") or s_asset).lower()
    duration = cfg.get("duration") or s_duration

    start = _parse_time(market.get("eventStartTime") or event.get("startTime"))
    if start is None and s_ts is not None:
        start = _to_utc(s_ts)
    end = _parse_time(market.get("endDate") or event.get("endDate"))
    if end is None and start is not None and duration in DURATION_SECONDS:
        end = start + pd.Timedelta(seconds=DURATION_SECONDS[duration])

    outcomes = [str(o).strip().lower() for o in _json_list(market.get("outcomes"))]
    tokens = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    prices = [_f(p) for p in _json_list(market.get("outcomePrices"))]
    i_up, i_down = 0, 1
    if "up" in outcomes and "down" in outcomes:
        i_up, i_down = outcomes.index("up"), outcomes.index("down")

    def pick(seq: list, i: int, default: Any) -> Any:
        return seq[i] if i < len(seq) else default

    token_up, token_down = pick(tokens, i_up, ""), pick(tokens, i_down, "")
    p_up, p_down = pick(prices, i_up, math.nan), pick(prices, i_down, math.nan)
    closed = bool(market.get("closed", event.get("closed", False)))
    resolved_up: bool | None = None
    if closed and np.isfinite(p_up) and np.isfinite(p_down):
        if abs(p_up - 1.0) < 1e-9 and abs(p_down) < 1e-9:
            resolved_up = True
        elif abs(p_up) < 1e-9 and abs(p_down - 1.0) < 1e-9:
            resolved_up = False

    fee_schedule = market.get("feeSchedule")
    if not market.get("feesEnabled", fee_schedule is not None) or not isinstance(fee_schedule, dict):
        fee_schedule = None
    else:
        fee_schedule = dict(fee_schedule)
    twap = cfg.get("twapLookbackSeconds") if cfg.get("twapEnabled") else None

    return UpDownMarket(
        slug=slug,
        asset=asset,
        duration=duration,
        start=start,
        end=end,
        condition_id=str(market.get("conditionId") or ""),
        token_up=token_up,
        token_down=token_down,
        closed=closed,
        resolved_up=resolved_up,
        outcome_prices=(p_up, p_down) if prices else (),
        best_bid=_f(market.get("bestBid")),
        best_ask=_f(market.get("bestAsk")),
        spread=_f(market.get("spread")),
        volume=_f(market.get("volumeNum", market.get("volume"))),
        liquidity=_f(market.get("liquidityNum", market.get("liquidity"))),
        fee_schedule=fee_schedule,
        twap_lookback_s=int(twap) if twap is not None else None,
        restricted=bool(market.get("restricted", event.get("restricted", False))),
        accepting_orders=bool(market.get("acceptingOrders", False)),
        raw=market,
        question=str(market.get("question") or event.get("title") or ""),
        fee_type=market.get("feeType"),
        resolution_source=market.get("resolutionSource") or event.get("resolutionSource"),
        tick_size=_f(market.get("orderPriceMinTickSize")),
        last_trade_price=_f(market.get("lastTradePrice")),
        uma_status=market.get("umaResolutionStatus"),
    )


# ---------------------------------------------------------------------------
# Limiteur de débit
# ---------------------------------------------------------------------------
class RateLimiter:
    """Seau à jetons thread-safe : ``rate`` requêtes/s en moyenne, rafale ``burst``."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else max(1.0, self.rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if not self.rate or self.rate <= 0 or math.isinf(self.rate):
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class PolymarketClient:
    """Client HTTP lecture seule (gamma + CLOB + data-api) avec cache disque des marchés résolus.

    Robustesse : ``retries`` retentatives avec backoff exponentiel sur erreurs réseau,
    timeouts, 429 et 5xx ; 404 -> ``None`` ; limitation de débit partagée entre threads.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        cache_dir: Path = CACHE_DIR / "polymarket",
        max_rps: float = 8.0,
        timeout: float = 15.0,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        use_cache: bool = True,
    ):
        self.session = session or requests.Session()
        if session is None:
            self.session.headers.update({"User-Agent": "tradebot/0.1 (read-only research)"})
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.retries = int(retries)
        self.backoff = float(backoff)
        self.use_cache = use_cache
        self._limiter = RateLimiter(max_rps)
        self._cache_lock = threading.RLock()
        self._month_cache: dict[tuple[str, str, str], dict[str, str | None]] = {}
        # jeton -> marché connu (pour savoir si son historique de prix est immuable)
        self._tokens: dict[str, UpDownMarket] = {}

    # -- HTTP ----------------------------------------------------------------
    def _get(self, url: str, params: Any = None) -> Any | None:
        """GET JSON ; ``None`` sur 404 ; lève :class:`PolymarketError` après les retentatives."""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            self._limiter.acquire()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                log.debug("GET %s : %s (essai %d)", url, exc, attempt + 1)
                continue
            status = resp.status_code
            if status == 404:
                return None
            if status == 429 or status >= 500:
                last = PolymarketError(f"HTTP {status} sur {url}")
                retry_after = _f(getattr(resp, "headers", {}).get("Retry-After"))
                if np.isfinite(retry_after) and retry_after > 0:
                    time.sleep(min(retry_after, 30.0))
                continue
            if status >= 400:
                raise PolymarketError(f"HTTP {status} sur {url} : {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                last = exc
                continue
        raise PolymarketError(f"échec après {self.retries + 1} essais : {url}") from last

    def geoblock(self) -> dict:
        """Statut de géoblocage de l'IP courante (``{blocked, ip, country, region}``)."""
        return self._get(GEOBLOCK_URL) or {}

    # -- Cache des marchés résolus -----------------------------------------------
    def _month_path(self, asset: str, duration: str, month: str) -> Path:
        return self.cache_dir / "markets" / f"{asset}_{duration}_{month}.parquet"

    @staticmethod
    def _month_of(start_ts: int) -> str:
        return time.strftime("%Y-%m", time.gmtime(start_ts))

    def _read_month_file(self, path: Path) -> dict[str, str | None]:
        entries: dict[str, str | None] = {}
        if self.use_cache and path.exists():
            try:
                df = pd.read_parquet(path)
                for slug, missing, raw in zip(df["slug"], df["missing"], df["raw"]):
                    entries[str(slug)] = None if bool(missing) else str(raw)
            except Exception as exc:  # fichier corrompu : on l'ignore
                log.warning("cache Polymarket illisible %s : %s", path, exc)
        return entries

    def _load_month(self, asset: str, duration: str, month: str) -> dict[str, str | None]:
        key = (asset, duration, month)
        with self._cache_lock:
            if key not in self._month_cache:
                self._month_cache[key] = self._read_month_file(self._month_path(asset, duration, month))
            return self._month_cache[key]

    def _cache_lookup(self, slug: str) -> tuple[bool, UpDownMarket | None]:
        """(trouvé, marché) ; trouvé=True et marché=None -> inexistant confirmé."""
        if not self.use_cache:
            return False, None
        asset, duration, ts = parse_slug(slug)
        entries = self._load_month(asset, duration, self._month_of(ts))
        if slug not in entries:
            return False, None
        raw = entries[slug]
        if raw is None:
            return True, None
        return True, parse_market(json.loads(raw))

    def _cache_store(self, resolved: Iterable[UpDownMarket], missing: Iterable[str] = ()) -> None:
        if not self.use_cache:
            return
        groups: dict[tuple[str, str, str], dict[str, str | None]] = {}
        for m in resolved:
            asset, duration, ts = parse_slug(m.slug)
            slim = {k: m.raw[k] for k in _RAW_KEEP if k in m.raw}
            groups.setdefault((asset, duration, self._month_of(ts)), {})[m.slug] = json.dumps(slim)
        for slug in missing:
            asset, duration, ts = parse_slug(slug)
            groups.setdefault((asset, duration, self._month_of(ts)), {})[slug] = None
        with self._cache_lock:
            for (asset, duration, month), new in groups.items():
                entries = self._load_month(asset, duration, month)
                changed = {k: v for k, v in new.items() if k not in entries or entries[k] != v}
                if not changed:
                    continue
                # relit le fichier : un autre client / processus a pu l'enrichir entre-temps
                path = self._month_path(asset, duration, month)
                for k, v in self._read_month_file(path).items():
                    entries.setdefault(k, v)
                entries.update(changed)
                slugs = sorted(entries, key=lambda s: parse_slug(s)[2])
                df = pd.DataFrame(
                    {
                        "slug": slugs,
                        "start_ts": [parse_slug(s)[2] for s in slugs],
                        "missing": [entries[s] is None for s in slugs],
                        "raw": [entries[s] or "" for s in slugs],
                    }
                )
                _atomic_parquet(df, path)

    def _remember(self, markets: Iterable[UpDownMarket]) -> None:
        for m in markets:
            if m.token_up:
                self._tokens[m.token_up] = m
            if m.token_down:
                self._tokens[m.token_down] = m

    # -- Marchés -------------------------------------------------------------
    def _fetch_events(self, slugs: Sequence[str]) -> dict[str, UpDownMarket]:
        params = [("slug", s) for s in slugs] + [("limit", len(slugs))]
        events = self._get(f"{GAMMA_URL}/events", params) or []
        out: dict[str, UpDownMarket] = {}
        wanted = set(slugs)
        for ev in events:
            markets = ev.get("markets") or []
            if not markets:
                continue
            m = parse_market(markets[0], ev)
            if m.slug in wanted:
                out[m.slug] = m
        return out

    def _markets_for_slugs(self, slugs: Sequence[str], max_workers: int = 8) -> dict[str, UpDownMarket]:
        found: dict[str, UpDownMarket] = {}
        todo: list[str] = []
        for slug in slugs:
            hit, m = self._cache_lookup(slug)
            if not hit:
                todo.append(slug)
            elif m is not None:
                found[slug] = m
        if todo:
            batches = [todo[i : i + GAMMA_BATCH] for i in range(0, len(todo), GAMMA_BATCH)]
            if len(batches) == 1 or max_workers <= 1:
                results = [self._fetch_events(b) for b in batches]
            else:
                with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as ex:
                    results = list(ex.map(self._fetch_events, batches))
            fetched: dict[str, UpDownMarket] = {}
            for r in results:
                fetched.update(r)
            now = time.time()
            missing = []
            for slug in todo:
                if slug in fetched:
                    continue
                asset, duration, ts = parse_slug(slug)
                if ts + DURATION_SECONDS[duration] + MISSING_CONFIRM_S < now:
                    missing.append(slug)
            self._cache_store([m for m in fetched.values() if m.is_resolved], missing)
            found.update(fetched)
        self._remember(found.values())
        return found

    def get_updown_market(self, asset: str, duration: str, start_ts: int) -> UpDownMarket | None:
        """Marché de la fenêtre commençant à ``start_ts`` ; ``None`` s'il n'existe pas."""
        slug = updown_slug(asset, duration, start_ts)
        return self._markets_for_slugs([slug], max_workers=1).get(slug)

    def list_updown_markets(
        self,
        asset: str,
        duration: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        max_workers: int = 8,
        only_closed: bool = False,
    ) -> list[UpDownMarket]:
        """Marchés dont la fenêtre commence dans ``[start, end)``, triés par début.

        Requêtes groupées (50 slugs par appel gamma), parallélisées ; les marchés résolus
        et les créneaux inexistants confirmés sont mis en cache (parquet mensuel).
        ``only_closed=True`` ne garde que les marchés fermés.
        """
        asset = _norm_asset(asset)
        slugs = [updown_slug(asset, duration, ts) for ts in slot_starts(duration, start, end)]
        found = self._markets_for_slugs(slugs, max_workers=max_workers)
        out = [found[s] for s in slugs if s in found]
        if only_closed:
            out = [m for m in out if m.closed]
        return out

    def current_and_next_markets(
        self, asset: str, duration: str, n_next: int = 2, now: pd.Timestamp | None = None
    ) -> list[UpDownMarket]:
        """Marché de la fenêtre en cours puis les ``n_next`` suivants (ceux qui existent)."""
        dur = _check_duration(duration)
        t = _to_utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
        now_ts = int(t.timestamp())
        cands = slot_starts(
            duration, t - pd.Timedelta(seconds=dur + 3600), t + pd.Timedelta(seconds=dur * (n_next + 1) + 3600)
        )
        past = [s for s in cands if s <= now_ts]
        future = [s for s in cands if s > now_ts][: max(0, n_next)]
        chosen = past[-1:] + future
        slugs = [updown_slug(asset, duration, s) for s in chosen]
        found = self._markets_for_slugs(slugs, max_workers=1)
        return [found[s] for s in slugs if s in found]

    # -- Prix, carnet, transactions --------------------------------------------
    def _prices_path(self, token_id: str, start_ts: int, end_ts: int, fidelity: int) -> Path:
        return self.cache_dir / "prices" / token_id[-2:] / f"{token_id}_{start_ts}_{end_ts}_{fidelity}.json"

    def prices_history(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        fidelity: int = 1,
        *,
        resolved: bool | None = None,
    ) -> pd.Series:
        """Historique du prix d'un jeton (≈ 1 point/min avec ``fidelity=1``), index UTC.

        Mis en cache disque si le marché du jeton est résolu (``resolved=True``, ou jeton
        d'un marché résolu déjà chargé par ce client, ou fenêtre terminée depuis > 1 jour).
        """
        a, b = _to_unix(start_ts), _to_unix(end_ts)
        if resolved is None:
            known = self._tokens.get(str(token_id))
            resolved = (known is not None and known.is_resolved) or b < time.time() - 86400
        path = self._prices_path(str(token_id), a, b, fidelity)
        if self.use_cache and resolved and path.exists():
            try:
                data = json.loads(path.read_text())
                return _history_series(data.get("t", []), data.get("p", []))
            except (ValueError, OSError) as exc:
                log.warning("cache de prix illisible %s : %s", path, exc)
        ts_all: list[int] = []
        ps_all: list[float] = []
        lo = a
        while lo < b or (lo == a == b):
            hi = min(b, lo + PRICES_MAX_SPAN_S)
            params = {"market": str(token_id), "startTs": lo, "endTs": hi, "fidelity": int(fidelity)}
            data = self._get(f"{CLOB_URL}/prices-history", params) or {}
            for pt in data.get("history") or []:
                ts_all.append(int(pt["t"]))
                ps_all.append(_f(pt["p"]))
            if hi >= b:
                break
            lo = hi
        s = _history_series(ts_all, ps_all)
        if self.use_cache and resolved and len(s):
            _atomic_write_text(
                path, json.dumps({"t": s.index.as_unit("s").asi8.tolist(), "p": s.tolist()})
            )
        return s

    def order_book(self, token_id: str) -> dict:
        """Carnet CLOB d'un jeton : bids triés décroissants, asks croissants, en (prix, taille).

        Clés : ``bids``, ``asks``, ``best_bid``, ``best_ask``, ``mid``, ``spread``,
        ``bid_depth_usd`` / ``ask_depth_usd`` (somme prix × taille à ±2 c du milieu),
        ``bid_size``/``ask_size`` (au meilleur prix), ``tick_size``, ``min_order_size``,
        ``last_trade_price``, ``timestamp`` (UTC), ``exists`` (False si pas de carnet : marché clos).
        """
        data = self._get(f"{CLOB_URL}/book", {"token_id": str(token_id)})
        return book_summary(data, token_id=str(token_id))

    def trades(self, condition_id: str, limit: int = 500) -> pd.DataFrame:
        """Dernières transactions (preneurs) d'un marché, les plus récentes d'abord.

        Colonnes : ``time`` (UTC), ``side`` (BUY/SELL du preneur), ``outcome`` (Up/Down),
        ``outcome_index``, ``price``, ``size`` (parts), ``notional`` ($), ``asset`` (jeton),
        ``wallet``, ``tx_hash``.
        """
        rows: list[dict] = []
        offset = 0
        while len(rows) < limit:
            page = min(500, limit - len(rows))
            data = self._get(
                f"{DATA_URL}/trades", {"market": condition_id, "limit": page, "offset": offset}
            ) or []
            rows.extend(data)
            if len(data) < page:
                break
            offset += len(data)
        return trades_frame(rows[:limit])


# ---------------------------------------------------------------------------
# Fonctions pures
# ---------------------------------------------------------------------------
def _history_series(ts: Sequence[int], ps: Sequence[float]) -> pd.Series:
    if not len(ts):
        idx = pd.DatetimeIndex([], tz="UTC", name="time").as_unit("ns")
        return pd.Series([], index=idx, dtype="float64", name="price")
    idx = pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True).as_unit("ns")
    s = pd.Series(np.asarray(ps, dtype="float64"), index=idx, name="price")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.index.name = "time"
    return s


def book_summary(data: dict | None, token_id: str = "", window: float = BOOK_DEPTH_WINDOW) -> dict:
    """Résumé d'une réponse ``/book`` du CLOB (prix et tailles sont des chaînes)."""
    data = data or {}
    bids = sorted(((_f(x["price"]), _f(x["size"])) for x in data.get("bids") or []), key=lambda x: -x[0])
    asks = sorted(((_f(x["price"]), _f(x["size"])) for x in data.get("asks") or []), key=lambda x: x[0])
    best_bid = bids[0][0] if bids else math.nan
    best_ask = asks[0][0] if asks else math.nan
    if bids and asks:
        mid = (best_bid + best_ask) / 2
    else:
        mid = math.nan
    ref = mid if np.isfinite(mid) else (best_bid if bids else best_ask)
    eps = 1e-9
    bid_depth = sum(p * s for p, s in bids if np.isfinite(ref) and p >= ref - window - eps)
    ask_depth = sum(p * s for p, s in asks if np.isfinite(ref) and p <= ref + window + eps)
    ts = data.get("timestamp")
    return {
        "token_id": str(data.get("asset_id") or token_id),
        "market": data.get("market"),
        "exists": bool(data),
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": best_ask - best_bid if bids and asks else math.nan,
        "bid_size": bids[0][1] if bids else 0.0,
        "ask_size": asks[0][1] if asks else 0.0,
        "bid_depth_usd": float(bid_depth),
        "ask_depth_usd": float(ask_depth),
        "depth_window": window,
        "tick_size": _f(data.get("tick_size")),
        "min_order_size": _f(data.get("min_order_size")),
        "last_trade_price": _f(data.get("last_trade_price")),
        "timestamp": pd.Timestamp(int(ts), unit="ms", tz="UTC") if ts not in (None, "") else None,
    }


_TRADE_COLUMNS = [
    "time", "side", "outcome", "outcome_index", "price", "size", "notional", "asset", "wallet", "tx_hash",
]


def trades_frame(rows: list[dict]) -> pd.DataFrame:
    """DataFrame des transactions ``data-api /trades`` (voir :meth:`PolymarketClient.trades`)."""
    if not rows:
        df = pd.DataFrame(columns=_TRADE_COLUMNS)
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.as_unit("ns")
        return df
    df = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "time": pd.to_datetime(df["timestamp"].astype("int64"), unit="s", utc=True).dt.as_unit("ns"),
            "side": df.get("side"),
            "outcome": df.get("outcome"),
            "outcome_index": df.get("outcomeIndex"),
            "price": pd.to_numeric(df["price"], errors="coerce").astype("float64"),
            "size": pd.to_numeric(df["size"], errors="coerce").astype("float64"),
            "asset": df.get("asset"),
            "wallet": df.get("proxyWallet"),
            "tx_hash": df.get("transactionHash"),
        }
    )
    out["notional"] = out["price"] * out["size"]
    return out[_TRADE_COLUMNS].reset_index(drop=True)


def up_price_at(
    history: pd.Series, ts: pd.Timestamp, max_staleness: pd.Timedelta = pd.Timedelta("3min")
) -> float:
    """Dernier prix connu à ``ts`` (point d'horodatage <= ts) ; NaN si aucun ou trop ancien."""
    if history is None or len(history) == 0:
        return math.nan
    t = _to_utc(ts)
    h = history if history.index.is_monotonic_increasing else history.sort_index()
    idx = h.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    i = idx.searchsorted(t, side="right") - 1
    if i < 0:
        return math.nan
    if max_staleness is not None and t - idx[i] > pd.Timedelta(max_staleness):
        return math.nan
    return float(h.iloc[i])


def taker_fee(
    price: float | np.ndarray,
    shares: float | np.ndarray,
    fee_schedule: dict | None,
    *,
    decimals: int | None = 5,
) -> float | np.ndarray:
    """Frais preneur Polymarket en USDC : ``fee = shares × rate × (p × (1 − p)) ** exponent``.

    Source : https://docs.polymarket.com/trading/fees (consulté le 25/09/2026) :
    « fee = C × feeRate × p × (1 − p) », C = nombre de parts, p = prix ; crypto : feeRate
    0,07 ; table officielle pour 100 parts : 1,75 $ à 0,50, 1,47 $ à 0,30, 0,07 $ à 0,01 ;
    « Fees are rounded to 5 decimal places » (minimum 0,00001 USDC). La page
    /market-data/market-details précise que ``feeSchedule.exponent`` est « applied to the
    price component of the fee curve ».

    Généralisation à ``exponent != 1`` : (p(1−p))^exponent. Elle reproduit aussi l'ancien
    barème ``crypto_fees`` (rate 0,25, exposant 2) : 100 parts à 0,50 -> 1,5625 $, montant
    (≈ 1,56 $, « un peu plus de 3 % ») rapporté par la presse en janvier 2026. La formule
    ``shares × price × rate × (p(1−p))**exponent`` (avec un facteur prix en plus) est
    incompatible avec la table officielle (0,875 $ au lieu de 1,75 $) et n'est PAS utilisée.

    Seuls les preneurs paient (``takerOnly``) ; ``fee_schedule=None`` -> 0. Le montant est
    symétrique autour de 0,50. ``decimals=None`` désactive l'arrondi.
    """
    p = np.asarray(price, dtype="float64")
    c = np.asarray(shares, dtype="float64")
    if not fee_schedule:
        fee = np.zeros(np.broadcast(p, c).shape)
    else:
        rate = _f(fee_schedule.get("rate", 0.0))
        exponent = _f(fee_schedule.get("exponent", 1.0))
        rate = 0.0 if not np.isfinite(rate) else rate
        exponent = 1.0 if not np.isfinite(exponent) else exponent
        with np.errstate(invalid="ignore"):
            fee = c * rate * np.power(np.clip(p * (1.0 - p), 0.0, None), exponent)
        fee = np.where((p < 0) | (p > 1), np.nan, fee)
    if decimals is not None:
        fee = np.round(fee, decimals)
    if np.ndim(fee) == 0:
        return float(fee)
    return fee


def breakeven_probability(price: float | np.ndarray, fee_schedule: dict | None) -> float | np.ndarray:
    """Probabilité de gain minimale pour qu'acheter une part au prix ``price`` (preneur) soit
    rentable : ``price + frais par part``. Ex. crypto v2 à 0,50 : 0,5175."""
    fee = taker_fee(price, 1.0, fee_schedule, decimals=None)
    return np.asarray(price, dtype="float64") + fee if np.ndim(fee) else float(price) + fee


_FRAME_COLUMNS = [
    "slug", "asset", "symbol", "duration", "start", "end", "condition_id", "token_up", "token_down",
    "closed", "resolved_up", "up_final", "up_price", "down_price", "best_bid", "best_ask", "spread",
    "volume", "liquidity", "fee_type", "fee_rate", "fee_exponent", "twap_lookback_s", "resolution_rule",
    "restricted", "accepting_orders", "question",
]


def markets_to_frame(markets: Iterable[UpDownMarket]) -> pd.DataFrame:
    """Une ligne par marché, triée par ``start``. ``up_final`` : 1.0 (Up), 0.0 (Down), NaN."""
    rows = []
    for m in markets:
        fs = m.fee_schedule or {}
        rows.append(
            {
                "slug": m.slug,
                "asset": m.asset,
                "symbol": m.symbol,
                "duration": m.duration,
                "start": m.start,
                "end": m.end,
                "condition_id": m.condition_id,
                "token_up": m.token_up,
                "token_down": m.token_down,
                "closed": bool(m.closed),
                "resolved_up": m.resolved_up,
                "up_final": math.nan if m.resolved_up is None else float(m.resolved_up),
                "up_price": m.outcome_prices[0] if len(m.outcome_prices) > 0 else math.nan,
                "down_price": m.outcome_prices[1] if len(m.outcome_prices) > 1 else math.nan,
                "best_bid": m.best_bid,
                "best_ask": m.best_ask,
                "spread": m.spread,
                "volume": m.volume,
                "liquidity": m.liquidity,
                "fee_type": m.fee_type,
                "fee_rate": _f(fs.get("rate")) if fs else 0.0,
                "fee_exponent": _f(fs.get("exponent")) if fs else math.nan,
                "twap_lookback_s": m.twap_lookback_s,
                "resolution_rule": m.resolution_rule,
                "restricted": bool(m.restricted),
                "accepting_orders": bool(m.accepting_orders),
                "question": m.question,
            }
        )
    df = pd.DataFrame(rows, columns=_FRAME_COLUMNS)
    for col in ("start", "end"):
        df[col] = pd.to_datetime(df[col], utc=True).dt.as_unit("ns")
    return df.sort_values("start", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Écritures atomiques
# ---------------------------------------------------------------------------
def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".parquet")
    os.close(fd)
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = [
    "ASSET_TO_SYMBOL",
    "DURATION_SECONDS",
    "UPDOWN_ASSETS",
    "PolymarketClient",
    "PolymarketError",
    "RateLimiter",
    "UpDownMarket",
    "book_summary",
    "breakeven_probability",
    "markets_to_frame",
    "parse_market",
    "parse_slug",
    "slot_starts",
    "taker_fee",
    "trades_frame",
    "up_price_at",
    "updown_slug",
]
