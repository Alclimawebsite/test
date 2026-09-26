"""Signal « live » (papier) : la formule exacte P(Up) appliquée en direct aux marchés Polymarket
« Up or Down » (``python -m tradebot polymarket-signal``).

Toutes les N secondes :

1. marchés en cours et suivant (:meth:`PolymarketClient.current_and_next_markets`) ;
2. prix Binance à la seconde (``data-api.binance.vision/api/v3/klines?interval=1s``) :
   **p(u) = close de la bougie 1 s ouverte à u − 1**, c.-à-d. le dernier prix connu à la seconde u.
   C'est la convention du diagnostic : TWAP60(T) = moyenne des closes des bougies ouvertes dans
   [T − 60, T) = moyenne de p sur (T − 60, T] (accord de 98,6 % avec l'issue officielle) ;
3. t = dernière seconde connue ; phase (1 : t ≤ S − 60 ; 2 : S − 60 < t ≤ S ; 3 : S < t ≤ E − 60 ;
   4 : E − 60 < t ≤ E), moyenne de départ réalisée Ā sur (S − 60, t] (phase 2), K = TWAP60(S)
   Binance (phases 3-4), somme finale réalisée B = Σ p(u) / 60 sur (E − 60, t] (phase 4) ;
4. σ par √s : Parkinson sur les hauts/bas des 60 dernières bougies 1 min (défaut), ou EWMA des
   rendements 1 s (demi-vie 600 s, choisie sur la 1re moitié dans ``reports/polymarket/maker/``) ;
5. P(Up) = Φ(m/s) (:func:`tradebot.polymarket_formula.fair_prob_up`, moyennes échantillonnées à 1 s),
   puis **erreur du proxy** : F − K est mesuré sur Binance, l'issue sur Chainlink ; l'écart entre les
   deux variations est ≈ gaussien, sans biais, d'écart-type σ_b ≈ 0,50 pb (BTC 5m + 15m, choisi sur
   le 04/09–13/09). Décision sur P = Φ(m / √(s² + σ_b²)) ; ``basis_sd = 0`` redonne la formule pure.
   Sans ce terme, s -> 0 à la clôture et la formule annonce 0 % ou 100 % même quand F − K Binance ne
   vaut que quelques dixièmes de pb. Vu en direct le 26/09 (btc-updown-5m-1790418000, E − 2 s) :
   F − K Binance = +0,44 pb, formule pure 100 % Up, ask Up 0,01–0,07 ; issue officielle Down
   (F − K Chainlink = −0,42 pb). Avec σ_b : Φ(0,44/0,50) ≈ 81 % ;
6. carnet CLOB des jetons Up et Down ; espérance d'un achat preneur de chaque côté
   (:func:`tradebot.polymarket_formula.taker_edge`, frais 0,07·a·(1 − a)) ; décision :
   acheter Up / acheter Down / rien (espérance > ``min_edge``).

Tout est en log-prix. K et F sont mesurés sur Binance : l'écart de niveau Chainlink ≈ Binance − 3 pb
s'annule dans F − K ; il reste l'erreur σ_b ci-dessus. Contrôle hors échantillon (14/09–24/09,
1 536 marchés BTC, à t = E) : Brier 0,0125 avec σ_b contre 0,0182 sans (ΔBrier −0,0058, IC 95 %
−0,0097 ; −0,0021, bootstrap groupé par créneau de 15 min), log-loss 0,044 contre 0,252.
Limites connues (non corrigées ici) : le flux Chainlink semble en retard d'≈ 4 s sur Binance
(décaler la fenêtre Binance de 4 s ramènerait σ_b à ≈ 0,34–0,40 pb sur le test ; non utilisé) ;
les cotations réagissent à Binance en ≈ 0,35 s, un avantage affiché peut donc avoir disparu avant
qu'un ordre n'arrive ; σ est supposé constant sur la fenêtre.

Journal CSV (``--log``) : une ligne par marché et par passage ; l'issue officielle (et les
``priceToBeat`` / ``finalPrice`` Chainlink) est ajoutée par un second passage
(:func:`fill_outcomes`, automatique pendant la boucle ou ``--fill-outcomes``), qui calcule aussi le
gain réalisé par part des décisions et un bilan (:func:`summarize_log`).

Simulation papier : aucun ordre n'est envoyé, aucune clé n'est utilisée. Polymarket est bloqué
en France (ANJ) : ne pas trader depuis la France.
"""

from __future__ import annotations

import csv
import logging
import math
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from tradebot import report
from tradebot.polymarket import ASSET_TO_SYMBOL, DURATION_SECONDS, GAMMA_URL, UpDownMarket, parse_market
from tradebot.polymarket_formula import (
    FEE_RATE, TWAP_WINDOW_S, ewma_sigma_per_s, fair_prob_up, parkinson_sigma_per_s, taker_cost, taker_edge,
)

__all__ = [
    "BINANCE_KLINES_URL", "PHASE_LABELS", "LOG_COLUMNS", "Signal", "SignalRunner", "PriceBuffer",
    "parse_klines", "phase_of", "formula_inputs", "sigma_from", "book_quotes", "decide", "evaluate_market",
    "format_line", "fill_outcomes", "summarize_log", "format_summary", "gamma_resolver", "run_signal",
    "PAPER_NOTICE", "BASIS_SD", "prob_with_basis",
]

log = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
HTTP_TIMEOUT_S = 5.0
L_S = int(TWAP_WINDOW_S)          # moyenne de 60 s
INITIAL_1S_BARS = 1000            # 1er appel : couvre (S − 60, maintenant] même pour un 15m (≤ 960 s)
INCREMENT_1S_LIMIT = 120          # appels suivants : bougies depuis la dernière connue
BUFFER_KEEP_S = 2400              # historique 1 s conservé (EWMA demi-vie 600 s, fenêtre 15m + 60 s)
PARKINSON_BARS = 60               # bougies 1 min pour Parkinson (parkinson_60 de l'état des lieux)
EWMA_HALFLIFE_S = 600.0           # demi-vie choisie sur la 1re moitié (reports/polymarket/maker)
STALE_S = 5.0                     # prix Binance plus vieux que ça : pas de décision
# Erreur Binance -> Chainlink sur F − K (log) : RMSE de [Δ Chainlink − Δ Binance TWAP60], BTC 5m + 15m,
# apprentissage 04/09–13/09 (3 840 marchés) : 0,497 pb, biais −0,002 pb ; test 14/09–24/09 : 0,56–0,62 pb.
BASIS_SD = 0.50e-4
TICK_OFFSET_S = 0.4               # la bougie 1 s close est publiée ≈ 0,1–0,4 s après la seconde
MARKETS_RETRY_S = 10.0            # nouvel essai gamma si le marché suivant manque
FILL_EVERY_S = 60.0               # second passage (issues) pendant la boucle
FILL_MIN_AGE_S = 60.0             # issue cherchée à partir de E + 60 s (résolution : 2 à 8 min)
N_BOOT = 2000
SLOT_S = 900                      # bootstrap groupé par créneau de 15 min

PHASE_LABELS = {1: "attente", 2: "départ", 3: "K connu", 4: "final"}
DECISIONS = {"up": "acheter Up", "down": "acheter Down", None: "rien"}

PAPER_NOTICE = (
    "SIMULATION PAPIER : aucun ordre n'est envoyé, aucune clé n'est utilisée ; les décisions "
    "affichées ne sont que des calculs.\n"
    "Légal : Polymarket est bloqué en France (ANJ, 16/07/2026) et ses CGU excluent les personnes "
    "situées en France (pays en « close-only ») : ne pas trader depuis la France, ne pas "
    "contourner le géoblocage.")

KlinesFn = Callable[..., list]
Resolver = Callable[[str], "dict | None"]


