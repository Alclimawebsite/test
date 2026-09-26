"""Lecture des enregistrements du collecteur temps réel (``scripts/polymarket_live_collector.py``)
et reconstruction du carnet d'ordres des marchés Polymarket « Up or Down ».

Entrées : ``data/cache/polymarket/live/<slug>.jsonl.gz`` (+ segments ``<slug>.s<k>.jsonl.gz``) et
``<slug>.meta.json``. Deux formats de lignes sont acceptés :

* format 1 : ``{"rx": ns, "msg": "<message WebSocket brut>"}`` (ou ``{"rx", "ev": "connect"|"error"}``) ;
* format 2 : lignes compactes ``{"rx", "e": "book"|"pc"|"bba"|"trade"|"tick"|"resolved"|…}``
  (voir l'en-tête du collecteur).

Le carnet est reconstruit dans le **repère du jeton Up** : un bid Down à ``p`` est un ask Up à
``1 − p`` et réciproquement (les messages du CLOB sont exactement symétriques : vérifié sur
290 000 ``price_change`` le 26/09/2026, 0 écart). Un instantané ``book`` (émis à chaque trade)
remet le carnet à zéro ; les ``price_change`` donnent la **nouvelle taille** d'un niveau (0 =
niveau vidé). Les prix sont stockés en millièmes (entiers) : le pas passe de 0,01 à 0,001 quand
le prix dépasse 0,96.

Sorties :

* :func:`book_series` : meilleur bid/ask, milieu, écart, profondeur aux niveaux 0,48–0,52 (bids Up
  et bids Down) sur une grille régulière ;
* :func:`trades_frame` : trades preneurs (``last_trade_price`` : jeton, côté, prix, taille, ts
  serveur ms) avec le niveau consommé dans le repère Up et l'état du carnet juste avant ;
* :func:`bba_control` : accord entre le carnet reconstruit et les messages ``best_bid_ask`` ;
* :func:`replay` : générateur ``(événement, carnet)`` pour les simulateurs.

Lecture seule de données publiques : aucun ordre, aucune clé.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import zlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.config import CACHE_DIR

__all__ = [
    "LIVE_DIR", "LEVELS", "Book", "LiveMarket", "load_market", "list_markets", "market_files",
    "read_events", "apply_event", "replay", "book_series", "trades_frame", "bba_control", "consumed_level",
    "resolution_of", "refresh_resolutions",
]

log = logging.getLogger(__name__)

LIVE_DIR = CACHE_DIR / "polymarket" / "live"
LEVELS: tuple[float, ...] = (0.48, 0.49, 0.50, 0.51, 0.52)
SCALE = 1000                      # prix en millièmes
UP, DOWN = 0, 1
_KINDS_BOOK = ("book", "pc")

# Un événement normalisé : (rx_ns, ts_ms, kind, asset, payload)
#   book  : payload = (bids [(p_int, size)], asks [(p_int, size)])
#   pc    : payload = [(asset, p_int, size, side "B"/"S", best_bid, best_ask)]
#   bba   : payload = (best_bid, best_ask)
#   trade : payload = (price, size, side, tx)
#   tick  : payload = (old, new) ; resolved : payload = "Up"/"Down" ; autres : payload = dict
Event = tuple[int, int, str, int, object]


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------
def _p_int(price) -> int:
    return int(round(float(price) * SCALE))


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def list_markets(live_dir: Path = LIVE_DIR) -> list[str]:
    """Slugs disposant d'un ``meta.json`` et d'au moins un fichier de données."""
    out = []
    for meta in sorted(live_dir.glob("*.meta.json")):
        slug = meta.name[: -len(".meta.json")]
        if market_files(slug, live_dir):
            out.append(slug)
    return out


def market_files(slug: str, live_dir: Path = LIVE_DIR) -> list[Path]:
    """Fichier principal puis segments ``.s<k>`` dans l'ordre."""
    main = live_dir / f"{slug}.jsonl.gz"
    segs = sorted(live_dir.glob(f"{slug}.s*.jsonl.gz"), key=lambda p: int(p.name.split(".s")[1].split(".")[0]))
    return ([main] if main.exists() else []) + segs


