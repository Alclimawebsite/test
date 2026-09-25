"""Tests du wrapper TimesFM (src/tradebot/forecaster.py).

Tous les tests utilisent un FAUX modèle injecté (aucun téléchargement), sauf le test
``slow`` qui charge les vrais TimesFM 3.0 et 2.5 depuis le cache Hugging Face (exécuté
seulement avec ``TRADEBOT_SLOW=1`` ; 2.5 est téléchargé s'il manque, ~0,9 Go).

1. ``prob_up`` : médiane = seuil -> 0,5 ; tous les quantiles au-dessus du seuil -> >= 0,9 ;
   monotonie ; bornes ; antisymétrie ; égalités ; cohérence avec ``np.interp``.
2. Centrage float64 : le modèle reçoit un contexte dont la dernière valeur vaut 0 ; la
   dernière valeur est rajoutée aux sorties ; le piège float32 du log-prix brut.
3. Covariables : z-score sur la fenêtre, NaN, bornage ; lots homogènes (jamais de
   mélange avec/sans covariables, ni de nombres de covariables différents).
4. Backend 2.5 : copie de la liste d'entrée, déciles = ``q[..., 1:]``, covariables refusées.
5. ``calibrate_isotonic``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradebot.config import CACHE_DIR, TimesFMConfig
from tradebot.forecaster import (QUANTILE_LEVELS, ForecastBatch, TimesFMForecaster,
                                 calibrate_isotonic, prepare_covariates, prob_up,
                                 to_log_return, transform_context)

Z9 = np.array([-1.2816, -0.8416, -0.5244, -0.2533, 0.0, 0.2533, 0.5244, 0.8416, 1.2816])
LEVELS = np.asarray(QUANTILE_LEVELS)


# -----------------------------------------------------------------------------
# Faux modèles
# -----------------------------------------------------------------------------


class FakeTimesFM3:
    """Imite ``TimesFM3Forecaster.predict_batch`` (générateur de ForecastOutput).

    Quantiles = ``offset(contexte, covariables) + scale * sqrt(pas) * z`` ; enregistre
    chaque appel pour inspection.
    """

    def __init__(self, scale: float = 1e-3, drift: float = 0.0, cov_weight: float = 0.0):
        self.scale, self.drift, self.cov_weight = scale, drift, cov_weight
        self.calls: list[dict] = []

    def predict_batch(self, contexts, horizon, past_only_covariates=None,
                      past_future_covariates=None, ts_ids=None, return_quantiles=False,
                      use_symmetric_averaging=False, **kwargs):
        self.calls.append(dict(contexts=[np.array(c) for c in contexts], horizon=horizon,
                               covariates=None if past_only_covariates is None
                               else [None if c is None else np.array(c)
                                     for c in past_only_covariates],
                               return_quantiles=return_quantiles,
                               symmetric=use_symmetric_averaging))
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        for i, c in enumerate(contexts):
            c = np.asarray(c, dtype=np.float64)
            off = self.drift * float(np.mean(c[-5:]))
            if past_only_covariates is not None and past_only_covariates[i] is not None:
                off += self.cov_weight * float(np.mean(np.asarray(past_only_covariates[i])[:, -1]))
            q = off + self.scale * steps * Z9[None, :]
            yield SimpleNamespace(ts_id=None, forecast=q[:, 4].copy(),
                                  quantiles=q if return_quantiles else None)


class FakeTimesFM25:
    """Imite ``TimesFM_2p5_200M_torch.forecast`` : modifie la liste reçue, sortie (n, h, 10)."""

    def __init__(self, scale: float = 1e-3, with_compile: bool = False):
        self.scale = scale
        self.inputs_seen: list[list[np.ndarray]] = []
        self.forecast_config = None
        self.compiled_with = []
        if with_compile:
            self.compile = self._compile

    def _compile(self, forecast_config):
        self.compiled_with.append(forecast_config)
        self.forecast_config = forecast_config

    def forecast(self, horizon, inputs):
        n = len(inputs)
        self.inputs_seen.append([np.array(x) for x in inputs])
        inputs += [np.zeros(3)] * 5            # comme le vrai : complète la liste reçue
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        dec = self.scale * steps * Z9[None, :]
        q = np.empty((n, horizon, 10))
        for i in range(n):
            q[i, :, 1:] = dec + i                 # décalage i pour vérifier l'ordre
            q[i, :, 0] = 99.0                     # « moyenne » : ne doit jamais servir
        return q[:, :, 5].copy(), q


def _btc_like(n: int = 512, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 84_000.0 * np.exp(np.cumsum(rng.normal(0.0, 4e-4, n)))


# -----------------------------------------------------------------------------
# prob_up
# -----------------------------------------------------------------------------


def _random_quantiles(n: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    center = rng.normal(0.0, 1.0, n)
    widths = rng.uniform(0.01, 1.0, (n, 9))
    return np.sort(center[:, None] + np.cumsum(widths, axis=1) - widths.sum(1, keepdims=True) / 2,
                   axis=1)


def test_prob_up_median_equal_threshold_is_half():
    q = _random_quantiles(200)
    p = prob_up(q, q[:, 4])
    np.testing.assert_allclose(p, 0.5, atol=1e-12)


def test_prob_up_all_quantiles_above_threshold():
    q = _random_quantiles(200)
    thr = q[:, 0] - 1e-9
    assert np.all(prob_up(q, thr) >= 0.9)
    assert np.all(prob_up(q, q[:, -1] + 1e-9) <= 0.1)
    # très loin : bornes [0,01 ; 0,99]
    np.testing.assert_allclose(prob_up(q, q[:, 0] - 1e6), 0.99)
    np.testing.assert_allclose(prob_up(q, q[:, -1] + 1e6), 0.01)


def test_prob_up_monotone_in_threshold_and_location():
    q = _random_quantiles(50)
    grid = np.linspace(-6, 6, 401)
    P = np.stack([prob_up(q, t) for t in grid], axis=1)        # (50, 401)
    assert np.all(np.diff(P, axis=1) <= 1e-12)                  # décroît avec le seuil
    shifts = np.linspace(-3, 3, 61)
    S = np.stack([prob_up(q + s, 0.0) for s in shifts], axis=1)
    assert np.all(np.diff(S, axis=1) >= -1e-12)                 # croît avec la position
    assert np.all((P >= 0.01) & (P <= 0.99))


def test_prob_up_sign_of_median_and_antisymmetry():
    q = _random_quantiles(300, seed=3)
    rng = np.random.default_rng(4)
    thr = q[:, 4] + rng.normal(0, 0.3, 300)
    p = prob_up(q, thr)
    assert np.array_equal(p > 0.5, q[:, 4] > thr)
    # loi reflétée : P(-X > -t) = 1 - P(X > t)
    p_ref = prob_up(-q[:, ::-1], -thr)
    np.testing.assert_allclose(p_ref, 1.0 - p, atol=1e-12)


def test_prob_up_matches_interp_reference():
    """Même résultat que la version ligne à ligne de docs/research/timesfm.md § 5."""
    q = _random_quantiles(100, seed=5)
    last = np.random.default_rng(6).normal(0, 1.5, 100)
    lo = q[:, :1] - (q[:, 1:2] - q[:, :1])
    hi = q[:, -1:] + (q[:, -1:] - q[:, -2:-1])
    xs = np.concatenate([lo, q, hi], axis=1)
    ps = np.r_[0.0, LEVELS, 1.0]
    ref = np.clip(1.0 - np.array([np.interp(t, x, ps) for t, x in zip(last, xs)]), 0.01, 0.99)
    np.testing.assert_allclose(prob_up(q, last), ref, atol=1e-12)


def test_prob_up_ties_nan_shapes():
    # distribution dégénérée au seuil -> 0,5 (égalités comptées pour moitié)
    assert prob_up(np.zeros((1, 9)), 0.0)[0] == pytest.approx(0.5)
    # 1D -> scalaire ; seuil scalaire diffusé ; NaN -> NaN
    assert np.ndim(prob_up(np.arange(1.0, 10.0), 5.0)) == 0
    q = _random_quantiles(3)
    q[1, 2] = np.nan
    p = prob_up(q, 0.0)
    assert np.isnan(p[1]) and np.isfinite(p[[0, 2]]).all()
    assert np.isnan(prob_up(_random_quantiles(2), np.array([0.0, np.nan]))[1])
    # quantiles non triés : triés en interne
    qs = _random_quantiles(5)
    np.testing.assert_allclose(prob_up(qs[:, ::-1], 0.1), prob_up(qs, 0.1))
    with pytest.raises(ValueError):
        prob_up(np.zeros((2, 8)), 0.0)


def test_prob_up_arbitrary_threshold_polymarket_style():
    """Seuil quelconque (ex. prix de départ Polymarket) : P décroît quand le seuil monte."""
    q = np.log(100.0) + 0.01 * Z9[None, :]
    start_prices = np.log(np.array([99.0, 100.0, 101.0]))
    p = prob_up(np.repeat(q, 3, axis=0), start_prices)
    assert p[0] > 0.5 and p[1] == pytest.approx(0.5) and p[2] < 0.5


# -----------------------------------------------------------------------------
# Transformations
# -----------------------------------------------------------------------------


def test_transform_context_kinds():
    p = np.array([100.0, 110.0, np.nan, 121.0])
    np.testing.assert_allclose(transform_context(p, "price"), p)
    np.testing.assert_allclose(transform_context(p, "logprice"), np.log(p))
    np.testing.assert_allclose(transform_context(p, "cumret"), np.log(p / 100.0))
    with pytest.raises(ValueError):
        transform_context(p, "returns")
    with pytest.raises(ValueError):
        transform_context(np.array([1.0, -1.0]), "logprice")
    lr = to_log_return(np.array([np.log(110.0)]), np.array([np.log(100.0)]), "logprice")
    assert lr[0] == pytest.approx(math.log(1.1))
    assert to_log_return(np.array([110.0]), np.array([100.0]), "price")[0] == pytest.approx(math.log(1.1))


# -----------------------------------------------------------------------------
# Centrage float64, sorties, options
# -----------------------------------------------------------------------------


def test_context_centered_in_float64_before_call():
    fake = FakeTimesFM3()
    fc = TimesFMForecaster(TimesFMConfig(), model=fake)
    prices = [_btc_like(512, s) for s in range(3)]
    ctxs = [transform_context(p, "logprice") for p in prices]
    batch = fc.forecast(ctxs, 15)
    (call,) = fake.calls
    for got, p in zip(call["contexts"], prices):
        assert got.dtype == np.float64
        assert got[-1] == 0.0                                   # dernière valeur centrée à 0
        np.testing.assert_allclose(got, np.log(p / p[-1]), rtol=0, atol=1e-12)
    # la dernière valeur est rajoutée : médiane du faux modèle (0) -> log(p[-1])
    np.testing.assert_allclose(batch.last_value, [np.log(p[-1]) for p in prices], rtol=0, atol=0)
    np.testing.assert_allclose(batch.median[:, 0], batch.last_value, rtol=0, atol=1e-15)
    assert batch.median.shape == (3, 15) and batch.quantiles.shape == (3, 15, 9)
    assert np.all(np.diff(batch.quantiles, axis=-1) >= 0)
    np.testing.assert_array_equal(batch.median, batch.quantiles[:, :, 4])
    # options transmises : un seul appel, horizon demandé, quantiles, moyenne symétrique
    assert call["horizon"] == 15 and call["return_quantiles"] and call["symmetric"]
    assert call["covariates"] is None


def test_float32_trap_is_avoided_by_centering():
    """Le log-prix brut perd sa variance en float32 ; le contexte centré la conserve."""
    lp = transform_context(_btc_like(512, 7), "logprice")
    true_var = np.var(lp)
    raw32 = lp.astype(np.float32)
    naive = float(np.mean(raw32 * raw32) - np.mean(raw32) ** 2)    # E[y²] − E[y]² en float32
    fake = FakeTimesFM3()
    TimesFMForecaster(TimesFMConfig(), model=fake).forecast([lp], 5)
    c32 = fake.calls[0]["contexts"][0].astype(np.float32)
    centered = float(np.mean(c32 * c32) - np.mean(c32) ** 2)
    assert abs(centered - true_var) / true_var < 1e-3
    assert abs(naive - true_var) / true_var > 0.1


def test_symmetric_averaging_can_be_disabled_and_prob_up_method():
    fake = FakeTimesFM3(scale=2e-3)
    fc = TimesFMForecaster(TimesFMConfig(), model=fake, use_symmetric_averaging=False)
    b = fc.forecast([np.log(_btc_like(300))], 15)
    assert fake.calls[0]["symmetric"] is False
    assert b.prob_up(5)[0] == pytest.approx(0.5)                 # médiane = dernière valeur
    assert b.prob_up(5, threshold=b.last_value - 1.0)[0] == pytest.approx(0.99)
    with pytest.raises(ValueError):
        b.prob_up(16)


def test_nan_handling_in_context():
    fake = FakeTimesFM3()
    fc = TimesFMForecaster(TimesFMConfig(context_transform="price"), model=fake)
    ctx = np.array([np.nan, np.nan, 1.0, 2.0, np.nan, 4.0, 5.0])
    cov = np.array([[10.0, 20.0, 1.0, 2.0, 3.0, 4.0, 5.0]])
    fc.forecast([ctx], 5, [cov])
    got = fake.calls[0]["contexts"][0]
    np.testing.assert_allclose(got, [-4.0, -3.0, -2.0, -1.0, 0.0])   # NaN initiaux retirés, interpolé
    assert fake.calls[0]["covariates"][0].shape == (1, 5)           # covariables coupées pareil
    np.testing.assert_allclose(fake.calls[0]["covariates"][0], prepare_covariates(cov[:, 2:]))
    with pytest.raises(ValueError, match="dernière valeur"):
        fc.forecast([np.array([1.0, 2.0, np.nan])], 5)
    with pytest.raises(ValueError):
        fc.forecast([np.ones(10)], 5, [np.ones((2, 9))])          # largeur incorrecte
    with pytest.raises(ValueError):
        fc.forecast([np.ones(10)], 0)


# -----------------------------------------------------------------------------
# Covariables et lots homogènes
# -----------------------------------------------------------------------------


def test_prepare_covariates_zscore_nan_clip():
    rng = np.random.default_rng(0)
    raw = np.vstack([rng.normal(50.0, 3.0, 400), np.full(400, 7.0), rng.normal(0, 1, 400)])
    raw[0, 10:13] = np.nan                     # NaN internes -> interpolés
    raw[2, :4] = np.nan                        # NaN initiaux -> 0
    raw[2, 50] = 1e6                           # valeur aberrante (z ≈ 20) -> bornée
    out = prepare_covariates(raw)
    assert out.shape == raw.shape and np.isfinite(out).all()
    ok = np.isfinite(raw[0])
    assert abs(out[0, ok].mean()) < 0.05 and abs(out[0, ok].std() - 1.0) < 0.05
    z = (raw[0] - np.nanmean(raw[0])) / np.nanstd(raw[0])
    np.testing.assert_allclose(out[0, 10:13], np.interp([10, 11, 12], [9, 13], z[[9, 13]]))
    np.testing.assert_array_equal(out[1], 0.0)                      # constante -> 0
    np.testing.assert_array_equal(out[2, :4], 0.0)
    assert out[2].max() == 10.0 and out.min() >= -10.0
    np.testing.assert_array_equal(prepare_covariates(np.full((1, 5), np.nan)), 0.0)


def test_batches_are_homogeneous_and_order_preserved():
    fake = FakeTimesFM3(cov_weight=1.0)
    fc = TimesFMForecaster(TimesFMConfig(context_transform="price"), model=fake)
    L = 64
    ctxs = [np.linspace(0, 1, L) + 10.0 * i for i in range(6)]
    rng = np.random.default_rng(0)
    covs = [None, rng.normal(size=(2, L)), None, rng.normal(size=(3, L)),
            rng.normal(size=(2, L)), np.empty((0, L))]
    b = fc.forecast(ctxs, 10, covs)
    assert len(fake.calls) == 3                                      # sans / 2 cov. / 3 cov.
    for call in fake.calls:
        cv = call["covariates"]
        if cv is None:
            continue
        assert all(c is not None for c in cv)                        # jamais de mélange
        assert len({c.shape for c in cv}) == 1                        # même nombre de covariables
    sizes = sorted(len(c["contexts"]) for c in fake.calls)
    assert sizes == [1, 2, 3]
    # ordre : chaque sortie correspond à son contexte (dernière valeur rajoutée)
    np.testing.assert_allclose(b.last_value, [c[-1] for c in ctxs])
    for i in (0, 2, 5):                    # sans covariable : médiane = dernière valeur
        assert b.median[i, 0] == pytest.approx(ctxs[i][-1])
    for i in (1, 3, 4):                    # avec : décalage = moyenne de la dernière colonne
        expected = ctxs[i][-1] + prepare_covariates(covs[i])[:, -1].mean()
        assert b.median[i, 0] == pytest.approx(expected)


# -----------------------------------------------------------------------------
# Backend 2.5
# -----------------------------------------------------------------------------


def test_timesfm25_backend_copies_inputs_and_uses_deciles():
    fake = FakeTimesFM25()
    fc = TimesFMForecaster(TimesFMConfig(backend="timesfm2p5"), model=fake)
    ctxs = [np.log(_btc_like(128, s)) for s in range(3)]
    before = [c.copy() for c in ctxs]
    b = fc.forecast(ctxs, 15)
    assert len(ctxs) == 3 and all(np.array_equal(a, c) for a, c in zip(before, ctxs))
    for seen in fake.inputs_seen[0]:
        assert seen[-1] == 0.0
    # déciles = q[..., 1:] (jamais la moyenne q[..., 0] = 99), ordre conservé
    np.testing.assert_allclose(b.median[:, 0] - b.last_value, [0.0, 1.0, 2.0], atol=1e-12)
    assert np.all(b.quantiles - b.last_value[:, None, None] < 50)
    with pytest.raises(ValueError, match="2.5"):
        fc.forecast(ctxs, 15, [np.ones((1, 128))] * 3)


def test_timesfm25_compile_config():
    fake = FakeTimesFM25(with_compile=True)
    fc = TimesFMForecaster(TimesFMConfig(backend="timesfm2p5", context_len=500), model=fake)
    fc.forecast([np.log(_btc_like(500))], 15)
    fc.forecast([np.log(_btc_like(500))], 15)
    (cfg,) = fake.compiled_with                                       # compilé une seule fois
    assert cfg.max_context == 512 and cfg.max_horizon == 128
    assert cfg.normalize_inputs and cfg.use_continuous_quantile_head
    assert cfg.force_flip_invariance and not cfg.infer_is_positive and cfg.fix_quantile_crossing
    fc.forecast([np.log(_btc_like(1000))], 15)                        # contexte plus long : recompile
    assert fake.compiled_with[-1].max_context == 1024


# -----------------------------------------------------------------------------
# Recalibration
# -----------------------------------------------------------------------------


def test_calibrate_isotonic_improves_overconfident_probabilities():
    rng = np.random.default_rng(0)
    n = 6000
    p_raw = rng.uniform(0.2, 0.8, n)
    p_true = 0.5 + (p_raw - 0.5) / 4                                # P(hausse) trop confiante
    y = (rng.uniform(size=n) < p_true).astype(float)
    tr, te = slice(0, 4000), slice(4000, None)                       # apprentissage antérieur
    f = calibrate_isotonic(p_raw[tr], y[tr])
    p_cal = f(p_raw[te])
    assert np.all((p_cal >= 0.01) & (p_cal <= 0.99))
    order = np.argsort(p_raw[te])
    assert np.all(np.diff(p_cal[order]) >= -1e-12)                   # monotone
    brier_raw = np.mean((p_raw[te] - y[te]) ** 2)
    brier_cal = np.mean((p_cal - y[te]) ** 2)
    assert brier_cal < brier_raw
    out = f(np.array([np.nan, 0.5]))
    assert np.isnan(out[0]) and np.isfinite(out[1])
    with pytest.raises(ValueError):
        calibrate_isotonic(np.full(20, 0.6), np.ones(20))           # une seule classe


def test_forecast_batch_contract_fields():
    b = ForecastBatch(median=np.zeros((2, 3)), quantiles=np.zeros((2, 3, 9)),
                      last_value=np.zeros(2))
    assert b.quantile_levels == QUANTILE_LEVELS and b.n == 2
    with pytest.raises(ValueError):
        TimesFMForecaster(TimesFMConfig(backend="timesfm9"), model=FakeTimesFM3())


# -----------------------------------------------------------------------------
# Vrai TimesFM 3.0 (lent, désactivé par défaut)
# -----------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("TRADEBOT_SLOW") != "1", reason="TRADEBOT_SLOW=1 pour lancer")
@pytest.mark.parametrize("backend", ["timesfm3", "timesfm2p5"])
def test_real_timesfm_on_btc_from_cache(backend):
    """Vrai modèle (poids du cache HF, sinon téléchargement) sur 512 barres BTC réelles."""
    path = Path(CACHE_DIR) / "BTCUSDT_1m.parquet"
    if not path.exists():
        pytest.skip("cache BTCUSDT absent")
    close = pd.read_parquet(path, columns=["close"])["close"].to_numpy(float)[-512:]
    assert np.isfinite(close).all()
    fc = TimesFMForecaster(TimesFMConfig(backend=backend, context_len=512, batch_size=8))
    ctx = transform_context(close, "logprice")
    # série réelle + sa réflexion autour du dernier prix (antisymétrie attendue)
    refl = 2 * ctx[-1] - ctx
    b = fc.forecast([ctx, refl], 15)
    assert b.median.shape == (2, 15) and b.quantiles.shape == (2, 15, 9)
    assert np.isfinite(b.quantiles).all()
    assert np.all(np.diff(b.quantiles, axis=-1) >= 0)
    assert b.last_value[0] == pytest.approx(np.log(close[-1]), abs=1e-12)
    rel = b.quantiles[0] - b.last_value[0]                         # log-rendements prévus
    assert np.all(np.abs(rel[:, 4]) < 0.01)                         # médiane proche du dernier prix
    width = rel[:, -1] - rel[:, 0]
    assert np.all(width > 0) and np.all(width < 0.05)
    assert width[14] > width[0]                                     # incertitude croissante
    p = np.array([b.prob_up(h) for h in (5, 10, 15)])
    assert np.all((p >= 0.01) & (p <= 0.99))
    np.testing.assert_allclose(p[:, 1], 1.0 - p[:, 0], atol=2e-3)   # moyenne symétrique
    # un lot plus grand : mêmes prévisions pour une série seule ou en lot
    b8 = fc.forecast([ctx] + [transform_context(close[:-k], "logprice") for k in range(1, 8)], 15)
    np.testing.assert_allclose(b8.quantiles[0], b.quantiles[0], rtol=0, atol=1e-5)
