#!/usr/bin/env python
"""Backtest de la « rotation » entre memecoins (fil X « Ultimate Memecoin Trading Guide », 2026).

Question : vendre le memecoin qui vient de faire 3x et acheter celui qui est en bas de sa
fourchette rapporte-t-il plus que garder ses pièces, ou qu'un panier, une fois les frais payés ?

Données publiques, lecture seule (voir ``tradebot.memecoins``) : les 55 perpétuels USDT classés
« Meme » par Binance, retirés de la cote compris, en bougies quotidiennes. Sorties dans
``reports/memecoins/``.

    python scripts/memecoin_rotation.py [--refresh] [--placebo 500] [--boot 2000]
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import memecoins as mc
from tradebot.config import REPORTS_DIR, ROOT
from tradebot.report import BG, GRID, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes, fmt_number

log = logging.getLogger("memecoin_rotation")
OUT = REPORTS_DIR / "memecoins"
A_PRIORI = mc.RotationParams()          # règle de l'auteur, fixée avant tout test
MIN_ELIGIBLE = 8                         # début de l'étude : au moins 8 pièces éligibles
HORIZONS = (30, 60)
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_VIOLET, C_GREY = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#4a3aa7", "#8a94a3"
GRID_SPACE = {"mult": (2.0, 3.0, 4.0), "low_win": (14, 30), "range_win": (30, 60, 90),
              "bottom": (0.1, 0.2, 0.3), "k": (1, 2, 3)}


def fr(x, d: int = 1, signed: bool = False, pct: bool = False) -> str:
    return fmt_number(x, d, signed=signed, pct=pct)


def pc(x, d: int = 0, signed: bool = False) -> str:
    """Pourcentage à la française (x en fraction)."""
    if x is None or not np.isfinite(x):
        return "—"
    return fr(100 * x, d, signed=signed) + "\u00a0%"


def xm(x, d: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else fr(x, d) + "\u00a0×"


def day(d) -> str:
    return pd.Timestamp(d).strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# 1. États A (« vient de faire 3x ») et B (« bas de fourchette ») : que se passe-t-il ensuite ?
# ---------------------------------------------------------------------------
def states(panel: mc.Panel, el: pd.DataFrame, p: mc.RotationParams) -> dict[str, pd.DataFrame]:
    C = panel.close
    did = (C >= p.mult * mc.rolling_low(C, p.low_win)) & el
    bot = (mc.range_position(C, p.range_win) <= p.bottom) & el
    return {"A": did & ~did.shift(1, fill_value=False), "B": bot & ~bot.shift(1, fill_value=False), "tous": el}


def cluster_boot(vals: np.ndarray, groups: np.ndarray, stat, n_boot: int, seed: int = 0) -> tuple[float, float]:
    """IC 95 % par bootstrap en tirant des semaines entières (événements d'une même semaine corrélés)."""
    rng = np.random.default_rng(seed)
    ug = np.unique(groups)
    by = {g: vals[groups == g] for g in ug}
    out = []
    for _ in range(n_boot):
        pick = rng.choice(ug, ug.size)
        out.append(stat(np.concatenate([by[g] for g in pick])))
    return float(np.nanpercentile(out, 2.5)), float(np.nanpercentile(out, 97.5))


def event_study(panel, el, start, n_boot) -> pd.DataFrame:
    st = states(panel, el, A_PRIORI)
    after = (panel.dates >= start)[:, None]
    rows = []
    for h in HORIZONS:
        fw = mc.forward_stats(panel, h)
        for key, lab in (("A", "A : vient de faire 3x"), ("B", "B : bas de fourchette"), ("tous", "toute pièce éligible, tout jour")):
            m = st[key].to_numpy() & after
            r, mx, mn = (fw[k].to_numpy()[m] for k in ("ret", "max", "min"))
            wk = np.broadcast_to(panel.dates.to_numpy()[:, None], m.shape)[m].astype("datetime64[W]").astype(int)
            ok = np.isfinite(r) & np.isfinite(mx) & np.isfinite(mn)
            r, mx, mn, wk = r[ok], mx[ok], mn[ok], wk[ok]
            row = {"horizon_j": h, "etat": lab, "n": int(r.size), "semaines": int(np.unique(wk).size),
                   "ret_median": float(np.median(r)), "ret_moyen": float(r.mean()),
                   "p_double": float((mx >= 1.0).mean()), "p_triple": float((mx >= 2.0).mean()),
                   "p_moins30": float((mn <= -0.3).mean()), "p_moins70": float((mn <= -0.7).mean()),
                   "max_median": float(np.median(mx)), "min_median": float(np.median(mn))}
            if key != "tous":
                row["ret_median_lo"], row["ret_median_hi"] = cluster_boot(r, wk, np.median, n_boot)
                row["p_triple_lo"], row["p_triple_hi"] = cluster_boot((mx >= 2.0).astype(float), wk, np.mean, n_boot)
                row["p_moins30_lo"], row["p_moins30_hi"] = cluster_boot((mn <= -0.3).astype(float), wk, np.mean, n_boot)
            rows.append(row)
    return pd.DataFrame(rows)


def paired_rotations(panel, el, start, n_boot, cost: float) -> pd.DataFrame:
    """Chaque fois qu'une pièce A fait 3x : B = la pièce éligible la plus basse dans sa fourchette
    (si <= 20 %). Compare le rendement de B et de A sur h jours (log), coûts de la rotation déduits,
    et le compare au choix d'une pièce éligible quelconque (moyenne de toutes les autres)."""
    p = A_PRIORI
    C = panel.close
    st = states(panel, el, p)["A"]
    rp = mc.range_position(C, p.range_win)
    rows = []
    for h in HORIZONS:
        fw = mc.forward_stats(panel, h)["ret"]
        recs = []
        for d, a in zip(*np.nonzero(st.to_numpy())):
            date = panel.dates[d]
            if date < start or not np.isfinite(fw.iat[d, a]):
                continue
            cand = np.flatnonzero(el.to_numpy()[d])
            cand = cand[cand != a]
            r = rp.to_numpy()[d, cand]
            f = fw.to_numpy()[d, cand]
            ok = np.isfinite(r) & np.isfinite(f)
            if not ok.any():
                continue
            cand, r, f = cand[ok], r[ok], f[ok]
            j = int(np.argmin(r))
            if r[j] > p.bottom:
                continue
            la = math.log1p(fw.iat[d, a])
            recs.append({"date": date, "A": panel.symbols[a], "B": panel.symbols[cand[j]],
                         "diff_B": math.log1p(f[j]) - la + 2 * math.log1p(-cost),
                         "diff_autre": float(np.mean(np.log1p(f))) - la + 2 * math.log1p(-cost)})
        df = pd.DataFrame(recs)
        if df.empty:
            continue
        wk = df["date"].to_numpy().astype("datetime64[W]").astype(int)
        for col, lab in (("diff_B", "rotation vers B (bas de fourchette)"), ("diff_autre", "rotation vers une pièce quelconque")):
            v = df[col].to_numpy()
            lo, hi = cluster_boot(v, wk, np.mean, n_boot)
            rows.append({"horizon_j": h, "choix": lab, "n": len(v), "semaines": int(np.unique(wk).size),
                         "gain_log_moyen": float(v.mean()), "ic_lo": lo, "ic_hi": hi,
                         "gain_median": float(np.median(v)), "p_gagne": float((v > 0).mean())})
        if h == 60:
            df.to_csv(OUT / "rotations_appariees_60j.csv", index=False)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Les pièces en retard rattrapent-elles ? (corrélation de rang, jour par jour)
# ---------------------------------------------------------------------------
def ic_table(panel, el, start) -> pd.DataFrame:
    C = panel.close
    sigs = {"position dans la fourchette 60 j": mc.range_position(C, 60),
            "rendement des 30 derniers jours": C / C.shift(30) - 1,
            "rendement des 7 derniers jours": C / C.shift(7) - 1}
    rows = []
    for h in (7, 30):
        fw = mc.forward_stats(panel, h)["ret"]
        for name, s in sigs.items():
            ic = mc.cross_sectional_ic(s[s.index >= start], fw[fw.index >= start], el[el.index >= start])
            rows.append({"signal": name, "horizon_j": h, "jours": int(ic.size), "ic_moyen": float(ic.mean()),
                         "t_nw": mc.newey_west_t(ic, lags=h), "part_positive": float((ic > 0).mean())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. Portefeuilles
# ---------------------------------------------------------------------------
def n_rot(res: mc.SimResult) -> int:
    return int((res.trades["why"] == "rotation").sum()) if len(res.trades) else 0


def main_runs(panel, el, start, n_placebo):
    p = A_PRIORI
    runs = {
        "rotation (règle de l'auteur)": mc.simulate_rotation(panel, el, p, start=start),
        "détenteur (mêmes achats de départ, aucune rotation)": mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=start),
        "rotation inverse (vers le haut de fourchette)": mc.simulate_rotation(panel, el, mc.with_params(p, target="top"), start=start),
        "panier équipondéré (rééquilibré tous les 30 j)": mc.simulate_basket(panel, el, cost=p.cost, start=start),
    }
    plac = []
    for s in range(n_placebo):
        r = mc.simulate_rotation(panel, el, mc.with_params(p, target="random", seed=s), start=start)
        plac.append(r.returns)
    return runs, pd.concat(plac, axis=1)


def perf_table(runs: dict) -> pd.DataFrame:
    rows = []
    for k, res in runs.items():
        row = {"strategie": k, **mc.perf(res.returns)}
        row["rotations"] = n_rot(res) if len(res.trades) and "why" in res.trades else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def rolling_starts(panel, el, start, end, months=12) -> pd.DataFrame:
    """Départs mensuels, 12 mois de détention : la rotation bat-elle le détenteur / le panier ?"""
    p = A_PRIORI
    rows = []
    s = start
    while s + pd.Timedelta(days=30 * months) <= end:
        e = s + pd.Timedelta(days=30 * months)
        f = {}
        for lab, fn in (("rotation", lambda: mc.simulate_rotation(panel, el, p, start=s, end=e)),
                        ("detenteur", lambda: mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=s, end=e)),
                        ("inverse", lambda: mc.simulate_rotation(panel, el, mc.with_params(p, target="top"), start=s, end=e)),
                        ("panier", lambda: mc.simulate_basket(panel, el, cost=p.cost, start=s, end=e))):
            res = fn()
            f[lab] = float((1 + res.returns.dropna()).prod())
            if lab == "rotation":
                f["rotations"] = n_rot(res)
        rows.append({"debut": s, "fin": e, **f})
        s = s + pd.Timedelta(days=30)
    return pd.DataFrame(rows)


def grid_runs(panel, el, basket: pd.Series, start, end=None):
    keys = list(GRID_SPACE)
    rows, exc = [], {}
    for vals in itertools.product(*(GRID_SPACE[k] for k in keys)):
        kw = dict(zip(keys, vals))
        res = mc.simulate_rotation(panel, el, mc.with_params(A_PRIORI, **kw), start=start, end=end)
        r = res.returns.dropna()
        b = basket.reindex(r.index)
        x = np.log1p(r) - np.log1p(b)
        name = "mult={mult:g} low={low_win} range={range_win} bottom={bottom:g} k={k}".format(**kw)
        exc[name] = x
        rows.append({"variante": name, **kw, **mc.perf(r), "rotations": n_rot(res),
                     "exces_log_an": float(x.mean() * 365), "sharpe_exces": float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else np.nan,
                     "a_priori": all(getattr(A_PRIORI, k) == v for k, v in kw.items())})
    return pd.DataFrame(rows), pd.DataFrame(exc)


def robustness(panel, el_base, start, basket_cost_fn) -> pd.DataFrame:
    p = A_PRIORI
    el_comm = mc.eligibility(panel, community=True)
    cases = [("règle de l'auteur (0,75 % par échange, exécution à l'ouverture de J+1)", p, el_base),
             ("frais 0,25 % par échange", mc.with_params(p, cost=0.0025), el_base),
             ("frais 1,5 % par échange", mc.with_params(p, cost=0.015), el_base),
             ("exécution à la clôture de J+1", mc.with_params(p, execution="next_close"), el_base),
             ("pièce retirée de la cote revendue −50 %", mc.with_params(p, delist_haircut=0.5), el_base),
             ("k = 1 pièce", mc.with_params(p, k=1), el_base),
             ("k = 3 pièces", mc.with_params(p, k=3), el_base),
             ("seuil 2x au lieu de 3x", mc.with_params(p, mult=2.0), el_base),
             ("univers « community coins » (déjà −70 % puis ×2)", p, el_comm)]
    rows = []
    for lab, q, el in cases:
        rot = mc.simulate_rotation(panel, el, q, start=start)
        hold = mc.simulate_rotation(panel, el, mc.with_params(q, target="none"), start=start)
        bas = mc.simulate_basket(panel, el, cost=q.cost, delist_haircut=q.delist_haircut, start=start)
        pr, ph, pb = mc.perf(rot.returns), mc.perf(hold.returns), mc.perf(bas.returns)
        rows.append({"cas": lab, "rotation_x": pr.get("total_x"), "detenteur_x": ph.get("total_x"), "panier_x": pb.get("total_x"),
                     "rotation_dd": pr.get("max_dd"), "rotations": n_rot(rot)})
    return pd.DataFrame(rows)


def regimes(panel, el, rot: pd.Series, bas: pd.Series) -> pd.DataFrame:
    """Régime du jour d : indice équipondéré de toutes les pièces actives au-dessus / en dessous de sa
    moyenne mobile 200 j, mesuré à la clôture de d − 1 (pas d'information du jour même)."""
    r = panel.close.pct_change(fill_method=None).where(panel.active & panel.active.shift(1, fill_value=False))
    idx = (1 + r.mean(axis=1).fillna(0)).cumprod()
    up = (idx > idx.rolling(200, min_periods=200).mean()).shift(1)
    rows = []
    for lab, sel in (("haussier (indice > MM 200 j)", up == True), ("baissier (indice < MM 200 j)", up == False)):  # noqa: E712
        s = sel.reindex(rot.index).fillna(False).to_numpy(bool)
        for name, x in (("rotation", rot), ("panier", bas)):
            v = x.to_numpy()[s]
            v = v[np.isfinite(v)]
            rows.append({"regime": lab, "strategie": name, "jours": int(v.size),
                         "rendement_log_an": float(np.log1p(v).mean() * 365) if v.size else np.nan,
                         "multiple": float(np.prod(1 + v)) if v.size else np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Graphiques
# ---------------------------------------------------------------------------
def fig_equity(runs: dict, plac: pd.DataFrame, path: Path, title: str, subtitle: str):
    plt = _pyplot()
    W = 10.0
    t, s, hh = _header(W, title, subtitle)
    fig = plt.figure(figsize=(W, 5.2 + hh), facecolor=BG)
    ax = fig.add_axes([0.07, 0.1, 0.72, (5.2 - 0.7) / (5.2 + hh)])
    _style_axes(ax)
    eqp = (1 + plac.fillna(0)).cumprod()
    lo, med, hi = eqp.quantile(0.05, axis=1), eqp.median(axis=1), eqp.quantile(0.95, axis=1)
    ax.fill_between(eqp.index, lo, hi, color=C_GREY, alpha=0.18, lw=0)
    ax.plot(eqp.index, med, color=C_GREY, lw=1.2, ls="--")
    colors = [C_BLUE, C_AQUA, C_ORANGE, TEXT]
    short = ["rotation (auteur)", "détenteur", "rotation inverse", "panier"]
    ends = []
    for (k, res), c, lab in zip(runs.items(), colors, short):
        eq = (1 + res.returns.fillna(0)).cumprod()
        ax.plot(eq.index, eq, color=c, lw=2.2 if "auteur" in k else 1.6)
        ends.append((float(eq.iloc[-1]), lab, c))
    ends.append((float(med.iloc[-1]), "au hasard (médiane,\nbande 5–95 %)", C_GREY))
    ax.set_yscale("log")
    ax.axhline(1, color=GRID, lw=1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: fr(v, 2 if v < 1 else 1) + " ×"))
    # étiquettes à droite, espacées
    ends.sort()
    ylo, yhi = [math.log10(v) for v in ax.get_ylim()]
    pos, last = [], -1e9
    for v, k, c in ends:
        y = max(math.log10(max(v, 1e-9)), last + (yhi - ylo) * 0.075)
        pos.append((y, v, k, c))
        last = y
    for y, v, k, c in pos:
        ax.annotate(f"{xm(v)} · {k}", xy=(eqp.index[-1], v), xytext=(1.02, (y - ylo) / (yhi - ylo)),
                    textcoords="axes fraction", color=c, fontsize=8.5, va="center", ha="left",
                    arrowprops=dict(arrowstyle="-", color=c, lw=0.6, alpha=0.6))
    _draw_header(fig, t, s)
    _save(fig, path)


def fig_states(ev: pd.DataFrame, path: Path, title: str, subtitle: str):
    plt = _pyplot()
    W = 10.0
    t, s, hh = _header(W, title, subtitle)
    fig = plt.figure(figsize=(W, 4.4 + hh), facecolor=BG)
    e = ev[ev["horizon_j"] == 60].set_index("etat")
    labs = list(e.index)
    metrics = [("p_triple", "fait +200 % (3x)\nà un moment dans les 60 j", "B : l'auteur suppose « le même 3x »"),
               ("p_moins30", "perd encore 30 %\nà un moment dans les 60 j", "B : l'auteur suppose « −30 % au pire »"),
               ("ret_median", "rendement médian\nà 60 jours", None)]
    cols = [C_ORANGE, C_BLUE, C_GREY]
    for j, (m, lab, note) in enumerate(metrics):
        H = 4.4 + hh
        ax = fig.add_axes([0.06 + j * 0.325, 0.75 / H, 0.27, (H - hh - 0.6 - 0.75) / H])
        _style_axes(ax)
        v = e[m].to_numpy()
        bars = ax.bar(range(len(labs)), v, color=cols, width=0.62)
        tops = v.copy()
        if m != "ret_median" and (m + "_hi") in e:
            tops = np.fmax(v, e[m + "_hi"].to_numpy())
        for b, x, tp in zip(bars, v, tops):
            if x >= 0:
                ax.text(b.get_x() + b.get_width() / 2, tp + 0.01, pc(x), ha="center", va="bottom", fontsize=9, color=TEXT)
            else:
                ax.text(b.get_x() + b.get_width() / 2, x / 2, pc(x), ha="center", va="center", fontsize=9, color="#ffffff")
        if m != "ret_median":
            lo, hi = e.get(m + "_lo"), e.get(m + "_hi")
            if lo is not None:
                for i, (a, b2) in enumerate(zip(lo, hi)):
                    if np.isfinite(a):
                        ax.plot([i, i], [a, b2], color=TEXT_2, lw=1)
        ax.set_xticks(range(len(labs)))
        ax.set_xticklabels(["A : vient de\nfaire 3x", "B : bas de\nfourchette", "pièce\nquelconque"], fontsize=8.5, color=TEXT_2)
        ax.set_title(lab, fontsize=9.5, color=TEXT, loc="left")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: pc(v)))
        if m == "ret_median":
            ax.axhline(0, color=TEXT_2, lw=0.8)
            ax.set_ylim(min(0.0, float(v.min()) * 1.15), max(0.02, float(v.max()) * 1.2))
        if m == "p_triple":
            ax.set_ylim(0, max(0.2, float(np.nanmax(tops)) * 1.15))
            ax.yaxis.set_major_locator(plt.MultipleLocator(0.05))
        if m == "p_moins30":
            ax.set_ylim(0, 1)
    _draw_header(fig, t, s)
    _save(fig, path)


