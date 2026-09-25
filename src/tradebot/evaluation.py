"""Pouvoir prédictif des indicateurs et modèles combinés (phase 1).

Contrat : ``docs/ARCHITECTURE.md`` (section ``evaluation.py``) ; méthode :
``docs/research/methodologie.md`` (§ 2.8 IC, § 3 chevauchement, § 4 walk-forward purgé,
§ 6 tests multiples).

Fonctions publiques
-------------------
* :func:`indicator_scores` : pour UN actif, chaque indicateur × horizon ``h`` :
  IC de Spearman global, IC journalier (jours UTC), t de Newey-West, split
  chronologique apprentissage / test avec purge de ``h`` barres, hit-rate et AUC hors
  échantillon avec le signe appris sur l'apprentissage.
* :func:`aggregate_scores` : agrégation entre actifs (IC moyen, t inter-actifs,
  p combinée de Stouffer, q-valeurs de Benjamini-Hochberg).
* :func:`combined_model_walkforward` / :func:`direction_summary` : modèle combiné
  (logit ou gradient boosting) en walk-forward croissant purgé, puis métriques de
  direction hors échantillon.
* Outils testés séparément : :func:`newey_west_tstat`, :func:`benjamini_hochberg`,
  :func:`stouffer`, :func:`purged_walk_forward_splits`.

Performance (≈ 525 000 barres par an et par actif, ~170 indicateurs × 3 horizons)
---------------------------------------------------------------------------------
Spearman = Pearson des rangs. Chaque colonne n'est **triée qu'une fois** (et chaque
cible ``ret_h`` une fois par horizon). Les rangs sur un sous-échantillon (lignes où
indicateur et cible existent, partie apprentissage, partie test, lignes à label
défini, chaque jour UTC) s'obtiennent ensuite **exactement** en O(n) : on filtre
l'ordre de tri global par le masque du sous-échantillon, qui reste trié, puis on
attribue les rangs moyens des ex-aequo. Pour les rangs par jour, un tri stable par
code du jour (tri radix, O(n), codes entiers de 16 bits), fait une fois par colonne,
regroupe les jours sans défaire l'ordre des valeurs. Tout le « côté cible » (rangs de
``ret_h`` sur l'échantillon, par jour, sur l'apprentissage et le test) ne dépend que
du masque de NaN de l'indicateur : il est calculé une fois par masque distinct et
partagé (≈ 40 masques pour ~160 indicateurs sur BTC).
Le calcul est limité par la bande passante mémoire (gathers aléatoires), pas par le
CPU : 4 fils ou 4 processus n'ont rien accéléré lors de nos mesures (VM 4 CPU), d'où
un calcul séquentiel. Mesuré le 25/09/2026 sur BTCUSDT, 1 an (525 599 barres),
158 indicateurs × 3 horizons : **≈ 54 s** pour ``indicator_scores`` (résultats
identiques à 4e-15 près à une version sans cache), 0,05 s pour ``aggregate_scores`` ;
``combined_model_walkforward`` avec ``row_step=5`` : ≈ 80 s (logit), ≈ 33 s (hgb,
``max_train_rows=100_000``).
Aucun sous-échantillonnage n'est nécessaire dans ``indicator_scores`` : Newey-West
est calculé sur toutes les lignes (2h produits scalaires de longueur n).

Conventions
-----------
* Échantillon d'une paire (indicateur, h) : lignes où l'indicateur et ``ret_h`` sont
  finis. Les lignes sont prises dans l'ordre du temps ; un trou de données n'est pas
  « comblé » pour Newey-West (les retards comptent en lignes).
* Cas dégénérés (colonne constante ou vide, trop peu de lignes, une seule classe…) :
  NaN dans les colonnes concernées, jamais d'exception.
"""

from __future__ import annotations

import hashlib
import math
import warnings
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

__all__ = [
    "MIN_DAY_OBS",
    "SCORE_COLUMNS",
    "AGGREGATE_COLUMNS",
    "SUMMARY_COLUMNS",
    "indicator_scores",
    "aggregate_scores",
    "combined_model_walkforward",
    "direction_summary",
    "purged_walk_forward_splits",
    "newey_west_tstat",
    "benjamini_hochberg",
    "stouffer",
]

# Un jour UTC n'entre dans l'IC journalier que s'il compte au moins MIN_DAY_OBS lignes.
MIN_DAY_OBS = 100

SCORE_COLUMNS: tuple[str, ...] = (
    "indicator", "horizon",
    "n",                      # lignes où indicateur et ret_h sont finis
    "ic_spearman",            # IC de Spearman global (poolé)
    "ic_daily_mean",          # moyenne des IC de Spearman par jour UTC (jours >= MIN_DAY_OBS)
    "ic_daily_t",             # mean / (std / sqrt(n_days)) : t « à la Fama-MacBeth »
    "n_days",                 # nombre de jours retenus pour l'IC journalier
    "frac_months_same_sign",  # part des mois dont l'IC journalier moyen a le signe de l'IC global
    "ic_nw_t",                # t Newey-West (Bartlett, lags = h), rang(ret) sur rang(x) [contrat]
    "ic_nw_t_2h",             # idem avec lags = 2h (méthodologie § 3.3)
    "p_value",                # p bilatérale (loi normale) de ic_nw_t                    [contrat]
    "t_cons",                 # t le plus prudent de ic_nw_t, ic_nw_t_2h, ic_daily_t (0 si signes opposés)
    "p_value_cons",           # p bilatérale de t_cons (à utiliser pour les décisions)
    "ic_train", "ic_test",    # IC de Spearman sur la partie apprentissage (purgée) / test
    "train_sign",             # signe de ic_train (+1 / -1), NaN si indéfini
    "median_train",           # médiane de l'indicateur sur l'apprentissage
    "hit_rate_oos",           # P(prévision = dir_h) sur le test, hausse si (x - médiane)·signe > 0
    "auc_oos",                # AUC de train_sign·x contre dir_h sur le test
    "up_rate_oos",            # part de hausses sur le test (taux de base)
    "n_oos",                  # lignes de test à label défini
    "n_eff_oos",              # n_oos / h (cibles chevauchantes)
    "p_hit",                  # binomiale unilatérale (hit > 0,5) sur floor(n_eff_oos) essais
)

