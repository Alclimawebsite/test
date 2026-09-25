"""Tests de src/tradebot/report.py : formats français, Markdown sans tabulate, synthèses,
titres-conclusions et graphiques (fichiers PNG valides). Hors ligne, rapides."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot import report
from tradebot.backtest import direction_metrics, toy_strategy
from tradebot.evaluation import AGGREGATE_COLUMNS, SCORE_COLUMNS, SUMMARY_COLUMNS

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_agg(seed: int = 0) -> pd.DataFrame:
    """Agrégat synthétique au format de evaluation.aggregate_scores."""
    rng = np.random.default_rng(seed)
    fams = ["tendance", "momentum", "volume", "flux"]
    rows = []
    for i in range(24):
        for h in (5, 10, 15):
            ic = -0.004 * i * (h / 15) if i % 3 else 0.003 * i * (h / 15)
            rows.append(dict(indicator=f"ind_{i:02d}", horizon=h, family=fams[i % 4],
                             n_tickers=2 if i != 23 else 1, ic_mean=ic, ic_std=0.01,
                             ic_t_cross=ic / 0.01, frac_same_sign=1.0, ic_daily_t_mean=ic * 100,
                             ic_train_mean=ic * 0.9, ic_test_mean=ic * 1.1,
                             hit_rate_oos_mean=0.5 + abs(ic) / 2, auc_oos_mean=0.5 + abs(ic),
                             z_combined=ic * 80, p_combined=0.01, q_value=0.02,
                             q_value_by=float(rng.uniform(0, 0.2))))
    return pd.DataFrame(rows, columns=list(AGGREGATE_COLUMNS))


def make_metrics(acc_model: float, n: int, horizons=(5, 10, 15), facet: str | None = None
                 ) -> pd.DataFrame:
    rows = []
    for h in horizons:
        for model, acc in (("timesfm", acc_model), ("reversal_h", 0.51), ("momentum_h", 0.49),
                           ("always_up", 0.5)):
            r = dict(model=model, horizon=h, accuracy=acc, n=n, n_eff=n)
            if facet:
                r["ticker"] = facet
            rows.append(r)
    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# Formats
# -----------------------------------------------------------------------------


def test_fmt_number_french():
    assert report.fmt_number(0.0523) == "0,052"
    assert report.fmt_number(-0.0523, signed=True) == "−0,052"
    assert report.fmt_number(0.0523, signed=True) == "+0,052"
    assert report.fmt_number(12345, 0) == "12 345"
    assert report.fmt_number(0.5213, 1, pct=True) == "52,1 %"
    assert report.fmt_number(float("nan")) == "—"
    assert report.fmt_number(None) == "—"
    assert report.fmt_number(np.float64(1.25), 1) in ("1,2", "1,3")


def test_to_markdown_formats_and_escaping():
    df = pd.DataFrame({
        "name": ["a|b", "c"],
        "n": [1234, 5],
        "ic": [0.0512, np.nan],
        "rate": [0.5234, 0.4],
        "t": [pd.Timestamp("2026-09-25 10:30", tz="UTC"), pd.NaT],
    })
    md = report.to_markdown(df, {"ic": "+3", "rate": "1%"}, code_columns=["name"])
    lines = md.splitlines()
    assert lines[0] == "| name | n | ic | rate | t |"
    assert lines[1] == "|---|---|---|---|---|"
    assert lines[2] == "| `a\\|b` | 1 234 | +0,051 | 52,3 % | 2026-09-25 10:30 UTC |"
    assert lines[3] == "| `c` | 5 | — | 40,0 % | — |"
    assert len(lines) == 4
    # décimales communes + index
    md2 = report.to_markdown(df[["ic"]], 2, index=True)
    assert md2.splitlines()[0] == "| index | ic |"
    assert "0,05" in md2


def test_columns_doc_cover_every_output_column():
    keys = set(direction_metrics(np.array([0, 1, 1, 0, 1.0]), np.array([.4, .6, .7, .3, .2]), 1))
    preds = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC"),
                          "horizon": 5, "p_up": [0.6, 0.4, 0.7, 0.3], "ret": [1e-3, -1e-3, 2e-3, 1e-3]})
    keys |= set(toy_strategy(preds, 5))
    cols = (set(SCORE_COLUMNS) | set(AGGREGATE_COLUMNS) | set(SUMMARY_COLUMNS)
            | set(report.FAMILY_COLUMNS) | set(report.METRIC_COLUMNS) | set(report.DIAG_COLUMNS)
            | set(report.STRATEGY_COLUMNS) | set(report.PRED_FILE_COLUMNS)
            | set(report.RUN_COLUMNS) | set(report.DATA_COLUMNS) | set(report.FOLD_COLUMNS) | keys)
    missing = sorted(c for c in cols if c not in report.COLUMN_DOCS)
    assert not missing, missing
    table = report.columns_doc_table(["ic_mean", "inconnue"])
    assert "| `ic_mean` |" in table and "| `inconnue` | — |" in table


def test_min_detectable_accuracy_matches_methodology():
    # méthodologie § 5 : ~290 origines non chevauchantes -> ~57 % ; 3 865 -> 52 %
    assert report.min_detectable_accuracy(290) == pytest.approx(0.573, abs=0.002)
    assert report.min_detectable_accuracy(3865) == pytest.approx(0.52, abs=0.001)
    assert math.isnan(report.min_detectable_accuracy(0))


# -----------------------------------------------------------------------------
# Synthèses et titres
# -----------------------------------------------------------------------------


def test_family_summary():
    agg = make_agg()
    fam = report.family_summary(agg)
    assert list(fam.columns) == list(report.FAMILY_COLUMNS)
    assert fam["max_abs_ic"].is_monotonic_decreasing
    for _, r in fam.iterrows():
        sub = agg[agg["family"] == r["family"]]
        assert r["max_abs_ic"] == pytest.approx(sub["ic_mean"].abs().max())
        best = sub.loc[sub["ic_mean"].abs().idxmax()]
        assert r["best_indicator"] == best["indicator"]
        assert r["best_ic"] == pytest.approx(best["ic_mean"])
        assert r["n_indicators"] == sub["indicator"].nunique()
        assert r["frac_q_by_05"] == pytest.approx((sub["q_value_by"] < 0.05).mean())
    assert report.family_summary(agg.iloc[:0]).empty


def test_top_bottom():
    agg = make_agg()
    top, bottom = report.top_bottom(agg, 5)
    assert len(top) == 5 and len(bottom) == 5
    assert top["ic_mean"].abs().is_monotonic_decreasing
    assert top["ic_mean"].abs().iloc[0] == pytest.approx(agg["ic_mean"].abs().max())
    assert bottom["ic_mean"].abs().is_monotonic_increasing
    # le bas ne retient que les couples mesurés sur tous les actifs
    assert (bottom["n_tickers"] == 2).all()
    assert "ind_23" not in set(bottom["indicator"])


def test_ic_title():
    assert report.ic_title([-0.05, -0.04, -0.03, 0.01]).startswith("Retournement dominant")
    assert report.ic_title([0.05, 0.04, 0.03, -0.01]).startswith("Continuation dominante")
    assert report.ic_title([0.05, -0.04]).startswith("Continuation et retournement")
    assert report.ic_title([-0.01]).endswith("très faible")
    assert report.ic_title([-0.05]).endswith("faible")
    assert report.ic_title([-0.2]).endswith("notable")
    assert "0,052" in report.ic_title([-0.052])
    assert report.ic_title([np.nan]) == "Aucun IC calculable"


def test_verdict_vs_baselines():
    base = ["reversal_h", "momentum_h", "always_up"]
    good = report.verdict_vs_baselines(make_metrics(0.60, 20_000), ["timesfm"], base, "TimesFM")
    assert good == "TimesFM bat les baselines partout"
    noise = report.verdict_vs_baselines(make_metrics(0.52, 200), ["timesfm"], base, "TimesFM")
    assert noise.startswith("TimesFM ≈ baselines") and "200" in noise
    bad = report.verdict_vs_baselines(make_metrics(0.45, 20_000), ["timesfm"], base, "TimesFM")
    assert bad.startswith("TimesFM fait moins bien") and "5/10/15" in bad
    m = pd.concat([make_metrics(0.60, 20_000, facet="A"), make_metrics(0.52, 100, facet="B")])
    mixed = report.verdict_vs_baselines(m, ["timesfm"], base, "X", facet_col="ticker")
    assert "5/10/15" in mixed and "seulement" in mixed


def test_ink_on_contrast():
    assert report._ink_on((0.08, 0.25, 0.55, 1.0)) == "#ffffff"      # bleu foncé
    assert report._ink_on((0.94, 0.94, 0.93, 1.0)) == report.TEXT     # gris neutre


# -----------------------------------------------------------------------------
# Graphiques
# -----------------------------------------------------------------------------


def _is_png(path) -> bool:
    with open(path, "rb") as f:
        return f.read(8) == PNG_MAGIC


def test_plots_write_valid_png(tmp_path):
    agg = make_agg()
    p1 = report.plot_ic_heatmap(agg, tmp_path / "heat.png", top=10)
    p2 = report.plot_family_bars(report.family_summary(agg), tmp_path / "fam.png")
    p3 = report.plot_excess_accuracy(make_metrics(0.53, 300), tmp_path / "acc.png",
                                     models=["timesfm", "reversal_h", "momentum_h", "always_up"],
                                     label_models=("timesfm",), title="Titre de test")
    m = pd.concat([make_metrics(0.53, 300, facet="BTCUSDT"), make_metrics(0.48, 300, facet="SOLUSDT")])
    p4 = report.plot_excess_accuracy(m, tmp_path / "acc_facets.png", facet_col="ticker",
                                     models=["timesfm", "reversal_h", "unknown_model"])
    for p in (p1, p2, p3, p4):
        assert p.exists() and p.stat().st_size > 5_000 and _is_png(p)
    with pytest.raises(ValueError):
        report.plot_ic_heatmap(agg.iloc[:0], tmp_path / "empty.png")
    with pytest.raises(ValueError):
        report.plot_excess_accuracy(make_metrics(0.5, 10), tmp_path / "x.png", models=["nope"])


def test_heatmap_keeps_top_by_abs_ic(tmp_path, monkeypatch):
    """Les lignes tracées sont les `top` indicateurs de plus grand |IC| (tous horizons)."""
    import matplotlib.axes

    captured = {}
    orig = matplotlib.axes.Axes.set_yticks

    def spy(self, ticks, labels=None, **kw):
        if labels is not None:
            captured["labels"] = list(labels)
        return orig(self, ticks, labels, **kw)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_yticks", spy)
    agg = make_agg()
    report.plot_ic_heatmap(agg, tmp_path / "h.png", top=4)
    strength = agg.groupby("indicator")["ic_mean"].apply(lambda s: s.abs().max())
    expected = list(strength.sort_values(ascending=False, kind="stable").index[:4])
    assert captured["labels"] == expected


def test_write_helpers(tmp_path):
    p = report.write_csv(pd.DataFrame({"a": [1.5]}), tmp_path / "sub" / "x.csv")
    assert pd.read_csv(p)["a"].iloc[0] == 1.5
    t = report.write_text(tmp_path / "r.md", "bonjour")
    assert t.read_text(encoding="utf-8") == "bonjour\n"
    j = report.write_json(tmp_path / "m.json", {"ts": pd.Timestamp("2026-01-01", tz="UTC"),
                                                  "x": np.float64("nan"), "n": np.int64(3)})
    import json

    assert json.loads(j.read_text()) == {"ts": "2026-01-01 00:00:00+00:00", "x": None, "n": 3}
    assert report.report_dir("etude_test", root=tmp_path) == tmp_path / "etude_test"
    assert (tmp_path / "etude_test").is_dir()
