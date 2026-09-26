#!/usr/bin/env python
"""Backtest de la « rotation » entre memecoins (fil X « Ultimate Memecoin Trading Guide », 2026).

Question : vendre le memecoin qui vient de faire 3x et acheter celui qui est en bas de sa
fourchette rapporte-t-il plus que garder ses pièces, ou qu'un panier, une fois les frais payés ?

Données publiques, lecture seule (voir ``tradebot.memecoins``) : les 55 perpétuels USDT classés
« Meme » par Binance, retirés de la cote compris, en bougies quotidiennes. Sorties dans
``reports/memecoins/``.

    python scripts/memecoin_rotation.py [--refresh] [--placebo 500] [--placebo-formes 100] [--boot 2000]
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
GRID_SPACE = {"trigger": mc.TRIGGERS, "mult": (2.0, 3.0, 4.0), "range_win": (30, 60, 90),
              "bottom": (0.1, 0.2, 0.3), "k": (1, 2, 3, 5)}


def fr(x, d: int = 1, signed: bool = False, pct: bool = False) -> str:
    return fmt_number(x, d, signed=signed, pct=pct)


def pc(x, d: int = 0, signed: bool = False) -> str:
    """Pourcentage à la française (x en fraction)."""
    if x is None or not np.isfinite(x):
        return "—"
    return fr(100 * x, d, signed=signed) + "\u00a0%"


def xm(x, d: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    if 0 < abs(x) < 0.1:
        d = max(d, 1 - int(math.floor(math.log10(abs(x)))))     # 0,003 × plutôt que 0,00 ×
    return fr(x, d) + "\u00a0×"


def day(d) -> str:
    return pd.Timestamp(d).strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# 1. États A (« vient de faire 3x ») et B (« bas de fourchette ») : que se passe-t-il ensuite ?
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1. États A (« vient de faire 3x ») et B (« bas de fourchette ») : que se passe-t-il ensuite ?
# ---------------------------------------------------------------------------
def states(panel: mc.Panel, el: pd.DataFrame, p: mc.RotationParams) -> dict[str, pd.DataFrame]:
    C = panel.close
    did = (C >= p.mult * mc.rolling_low(C, p.low_win)) & el
    bot = (mc.range_position(C, p.range_win) <= p.bottom) & el
    return {"A": did & ~did.shift(1, fill_value=False), "B": bot & ~bot.shift(1, fill_value=False), "tous": el}


def month_of(dates) -> np.ndarray:
    return np.asarray(dates, dtype="datetime64[ns]").astype("datetime64[M]").astype(int)


def cluster_boot(vals: np.ndarray, groups: np.ndarray, stat, n_boot: int, seed: int = 0) -> tuple[float, float]:
    """IC 95 % par bootstrap en tirant des mois entiers (événements d'un même mois, et fenêtres futures
    de 30 à 60 jours qui se chevauchent, sont corrélés)."""
    rng = np.random.default_rng(seed)
    ug = np.unique(groups)
    by = {g: vals[groups == g] for g in ug}
    out = []
    for _ in range(n_boot):
        pick = rng.choice(ug, ug.size)
        out.append(stat(np.concatenate([by[g] for g in pick])))
    return float(np.nanpercentile(out, 2.5)), float(np.nanpercentile(out, 97.5))


def _event_values(panel, mask, start, fw, keys):
    after = (panel.dates >= start)[:, None]
    m = mask.to_numpy() & after
    vals = [fw[k].to_numpy()[m] for k in keys]
    grp = month_of(np.broadcast_to(panel.dates.to_numpy()[:, None], m.shape)[m])
    ok = np.all([np.isfinite(v) for v in vals], axis=0)
    return [v[ok] for v in vals], grp[ok]


def event_study(panel, el, start, n_boot) -> pd.DataFrame:
    st = states(panel, el, A_PRIORI)
    rows = []
    for h in HORIZONS:
        fw = mc.forward_stats(panel, h)
        for key, lab in (("A", "A : vient de faire 3x"), ("B", "B : bas de fourchette"), ("tous", "toute pièce éligible, tout jour")):
            (r, mx, mn), grp = _event_values(panel, st[key], start, fw, ("ret", "max", "min"))
            row = {"horizon_j": h, "etat": lab, "n": int(r.size), "mois": int(np.unique(grp).size),
                   "ret_median": float(np.median(r)), "ret_moyen": float(r.mean()),
                   "p_double": float((mx >= 1.0).mean()), "p_triple": float((mx >= 2.0).mean()),
                   "p_moins30": float((mn <= -0.3).mean()), "p_moins70": float((mn <= -0.7).mean()),
                   "max_median": float(np.median(mx)), "min_median": float(np.median(mn))}
            if key != "tous":
                row["ret_median_lo"], row["ret_median_hi"] = cluster_boot(r, grp, np.median, n_boot)
                row["p_triple_lo"], row["p_triple_hi"] = cluster_boot((mx >= 2.0).astype(float), grp, np.mean, n_boot)
                row["p_moins30_lo"], row["p_moins30_hi"] = cluster_boot((mn <= -0.3).astype(float), grp, np.mean, n_boot)
            rows.append(row)
    return pd.DataFrame(rows)


def drawdown_after_A(panel, el, start) -> pd.DataFrame:
    """Plus forte baisse depuis un sommet (et non depuis le jour du 3x), sur 60, 120 et 180 jours :
    la prémisse de l'auteur « un repli typique de −70 % » se lit ainsi."""
    st = states(panel, el, A_PRIORI)
    rows = []
    for h in (60, 120, 180):
        fw = mc.forward_stats(panel, h)
        for key, lab in (("A", "A : vient de faire 3x"), ("tous", "toute pièce éligible, tout jour")):
            (dd,), grp = _event_values(panel, st[key], start, fw, ("dd",))
            rows.append({"horizon_j": h, "etat": lab, "n": int(dd.size), "dd_median": float(np.median(dd)),
                         "p_dd70": float((dd <= -0.7).mean()), "p_dd50": float((dd <= -0.5).mean())})
    return pd.DataFrame(rows)


