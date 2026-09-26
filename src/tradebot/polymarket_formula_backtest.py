"""Backtest de la formule exacte P(Up) (``tradebot.polymarket_formula``) sur l'historique et le
carnet réel des marchés Polymarket « Up or Down ».

Trois questions : la formule est-elle juste (calibration contre l'issue officielle) ? bat-elle le
prix du marché au même instant ? peut-on gagner avec, et à quelle vitesse faut-il agir ?

Conventions (toutes les séries sont en secondes Unix) :

* **Prix Binance 1 s** : la bougie 1 s ouverte à ``u − 1`` clôt à ``u`` ; son close est « le prix
  à l'instant ``u`` », connu à ``u``. :class:`SecondPrices` indexe donc les log-closes par l'instant
  où ils sont connus : à ``t``, seuls les prix aux instants ``u <= t`` sont lisibles.
* **Moyennes** : ``K`` = moyenne des log-prix aux instants ``u ∈ (S − 60, S]`` (60 points, comme
  les points Chainlink de la règle officielle), ``start_avg`` = moyenne sur ``(S − 60, t]``,
  ``end_partial_sum`` = somme sur ``(E − 60, t]`` divisée par 60. ``K`` vient de Binance (même
  source que ``p_t``) : l'écart de niveau Chainlink − Binance (≈ −3 pb) s'annule dans ``p_t − K``.
* **Décalage Chainlink** (``lag_s``, variante) : si Chainlink(u) ≈ Binance(u − c), connaître
  Binance jusqu'à ``t`` revient à connaître Chainlink jusqu'à ``t + c`` ; la formule est alors
  évaluée à ``τ = t + c`` avec des moyennes Binance décalées de ``c`` (toujours rien après ``t``).
* **Trades preneurs** (data-api Polymarket) : horodatés **au bloc Polygon**, ≈ 2 s après
  l'appariement. Prix ramené au repère Up (trade sur Down à ``p`` -> ``1 − p``).
* **Frais preneur** : ``0,07 · p · (1 − p)`` par part (crypto_fees_v2).

Lecture seule de données publiques : aucun ordre, aucune clé.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import lfilter
from scipy.stats import norm

from tradebot.polymarket_formula import FEE_RATE, TWAP_WINDOW_S, twap_diff_moments

__all__ = [
    "DECISIONS", "HIST_DELAYS", "LIVE_DELAYS", "SecondPrices", "decision_times", "formula_inputs",
    "variance_factor", "formula_probs", "parkinson_sigma_series", "bar_sigma_at", "ewma_var_series",
    "prepare_trades", "market_price_at", "first_trade_in", "estimated_asks", "choose_side",
    "taker_fee", "pnl_per_share", "brier", "log_loss", "calibration_deciles", "tail_check",
    "logit", "fit_stack", "predict_stack", "book_states_at", "fill_price", "mid_path",
    "reaction_delays", "formula_path_ms", "step_values", "fetch_binance_1s", "fetch_agg_trades", "bars_1m_from_1s",
]

log = logging.getLogger(__name__)

L_S = int(TWAP_WINDOW_S)
# Instants de décision : (étiquette, ancre "S" ou "E", décalage en s)
DECISIONS: tuple[tuple[str, str, int], ...] = (
    ("S-45", "S", -45), ("S-30", "S", -30), ("S-20", "S", -20), ("S-10", "S", -10), ("S-5", "S", -5),
    ("S-2", "S", -2), ("S", "S", 0), ("S+2", "S", 2), ("S+5", "S", 5), ("S+10", "S", 10), ("S+30", "S", 30),
    ("S+60", "S", 60), ("S+120", "S", 120), ("E-90", "E", -90), ("E-60", "E", -60), ("E-30", "E", -30),
    ("E-10", "E", -10),
)
HIST_DELAYS: tuple[float, ...] = (0, 1, 2, 3, 5, 10)
LIVE_DELAYS: tuple[float, ...] = (0, 0.1, 0.3, 0.5, 1, 2, 5)
HALF_SPREAD = 0.005


def decision_times(S: np.ndarray, E: np.ndarray, decisions: Sequence[tuple[str, str, int]] = DECISIONS) -> np.ndarray:
    """Tableau (n_marchés, n_instants) des instants de décision en s Unix."""
    S = np.asarray(S, dtype="int64")
    E = np.asarray(E, dtype="int64")
    cols = [(S if a == "S" else E) + int(o) for _, a, o in decisions]
    return np.stack(cols, axis=1)


# ---------------------------------------------------------------------------
# Prix Binance à la seconde
# ---------------------------------------------------------------------------
class SecondPrices:
    """Log-prix à la seconde indexés par l'instant où ils sont connus.

    ``sec_open`` : heures d'ouverture des bougies 1 s (contiguës après remplissage), ``close`` :
    closes. Le prix à l'instant ``u`` est le close de la bougie ouverte à ``u − 1``.
    """

    def __init__(self, sec_open: np.ndarray, close: np.ndarray):
        sec_open = np.asarray(sec_open, dtype="int64")
        close = np.asarray(close, dtype="float64")
        if sec_open.size == 0:
            raise ValueError("série vide")
        order = np.argsort(sec_open, kind="stable")
        sec_open, close = sec_open[order], close[order]
        n = int(sec_open[-1] - sec_open[0] + 1)
        full = np.full(n, np.nan)
        full[sec_open - sec_open[0]] = close
        full = pd.Series(full).ffill().to_numpy()          # secondes sans trade : close reporté
        self.t0 = int(sec_open[0]) + 1                     # instant où x[0] est connu
        self.x = np.log(full)
        self.cs = np.concatenate([[0.0], np.nancumsum(self.x)])
        self.t_last = self.t0 + self.x.size - 1

    def _idx(self, u) -> np.ndarray:
        return np.asarray(u, dtype="int64") - self.t0

    def at(self, u) -> np.ndarray:
        """Log-prix à l'instant ``u`` (NaN hors plage)."""
        i = self._idx(u)
        ok = (i >= 0) & (i < self.x.size)
        out = np.full(i.shape, np.nan)
        out[ok] = self.x[i[ok]]
        return out

    def window_sum(self, a, b) -> np.ndarray:
        """Somme des log-prix aux instants ``u ∈ (a, b]`` (NaN hors plage ; 0 si ``b <= a``)."""
        a = np.asarray(a, dtype="int64")
        b = np.asarray(b, dtype="int64")
        a, b = np.broadcast_arrays(a, b)
        i = a - self.t0 + 1
        j = b - self.t0 + 1
        ok = (i >= 0) & (j <= self.x.size) & (i <= j)
        out = np.where(b <= a, 0.0, np.nan)
        m = ok & (b > a)
        out[m] = self.cs[j[m]] - self.cs[i[m]]
        return out

    def window_mean(self, a, b) -> np.ndarray:
        a = np.asarray(a, dtype="int64")
        b = np.asarray(b, dtype="int64")
        with np.errstate(invalid="ignore", divide="ignore"):
            return self.window_sum(a, b) / np.where(b > a, b - a, np.nan)

    def returns(self) -> np.ndarray:
        """Rendements log 1 s alignés sur ``x`` (``r[k] = x[k] − x[k − 1]``, ``r[0]`` = NaN)."""
        return np.diff(self.x, prepend=np.nan)