AGGREGATE_COLUMNS: tuple[str, ...] = (
    "indicator", "horizon", "family", "n_tickers",
    "ic_mean", "ic_std", "ic_t_cross", "frac_same_sign",
    "ic_daily_t_mean", "ic_train_mean", "ic_test_mean",
    "hit_rate_oos_mean", "auc_oos_mean",
    "z_combined", "p_combined", "q_value", "q_value_by",
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "horizon", "n", "n_eff", "accuracy", "balanced_accuracy", "auc", "brier",
    "log_loss", "bss", "acc_w", "up_rate", "p_binom",
)


# =============================================================================
# Outils statistiques
# =============================================================================

def _lag_products(e: np.ndarray, max_lag: int) -> np.ndarray:
    """gamma[k] = sum_t e[t] * e[t-k] pour k = 0..max_lag (non normalisé)."""
    m = e.size
    out = np.zeros(max_lag + 1)
    out[0] = float(e @ e)
    for k in range(1, min(max_lag, m - 1) + 1):
        out[k] = float(e[k:] @ e[:-k])
    return out


def _bartlett_sum(gamma: np.ndarray, lags: int) -> float:
    """S = gamma_0 + 2 * sum_{k=1..L} (1 - k/(L+1)) gamma_k (noyau de Bartlett)."""
    lags = min(int(lags), gamma.size - 1)
    if lags <= 0:
        return float(gamma[0])
    k = np.arange(1, lags + 1)
    w = 1.0 - k / (lags + 1.0)
    return float(gamma[0] + 2.0 * np.dot(w, gamma[1:lags + 1]))


def newey_west_tstat(y, x, maxlags: int) -> float:
    """t de la pente de la régression MCO ``y ~ x`` **sans constante**, erreurs HAC.

    Newey-West avec noyau de Bartlett et ``maxlags`` retards, sans correction de petit
    échantillon : identique à ``statsmodels.OLS(y, x).fit(cov_type="HAC",
    cov_kwds={"maxlags": maxlags}).tvalues[0]``. Les paires non finies sont retirées.
    Pour l'IC, passer les rangs standardisés (centrés) de la cible et de l'indicateur :
    la pente vaut alors exactement l'IC de Spearman. NaN si dégénéré.
    """
    y = np.asarray(y, dtype=float).ravel()
    x = np.asarray(x, dtype=float).ravel()
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < 2:
        return float("nan")
    sxx = float(x @ x)
    if not sxx > 0:
        return float("nan")
    beta = float(x @ y) / sxx
    e = x * (y - beta * x)
    s = _bartlett_sum(_lag_products(e, max(int(maxlags), 0)), maxlags)
    var = s / sxx ** 2
    if not var > 0:
        return float("nan")
    return beta / math.sqrt(var)


def benjamini_hochberg(pvalues, method: str = "bh") -> np.ndarray:
    """q-valeurs de Benjamini-Hochberg (``"bh"``) ou Benjamini-Yekutieli (``"by"``).

    Même résultat que ``statsmodels.stats.multitest.multipletests(p, method="fdr_bh"
    | "fdr_by")[1]`` sur les p finies ; les NaN restent NaN et ne comptent pas dans m.
    BY reste valide sous dépendance quelconque (indicateurs très corrélés entre eux).
    """
    p = np.asarray(pvalues, dtype=float).ravel()
    q = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    m = int(ok.sum())
    if m == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv, kind="stable")
    ranked = pv[order] * m / np.arange(1, m + 1)
    if method == "by":
        ranked *= np.sum(1.0 / np.arange(1, m + 1))
    elif method != "bh":
        raise ValueError("method doit valoir 'bh' ou 'by'")
    q_sorted = np.minimum.accumulate(ranked[::-1])[::-1]
    q_ok = np.empty(m)
    q_ok[order] = np.minimum(q_sorted, 1.0)
    q[ok] = q_ok
    return q


def stouffer(z_scores) -> tuple[float, float]:
    """Combinaison de Stouffer : Z = sum(z_i) / sqrt(k), p bilatérale = 2 * Phi(-|Z|).

    Les z_i sont des statistiques **signées** (ici des t robustes par actif) : des
    effets de même signe s'additionnent, des effets opposés se compensent. Suppose des
    z_i indépendants ; entre cryptos corrélées (BTC/ETH…) la p combinée est donc
    optimiste. Les NaN sont ignorés ; (NaN, NaN) si aucun z fini.
    """
    z = np.asarray(z_scores, dtype=float).ravel()
    z = z[np.isfinite(z)]
    if z.size == 0:
        return float("nan"), float("nan")
    zc = float(z.sum() / math.sqrt(z.size))
    return zc, float(2.0 * stats.norm.sf(abs(zc)))


def _binom_p_greater(hit: float, n_eff: float) -> float:
    """P(X >= k), X ~ B(floor(n_eff), 1/2), k = round(hit * floor(n_eff))."""
    n = int(math.floor(n_eff)) if np.isfinite(n_eff) else 0
    if n < 1 or not np.isfinite(hit):
        return float("nan")
    k = int(round(hit * n))
    return float(stats.binom.sf(k - 1, n, 0.5))


def _t_conservative(*ts: float) -> float:
    """t le plus prudent : |t| minimal si tous les t finis ont le même signe, 0 sinon."""
    vals = np.array([t for t in ts if t is not None and np.isfinite(t)], dtype=float)
    if vals.size == 0:
        return float("nan")
    if (vals > 0).all() or (vals < 0).all():
        return float(vals[np.argmin(np.abs(vals))])
    return 0.0


# =============================================================================
# Rangs exacts sur sous-échantillons à partir d'un seul tri
# =============================================================================

