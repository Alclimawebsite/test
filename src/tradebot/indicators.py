"""Catalogue d'indicateurs techniques **strictement causaux** et registre.

Chaque indicateur est une fonction ``df -> pd.Series`` enregistrée dans ``REGISTRY``
(voir :class:`IndicatorSpec`). ``compute_indicators`` les calcule en bloc sur un
DataFrame OHLCV au format du contrat (``docs/ARCHITECTURE.md``).

Conventions
-----------
* **Causalité** : la valeur en ``t`` n'utilise que les barres ``<= t`` (fenêtres
  glissantes et moyennes exponentielles tournées vers le passé, ``shift`` positifs,
  cumuls). Jamais de ``shift`` négatif, de fenêtre centrée, de ``bfill`` ni de
  statistique calculée sur l'échantillon complet. Vérifié par
  ``tests/test_indicators.py`` : on tronque les données après ``t`` et toutes les
  valeurs jusqu'à ``t`` doivent rester identiques.
* **Normalisation** : jamais un niveau de prix brut. Chaque indicateur est un
  rapport au prix, un rendement logarithmique, un oscillateur borné, un z-score
  glissant, ou est exprimé en unités de volatilité (``σ60`` = volatilité réalisée
  par barre sur les 60 dernières barres, ou ATR). Les indicateurs sont ainsi
  comparables entre actifs.
* **Fenêtres** en barres (1 barre = 1 minute) : paramètres classiques (14, 20, 26…)
  complétés d'échelles en minutes (5, 15, 30, 60, 240, 1440 = 1 jour).
* **Période d'initialisation** : une fenêtre de ``n`` barres exige ``n`` observations
  (``min_periods = n``) et donne NaN avant. Seules exceptions : le z-score du
  funding sur 30 jours (au moins 1 jour d'historique) et le profil intraday du volume
  (au moins 2 jours précédents sur les 7 derniers).
* **Trous de données** : les fenêtres sont comptées en lignes. Une panne de
  l'exchange est donc « enjambée » ; c'est ``targets.py`` qui invalide les cibles
  qui traversent un trou.
* ``compute_indicators`` remplace ±inf par NaN et renvoie du float64.

Performance : tout est vectorisé (pandas ``rolling``/``ewm``, cumuls numpy, fenêtres
``sliding_window_view`` découpées en blocs). La seule récursion non linéaire, le
Supertrend, est résolue sans boucle par ligne (voir ``_ratchet_band``). Pendant un
appel à ``compute_indicators``, les séries intermédiaires communes (rendements,
σ60, ATR, EMA…) sont mises en cache et partagées entre indicateurs.
"""

from __future__ import annotations

import contextvars
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

__all__ = [
    "FAMILIES",
    "IndicatorSpec",
    "REGISTRY",
    "compute_indicators",
    "list_indicators",
]

FAMILIES: tuple[str, ...] = (
    "tendance",
    "momentum",
    "volatilite",
    "volume",
    "flux",
    "microstructure",
    "statistique",
    "calendrier",
    "marche",
    "derives",
)

# Unité de volatilité : σ réalisée par barre sur VOL_WINDOW barres.
VOL_WINDOW = 60
# Fenêtre des z-scores « de régime » : 1 jour de barres de 1 minute.
Z_WINDOW = 1440
# Funding : z-score sur 30 jours, calculable dès 1 jour d'historique.
FUNDING_Z_WINDOW = 30 * 1440
FUNDING_Z_MIN_PERIODS = 1440
# Fixings de funding Binance USDⓈ-M toutes les 8 h (00:00, 08:00, 16:00 UTC)
# pour BTCUSDT, ETHUSDT, SOLUSDT… (quelques contrats passent à 4 h : non géré ici).
FUNDING_PERIOD_MINUTES = 8 * 60
# Profil intraday du volume : même minute des PROFILE_DAYS jours précédents.
PROFILE_DAYS = 7
PROFILE_MIN_DAYS = 2
# Alignement de la référence (BTC) : report de la dernière valeur sur 5 barres au plus.
BENCH_FFILL_LIMIT = 5

_BASE_COLUMNS = ("open", "high", "low", "close")
_CHUNK = 1 << 16  # lignes par bloc pour les calculs sur fenêtres glissantes numpy


@dataclass(frozen=True)
class IndicatorSpec:
    """Description d'un indicateur du registre."""

    name: str  # identifiant unique snake_case, ex. "rsi_14"
    family: str  # une valeur de FAMILIES
    description: str  # une phrase en français
    func: Callable[[pd.DataFrame], pd.Series]
    needs_volume: bool = False
    needs_benchmark: bool = False  # colonne bench_close
    needs_columns: tuple[str, ...] = ()  # ex. ("taker_buy_volume",) ou ("funding_rate",)


REGISTRY: dict[str, IndicatorSpec] = {}


def _register(
    name: str,
    family: str,
    description: str,
    func: Callable[[pd.DataFrame], pd.Series],
    *,
    needs_volume: bool = False,
    needs_benchmark: bool = False,
    needs_columns: Iterable[str] = (),
) -> None:
    if name in REGISTRY:
        raise ValueError(f"indicateur déclaré deux fois : {name}")
    if family not in FAMILIES:
        raise ValueError(f"famille inconnue pour {name} : {family}")
    REGISTRY[name] = IndicatorSpec(
        name=name,
        family=family,
        description=description,
        func=func,
        needs_volume=needs_volume,
        needs_benchmark=needs_benchmark,
        needs_columns=tuple(needs_columns),
    )


# =============================================================================
# Cache des intermédiaires (actif uniquement pendant compute_indicators)
# =============================================================================

_CACHE: contextvars.ContextVar[tuple[pd.DataFrame, dict] | None] = contextvars.ContextVar(
    "tradebot_indicators_cache", default=None
)


def _cached(df: pd.DataFrame, key, fn: Callable[[], object]):
    """Mémorise ``fn()`` pour ``df`` pendant un appel à compute_indicators.

    Hors de compute_indicators (appel direct de ``spec.func(df)``), calcule sans cache.
    Les objets mis en cache ne doivent jamais être modifiés sur place.
    """
    ctx = _CACHE.get()
    if ctx is None or ctx[0] is not df:
        return fn()
    store = ctx[1]
    if key not in store:
        store[key] = fn()
    return store[key]


# =============================================================================
# Utilitaires numériques
# =============================================================================


def _series(values, df: pd.DataFrame) -> pd.Series:
    return pd.Series(np.asarray(values, dtype="float64"), index=df.index)


def _div(a, b):
    """a / b avec NaN à la place de ±inf (division par zéro)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    return out.where(np.isfinite(out))


def _log(x: pd.Series) -> pd.Series:
    """Logarithme népérien, NaN pour les valeurs <= 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(x)
    return out.where(np.isfinite(out))


def _sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def _rsum(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).sum()


def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(x: pd.Series, n: int) -> pd.Series:
    """Lissage de Wilder (RMA) : EMA de paramètre alpha = 1/n."""
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _zscore(x: pd.Series, n: int, min_periods: int | None = None) -> pd.Series:
    roll = x.rolling(n, min_periods=n if min_periods is None else min_periods)
    return _div(x - roll.mean(), roll.std())