def _iter_lines(path: Path) -> Iterator[str]:
    """Lignes d'un gzip, tolérant aux fichiers tronqués (collecteur tué) ou multi-membres."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.endswith("\n"):
                    yield line
    except (EOFError, OSError, zlib.error) as exc:
        log.info("%s : fin tronquée (%s)", path.name, str(exc)[:60])


def _parse_v1_msg(rx: int, raw: str, asset_index: dict[str, int]) -> list[Event]:
    try:
        m = json.loads(raw)
    except ValueError:
        return []
    msgs = m if isinstance(m, list) else [m]
    out: list[Event] = []
    for x in msgs:
        if not isinstance(x, dict):
            continue
        et = x.get("event_type")
        a = asset_index.get(str(x.get("asset_id")), -1)
        ts = int(x.get("timestamp") or 0)
        if et == "price_change":
            rows = []
            for c in x.get("price_changes") or []:
                ai = asset_index.get(str(c.get("asset_id")), -1)
                if ai < 0:
                    continue
                rows.append((ai, _p_int(c["price"]), _f(c.get("size")), str(c.get("side", ""))[:1],
                             _f(c.get("best_bid")), _f(c.get("best_ask"))))
            if rows:
                out.append((rx, ts, "pc", -1, rows))
        elif et == "book" and a >= 0:
            out.append((rx, ts, "book", a, ([(_p_int(l["price"]), _f(l["size"])) for l in x.get("bids") or []],
                                            [(_p_int(l["price"]), _f(l["size"])) for l in x.get("asks") or []])))
        elif et == "best_bid_ask" and a >= 0:
            out.append((rx, ts, "bba", a, (_f(x.get("best_bid")), _f(x.get("best_ask")))))
        elif et == "last_trade_price" and a >= 0:
            out.append((rx, ts, "trade", a, (_f(x.get("price")), _f(x.get("size")), str(x.get("side", "")),
                                             str(x.get("transaction_hash", "")))))
        elif et == "tick_size_change" and a >= 0:
            out.append((rx, ts, "tick", a, (_f(x.get("old_tick_size")), _f(x.get("new_tick_size")))))
        elif et == "market_resolved":
            ids = [str(i) for i in x.get("assets_ids") or []]
            if a >= 0 or any(i in asset_index for i in ids):
                out.append((rx, ts, "resolved", -1, str(x.get("winning_outcome", ""))))
    return out


def _parse_v2(rx: int, d: dict) -> Event | None:
    e = d.get("e")
    ts = int(d.get("ts") or 0)
    a = int(d.get("a", -1))
    if e == "pc":
        rows = [(int(c[0]), _p_int(c[1]), _f(c[2]), str(c[3])[:1], _f(c[4]), _f(c[5])) for c in d.get("c") or []]
        return (rx, ts, "pc", -1, rows) if rows else None
    if e == "book":
        return (rx, ts, "book", a, ([(_p_int(p), _f(s)) for p, s in d.get("b") or []],
                                   [(_p_int(p), _f(s)) for p, s in d.get("k") or []]))
    if e == "bba":
        return (rx, ts, "bba", a, (_f(d.get("bb")), _f(d.get("ba"))))
    if e == "trade":
        return (rx, ts, "trade", a, (_f(d.get("p")), _f(d.get("s")), str(d.get("side", "")), str(d.get("tx", ""))))
    if e == "tick":
        return (rx, ts, "tick", a, (_f(d.get("old")), _f(d.get("new"))))
    if e == "resolved":
        return (rx, ts, "resolved", -1, str(d.get("win", "")))
    return (rx, ts, str(e), a, d)


def read_events(paths: Sequence[Path], token_up: str, token_down: str) -> list[Event]:
    """Événements normalisés de tous les fichiers, triés par heure de réception, trades dédoublonnés
    (segments qui se recouvrent lors d'un remplacement du collecteur)."""
    asset_index = {str(token_up): UP, str(token_down): DOWN}
    events: list[Event] = []
    for path in paths:
        for line in _iter_lines(path):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rx = int(d.get("rx") or 0)
            if "msg" in d and "e" not in d:
                events.extend(_parse_v1_msg(rx, d["msg"], asset_index))
            elif "ev" in d:
                events.append((rx, 0, str(d["ev"]), -1, d))
            else:
                ev = _parse_v2(rx, d)
                if ev is not None:
                    events.append(ev)
    if len(paths) > 1:
        events.sort(key=lambda e: e[0])
        seen: set = set()
        deduped = []
        for ev in events:
            if ev[2] == "trade":
                key = (ev[3], ev[1], ev[4])
                if key in seen:
                    continue
                seen.add(key)
            deduped.append(ev)
        events = deduped
    return events


# ---------------------------------------------------------------------------
# Carnet (repère Up)
# ---------------------------------------------------------------------------
class Book:
    """Carnet agrégé par niveau dans le repère du jeton Up (prix en millièmes)."""

    __slots__ = ("bids", "asks", "n_snapshots", "ts")

    def __init__(self) -> None:
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}
        self.n_snapshots = 0
        self.ts = 0

    def apply_snapshot(self, asset: int, bids: Iterable[tuple[int, float]], asks: Iterable[tuple[int, float]]) -> None:
        if asset == UP:
            self.bids = {p: s for p, s in bids if s > 0}
            self.asks = {p: s for p, s in asks if s > 0}
        else:   # bid Down à p = ask Up à 1 − p
            self.asks = {SCALE - p: s for p, s in bids if s > 0}
            self.bids = {SCALE - p: s for p, s in asks if s > 0}
        self.n_snapshots += 1

    def apply_change(self, asset: int, p_int: int, size: float, side: str) -> None:
        """``side`` = côté de l'ordre au repos (``"B"`` bid, ``"S"`` ask) sur ``asset``."""
        if asset == DOWN:
            p_int = SCALE - p_int
            side = "S" if side == "B" else "B"
        levels = self.bids if side == "B" else self.asks
        if size > 0:
            levels[p_int] = size
        else:
            levels.pop(p_int, None)

    def best_bid(self) -> float:
        return max(self.bids) / SCALE if self.bids else math.nan

    def best_ask(self) -> float:
        return min(self.asks) / SCALE if self.asks else math.nan

    def mid(self) -> float:
        return (self.best_bid() + self.best_ask()) / 2.0

    def spread(self) -> float:
        return self.best_ask() - self.best_bid()

    def size(self, side: str, price: float) -> float:
        """Taille affichée au niveau ``price`` (repère Up) : ``side`` = ``"bid"`` ou ``"ask"``."""
        return (self.bids if side == "bid" else self.asks).get(_p_int(price), 0.0)

    def size_down_bid(self, price: float) -> float:
        """Taille des bids Down à ``price`` (= asks Up à 1 − price)."""
        return self.asks.get(SCALE - _p_int(price), 0.0)

    def depth_best(self) -> tuple[float, float]:
        b = self.bids[max(self.bids)] if self.bids else 0.0
        a = self.asks[min(self.asks)] if self.asks else 0.0
        return b, a

    def depth_within(self, side: str, width: float) -> float:
        """Taille cumulée à moins de ``width`` du milieu, côté ``bid`` ou ``ask`` (repère Up)."""
        mid = self.mid()
        if not math.isfinite(mid):
            return math.nan
        levels = self.bids if side == "bid" else self.asks
        return float(sum(s for p, s in levels.items() if abs(p / SCALE - mid) <= width + 1e-9))

    def copy(self) -> "Book":
        b = Book()
        b.bids, b.asks, b.n_snapshots, b.ts = dict(self.bids), dict(self.asks), self.n_snapshots, self.ts
        return b