def _avg_ranks_sorted(v: np.ndarray, g: np.ndarray | None = None) -> np.ndarray:
    """Rangs moyens (base 1) des ex-aequo pour des valeurs ``v`` déjà triées.

    Avec ``g`` (codes de groupe, ``(g, v)`` trié lexicographiquement), rangs *dans* chaque
    groupe. Peu de passes mémoire : on part de ``1..m`` et on ne corrige que les
    positions ex-aequo (rares pour un indicateur continu).
    """
    m = v.size
    ranks = np.arange(1.0, m + 1.0)
    if m == 0:
        return ranks
    eq = v[1:] == v[:-1]
    gstarts = None
    if g is not None:
        gb = g[1:] != g[:-1]
        eq &= ~gb
        gstarts = np.flatnonzero(gb) + 1
    if eq.any():
        tie = np.zeros(m, dtype=bool)
        tie[1:] = eq
        tie[:-1] |= eq
        ti = np.flatnonzero(tie)                     # positions des ex-aequo
        st = np.ones(ti.size, dtype=bool)            # début d'un groupe d'ex-aequo ?
        st[1:] = ~eq[ti[1:] - 1]
        gs = np.flatnonzero(st)
        size = np.diff(gs, append=ti.size)
        ranks[ti] = np.repeat(ti[gs] + (size + 1) * 0.5, size)
    if gstarts is not None and gstarts.size:
        bounds = np.concatenate(([0], gstarts))
        ranks -= np.repeat(bounds.astype(float), np.diff(bounds, append=m))
    return ranks


def _center_ranks(ranks: np.ndarray) -> np.ndarray:
    """Rangs moyens moins leur moyenne exacte (m+1)/2 (en place)."""
    ranks -= (ranks.size + 1) * 0.5
    return ranks


@dataclass
class _Horizon:
    h: int
    rok: np.ndarray        # ret_h fini
    order_r: np.ndarray    # indices des lignes rok triées par ret_h croissant
    rs: np.ndarray         # ret_h[order_r]
    order_rd: np.ndarray   # mêmes lignes triées par (jour, ret_h)
    rds: np.ndarray        # ret_h[order_rd]
    d: np.ndarray          # dir_h (1 / 0 / NaN)
    dok: np.ndarray        # dir_h fini
    train: np.ndarray      # position < split - h (apprentissage purgé)


@dataclass
class _Context:
    n: int
    day: np.ndarray        # code du jour par ligne (n_days = jour inconnu)
    n_days: int
    day_month: np.ndarray  # code du mois de chaque jour (-1 inconnu)
    test: np.ndarray       # position >= split
    horizons: list


@dataclass
class _TargetSide:
    """Tout ce qui ne dépend que de ret_h et de l'échantillon V (partagé entre colonnes)."""
    rows: np.ndarray       # lignes de V, ordre du temps
    zb: np.ndarray | None  # rangs standardisés de ret_h sur V, ordre du temps
    b_day: np.ndarray      # (n,) rangs de ret_h dans le jour, centrés
    cnt_day: np.ndarray    # lignes de V par jour
    sbb_day: np.ndarray    # somme des carrés de b_day par jour
    b_split: np.ndarray    # (n,) rangs centrés dans l'apprentissage / dans le test
    sbb_train: float
    sbb_test: float


def _target_side(hz: _Horizon, V: np.ndarray, ctx: _Context) -> _TargetSide:
    n = ctx.n
    rows = np.flatnonzero(V)
    n_v = rows.size
    sel = V[hz.order_r]
    ir, rv = hz.order_r[sel], hz.rs[sel]
    buf = np.empty(n)
    buf[ir] = _avg_ranks_sorted(rv)
    b = buf[rows]
    b -= b.mean()
    sbb = float(b @ b)
    zb = b / math.sqrt(sbb / n_v) if sbb > 0 else None

    nb = ctx.n_days + 1
    cnt = np.bincount(ctx.day[rows], minlength=nb).astype(float)
    sel = V[hz.order_rd]
    ird = hz.order_rd[sel]
    gd = ctx.day[ird]
    bd = _avg_ranks_sorted(hz.rds[sel], gd)
    bd -= (cnt[gd] + 1.0) * 0.5
    b_day = np.empty(n)
    b_day[ird] = bd
    sbb_day = np.bincount(gd, bd * bd, minlength=nb)

    b_split = np.empty(n)
    sbb_part = []
    for part in (hz.train, ctx.test):
        s = part[ir]
        c = _center_ranks(_avg_ranks_sorted(rv[s]))
        b_split[ir[s]] = c
        sbb_part.append(float(c @ c))
    return _TargetSide(rows=rows, zb=zb, b_day=b_day, cnt_day=cnt, sbb_day=sbb_day,
                       b_split=b_split, sbb_train=sbb_part[0], sbb_test=sbb_part[1])


def _corr_centered(a: np.ndarray, b: np.ndarray, sbb: float) -> float:
    """Corrélation de deux vecteurs de rangs déjà centrés (Σb² fourni)."""
    if a.size < 3:
        return float("nan")
    saa = float(a @ a)
    if not (saa > 0 and sbb > 0):
        return float("nan")
    return float(a @ b) / math.sqrt(saa * sbb)


def _day_codes(features: pd.DataFrame, targets: pd.DataFrame) -> tuple[np.ndarray, int, np.ndarray]:
    """Codes des jours (colonne ``session`` sinon date de l'index) et mois de chaque jour."""
    n = len(features)
    if "session" in features.columns:
        keys = pd.Index(features["session"])
    elif "session" in targets.columns:
        keys = pd.Index(targets["session"])
    elif isinstance(features.index, pd.DatetimeIndex):
        keys = features.index.normalize()          # date dans le fuseau de l'index (UTC crypto)
    else:                                           # pas de notion de jour : pas d'IC journalier
        return np.zeros(n, dtype=np.uint16), 0, np.empty(0, dtype=np.int64)
    codes, uniques = pd.factorize(keys, sort=False)
    n_days = len(uniques)
    codes = np.where(codes < 0, n_days, codes)
    # uint16 -> le tri stable par jour est un tri radix O(n)
    dtype = np.uint16 if n_days < np.iinfo(np.uint16).max else np.int64
    try:
        u = pd.DatetimeIndex(pd.to_datetime(pd.Index(uniques), errors="coerce"))
        month_key = np.where(u.isna(), -1, np.asarray(u.year) * 12 + np.asarray(u.month))
    except (TypeError, ValueError):
        month_key = np.full(n_days, -1)
    month_key = np.asarray(month_key, dtype=np.int64)
    day_month = np.asarray(pd.factorize(month_key)[0], dtype=np.int64)
    day_month[month_key < 0] = -1
    return codes.astype(dtype), n_days, day_month