def _rolling_mad(x: np.ndarray, center: np.ndarray, n: int) -> np.ndarray:
    """Écart absolu moyen de chaque fenêtre de n valeurs à ``center[t]`` (fenêtre finissant en t)."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    win = sliding_window_view(x, n)
    for a in range(0, win.shape[0], _CHUNK):
        blk = win[a : a + _CHUNK]
        c = center[a + n - 1 : a + n - 1 + blk.shape[0]]
        out[a + n - 1 : a + n - 1 + blk.shape[0]] = np.abs(blk - c[:, None]).mean(axis=1)
    return out


def _bars_since_max(x: np.ndarray, n: int) -> np.ndarray:
    """Nombre de barres depuis le maximum des n+1 dernières valeurs (0 = barre courante).

    En cas d'égalité, on retient l'occurrence la plus récente. NaN si la fenêtre
    contient un NaN ou si l'historique est trop court.
    """
    m = n + 1
    out = np.full(len(x), np.nan)
    if len(x) < m:
        return out
    isnan = np.isnan(x)
    xx = np.where(isnan, -np.inf, x)
    win = sliding_window_view(xx, m)
    for a in range(0, win.shape[0], _CHUNK):
        blk = win[a : a + _CHUNK, ::-1]
        out[a + m - 1 : a + m - 1 + blk.shape[0]] = blk.argmax(axis=1)
    if isnan.any():
        nan_in_win = pd.Series(isnan.astype("float64")).rolling(m, min_periods=1).sum().to_numpy()
        out[nan_in_win > 0] = np.nan
    return out


def _prev_ge_index(values: np.ndarray, queries: np.ndarray) -> np.ndarray:
    """Pour chaque k, le plus grand j <= k tel que values[j] >= queries[k] (-1 si aucun).

    Recherche vectorisée par « binary lifting » sur une table creuse de maxima
    glissants : O(N log N) opérations numpy, sans boucle par ligne. Causal : le
    résultat en k ne dépend que de values[:k+1] et de queries[k].
    """
    n = len(values)
    if n == 0:
        return np.empty(0, dtype=np.int64)
    levels = [values]  # levels[l][j] = max(values[j : j + 2**l])
    size = 1
    while size * 2 <= n:
        prev = levels[-1]
        levels.append(np.maximum(prev[:-size], prev[size:]))
        size *= 2
    pos = np.arange(1, n + 1)  # borne droite exclue de la zone déjà « sautée »
    for lvl in range(len(levels) - 1, -1, -1):
        step = 1 << lvl
        start = pos - step
        can = start >= 0
        block_max = levels[lvl][np.maximum(start, 0)]
        skip = can & (block_max < queries)
        pos = np.where(skip, start, pos)
    return pos - 1


def _ratchet_band(values: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """Bande « cliquet » du Supertrend (convention TradingView), sans boucle par ligne.

    band[i] = max(values[i], band[i-1]) si closes[i-1] > band[i-1], sinon values[i].
    La bande est donc le maximum cumulé de ``values`` depuis la dernière
    réinitialisation ; le segment commencé en s se termine au premier k >= s tel que
    closes[k] <= max(values[s..k]), c'est-à-dire q(k) >= s avec
    q = _prev_ge_index(values, closes). On calcule la fin de segment pour tout
    départ possible (minimum cumulé à rebours), puis on enchaîne les segments :
    une itération par segment, pas par barre. Causal : la fin d'un segment ne
    dépend que des barres qui le précèdent.
    """
    n = len(values)
    if n == 0:
        return np.empty(0)
    finite = np.isfinite(values)
    vals = np.where(finite, values, -np.inf)
    qs = np.where(np.isfinite(closes), closes, np.inf)
    q = _prev_ge_index(vals, qs)
    first_end = np.full(n, n, dtype=np.int64)
    ok = q >= 0
    np.minimum.at(first_end, q[ok], np.nonzero(ok)[0])
    seg_end = np.minimum.accumulate(first_end[::-1])[::-1].tolist()
    new_seg = np.zeros(n, dtype=np.int64)
    s = seg_end[0] + 1
    while s < n:
        new_seg[s] = 1
        s = seg_end[s] + 1
    seg_id = np.cumsum(new_seg)
    band = pd.Series(vals).groupby(seg_id).cummax().to_numpy().copy()
    band[~finite | ~np.isfinite(band)] = np.nan
    return band


# =============================================================================
# Séries de base (mises en cache)
# =============================================================================


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return _cached(df, ("col", name), lambda: df[name].astype("float64"))


def _open(df):
    return _col(df, "open")


def _high(df):
    return _col(df, "high")


def _low(df):
    return _col(df, "low")


def _close(df):
    return _col(df, "close")


def _volume(df):
    return _col(df, "volume")


def _logc(df):
    return _cached(df, "logc", lambda: _log(_close(df)))


def _r1(df):
    """Rendement logarithmique d'une barre."""
    return _cached(df, "r1", lambda: _logc(df).diff())


def _ret(df, k: int):
    """Rendement logarithmique sur k barres : ln(C_t / C_{t-k})."""
    return _cached(df, ("ret", k), lambda: _logc(df) - _logc(df).shift(k))


def _rv(df, n: int):
    """Volatilité réalisée par barre : racine de la moyenne des r² sur n barres."""
    return _cached(
        df, ("rv", n), lambda: np.sqrt((_r1(df) ** 2).rolling(n, min_periods=n).mean().clip(lower=0.0))
    )


def _vol_unit(df):
    """σ60 : unité de volatilité commune (par barre)."""
    return _rv(df, VOL_WINDOW)


def _ret_vol(df, k: int):
    """Rendement sur k barres en unités de volatilité σ60·√k."""
    return _cached(df, ("ret_vol", k), lambda: _div(_ret(df, k), _vol_unit(df) * math.sqrt(k)))


def _sma_close(df, n):
    return _cached(df, ("sma_close", n), lambda: _sma(_close(df), n))


def _ema_close(df, n):
    return _cached(df, ("ema_close", n), lambda: _ema(_close(df), n))


def _hh(df, n):
    return _cached(df, ("hh", n), lambda: _high(df).rolling(n, min_periods=n).max())


def _ll(df, n):
    return _cached(df, ("ll", n), lambda: _low(df).rolling(n, min_periods=n).min())


def _typical(df):
    return _cached(df, "typical", lambda: (_high(df) + _low(df) + _close(df)) / 3.0)


def _true_range(df):
    def calc():
        h, lo, pc = _high(df), _low(df), _close(df).shift(1)
        return np.fmax(h - lo, np.fmax((h - pc).abs(), (lo - pc).abs()))

    return _cached(df, "tr", calc)


def _atr(df, n):
    return _cached(df, ("atr", n), lambda: _wilder(_true_range(df), n))


def _bar_range(df):
    return _cached(df, "bar_range", lambda: _high(df) - _low(df))


def _range_ratio(df, num: pd.Series) -> pd.Series:
    """num / (haut − bas), 0 pour une barre plate (haut == bas)."""
    rng = _bar_range(df)
    return _div(num, rng).mask(rng == 0, 0.0)


def _clv(df):
    """Close location value : ((C − L) − (H − C)) / (H − L), dans [−1, 1]."""
    return _cached(
        df, "clv", lambda: _range_ratio(df, (_close(df) - _low(df)) - (_high(df) - _close(df)))
    )


def _quote_volume(df):
    """Volume en quote (prix × quantité) ; à défaut prix typique × volume."""

    def calc():
        est = _typical(df) * _volume(df)
        if "quote_volume" in df.columns:
            q = _col(df, "quote_volume")
            return q.where(q.notna(), est)
        return est

    return _cached(df, "quote", calc)


def _session_info(df) -> tuple[np.ndarray, np.ndarray]:
    """(position de la 1re barre de la séance, identifiant de séance) pour chaque barre.

    Utilise la colonne ``session`` (date UTC pour la crypto) si elle existe, sinon
    la date de l'index dans son fuseau.
    """

    def calc():
        n = len(df)
        if "session" in df.columns and df["session"].notna().any():
            key = df["session"].to_numpy()
        else:
            key = df.index.normalize().to_numpy()
        new = np.ones(n, dtype=bool)
        if n > 1:
            new[1:] = np.asarray(key[1:] != key[:-1], dtype=bool)
        start = np.maximum.accumulate(np.where(new, np.arange(n), 0)) if n else np.empty(0, np.int64)
        gid = np.cumsum(new) - 1
        return start, gid

    return _cached(df, "session_info", calc)


def _utc_index(df) -> pd.DatetimeIndex:
    def calc():
        idx = df.index
        return idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")

    return _cached(df, "utc_index", calc)


def _local_clock(df, tz: str) -> tuple[np.ndarray, np.ndarray]:
    """(minute du jour, jour de semaine lundi=0) dans le fuseau ``tz`` (heure d'été gérée)."""

    def calc():
        loc = _utc_index(df).tz_convert(tz)
        minutes = np.asarray(loc.hour, dtype=np.int64) * 60 + np.asarray(loc.minute, dtype=np.int64)
        return minutes, np.asarray(loc.dayofweek, dtype=np.int64)

    return _cached(df, ("clock", tz), calc)


# =============================================================================
# Tendance
# =============================================================================


def _sma_dist(df, n):
    return _div(_logc(df) - _log(_sma_close(df, n)), _vol_unit(df))


def _ema_dist(df, n):
    return _div(_logc(df) - _log(_ema_close(df, n)), _vol_unit(df))