# ---------------------------------------------------------------------------
# Binance : bougies et prix à la seconde
# ---------------------------------------------------------------------------
def binance_klines(symbol: str, interval: str, *, limit: int = 120, start_ms: int | None = None,
                   end_ms: int | None = None, timeout: float = HTTP_TIMEOUT_S) -> list:
    """Klines brutes de l'API publique (listes de 12 champs) ; lève en cas d'erreur HTTP."""
    from tradebot.data import _session

    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": int(limit)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    r = _session().get(BINANCE_KLINES_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_klines(raw: Sequence[Sequence], now: float | None = None) -> pd.DataFrame:
    """Klines brutes -> DataFrame indexé par la seconde d'ouverture (int), colonnes ``high``,
    ``low``, ``close`` ; les bougies non closes à ``now`` (``close_time >= now``) sont retirées."""
    rows = [r for r in raw if now is None or int(r[6]) < now * 1000.0]
    if not rows:
        return pd.DataFrame({"high": [], "low": [], "close": []}, index=pd.Index([], dtype="int64", name="open_s"))
    df = pd.DataFrame(
        {"high": [float(r[2]) for r in rows], "low": [float(r[3]) for r in rows],
         "close": [float(r[4]) for r in rows]},
        index=pd.Index([int(r[0]) // 1000 for r in rows], dtype="int64", name="open_s"))
    return df[~df.index.duplicated(keep="last")].sort_index()


class PriceBuffer:
    """p(u) Binance à la seconde u (close de la bougie 1 s ouverte à u − 1), fenêtre glissante."""

    def __init__(self, keep_s: int = BUFFER_KEEP_S):
        self.keep_s = int(keep_s)
        self._close: dict[int, float] = {}

    def __len__(self) -> int:
        return len(self._close)

    @property
    def last(self) -> int | None:
        """Dernière seconde u connue (information la plus récente)."""
        return max(self._close) if self._close else None

    @property
    def first(self) -> int | None:
        return min(self._close) if self._close else None

    def clear(self) -> None:
        self._close.clear()

    def add_bars(self, bars: pd.DataFrame) -> None:
        for o, c in zip(bars.index, bars["close"]):
            if np.isfinite(c) and c > 0:
                self._close[int(o) + 1] = float(c)
        last = self.last
        if last is not None:
            for u in [u for u in self._close if u <= last - self.keep_s]:
                del self._close[u]

    def log_series(self) -> pd.Series:
        """log p(u) sur toutes les secondes de [first, last] (trous comblés par le dernier prix)."""
        if not self._close:
            return pd.Series([], dtype="float64", index=pd.Index([], dtype="int64"))
        s = pd.Series(self._close, dtype="float64").sort_index()
        s = s.reindex(np.arange(int(s.index[0]), int(s.index[-1]) + 1)).ffill()
        return np.log(s)


# ---------------------------------------------------------------------------
# Formule : phase, moyennes, σ
# ---------------------------------------------------------------------------
def phase_of(t: float, S: float, E: float, L: float = TWAP_WINDOW_S) -> int:
    """Phase de la formule à l'instant d'information t (1 à 4, voir polymarket_formula)."""
    if t <= S - L:
        return 1
    if t <= S:
        return 2
    if t <= E - L:
        return 3
    return 4


class MissingPrices(ValueError):
    """Les prix à la seconde ne couvrent pas la fenêtre de moyenne demandée."""


def _window(logp: pd.Series, a: int, b: int) -> np.ndarray:
    """log p(u) pour u dans (a, b] ; exige que toute la fenêtre soit couverte."""
    if b <= a:
        return np.array([], dtype="float64")
    if not len(logp) or logp.index[0] > a + 1 or logp.index[-1] < b:
        raise MissingPrices(f"prix manquants sur ({a}, {b}]")
    return logp.loc[a + 1: b].to_numpy()


@dataclass(frozen=True)
class FormulaInputs:
    t: int
    phase: int
    log_price: float
    start_avg: float | None = None        # phase 2 : moyenne de log p sur (S − 60, t]
    price_to_beat: float | None = None    # phases 3-4 : log K = moyenne de log p sur (S − 60, S]
    end_partial_sum: float | None = None  # phase 4 : Σ log p sur (E − 60, t] / 60

    def kwargs(self) -> dict:
        return {"start_avg": self.start_avg, "price_to_beat": self.price_to_beat,
                "end_partial_sum": self.end_partial_sum}


def formula_inputs(logp: pd.Series, t: int, S: int, E: int, *, k_log: float | None = None,
                   L: int = L_S) -> FormulaInputs:
    """Entrées de :func:`fair_prob_up` à la seconde t depuis log p(u) (index : secondes).

    ``k_log`` : log K déjà connu (sinon recalculé depuis ``logp``). Lève :class:`MissingPrices`
    si une fenêtre nécessaire n'est pas couverte.
    """
    t = int(t)
    if t not in logp.index:
        raise MissingPrices(f"prix à t={t} inconnu")
    lp = float(logp.loc[t])
    ph = phase_of(t, S, E, L)
    if ph == 1:
        return FormulaInputs(t, 1, lp)
    if ph == 2:
        return FormulaInputs(t, 2, lp, start_avg=float(_window(logp, S - L, t).mean()))
    k = float(k_log) if k_log is not None else float(_window(logp, S - L, S).mean())
    if ph == 3:
        return FormulaInputs(t, 3, lp, price_to_beat=k)
    end_sum = float(_window(logp, E - L, min(t, E)).sum()) / L
    return FormulaInputs(t, 4, lp, price_to_beat=k, end_partial_sum=end_sum)


def sigma_from(method: str, *, bars1m: pd.DataFrame | None = None, logp: pd.Series | None = None,
               n_bars: int = PARKINSON_BARS, halflife_s: float = EWMA_HALFLIFE_S) -> float:
    """σ par √seconde : ``parkinson`` (hauts/bas des ``n_bars`` dernières bougies 1 min closes) ou
    ``ewma`` (rendements 1 s, demi-vie ``halflife_s``)."""
    if method == "parkinson":
        if bars1m is None or not len(bars1m):
            return math.nan
        b = bars1m.iloc[-n_bars:]
        return parkinson_sigma_per_s(b["high"].to_numpy(), b["low"].to_numpy(), bar_seconds=60.0)
    if method == "ewma":
        if logp is None or len(logp) < 30:
            return math.nan
        return ewma_sigma_per_s(logp.to_numpy(), dt_seconds=1.0, halflife_s=halflife_s)
    raise ValueError(f"méthode de σ inconnue : {method!r}")


# ---------------------------------------------------------------------------
# Carnet et décision
# ---------------------------------------------------------------------------
def _fin(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def book_quotes(book_up: dict | None, book_down: dict | None) -> dict:
    """Meilleurs prix des deux jetons ; un côté vide est complété par le miroir de l'autre
    (bid Down à p = ask Up à 1 − p : les carnets du CLOB sont exactement symétriques)."""
    bu, bd = book_up or {}, book_down or {}
    bid_up, ask_up = _fin(bu.get("best_bid")), _fin(bu.get("best_ask"))
    bid_dn, ask_dn = _fin(bd.get("best_bid")), _fin(bd.get("best_ask"))
    size_up, size_dn = _fin(bu.get("ask_size")), _fin(bd.get("ask_size"))
    if math.isnan(ask_up) and not math.isnan(bid_dn):
        ask_up, size_up = round(1.0 - bid_dn, 6), _fin(bd.get("bid_size"))
    if math.isnan(ask_dn) and not math.isnan(bid_up):
        ask_dn, size_dn = round(1.0 - bid_up, 6), _fin(bu.get("bid_size"))
    if math.isnan(bid_up) and not math.isnan(ask_dn):
        bid_up = round(1.0 - ask_dn, 6)
    if math.isnan(bid_dn) and not math.isnan(ask_up):
        bid_dn = round(1.0 - ask_up, 6)
    ts = bu.get("timestamp")
    return {"bid_up": bid_up, "ask_up": ask_up, "ask_up_size": size_up, "bid_down": bid_dn,
            "ask_down": ask_dn, "ask_down_size": size_dn,
            "book_ts": ts.timestamp() if isinstance(ts, pd.Timestamp) else math.nan}


def decide(prob_up: float, ask_up: float, ask_down: float, *, fee_rate: float = FEE_RATE,
           min_edge: float = 0.0) -> tuple[float, float, str | None]:
    """(EV Up, EV Down, côté) par part, frais preneur inclus (:func:`taker_edge`).

    Un côté sans ask (ou ask hors ]0, 1[) est exclu ; on achète le côté de plus grande espérance
    si elle dépasse ``min_edge`` (0 = règle de :func:`taker_edge`)."""
    if not np.isfinite(prob_up):
        return math.nan, math.nan, None
    ok_up = np.isfinite(ask_up) and 0.0 < ask_up < 1.0
    ok_dn = np.isfinite(ask_down) and 0.0 < ask_down < 1.0
    e = taker_edge(prob_up, ask_up if ok_up else 0.5, ask_down if ok_dn else 0.5, fee_rate)
    ev_up = float(e["ev_up"]) if ok_up else math.nan
    ev_dn = float(e["ev_down"]) if ok_dn else math.nan
    cands = [(v, s) for v, s in ((ev_up, "up"), (ev_dn, "down")) if np.isfinite(v) and v > min_edge]
    side = max(cands, key=lambda x: (x[0], x[1] == "up"))[1] if cands else None
    return ev_up, ev_dn, side


def market_fee_rate(market: UpDownMarket) -> float:
    """Taux preneur du marché (``feeSchedule.rate``) ; 0 si frais désactivés."""
    fs = market.fee_schedule
    if not fs:
        return 0.0
    rate = _fin(fs.get("rate"))
    expo = _fin(fs.get("exponent", 1.0))
    if np.isfinite(expo) and abs(expo - 1.0) > 1e-9:
        log.warning("%s : exposant de frais %s ≠ 1, frais approchés par rate·a·(1 − a)", market.slug, expo)
    return rate if np.isfinite(rate) else FEE_RATE


# ---------------------------------------------------------------------------
# Une ligne de signal
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    ts_utc: str
    wall_s: float
    t_info_s: int
    binance_age_s: float
    asset: str
    duration: str
    slug: str
    role: str                 # "en cours" | "suivant"
    start_s: int
    end_s: int
    phase: int
    s_to_start: float
    s_to_end: float
    spot: float
    k_binance: float          # TWAP60(S) Binance (prix) ; NaN avant S
    start_avg_price: float    # exp(Ā) en phase 2
    end_partial_sum: float    # B (log) en phase 4
    sigma_method: str
    sigma_per_s: float
    mean: float
    sd: float                 # s(t) de la formule (marche aléatoire seule)
    basis_sd: float           # σ_b (erreur du proxy Binance -> Chainlink)
    p_formula: float          # Φ(m/s) : formule pure
    p_up: float               # Φ(m/√(s² + σ_b²)) : probabilité utilisée pour décider
    bid_up: float
    ask_up: float
    ask_up_size: float
    bid_down: float
    ask_down: float
    ask_down_size: float
    book_ts_s: float          # horodatage serveur du carnet Up (s Unix) ; lu après ``wall_s``
    fee_rate: float
    ev_up: float
    ev_down: float
    min_edge: float
    decision: str
    note: str = ""
    # complétés par le second passage (fill_outcomes)
    resolved_up: float = math.nan
    price_to_beat_cl: float = math.nan
    final_price_cl: float = math.nan
    pnl: float = math.nan


LOG_COLUMNS: list[str] = [f.name for f in fields(Signal)]


def prob_with_basis(mean: float, sd: float, basis_sd: float = BASIS_SD) -> float:
    """P(Up) = Φ(m / √(s² + σ_b²)) ; égalité (écart-type nul) : 1 si m >= 0, sinon 0."""
    tot = math.sqrt(sd * sd + basis_sd * basis_sd)
    if not np.isfinite(tot):
        return math.nan
    if tot <= 0:
        return 1.0 if mean >= 0 else 0.0
    return float(norm.cdf(mean / tot))


def evaluate_market(market: UpDownMarket, *, now: float, logp: pd.Series, sigma: float, sigma_method: str,
                    k_log: float | None, book_up: dict | None, book_down: dict | None,
                    min_edge: float = 0.0, basis_sd: float = BASIS_SD, stale_s: float = STALE_S) -> Signal:
    """Calcule la ligne de signal d'un marché à l'instant ``now`` (horloge murale, s Unix)."""
    S, E = int(market.start.timestamp()), int(market.end.timestamp())
    t = int(logp.index[-1]) if len(logp) else 0
    q = book_quotes(book_up, book_down)
    fee = market_fee_rate(market)
    notes: list[str] = []
    fv = None
    inp = None
    if len(logp):
        try:
            inp = formula_inputs(logp, t, S, E, k_log=k_log)
            if np.isfinite(sigma):
                fv = fair_prob_up(t, S, E, inp.log_price, sigma, **inp.kwargs(), dt=1.0)
            else:
                notes.append("σ indisponible")
        except MissingPrices as exc:
            notes.append(str(exc))
    else:
        notes.append("aucun prix Binance")
    age = now - t if len(logp) else math.nan
    p = prob_with_basis(fv.mean, fv.sd, basis_sd) if fv is not None else math.nan
    ev_up, ev_dn, side = decide(p, q["ask_up"], q["ask_down"], fee_rate=fee, min_edge=min_edge)
    if np.isfinite(age) and age > stale_s:
        notes.append(f"Binance en retard de {age:.0f} s")
        side = None
    if not market.accepting_orders:
        notes.append("marché fermé aux ordres")
        side = None
    k = inp.price_to_beat if inp is not None and inp.price_to_beat is not None else k_log
    return Signal(
        ts_utc=pd.Timestamp(now, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        wall_s=round(now, 3), t_info_s=t, binance_age_s=round(age, 3) if np.isfinite(age) else math.nan,
        asset=market.asset, duration=market.duration, slug=market.slug,
        role="en cours" if now >= S else "suivant", start_s=S, end_s=E,
        phase=inp.phase if inp is not None else phase_of(t, S, E),
        s_to_start=round(S - now, 1), s_to_end=round(E - now, 1),
        spot=math.exp(inp.log_price) if inp is not None else math.nan,
        k_binance=math.exp(k) if k is not None else math.nan,
        start_avg_price=math.exp(inp.start_avg) if inp is not None and inp.start_avg is not None else math.nan,
        end_partial_sum=inp.end_partial_sum if inp is not None and inp.end_partial_sum is not None else math.nan,
        sigma_method=sigma_method, sigma_per_s=sigma,
        mean=fv.mean if fv is not None else math.nan, sd=fv.sd if fv is not None else math.nan,
        basis_sd=basis_sd, p_formula=fv.prob_up if fv is not None else math.nan, p_up=p,
        bid_up=q["bid_up"], ask_up=q["ask_up"], ask_up_size=q["ask_up_size"], bid_down=q["bid_down"],
        ask_down=q["ask_down"], ask_down_size=q["ask_down_size"],
        book_ts_s=round(q["book_ts"], 3) if np.isfinite(q["book_ts"]) else math.nan,
        fee_rate=fee, ev_up=ev_up, ev_down=ev_dn, min_edge=min_edge, decision=DECISIONS[side],
        note=" ; ".join(notes))


def _num(v: float, dec: int = 2) -> str:
    return report.fmt_number(v, dec)


def _cents(v: float) -> str:
    return (report.fmt_number(v * 100.0, 1, signed=True) + " c") if np.isfinite(v) else "—"


def format_line(s: Signal) -> str:
    """heure | marché | phase | P(Up) | bid/ask Up | EV Up / EV Down | décision | compte à rebours."""
    hhmmss = pd.Timestamp(s.wall_s, unit="s", tz="UTC").strftime("%H:%M:%S")
    start = pd.Timestamp(s.start_s, unit="s", tz="UTC").strftime("%H:%M")
    end = pd.Timestamp(s.end_s, unit="s", tz="UTC").strftime("%H:%M")
    market = f"{s.asset.upper()} {s.duration} {start}→{end} {s.role:<8}"
    phase = f"phase {s.phase} {PHASE_LABELS.get(s.phase, '?'):<7}"
    prob = f"P(Up) {report.fmt_number(s.p_up, 1, pct=True):>7}"
    book = f"Up {_num(s.bid_up)}/{_num(s.ask_up)}"
    ev = f"EV Up {_cents(s.ev_up):>8} · Down {_cents(s.ev_down):>8}"
    if s.phase <= 2:
        cd = f"S dans {max(s.s_to_start, 0):.0f} s"
    else:
        cd = f"E dans {max(s.s_to_end, 0):.0f} s"
    line = " | ".join([hhmmss, market, phase, prob, book, ev, f"{s.decision:<12}", cd])
    return line + (f"  ({s.note})" if s.note else "")


# ---------------------------------------------------------------------------
# Journal CSV et second passage
# ---------------------------------------------------------------------------
def _csv_value(v: Any) -> Any:
    if isinstance(v, float):
        return "" if not math.isfinite(v) else repr(round(v, 10))
    return v


def append_log(path: Path | str, signals: Sequence[Signal]) -> None:
    """Ajoute des lignes au journal (en-tête écrit si le fichier est nouveau ou vide)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_COLUMNS)
        for s in signals:
            d = asdict(s)
            w.writerow([_csv_value(d[c]) for c in LOG_COLUMNS])


def read_log(path: Path | str) -> pd.DataFrame:
    df = pd.read_csv(path, keep_default_na=True)
    for c in ("resolved_up", "price_to_beat_cl", "final_price_cl", "pnl"):
        if c not in df.columns:
            df[c] = math.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def gamma_resolver(client) -> Resolver:
    """Issue officielle d'un slug via gamma : ``{resolved_up, price_to_beat, final_price}`` ou None."""
    def resolve(slug: str) -> dict | None:
        events = client._get(f"{GAMMA_URL}/events", [("slug", slug), ("limit", 1)]) or []
        ev = events[0] if events else {}
        if not ev or not ev.get("markets"):
            return None
        m = parse_market(ev["markets"][0], ev)
        meta = ev.get("eventMetadata") or {}
        return {"resolved_up": m.resolved_up, "price_to_beat": _fin(meta.get("priceToBeat")),
                "final_price": _fin(meta.get("finalPrice"))}
    return resolve


def realized_pnl(df: pd.DataFrame) -> pd.Series:
    """Gain réalisé par part des lignes « acheter » : 1{gagnant} − (ask + frais)."""
    y = df["resolved_up"]
    cost_up = taker_cost(df["ask_up"], df["fee_rate"])
    cost_dn = taker_cost(df["ask_down"], df["fee_rate"])
    pnl = pd.Series(np.nan, index=df.index)
    up = df["decision"].eq(DECISIONS["up"]) & y.notna()
    dn = df["decision"].eq(DECISIONS["down"]) & y.notna()
    pnl[up] = y[up] - cost_up[up]
    pnl[dn] = (1.0 - y[dn]) - cost_dn[dn]
    return pnl


def fill_outcomes(path: Path | str, resolver: Resolver, now: float | None = None,
                  min_age_s: float = FILL_MIN_AGE_S) -> int:
    """Second passage : complète l'issue des marchés terminés du journal (réécriture atomique).
    Renvoie le nombre de marchés nouvellement résolus."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return 0
    now = time.time() if now is None else now
    df = read_log(path)
    pending = df.loc[df["resolved_up"].isna() & (df["end_s"] + min_age_s <= now), "slug"].unique()
    n = 0
    for slug in pending:
        try:
            res = resolver(str(slug))
        except Exception as exc:  # noqa: BLE001 — réseau : on réessaiera au prochain passage
            log.warning("%s : issue indisponible (%r)", slug, exc)
            continue
        if not res or res.get("resolved_up") is None:
            continue
        m = df["slug"] == slug
        df.loc[m, "resolved_up"] = float(bool(res["resolved_up"]))
        df.loc[m, "price_to_beat_cl"] = res.get("price_to_beat", math.nan)
        df.loc[m, "final_price_cl"] = res.get("final_price", math.nan)
        n += 1
    if n:
        df["pnl"] = realized_pnl(df)
        tmp = path.with_suffix(path.suffix + ".tmp")
        df[[c for c in LOG_COLUMNS if c in df.columns]].to_csv(tmp, index=False)
        tmp.replace(path)
    return n


def _grouped_boot_ci(values: np.ndarray, groups: np.ndarray, n_boot: int = N_BOOT, seed: int = 0,
                     level: float = 0.95) -> tuple[float, float]:
    """IC percentile de la moyenne, bootstrap par groupes (créneaux de 15 min) tirés avec remise."""
    ug = np.unique(groups)
    if len(ug) < 2:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    idx = [np.flatnonzero(groups == g) for g in ug]
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(ug), len(ug))
        sel = np.concatenate([idx[i] for i in pick])
        means[b] = values[sel].mean()
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def summarize_log(df: pd.DataFrame, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Bilan des marchés résolus du journal.

    * Brier de P_formule et du milieu du carnet contre l'issue (moyenne par marché, puis entre
      marchés : les lignes d'un même marché sont très corrélées) ;
    * **première** décision de chaque marché (celle qu'on prendrait une fois) : gain réalisé par
      part et espérance annoncée, IC 95 % par bootstrap groupé par créneau de 15 min.
    Une seule règle, fixée avant de regarder ces données : pas de correction de tests multiples.
    """
    res = df[df["resolved_up"].notna()].copy()
    out: dict[str, Any] = {"n_rows": int(len(df)), "n_markets": int(df["slug"].nunique()),
                           "n_resolved_markets": int(res["slug"].nunique())}
    if res.empty:
        return out
    res["mid_up"] = (res["bid_up"] + res["ask_up"]) / 2
    ok = res["p_up"].notna() & res["mid_up"].notna()
    r = res[ok]
    if len(r):
        pure = r["p_formula"] if "p_formula" in r.columns else r["p_up"]
        by_m = r.assign(b_f=(r["p_up"] - r["resolved_up"]) ** 2, b_p=(pure - r["resolved_up"]) ** 2,
                        b_m=(r["mid_up"] - r["resolved_up"]) ** 2)
        g = by_m.groupby("slug")[["b_f", "b_p", "b_m"]].mean()
        out.update(brier_formula=float(g["b_f"].mean()), brier_pure=float(g["b_p"].mean()),
                   brier_book_mid=float(g["b_m"].mean()), n_brier_rows=int(len(r)), n_brier_markets=int(len(g)))
    dec = res[res["decision"].isin([DECISIONS["up"], DECISIONS["down"]])].sort_values("wall_s")
    out["n_decision_rows"] = int(len(dec))
    if len(dec):
        first = dec.groupby("slug", sort=False).head(1).copy()
        first["ev"] = np.where(first["decision"] == DECISIONS["up"], first["ev_up"], first["ev_down"])
        first["pnl"] = realized_pnl(first)
        v = first["pnl"].to_numpy(float)
        lo, hi = _grouped_boot_ci(v, (first["start_s"].to_numpy() // SLOT_S), n_boot=n_boot, seed=seed)
        out.update(n_decision_markets=int(len(first)), mean_pnl_first=float(v.mean()),
                   ci_pnl_first=(lo, hi), mean_ev_first=float(first["ev"].mean()),
                   win_rate_first=float((v > 0).mean()), mean_pnl_rows=float(realized_pnl(dec).mean()))
    return out


def format_summary(s: dict) -> str:
    lines = [f"Bilan du journal : {s['n_rows']} lignes, {s['n_markets']} marchés, "
             f"{s['n_resolved_markets']} résolus."]
    if "brier_formula" in s:
        lines.append(f"Brier (moyenne par marché, {s['n_brier_markets']} marchés, {s['n_brier_rows']} lignes) : "
                     f"P utilisée {_num(s['brier_formula'], 4)}, formule pure {_num(s['brier_pure'], 4)}, "
                     f"milieu du carnet {_num(s['brier_book_mid'], 4)} (plus bas = mieux).")
    if s.get("n_decision_markets"):
        lo, hi = s["ci_pnl_first"]
        ci = f"IC 95 % {_cents(lo)} ; {_cents(hi)}" if np.isfinite(lo) else "IC : trop peu de créneaux"
        lines.append(f"Première décision par marché ({s['n_decision_markets']} marchés) : gain réalisé moyen "
                     f"{_cents(s['mean_pnl_first'])} par part ({ci}, bootstrap groupé par créneau de 15 min), "
                     f"espérance annoncée {_cents(s['mean_ev_first'])}, gagnantes "
                     f"{report.fmt_number(s['win_rate_first'], 0, pct=True)}.")
        lines.append(f"Toutes les lignes « acheter » ({s['n_decision_rows']}, très corrélées) : "
                     f"{_cents(s['mean_pnl_rows'])} par part.")
    elif s["n_resolved_markets"]:
        lines.append("Aucune décision d'achat sur les marchés résolus.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Boucle
# ---------------------------------------------------------------------------
class SignalRunner:
    """Un passage (:meth:`tick`) = marchés, prix Binance, σ, carnets, formule, décisions.

    ``client`` : :class:`PolymarketClient` (ou un faux : ``current_and_next_markets`` et
    ``order_book``) ; ``klines`` : même signature que :func:`binance_klines` ; ``clock`` :
    horloge murale (s Unix). Tout est injectable pour les tests hors ligne.
    """

    def __init__(self, asset: str, duration: str, *, client, klines: KlinesFn = binance_klines,
                 sigma_method: str = "parkinson", min_edge: float = 0.0, basis_sd: float = BASIS_SD,
                 log_path: Path | str | None = None,
                 resolver: Resolver | None = None, clock: Callable[[], float] = time.time, out=None,
                 max_workers: int = 6):
        asset = asset.lower()
        if asset not in ASSET_TO_SYMBOL:
            raise ValueError(f"actif sans paire Binance : {asset!r} (choix : {', '.join(ASSET_TO_SYMBOL)})")
        if duration not in DURATION_SECONDS:
            raise ValueError(f"durée inconnue : {duration!r}")
        if sigma_method not in ("parkinson", "ewma"):
            raise ValueError(f"méthode de σ inconnue : {sigma_method!r}")
        self.asset, self.duration = asset, duration
        self.symbol = ASSET_TO_SYMBOL[asset]
        self.D = DURATION_SECONDS[duration]
        self.client, self.klines, self.clock = client, klines, clock
        self.sigma_method, self.min_edge = sigma_method, float(min_edge)
        if not basis_sd >= 0:
            raise ValueError("basis_sd doit être >= 0")
        self.basis_sd = float(basis_sd)
        self.log_path = Path(log_path) if log_path else None
        self.resolver = resolver
        self.out = out if out is not None else sys.stdout
        self.prices = PriceBuffer()
        self.bars1m: pd.DataFrame | None = None
        self._minute_1m: int | None = None
        self._markets: list[UpDownMarket] = []
        self._markets_slot: int | None = None
        self._markets_tried = -math.inf
        self._k_log: dict[str, float] = {}
        self._seen: set[str] = set()
        self._last_fill = -math.inf
        self._last_context_minute: int | None = None
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    def _print(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    # -- données ---------------------------------------------------------------
    def _refresh_markets(self, now: float) -> None:
        slot = int(now // self.D) * self.D
        complete = len(self._markets) >= 2
        if slot == self._markets_slot and (complete or now - self._markets_tried < MARKETS_RETRY_S):
            return
        self._markets_tried = now
        ms = self.client.current_and_next_markets(self.asset, self.duration, n_next=1,
                                                  now=pd.Timestamp(now, unit="s", tz="UTC"))
        self._markets = [m for m in ms if m.end.timestamp() > now][:2]
        self._markets_slot = slot
        for m in self._markets:
            if m.slug not in self._seen:
                self._seen.add(m.slug)
                self._print(f"   marché {m.slug} : S = {m.start:%H:%M:%S}, E = {m.end:%H:%M:%S} UTC"
                            + ("" if m.accepting_orders else " (fermé aux ordres)"))

    def _fetch_1s(self, now: float) -> pd.DataFrame:
        last = self.prices.last
        if last is None or now - last > INITIAL_1S_BARS - 5:
            self.prices.clear()
            raw = self.klines(self.symbol, "1s", limit=INITIAL_1S_BARS)
        else:
            # bougies ouvertes à partir de `last` (celle ouverte à last − 1 a donné p(last))
            raw = self.klines(self.symbol, "1s", limit=max(INCREMENT_1S_LIMIT, int(now - last) + 5),
                              start_ms=last * 1000)
        return parse_klines(raw, now)

    def _fetch_1m(self, now: float) -> pd.DataFrame | None:
        minute = int(now // 60)
        if self.sigma_method != "parkinson" or minute == self._minute_1m:
            return None
        raw = self.klines(self.symbol, "1m", limit=PARKINSON_BARS + 1)
        return parse_klines(raw, now)

    def _k_for(self, m: UpDownMarket, logp: pd.Series) -> float | None:
        """log K (moyenne de log p sur (S − 60, S]) une fois S passé ; mis en cache par marché."""
        S = int(m.start.timestamp())
        if m.slug in self._k_log:
            return self._k_log[m.slug]
        if not len(logp) or logp.index[-1] <= S:
            return None
        try:
            k = float(_window(logp, S - L_S, S).mean())
        except MissingPrices:
            raw = self.klines(self.symbol, "1s", limit=L_S, start_ms=(S - L_S) * 1000, end_ms=(S - 1) * 1000)
            bars = parse_klines(raw)
            closes = bars["close"].to_numpy()
            if len(closes) < L_S - 5:
                return None
            k = float(np.mean(np.log(closes)))
        self._k_log[m.slug] = k
        self._print(f"   K {m.slug} (moyenne Binance sur (S−60, S]) = {_num(math.exp(k))} $")
        return k

    # -- passage -----------------------------------------------------------------
    def tick(self) -> list[Signal]:
        now = self.clock()
        self._refresh_markets(now)
        f1s = self._pool.submit(self._fetch_1s, now)
        f1m = self._pool.submit(self._fetch_1m, now)
        fbooks = [(self._pool.submit(self.client.order_book, m.token_up),
                   self._pool.submit(self.client.order_book, m.token_down)) for m in self._markets]
        self.prices.add_bars(f1s.result())
        try:
            bars1m = f1m.result()
        except Exception as exc:  # noqa: BLE001 — on garde les bougies 1 min précédentes
            log.warning("bougies 1 min indisponibles (%r)", exc)
            bars1m = None
        if bars1m is not None and len(bars1m):
            self.bars1m, self._minute_1m = bars1m, int(now // 60)
        logp = self.prices.log_series()
        sigma = sigma_from(self.sigma_method, bars1m=self.bars1m, logp=logp)
        signals: list[Signal] = []
        for m, (fu, fd) in zip(self._markets, fbooks):
            try:
                bu, bd = fu.result(), fd.result()
            except Exception as exc:  # noqa: BLE001 — carnet indisponible : on continue sans
                log.warning("%s : carnet indisponible (%r)", m.slug, exc)
                bu = bd = None
            k = self._k_for(m, logp)
            signals.append(evaluate_market(m, now=now, logp=logp, sigma=sigma, sigma_method=self.sigma_method,
                                           k_log=k, book_up=bu, book_down=bd, min_edge=self.min_edge,
                                           basis_sd=self.basis_sd))
        self._maybe_context(now, logp, sigma)
        for s in signals:
            self._print(format_line(s))
        if self.log_path is not None and signals:
            append_log(self.log_path, signals)
        return signals

    def _maybe_context(self, now: float, logp: pd.Series, sigma: float) -> None:
        minute = int(now // 60)
        if minute == self._last_context_minute or not len(logp):
            return
        self._last_context_minute = minute
        t = int(logp.index[-1])
        spot = math.exp(float(logp.iloc[-1]))
        sig_bp = sigma * 1e4 if np.isfinite(sigma) else math.nan
        sd_d = sigma * math.sqrt(self.D) * 1e4 if np.isfinite(sigma) else math.nan
        self._print(f"-- {self.symbol} {_num(spot)} $ (Binance, il y a {report.fmt_number(now - t, 1)} s) · "
                    f"σ {self.sigma_method} = {report.fmt_number(sig_bp, 3)} pb/√s "
                    f"(≈ {report.fmt_number(sd_d, 1)} pb sur {self.duration})")

    def maybe_fill(self, force: bool = False) -> int:
        if self.log_path is None or self.resolver is None:
            return 0
        now = self.clock()
        if not force and now - self._last_fill < FILL_EVERY_S:
            return 0
        self._last_fill = now
        n = fill_outcomes(self.log_path, self.resolver, now=now)
        if n:
            self._print(f"   issue officielle ajoutée pour {n} marché(s) dans {self.log_path}")
        return n

    def run(self, interval: float = 1.0, *, once: bool = False, max_seconds: float | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int:
        """Boucle jusqu'à Ctrl-C, ``once`` ou ``max_seconds`` ; renvoie le nombre de passages."""
        t0 = self.clock()
        n = 0
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 — réseau : on affiche et on continue
                    if once:
                        raise
                    self._print(f"{pd.Timestamp(self.clock(), unit='s', tz='UTC'):%H:%M:%S} | erreur : {exc!r}")
                n += 1
                if once:
                    break
                try:
                    self.maybe_fill()
                except Exception as exc:  # noqa: BLE001
                    log.warning("second passage impossible : %r", exc)
                now = self.clock()
                if max_seconds is not None and now - t0 + interval > max_seconds:
                    break
                # prochain passage aligné sur l'horloge, décalé de TICK_OFFSET_S (bougie 1 s publiée)
                nxt = (math.floor((now - TICK_OFFSET_S) / interval) + 1) * interval + TICK_OFFSET_S
                sleep(max(0.0, nxt - now))
        except KeyboardInterrupt:
            self._print("Arrêt demandé.")
        finally:
            self.close()
        return n


def banner(asset: str, duration: str, sigma_method: str, interval: float, min_edge: float,
           log_path: Path | str | None, basis_sd: float = BASIS_SD) -> str:
    sig = ("Parkinson sur les hauts/bas des 60 dernières bougies 1 min" if sigma_method == "parkinson"
           else f"EWMA des rendements 1 s (demi-vie {EWMA_HALFLIFE_S:.0f} s)")
    return "\n".join([
        f"Signal Polymarket « Up or Down » {asset.upper()} {duration} — formule exacte P(Up) = Φ(m/s) "
        "(marche aléatoire, règle TWAP-60).",
        PAPER_NOTICE,
        f"Prix : Binance {ASSET_TO_SYMBOL[asset]} 1 s ; K = moyenne sur (S−60, S] ; σ : {sig}. "
        "EV = espérance par part achetée au meilleur ask, frais preneur 0,07·a·(1−a) inclus, en cents.",
        f"P(Up) affichée = Φ(m / √(s² + σ_b²)) : formule exacte + erreur du proxy Binance -> Chainlink "
        f"σ_b = {report.fmt_number(basis_sd * 1e4, 2)} pb (calibrée sur BTC, 04/09–13/09"
        + ("" if asset == "btc" else " ; non vérifiée sur cet actif") + ") ; formule pure : --basis-sd 0.",
        f"Décision : acheter le côté d'EV la plus haute si elle dépasse {_cents(min_edge)} ; sinon rien. "
        f"Passage toutes les {report.fmt_number(interval, 1)} s ; heures UTC"
        + (f" ; journal : {log_path}" if log_path else "") + ".",
        "Mises en garde : σ supposé constant ; Chainlink semble en retard d'≈ 4 s sur Binance (non corrigé) ; "
        "les cotations réagissent en ≈ 0,35 s : un avantage affiché peut avoir disparu avant exécution.",
        "Phases : 1 attente (t ≤ S−60, P = 0,5) · 2 départ (moyenne de départ en cours) · 3 K connu · "
        "4 final (moyenne finale en cours).",
        "",
    ])


def run_signal(asset: str = "btc", duration: str = "5m", *, interval: float = 1.0, sigma: str = "parkinson",
               once: bool = False, log_path: Path | str | None = None, min_edge: float = 0.0,
               basis_sd: float = BASIS_SD,
               max_seconds: float | None = None, fill_only: bool = False, client=None,
               klines: KlinesFn = binance_klines, resolver: Resolver | None = None,
               clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
               out=None) -> int:
    """Point d'entrée de ``polymarket-signal`` ; renvoie le nombre de passages effectués."""
    out = out if out is not None else sys.stdout
    if client is None:
        from tradebot.polymarket import PolymarketClient

        client = PolymarketClient(max_rps=20.0, timeout=HTTP_TIMEOUT_S, retries=1, backoff=0.2)
    if resolver is None and log_path is not None:
        resolver = gamma_resolver(client)
    if fill_only:
        if log_path is None:
            raise ValueError("--fill-outcomes exige --log")
        print(PAPER_NOTICE, file=out)
        n = fill_outcomes(log_path, resolver, now=clock())
        print(f"Issue officielle ajoutée pour {n} marché(s) : {log_path}", file=out)
        print(format_summary(summarize_log(read_log(log_path))), file=out)
        return 0
    print(banner(asset, duration, sigma, interval, min_edge, log_path, basis_sd), file=out, flush=True)
    runner = SignalRunner(asset, duration, client=client, klines=klines, sigma_method=sigma, min_edge=min_edge,
                          basis_sd=basis_sd, log_path=log_path, resolver=resolver, clock=clock, out=out)
    n = runner.run(interval, once=once, max_seconds=max_seconds, sleep=sleep)
    if log_path is not None and Path(log_path).exists():
        try:
            runner.maybe_fill(force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("second passage impossible : %r", exc)
        print(format_summary(summarize_log(read_log(log_path))), file=out)
        print("Pour compléter les issues plus tard (résolution 2 à 8 min après E) : "
              f"python -m tradebot polymarket-signal --log {log_path} --fill-outcomes", file=out)
    return n