def paired_rotations(panel, el, start, n_boot, cost: float) -> pd.DataFrame:
    """Chaque fois qu'une pièce A fait 3x et qu'une pièce éligible est sous 20 % de sa fourchette :
    B = la plus basse. Écart de richesse pour 1 $ : (1 + r_B)(1 − coût)² − (1 + r_A) sur h jours, ce
    que la rotation laisse de plus (ou de moins) que garder A ; sa moyenne est l'écart d'espérance de
    richesse (un rapport de richesses serait gonflé par les cas où A s'effondre). Même calcul pour une
    pièce éligible quelconque (moyenne de toutes les autres)."""
    p = A_PRIORI
    C = panel.close
    st = states(panel, el, p)["A"]
    rp = mc.range_position(C, p.range_win).to_numpy()
    E = el.to_numpy()
    k2 = (1 - cost) ** 2
    rows = []
    for h in HORIZONS:
        fw = mc.forward_stats(panel, h)["ret"].to_numpy()
        recs = []
        for d, a in zip(*np.nonzero(st.to_numpy())):
            date = panel.dates[d]
            if date < start or not np.isfinite(fw[d, a]):
                continue
            cand = np.flatnonzero(E[d])
            cand = cand[cand != a]
            r, f = rp[d, cand], fw[d, cand]
            ok = np.isfinite(r) & np.isfinite(f)
            if not ok.any():
                continue
            cand, r, f = cand[ok], r[ok], f[ok]
            j = int(np.argmin(r))
            if r[j] > p.bottom:
                continue
            ga = 1 + fw[d, a]
            recs.append({"date": date, "A": panel.symbols[a], "B": panel.symbols[cand[j]],
                         "rot_B": (1 + f[j]) * k2 - ga, "rot_autre": float(np.mean(1 + f)) * k2 - ga})
        df = pd.DataFrame(recs)
        if df.empty:
            continue
        grp = month_of(df["date"])
        for col, lab in (("rot_B", "rotation vers B (bas de fourchette)"), ("rot_autre", "rotation vers une pièce quelconque")):
            v = df[col].to_numpy()
            lo, hi = cluster_boot(v, grp, np.mean, n_boot)
            rows.append({"horizon_j": h, "choix": lab, "n": len(v), "mois": int(np.unique(grp).size),
                         "gain_moyen": float(v.mean()), "ic_lo": lo, "ic_hi": hi,
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


def mult(res: mc.SimResult) -> float:
    return float((1 + res.returns.dropna()).prod())


def main_runs(panel, el, start, n_placebo):
    p = A_PRIORI
    runs = {
        "rotation (règle testée)": mc.simulate_rotation(panel, el, p, start=start),
        "détenteur (mêmes achats de départ, aucune rotation)": mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=start),
        "rotation inverse (mêmes achats et déclenchements, cible en haut de fourchette)": mc.simulate_rotation(panel, el, mc.with_params(p, target="top"), start=start),
        "panier équipondéré (30 tranches, rééquilibrage mensuel)": mc.simulate_basket(panel, el, cost=p.cost, start=start),
    }
    plac = [mc.simulate_rotation(panel, el, mc.with_params(p, target="random", seed=s), start=start).returns
            for s in range(n_placebo)]
    return runs, pd.concat(plac, axis=1)


def perf_table(runs: dict) -> pd.DataFrame:
    rows = []
    for k, res in runs.items():
        row = {"strategie": k, **mc.perf(res.returns)}
        row["rotations"] = n_rot(res) if len(res.trades) and "why" in res.trades else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def windows(start, end, months=12, step=30):
    s = start
    while s + pd.Timedelta(days=30 * months) <= end:
        yield s, s + pd.Timedelta(days=30 * months)
        s = s + pd.Timedelta(days=step)


def wlt(a: np.ndarray, b: np.ndarray, tol: float = 1e-9) -> tuple[int, int, int]:
    """Victoires, défaites, égalités de a contre b (égalité : même multiple à 1e-9 près)."""
    d = np.log(a) - np.log(b)
    return int((d > tol).sum()), int((d < -tol).sum()), int((np.abs(d) <= tol).sum())


def sign_test_p(w: int, l: int) -> float:
    from scipy.stats import binomtest
    return float(binomtest(w, w + l, 0.5).pvalue) if w + l else float("nan")


def rolling_starts(panel, el, start, end, p=A_PRIORI) -> pd.DataFrame:
    """Départs mensuels, 12 mois de détention : rotation, détenteur, rotation inverse, panier."""
    rows = []
    for s, e in windows(start, end):
        rot = mc.simulate_rotation(panel, el, p, start=s, end=e)
        rows.append({"debut": s, "fin": e, "rotation": mult(rot), "rotations": n_rot(rot),
                     "detenteur": mult(mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=s, end=e)),
                     "inverse": mult(mc.simulate_rotation(panel, el, mc.with_params(p, target="top"), start=s, end=e)),
                     "panier": mult(mc.simulate_basket(panel, el, cost=p.cost, start=s, end=e))})
    return pd.DataFrame(rows)


FORMS = [("F0", "3 × le plus bas des 30 derniers jours", dict(trigger="low", mult=3.0)),
         ("F1", "3 × le plus bas de la fourchette de 60 jours", dict(trigger="range", mult=3.0)),
         ("F2", "3 × le prix d'achat", dict(trigger="entry", mult=3.0)),
         ("F3", "haut de fourchette (≥ 90 %) et 2 × son plus bas", dict(trigger="range_top", mult=2.0))]


def formalisations(panel, universes: dict, start, end, n_plac: int) -> pd.DataFrame:
    """Quatre lectures raisonnables de « vendre en haut de fourchette » × deux univers × 2 ou 5 pièces :
    période entière, départs glissants de 12 mois, placebos (mêmes achats et déclenchements, cible au
    hasard)."""
    rows = []
    for uname, el in universes.items():
        for fid, flab, kw in FORMS:
            for k in (2, 5):
                p = mc.with_params(A_PRIORI, k=k, **kw)
                rot = mc.simulate_rotation(panel, el, p, start=start)
                hold = mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=start)
                plac = np.array([mult(mc.simulate_rotation(panel, el, mc.with_params(p, target="random", seed=s), start=start))
                                 for s in range(n_plac)])
                ra, ha, nr = [], [], []
                for s, e in windows(start, end):
                    r = mc.simulate_rotation(panel, el, p, start=s, end=e)
                    ra.append(mult(r))
                    nr.append(n_rot(r))
                    ha.append(mult(mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=s, end=e)))
                w, l, t = wlt(np.array(ra), np.array(ha))
                rows.append({"univers": uname, "forme": fid, "declencheur": flab, "k": k,
                             "rotation_x": mult(rot), "detenteur_x": mult(hold), "rotations": n_rot(rot),
                             "placebo_median_x": float(np.median(plac)), "part_placebos_battus": float((plac < mult(rot)).mean()),
                             "fenetres_gagnees": w, "fenetres_perdues": l, "fenetres_egales": t,
                             "rotations_par_fenetre": float(np.mean(nr)), "p_signe": sign_test_p(w, l)})
                log.info("formalisation %s %s k=%d : %.2f× contre %.2f×", uname, fid, k, rows[-1]["rotation_x"], rows[-1]["detenteur_x"])
    return pd.DataFrame(rows)


GRID_KEYS = ("trigger", "mult", "range_win", "bottom", "k")


def grid_runs(panel, el, basket: pd.Series, start, end=None):
    """Tous les réglages ; chaque variante est comparée au détenteur qui a fait les mêmes achats
    (même k, même fourchette pour l'entrée). Renvoie le tableau et les écarts quotidiens (log)."""
    holders = {}
    rows, exc = [], {}
    for vals in itertools.product(*(GRID_SPACE[k] for k in GRID_KEYS)):
        kw = dict(zip(GRID_KEYS, vals))
        p = mc.with_params(A_PRIORI, **kw)
        hk = (kw["k"], kw["range_win"])
        if hk not in holders:
            holders[hk] = mc.simulate_rotation(panel, el, mc.with_params(p, target="none"), start=start, end=end).returns.dropna()
        h = holders[hk]
        res = mc.simulate_rotation(panel, el, p, start=start, end=end)
        r = res.returns.dropna()
        x = np.log1p(r) - np.log1p(h.reindex(r.index))
        name = "{trigger} mult={mult:g} range={range_win} bottom={bottom:g} k={k}".format(**kw)
        exc[name] = x
        b = basket.reindex(r.index)
        rows.append({"variante": name, **kw, "total_x": float((1 + r).prod()), "detenteur_x": float((1 + h).prod()),
                     "rotations": n_rot(res), "exces_detenteur_log_an": float(x.mean() * 365),
                     "sharpe_exces": float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else np.nan,
                     "bat_detenteur": bool(x.sum() > 1e-9), "bat_panier": bool(np.log1p(r).sum() > np.log1p(b).sum()),
                     "a_priori": all(getattr(A_PRIORI, k) == v for k, v in kw.items())})
    return pd.DataFrame(rows), pd.DataFrame(exc)


