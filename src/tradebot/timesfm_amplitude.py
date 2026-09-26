"""À quoi sert l'amplitude prévue par TimesFM ? Fonctions réutilisables (hors-ligne).

TimesFM 3.0 ne prédit pas le sens (AUC ≈ 0,51, voir ``reports/timesfm``), mais ses
déciles sont calibrés en amplitude. Ce module fournit les briques pour trois tests
(exécution complète : ``scripts/timesfm_amplitude.py``) :

1. **prévision de volatilité** à h barres : σ_TimesFM(h) contre des estimateurs de
   volatilité réalisée (EWMA, écart-type roulant, Parkinson, Garman-Klass, moyenne
   historique) sur les cibles ``r_h²`` et ``|r_h|`` (QLIKE, MSE, MAE, Mincer-Zarnowitz,
   couverture) et régression d'encompassing ;
2. **juste valeur en cours de fenêtre** Polymarket : ``P(Up) = Φ(d / σ_restant)`` avec
   ``d = log(prix Binance à S+k / TWAP60(S))`` et ``σ_restant = σ_par_minute · √(τ − 0,5)``
   (τ = minutes restantes ; le TWAP60 final ≈ prix à E − 30 s) ;
3. **régime** : quintiles d'amplitude prévue (bornes choisies sur la période d'apprentissage).

Conventions
-----------
* Une **origine** ``t`` est l'horodatage d'ouverture de la dernière barre 1m du contexte
  (sa clôture est la dernière valeur connue). Le contexte = ``context_len`` barres
  consécutives se terminant en ``t`` ; le pas ``h`` de la prévision vise ``close[t + h]``.
  Rien après ``t`` n'entre dans le contexte (:func:`context_windows` ; test unitaire).
* Pour un marché Polymarket ouvrant à ``S``, l'origine « prévu à S » est ``t = S − 120 s``
  (barre close à ``S − 60 s``, même information que le backtest à ``S − 30 s``) ; l'horizon
  jusqu'à la clôture ``E = S + D`` vaut ``D/60 + 1`` pas.
* ``σ_TimesFM(h) = (q90 − q10) / 2,5631`` (échelle gaussienne), en **points de base de
  log-prix** ; variante robuste ``(q75 − q25) / 1,349`` avec q25/q75 interpolés entre les
  déciles (:func:`sigma_from_quantiles`).
* Toutes les volatilités réalisées sont **causales** : la valeur à la barre ``t`` n'utilise
  que les barres ``<= t`` (:func:`realized_vol_estimators`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .forecaster import QUANTILE_LEVELS

__all__ = [
    "Z_Q10_Q90", "Z_IQR", "BP", "R2_FLOOR_BP2",
    "sigma_from_quantiles", "sigma_per_minute", "sigma_remaining", "prob_up_gaussian",
    "context_windows", "forecast_quantiles", "quantile_frame",
    "realized_vol_estimators", "ewma_variance", "choose_ewma_lambda", "future_log_return",
    "realized_variance_h", "qlike", "mse", "mae_abs", "mincer_zarnowitz", "coverage",
    "encompassing_fit", "encompassing_predict", "qlike_combination_fit", "brier", "log_loss", "accuracy",
    "quintile_edges", "quintile_of", "train_ecdf", "polymarket_origin",
    "market_price_at", "slot_of",
]

# (q0.9 − q0.1) d'une loi normale réduite = 2 × 1,28155 ; (q0.75 − q0.25) = 2 × 0,67449.
Z_Q10_Q90 = 2.5631031
Z_IQR = 1.3489795
BP = 1e4                      # log-rendement -> points de base
R2_FLOOR_BP2 = 0.1 ** 2       # plancher de r_h² dans QLIKE : (0,1 pb)² (r_h = 0 exact sinon)
_LEVELS = np.asarray(QUANTILE_LEVELS)
_QCOLS = tuple(f"q{int(round(l * 100))}" for l in QUANTILE_LEVELS)   # q10 … q90


# ---------------------------------------------------------------------------
# σ depuis les quantiles ; Φ(d/σ)
# ---------------------------------------------------------------------------
def _interp_quantile(q: np.ndarray, level: float, levels: np.ndarray = _LEVELS) -> np.ndarray:
    """Quantile de niveau ``level`` interpolé linéairement entre les niveaux connus (n, k)."""
    j = int(np.searchsorted(levels, level, side="right")) - 1
    j = min(max(j, 0), len(levels) - 2)
    w = (level - levels[j]) / (levels[j + 1] - levels[j])
    return q[:, j] + w * (q[:, j + 1] - q[:, j])


def sigma_from_quantiles(quantiles_h, method: str = "q10q90",
                         levels: Sequence[float] = QUANTILE_LEVELS, scale: float = 1.0) -> np.ndarray:
    """Écart-type prévu depuis les quantiles ``(n, 9)`` d'un même horizon, dans **l'unité des
    quantiles** (pb si ils viennent de :func:`forecast_quantiles` ; log-prix sinon, avec
    ``scale=BP`` pour obtenir des pb). Seuls les écarts entre quantiles comptent.

    * ``"q10q90"`` : ``(q90 − q10) / 2,5631`` — échelle gaussienne, la référence ;
    * ``"iqr"`` : ``(q75 − q25) / 1,349`` — plus robuste aux queues ; q25 et q75 sont
      interpolés linéairement entre les déciles 20/30 et 70/80.

    Les quantiles sont triés ligne par ligne ; NaN -> NaN ; résultat ``>= 0``.
    """
    q = np.asarray(quantiles_h, dtype="float64")
    single = q.ndim == 1
    if single:
        q = q[None, :]
    lv = np.asarray(levels, dtype="float64")
    if q.ndim != 2 or q.shape[1] != lv.size:
        raise ValueError(f"quantiles attendus en (n, {lv.size}), forme reçue {q.shape}")
    bad = ~np.isfinite(q).all(axis=1)
    qs = np.sort(np.where(np.isfinite(q), q, 0.0), axis=1)
    if method == "q10q90":
        lo, hi = _interp_quantile(qs, 0.1, lv), _interp_quantile(qs, 0.9, lv)
        z = Z_Q10_Q90
    elif method == "iqr":
        lo, hi = _interp_quantile(qs, 0.25, lv), _interp_quantile(qs, 0.75, lv)
        z = Z_IQR
    else:
        raise ValueError(f"méthode inconnue : {method!r} (attendu : 'q10q90' ou 'iqr')")
    s = np.clip((hi - lo) / z, 0.0, None) * float(scale)
    s[bad] = np.nan
    return s[0] if single else s


def sigma_per_minute(sigma_h, h) -> np.ndarray:
    """σ par minute à partir de σ à ``h`` barres (loi en √h : variance linéaire en h)."""
    return np.asarray(sigma_h, dtype="float64") / np.sqrt(np.asarray(h, dtype="float64"))


def sigma_remaining(sigma_per_min, tau_min, twap_shift_min: float = 0.5) -> np.ndarray:
    """σ de ``log(TWAP60(E) / prix à S+k)`` : ``σ_par_minute · √(τ − 0,5)``.

    ``τ`` = minutes restantes jusqu'à ``E`` ; le TWAP60 final vaut ≈ le prix à ``E − 30 s``,
    d'où la demi-minute retirée. ``τ − 0,5 <= 0`` -> NaN.
    """
    s = np.asarray(sigma_per_min, dtype="float64")
    tau = np.asarray(tau_min, dtype="float64") - twap_shift_min
    with np.errstate(invalid="ignore"):
        return np.where(tau > 0, s * np.sqrt(np.clip(tau, 0.0, None)), np.nan)


def prob_up_gaussian(d_bp, sigma_bp, clip: tuple[float, float] = (0.01, 0.99)) -> np.ndarray:
    """``P(Up) = Φ(d / σ)`` : d et σ en points de base (mêmes unités). σ = 0 -> 0,5 si d = 0,
    sinon 0/1 (bornée à ``clip``) ; NaN si d ou σ n'est pas fini ou σ < 0."""
    d = np.asarray(d_bp, dtype="float64")
    s = np.asarray(sigma_bp, dtype="float64")
    d, s = np.broadcast_arrays(d, s)
    out = np.full(d.shape, np.nan)
    ok = np.isfinite(d) & np.isfinite(s) & (s >= 0)
    pos = ok & (s > 0)
    out[pos] = ndtr(d[pos] / s[pos])
    zero = ok & (s == 0)
    out[zero] = np.where(d[zero] > 0, 1.0, np.where(d[zero] < 0, 0.0, 0.5))
    out[ok] = np.clip(out[ok], clip[0], clip[1])
    return out