def fig_hist(values: np.ndarray, marks: list[tuple[float, str, str]], path: Path, title: str, subtitle: str, xlabel: str, log: bool = True):
    plt = _pyplot()
    W = 10.0
    t, s, hh = _header(W, title, subtitle)
    fig = plt.figure(figsize=(W, 4.0 + hh), facecolor=BG)
    ax = fig.add_axes([0.07, 0.15, 0.9, (4.0 - 0.8) / (4.0 + hh)])
    _style_axes(ax)
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    x = np.log10(v) if log else v
    ax.hist(x, bins=40, color=C_GREY, alpha=0.55)
    ytop = ax.get_ylim()[1]
    for k, (val, lab, c) in enumerate(marks):
        xv = math.log10(val) if log else val
        ax.axvline(xv, color=c, lw=2)
        ax.text(xv, ytop * (0.95 - 0.12 * k), " " + lab, color=c, fontsize=9, va="top")
    if log:
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda u, _: fr(10 ** u, 2 if 10 ** u < 1 else 1) + " ×"))
    ax.set_xlabel(xlabel, color=TEXT_2, fontsize=9)
    _draw_header(fig, t, s)
    _save(fig, path)


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------
def md_table(df: pd.DataFrame, cols: list[tuple[str, str, object]]) -> str:
    head = "| " + " | ".join(c[1] for c in cols) + " |\n|" + "|".join("---" for _ in cols) + "|\n"
    body = ""
    for _, r in df.iterrows():
        cells = []
        for key, _, f in cols:
            v = r.get(key)
            cells.append(f(v) if callable(f) else (str(v) if v is not None else "—"))
        body += "| " + " | ".join(cells) + " |\n"
    return head + body