def formula_inputs(prices: SecondPrices, t, S, E, L: int = L_S, lag_s: int = 0) -> dict[str, np.ndarray]:
    """Entrées de la formule à l'instant ``t`` (vectorisé), à partir des seules données ``<= t``.

    Renvoie ``tau`` (instant d'évaluation = ``t + lag_s``), ``log_price``, ``start_avg`` (phase 2),
    ``price_to_beat`` (log K, phases 3-4), ``end_partial_sum`` (phase 4) ; NaN quand sans objet.
    Avec ``lag_s = c`` : moyennes Chainlink sur ``(a, b]`` ≈ moyennes Binance sur ``(a − c, b − c]``.
    """
    t = np.asarray(t, dtype="int64")
    S = np.broadcast_to(np.asarray(S, dtype="int64"), t.shape)
    E = np.broadcast_to(np.asarray(E, dtype="int64"), t.shape)
    c = int(lag_s)
    tau = t + c
    lp = prices.at(t)
    nan = np.full(t.shape, np.nan)
    ph2 = (tau > S - L) & (tau <= S)
    start_avg = np.where(ph2, prices.window_mean(S - L - c, np.where(ph2, t, S - L - c + 1)), nan)
    after = tau > S
    ptb = np.where(after, prices.window_mean(S - L - c, S - c), nan)
    ph4 = tau > E - L
    eps = np.where(ph4, prices.window_sum(E - L - c, np.where(ph4, t, E - L - c)) / L, nan)
    return {"tau": tau, "log_price": lp, "start_avg": start_avg, "price_to_beat": ptb, "end_partial_sum": eps}


_VF_CACHE: dict[tuple, float] = {}