# ---------------------------------------------------------------------------
# Contextes et prévision (rien après l'origine)
# ---------------------------------------------------------------------------
def context_windows(close: pd.Series, origins_s: np.ndarray, context_len: int,
                    bar_s: int = 60, max_gap_bars: int = 3) -> tuple[list[np.ndarray], np.ndarray]:
    """Fenêtres de ``context_len`` closes consécutifs (grille régulière) se terminant à
    chaque origine (ouverture de barre, s Unix).

    ``close`` : Series indexée par l'ouverture de barre (UTC). Une origine est **écartée**
    (``ok=False``) si elle n'est pas dans les données, si l'historique est trop court, si
    la fenêtre contient un trou de plus de ``max_gap_bars`` barres consécutives, ou si la
    dernière valeur manque ; les petits trous sont interpolés linéairement. Aucune barre
    postérieure à l'origine n'est lue. Retour : ``(fenêtres (prix), ok)``.
    """
    if context_len < 2:
        raise ValueError("context_len doit être >= 2")
    s = close.dropna().sort_index()
    ts = ((pd.DatetimeIndex(s.index).tz_convert("UTC") - pd.Timestamp(0, tz="UTC"))
          // pd.Timedelta("1s")).to_numpy().astype("int64")
    vals = s.to_numpy(dtype="float64")
    O = np.asarray(origins_s, dtype="int64")
    out: list[np.ndarray] = []
    ok = np.zeros(len(O), dtype=bool)
    for i, o in enumerate(O):
        p = int(np.searchsorted(ts, o))
        if p >= len(ts) or ts[p] != o:
            continue
        t0 = o - bar_s * (context_len - 1)
        p0 = int(np.searchsorted(ts, t0))
        seg_ts, seg = ts[p0:p + 1], vals[p0:p + 1]
        if seg_ts.size == 0 or seg_ts[0] < t0:   # impossible par construction, sûreté
            continue
        grid = np.full(context_len, np.nan)
        idx = (seg_ts - t0) // bar_s
        grid[idx] = seg
        finite = np.isfinite(grid)
        # première et dernière barres obligatoires ; trous internes <= max_gap_bars, interpolés
        if not finite[0] or not finite[-1] or _max_nan_run(finite) > max_gap_bars:
            continue
        pos = np.flatnonzero(finite)
        grid = np.interp(np.arange(context_len), pos, grid[pos])
        out.append(grid)
        ok[i] = True
    return out, ok


def _max_nan_run(finite: np.ndarray) -> int:
    """Longueur du plus long bloc de NaN consécutifs (0 si aucun)."""
    f = np.asarray(finite, dtype=bool)
    if f.all():
        return 0
    # positions des valeurs finies encadrées par des sentinelles : écarts − 1 = longueurs des trous
    pos = np.r_[-1, np.flatnonzero(f), f.size]
    return int((np.diff(pos) - 1).max())


def forecast_quantiles(forecaster, close: pd.Series, origins_s: np.ndarray, horizon: int,
                       context_len: int = 512, chunk: int = 128, context_transform: str = "logprice",
                       progress=None, max_gap_bars: int = 3) -> pd.DataFrame:
    """Déciles TimesFM en **log-rendement (pb)** depuis la dernière valeur, pour chaque origine
    et chaque pas ``1..horizon``.

    ``forecaster`` : :class:`tradebot.forecaster.TimesFMForecaster` (ou tout objet avec
    ``forecast(contexts, horizon) -> ForecastBatch``). Colonnes : ``origin_s``, ``h``,
    ``q10`` … ``q90`` (pb), ``last_close``. Les origines écartées (trou, historique court) sont
    absentes. ``progress(n_done, n_total)`` optionnel.
    """
    from .forecaster import to_log_return, transform_context

    ctx, ok = context_windows(close, origins_s, context_len, max_gap_bars=max_gap_bars)
    O = np.asarray(origins_s, dtype="int64")[ok]
    frames = []
    done = 0
    for c0 in range(0, len(ctx), chunk):
        block = [transform_context(c, context_transform) for c in ctx[c0:c0 + chunk]]
        fb = forecaster.forecast(block, horizon)
        q = fb.quantiles                                    # (n, horizon, 9), espace transformé
        lr = to_log_return(q, fb.last_value[:, None, None], context_transform) * BP
        n, H, K = lr.shape
        df = pd.DataFrame(lr.reshape(n * H, K), columns=list(_QCOLS))
        df.insert(0, "h", np.tile(np.arange(1, H + 1), n))
        df.insert(0, "origin_s", np.repeat(O[c0:c0 + chunk], H))
        df["last_close"] = np.repeat(np.asarray([c[-1] for c in ctx[c0:c0 + chunk]]), H)
        frames.append(df)
        done += n
        if progress is not None:
            progress(done, len(ctx))
    if not frames:
        return pd.DataFrame(columns=["origin_s", "h", *_QCOLS, "last_close"])
    return pd.concat(frames, ignore_index=True)


def quantile_frame(qdf: pd.DataFrame, h: int) -> tuple[np.ndarray, np.ndarray]:
    """``(origin_s, quantiles (n, 9) en pb)`` du pas ``h`` d'un résultat de
    :func:`forecast_quantiles`."""
    sub = qdf[qdf["h"] == int(h)]
    return sub["origin_s"].to_numpy(dtype="int64"), sub[list(_QCOLS)].to_numpy(dtype="float64")


# ---------------------------------------------------------------------------
# Volatilité réalisée (causale, par barre)
# ---------------------------------------------------------------------------
def ewma_variance(r2: np.ndarray, lam: float) -> np.ndarray:
    """EWMA causale de ``r²`` : ``v_t = λ v_{t−1} + (1 − λ) r_t²`` (NaN de ``r²`` ignorés :
    ``v`` reporté). ``v_t`` utilise ``r_t`` (barre close en ``t``) et les barres antérieures."""
    if not 0.0 < lam < 1.0:
        raise ValueError("lambda dans ]0, 1[ attendu")
    x = np.asarray(r2, dtype="float64")
    out = np.full(x.shape, np.nan)
    v = np.nan
    for i, xi in enumerate(x):
        if np.isfinite(xi):
            v = xi if not np.isfinite(v) else lam * v + (1.0 - lam) * xi
        out[i] = v
    return out


def realized_vol_estimators(bars: pd.DataFrame, lam: float = 0.97,
                            windows: Sequence[int] = (60, 240), range_window: int = 60) -> pd.DataFrame:
    """Variances **par minute** (log-prix, pb²) estimées à chaque barre avec les barres
    ``<= t`` : ``ewma`` (λ), ``rstd{w}`` (variance des rendements 1m sur ``w`` barres),
    ``park{rw}`` (Parkinson), ``gk{rw}`` (Garman-Klass) ; plus ``r2`` (rendement 1m au carré,
    pb²) et ``ret`` (pb). Index = ouverture de barre. Barres absentes de la grille : pas de
    report (les fenêtres roulantes comptent les barres présentes, ``min_periods`` = 80 %)."""
    b = bars.sort_index()
    c = b["close"].to_numpy(dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.r_[np.nan, np.diff(np.log(c))] * BP
        lhl = np.log(b["high"].to_numpy(dtype="float64") / b["low"].to_numpy(dtype="float64")) * BP
        lco = np.log(c / b["open"].to_numpy(dtype="float64")) * BP
    # un rendement enjambant un trou de grille n'est pas un rendement 1 min
    idx = pd.DatetimeIndex(b.index)
    step = (idx[1:] - idx[:-1]) == pd.Timedelta("1min")
    r[1:][~step] = np.nan
    r2 = r * r
    out = pd.DataFrame(index=b.index)
    out["ret"] = r
    out["r2"] = r2
    out["ewma"] = ewma_variance(r2, lam)
    s_r = pd.Series(r, index=b.index)
    for w in windows:
        out[f"rstd{w}"] = s_r.rolling(w, min_periods=int(0.8 * w)).var(ddof=0).to_numpy()
    rw = int(range_window)
    park = pd.Series(lhl * lhl / (4.0 * math.log(2.0)), index=b.index)
    gk = pd.Series(0.5 * lhl * lhl - (2.0 * math.log(2.0) - 1.0) * lco * lco, index=b.index)
    out[f"park{rw}"] = park.rolling(rw, min_periods=int(0.8 * rw)).mean().to_numpy()
    out[f"gk{rw}"] = gk.rolling(rw, min_periods=int(0.8 * rw)).mean().clip(lower=0.0).to_numpy()
    return out


def choose_ewma_lambda(r2_1m: np.ndarray, targets: dict[int, tuple[np.ndarray, np.ndarray]],
                       grid: Sequence[float] = (0.90, 0.94, 0.96, 0.97, 0.98, 0.99, 0.995)) -> float:
    """λ qui minimise la QLIKE moyenne (tous horizons) sur l'apprentissage.

    ``targets[h] = (positions des origines dans r2_1m, r_h² réalisé (pb²))`` ; la prévision
    à ``h`` est ``h × EWMA_λ(t)``. Les origines doivent être dans l'apprentissage seulement.
    """
    best, best_q = None, math.inf
    for lam in grid:
        v = ewma_variance(r2_1m, lam)
        tot, n = 0.0, 0
        for h, (pos, y2) in targets.items():
            s2 = h * v[np.asarray(pos, dtype="int64")]
            q = qlike(y2, s2)
            ok = np.isfinite(q)
            tot += float(q[ok].sum())
            n += int(ok.sum())
        m = tot / n if n else math.inf
        if m < best_q:
            best, best_q = lam, m
    if best is None:
        raise ValueError("aucun λ évaluable")
    return float(best)


def future_log_return(close: pd.Series, origins_s: np.ndarray, h: int, bar_s: int = 60) -> np.ndarray:
    """``log(close[t + h] / close[t])`` en pb (NaN si l'une des deux barres manque)."""
    s = close.dropna().sort_index()
    ts = ((pd.DatetimeIndex(s.index).tz_convert("UTC") - pd.Timestamp(0, tz="UTC"))
          // pd.Timedelta("1s")).to_numpy().astype("int64")
    v = s.to_numpy(dtype="float64")
    O = np.asarray(origins_s, dtype="int64")
    out = np.full(len(O), np.nan)
    p0 = np.searchsorted(ts, O)
    p1 = np.searchsorted(ts, O + h * bar_s)
    ok0 = (p0 < len(ts)) & (ts[np.minimum(p0, len(ts) - 1)] == O)
    ok1 = (p1 < len(ts)) & (ts[np.minimum(p1, len(ts) - 1)] == O + h * bar_s)
    ok = ok0 & ok1
    out[ok] = np.log(v[p1[ok]] / v[p0[ok]]) * BP
    return out


def realized_variance_h(r2_1m: pd.Series, origins_s: np.ndarray, h: int, bar_s: int = 60) -> np.ndarray:
    """Variance réalisée ``Σ r²_1m`` des ``h`` barres qui suivent l'origine (barres ouvertes
    en ``t + 60 … t + h·60``), pb² ; NaN si une barre manque."""
    s = r2_1m.sort_index()
    ts = ((pd.DatetimeIndex(s.index).tz_convert("UTC") - pd.Timestamp(0, tz="UTC"))
          // pd.Timedelta("1s")).to_numpy().astype("int64")
    v = s.to_numpy(dtype="float64")
    O = np.asarray(origins_s, dtype="int64")
    out = np.full(len(O), np.nan)
    for i, o in enumerate(O):
        p = int(np.searchsorted(ts, o + bar_s))
        q = p + h
        if q > len(ts) or ts[p] != o + bar_s or ts[q - 1] != o + h * bar_s:
            continue
        seg = v[p:q]
        if np.isfinite(seg).all():
            out[i] = float(seg.sum())
    return out


# ---------------------------------------------------------------------------
# Scores de volatilité
# ---------------------------------------------------------------------------
def qlike(r2, s2, floor: float = R2_FLOOR_BP2) -> np.ndarray:
    """QLIKE ``r²/σ² − log(r²/σ²) − 1`` (>= 0, minimum en σ² = r²) ; ``r²`` plancheré à
    ``floor`` (pb²) car ``r_h = 0`` arrive ; NaN si σ² <= 0 ou non fini."""
    y = np.clip(np.asarray(r2, dtype="float64"), floor, None)
    s = np.asarray(s2, dtype="float64")
    y, s = np.broadcast_arrays(y, s)
    out = np.full(y.shape, np.nan)
    ok = np.isfinite(y) & np.isfinite(s) & (s > 0)
    x = y[ok] / s[ok]
    out[ok] = x - np.log(x) - 1.0
    return out


def mse(r2, s2) -> np.ndarray:
    y = np.asarray(r2, dtype="float64")
    s = np.asarray(s2, dtype="float64")
    return (y - s) ** 2


def mae_abs(r_abs, sigma) -> np.ndarray:
    """|r| contre l'espérance gaussienne ``σ √(2/π)``."""
    y = np.asarray(r_abs, dtype="float64")
    s = np.asarray(sigma, dtype="float64")
    return np.abs(y - s * math.sqrt(2.0 / math.pi))


def mincer_zarnowitz(r2, s2) -> dict:
    """Régression ``r² = a + b σ²`` (MCO) : ``a``, ``b``, ``r2`` (R²), ``n`` ; NaN si < 10 points."""
    y = np.asarray(r2, dtype="float64")
    x = np.asarray(s2, dtype="float64")
    ok = np.isfinite(y) & np.isfinite(x)
    if ok.sum() < 10 or np.nanstd(x[ok]) == 0:
        return {"a": math.nan, "b": math.nan, "r2": math.nan, "n": int(ok.sum())}
    X = np.c_[np.ones(ok.sum()), x[ok]]
    beta, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
    resid = y[ok] - X @ beta
    sst = float(((y[ok] - y[ok].mean()) ** 2).sum())
    return {"a": float(beta[0]), "b": float(beta[1]),
            "r2": float(1.0 - (resid ** 2).sum() / sst) if sst > 0 else math.nan, "n": int(ok.sum())}


def coverage(r, lo, hi) -> np.ndarray:
    """1 si ``lo <= r <= hi``, 0 sinon, NaN si une borne manque."""
    y = np.asarray(r, dtype="float64")
    a = np.asarray(lo, dtype="float64")
    b = np.asarray(hi, dtype="float64")
    y, a, b = np.broadcast_arrays(y, a, b)
    out = np.full(y.shape, np.nan)
    ok = np.isfinite(y) & np.isfinite(a) & np.isfinite(b)
    out[ok] = ((y[ok] >= a[ok]) & (y[ok] <= b[ok])).astype("float64")
    return out


def encompassing_fit(r2, s2_list: Sequence[np.ndarray]) -> dict:
    """MCO ``r² = a + Σ b_i σ²_i`` sur l'apprentissage ; erreurs-types robustes (HC1).
    Retour : ``coef`` (a, b_1…), ``se``, ``t``, ``n``."""
    y = np.asarray(r2, dtype="float64")
    X = np.c_[[np.asarray(s, dtype="float64") for s in s2_list]].T
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    Xo = np.c_[np.ones(ok.sum()), X[ok]]
    yo = y[ok]
    beta, *_ = np.linalg.lstsq(Xo, yo, rcond=None)
    e = yo - Xo @ beta
    XtX_inv = np.linalg.pinv(Xo.T @ Xo)
    meat = (Xo * (e[:, None] ** 2)).T @ Xo
    n, k = Xo.shape
    cov = XtX_inv @ meat @ XtX_inv * (n / max(n - k, 1))
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    with np.errstate(invalid="ignore", divide="ignore"):
        t = beta / se
    return {"coef": beta, "se": se, "t": t, "n": int(n)}


def encompassing_predict(coef, s2_list: Sequence[np.ndarray], floor_frac: float = 0.05) -> np.ndarray:
    """Variance combinée ``a + Σ b_i σ²_i``, plancherée à ``floor_frac`` × la moyenne des
    σ²_i (une combinaison MCO peut devenir négative)."""
    X = np.c_[[np.asarray(s, dtype="float64") for s in s2_list]].T
    pred = coef[0] + X @ np.asarray(coef[1:], dtype="float64")
    base = np.nanmean(X, axis=1)
    return np.maximum(pred, floor_frac * base)


def qlike_combination_fit(r2, s2_list: Sequence[np.ndarray], floor: float = R2_FLOOR_BP2) -> np.ndarray:
    """Poids ``w_i >= 0`` (sans constante) de ``σ² = Σ w_i σ²_i`` qui minimisent la QLIKE
    moyenne sur l'apprentissage — combinaison **multiplicative**, robuste aux valeurs extrêmes
    de r² contrairement aux MCO de :func:`encompassing_fit`. Optimisation de Nelder-Mead sur
    ``log w`` (départ : poids égaux)."""
    from scipy.optimize import minimize

    y = np.asarray(r2, dtype="float64")
    X = np.c_[[np.asarray(s, dtype="float64") for s in s2_list]].T
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1) & (X > 0).all(axis=1)
    y, X = y[ok], X[ok]
    k = X.shape[1]
    if y.size < 10:
        raise ValueError("au moins 10 observations complètes sont nécessaires")

    def loss(logw):
        s2 = X @ np.exp(logw)
        return float(np.mean(qlike(y, s2, floor)))

    res = minimize(loss, np.full(k, -math.log(k)), method="Nelder-Mead",
                   options={"xatol": 1e-4, "fatol": 1e-8, "maxiter": 2000})
    return np.exp(res.x)


# ---------------------------------------------------------------------------
# Scores de probabilité (Polymarket)
# ---------------------------------------------------------------------------
def brier(y, p) -> np.ndarray:
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    return (p - y) ** 2


def log_loss(y, p, eps: float = 1e-6) -> np.ndarray:
    y = np.asarray(y, dtype="float64")
    p = np.clip(np.asarray(p, dtype="float64"), eps, 1.0 - eps)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


def accuracy(y, p) -> np.ndarray:
    """1 si ``(p >= 0,5) == y`` ; NaN si p ou y manque."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    out = np.full(np.broadcast(y, p).shape, np.nan)
    ok = np.isfinite(y) & np.isfinite(p)
    out[ok] = ((p[ok] >= 0.5) == (y[ok] >= 0.5)).astype("float64")
    return out


# ---------------------------------------------------------------------------
# Régime : quintiles d'amplitude
# ---------------------------------------------------------------------------
def quintile_edges(x_train, n: int = 5) -> np.ndarray:
    """Bornes intérieures (``n − 1`` quantiles) calculées sur l'apprentissage seulement."""
    x = np.asarray(x_train, dtype="float64")
    x = x[np.isfinite(x)]
    if x.size < n:
        raise ValueError("pas assez de valeurs pour des quantiles")
    return np.quantile(x, np.arange(1, n) / n)


def quintile_of(x, edges) -> np.ndarray:
    """Numéro de quintile 1..n (``edges`` de :func:`quintile_edges`) ; NaN -> −1."""
    x = np.asarray(x, dtype="float64")
    out = np.where(np.isfinite(x), np.searchsorted(np.asarray(edges), x, side="right") + 1, -1)
    return out.astype("int64")


def train_ecdf(x_train, x) -> np.ndarray:
    """Rang (0..1) de ``x`` dans la distribution d'apprentissage (fonction de répartition
    empirique) : « amplitude_quantile_train »."""
    a = np.sort(np.asarray(x_train, dtype="float64"))
    a = a[np.isfinite(a)]
    v = np.asarray(x, dtype="float64")
    out = np.full(v.shape, np.nan)
    ok = np.isfinite(v)
    out[ok] = np.searchsorted(a, v[ok], side="right") / max(a.size, 1)
    return out


# ---------------------------------------------------------------------------
# Polymarket : origines, prix du marché en cours de fenêtre
# ---------------------------------------------------------------------------
def polymarket_origin(start_s, offset_s: int = 120) -> np.ndarray:
    """Origine TimesFM « prévu à S » : ``S − 120 s`` (barre 1m close à ``S − 60 s``)."""
    return np.asarray(start_s, dtype="int64") - int(offset_s)


def market_price_at(hist: pd.Series, ts_s: int, staleness_s: int = 90) -> tuple[float, float]:
    """(prix, ancienneté s) du dernier point ``<= ts_s`` de l'historique (index UTC) ; NaN si
    aucun point ou si le dernier a plus de ``staleness_s`` secondes."""
    if hist is None or len(hist) == 0:
        return math.nan, math.nan
    idx = pd.DatetimeIndex(hist.index)
    t = pd.Timestamp(int(ts_s), unit="s", tz="UTC")
    i = idx.searchsorted(t, side="right") - 1
    if i < 0:
        return math.nan, math.nan
    age = (t - idx[i]).total_seconds()
    if age > staleness_s:
        return math.nan, age
    return float(hist.iloc[i]), float(age)


def slot_of(ts_s, slot_s: int = 900) -> np.ndarray:
    """Créneau de 15 min (grappe du bootstrap) d'un horodatage en secondes."""
    return np.asarray(ts_s, dtype="int64") // int(slot_s)


@dataclass
class VolScores:
    """Scores agrégés d'un modèle de volatilité (test hors échantillon)."""
    model: str
    h: int
    n: int
    qlike: float
    mse: float
    mae: float
    mz_a: float
    mz_b: float
    mz_r2: float
    coverage_80: float