def src_line(snippet: str, path: str = "src/tradebot/memecoins.py") -> str:
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lines, 1):
        if snippet in l:
            return f"`{path}:{i}` : `{l.strip()}`"
    raise ValueError(f"citation introuvable : {snippet}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="retélécharger les bougies Binance")
    ap.add_argument("--placebo", type=int, default=500)
    ap.add_argument("--boot", type=int, default=2000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    panel, uni, ref = mc.load_all(refresh=args.refresh)
    el = mc.eligibility(panel)
    n_el = el.sum(axis=1)
    start = n_el[n_el >= MIN_ELIGIBLE].index[0]
    end = panel.dates[-1]
    log.info("univers %d pièces, étude du %s au %s", len(uni), day(start), day(end))
    delisted = [s for s in panel.symbols if not panel.active[s].iloc[-1]]
    uni_out = uni.copy()
    uni_out["premier_jour"] = [panel.active[s].idxmax() for s in uni_out["symbol"]]
    uni_out["dernier_jour"] = [panel.active[s][::-1].idxmax() for s in uni_out["symbol"]]
    uni_out["retire"] = uni_out["symbol"].isin(delisted)
    uni_out["jours_eligible"] = [int(el[s][el.index >= start].sum()) for s in uni_out["symbol"]]
    uni_out.to_csv(OUT / "univers.csv", index=False)

    ev = event_study(panel, el, start, args.boot)
    ev.to_csv(OUT / "etats_A_B.csv", index=False)
    pairs = paired_rotations(panel, el, start, args.boot, A_PRIORI.cost)
    pairs.to_csv(OUT / "rotations_appariees.csv", index=False)
    ic = ic_table(panel, el, start)
    ic.to_csv(OUT / "ic_retour_moyenne.csv", index=False)
    log.info("états et IC : %.0f s", time.time() - t0)

    runs, plac = main_runs(panel, el, start, args.placebo)
    perf = perf_table(runs)
    plac_x = (1 + plac.fillna(0)).prod().to_numpy()
    rot = runs["rotation (règle de l'auteur)"]
    rot_x = float((1 + rot.returns.fillna(0)).prod())
    pct_plac = float((plac_x < rot_x).mean())
    perf.to_csv(OUT / "portefeuilles.csv", index=False)
    rot.trades.to_csv(OUT / "rotations_regle_auteur.csv", index=False)
    log.info("portefeuilles + %d placebos : %.0f s", args.placebo, time.time() - t0)

    roll = rolling_starts(panel, el, start, end)
    roll.to_csv(OUT / "departs_glissants_12_mois.csv", index=False)

    basket = runs["panier équipondéré (rééquilibré tous les 30 j)"].returns
    grid, exc = grid_runs(panel, el, basket, start)
    grid.to_csv(OUT / "grille_162_variantes.csv", index=False)
    sr_trials = (exc.mean() / exc.std()).to_numpy()
    best = grid.sort_values("sharpe_exces", ascending=False).iloc[0]
    dsr = mc.deflated_sharpe(exc[best["variante"]].dropna().to_numpy(), sr_trials)
    apr = grid[grid["a_priori"]].iloc[0]
    psr_apr = mc.probabilistic_sharpe(exc[apr["variante"]].dropna().to_numpy(), 0.0)

    # walk-forward : choix sur la 1re moitié, test sur la 2e
    mid = start + (end - start) / 2
    bas1 = mc.simulate_basket(panel, el, cost=A_PRIORI.cost, start=start, end=mid).returns
    g1, _ = grid_runs(panel, el, bas1, start, end=mid)
    pick = g1.sort_values("sharpe_exces", ascending=False).iloc[0]
    kw = {k: pick[k] for k in GRID_SPACE}
    kw["k"], kw["low_win"], kw["range_win"] = int(kw["k"]), int(kw["low_win"]), int(kw["range_win"])
    r2 = mc.simulate_rotation(panel, el, mc.with_params(A_PRIORI, **kw), start=mid).returns
    b2 = mc.simulate_basket(panel, el, cost=A_PRIORI.cost, start=mid).returns
    h2 = mc.simulate_rotation(panel, el, mc.with_params(A_PRIORI, target="none", k=kw["k"]), start=mid).returns
    x2 = (np.log1p(r2) - np.log1p(b2.reindex(r2.index))).dropna()
    wf = {"coupure": str(mid.date()), "variante_choisie": pick["variante"], "exces_log_an_1re_moitie": float(pick["exces_log_an"]),
          "rotation_x_2e": float((1 + r2.dropna()).prod()), "panier_x_2e": float((1 + b2.dropna()).prod()),
          "detenteur_x_2e": float((1 + h2.dropna()).prod()), "exces_log_an_2e": float(x2.mean() * 365),
          "psr_2e": mc.probabilistic_sharpe(x2.to_numpy(), 0.0)}
    log.info("grille et walk-forward : %.0f s", time.time() - t0)

    rob = robustness(panel, el, start, None)
    rob.to_csv(OUT / "robustesse.csv", index=False)
    reg = regimes(panel, el, rot.returns, basket)
    reg.to_csv(OUT / "regimes.csv", index=False)

    # bootstrap de l'écart rotation − panier (log, par blocs de 30 j)
    xr = (np.log1p(rot.returns) - np.log1p(basket)).dropna().to_numpy()
    bs = mc.block_bootstrap(xr, lambda v: v.mean() * 365, block=30, n_boot=args.boot)
    xh = (np.log1p(rot.returns) - np.log1p(runs["détenteur (mêmes achats de départ, aucune rotation)"].returns)).dropna().to_numpy()
    bh = mc.block_bootstrap(xh, lambda v: v.mean() * 365, block=30, n_boot=args.boot, seed=1)
    boot = {"rot_moins_panier_log_an": float(xr.mean() * 365), "lo": float(np.percentile(bs, 2.5)), "hi": float(np.percentile(bs, 97.5)),
            "rot_moins_detenteur_log_an": float(xh.mean() * 365), "lo_h": float(np.percentile(bh, 2.5)), "hi_h": float(np.percentile(bh, 97.5))}

    # derniers 30 jours (la période citée par l'auteur)
    last30 = {k: float((1 + res.returns.iloc[-30:].fillna(0)).prod()) for k, res in runs.items()}

    # ------------------------------------------------------------------ graphiques
    evA = ev[(ev["horizon_j"] == 60) & ev["etat"].str.startswith("A")].iloc[0]
    evB = ev[(ev["horizon_j"] == 60) & ev["etat"].str.startswith("B")].iloc[0]
    evT = ev[(ev["horizon_j"] == 60) & ev["etat"].str.startswith("toute")].iloc[0]
    fig_states(ev, OUT / "etats_A_B.png",
               f"En bas de sa fourchette, un memecoin refait 3x dans {pc(evB['p_triple'])} des cas en 60 jours, et reperd 30 % dans {pc(evB['p_moins30'])} des cas",
               f"55 perpétuels « Meme » de Binance (retirés compris), {day(start)} – {day(end)}. A : 1er jour où la clôture vaut ≥ 3 × le plus bas des 30 j "
               f"(n = {int(evA['n'])}). B : 1er jour sous 20 % de la fourchette 60 j (n = {int(evB['n'])}). Barres : IC 95 % (bootstrap par semaine).")
    fig_equity(runs, plac, OUT / "portefeuilles.png",
               f"La rotation de l'auteur finit à {xm(rot_x)} la mise, le panier à {xm(float((1 + basket.fillna(0)).prod()))} : "
               f"elle fait mieux que {pc(pct_plac)} des rotations au hasard",
               f"Du {day(start)} au {day(end)}, 2 pièces détenues, 0,75 % de frais et glissement par échange, décision à la clôture et "
               f"exécution le lendemain. Échelle logarithmique.")
    fig_hist(grid["total_x"].to_numpy(), [(float(apr["total_x"]), "règle de l'auteur", C_BLUE),
                                          (float((1 + basket.fillna(0)).prod()), "panier équipondéré", TEXT)],
             OUT / "grille_variantes.png",
             f"{int((grid['exces_log_an'] > 0).sum())} réglages sur {len(grid)} battent le panier ; le meilleur ne résiste pas à la correction "
             f"pour essais multiples (DSR = {fr(dsr['dsr'], 2)})",
             "Multiple final de la mise pour 162 réglages de la rotation (seuil 2x/3x/4x, plus bas 14/30 j, fourchette 30/60/90 j, bas 10/20/30 %, "
             "1 à 3 pièces), même période, mêmes frais.", "multiple final de la mise (échelle log)")
    fig_hist(roll["rotation"].to_numpy() / roll["detenteur"].to_numpy(), [(1.0, "égalité", TEXT)], OUT / "departs_glissants.png",
             f"Sur {len(roll)} départs mensuels tenus 12 mois, la rotation fait mieux que garder ses pièces dans "
             f"{pc(float((roll['rotation'] > roll['detenteur']).mean()))} des cas",
             "Rapport « multiple de la rotation / multiple du détenteur » (mêmes achats de départ, frais compris). À droite de 1 : la rotation gagne.",
             "rotation / détenteur (échelle log)")

    # ------------------------------------------------------------------ README
    runtime = time.time() - t0
    meta = {"genere": pd.Timestamp.now(tz="UTC").isoformat(), "debut": str(start.date()), "fin": str(end.date()),
            "pieces": len(uni), "retirees": len(delisted), "placebos": args.placebo, "boot": args.boot,
            "dsr": dsr, "psr_a_priori": psr_apr, "walk_forward": wf, "bootstrap": boot, "derniers_30_jours": last30,
            "part_placebos_battus": pct_plac, "runtime_s": runtime}
    (OUT / "run.json").write_text(json.dumps(meta, indent=2, default=str, ensure_ascii=False))
    write_readme(uni_out, delisted, start, end, ev, pairs, ic, perf, plac_x, rot_x, pct_plac, roll, grid, dsr, apr, psr_apr,
                 best, wf, rob, reg, boot, last30, runtime)
    log.info("terminé en %.0f s", runtime)
    return 0


def write_readme(uni, delisted, start, end, ev, pairs, ic, perf, plac_x, rot_x, pct_plac, roll, grid, dsr, apr, psr_apr,
                 best, wf, rob, reg, boot, last30, runtime):
    e60 = ev[ev["horizon_j"] == 60].set_index("etat")
    A = e60.loc["A : vient de faire 3x"]
    B = e60.loc["B : bas de fourchette"]
    T = e60.loc["toute pièce éligible, tout jour"]
    p60 = pairs[pairs["horizon_j"] == 60].set_index("choix")
    pB = p60.loc["rotation vers B (bas de fourchette)"] if "rotation vers B (bas de fourchette)" in p60.index else None
    pX = p60.loc["rotation vers une pièce quelconque"] if "rotation vers une pièce quelconque" in p60.index else None
    ic_rp = ic[(ic["signal"] == "position dans la fourchette 60 j") & (ic["horizon_j"] == 30)].iloc[0]
    ic_m30 = ic[(ic["signal"] == "rendement des 30 derniers jours") & (ic["horizon_j"] == 30)].iloc[0]
    P = perf.set_index("strategie")
    rot = P.loc["rotation (règle de l'auteur)"]
    hold = P.loc["détenteur (mêmes achats de départ, aucune rotation)"]
    bas = P.loc["panier équipondéré (rééquilibré tous les 30 j)"]
    inv = P.loc["rotation inverse (vers le haut de fourchette)"]
    win_h = float((roll["rotation"] > roll["detenteur"]).mean())
    win_b = float((roll["rotation"] > roll["panier"]).mean())
    n_beat = int((grid["exces_log_an"] > 0).sum())
    def sign(v, t):
        if abs(t) < 2:
            return "non significatif (|t| < 2) : ni retour à la moyenne ni momentum démontré"
        return "retour à la moyenne (les pièces en retard rattrapent)" if v < 0 else "momentum (les pièces en avance continuent)"

    def evrow(r, lab):
        return (f"| {lab} | {int(r['n'])} | {pc(r['p_double'])} | {pc(r['p_triple'])} | {pc(r['p_moins30'])} | {pc(r['p_moins70'])} | "
                f"{pc(r['ret_median'], signed=True)} | {pc(r['ret_moyen'], signed=True)} |")

    L = []
    L.append("# Memecoins : la « rotation » entre community coins bat-elle la détention ?\n")
    L.append(f"*Généré le {pd.Timestamp.now(tz='UTC').strftime('%d/%m/%Y %H:%M')} UTC par `scripts/memecoin_rotation.py` ({runtime:.0f} s). "
             f"Données : {len(uni)} perpétuels USDT classés « Meme » par Binance, dont {len(delisted)} retirés de la cote, bougies quotidiennes ; "
             f"étude du {day(start)} au {day(end)}.*\n")
    L.append("> Recherche sur données publiques historiques, aucun ordre, aucune recommandation. Les liens de parrainage et le canal "
             "d'« appels » de l'article testé ne sont pas repris ici.\n")
    L.append("## 0. Réponse courte\n")
    L.append("La thèse testée (fil X « Ultimate Memecoin Trading Guide », 2026) : parmi des memecoins établis, vendre celui qui vient de faire "
             "3x (A) et acheter celui qui est en bas de sa fourchette (B), car B « va faire le même 3x » avec « −30 % au pire », alors que A "
             "« fera peut-être +100 % » avant « un repli typique de −70 % ». Exemple de l'auteur : rotateur 10 k$ → 270 k$, détenteur 10 k$ → 27 k$.\n")
    L.append(f"* **B ne fait pas « le même 3x ».** Au premier jour sous 20 % de sa fourchette de 60 jours, un memecoin atteint +200 % dans les "
             f"60 jours suivants dans **{pc(B['p_triple'])}** des cas (IC 95 % {pc(B['p_triple_lo'])} – {pc(B['p_triple_hi'])}), contre "
             f"{pc(T['p_triple'])} pour une pièce éligible quelconque un jour quelconque. Il reperd 30 % à un moment dans **{pc(B['p_moins30'])}** "
             f"des cas (l'auteur : « −30 % au pire »). Rendement médian à 60 jours : {pc(B['ret_median'], signed=True)}.")
    L.append(f"* **A retombe, mais pas du « −70 % typique ».** Après un 3x, rendement médian à 60 jours {pc(A['ret_median'], signed=True)} "
             f"(pièce quelconque : {pc(T['ret_median'], signed=True)} ; moyenne de A {pc(A['ret_moyen'], signed=True)}, tirée par quelques "
             f"envolées), plus bas médian {pc(A['min_median'], signed=True)}, −70 % touché dans {pc(A['p_moins70'])} des cas (pièce "
             f"quelconque : {pc(T['p_moins70'])}), +100 % dans {pc(A['p_double'])} (n = {int(A['n'])} événements, petit échantillon).")
    if pB is not None:
        L.append(f"* **La décision de rotation elle-même** (vendre A le jour de son 3x, acheter la pièce la plus basse dans sa fourchette, "
                 f"frais déduits) : écart moyen de rendement à 60 jours B − A = {pc(math.expm1(pB['gain_log_moyen']), signed=True)} "
                 f"(IC 95 % {pc(math.expm1(pB['ic_lo']), signed=True)} ; {pc(math.expm1(pB['ic_hi']), signed=True)}), B gagne dans "
                 f"{pc(pB['p_gagne'])} des {int(pB['n'])} cas. Acheter une pièce éligible quelconque à la place : "
                 f"{pc(math.expm1(pX['gain_log_moyen']), signed=True)}.")
        p30 = pairs[pairs["horizon_j"] == 30].set_index("choix")
        if "rotation vers une pièce quelconque" in p30.index:
            q30 = p30.loc["rotation vers une pièce quelconque"]
            b30 = p30.loc["rotation vers B (bas de fourchette)"]
            L.append(f"* **Vendre A après son 3x n'est pas absurde, acheter le bas de fourchette n'apporte rien.** À 30 jours, une pièce "
                     f"éligible quelconque fait en moyenne {pc(math.expm1(q30['gain_log_moyen']), signed=True)} de mieux que A "
                     f"(IC 95 % {pc(math.expm1(q30['ic_lo']), signed=True)} ; {pc(math.expm1(q30['ic_hi']), signed=True)} : limite, et non "
                     f"significatif à 60 jours), contre {pc(math.expm1(b30['gain_log_moyen']), signed=True)} pour la pièce en bas de "
                     f"fourchette (IC {pc(math.expm1(b30['ic_lo']), signed=True)} ; {pc(math.expm1(b30['ic_hi']), signed=True)}). Deux horizons "
                     f"essayés, {int(q30['n'])} événements : un indice, pas une règle.")
    L.append(f"* **Les pièces en retard ne rattrapent pas de façon exploitable.** Corrélation de rang, jour par jour, entre la position dans la "
             f"fourchette et le rendement des 30 jours suivants : {fr(ic_rp['ic_moyen'], 3, signed=True)} (t de Newey-West "
             f"{fr(ic_rp['t_nw'], 1, signed=True)}) ; avec le rendement des 30 derniers jours : {fr(ic_m30['ic_moyen'], 3, signed=True)} "
             f"(t {fr(ic_m30['t_nw'], 1, signed=True)}). Verdict : {sign(ic_rp['ic_moyen'], ic_rp['t_nw'])}.")
    L.append(f"* **Portefeuilles, frais compris** ({day(start)} – {day(end)}) : rotation de l'auteur {xm(rot['total_x'])} la mise "
             f"({int(rot['rotations'])} rotations, perte maximale {pc(rot['max_dd'])}), détenteur des mêmes pièces {xm(hold['total_x'])}, "
             f"panier équipondéré {xm(bas['total_x'])}, rotation inverse {xm(inv['total_x'])}. La rotation fait mieux que "
             f"{pc(pct_plac)} de 500 rotations vers une pièce **au hasard**.")
    L.append(f"* **Selon le point de départ** ({len(roll)} départs mensuels tenus 12 mois) : la rotation bat le détenteur dans {pc(win_h)} "
             f"des cas et le panier dans {pc(win_b)}.")
    L.append(f"* **En essayant 162 réglages**, {n_beat} battent le panier sur la période ; le meilleur ({best['variante']}) a un Sharpe "
             f"d'écart de {fr(best['sharpe_exces'], 2)} mais un **Sharpe dégonflé de {fr(dsr['dsr'], 2)}** (seuil 0,95) : indiscernable du "
             f"hasard une fois les essais comptés. Choisi sur la 1re moitié ({wf['variante_choisie']}), il fait {xm(wf['rotation_x_2e'])} sur la "
             f"2e moitié, contre {xm(wf['panier_x_2e'])} pour le panier et {xm(wf['detenteur_x_2e'])} pour le détenteur.")
    L.append(f"* **Verdict.** Sur les memecoins établis de Binance, la règle « vendre le 3x, acheter le bas de fourchette » ne montre aucun "
             f"avantage démontré sur la détention ou sur un panier. Le calcul de l'auteur (rapport gain/risque de 6,7 contre 1,4, 270 k$ contre "
             f"27 k$) suppose connue l'issue : que B refera 3x et ne perdra pas plus de 30 %. Mesuré, ce scénario arrive dans "
             f"{pc(B['p_triple'])} des cas pour le 3x, et la chute de 30 % dans {pc(B['p_moins30'])}.\n")

    L.append("## 1. Données et règle testée\n")
    L.append(f"* **Univers** : les perpétuels USDT que Binance classe « Meme » (`underlyingSubType`), y compris ceux retirés de la cote "
             f"({', '.join(sorted(s.replace('USDT', '') for s in delisted))}). Le prix du perpétuel suit le spot à quelques pb : il sert de prix, "
             f"sans levier ni financement. Liste : `univers.csv`.")
    L.append(f"* **Éligible au jour d** (point-in-time) : échangé ce jour, coté depuis au moins 60 jours, volume quotidien médian (30 j) d'au "
             f"moins 5 M$ (l'auteur veut pouvoir « entrer et sortir 50–500 k$ »). Début de l'étude : premier jour avec au moins {MIN_ELIGIBLE} "
             f"pièces éligibles ({day(start)}).")
    L.append("* **Règle de l'auteur, fixée avant tout test** : 2 pièces détenues. À la clôture de chaque jour, une pièce détenue qui vaut "
             "au moins 3 × son plus bas des 30 derniers jours est vendue et remplacée par la pièce éligible non détenue la plus basse dans sa "
             "fourchette de 60 jours, si elle est sous 20 % de cette fourchette (sinon on garde). Exécution à l'ouverture du lendemain, "
             "0,75 % de frais et glissement par échange (l'auteur : « 1–2 % par rotation »). Pièce retirée de la cote : vendue à sa dernière "
             "clôture échangée.")
    L.append("* **Comparaisons** : le détenteur (mêmes achats de départ, aucune rotation), le panier équipondéré de toutes les pièces "
             "éligibles (rééquilibré tous les 30 jours, mêmes frais), la rotation inverse (vers le haut de fourchette) et 500 rotations "
             "placebo (mêmes déclenchements, pièce achetée au hasard).\n")

    L.append("## 2. A après un 3x, B en bas de fourchette : ce qui arrive ensuite\n")
    L.append("![états A et B](etats_A_B.png)\n")
    L.append("| état (60 jours suivants) | n | atteint +100 % | atteint +200 % (3x) | touche −30 % | touche −70 % | rendement médian | rendement moyen |")
    L.append("|---|---|---|---|---|---|---|---|")
    for lab in ("A : vient de faire 3x", "B : bas de fourchette", "toute pièce éligible, tout jour"):
        L.append(evrow(e60.loc[lab], lab))
    L.append("")
    L.append("Hypothèses de l'auteur, face à la mesure :\n")
    L.append("| affirmation | mesure (60 jours) |")
    L.append("|---|---|")
    L.append(f"| A après un 3x : « peut-être +100 % » | +100 % atteint dans {pc(A['p_double'])} des cas |")
    L.append(f"| A après un 3x : « repli typique de −70 % » | −70 % touché dans {pc(A['p_moins70'])} des cas ; plus bas médian {pc(A['min_median'], signed=True)} |")
    L.append(f"| B en bas de fourchette : « le même 3x, +200 % » | +200 % atteint dans {pc(B['p_triple'])} des cas |")
    L.append(f"| B en bas de fourchette : « −30 % au pire » | −30 % touché dans {pc(B['p_moins30'])} des cas ; plus bas médian {pc(B['min_median'], signed=True)} |")
    L.append("")
    if pB is not None:
        L.append("La décision de rotation, appariée (même jour, frais de la vente et de l'achat déduits) :\n")
        L.append(md_table(pairs, [("horizon_j", "horizon", lambda v: f"{int(v)} j"), ("choix", "on achète", str), ("n", "rotations", lambda v: str(int(v))),
                                  ("gain_log_moyen", "écart moyen (B − A)", lambda v: pc(math.expm1(v), 1, signed=True)),
                                  ("ic_lo", "IC 95 % bas", lambda v: pc(math.expm1(v), 1, signed=True)),
                                  ("ic_hi", "IC 95 % haut", lambda v: pc(math.expm1(v), 1, signed=True)),
                                  ("p_gagne", "la rotation gagne", lambda v: pc(v))]))
    L.append("## 3. Les pièces en retard rattrapent-elles ?\n")
    L.append("Corrélation de rang (Spearman), jour par jour, entre un signal et le rendement futur, sur les pièces éligibles ; t de Newey-West "
             "(rendements futurs qui se chevauchent). Négatif = retour à la moyenne (ce qui ferait marcher la rotation), positif = momentum.\n")
    L.append(md_table(ic, [("signal", "signal", str), ("horizon_j", "horizon", lambda v: f"{int(v)} j"), ("jours", "jours", lambda v: str(int(v))),
                           ("ic_moyen", "corrélation moyenne", lambda v: fr(v, 3, signed=True)), ("t_nw", "t (Newey-West)", lambda v: fr(v, 1, signed=True)),
                           ("part_positive", "jours positifs", lambda v: pc(v))]))
    L.append("## 4. Portefeuilles\n")
    L.append("![portefeuilles](portefeuilles.png)\n")
    L.append(md_table(perf, [("strategie", "stratégie", str), ("total_x", "multiple final", xm), ("cagr", "par an", lambda v: pc(v, 0, signed=True)),
                             ("vol", "volatilité", lambda v: pc(v)), ("sharpe", "Sharpe", lambda v: fr(v, 2)), ("max_dd", "perte max.", lambda v: pc(v)),
                             ("rotations", "rotations", lambda v: "—" if not np.isfinite(v) else str(int(v)))]))
    L.append(f"* Rotations placebo (même règle, pièce achetée au hasard) : multiple médian {xm(float(np.median(plac_x)))}, 5–95 % "
             f"{xm(float(np.percentile(plac_x, 5)))} – {xm(float(np.percentile(plac_x, 95)))} ; la règle de l'auteur ({xm(rot_x)}) fait mieux "
             f"que {pc(pct_plac)} d'entre elles.")
    L.append(f"* Écart rotation − panier : {pc(math.expm1(boot['rot_moins_panier_log_an']), signed=True)} par an (IC 95 % par blocs de 30 j : "
             f"{pc(math.expm1(boot['lo']), signed=True)} ; {pc(math.expm1(boot['hi']), signed=True)}). Rotation − détenteur : "
             f"{pc(math.expm1(boot['rot_moins_detenteur_log_an']), signed=True)} par an (IC {pc(math.expm1(boot['lo_h']), signed=True)} ; "
             f"{pc(math.expm1(boot['hi_h']), signed=True)}).")
    L.append(f"* Sur les 30 derniers jours (la période de l'auteur : « 150 k$ → 420 k$ ») : "
             + " ; ".join(f"{k.split(' (')[0]} {xm(v)}" for k, v in last30.items()) + ".\n")
    tr = pd.read_csv(OUT / "rotations_regle_auteur.csv", parse_dates=["date"])
    rt = tr[tr["why"] == "rotation"]
    yrs = (end - start).days / 365.25
    L.append(f"* **La règle ne se déclenche presque jamais** avec 2 pièces : {len(rt)} rotation{'s' if len(rt) > 1 else ''} en "
             f"{fr(yrs, 1)} ans ("
             + " ; ".join(f"{day(r.date)} : {r.sell.replace('USDT', '')} ({xm(r.sell_mult, 1)} son plus bas de 30 j) → {r.buy.replace('USDT', '')}"
                          for r in rt.itertuples())
             + "). Le multiple final tient donc à ces décisions et aux deux achats de départ : les tests qui comptent sont les "
               "rotations appariées (section 2), les départs glissants (ci-dessous) et la grille (section 5).\n")
    L.append("![départs glissants](departs_glissants.png)\n")
    L.append(f"Départs mensuels tenus 12 mois ({len(roll)} départs, `departs_glissants_12_mois.csv`) : la rotation bat le détenteur dans "
             f"{pc(win_h)} des cas, le panier dans {pc(win_b)} ; multiple médian rotation {xm(float(roll['rotation'].median()))}, détenteur "
             f"{xm(float(roll['detenteur'].median()))}, panier {xm(float(roll['panier'].median()))}.")
    first, last3 = roll.iloc[: len(roll) // 2], roll.iloc[-3:]
    L.append(f"Le résultat dépend de la date de départ : les départs de la 1re moitié donnent la rotation gagnante contre le détenteur "
             f"dans {pc(float((first['rotation'] > first['detenteur']).mean()))} des cas, les trois derniers (départs du "
             f"{day(last3['debut'].iloc[0])} au {day(last3['debut'].iloc[-1])}, fenêtres qui se chevauchent) dans "
             f"{pc(float((last3['rotation'] > last3['detenteur']).mean()))} (rotation {', '.join(xm(v) for v in last3['rotation'])} contre "
             f"détenteur {', '.join(xm(v) for v in last3['detenteur'])}), avec 2 ou 3 rotations chacun. Rien de stable : ce sont quelques "
             f"décisions, pas une règle qui marche.\n")
    L.append("## 5. Tous les réglages, et la correction pour essais multiples\n")
    L.append("![grille](grille_variantes.png)\n")
    L.append(f"* 162 réglages (`grille_162_variantes.csv`) : {n_beat} battent le panier sur toute la période. Règle de l'auteur : "
             f"{xm(float(apr['total_x']))}, écart au panier {pc(math.expm1(apr['exces_log_an']), signed=True)} par an, probabilité que son "
             f"vrai Sharpe d'écart soit positif (PSR) {fr(psr_apr, 2)}.")
    L.append(f"* Meilleur réglage : {best['variante']}, {xm(float(best['total_x']))}, Sharpe d'écart {fr(best['sharpe_exces'], 2)}. Sharpe "
             f"dégonflé (Bailey et López de Prado, en Sharpe **par jour**, maximum attendu de {dsr['n_trials']} essais sans talent : "
             f"{fr(dsr['sr0_per_period'] * math.sqrt(365), 2)} annualisé) : **{fr(dsr['dsr'], 2)}**, sous le seuil de 0,95.")
    L.append(f"* Walk-forward : réglage choisi sur {day(start)} – {day(pd.Timestamp(wf['coupure']))} ({wf['variante_choisie']}, "
             f"{pc(math.expm1(wf['exces_log_an_1re_moitie']), signed=True)} par an contre le panier), puis appliqué à la 2e moitié : "
             f"{xm(wf['rotation_x_2e'])} contre {xm(wf['panier_x_2e'])} (panier) et {xm(wf['detenteur_x_2e'])} (détenteur) ; écart "
             f"{pc(math.expm1(wf['exces_log_an_2e']), signed=True)} par an, PSR {fr(wf['psr_2e'], 2)}.\n")
    L.append("## 6. Robustesse\n")
    L.append(md_table(rob, [("cas", "variante", str), ("rotation_x", "rotation", xm), ("detenteur_x", "détenteur", xm), ("panier_x", "panier", xm),
                            ("rotation_dd", "perte max. rotation", lambda v: pc(v)), ("rotations", "rotations", lambda v: str(int(v)))]))
    L.append("Régimes (indice équipondéré des memecoins au-dessus ou en dessous de sa moyenne mobile 200 jours, mesuré la veille) :\n")
    L.append(md_table(reg, [("regime", "régime", str), ("strategie", "stratégie", str), ("jours", "jours", lambda v: str(int(v))),
                            ("multiple", "multiple sur ces jours", xm), ("rendement_log_an", "rendement log annualisé", lambda v: pc(v, 0, signed=True))]))
    L.append("La rotation fait mieux que le panier en régime baissier et bien moins bien en régime haussier. Sur quelques rotations "
             "seulement, c'est surtout le hasard de deux ou trois décisions, pas une propriété démontrée de la règle.\n")
    L.append("## 7. Limites\n")
    L.append("* **Univers Binance.** Les pièces de l'auteur (CATE, NEET…) ne sont pas cotées sur Binance ; celles qui le sont (SPX, FARTCOIN, "
             "USELESS, POPCAT, PEPE, BONK, WIF…) sont les « community coins » les plus liquides, exactement la catégorie qu'il recommande. "
             "Être coté sur Binance est déjà une sélection (les pièces cotées avaient réussi), mais connue le jour de la cotation : pas "
             "d'information future. Sur les DEX, beaucoup plus de pièces meurent : le biais irait contre la rotation (B en bas de fourchette "
             "est plus souvent une pièce qui meurt).")
    L.append("* **Bougies quotidiennes.** Un 3x réalisé et défait dans la journée échappe à la règle ; l'auteur dit détenir 38 jours en "
             "moyenne, l'échelle quotidienne convient.")
    L.append("* **Petits échantillons d'événements** (quelques dizaines de 3x) : les IC sont larges ; la conclusion repose sur l'absence de "
             "tout avantage démontré, pas sur une perte démontrée.")
    L.append("* **Une seule histoire de marché** (2024–2026, un cycle des memecoins) : la section 6 sépare haussier et baissier, mais "
             "sur un seul cycle.\n")
    q_exec = src_line("v *= O[i + 1, sell] / C[i, sell] * (1.0 - c)")
    q_roll = src_line("lo = close.rolling(w, min_periods=w).min()")
    q_uni = src_line('and "Meme" in (s.get("underlyingSubType") or [])')
    q_where = src_line("tabs[k] = tabs[k].where(last)")
    q_cost = src_line("v *= 1.0 - c")
    q_fee = src_line("fee = cost * np.abs(tgt - w).sum()")
    q_open = src_line('elif p.execution == "open":')
    q_date = src_line('df["date"] = pd.to_datetime(df["open_ms"], unit="ms").dt.normalize()')
    L.append("## 8. Grille en 8 points appliquée à ce backtest\n")
    L.append("Statut du **défaut** (PRÉSENT = le défaut existe), avec la ligne de code qui le montre.\n")
    L.append(f"1. **Look-ahead** : ABSENT. Décision à la clôture de d, exécution le lendemain : {q_exec} ; "
             f"signaux sur fenêtres qui finissent au jour d : {q_roll}.")
    L.append(f"2. **Survivorship** : ABSENT par rapport à la cote Binance (retirés compris : {q_uni}) ; "
             "PARTIEL par rapport aux DEX (seules les pièces un jour cotées sur Binance).")
    L.append(f"3. **Repainting** : ABSENT. Fenêtres glissantes non centrées, prix figés après retrait : {q_where} ; "
             f"le régime est mesuré la veille (`.shift(1)` dans `scripts/memecoin_rotation.py`).")
    L.append(f"4. **Coûts** : ABSENT. Chaque vente et chaque achat paient `cost` : {q_cost} ; panier : {q_fee}.")
    L.append(f"5. **Exécution à un prix jamais disponible** : PARTIEL. L'ouverture de J+1 vaut la clôture de J sur un marché ouvert 24 h/24 : "
             f"{q_open} ; variante prudente à la clôture de J+1 dans la section 6 ; une pièce retirée est vendue à sa "
             f"dernière clôture échangée (variante −50 % en section 6).")
    L.append(f"6. **Ajustement des paramètres** : 5 paramètres (seuil, fenêtre du plus bas, fenêtre de la fourchette, bas de fourchette, "
             f"nombre de pièces) fixés **avant** le test sur les chiffres de l'article ; 162 réglages essayés ensuite, comptés dans le Sharpe "
             f"dégonflé et testés en walk-forward (section 5).")
    L.append("7. **Échantillon** : PARTIEL. Hausse 2024 puis baisse : les deux régimes sont présents (section 6), mais sur un seul cycle.")
    L.append(f"8. **Alignement** : ABSENT. Une seule source, bougies UTC 00:00 : {q_date}.\n")
    L.append("## Fichiers\n")
    for f, d in (("univers.csv", "les pièces, dates de cotation et de retrait, jours éligibles"),
                 ("etats_A_B.csv", "ce qui suit un 3x (A) et un bas de fourchette (B), 30 et 60 jours"),
                 ("rotations_appariees.csv", "décision de rotation B − A appariée, et contre une pièce quelconque"),
                 ("rotations_appariees_60j.csv", "chaque rotation appariée à 60 jours"),
                 ("ic_retour_moyenne.csv", "corrélations de rang signal / rendement futur"),
                 ("portefeuilles.csv", "performances des portefeuilles"),
                 ("rotations_regle_auteur.csv", "chaque achat et rotation de la règle de l'auteur"),
                 ("departs_glissants_12_mois.csv", "départs mensuels tenus 12 mois"),
                 ("grille_162_variantes.csv", "tous les réglages essayés"),
                 ("robustesse.csv", "variantes de frais, d'exécution, d'univers"),
                 ("regimes.csv", "par régime de marché"), ("run.json", "paramètres et résumé")):
        L.append(f"* `{f}` : {d}")
    (OUT / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