def variance_factor(rel_S: int, D: int, L: int = L_S, dt: float | None = 1.0) -> float:
    """Var(F − K | t) / σ² pour ``t = S + rel_S`` (mise en cache ; via ``twap_diff_moments``)."""
    key = (int(rel_S), int(D), int(L), dt)
    v = _VF_CACHE.get(key)
    if v is None:
        S, E, t = 0.0, float(D), float(rel_S)
        kw = dict(start_avg=0.0, price_to_beat=0.0, end_partial_sum=0.0, L=float(L), dt=dt)
        _, sd, _ = twap_diff_moments(t, S, E, 0.0, 1.0, **kw)
        v = sd * sd
        _VF_CACHE[key] = v
    return v


def formula_probs(inputs: dict[str, np.ndarray], S, E, sigma, L: int = L_S, dt: float | None = 1.0,
                  extra_sd: float | np.ndarray = 0.0) -> dict[str, np.ndarray]:
    """P(Up) = Φ(m / s) vectorisé (mêmes formules que ``fair_prob_up``).

    ``s² = σ² · facteur(τ) + extra_sd²`` (``extra_sd`` : bruit de source Chainlink/Binance sur
    ``F − K``, 0 = formule telle quelle). Renvoie ``p``, ``m``, ``sd``, ``phase``.
    """
    tau = np.asarray(inputs["tau"], dtype="int64")
    S = np.broadcast_to(np.asarray(S, dtype="int64"), tau.shape)
    E = np.broadcast_to(np.asarray(E, dtype="int64"), tau.shape)
    sig = np.broadcast_to(np.asarray(sigma, dtype="float64"), tau.shape)
    lp = inputs["log_price"]
    rel = tau - S
    D = E - S
    phase = np.select([tau <= S - L, tau <= S, tau <= E - L], [1, 2, 3], 4)
    a = L - (S - tau)
    m = np.select(
        [phase == 1, phase == 2, phase == 3],
        [np.zeros(tau.shape), (a / L) * (lp - inputs["start_avg"]), lp - inputs["price_to_beat"]],
        inputs["end_partial_sum"] + (np.maximum(E - tau, 0) / L) * lp - inputs["price_to_beat"],
    )
    vf = np.empty(tau.shape)
    keys = pd.DataFrame({"r": rel.ravel(), "d": D.ravel()})
    for (r, d), idx in keys.groupby(["r", "d"]).indices.items():
        vf.ravel()[idx] = variance_factor(int(r), int(d), L, dt)
    sd = np.sqrt(sig * sig * vf + np.asarray(extra_sd, dtype="float64") ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = m / sd
        p = norm.cdf(z)
    p = np.where(sd > 0, p, np.where(m >= 0, 1.0, 0.0))
    p = np.where(np.isfinite(m) & np.isfinite(sd), p, np.nan)
    return {"p": p, "m": m, "sd": sd, "phase": phase}


# ---------------------------------------------------------------------------
# Estimateurs de σ (tous causaux)
# ---------------------------------------------------------------------------
def parkinson_sigma_series(bars1m: pd.DataFrame, n: int = 60, min_bars: int = 30) -> pd.Series:
    """σ Parkinson par √s sur les ``n`` bougies 1 m finissant par chaque bougie (index : heure
    d'ouverture en s Unix). ``σ²_barre = ln(H/L)² / (4 ln 2)`` (cf. ``parkinson_sigma_per_s``)."""
    idx = pd.DatetimeIndex(bars1m.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    open_s = ((idx - pd.Timestamp(0, tz="UTC")) // pd.Timedelta("1s")).to_numpy().astype("int64")
    h = bars1m["high"].to_numpy(dtype="float64")
    lo = bars1m["low"].to_numpy(dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        v = np.log(h / lo) ** 2 / (4 * math.log(2))
    v[~np.isfinite(v)] = np.nan
    s = pd.Series(v, index=open_s)
    full = s.reindex(np.arange(open_s.min(), open_s.max() + 60, 60))
    var_bar = full.rolling(n, min_periods=min_bars).mean()
    return np.sqrt(var_bar / 60.0)


def bar_sigma_at(series: pd.Series, t, bar_s: int = 60) -> np.ndarray:
    """Valeur de ``series`` (indexée par l'ouverture des bougies) à la dernière bougie **close**
    à ``t`` : ouverture ``<= t − bar_s``."""
    t = np.asarray(t, dtype="int64")
    o = (t // bar_s) * bar_s - bar_s
    return series.reindex(o.ravel()).to_numpy(dtype="float64").reshape(t.shape)


def ewma_var_series(prices: SecondPrices, halflife_s: float) -> np.ndarray:
    """Variance EWMA des rendements log 1 s, alignée sur ``prices.x`` : la valeur à l'indice ``k``
    n'utilise que les rendements ``r[1..k]`` (connus à l'instant ``t0 + k``), poids normalisés
    ``λ^âge / Σ λ^âge`` comme ``ewma_sigma_per_s`` (donc causal dès le début de la série).
    Rendements non finis ignorés."""
    r2 = prices.returns() ** 2
    ok = np.isfinite(r2)
    lam = 0.5 ** (1.0 / float(halflife_s))
    num = lfilter([1.0], [1.0, -lam], np.where(ok, r2, 0.0))
    den = lfilter([1.0], [1.0, -lam], ok.astype("float64"))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


# ---------------------------------------------------------------------------
# Trades preneurs : prix du marché, exécution
# ---------------------------------------------------------------------------
@dataclass
class MarketTrades:
    """Trades preneurs d'un marché triés par (bloc, seq) : ``ts`` s Unix, ``token`` 0 Up / 1 Down,
    ``buy`` (côté preneur), ``price`` (prix du jeton), ``p_up`` (repère Up), ``size``."""
    ts: np.ndarray
    token: np.ndarray
    buy: np.ndarray
    price: np.ndarray
    p_up: np.ndarray
    size: np.ndarray


def prepare_trades(df: pd.DataFrame) -> dict[str, MarketTrades]:
    """``df`` : colonnes ``ts`` (datetime UTC ou s), ``side``, ``outcome``, ``price``, ``size``, ``slug``,
    ``seq`` -> dictionnaire slug -> :class:`MarketTrades`."""
    if df.empty:
        return {}
    ts = df["ts"]
    if pd.api.types.is_datetime64_any_dtype(ts.dtype):
        sec = (pd.DatetimeIndex(ts).as_unit("ns").asi8 // 1_000_000_000).astype("int64")
    else:
        sec = ts.to_numpy(dtype="int64")
    d = pd.DataFrame({
        "slug": df["slug"].astype(str).to_numpy(), "ts": sec,
        "seq": df["seq"].to_numpy(dtype="int64") if "seq" in df else np.arange(len(df)),
        "token": (df["outcome"].astype(str).str.lower() == "down").to_numpy().astype("int8"),
        "buy": (df["side"].astype(str).str.upper() == "BUY").to_numpy(),
        "price": df["price"].to_numpy(dtype="float64"), "size": df["size"].to_numpy(dtype="float64"),
    })
    d["p_up"] = np.where(d["token"] == 1, 1.0 - d["price"], d["price"])
    d = d.sort_values(["slug", "ts", "seq"], kind="stable")
    out = {}
    for slug, idx in d.groupby("slug", sort=False).indices.items():
        g = d.iloc[idx]
        out[slug] = MarketTrades(g["ts"].to_numpy(), g["token"].to_numpy(), g["buy"].to_numpy(),
                                 g["price"].to_numpy(), g["p_up"].to_numpy(), g["size"].to_numpy())
    return out


def market_price_at(tr: MarketTrades, t, window_s: int = 3) -> dict[str, np.ndarray]:
    """Prix du marché (repère Up) à ``t`` : dernier trade de bloc ``<= t`` (``last``, ``age`` s) et
    VWAP des trades de bloc dans ``[t − window_s, t]`` (``vwap``, ``n_win``)."""
    t = np.asarray(t, dtype="int64")
    j = np.searchsorted(tr.ts, t, side="right") - 1
    has = j >= 0
    jj = np.where(has, j, 0)
    last = np.where(has, tr.p_up[jj] if tr.ts.size else np.nan, np.nan)
    age = np.where(has, t - (tr.ts[jj] if tr.ts.size else 0), np.nan)
    i0 = np.searchsorted(tr.ts, t - window_s, side="left")
    csv = np.concatenate([[0.0], np.cumsum(tr.p_up * tr.size)])
    css = np.concatenate([[0.0], np.cumsum(tr.size)])
    num = csv[j + 1] - csv[i0]
    den = css[j + 1] - css[i0]
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(den > 0, num / den, np.nan)
    return {"last": last, "age": age, "vwap": vwap, "n_win": np.maximum(j + 1 - i0, 0)}


def first_trade_in(ts: np.ndarray, price: np.ndarray, t_from, t_to) -> tuple[np.ndarray, np.ndarray]:
    """Premier trade (ordre du tableau, trié par bloc) de bloc dans ``[t_from, t_to]`` : (prix, bloc) ;
    NaN si aucun."""
    t_from = np.asarray(t_from, dtype="float64")
    t_to = np.asarray(t_to, dtype="float64")
    i = np.searchsorted(ts, t_from, side="left")
    ok = (i < ts.size)
    ii = np.where(ok, i, 0)
    ok &= ts[ii] <= t_to if ts.size else False
    px = np.where(ok, price[ii] if ts.size else np.nan, np.nan)
    at = np.where(ok, ts[ii] if ts.size else np.nan, np.nan)
    return px, at


def estimated_asks(p_ref: np.ndarray, half_spread: float = HALF_SPREAD) -> tuple[np.ndarray, np.ndarray]:
    """Asks estimés (Up, Down) à partir d'un prix de référence du jeton Up."""
    p = np.asarray(p_ref, dtype="float64")
    return np.clip(p + half_spread, 0.001, 0.999), np.clip(1.0 - p + half_spread, 0.001, 0.999)


def taker_fee(price, fee_rate: float = FEE_RATE) -> np.ndarray:
    p = np.asarray(price, dtype="float64")
    return fee_rate * p * (1.0 - p)


def choose_side(prob, ask_up, ask_dn, margin: float, fee_rate: float = FEE_RATE) -> tuple[np.ndarray, np.ndarray]:
    """Côté à acheter (+1 Up, −1 Down, 0 rien) et avantage estimé par part : achat si
    ``P − (ask + frais) > margin`` (le plus grand des deux côtés)."""
    p = np.asarray(prob, dtype="float64")
    au = np.asarray(ask_up, dtype="float64")
    ad = np.asarray(ask_dn, dtype="float64")
    ev_up = p - (au + taker_fee(au, fee_rate))
    ev_dn = (1.0 - p) - (ad + taker_fee(ad, fee_rate))
    ev_up = np.where(np.isfinite(ev_up), ev_up, -np.inf)
    ev_dn = np.where(np.isfinite(ev_dn), ev_dn, -np.inf)
    side = np.where((ev_up >= ev_dn) & (ev_up > margin), 1, np.where(ev_dn > margin, -1, 0))
    side = np.where(np.isfinite(p), side, 0)
    edge = np.where(side == 1, ev_up, np.where(side == -1, ev_dn, np.nan))
    return side.astype("int8"), edge


def pnl_per_share(side, price, y, fee_rate: float = FEE_RATE) -> np.ndarray:
    """``1{gagné} − prix − frais`` pour une part achetée ; NaN sans position ou sans exécution."""
    side = np.asarray(side)
    price = np.asarray(price, dtype="float64")
    y = np.asarray(y, dtype="float64")
    win = np.where(side == 1, y, 1.0 - y)
    out = win - price - taker_fee(price, fee_rate)
    return np.where((side != 0) & np.isfinite(price), out, np.nan)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def brier(y, p) -> np.ndarray:
    return (np.asarray(p, dtype="float64") - np.asarray(y, dtype="float64")) ** 2


def log_loss(y, p, eps: float = 1e-3) -> np.ndarray:
    """Log-loss par observation, probabilités bornées à ``[eps, 1 − eps]`` (le pas de prix minimal
    de Polymarket est 0,001 : même borne pour la formule et le marché)."""
    y = np.asarray(y, dtype="float64")
    p = np.clip(np.asarray(p, dtype="float64"), eps, 1 - eps)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def calibration_deciles(y, p, bins: int = 10) -> pd.DataFrame:
    """Courbe de fiabilité par déciles de ``p`` : p moyen, fréquence de Up, n, bornes."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if p.size == 0:
        return pd.DataFrame(columns=["bin", "p_mean", "up_rate", "n", "p_lo", "p_hi"])
    order = np.argsort(p, kind="mergesort")
    k = np.empty(p.size, dtype="int64")
    k[order] = (np.arange(p.size) * bins) // p.size        # déciles de rang (ex-aequo répartis)
    df = pd.DataFrame({"bin": k, "p": p, "y": y}).groupby("bin").agg(
        p_mean=("p", "mean"), up_rate=("y", "mean"), n=("y", "size"), p_lo=("p", "min"), p_hi=("p", "max"))
    return df.reset_index()


def tail_check(y, p, lo: float = 0.02) -> pd.DataFrame:
    """Queues : quand la formule dit < ``lo`` (ou > 1 − ``lo``), fréquence réelle de l'improbable."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    rows = []
    for name, m, exp_ in (("P < %.2f" % lo, p < lo, p), ("P > %.2f" % (1 - lo), p > 1 - lo, 1 - p)):
        m = m & np.isfinite(y)
        surprise = y[m] if name.startswith("P <") else 1 - y[m]
        rows.append({"zone": name, "n": int(m.sum()), "attendu": float(np.mean(exp_[m])) if m.any() else math.nan,
                     "observe": float(np.mean(surprise)) if m.any() else math.nan,
                     "surprises": int(np.sum(surprise)) if m.any() else 0})
    return pd.DataFrame(rows)


def logit(p, eps: float = 1e-3) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype="float64"), eps, 1 - eps)
    return np.log(p / (1 - p))


def fit_stack(y, p_formula, p_market) -> np.ndarray:
    """Logistique ``y ~ a + b·logit(p_formule) + c·logit(p_marché)`` (quasi sans pénalité) ;
    renvoie ``[a, b, c]``."""
    from sklearn.linear_model import LogisticRegression

    X = np.column_stack([logit(p_formula), logit(p_market)])
    y = np.asarray(y, dtype="float64")
    ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
    if ok.sum() < 20 or np.unique(y[ok]).size < 2:
        return np.array([0.0, 1.0, 0.0])
    lr = LogisticRegression(C=1e4, max_iter=1000).fit(X[ok], y[ok].astype(int))
    return np.concatenate([lr.intercept_, lr.coef_[0]])


def predict_stack(coef: np.ndarray, p_formula, p_market) -> np.ndarray:
    z = coef[0] + coef[1] * logit(p_formula) + coef[2] * logit(p_market)
    return 1.0 / (1.0 + np.exp(-z))


# ---------------------------------------------------------------------------
# Carnet réel (fichiers du collecteur)
# ---------------------------------------------------------------------------
def book_states_at(market, times_ms: Sequence[int], depth: int = 15) -> list[dict]:
    """État du carnet (repère Up) aux instants ``times_ms`` (ms, serveur) : dernier état dont
    l'horodatage serveur est ``<= t`` (même règle que ``polymarket_book.book_series``).

    Chaque état : ``bids`` [(prix, taille)] décroissants, ``asks`` croissants (``depth`` niveaux),
    ``fresh`` (au moins un instantané reçu)."""
    from tradebot.polymarket_book import Book, apply_event

    order = np.argsort(np.asarray(times_ms, dtype="int64"), kind="stable")
    out: list[dict | None] = [None] * len(order)
    book = Book()
    ev = market.events
    i, n = 0, len(ev)
    for k in order:
        g = int(times_ms[k])
        while i < n:
            e = ev[i]
            if e[2] in ("book", "pc") and e[1] > g:
                break
            apply_event(book, e)
            i += 1
        bids = sorted(book.bids.items(), reverse=True)[:depth]
        asks = sorted(book.asks.items())[:depth]
        out[k] = {"bids": [(p / 1000.0, s) for p, s in bids], "asks": [(p / 1000.0, s) for p, s in asks],
                  "fresh": book.n_snapshots > 0}
    return out  # type: ignore[return-value]


def fill_price(state: dict, side: int, qty: float) -> tuple[float, float, float, float]:
    """Achat preneur de ``qty`` parts du jeton ``side`` (+1 Up, −1 Down) sur l'état ``state`` :
    (meilleur ask, taille au meilleur ask, prix moyen payé, quantité obtenue). Down : ask Down
    = 1 − bid Up."""
    if not state or not state.get("fresh"):
        return math.nan, 0.0, math.nan, 0.0
    levels = state["asks"] if side == 1 else [(round(1.0 - p, 3), s) for p, s in state["bids"]]
    if not levels:
        return math.nan, 0.0, math.nan, 0.0
    best, best_sz = levels[0]
    left, cost = float(qty), 0.0
    for p, s in levels:
        take = min(left, s)
        cost += take * p
        left -= take
        if left <= 1e-12:
            break
    got = float(qty) - max(left, 0.0)
    return float(best), float(best_sz), (cost / got if got > 0 else math.nan), got


def mid_path(market, t_from_ms: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Milieu du carnet (repère Up) après chaque événement de carnet : (ts serveur ms cumulés au
    maximum pour rester croissants, milieu)."""
    from tradebot.polymarket_book import Book, apply_event

    book = Book()
    ts, mids = [], []
    last = -1
    for e in market.events:
        if not apply_event(book, e):
            continue
        tt = max(int(e[1]), last)
        last = tt
        if t_from_ms is not None and tt < t_from_ms - 60_000:
            continue
        bb, ba = book.best_bid(), book.best_ask()
        ts.append(tt)
        mids.append((bb + ba) / 2.0 if (math.isfinite(bb) and math.isfinite(ba)) else math.nan)
    return np.asarray(ts, dtype="int64"), np.asarray(mids, dtype="float64")


def formula_path_ms(prices: SecondPrices, grid_ms: np.ndarray, trade_ms: np.ndarray, trade_px: np.ndarray, S: int, E: int,
                    sigma: float, lag_s: int = 0, extra_sd: float = 0.0, L: int = L_S) -> np.ndarray:
    """P(Up) sur une grille en ms : moyennes, phase et variance à la seconde ``floor(g / 1000)``
    (prix 1 s connus), log-prix = dernier trade Binance d'horodatage ``<= g``."""
    g = np.asarray(grid_ms, dtype="int64")
    t_sec = g // 1000
    inp = formula_inputs(prices, t_sec, S, E, L, lag_s)
    T = np.asarray(trade_ms, dtype="int64")
    j = np.searchsorted(T, g, side="right") - 1
    px = np.asarray(trade_px, dtype="float64")
    inp["log_price"] = np.where(j >= 0, np.log(px[np.maximum(j, 0)]) if px.size else np.nan, np.nan)
    return formula_probs(inp, S, E, sigma, L, extra_sd=extra_sd)["p"]


def step_values(event_ms: np.ndarray, values: np.ndarray, grid_ms: np.ndarray) -> np.ndarray:
    """Valeur en escalier (dernier événement ``<= g``) sur une grille ; NaN avant le premier."""
    e = np.asarray(event_ms, dtype="int64")
    j = np.searchsorted(e, np.asarray(grid_ms, dtype="int64"), side="right") - 1
    v = np.asarray(values, dtype="float64")
    return np.where(j >= 0, v[np.maximum(j, 0)] if v.size else np.nan, np.nan)


def reaction_delays(grid_ms: np.ndarray, p_formula: np.ndarray, mid: np.ndarray, jump: float = 0.05,
                    window_ms: int = 1000, quiet_ms: int = 3000, horizon_ms: int = 20_000) -> pd.DataFrame:
    """Délai de réaction du carnet aux mouvements de la formule.

    Événement : premier point ``g`` de la grille où ``|P(g) − P(g − window)| > jump`` (hors des
    ``quiet_ms`` suivant l'événement précédent). ``dP = P(g) − P(g − window)``, ``mid0 =
    mid(g − window)``. ``t_formula`` : premier instant de ``(g − window, g]`` où la formule a fait
    la moitié de ``dP`` ; ``t_book`` : premier instant ``>= g − window`` où
    ``signe(dP)·(mid − mid0) >= |dP|/2``. ``delay_ms = t_book − t_formula`` (négatif : le carnet
    a bougé avant). Sans réaction dans ``horizon_ms`` : ``censored``.
    """
    g = np.asarray(grid_ms, dtype="int64")
    P = np.asarray(p_formula, dtype="float64")
    M = np.asarray(mid, dtype="float64")
    if g.size < 3:
        return pd.DataFrame(columns=["t_event", "dP", "p0", "mid0", "t_formula", "t_book", "delay_ms", "censored"])
    step = int(np.median(np.diff(g)))
    w = max(1, int(round(window_ms / step)))
    rows = []
    last_ev = -10 ** 18
    k = w
    while k < g.size:
        if g[k] - last_ev < quiet_ms or not (np.isfinite(P[k]) and np.isfinite(P[k - w])):
            k += 1
            continue
        dP = P[k] - P[k - w]
        if abs(dP) <= jump:
            k += 1
            continue
        sgn = 1.0 if dP > 0 else -1.0
        half = abs(dP) / 2.0
        seg = P[k - w: k + 1]
        jf = int(np.argmax(sgn * (seg - P[k - w]) >= half))
        t_formula = int(g[k - w + jf])
        mid0 = M[k - w]
        t_book, censored = math.nan, True
        if np.isfinite(mid0):
            end = np.searchsorted(g, g[k] + horizon_ms, side="right")
            segm = sgn * (M[k - w: end] - mid0)
            hit = np.flatnonzero(np.nan_to_num(segm, nan=-np.inf) >= half)
            if hit.size:
                t_book, censored = float(g[k - w + hit[0]]), False
        rows.append({"t_event": int(g[k]), "dP": float(dP), "p0": float(P[k - w]), "mid0": float(mid0),
                     "t_formula": t_formula, "t_book": t_book,
                     "delay_ms": (t_book - t_formula) if not censored else math.nan, "censored": censored})
        last_ev = g[k]
        k += 1
    return pd.DataFrame(rows, columns=["t_event", "dP", "p0", "mid0", "t_formula", "t_book", "delay_ms", "censored"])


# ---------------------------------------------------------------------------
# Binance récent (data-api), cache sous data/cache/pm_formula/
# ---------------------------------------------------------------------------
def _http_json(url: str, params: dict):
    from tradebot.data import _http_get

    r = _http_get(url, params=params, allow_404=False)
    used = r.headers.get("x-mbx-used-weight-1m")
    if used is not None and int(used) > 4800:
        time.sleep(61.0 - time.time() % 60.0)
    return r.json()


def fetch_binance_1s(symbol: str, start_ts: int, end_ts: int, cache_dir: Path,
                     fetcher: Callable[[str, int, int], pd.DataFrame] | None = None,
                     now: float | None = None) -> pd.DataFrame:
    """Bougies 1 s (``sec`` = ouverture, ``open, high, low, close``) de ``[start_ts, end_ts)``, par
    heures entières ; une heure révolue est mise en cache (``<symbole>_<AAAAMMJJHH>.parquet``)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now

    def api(sym: str, a: int, b: int) -> pd.DataFrame:
        rows, cur = [], a * 1000
        while cur < b * 1000:
            page = _http_json("https://data-api.binance.vision/api/v3/klines",
                              {"symbol": sym, "interval": "1s", "startTime": cur, "endTime": b * 1000 - 1, "limit": 1000})
            if not page:
                break
            rows.extend(page)
            nxt = int(page[-1][0]) + 1000
            if len(page) < 1000 or nxt <= cur:
                break
            cur = nxt
        df = pd.DataFrame([(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows],
                          columns=["sec", "open", "high", "low", "close"])
        return df

    fetcher = fetcher or api
    parts = []
    h0 = (int(start_ts) // 3600) * 3600
    for h in range(h0, int(end_ts), 3600):
        path = cache_dir / f"{symbol}_{pd.Timestamp(h, unit='s', tz='UTC'):%Y%m%d%H}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path))
            continue
        df = fetcher(symbol, h, min(h + 3600, int(now) - 1))
        if h + 3600 <= now - 5 and len(df) >= 3000:
            df.to_parquet(path)
        parts.append(df)
    if not parts:
        return pd.DataFrame(columns=["sec", "open", "high", "low", "close"])
    out = pd.concat(parts, ignore_index=True).drop_duplicates("sec").sort_values("sec")
    return out[(out["sec"] >= start_ts) & (out["sec"] < end_ts)].reset_index(drop=True)


def bars_1m_from_1s(df1s: pd.DataFrame) -> pd.DataFrame:
    """Bougies 1 m (haut/bas exacts) agrégées depuis des bougies 1 s, index UTC (ouverture)."""
    m = (df1s["sec"].to_numpy(dtype="int64") // 60) * 60
    g = df1s.assign(m=m).groupby("m").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                          close=("close", "last"), n=("sec", "size"))
    g = g[g["n"] >= 30]
    g.index = pd.to_datetime(g.index, unit="s", utc=True)
    return g


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int, cache_dir: Path,
                     fetcher: Callable[[str, int, int], list] | None = None) -> pd.DataFrame:
    """Trades agrégés Binance ``[start_ms, end_ms)`` (``T`` ms, ``p``), cache par plage."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_{int(start_ms)}_{int(end_ms)}.parquet"
    if path.exists():
        return pd.read_parquet(path)

    def api(sym: str, a: int, b: int) -> list:
        url = "https://data-api.binance.vision/api/v3/aggTrades"
        cur, page = a, []
        while cur < b and not page:        # premier identifiant >= a (fenêtres d'une minute)
            page = _http_json(url, {"symbol": sym, "startTime": cur, "endTime": min(b - 1, cur + 59_999), "limit": 1000})
            cur += 60_000
        rows: list = []
        while page:                        # puis pagination par identifiant (page vide : présent atteint)
            rows.extend(r for r in page if int(r["T"]) < b)
            if int(page[-1]["T"]) >= b:
                break
            page = _http_json(url, {"symbol": sym, "fromId": int(page[-1]["a"]) + 1, "limit": 1000})
        return rows

    rows = (fetcher or api)(symbol, int(start_ms), int(end_ms))
    df = pd.DataFrame({"a": [int(r["a"]) for r in rows], "T": [int(r["T"]) for r in rows],
                       "p": [float(r["p"]) for r in rows]})
    if len(df):
        df = df.drop_duplicates("a").sort_values(["T", "a"]).reset_index(drop=True)[["T", "p"]]
    else:
        df = pd.DataFrame({"T": np.zeros(0, dtype="int64"), "p": np.zeros(0)})
    if end_ms <= time.time() * 1000 - 5000:
        df.to_parquet(path)
    return df


def slot_of(ts, slot_s: int = 900) -> np.ndarray:
    return np.asarray(ts, dtype="int64") // int(slot_s)


def iter_chunks(seq: Sequence, n: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i: i + n]
