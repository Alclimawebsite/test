"""Simulateur **maker** sur le carnet réel Polymarket « Up or Down » (ordres au repos, position
dans la file d'attente), à partir des enregistrements de :mod:`tradebot.polymarket_book`.

Modèle d'exécution d'un ordre au repos (achat de ``size`` parts du jeton ``side`` au prix ``L``) :

* placé à ``t_place`` (+ ``latency_ms``) ; il a **devant lui** la taille affichée à ce niveau à cet
  instant (file ``q_ahead``). Dans le repère Up : un achat de Down à ``L`` est un ask Up à ``1 − L`` ;
* la file ne peut jamais dépasser la taille affichée au niveau (les annulations devant nous la
  réduisent : ``q = min(q, affiché)``, borne supérieure exacte puisque les ordres arrivés après
  nous sont derrière) ;
* chaque trade preneur qui consomme ce niveau (``last_trade_price`` du côté opposé, en tenant
  compte de la complémentarité : un achat preneur de Down à ``1 − L`` consomme aussi les bids Up
  à ``L``) sert d'abord la file, puis nous ; un trade **au-delà** du niveau (traversée) nous
  exécute entièrement (priorité prix) ;
* annulation à ``t_cancel`` (exécution partielle conservée) ; paiement à la résolution :
  ``1{gagnant} − L`` par part, sans frais ; **remise maker** estimée à part :
  ``rebate_rate × frais preneur au même prix`` (0,2 × 0,07 × p(1 − p) ≈ 0,35 c à 0,50).

Stratégies (fonctions ``orders_*`` : listes d':class:`Order` par marché) :

* (a) ``signal`` : côté favorisé par le TWAP partiel 1 s à S−30 s (``gap_m30`` du diagnostic,
  bougies 1 s Binance), prix 0,48 / 0,49 / 0,50, annulation à S, S+30 s, S+60 s ;
* (b) ``two_sided`` : achat Up **et** achat Down à 0,49 avant l'ouverture (capture de l'écart) ;
* (c) ``fair_value`` : pendant la fenêtre, juste valeur ``Φ(d/σ)`` (``d`` = écart log entre
  l'estimation du TWAP final et ``priceToBeat``, ``σ`` = volatilité EWMA 1 s, variance du TWAP
  restant), cotée à ± 1 c, re-cotée toutes les 10 s.

Les simulations sont indépendantes (nos ordres n'altèrent pas le carnet enregistré) : chaque
combinaison prix × annulation est un ordre distinct. Simulation papier : aucun ordre réel.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from tradebot.config import CACHE_DIR
from tradebot.polymarket import ASSET_TO_SYMBOL, taker_fee
from tradebot.polymarket_book import (
    Book, LiveMarket, _KINDS_BOOK, _p_int, apply_event, consumed_level,
)

__all__ = [
    "Order", "OrderResult", "MakerSimulator", "simulate", "fetch_binance_1s", "partial_twap_gap",
    "fair_value_series", "orders_signal", "orders_two_sided", "orders_fair_value", "results_frame",
    "summarize", "MAKER_REBATE_RATE", "DEFAULT_SIZE", "LATENCY_MS",
]

log = logging.getLogger(__name__)

MAKER_REBATE_RATE = 0.20
DEFAULT_SIZE = 10.0           # parts par ordre (taille médiane des trades preneurs : 6 à 10)
LATENCY_MS = 300              # décision -> ordre visible par le CLOB
SIGNAL_PRICES = (0.48, 0.49, 0.50)
CANCEL_OFFSETS = (0, 30, 60)  # annulation à S, S+30 s, S+60 s
BINANCE_1S_DIR = CACHE_DIR / "pm_maker_live" / "binance_1s"
SIGMA_HALFLIFE_S = 60.0
SIGMA_FLOOR = 1e-6            # volatilité 1 s minimale (log), évite Φ(±∞)
FV_STEP_S = 10.0
FV_MARGIN = 0.01
FV_LEVEL_MIN, FV_LEVEL_MAX = 0.05, 0.95


# ---------------------------------------------------------------------------
# Ordres et résultats
# ---------------------------------------------------------------------------
@dataclass
class Order:
    """Achat de ``size`` parts de ``side`` (``"up"``/``"down"``) à ``price``, au repos de ``t_place``
    à ``t_cancel`` (secondes serveur, absolues)."""

    strategy: str
    side: str
    price: float
    t_place: float
    t_cancel: float
    size: float = DEFAULT_SIZE
    label: str = ""
    tags: dict = field(default_factory=dict)

    @property
    def up_side(self) -> str:
        """Côté du carnet (repère Up) où l'ordre repose : bid Up ou ask Up."""
        return "bid" if self.side == "up" else "ask"

    @property
    def up_level(self) -> float:
        return self.price if self.side == "up" else round(1.0 - self.price, 3)