def consumed_level(asset: int, side: str, price: float) -> tuple[str, float]:
    """Niveau du carnet (repère Up) consommé par un trade preneur : ``(côté consommé, niveau Up)``.

    BUY Up à p consomme les asks Up à p ; SELL Up à p les bids Up à p ; BUY Down à p les asks Down
    = bids Up à 1 − p ; SELL Down à p les bids Down = asks Up à 1 − p.
    """
    side = str(side).upper()
    if asset == UP:
        return ("ask", price) if side == "BUY" else ("bid", price)
    return ("bid", round(1.0 - price, 3)) if side == "BUY" else ("ask", round(1.0 - price, 3))


def apply_event(book: Book, ev: Event) -> bool:
    """Applique un événement de carnet (``book`` ou ``pc``) ; renvoie False pour les autres."""
    kind = ev[2]
    if kind == "book":
        bids, asks = ev[4]
        book.apply_snapshot(ev[3], bids, asks)
    elif kind == "pc":
        for a, p_int, size, side, _bb, _ba in ev[4]:
            book.apply_change(a, p_int, size, side)
    else:
        return False
    book.ts = ev[1]
    return True


def replay(events: Iterable[Event], book: Book | None = None) -> Iterator[tuple[Event, Book]]:
    """Applique les événements au carnet (même instance, à lire immédiatement) et les renvoie
    **après** application pour ``book``/``pc``, **avant** pour les autres (trade, bba…), afin que le
    consommateur voie l'état du carnet au moment du message."""
    book = book if book is not None else Book()
    for ev in events:
        apply_event(book, ev)
        yield ev, book


