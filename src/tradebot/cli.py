"""Ligne de commande : ``python -m tradebot <commande>`` (ou ``tradebot <commande>``).

Commandes
---------
* ``fetch`` : télécharge / complète le cache OHLCV 1 min (+ dérivés) et résume la couverture.
* ``study`` : **état des lieux** des indicateurs -> ``reports/etat_des_lieux/`` : scores par
  actif (IC, t robustes, hit-rate et AUC hors échantillon), agrégat entre actifs avec
  q-valeurs, top / bottom 20, synthèse par famille, modèles combinés (logit, gradient
  boosting) en walk-forward purgé comparés aux baselines, README généré. Un actif à la fois
  (mémoire : ≈ 525 000 barres × ~160 indicateurs par an et par actif).
* ``timesfm-backtest`` : TimesFM zero-shot contre les baselines -> ``reports/timesfm/``.
  Origines dans la partie **test** (après ``train_frac``) par défaut ; ``--covariates top``
  choisit les ``top-k`` indicateurs par ``ic_train_mean`` de
  ``reports/etat_des_lieux/aggregate.csv`` (apprentissage seulement) et recule le début des
  origines après la fin de l'apprentissage de l'étude si besoin (pas de fuite).
* ``predict`` : P(hausse) à 5 / 10 / 15 min depuis la dernière barre close, médiane et
  déciles prévus ; rappel de la licence non commerciale de TimesFM 3.0.
* ``polymarket-signal`` : formule exacte P(Up) = Φ(m/s) appliquée **en direct** aux marchés
  Polymarket « Up or Down » (en cours et suivant) : phase, TWAP partiel / K / moyenne finale
  Binance 1 s, σ (EWMA ou Parkinson, × k calibré sur l'historique), carnet CLOB, espérance preneur
  de chaque côté et décision,
  toutes les N secondes ; journal CSV optionnel complété par l'issue officielle (second passage).
  **Simulation papier** : aucun ordre, aucune clé (voir :mod:`tradebot.polymarket_signal`).

Chaque étape est chronométrée (journal + ``timings.csv`` / ``runs.csv``). Les fonctions
``run_*`` acceptent un ``loader`` (même signature que ``data.load_universe``) et un
``forecaster`` injectables : les tests les exécutent hors ligne sur des données synthétiques.
"""

from __future__ import annotations

import argparse
import gc
import logging
import math
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__, report
from .config import (COST_BPS, DEFAULT_INTERVAL, HORIZONS, REPORTS_DIR, ROOT,
                     StudyConfig, TimesFMConfig, benchmark_for)

log = logging.getLogger("tradebot")

STUDY_NAME = "etat_des_lieux"
TIMESFM_NAME = "timesfm"
BASELINE_NAMES: tuple[str, ...] = ("reversal_h", "momentum_h", "always_up", "majority_prev_day")
COMBINED_MODELS: tuple[str, ...] = ("logit", "hgb")
# TimesFM 3.0 a été entraîné jusqu'à 32 variates (cible + covariables) et chaque covariable
# multiplie le coût par (1 + k) : on plafonne « --covariates all ».
MAX_COVARIATES = 31
DEFAULT_TIMESFM_TICKERS: tuple[str, ...] = ("BTCUSDT", "SOLUSDT")
# Recalibration isotonique de P(hausse) (1re moitié -> 2e moitié) : labels minimum.
MIN_CALIBRATION = 300

LICENSE_TIMESFM3 = (
    "Rappel licence : les poids de TimesFM 3.0 sont sous licence NON commerciale "
    "(timesfm-non-commercial-license-v1.0). Recherche uniquement : aucun trading avec de "
    "l'argent réel, aucun usage en production. Pour un usage réel : --backend timesfm2p5 "
    "(Apache-2.0).")
RAW_PROBA_WARNING = (
    "P(hausse) brute, non recalibrée (trop confiante d'après docs/research/timesfm.md) ; "
    "les backtests donnent une accuracy proche de 50–55 %, sans avantage démontré après frais.")

Loader = Callable[..., dict]


class CliError(RuntimeError):
    """Erreur d'usage ou de données, affichée sans trace."""


# ---------------------------------------------------------------------------
# Outils communs
# ---------------------------------------------------------------------------