@dataclass
class OrderResult:
    order: Order
    status: str                 # "filled" | "partial" | "cancelled" | "expired" | "crossing" | "no_book"
    q_ahead0: float             # file devant nous à la pose
    filled: float               # parts exécutées
    t_placed: float             # s (après latence) ; NaN si non posé
    t_fill: float               # première exécution (s) ; NaN sinon
    t_full: float               # exécution complète (s) ; NaN sinon
    fill_reason: str            # "" | "queue" (trades au niveau) | "traversal" (trade au-delà) | "cross" (le carnet nous croise)
    traded_at_level: float      # volume preneur passé au niveau pendant la vie de l'ordre
    mid_at_place: float
    q_ahead_end: float

    @property
    def fill_by_traversal(self) -> bool:
        return self.fill_reason == "traversal"


# ---------------------------------------------------------------------------
# Simulateur
# ---------------------------------------------------------------------------
class _Live:
    __slots__ = ("o", "q0", "q", "filled", "t_placed", "t_fill", "t_full", "reason", "traded", "mid0", "level_int")

    def __init__(self, o: Order, q: float, t: float, mid: float):
        self.o, self.q0, self.q, self.filled, self.t_placed = o, q, q, 0.0, t
        self.t_fill = self.t_full = math.nan
        self.reason, self.traded, self.mid0 = "", 0.0, mid
        self.level_int = _p_int(o.up_level)

    def fill_all(self, ts: float, reason: str) -> None:
        if self.filled == 0:
            self.t_fill = ts
        self.filled = self.o.size
        self.q = 0.0
        self.reason = reason

    def result(self, status: str) -> OrderResult:
        return OrderResult(order=self.o, status=status, q_ahead0=self.q0, filled=self.filled, t_placed=self.t_placed,
                           t_fill=self.t_fill, t_full=self.t_full, fill_reason=self.reason,
                           traded_at_level=self.traded, mid_at_place=self.mid0, q_ahead_end=self.q)


def _not_placed(o: Order, status: str, t: float = math.nan, mid: float = math.nan) -> OrderResult:
    return OrderResult(o, status, math.nan, 0.0, t, math.nan, math.nan, "", 0.0, mid, math.nan)


def _crosses(book: Book, up_side: str, level_int: int) -> bool:
    """Un bid Up à ``L`` croise si le meilleur ask ≤ L ; un ask Up si le meilleur bid ≥ L."""
    if up_side == "bid":
        return bool(book.asks) and min(book.asks) <= level_int
    return bool(book.bids) and max(book.bids) >= level_int