# ---------------------------------------------------------------------------
# Marché
# ---------------------------------------------------------------------------
@dataclass
class LiveMarket:
    slug: str
    meta: dict
    events: list[Event]
    files: list[Path] = field(default_factory=list)

    @property
    def start_ts(self) -> int:
        return int(pd.Timestamp(self.meta["start"]).timestamp())

    @property
    def end_ts(self) -> int:
        return int(pd.Timestamp(self.meta["end"]).timestamp())

    @property
    def duration_s(self) -> int:
        return self.end_ts - self.start_ts

    @property
    def asset(self) -> str:
        return str(self.meta.get("asset", ""))

    @property
    def duration(self) -> str:
        return str(self.meta.get("duration", ""))

    @property
    def resolved_up(self) -> bool | None:
        return resolution_of(self.meta, self.events)

    @property
    def price_to_beat(self) -> float:
        return _f((self.meta.get("event_metadata") or {}).get("priceToBeat"))

    @property
    def final_price(self) -> float:
        return _f((self.meta.get("event_metadata") or {}).get("finalPrice"))

    @property
    def fee_schedule(self) -> dict | None:
        return self.meta.get("fee_schedule")

    def n_events(self, kind: str) -> int:
        return sum(1 for e in self.events if e[2] == kind)

    def coverage(self) -> tuple[float, float]:
        """Premier et dernier horodatage serveur (s) des événements de carnet."""
        ts = [e[1] for e in self.events if e[2] in ("book", "pc", "trade", "bba") and e[1] > 0]
        return (min(ts) / 1000.0, max(ts) / 1000.0) if ts else (math.nan, math.nan)


def resolution_of(meta: dict, events: Sequence[Event] = ()) -> bool | None:
    """Issue : ``meta["resolved_up"]`` (gamma) sinon l'événement ``market_resolved`` du WebSocket."""
    r = meta.get("resolved_up")
    if r is not None:
        return bool(r)
    for ev in events:
        if ev[2] == "resolved":
            win = str(ev[4]).lower()
            if win in ("up", "down"):
                return win == "up"
    return None


def load_market(slug: str, live_dir: Path = LIVE_DIR) -> LiveMarket:
    meta = json.loads((live_dir / f"{slug}.meta.json").read_text())
    files = market_files(slug, live_dir)
    events = read_events(files, meta["token_up"], meta["token_down"])
    return LiveMarket(slug=slug, meta=meta, events=events, files=files)