class StepTimer:
    """Chronomètre des étapes : journalise la durée et garde (actif, étape, secondes)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    @contextmanager
    def step(self, name: str, ticker: str = ""):
        t0 = time.perf_counter()
        log.info("[%s] %s…", ticker or "-", name)
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            self.rows.append({"ticker": ticker, "step": name, "seconds": round(dt, 3)})
            log.info("[%s] %s : %.1f s", ticker or "-", name, dt)

    def total(self, ticker: str | None = None) -> float:
        return float(sum(r["seconds"] for r in self.rows if ticker is None or r["ticker"] == ticker))

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=["ticker", "step", "seconds"])


def _default_loader() -> Loader:
    from .data import load_universe

    return load_universe


def _load_one(loader: Loader, ticker: str, *, interval: str, days: int, cache: bool,
              with_derivatives: bool) -> pd.DataFrame:
    frames = loader([ticker], interval=interval, days=days, cache=cache,
                    with_derivatives=with_derivatives)
    df = frames.get(ticker) if frames else None
    if df is None or len(df) == 0:
        raise CliError(f"{ticker} : aucune donnée chargée (voir le journal)")
    return df


class _Benchmarks:
    """Close de la référence de marché (BTCUSDT pour les altcoins), chargé une seule fois."""

    def __init__(self, loader: Loader, interval: str, days: int, cache: bool) -> None:
        self.loader, self.interval, self.days, self.cache = loader, interval, days, cache
        self._close: dict[str, pd.Series] = {}

    def put(self, ticker: str, df: pd.DataFrame) -> None:
        self._close[ticker.upper()] = df["close"].copy()

    def for_ticker(self, ticker: str) -> pd.Series | None:
        bench = benchmark_for(ticker)
        if bench is None:
            return None
        key = bench.upper()
        if key not in self._close:
            try:
                df = _load_one(self.loader, bench, interval=self.interval, days=self.days,
                               cache=self.cache, with_derivatives=False)
                self.put(bench, df)
            except Exception as exc:  # la force relative sera simplement absente
                log.warning("%s : référence %s indisponible (%s) ; indicateurs « marche » ignorés",
                            ticker, bench, exc)
                return None
        return self._close[key]


def _missing_bars(index: pd.DatetimeIndex) -> tuple[int, int]:
    """(barres attendues sur la grille régulière, barres manquantes)."""
    if len(index) < 2:
        return len(index), 0
    from .targets import bar_duration

    bar = bar_duration(index)
    expected = int(round((index[-1] - index[0]) / bar)) + 1
    return expected, max(expected - len(index), 0)


def _git_info() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except Exception:
            return ""
    return {"commit": run("rev-parse", "--short", "HEAD"),
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def _versions() -> dict:
    import scipy
    import sklearn

    return {"python": platform.python_version(), "tradebot": __version__,
            "numpy": np.__version__, "pandas": pd.__version__, "scipy": scipy.__version__,
            "sklearn": sklearn.__version__}


def _now_utc() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _command_line(argv: Sequence[str] | None) -> str:
    args = list(sys.argv[1:] if argv is None else argv)
    return "python -m tradebot " + " ".join(args)


def _read_aggregate(path: Path | str | None) -> pd.DataFrame | None:
    p = Path(path) if path is not None else REPORTS_DIR / STUDY_NAME / "aggregate.csv"
    if not p.exists():
        return None
    return pd.read_csv(p)


def _study_train_end(aggregate_path: Path | str | None) -> pd.Timestamp | None:
    """Fin (la plus tardive entre actifs) de la partie apprentissage de l'étude qui a produit
    ``aggregate.csv`` (lue dans ``run.json`` à côté). None si inconnue."""
    p = Path(aggregate_path) if aggregate_path is not None else REPORTS_DIR / STUDY_NAME / "aggregate.csv"
    meta = p.parent / "run.json"
    if not meta.exists():
        return None
    import json

    try:
        split_times = json.loads(meta.read_text(encoding="utf-8")).get("split_times", {})
        ts = [pd.Timestamp(v) for v in split_times.values() if v]
    except Exception as exc:
        log.warning("run.json illisible (%s)", exc)
        return None
    ts = [t.tz_localize("UTC") if t.tzinfo is None else t for t in ts]
    return max(ts) if ts else None


# ---------------------------------------------------------------------------
# Sélection des covariables (sur l'apprentissage seulement)
# ---------------------------------------------------------------------------


def rank_indicators_by_train_ic(aggregate: pd.DataFrame,
                                horizons: Iterable[int] | None = None) -> list[str]:
    """Indicateurs triés par |ic_train_mean| moyen sur les horizons (décroissant).

    ``ic_train_mean`` ne dépend que des parties apprentissage de l'étude : choisir sur ce
    critère n'utilise pas la période de test.
    """
    need = {"indicator", "horizon", "ic_train_mean"}
    if not need <= set(aggregate.columns):
        raise CliError(f"aggregate.csv doit contenir les colonnes {sorted(need)}")
    d = aggregate[list(need)].copy()
    if horizons is not None:
        d = d[d["horizon"].isin([int(h) for h in horizons])]
    d["abs_ic"] = pd.to_numeric(d["ic_train_mean"], errors="coerce").abs()
    d = d[np.isfinite(d["abs_ic"])]
    score = d.groupby("indicator", sort=False)["abs_ic"].mean()
    return score.sort_values(ascending=False, kind="stable").index.tolist()


def select_covariates(mode: str, *, available: Sequence[str], aggregate: pd.DataFrame | None,
                      top_k: int = 5, horizons: Iterable[int] | None = None,
                      max_covariates: int = MAX_COVARIATES) -> list[str]:
    """Noms des covariables à passer à TimesFM.

    * ``none`` : aucune ;
    * ``top`` : les ``top_k`` premiers de :func:`rank_indicators_by_train_ic` parmi
      ``available`` (calculables pour cet actif) ;
    * ``all`` : tous les ``available`` (classés par |ic_train_mean| si l'agrégat existe),
      plafonnés à ``max_covariates``.
    """
    if mode == "none":
        return []
    ranking = rank_indicators_by_train_ic(aggregate, horizons) if aggregate is not None else []
    avail = list(dict.fromkeys(available))
    if mode == "top":
        if aggregate is None:
            raise CliError("--covariates top : aggregate.csv introuvable ; lancer d'abord "
                           "`python -m tradebot study` (ou passer --aggregate)")
        if top_k < 1:
            raise CliError("--top-k doit être >= 1")
        return [x for x in ranking if x in set(avail)][:top_k]
    if mode == "all":
        ranked = [x for x in ranking if x in set(avail)]
        chosen = ranked + [x for x in avail if x not in set(ranked)]
        if len(chosen) > max_covariates:
            log.warning("--covariates all : %d indicateurs, plafonnés à %d (TimesFM 3.0 entraîné "
                        "jusqu'à 32 variates ; coût × (1 + k))", len(chosen), max_covariates)
            chosen = chosen[:max_covariates]
        return chosen
    raise CliError(f"--covariates inconnu : {mode!r} (none | top | all)")


def _compute_covariates(df: pd.DataFrame, mode: str, *, aggregate: pd.DataFrame | None,
                        top_k: int, horizons: Sequence[int], benchmark: pd.Series | None,
                        max_covariates: int) -> pd.DataFrame | None:
    """Indicateurs retenus comme covariables past-only (valeur en t calculée sur <= t)."""
    from .indicators import REGISTRY, compute_indicators

    if mode == "none":
        return None
    if mode == "top":
        ranking = select_covariates("top", available=list(REGISTRY), aggregate=aggregate,
                                    top_k=max(3 * top_k, top_k + 10), horizons=horizons)
        feats = compute_indicators(df, names=ranking, benchmark=benchmark)
    else:
        feats = compute_indicators(df, benchmark=benchmark)
    usable = [c for c in feats.columns if feats[c].notna().any()]
    chosen = select_covariates(mode, available=usable, aggregate=aggregate, top_k=top_k,
                               horizons=horizons, max_covariates=max_covariates)
    if not chosen:
        raise CliError("aucune covariable calculable pour cet actif")
    return feats[chosen]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def run_fetch(tickers: Sequence[str], *, interval: str = DEFAULT_INTERVAL, days: int = 365,
              derivatives: bool = True, cache: bool = True, loader: Loader | None = None
              ) -> pd.DataFrame:
    """Charge (et met en cache) chaque actif ; rend un résumé de couverture par actif."""
    loader = loader or _default_loader()
    rows = []
    for ticker in tickers:
        t0 = time.perf_counter()
        frames = loader([ticker], interval=interval, days=days, cache=cache,
                        with_derivatives=derivatives)
        dt = time.perf_counter() - t0
        df = frames.get(ticker) if frames else None
        if df is None or len(df) == 0:
            log.error("%s : aucune donnée", ticker)
            rows.append({"ticker": ticker, "n_bars": 0, "seconds": round(dt, 1)})
            continue
        expected, missing = _missing_bars(df.index)
        row = {"ticker": ticker, "n_bars": len(df), "start": df.index[0], "end": df.index[-1],
               "missing_bars": missing, "missing_pct": missing / max(expected, 1)}
        for c in ("open_interest", "funding_rate"):
            if c in df.columns:
                row[f"{c}_coverage"] = float(df[c].notna().mean())
        row["seconds"] = round(dt, 1)
        rows.append(row)
        log.info("%s : %d barres (%s → %s), %d manquantes, %.1f s", ticker, len(df),
                 df.index[0], df.index[-1], missing, dt)
        del df, frames
        gc.collect()
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# study
# ---------------------------------------------------------------------------


def _combined_and_baselines(ticker: str, df: pd.DataFrame, feats: pd.DataFrame,
                            tg: pd.DataFrame, horizons: Sequence[int], *, n_splits: int,
                            row_step: int, max_train_rows: int | None, timer: StepTimer
                            ) -> tuple[list[pd.DataFrame], list[dict]]:
    from .backtest import baseline_predictions
    from .evaluation import combined_model_walkforward, direction_summary

    summaries: list[pd.DataFrame] = []
    folds: list[dict] = []
    oos_index = None
    oos_ref = None
    for model in COMBINED_MODELS:
        with timer.step(f"modèle combiné {model}", ticker):
            oos = combined_model_walkforward(feats, tg, horizons, n_splits=n_splits, model=model,
                                             max_train_rows=max_train_rows, row_step=row_step)
        s = direction_summary(oos, horizons)
        s.insert(0, "model", model)
        s.insert(0, "ticker", ticker)
        summaries.append(s)
        for f in oos.attrs.get("folds", []):
            folds.append({"ticker": ticker, "model": model, **f})
        if oos_index is None and len(oos):
            oos_index, oos_ref = oos.index, oos
    if oos_index is None:
        return summaries, folds
    with timer.step("baselines (mêmes barres)", ticker):
        base = baseline_predictions(df, oos_index, horizons)
        for name in BASELINE_NAMES:
            frame = pd.DataFrame(index=oos_index)
            for h in horizons:
                b = base[base["horizon"] == h].set_index("time")[name]
                frame[f"p_up_{h}"] = b.reindex(oos_index).to_numpy()
                frame[f"y_{h}"] = oos_ref[f"y_{h}"].to_numpy()
                frame[f"ret_{h}"] = oos_ref[f"ret_{h}"].to_numpy()
            frame.attrs["row_step"] = oos_ref.attrs.get("row_step", 1)
            s = direction_summary(frame, horizons)
            s.insert(0, "model", name)
            s.insert(0, "ticker", ticker)
            summaries.append(s)
    return summaries, folds


def run_study(tickers: Sequence[str], *, interval: str = DEFAULT_INTERVAL, days: int = 365,
              horizons: Sequence[int] = HORIZONS, train_frac: float = 0.6,
              combined: bool = True, n_splits: int = 5, row_step: int = 5,
              max_train_rows: int | None = 100_000, cache: bool = True,
              out_dir: Path | str | None = None, loader: Loader | None = None,
              command: str = "") -> dict:
    """État des lieux des indicateurs ; écrit ``reports/etat_des_lieux/`` (ou ``out_dir``).

    Pour chaque actif, **un à la fois** : chargement (OHLCV + dérivés), indicateurs (avec
    BTCUSDT comme référence pour les altcoins), cibles, ``evaluation.indicator_scores``,
    puis (sauf ``combined=False``) modèles combinés logit et hgb en walk-forward purgé et
    baselines sur les mêmes barres. Ensuite agrégat entre actifs, synthèses, graphiques et
    README. Rend un dict (chemins, agrégat, temps).
    """
    from .evaluation import aggregate_scores, indicator_scores
    from .indicators import compute_indicators, list_indicators
    from .targets import make_targets

    t_start = time.perf_counter()
    loader = loader or _default_loader()
    horizons = tuple(int(h) for h in horizons)
    out = Path(out_dir) if out_dir is not None else report.report_dir(STUDY_NAME)
    out.mkdir(parents=True, exist_ok=True)
    timer = StepTimer()
    benches = _Benchmarks(loader, interval, days, cache)
    # les références (BTC) d'abord : leur close resservira aux altcoins sans rechargement
    bench_set = {benchmark_for(t) for t in tickers} - {None}
    order = sorted(dict.fromkeys(tickers), key=lambda t: 0 if t in bench_set else 1)

    per_ticker: dict[str, pd.DataFrame] = {}
    data_rows: list[dict] = []
    summaries: list[pd.DataFrame] = []
    fold_rows: list[dict] = []
    split_times: dict[str, str] = {}
    for ticker in order:
        try:
            with timer.step("chargement", ticker):
                df = _load_one(loader, ticker, interval=interval, days=days, cache=cache,
                               with_derivatives=True)
        except Exception as exc:
            log.error("%s : chargement impossible (%s) ; actif ignoré", ticker, exc)
            continue
        if ticker.upper() in bench_set:
            benches.put(ticker, df)
        with timer.step("référence de marché", ticker):
            bench = benches.for_ticker(ticker)
        with timer.step("indicateurs", ticker):
            feats = compute_indicators(df, benchmark=bench)
        with timer.step("cibles", ticker):
            tg = make_targets(df, horizons)
        with timer.step("scores des indicateurs", ticker):
            sc = indicator_scores(feats, tg, horizons, train_frac)
        per_ticker[ticker] = sc
        report.write_csv(sc.assign(ticker=ticker)[["ticker", *sc.columns]],
                         out / f"scores_{ticker}.csv")
        split = sc.attrs.get("split_time")
        split_times[ticker] = None if split is None else str(split)
        expected, missing = _missing_bars(df.index)
        data_rows.append({
            "ticker": ticker, "n_bars": len(df), "start": df.index[0], "end": df.index[-1],
            "missing_bars": missing, "split_time": split, "n_indicators": feats.shape[1],
            "n_skipped": len(feats.attrs.get("skipped", {})),
            "benchmark": benchmark_for(ticker) if bench is not None else None,
            "deriv_coverage": float(df["open_interest"].notna().mean())
            if "open_interest" in df.columns else float("nan")})
        if combined:
            s, f = _combined_and_baselines(ticker, df, feats, tg, horizons, n_splits=n_splits,
                                           row_step=row_step, max_train_rows=max_train_rows,
                                           timer=timer)
            summaries += s
            fold_rows += f
        del df, feats, tg
        gc.collect()
        log.info("[%s] actif terminé en %.1f s", ticker, timer.total(ticker))

    if not per_ticker:
        raise CliError("aucun actif n'a pu être étudié")
    with timer.step("agrégat et rapport"):
        agg = aggregate_scores(per_ticker)
        report.write_csv(agg, out / "aggregate.csv")
        top, bottom = report.top_bottom(agg, 20)
        report.write_csv(top, out / "top_20.csv")
        report.write_csv(bottom, out / "bottom_20.csv")
        fam = report.family_summary(agg)
        report.write_csv(fam, out / "familles.csv")
        report.write_csv(list_indicators(), out / "catalogue_indicateurs.csv")
        data_table = pd.DataFrame(data_rows)
        report.write_csv(data_table, out / "donnees.csv")
        comb = pd.concat(summaries, ignore_index=True) if summaries else None
        if comb is not None:
            report.write_csv(comb, out / "combined_summary.csv")
            report.write_csv(pd.DataFrame(fold_rows), out / "combined_folds.csv")
        figures: dict[str, str] = {}
        try:
            report.plot_ic_heatmap(agg, out / "ic_heatmap.png", top=40,
                                   subtitle=f"Top 40 indicateurs par |IC moyen| sur {len(per_ticker)} "
                                            f"actif(s), {days} jours ; * : q de Benjamini-Yekutieli < 5 %")
            figures["ic_heatmap"] = "ic_heatmap.png"
        except ValueError as exc:
            log.warning("carte de chaleur non produite (%s)", exc)
        if len(fam):
            report.plot_family_bars(fam, out / "familles.png")
            figures["familles"] = "familles.png"
        if comb is not None and len(comb):
            models = [*COMBINED_MODELS, "reversal_h", "momentum_h", "always_up"]
            title = report.verdict_vs_baselines(comb, COMBINED_MODELS, BASELINE_NAMES,
                                                "Le modèle combiné", facet_col="ticker")
            report.plot_excess_accuracy(
                comb, out / "modeles_combines.png", models=models, facet_col="ticker",
                label_models=("logit", "hgb"), title=title,
                subtitle=f"Walk-forward purgé ({n_splits} plis), accuracy − 50 % ; baselines sur "
                         "les mêmes barres ; bande grise : ± 1,96 × 0,5 / √n_eff")
            figures["modeles_combines"] = "modeles_combines.png"
    timings = timer.frame()
    total = time.perf_counter() - t_start
    timings = pd.concat([timings, pd.DataFrame([{"ticker": "", "step": "total",
                                                 "seconds": round(total, 3)}])],
                        ignore_index=True)
    report.write_csv(timings, out / "timings.csv")
    params = dict(tickers=list(per_ticker), interval=interval, days=days, horizons=list(horizons),
                  train_frac=train_frac, combined=combined, n_splits=n_splits, row_step=row_step,
                  max_train_rows=max_train_rows)
    meta = dict(command=command, generated_at=f"{_now_utc():%Y-%m-%d %H:%M} UTC", params=params,
                split_times=split_times, git=_git_info(), versions=_versions(),
                total_seconds=round(total, 1))
    report.write_json(out / "run.json", meta)
    readme = report.render_study_readme(
        meta=meta, data_table=data_table, agg=agg, top=top, bottom=bottom, fam=fam,
        combined=comb, timings=timings, figures=figures)
    report.write_text(out / "README.md", readme)
    log.info("état des lieux écrit dans %s (%.1f s)", out, total)
    return dict(out_dir=out, aggregate=agg, per_ticker=per_ticker, combined=comb,
                timings=timings, meta=meta)


# ---------------------------------------------------------------------------
# timesfm-backtest
# ---------------------------------------------------------------------------


def _make_forecaster(backend: str, context_len: int, transform: str, batch_size: int,
                     symmetric: bool):
    from .forecaster import TimesFMForecaster

    cfg = TimesFMConfig(backend=backend, context_len=context_len, batch_size=batch_size,
                        context_transform=transform)
    return TimesFMForecaster(cfg, use_symmetric_averaging=symmetric)


def _past_return(close: pd.Series, times: pd.DatetimeIndex, lag: pd.Timedelta) -> np.ndarray:
    """log(close[t] / close[t − lag]) aux instants ``times`` (NaN si une des barres manque)."""
    lc = np.log(close.astype(float))
    now = lc.reindex(times).to_numpy(dtype=float)
    past = lc.reindex(times - lag).to_numpy(dtype=float)
    return now - past


def _rank(x: np.ndarray) -> np.ndarray:
    return pd.Series(x).rank().to_numpy(dtype=float)


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 10:
        return float("nan")
    ra, rb = _rank(a[ok]), _rank(b[ok])
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def _partial_spearman(a: np.ndarray, b: np.ndarray, controls: Sequence[np.ndarray]) -> float:
    """Corrélation des rangs de a et b après régression (MCO) sur les rangs des contrôles."""
    ok = np.isfinite(a) & np.isfinite(b)
    for c in controls:
        ok &= np.isfinite(c)
    if ok.sum() < 10 + len(controls):
        return float("nan")
    Z = np.column_stack([np.ones(int(ok.sum()))] + [_rank(c[ok]) for c in controls])
    ra, rb = _rank(a[ok]), _rank(b[ok])
    ea = ra - Z @ np.linalg.lstsq(Z, ra, rcond=None)[0]
    eb = rb - Z @ np.linalg.lstsq(Z, rb, rcond=None)[0]
    if ea.std() == 0 or eb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ea, eb)[0, 1])


def _acc_w(y: np.ndarray, p: np.ndarray, r: np.ndarray) -> float:
    """Accuracy pondérée par |rendement| (méthodologie § 8.6)."""
    ok = np.isfinite(y) & np.isfinite(p) & np.isfinite(r)
    w = np.abs(r[ok])
    if w.sum() <= 0:
        return float("nan")
    right = (p[ok] > 0.5) == (y[ok] == 1.0)
    return float(w[right].sum() / w.sum())


def _dm_brier_t(y: np.ndarray, p: np.ndarray, p_ref: np.ndarray, lags: int) -> float:
    """Diebold-Mariano sur le Brier (modèle − référence), t Newey-West ; < 0 = modèle meilleur."""
    from .evaluation import newey_west_tstat

    ok = np.isfinite(y) & np.isfinite(p) & np.isfinite(p_ref)
    if ok.sum() < 10:
        return float("nan")
    d = (p[ok] - y[ok]) ** 2 - (p_ref[ok] - y[ok]) ** 2
    if np.allclose(d, d[0]):
        return float("nan")
    return float(newey_west_tstat(d, np.ones_like(d), lags))


def _calibrate_second_half(preds: pd.DataFrame, bar: pd.Timedelta,
                           min_fit: int = MIN_CALIBRATION) -> pd.Series:
    """P(hausse) recalibrée (isotonique) sur la 2e moitié des origines, apprise sur la 1re
    moitié (purgée de h barres : seules les cibles connues avant la 2e moitié servent).

    Rien (NaN) si la 1re moitié compte moins de ``min_fit`` labels : une régression
    isotonique sur une centaine de points se réduit au taux de base de la période."""
    from .forecaster import calibrate_isotonic

    out = pd.Series(np.nan, index=preds.index)
    times = pd.DatetimeIndex(preds["time"].unique()).sort_values()
    if len(times) < 20:
        return out
    t_mid = times[len(times) // 2]
    for h, g in preds.groupby("horizon"):
        fit = g[pd.DatetimeIndex(g["time"]) + (int(h) + 1) * bar <= t_mid]
        app = g[pd.DatetimeIndex(g["time"]) >= t_mid]
        n_fit = int(fit["y_true"].notna().sum())
        if n_fit < min_fit:
            log.info("recalibration non faite à h=%s : %d labels d'apprentissage (< %d)", h,
                     n_fit, min_fit)
            continue
        try:
            f = calibrate_isotonic(fit["p_up"].to_numpy(), fit["y_true"].to_numpy())
        except ValueError as exc:
            log.warning("recalibration impossible à h=%s (%s)", h, exc)
            continue
        out.loc[app.index] = f(app["p_up"].to_numpy())
    return out


def _timesfm_metrics(preds: pd.DataFrame, stride: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(métriques de direction par modèle × horizon, diagnostics TimesFM par horizon)."""
    from .backtest import metrics_by_horizon

    rows = []
    specs = [("timesfm", "p_up", "toutes", preds)]
    cal = preds[np.isfinite(preds["p_up_cal"].to_numpy(dtype=float))]
    if len(cal):
        specs.append(("timesfm_cal", "p_up_cal", "2e moitié", cal))
    specs += [(b, b, "toutes", preds) for b in BASELINE_NAMES]
    for model, col, sample, d in specs:
        m = metrics_by_horizon(d, stride=stride, p_col=col)
        for _, r in m.iterrows():
            g = d[d["horizon"] == r["horizon"]]
            y, p = g["y_true"].to_numpy(float), g[col].to_numpy(float)
            extra = dict(acc_w=_acc_w(y, p, g["ret"].to_numpy(float)))
            if model != "reversal_h":
                extra["dm_brier_vs_reversal_t"] = _dm_brier_t(
                    y, p, g["reversal_h"].to_numpy(float), 2 * int(r["h_eff"]))
            rows.append({"model": model, "sample": sample, **r.to_dict(), **extra})
    metrics = pd.DataFrame(rows)
    for c in ("horizon", "h_eff", "n", "n_eff"):
        metrics[c] = metrics[c].astype(int)

    diag = []
    for h, g in preds.groupby("horizon"):
        r = g["ret"].to_numpy(float)
        ok = np.isfinite(r)
        q10, q90 = g["q10_ret"].to_numpy(float), g["q90_ret"].to_numpy(float)
        pr = g["pred_ret"].to_numpy(float)
        p = g["p_up"].to_numpy(float)
        diag.append(dict(
            horizon=int(h), n=int(ok.sum()),
            coverage_q10_q90=float(np.mean((r[ok] >= q10[ok]) & (r[ok] <= q90[ok])))
            if ok.any() else float("nan"),
            rho_past_60=_spearman(p, g["past_ret_60"].to_numpy(float)),
            ic_p_up=_spearman(p, r),
            ic_partial=_partial_spearman(p, r, [g["past_ret_60"].to_numpy(float),
                                                g["past_ret_h"].to_numpy(float)]),
            median_abs_pred_bps=float(np.nanmedian(np.abs(pr)) * 1e4),
            median_abs_ret_bps=float(np.nanmedian(np.abs(r)) * 1e4) if ok.any() else float("nan")))
    return metrics, pd.DataFrame(diag)


def run_timesfm_backtest(tickers: Sequence[str], *, interval: str = DEFAULT_INTERVAL,
                         days: int = 365, horizons: Sequence[int] = HORIZONS,
                         train_frac: float = 0.6, covariates: str = "none", top_k: int = 5,
                         stride: int = 15, context_len: int = 512, backend: str = "timesfm3",
                         max_origins: int | None = 1000, transform: str = "logprice",
                         cost_bps: float = COST_BPS["futures_taker"], threshold: float = 0.55,
                         batch_size: int = 32, start=None, end=None, symmetric: bool = True,
                         max_covariates: int = MAX_COVARIATES,
                         aggregate_path: Path | str | None = None, cache: bool = True,
                         out_dir: Path | str | None = None, loader: Loader | None = None,
                         forecaster=None, command: str = "") -> pd.DataFrame:
    """Walk-forward TimesFM contre les baselines ; écrit ``reports/timesfm/`` (ou ``out_dir``).

    Origines : grille alignée sur l'horloge tous les ``stride`` barres, dans la partie **test**
    de chaque actif (après ``round(train_frac × n)`` barres, même coupure que
    ``evaluation.indicator_scores``) sauf ``start`` explicite ; ``max_origins`` origines au plus,
    réparties régulièrement. Avec des covariables choisies dans ``aggregate.csv``, le début est
    reculé si besoin après la fin de l'apprentissage de l'étude (``run.json``).

    Par actif : prévisions + baselines (``{run}_predictions.csv``), métriques de direction avec
    ``h_eff = ceil(h / stride)`` (``{run}_metrics.csv``), diagnostics (couverture des
    déciles, corrélation au rendement passé, IC partiel ; ``{run}_diagnostics.csv``),
    mini-stratégie (``{run}_strategy.csv``) sur P(hausse) brute et recalibrée, graphique
    ``{run}_accuracy.png``. ``runs.csv`` et ``README.md`` sont mis à jour. Rend les lignes
    de ``runs.csv`` de cet appel.
    """
    from .backtest import baseline_predictions, toy_strategy, walk_forward_timesfm
    from .targets import bar_duration

    if covariates not in ("none", "top", "all"):
        raise CliError(f"--covariates inconnu : {covariates!r}")
    if covariates != "none" and backend != "timesfm3":
        raise CliError("les covariables exigent --backend timesfm3 (TimesFM 2.5 : XReg exige JAX "
                       "et des covariables futures)")
    horizons = tuple(sorted({int(h) for h in horizons}))
    loader = loader or _default_loader()
    out = Path(out_dir) if out_dir is not None else report.report_dir(TIMESFM_NAME)
    out.mkdir(parents=True, exist_ok=True)
    timer = StepTimer()
    aggregate = _read_aggregate(aggregate_path) if covariates != "none" else None
    study_train_end = _study_train_end(aggregate_path) if aggregate is not None else None
    benches = _Benchmarks(loader, interval, days, cache)
    if backend == "timesfm3":
        log.warning(LICENSE_TIMESFM3)

    fc = forecaster
    run_rows = []
    for ticker in tickers:
        with timer.step("chargement", ticker):
            df = _load_one(loader, ticker, interval=interval, days=days, cache=cache,
                           with_derivatives=covariates != "none")
        bar = bar_duration(df.index)
        n = len(df)
        split_row = min(max(int(round(train_frac * n)), 0), n - 1)
        test_start = df.index[split_row]
        start_ts = pd.Timestamp(start) if start is not None else test_start
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        if study_train_end is not None and start_ts < study_train_end:
            log.warning("[%s] début des origines reculé de %s à %s : fin de l'apprentissage de "
                        "l'étude qui a choisi les covariables (pas de fuite)", ticker, start_ts,
                        study_train_end)
            start_ts = study_train_end
        cov_df = None
        with timer.step("covariables", ticker):
            if covariates != "none":
                bench = benches.for_ticker(ticker)
                cov_df = _compute_covariates(df, covariates, aggregate=aggregate, top_k=top_k,
                                             horizons=horizons, benchmark=bench,
                                             max_covariates=max_covariates)
                log.info("[%s] covariables : %s", ticker, ", ".join(cov_df.columns))
        if fc is None:
            with timer.step("chargement TimesFM"):
                fc = _make_forecaster(backend, context_len, transform, batch_size, symmetric).load()
        with timer.step("walk-forward TimesFM", ticker):
            preds = walk_forward_timesfm(df, fc, horizons, context_len=context_len, stride=stride,
                                         covariates=cov_df, start=start_ts, end=end,
                                         max_origins=max_origins)
        if len(preds) == 0:
            log.error("[%s] aucune origine (période test trop courte ?) ; actif ignoré", ticker)
            continue
        wf_attrs = dict(preds.attrs)
        with timer.step("baselines et métriques", ticker):
            base = baseline_predictions(df, pd.DatetimeIndex(preds["time"].unique()), horizons)
            preds = preds.merge(base[["time", "horizon", *BASELINE_NAMES]],
                                on=["time", "horizon"], how="left")
            times = pd.DatetimeIndex(preds["time"])
            preds["past_ret_60"] = _past_return(df["close"], times, 60 * bar)
            preds["past_ret_h"] = np.nan
            for h in horizons:
                m = (preds["horizon"] == h).to_numpy()
                preds.loc[m, "past_ret_h"] = _past_return(df["close"], times[m], h * bar)
            preds["p_up_cal"] = _calibrate_second_half(preds, bar)
            metrics, diag = _timesfm_metrics(preds, stride)
            diag["frac_pred_gt_cost"] = [
                float(np.mean(np.abs(g["pred_ret"].to_numpy(float)) * 1e4 > cost_bps))
                for _, g in preds.groupby("horizon")]
            strat = []
            for h in horizons:
                for signal, col, thr, d in (("timesfm", "p_up", threshold, preds),
                                            ("timesfm_cal", "p_up_cal", threshold,
                                             preds[np.isfinite(preds["p_up_cal"])]),
                                            ("reversal_h", "reversal_h", 0.5, preds)):
                    if len(d) == 0:
                        continue
                    strat.append({"signal": signal, **toy_strategy(d, h, thr, cost_bps,
                                                                   p_col=col, bar=bar)})
            strat = pd.DataFrame(strat)
        cov_tag = "none" if covariates == "none" else (f"top{top_k}" if covariates == "top"
                                                       else "all")
        run_id = f"{ticker}_{backend}_{cov_tag}_s{stride}_c{context_len}_{transform}"
        cols = ["time", "horizon", "last_close", "pred_median", "p_up", "p_up_cal", "ret",
                "y_true", "pred_ret", "q10_ret", "q90_ret", *BASELINE_NAMES, "past_ret_60"]
        report.write_csv(preds[cols], out / f"{run_id}_predictions.csv")
        report.write_csv(metrics, out / f"{run_id}_metrics.csv")
        report.write_csv(diag, out / f"{run_id}_diagnostics.csv")
        report.write_csv(strat, out / f"{run_id}_strategy.csv")
        chart_df = metrics[metrics["sample"] == "toutes"]
        title = report.verdict_vs_baselines(chart_df, ["timesfm"], BASELINE_NAMES,
                                            f"TimesFM ({ticker})")
        report.plot_excess_accuracy(
            chart_df, out / f"{run_id}_accuracy.png",
            models=["timesfm", *BASELINE_NAMES], label_models=("timesfm",), title=title,
            subtitle=f"{wf_attrs.get('n_origins')} origines (pas {stride} min), backend {backend}, "
                     f"covariables : {cov_tag} ; accuracy − 50 % ; bande grise : bruit ± 1,96 × 0,5 / √n_eff")
        wf_s = next((r["seconds"] for r in reversed(timer.rows)
                     if r["ticker"] == ticker and r["step"] == "walk-forward TimesFM"), float("nan"))
        n_or = int(wf_attrs.get("n_origins", 0))
        run_rows.append(dict(
            run=run_id, ticker=ticker, generated_at=f"{_now_utc():%Y-%m-%d %H:%M} UTC", command=command,
            backend=backend, covariates=cov_tag,
            covariate_names=";".join(cov_df.columns) if cov_df is not None else "",
            stride=stride, context_len=context_len, transform=transform,
            symmetric=symmetric, max_origins=max_origins, n_candidates=wf_attrs.get("n_candidates"),
            n_excluded_gaps=wf_attrs.get("n_excluded_gaps"), n_origins=n_or,
            first_origin=preds["time"].min(), last_origin=preds["time"].max(),
            test_part_start=test_start, days=days, train_frac=train_frac, cost_bps=cost_bps,
            threshold=threshold, walk_forward_s=round(wf_s, 1),
            origins_per_s=round(n_or / wf_s, 2) if wf_s and wf_s > 0 else float("nan")))
        del df, preds, cov_df
        gc.collect()

    runs = pd.DataFrame(run_rows)
    runs_path = out / "runs.csv"
    if runs_path.exists() and len(runs):
        old = pd.read_csv(runs_path)
        old = old[~old["run"].isin(runs["run"])]
        all_runs = pd.concat([old, runs], ignore_index=True)
    else:
        all_runs = runs if len(runs) else (pd.read_csv(runs_path) if runs_path.exists()
                                           else runs)
    if len(all_runs):
        report.write_csv(all_runs, runs_path)
        report.write_text(out / "README.md", report.render_timesfm_readme(out, all_runs))
    report.write_csv(timer.frame(), out / "timings_last_run.csv")
    return runs


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def run_predict(ticker: str, *, interval: str = DEFAULT_INTERVAL,
                horizons: Sequence[int] = HORIZONS, covariates: str = "none", top_k: int = 5,
                context_len: int = 512, backend: str = "timesfm3", transform: str = "logprice",
                days: int | None = None, batch_size: int = 32, symmetric: bool = True,
                max_covariates: int = MAX_COVARIATES, aggregate_path: Path | str | None = None,
                cache: bool = True, loader: Loader | None = None, forecaster=None,
                stream=None) -> pd.DataFrame:
    """P(hausse) à chaque horizon depuis la **dernière barre close** ; affiche un résumé.

    Rend un DataFrame (une ligne par horizon) : ``origin`` (ouverture de la dernière barre),
    ``target_close_time`` (clôture de la barre visée), ``last_close``, ``p_up``,
    ``pred_median``, ``pred_ret_bps``, ``q10``, ``q90`` (prix).
    """
    from .backtest import walk_forward_timesfm
    from .targets import bar_duration

    stream = stream if stream is not None else sys.stdout
    if covariates != "none" and backend != "timesfm3":
        raise CliError("les covariables exigent --backend timesfm3")
    horizons = tuple(sorted({int(h) for h in horizons}))
    loader = loader or _default_loader()
    if days is None:
        # contexte (+ marge) ; avec covariables : échauffement des indicateurs (profil 7 jours)
        days = max(2, math.ceil(context_len / 1440) + 1) if covariates == "none" else 10
    df = _load_one(loader, ticker, interval=interval, days=days, cache=cache,
                   with_derivatives=covariates != "none")
    bar = bar_duration(df.index)
    cov_df = None
    if covariates != "none":
        aggregate = _read_aggregate(aggregate_path)
        benches = _Benchmarks(loader, interval, days, cache)
        cov_df = _compute_covariates(df, covariates, aggregate=aggregate, top_k=top_k,
                                     horizons=horizons, benchmark=benches.for_ticker(ticker),
                                     max_covariates=max_covariates)
    fc = forecaster or _make_forecaster(backend, context_len, transform, batch_size, symmetric)
    last = df.index[-1]
    preds = walk_forward_timesfm(df, fc, horizons, context_len=context_len, stride=1,
                                 covariates=cov_df, start=last, end=last)
    if len(preds) == 0:
        raise CliError(f"{ticker} : contexte incomplet avant {last} (trou de plus de 3 barres "
                       f"ou moins de {context_len} barres)")
    q10 = preds["last_close"] * np.exp(preds["q10_ret"])
    q90 = preds["last_close"] * np.exp(preds["q90_ret"])
    res = pd.DataFrame({
        "ticker": ticker, "horizon": preds["horizon"].astype(int),
        "origin": preds["time"], "target_close_time": preds["time"] + (preds["horizon"] + 1) * bar,
        "last_close": preds["last_close"], "p_up": preds["p_up"],
        "pred_median": preds["pred_median"], "pred_ret_bps": preds["pred_ret"] * 1e4,
        "q10": q10, "q90": q90})
    age = _now_utc() - (last + bar)
    lines = [f"{ticker} — dernière barre close : {last:%Y-%m-%d %H:%M} UTC (clôture "
             f"{last + bar:%H:%M}), prix {fmt_price(float(preds['last_close'].iloc[0]))}"]
    if age > pd.Timedelta(minutes=5):
        lines.append(f"ATTENTION : dernière barre en retard de {_fmt_duration(age)} sur l'heure "
                     "actuelle (cache ou source à jour ?).")
    lines.append(f"Backend {backend}, contexte {context_len} barres ({transform}), covariables : "
                 f"{', '.join(cov_df.columns) if cov_df is not None else 'aucune'}")
    lines.append("")
    table = pd.DataFrame({
        "horizon": [f"{h} min" for h in res["horizon"]],
        "échéance (clôture UTC)": [f"{t:%H:%M}" for t in res["target_close_time"]],
        "P(hausse)": [report.fmt_number(p, 1, pct=True) for p in res["p_up"]],
        "médiane prévue": [fmt_price(v) for v in res["pred_median"]],
        "écart (pb)": [report.fmt_number(v, 1, signed=True) for v in res["pred_ret_bps"]],
        "intervalle 10–90 %": [f"{fmt_price(a)} – {fmt_price(b)}" for a, b in zip(res["q10"],
                                                                                  res["q90"])]})
    lines.append(report.to_markdown(table))
    lines.append("")
    lines.append(RAW_PROBA_WARNING)
    if backend == "timesfm3":
        lines.append(LICENSE_TIMESFM3)
    print("\n".join(lines), file=stream)
    return res


def _fmt_duration(td: pd.Timedelta) -> str:
    """Durée lisible en français : ``2 j 3 h 5 min``."""
    minutes = int(td.total_seconds() // 60)
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    parts = ([f"{d} j"] if d else []) + ([f"{h} h"] if h else []) + [f"{m} min"]
    return " ".join(parts)


def fmt_price(v: float) -> str:
    """Prix lisible : 4 chiffres significatifs après la virgule sous 10, 2 décimales sinon."""
    if v is None or not np.isfinite(v):
        return "—"
    dec = 2 if abs(v) >= 10 else 4 if abs(v) >= 0.1 else 6
    return report.fmt_number(v, dec)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _positive_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("entier >= 1 attendu")
    return v


def _positive_float(s: str) -> float:
    v = float(s)
    if not v > 0:
        raise argparse.ArgumentTypeError("nombre > 0 attendu")
    return v


def _frac(s: str) -> float:
    v = float(s)
    if not 0.0 < v < 1.0:
        raise argparse.ArgumentTypeError("valeur dans ]0, 1[ attendue")
    return v


def build_parser() -> argparse.ArgumentParser:
    cfg = StudyConfig()
    p = argparse.ArgumentParser(
        prog="tradebot",
        description="État des lieux des indicateurs et prévision de direction (5/10/15 min) "
                    "des cryptos avec Google TimesFM. Recherche uniquement.")
    p.add_argument("--version", action="version", version=f"tradebot {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="journal détaillé (DEBUG)")
    p.add_argument("-q", "--quiet", action="store_true", help="avertissements et erreurs seulement")
    sub = p.add_subparsers(dest="command", required=True, metavar="commande")

    def common(sp, tickers_default, days_default=cfg.days):
        sp.add_argument("--tickers", nargs="+", default=list(tickers_default), metavar="TICKER",
                        help=f"paires Binance (défaut : {' '.join(tickers_default)})")
        sp.add_argument("--interval", default=cfg.interval, help="intervalle des barres (défaut : 1m)")
        sp.add_argument("--days", type=_positive_int, default=days_default,
                        help=f"jours d'historique (défaut : {days_default})")
        sp.add_argument("--no-cache", action="store_true", help="ignorer le cache parquet")

    sp = sub.add_parser("fetch", help="télécharger / compléter le cache OHLCV (+ dérivés)")
    common(sp, cfg.tickers)
    sp.add_argument("--no-derivatives", action="store_true",
                    help="ne pas charger funding / open interest / ratios long-short")

    sp = sub.add_parser("study", help="état des lieux des indicateurs -> reports/etat_des_lieux/")
    common(sp, cfg.tickers)
    sp.add_argument("--horizons", nargs="+", type=_positive_int, default=list(cfg.horizons),
                    help="horizons en barres (défaut : 5 10 15)")
    sp.add_argument("--train-frac", type=_frac, default=cfg.train_frac,
                    help="part chronologique d'apprentissage (défaut : 0.6)")
    sp.add_argument("--no-combined", action="store_true",
                    help="ne pas entraîner les modèles combinés (logit, hgb)")
    sp.add_argument("--n-splits", type=_positive_int, default=5,
                    help="plis du walk-forward des modèles combinés (défaut : 5)")
    sp.add_argument("--row-step", type=_positive_int, default=5,
                    help="une origine toutes les N barres pour les modèles combinés (défaut : 5)")
    sp.add_argument("--max-train-rows", type=int, default=100_000,
                    help="apprentissage éclairci au-delà de N lignes (0 = pas de limite ; défaut : 100000)")
    sp.add_argument("--out-dir", default=None, help="dossier de sortie (défaut : reports/etat_des_lieux)")

    sp = sub.add_parser("timesfm-backtest", help="walk-forward TimesFM vs baselines -> reports/timesfm/")
    common(sp, DEFAULT_TIMESFM_TICKERS)
    sp.add_argument("--horizons", nargs="+", type=_positive_int, default=list(cfg.horizons))
    sp.add_argument("--train-frac", type=_frac, default=cfg.train_frac,
                    help="origines après cette part de la période (partie test ; défaut : 0.6)")
    sp.add_argument("--covariates", choices=("none", "top", "all"), default="none",
                    help="covariables past-only : aucune, top-k par ic_train_mean de "
                         "reports/etat_des_lieux/aggregate.csv, ou toutes (plafonnées)")
    sp.add_argument("--top-k", type=_positive_int, default=5, help="nombre de covariables « top » (défaut : 5)")
    sp.add_argument("--max-covariates", type=_positive_int, default=MAX_COVARIATES,
                    help=f"plafond pour --covariates all (défaut : {MAX_COVARIATES})")
    sp.add_argument("--aggregate", default=None,
                    help="chemin de aggregate.csv (défaut : reports/etat_des_lieux/aggregate.csv)")
    sp.add_argument("--stride", type=_positive_int, default=15,
                    help="une origine toutes les N barres, alignée sur l'horloge (défaut : 15)")
    sp.add_argument("--context-len", type=_positive_int, default=cfg.timesfm.context_len)
    sp.add_argument("--backend", choices=("timesfm3", "timesfm2p5"), default=cfg.timesfm.backend,
                    help="timesfm3 (licence non commerciale) ou timesfm2p5 (Apache-2.0)")
    sp.add_argument("--max-origins", type=int, default=1000,
                    help="origines au plus, réparties sur la période (0 = toutes ; défaut : 1000)")
    sp.add_argument("--transform", choices=("logprice", "price", "cumret"),
                    default=cfg.timesfm.context_transform)
    sp.add_argument("--cost-bps", type=float, default=cfg.cost_bps,
                    help="coût aller-retour de la mini-stratégie en pb (défaut : 10, futures taker)")
    sp.add_argument("--threshold", type=float, default=0.55,
                    help="seuil de P(hausse) de la mini-stratégie (défaut : 0.55)")
    sp.add_argument("--batch-size", type=_positive_int, default=cfg.timesfm.batch_size)
    sp.add_argument("--no-symmetric", action="store_true",
                    help="désactiver la moyenne symétrique (plus rapide, biais possible)")
    sp.add_argument("--start", default=None, help="première origine (UTC) ; défaut : début du test")
    sp.add_argument("--end", default=None, help="dernière origine (UTC)")
    sp.add_argument("--out-dir", default=None, help="dossier de sortie (défaut : reports/timesfm)")

    sp = sub.add_parser("predict", help="P(hausse) à 5/10/15 min depuis la dernière barre close")
    sp.add_argument("--ticker", required=True, help="paire Binance, ex. BTCUSDT")
    sp.add_argument("--interval", default=cfg.interval)
    sp.add_argument("--horizons", nargs="+", type=_positive_int, default=list(cfg.horizons))
    sp.add_argument("--covariates", choices=("none", "top", "all"), default="none")
    sp.add_argument("--top-k", type=_positive_int, default=5)
    sp.add_argument("--max-covariates", type=_positive_int, default=MAX_COVARIATES)
    sp.add_argument("--aggregate", default=None)
    sp.add_argument("--context-len", type=_positive_int, default=cfg.timesfm.context_len)
    sp.add_argument("--backend", choices=("timesfm3", "timesfm2p5"), default=cfg.timesfm.backend)
    sp.add_argument("--transform", choices=("logprice", "price", "cumret"),
                    default=cfg.timesfm.context_transform)
    sp.add_argument("--days", type=_positive_int, default=None,
                    help="jours chargés (défaut : 2 sans covariables, 10 avec)")
    sp.add_argument("--no-symmetric", action="store_true")
    sp.add_argument("--no-cache", action="store_true")

    sp = sub.add_parser("polymarket-signal",
                        help="formule P(Up) en direct sur Polymarket Up/Down (simulation papier)")
    sp.add_argument("--asset", default="btc", choices=("btc", "eth", "sol", "xrp", "bnb", "doge"),
                    help="actif (défaut : btc)")
    sp.add_argument("--duration", default="5m", choices=("5m", "15m"), help="durée du marché (défaut : 5m)")
    sp.add_argument("--interval", type=_positive_float, default=1.0,
                    help="secondes entre deux passages (défaut : 1)")
    sp.add_argument("--sigma", choices=("parkinson", "ewma"), default="ewma",
                    help="σ : EWMA des rendements 1 s ou Parkinson (hauts/bas 1 min) (défaut : ewma)")
    sp.add_argument("--sigma-scale", type=_positive_float, default=None,
                    help="facteur k appliqué à σ (défaut : 1,40 pour ewma, 1,50 pour parkinson, choisis sur "
                         "l'historique BTC 04/09–13/09 ; 1 = formule brute, trop sûre d'elle)")
    sp.add_argument("--min-edge", type=float, default=0.0,
                    help="espérance minimale pour « acheter », en cents par part (défaut : 0)")
    sp.add_argument("--basis-sd", type=float, default=0.5,
                    help="erreur du proxy Binance -> Chainlink sur F − K, en pb (défaut : 0,5, calibrée "
                         "sur BTC 04/09–13/09 ; 0 = formule pure)")
    sp.add_argument("--once", action="store_true", help="un seul passage")
    sp.add_argument("--max-seconds", type=_positive_float, default=None,
                    help="arrêt après N secondes (défaut : jusqu'à Ctrl-C)")
    sp.add_argument("--log", default=None, metavar="FICHIER.csv",
                    help="journal CSV (une ligne par marché et par passage, issue ajoutée ensuite)")
    sp.add_argument("--fill-outcomes", action="store_true",
                    help="second passage seulement : ajouter l'issue officielle au journal --log et faire le bilan")
    return p


def _configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s : %(message)s",
                        datefmt="%H:%M:%S", force=True)
    for noisy in ("urllib3", "matplotlib", "PIL", "huggingface_hub", "filelock", "fsspec"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    cmdline = _command_line(argv)
    if args.command in ("timesfm-backtest", "predict") and args.covariates != "none" \
            and args.backend != "timesfm3":
        parser.error("les covariables exigent --backend timesfm3")
    if args.command == "polymarket-signal" and args.fill_outcomes and not args.log:
        parser.error("--fill-outcomes exige --log")
    if args.command == "polymarket-signal" and not args.basis_sd >= 0:
        parser.error("--basis-sd doit être >= 0")
    try:
        if args.command == "fetch":
            summary = run_fetch(args.tickers, interval=args.interval, days=args.days,
                                derivatives=not args.no_derivatives, cache=not args.no_cache)
            print(report.to_markdown(summary, {"missing_pct": "2%", "open_interest_coverage": "1%",
                                               "funding_rate_coverage": "1%", "seconds": 1}))
        elif args.command == "study":
            res = run_study(args.tickers, interval=args.interval, days=args.days,
                            horizons=args.horizons, train_frac=args.train_frac,
                            combined=not args.no_combined, n_splits=args.n_splits,
                            row_step=args.row_step, max_train_rows=args.max_train_rows or None,
                            cache=not args.no_cache, out_dir=args.out_dir, command=cmdline)
            print(f"Rapport : {res['out_dir'] / 'README.md'}")
        elif args.command == "timesfm-backtest":
            runs = run_timesfm_backtest(
                args.tickers, interval=args.interval, days=args.days, horizons=args.horizons,
                train_frac=args.train_frac, covariates=args.covariates, top_k=args.top_k,
                stride=args.stride, context_len=args.context_len, backend=args.backend,
                max_origins=args.max_origins or None, transform=args.transform,
                cost_bps=args.cost_bps, threshold=args.threshold, batch_size=args.batch_size,
                start=args.start, end=args.end, symmetric=not args.no_symmetric,
                max_covariates=args.max_covariates, aggregate_path=args.aggregate,
                cache=not args.no_cache, out_dir=args.out_dir, command=cmdline)
            out = Path(args.out_dir) if args.out_dir else REPORTS_DIR / TIMESFM_NAME
            print(f"Rapport : {out / 'README.md'} ({len(runs)} run(s) ajouté(s) ou remplacé(s))")
        elif args.command == "predict":
            run_predict(args.ticker, interval=args.interval, horizons=args.horizons,
                        covariates=args.covariates, top_k=args.top_k,
                        context_len=args.context_len, backend=args.backend,
                        transform=args.transform, days=args.days,
                        symmetric=not args.no_symmetric, max_covariates=args.max_covariates,
                        aggregate_path=args.aggregate, cache=not args.no_cache)
        elif args.command == "polymarket-signal":
            from .polymarket_signal import run_signal
            try:
                run_signal(args.asset, args.duration, interval=args.interval, sigma=args.sigma,
                           once=args.once, log_path=args.log, min_edge=args.min_edge / 100.0,
                           basis_sd=args.basis_sd / 1e4, sigma_scale=args.sigma_scale,
                           max_seconds=args.max_seconds,
                           fill_only=args.fill_outcomes)
            except (OSError, RuntimeError, ValueError) as exc:  # réseau, API, données
                raise CliError(f"polymarket-signal : {exc}") from exc
    except CliError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrompu")
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