def _lin_slope(df, n):
    """Pente MCO de ln(close) sur n barres (convolution causale), × √n / σ60."""
    y = _logc(df).to_numpy()
    w = np.arange(n, dtype="float64") - (n - 1) / 2.0
    w /= np.dot(w, w)
    slope = np.full(len(y), np.nan)
    if len(y) >= n:
        slope[n - 1 :] = np.convolve(y, w[::-1], mode="valid")
    return _div(_series(slope, df) * math.sqrt(n), _vol_unit(df))


def _macd_parts(df, f, s, g):
    def calc():
        line = _ema_close(df, f) - _ema_close(df, s)
        signal = line.ewm(span=g, adjust=False, min_periods=g).mean()
        return {"line": line, "signal": signal, "hist": line - signal}

    return _cached(df, ("macd", f, s, g), calc)


def _macd(df, f, s, g, part):
    return _div(_macd_parts(df, f, s, g)[part], _close(df) * _vol_unit(df))


def _dmi(df, n):
    """(+DI, −DI, ADX) de Wilder, en fractions dans [0, 1]."""

    def calc():
        up = _high(df).diff()
        down = -_low(df).diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        atr = _atr(df, n)
        pdi = _div(_wilder(plus_dm, n), atr)
        mdi = _div(_wilder(minus_dm, n), atr)
        dx = _div((pdi - mdi).abs(), pdi + mdi)
        return pdi, mdi, _wilder(dx, n)

    return _cached(df, ("dmi", n), calc)


def _adx(df, n):
    return _dmi(df, n)[2]


def _di_diff(df, n):
    pdi, mdi, _ = _dmi(df, n)
    return pdi - mdi


def _aroon_osc(df, n):
    up = (n - _bars_since_max(_high(df).to_numpy(), n)) / n
    down = (n - _bars_since_max(-_low(df).to_numpy(), n)) / n
    return _series(up - down, df)


def _ema_gap(df, f, s):
    return _div(_ema_close(df, f) - _ema_close(df, s), _close(df) * _vol_unit(df))


def _efficiency_ratio(df, n):
    c = _close(df)
    return _div((c - c.shift(n)).abs(), _rsum(c.diff().abs(), n))


def _mid_range(df, n):
    return _cached(df, ("mid_range", n), lambda: (_hh(df, n) + _ll(df, n)) / 2.0)


def _ichimoku_dist(df, n):
    return _div(_close(df) - _mid_range(df, n), _atr(df, 14))


def _ichimoku_tk_diff(df):
    return _div(_mid_range(df, 9) - _mid_range(df, 26), _atr(df, 14))


def _ichimoku_cloud_dist(df):
    span_a = ((_mid_range(df, 9) + _mid_range(df, 26)) / 2.0).shift(26)
    span_b = _mid_range(df, 52).shift(26)
    top = np.fmax(span_a, span_b).where(span_a.notna() & span_b.notna())
    bottom = np.fmin(span_a, span_b).where(span_a.notna() & span_b.notna())
    c = _close(df)
    gap = (c - top).where(c > top, 0.0).where(c >= bottom, c - bottom)
    gap = gap.where(top.notna() & c.notna())
    return _div(gap, _atr(df, 14))


def _supertrend(df, n=10, mult=3.0):
    """(distance en ATR, direction ±1) du Supertrend, convention TradingView.

    Bande basse = cliquet haussier de hl2 − mult·ATR, bande haute = cliquet baissier
    de hl2 + mult·ATR ; la tendance passe à +1 quand le close dépasse la bande haute
    précédente, à −1 quand il passe sous la bande basse précédente (en cas de double
    signal, rarissime, la tendance s'inverse). Tendance initiale : +1.
    """

    def calc():
        c = _close(df).to_numpy()
        atr = _atr(df, n).to_numpy()
        hl2 = ((_high(df) + _low(df)) / 2.0).to_numpy()
        size = len(c)
        dist = np.full(size, np.nan)
        direction = np.full(size, np.nan)
        valid = np.isfinite(atr) & np.isfinite(hl2) & np.isfinite(c)
        if not valid.any():
            return _series(dist, df), _series(direction, df)
        s0 = int(np.argmax(valid))
        cc, aa, mid = c[s0:], atr[s0:], hl2[s0:]
        lower = _ratchet_band(mid - mult * aa, cc)
        upper = -_ratchet_band(-(mid + mult * aa), -cc)
        lower_prev = np.r_[np.nan, lower[:-1]]
        upper_prev = np.r_[np.nan, upper[:-1]]
        with np.errstate(invalid="ignore"):
            up_break = cc > upper_prev
            down_break = cc < lower_prev
        toggle = up_break & down_break
        set_up = up_break & ~down_break
        set_down = down_break & ~up_break
        m = len(cc)
        last_set = np.maximum.accumulate(np.where(set_up | set_down, np.arange(m), -1))
        has_set = last_set >= 0
        ls = np.maximum(last_set, 0)
        base = np.where(has_set, np.where(set_up[ls], 1.0, -1.0), 1.0)
        tcum = np.cumsum(toggle)
        flips = tcum - np.where(has_set, tcum[ls], 0)
        state = np.where(flips % 2 == 0, base, -base)
        line = np.where(state > 0, lower, upper)
        with np.errstate(divide="ignore", invalid="ignore"):
            dist[s0:] = (cc - line) / aa
        direction[s0:] = state
        return _series(dist, df), _series(direction, df)

    return _cached(df, ("supertrend", n, mult), calc)


def _supertrend_dist(df):
    return _supertrend(df)[0]


def _supertrend_dir(df):
    return _supertrend(df)[1]


# =============================================================================
# Momentum
# =============================================================================


def _ret_skip(df, k):
    lc = _logc(df)
    return _div(lc.shift(1) - lc.shift(k), _vol_unit(df) * math.sqrt(k - 1))


def _rsi(df, n):
    d = _close(df).diff()
    gain = _wilder(d.clip(lower=0.0), n)
    loss = _wilder((-d).clip(lower=0.0), n)
    return 2.0 * _div(gain, gain + loss) - 1.0


def _stoch_k_raw(df, n):
    """%K rapide dans [0, 1] : position du close dans [plus bas, plus haut] sur n barres."""
    return _cached(df, ("stoch_k", n), lambda: _div(_close(df) - _ll(df, n), _hh(df, n) - _ll(df, n)))


def _stoch_k(df, n):
    return 2.0 * _stoch_k_raw(df, n) - 1.0


def _stoch_d(df, n, d):
    return 2.0 * _sma(_stoch_k_raw(df, n), d) - 1.0


def _williams_r(df, n):
    return _stoch_k_raw(df, n) - 1.0


def _cci(df, n):
    tp = _typical(df)
    sma = _sma(tp, n)
    mad = _series(_rolling_mad(tp.to_numpy(), sma.to_numpy(), n), df)
    return _div(tp - sma, 0.015 * mad) / 100.0


def _tsi(df, r, s):
    m = _close(df).diff()
    return _div(_ema(_ema(m, r), s), _ema(_ema(m.abs(), r), s))


def _ultimate_osc(df):
    c = _close(df)
    pc = c.shift(1)
    low_ = np.fmin(_low(df), pc)
    high_ = np.fmax(_high(df), pc)
    bp = c - low_
    tr = high_ - low_
    avg = {n: _div(_rsum(bp, n), _rsum(tr, n)) for n in (7, 14, 28)}
    uo = (4.0 * avg[7] + 2.0 * avg[14] + avg[28]) / 7.0
    return 2.0 * uo - 1.0


def _mom_z(df, k):
    return _zscore(_ret(df, k), Z_WINDOW)


# =============================================================================
# Volatilité
# =============================================================================


def _atr_pct(df, n):
    return _div(_atr(df, n), _close(df))


def _bollinger(df, n, k):
    def calc():
        c = _close(df)
        sma = _sma_close(df, n)
        sd = c.rolling(n, min_periods=n).std(ddof=0)
        pctb = _div(c - (sma - k * sd), 2.0 * k * sd)
        width = _div(2.0 * k * sd, sma)
        return pctb, width

    return _cached(df, ("bollinger", n, k), calc)


def _bb_pctb(df, n, k):
    return _bollinger(df, n, k)[0]


def _bb_width(df, n, k):
    return _bollinger(df, n, k)[1]