def refresh_resolutions(client, live_dir: Path = LIVE_DIR, min_age_s: float = 180.0, max_age_h: float = 48.0) -> int:
    """Complète l'issue et ``priceToBeat``/``finalPrice`` des ``meta.json`` non résolus via gamma
    (``client`` : :class:`tradebot.polymarket.PolymarketClient`). Renvoie le nombre de mises à jour."""
    import time

    from tradebot.polymarket import GAMMA_URL, parse_market

    now = time.time()
    n = 0
    for path in sorted(live_dir.glob("*.meta.json")):
        meta = json.loads(path.read_text())
        end = pd.Timestamp(meta["end"]).timestamp()
        if meta.get("resolved_up") is not None or now - end < min_age_s or now - end > max_age_h * 3600:
            continue
        try:
            events = client._get(f"{GAMMA_URL}/events", [("slug", meta["slug"]), ("limit", 1)])
            ev = (events or [{}])[0]
            if not ev:
                continue
            fresh = parse_market((ev.get("markets") or [{}])[0], ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s : gamma injoignable (%r)", meta["slug"], exc)
            continue
        meta.update(resolved_up=fresh.resolved_up, closed=fresh.closed, event_metadata=ev.get("eventMetadata"),
                    resolution_checked=now)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(path)
        n += fresh.resolved_up is not None
    return n


# ---------------------------------------------------------------------------
# Séries
# ---------------------------------------------------------------------------
def _level_cols(levels: Sequence[float]) -> list[tuple[str, float]]:
    return [(f"{int(round(l * 100)):03d}", l) for l in levels]


def book_series(market: LiveMarket, step_s: float = 1.0, t0_rel: float = -150.0, t1_rel: float | None = None,
                levels: Sequence[float] = LEVELS) -> pd.DataFrame:
    """État du carnet sur une grille régulière (``S + t0_rel`` … ``E + 60 s`` par défaut) : dernier
    état dont l'horodatage serveur est ≤ l'instant de la grille.

    Colonnes : ``t_rel_s``, ``ts`` (s), ``best_bid``, ``best_ask``, ``mid``, ``spread``,
    ``depth_best_bid``, ``depth_best_ask`` (tailles aux meilleurs niveaux), ``bid_up_048``…
    (bids Up à 0,48…0,52), ``bid_dn_048``… (bids Down à 0,48…0,52 = asks Up à 0,52…0,48),
    ``depth_bid_10c``/``depth_ask_10c`` (cumul à ± 0,10 du milieu), ``n_snapshots``, ``fresh``
    (False tant qu'aucun instantané n'a été reçu).
    """
    S = market.start_ts
    t1_rel = (market.duration_s + 60.0) if t1_rel is None else t1_rel
    grid = np.arange(t0_rel, t1_rel + 1e-9, step_s)
    grid_ms = ((S + grid) * 1000.0).astype("int64")
    cols = _level_cols(levels)
    rows: list[dict] = []
    book = Book()
    events = market.events
    i, n = 0, len(events)
    for g_ms, g_rel in zip(grid_ms, grid):
        # appliquer tous les événements de carnet d'horodatage ≤ g_ms (sans dépasser)
        while i < n:
            ev = events[i]
            if ev[2] in _KINDS_BOOK and ev[1] > g_ms:
                break
            apply_event(book, ev)
            i += 1
        row = {"t_rel_s": float(g_rel), "ts": g_ms / 1000.0, "best_bid": book.best_bid(), "best_ask": book.best_ask(),
               "n_snapshots": book.n_snapshots, "fresh": book.n_snapshots > 0}
        row["mid"] = (row["best_bid"] + row["best_ask"]) / 2.0
        row["spread"] = row["best_ask"] - row["best_bid"]
        row["depth_best_bid"], row["depth_best_ask"] = book.depth_best()
        for name, lvl in cols:
            row[f"bid_up_{name}"] = book.size("bid", lvl)
            row[f"bid_dn_{name}"] = book.size_down_bid(lvl)
        row["depth_bid_10c"] = book.depth_within("bid", 0.10)
        row["depth_ask_10c"] = book.depth_within("ask", 0.10)
        rows.append(row)
    df = pd.DataFrame(rows)
    df.loc[~df["fresh"], [c for c in df.columns if c not in ("t_rel_s", "ts", "n_snapshots", "fresh")]] = np.nan
    return df


def trades_frame(market: LiveMarket, pre_ms: int = 200) -> pd.DataFrame:
    """Trades preneurs avec l'état du carnet **avant** le trade.

    Colonnes : ``rx``, ``ts`` (ms serveur), ``t_rel_s``, ``asset`` (``up``/``down``), ``side``
    (côté preneur), ``price``, ``size``, ``tx``, ``consumes`` (``bid``/``ask`` Up), ``level_up``,
    ``best_bid_pre``, ``best_ask_pre``, ``depth_level_pre`` (taille affichée au niveau consommé),
    ``traverses`` (le prix dépasse strictement le meilleur niveau d'avant : au moins deux niveaux
    consommés) et ``exceeds_depth`` (taille > taille affichée au meilleur niveau). L'état « avant »
    est le dernier état de carnet d'horodatage ≤ ts − ``pre_ms`` (les mises à jour du carnet
    peuvent arriver avant le message ``last_trade_price`` du même trade).
    """
    hist: list[tuple[int, float, float, dict, dict]] = []   # (ts, bb, ba, bids, asks) états récents
    rows = []
    S = market.start_ts
    last_snapshot_state = None
    for ev, book in replay(market.events):
        kind = ev[2]
        if kind in _KINDS_BOOK:
            # ne garder que quelques états récents (fenêtre de 5 s)
            hist.append((ev[1], book.best_bid(), book.best_ask(), dict(book.bids), dict(book.asks)))
            cutoff = ev[1] - 5000
            while len(hist) > 1 and hist[0][0] < cutoff and hist[1][0] <= cutoff:
                hist.pop(0)
            last_snapshot_state = hist[-1]
            continue
        if kind != "trade":
            continue
        price, size, side, tx = ev[4]
        cons, lvl = consumed_level(ev[3], side, price)
        state = None
        for st in reversed(hist):
            if st[0] <= ev[1] - pre_ms:
                state = st
                break
        if state is None and hist:
            state = hist[0]
        if state is None:
            bb = ba = depth = math.nan
        else:
            _, bb, ba, bids, asks = state
            depth = (bids if cons == "bid" else asks).get(_p_int(lvl), 0.0)
        if cons == "ask":
            best_pre, trav = ba, (math.isfinite(ba) and lvl > ba + 1e-9)
        else:
            best_pre, trav = bb, (math.isfinite(bb) and lvl < bb - 1e-9)
        best_depth = math.nan
        if state is not None and math.isfinite(best_pre):
            best_depth = (state[3] if cons == "bid" else state[4]).get(_p_int(best_pre), 0.0)
        rows.append({
            "rx": ev[0], "ts": ev[1], "t_rel_s": ev[1] / 1000.0 - S, "asset": "up" if ev[3] == UP else "down",
            "side": side, "price": price, "size": size, "tx": tx, "consumes": cons, "level_up": lvl,
            "best_bid_pre": bb, "best_ask_pre": ba, "depth_level_pre": depth, "depth_best_pre": best_depth,
            "traverses": bool(trav), "exceeds_depth": bool(math.isfinite(best_depth) and size > best_depth + 1e-9),
        })
    cols = ["rx", "ts", "t_rel_s", "asset", "side", "price", "size", "tx", "consumes", "level_up", "best_bid_pre",
            "best_ask_pre", "depth_level_pre", "depth_best_pre", "traverses", "exceeds_depth"]
    return pd.DataFrame(rows, columns=cols)


def bba_control(market: LiveMarket, lag_ms: int = 200, tol: float = 1e-9) -> dict:
    """Accord entre le carnet reconstruit et les messages ``best_bid_ask`` (jeton Up ou Down, ramené
    au repère Up).

    Le serveur émet ``best_bid_ask`` **avant** le ``price_change`` qui produit ce nouveau meilleur
    prix (même horodatage) : un message est compté « d'accord » si le carnet lui correspond au
    moment de sa réception ou après application des mises à jour d'horodatage ≤ ts + ``lag_ms``.
    Renvoie ``{"n", "agree", "agree_immediate", "agree_rate"}``.
    """
    n = ok = ok_now = 0
    pending: list[tuple[int, float, float]] = []      # (ts, bb, ba) en attente de confirmation

    def match(bb: float, ba: float, book: Book) -> bool:
        eb = abs(book.best_bid() - bb) <= 1e-6 + tol if math.isfinite(bb) else not book.bids
        ea = abs(book.best_ask() - ba) <= 1e-6 + tol if math.isfinite(ba) else not book.asks
        return bool(eb and ea)

    for ev, book in replay(market.events):
        kind = ev[2]
        if kind in _KINDS_BOOK and pending:
            keep = []
            for ts, bb, ba in pending:
                if match(bb, ba, book):
                    ok += 1
                elif ev[1] <= ts + lag_ms:
                    keep.append((ts, bb, ba))
            pending = keep
        if kind != "bba" or book.n_snapshots == 0:
            continue
        bb, ba = ev[4]
        if ev[3] == DOWN:
            bb, ba = 1.0 - ba, 1.0 - bb
        n += 1
        if match(bb, ba, book):
            ok += 1
            ok_now += 1
        else:
            pending.append((ev[1], bb, ba))
    return {"n": n, "agree": ok, "agree_immediate": ok_now, "agree_rate": ok / n if n else math.nan}
