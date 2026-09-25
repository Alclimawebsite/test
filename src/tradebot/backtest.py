"""Walk-forward TimesFM contre baselines : métriques de direction et mini-stratégie avec frais.

* :func:`walk_forward_timesfm` : une origine toutes les ``stride`` barres ; à l'origine
  ``t`` (clôture de la barre ``t``), TimesFM reçoit les ``context_len`` dernières
  clôtures **jusqu'à t inclus** (et les covariables jusqu'à t inclus), prévoit
  ``max(horizons)`` pas en un seul appel et on lit les pas ``h - 1``. Aucune donnée
  postérieure à ``t`` n'entre dans la prévision (testé). La cible est
  ``targets.future_log_return`` (NaN si trou dans les données).
* :func:`baseline_predictions` : ``momentum_h``, ``reversal_h`` (le vrai rival à
  5–15 min), ``always_up``, ``majority_prev_day`` sur exactement les mêmes origines.
* :func:`direction_metrics` : accuracy, balanced accuracy, AUC, Brier, log-loss,
  binomial sur ``n_eff = n / h``, Pesaran-Timmermann (1992) codé à la main.
  Origines espacées de ``stride`` barres : passer ``h_eff = ceil(h / stride)``
  (``docs/research/methodologie.md`` § 2.4), ce que fait :func:`metrics_by_horizon`.
* :func:`toy_strategy` : trades non chevauchants long/short au-delà d'un seuil de
  probabilité, rendement brut et net de frais en points de base.

Rappel (``docs/research/timesfm.md``, ``methodologie.md``) : quelques centaines
d'origines ne détectent rien sous ~57 % d'accuracy ; la P(hausse) brute de TimesFM est
trop confiante (la recalibrer avant d'utiliser un seuil de 0,55) ; TimesFM 3.0 est sous
licence non commerciale (recherche uniquement).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable

import numpy as np
import pandas as pd
from scipy import stats

from .forecaster import to_log_return, transform_context
from .targets import bar_duration, direction_label, future_log_return

log = logging.getLogger(__name__)

# Probabilité attribuée par les baselines binaires : 0,5 ± 0,02 (log-loss et Brier
# comparables à ceux d'un signal faible ; AUC = balanced accuracy pour un score binaire).
BASELINE_P = 0.52
BASELINES: tuple[str, ...] = ("momentum_h", "reversal_h", "always_up", "majority_prev_day")
PRED_COLUMNS: tuple[str, ...] = ("time", "horizon", "last_close", "pred_median", "p_up", "ret",
                                 "y_true", "pred_ret", "q10_ret", "q90_ret")


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _to_ts(x, tz) -> pd.Timestamp | None:
    if x is None:
        return None
    ts = pd.Timestamp(x)
    if ts.tzinfo is None and tz is not None:
        ts = ts.tz_localize(tz)
    elif ts.tzinfo is not None and tz is not None:
        ts = ts.tz_convert(tz)
    return ts


def _nan_run_lengths(isnan: np.ndarray) -> np.ndarray:
    """Longueur de la plage de NaN qui se termine à chaque position (0 si valeur présente)."""
    idx = np.arange(isnan.size)
    last_ok = np.maximum.accumulate(np.where(~isnan, idx, -1))
    return np.where(isnan, idx - last_ok, 0)


def _clean_close(df: pd.DataFrame) -> pd.DataFrame:
    if "close" not in df.columns:
        raise ValueError("colonne 'close' absente")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("index DatetimeIndex attendu")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("index trié et sans doublon attendu")
    close = df["close"].astype(float)
    return close[np.isfinite(close.to_numpy())].to_frame("close")


def _empty_predictions() -> pd.DataFrame:
    out = pd.DataFrame({c: pd.Series(dtype=float) for c in PRED_COLUMNS})
    out["time"] = pd.Series(dtype="datetime64[ns, UTC]")
    out["horizon"] = pd.Series(dtype=int)
    return out


# ---------------------------------------------------------------------------
# Walk-forward TimesFM
# ---------------------------------------------------------------------------

def walk_forward_timesfm(df: pd.DataFrame, forecaster, horizons: Iterable[int] = (5, 10, 15),
                         context_len: int = 512, stride: int = 5,
                         covariates: pd.DataFrame | None = None, start=None, end=None,
                         max_origins: int | None = None, max_gap_bars: int = 3,
                         chunk_size: int | None = None) -> pd.DataFrame:
    """Prévisions TimesFM hors échantillon, une ligne par (origine, horizon).

    Origines : barres présentes dont l'horodatage est un multiple de ``stride`` barres
    (grille alignée sur l'horloge UTC : avec ``stride=15`` en 1 min, à :00, :15, :30,
    :45), entre ``start`` et ``end`` inclus, avec au moins ``context_len`` barres
    d'historique. Le contexte est pris sur la **grille régulière** des ``context_len``
    barres se terminant en ``t`` inclus : une origine dont le contexte contient un trou
    de plus de ``max_gap_bars`` barres consécutives est **écartée** (comptée dans
    ``attrs["n_excluded_gaps"]``) ; les petits trous sont interpolés linéairement.
    ``max_origins`` : sous-échantillonnage régulier des origines retenues (couvre toute
    la période, espacement >= ``stride``).

    ``covariates`` : DataFrame indexé comme ``df`` (indicateurs causaux, valeur en ``t``
    calculée sur les barres <= ``t``) ; fenêtre identique au contexte, **dernière colonne
    = barre t** ; centrage/réduction sur la fenêtre dans le forecaster.

    Colonnes : ``time`` (origine), ``horizon``, ``last_close`` (close[t]),
    ``pred_median`` (médiane prévue, en prix), ``p_up`` (P(close[t+h] > close[t]) brute,
    non recalibrée), ``ret`` (``log(close[t+h]/close[t])``, NaN si trou ou fin de
    données), ``y_true`` (1/0, NaN si égalité ou ret NaN), plus ``pred_ret``,
    ``q10_ret``, ``q90_ret`` (médiane et déciles extrêmes en log-rendement).
    """
    hs = tuple(sorted({int(h) for h in horizons}))
    if not hs or hs[0] < 1:
        raise ValueError("horizons >= 1 attendus")
    if context_len < 2:
        raise ValueError("context_len doit être >= 2")
    if stride < 1:
        raise ValueError("stride doit être >= 1")
    if max_gap_bars < 0:
        raise ValueError("max_gap_bars doit être >= 0")
    t_start = time.perf_counter()

    dfc = _clean_close(df)
    if len(dfc) < 2:
        raise ValueError("pas assez de barres")
    bar = bar_duration(dfc.index)
    tz = dfc.index.tz
    grid = pd.date_range(dfc.index[0], dfc.index[-1], freq=bar)
    if not dfc.index.isin(grid).all():
        raise ValueError("horodatages hors de la grille régulière (barres irrégulières)")
    close_grid = dfc["close"].reindex(grid).to_numpy(dtype=np.float64)
    present = np.isfinite(close_grid)
    max_run = (pd.Series(_nan_run_lengths(~present))
               .rolling(context_len, min_periods=1).max().to_numpy())

    pos = np.arange(grid.size)
    offset = int(grid[0].value // bar.value)
    cand = present & (pos >= context_len - 1) & ((pos + offset) % stride == 0)
    ts_start, ts_end = _to_ts(start, tz), _to_ts(end, tz)
    if ts_start is not None:
        cand &= grid >= ts_start
    if ts_end is not None:
        cand &= grid <= ts_end
    cand_pos = np.flatnonzero(cand)
    gap_bad = max_run[cand_pos] > max_gap_bars
    n_excluded = int(gap_bad.sum())
    origins = cand_pos[~gap_bad]
    if max_origins is not None and origins.size > max_origins:
        keep = np.unique(np.linspace(0, origins.size - 1, int(max_origins)).round().astype(int))
        origins = origins[keep]

    cfg = getattr(forecaster, "cfg", None)
    kind = getattr(cfg, "context_transform", "logprice")
    attrs = dict(n_candidates=int(cand_pos.size), n_excluded_gaps=n_excluded,
                 n_origins=int(origins.size), stride=int(stride), context_len=int(context_len),
                 horizons=hs, bar=bar, max_gap_bars=int(max_gap_bars), context_transform=kind,
                 backend=getattr(cfg, "backend", None),
                 covariates=None if covariates is None else list(pd.DataFrame(covariates).columns))
    log.info("walk-forward TimesFM : %d origines retenues sur %d candidates (%d écartées pour "
             "trou > %d barres), contexte %d, stride %d, horizons %s",
             origins.size, cand_pos.size, n_excluded, max_gap_bars, context_len, stride, hs)
    if origins.size == 0:
        out = _empty_predictions()
        out.attrs.update(attrs, elapsed_s=time.perf_counter() - t_start)
        log.warning("walk-forward TimesFM : aucune origine")
        return out

    cov_arr = None
    if covariates is not None:
        cov_df = pd.DataFrame(covariates)
        if cov_df.shape[1] == 0:
            raise ValueError("covariates sans colonne")
        cov_arr = cov_df.astype(float).reindex(grid).to_numpy(dtype=np.float64).T   # (k, n_grid)
        if not np.isfinite(cov_arr).any():
            raise ValueError("covariables sans aucune valeur alignée sur l'index de df")

    ret_by_h = {h: future_log_return(dfc, h, bar).reindex(grid[origins]).to_numpy(dtype=np.float64)
                for h in hs}
    horizon = hs[-1]
    batch = getattr(cfg, "batch_size", 32) or 32
    chunk = int(chunk_size) if chunk_size else max(32, 4 * int(batch))

    parts: list[dict[str, np.ndarray]] = []
    done = 0
    for c0 in range(0, origins.size, chunk):
        sel = origins[c0:c0 + chunk]
        ctxs = [transform_context(close_grid[p - context_len + 1:p + 1], kind) for p in sel]
        covs = None if cov_arr is None else [cov_arr[:, p - context_len + 1:p + 1] for p in sel]
        fb = forecaster.forecast(ctxs, horizon, covs)
        last_close = close_grid[sel]
        for h in hs:
            pred_ret = to_log_return(fb.median[:, h - 1], fb.last_value, kind)
            r = ret_by_h[h][c0:c0 + sel.size]
            parts.append(dict(
                time_pos=sel, horizon=np.full(sel.size, h), last_close=last_close,
                pred_median=last_close * np.exp(pred_ret), p_up=fb.prob_up(h), ret=r,
                pred_ret=pred_ret,
                q10_ret=to_log_return(fb.quantiles[:, h - 1, 0], fb.last_value, kind),
                q90_ret=to_log_return(fb.quantiles[:, h - 1, -1], fb.last_value, kind)))
        done += sel.size
        el = time.perf_counter() - t_start
        log.info("walk-forward TimesFM : %d/%d origines (%.0f %%), %.1f s écoulées, "
                 "reste ~%.0f s", done, origins.size, 100.0 * done / origins.size, el,
                 el / done * (origins.size - done))

    cat = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    ret = cat.pop("ret")
    out = pd.DataFrame({
        "time": grid[cat.pop("time_pos")],
        "horizon": cat.pop("horizon").astype(int),
        "ret": ret,
        "y_true": direction_label(pd.Series(ret)).to_numpy(),
        **cat,
    })
    out = out.sort_values(["time", "horizon"], kind="stable").reset_index(drop=True)
    out = out[list(PRED_COLUMNS)]
    attrs["elapsed_s"] = time.perf_counter() - t_start
    out.attrs.update(attrs)
    log.info("walk-forward TimesFM terminé : %d lignes en %.1f s", len(out), attrs["elapsed_s"])
    return out


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def _binary_p(signal: np.ndarray, p: float = BASELINE_P) -> np.ndarray:
    """``p`` si signal > 0, ``1 - p`` si < 0, 0,5 si nul ou inconnu."""
    s = np.asarray(signal, dtype=np.float64)
    return np.where(s > 0, p, np.where(s < 0, 1.0 - p, 0.5))


def baseline_predictions(df: pd.DataFrame, origins, horizons: Iterable[int] = (5, 10, 15)
                         ) -> pd.DataFrame:
    """Baselines naïves aux mêmes origines, une ligne par (origine, horizon).

    Colonnes de probabilité de hausse (``BASELINE_P`` = 0,52 pour « hausse »,
    0,48 pour « baisse », 0,5 sans information) :

    * ``momentum_h`` : signe du rendement des ``h`` dernières barres (jusqu'à t inclus) ;
    * ``reversal_h`` : l'inverse — le vrai rival à 5–15 min ;
    * ``always_up`` : toujours hausse (révèle l'avantage dû au seul taux de base) ;
    * ``majority_prev_day`` : direction majoritaire des labels à ``h`` barres de la veille
      (jour UTC disponible précédent), en ne gardant que les labels entièrement connus à
      la fin de ce jour-là (barre ``t + h`` dans le même jour) : causal.

    Plus ``ret`` et ``y_true`` (mêmes définitions que :func:`walk_forward_timesfm`).
    """
    hs = tuple(sorted({int(h) for h in horizons}))
    dfc = _clean_close(df)
    bar = bar_duration(dfc.index)
    tz = dfc.index.tz
    org = pd.DatetimeIndex(pd.to_datetime(pd.Index(origins)))
    if org.tz is None and tz is not None:
        org = org.tz_localize(tz)
    elif org.tz is not None and tz is not None:
        org = org.tz_convert(tz)
    org = org.unique().sort_values()
    utc = dfc.index.tz_convert("UTC") if tz is not None else dfc.index
    day = utc.floor("D")
    org_day = (org.tz_convert("UTC") if org.tz is not None else org).floor("D")

    parts = []
    for h in hs:
        fut = future_log_return(dfc, h, bar)
        past = fut.shift(h)                     # log(close[t] / close[t-h]), NaN si trou
        y = direction_label(fut)
        same_day = (utc + h * bar).floor("D") == day
        daily = y[same_day].groupby(day[same_day]).mean().dropna()
        k = pd.DatetimeIndex(daily.index).searchsorted(org_day, side="left") - 1   # veille disponible
        share = (np.where(k >= 0, daily.to_numpy()[np.clip(k, 0, None)], np.nan) if len(daily)
                 else np.full(org.size, np.nan))
        past_o = past.reindex(org).to_numpy(dtype=np.float64)
        ret_o = fut.reindex(org).to_numpy(dtype=np.float64)
        parts.append(pd.DataFrame({
            "time": org, "horizon": h,
            "momentum_h": _binary_p(past_o),
            "reversal_h": _binary_p(-past_o),
            "always_up": np.full(org.size, BASELINE_P),
            "majority_prev_day": _binary_p(share - 0.5),
            "ret": ret_o,
            "y_true": direction_label(pd.Series(ret_o)).to_numpy(),
        }))
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["time", "horizon"], kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------

def pesaran_timmermann(y_true, y_pred) -> tuple[float, float]:
    """Test de Pesaran et Timmermann (1992), H0 : prévision et réalisation indépendantes.

    Rend (statistique, p-valeur unilatérale). NaN si la prévision ou la réalisation est
    constante. Suppose des observations indépendantes (voir ``pt_hac_*`` sinon).
    """
    y = np.asarray(y_true, dtype=np.float64)
    x = np.asarray(y_pred, dtype=np.float64)
    n = y.size
    if n == 0:
        return float("nan"), float("nan")
    p_hat = float(np.mean(y == x))
    py, px = float(y.mean()), float(x.mean())
    if py in (0.0, 1.0) or px in (0.0, 1.0):
        return float("nan"), float("nan")
    p_star = py * px + (1 - py) * (1 - px)
    v_p = p_star * (1 - p_star) / n
    v_star = ((2 * py - 1) ** 2 * px * (1 - px) / n + (2 * px - 1) ** 2 * py * (1 - py) / n
              + 4 * py * px * (1 - py) * (1 - px) / n ** 2)
    var = v_p - v_star
    if not var > 0:
        return float("nan"), float("nan")
    stat = (p_hat - p_star) / math.sqrt(var)
    return float(stat), float(stats.norm.sf(stat))


# PT HAC : nombre minimal de prévisions dans chaque classe. En dessous, la variance HAC
# de la pente repose sur une poignée d'observations et le t explose (ex. 2 « hausse »
# justes sur 98 origines -> t ≈ 10 alors que l'accuracy vaut 46 %).
PT_HAC_MIN_CLASS = 10


def _pt_hac(y: np.ndarray, pred: np.ndarray, lags: int) -> tuple[float, float]:
    """PT robuste à l'autocorrélation : t (Newey-West) de b dans y = a + b·ŷ + u.

    NaN si moins de ``PT_HAC_MIN_CLASS`` prévisions dans l'une des deux classes."""
    n_up = int(np.count_nonzero(pred == 1.0))
    if (y.size < 10 or min(n_up, pred.size - n_up) < PT_HAC_MIN_CLASS
            or y.min() == y.max()):
        return float("nan"), float("nan")
    import statsmodels.api as sm

    X = sm.add_constant(pred.astype(float))
    res = sm.OLS(y.astype(float), X).fit(cov_type="HAC", cov_kwds={"maxlags": int(lags)})
    t = float(res.tvalues[1])
    return t, float(stats.norm.sf(t))


def direction_metrics(y_true, p_up, h: int) -> dict:
    """Métriques de direction d'une probabilité de hausse ``p_up`` contre ``y_true`` (1/0).

    ``h`` : chevauchement des cibles, en nombre d'origines (``h`` si une origine par
    barre ; ``ceil(h / stride)`` si origines espacées de ``stride`` barres). Paires NaN
    ignorées ; prévision « hausse » si ``p_up > 0,5``.

    Clés du contrat : ``n, accuracy, balanced_accuracy, auc, brier, log_loss, p_binom``
    (binomial unilatéral sur ``n_eff = n // h``), ``pt_stat, pt_pvalue`` (Pesaran-
    Timmermann 1992, unilatéral). En plus : ``n_eff, base_rate`` (part de hausses),
    ``excess_accuracy`` (accuracy − max(taux de base, 1 − taux de base)),
    ``pt_hac_t, pt_hac_pvalue`` (Newey-West, ``lags = 2h``).
    """
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    if h < 1:
        raise ValueError("h doit être >= 1")
    y = np.asarray(y_true, dtype=np.float64).ravel()
    p = np.asarray(p_up, dtype=np.float64).ravel()
    if y.shape != p.shape:
        raise ValueError("y_true et p_up doivent avoir la même taille")
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("y_true doit valoir 0, 1 ou NaN")
    n = int(y.size)
    keys = ("accuracy", "balanced_accuracy", "auc", "brier", "log_loss", "p_binom", "pt_stat",
            "pt_pvalue", "base_rate", "excess_accuracy", "pt_hac_t", "pt_hac_pvalue")
    out: dict = {"n": n, "n_eff": max(n // h, 1) if n else 0, **{k: float("nan") for k in keys}}
    if n == 0:
        return out
    pred = (p > 0.5).astype(np.float64)
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    acc = float(np.mean(pred == y))
    base = float(y.mean())
    rec = [np.mean(pred[y == c] == c) for c in (0.0, 1.0) if np.any(y == c)]
    n_eff = out["n_eff"]
    pt_stat, pt_p = pesaran_timmermann(y, pred)
    hac_t, hac_p = _pt_hac(y, pred, 2 * h)
    out.update(
        accuracy=acc,
        balanced_accuracy=float(np.mean(rec)),
        auc=float(roc_auc_score(y, p)) if 0.0 < base < 1.0 else float("nan"),
        brier=float(brier_score_loss(y, pc)),
        log_loss=float(log_loss(y, pc, labels=[0.0, 1.0])),
        p_binom=float(stats.binomtest(int(round(acc * n_eff)), n_eff, 0.5,
                                      alternative="greater").pvalue),
        pt_stat=pt_stat, pt_pvalue=pt_p,
        base_rate=base, excess_accuracy=acc - max(base, 1.0 - base),
        pt_hac_t=hac_t, pt_hac_pvalue=hac_p,
    )
    return out


def metrics_by_horizon(preds: pd.DataFrame, stride: int | None = None, p_col: str = "p_up"
                       ) -> pd.DataFrame:
    """:func:`direction_metrics` pour chaque horizon, avec ``h_eff = ceil(h / stride)``.

    ``stride`` : espacement des origines en barres (défaut : ``preds.attrs["stride"]``,
    sinon 1). Une ligne par horizon.
    """
    s = int(stride or preds.attrs.get("stride", 1) or 1)
    rows = []
    for h, g in preds.groupby("horizon", sort=True):
        h_eff = max(1, math.ceil(int(h) / s))
        rows.append({"horizon": int(h), "h_eff": h_eff,
                     **direction_metrics(g["y_true"], g[p_col], h_eff)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Mini-stratégie
# ---------------------------------------------------------------------------

def toy_strategy(preds: pd.DataFrame, h: int, threshold: float = 0.55, cost_bps: float = 10.0,
                 *, p_col: str = "p_up", bar: pd.Timedelta | None = None) -> dict:
    """Trades non chevauchants long/short sur l'horizon ``h``, avec frais.

    À chaque origine (ordre chronologique), si aucune position n'est ouverte : long si
    ``p >= threshold``, short si ``p <= 1 - threshold`` ; la position est tenue ``h``
    barres (de close[t] à close[t+h]) et la suivante ne peut s'ouvrir qu'à ``t + h``
    barres ou plus tard. Les origines sans rendement réalisé (``ret`` NaN) sont ignorées.
    ``cost_bps`` : coût aller-retour en points de base (10 = futures taker Binance).
    ``bar`` : durée d'une barre (défaut : ``preds.attrs["bar"]``, sinon 1 minute).

    Rend : ``n_signals, n_trades, n_long, n_short, hit_rate`` (part des trades du bon
    côté), ``gross_bps_mean, net_bps_mean, net_bps_se, gross_bps_sum, net_bps_sum``.
    Brut = ``side × log-rendement`` en pb ; net = brut − ``cost_bps`` par trade.
    ``p_col`` permet d'évaluer une P(hausse) recalibrée ou une baseline.
    """
    if not 0.5 <= threshold < 1.0:
        raise ValueError("threshold doit être dans [0,5 ; 1[")
    bar = bar if bar is not None else preds.attrs.get("bar")
    bar = pd.Timedelta(bar) if bar is not None else pd.Timedelta(minutes=1)
    d = preds
    if "horizon" in d.columns:
        d = d[d["horizon"] == h]
    d = d[np.isfinite(d[p_col].to_numpy(float)) & np.isfinite(d["ret"].to_numpy(float))]
    d = d.sort_values("time", kind="stable")
    p = d[p_col].to_numpy(dtype=np.float64)
    r = d["ret"].to_numpy(dtype=np.float64)
    times = pd.DatetimeIndex(d["time"])
    t = times.asi8                                  # entiers dans l'unité de l'index
    hold = int(round(h * bar / pd.Timedelta(1, unit=times.unit)))
    side = np.where((p >= threshold) & (p > 0.5), 1.0,
                    np.where((p <= 1.0 - threshold) & (p < 0.5), -1.0, 0.0))

    trades_side, trades_ret = [], []
    free_at = None
    for i in np.flatnonzero(side != 0):
        if free_at is not None and t[i] < free_at:
            continue
        trades_side.append(side[i])
        trades_ret.append(r[i])
        free_at = t[i] + hold
    s = np.asarray(trades_side)
    gross = s * np.asarray(trades_ret) * 1e4
    net = gross - cost_bps
    n_tr = int(gross.size)
    return dict(
        h=int(h), threshold=float(threshold), cost_bps=float(cost_bps),
        n_signals=int(np.count_nonzero(side)), n_trades=n_tr,
        n_long=int(np.sum(s > 0)), n_short=int(np.sum(s < 0)),
        hit_rate=float(np.mean(gross > 0)) if n_tr else float("nan"),
        gross_bps_mean=float(gross.mean()) if n_tr else float("nan"),
        net_bps_mean=float(net.mean()) if n_tr else float("nan"),
        net_bps_se=float(net.std(ddof=1) / math.sqrt(n_tr)) if n_tr > 1 else float("nan"),
        gross_bps_sum=float(gross.sum()), net_bps_sum=float(net.sum()),
    )