def _bb_width_rel(df, n, k):
    w = _bb_width(df, n, k)
    return _div(w, _sma(w, Z_WINDOW))


def _keltner_pos(df, n, n_atr, mult):
    return _div(_close(df) - _ema_close(df, n), mult * _atr(df, n_atr))


def _donchian_pos(df, n):
    return _stoch_k_raw(df, n) - 0.5


def _parkinson(df, n):
    hl = (_log(_high(df)) - _log(_low(df))) ** 2
    return np.sqrt(_sma(hl, n).clip(lower=0.0) / (4.0 * math.log(2.0)))


def _garman_klass(df, n):
    hl = (_log(_high(df)) - _log(_low(df))) ** 2
    co = (_logc(df) - _log(_open(df))) ** 2
    term = 0.5 * hl - (2.0 * math.log(2.0) - 1.0) * co
    return np.sqrt(_sma(term, n).clip(lower=0.0))


def _rv_ratio(df, a, b):
    return _div(_rv(df, a), _rv(df, b))


def _range_atr(df, n):
    return _div(_bar_range(df), _atr(df, n))


def _jump_ratio(df, n):
    r = _r1(df)
    rv = _rsum(r**2, n)
    bv = (math.pi / 2.0) * _rsum(r.abs() * r.abs().shift(1), n)
    return _div((rv - bv).clip(lower=0.0), rv)


def _ret_x_rv_ratio(df):
    return _ret_vol(df, 15) * _rv_ratio(df, 60, 1440)


# =============================================================================
# Volume
# =============================================================================


def _signed_volume(df):
    """signe(ΔC) × volume (0 si le close ne change pas) : incréments de l'OBV."""
    return _cached(df, "signed_vol", lambda: np.sign(_close(df).diff()) * _volume(df))


def _obv_z(df, n):
    return _zscore(_signed_volume(df).cumsum(), n)


def _obv_slope(df, n):
    return _div(_rsum(_signed_volume(df), n), _rsum(_volume(df), n))


def _vwap_dev_session(df):
    _, gid = _session_info(df)
    num = _quote_volume(df).groupby(gid).cumsum()
    den = _volume(df).groupby(gid).cumsum()
    vwap = _div(num, den)
    return _div(_logc(df) - _log(vwap), _vol_unit(df))


def _vwap_dev(df, n):
    vwap = _div(_rsum(_quote_volume(df), n), _rsum(_volume(df), n))
    return _div(_logc(df) - _log(vwap), _vol_unit(df))


def _mfi(df, n):
    tp = _typical(df)
    flow = tp * _volume(df)
    dtp = tp.diff()
    pos = _rsum(flow.where(dtp > 0, 0.0), n)
    neg = _rsum(flow.where(dtp < 0, 0.0), n)
    return 2.0 * _div(pos, pos + neg) - 1.0


def _cmf(df, n):
    return _div(_rsum(_clv(df) * _volume(df), n), _rsum(_volume(df), n))


def _force_index(df, n):
    v = _volume(df)
    return _div(_ema(_r1(df) * v, n), _ema(v, n) * _vol_unit(df))


def _rel_volume(df, n):
    v = _volume(df)
    return _div(v, _sma(v, n))


def _same_minute_mean(x: pd.Series, days: int, min_days: int) -> np.ndarray:
    """Moyenne de x à la même heure (même minute) sur les ``days`` jours précédents.

    Recherche exacte des horodatages t − k jours (k = 1..days) : les jours manquants
    sont ignorés ; NaN si moins de ``min_days`` valeurs. Causal (passé uniquement).
    """
    idx = x.index
    acc = np.zeros(len(x))
    cnt = np.zeros(len(x))
    for k in range(1, days + 1):
        prev = x.reindex(idx - pd.Timedelta(days=k)).to_numpy(dtype="float64")
        ok = np.isfinite(prev)
        acc += np.where(ok, prev, 0.0)
        cnt += ok
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = acc / cnt
    mean[cnt < min_days] = np.nan
    return mean


def _rel_volume_tod(df):
    v = _volume(df)
    return _div(v, _series(_same_minute_mean(v, PROFILE_DAYS, PROFILE_MIN_DAYS), df))


def _adl_z(df, n):
    return _zscore((_clv(df) * _volume(df)).cumsum(), n)


def _vpt_z(df, n):
    c = _close(df)
    return _zscore((_volume(df) * _div(c, c.shift(1)).sub(1.0)).cumsum(), n)


def _eom(df, n):
    """Ease of Movement sans dimension : Δln(milieu) × range relatif / volume relatif, lissé, / σ60²."""
    mid = (_high(df) + _low(df)) / 2.0
    rng_rel = _div(_bar_range(df), mid)
    vrel = _rel_volume(df, VOL_WINDOW)
    emv = _div(_log(mid).diff() * rng_rel, vrel).mask(rng_rel == 0, 0.0)
    return _div(_sma(emv, n), _vol_unit(df) ** 2)


# =============================================================================
# Flux d'ordres (volume taker acheteur, nombre de trades)
# =============================================================================


def _taker_buy(df):
    return _col(df, "taker_buy_volume")


def _taker_imb(df, k):
    def calc():
        b, v = _taker_buy(df), _volume(df)
        if k == 1:
            return 2.0 * _div(b, v) - 1.0
        return 2.0 * _div(_rsum(b, k), _rsum(v, k)) - 1.0

    return _cached(df, ("taker_imb", k), calc)


def _taker_imb_z(df, k):
    return _zscore(_taker_imb(df, k), Z_WINDOW)


def _cvd_z(df, n):
    cvd = (2.0 * _taker_buy(df) - _volume(df)).cumsum()
    return _zscore(cvd, n)


def _trades_rel(df, n):
    t = _col(df, "trades")
    return _div(t, _sma(t, n))


def _avg_trade_size_rel(df, n):
    v, t = _volume(df), _col(df, "trades")
    return _div(_div(v, t), _div(_rsum(v, n), _rsum(t, n)))


def _tick_imb(df, n):
    sgn = np.sign(_close(df).diff())
    sgn = sgn.mask(sgn == 0).ffill()  # règle du tick : signe de la dernière variation non nulle
    v = _volume(df)
    return _div(_rsum(sgn * v, n), _rsum(v, n))


def _ret_x_taker(df):
    return _ret_vol(df, 15) * _taker_imb(df, 15).abs()


# =============================================================================
# Microstructure
# =============================================================================


def _clv_mean(df, n):
    return _sma(_clv(df), n)


def _body_ratio(df):
    return _range_ratio(df, _close(df) - _open(df))


def _upper_wick(df):
    return _range_ratio(df, _high(df) - np.fmax(_open(df), _close(df)))


def _lower_wick(df):
    return _range_ratio(df, np.fmin(_open(df), _close(df)) - _low(df))


def _amihud(df, n):
    return _cached(
        df, ("amihud", n), lambda: _log(_div(_rsum(_r1(df).abs(), n), _rsum(_quote_volume(df), n)))
    )


def _amihud_rel(df, n):
    a = _amihud(df, n)
    return a - _sma(a, Z_WINDOW)


def _roll_spread(df, n):
    r = _r1(df)
    cov = r.rolling(n, min_periods=n).cov(r.shift(1))
    return 2.0 * np.sqrt((-cov).clip(lower=0.0))


def _cs_spread(df, n):
    """Écart de Corwin-Schultz (2012) sur paires de barres consécutives, moyenné sur n barres."""
    lh, ll = _log(_high(df)), _log(_low(df))
    hl2 = (lh - ll) ** 2
    beta = hl2 + hl2.shift(1)
    gamma = (np.maximum(lh, lh.shift(1)) - np.minimum(ll, ll.shift(1))) ** 2
    k = 3.0 - 2.0 * math.sqrt(2.0)
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    ea = np.exp(alpha)
    spread = (2.0 * (ea - 1.0) / (1.0 + ea)).clip(lower=0.0)
    return _sma(spread, n)


def _zero_ret_frac(df, n):
    r = _r1(df)
    return _sma((r == 0).astype("float64").where(r.notna()), n)


# =============================================================================
# Statistique
# =============================================================================


def _price_zscore(df, n):
    return _zscore(_close(df), n)


def _acf1(df, n):
    r = _r1(df)
    return r.rolling(n, min_periods=n).corr(r.shift(1))


def _variance_ratio(df, q, n):
    return _div(_ret(df, q).rolling(n, min_periods=n).var(), q * _r1(df).rolling(n, min_periods=n).var())


