#!/usr/bin/env python
"""Runners pump.fun : quels signaux, à quel moment, annoncent un token qui décolle ?

Univers : tous les lancements enregistrés par ``scripts/pumpfun_collector.py`` (sans biais du
survivant) et, pour le seul moment « après graduation », toutes les graduations servies par
l'API. Seuls les tokens sur la courbe standard cotée en SOL sont étudiés (voir
``pumpfun.is_standard_curve``) ; leur part est indiquée dans le rapport.

Pour chaque token et chaque moment de détection (1, 5, 15 min après la création ; passage à 50 %
de la courbe ; 5 min après la graduation), on calcule les variables connues à l'instant t, les
trois définitions de runner (×10 ; graduation ; 1 M$ de market cap) à 1 h, 6 h et 24 h, et le
P&L d'un achat réel (latence, glissement sur la courbe, frais). Puis :

1. taux de runners par moment (ce qu'on gagne à attendre) ;
2. pouvoir de chaque variable seule (AUC, FDR de Benjamini-Hochberg) ;
3. modèles entraînés sur le début de la période et testés sur la fin (logistique, boosting) ;
4. P&L d'une stratégie « acheter les 10 % les mieux notés » contre « acheter tout ».

Sorties : ``reports/pumpfun_runners/`` (CSV, PNG, ``run.json``, ``modele_*.json`` pour le
détecteur live). Recherche et simulation papier : aucun ordre, aucune clé.

    python scripts/pumpfun_runners_study.py [--workers 6] [--max-tokens 0] [--reuse]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, rankdata

from tradebot import pumpfun as pf
from tradebot import pumpfun_runners as pr
from tradebot.config import REPORTS_DIR
from tradebot.evaluation import benjamini_hochberg

log = logging.getLogger("pumpfun_runners_study")
OUT = REPORTS_DIR / "pumpfun_runners"
TOKENS_DIR = pf.CACHE / "tokens"
FINAL_AFTER_MS = 26 * pr.HOUR_MS          # au-delà, les données d'un token ne changent plus pour l'étude

TARGETS = ("x10", "x3", "grad", "mcap1m", "mcap100k")
TARGET_LABEL = {"x10": "×10", "x3": "×3", "grad": "graduation", "mcap1m": "1 M$ de market cap",
                "mcap100k": "100 k$ de market cap"}
MOMENT_LABEL = {"launch_1m": "1 min après le lancement", "launch_5m": "5 min après le lancement",
                "launch_15m": "15 min après le lancement", "pre_grad": "courbe à 50 %",
                "post_grad": "5 min après la graduation"}
NON_FEATURES = {"mint", "symbol", "creator", "created_ms", "moment", "t_ms", "p_ref", "p_entry", "entry_slip",
                "sol_usd", "grad_ms", "source", "price_sol"}
TOP_FRAC = 0.10


# ---------------------------------------------------------------------------
# 1. Jeu de données
# ---------------------------------------------------------------------------
def _cache_path(mint: str) -> Path:
    return TOKENS_DIR / f"{mint}.pkl"


def load_or_fetch(cli, dex_cli, coin: dict, moments: tuple[str, ...], now_ms: int, reuse: bool) -> pr.TokenData:
    path = _cache_path(coin["mint"])
    if path.exists():
        try:
            fetched, td = pickle.loads(path.read_bytes())
            if "post_grad" in moments and not math.isfinite(getattr(td, "post_ms", math.nan)) \
                    and "post_grad_not_located" not in td.notes:
                td.notes = [n for n in td.notes if not n.startswith("post_grad")]    # cache d'avant le correctif
                td.trades.pop("post_grad", None)
                pr.fetch_post_grad(cli, td)
                path.write_bytes(pickle.dumps((fetched, td)))
            if reuse or fetched - int(coin["created_timestamp"]) >= FINAL_AFTER_MS:
                td.fetched_ms = fetched
                return td
        except Exception:  # noqa: BLE001 — cache illisible : on retélécharge
            pass
    td = pr.fetch_token(cli, coin, moments=moments, dex_cli=dex_cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps((now_ms, td)))
    td.fetched_ms = now_ms
    return td


def in_sample(mint: str, rate: float) -> bool:
    """Tirage uniforme et reproductible d'un lancement (hachage de l'adresse), indépendant de son issue."""
    return rate >= 1 or (int(hashlib.sha256(mint.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF) < rate


def build_dataset(workers: int, max_tokens: int, reuse: bool, costs: pr.Costs,
                  sample_rate: float = 1.0, cached_only: bool = False, grad_sample_rate: float = 1.0) -> tuple[pd.DataFrame, dict]:
    U = pf.load_universe()
    now_ms = int(time.time() * 1000)
    info: dict = {"now_ms": now_ms}
    launches = U.launches.copy()
    launches["std"] = [pf.is_standard_curve(r) for r in launches.to_dict("records")]
    grads = U.graduations.copy()
    grads["std"] = [pf.is_standard_curve(r) for r in grads.to_dict("records")]
    info["launches_seen"] = len(launches)
    info["launches_standard"] = int(launches["std"].sum())
    info["launches_mayhem"] = int(launches["mayhem_state"].notna().sum()) if "mayhem_state" in launches else 0
    info["graduations_seen"] = len(grads)
    info["graduations_standard"] = int(grads["std"].sum())
    ready = launches[launches["std"] & (launches["created_timestamp"] <= now_ms - 17 * pr.MIN_MS)]
    ready = ready[[in_sample(m, sample_rate) for m in ready["mint"]]]
    info["launch_sample_rate"] = sample_rate
    info["grad_sample_rate"] = grad_sample_rate
    jobs: dict[str, tuple[dict, tuple[str, ...], str]] = {}
    for c in ready.to_dict("records"):
        jobs[c["mint"]] = (c, pr.MOMENTS, "launches")
    for c in grads[grads["std"]].to_dict("records"):
        if c["mint"] not in jobs and in_sample(c["mint"], grad_sample_rate):
            jobs[c["mint"]] = (c, ("post_grad",), "graduations")
    items = list(jobs.values())
    if max_tokens:
        items = items[-max_tokens:]
    if cached_only:
        items = [it for it in items if _cache_path(it[0]["mint"]).exists()]
        reuse = True
    info["tokens_requested"] = len(items)
    if items:
        span = [min(c["created_timestamp"] for c, _, _ in items), max(c["created_timestamp"] for c, _, _ in items)]
        info["created_span_ms"] = span
    cli = pf.PumpFunClient(per_second=0.33)        # swap-api : ≈ 20 requêtes par minute
    dex_cli = pf.PumpFunClient(per_second=0.9)
    rows, notes, failed = [], {}, 0
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(load_or_fetch, cli, dex_cli, c, m, now_ms, reuse): (c, m, src) for c, m, src in items}
        for k, fut in enumerate(as_completed(futs), 1):
            c, m, src = futs[fut]
            try:
                td = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("%s : %r", c["mint"], exc)
                continue
            for n in td.notes:
                notes[n.split(":")[0]] = notes.get(n.split(":")[0], 0) + 1
            for r in pr.token_rows(td, min(now_ms, getattr(td, "fetched_ms", now_ms)), costs, m):
                r["source"] = src
                rows.append(r)
            if k % 200 == 0:
                log.info("%d / %d tokens (%.0f s)", k, len(items), time.time() - t0)
    info["tokens_failed"] = failed
    info["notes"] = notes
    df = pd.DataFrame(rows)
    return df, info


# ---------------------------------------------------------------------------
# 2. Analyses
# ---------------------------------------------------------------------------
def feature_columns(df: pd.DataFrame) -> list[str]:
    skip = NON_FEATURES | {c for c in df.columns if c.startswith(("maxmult_", "pnl_", "grad_", "x3_", "x5_", "x10_",
                                                                     "mcap100k_", "mcap1m_"))}
    cols = [c for c in df.columns if c not in skip and pd.api.types.is_numeric_dtype(df[c])]
    return [c for c in cols if df[c].notna().mean() > 0.5 and df[c].nunique() > 1]


def target_frame(df: pd.DataFrame, moment: str, target: str, h: int) -> pd.DataFrame:
    """Lignes du moment dont l'étiquette est connue ; pour la market cap, seulement si le seuil n'est pas déjà atteint."""
    col = f"{target}_{h}h"
    if col not in df:
        return df.iloc[:0]
    d = df[(df["moment"] == moment) & df[col].notna()].copy()
    if target in pr.MCAPS_USD:
        d = d[d["p_ref"] * pf.TOTAL_SUPPLY * d["sol_usd"] < pr.MCAPS_USD[target]]
    d["y"] = d[col].astype(int)
    return d


def auc_and_p(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    ok = np.isfinite(x)
    x, y = x[ok], y[ok]
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 < 3 or n0 < 3:
        return math.nan, math.nan
    r = rankdata(x)
    auc = (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
    se = math.sqrt((n1 + n0 + 1) / (12 * n1 * n0))
    return float(auc), float(2 * norm.sf(abs(auc - 0.5) / se))


def univariate(df: pd.DataFrame, feats: list[str]) -> pd.DataFrame:
    out = []
    for m in pr.MOMENTS:
        for tg in TARGETS:
            for h in pr.HORIZONS_H:
                d = target_frame(df, m, tg, h)
                if len(d) == 0 or d["y"].sum() < 5:
                    continue
                res = [(f, *auc_and_p(d[f].to_numpy(float), d["y"].to_numpy())) for f in feats if f in d]
                t = pd.DataFrame(res, columns=["feature", "auc", "p"]).dropna()
                if t.empty:
                    continue
                t["q"] = benjamini_hochberg(t["p"].to_numpy())
                t.insert(0, "moment", m)
                t.insert(1, "target", tg)
                t.insert(2, "h", h)
                t["n"], t["n_pos"] = len(d), int(d["y"].sum())
                out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _signed_log(X: np.ndarray) -> np.ndarray:
    return np.sign(X) * np.log1p(np.abs(X))


def make_model(kind: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler
    if kind == "logit":
        return make_pipeline(SimpleImputer(strategy="median"), FunctionTransformer(_signed_log),
                             StandardScaler(), LogisticRegression(C=0.2, class_weight="balanced", max_iter=2000))
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=40,
                                          l2_regularization=1.0, class_weight="balanced", random_state=0)


def boot_ci(stat, n: int, groups: np.ndarray, n_boot: int = 1000, seed: int = 0) -> tuple[float, float]:
    """IC 95 % par bootstrap en tirant des heures entières (les tokens d'une même heure se ressemblent)."""
    rng = np.random.default_rng(seed)
    ug = np.unique(groups)
    idx_by = {g: np.flatnonzero(groups == g) for g in ug}
    vals = []
    for _ in range(n_boot):
        pick = rng.choice(ug, ug.size)
        idx = np.concatenate([idx_by[g] for g in pick])
        v = stat(idx)
        if np.isfinite(v):
            vals.append(v)
    if len(vals) < 50:
        return math.nan, math.nan
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def evaluate_models(df: pd.DataFrame, feats: list[str], h_pnl: int) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    from sklearn.metrics import average_precision_score, roc_auc_score
    rows, strat, models = [], [], {}
    for m in pr.MOMENTS:
        for tg in TARGETS:
            for h in pr.HORIZONS_H:
                d = target_frame(df, m, tg, h).sort_values("t_ms")
                if len(d) < 100:
                    continue
                cut = int(len(d) * 0.6)
                tr, te = d.iloc[:cut], d.iloc[cut:]
                if tr["y"].sum() < 15 or te["y"].sum() < 5 or (1 - te["y"]).sum() < 5:
                    continue
                f = [c for c in feats if c in d and tr[c].notna().any()]
                hours = (te["t_ms"] // pr.HOUR_MS).to_numpy()
                yte = te["y"].to_numpy()
                for kind in ("logit", "gbm"):
                    mdl = make_model(kind).fit(tr[f].to_numpy(float), tr["y"].to_numpy())
                    s = mdl.predict_proba(te[f].to_numpy(float))[:, 1]
                    auc = roc_auc_score(yte, s)
                    lo, hi = boot_ci(lambda i: roc_auc_score(yte[i], s[i]) if 0 < yte[i].sum() < i.size else math.nan,
                                     len(te), hours, n_boot=500)
                    k = max(1, int(round(TOP_FRAC * len(te))))
                    top = np.argsort(-s)[:k]
                    rows.append({"moment": m, "target": tg, "h": h, "model": kind, "n_train": len(tr),
                                 "n_test": len(te), "pos_train": int(tr["y"].sum()), "pos_test": int(yte.sum()),
                                 "base_rate": yte.mean(), "auc": auc, "auc_lo": lo, "auc_hi": hi,
                                 "ap": average_precision_score(yte, s), "prec_top10": yte[top].mean(),
                                 "lift_top10": yte[top].mean() / yte.mean()})
                    if kind == "logit":
                        models[(m, tg, h)] = (mdl, f)
                    pc = f"pnl_hold_{h_pnl}h"
                    if pc not in te or te[pc].notna().sum() < 50:
                        continue
                    for rule in ("hold", "tp2", "tp3"):
                        col = f"pnl_{rule}_{h_pnl}h"
                        v = te[col].to_numpy(float)
                        ok = np.isfinite(v)
                        sel = np.zeros(len(te), bool)
                        sel[top] = True
                        diff = lambda i: np.nanmean(v[i][sel[i] & ok[i]]) - np.nanmean(v[i][ok[i]])  # noqa: E731
                        lo2, hi2 = boot_ci(diff, len(te), hours, n_boot=1000)
                        strat.append({"moment": m, "target": tg, "h": h, "model": kind, "rule": rule, "h_pnl": h_pnl,
                                      "n_sel": int((sel & ok).sum()), "n_all": int(ok.sum()),
                                      "mean_sel": np.nanmean(v[sel & ok]) - 1, "median_sel": np.nanmedian(v[sel & ok]) - 1,
                                      "win_sel": np.mean(v[sel & ok] > 1), "mean_all": np.nanmean(v[ok]) - 1,
                                      "median_all": np.nanmedian(v[ok]) - 1, "win_all": np.mean(v[ok] > 1),
                                      "diff_lo": lo2, "diff_hi": hi2})
    return pd.DataFrame(rows), pd.DataFrame(strat), models


def export_model(mdl, feats: list[str], d: pd.DataFrame, meta: dict, path: Path) -> None:
    """Coefficients de la logistique en JSON (lisibles et rejouables par le détecteur live sans pickle)."""
    imp, _, sc, lr = mdl.steps[0][1], mdl.steps[1][1], mdl.steps[2][1], mdl.steps[3][1]
    s = mdl.predict_proba(d[feats].to_numpy(float))[:, 1]
    payload = {**meta, "features": feats, "median": imp.statistics_.tolist(), "mean": sc.mean_.tolist(),
               "scale": sc.scale_.tolist(), "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0]),
               "score_top10_threshold": float(np.quantile(s, 1 - TOP_FRAC)), "transform": "signed_log1p"}
    path.write_text(json.dumps(payload, indent=1))


def runner_rates(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for m in pr.MOMENTS:
        for tg in TARGETS:
            for h in pr.HORIZONS_H:
                d = target_frame(df, m, tg, h)
                if len(d) == 0:
                    continue
                p = d["y"].mean()
                se = math.sqrt(max(p * (1 - p), 1e-12) / len(d))
                out.append({"moment": m, "target": tg, "h": h, "n": len(d), "n_pos": int(d["y"].sum()), "rate": p,
                            "lo": max(0.0, p - 1.96 * se), "hi": min(1.0, p + 1.96 * se)})
    return pd.DataFrame(out)


def social_table(df: pd.DataFrame, h: int) -> pd.DataFrame:
    groups = {"sans lien X": lambda d: d["has_twitter"] == 0, "compte X": lambda d: d["tw_profile"] == 1,
              "tweet X": lambda d: d["tw_status"] == 1, "communauté X": lambda d: d["tw_community"] == 1,
              "Telegram": lambda d: d["has_telegram"] == 1, "site propre": lambda d: (d["has_website"] == 1) & (d["website_launchpad"] == 0),
              "aucun lien": lambda d: d["n_socials"] == 0, "fiche DexScreener payée": lambda d: d["dex_paid_profile"] == 1,
              "boost DexScreener": lambda d: d["dex_boost_amount"] > 0}
    out = []
    for m in pr.MOMENTS:
        for tg in ("x10", "grad", "mcap1m"):
            d = target_frame(df, m, tg, h)
            if len(d) < 50:
                continue
            for g, fn in groups.items():
                try:
                    s = d[fn(d).fillna(False).astype(bool)]
                except KeyError:
                    continue
                if len(s) < 10:
                    continue
                out.append({"moment": m, "target": tg, "h": h, "groupe": g, "n": len(s), "rate": s["y"].mean(),
                            "rate_all": d["y"].mean()})
    return pd.DataFrame(out)


def pnl_table(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for m in pr.MOMENTS:
        d = df[df["moment"] == m]
        for h in pr.HORIZONS_H:
            for rule in ("hold", "tp2", "tp3", "tp5", "tp10"):
                c = f"pnl_{rule}_{h}h"
                if c not in d or d[c].notna().sum() < 20:
                    continue
                v = d[c].dropna().to_numpy()
                out.append({"moment": m, "h": h, "rule": rule, "n": v.size, "mean": v.mean() - 1,
                            "median": np.median(v) - 1, "win": np.mean(v > 1), "p90": np.quantile(v, 0.9) - 1,
                            "max": v.max() - 1})
    return pd.DataFrame(out)


def plot_rates(rates: pd.DataFrame, path: Path) -> None:
    """Part des runners selon le moment de détection (IC 95 %), une colonne par définition."""
    from tradebot.report import BG, GRID, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes
    plt = _pyplot()
    h = 6 if (rates["h"] == 6).any() else 1
    tg = [t for t in ("x10", "grad", "mcap1m") if ((rates["target"] == t) & (rates["h"] == h)).any()]
    if not tg:
        return
    W = 10.0
    title, sub, hh = _header(W, f"Runners pump.fun : ce que change le moment de détection (horizon {h} h)",
                             "Part des tokens qui deviennent des runners après l'instant t, selon la définition ; "
                             "barres : IC 95 %.")
    fig, axes = plt.subplots(1, len(tg), figsize=(W, 3.6 + hh), facecolor=BG, squeeze=False)
    fig.subplots_adjust(top=1 - hh / (3.6 + hh), bottom=0.2, left=0.2, right=0.98, wspace=0.35)
    for ax, t in zip(axes[0], tg):
        d = rates[(rates["target"] == t) & (rates["h"] == h)].set_index("moment").reindex(list(pr.MOMENTS)).dropna(subset=["n"])
        y = np.arange(len(d))[::-1]
        ax.errorbar(100 * d["rate"], y, xerr=[100 * (d["rate"] - d["lo"]), 100 * (d["hi"] - d["rate"])], fmt="o",
                    color="#2a78d6", ecolor="#8a94a3", capsize=3)
        ax.set_yticks(y, [f"{MOMENT_LABEL[m]} (n={int(n)})" for m, n in zip(d.index, d["n"])] if ax is axes[0][0]
                      else [""] * len(y), fontsize=8, color=TEXT)
        ax.set_title(TARGET_LABEL[t], fontsize=10, color=TEXT)
        ax.set_xlabel("% de runners", fontsize=9, color=TEXT_2)
        ax.set_xlim(left=0)
        _style_axes(ax, ygrid=False, xgrid=True)
    _draw_header(fig, title, sub)
    _save(fig, path)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sample-rate", type=float, default=0.20, help="part des lancements étudiés (tirage uniforme)")
    ap.add_argument("--max-tokens", type=int, default=0, help="n'étudier que les N tokens les plus récents (essai)")
    ap.add_argument("--reuse", action="store_true", help="réutiliser le cache par token même s'il n'est pas final")
    ap.add_argument("--size-sol", type=float, default=0.5)
    ap.add_argument("--latency-s", type=float, default=2.0)
    ap.add_argument("--pnl-h", type=int, default=1, help="horizon (h) du P&L de la stratégie top 10 %%")
    ap.add_argument("--grad-sample-rate", type=float, default=0.5, help="part des graduations étudiées (tirage uniforme)")
    ap.add_argument("--cached-only", action="store_true", help="n'utiliser que les tokens déjà téléchargés")
    ap.add_argument("--report-only", action="store_true", help="relire dataset.parquet sans rien télécharger")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.time()
    costs = pr.Costs(size_sol=a.size_sol, latency_s=a.latency_s)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.report_only:
        df = pd.read_parquet(OUT / "dataset.parquet")
        info = json.loads((OUT / "run.json").read_text())
    else:
        df, info = build_dataset(a.workers, a.max_tokens, a.reuse, costs, a.sample_rate, a.cached_only, a.grad_sample_rate)
        if df.empty:
            log.error("aucune ligne : laisser tourner le collecteur")
            return 1
        df.to_parquet(OUT / "dataset.parquet", index=False)
    feats = feature_columns(df)
    rates = runner_rates(df)
    uni = univariate(df, feats)
    mets, strat, models = evaluate_models(df, feats, a.pnl_h)
    soc = pd.concat([social_table(df, h) for h in (1, 6)], ignore_index=True)
    pnl = pnl_table(df)
    for name, t in (("taux_runners", rates), ("variables_auc", uni), ("modeles", mets), ("strategie_top10", strat),
                    ("social", soc), ("pnl_par_moment", pnl)):
        t.to_csv(OUT / f"{name}.csv", index=False)
    for (m, tg, h), (_, f) in models.items():
        d = target_frame(df, m, tg, h)                        # modèle final : toutes les lignes étiquetées
        mdl = make_model("logit").fit(d[f].to_numpy(float), d["y"].to_numpy())
        export_model(mdl, f, d, {"moment": m, "target": tg, "h": h, "n": len(d), "n_pos": int(d["y"].sum()),
                                 "trained_until_ms": int(d["t_ms"].max())}, OUT / f"modele_{m}_{tg}_{h}h.json")
    plot_rates(rates, OUT / "taux_runners.png")
    info.update({"rows": len(df), "rows_by_moment": df["moment"].value_counts().to_dict(), "features": feats,
                 "costs": costs.__dict__})
    if not a.report_only:
        info["seconds"] = round(time.time() - t0)
    (OUT / "run.json").write_text(json.dumps(info, indent=1, default=str))
    print(json.dumps({k: info.get(k) for k in ("rows", "rows_by_moment", "tokens_requested", "tokens_failed", "notes",
                                           "seconds")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
