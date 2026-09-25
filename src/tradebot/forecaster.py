"""Wrapper TimesFM (3.0 et 2.5) : prévision de quantiles et probabilité de hausse.

Deux moteurs derrière une même interface (:class:`TimesFMForecaster`) :

* ``"timesfm3"`` : TimesFM 3.0 (``timesfm3.TimesFM3Forecaster``, 330 M paramètres,
  covariables « past-only » natives). **Poids sous licence non commerciale
  (``timesfm-non-commercial-license-v1.0``) : recherche uniquement**, jamais de trading
  réel, même en compte personnel (voir ``docs/research/timesfm.md`` § 1.2).
* ``"timesfm2p5"`` : TimesFM 2.5 (``timesfm.TimesFM_2p5_200M_torch``, Apache-2.0,
  utilisable en production). Pas de covariables : XReg exige JAX et des covariables
  *futures*, ce qui ne convient pas à des indicateurs past-only.

Recommandations appliquées (``docs/research/timesfm.md`` § 5)
------------------------------------------------------------
1. **Centrage en float64** de chaque contexte avant l'appel : on soustrait la dernière
   valeur (pour ``logprice`` cela revient à ``log(p / p[-1])``), puis on la rajoute aux
   quantiles et à la médiane. Sans cela, le calcul en float32 de la variance dans la
   détendance interne de 3.0 s'effondre sur un log-prix brut (≈ 11,3) : décision de
   détendre fausse dans 27 % des fenêtres BTC.
2. Un seul appel ``horizon = max(horizons)`` ; l'horizon ``h`` se lit au pas ``h - 1``.
3. Moyenne symétrique activée par défaut (``use_symmetric_averaging`` en 3.0,
   ``force_flip_invariance`` en 2.5) : prévision exactement antisymétrique, aucun biais
   haussier appris. Coût ≈ +50 %.
4. :func:`prob_up` : fonction de répartition linéaire par morceaux entre les déciles,
   queues prolongées linéairement, bornée à [0,01 ; 0,99]. Propriété exacte :
   ``P(hausse) > 0,5`` ⇔ ``médiane > seuil``. La P(hausse) brute est trop confiante
   (Brier > 0,25) : la recalibrer avec :func:`calibrate_isotonic` sur une période
   d'apprentissage **antérieure** avant d'utiliser un seuil comme 0,55.
5. Covariables past-only : centrées et réduites sur la fenêtre de contexte uniquement
   (causal), NaN internes interpolés puis NaN restants à 0, bornées à ±10
   (:func:`prepare_covariates`).
6. **Lots homogènes** : les séries avec et sans covariables (ou avec un nombre différent
   de covariables) ne sont jamais envoyées dans le même appel ; TimesFM 3.0 remplacerait
   la covariable absente par une variate constante nulle, qui modifie la prévision.

Pièges de l'API gérés ici : ``predict_batch`` de 3.0 est un **générateur** (consommé avec
``list``) ; ``forecast`` de 2.5 **modifie la liste reçue** (on lui passe une copie).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import ROOT, TimesFMConfig

log = logging.getLogger(__name__)

QUANTILE_LEVELS: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MEDIAN_INDEX = 4
P_MIN, P_MAX = 0.01, 0.99
COV_CLIP = 10.0

MODEL_REPOS: dict[str, str] = {
    "timesfm3": "google/timesfm-3.0-pytorch",
    "timesfm2p5": "google/timesfm-2.5-200m-pytorch",
}
CONTEXT_KINDS = ("logprice", "price", "cumret")

# TimesFM 2.5 : le contexte compilé est un multiple de 32, l'horizon un multiple de 128.
_P25_INPUT_PATCH = 32
_P25_OUTPUT_PATCH = 128


# ---------------------------------------------------------------------------
# Transformations du contexte
# ---------------------------------------------------------------------------

def transform_context(prices: np.ndarray, kind: str = "logprice") -> np.ndarray:
    """Espace dans lequel TimesFM voit la série (float64, NaN conservés).

    * ``"logprice"`` : ``log(p)`` ;
    * ``"price"`` : ``p`` ;
    * ``"cumret"`` : ``log(p / p0)`` avec ``p0`` la première valeur finie.

    Le centrage sur la dernière valeur est fait ensuite par
    :meth:`TimesFMForecaster.forecast` : les trois espaces donnent presque la même
    prévision relative (RevIN interne), seul le log-prix *non centré* est piégeux.
    """
    p = np.asarray(prices, dtype=np.float64)
    if p.ndim != 1:
        raise ValueError(f"prix attendus en 1D, forme reçue {p.shape}")
    if kind == "price":
        return p.copy()
    if kind not in CONTEXT_KINDS:
        raise ValueError(f"transformation inconnue : {kind!r} (attendu : {CONTEXT_KINDS})")
    finite = np.isfinite(p)
    if np.any(p[finite] <= 0):
        raise ValueError(f"prix <= 0 incompatibles avec la transformation {kind!r}")
    with np.errstate(invalid="ignore", divide="ignore"):
        lp = np.log(p)
    if kind == "logprice":
        return lp
    if not finite.any():
        return lp
    return lp - lp[int(np.argmax(finite))]


def to_log_return(values: np.ndarray, last_value: np.ndarray, kind: str = "logprice"
                  ) -> np.ndarray:
    """Convertit des valeurs prévues (espace ``kind``) en log-rendement depuis ``last_value``."""
    v = np.asarray(values, dtype=np.float64)
    last = np.asarray(last_value, dtype=np.float64)
    if kind in ("logprice", "cumret"):
        return v - last
    if kind == "price":
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = v / last
            return np.where(ratio > 0, np.log(np.where(ratio > 0, ratio, 1.0)), np.nan)
    raise ValueError(f"transformation inconnue : {kind!r}")


def _interp_interior(row: np.ndarray) -> np.ndarray:
    """Interpolation linéaire des NaN situés entre deux valeurs finies (bords inchangés)."""
    finite = np.isfinite(row)
    if finite.all() or finite.sum() < 2:
        return row
    pos = np.flatnonzero(finite)
    lo, hi = pos[0], pos[-1]
    inner = np.arange(lo, hi + 1)
    out = row.copy()
    out[lo:hi + 1] = np.interp(inner, pos, row[pos])
    return out


def prepare_covariates(cov: np.ndarray, clip: float = COV_CLIP) -> np.ndarray:
    """Covariables past-only ``(k, L)`` prêtes pour TimesFM (float64).

    Pour chaque ligne, sur la **fenêtre de contexte seulement** (causal) : z-score avec la
    moyenne et l'écart-type des valeurs finies (ligne constante -> 0), NaN/inf internes
    interpolés linéairement, NaN restants (bords, ligne vide) mis à 0 (= la moyenne),
    puis bornage à ``±clip``. Le centrage évite aussi le piège float32 de la détendance.
    """
    a = np.asarray(cov, dtype=np.float64)
    if a.ndim == 1:
        a = a[None, :]
    if a.ndim != 2:
        raise ValueError(f"covariables attendues en (k, L), forme reçue {a.shape}")
    out = np.zeros_like(a)
    for r in range(a.shape[0]):
        row = np.where(np.isfinite(a[r]), a[r], np.nan)
        finite = np.isfinite(row)
        if not finite.any():
            continue
        mu = float(row[finite].mean())
        sd = float(row[finite].std())
        if not np.isfinite(sd) or sd <= 1e-12 * max(1.0, abs(mu)):
            z = np.where(finite, 0.0, np.nan)
        else:
            z = (row - mu) / sd
        z = _interp_interior(z)
        out[r] = np.clip(np.nan_to_num(z, nan=0.0), -clip, clip)
    return out


# ---------------------------------------------------------------------------
# Probabilité de hausse depuis les quantiles
# ---------------------------------------------------------------------------

def _pw_linear_cdf(knots: np.ndarray, probs: np.ndarray, t: np.ndarray, strict: bool
                   ) -> np.ndarray:
    """CDF linéaire par morceaux passant par (knots[:, j], probs[j]), évaluée en ``t``.

    ``strict=False`` donne la limite à droite F(t) = P(X <= t), ``strict=True`` la limite
    à gauche P(X < t) ; les deux diffèrent seulement si plusieurs nœuds sont égaux à t.
    """
    n, k = knots.shape
    below = knots < t[:, None] if strict else knots <= t[:, None]
    cnt = below.sum(axis=1)                        # nœuds à gauche de t
    j = np.clip(cnt - 1, 0, k - 2)
    rows = np.arange(n)
    x0, x1 = knots[rows, j], knots[rows, j + 1]
    p0, p1 = probs[j], probs[j + 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = np.where(x1 > x0, (t - x0) / (x1 - x0), 1.0)
    cdf = p0 + (p1 - p0) * np.clip(frac, 0.0, 1.0)
    cdf = np.where(cnt <= 0, 0.0, np.where(cnt >= k, 1.0, cdf))
    return cdf


def prob_up(quantiles_h: np.ndarray, threshold, levels: Sequence[float] = QUANTILE_LEVELS
            ) -> np.ndarray:
    """P(X_h > seuil) à partir des quantiles ``(n, len(levels))`` d'un même horizon.

    ``threshold`` : scalaire ou tableau ``(n,)`` dans le **même espace** que les quantiles.
    Par défaut on passe la dernière valeur du contexte (``ForecastBatch.last_value``, ce que
    fait :meth:`ForecastBatch.prob_up`) : c'est la probabilité de hausse. Un seuil
    quelconque sert par exemple à Polymarket (P(prix de fin >= prix de départ)).

    Construction : fonction de répartition linéaire par morceaux passant par les points
    ``(q_k, level_k)``, queues prolongées linéairement jusqu'à 0 et 1
    (``q_0 = q_1 - (q_2 - q_1)``, ``q_1.0 = q_9 + (q_9 - q_8)``), puis ``1 - F(seuil)``
    bornée à [0,01 ; 0,99]. Les égalités (plusieurs quantiles égaux au seuil) sont
    comptées pour moitié. Les quantiles sont triés ligne par ligne ; NaN -> NaN.
    Vectorisé : pas de boucle Python sur les séries.
    """
    q = np.asarray(quantiles_h, dtype=np.float64)
    single = q.ndim == 1
    if single:
        q = q[None, :]
    if q.ndim != 2:
        raise ValueError(f"quantiles attendus en (n, n_niveaux), forme reçue {q.shape}")
    lv = np.asarray(levels, dtype=np.float64)
    if q.shape[1] != lv.size or lv.size < 2:
        raise ValueError(f"{q.shape[1]} quantiles pour {lv.size} niveaux")
    if np.any(np.diff(lv) <= 0) or lv[0] <= 0 or lv[-1] >= 1:
        raise ValueError("niveaux attendus strictement croissants dans ]0, 1[")
    n = q.shape[0]
    t = np.broadcast_to(np.asarray(threshold, dtype=np.float64), (n,)).copy()
    bad = np.isnan(q).any(axis=1) | np.isnan(t)
    q = np.sort(np.where(np.isnan(q), 0.0, q), axis=1)
    t = np.where(np.isnan(t), 0.0, t)

    # Queues : on prolonge les segments extrêmes jusqu'aux probabilités 0 et 1.
    slope_lo = (q[:, 1] - q[:, 0]) / (lv[1] - lv[0])
    slope_hi = (q[:, -1] - q[:, -2]) / (lv[-1] - lv[-2])
    lo = q[:, 0] - slope_lo * lv[0]
    hi = q[:, -1] + slope_hi * (1.0 - lv[-1])
    knots = np.concatenate([lo[:, None], q, hi[:, None]], axis=1)
    probs = np.concatenate([[0.0], lv, [1.0]])

    cdf_right = _pw_linear_cdf(knots, probs, t, strict=False)
    cdf_left = _pw_linear_cdf(knots, probs, t, strict=True)
    p = np.clip(1.0 - 0.5 * (cdf_left + cdf_right), P_MIN, P_MAX)
    p[bad] = np.nan
    return p[0] if single else p


def calibrate_isotonic(p_train, y_train, clip: tuple[float, float] = (P_MIN, P_MAX)
                       ) -> Callable[[np.ndarray], np.ndarray]:
    """Fonction de recalibration monotone apprise par régression isotonique.

    À ajuster **uniquement** sur des prévisions et des labels *antérieurs* à la période
    où on l'applique (sinon fuite du futur). Paires NaN ignorées ; la fonction rendue
    conserve les NaN, borne sa sortie à ``clip`` et extrapole à plat hors du domaine vu.
    """
    from sklearn.isotonic import IsotonicRegression

    p = np.asarray(p_train, dtype=np.float64).ravel()
    y = np.asarray(y_train, dtype=np.float64).ravel()
    if p.shape != y.shape:
        raise ValueError("p_train et y_train doivent avoir la même taille")
    ok = np.isfinite(p) & np.isfinite(y)
    p, y = p[ok], y[ok]
    if p.size < 10 or np.unique(y).size < 2:
        raise ValueError("au moins 10 observations et les deux classes sont nécessaires")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("y_train doit valoir 0 ou 1")
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
    iso.fit(p, y)
    lo, hi = clip

    def recalibrate(p_new) -> np.ndarray:
        x = np.asarray(p_new, dtype=np.float64)
        flat = x.ravel()
        out = np.full(flat.shape, np.nan)
        fin = np.isfinite(flat)
        if fin.any():
            out[fin] = np.clip(iso.predict(flat[fin]), lo, hi)
        return out.reshape(x.shape)

    recalibrate.model = iso  # type: ignore[attr-defined]
    return recalibrate


# ---------------------------------------------------------------------------
# Prévision
# ---------------------------------------------------------------------------

@dataclass
class ForecastBatch:
    median: np.ndarray       # (n_series, horizon), espace du contexte transformé
    quantiles: np.ndarray    # (n_series, horizon, 9), déciles 0.1..0.9 triés
    last_value: np.ndarray   # (n_series,), dernière valeur du contexte (même espace)
    quantile_levels: tuple[float, ...] = QUANTILE_LEVELS

    @property
    def n(self) -> int:
        return int(self.median.shape[0])

    def prob_up(self, h: int, threshold=None) -> np.ndarray:
        """P(valeur à h pas > seuil) ; seuil = dernière valeur du contexte par défaut."""
        if not 1 <= h <= self.quantiles.shape[1]:
            raise ValueError(f"h={h} hors de [1, {self.quantiles.shape[1]}]")
        thr = self.last_value if threshold is None else threshold
        return prob_up(self.quantiles[:, h - 1, :], thr, self.quantile_levels)


class TimesFMForecaster:
    """Prévisions TimesFM par lots, contexte centré en float64.

    ``model`` injectable (tests, modèle déjà chargé) : un objet avec
    ``predict_batch(contexts, horizon, past_only_covariates=..., return_quantiles=...,
    use_symmetric_averaging=...)`` (API 3.0) pour ``cfg.backend == "timesfm3"``, ou
    ``forecast(horizon, inputs) -> (point, quantiles (n, h, 10))`` (API 2.5, déjà compilé
    ou muni de ``compile``) pour ``"timesfm2p5"``. Sinon les poids sont chargés depuis
    Hugging Face au premier appel : cache local d'abord (``cache_dir``, sinon cache HF par
    défaut ou cache voisin du dépôt contenant déjà les poids), téléchargement sinon
    (interdit si ``local_files_only``).
    """

    def __init__(self, cfg: TimesFMConfig = TimesFMConfig(), model=None, *,
                 use_symmetric_averaging: bool = True, cache_dir: str | None = None,
                 local_files_only: bool = False):
        if cfg.backend not in MODEL_REPOS:
            raise ValueError(f"backend inconnu : {cfg.backend!r} (attendu : {tuple(MODEL_REPOS)})")
        if cfg.context_transform not in CONTEXT_KINDS:
            raise ValueError(f"context_transform inconnu : {cfg.context_transform!r}")
        self.cfg = cfg
        self.use_symmetric_averaging = bool(use_symmetric_averaging)
        self.cache_dir = cache_dir
        self.local_files_only = local_files_only
        self._model = model
        self._compiled_for: tuple[int, int] | None = None

    # -- chargement ---------------------------------------------------------
    @property
    def model(self):
        if self._model is None:
            self._model = self._load_model()
        return self._model

    def load(self) -> TimesFMForecaster:
        """Charge les poids tout de suite (sinon : au premier appel de :meth:`forecast`)."""
        _ = self.model
        return self

    def _resolve_cache_dir(self, repo: str) -> str | None:
        """Cache HF à utiliser : celui passé, sinon le cache par défaut s'il contient déjà les
        poids, sinon un cache voisin du dépôt (``<dépôt>/.cache`` ou ``<dépôt>/../.cache``)
        qui les contient (évite de retélécharger ~1 Go quand ``HOME`` change)."""
        if self.cache_dir is not None:
            return str(self.cache_dir)
        folder = "models--" + repo.replace("/", "--")
        try:
            from huggingface_hub import constants

            if (Path(constants.HF_HUB_CACHE) / folder).exists():
                return None
        except Exception:  # pragma: no cover - huggingface_hub absent ou ancien
            return None
        for cand in (ROOT / ".cache" / "huggingface" / "hub",
                     ROOT.parent / ".cache" / "huggingface" / "hub"):
            if (cand / folder).exists():
                return str(cand)
        return None

    def _load_model(self):
        repo = MODEL_REPOS[self.cfg.backend]
        cache_dir = self._resolve_cache_dir(repo)
        if self.cfg.backend == "timesfm3":
            log.warning("TimesFM 3.0 : poids sous licence NON commerciale "
                        "(timesfm-non-commercial-license-v1.0) — recherche uniquement, "
                        "aucun usage en trading réel")
            from timesfm3 import TimesFM3Forecaster

            def load(local_only: bool):
                return TimesFM3Forecaster.from_pretrained(
                    repo, device=self.cfg.device, per_core_batch_size=self.cfg.batch_size,
                    cache_dir=cache_dir, local_files_only=local_only)
        else:
            import timesfm

            def load(local_only: bool):
                return timesfm.TimesFM_2p5_200M_torch.from_pretrained(
                    repo, torch_compile=False, cache_dir=cache_dir, local_files_only=local_only)

        # D'abord le cache local (pas de requête réseau : la vérification en ligne peut
        # prendre des minutes derrière un proxy), puis téléchargement si absent.
        try:
            model = load(True)
        except Exception as exc:
            if self.local_files_only:
                raise
            log.info("poids %s absents du cache (%s) : téléchargement", repo, type(exc).__name__)
            model = load(False)
        log.info("TimesFM %s chargé (%s, cache %s)", self.cfg.backend, repo, cache_dir or "HF")
        return model

    @property
    def quantile_levels(self) -> tuple[float, ...]:
        cfg = getattr(self._model, "config", None)
        qs = getattr(cfg, "quantiles", None)
        if qs is not None and len(qs) == len(QUANTILE_LEVELS):
            return tuple(float(x) for x in qs)
        return QUANTILE_LEVELS

    # -- API ------------------------------------------------------------------
    def forecast(self, contexts: list[np.ndarray], horizon: int,
                 covariates: list[np.ndarray | None] | None = None) -> ForecastBatch:
        """Prévoit ``horizon`` pas pour chaque contexte 1D (déjà dans l'espace transformé).

        Chaque contexte est centré en float64 sur sa dernière valeur, qui doit être finie
        (NaN initiaux retirés, NaN internes interpolés linéairement) ; cette valeur est
        rajoutée aux sorties. ``covariates[i]`` : ``None`` ou tableau ``(k, len(contexts[i]))`` dont
        la dernière colonne correspond à la dernière barre du contexte (TimesFM 3.0
        uniquement). Les séries sont regroupées par nombre de covariables : chaque groupe
        fait l'objet d'appels séparés (lots homogènes).
        """
        if horizon < 1:
            raise ValueError("horizon doit être >= 1")
        n = len(contexts)
        if n == 0:
            raise ValueError("aucun contexte")
        if covariates is not None and len(covariates) != n:
            raise ValueError(f"{len(covariates)} covariables pour {n} contextes")
        covs = list(covariates) if covariates is not None else [None] * n

        centered: list[np.ndarray] = []
        prepared: list[np.ndarray | None] = []
        last = np.empty(n)
        for i, ctx in enumerate(contexts):
            c = np.asarray(ctx, dtype=np.float64)
            if c.ndim != 1:
                raise ValueError(f"contexte {i} : 1D attendu, forme {c.shape}")
            c = np.where(np.isfinite(c), c, np.nan)
            finite = np.isfinite(c)
            if c.size == 0 or not finite[-1]:
                # prévoir depuis une barre antérieure décalerait tous les horizons
                raise ValueError(f"contexte {i} : la dernière valeur doit être finie")
            first = int(np.argmax(finite))
            last[i] = c[-1]
            c = _interp_interior(c[first:])
            centered.append(c - last[i])
            cov = covs[i]
            if cov is None:
                prepared.append(None)
                continue
            cv = np.asarray(cov, dtype=np.float64)
            if cv.ndim == 1:
                cv = cv[None, :]
            if cv.ndim != 2 or cv.shape[1] != len(ctx):
                raise ValueError(f"covariables {i} : forme {cv.shape}, attendu (k, {len(ctx)})")
            if cv.shape[0] == 0:
                prepared.append(None)
                continue
            prepared.append(prepare_covariates(cv[:, first:]))

        if any(p is not None for p in prepared) and self.cfg.backend != "timesfm3":
            raise ValueError("covariables non prises en charge par TimesFM 2.5 (XReg exige JAX "
                             "et des covariables futures) : utiliser backend='timesfm3' ou un "
                             "modèle de 2e niveau")

        median = np.empty((n, horizon))
        quant = np.empty((n, horizon, len(QUANTILE_LEVELS)))
        groups: dict[int, list[int]] = {}
        for i, p in enumerate(prepared):
            groups.setdefault(0 if p is None else p.shape[0], []).append(i)
        for k, idx in sorted(groups.items()):
            ctx_g = [centered[i] for i in idx]
            cov_g = None if k == 0 else [prepared[i] for i in idx]
            q = self._predict(ctx_g, horizon, cov_g)
            quant[idx] = q
        quant = np.sort(quant, axis=-1)
        quant += last[:, None, None]
        median[:] = quant[:, :, MEDIAN_INDEX]
        return ForecastBatch(median=median, quantiles=quant, last_value=last,
                             quantile_levels=self.quantile_levels)

    # -- moteurs ----------------------------------------------------------------
    def _predict(self, contexts: list[np.ndarray], horizon: int,
                 covariates: list[np.ndarray] | None) -> np.ndarray:
        """Quantiles centrés ``(n, horizon, 9)`` pour un lot homogène."""
        if self.cfg.backend == "timesfm3":
            return self._predict_3(contexts, horizon, covariates)
        return self._predict_2p5(contexts, horizon)

    def _predict_3(self, contexts, horizon, covariates) -> np.ndarray:
        kwargs = dict(return_quantiles=True, use_symmetric_averaging=self.use_symmetric_averaging)
        if covariates is not None:
            kwargs["past_only_covariates"] = covariates
        outs = list(self.model.predict_batch(contexts, horizon, **kwargs))   # générateur
        if len(outs) != len(contexts):
            raise RuntimeError(f"TimesFM a rendu {len(outs)} prévisions pour {len(contexts)} séries")
        q = np.stack([np.asarray(o.quantiles, dtype=np.float64) for o in outs])
        if q.shape != (len(contexts), horizon, len(QUANTILE_LEVELS)):
            raise RuntimeError(f"quantiles de forme inattendue {q.shape}")
        return q

    def _ensure_compiled_2p5(self, max_len: int, horizon: int) -> None:
        model = self.model
        if not hasattr(model, "compile"):
            return
        ctx = max(self.cfg.context_len, max_len)
        ctx = math.ceil(ctx / _P25_INPUT_PATCH) * _P25_INPUT_PATCH
        hor = math.ceil(max(horizon, _P25_OUTPUT_PATCH) / _P25_OUTPUT_PATCH) * _P25_OUTPUT_PATCH
        if (self._compiled_for is not None and ctx <= self._compiled_for[0]
                and hor <= self._compiled_for[1]):
            return
        if self._compiled_for is None and getattr(model, "forecast_config", None) is not None:
            fc = model.forecast_config             # modèle injecté déjà compilé
            if ctx <= fc.max_context and hor <= fc.max_horizon:
                self._compiled_for = (fc.max_context, fc.max_horizon)
                return
        from timesfm import ForecastConfig

        model.compile(ForecastConfig(
            max_context=ctx, max_horizon=hor, per_core_batch_size=self.cfg.batch_size,
            normalize_inputs=True, use_continuous_quantile_head=True,
            force_flip_invariance=self.use_symmetric_averaging, infer_is_positive=False,
            fix_quantile_crossing=True))
        self._compiled_for = (ctx, hor)

    def _predict_2p5(self, contexts, horizon) -> np.ndarray:
        self._ensure_compiled_2p5(max(len(c) for c in contexts), horizon)
        inputs = [np.array(c, dtype=np.float64) for c in contexts]  # copie : forecast() la modifie
        _, q = self.model.forecast(horizon=horizon, inputs=inputs)
        q = np.asarray(q, dtype=np.float64)
        if q.ndim != 3 or q.shape[0] < len(contexts) or q.shape[1] < horizon or q.shape[2] != 10:
            raise RuntimeError(f"sortie TimesFM 2.5 de forme inattendue {q.shape}")
        return q[: len(contexts), :horizon, 1:]           # [..., 0] = moyenne ; 1..9 = déciles