def _skew(df, n):
    return _r1(df).rolling(n, min_periods=n).skew()


def _kurt(df, n):
    return _r1(df).rolling(n, min_periods=n).kurt()


# =============================================================================
# Calendrier (UTC, marché 24/7)
# =============================================================================


def _utc_minutes(df):
    return _local_clock(df, "UTC")[0]


def _hour_angle(df):
    return 2.0 * np.pi * _utc_minutes(df) / 1440.0


def _hour_sin(df):
    return _series(np.sin(_hour_angle(df)), df)


def _hour_cos(df):
    return _series(np.cos(_hour_angle(df)), df)


def _dow_angle(df):
    minutes, dow = _local_clock(df, "UTC")
    return 2.0 * np.pi * (dow + minutes / 1440.0) / 7.0


def _dow_sin(df):
    return _series(np.sin(_dow_angle(df)), df)


def _dow_cos(df):
    return _series(np.cos(_dow_angle(df)), df)


def _is_weekend(df):
    return _series(_local_clock(df, "UTC")[1] >= 5, df)


def _in_session(df, tz, start_min, end_min):
    """1 si la barre s'ouvre dans [start, end) heure locale de ``tz``, du lundi au vendredi."""
    minutes, dow = _local_clock(df, tz)
    return _series((minutes >= start_min) & (minutes < end_min) & (dow < 5), df)


def _min_since_funding(df):
    return _series(_utc_minutes(df) % FUNDING_PERIOD_MINUTES, df)


def _min_to_funding(df):
    return _series(FUNDING_PERIOD_MINUTES - _utc_minutes(df) % FUNDING_PERIOD_MINUTES, df)


def _ret_since_day_open(df):
    start, _ = _session_info(df)
    first_open = _open(df).to_numpy()[start]
    return _logc(df) - _log(_series(first_open, df))


def _ret_since_day_open_vol(df):
    start, _ = _session_info(df)
    bars = np.arange(len(df)) - start + 1.0
    return _div(_ret_since_day_open(df), _vol_unit(df) * np.sqrt(bars))


def _ret_x_weekend(df):
    return _ret_vol(df, 15) * _is_weekend(df)


# =============================================================================
# Marché (référence : colonne bench_close, BTCUSDT pour les altcoins)
# =============================================================================


def _bench_ret(df, k):
    def calc():
        lb = _cached(df, "bench_logc", lambda: _log(_col(df, "bench_close")))
        return lb - lb.shift(k)

    return _cached(df, ("bench_ret", k), calc)


def _rel_ret(df, k):
    return _ret(df, k) - _bench_ret(df, k)


def _beta_corr(df, n):
    def calc():
        r, rb = _r1(df), _bench_ret(df, 1)
        roll = r.rolling(n, min_periods=n)
        beta = _div(roll.cov(rb), rb.rolling(n, min_periods=n).var())
        return beta, roll.corr(rb)

    return _cached(df, ("beta_corr", n), calc)


def _beta_bench(df, n):
    return _beta_corr(df, n)[0]


def _corr_bench(df, n):
    return _beta_corr(df, n)[1]


def _resid_ret(df, k, n):
    return _ret(df, k) - _beta_corr(df, n)[0] * _bench_ret(df, k)


# =============================================================================
# Dérivés (futures USDⓈ-M : funding, open interest, ratios long/short)
# =============================================================================


def _funding_bp(df):
    return _col(df, "funding_rate") * 1e4


def _funding_z(df):
    return _zscore(_col(df, "funding_rate"), FUNDING_Z_WINDOW, FUNDING_Z_MIN_PERIODS)


def _oi_chg(df, k):
    def calc():
        oi = _col(df, "open_interest")
        return 100.0 * (_div(oi, oi.shift(k)) - 1.0)

    return _cached(df, ("oi_chg", k), calc)


def _oi_z(df, n):
    return _zscore(_col(df, "open_interest"), n)


def _oi_x_ret(df, k):
    return _oi_chg(df, k) * _ret_vol(df, k)


def _log_col(df, col):
    return _cached(df, ("log_col", col), lambda: _log(_col(df, col)))


def _log_col_z(df, col, n):
    return _zscore(_log_col(df, col), n)


# =============================================================================
# Déclaration du registre
# =============================================================================

_VOL = {"needs_volume": True}

# --- tendance ---------------------------------------------------------------
for _n in (5, 15, 30, 60, 240):
    _register(f"sma_dist_{_n}", "tendance",
              f"Écart logarithmique du close à sa moyenne mobile simple sur {_n} barres, en unités de volatilité σ60.",
              partial(_sma_dist, n=_n))
for _n in (5, 15, 30, 60, 240):
    _register(f"ema_dist_{_n}", "tendance",
              f"Écart logarithmique du close à sa moyenne mobile exponentielle sur {_n} barres, en unités de volatilité σ60.",
              partial(_ema_dist, n=_n))
for _n in (15, 60, 240):
    _register(f"lin_slope_{_n}", "tendance",
              f"Pente de la régression linéaire de ln(close) sur {_n} barres, ramenée au mouvement attendu σ60·√{_n}.",
              partial(_lin_slope, n=_n))
for (_f, _s, _g) in ((12, 26, 9), (6, 13, 5)):
    _register(f"macd_line_{_f}_{_s}", "tendance",
              f"Ligne MACD (EMA {_f} − EMA {_s}) rapportée à close·σ60.",
              partial(_macd, f=_f, s=_s, g=_g, part="line"))
    _register(f"macd_signal_{_f}_{_s}_{_g}", "tendance",
              f"Ligne de signal du MACD ({_f}, {_s}) : EMA {_g} de la ligne MACD, rapportée à close·σ60.",
              partial(_macd, f=_f, s=_s, g=_g, part="signal"))
    _register(f"macd_hist_{_f}_{_s}_{_g}", "tendance",
              f"Histogramme du MACD ({_f}, {_s}, {_g}) : ligne − signal, rapporté à close·σ60.",
              partial(_macd, f=_f, s=_s, g=_g, part="hist"))
_register("adx_14", "tendance",
          "ADX de Wilder sur 14 barres (force de la tendance, sans signe), dans [0, 1].",
          partial(_adx, n=14))
_register("di_diff_14", "tendance",
          "Écart DI+ − DI− de Wilder sur 14 barres (sens de la tendance), dans [−1, 1].",
          partial(_di_diff, n=14))
_register("aroon_osc_25", "tendance",
          "Oscillateur Aroon sur 25 barres (Aroon haut − Aroon bas), dans [−1, 1].",
          partial(_aroon_osc, n=25))
for (_f, _s) in ((5, 20), (15, 60), (60, 240)):
    _register(f"ema_gap_{_f}_{_s}", "tendance",
              f"Écart entre EMA {_f} et EMA {_s} rapporté à close·σ60.",
              partial(_ema_gap, f=_f, s=_s))
for _n in (10, 30):
    _register(f"efficiency_ratio_{_n}", "tendance",
              f"Efficiency ratio de Kaufman sur {_n} barres : |déplacement net| / chemin parcouru, dans [0, 1].",
              partial(_efficiency_ratio, n=_n))
_register("ichimoku_tenkan_dist", "tendance",
          "Distance du close à la Tenkan-sen (milieu du range sur 9 barres), en ATR 14.",
          partial(_ichimoku_dist, n=9))
_register("ichimoku_kijun_dist", "tendance",
          "Distance du close à la Kijun-sen (milieu du range sur 26 barres), en ATR 14.",
          partial(_ichimoku_dist, n=26))
_register("ichimoku_tk_diff", "tendance",
          "Écart Tenkan-sen − Kijun-sen, en ATR 14.",
          _ichimoku_tk_diff)
_register("ichimoku_cloud_dist", "tendance",
          "Distance du close au nuage Ichimoku (spans décalés de 26 barres vers le futur, donc connus), en ATR 14 ; 0 dans le nuage.",
          _ichimoku_cloud_dist)
_register("supertrend_dist", "tendance",
          "Distance signée du close à la ligne Supertrend (ATR 10 × 3), en ATR 10 : positive en tendance haussière.",
          _supertrend_dist)
_register("supertrend_dir", "tendance",
          "Direction du Supertrend (ATR 10 × 3) : +1 haussier, −1 baissier.",
          _supertrend_dir)