def distinct_series(exc: pd.DataFrame, names) -> list[str]:
    seen, keep = set(), []
    for n in names:
        key = tuple(np.round(exc[n].fillna(0).cumsum().to_numpy()[:: max(1, len(exc) // 50)], 9))
        if key not in seen:
            seen.add(key)
            keep.append(n)
    return keep


def periodic_table(panel, el, start, basket: pd.Series, n_rand: int, n_boot: int) -> pd.DataFrame:
    """Rotation systématique vers le bas de fourchette (hebdomadaire, mensuelle), contre le haut de
    fourchette, le hasard et le panier."""
    rows = []
    for every, lab in ((7, "hebdomadaire"), (30, "mensuelle")):
        for k in (2, 5):
            res = {rule: mc.simulate_periodic(panel, el, k, every, rule, start=start, tranches=every)
                   for rule in ("bottom", "top")}
            rnd = np.array([mult(mc.simulate_periodic(panel, el, k, every, "random", seed=s, start=start, tranches=every))
                            for s in range(n_rand)])
            x = (np.log1p(res["bottom"].returns) - np.log1p(basket.reindex(res["bottom"].returns.index))).dropna().to_numpy()
            bs = mc.block_bootstrap(x, lambda v: v.mean() * 365, block=30, n_boot=n_boot)
            rows.append({"frequence": lab, "k": k, "bas_x": mult(res["bottom"]), "haut_x": mult(res["top"]),
                         "hasard_median_x": float(np.median(rnd)), "panier_x": float((1 + basket.dropna()).prod()),
                         "exces_bas_panier_log_an": float(x.mean() * 365), "ic_lo": float(np.percentile(bs, 2.5)),
                         "ic_hi": float(np.percentile(bs, 97.5))})
    return pd.DataFrame(rows)


def robustness(panel, el_base, start, uni_ok) -> pd.DataFrame:
    p = A_PRIORI
    el_comm = mc.eligibility(panel, community=True)
    cases = [("règle testée (0,75 % par échange, ouverture de J+1, préavis de retrait 5 j)", p, el_base, "open"),
             ("frais 0,25 % par échange", mc.with_params(p, cost=0.0025), el_base, "open"),
             ("frais 1,5 % par échange", mc.with_params(p, cost=0.015), el_base, "open"),
             ("exécution à la clôture de J+1 (panier compris)", mc.with_params(p, execution="next_close"), el_base, "next_close"),
             ("sans préavis, pièce retirée revendue −50 %", mc.with_params(p, delist_haircut=0.5, notice_days=0),
              mc.eligibility(panel, notice_days=0), "open"),
             ("volume médian ≥ 10 M$", p, mc.eligibility(panel, min_qvol=10e6), "open"),
             ("volume médian ≥ 20 M$", p, mc.eligibility(panel, min_qvol=20e6), "open"),
             ("volume médian ≥ 50 M$", p, mc.eligibility(panel, min_qvol=50e6), "open"),
             ("k = 1 pièce", mc.with_params(p, k=1), el_base, "open"),
             ("k = 3 pièces", mc.with_params(p, k=3), el_base, "open"),
             ("seuil 2x au lieu de 3x", mc.with_params(p, mult=2.0), el_base, "open"),
             ("univers « community coins » (déjà −70 % puis ×2)", p, el_comm, "open")]
    rows = []
    for lab, q, el, ex in cases:
        rot = mc.simulate_rotation(panel, el, q, start=start)
        hold = mc.simulate_rotation(panel, el, mc.with_params(q, target="none"), start=start)
        bas = mc.simulate_basket(panel, el, cost=q.cost, delist_haircut=q.delist_haircut, notice_days=q.notice_days,
                                 execution=ex, start=start)
        rows.append({"cas": lab, "rotation_x": mult(rot), "detenteur_x": mult(hold), "panier_x": mult(bas),
                     "rotation_dd": mc.perf(rot.returns).get("max_dd"), "rotations": n_rot(rot)})
    return pd.DataFrame(rows)


def regimes(panel, el, rot: pd.Series, hold: pd.Series, bas: pd.Series) -> pd.DataFrame:
    """Régime du jour d : indice équipondéré de toutes les pièces actives au-dessus / en dessous de sa
    moyenne mobile 200 j, mesuré à la clôture de d − 1 (pas d'information du jour même)."""
    r = panel.close.pct_change(fill_method=None).where(panel.active & panel.active.shift(1, fill_value=False))
    idx = (1 + r.mean(axis=1).fillna(0)).cumprod()
    up = (idx > idx.rolling(200, min_periods=200).mean()).shift(1)
    rows = []
    for lab, sel in (("haussier (indice > MM 200 j)", up == True), ("baissier (indice < MM 200 j)", up == False)):  # noqa: E712
        s = sel.reindex(rot.index).fillna(False).to_numpy(bool)
        for name, x in (("rotation", rot), ("détenteur", hold), ("panier", bas)):
            v = x.reindex(rot.index).to_numpy()[s]
            v = v[np.isfinite(v)]
            rows.append({"regime": lab, "strategie": name, "jours": int(v.size),
                         "rendement_log_an": float(np.log1p(v).mean() * 365) if v.size else np.nan,
                         "multiple": float(np.prod(1 + v)) if v.size else np.nan})
    return pd.DataFrame(rows)


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


def fig_forms(forms: pd.DataFrame, path: Path, title: str, subtitle: str):
    """Rapport rotation / détenteur sur la période entière (barres, échelle log) et bilan des départs
    glissants (texte), pour chaque formalisation × univers × nombre de pièces."""
    plt = _pyplot()
    W = 10.0
    t, s, hh = _header(W, title, subtitle)
    n = len(forms)
    H = 0.34 * n + 1.1
    fig = plt.figure(figsize=(W, H + hh), facecolor=BG)
    ax = fig.add_axes([0.36, 0.6 / (H + hh), 0.36, (H - 0.9) / (H + hh)])
    _style_axes(ax, ygrid=False, xgrid=True)
    ratio = np.log10(forms["rotation_x"].to_numpy() / forms["detenteur_x"].to_numpy())
    y = np.arange(n)[::-1]
    cols = [C_BLUE if v > 0 else C_ORANGE for v in ratio]
    ax.barh(y, ratio, color=cols, height=0.62)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    lab = [f"{r.forme} · {'Binance' if r.univers.startswith('Binance') else 'community'} · {r.k} pièces" for r in forms.itertuples()]
    ax.set_yticks(y)
    ax.set_yticklabels(lab, fontsize=8.5, color=TEXT_2)
    lim = max(1.0, float(np.nanmax(np.abs(ratio))) * 1.1)
    ax.set_xlim(-lim, lim)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda u, _: fr(10 ** u, 2 if 10 ** u < 1 else 1) + " ×"))
    ax.set_xlabel("rotation / détenteur sur toute la période (échelle log)", color=TEXT_2, fontsize=9)
    for yy, r in zip(y, forms.itertuples()):
        fig.text(0.74, (0.6 + (yy + 0.5) / n * (H - 0.9)) / (H + hh), f"{r.rotations} rot. · départs : {r.fenetres_gagnees} G / "
                 f"{r.fenetres_perdues} P / {r.fenetres_egales} =", fontsize=8.3, color=TEXT, va="center")
    _draw_header(fig, t, s)
    _save(fig, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="retélécharger les bougies Binance")
    ap.add_argument("--placebo", type=int, default=500)
    ap.add_argument("--placebo-formes", type=int, default=100)
    ap.add_argument("--boot", type=int, default=2000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in ("departs_glissants.png", "grille_162_variantes.csv", "rotations_appariees.csv", "robustesse.csv"):
        (OUT / old).unlink(missing_ok=True)

    panel, uni, ref = mc.load_all(refresh=args.refresh)
    el = mc.eligibility(panel)
    el_comm = mc.eligibility(panel, community=True)
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
    ddA = drawdown_after_A(panel, el, start)
    ddA.to_csv(OUT / "baisse_depuis_sommet_A.csv", index=False)
    pairs = paired_rotations(panel, el, start, args.boot, A_PRIORI.cost)
    pairs.to_csv(OUT / "rotations_appariees_resume.csv", index=False)
    ic = ic_table(panel, el, start)
    ic.to_csv(OUT / "ic_retour_moyenne.csv", index=False)
    log.info("états et IC : %.0f s", time.time() - t0)

    runs, plac = main_runs(panel, el, start, args.placebo)
    perf = perf_table(runs)
    perf.to_csv(OUT / "portefeuilles.csv", index=False)
    rot = runs["rotation (règle testée)"]
    hold = runs["détenteur (mêmes achats de départ, aucune rotation)"]
    basket = runs["panier équipondéré (30 tranches, rééquilibrage mensuel)"].returns
    plac_x = (1 + plac.fillna(0)).prod().to_numpy()
    rot_x = mult(rot)
    pct_plac = float((plac_x < rot_x).mean())
    rot.trades.to_csv(OUT / "rotations_regle_testee.csv", index=False)
    last30 = {k: float((1 + res.returns.iloc[-30:].fillna(0)).prod()) for k, res in runs.items()}
    log.info("portefeuilles + %d placebos : %.0f s", args.placebo, time.time() - t0)

    roll = rolling_starts(panel, el, start, end)
    roll.to_csv(OUT / "departs_glissants_12_mois.csv", index=False)
    forms = formalisations(panel, {"Binance « Meme »": el, "community coins": el_comm}, start, end, args.placebo_formes)
    forms.to_csv(OUT / "formalisations.csv", index=False)
    log.info("formalisations : %.0f s", time.time() - t0)

    grid, exc = grid_runs(panel, el, basket, start)
    grid.to_csv(OUT / "grille_variantes.csv", index=False)
    rotating = grid[grid["rotations"] > 0]
    distinct = distinct_series(exc, rotating.sort_values("sharpe_exces", ascending=False)["variante"])
    sr_trials = np.array([exc[n].mean() / exc[n].std() for n in distinct])
    best = grid.set_index("variante").loc[distinct[int(np.nanargmax(sr_trials))]]
    dsr = mc.deflated_sharpe(exc[best.name].dropna().to_numpy(), sr_trials)
    apr = grid[grid["a_priori"]].iloc[0]

    # walk-forward : choix sur la 1re moitié parmi les réglages qui tournent, test sur la 2e
    mid = start + (end - start) / 2
    bas1 = mc.simulate_basket(panel, el, cost=A_PRIORI.cost, start=start, end=mid).returns
    g1, x1 = grid_runs(panel, el, bas1, start, end=mid)
    r1 = g1[g1["rotations"] > 0]
    top = float(r1["sharpe_exces"].max())
    tied = r1[np.isclose(r1["sharpe_exces"], top, rtol=0, atol=1e-10)]
    wf_rows = []
    for v in tied.itertuples():
        kw = {k: getattr(v, k) for k in GRID_KEYS}
        kw["k"], kw["range_win"] = int(kw["k"]), int(kw["range_win"])
        q = mc.with_params(A_PRIORI, **kw)
        r2 = mc.simulate_rotation(panel, el, q, start=mid)
        h2 = mc.simulate_rotation(panel, el, mc.with_params(q, target="none"), start=mid)
        wf_rows.append({"variante": v.variante, "rotation_x_2e": mult(r2), "detenteur_x_2e": mult(h2), "rotations_2e": n_rot(r2)})
    wf_df = pd.DataFrame(wf_rows)
    wf_df.to_csv(OUT / "walk_forward.csv", index=False)
    wf = {"coupure": str(mid.date()), "n_egalites": len(tied), "variantes": list(tied["variante"]),
          "exces_1re_log_an": float(tied["exces_detenteur_log_an"].iloc[0]),
          "rot_med": float(wf_df["rotation_x_2e"].median()), "hold_med": float(wf_df["detenteur_x_2e"].median()),
          "part_bat_detenteur": float((wf_df["rotation_x_2e"] > wf_df["detenteur_x_2e"] + 1e-9).mean()),
          "panier_2e": mult(mc.simulate_basket(panel, el, cost=A_PRIORI.cost, start=mid))}
    log.info("grille et walk-forward : %.0f s", time.time() - t0)

    per = periodic_table(panel, el, start, basket, n_rand=50, n_boot=args.boot)
    per.to_csv(OUT / "rotation_periodique.csv", index=False)
    rob = robustness(panel, el, start, None)
    rob.to_csv(OUT / "robustesse_variantes.csv", index=False)
    reg = regimes(panel, el, rot.returns, hold.returns, basket)
    reg.to_csv(OUT / "regimes.csv", index=False)

    # ------------------------------------------------------------------ graphiques
    evB = ev[(ev["horizon_j"] == 60) & ev["etat"].str.startswith("B")].iloc[0]
    evA = ev[(ev["horizon_j"] == 60) & ev["etat"].str.startswith("A")].iloc[0]
    fig_states(ev, OUT / "etats_A_B.png",
               f"En bas de sa fourchette, un memecoin refait 3x dans {pc(evB['p_triple'])} des cas en 60 jours, et reperd 30 % dans {pc(evB['p_moins30'])} des cas",
               f"55 perpétuels « Meme » de Binance (retirés compris), {day(start)} – {day(end)}. A : 1er jour où la clôture vaut ≥ 3 × le plus bas des 30 j "
               f"(n = {int(evA['n'])}). B : 1er jour sous 20 % de la fourchette 60 j (n = {int(evB['n'])}). Barres : IC 95 % (bootstrap par mois).")
    fig_equity(runs, plac, OUT / "portefeuilles.png",
               f"Avec 2 pièces, la règle ne tourne que {n_rot(rot)} fois en {fr((end - start).days / 365.25, 1)} ans : "
               f"{xm(rot_x)} la mise, contre {xm(mult(hold))} pour le détenteur des mêmes pièces",
               f"Du {day(start)} au {day(end)}, 0,75 % de frais et glissement par échange, décision à la clôture et exécution le lendemain. "
               f"Bande grise : 500 rotations au hasard (mêmes achats de départ et mêmes déclenchements). Échelle logarithmique.")
    fig_forms(forms, OUT / "formalisations.png",
              "Selon la façon de lire la règle, la rotation fait de 0,1× à 6× le détenteur sur la période ; d'un départ à l'autre, "
              "elle gagne à peu près aussi souvent qu'elle perd",
              "Barre : rotation / détenteur (mêmes achats de départ) sur toute la période. Texte : nombre de rotations et bilan des départs "
              "mensuels tenus 12 mois (G gagnés, P perdus, = égalité sans rotation). F0–F3 : déclencheurs, voir le tableau.")
    rr = (rotating["total_x"] / rotating["detenteur_x"]).to_numpy()
    fig_hist(rr, [(1.0, "égalité avec le détenteur", TEXT)], OUT / "grille_variantes.png",
             f"Sur {len(rotating)} réglages qui tournent au moins une fois, {int(rotating['bat_detenteur'].sum())} font mieux que garder "
             f"les mêmes pièces",
             f"Rapport « multiple de la rotation / multiple du détenteur aux mêmes achats » pour {len(grid)} réglages (4 déclencheurs, "
             f"seuil 2/3/4, fourchette 30/60/90 j, bas 10/20/30 %, 1/2/3/5 pièces), dont {len(grid) - len(rotating)} ne tournent jamais "
             f"(exclus). Même période, mêmes frais.", "rotation / détenteur (échelle log)")

    runtime = time.time() - t0
    meta = {"genere": pd.Timestamp.now(tz="UTC").isoformat(), "debut": str(start.date()), "fin": str(end.date()),
            "pieces": len(uni), "retirees": len(delisted), "placebos": args.placebo, "boot": args.boot,
            "dsr": dsr, "meilleur_reglage": best.name, "walk_forward": wf, "derniers_30_jours": last30,
            "part_placebos_battus": pct_plac, "runtime_s": runtime}
    (OUT / "run.json").write_text(json.dumps(meta, indent=2, default=str, ensure_ascii=False))
    write_readme(dict(uni=uni_out, delisted=delisted, start=start, end=end, ev=ev, ddA=ddA, pairs=pairs, ic=ic, perf=perf,
                      plac_x=plac_x, rot_x=rot_x, pct_plac=pct_plac, rot=rot, hold=hold, roll=roll, forms=forms, grid=grid,
                      rotating=rotating, distinct=distinct, dsr=dsr, best=best, apr=apr, wf=wf, wf_df=wf_df, per=per,
                      rob=rob, reg=reg, last30=last30, runtime=runtime))
    log.info("terminé en %.0f s", runtime)
    return 0


def write_readme(d: dict):
    ev, pairs, ic, perf, roll, forms, grid, rotating = (d[k] for k in ("ev", "pairs", "ic", "perf", "roll", "forms", "grid", "rotating"))
    start, end, wf, dsr, best, per, rob, reg, ddA = (d[k] for k in ("start", "end", "wf", "dsr", "best", "per", "rob", "reg", "ddA"))
    e60 = ev[ev["horizon_j"] == 60].set_index("etat")
    A, B, T = e60.loc["A : vient de faire 3x"], e60.loc["B : bas de fourchette"], e60.loc["toute pièce éligible, tout jour"]
    dA = ddA[ddA["etat"].str.startswith("A")].set_index("horizon_j")
    dT = ddA[ddA["etat"].str.startswith("toute")].set_index("horizon_j")
    p60 = pairs[pairs["horizon_j"] == 60].set_index("choix")
    p30 = pairs[pairs["horizon_j"] == 30].set_index("choix")
    pB, pX = p60.loc["rotation vers B (bas de fourchette)"], p60.loc["rotation vers une pièce quelconque"]
    ic_rp = ic[(ic["signal"] == "position dans la fourchette 60 j") & (ic["horizon_j"] == 30)].iloc[0]
    P = perf.set_index("strategie")
    rot = P.loc["rotation (règle testée)"]
    hold = P.loc["détenteur (mêmes achats de départ, aucune rotation)"]
    bas = P.loc["panier équipondéré (30 tranches, rééquilibrage mensuel)"]
    inv = P.loc["rotation inverse (mêmes achats et déclenchements, cible en haut de fourchette)"]
    w, l, t = wlt(roll["rotation"].to_numpy(), roll["detenteur"].to_numpy())
    wb, lb, _ = wlt(roll["rotation"].to_numpy(), roll["panier"].to_numpy())
    wi, li, ti = wlt(roll["inverse"].to_numpy(), roll["rotation"].to_numpy())
    fw, fl, ft = int(forms["fenetres_gagnees"].sum()), int(forms["fenetres_perdues"].sum()), int(forms["fenetres_egales"].sum())
    fbest = forms.loc[(forms["rotation_x"] / forms["detenteur_x"]).idxmax()]
    fworst = forms.loc[(forms["rotation_x"] / forms["detenteur_x"]).idxmin()]
    n_bh = int(rotating["bat_detenteur"].sum())
    wk2 = per[(per["frequence"] == "hebdomadaire") & (per["k"] == 2)].iloc[0]
    tr = d["rot"].trades
    rt = tr[tr["why"] == "rotation"]
    yrs = (end - start).days / 365.25
    q_exec = src_line("v *= O[i + 1, sell] / C[i, sell] * (1.0 - c)")
    q_roll = src_line("lo = close.rolling(w, min_periods=w).min()")
    q_uni = src_line('and "Meme" in (s.get("underlyingSubType") or [])')
    q_where = src_line("tabs[k] = tabs[k].where(last)")
    q_cost = src_line("v *= 1.0 - c")
    q_open = src_line('elif p.execution == "open":')
    q_date = src_line('df["date"] = pd.to_datetime(df["open_ms"], unit="ms").dt.normalize()')
    q_soon = src_line("if s is not None and (soon[i, s] or not act[i + 1, s]):")
    q_cand = src_line("return np.array([x for x in np.flatnonzero(E[i]) if x not in held and act[i + 1, x]], dtype=int)")

    L = []
    L.append("# Memecoins : la « rotation » entre community coins bat-elle la détention ?\n")
    L.append(f"*Généré le {pd.Timestamp.now(tz='UTC').strftime('%d/%m/%Y %H:%M')} UTC par `scripts/memecoin_rotation.py` ({d['runtime']:.0f} s). "
             f"Données : {len(d['uni'])} perpétuels USDT classés « Meme » par Binance, dont {len(d['delisted'])} retirés de la cote, bougies "
             f"quotidiennes ; étude du {day(start)} au {day(end)}. Version corrigée après relecture contradictoire (§ 12).*\n")
    L.append("> Recherche sur données publiques historiques, aucun ordre, aucune recommandation. Les liens de parrainage et le canal "
             "d'« appels » de l'article testé ne sont pas repris ici.\n")
    L.append("## 0. Réponse courte\n")
    L.append("La thèse testée (fil X « Ultimate Memecoin Trading Guide », 2026) : parmi des memecoins établis, vendre celui qui vient de faire "
             "3x (A) et acheter celui qui est en bas de sa fourchette (B), car B « va faire le même 3x » avec « −30 % au pire », alors que A "
             "« fera peut-être +100 % » avant « un repli typique de −70 % ». Exemple de l'auteur : rotateur 10 k$ → 270 k$, détenteur 10 k$ → 27 k$.\n")
    L.append(f"* **B ne fait pas « le même 3x ».** Au premier jour sous 20 % de sa fourchette de 60 jours, un memecoin atteint +200 % dans les "
             f"60 jours suivants dans **{pc(B['p_triple'])}** des cas (IC 95 % {pc(B['p_triple_lo'])} – {pc(B['p_triple_hi'])}), comme une pièce "
             f"éligible quelconque ({pc(T['p_triple'])}). Il reperd 30 % à un moment dans **{pc(B['p_moins30'])}** des cas (IC "
             f"{pc(B['p_moins30_lo'])} – {pc(B['p_moins30_hi'])} ; l'auteur : « −30 % au pire »).")
    L.append(f"* **A retombe bien, mais lentement.** Depuis son sommet, la plus forte baisse médiane après un 3x est de "
             f"{pc(dA.loc[60, 'dd_median'], signed=True)} sur 60 jours et {pc(dA.loc[180, 'dd_median'], signed=True)} sur 180 jours ; "
             f"−70 % ou pire dans {pc(dA.loc[60, 'p_dd70'])} des cas à 60 jours et {pc(dA.loc[180, 'p_dd70'])} à 180 jours (pièce "
             f"quelconque : {pc(dT.loc[180, 'p_dd70'])}). Le « repli typique de −70 % » de l'auteur se vérifie donc à long terme ; "
             f"il ne dit pas quand vendre (n = {int(dA.loc[60, 'n'])} événements).")
    L.append(f"* **La décision de rotation elle-même** ({int(pB['n'])} cas : vendre A le jour de son 3x, acheter la pièce la plus basse de sa "
             f"fourchette, frais déduits) : pour 1 $, la rotation laisse en moyenne **{pc(pB['gain_moyen'], signed=True)}** de richesse à 60 jours par rapport à garder A "
             f"(IC 95 % {pc(pB['ic_lo'], signed=True)} ; {pc(pB['ic_hi'], signed=True)}, médiane {pc(pB['gain_median'], signed=True)}), "
             f"gagnante dans {pc(pB['p_gagne'])} des cas. Vers une pièce éligible quelconque : {pc(pX['gain_moyen'], signed=True)} "
             f"(IC {pc(pX['ic_lo'], signed=True)} ; {pc(pX['ic_hi'], signed=True)}). Aucun des deux n'est démontré.")
    L.append(f"* **Les pièces en retard ne rattrapent pas de façon mesurable.** Corrélation de rang entre la position dans la fourchette et le "
             f"rendement des 30 jours suivants : {fr(ic_rp['ic_moyen'], 3, signed=True)} (t de Newey-West {fr(ic_rp['t_nw'], 1, signed=True)}) : "
             f"ni retour à la moyenne ni momentum démontré.")
    L.append(f"* **Portefeuille de la règle testée (F0, 2 pièces)** : elle ne tourne que {n_rot(d['rot'])} fois en {fr(yrs, 1)} ans ; "
             f"{xm(rot['total_x'])} la mise contre {xm(hold['total_x'])} pour le détenteur des mêmes pièces, {xm(bas['total_x'])} pour le "
             f"panier et {xm(inv['total_x'])} pour la rotation inverse (mêmes achats et déclenchements, cible en haut de fourchette). Ce "
             f"résultat tient à {n_rot(d['rot'])} décisions : ce n'est pas une mesure de la stratégie.")
    L.append(f"* **Quatre lectures de la règle, deux univers, 2 ou 5 pièces** (16 cas, § 6) : sur la période entière, la rotation va de "
             f"{xm(float(fworst['rotation_x'] / fworst['detenteur_x']))} à {xm(float(fbest['rotation_x'] / fbest['detenteur_x']))} le "
             f"détenteur. Le meilleur cas ({fbest['forme']}, {fbest['univers']}, {int(fbest['k'])} pièces : {xm(fbest['rotation_x'])} contre "
             f"{xm(fbest['detenteur_x'])}, {int(fbest['rotations'])} rotations) est une seule trajectoire : sur les départs mensuels tenus "
             f"12 mois, il gagne {int(fbest['fenetres_gagnees'])} fois, perd {int(fbest['fenetres_perdues'])} fois et fait "
             f"{int(fbest['fenetres_egales'])} égalités. Tous cas réunis : {fw} départs gagnés, {fl} perdus, {ft} égalités.")
    L.append(f"* **Tous les réglages** ({len(grid)} ; {len(rotating)} tournent au moins une fois) : {n_bh} des {len(rotating)} font mieux que "
             f"le détenteur aux mêmes achats. Le meilleur ({best.name}) a un Sharpe dégonflé de **{fr(dsr['dsr'], 2)}** "
             f"({dsr['n_trials']} séries distinctes ; seuil 0,95). Walk-forward : le réglage choisi sur la 1re moitié fait "
             f"{xm(wf['rot_med'])} sur la 2e, contre {xm(wf['hold_med'])} pour le détenteur aux mêmes achats.")
    L.append(f"* **Une rotation systématique vers le bas de fourchette détruit de la valeur** : chaque semaine vers les 2 pièces les plus "
             f"basses, {xm(wk2['bas_x'])} la mise contre {xm(wk2['panier_x'])} pour le panier (écart {pc(math.expm1(wk2['exces_bas_panier_log_an']), signed=True)} "
             f"par an, IC {pc(math.expm1(wk2['ic_lo']), signed=True)} ; {pc(math.expm1(wk2['ic_hi']), signed=True)}).")
    L.append(f"* **Verdict.** Aucune version de « vendre le 3x, acheter le bas de fourchette » ne montre d'avantage qui se répète d'un "
             f"point de départ à l'autre, ni dans les décisions elles-mêmes. Le calcul de l'auteur (gain/risque 6,7 contre 1,4 ; 270 k$ "
             f"contre 27 k$) suppose connue l'issue : que B refera 3x ({pc(B['p_triple'])} des cas mesurés) sans perdre plus de 30 % "
             f"({pc(1 - B['p_moins30'])}). Une trajectoire spectaculaire existe (F2), mais c'est une trajectoire, pas une règle.\n")

    L.append("## 1. Données et règle testée\n")
    L.append(f"* **Univers** : les perpétuels USDT que Binance classe « Meme » (`underlyingSubType`), y compris ceux retirés de la cote "
             f"({', '.join(sorted(s.replace('USDT', '') for s in d['delisted']))}). Le prix du perpétuel sert de prix spot (sans levier ni "
             f"financement) : l'écart est de quelques pb pour les 28 pièces qui ont aussi un marché spot Binance. Liste : `univers.csv`.")
    L.append(f"* **Éligible au jour d** (point-in-time) : échangé ce jour, coté depuis au moins 60 jours, volume quotidien médian (30 j) d'au "
             f"moins 5 M$, et pas de retrait de la cote annoncé. Binance annonce ses retraits quelques jours à l'avance (NEIROETH : annonce le "
             f"22/09/2025, règlement le 26/09) : on considère l'information publique 5 jours avant le dernier jour échangé, et une pièce détenue "
             f"est vendue le lendemain de l'annonce. Début : premier jour avec au moins {MIN_ELIGIBLE} pièces éligibles ({day(start)}).")
    L.append("* **Règle testée d'abord (F0)** : 2 pièces. À la clôture de chaque jour, une pièce détenue qui vaut au moins 3 × son plus bas "
             "des 30 derniers jours est vendue si une pièce éligible non détenue est sous 20 % de sa fourchette de 60 jours ; on achète alors "
             "la plus basse. Achats de départ : les pièces les plus basses de leur fourchette. Exécution à l'ouverture du lendemain, 0,75 % de "
             "frais et glissement par échange (l'auteur : « 1–2 % par rotation »). Trois autres déclencheurs (F1–F3) sont testés au § 6.")
    L.append("* **Comparaisons à conditions égales** : le détenteur (mêmes achats de départ, aucune rotation), la rotation inverse et 500 "
             "placebos (mêmes achats de départ, mêmes déclenchements, seule la pièce achetée change : la plus haute, ou une au hasard), et "
             "le panier équipondéré de toutes les pièces éligibles (30 tranches rééquilibrées chacune tous les 30 jours à des dates décalées, "
             "mêmes frais).\n")

    L.append("## 2. A après un 3x, B en bas de fourchette : ce qui arrive ensuite\n")
    L.append("![états A et B](etats_A_B.png)\n")
    L.append("| état (60 jours suivants) | n | atteint +100 % | atteint +200 % (3x) | touche −30 % | touche −70 % | rendement médian | rendement moyen |")
    L.append("|---|---|---|---|---|---|---|---|")
    for lab in ("A : vient de faire 3x", "B : bas de fourchette", "toute pièce éligible, tout jour"):
        r = e60.loc[lab]
        L.append(f"| {lab} | {int(r['n'])} | {pc(r['p_double'])} | {pc(r['p_triple'])} | {pc(r['p_moins30'])} | {pc(r['p_moins70'])} | "
                 f"{pc(r['ret_median'], signed=True)} | {pc(r['ret_moyen'], signed=True)} |")
    L.append("\n« Touche −30 % » se mesure depuis le prix du jour de l'état. La baisse depuis un sommet (ce que l'auteur appelle un repli) :\n")
    L.append(md_table(ddA, [("horizon_j", "horizon", lambda v: f"{int(v)} j"), ("etat", "état", str), ("n", "n", lambda v: str(int(v))),
                            ("dd_median", "baisse médiane depuis un sommet", lambda v: pc(v, 0, signed=True)),
                            ("p_dd50", "−50 % ou pire", lambda v: pc(v)), ("p_dd70", "−70 % ou pire", lambda v: pc(v))]))
    L.append("Hypothèses de l'auteur, face à la mesure :\n")
    L.append("| affirmation | mesure |")
    L.append("|---|---|")
    L.append(f"| A après un 3x : « peut-être +100 % » | +100 % atteint dans {pc(A['p_double'])} des cas en 60 jours |")
    L.append(f"| A après un 3x : « repli typique de −70 % » | depuis un sommet : −70 % ou pire dans {pc(dA.loc[60, 'p_dd70'])} des cas en 60 jours, {pc(dA.loc[180, 'p_dd70'])} en 180 jours |")
    L.append(f"| B en bas de fourchette : « le même 3x, +200 % » | +200 % atteint dans {pc(B['p_triple'])} des cas en 60 jours |")
    L.append(f"| B en bas de fourchette : « −30 % au pire » | −30 % touché dans {pc(B['p_moins30'])} des cas en 60 jours ; plus bas médian {pc(B['min_median'], signed=True)} |")
    L.append("")
    L.append("## 3. La décision de rotation, appariée\n")
    L.append("Le jour où A fait 3x : ce que rapporte, pour 1 $, le fait de vendre A et d'acheter B (ou une pièce quelconque), en "
             "richesse finale, frais de la vente et de l'achat déduits : (1 + r_B)(1 − coût)² − (1 + r_A). La moyenne est l'écart "
             "d'espérance de richesse ; la médiane dit ce qui arrive le plus souvent. IC 95 % par bootstrap en tirant des mois entiers.\n")
    L.append(md_table(pairs, [("horizon_j", "horizon", lambda v: f"{int(v)} j"), ("choix", "on achète", str), ("n", "rotations", lambda v: str(int(v))),
                              ("gain_moyen", "gain moyen", lambda v: pc(v, 1, signed=True)), ("ic_lo", "IC 95 % bas", lambda v: pc(v, 1, signed=True)),
                              ("ic_hi", "IC 95 % haut", lambda v: pc(v, 1, signed=True)), ("gain_median", "gain médian", lambda v: pc(v, 1, signed=True)),
                              ("p_gagne", "la rotation gagne", lambda v: pc(v))]))
    L.append("## 4. Les pièces en retard rattrapent-elles ?\n")
    L.append("Corrélation de rang (Spearman), jour par jour, entre un signal et le rendement futur, sur les pièces éligibles ; t de Newey-West "
             "(rendements futurs qui se chevauchent). Négatif = retour à la moyenne (ce qui ferait marcher la rotation), positif = momentum.\n")
    L.append(md_table(ic, [("signal", "signal", str), ("horizon_j", "horizon", lambda v: f"{int(v)} j"), ("jours", "jours", lambda v: str(int(v))),
                           ("ic_moyen", "corrélation moyenne", lambda v: fr(v, 3, signed=True)), ("t_nw", "t (Newey-West)", lambda v: fr(v, 1, signed=True)),
                           ("part_positive", "jours positifs", lambda v: pc(v))]))
    L.append("## 5. Portefeuilles de la règle testée (F0)\n")
    L.append("![portefeuilles](portefeuilles.png)\n")
    L.append(md_table(perf, [("strategie", "stratégie", str), ("total_x", "multiple final", xm), ("cagr", "par an", lambda v: pc(v, 0, signed=True)),
                             ("vol", "volatilité", lambda v: pc(v)), ("sharpe", "Sharpe", lambda v: fr(v, 2)), ("max_dd", "perte max.", lambda v: pc(v)),
                             ("rotations", "rotations", lambda v: "—" if not np.isfinite(v) else str(int(v)))]))
    L.append(f"* Rotations effectuées : " + (" ; ".join(
        f"{day(r.date)} : {r.sell.replace('USDT', '')} ({xm(r.sell_vs_low, 1)} son plus bas de 30 j) → {r.buy.replace('USDT', '')}"
        for r in rt.itertuples()) or "aucune") + ". Le multiple final tient à ces décisions et aux deux achats de départ.")
    L.append(f"* Placebos (mêmes achats de départ et mêmes déclenchements, pièce achetée au hasard) : multiple médian "
             f"{xm(float(np.median(d['plac_x'])))}, 5–95 % {xm(float(np.percentile(d['plac_x'], 5)))} – {xm(float(np.percentile(d['plac_x'], 95)))} ; "
             f"la règle ({xm(d['rot_x'])}) fait mieux que {pc(d['pct_plac'])} d'entre eux.")
    L.append(f"* Départs mensuels tenus 12 mois ({len(roll)} fenêtres qui se chevauchent à 11 mois sur 12, soit environ "
             f"{fr((end - start).days / 365.25, 1)} années indépendantes) : contre le détenteur, {w} gagnées, {l} perdues, {t} égalités "
             f"(aucune rotation) ; test de signe sur les cas tranchés p = {fr(sign_test_p(w, l), 2)}. Contre le panier : {wb} gagnées, {lb} "
             f"perdues. La rotation inverse bat la règle dans {wi} fenêtres et perd dans {li}.")
    L.append(f"* Sur les 30 derniers jours (l'auteur parle de ses propres 30 derniers jours, publiés la veille de cette étude) : "
             + " ; ".join(f"{k.split(' (')[0]} {xm(v)}" for k, v in d["last30"].items()) + ".\n")
    L.append("## 6. Quatre lectures de la règle, deux univers\n")
    L.append("![formalisations](formalisations.png)\n")
    L.append("F0 : 3 × le plus bas des 30 derniers jours. F1 : 3 × le plus bas de la fourchette de 60 jours. F2 : 3 × le prix d'achat. "
             "F3 : en haut de fourchette (≥ 90 %) et 2 × son plus bas. Univers « community coins » : pièces qui ont déjà perdu 70 % depuis "
             "un sommet puis doublé depuis le creux (critère de l'auteur, point-in-time). Placebos : 100 tirages, mêmes achats et "
             "déclenchements.\n")
    L.append(md_table(forms, [("univers", "univers", str), ("forme", "règle", str), ("k", "pièces", lambda v: str(int(v))),
                              ("rotation_x", "rotation", xm), ("detenteur_x", "détenteur", xm), ("rotations", "rotations", lambda v: str(int(v))),
                              ("part_placebos_battus", "placebos battus", lambda v: pc(v)),
                              ("fenetres_gagnees", "départs gagnés", lambda v: str(int(v))), ("fenetres_perdues", "perdus", lambda v: str(int(v))),
                              ("fenetres_egales", "égalités", lambda v: str(int(v))), ("p_signe", "test de signe p", lambda v: fr(v, 2))]))
    L.append(f"Le cas le plus favorable à l'auteur ({fbest['forme']}, {fbest['univers']}, {int(fbest['k'])} pièces) enchaîne quelques "
             f"triplements successifs et bat la plupart de ses placebos sur la période entière. Mais d'un départ mensuel à l'autre il ne gagne "
             f"pas plus souvent qu'il ne perd : c'est une trajectoire chanceuse, pas un avantage qui se répète.\n")
    L.append("## 7. Tous les réglages, et la correction pour essais multiples\n")
    L.append("![grille](grille_variantes.png)\n")
    L.append(f"* {len(grid)} réglages (`grille_variantes.csv`) : {len(grid) - len(rotating)} ne tournent jamais (ce sont des détentions, "
             f"exclues). Parmi les {len(rotating)} autres, {n_bh} font mieux que le détenteur aux mêmes achats et "
             f"{int(rotating['bat_panier'].sum())} mieux que le panier.")
    L.append(f"* Meilleur réglage qui tourne : {best.name}, {xm(float(best['total_x']))} contre {xm(float(best['detenteur_x']))} pour son "
             f"détenteur. Sharpe dégonflé (Bailey et López de Prado, Sharpe **par jour** de l'écart au détenteur, {dsr['n_trials']} séries "
             f"distinctes) : **{fr(dsr['dsr'], 2)}**, sous le seuil de 0,95.")
    tete = (f"le meilleur réglage qui tourne ({wf['variantes'][0]})" if wf['n_egalites'] == 1 else
            f"{wf['n_egalites']} réglages qui tournent, à égalité en tête (même série de rendements)")
    L.append(f"* Walk-forward : sur {day(start)} – {day(pd.Timestamp(wf['coupure']))}, on retient {tete}. Sur la 2e moitié : "
             f"{xm(wf['rot_med'])} (médiane), contre {xm(wf['hold_med'])} pour le détenteur aux mêmes achats et {xm(wf['panier_2e'])} pour "
             f"le panier (`walk_forward.csv`).\n")
    L.append("## 8. Rotation systématique\n")
    L.append("Tous les 7 ou 30 jours, détenir à parts égales les k pièces les plus basses (ou les plus hautes, ou au hasard) de leur "
             "fourchette de 60 jours. Écart au panier : log annualisé, IC 95 % par blocs de 30 jours.\n")
    L.append(md_table(per, [("frequence", "fréquence", str), ("k", "pièces", lambda v: str(int(v))), ("bas_x", "bas de fourchette", xm),
                            ("haut_x", "haut de fourchette", xm), ("hasard_median_x", "au hasard (médiane)", xm), ("panier_x", "panier", xm),
                            ("exces_bas_panier_log_an", "bas − panier, par an", lambda v: pc(math.expm1(v), 0, signed=True)),
                            ("ic_lo", "IC bas", lambda v: pc(math.expm1(v), 0, signed=True)), ("ic_hi", "IC haut", lambda v: pc(math.expm1(v), 0, signed=True))]))
    L.append("## 9. Robustesse et régimes\n")
    rob2 = rob.copy()
    rob2["cas"] = [c + (" — aucune rotation : non informatif" if n == 0 else "") for c, n in zip(rob2["cas"], rob2["rotations"])]
    L.append(md_table(rob2, [("cas", "variante (règle F0)", str), ("rotation_x", "rotation", xm), ("detenteur_x", "détenteur", xm), ("panier_x", "panier", xm),
                             ("rotation_dd", "perte max. rotation", lambda v: pc(v)), ("rotations", "rotations", lambda v: str(int(v)))]))
    L.append("Régimes (indice équipondéré des memecoins au-dessus ou en dessous de sa moyenne mobile 200 jours, mesuré la veille) :\n")
    L.append(md_table(reg, [("regime", "régime", str), ("strategie", "stratégie", str), ("jours", "jours", lambda v: str(int(v))),
                            ("multiple", "multiple sur ces jours", xm), ("rendement_log_an", "rendement log annualisé", lambda v: pc(v, 0, signed=True))]))
    L.append("La règle F0 ne tourne que quelques fois : l'écart entre régimes reflète ces décisions, pas une propriété de la règle.\n")
    L.append("## 10. Limites\n")
    L.append("* **Univers Binance.** Les pièces de l'auteur (CATE, NEET…) ne sont pas cotées sur Binance ; celles qui le sont (SPX, FARTCOIN, "
             "USELESS, POPCAT, PEPE, BONK, WIF…) sont les « community coins » les plus liquides. Être coté est déjà une sélection, connue le "
             "jour de la cotation. L'étiquette « Meme » est celle de Binance aujourd'hui : quelques memecoins étiquetés AI ou Gaming en sont "
             "absents ; les ajouter ne change pas le verdict (relecture : la part des départs où la rotation bat le panier passerait de 19 % à 29 %).")
    L.append("* **Bougies quotidiennes.** Un 3x fait et défait dans la journée échappe à la règle ; l'auteur dit détenir 38 jours en moyenne. "
             "La règle F0 à 2 pièces détient bien plus longtemps : c'est pourquoi le § 6 teste d'autres déclencheurs et 5 pièces.")
    L.append("* **Petits échantillons.** Quelques dizaines de 3x, 2 à 3 années indépendantes de départs glissants, un seul cycle des "
             "memecoins (2024–2026). La conclusion est l'absence d'avantage démontré, pas une perte démontrée (sauf la rotation "
             "systématique du § 8).")
    L.append("* **Prix du perpétuel.** Pour les pièces sans spot Binance, l'écart au spot n'est pas mesuré ici (la relecture l'estime à "
             "moins de 1 % sur les deux achats concernés).\n")
    L.append("## 11. Grille en 8 points appliquée à ce backtest\n")
    L.append("Statut du **défaut** (PRÉSENT = le défaut existe), avec la ligne de code qui le montre.\n")
    L.append(f"1. **Look-ahead** : ABSENT, avec une exception documentée. Décision à la clôture de d, exécution le lendemain : {q_exec} ; "
             f"signaux sur fenêtres qui finissent au jour d : {q_roll}. Le moteur lit le statut de cotation du lendemain ({q_soon} ; "
             f"{q_cand}) : c'est une information publique grâce au préavis de retrait de Binance ; sans effet chiffré (relecture : un seul cas "
             f"concerné, NEIROETH).")
    L.append(f"2. **Survivorship** : ABSENT par rapport à la cote Binance (retirés compris : {q_uni}) ; PARTIEL par rapport aux DEX.")
    L.append(f"3. **Repainting** : ABSENT. Fenêtres glissantes non centrées, prix figés après retrait : {q_where} ; le régime est mesuré la "
             f"veille (`.shift(1)` dans `scripts/memecoin_rotation.py`).")
    L.append(f"4. **Coûts** : ABSENT. Chaque vente et chaque achat paient `cost` : {q_cost} ; le panier paie le même coût sur ses rééquilibrages.")
    L.append(f"5. **Exécution à un prix jamais disponible** : PARTIEL. L'ouverture de J+1 vaut la clôture de J sur un marché ouvert 24 h/24 "
             f"(écart médian 0,8 pb) : {q_open} ; variante à la clôture de J+1, panier compris, au § 9.")
    L.append("6. **Ajustement des paramètres** : F0 fixée avant le test sur les chiffres de l'article ; F1–F3 ajoutées pour ne pas tester un "
             "homme de paille ; 432 réglages essayés ensuite, comptés dans le Sharpe dégonflé (séries distinctes) et testés en walk-forward.")
    L.append("7. **Échantillon** : PARTIEL. Hausse 2024 puis baisse : les deux régimes sont présents (§ 9), mais sur un seul cycle.")
    L.append(f"8. **Alignement** : ABSENT. Une seule source, bougies UTC 00:00 : {q_date}.\n")
    L.append("## 12. Corrections apportées après la relecture contradictoire\n")
    L.append("Quatre relecteurs (fuite d'information, statistiques, exécution, fidélité à l'auteur) et un contradicteur par défaut signalé. "
             "Défauts confirmés et corrigés dans cette version :\n")
    for item in ("les réglages qui ne tournent jamais étaient comptés comme des rotations « qui battent le panier » : ils sont exclus (§ 7) ;",
                 "les départs glissants comptaient les égalités (aucune rotation) comme des défaites : victoires, défaites et égalités sont "
                 "séparées, et le chevauchement des fenêtres est indiqué ;",
                 "le test apparié moyennait des log-rendements présentés comme des rendements : il donne maintenant le gain moyen en richesse ;",
                 "la rotation inverse et les placebos ne partaient pas des mêmes achats que la règle, et les placebos vendaient à chaque 3x : "
                 "achats de départ et déclenchements sont maintenant identiques, seule la cible change ;",
                 "le Sharpe dégonflé comptait 162 essais dont 72 doublons : il est calculé sur les séries distinctes des réglages qui tournent ;",
                 "le walk-forward choisissait, par un départage arbitraire, une détention de PEPE : il choisit parmi les réglages qui tournent "
                 "et rapporte tous les ex aequo ;",
                 "le « repli de −70 % » était mesuré depuis le jour du 3x : il l'est depuis un sommet, sur 60 à 180 jours ;",
                 "une formalisation unique qui ne tournait que 2 fois : 4 déclencheurs, 2 univers, 2 ou 5 pièces (§ 6) ;",
                 "les pièces dont le retrait était annoncé restaient achetables : préavis de 5 jours ;",
                 "le panier était rééquilibré à une date favorable et exécuté sans délai dans la variante prudente : 30 tranches décalées, "
                 "même délai que la rotation ;",
                 "le seuil de volume n'était pas testé : 5, 10, 20 et 50 M$ (§ 9) ; la rotation systématique vers le bas de fourchette est "
                 "ajoutée (§ 8)."):
        L.append(f"* {item[0].upper() + item[1:]}")
    L.append("")
    L.append("## Fichiers\n")
    for f, dsc in (("univers.csv", "les pièces, dates de cotation et de retrait, jours éligibles"),
                   ("etats_A_B.csv", "ce qui suit un 3x (A) et un bas de fourchette (B), 30 et 60 jours"),
                   ("baisse_depuis_sommet_A.csv", "baisse depuis un sommet après un 3x, 60 à 180 jours"),
                   ("rotations_appariees_resume.csv", "décision de rotation appariée, résumé"),
                   ("rotations_appariees_60j.csv", "chaque rotation appariée à 60 jours"),
                   ("ic_retour_moyenne.csv", "corrélations de rang signal / rendement futur"),
                   ("portefeuilles.csv", "performances des portefeuilles de la règle F0"),
                   ("rotations_regle_testee.csv", "chaque achat et rotation de la règle F0"),
                   ("departs_glissants_12_mois.csv", "départs mensuels tenus 12 mois"),
                   ("formalisations.csv", "F0–F3 × univers × nombre de pièces"),
                   ("grille_variantes.csv", "tous les réglages essayés"), ("walk_forward.csv", "réglages choisis sur la 1re moitié, 2e moitié"),
                   ("rotation_periodique.csv", "rotation systématique"), ("robustesse_variantes.csv", "frais, exécution, retraits, volume, univers"),
                   ("regimes.csv", "par régime de marché"), ("run.json", "paramètres et résumé")):
        L.append(f"* `{f}` : {dsc}")
    (OUT / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
