"""Temps de réaction nécessaire pour prendre les prix périmés des marchés Polymarket « Up or Down ».

Données : les deux collecteurs temps réel, **même horloge locale** (``rx``, ns) :

* ``scripts/cex_ws_collector.py`` -> ``data/cache/cex_ws/<source>_<AAAAMMJJHH>.jsonl.gz``, lignes
  ``{"rx": ns, "msg": "<message brut>"}`` : Binance ``bookTicker`` / ``aggTrade`` (``E``, ``T`` en ms),
  Coinbase ``ticker`` (``time`` ISO), RTDS Polymarket ``crypto_prices_chainlink`` (le flux Chainlink de
  résolution, 1 point/s, ``payload.timestamp`` ms) et ``crypto_prices`` ;
* ``scripts/polymarket_live_collector.py`` -> carnet CLOB par marché (lecteur
  :mod:`tradebot.polymarket_book`, ``rx`` local + ``ts`` serveur ms).

Chaîne de calcul (tout en secondes flottantes sur l'horloge locale, sauf mention) :

1. :class:`PriceSeries` : prix « à la date » (dernier point reçu ``<= t``) ; :func:`seconds_asof`
   reconstruit la série 1 s (valeur à la seconde ``k`` = dernier prix reçu ``<= k``) ;
   :func:`ewma_var_1s` : variance EWMA **causale** des rendements 1 s.
2. :class:`TwapFormula` : P(Up | t) exacte pour des moyennes de points **échantillonnés chaque seconde**
   (Chainlink), toutes phases confondues :

       m(τ) = (1/n) [ Σ_{b∈B, b<=τ} x_b + #{b>τ} x_τ − Σ_{a∈A, a<=τ} x_a − #{a>τ} x_τ ]
       Var(τ)/σ² = ∫_τ^E g(u)² du,  g(u) = (#{b >= u} − #{a >= u})/n

   avec A = {S−L+1, …, S}, B = {E−L+1, …, E} (n = L = 60 points), x = log-prix. Identique à
   :func:`tradebot.polymarket_formula.twap_diff_moments` (``dt = 1``) aux secondes entières. Le
   retard de Chainlink sur Binance (``lag_s``) se traite en changeant d'horloge : le point Chainlink
   d'horodatage ``s`` vaut Binance à ``s − lag`` ; l'information disponible à ``t`` (Binance) est celle
   de l'instant Chainlink ``τ = t + lag``.
3. :class:`BookTimeline` : meilleurs bid/ask (repère Up) et tailles après chaque événement de carnet
   (messages de même horodatage serveur regroupés), horodatés à la réception locale ; côté Down :
   ask = 1 − bid Up.
4. :func:`reaction_events` : délai de réaction du carnet aux sauts de la formule (1re retouche du
   meilleur prix, 50 % et 90 % du saut parcourus par le milieu, placebo).
5. :func:`find_opportunities` : prix périmés (P − coût preneur > marge), durée de vie de chaque
   niveau jusqu'à son retrait, origine (saut Binance / désaccord persistant / nouvel ask) ;
   :func:`removal_cause` : retiré par un preneur ou annulé ; :func:`cooldown_mask` : un ordre par
   marché × côté et par seconde ; :func:`simulate_fills` : exécution à t + ℓ ; :func:`pnl_curve`, :func:`bootstrap_by_group`, :func:`breakeven_latency` (option
   isotone), :func:`last_significant_latency` : P&L selon ℓ, latence critique ℓ* et gain démontré.
6. :func:`lag_scan` : retard de Chainlink sur Binance (corrélation des rendements selon le décalage).

Lecture seule, simulation papier. Polymarket est bloqué en France (ANJ) : rien ici n'est utilisable pour
trader depuis la France.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import lfilter
from scipy.special import ndtr

from tradebot.config import CACHE_DIR
from tradebot.polymarket_book import Book, LiveMarket, _iter_lines, apply_event, consumed_level

__all__ = [
    "CEX_DIR", "LATENCIES_MS", "FEE_RATE", "MAX_SHARES", "MARGINS",
    "cex_files", "load_binance", "load_coinbase", "load_rtds",
    "PriceSeries", "asof_index", "asof_values", "seconds_asof", "twap_1s", "ewma_var_1s", "SigmaSeries",
    "TwapFormula", "BookTimeline", "book_timeline", "trades_up_frame", "ladders_at",
    "taker_cost", "side_prices", "reaction_events", "find_opportunities", "removal_cause", "cooldown_mask",
    "simulate_fills",
    "pnl_curve", "bootstrap_by_group", "breakeven_latency", "isotonic_decreasing", "last_significant_latency",
    "lag_scan", "summarize_latency",
]

CEX_DIR = CACHE_DIR / "cex_ws"
LATENCIES_MS: tuple[int, ...] = (0, 25, 50, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000)
MARGINS: tuple[float, ...] = (0.0, 0.01, 0.02)
FEE_RATE = 0.07
MAX_SHARES = 50.0
L_S = 60
_EPS = 1e-9
_HOUR_RE = re.compile(r"^(?P<src>[a-z]+)_(?P<h>\d{10})\.jsonl\.gz$")


# ---------------------------------------------------------------------------
# Lecture des flux CEX / RTDS
# ---------------------------------------------------------------------------
def cex_files(source: str, start_s: float | None = None, end_s: float | None = None,
              cex_dir: Path = CEX_DIR) -> list[Path]:
    """Fichiers horaires ``<source>_<AAAAMMJJHH>.jsonl.gz`` qui recouvrent [start_s, end_s] (triés)."""
    out = []
    for p in sorted(Path(cex_dir).glob(f"{source}_*.jsonl.gz")):
        m = _HOUR_RE.match(p.name)
        if not m or m.group("src") != source:
            continue
        h0 = _hour_ts(m.group("h"))
        if start_s is not None and h0 + 3600 < start_s:
            continue
        if end_s is not None and h0 > end_s:
            continue
        out.append(p)
    return out


def _hour_ts(h: str) -> float:
    return float(pd.Timestamp(f"{h[:4]}-{h[4:6]}-{h[6:8]} {h[8:10]}:00", tz="UTC").timestamp())


def _outer(paths: Iterable[Path]):
    """(rx en s, message brut) de chaque ligne lisible (fichiers tronqués tolérés)."""
    for path in paths:
        for line in _iter_lines(Path(path)):
            try:
                d = json.loads(line)
                yield int(d["rx"]) / 1e9, d["msg"]
            except (ValueError, KeyError, TypeError):
                continue


def load_binance(paths: Sequence[Path]) -> dict[str, dict[str, pd.DataFrame]]:
    """``{"BTCUSDT": {"book": df, "trades": df}, …}``.

    ``book`` : ``rx`` (s), ``bid``, ``ask``, ``bid_sz``, ``ask_sz``, ``mid`` (bookTicker, sans horodatage
    serveur) ; ``trades`` : ``rx`` (s), ``E``, ``T`` (s, serveur), ``price``, ``qty``, ``buyer_maker``.
    """
    book: dict[str, list] = {}
    trades: dict[str, list] = {}
    for rx, raw in _outer(paths):
        try:
            m = json.loads(raw)
            stream, d = m["stream"], m["data"]
        except (ValueError, KeyError, TypeError):
            continue
        if stream.endswith("@bookTicker"):
            book.setdefault(d["s"], []).append((rx, float(d["b"]), float(d["a"]), float(d["B"]), float(d["A"])))
        elif stream.endswith("@aggTrade"):
            trades.setdefault(d["s"], []).append((rx, d["E"] / 1e3, d["T"] / 1e3, float(d["p"]), float(d["q"]),
                                                  bool(d.get("m"))))
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in set(book) | set(trades):
        b = pd.DataFrame(book.get(sym, []), columns=["rx", "bid", "ask", "bid_sz", "ask_sz"])
        b = b.sort_values("rx", kind="stable").reset_index(drop=True)
        b["mid"] = (b["bid"] + b["ask"]) / 2.0
        t = pd.DataFrame(trades.get(sym, []), columns=["rx", "E", "T", "price", "qty", "buyer_maker"])
        out[sym] = {"book": b, "trades": t.sort_values("rx", kind="stable").reset_index(drop=True)}
    return out


def load_coinbase(paths: Sequence[Path]) -> dict[str, pd.DataFrame]:
    """``{"BTC-USD": df}`` : ``rx`` (s), ``time`` (s, serveur), ``price``, ``bid``, ``ask``, ``mid``."""
    rows: dict[str, list] = {}
    for rx, raw in _outer(paths):
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if m.get("type") != "ticker":
            continue
        try:
            rows.setdefault(m["product_id"], []).append((rx, m["time"], float(m["price"]), float(m["best_bid"]),
                                                         float(m["best_ask"])))
        except (KeyError, TypeError, ValueError):
            continue
    out = {}
    for prod, r in rows.items():
        df = pd.DataFrame(r, columns=["rx", "time", "price", "bid", "ask"])
        df["time"] = (pd.to_datetime(df["time"], utc=True, format="ISO8601")
                      - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)
        df["mid"] = (df["bid"] + df["ask"]) / 2.0
        out[prod] = df.sort_values("rx", kind="stable").reset_index(drop=True)
    return out


def load_rtds(paths: Sequence[Path]) -> dict[str, pd.DataFrame]:
    """``{"btc/usd": df, "btcusdt": df, …}`` (symboles avec « / » = Chainlink).

    Colonnes : ``rx`` (s), ``ts`` (s, horodatage de l'observation = ``payload.timestamp``),
    ``msg_ts`` (s, horodatage du message RTDS ; NaN pour l'historique), ``value``, ``snapshot``
    (True = point d'historique envoyé à l'abonnement). Dédoublonné par ``ts`` (mise à jour temps réel préférée).
    """
    rows: dict[str, list] = {}
    for rx, raw in _outer(paths):
        try:
            m = json.loads(raw)
            p = m.get("payload") or {}
        except (ValueError, AttributeError):
            continue
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol")
        if not sym:
            continue
        if m.get("type") == "update" and "value" in p:
            rows.setdefault(sym, []).append((rx, p["timestamp"] / 1e3, (m.get("timestamp") or math.nan) / 1e3,
                                             float(p["value"]), False))
        elif isinstance(p.get("data"), list):
            for pt in p["data"]:
                try:
                    rows.setdefault(sym, []).append((rx, pt["timestamp"] / 1e3, math.nan, float(pt["value"]), True))
                except (KeyError, TypeError, ValueError):
                    continue
    out = {}
    for sym, r in rows.items():
        df = pd.DataFrame(r, columns=["rx", "ts", "msg_ts", "value", "snapshot"])
        df = df.sort_values(["ts", "snapshot", "rx"], kind="stable").drop_duplicates("ts", keep="first")
        out[sym] = df.reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Alignement temporel
# ---------------------------------------------------------------------------
def asof_index(times: np.ndarray, query) -> np.ndarray:
    """Indice du dernier point d'horodatage ``<= query`` (−1 avant le premier)."""
    return np.searchsorted(np.asarray(times, dtype="float64"), np.asarray(query, dtype="float64"), side="right") - 1


def asof_values(times: np.ndarray, values: np.ndarray, query) -> np.ndarray:
    """Valeur en escalier : dernière valeur connue ``<= query`` ; NaN avant le premier point."""
    v = np.asarray(values, dtype="float64")
    j = asof_index(times, query)
    if v.size == 0:
        return np.full(np.shape(j), np.nan)
    return np.where(j >= 0, v[np.maximum(j, 0)], np.nan)


@dataclass
class PriceSeries:
    """Prix reçus (``t`` croissant, s) ; ``at(q)`` = log-prix « à la date » (dernier reçu ``<= q``)."""

    t: np.ndarray
    price: np.ndarray

    def __post_init__(self) -> None:
        self.t = np.asarray(self.t, dtype="float64")
        self.price = np.asarray(self.price, dtype="float64")
        self.logp = np.log(self.price)

    def at(self, q) -> np.ndarray:
        return asof_values(self.t, self.logp, q)

    def max_gap(self, t0: float, t1: float) -> float:
        """Plus long intervalle sans mise à jour dans [t0, t1] (s), bornes comprises."""
        i0, i1 = np.searchsorted(self.t, [t0, t1])
        pts = np.concatenate([[t0], self.t[i0:i1], [t1]])
        return float(np.max(np.diff(pts))) if pts.size > 1 else math.inf


def seconds_asof(series: PriceSeries, s0: int, s1: int) -> tuple[np.ndarray, np.ndarray]:
    """Série 1 s reconstruite : secondes entières ``s0..s1`` et log-prix au dernier point reçu ``<= s``."""
    secs = np.arange(int(s0), int(s1) + 1, dtype="int64")
    return secs, series.at(secs.astype("float64"))


def twap_1s(series: PriceSeries, t_end: int, L: int = L_S, log: bool = False) -> float:
    """Moyenne des points 1 s reconstruits sur (t_end − L, t_end] (règle TWAP-60 : ``t_end − L + 1 … t_end``).
    ``log`` : moyenne des log-prix, sinon des prix."""
    _, x = seconds_asof(series, t_end - L + 1, t_end)
    return float(np.mean(x) if log else np.mean(np.exp(x)))


def ewma_var_1s(x1s: np.ndarray, halflife_s: float = 600.0, min_obs: int = 120,
                v_init: float | None = None) -> np.ndarray:
    """Variance EWMA **causale** des rendements 1 s : ``v[k]`` n'utilise que les rendements
    ``x[j] − x[j−1]``, ``j <= k``. Initialisée par la moyenne des ``min_obs`` premiers carrés (NaN avant) ;
    avec ``v_init`` (variance a priori, p. ex. estimée sur l'historique précédent), la récursion part de
    ``v_init`` dès la seconde 0."""
    x = np.asarray(x1s, dtype="float64")
    v = np.full(x.size, np.nan)
    r = np.diff(x) if x.size > 1 else np.array([])
    r = np.where(np.isfinite(r), r, 0.0)
    r2 = r * r
    lam = 0.5 ** (1.0 / halflife_s)
    if v_init is not None:
        v[0] = v_init
        if r2.size:
            y, _ = lfilter([1.0 - lam], [1.0, -lam], r2, zi=[lam * v_init])
            v[1:] = y
        return v
    if x.size <= min_obs + 1:
        return v
    v0 = float(np.mean(r2[:min_obs]))
    tail = r2[min_obs:]
    y, _ = lfilter([1.0 - lam], [1.0, -lam], tail, zi=[lam * v0])
    v[min_obs] = v0            # rendements 1..min_obs connus à la seconde min_obs
    v[min_obs + 1:] = y
    return v


@dataclass
class SigmaSeries:
    """σ par √s « à la date » (valeur de la dernière seconde entière ``<= t``)."""

    secs: np.ndarray
    var: np.ndarray

    def at(self, t) -> np.ndarray:
        return np.sqrt(asof_values(self.secs.astype("float64"), self.var, t))

    @classmethod
    def from_prices(cls, series: PriceSeries, halflife_s: float = 600.0, min_obs: int = 120,
                    v_init: float | None = None, factor: float = 1.0) -> "SigmaSeries":
        """σ EWMA causal × ``factor`` (correction d'échelle : autocorrélation des rendements 1 s)."""
        s0, s1 = int(math.floor(series.t[0])) + 1, int(math.floor(series.t[-1]))
        secs, x = seconds_asof(series, s0, s1)
        return cls(secs, ewma_var_1s(x, halflife_s, min_obs, v_init) * factor ** 2)


# ---------------------------------------------------------------------------
# Formule (moyennes de points 1 s)
# ---------------------------------------------------------------------------
class TwapFormula:
    """P(Up) exacte à tout instant pour un marché [S, E] (horloge Chainlink, secondes entières).

    ``lag_s`` : retard de Chainlink sur la série de prix utilisée (Binance, horloge locale) ; le
    point Chainlink ``s`` vaut ``price(s − lag)``.
    """

    def __init__(self, S: int, E: int, L: int = L_S, lag_s: float = 0.0):
        if E - S <= L:
            raise ValueError("la fenêtre doit durer plus que la moyenne de L secondes")
        self.S, self.E, self.L, self.lag = int(S), int(E), int(L), float(lag_s)
        self.A = np.arange(self.S - L + 1, self.S + 1, dtype="int64")
        self.B = np.arange(self.E - L + 1, self.E + 1, dtype="int64")
        self.k_lo = self.S - L                          # g = 0 pour k <= S − L
        k = np.arange(self.k_lo, self.E + 2, dtype="int64")
        cnt_b = np.clip(self.E - np.maximum(k, self.E - L + 1) + 1, 0, L)
        cnt_a = np.clip(self.S - np.maximum(k, self.S - L + 1) + 1, 0, L)
        g2 = ((cnt_b - cnt_a) / L) ** 2
        g2[k > self.E] = 0.0
        self._g2 = g2
        self._suffix = np.concatenate([np.cumsum(g2[::-1])[::-1], [0.0]])   # Σ_{j >= k} g_j²
        self.xA = self.xB = None

    def set_realized(self, series: PriceSeries) -> "TwapFormula":
        """Points réalisés : log-prix à ``s − lag`` pour chaque seconde des deux moyennes."""
        self.xA = series.at(self.A - self.lag)
        self.xB = series.at(self.B - self.lag)
        self._cA = np.concatenate([[0.0], np.cumsum(self.xA)])
        self._cB = np.concatenate([[0.0], np.cumsum(self.xB)])
        return self

    def var_factor(self, t) -> np.ndarray:
        """Var(F − K | t)/σ² (secondes)."""
        tau = np.asarray(t, dtype="float64") + self.lag
        k0 = np.floor(tau).astype("int64") + 1
        k0c = np.clip(k0, self.k_lo, self.E + 1)
        i = k0c - self.k_lo
        g2_first = self._g2[np.minimum(i, self._g2.size - 1)]
        part = np.where(k0 <= self.k_lo, 0.0, (k0 - tau) * g2_first)
        out = part + self._suffix[np.minimum(i + 1, self._suffix.size - 1)]
        return np.where(k0 > self.E, 0.0, out)

    def mean(self, t, x_now) -> np.ndarray:
        """E[F − K | t] en log-prix, ``x_now`` = log-prix courant (Binance à t)."""
        if self.xA is None:
            raise RuntimeError("set_realized() d'abord")
        tau = np.asarray(t, dtype="float64") + self.lag
        fl = np.floor(tau).astype("int64")
        nA = np.clip(fl - (self.S - self.L), 0, self.L)
        nB = np.clip(fl - (self.E - self.L), 0, self.L)
        x = np.asarray(x_now, dtype="float64")
        sum_b = self._cB[nB] + (self.L - nB) * x
        sum_a = self._cA[nA] + (self.L - nA) * x
        # x_now n'intervient pas quand tous les points sont connus (0 × NaN évité)
        sum_b = np.where(nB == self.L, self._cB[self.L], sum_b)
        sum_a = np.where(nA == self.L, self._cA[self.L], sum_a)
        return (sum_b - sum_a) / self.L

    def phase(self, t) -> np.ndarray:
        tau = np.asarray(t, dtype="float64") + self.lag
        return np.select([tau <= self.S - self.L, tau <= self.S, tau <= self.E - self.L], [1, 2, 3], 4)

    def prob(self, t, x_now, sigma, extra_sd: float = 0.0) -> np.ndarray:
        """P(Up | t) = Φ(m / √(σ² Var + extra_sd²)) ; ``extra_sd`` = erreur de suivi Chainlink − Binance
        (log) de F − K ; écart-type nul : 1 si m >= 0, sinon 0."""
        m = self.mean(t, x_now)
        sd = np.sqrt((np.asarray(sigma, dtype="float64") ** 2) * self.var_factor(t) + extra_sd ** 2)
        with np.errstate(divide="ignore", invalid="ignore"):
            p = ndtr(m / sd)
        p = np.where(sd > 0, p, np.where(m >= 0, 1.0, 0.0))
        return np.where(np.isfinite(m) & np.isfinite(sd), p, np.nan)


# ---------------------------------------------------------------------------
# Carnet Polymarket sur l'horloge locale
# ---------------------------------------------------------------------------
@dataclass
class BookTimeline:
    """État du carnet (repère Up) après chaque événement de carnet, trié par réception locale."""

    rx: np.ndarray        # s, horloge locale
    ts: np.ndarray        # s, horodatage serveur
    bid: np.ndarray       # meilleur bid Up
    ask: np.ndarray       # meilleur ask Up
    bid_sz: np.ndarray
    ask_sz: np.ndarray
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def mid(self) -> np.ndarray:
        return (self.bid + self.ask) / 2.0

    def idx(self, t) -> np.ndarray:
        return asof_index(self.rx, t)

    def side_at(self, side: int, t) -> tuple[np.ndarray, np.ndarray]:
        """(meilleur ask, taille) du jeton ``side`` (+1 Up, −1 Down) vus localement à ``t``."""
        j = self.idx(t)
        ok = j >= 0
        jj = np.maximum(j, 0)
        if side == 1:
            a, s = self.ask[jj], self.ask_sz[jj]
        else:
            a, s = np.round(1.0 - self.bid[jj], 3), self.bid_sz[jj]
        return np.where(ok, a, np.nan), np.where(ok, s, 0.0)

    def mid_at(self, t) -> np.ndarray:
        return asof_values(self.rx, self.mid, t)


def book_timeline(market: LiveMarket, t_from: float | None = None, t_to: float | None = None,
                  consolidate: bool = True) -> BookTimeline:
    """Rejoue les événements du marché ; garde l'état après chaque événement de carnet dont la réception
    tombe dans [t_from, t_to] (s, local). Les états sans instantané reçu sont ignorés.

    ``consolidate`` : une opération du CLOB (annulation + nouvel ordre, trade + instantané…) arrive en
    plusieurs messages de **même horodatage serveur**, à quelques µs d'intervalle ; seuls les états après
    le dernier message de chaque série consécutive de même ``ts`` sont gardés (les états intermédiaires
    n'existent pas pour un ordre qui arrive des dizaines de ms plus tard)."""
    rx, ts, bb, ba, bs, as_ = [], [], [], [], [], []
    book = Book()
    lo = -math.inf if t_from is None else t_from * 1e9
    hi = math.inf if t_to is None else t_to * 1e9
    for ev in market.events:
        if not apply_event(book, ev):
            continue
        if ev[0] < lo or ev[0] > hi or book.n_snapshots == 0:
            continue
        bids, asks = book.bids, book.asks
        if bids:
            pb = max(bids)
            bb.append(pb / 1000.0)
            bs.append(bids[pb])
        else:
            bb.append(math.nan)
            bs.append(0.0)
        if asks:
            pa = min(asks)
            ba.append(pa / 1000.0)
            as_.append(asks[pa])
        else:
            ba.append(math.nan)
            as_.append(0.0)
        rx.append(ev[0] / 1e9)
        ts.append(ev[1] / 1e3)
    f = lambda v: np.asarray(v, dtype="float64")  # noqa: E731
    order = np.argsort(f(rx), kind="stable")
    cols = [f(v)[order] for v in (rx, ts, bb, ba, bs, as_)]
    if consolidate and cols[0].size > 1:
        keep = np.append(cols[1][1:] != cols[1][:-1], True)
        cols = [c[keep] for c in cols]
    tl = BookTimeline(*cols)
    tl.trades = trades_up_frame(market)
    return tl


def trades_up_frame(market: LiveMarket) -> pd.DataFrame:
    """Trades preneurs : ``rx``, ``ts`` (s), ``consumes`` (``ask``/``bid`` Up), ``level_up``, ``size``."""
    rows = []
    for ev in market.events:
        if ev[2] != "trade":
            continue
        price, size, side, _tx = ev[4]
        cons, lvl = consumed_level(ev[3], side, price)
        rows.append((ev[0] / 1e9, ev[1] / 1e3, cons, round(float(lvl), 3), float(size)))
    return pd.DataFrame(rows, columns=["rx", "ts", "consumes", "level_up", "size"])


def ladders_at(market: LiveMarket, times: Sequence[float], depth: int = 10) -> list[dict]:
    """Carnet complet (``depth`` niveaux) vu localement aux instants ``times`` (s) :
    ``{"up": [(ask, taille)…], "down": [(ask Down, taille)…]}`` (asks croissants)."""
    times = np.asarray(times, dtype="float64")
    order = np.argsort(times, kind="stable")
    out: list[dict] = [{"up": [], "down": []} for _ in range(times.size)]
    book = Book()
    ev = market.events
    i, n = 0, len(ev)
    for k in order:
        lim = times[k] * 1e9
        while i < n and ev[i][0] <= lim:
            apply_event(book, ev[i])
            i += 1
        if book.n_snapshots == 0:
            continue
        up = sorted(book.asks.items())[:depth]
        dn = sorted(book.bids.items(), reverse=True)[:depth]
        out[k] = {"up": [(p / 1000.0, s) for p, s in up], "down": [(round(1.0 - p / 1000.0, 3), s) for p, s in dn]}
    return out


# ---------------------------------------------------------------------------
# Avantage preneur
# ---------------------------------------------------------------------------
def taker_cost(ask, fee_rate: float = FEE_RATE):
    """Coût d'une part achetée au prix ``ask``, frais preneur 0,07·a·(1 − a) inclus."""
    a = np.asarray(ask, dtype="float64")
    return a + fee_rate * a * (1.0 - a)


def side_prices(tl: BookTimeline, t) -> dict[str, np.ndarray]:
    """Asks et tailles des deux jetons vus à ``t`` (tableau)."""
    a_up, s_up = tl.side_at(1, t)
    a_dn, s_dn = tl.side_at(-1, t)
    return {"ask_up": a_up, "size_up": s_up, "ask_dn": a_dn, "size_dn": s_dn, "mid": tl.mid_at(t)}


# ---------------------------------------------------------------------------
# 2. Réaction du carnet aux sauts de la formule
# ---------------------------------------------------------------------------
REACTION_COLS = ["t_event", "t_prev", "t0", "dP", "p_prev", "p_event", "mid0", "phase", "delay_first_ms",
                 "delay_ask_ms", "delay_bid_ms", "delay_placebo_ms", "delay_50_ms", "delay_90_ms", "censored_50",
                 "censored_90"]


def reaction_events(grid_t: np.ndarray, P: np.ndarray, tl: BookTimeline, prob_at: Callable[[np.ndarray], np.ndarray],
                    price_times: np.ndarray, jump: float = 0.05, window_s: float = 1.0, refractory_s: float = 2.0,
                    horizon_s: float = 30.0, phase: np.ndarray | None = None, placebo_s: float = 3.0) -> pd.DataFrame:
    """Sauts de P_formule d'au moins ``jump`` en ``window_s`` et délai de réaction du carnet.

    Événement : premier point ``k`` de la grille où ``|P[k] − P[k − w]| >= jump`` (puis rien pendant
    ``refractory_s``). ``t0`` : premier instant de ``(t_prev, t_k]`` (mises à jour du prix comprises) où la
    formule a fait la moitié du saut ``dP`` : c'est l'heure du mouvement vu d'ici. Délais (ms, depuis
    ``t0``, négatifs possibles : le carnet a bougé avant) : ``delay_50``/``delay_90`` = premier événement
    de carnet (depuis ``t_prev``) où le milieu a parcouru 50 % / 90 % de ``dP`` depuis ``mid0 =
    mid(t_prev)`` ; ``delay_ask``/``delay_bid`` = premier changement du meilleur ask / bid Up dans le
    sens de ``dP`` ; ``delay_first`` = le plus tôt des deux. Pas de réaction dans ``horizon_s`` : censuré.
    """
    g = np.asarray(grid_t, dtype="float64")
    P = np.asarray(P, dtype="float64")
    if g.size < 3:
        return pd.DataFrame(columns=REACTION_COLS)
    step = float(np.median(np.diff(g)))
    w = max(1, int(round(window_s / step)))
    dP_all = np.full(g.size, np.nan)
    dP_all[w:] = P[w:] - P[:-w]
    cand = np.flatnonzero(np.abs(np.nan_to_num(dP_all, nan=0.0)) >= jump)
    rows = []
    last = -math.inf
    mids = tl.mid
    pt = np.asarray(price_times, dtype="float64")
    for k in cand:
        if g[k] - last < refractory_s:
            continue
        last = g[k]
        t_prev, t_ev = g[k - w], g[k]
        dP = P[k] - P[k - w]
        sgn = 1.0 if dP > 0 else -1.0
        # instant du mouvement : mises à jour du prix dans (t_prev, t_ev]
        i0, i1 = np.searchsorted(pt, [t_prev, t_ev], side="right")
        tt = np.concatenate([pt[i0:i1], g[k - w + 1:k + 1]])
        tt.sort()
        pp = prob_at(tt)
        hit = np.flatnonzero(sgn * (pp - P[k - w]) >= abs(dP) / 2.0)
        t0 = float(tt[hit[0]]) if hit.size else float(t_ev)
        j_prev = int(tl.idx(t_prev))
        row = {"t_event": t_ev, "t_prev": t_prev, "t0": t0, "dP": dP, "p_prev": P[k - w], "p_event": P[k],
               "mid0": math.nan, "phase": int(phase[k]) if phase is not None else -1,
               "delay_first_ms": math.nan, "delay_ask_ms": math.nan, "delay_bid_ms": math.nan,
               "delay_placebo_ms": math.nan, "delay_50_ms": math.nan, "delay_90_ms": math.nan, "censored_50": True,
               "censored_90": True}
        if j_prev >= 0 and np.isfinite(mids[j_prev]):
            mid0 = mids[j_prev]
            row["mid0"] = mid0
            j_end = int(np.searchsorted(tl.rx, t_prev + horizon_s, side="right"))
            seg = slice(j_prev + 1, j_end)
            rx = tl.rx[seg]
            move = sgn * (mids[seg] - mid0)
            move = np.where(np.isfinite(move), move, -np.inf)
            for frac, key in ((0.5, "50"), (0.9, "90")):
                h = np.flatnonzero(move >= frac * abs(dP) - 1e-12)
                if h.size:
                    row[f"delay_{key}_ms"] = (rx[h[0]] - t0) * 1e3
                    row[f"censored_{key}"] = False
            # 1re retouche dans le sens du saut, depuis t0 et par rapport aux prix affichés à t0
            j0 = int(tl.idx(t0))
            if j0 >= 0:
                j_hi = int(np.searchsorted(tl.rx, t0 + horizon_s, side="right"))
                s2 = slice(j0 + 1, j_hi)
                da = sgn * (tl.ask[s2] - tl.ask[j0])
                db = sgn * (tl.bid[s2] - tl.bid[j0])
                ha = np.flatnonzero(np.nan_to_num(da, nan=0.0) > 1e-9)
                hb = np.flatnonzero(np.nan_to_num(db, nan=0.0) > 1e-9)
                if ha.size:
                    row["delay_ask_ms"] = (tl.rx[j0 + 1 + ha[0]] - t0) * 1e3
                if hb.size:
                    row["delay_bid_ms"] = (tl.rx[j0 + 1 + hb[0]] - t0) * 1e3
                if ha.size or hb.size:
                    row["delay_first_ms"] = np.nanmin([row["delay_ask_ms"], row["delay_bid_ms"]])
            # placebo : même mesure à t0 − placebo_s, arrêtée à t0
            tp = t0 - placebo_s
            jp = int(tl.idx(tp))
            if jp >= 0:
                j_hi = int(np.searchsorted(tl.rx, t0, side="left"))
                s3 = slice(jp + 1, j_hi)
                da = sgn * (tl.ask[s3] - tl.ask[jp])
                db = sgn * (tl.bid[s3] - tl.bid[jp])
                h = np.flatnonzero((np.nan_to_num(da, nan=0.0) > 1e-9) | (np.nan_to_num(db, nan=0.0) > 1e-9))
                if h.size:
                    row["delay_placebo_ms"] = (tl.rx[jp + 1 + h[0]] - tp) * 1e3
        rows.append(row)
    return pd.DataFrame(rows, columns=REACTION_COLS)


# ---------------------------------------------------------------------------
# 3. Prix périmés : opportunités et durée de vie
# ---------------------------------------------------------------------------
OPP_COLS = ["side", "t_start", "ask0", "size0", "p_side", "edge0", "p_side_1s", "edge_1s", "trigger", "t_end_book",
            "t_end_edge", "life_book_ms", "life_edge_ms", "censored", "idx"]


def find_opportunities(T: np.ndarray, P: np.ndarray, ask_up: np.ndarray, size_up: np.ndarray, ask_dn: np.ndarray,
                       size_dn: np.ndarray, margin: float = 0.0, horizon_s: float = 120.0,
                       fee_rate: float = FEE_RATE, lookback_s: float = 1.0, jump: float = 0.01,
                       ask_range: tuple[float, float] = (0.0, 1.0)) -> pd.DataFrame:
    """Opportunités preneur : ``P_côté(t) − coût(ask_côté(t)) > margin`` sur la chronologie ``T``.

    Une opportunité = un **niveau de prix** (côté, ask0) tant qu'il reste dans le carnet : elle commence
    au premier instant où l'avantage dépasse la marge à ce niveau et se termine (``t_end_book``) quand le
    meilleur ask du côté passe **au-dessus** de ask0 (niveau annulé ou consommé ; un ask plus bas ne la
    termine pas). ``t_end_edge`` : premier instant où l'avantage (au meilleur ask courant) repasse sous la
    marge (le prix Binance est revenu, ou le niveau a disparu). Un niveau encore vivant n'ouvre pas de
    nouvelle opportunité. ``censored`` : fin non observée dans ``horizon_s`` ou dans les données.

    ``trigger`` (origine, d'après l'état ``lookback_s`` plus tôt) : ``persistante`` si l'avantage dépassait
    déjà la marge (au meilleur ask d'alors) ; sinon ``saut`` si P_côté a monté d'au moins ``jump`` (le
    prix Binance a bougé : prix périmé au sens strict) ; sinon ``carnet`` (un ask moins cher est apparu).
    ``ask_range`` : seuls les asks dans cet intervalle ouvrent une opportunité (les queues 0,01–0,04 et
    0,96–0,99 relèvent surtout de l'erreur de modèle).
    """
    T = np.asarray(T, dtype="float64")
    P = np.asarray(P, dtype="float64")
    jb = np.searchsorted(T, T - lookback_s, side="right") - 1
    okb = jb >= 0
    jb = np.maximum(jb, 0)
    rows = []
    for side, ask, size in ((1, ask_up, size_up), (-1, ask_dn, size_dn)):
        ask = np.asarray(ask, dtype="float64")
        size = np.asarray(size, dtype="float64")
        ps = P if side == 1 else 1.0 - P
        edge = ps - taker_cost(ask, fee_rate)
        ps_b = np.where(okb, ps[jb], np.nan)
        edge_b = np.where(okb, edge[jb], np.nan)
        on = (np.nan_to_num(edge, nan=-1.0) > margin + _EPS) & (ask >= ask_range[0] - _EPS) & (ask <= ask_range[1] + _EPS)
        prev_on = np.concatenate([[False], on[:-1]])
        prev_ask = np.concatenate([[np.nan], ask[:-1]])
        cand = np.flatnonzero(on & (~prev_on | (np.abs(ask - prev_ask) > _EPS)))
        live: dict[float, float] = {}
        for i in cand:
            lvl = round(float(ask[i]), 3)
            if T[i] < live.get(lvl, -math.inf):
                continue
            j_end = int(np.searchsorted(T, T[i] + horizon_s, side="right"))
            seg_ask = ask[i + 1:j_end]
            gone = np.flatnonzero(~(seg_ask <= lvl + _EPS))          # ask plus haut ou carnet vide (NaN)
            off = np.flatnonzero(~on[i + 1:j_end])
            censored = gone.size == 0
            t_end = T[i + 1 + gone[0]] if gone.size else T[min(j_end, T.size) - 1]
            t_edge = T[i + 1 + off[0]] if off.size else t_end
            live[lvl] = t_end if not censored else math.inf
            if np.nan_to_num(edge_b[i], nan=-1.0) > margin + _EPS:
                trig = "persistante"
            elif np.isfinite(ps_b[i]) and ps[i] - ps_b[i] >= jump:
                trig = "saut"
            else:
                trig = "carnet"
            rows.append({"side": "up" if side == 1 else "down", "t_start": T[i], "ask0": lvl, "size0": size[i],
                         "p_side": ps[i], "edge0": edge[i], "p_side_1s": ps_b[i], "edge_1s": edge_b[i], "trigger": trig,
                         "t_end_book": t_end, "t_end_edge": min(t_edge, t_end),
                         "life_book_ms": (t_end - T[i]) * 1e3, "life_edge_ms": (min(t_edge, t_end) - T[i]) * 1e3,
                         "censored": bool(censored), "idx": int(i)})
    df = pd.DataFrame(rows, columns=OPP_COLS)
    return df.sort_values("t_start", kind="stable").reset_index(drop=True)


def removal_cause(opps: pd.DataFrame, trades: pd.DataFrame, slack_s: float = 0.25) -> pd.DataFrame:
    """Qui a retiré le niveau : ``preneur`` (un trade l'a consommé entre le début et la fin + ``slack_s``)
    ou ``retrait`` (annulé par le teneur). Ajoute ``removed_by``, ``taken_size`` (parts échangées) et
    ``first_take_rx`` / ``first_take_ts`` (réception locale et horodatage serveur du premier trade sur ce
    niveau, s ; NaN sans trade) : avec le délai preneur, un trade apparié peu après la détection vient d'un
    ordre parti **avant** le mouvement, pas d'un preneur plus rapide."""
    out = opps.copy()
    by, taken, f_rx, f_ts = [], [], [], []
    tr = trades if trades is not None and len(trades) else pd.DataFrame(columns=["rx", "ts", "consumes", "level_up",
                                                                                   "size"])
    trx = tr["rx"].to_numpy(dtype="float64") if len(tr) else np.array([])
    for r in out.itertuples(index=False):
        if r.censored:
            by.append("non observé")
            taken.append(math.nan)
            f_rx.append(math.nan)
            f_ts.append(math.nan)
            continue
        i0, i1 = np.searchsorted(trx, [r.t_start, r.t_end_book + slack_s])
        sub = tr.iloc[i0:i1]
        if r.side == "up":
            m = (sub["consumes"] == "ask") & (np.abs(sub["level_up"] - r.ask0) < 1e-6)
        else:
            m = (sub["consumes"] == "bid") & (np.abs(sub["level_up"] - round(1.0 - r.ask0, 3)) < 1e-6)
        s = float(sub.loc[m, "size"].sum()) if len(sub) else 0.0
        by.append("preneur" if s > 0 else "retrait")
        taken.append(s)
        first = sub.loc[m].iloc[0] if s > 0 else None
        f_rx.append(float(first["rx"]) if first is not None else math.nan)
        f_ts.append(float(first["ts"]) if first is not None and "ts" in sub else math.nan)
    out["removed_by"] = by
    out["taken_size"] = taken
    out["first_take_rx"] = f_rx
    out["first_take_ts"] = f_ts
    return out


def cooldown_mask(t: Sequence[float], keys: Sequence, cooldown_s: float = 1.0) -> np.ndarray:
    """Un ordre par clé (p. ex. marché × côté) et par ``cooldown_s`` : ``True`` pour les instants retenus.

    Un saut de Binance fait souvent apparaître plusieurs niveaux périmés en quelques ms (le teneur retire ses
    prix un à un) ; un robot n'envoie qu'**un** ordre, et la simulation ne retire pas du carnet les parts
    qu'il a déjà prises : sans ce filtre, les mêmes parts seraient achetées plusieurs fois. Un instant est
    retenu s'il suit d'au moins ``cooldown_s`` le dernier instant **retenu** de la même clé."""
    t = np.asarray(t, dtype="float64")
    keys = np.asarray(keys, dtype=object)
    keep = np.zeros(t.size, dtype=bool)
    last: dict = {}
    for i in np.argsort(t, kind="stable"):
        k = keys[i]
        if t[i] >= last.get(k, -math.inf) + cooldown_s - _EPS:
            keep[i] = True
            last[k] = t[i]
    return keep


# ---------------------------------------------------------------------------
# 4. Exécution à t + ℓ et P&L
# ---------------------------------------------------------------------------
def simulate_fills(opps: pd.DataFrame, tl: BookTimeline, latencies_ms: Sequence[int] = LATENCIES_MS,
                   margin: float = 0.0, max_shares: float = MAX_SHARES, outcome_up: bool | None = None,
                   markout_s: float = 10.0, fee_rate: float = FEE_RATE, end_s: float | None = None) -> pd.DataFrame:
    """Pour chaque opportunité détectée à ``t_start`` et chaque latence ℓ : ordre au carnet vu à
    ``t_start + ℓ`` ; achat au meilleur ask du côté si ``p_side(t) − coût(ask) > margin`` (limite
    calculée à t), quantité ``min(taille au meilleur ask, max_shares)``.

    Colonnes : ``latency_ms``, ``filled``, ``fill_ask``, ``qty``, ``edge_fill`` (avantage attendu par
    part au prix payé), ``pnl`` (issue officielle ; NaN si inconnue), ``markout`` (valorisé au milieu du
    côté ``markout_s`` après l'exécution ; à l'issue si cet instant dépasse la clôture ``end_s`` ou si le
    carnet n'a plus de milieu) et les colonnes d'identification de l'opportunité.
    """
    rows = []
    if opps.empty:
        return pd.DataFrame(columns=["opp_id", "side", "t_start", "latency_ms", "filled", "fill_ask", "qty",
                                     "edge_fill", "pnl", "markout", "pnl_per_share"])
    t0 = opps["t_start"].to_numpy(dtype="float64")
    ps = opps["p_side"].to_numpy(dtype="float64")
    sides = np.where(opps["side"].to_numpy() == "up", 1, -1)
    for lat in latencies_ms:
        tf = t0 + lat / 1e3
        a_up, s_up = tl.side_at(1, tf)
        a_dn, s_dn = tl.side_at(-1, tf)
        a = np.where(sides == 1, a_up, a_dn)
        s = np.where(sides == 1, s_up, s_dn)
        cost = taker_cost(a, fee_rate)
        edge = ps - cost
        filled = np.nan_to_num(edge, nan=-1.0) > margin + _EPS
        qty = np.where(filled, np.minimum(np.nan_to_num(s, nan=0.0), max_shares), 0.0)
        filled &= qty > 0
        mid_later = tl.mid_at(tf + markout_s)
        mid_side = np.where(sides == 1, mid_later, 1.0 - mid_later)
        if outcome_up is None:
            win = np.full(t0.size, np.nan)
        else:
            win = np.where(sides == 1, float(outcome_up), 1.0 - float(outcome_up))
        after = (tf + markout_s >= end_s) if end_s is not None else np.zeros(t0.size, bool)
        mid_side = np.where(after | ~np.isfinite(mid_side), win, mid_side)
        pnl = np.where(filled, qty * (win - cost), 0.0)
        mk = np.where(filled, qty * (mid_side - cost), 0.0)
        known = outcome_up is not None
        rows.append(pd.DataFrame({
            "opp_id": np.arange(t0.size), "side": np.where(sides == 1, "up", "down"), "t_start": t0,
            "latency_ms": int(lat), "filled": filled, "fill_ask": np.where(filled, a, np.nan), "qty": qty,
            "edge_fill": np.where(filled, edge, np.nan), "pnl": pnl if known else np.full(t0.size, np.nan),
            "markout": mk, "pnl_per_share": np.where(filled & known, win - cost, np.nan)}))
    return pd.concat(rows, ignore_index=True)


def pnl_curve(fills: pd.DataFrame, value: str = "pnl", group: str = "market") -> pd.DataFrame:
    """Par latence : opportunités, exécutions, taux d'exécution, parts, P&L total / par opportunité /
    par part (``value`` = ``pnl`` ou ``markout``)."""
    rows = []
    for lat, g in fills.groupby("latency_ms", sort=True):
        n = len(g)
        nf = int(g["filled"].sum())
        sh = float(g["qty"].sum())
        tot = float(g[value].sum())
        rows.append({"latency_ms": int(lat), "n_opp": n, "n_filled": nf, "fill_rate": nf / n if n else math.nan,
                     "shares": sh, f"{value}_total": tot, f"{value}_per_opp": tot / n if n else math.nan,
                     f"{value}_per_share": tot / sh if sh > 0 else math.nan,
                     "n_groups": g[group].nunique() if group in g else math.nan})
    return pd.DataFrame(rows)


def bootstrap_by_group(fills: pd.DataFrame, value: str = "pnl", group: str = "market", n_boot: int = 2000,
                       seed: int = 7, denom: float | None = None) -> dict[str, np.ndarray]:
    """Bootstrap groupé (tirage des ``group`` avec remise). Renvoie ``latencies``, ``point`` (moyenne par
    opportunité, ou total / ``denom`` si ``denom`` est donné) et ``reps`` (n_boot × n_latences)."""
    lats = np.array(sorted(fills["latency_ms"].unique()), dtype="int64")
    groups = np.array(sorted(fills[group].unique()))
    if groups.size == 0:
        return {"latencies": lats, "point": np.full(lats.size, np.nan), "reps": np.empty((0, lats.size))}
    fills = fills[np.isfinite(fills[value].to_numpy(dtype="float64"))]
    gi = {g: i for i, g in enumerate(groups)}
    li = {lat: i for i, lat in enumerate(lats)}
    sums = np.zeros((groups.size, lats.size))
    cnts = np.zeros((groups.size, lats.size))
    gcol = fills[group].map(gi).to_numpy()
    lcol = fills["latency_ms"].map(li).to_numpy()
    np.add.at(sums, (gcol, lcol), fills[value].to_numpy(dtype="float64"))
    np.add.at(cnts, (gcol, lcol), 1.0)

    def stat(s: np.ndarray, c: np.ndarray) -> np.ndarray:
        if denom is not None:
            return s / denom
        with np.errstate(invalid="ignore", divide="ignore"):
            return s / c

    point = stat(sums.sum(0), cnts.sum(0))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, groups.size, size=(n_boot, groups.size))
    reps = np.stack([stat(sums[r].sum(0), cnts[r].sum(0)) for r in idx]) if n_boot else np.empty((0, lats.size))
    return {"latencies": lats, "point": point, "reps": reps}


def isotonic_decreasing(values: Sequence[float], weights: Sequence[float] | None = None) -> np.ndarray:
    """Régression isotone **décroissante** (algorithme PAVA) : la suite non croissante la plus proche au sens
    des moindres carrés pondérés. Sert à lisser une courbe de P&L selon la latence, qui ne peut que baisser
    en espérance quand on arrive plus tard."""
    y = np.asarray(values, dtype="float64")
    w = np.ones_like(y) if weights is None else np.asarray(weights, dtype="float64")
    blocks: list[list[float]] = []            # [moyenne, poids, longueur]
    for yi, wi in zip(y, w):
        blocks.append([yi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            m2, w2, n2 = blocks.pop()
            m1, w1, n1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    return np.concatenate([np.full(int(n), m) for m, _, n in blocks]) if blocks else y


def breakeven_latency(latencies: Sequence[float], values: Sequence[float], monotone: bool = False) -> float:
    """Première latence où la courbe de P&L devient <= 0 (interpolation linéaire entre les points de la
    grille). 0 si déjà <= 0 à la première latence ; ``inf`` si elle reste > 0. ``monotone`` : la courbe
    est d'abord rendue décroissante (:func:`isotonic_decreasing`), ce qui stabilise ℓ* quand le P&L oscille
    autour de 0."""
    x = np.asarray(latencies, dtype="float64")
    y = np.asarray(values, dtype="float64")
    ok = np.isfinite(y)
    x, y = x[ok], y[ok]
    if monotone and y.size:
        y = isotonic_decreasing(y)
    if y.size == 0:
        return math.nan
    if y[0] <= 0:
        return float(x[0])
    for i in range(1, y.size):
        if y[i] <= 0:
            return float(x[i - 1] + (x[i] - x[i - 1]) * y[i - 1] / (y[i - 1] - y[i]))
    return math.inf


def last_significant_latency(latencies: Sequence[float], lower_bounds: Sequence[float]) -> float:
    """Plus grande latence ℓ de la grille telle que la borne basse de l'IC reste > 0 pour **toutes** les
    latences <= ℓ (gain démontré). NaN si ce n'est pas le cas dès la première."""
    x = np.asarray(latencies, dtype="float64")
    lo = np.asarray(lower_bounds, dtype="float64")
    ok = np.nan_to_num(lo, nan=-np.inf) > 0
    if not ok.size or not ok[0]:
        return math.nan
    stop = np.flatnonzero(~ok)
    return float(x[stop[0] - 1]) if stop.size else float(x[-1])


# ---------------------------------------------------------------------------
# Latences de transport et retard Chainlink
# ---------------------------------------------------------------------------
def summarize_latency(delays_ms: np.ndarray) -> dict:
    d = np.asarray(delays_ms, dtype="float64")
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"n": 0, "p01": math.nan, "p10": math.nan, "median": math.nan, "p90": math.nan, "p99": math.nan}
    q = np.percentile(d, [1, 10, 50, 90, 99])
    return {"n": int(d.size), "p01": q[0], "p10": q[1], "median": q[2], "p90": q[3], "p99": q[4]}


def lag_scan(ref_ts: np.ndarray, ref_logp: np.ndarray, series: PriceSeries, lags_s: np.ndarray,
             horizon_s: float = 1.0) -> pd.DataFrame:
    """Corrélation entre les rendements de la référence (Chainlink, points aux instants ``ref_ts``) sur
    ``horizon_s`` et ceux de ``series`` (Binance) sur la même fenêtre décalée de ``−lag``.

    Chainlink(s) ≈ Binance(s − lag) : le ``lag`` de corrélation maximale est le retard de Chainlink.
    """
    ts = np.asarray(ref_ts, dtype="float64")
    x = np.asarray(ref_logp, dtype="float64")
    pos = {round(t, 3): i for i, t in enumerate(ts)}
    prev = np.array([pos.get(round(t - horizon_s, 3), -1) for t in ts])
    ok = prev >= 0
    r_ref = np.where(ok, x - x[np.maximum(prev, 0)], np.nan)
    rows = []
    for lag in np.asarray(lags_s, dtype="float64"):
        b1 = series.at(ts - lag)
        b0 = series.at(ts - horizon_s - lag)
        r_b = b1 - b0
        m = ok & np.isfinite(r_b) & np.isfinite(r_ref)
        c = float(np.corrcoef(r_ref[m], r_b[m])[0, 1]) if m.sum() > 10 and np.std(r_b[m]) > 0 else math.nan
        rows.append({"lag_ms": int(round(lag * 1e3)), "corr": c, "n": int(m.sum())})
    return pd.DataFrame(rows)