class MakerSimulator:
    """Rejoue les événements d'un marché et exécute des ordres au repos virtuels."""

    def __init__(self, market: LiveMarket, latency_ms: int = LATENCY_MS):
        self.market = market
        self.latency_s = latency_ms / 1000.0

    def run(self, orders: Sequence[Order]) -> list[OrderResult]:
        if not orders:
            return []
        pending = sorted(orders, key=lambda o: o.t_place)
        pend_i = 0
        live: list[_Live] = []
        done: dict[int, OrderResult] = {}
        book = Book()
        last_ts = 0.0

        def finish(lv: _Live, status: str) -> None:
            done[id(lv.o)] = lv.result(status)

        for ev in self.market.events:
            ts = ev[1] / 1000.0
            if ts <= 0:
                continue
            kind = ev[2]
            # 1) poser les ordres dont l'heure (avec latence) est atteinte, sur l'état courant
            while pend_i < len(pending) and pending[pend_i].t_place + self.latency_s <= ts:
                o = pending[pend_i]
                pend_i += 1
                t_eff = max(o.t_place + self.latency_s, last_ts)
                if book.n_snapshots == 0:
                    done[id(o)] = _not_placed(o, "no_book")
                    continue
                level_int = _p_int(o.up_level)
                if _crosses(book, o.up_side, level_int):
                    # ordre immédiatement exécutable : ce serait un ordre preneur, pas un ordre au repos
                    done[id(o)] = _not_placed(o, "crossing", t_eff, book.mid())
                    continue
                levels = book.bids if o.up_side == "bid" else book.asks
                live.append(_Live(o, levels.get(level_int, 0.0), t_eff, book.mid()))
            # 2) annulations
            if live:
                still = []
                for lv in live:
                    if lv.o.t_cancel <= ts:
                        finish(lv, "partial" if lv.filled > 0 else "cancelled")
                    else:
                        still.append(lv)
                live = still
            # 3) événement
            if kind in _KINDS_BOOK:
                apply_event(book, ev)
                if live:
                    still = []
                    for lv in live:
                        if _crosses(book, lv.o.up_side, lv.level_int):
                            # un ordre adverse est entré dans notre niveau : il nous exécute (nous étions là avant)
                            lv.fill_all(ts, "cross")
                            lv.t_full = ts
                            finish(lv, "filled")
                            continue
                        levels = book.bids if lv.o.up_side == "bid" else book.asks
                        shown = levels.get(lv.level_int, 0.0)
                        if shown < lv.q:
                            lv.q = shown
                        still.append(lv)
                    live = still
            elif kind == "trade" and live:
                price, size, side, _tx = ev[4]
                cons, lvl = consumed_level(ev[3], side, price)
                lvl_int = _p_int(lvl)
                still = []
                for lv in live:
                    if cons != lv.o.up_side:
                        still.append(lv)
                        continue
                    beyond = (lvl_int < lv.level_int) if cons == "bid" else (lvl_int > lv.level_int)
                    if lvl_int == lv.level_int:
                        lv.traded += size
                        eat = min(size, lv.q)
                        lv.q -= eat
                        rem = size - eat
                        if rem > 0:
                            take = min(rem, lv.o.size - lv.filled)
                            if take > 0:
                                if lv.filled == 0:
                                    lv.t_fill = ts
                                lv.filled += take
                                lv.reason = lv.reason or "queue"
                    elif beyond:
                        lv.fill_all(ts, "traversal")
                    if lv.filled >= lv.o.size - 1e-9:
                        lv.t_full = ts
                        finish(lv, "filled")
                    else:
                        still.append(lv)
                live = still
            last_ts = ts
        for lv in live:
            finish(lv, "partial" if lv.filled > 0 else "expired")
        while pend_i < len(pending):
            done[id(pending[pend_i])] = _not_placed(pending[pend_i], "no_book")
            pend_i += 1
        return [done[id(o)] for o in orders]


def simulate(market: LiveMarket, orders: Sequence[Order], latency_ms: int = LATENCY_MS) -> list[OrderResult]:
    return MakerSimulator(market, latency_ms).run(orders)


