"""Tests de src/tradebot/cli.py : parseur, sélection des covariables sans fuite, et exécution
de bout en bout de ``study``, ``timesfm-backtest``, ``predict`` et ``fetch`` sur des données
synthétiques avec un chargeur et un FAUX TimesFM injectés (aucun accès réseau)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradebot import cli
from tradebot.config import TimesFMConfig
from tradebot.data import add_session
from tradebot.evaluation import AGGREGATE_COLUMNS, SUMMARY_COLUMNS
from tradebot.forecaster import TimesFMForecaster

Z9 = np.array([-1.2816, -0.8416, -0.5244, -0.2533, 0.0, 0.2533, 0.5244, 0.8416, 1.2816])
SEEDS = {"BTCUSDT": 1, "SOLUSDT": 2, "ETHUSDT": 3}
END = pd.Timestamp("2026-09-20 00:00", tz="UTC")
DERIV = ("open_interest", "open_interest_value", "ls_ratio_top", "ls_ratio_accounts",
         "taker_ls_ratio", "funding_rate")


def make_market(ticker: str, days: int) -> pd.DataFrame:
    """OHLCV 1 min synthétique au format de data.load_universe (+ dérivés, session, 1 trou)."""
    n = days * 1440
    rng = np.random.default_rng(SEEDS.get(ticker, 9))
    idx = pd.date_range(end=END, periods=n, freq="1min", name="open_time")
    r = rng.normal(0, 8e-4, n)
    r[1:] -= 0.08 * r[:-1]
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(3, 0.6, n)
    df = pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * (1 + rng.uniform(0, 5e-4, n)),
        "low": np.minimum(open_, close) * (1 - rng.uniform(0, 5e-4, n)), "close": close,
        "volume": vol, "quote_volume": vol * close,
        "trades": rng.integers(50, 500, n).astype(float),
        "taker_buy_volume": vol * rng.uniform(0.3, 0.7, n)}, index=idx)
    df = df.drop(df.index[1000:1003])
    oi = 1e6 * np.exp(np.cumsum(rng.normal(0, 1e-3, len(df))))
    df = df.assign(open_interest=oi, open_interest_value=oi * df["close"],
                   ls_ratio_top=1 + 0.1 * rng.standard_normal(len(df)),
                   ls_ratio_accounts=1 + 0.1 * rng.standard_normal(len(df)),
                   taker_ls_ratio=1 + 0.1 * rng.standard_normal(len(df)), funding_rate=1e-4)
    return add_session(df, ticker)


class Loader:
    """Imite data.load_universe ; compte les appels par actif."""

    def __init__(self, max_days: int = 3):
        self.max_days = max_days
        self.calls: list[tuple[str, int, bool]] = []

    def __call__(self, tickers, interval="1m", days=365, cache=True, with_derivatives=True):
        out = {}
        for t in tickers:
            self.calls.append((t, days, with_derivatives))
            df = make_market(t, min(days, self.max_days))
            out[t] = df if with_derivatives else df.drop(columns=list(DERIV))
        return out


class FakeTimesFM3:
    """Faux predict_batch 3.0 : léger retour à la moyenne + effet des covariables."""

    def __init__(self):
        self.calls = []

    def predict_batch(self, contexts, horizon, past_only_covariates=None, return_quantiles=False,
                      use_symmetric_averaging=False, **kw):
        self.calls.append(dict(n=len(contexts), horizon=horizon,
                               k=None if past_only_covariates is None
                               else past_only_covariates[0].shape[0]))
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        for i, c in enumerate(contexts):
            off = -0.3 * float(np.mean(np.asarray(c)[-15:]))
            if past_only_covariates is not None:
                off += 1e-5 * float(np.mean(past_only_covariates[i][:, -1]))
            q = off + 8e-4 * steps * Z9[None, :]
            yield SimpleNamespace(forecast=q[:, 4], quantiles=q)


class FakeTimesFM25:
    def forecast(self, horizon, inputs):
        n = len(inputs)
        steps = np.sqrt(np.arange(1, horizon + 1))[:, None]
        q = np.empty((n, horizon, 10))
        q[:, :, 1:] = 8e-4 * steps * Z9[None, :]
        q[:, :, 0] = 0.0
        return q[:, :, 5].copy(), q


def fake_forecaster(backend: str = "timesfm3") -> TimesFMForecaster:
    model = FakeTimesFM3() if backend == "timesfm3" else FakeTimesFM25()
    return TimesFMForecaster(TimesFMConfig(backend=backend), model=model)


@pytest.fixture(scope="module")
def study(tmp_path_factory):
    out = tmp_path_factory.mktemp("etat_des_lieux")
    loader = Loader(max_days=3)
    res = cli.run_study(["SOLUSDT", "BTCUSDT"], days=3, out_dir=out, loader=loader, n_splits=3,
                        row_step=5, command="python -m tradebot study (test)")
    return SimpleNamespace(out=out, res=res, loader=loader)


# -----------------------------------------------------------------------------
# Parseur
# -----------------------------------------------------------------------------


def test_parser_defaults_and_choices():
    p = cli.build_parser()
    a = p.parse_args(["study"])
    assert a.tickers == ["BTCUSDT", "SOLUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]
    assert a.horizons == [5, 10, 15] and a.train_frac == 0.6 and a.days == 365
    assert not a.no_combined
    b = p.parse_args(["timesfm-backtest", "--tickers", "BTCUSDT", "--covariates", "top",
                      "--top-k", "3", "--stride", "30", "--max-origins", "200"])
    assert (b.covariates, b.top_k, b.stride, b.max_origins) == ("top", 3, 30, 200)
    assert b.backend == "timesfm3" and b.context_len == 512 and b.transform == "logprice"
    assert b.cost_bps == 10.0
    c = p.parse_args(["predict", "--ticker", "SOLUSDT"])
    assert c.ticker == "SOLUSDT" and c.covariates == "none"
    d = p.parse_args(["fetch", "--no-derivatives", "--days", "5"])
    assert d.no_derivatives and d.days == 5
    for bad in (["study", "--train-frac", "1.5"], ["timesfm-backtest", "--covariates", "some"],
                ["timesfm-backtest", "--stride", "0"], ["predict"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


def test_main_rejects_covariates_with_timesfm25(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["timesfm-backtest", "--covariates", "top", "--backend", "timesfm2p5"])
    assert e.value.code == 2
    assert "timesfm3" in capsys.readouterr().err


def test_module_entry_point():
    r = subprocess.run([sys.executable, "-m", "tradebot", "--help"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0
    for cmd in ("fetch", "study", "timesfm-backtest", "predict"):
        assert cmd in r.stdout


# -----------------------------------------------------------------------------
# Sélection des covariables
# -----------------------------------------------------------------------------


def test_select_covariates_uses_train_ic_only():
    agg = pd.DataFrame({
        "indicator": ["a", "a", "b", "b", "c", "c", "d", "d"],
        "horizon": [5, 15] * 4,
        "ic_train_mean": [-0.05, -0.03, 0.02, 0.02, 0.0, 0.1, np.nan, np.nan],
        "ic_mean": [0.0, 0.0, 0.9, 0.9, 0.0, 0.0, 0.9, 0.9],   # le test ne doit pas compter
    })
    assert cli.rank_indicators_by_train_ic(agg) == ["c", "a", "b"]
    assert cli.rank_indicators_by_train_ic(agg, horizons=[5]) == ["a", "b", "c"]
    assert cli.select_covariates("none", available=["a"], aggregate=agg) == []
    assert cli.select_covariates("top", available=["a", "b", "c"], aggregate=agg, top_k=2) == ["c", "a"]
    assert cli.select_covariates("top", available=["a", "b"], aggregate=agg, top_k=2) == ["a", "b"]
    assert cli.select_covariates("all", available=["z", "b", "a"], aggregate=agg) == ["a", "b", "z"]
    assert cli.select_covariates("all", available=list("abcdefgh"), aggregate=None,
                                 max_covariates=3) == ["a", "b", "c"]
    with pytest.raises(cli.CliError):
        cli.select_covariates("top", available=["a"], aggregate=None)
    with pytest.raises(cli.CliError):
        cli.select_covariates("bof", available=["a"], aggregate=agg)


# -----------------------------------------------------------------------------
# study
# -----------------------------------------------------------------------------


def test_study_writes_complete_report(study):
    out = study.out
    for name in ("README.md", "aggregate.csv", "top_20.csv", "bottom_20.csv", "familles.csv",
                 "combined_summary.csv", "combined_folds.csv", "catalogue_indicateurs.csv",
                 "donnees.csv", "timings.csv", "run.json", "scores_BTCUSDT.csv",
                 "scores_SOLUSDT.csv", "ic_heatmap.png", "familles.png", "modeles_combines.png"):
        assert (out / name).exists(), name
    agg = pd.read_csv(out / "aggregate.csv")
    assert list(agg.columns) == list(AGGREGATE_COLUMNS)
    assert set(agg["n_tickers"]) <= {0, 1, 2} and (agg["n_tickers"] == 2).any()
    top = pd.read_csv(out / "top_20.csv")
    assert len(top) == 20 and top["ic_mean"].abs().is_monotonic_decreasing
    comb = pd.read_csv(out / "combined_summary.csv")
    assert list(comb.columns) == ["ticker", "model", *SUMMARY_COLUMNS]
    assert set(comb["model"]) == {"logit", "hgb", *cli.BASELINE_NAMES}
    assert set(comb["ticker"]) == {"BTCUSDT", "SOLUSDT"}
    # baselines et modèles évalués sur exactement les mêmes barres
    for (tk, h), g in comb.groupby(["ticker", "horizon"]):
        assert g["n"].nunique() == 1, (tk, h)
    folds = pd.read_csv(out / "combined_folds.csv")
    purge = pd.to_datetime(folds["test_start"]) - pd.to_datetime(folds["train_end"])
    assert (purge >= pd.to_timedelta(folds["horizon"], unit="min")).all()
    readme = (out / "README.md").read_text(encoding="utf-8")
    for col in AGGREGATE_COLUMNS:
        assert f"`{col}`" in readme, col
    assert "## 0. En bref" in readme and "ic_heatmap.png" in readme
    meta = json.loads((out / "run.json").read_text())
    assert set(meta["split_times"]) == {"BTCUSDT", "SOLUSDT"}
    assert meta["params"]["train_frac"] == 0.6 and meta["params"]["horizons"] == [5, 10, 15]
    t = pd.read_csv(out / "timings.csv")
    assert {"chargement", "indicateurs", "scores des indicateurs", "modèle combiné logit",
            "modèle combiné hgb", "total"} <= set(t["step"])


def test_study_loads_benchmark_once_and_first(study):
    names = [c[0] for c in study.loader.calls]
    assert names == ["BTCUSDT", "SOLUSDT"]           # BTC d'abord, jamais rechargé
    data = pd.read_csv(study.out / "donnees.csv")
    sol = data.set_index("ticker").loc["SOLUSDT"]
    btc = data.set_index("ticker").loc["BTCUSDT"]
    assert sol["benchmark"] == "BTCUSDT" and pd.isna(btc["benchmark"])
    assert sol["n_indicators"] > btc["n_indicators"]   # force relative seulement pour SOL
    assert (data["missing_bars"] == 3).all()
    # scores : la coupure apprentissage/test est celle annoncée dans run.json
    meta = json.loads((study.out / "run.json").read_text())
    df = make_market("SOLUSDT", 3)
    assert pd.Timestamp(meta["split_times"]["SOLUSDT"]) == df.index[round(0.6 * len(df))]


def test_study_without_combined(tmp_path):
    res = cli.run_study(["BTCUSDT"], days=2, out_dir=tmp_path, loader=Loader(2), combined=False,
                        horizons=(5,))
    assert res["combined"] is None
    assert not (tmp_path / "combined_summary.csv").exists()
    assert "Non calculés" in (tmp_path / "README.md").read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# timesfm-backtest
# -----------------------------------------------------------------------------


def test_timesfm_backtest_none_in_test_part(tmp_path):
    loader = Loader(3)
    fc = fake_forecaster()
    runs = cli.run_timesfm_backtest(["BTCUSDT"], days=3, stride=15, max_origins=40,
                                    out_dir=tmp_path, loader=loader, forecaster=fc)
    assert len(runs) == 1
    run = runs["run"].iloc[0]
    assert run == "BTCUSDT_timesfm3_none_s15_c512_logprice"
    preds = pd.read_csv(tmp_path / f"{run}_predictions.csv", parse_dates=["time"])
    df = make_market("BTCUSDT", 3)
    test_start = df.index[round(0.6 * len(df))]
    assert preds["time"].min() >= test_start               # origines dans la partie test
    assert preds["time"].nunique() == 40
    assert set(preds["horizon"]) == {5, 10, 15}
    assert (preds["time"].dt.minute % 15 == 0).all()
    assert preds["p_up"].between(0.01, 0.99).all()
    for b in cli.BASELINE_NAMES:
        assert preds[b].isin([0.48, 0.5, 0.52]).all()
    assert preds["p_up_cal"].isna().all()                   # < 300 labels : pas de recalibration
    m = pd.read_csv(tmp_path / f"{run}_metrics.csv")
    assert set(m["model"]) == {"timesfm", *cli.BASELINE_NAMES}
    assert (m["h_eff"] == np.ceil(m["horizon"] / 15)).all()
    assert m.loc[m["model"] != "reversal_h", "dm_brier_vs_reversal_t"].notna().any()
    st = pd.read_csv(tmp_path / f"{run}_strategy.csv")
    assert set(st["signal"]) == {"timesfm", "reversal_h"}
    dg = pd.read_csv(tmp_path / f"{run}_diagnostics.csv")
    assert list(dg["horizon"]) == [5, 10, 15] and dg["coverage_q10_q90"].between(0, 1).all()
    assert (tmp_path / f"{run}_accuracy.png").exists()
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert run in readme and "non commerciale" in readme
    # pas de covariables demandées -> pas de dérivés chargés
    assert loader.calls == [("BTCUSDT", 3, False)]


def test_timesfm_backtest_top_covariates_no_leak(study, tmp_path):
    loader = Loader(3)
    fc = fake_forecaster()
    agg_path = study.out / "aggregate.csv"
    runs = cli.run_timesfm_backtest(["SOLUSDT"], days=3, stride=5, max_origins=30,
                                    covariates="top", top_k=3, train_frac=0.2,
                                    aggregate_path=agg_path, out_dir=tmp_path, loader=loader,
                                    forecaster=fc)
    r = runs.iloc[0]
    assert r["covariates"] == "top3"
    names = r["covariate_names"].split(";")
    agg = pd.read_csv(agg_path)
    assert names == cli.rank_indicators_by_train_ic(agg, (5, 10, 15))[:3]
    # train_frac=0.2 placerait les origines dans l'apprentissage de l'étude : début reculé
    meta = json.loads((study.out / "run.json").read_text())
    study_train_end = max(pd.Timestamp(v) for v in meta["split_times"].values())
    preds = pd.read_csv(tmp_path / f"{r['run']}_predictions.csv", parse_dates=["time"])
    assert preds["time"].min() >= study_train_end
    # lots homogènes : chaque appel au modèle reçoit 3 covariables
    assert fc._model.calls and all(c["k"] == 3 for c in fc._model.calls)
    # la référence BTC est chargée pour la force relative
    assert ("BTCUSDT", 3, False) in loader.calls


def test_timesfm_backtest_runs_are_upserted(tmp_path):
    fc = fake_forecaster()
    kw = dict(days=2, max_origins=10, out_dir=tmp_path, loader=Loader(2), forecaster=fc)
    cli.run_timesfm_backtest(["BTCUSDT"], stride=15, **kw)
    cli.run_timesfm_backtest(["BTCUSDT"], stride=30, **kw)
    cli.run_timesfm_backtest(["BTCUSDT"], stride=15, **kw)
    runs = pd.read_csv(tmp_path / "runs.csv")
    assert sorted(runs["run"]) == ["BTCUSDT_timesfm3_none_s15_c512_logprice",
                                   "BTCUSDT_timesfm3_none_s30_c512_logprice"]
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "2 run(s)" in readme


def test_timesfm_backtest_rejects_bad_options(tmp_path):
    with pytest.raises(cli.CliError):
        cli.run_timesfm_backtest(["BTCUSDT"], covariates="top", backend="timesfm2p5",
                                 out_dir=tmp_path, loader=Loader(2))
    with pytest.raises(cli.CliError):
        cli.run_timesfm_backtest(["BTCUSDT"], covariates="top", out_dir=tmp_path,
                                 loader=Loader(2), aggregate_path=tmp_path / "absent.csv",
                                 forecaster=fake_forecaster(), days=2)


def test_calibration_needs_enough_history():
    rng = np.random.default_rng(0)
    n = 800
    t = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    p = rng.uniform(0.3, 0.7, n)
    y = (rng.uniform(size=n) < p).astype(float)
    preds = pd.DataFrame({"time": t, "horizon": 5, "p_up": p, "y_true": y})
    cal = cli._calibrate_second_half(preds, pd.Timedelta(minutes=1))
    first = preds["time"] < t[n // 2]
    assert cal[first].isna().all() and cal[~first].notna().all()
    assert cal[~first].between(0.01, 0.99).all()
    few = cli._calibrate_second_half(preds.iloc[:200], pd.Timedelta(minutes=1))
    assert few.isna().all()


# -----------------------------------------------------------------------------
# predict et fetch
# -----------------------------------------------------------------------------


def test_predict_prints_probabilities_and_license():
    buf = io.StringIO()
    res = cli.run_predict("SOLUSDT", loader=Loader(3), forecaster=fake_forecaster(), stream=buf)
    assert list(res["horizon"]) == [5, 10, 15]
    assert res["p_up"].between(0.01, 0.99).all()
    last = make_market("SOLUSDT", 2).index[-1]
    assert (res["origin"] == last).all()
    assert list(res["target_close_time"]) == [last + pd.Timedelta(minutes=h + 1) for h in (5, 10, 15)]
    assert (res["q10"] <= res["pred_median"]).all() and (res["pred_median"] <= res["q90"]).all()
    text = buf.getvalue()
    assert "P(hausse)" in text and "NON commerciale" in text and "5 min" in text
    assert "en retard" in text                       # données synthétiques anciennes


def test_predict_timesfm25_has_no_license_warning_and_covariates_need_timesfm3():
    buf = io.StringIO()
    res = cli.run_predict("BTCUSDT", backend="timesfm2p5", loader=Loader(3),
                          forecaster=fake_forecaster("timesfm2p5"), stream=buf)
    assert len(res) == 3 and "NON commerciale" not in buf.getvalue()
    with pytest.raises(cli.CliError):
        cli.run_predict("BTCUSDT", backend="timesfm2p5", covariates="top", loader=Loader(3),
                        forecaster=fake_forecaster("timesfm2p5"), stream=buf)


def test_predict_with_top_covariates(study):
    buf = io.StringIO()
    fc = fake_forecaster()
    res = cli.run_predict("SOLUSDT", covariates="top", top_k=2, loader=Loader(3), forecaster=fc,
                          aggregate_path=study.out / "aggregate.csv", stream=buf)
    assert len(res) == 3
    assert fc._model.calls[-1]["k"] == 2


def test_run_fetch_summary():
    s = cli.run_fetch(["BTCUSDT", "SOLUSDT"], days=2, loader=Loader(2))
    assert list(s["ticker"]) == ["BTCUSDT", "SOLUSDT"]
    assert (s["n_bars"] == 2 * 1440 - 3).all() and (s["missing_bars"] == 3).all()
    assert (s["open_interest_coverage"] == 1.0).all()
    s2 = cli.run_fetch(["BTCUSDT"], days=2, derivatives=False, loader=Loader(2))
    assert "open_interest_coverage" not in s2.columns