# --- momentum ---------------------------------------------------------------
_RET_WINDOWS = (1, 5, 10, 15, 30, 60, 240)
for _k in _RET_WINDOWS:
    _register(f"ret_{_k}", "momentum",
              f"Rendement logarithmique des {_k} dernières barres : ln(C_t / C_t−{_k}).",
              partial(_ret, k=_k))
for _k in _RET_WINDOWS:
    _register(f"ret_vol_{_k}", "momentum",
              f"Rendement logarithmique sur {_k} barres en unités de volatilité (σ60·√{_k}).",
              partial(_ret_vol, k=_k))
for _k in (5, 15, 60):
    _register(f"ret_skip_{_k}", "momentum",
              f"Rendement de t−{_k} à t−1 (sans la dernière barre), en unités de volatilité σ60·√{_k - 1}.",
              partial(_ret_skip, k=_k))
for _n in (2, 6, 14):
    _register(f"rsi_{_n}", "momentum",
              f"RSI de Wilder sur {_n} barres, recentré : (RSI − 50) / 50, dans [−1, 1].",
              partial(_rsi, n=_n))
for (_n, _d) in ((14, 3), (5, 3)):
    _register(f"stoch_k_{_n}", "momentum",
              f"Stochastique %K rapide sur {_n} barres, ramené à [−1, 1].",
              partial(_stoch_k, n=_n))
    _register(f"stoch_d_{_n}_{_d}", "momentum",
              f"Stochastique %D : moyenne sur {_d} barres du %K {_n}, ramenée à [−1, 1].",
              partial(_stoch_d, n=_n, d=_d))
_register("williams_r_14", "momentum",
          "Williams %R sur 14 barres divisé par 100, dans [−1, 0] (miroir du stochastique).",
          partial(_williams_r, n=14))
_register("cci_20", "momentum",
          "Commodity Channel Index sur 20 barres divisé par 100 (borné à ±6,67 par construction).",
          partial(_cci, n=20))
_register("tsi_25_13", "momentum",
          "True Strength Index (double lissage exponentiel 25 puis 13 des variations), dans [−1, 1].",
          partial(_tsi, r=25, s=13))
_register("ultimate_osc", "momentum",
          "Ultimate Oscillator de Williams (7, 14, 28 barres), ramené à [−1, 1].",
          _ultimate_osc)
for _k in (15, 60):
    _register(f"mom_z_{_k}", "momentum",
              f"Z-score du rendement sur {_k} barres par rapport au dernier jour (1440 barres).",
              partial(_mom_z, k=_k))

# --- volatilité ---------------------------------------------------------------
_register("atr_pct_14", "volatilite",
          "ATR de Wilder sur 14 barres rapporté au close.",
          partial(_atr_pct, n=14))
_register("bb_pctb_20_2", "volatilite",
          "%B de Bollinger (20, 2) : position du close dans les bandes (0 = bande basse, 1 = bande haute).",
          partial(_bb_pctb, n=20, k=2.0))
_register("bb_width_20_2", "volatilite",
          "Largeur des bandes de Bollinger (20, 2) rapportée à la moyenne mobile.",
          partial(_bb_width, n=20, k=2.0))
_register("bb_width_rel_20", "volatilite",
          "Largeur de Bollinger (20, 2) rapportée à sa moyenne du dernier jour (compression ou détente).",
          partial(_bb_width_rel, n=20, k=2.0))
_register("keltner_pos_20", "volatilite",
          "Position dans le canal de Keltner : (close − EMA 20) / (2 × ATR 10), ±1 sur les bandes.",
          partial(_keltner_pos, n=20, n_atr=10, mult=2.0))
for _n in (20, 60):
    _register(f"donchian_pos_{_n}", "volatilite",
              f"Position du close dans le canal de Donchian sur {_n} barres, dans [−0,5 ; 0,5].",
              partial(_donchian_pos, n=_n))
for _n in (15, 60, 240):
    _register(f"rv_{_n}", "volatilite",
              f"Volatilité réalisée par barre : racine de la moyenne des rendements au carré sur {_n} barres.",
              partial(_rv, n=_n))
for _n in (15, 60):
    _register(f"parkinson_{_n}", "volatilite",
              f"Volatilité de Parkinson (range haut-bas) par barre, sur {_n} barres.",
              partial(_parkinson, n=_n))
    _register(f"garman_klass_{_n}", "volatilite",
              f"Volatilité de Garman-Klass (OHLC) par barre, sur {_n} barres.",
              partial(_garman_klass, n=_n))
for (_a, _b) in ((15, 240), (60, 1440)):
    _register(f"rv_ratio_{_a}_{_b}", "volatilite",
              f"Rapport de volatilité réalisée courte / longue ({_a} / {_b} barres).",
              partial(_rv_ratio, a=_a, b=_b))
_register("range_atr_14", "volatilite",
          "Range de la barre (haut − bas) en ATR 14.",
          partial(_range_atr, n=14))
_register("jump_ratio_240", "volatilite",
          "Part des sauts dans la variance réalisée sur 240 barres : max(RV − BV, 0) / RV (BV = variation bipuissance).",
          partial(_jump_ratio, n=240))
_register("ret15_x_rv_ratio", "volatilite",
          "Interaction : rendement 15 barres en σ × rapport de volatilité 60 / 1440 barres.",
          _ret_x_rv_ratio)

# --- volume -------------------------------------------------------------------
_register("obv_z_240", "volume",
          "Z-score roulant sur 240 barres de l'On-Balance Volume.",
          partial(_obv_z, n=240), **_VOL)
for _n in (15, 60):
    _register(f"obv_slope_{_n}", "volume",
              f"Pente de l'OBV sur {_n} barres rapportée au volume échangé, dans [−1, 1].",
              partial(_obv_slope, n=_n), **_VOL)
_register("vwap_dev_session", "volume",
          "Écart logarithmique du close au VWAP de la journée UTC (réinitialisé à 00:00), en unités σ60.",
          _vwap_dev_session, **_VOL)
for _n in (60, 240):
    _register(f"vwap_dev_{_n}", "volume",
              f"Écart logarithmique du close au VWAP roulant sur {_n} barres, en unités σ60.",
              partial(_vwap_dev, n=_n), **_VOL)
_register("mfi_14", "volume",
          "Money Flow Index sur 14 barres, recentré : (MFI − 50) / 50, dans [−1, 1].",
          partial(_mfi, n=14), **_VOL)
_register("cmf_20", "volume",
          "Chaikin Money Flow sur 20 barres, dans [−1, 1].",
          partial(_cmf, n=20), **_VOL)
_register("force_index_13", "volume",
          "Force index : EMA 13 de (rendement × volume) / EMA 13 du volume, en unités σ60.",
          partial(_force_index, n=13), **_VOL)
for _n in (60, 1440):
    _register(f"rel_volume_{_n}", "volume",
              f"Volume de la barre rapporté à sa moyenne sur {_n} barres.",
              partial(_rel_volume, n=_n), **_VOL)
_register("rel_volume_tod", "volume",
          f"Volume rapporté à la moyenne de la même minute sur les {PROFILE_DAYS} jours précédents (profil intraday causal).",
          _rel_volume_tod, **_VOL)
_register("adl_z_240", "volume",
          "Z-score roulant sur 240 barres de la ligne accumulation/distribution de Chaikin.",
          partial(_adl_z, n=240), **_VOL)
_register("vpt_z_240", "volume",
          "Z-score roulant sur 240 barres du Volume-Price Trend.",
          partial(_vpt_z, n=240), **_VOL)
_register("eom_14", "volume",
          "Ease of Movement sans dimension : Δln(milieu) × range relatif / volume relatif, moyenné sur 14 barres, / σ60².",
          partial(_eom, n=14), **_VOL)

# --- flux ---------------------------------------------------------------------
_TAKER = {"needs_volume": True, "needs_columns": ("taker_buy_volume",)}
_TRADES = {"needs_volume": True, "needs_columns": ("trades",)}
_register("taker_imb_1", "flux",
          "Déséquilibre taker de la barre : 2 × taker_buy_volume / volume − 1, dans [−1, 1].",
          partial(_taker_imb, k=1), **_TAKER)
for _k in (5, 15, 60):
    _register(f"taker_imb_{_k}", "flux",
              f"Déséquilibre taker cumulé sur {_k} barres : 2 × Σ achats taker / Σ volume − 1, dans [−1, 1].",
              partial(_taker_imb, k=_k), **_TAKER)