# ---------------------------------------------------------------------------
# Binance 1 s : signal à S−30 s et juste valeur
# ---------------------------------------------------------------------------
def fetch_binance_1s(symbol: str, start_ts: int, end_ts: int, cache_dir: Path = BINANCE_1S_DIR,
                     fetcher: Callable | None = None) -> pd.DataFrame:
    """Bougies 1 s ``[start_ts, end_ts)`` (API ``data-api.binance.vision``), cache parquet par plage."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_{start_ts}_{end_ts}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    if fetcher is None:
        from tradebot.data import _fetch_api_klines

        fetcher = _fetch_api_klines
    start = pd.Timestamp(start_ts, unit="s", tz="UTC")
    end = pd.Timestamp(end_ts, unit="s", tz="UTC")
    df = fetcher(symbol, "1s", start, end, pd.Timestamp.now(tz="UTC"))
    df = df[["open", "high", "low", "close", "volume"]].copy()
    if len(df) and df.index[-1] >= end - pd.Timedelta(seconds=2):
        df.to_parquet(path)        # plage complète : immuable
    return df


def partial_twap_gap(bars1s: pd.DataFrame, start_ts: int, offset_s: int = 30) -> float:
    """``gap_m30`` du diagnostic : log(spot(S−30) / moyenne des closes 1 s ouverts dans
    [S−60, S−30)). Positif -> Up favorisé (momentum). NaN si moins de 20 bougies."""
    a = pd.Timestamp(start_ts - 60, unit="s", tz="UTC")
    b = pd.Timestamp(start_ts - offset_s, unit="s", tz="UTC")
    w = bars1s[(bars1s.index >= a) & (bars1s.index < b)]["close"]
    if len(w) < 20 or not (w > 0).all():
        return math.nan
    return float(np.log(w.iloc[-1] / w.mean()))


def fair_value_series(bars1s: pd.DataFrame, start_ts: int, end_ts: int, price_to_beat: float | None = None,
                      halflife_s: float = SIGMA_HALFLIFE_S, sigma_floor: float = SIGMA_FLOOR) -> pd.DataFrame:
    """Juste valeur P(Up) à chaque seconde ``t`` de la fenêtre, avec l'information close à ``t``.

    * ``K`` = TWAP60(S) Binance (moyenne des closes 1 s ouverts dans (S−60, S]) par défaut : le
      niveau Binance dépasse le niveau Chainlink d'environ 3 pb en moyenne (diagnostic), ce qui
      biaiserait ``d`` si l'on comparait le spot Binance au ``priceToBeat`` Chainlink ; passer
      ``price_to_beat`` pour l'utiliser malgré tout ;
    * tant que ``t ≤ E−60`` : ``d = log(spot/K)``, ``var = σ²·((E−60−t) + 20)`` (variance d'une
      moyenne de marche aléatoire sur les 60 dernières secondes) ;
    * ensuite : le TWAP final vaut ``(Σ closes connus + (E−t)·spot)/60``, ``var = σ²·(m/60)²·m/3``
      avec ``m = E − t`` secondes restantes ;
    * ``σ`` = écart-type EWMA (demi-vie 60 s) des rendements log 1 s, plancher ``sigma_floor``.

    Colonnes : ``t`` (s), ``t_rel_s``, ``spot``, ``d``, ``sd``, ``p_up``.
    """
    close = bars1s["close"].astype("float64")
    sec = np.asarray(close.index.view("int64") // 10**9)
    c = close.to_numpy()
    r = np.diff(np.log(c), prepend=np.nan)
    var_ewm = pd.Series(r ** 2).ewm(halflife=halflife_s, min_periods=10).mean().to_numpy()
    sigma = np.sqrt(np.nan_to_num(var_ewm, nan=sigma_floor ** 2))
    sigma = np.maximum(sigma, sigma_floor)
    K = price_to_beat if price_to_beat is not None and np.isfinite(price_to_beat) and price_to_beat > 0 else None
    if K is None:
        w = (sec > start_ts - 60) & (sec <= start_ts)     # bougies ouvertes dans (S−60, S] ≈ TWAP60(S)
        K = float(c[w].mean()) if w.any() else float(c[0])
    rows = []
    E = end_ts
    for t in range(start_ts, end_ts + 1):
        i = np.searchsorted(sec, t - 1, side="right") - 1      # dernière bougie ouverte ≤ t−1 (close à t)
        if i < 0:
            continue
        spot, sg = c[i], sigma[i]
        if t <= E - 60:
            d = math.log(spot / K)
            var = sg ** 2 * ((E - 60 - t) + 20.0)
        else:
            m = E - t
            known = (sec >= E - 60) & (sec <= t - 1)
            s_known = c[known].sum()
            n_known = int(known.sum())
            twap = (s_known + (60 - n_known) * spot) / 60.0
            d = math.log(twap / K)
            var = sg ** 2 * (m / 60.0) ** 2 * max(m, 1) / 3.0 if m > 0 else 0.0
        sd = math.sqrt(var) if var > 0 else 0.0
        p = float(norm.cdf(d / sd)) if sd > 0 else (1.0 if d >= 0 else 0.0)
        rows.append({"t": t, "t_rel_s": t - start_ts, "spot": spot, "d": d, "sd": sd, "p_up": p})
    return pd.DataFrame(rows, columns=["t", "t_rel_s", "spot", "d", "sd", "p_up"])


# ---------------------------------------------------------------------------
# Stratégies
# ---------------------------------------------------------------------------
def orders_signal(market: LiveMarket, gap: float, prices: Sequence[float] = SIGNAL_PRICES,
                  cancel_offsets: Sequence[int] = CANCEL_OFFSETS, place_offset_s: int = -30,
                  size: float = DEFAULT_SIZE) -> list[Order]:
    """(a) Côté favorisé par le TWAP partiel ; un ordre par (prix, annulation)."""
    if not np.isfinite(gap) or gap == 0.0:
        return []
    side = "up" if gap > 0 else "down"
    S = market.start_ts
    return [Order("signal", side, p, S + place_offset_s, S + c, size, label=f"{p:.2f}/S+{c}",
                  tags={"price": p, "cancel": c, "gap": gap})
            for p in prices for c in cancel_offsets]


def orders_two_sided(market: LiveMarket, price: float = 0.49, place_offset_s: int = -60,
                     cancel_offsets: Sequence[int] = CANCEL_OFFSETS, size: float = DEFAULT_SIZE) -> list[Order]:
    """(b) Achat Up et achat Down à ``price`` avant l'ouverture, pour chaque annulation."""
    S = market.start_ts
    out = []
    for c in cancel_offsets:
        for side in ("up", "down"):
            out.append(Order("two_sided", side, price, S + place_offset_s, S + c, size, label=f"{price:.2f}/S+{c}",
                             tags={"price": price, "cancel": c, "pair": c}))
    return out