def _numeric_columns(features: pd.DataFrame) -> list[int]:
    """Positions des colonnes numériques ou booléennes (hors ``session``)."""
    out = []
    for j, (c, dt) in enumerate(zip(features.columns, features.dtypes)):
        if c == "session":
            continue
        if pd.api.types.is_bool_dtype(dt) or pd.api.types.is_numeric_dtype(dt):
            out.append(j)
    return out


def _column_values(features: pd.DataFrame, j: int) -> np.ndarray:
    """Colonne ``j`` (position) en float64, ±inf -> NaN (copie)."""
    s = features.iloc[:, j]
    x = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    x = np.array(x, dtype=float, copy=True)
    x[~np.isfinite(x)] = np.nan
    return x


def _align_targets(features: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    if targets.index.equals(features.index):
        return targets
    return targets.reindex(features.index)


def _target_arrays(targets: pd.DataFrame, h: int) -> tuple[np.ndarray, np.ndarray]:
    """(ret_h, dir_h) en float ; dir_h déduit du signe de ret_h s'il manque."""
    rc, dc = f"ret_{h}", f"dir_{h}"
    if rc not in targets.columns:
        raise KeyError(f"colonne {rc!r} absente de targets (voir targets.make_targets)")
    r = pd.to_numeric(targets[rc], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    r = np.where(np.isfinite(r), r, np.nan)
    if dc in targets.columns:
        d = pd.to_numeric(targets[dc], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        d = np.where(np.isfinite(d), d, np.nan)
    else:
        d = np.where(r > 0, 1.0, np.where(r < 0, 0.0, np.nan))
    return r, d


def _nan_row(name, h: int, n: int = 0) -> dict:
    row = {c: np.nan for c in SCORE_COLUMNS}
    row.update(indicator=name, horizon=int(h), n=int(n), n_days=0, n_oos=0, n_eff_oos=0.0)
    return row


def _mask_key(mask: np.ndarray) -> bytes:
    return hashlib.blake2b(np.packbits(mask).tobytes(), digest_size=16).digest()


def _scores_one_column(name, x: np.ndarray, ctx: _Context, tcache: dict) -> list[dict]:
    """Toutes les statistiques d'un indicateur pour chaque horizon.

    ``x`` n'est trié qu'une fois (par valeur, puis par (jour, valeur) avec un tri radix
    stable) ; ``tcache`` garde le côté cible pour le masque de NaN courant.
    """
    n = ctx.n
    xok = np.isfinite(x)
    if not xok.any():
        return [_nan_row(name, hz.h) for hz in ctx.horizons]
    order_x = np.flatnonzero(xok)
    order_x = order_x[np.argsort(x[order_x])]
    xs = x[order_x]
    if ctx.n_days > 0:
        order_xd = order_x[np.argsort(ctx.day[order_x], kind="stable")]
        xds = x[order_xd]
    key = _mask_key(xok)
    buf = np.empty(n)
    nb = ctx.n_days + 1
    rows_out = []
    for hz in ctx.horizons:
        h = hz.h
        V = xok & hz.rok
        n_v = int(np.count_nonzero(V))
        row = _nan_row(name, h, n_v)
        rows_out.append(row)
        if n_v < 3:
            continue
        ts = tcache.get((h, key))
        if ts is None:
            ts = tcache[(h, key)] = _target_side(hz, V, ctx)

        # --- IC global + Newey-West ------------------------------------------
        sel = V[order_x]
        ix, xv = order_x[sel], xs[sel]
        buf[ix] = _avg_ranks_sorted(xv)
        a = buf[ts.rows]
        a -= a.mean()
        saa = float(a @ a)
        ic = float("nan")
        if saa > 0 and ts.zb is not None:
            za = a / math.sqrt(saa / n_v)      # rangs standardisés (écart type ddof=0)
            zb = ts.zb
            ic = float(za @ zb) / n_v          # = pente MCO sans constante = Spearman
            e = za * (zb - ic * za)
            gamma = _lag_products(e, 2 * h)
            for col, lags in (("ic_nw_t", h), ("ic_nw_t_2h", 2 * h)):
                var = _bartlett_sum(gamma, lags) / float(n_v) ** 2
                row[col] = ic / math.sqrt(var) if var > 0 else np.nan
        row["ic_spearman"] = ic
        if np.isfinite(row["ic_nw_t"]):
            row["p_value"] = float(2.0 * stats.norm.sf(abs(row["ic_nw_t"])))

        # --- IC par jour UTC --------------------------------------------------
        if ctx.n_days > 0:
            sel = V[order_xd]
            ixd = order_xd[sel]
            gd = ctx.day[ixd]
            ad = _avg_ranks_sorted(xds[sel], gd)
            ad -= (ts.cnt_day[gd] + 1.0) * 0.5
            sab = np.bincount(gd, ad * ts.b_day[ixd], minlength=nb)
            saa_d = np.bincount(gd, ad * ad, minlength=nb)
            good = (ts.cnt_day >= MIN_DAY_OBS) & (saa_d > 0) & (ts.sbb_day > 0)
            good[ctx.n_days] = False
            vals = sab[good] / np.sqrt(saa_d[good] * ts.sbb_day[good])
            row["n_days"] = int(vals.size)
            if vals.size >= 1:
                row["ic_daily_mean"] = float(vals.mean())
            if vals.size >= 2:
                sd = float(vals.std(ddof=1))
                if sd > 0:
                    row["ic_daily_t"] = float(vals.mean() / (sd / math.sqrt(vals.size)))
            if vals.size >= 1 and np.isfinite(ic) and ic != 0:
                mcode = ctx.day_month[np.flatnonzero(good[:ctx.n_days])]
                keep = mcode >= 0
                if keep.any():
                    msum = np.bincount(mcode[keep], vals[keep])
                    mcnt = np.bincount(mcode[keep])
                    mmean = msum[mcnt > 0] / mcnt[mcnt > 0]
                    row["frac_months_same_sign"] = float(np.mean(np.sign(mmean) == np.sign(ic)))

        row["t_cons"] = _t_conservative(row["ic_nw_t"], row["ic_nw_t_2h"], row["ic_daily_t"])
        if np.isfinite(row["t_cons"]):
            row["p_value_cons"] = float(2.0 * stats.norm.sf(abs(row["t_cons"])))

        # --- split chronologique : apprentissage purgé / test ---------------
        sel = hz.train[ix]
        ixt, xvt = ix[sel], xv[sel]
        m = ixt.size
        if m:
            row["median_train"] = float(0.5 * (xvt[(m - 1) // 2] + xvt[m // 2]))
            row["ic_train"] = _corr_centered(_center_ranks(_avg_ranks_sorted(xvt)),
                                             ts.b_split[ixt], ts.sbb_train)
        ic_tr = row["ic_train"]
        sign = float(np.sign(ic_tr)) if np.isfinite(ic_tr) and ic_tr != 0 else np.nan
        row["train_sign"] = sign

        sel = ctx.test[ix]
        ixe, xve = ix[sel], xv[sel]
        if ixe.size:
            row["ic_test"] = _corr_centered(_center_ranks(_avg_ranks_sorted(xve)),
                                            ts.b_split[ixe], ts.sbb_test)

        # --- hit-rate / AUC hors échantillon ---------------------------------
        sel = hz.dok[ixe]
        ixo, xvo = ixe[sel], xve[sel]
        n_oos = int(ixo.size)
        row["n_oos"] = n_oos
        row["n_eff_oos"] = n_oos / h
        if n_oos:
            yo = hz.d[ixo] == 1.0
            n1 = int(np.count_nonzero(yo))
            n0 = n_oos - n1
            row["up_rate_oos"] = n1 / n_oos
            med = row["median_train"]
            if np.isfinite(sign) and np.isfinite(med):
                pred_up = (xvo - med) * sign > 0
                hit = float(np.mean(pred_up == yo))
                row["hit_rate_oos"] = hit
                row["p_hit"] = _binom_p_greater(hit, n_oos / h)
                if n1 > 0 and n0 > 0:
                    ranks = _avg_ranks_sorted(xvo)
                    auc_x = (float(ranks[yo].sum()) - n1 * (n1 + 1) / 2.0) / (n1 * n0)
                    row["auc_oos"] = auc_x if sign > 0 else 1.0 - auc_x
    return rows_out


def indicator_scores(features: pd.DataFrame, targets: pd.DataFrame,
                     horizons: Iterable[int] = (5, 10, 15), train_frac: float = 0.6) -> pd.DataFrame:
    """Pouvoir prédictif de chaque indicateur, pour UN actif, à chaque horizon.

    Paramètres
    ----------
    features : indicateurs (``indicators.compute_indicators``), indexés par le temps.
        Les colonnes non numériques sont ignorées ; une colonne ``session`` sert de clé
        de jour (sinon ``session`` de ``targets``, sinon la date de l'index : jour UTC
        pour la crypto).
    targets : colonnes ``ret_{h}`` (obligatoire) et ``dir_{h}`` (``targets.make_targets`` ;
        déduite du signe de ``ret_{h}`` si absente), réalignées sur ``features.index``.
    horizons : horizons ``h`` en barres.
    train_frac : part chronologique d'apprentissage, dans ]0, 1[. Le point de coupure
        est commun à tous les indicateurs : ``split = round(train_frac * len(features))``
        (en lignes). Apprentissage = lignes ``< split - h`` (**purge / embargo** de h
        barres : leur cible chevaucherait le test), test = lignes ``>= split``.

    Retour
    ------
    Une ligne par (indicateur, horizon), colonnes :data:`SCORE_COLUMNS`. Définitions :

    * ``ic_spearman`` : Spearman(x, ret_h) sur les lignes où les deux sont finis.
    * ``ic_daily_mean`` / ``ic_daily_t`` : IC de Spearman calculé dans chaque jour
      (jours < ``MIN_DAY_OBS`` lignes ignorés), moyenne et ``mean / (sd / sqrt(D))``.
    * ``ic_nw_t`` : t de la régression des rangs standardisés de ret_h sur ceux de x
      (pente = IC), erreurs Newey-West Bartlett ``maxlags = h`` ; ``ic_nw_t_2h`` avec
      ``2h`` (plus prudent, méthodologie § 3.3) ; ``p_value`` : bilatérale de ``ic_nw_t``.
    * ``t_cons`` / ``p_value_cons`` : le plus prudent de ``ic_nw_t``, ``ic_nw_t_2h`` et
      ``ic_daily_t`` (0 si leurs signes divergent), recommandé pour les décisions.
    * ``train_sign`` = signe de ``ic_train`` ; sur le test, hausse prévue si
      ``(x - median_train) * train_sign > 0`` ; ``hit_rate_oos`` contre ``dir_h`` ;
      ``auc_oos`` de ``train_sign * x`` ; ``p_hit`` binomiale unilatérale sur
      ``floor(n_oos / h)`` essais (conservatrice, méthodologie § 2.4).

    ``attrs`` : ``train_frac``, ``split_row``, ``split_time``, ``n_rows``.
    """
    if not 0.0 < float(train_frac) < 1.0:
        raise ValueError("train_frac doit être dans ]0, 1[")
    horizons = [int(h) for h in horizons]
    if any(h < 1 for h in horizons):
        raise ValueError("les horizons doivent être >= 1")
    if not isinstance(features, pd.DataFrame) or not isinstance(targets, pd.DataFrame):
        raise TypeError("features et targets doivent être des pd.DataFrame")
    cols = _numeric_columns(features)
    if not cols or not horizons:
        return pd.DataFrame(columns=list(SCORE_COLUMNS))
    if not features.index.is_monotonic_increasing:
        features = features.sort_index(kind="stable")
    targets = _align_targets(features, targets)
    n = len(features)

    split = int(round(float(train_frac) * n))
    pos = np.arange(n)
    day, n_days, day_month = _day_codes(features, targets)
    hzs = []
    for h in horizons:
        r, d = _target_arrays(targets, h)
        rok = np.isfinite(r)
        order_r = np.flatnonzero(rok)
        order_r = order_r[np.argsort(r[order_r])]
        order_rd = order_r[np.argsort(day[order_r], kind="stable")]
        hzs.append(_Horizon(h=h, rok=rok, order_r=order_r, rs=r[order_r], order_rd=order_rd,
                            rds=r[order_rd], d=d, dok=np.isfinite(d), train=pos < split - h))
    ctx = _Context(n=n, day=day, n_days=n_days, day_month=day_month, test=pos >= split,
                   horizons=hzs)

    # Colonnes regroupées par masque de NaN : le côté cible (rangs de ret_h sur V, par
    # jour, apprentissage / test) est calculé une fois par masque puis réutilisé.
    groups: dict[bytes, list[int]] = {}
    for j in cols:
        groups.setdefault(_mask_key(np.isfinite(_column_values(features, j))), []).append(j)
    names = list(features.columns)
    results: dict[int, list] = {}
    with np.errstate(all="ignore"):
        for members in groups.values():
            tcache: dict = {}
            for j in members:
                results[j] = _scores_one_column(names[j], _column_values(features, j), ctx, tcache)
    out = pd.DataFrame([row for j in cols for row in results[j]], columns=list(SCORE_COLUMNS))
    for c in ("horizon", "n", "n_days", "n_oos"):
        out[c] = out[c].astype("int64")
    out.attrs.update(train_frac=float(train_frac), split_row=split, n_rows=n,
                     split_time=features.index[split] if 0 <= split < n else None)
    return out


# =============================================================================
# Agrégation entre actifs
# =============================================================================

def _default_families() -> dict:
    try:
        from tradebot.indicators import REGISTRY
    except Exception:  # pragma: no cover - module indicateurs indisponible
        return {}
    return {name: spec.family for name, spec in REGISTRY.items()}


def aggregate_scores(per_ticker: Mapping[str, pd.DataFrame],
                     families: Mapping[str, str] | None = None) -> pd.DataFrame:
    """Agrège les ``indicator_scores`` de plusieurs actifs, par (indicateur, horizon).

    Colonnes (:data:`AGGREGATE_COLUMNS`) :

    * ``n_tickers`` : actifs dont l'IC est défini ; ``ic_mean`` / ``ic_std`` (ddof=1) ;
      ``ic_t_cross = ic_mean / (ic_std / sqrt(n_tickers))`` ; ``frac_same_sign`` : part
      des actifs dont l'IC a le signe de ``ic_mean``.
    * ``ic_daily_t_mean``, ``ic_train_mean`` (sélection sans fuite : n'utilise que les
      parties apprentissage), ``ic_test_mean``, ``hit_rate_oos_mean``, ``auc_oos_mean``.
    * ``z_combined`` / ``p_combined`` : **Stouffer** sur les t robustes signés de chaque
      actif (``t_cons`` s'il existe, sinon ``ic_nw_t``) : ``Z = sum(t_i)/sqrt(k)``,
      ``p = 2 Phi(-|Z|)``. Hypothèse d'indépendance entre actifs : les cryptos étant
      corrélées, cette p est optimiste ; la juger avec ``frac_same_sign`` et
      ``ic_t_cross``.
    * ``q_value`` : Benjamini-Hochberg de ``p_combined`` sur **toutes** les lignes
      (indicateurs × horizons) ; ``q_value_by`` : Benjamini-Yekutieli (dépendance
      quelconque).
    * ``family`` : ``families[indicator]`` ; par défaut la famille du registre
      ``indicators.REGISTRY`` (NaN si inconnue).

    Tri par ``|ic_mean|`` décroissant (NaN en dernier).
    """
    frames = []
    for ticker, df in (per_ticker or {}).items():
        if df is None or len(df) == 0:
            continue
        frames.append(df.assign(ticker=ticker))
    if not frames:
        return pd.DataFrame(columns=list(AGGREGATE_COLUMNS))
    allr = pd.concat(frames, ignore_index=True)
    for c in ("ic_spearman", "ic_daily_t", "ic_train", "ic_test", "hit_rate_oos", "auc_oos",
              "ic_nw_t", "t_cons"):
        if c not in allr.columns:
            allr[c] = np.nan
        allr[c] = pd.to_numeric(allr[c], errors="coerce").astype(float)
    t_used = allr["t_cons"].where(np.isfinite(allr["t_cons"]), allr["ic_nw_t"])
    allr["_t"] = t_used

    keys = ["indicator", "horizon"]
    g = allr.groupby(keys, sort=False)
    out = g["ic_spearman"].agg(n_tickers="count", ic_mean="mean", ic_std="std")
    out["ic_mean_sign"] = np.sign(out["ic_mean"])
    sgn = allr.join(out["ic_mean_sign"], on=keys)
    same = (np.sign(sgn["ic_spearman"]) == sgn["ic_mean_sign"]).where(sgn["ic_spearman"].notna())
    out["frac_same_sign"] = same.astype(float).groupby([sgn["indicator"], sgn["horizon"]],
                                                       sort=False).mean()
    out.loc[out["n_tickers"] == 0, "frac_same_sign"] = np.nan
    se = out["ic_std"] / np.sqrt(out["n_tickers"])
    out["ic_t_cross"] = (out["ic_mean"] / se).where((out["n_tickers"] >= 2) & (se > 0))
    for src, dst in (("ic_daily_t", "ic_daily_t_mean"), ("ic_train", "ic_train_mean"),
                     ("ic_test", "ic_test_mean"), ("hit_rate_oos", "hit_rate_oos_mean"),
                     ("auc_oos", "auc_oos_mean")):
        out[dst] = g[src].mean()
    tsum = g["_t"].sum(min_count=1)
    tcnt = g["_t"].count()
    out["z_combined"] = (tsum / np.sqrt(tcnt)).where(tcnt > 0)
    out["p_combined"] = 2.0 * stats.norm.sf(np.abs(out["z_combined"].to_numpy(dtype=float)))
    out["q_value"] = benjamini_hochberg(out["p_combined"].to_numpy(), "bh")
    out["q_value_by"] = benjamini_hochberg(out["p_combined"].to_numpy(), "by")
    out = out.reset_index()
    fam = dict(_default_families() if families is None else families)
    out["family"] = out["indicator"].map(lambda k: fam.get(k, np.nan))
    out["horizon"] = out["horizon"].astype("int64")
    out["n_tickers"] = out["n_tickers"].astype("int64")
    out = out[list(AGGREGATE_COLUMNS)]
    order = np.argsort(-np.abs(out["ic_mean"].to_numpy(dtype=float)), kind="stable")
    out = out.iloc[order]
    nan_last = out["ic_mean"].isna().to_numpy()
    out = pd.concat([out[~nan_last], out[nan_last]]).reset_index(drop=True)
    return out


# =============================================================================
# Modèle combiné en walk-forward purgé
# =============================================================================

def purged_walk_forward_splits(n_rows: int, n_splits: int, h: int,
                               row_step: int = 1) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Plis walk-forward **croissants** et purgés, en positions de lignes (0..n_rows-1).

    Les origines retenues forment la grille ``0, row_step, 2*row_step, …``. La grille
    est découpée comme ``sklearn.model_selection.TimeSeriesSplit(n_splits)`` (plis de
    test contigus couvrant les ``n_splits/(n_splits+1)`` dernières origines). Pour un pli
    de test commençant à la ligne ``te0``, l'apprentissage garde les origines
    ``< te0 - h`` : les h dernières lignes, dont la cible ``[t, t+h]`` chevauche le test,
    sont **purgées** (même règle que ``TimeSeriesSplit(gap=h)`` quand ``row_step = 1``).
    Rien n'est produit si la grille compte moins de ``n_splits + 1`` origines.
    """
    from sklearn.model_selection import TimeSeriesSplit

    row_step = max(1, int(row_step))
    grid = np.arange(0, max(int(n_rows), 0), row_step)
    if n_splits < 2 or grid.size < n_splits + 1:
        return
    for tr, te in TimeSeriesSplit(n_splits=n_splits).split(grid):
        te_pos = grid[te]
        tr_pos = grid[tr]
        yield tr_pos[tr_pos < te_pos[0] - h], te_pos


def _thin_recent(pos: np.ndarray, max_rows: int | None) -> np.ndarray:
    """Au plus ``max_rows`` lignes régulièrement espacées, en gardant la plus récente."""
    if max_rows is None or pos.size <= max_rows:
        return pos
    step = int(math.ceil(pos.size / max(int(max_rows), 1)))
    return pos[::-1][::step][::-1]


def _make_model(model: str, random_state: int):
    if model == "logit":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000))
    if model == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=200, early_stopping=True,
            n_iter_no_change=20, scoring="loss", validation_fraction=None,
            random_state=random_state)
    raise ValueError("model doit valoir 'logit' ou 'hgb'")


def _fit_predict(model: str, X: np.ndarray, y: np.ndarray, tr: np.ndarray, te: np.ndarray,
                 h: int, random_state: int) -> tuple[np.ndarray, int]:
    """Probabilité de hausse sur ``te`` ; imputation par la médiane de l'apprentissage."""
    p = np.full(te.size, np.nan)
    ytr = y[tr]
    if tr.size < 10 or ytr.min() == ytr.max():
        return p, 0
    Xtr = X[tr]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(Xtr, axis=0)
    keep = np.isfinite(med)
    if not keep.any():
        return p, 0
    Xtr = Xtr[:, keep]
    med = med[keep]
    Xte = X[te][:, keep]
    np.copyto(Xtr, med, where=np.isnan(Xtr))
    np.copyto(Xte, med, where=np.isnan(Xte))
    est = _make_model(model, random_state)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if model == "hgb":
            # Arrêt précoce CHRONOLOGIQUE : validation = derniers 10 % de l'apprentissage,
            # séparés du cœur par une purge de h lignes (pas de tirage aléatoire).
            n_val = max(int(0.1 * tr.size), 1)
            cut = tr[-n_val]
            core = tr[:-n_val] < cut - h
            yc, yv = ytr[:-n_val][core], ytr[-n_val:]
            if core.sum() >= 10 and yc.min() != yc.max() and yv.min() != yv.max():
                est.fit(Xtr[:-n_val][core], yc, X_val=Xtr[-n_val:], y_val=yv)
            else:
                est.set_params(early_stopping=False)
                est.fit(Xtr, ytr)
        else:
            est.fit(Xtr, ytr)
    proba = est.predict_proba(Xte)
    classes = list(est.classes_)
    p[:] = proba[:, classes.index(1.0)]
    return p, int(tr.size)


def combined_model_walkforward(features: pd.DataFrame, targets: pd.DataFrame,
                               horizons: Iterable[int] = (5, 10, 15), n_splits: int = 5,
                               model: str = "logit", max_train_rows: int | None = None,
                               row_step: int = 1, *, random_state: int = 0) -> pd.DataFrame:
    """Probabilité de hausse hors échantillon d'un modèle combinant tous les indicateurs.

    Walk-forward **croissant** en ``n_splits`` plis (:func:`purged_walk_forward_splits`) :
    à chaque pli, l'apprentissage couvre tout le passé moins les ``h`` dernières lignes
    (**purge**), les NaN sont imputés par la médiane de l'apprentissage (colonnes
    entièrement NaN à l'apprentissage retirées), puis :

    * ``"logit"`` : ``StandardScaler`` + ``LogisticRegression(C=0.1)`` ;
    * ``"hgb"`` : ``HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
      max_iter=200)`` avec arrêt précoce **chronologique** (validation = derniers 10 %
      de l'apprentissage, purgés de h lignes).

    Sous-échantillonnage (documenté, désactivé par défaut) :

    * ``row_step = s`` : seules les origines ``0, s, 2s, …`` servent (apprentissage et
      test). Le chevauchement entre origines tombe à ``h/s`` ; ``direction_summary`` en
      tient compte (``n_eff = n / ceil(h/s)``).
    * ``max_train_rows`` : si l'apprentissage d'un pli dépasse cette taille, il est
      éclairci régulièrement (une ligne sur ``ceil(n/max)``, la plus récente gardée) : on
      conserve tout l'historique (fenêtre croissante) avec des cibles moins redondantes.

    Retour : DataFrame indexé par les origines hors échantillon (union des plis de test),
    colonnes ``fold`` puis ``p_up_{h}``, ``y_{h}`` (``dir_h``), ``ret_{h}`` pour chaque h.
    ``attrs`` : ``model``, ``n_splits``, ``row_step``, ``max_train_rows`` et ``folds``
    (bornes temporelles de l'apprentissage et du test de chaque pli et horizon).
    """
    if model not in ("logit", "hgb"):
        raise ValueError("model doit valoir 'logit' ou 'hgb'")
    horizons = [int(h) for h in horizons]
    row_step = max(1, int(row_step))
    cols_out = ["fold"] + [f"{k}_{h}" for h in horizons for k in ("p_up", "y", "ret")]
    cols = _numeric_columns(features)
    if not features.index.is_monotonic_increasing:
        features = features.sort_index(kind="stable")
    targets = _align_targets(features, targets)
    n = len(features)
    attrs = dict(model=model, n_splits=int(n_splits), row_step=row_step,
                 max_train_rows=max_train_rows, horizons=tuple(horizons), folds=[],
                 features=[features.columns[j] for j in cols])

    folds_ref = list(purged_walk_forward_splits(n, n_splits, 0, row_step))
    if not folds_ref or not cols:
        out = pd.DataFrame(columns=cols_out, index=features.index[:0])
        out.attrs.update(attrs)
        return out
    te_all = np.concatenate([te for _, te in folds_ref])
    fold_id = np.concatenate([np.full(te.size, k) for k, (_, te) in enumerate(folds_ref)])
    X = np.empty((n, len(cols)))
    for k, j in enumerate(cols):
        X[:, k] = _column_values(features, j)
    idx = features.index
    out = pd.DataFrame({"fold": fold_id}, index=idx[te_all])
    for h in horizons:
        r, y = _target_arrays(targets, h)
        p_up = np.full(te_all.size, np.nan)
        start = 0
        for k, (tr, te) in enumerate(purged_walk_forward_splits(n, n_splits, h, row_step)):
            tr = tr[np.isfinite(y[tr])]
            tr = _thin_recent(tr, max_train_rows)
            p, n_tr = _fit_predict(model, X, y, tr, te, h, random_state)
            p_up[start:start + te.size] = p
            start += te.size
            attrs["folds"].append(dict(
                horizon=h, fold=k, n_train=n_tr,
                train_start=idx[tr[0]] if tr.size else None,
                train_end=idx[tr[-1]] if tr.size else None,
                train_end_row=int(tr[-1]) if tr.size else -1,
                test_start=idx[te[0]], test_end=idx[te[-1]], test_start_row=int(te[0])))
        out[f"p_up_{h}"] = p_up
        out[f"y_{h}"] = y[te_all]
        out[f"ret_{h}"] = r[te_all]
    out = out[cols_out]
    out.attrs.update(attrs)
    return out


def direction_summary(df_oos: pd.DataFrame, horizons: Iterable[int] = (5, 10, 15)) -> pd.DataFrame:
    """Métriques de direction hors échantillon par horizon (sortie de
    :func:`combined_model_walkforward`, ou tout DataFrame ``p_up_{h}``, ``y_{h}`` [, ``ret_{h}``]).

    Colonnes (:data:`SUMMARY_COLUMNS`) : ``n`` (lignes où y et p sont définis),
    ``n_eff = n / ceil(h / row_step)`` (``row_step`` lu dans ``df_oos.attrs``, 1 par
    défaut), ``accuracy`` (hausse prévue si p > 0,5), ``balanced_accuracy``, ``auc``,
    ``brier``, ``log_loss`` (p bornée à [1e-6, 1-1e-6]), ``bss`` = 1 - Brier / Brier de la
    climatologie hors échantillon ȳ(1-ȳ) (référence prudente), ``acc_w`` : accuracy
    pondérée par |ret_h| (méthodologie § 8.6), ``up_rate`` et ``p_binom`` (binomiale
    unilatérale de l'accuracy sur ``floor(n_eff)`` essais). NaN si indéfini.
    """
    from sklearn.metrics import roc_auc_score

    row_step = int(getattr(df_oos, "attrs", {}).get("row_step", 1) or 1)
    rows = []
    for h in [int(h) for h in horizons]:
        row = {c: np.nan for c in SUMMARY_COLUMNS}
        row.update(horizon=h, n=0)
        rows.append(row)
        yc, pc, rc = f"y_{h}", f"p_up_{h}", f"ret_{h}"
        if yc not in df_oos.columns or pc not in df_oos.columns:
            continue
        y = pd.to_numeric(df_oos[yc], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        p = pd.to_numeric(df_oos[pc], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        ok = np.isfinite(y) & np.isfinite(p)
        y, p = y[ok], p[ok]
        n = int(y.size)
        row["n"] = n
        if n == 0:
            continue
        h_eff = int(math.ceil(h / row_step))
        row["n_eff"] = n / h_eff
        up = y == 1.0
        pred = p > 0.5
        acc = float(np.mean(pred == up))
        row["accuracy"] = acc
        row["up_rate"] = float(up.mean())
        n1 = int(up.sum())
        if 0 < n1 < n:
            row["balanced_accuracy"] = 0.5 * (float(pred[up].mean()) + float((~pred[~up]).mean()))
            row["auc"] = float(roc_auc_score(up, p))
        row["brier"] = float(np.mean((p - y) ** 2))
        pc_ = np.clip(p, 1e-6, 1 - 1e-6)
        row["log_loss"] = float(-np.mean(y * np.log(pc_) + (1 - y) * np.log(1 - pc_)))
        ref = row["up_rate"] * (1 - row["up_rate"])
        if ref > 0:
            row["bss"] = 1.0 - row["brier"] / ref
        if rc in df_oos.columns:
            r = pd.to_numeric(df_oos[rc], errors="coerce").to_numpy(dtype=float, na_value=np.nan)[ok]
            good = np.isfinite(r)
            tot = float(np.abs(r[good]).sum())
            if tot > 0:
                right = np.where(pred[good], r[good] > 0, r[good] < 0)
                row["acc_w"] = float(np.abs(r[good])[right].sum() / tot)
        row["p_binom"] = _binom_p_greater(acc, n / h_eff)
    out = pd.DataFrame(rows, columns=list(SUMMARY_COLUMNS))
    out["n"] = out["n"].astype("int64")
    return out