_register("taker_imb_z_15", "flux",
          "Z-score (1 jour) du déséquilibre taker sur 15 barres, qui corrige le biais acheteur ou vendeur propre à la paire.",
          partial(_taker_imb_z, k=15), **_TAKER)
for _n in (60, 240):
    _register(f"cvd_z_{_n}", "flux",
              f"Z-score roulant sur {_n} barres du delta de volume cumulé (CVD = Σ achats taker − ventes taker).",
              partial(_cvd_z, n=_n), **_TAKER)
_register("trades_rel_60", "flux",
          "Nombre de trades de la barre rapporté à sa moyenne sur 60 barres.",
          partial(_trades_rel, n=60), **_TRADES)
_register("avg_trade_size_rel_60", "flux",
          "Taille moyenne des trades (volume / trades) rapportée à celle des 60 dernières barres.",
          partial(_avg_trade_size_rel, n=60), **_TRADES)
for _n in (15, 60):
    _register(f"tick_imb_{_n}", "flux",
              f"Déséquilibre signé par la règle du tick sur {_n} barres (signe de la dernière variation non nulle), dans [−1, 1].",
              partial(_tick_imb, n=_n), **_VOL)
_register("ret15_x_taker_abs15", "flux",
          "Interaction : rendement 15 barres en σ × |déséquilibre taker 15 barres| (retournement accentué par le flux).",
          _ret_x_taker, **_TAKER)

# --- microstructure -------------------------------------------------------------
_register("clv", "microstructure",
          "Position du close dans le range de la barre (close location value), dans [−1, 1].",
          _clv)
_register("clv_15", "microstructure",
          "Moyenne sur 15 barres de la position du close dans le range de la barre, dans [−1, 1].",
          partial(_clv_mean, n=15))
_register("body_ratio", "microstructure",
          "Corps de la bougie rapporté au range : (close − open) / (haut − bas), dans [−1, 1].",
          _body_ratio)
_register("upper_wick", "microstructure",
          "Mèche haute rapportée au range de la barre, dans [0, 1].",
          _upper_wick)
_register("lower_wick", "microstructure",
          "Mèche basse rapportée au range de la barre, dans [0, 1].",
          _lower_wick)
_register("amihud_60", "microstructure",
          "Illiquidité d'Amihud sur 60 barres, en log : ln(Σ|r| / Σ volume en quote).",
          partial(_amihud, n=60), **_VOL)
_register("amihud_rel_60", "microstructure",
          "Illiquidité d'Amihud (60 barres, log) moins sa moyenne du dernier jour.",
          partial(_amihud_rel, n=60), **_VOL)
for _n in (60, 240):
    _register(f"roll_spread_{_n}", "microstructure",
              f"Estimateur de spread de Roll sur {_n} barres : 2√max(−cov(r_t, r_t−1), 0), en fraction du prix.",
              partial(_roll_spread, n=_n))
_register("cs_spread_60", "microstructure",
          "Estimateur de spread de Corwin-Schultz (hauts et bas de 2 barres), moyenné sur 60 barres, en fraction du prix.",
          partial(_cs_spread, n=60))
_register("zero_ret_frac_60", "microstructure",
          "Part des barres à rendement nul sur 60 barres (illiquidité, pas de cotation).",
          partial(_zero_ret_frac, n=60))

# --- statistique ---------------------------------------------------------------
for _n in (20, 60, 240):
    _register(f"zscore_{_n}", "statistique",
              f"Z-score du close par rapport à sa moyenne et son écart-type sur {_n} barres.",
              partial(_price_zscore, n=_n))
for _n in (60, 240):
    _register(f"acf1_{_n}", "statistique",
              f"Autocorrélation roulante des rendements au retard 1 sur {_n} barres.",
              partial(_acf1, n=_n))
_register("vr_5_240", "statistique",
          "Ratio de variance de Lo-MacKinlay (q = 5) sur 240 barres : < 1 retournement, > 1 tendance.",
          partial(_variance_ratio, q=5, n=240))
for _n in (60, 240):
    _register(f"skew_{_n}", "statistique",
              f"Asymétrie (skewness) roulante des rendements sur {_n} barres.",
              partial(_skew, n=_n))
    _register(f"kurt_{_n}", "statistique",
              f"Kurtosis en excès roulante des rendements sur {_n} barres.",
              partial(_kurt, n=_n))

# --- calendrier ---------------------------------------------------------------
_register("hour_sin", "calendrier",
          "Heure UTC de la journée encodée sur un cercle (sinus, période 24 h).",
          _hour_sin)
_register("hour_cos", "calendrier",
          "Heure UTC de la journée encodée sur un cercle (cosinus, période 24 h).",
          _hour_cos)
_register("dow_sin", "calendrier",
          "Jour de la semaine UTC (avec l'heure) encodé sur un cercle (sinus, période 7 jours).",
          _dow_sin)
_register("dow_cos", "calendrier",
          "Jour de la semaine UTC (avec l'heure) encodé sur un cercle (cosinus, période 7 jours).",
          _dow_cos)
_register("is_weekend", "calendrier",
          "1 le samedi et le dimanche (UTC), 0 sinon.",
          _is_weekend)
_register("session_asia", "calendrier",
          "1 pendant la séance de Tokyo (09:00–15:00 heure de Tokyo, soit 00:00–06:00 UTC), du lundi au vendredi.",
          partial(_in_session, tz="Asia/Tokyo", start_min=9 * 60, end_min=15 * 60))
_register("session_europe", "calendrier",
          "1 pendant la séance de Londres (08:00–16:30 heure de Londres, heure d'été gérée), du lundi au vendredi.",
          partial(_in_session, tz="Europe/London", start_min=8 * 60, end_min=16 * 60 + 30))
_register("session_us", "calendrier",
          "1 pendant la séance de New York (09:30–16:00 heure de New York, 13:30–20:00 UTC en été), du lundi au vendredi, jours fériés ignorés.",
          partial(_in_session, tz="America/New_York", start_min=9 * 60 + 30, end_min=16 * 60))
_register("us_open_window", "calendrier",
          "1 dans la fenêtre de ±30 minutes autour de l'ouverture de New York (09:00–10:00 heure de New York), en semaine.",
          partial(_in_session, tz="America/New_York", start_min=9 * 60, end_min=10 * 60))
_register("min_since_funding", "calendrier",
          "Minutes écoulées depuis le dernier fixing de funding (00:00, 08:00, 16:00 UTC), dans [0, 480[.",
          _min_since_funding)
_register("min_to_funding", "calendrier",
          "Minutes restantes avant le prochain fixing de funding (00:00, 08:00, 16:00 UTC), dans ]0, 480].",
          _min_to_funding)
_register("ret_since_day_open", "calendrier",
          "Rendement logarithmique depuis l'ouverture de la journée UTC (00:00).",
          _ret_since_day_open)
_register("ret_since_day_open_vol", "calendrier",
          "Rendement depuis 00:00 UTC en unités de volatilité (σ60 × √nombre de barres écoulées).",
          _ret_since_day_open_vol)
_register("ret15_x_weekend", "calendrier",
          "Interaction : rendement 15 barres en σ × indicateur de week-end (retournement plus fort le week-end).",
          _ret_x_weekend)

# --- marché -------------------------------------------------------------------
_BENCH = {"needs_benchmark": True}
for _k in (1, 5, 15):
    _register(f"bench_ret_{_k}", "marche",
              f"Rendement logarithmique de la référence (BTC pour les altcoins) sur {_k} barres (avance-retard).",
              partial(_bench_ret, k=_k), **_BENCH)
for _k in (5, 15, 60):
    _register(f"rel_ret_{_k}", "marche",
              f"Rendement relatif à la référence sur {_k} barres : ln(C) − ln(référence), variation sur {_k} barres.",
              partial(_rel_ret, k=_k), **_BENCH)
_register("beta_bench_240", "marche",
          "Bêta roulant des rendements 1 barre par rapport à la référence sur 240 barres.",
          partial(_beta_bench, n=240), **_BENCH)
_register("corr_bench_240", "marche",
          "Corrélation roulante des rendements 1 barre avec la référence sur 240 barres.",
          partial(_corr_bench, n=240), **_BENCH)