def _round_down(x: float, tick: float = 0.01) -> float:
    return math.floor(x / tick + 1e-9) * tick


def orders_fair_value(market: LiveMarket, fv: pd.DataFrame, step_s: float = FV_STEP_S, margin: float = FV_MARGIN,
                      start_offset_s: float = 10.0, end_offset_s: float = 10.0, size: float = DEFAULT_SIZE,
                      level_min: float = FV_LEVEL_MIN, level_max: float = FV_LEVEL_MAX,
                      bbo: pd.DataFrame | None = None) -> list[Order]:
    """(c) Toutes les ``step_s`` secondes : bid Up à ``p̂ − margin`` et bid Down à ``(1 − p̂) − margin``
    (arrondis au cent inférieur). Un ordre est conservé tant que son niveau ne change pas, sinon
    annulé et remplacé (nouvelle file). Tout est annulé à ``E − end_offset_s``.

    ``bbo`` (colonnes ``t_rel_s``, ``best_bid``, ``best_ask`` du jeton Up, par ex.
    :func:`tradebot.polymarket_book.book_series` au pas ``step_s``) plafonne les niveaux pour ne
    jamais croiser le carnet : bid Up ≤ meilleur ask − 1 c, bid Down ≤ (1 − meilleur bid) − 1 c.
    Sans ``bbo``, les ordres qui croisent sont écartés par le simulateur (statut ``crossing``).
    """
    S, E = market.start_ts, market.end_ts
    p_of = fv.set_index("t")["p_up"] if len(fv) else pd.Series(dtype=float)
    cap_up = cap_dn = None
    if bbo is not None and len(bbo):
        b = bbo.set_index("t_rel_s")
        cap_up, cap_dn = b["best_ask"], b["best_bid"]
    t_end = E - end_offset_s
    out: list[Order] = []
    current: dict[str, tuple[float, float]] = {}     # side -> (level, t_place)
    t = S + start_offset_s
    while t < t_end:
        p = p_of.get(int(t), math.nan)
        want = {}
        if np.isfinite(p):
            lu = _round_down(p - margin)
            ld = _round_down((1.0 - p) - margin)
            if cap_up is not None:
                ba = cap_up.get(float(t - S), math.nan)
                bb = cap_dn.get(float(t - S), math.nan)
                if np.isfinite(ba):
                    lu = min(lu, _round_down(ba - 0.01))
                if np.isfinite(bb):
                    ld = min(ld, _round_down((1.0 - bb) - 0.01))
            if level_min <= lu <= level_max:
                want["up"] = round(lu, 2)
            if level_min <= ld <= level_max:
                want["down"] = round(ld, 2)
        for side in ("up", "down"):
            cur = current.get(side)
            w = want.get(side)
            if cur is not None and (w is None or abs(w - cur[0]) > 1e-9):
                out.append(Order("fair_value", side, cur[0], cur[1], t, size, label=f"± {round(100 * margin)} c",
                                 tags={"p_up": float(p) if np.isfinite(p) else math.nan}))
                current.pop(side)
                cur = None
            if cur is None and w is not None:
                current[side] = (w, t)
        t += step_s
    for side, (lvl, t0) in current.items():
        out.append(Order("fair_value", side, lvl, t0, t_end, size, label=f"± {round(100 * margin)} c"))
    return out


# ---------------------------------------------------------------------------
# Résultats
# ---------------------------------------------------------------------------
def results_frame(results: Sequence[OrderResult], market: LiveMarket, resolved_up: bool | None = None) -> pd.DataFrame:
    """Une ligne par ordre : statut, file, exécution, délai, P&L par part (résolution), remise."""
    res = market.resolved_up if resolved_up is None else resolved_up
    fee_sched = market.fee_schedule
    S = market.start_ts
    rows = []
    for r in results:
        o = r.order
        win = (math.nan if res is None else float((o.side == "up") == bool(res)))
        pnl_share = (win - o.price) if np.isfinite(win) else math.nan
        filled = r.filled if np.isfinite(r.filled) else 0.0
        rows.append({
            "slug": market.slug, "asset": market.asset, "duration": market.duration, "start_ts": S,
            "strategy": o.strategy, "label": o.label, "side": o.side, "price": o.price, "size": o.size,
            "t_place_rel": o.t_place - S, "t_cancel_rel": o.t_cancel - S,
            "status": r.status, "placed": r.status not in ("no_book", "crossing"),
            "q_ahead0": r.q_ahead0, "q_ahead_end": r.q_ahead_end, "filled": filled,
            "fill_ratio": filled / o.size if o.size else math.nan, "executed": filled > 0,
            "executed_full": r.status == "filled", "fill_reason": r.fill_reason,
            "traded_at_level": r.traded_at_level, "mid_at_place": r.mid_at_place,
            "t_fill_rel": (r.t_fill - S) if np.isfinite(r.t_fill) else math.nan,
            "delay_s": (r.t_fill - r.t_placed) if np.isfinite(r.t_fill) and np.isfinite(r.t_placed) else math.nan,
            "resolved_up": res, "would_win": win, "pnl_per_share": pnl_share,
            "pnl_order": pnl_share * filled if np.isfinite(pnl_share) else math.nan,
            "pnl_per_placed_share": (pnl_share * filled / o.size) if np.isfinite(pnl_share) and o.size else math.nan,
            "rebate_per_share": MAKER_REBATE_RATE * float(taker_fee(o.price, 1.0, fee_sched, decimals=None)),
            **{f"tag_{k}": v for k, v in o.tags.items()},
        })
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, by: Sequence[str] = ("strategy", "label"), boot=None,
              group_col: str = "slot") -> pd.DataFrame:
    """Par groupe : ordres, taux d'exécution, P&L par ordre placé et par ordre exécuté (par part),
    sélection adverse (taux de gain exécutés / non exécutés), délai médian. ``boot`` :
    :class:`tradebot.polymarket_backtest.SlotBootstrap` construit sur ``df[group_col]`` pour les IC."""
    rows = []
    for key, g in df.groupby(list(by), sort=False):
        key = key if isinstance(key, tuple) else (key,)
        placed = g[g["placed"]]
        ex = placed[placed["executed"]]
        scored = placed[np.isfinite(placed["pnl_per_share"])]
        ex_s = scored[scored["executed"]]
        row = dict(zip(by, key))
        row.update({
            "n_orders": len(placed), "n_markets": placed["slug"].nunique(),
            "n_no_book": int((g["status"] == "no_book").sum()), "n_crossing": int((g["status"] == "crossing").sum()),
            "fill_rate": placed["executed"].mean() if len(placed) else math.nan,
            "fill_rate_full": placed["executed_full"].mean() if len(placed) else math.nan,
            "share_by_queue": (ex["fill_reason"] == "queue").mean() if len(ex) else math.nan,
            "share_by_traversal": (ex["fill_reason"] == "traversal").mean() if len(ex) else math.nan,
            "share_by_cross": (ex["fill_reason"] == "cross").mean() if len(ex) else math.nan,
            "q_ahead0_median": placed["q_ahead0"].median() if len(placed) else math.nan,
            "delay_median_s": ex["delay_s"].median() if len(ex) else math.nan,
            "delay_p75_s": ex["delay_s"].quantile(0.75) if len(ex) else math.nan,
            "n_scored": len(scored),
            "pnl_per_placed_c": 100 * scored["pnl_per_placed_share"].mean() if len(scored) else math.nan,
            "pnl_per_executed_c": 100 * ex_s["pnl_per_share"].mean() if len(ex_s) else math.nan,
            "rebate_per_executed_c": 100 * ex_s["rebate_per_share"].mean() if len(ex_s) else math.nan,
            "win_rate_executed": ex_s["would_win"].mean() if len(ex_s) else math.nan,
            "win_rate_not_executed": scored.loc[~scored["executed"], "would_win"].mean() if (~scored["executed"]).any() else math.nan,
        })
        row["adverse_selection_pts"] = 100 * (row["win_rate_executed"] - row["win_rate_not_executed"])
        if boot is not None and len(placed):
            m_placed = df.index.isin(placed.index)
            m_scored = df.index.isin(scored.index)
            _, flo, fhi = boot.mean(df["executed"].to_numpy(dtype=float), m_placed)
            row.update(fill_rate_ic_lo=flo, fill_rate_ic_hi=fhi)
            if len(scored):
                _, lo, hi = boot.mean(100 * df["pnl_per_placed_share"].to_numpy(), m_scored)
                row.update(pnl_per_placed_ic_lo=lo, pnl_per_placed_ic_hi=hi)
                _, lo2, hi2 = boot.mean(100 * df["pnl_per_share"].to_numpy(), m_scored & df["executed"].to_numpy())
                row.update(pnl_per_executed_ic_lo=lo2, pnl_per_executed_ic_hi=hi2)
        rows.append(row)
    return pd.DataFrame(rows)