_register("resid_ret_15", "marche",
          "Rendement idiosyncratique sur 15 barres : rendement − bêta 240 × rendement de la référence.",
          partial(_resid_ret, k=15, n=240), **_BENCH)

# --- dérivés ------------------------------------------------------------------
_register("funding_rate_bp", "derives",
          "Dernier taux de funding connu, en points de base par période de funding.",
          _funding_bp, needs_columns=("funding_rate",))
_register("funding_rate_z_30d", "derives",
          "Z-score du taux de funding sur 30 jours (dès 1 jour d'historique).",
          _funding_z, needs_columns=("funding_rate",))
for _k in (5, 15, 60):
    _register(f"oi_chg_{_k}", "derives",
              f"Variation de l'open interest sur {_k} barres, en %.",
              partial(_oi_chg, k=_k), needs_columns=("open_interest",))
_register("oi_z_1440", "derives",
          "Z-score de l'open interest sur 1 jour (1440 barres).",
          partial(_oi_z, n=Z_WINDOW), needs_columns=("open_interest",))
for _k in (15, 60):
    _register(f"oi_x_ret_{_k}", "derives",
              f"Interaction variation d'OI (%) × rendement en σ sur {_k} barres : > 0 si OI et prix vont dans le même sens (hausse + OI en hausse = nouvelles positions longues).",
              partial(_oi_x_ret, k=_k), needs_columns=("open_interest",))
for _col_name, _label in (
    ("ls_ratio_top", "ratio long/short des top traders (positions)"),
    ("ls_ratio_accounts", "ratio long/short de l'ensemble des comptes"),
    ("taker_ls_ratio", "ratio de volume taker acheteur/vendeur des futures"),
):
    _register(f"{_col_name}_log", "derives",
              f"Logarithme du {_label}.",
              partial(_log_col, col=_col_name), needs_columns=(_col_name,))
    _register(f"{_col_name}_z_1440", "derives",
              f"Z-score sur 1 jour du logarithme du {_label}.",
              partial(_log_col_z, col=_col_name, n=Z_WINDOW), needs_columns=(_col_name,))

del _n, _k, _f, _s, _g, _d, _a, _b, _col_name, _label


# =============================================================================
# API publique
# =============================================================================


def _has_data(df: pd.DataFrame, col: str) -> bool:
    if col not in df.columns:
        return False
    try:
        values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype="float64")
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(values).any())


def _has_volume(df: pd.DataFrame) -> bool:
    if not _has_data(df, "volume"):
        return False
    v = pd.to_numeric(df["volume"], errors="coerce").to_numpy(dtype="float64")
    return bool(np.nansum(np.abs(v)) > 0)


def _missing_requirement(spec: IndicatorSpec, df: pd.DataFrame, memo: dict) -> str | None:
    """Raison pour laquelle ``spec`` ne peut pas être calculé sur ``df`` (None si calculable)."""

    def check(key, fn):
        if key not in memo:
            memo[key] = fn()
        return memo[key]

    if spec.needs_volume and not check("__volume__", lambda: _has_volume(df)):
        return "volume absent, entièrement NaN ou nul"
    if spec.needs_benchmark and not check("bench_close", lambda: _has_data(df, "bench_close")):
        return "référence absente (colonne bench_close ou argument benchmark)"
    for col in spec.needs_columns:
        if not check(col, lambda c=col: _has_data(df, c)):
            return f"colonne {col} absente ou entièrement NaN"
    return None


def _align_benchmark(benchmark, index: pd.DatetimeIndex) -> pd.Series:
    """Close de la référence réindexé sur ``index``, report limité à BENCH_FFILL_LIMIT barres."""
    if isinstance(benchmark, pd.DataFrame):
        if "close" not in benchmark.columns:
            raise ValueError("benchmark : DataFrame sans colonne 'close'")
        benchmark = benchmark["close"]
    if not isinstance(benchmark, pd.Series) or not isinstance(benchmark.index, pd.DatetimeIndex):
        raise TypeError("benchmark doit être une pd.Series indexée par un DatetimeIndex")
    b = benchmark.astype("float64")
    b = b[~b.index.duplicated(keep="last")].sort_index()
    bidx = b.index
    if index.tz is not None:
        bidx = bidx.tz_localize("UTC") if bidx.tz is None else bidx
        bidx = bidx.tz_convert(index.tz)
    elif bidx.tz is not None:
        bidx = bidx.tz_convert("UTC").tz_localize(None)
    b = b.set_axis(bidx)
    return b.reindex(index).ffill(limit=BENCH_FFILL_LIMIT)


def _validate(df: pd.DataFrame) -> None:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df doit être un pd.DataFrame")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("df doit être indexé par un DatetimeIndex (horodatage d'ouverture des barres)")
    missing = [c for c in _BASE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"colonnes OHLC manquantes : {missing}")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("l'index doit être trié et sans doublon (voir data.clean_ohlcv)")


def compute_indicators(
    df: pd.DataFrame,
    names: Iterable[str] | str | None = None,
    benchmark: pd.Series | None = None,
) -> pd.DataFrame:
    """Calcule les indicateurs du registre sur ``df`` (format OHLCV du contrat).

    Paramètres
    ----------
    df : DataFrame indexé par un DatetimeIndex trié, sans doublon, avec au moins
        ``open, high, low, close`` ; colonnes optionnelles ``volume``,
        ``quote_volume``, ``trades``, ``taker_buy_volume``, ``session``,
        ``bench_close`` et les colonnes de dérivés.
    names : indicateurs à calculer (tous par défaut). Un nom inconnu lève ``KeyError``.
    benchmark : close de la référence (BTCUSDT pour les altcoins). Réindexé sur
        ``df.index`` avec report de la dernière valeur limité à 5 barres, puis
        utilisé comme colonne ``bench_close`` (remplace celle de ``df`` si présente).

    Retour
    ------
    DataFrame float64 indexé comme ``df``, une colonne par indicateur calculé, ±inf
    remplacés par NaN. Les indicateurs dont les colonnes requises sont absentes ou
    entièrement NaN (ou dont le volume est nul partout) sont ignorés ; la raison est
    donnée dans ``result.attrs["skipped"]`` ({nom: raison}).
    """
    _validate(df)
    work = df
    if benchmark is not None:
        work = df.assign(bench_close=_align_benchmark(benchmark, df.index))

    if names is None:
        selected = list(REGISTRY)
    else:
        selected = list(dict.fromkeys([names] if isinstance(names, str) else names))
        unknown = [x for x in selected if x not in REGISTRY]
        if unknown:
            raise KeyError(f"indicateur(s) inconnu(s) : {unknown}")

    memo: dict = {}
    skipped: dict[str, str] = {}
    runnable: list[IndicatorSpec] = []
    for name in selected:
        spec = REGISTRY[name]
        reason = _missing_requirement(spec, work, memo)
        if reason is None:
            runnable.append(spec)
        else:
            skipped[name] = reason

    n_rows = len(work)
    out = np.empty((n_rows, len(runnable)), dtype="float64", order="F")
    token = _CACHE.set((work, {}))
    try:
        with np.errstate(all="ignore"):
            for j, spec in enumerate(runnable):
                try:
                    values = spec.func(work)
                except Exception as exc:  # contexte explicite pour le débogage
                    raise RuntimeError(f"échec du calcul de l'indicateur {spec.name!r}") from exc
                arr = np.asarray(values, dtype="float64")
                if arr.shape != (n_rows,):
                    raise ValueError(f"{spec.name} : forme {arr.shape} au lieu de ({n_rows},)")
                col = out[:, j]
                col[:] = arr
                col[~np.isfinite(col)] = np.nan
    finally:
        _CACHE.reset(token)

    result = pd.DataFrame(out, index=df.index, columns=[s.name for s in runnable], copy=False)
    result.attrs["skipped"] = skipped
    return result


def list_indicators() -> pd.DataFrame:
    """Catalogue du registre : name, family, description, needs_volume, needs_benchmark, needs_columns."""
    rows = [
        {
            "name": s.name,
            "family": s.family,
            "description": s.description,
            "needs_volume": s.needs_volume,
            "needs_benchmark": s.needs_benchmark,
            "needs_columns": ", ".join(s.needs_columns),
        }
        for s in REGISTRY.values()
    ]
    return pd.DataFrame(
        rows,
        columns=["name", "family", "description", "needs_volume", "needs_benchmark", "needs_columns"],
    )
