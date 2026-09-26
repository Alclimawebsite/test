"""Simulation MAKER sur le carnet réel Polymarket « Up or Down » (enregistrements WebSocket du
collecteur ``scripts/polymarket_live_collector.py``).

Pour chaque marché collecté et terminé : reconstruction du carnet (:mod:`tradebot.polymarket_book`),
profondeur et écart autour de l'ouverture S, file d'attente typique aux niveaux 0,48–0,52,
part des trades preneurs qui traversent plusieurs niveaux, puis simulation d'ordres au repos
(:mod:`tradebot.polymarket_maker_live`) : (a) signal TWAP partiel à S−30 s, (b) deux côtés à 0,49,
(c) juste valeur Φ(d/σ) ± 1 c re-cotée toutes les 10 s. Sorties dans ``reports/polymarket/maker_live/``
(README.md, CSV, PNG).

Usage (à relancer à tout moment, le collecteur peut tourner en parallèle) :

    . .venv/bin/activate
    python scripts/polymarket_maker_live.py            # tout ce qui est collecté à l'instant T
    python scripts/polymarket_maker_live.py --no-refresh --workers 1 --B 500   # plus léger

Simulation papier : aucun ordre, aucune clé. Le rafraîchissement des issues interroge gamma
(lecture seule) ; les bougies 1 s Binance viennent de ``data-api.binance.vision`` (cache parquet).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot import polymarket_book as bk  # noqa: E402
from tradebot import polymarket_maker_live as mk  # noqa: E402
from tradebot.config import REPORTS_DIR  # noqa: E402
from tradebot.polymarket import ASSET_TO_SYMBOL  # noqa: E402
from tradebot.polymarket_backtest import SlotBootstrap  # noqa: E402
from tradebot.report import fmt_number, to_markdown  # noqa: E402

log = logging.getLogger("maker_live")

INSTANTS = (-60, -30, 0, 30)
LEVEL_COLS = ["bid_up_048", "bid_up_049", "bid_up_050", "bid_up_051", "bid_up_052",
              "bid_dn_048", "bid_dn_049", "bid_dn_050", "bid_dn_051", "bid_dn_052"]
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#4a3aa7"
STRAT_COLORS = {"signal": C_BLUE, "two_sided": C_ORANGE, "fair_value": C_AQUA}
STRAT_LABELS = {"signal": "(a) signal TWAP partiel à S−30 s", "two_sided": "(b) deux côtés à 0,49",
                "fair_value": "(c) juste valeur ± 1 c"}


def fr(x, nd=2, signed=False, pct=False) -> str:
    return fmt_number(x, nd, signed=signed, pct=pct)


def cents(x, nd=1) -> str:
    return fmt_number(x, nd, signed=True) + " c" if x is not None and np.isfinite(x) else "—"


def ic(lo, hi, nd=1, unit=" c") -> str:
    if lo is None or hi is None or not (np.isfinite(lo) and np.isfinite(hi)):
        return "IC —"
    return f"IC {fmt_number(lo, nd, signed=True)} ; {fmt_number(hi, nd, signed=True)}{unit}"


def cell_name(asset: str, duration: str) -> str:
    return f"{asset.upper()} {duration}"


# ---------------------------------------------------------------------------
# Un marché (exécuté dans un processus fils)
# ---------------------------------------------------------------------------
def process_market(slug: str, live_dir: str, size: float, latency_ms: int, now: float) -> dict:
    live = Path(live_dir)
    t0 = time.time()
    m = bk.load_market(slug, live)
    S, E, D = m.start_ts, m.end_ts, m.duration_s
    cov0, cov1 = m.coverage()
    info = {"slug": slug, "asset": m.asset, "duration": m.duration, "start": pd.Timestamp(S, unit="s", tz="UTC"),
            "start_ts": S, "slot": S // 900, "resolved_up": m.resolved_up, "price_to_beat": m.price_to_beat,
            "final_price": m.final_price, "n_snapshots": m.n_events("book"), "n_pc": m.n_events("pc"),
            "n_trades_msgs": m.n_events("trade"), "coverage_start_rel": cov0 - S if np.isfinite(cov0) else np.nan,
            "coverage_end_rel": cov1 - S if np.isfinite(cov1) else np.nan, "format": m.meta.get("format", 1),
            "n_files": len(m.files), "usable": True, "reason": ""}
    if E + 60 > now:
        info.update(usable=False, reason="fenêtre en cours")
        return {"info": info}
    if m.n_events("book") == 0:
        info.update(usable=False, reason="aucun instantané")
        return {"info": info}
    ser = bk.book_series(m, step_s=1.0, t0_rel=-150.0, t1_rel=D + 60.0)
    at = ser.set_index("t_rel_s")
    if not bool(at.loc[-60.0, "fresh"]):
        info.update(usable=False, reason="pas de carnet à S−60 s (collecte commencée trop tard)")
    ctl = bk.bba_control(m)
    info.update(bba_n=ctl["n"], bba_agree_rate=ctl["agree_rate"])
    for t in INSTANTS:
        r = at.loc[float(t)]
        info[f"mid_{t:+d}"] = r["mid"]
        info[f"spread_{t:+d}"] = r["spread"]
        info[f"depth_best_bid_{t:+d}"] = r["depth_best_bid"]
        info[f"depth_best_ask_{t:+d}"] = r["depth_best_ask"]
    info["mid_end"] = at.loc[float(D), "mid"]
    tr = bk.trades_frame(m)
    tr.insert(0, "slug", slug)
    tr["asset"], tr["duration"], tr["slot"] = m.asset, m.duration, S // 900
    info["n_trades"] = len(tr)
    # Binance 1 s : signal et juste valeur
    sym = ASSET_TO_SYMBOL.get(m.asset)
    gap = np.nan
    fv = pd.DataFrame()
    try:
        bars = mk.fetch_binance_1s(sym, S - 130, E + 2)
        gap = mk.partial_twap_gap(bars, S)
        fv = mk.fair_value_series(bars, S, E)
    except Exception as exc:  # noqa: BLE001
        info["binance_error"] = repr(exc)[:120]
    info["gap_m30"] = gap
    orders_df = pd.DataFrame()
    fv_ctl = []
    if info["usable"]:
        bbo = ser[(ser["t_rel_s"] >= 0) & (ser["t_rel_s"] <= D) & (ser["t_rel_s"] % 10 == 0)][["t_rel_s", "best_bid", "best_ask"]]
        orders = (mk.orders_signal(m, gap, size=size) + mk.orders_two_sided(m, size=size)
                  + (mk.orders_fair_value(m, fv, size=size, bbo=bbo) if len(fv) else []))
        res = mk.simulate(m, orders, latency_ms=latency_ms)
        orders_df = mk.results_frame(res, m)
        orders_df["slot"] = S // 900
        if len(fv):
            p_of = fv.set_index("t_rel_s")["p_up"]
            for t in (30, 60, D // 2, D - 60):
                fv_ctl.append({"slug": slug, "duration": m.duration, "slot": S // 900, "t_rel_s": t,
                               "p_fair": p_of.get(t, np.nan), "p_mid": at.loc[float(t), "mid"],
                               "y": np.nan if m.resolved_up is None else float(m.resolved_up)})
    ser.insert(0, "slug", slug)
    ser["asset"], ser["duration"] = m.asset, m.duration
    info["seconds"] = time.time() - t0
    return {"info": info, "series": ser, "trades": tr, "orders": orders_df, "fv_ctl": pd.DataFrame(fv_ctl)}


# ---------------------------------------------------------------------------
# Agrégats
# ---------------------------------------------------------------------------
def q_stats(x: pd.Series) -> dict:
    x = pd.to_numeric(x, errors="coerce").dropna()
    if not len(x):
        return {"n": 0, "median": np.nan, "p25": np.nan, "p75": np.nan}
    return {"n": int(len(x)), "median": float(x.median()), "p25": float(x.quantile(0.25)), "p75": float(x.quantile(0.75))}


def cells_of(df: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    out = [("Tous", pd.Series(True, index=df.index))]
    for (a, d), _ in df.groupby(["asset", "duration"]):
        out.append((cell_name(a, d), (df["asset"] == a) & (df["duration"] == d)))
    return out


def depth_tables(series: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(profondeur par instant et cellule, file d'attente avant/après S, séries médianes par cellule)."""
    cols = ["spread", "mid", "depth_best_bid", "depth_best_ask", "depth_bid_10c", "depth_ask_10c"] + LEVEL_COLS
    rows = []
    for name, mask in cells_of(series):
        s = series[mask]
        for t in INSTANTS:
            r = s[s["t_rel_s"] == float(t)]
            for c in cols:
                st = q_stats(r[c])
                rows.append({"cellule": name, "instant_s": t, "mesure": c, **st})
    depth = pd.DataFrame(rows)
    rows = []
    for name, mask in cells_of(series):
        s = series[mask]
        for phase, ts in (("avant S (S−60 s, S−30 s)", (-60.0, -30.0)), ("après S (S, S+30 s)", (0.0, 30.0))):
            r = s[s["t_rel_s"].isin(ts)]
            for c in LEVEL_COLS:
                st = q_stats(r[c])
                rows.append({"cellule": name, "phase": phase, "niveau": c, **st})
    queue = pd.DataFrame(rows)
    med_rows = []
    for name, mask in cells_of(series):
        s = series[mask & (series["t_rel_s"] >= -150) & (series["t_rel_s"] <= 120)]
        g = s.groupby("t_rel_s")
        med = g[cols].median()
        med.columns = [f"{c}_median" for c in cols]
        q25 = g[["spread", "depth_best_bid"]].quantile(0.25).rename(columns=lambda c: f"{c}_p25")
        q75 = g[["spread", "depth_best_bid"]].quantile(0.75).rename(columns=lambda c: f"{c}_p75")
        out = pd.concat([med, q25, q75], axis=1)
        out.insert(0, "n_marches", g["slug"].nunique())
        out.insert(0, "cellule", name)
        med_rows.append(out.reset_index())
    return depth, queue, pd.concat(med_rows, ignore_index=True)


def trades_stats(trades: pd.DataFrame, B: int, seed: int) -> pd.DataFrame:
    t = trades[np.isfinite(trades["best_bid_pre"])].copy()
    t["window"] = np.select([t["t_rel_s"] < -60, t["t_rel_s"] < 0, t["t_rel_s"] < 60], ["avant S−60 s", "[S−60 s, S)", "[S, S+60 s)"], "après S+60 s")
    rows = []
    boot = SlotBootstrap(t["slot"].to_numpy(), B=B, seed=seed) if len(t) else None
    for name, mask in cells_of(t):
        for w in ["toutes", "avant S−60 s", "[S−60 s, S)", "[S, S+60 s)", "après S+60 s"]:
            mm = mask & ((t["window"] == w) if w != "toutes" else True)
            g = t[mm]
            if not len(g):
                continue
            row = {"cellule": name, "fenêtre": w, "n_trades": len(g), "part_traverse": g["traverses"].mean(),
                   "part_depasse_profondeur": g["exceeds_depth"].mean(), "taille_mediane": g["size"].median(),
                   "taille_p90": g["size"].quantile(0.9), "part_achats": (g["side"] == "BUY").mean()}
            if boot is not None:
                _, lo, hi = boot.mean(t["traverses"].to_numpy(dtype=float), mm.to_numpy())
                row.update(part_traverse_ic_bas=lo, part_traverse_ic_haut=hi)
            rows.append(row)
    return pd.DataFrame(rows)


def fair_value_control(fvc: pd.DataFrame, B: int, seed: int) -> pd.DataFrame:
    f = fvc[np.isfinite(fvc["y"]) & np.isfinite(fvc["p_fair"]) & np.isfinite(fvc["p_mid"])].copy()
    if not len(f):
        return pd.DataFrame()
    f["brier_fair"] = (f["p_fair"] - f["y"]) ** 2
    f["brier_mid"] = (f["p_mid"] - f["y"]) ** 2
    f["d_brier"] = f["brier_fair"] - f["brier_mid"]
    f["acc_fair"] = ((f["p_fair"] >= 0.5) == (f["y"] == 1)).astype(float)
    f["acc_mid"] = ((f["p_mid"] >= 0.5) == (f["y"] == 1)).astype(float)
    boot = SlotBootstrap(f["slot"].to_numpy(), B=B, seed=seed)
    rows = []
    for dur in ["toutes"] + sorted(f["duration"].unique()):
        for t in sorted(f["t_rel_s"].unique()):
            mask = ((f["t_rel_s"] == t) & ((f["duration"] == dur) if dur != "toutes" else True)).to_numpy()
            if mask.sum() < 3:
                continue
            _, lo, hi = boot.mean(f["d_brier"].to_numpy(), mask)
            rows.append({"durée": dur, "t_rel_s": t, "n": int(mask.sum()), "brier_juste_valeur": f.loc[mask, "brier_fair"].mean(),
                         "brier_marché": f.loc[mask, "brier_mid"].mean(), "d_brier": f.loc[mask, "d_brier"].mean(),
                         "d_brier_ic_bas": lo, "d_brier_ic_haut": hi, "justesse_juste_valeur": f.loc[mask, "acc_fair"].mean(),
                         "justesse_marché": f.loc[mask, "acc_mid"].mean()})
    return pd.DataFrame(rows)


def strategy_tables(orders: pd.DataFrame, mid_ts: int, B: int, seed: int) -> dict:
    o = orders.copy()
    o["half"] = np.where(o["start_ts"] < mid_ts, "1re moitié", "2e moitié")
    o["cellule"] = [cell_name(a, d) for a, d in zip(o["asset"], o["duration"])]
    boot = SlotBootstrap(o["slot"].to_numpy(), B=B, seed=seed)
    out = {
        "by_label": mk.summarize(o, by=["strategy", "label"], boot=boot),
        "by_strategy": mk.summarize(o, by=["strategy"], boot=boot),
        "by_half": mk.summarize(o, by=["strategy", "label", "half"], boot=boot),
        "by_cell": mk.summarize(o, by=["strategy", "cellule"], boot=boot),
    }
    sub = o[o["strategy"] != "fair_value"].reset_index(drop=True)
    out["by_side"] = (mk.summarize(sub, by=["strategy", "side"], boot=SlotBootstrap(sub["slot"].to_numpy(), B=B, seed=seed))
                      if len(sub) else pd.DataFrame())
    # (a) : choix (prix, annulation) sur la 1re moitié, évaluation sur la 2e
    sig = o[(o["strategy"] == "signal") & (o["half"] == "1re moitié") & o["placed"] & np.isfinite(o["pnl_per_share"])]
    chosen = None
    if len(sig):
        s1 = sig.groupby("label").agg(n=("slug", "size"), pnl=("pnl_per_placed_share", "mean")).reset_index()
        s1 = s1[s1["n"] >= 5]
        if len(s1):
            chosen = s1.sort_values("pnl", ascending=False).iloc[0]["label"]
    out["chosen_label"] = chosen
    if chosen is not None:
        h = out["by_half"]
        out["chosen_eval"] = h[(h["strategy"] == "signal") & (h["label"] == chosen)]
    else:
        out["chosen_eval"] = pd.DataFrame()
    # (b) : P&L par paire (Up + Down) par marché et annulation
    two = o[(o["strategy"] == "two_sided") & o["placed"]]
    pair_rows = []
    for (slug, lab), g in two.groupby(["slug", "label"]):
        if len(g) < 2:
            continue
        pair_rows.append({"slug": slug, "label": lab, "slot": g["slot"].iloc[0], "n_exec": int(g["executed"].sum()),
                          "both": bool(g["executed"].all()), "pnl_pair_c": 100 * g["pnl_order"].sum() / g["size"].iloc[0]
                          if np.isfinite(g["pnl_order"]).all() else np.nan})
    pairs = pd.DataFrame(pair_rows)
    if len(pairs):
        pb = SlotBootstrap(pairs["slot"].to_numpy(), B=B, seed=seed)
        rows = []
        for lab, g in pairs.groupby("label"):
            mask = (pairs["label"] == lab).to_numpy()
            est, lo, hi = pb.mean(pairs["pnl_pair_c"].to_numpy(), mask)
            rows.append({"label": lab, "n_paires": len(g), "part_deux_executes": g["both"].mean(),
                         "part_un_seul": (g["n_exec"] == 1).mean(), "part_aucun": (g["n_exec"] == 0).mean(),
                         "pnl_par_paire_c": est, "ic_bas": lo, "ic_haut": hi})
        out["pairs"] = pd.DataFrame(rows)
    else:
        out["pairs"] = pd.DataFrame()
    out["orders"] = o
    return out


# ---------------------------------------------------------------------------
# Graphiques
# ---------------------------------------------------------------------------
def plot_depth(med: pd.DataFrame, depth: pd.DataFrame, n_markets: int, path: Path) -> str:
    from tradebot.report import BG, GRID, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    m = med[med["cellule"] == "Tous"].set_index("t_rel_s").sort_index()
    d = depth[(depth["cellule"] == "Tous")].set_index(["instant_s", "mesure"])
    sp30, sp0 = d.loc[(-30, "spread"), "median"], d.loc[(0, "spread"), "median"]
    db60, db30, db0 = (d.loc[(t, "depth_best_bid"), "median"] for t in (-60, -30, 0))
    q49 = d.loc[(-30, "bid_up_049"), "median"]
    title = (f"Avant l'ouverture, le carnet reste serré (écart médian {fr(100 * sp30, 1)} c à S−30 s, {fr(100 * sp0, 1)} c à S) "
             f"mais mince : {fr(db30, 0)} parts au meilleur bid à S−30 s ({fr(db60, 0)} à S−60 s, {fr(db0, 0)} à S), "
             f"{fr(q49, 0)} parts devant un bid Up à 0,49")
    sub = (f"Médiane sur {n_markets} marchés BTC/ETH 5m et BTC 15m collectés le 26/09/2026 (WebSocket CLOB, carnet reconstruit "
           "dans le repère Up). Bande : quartiles. S = ouverture de la fenêtre ; le trait pointillé marque S−30 s (décision).")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 6.4
    fig, axes = plt.subplots(2, 1, figsize=(W, H), facecolor=BG, sharex=True, gridspec_kw={"hspace": 0.18})
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - hh / H, bottom=0.75 / H)
    _draw_header(fig, t, s)
    x = m.index.to_numpy()
    ax = axes[0]
    _style_axes(ax)
    ax.fill_between(x, 100 * m["spread_p25"], 100 * m["spread_p75"], color=C_BLUE, alpha=0.12, linewidth=0)
    ax.plot(x, 100 * m["spread_median"], color=C_BLUE, lw=2.0)
    ax.set_ylabel("écart bid–ask (cents)", color=TEXT_2, fontsize=9)
    ax.set_ylim(0, max(3.0, float((100 * m["spread_p75"]).max()) * 1.1))
    ax.text(x[-1], 100 * m["spread_median"].iloc[-1], "  écart médian", color=TEXT, fontsize=8.5, va="center")
    ax = axes[1]
    _style_axes(ax)
    lines = [("depth_best_bid_median", "meilleur bid (Up)", C_BLUE), ("depth_best_ask_median", "meilleur ask (Up)", C_ORANGE),
             ("bid_up_049_median", "bid Up à 0,49", C_AQUA), ("bid_dn_049_median", "bid Down à 0,49", C_VIOLET)]
    for col, lab, c in lines:
        ax.plot(x, m[col], color=c, lw=2.0, label=lab)
        ax.text(x[-1], m[col].iloc[-1], f"  {lab}", color=TEXT, fontsize=8.5, va="center")
    ax.set_ylabel("taille affichée (parts)", color=TEXT_2, fontsize=9)
    ax.set_xlabel("secondes par rapport à l'ouverture S", color=TEXT_2, fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, ncol=2, labelcolor=TEXT)
    ymax = float(np.nanmax(m[[c for c, _, _ in lines]].to_numpy())) * 1.15
    ax.set_ylim(0, ymax if np.isfinite(ymax) and ymax > 0 else 1)
    for a in axes:
        a.axvline(0, color=TEXT_2, lw=0.9)
        a.axvline(-30, color=TEXT_2, lw=0.9, ls=(0, (3, 3)))
        a.set_xlim(-150, 150)
    axes[0].text(1, axes[0].get_ylim()[1] * 0.97, "S", color=TEXT_2, fontsize=8.5, va="top")
    axes[0].text(-29, axes[0].get_ylim()[1] * 0.97, "S−30 s", color=TEXT_2, fontsize=8.5, va="top")
    _save(fig, path)
    return title


def plot_pnl(by_label: pd.DataFrame, path: Path, n_markets: int) -> str:
    from tradebot.report import BG, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    plt = _pyplot()
    b = by_label[by_label["n_orders"] > 0].copy()
    order = {"signal": 0, "two_sided": 1, "fair_value": 2}
    b["k"] = b["strategy"].map(order)
    b = b.sort_values(["k", "label"]).reset_index(drop=True)
    best = b.sort_values("pnl_per_placed_c", ascending=False).iloc[0]
    pos = b[np.isfinite(b.get("pnl_per_placed_ic_lo", np.nan)) & (b["pnl_per_placed_ic_lo"] > 0)]
    if len(pos):
        title = (f"{len(pos)} configuration(s) sur {len(b)} ont un P&L maker positif avec un IC qui exclut 0 "
                 f"(au mieux {STRAT_LABELS[best['strategy']]} {best['label']} : {cents(best['pnl_per_placed_c'])} par part placée) ; "
                 f"n = {n_markets} marchés, IC très larges")
    else:
        title = (f"Aucune configuration maker n'a un P&L significativement positif sur {n_markets} marchés "
                 f"(au mieux {cents(best['pnl_per_placed_c'])} par part placée, {ic(best.get('pnl_per_placed_ic_lo'), best.get('pnl_per_placed_ic_hi'))})")
    sub = ("P&L à la résolution par part placée (exécutée ou non), sans frais ni remise ; ordres qui croiseraient le carnet écartés. "
           "Barres : IC 95 % bootstrap groupé par créneau de 15 min. À droite : taux d'exécution et sélection adverse "
           "(taux de gain des ordres exécutés − non exécutés, points).")
    W = 11.0
    t, s, hh = _header(W, title, sub)
    H = hh + 0.36 * len(b) + 1.35
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.30, right=0.80, top=1 - hh / H, bottom=1.05 / H)
    _draw_header(fig, t, s)
    _style_axes(ax, ygrid=False, xgrid=True)
    y = np.arange(len(b))[::-1]
    for yv, (_, r) in zip(y, b.iterrows()):
        c = STRAT_COLORS[r["strategy"]]
        v = r["pnl_per_placed_c"]
        ax.barh(yv, v, height=0.55, color=c, zorder=2)
        lo, hi = r.get("pnl_per_placed_ic_lo", np.nan), r.get("pnl_per_placed_ic_hi", np.nan)
        if np.isfinite(lo) and np.isfinite(hi):
            ax.plot([lo, hi], [yv, yv], color=TEXT, lw=1.2, zorder=3)
        txt = f"exéc. {fr(r['fill_rate'], 0, pct=True)} · n = {int(r['n_orders'])}"
        if np.isfinite(r["adverse_selection_pts"]):
            txt += f" · sél. adv. {fr(r['adverse_selection_pts'], 0, signed=True)} pt"
        ax.text(1.01, yv, txt, transform=ax.get_yaxis_transform(), va="center", ha="left", fontsize=8, color=TEXT_2)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{STRAT_LABELS[r['strategy']].split(')')[0]}) {r['label']}" for _, r in b.iterrows()], fontsize=8.5, color=TEXT)
    ax.set_xlabel("P&L par part placée (cents)", color=TEXT_2, fontsize=9)
    vals = np.r_[b["pnl_per_placed_c"].to_numpy(), b.get("pnl_per_placed_ic_lo", pd.Series(dtype=float)).to_numpy(),
                 b.get("pnl_per_placed_ic_hi", pd.Series(dtype=float)).to_numpy()]
    vals = vals[np.isfinite(vals)]
    if len(vals):
        ax.set_xlim(min(vals.min() - 2, -5), max(vals.max() + 2, 5))
    from matplotlib.lines import Line2D

    fig.legend(handles=[Line2D([0], [0], color=c, lw=6, label=STRAT_LABELS[k]) for k, c in STRAT_COLORS.items()],
               loc="lower center", bbox_to_anchor=(0.5, 0.01), ncol=3, frameon=False, fontsize=8, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_fill_signal(by_label: pd.DataFrame, path: Path) -> str | None:
    from tradebot.report import BG, TEXT, TEXT_2, _draw_header, _header, _pyplot, _save, _style_axes

    b = by_label[(by_label["strategy"] == "signal") & (by_label["n_orders"] > 0)].copy()
    if not len(b):
        return None
    b["price"] = b["label"].str.split("/").str[0].astype(float)
    b["cancel"] = b["label"].str.split("S+").str[1].astype(int)
    plt = _pyplot()
    ex_by_price = b.groupby("price")["fill_rate"].mean()
    ex_by_cancel = b.groupby("cancel")["fill_rate"].mean()
    c0, cmax = ex_by_cancel.index.min(), ex_by_cancel.index.max()
    gain = 100 * (ex_by_cancel[cmax] - ex_by_cancel[c0])
    price_span = 100 * (ex_by_price.max() - ex_by_price.min())
    part_time = (f"attendre jusqu'à S+{cmax} s ajoute {fr(gain, 0)} points d'exécution" if gain >= 10
                 else f"attendre jusqu'à S+{cmax} s n'ajoute que {fr(gain, 0)} points")
    part_price = (f"le prix (0,48 à 0,50) change peu ({fr(price_span, 0)} points)" if price_span < 10
                  else f"le prix compte ({fr(ex_by_price.min(), 0, pct=True)} à {fr(ex_by_price.max(), 0, pct=True)} de 0,48 à 0,50)")
    title = (f"(a) Signal maker posé à S−30 s : exécuté dans {fr(ex_by_cancel[c0], 0, pct=True)} des cas avant l'annulation à S ; "
             f"{part_time} ; {part_price}")
    sub = "Taux d'exécution des ordres au repos posés à S−30 s du côté favorisé par le TWAP partiel, par prix et heure d'annulation."
    W = 8.5
    t, s, hh = _header(W, title, sub)
    H = hh + 3.2
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.10, right=0.98, top=1 - hh / H, bottom=0.7 / H)
    _draw_header(fig, t, s)
    _style_axes(ax)
    cancels = sorted(b["cancel"].unique())
    prices = sorted(b["price"].unique())
    width = 0.8 / max(1, len(cancels))
    colors = [C_BLUE, C_ORANGE, C_AQUA]
    for j, c in enumerate(cancels):
        sub_b = b[b["cancel"] == c].set_index("price")
        xs = np.arange(len(prices)) + (j - (len(cancels) - 1) / 2) * width
        vals = [100 * sub_b["fill_rate"].get(p, np.nan) for p in prices]
        ax.bar(xs, vals, width=width * 0.92, color=colors[j % 3], label=f"annulation à S+{c} s" if c else "annulation à S", zorder=2)
        for xv, v in zip(xs, vals):
            if np.isfinite(v):
                ax.text(xv, v + 1, fr(v, 0), ha="center", va="bottom", fontsize=8, color=TEXT_2)
    ax.set_xticks(np.arange(len(prices)))
    ax.set_xticklabels([f"{p:.2f}" for p in prices], color=TEXT)
    ax.set_xlabel("prix de l'ordre (côté favorisé)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("taux d'exécution (%)", color=TEXT_2, fontsize=9)
    ax.set_ylim(0, 110)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left", labelcolor=TEXT)
    _save(fig, path)
    return title


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
def write_readme(ctx: dict, out: Path) -> None:
    M, depth, queue, tstats, st, fvc = ctx["markets"], ctx["depth"], ctx["queue"], ctx["tstats"], ctx["strat"], ctx["fv_ctl"]
    U = M[M["usable"]]
    n = len(U)
    period = (f"{U['start'].min():%d/%m/%Y %H:%M}–{U['start'].max():%H:%M} UTC" if n else "—")
    by_s = st["by_strategy"].set_index("strategy") if len(st["by_strategy"]) else pd.DataFrame()
    by_l = st["by_label"]
    dT = depth[depth["cellule"] == "Tous"].set_index(["instant_s", "mesure"])
    qT = queue[queue["cellule"] == "Tous"].set_index(["phase", "niveau"])
    tT = tstats[(tstats["cellule"] == "Tous")].set_index("fenêtre") if len(tstats) else pd.DataFrame()

    def dmed(t, c):
        return dT.loc[(t, c), "median"] if (t, c) in dT.index else np.nan

    def qrow(phase, lvl):
        r = qT.loc[(phase, lvl)]
        return f"{fr(r['median'], 0)} (p25 {fr(r['p25'], 0)} ; p75 {fr(r['p75'], 0)})"

    best_lab = by_l.sort_values("pnl_per_placed_c", ascending=False).iloc[0] if len(by_l) else None
    n_pos = int(((by_l.get("pnl_per_placed_ic_lo", pd.Series(dtype=float)) > 0)).sum()) if len(by_l) else 0
    lines = [
        "# Maker sur le carnet réel — Polymarket « Up or Down » BTC/ETH 5m, BTC 15m",
        "",
        f"*Généré le {ctx['generated']} par `scripts/polymarket_maker_live.py` (temps : {fr(ctx['elapsed'], 0)} s). "
        f"Marchés collectés par `scripts/polymarket_live_collector.py` (WebSocket CLOB, une connexion par marché) : "
        f"{len(M)} enregistrés, **{n} exploitables** (fenêtre finie, carnet reconstruit dès S−60 s), du {period}.*",
        "",
        "> Simulation papier sur données publiques : aucun ordre, aucune clé. Les ordres simulés n'altèrent pas le carnet "
        "enregistré : chaque configuration est une simulation indépendante.",
        "",
        "## 0. Résumé",
        "",
        f"* **n est petit** ({n} marchés sur {fr(len(U['slot'].unique()) if n else 0, 0)} créneaux de 15 min ; "
        f"{fr(U['resolved_up'].dropna().mean() if n and U['resolved_up'].notna().any() else float('nan'), 0, pct=True)} résolus Up sur "
        f"{int(U['resolved_up'].notna().sum()) if n else 0} issues connues) : les IC bootstrap (groupés par créneau) sont larges, et rien "
        "ci-dessous n'est démontré. Un taux de Up éloigné de 50 % sur si peu de marchés fait gagner mécaniquement les achats d'Up et perdre les achats de Down : "
        "lire les tableaux « par côté » avec cette réserve. C'est un premier passage à relancer quand la collecte aura duré 24 h.",
    ]
    if n:
        lines += [
            f"* **Carnet autour de l'ouverture** (médianes) : écart {fr(100 * dmed(-60, 'spread'), 1)} c à S−60 s, {fr(100 * dmed(-30, 'spread'), 1)} c à S−30 s, "
            f"{fr(100 * dmed(0, 'spread'), 1)} c à S, {fr(100 * dmed(30, 'spread'), 1)} c à S+30 s ; taille au meilleur bid {fr(dmed(-60, 'depth_best_bid'), 0)} / "
            f"{fr(dmed(-30, 'depth_best_bid'), 0)} / {fr(dmed(0, 'depth_best_bid'), 0)} / {fr(dmed(30, 'depth_best_bid'), 0)} parts aux mêmes instants "
            f"(meilleur ask : {fr(dmed(-60, 'depth_best_ask'), 0)} / {fr(dmed(-30, 'depth_best_ask'), 0)} / {fr(dmed(0, 'depth_best_ask'), 0)} / {fr(dmed(30, 'depth_best_ask'), 0)}).",
            f"* **File d'attente typique** (paramètre `Q_ahead` pour le simulateur historique) : avant S, bid Up à 0,49 : {qrow('avant S (S−60 s, S−30 s)', 'bid_up_049')} parts, "
            f"à 0,50 : {qrow('avant S (S−60 s, S−30 s)', 'bid_up_050')} ; bid Down à 0,49 : {qrow('avant S (S−60 s, S−30 s)', 'bid_dn_049')}, à 0,50 : {qrow('avant S (S−60 s, S−30 s)', 'bid_dn_050')}. "
            f"Après S (S, S+30 s) : bid Up 0,49 : {qrow('après S (S, S+30 s)', 'bid_up_049')} ; 0,50 : {qrow('après S (S, S+30 s)', 'bid_up_050')} ; "
            f"bid Down 0,49 : {qrow('après S (S, S+30 s)', 'bid_dn_049')} ; 0,50 : {qrow('après S (S, S+30 s)', 'bid_dn_050')}. "
            "Un niveau à 0 part est un niveau vide (le milieu est ailleurs) : la médiane mélange donc « file » et « niveau absent » ; le § 3 sépare les deux.",
        ]
        if len(tT):
            lines.append(
                f"* **Trades preneurs** : {fr(tT.loc['toutes', 'n_trades'], 0)} trades ; {fr(tT.loc['toutes', 'part_traverse'], 1, pct=True)} traversent plus d'un niveau "
                f"(IC {fr(tT.loc['toutes', 'part_traverse_ic_bas'], 1, pct=True)} ; {fr(tT.loc['toutes', 'part_traverse_ic_haut'], 1, pct=True)}) ; "
                f"{fr(tT.loc['toutes', 'part_depasse_profondeur'], 1, pct=True)} dépassent la taille affichée au meilleur niveau ; taille médiane {fr(tT.loc['toutes', 'taille_mediane'], 0)} parts."
                + (f" Dans [S−60 s, S) : {fr(tT.loc['[S−60 s, S)', 'part_traverse'], 1, pct=True)} de traversées ; dans [S, S+60 s) : {fr(tT.loc['[S, S+60 s)', 'part_traverse'], 1, pct=True)}."
                   if "[S−60 s, S)" in tT.index and "[S, S+60 s)" in tT.index else ""))
        if len(by_s):
            for k in ("signal", "two_sided", "fair_value"):
                if k not in by_s.index:
                    continue
                r = by_s.loc[k]
                lines.append(
                    f"* **{STRAT_LABELS[k]}** : {int(r['n_orders'])} ordres posables sur {int(r['n_markets'])} marchés "
                    f"({int(r['n_crossing'])} écartés car ils croiseraient le carnet), exécutés {fr(r['fill_rate'], 0, pct=True)} "
                    f"(délai médian {fr(r['delay_median_s'], 1)} s) ; P&L {cents(r['pnl_per_placed_c'])} par part placée "
                    f"({ic(r.get('pnl_per_placed_ic_lo'), r.get('pnl_per_placed_ic_hi'))}), {cents(r['pnl_per_executed_c'])} par part exécutée "
                    f"({ic(r.get('pnl_per_executed_ic_lo'), r.get('pnl_per_executed_ic_hi'))}) ; remise maker estimée {cents(r['rebate_per_executed_c'])} ; "
                    f"sélection adverse : taux de gain {fr(r['win_rate_executed'], 0, pct=True)} si exécuté contre {fr(r['win_rate_not_executed'], 0, pct=True)} sinon "
                    f"({fr(r['adverse_selection_pts'], 0, signed=True)} points).")
        if len(fvc):
            f30 = fvc[(fvc["durée"] == "toutes") & (fvc["t_rel_s"] == 30)]
            flast = fvc[(fvc["durée"] == "toutes")].sort_values("t_rel_s").iloc[-1]
            if len(f30):
                r30 = f30.iloc[0]
                lines.append(
                    f"* **Juste valeur Φ(d/σ) contre le milieu du carnet** (§ 6) : à S+30 s, Brier {fr(r30['brier_juste_valeur'], 3)} contre "
                    f"{fr(r30['brier_marché'], 3)} pour le marché (ΔBrier {fr(r30['d_brier'], 3, signed=True)}, IC {fr(r30['d_brier_ic_bas'], 3, signed=True)} ; "
                    f"{fr(r30['d_brier_ic_haut'], 3, signed=True)}) ; à S+{int(flast['t_rel_s'])} s, {fr(flast['brier_juste_valeur'], 3)} contre {fr(flast['brier_marché'], 3)} "
                    f"(ΔBrier {fr(flast['d_brier'], 3, signed=True)}) : l'information Binance 1 s vaut quelque chose dans les premières secondes, puis le carnet "
                    "en sait plus que notre gaussienne.")
        if best_lab is not None:
            lines.append(
                f"* **Verdict provisoire** : {n_pos} configuration(s) sur {len(by_l)} ont un IC entièrement positif (tests multiples : {len(by_l)} configurations, "
                f"aucune correction). La meilleure, {STRAT_LABELS[best_lab['strategy']]} {best_lab['label']}, fait {cents(best_lab['pnl_per_placed_c'])} par part placée "
                f"({ic(best_lab.get('pnl_per_placed_ic_lo'), best_lab.get('pnl_per_placed_ic_hi'))}) ; choisie a posteriori, elle est optimiste. "
                + ("La règle fixée d'avance (choix sur la 1re moitié, test sur la 2e) est au § 5." if st.get("chosen_label") else ""))
    lines += [
        "",
        "## 1. Données : le collecteur et ce qu'il enregistre",
        "",
        "* `scripts/polymarket_live_collector.py` ouvre une connexion `wss://ws-subscriptions-clob.polymarket.com/ws/market` par marché "
        "(jetons Up et Down), 2,5 min avant l'ouverture, fermée 2 min après la clôture ou dès l'événement `market_resolved`. "
        "Messages : `book` (instantané complet, émis à chaque trade), `price_change` (nouvelle taille d'un niveau), `best_bid_ask`, "
        "`last_trade_price` (côté, prix, taille du preneur, horodatage serveur ms), `tick_size_change`, `market_resolved`.",
        "* **Débit** mesuré avec le format brut (v1, messages tels quels) le 26/09 à 04:45 UTC : 55,5 Mo en 13,4 min, soit ≈ 250 Mo/h pour "
        "BTC 5m + BTC 15m + ETH 5m (≈ 6 Go / 24 h ; un marché BTC 5m actif pèse 16 Mo), dont 80 % de `price_change` (identifiants de jetons de 77 caractères "
        f"répétés, niveaux lointains). Estimation en régime établi d'après la taille moyenne par marché fini : v1 ≈ {fr(ctx['v1_rate_mb_h'], 0)} Mo/h. "
        "Le collecteur a été remplacé à 04:51 UTC (recouvrement de 3 min, sans perte) par un **format compact v2** : jetons codés 0/1, "
        "`price_change` filtrés aux niveaux à ± 0,10 du milieu, vidage gzip toutes les 5 s, chien de garde (reconnexion si 45 s sans message), "
        "arrêt propre sur SIGINT/SIGTERM, segments `<slug>.s<k>.jsonl.gz` quand un fichier existe déjà, rafraîchissement de l'issue et de "
        f"`priceToBeat`/`finalPrice` pendant 6 h après la clôture. Débit v2 estimé de la même façon : ≈ {fr(ctx['v2_rate_mb_h'], 0)} Mo/h "
        f"(≈ {fr(ctx['v2_rate_mb_h'] * 24 / 1000, 1)} Go / 24 h ; un marché BTC 5m ≈ 1,3 Mo, ETH 5m ≈ 0,4 à 1,2 Mo, BTC 15m ≈ 2,5 Mo). Mémoire du processus : ≈ 140 Mo.",
        "* **Reconstruction** (`tradebot.polymarket_book`) : carnet dans le repère du jeton Up (les messages Up et Down sont exactement "
        "symétriques : bid Down à p = ask Up à 1 − p, vérifié sur 290 000 `price_change`, 0 écart) ; instantané puis deltas ; "
        f"contrôle avec `best_bid_ask` : accord médian {fr(U['bba_agree_rate'].median(), 1, pct=True)} par marché (le serveur émet `best_bid_ask` juste avant le "
        "`price_change` correspondant ; on accepte 200 ms de retard). Les fichiers tronqués (collecteur tué) sont lus jusqu'à la coupure.",
        f"* **Marchés** : {len(M)} enregistrés, {n} exploitables. Écartés : "
        + ("; ".join(f"{k} ({v})" for k, v in M.loc[~M['usable'], 'reason'].value_counts().items()) or "aucun") + ".",
        "* **Binance 1 s** (`data-api.binance.vision`, cache `data/cache/pm_maker_live/binance_1s/`) : signal `gap_m30` = log(spot(S−30) / "
        "moyenne des closes 1 s sur [S−60, S−30)) et juste valeur pendant la fenêtre.",
        "",
        "## 2. Le carnet autour de l'ouverture",
        "",
        f"![{ctx['titles'].get('depth', '')}](profondeur_autour_de_S.png)",
        "",
        "Profondeur par instant (médiane ; p25 ; p75 sur les marchés exploitables), tous marchés :",
        "",
    ]
    if n:
        tab = depth[(depth["cellule"] == "Tous") & depth["mesure"].isin(["spread", "depth_best_bid", "depth_best_ask", "depth_bid_10c", "depth_ask_10c"])].copy()
        tab["valeur"] = [f"{fr(100 * r.median, 1)} c ({fr(100 * r.p25, 1)} ; {fr(100 * r.p75, 1)})" if r.mesure == "spread"
                         else f"{fr(r.median, 0)} ({fr(r.p25, 0)} ; {fr(r.p75, 0)})" for r in tab.itertuples()]
        piv = tab.pivot(index="mesure", columns="instant_s", values="valeur").reindex(["spread", "depth_best_bid", "depth_best_ask", "depth_bid_10c", "depth_ask_10c"])
        piv.index = ["écart bid–ask", "taille au meilleur bid (Up)", "taille au meilleur ask (Up)", "cumul bids à ± 0,10 du milieu", "cumul asks à ± 0,10 du milieu"]
        piv.columns = [f"S{t:+d} s" if t else "S" for t in piv.columns]
        lines += [to_markdown(piv.reset_index().rename(columns={"index": "mesure"})), ""]
        lines += ["Par cellule, à S−30 s (médianes) :", ""]
        rows = []
        for name in depth["cellule"].unique():
            d = depth[(depth["cellule"] == name) & (depth["instant_s"] == -30)].set_index("mesure")
            rows.append({"cellule": name, "marchés": int(d.loc["spread", "n"]), "écart (c)": 100 * d.loc["spread", "median"],
                         "meilleur bid": d.loc["depth_best_bid", "median"], "meilleur ask": d.loc["depth_best_ask", "median"],
                         "bid Up 0,49": d.loc["bid_up_049", "median"], "bid Up 0,50": d.loc["bid_up_050", "median"],
                         "bid Down 0,49": d.loc["bid_dn_049", "median"], "bid Down 0,50": d.loc["bid_dn_050", "median"]})
        lines += [to_markdown(pd.DataFrame(rows), {"écart (c)": 1, "meilleur bid": 0, "meilleur ask": 0, "bid Up 0,49": 0, "bid Up 0,50": 0, "bid Down 0,49": 0, "bid Down 0,50": 0}), ""]
    lines += [
        "## 3. File d'attente aux niveaux 0,48–0,52 et traversées",
        "",
        "Taille affichée (parts) à chaque niveau, avant S (instants S−60 s et S−30 s) et après S (S et S+30 s) ; « niveau présent » = "
        "niveau non vide (le milieu est proche). La médiane sur les niveaux présents est la file `Q_ahead` qu'un ordre posé à ce prix aurait devant lui.",
        "",
    ]
    if n:
        rows = []
        S_all = ctx["series"]
        for phase, ts in (("avant S", (-60.0, -30.0)), ("après S", (0.0, 30.0))):
            s = S_all[S_all["t_rel_s"].isin(ts)]
            for lvl in LEVEL_COLS:
                x = s[lvl].dropna()
                nz = x[x > 0]
                rows.append({"phase": phase, "niveau": lvl.replace("bid_up_", "bid Up 0,").replace("bid_dn_", "bid Down 0,").replace("0,0", "0,"),
                             "part niveau présent": (x > 0).mean() if len(x) else np.nan, "médiane (tous)": x.median() if len(x) else np.nan,
                             "médiane (présents)": nz.median() if len(nz) else np.nan, "p25 (présents)": nz.quantile(0.25) if len(nz) else np.nan,
                             "p75 (présents)": nz.quantile(0.75) if len(nz) else np.nan})
        lines += [to_markdown(pd.DataFrame(rows), {"part niveau présent": "0%", "médiane (tous)": 0, "médiane (présents)": 0, "p25 (présents)": 0, "p75 (présents)": 0}), ""]
        if len(tstats):
            t = tstats[tstats["cellule"] == "Tous"][["fenêtre", "n_trades", "part_traverse", "part_traverse_ic_bas", "part_traverse_ic_haut", "part_depasse_profondeur", "taille_mediane", "taille_p90", "part_achats"]]
            lines += ["Trades preneurs (tous marchés exploitables) : « traverse » = le prix du trade dépasse strictement le meilleur niveau d'avant (au moins deux niveaux consommés) ; "
                      "« dépasse la profondeur » = taille > taille affichée au meilleur niveau. IC bootstrap par créneau.", "",
                      to_markdown(t.rename(columns={"n_trades": "trades", "part_traverse": "traverse > 1 niveau", "part_traverse_ic_bas": "IC bas", "part_traverse_ic_haut": "IC haut",
                                                    "part_depasse_profondeur": "dépasse la profondeur", "taille_mediane": "taille médiane", "taille_p90": "taille p90", "part_achats": "part d'achats"}),
                                  {"traverse > 1 niveau": "1%", "IC bas": "1%", "IC haut": "1%", "dépasse la profondeur": "1%", "taille médiane": 0, "taille p90": 0, "part d'achats": "0%"}), ""]
    lines += [
        "## 4. Le simulateur maker",
        "",
        "Un ordre au repos (achat de 10 parts de Up ou de Down à L) posé à t0 (+ 300 ms de latence) a devant lui la taille affichée à L à t0. "
        "La file ne dépasse jamais la taille affichée (les annulations devant nous la réduisent). Chaque trade preneur qui consomme ce niveau "
        "(`last_trade_price` du côté opposé, en tenant compte de la complémentarité : un achat preneur de Down à 1 − L consomme les bids Up à L) "
        "sert d'abord la file, puis nous ; un trade au-delà du niveau (traversée) nous exécute entièrement ; si un ordre adverse entre dans notre "
        "niveau (le carnet nous « croise »), il nous exécute. Un ordre qui croiserait le carnet dès la pose serait un ordre preneur : il est écarté "
        "(`crossing`) et compté à part. Annulation à t_cancel (exécution partielle conservée). Paiement à la résolution : 1{gagnant} − L par part, "
        "sans frais ; remise maker estimée à part (0,2 × frais preneur au même prix). Tests : `tests/test_polymarket_maker_live.py`.",
        "",
        "Stratégies : (a) `signal` : côté favorisé par `gap_m30` (> 0 : Up), prix 0,48 / 0,49 / 0,50, pose à S−30 s, annulation à S, S+30 s, S+60 s ; "
        "(b) `two_sided` : achat Up et achat Down à 0,49 posés à S−60 s, mêmes annulations ; (c) `fair_value` : de S+10 s à E−10 s, toutes les 10 s, "
        "juste valeur p̂ = Φ(d/σ) avec d = log(TWAP final estimé / TWAP60(S) Binance) et σ = EWMA 1 s (variance du TWAP restant), bid Up à p̂ − 1 c et bid Down "
        "à (1 − p̂) − 1 c, plafonnés à 1 c sous le meilleur prix opposé (jamais croisants), conservés tant que le niveau ne change pas.",
        "",
        "## 5. Résultats par stratégie",
        "",
        f"![{ctx['titles'].get('pnl', '')}](pnl_maker_par_strategie.png)",
        "",
    ]
    if len(by_l):
        t = by_l.copy()
        t["stratégie"] = t["strategy"].map(STRAT_LABELS)
        cols = {"stratégie": "stratégie", "label": "config.", "n_orders": "ordres", "n_markets": "marchés", "n_crossing": "croisants (écartés)",
                "fill_rate": "exécutés", "share_by_queue": "via la file", "share_by_traversal": "via traversée", "share_by_cross": "via croisement",
                "q_ahead0_median": "file médiane", "delay_median_s": "délai médian (s)", "pnl_per_placed_c": "P&L / part placée (c)",
                "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut", "pnl_per_executed_c": "P&L / part exécutée (c)",
                "rebate_per_executed_c": "remise (c)", "win_rate_executed": "gain si exécuté", "win_rate_not_executed": "gain sinon"}
        t = t[[c for c in cols if c in t.columns]].rename(columns=cols)
        lines += [to_markdown(t, {"exécutés": "0%", "via la file": "0%", "via traversée": "0%", "via croisement": "0%", "file médiane": 0, "délai médian (s)": 1,
                                  "P&L / part placée (c)": "+1", "IC bas": "+1", "IC haut": "+1", "P&L / part exécutée (c)": "+1", "remise (c)": 2,
                                  "gain si exécuté": "0%", "gain sinon": "0%"}), ""]
    if len(st.get("pairs", [])):
        lines += ["(b) par **paire** Up + Down, sur les marchés où les deux ordres sont posables (quand le milieu n'est pas à 0,50, l'un des deux croise le carnet et "
                  "le tableau précédent ne garde que l'autre) : P&L de la paire en cents par part (les deux exécutés = écart de 2 c capté, un seul = position directionnelle, "
                  "le plus souvent du côté perdant) :", "",
                  to_markdown(st["pairs"].rename(columns={"label": "config.", "n_paires": "paires", "part_deux_executes": "les deux exécutés", "part_un_seul": "un seul", "part_aucun": "aucun",
                                                          "pnl_par_paire_c": "P&L / paire (c)", "ic_bas": "IC bas", "ic_haut": "IC haut"}),
                              {"les deux exécutés": "0%", "un seul": "0%", "aucun": "0%", "P&L / paire (c)": "+1", "IC bas": "+1", "IC haut": "+1"}), ""]
    if len(st.get("by_side", [])):
        lines += ["Par côté acheté (a et b) :", "",
                  to_markdown(st["by_side"][["strategy", "side", "n_orders", "n_crossing", "fill_rate", "pnl_per_placed_c", "pnl_per_placed_ic_lo", "pnl_per_placed_ic_hi", "win_rate_executed", "win_rate_not_executed"]]
                              .rename(columns={"strategy": "stratégie", "side": "côté", "n_orders": "ordres", "n_crossing": "croisants", "fill_rate": "exécutés", "pnl_per_placed_c": "P&L / part placée (c)",
                                               "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut", "win_rate_executed": "gain si exécuté", "win_rate_not_executed": "gain sinon"}),
                              {"exécutés": "0%", "P&L / part placée (c)": "+1", "IC bas": "+1", "IC haut": "+1", "gain si exécuté": "0%", "gain sinon": "0%"}), ""]
    if ctx["titles"].get("fill"):
        lines += [f"![{ctx['titles']['fill']}](execution_signal.png)", ""]
    lines += ["### 1re moitié / 2e moitié", ""]
    if st.get("chosen_label"):
        ce = st["chosen_eval"]
        lines += [f"Règle fixée d'avance pour (a) : la configuration (prix, annulation) au meilleur P&L par part placée sur la 1re moitié des marchés (au moins 5 ordres) est **{st['chosen_label']}** ; "
                  "son résultat sur la 2e moitié :", "",
                  to_markdown(ce[["half", "n_orders", "n_markets", "fill_rate", "pnl_per_placed_c", "pnl_per_placed_ic_lo", "pnl_per_placed_ic_hi", "win_rate_executed", "win_rate_not_executed"]]
                              .rename(columns={"half": "moitié", "n_orders": "ordres", "n_markets": "marchés", "fill_rate": "exécutés", "pnl_per_placed_c": "P&L / part placée (c)",
                                               "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut", "win_rate_executed": "gain si exécuté", "win_rate_not_executed": "gain sinon"}),
                              {"exécutés": "0%", "P&L / part placée (c)": "+1", "IC bas": "+1", "IC haut": "+1", "gain si exécuté": "0%", "gain sinon": "0%"}), ""]
    else:
        lines += ["Trop peu de marchés pour choisir une configuration sur la 1re moitié (il faut au moins 5 ordres posables par configuration).", ""]
    if len(st.get("by_half", [])):
        h = st["by_half"]
        h = h[h["n_orders"] > 0]
        lines += ["Toutes configurations, par moitié (`fichier resume_par_moitie.csv`) : P&L par part placée (c) et taux d'exécution.", "",
                  to_markdown(h[["strategy", "label", "half", "n_orders", "fill_rate", "pnl_per_placed_c", "pnl_per_placed_ic_lo", "pnl_per_placed_ic_hi"]]
                              .rename(columns={"strategy": "stratégie", "label": "config.", "half": "moitié", "n_orders": "ordres", "fill_rate": "exécutés",
                                               "pnl_per_placed_c": "P&L / part placée (c)", "pnl_per_placed_ic_lo": "IC bas", "pnl_per_placed_ic_hi": "IC haut"}),
                              {"exécutés": "0%", "P&L / part placée (c)": "+1", "IC bas": "+1", "IC haut": "+1"}), ""]
    lines += ["## 6. La juste valeur Φ(d/σ) contre le prix du marché pendant la fenêtre", ""]
    if len(fvc):
        lines += ["Brier de la juste valeur (Binance 1 s, information close à t) et du milieu du carnet au même instant, contre l'issue officielle ; ΔBrier < 0 = la juste valeur fait mieux ; IC bootstrap par créneau.", "",
                  to_markdown(fvc.rename(columns={"durée": "durée", "brier_juste_valeur": "Brier juste valeur", "brier_marché": "Brier marché", "d_brier": "ΔBrier", "d_brier_ic_bas": "IC bas", "d_brier_ic_haut": "IC haut",
                                                  "justesse_juste_valeur": "justesse juste valeur", "justesse_marché": "justesse marché"}),
                              {"Brier juste valeur": 3, "Brier marché": 3, "ΔBrier": "+3", "IC bas": "+3", "IC haut": "+3", "justesse juste valeur": "0%", "justesse marché": "0%"}), ""]
    else:
        lines += ["Pas encore de marché résolu avec bougies Binance.", ""]
    lines += [
        "## 7. À quoi servirait l'amplitude prévue par TimesFM ?",
        "",
        "TimesFM ne prédit pas le sens (AUC ≈ 0,51), mais ses quantiles sont calibrés en amplitude (couverture [q10, q90] de 0,76 à 0,84 pour 0,80 visé). "
        "Dans ce cadre, l'amplitude a exactement une place : **le σ de la juste valeur** Φ(d/σ) de la stratégie (c), donc le **prix auquel un maker doit coter**. "
        "Quand l'écart d entre le TWAP estimé et le seuil est connu (il l'est mécaniquement dès S−30 s, et de plus en plus pendant la fenêtre), la probabilité de "
        "Up ne dépend plus que de la dispersion attendue du reste de la fenêtre : un σ trop petit fait coter 0,90 ce qui vaut 0,70, un σ trop grand laisse "
        "de l'argent sur la table. Ici σ vient d'une EWMA de rendements 1 s (60 s de demi-vie) ; TimesFM (contexte 1 min, horizon 5 ou 15 pas) fournirait un "
        "σ **conditionnel** à l'horizon exact de la fenêtre, utile surtout sur le 15m où l'EWMA 1 s extrapole mal, et pour dimensionner l'ordre (taille, "
        "distance au milieu) plutôt que pour choisir le côté. Le test à faire : remplacer σ_EWMA par (q90 − q10)/2,56 de TimesFM 2.5 (Apache-2.0) dans (c) et "
        "comparer le Brier du § 6 et le P&L du § 5 ; le § 6 donne déjà la référence à battre (le milieu du carnet). Rien de tout cela ne crée un avantage sur le "
        "sens : cela ne fait que cotiser correctement ce que l'on sait.",
        "",
        "## 8. Limites",
        "",
        f"* **n = {n} marchés** sur une seule matinée UTC (régime de volatilité unique) ; {len(by_l)} configurations testées sans correction pour tests multiples ; "
        "la meilleure configuration est choisie a posteriori. À relancer après 24 h de collecte.",
        "* File d'attente : borne supérieure (les annulations devant nous ne sont vues que si le niveau affiché passe sous notre file) ; exécution par "
        "croisement supposée totale (optimiste si l'ordre adverse est plus petit que nous) ; le prix d'un `last_trade_price` est le niveau touché "
        "(prix moyen du preneur dans 93 % des cas) : une traversée est comptée à partir de ce prix.",
        "* Nos ordres n'influencent pas les autres participants (pas de réaction des makers concurrents, pas de retrait du preneur). La latence est fixée à 300 ms.",
        "* Signal (a) : bougies 1 s Binance closes à S−30 s ; le flux Chainlink retarde d'≈ 4 s sur Binance (diagnostic), non modélisé ; juste valeur (c) : "
        "TWAP60(S) Binance à la place de `priceToBeat` (écart de niveau Binance–Chainlink ≈ 3 pb), gaussienne, pas de saut.",
        "* Issues : `resolved_up` lu sur gamma (rafraîchi par le collecteur pendant 6 h) ou sur l'événement `market_resolved` du WebSocket ; les marchés sans issue "
        "comptent pour l'exécution mais pas pour le P&L.",
        "",
        "## 9. Fichiers et relance",
        "",
        "| fichier | contenu |",
        "|---|---|",
        "| `marches.csv` | un marché par ligne : issue, `priceToBeat`, `gap_m30`, couverture, accord `best_bid_ask`, milieu / écart / profondeur à S−60, S−30, S, S+30 |",
        "| `carnet_median.csv` | séries médianes (et quartiles) du carnet par cellule, de S−150 s à S+120 s |",
        "| `profondeur.csv`, `file_attente.csv` | profondeur par instant et cellule ; file typique aux niveaux 0,48–0,52 avant / après S |",
        "| `trades_stats.csv`, `trades.csv` | traversées, dépassements de profondeur, tailles ; tous les trades preneurs avec l'état du carnet d'avant |",
        "| `ordres.csv`, `resume_strategies.csv`, `resume_par_moitie.csv`, `resume_par_cellule.csv`, `paires_two_sided.csv` | ordres simulés et résumés avec IC |",
        "| `juste_valeur_controle.csv` | Brier juste valeur contre milieu du carnet |",
        "",
        "Relancer (le collecteur peut tourner en même temps ; les marchés en cours sont ignorés) :",
        "",
        "```bash",
        ". .venv/bin/activate",
        "pgrep -af live_collector || nohup python scripts/polymarket_live_collector.py --series btc:5m,btc:15m,eth:5m >> logs/live_collector.log 2>&1 &",
        "python scripts/polymarket_maker_live.py               # --workers 2 --B 2000 par défaut ; --no-refresh pour ne pas interroger gamma",
        "```",
    ]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def disk_rates(live_dir: Path) -> tuple[float, float]:
    """(Mo/h format v1, Mo/h format v2) en régime établi : pour chaque série (actif, durée) et chaque
    format, taille moyenne par marché fini × marchés par heure, sommé sur les séries."""
    from tradebot.polymarket import DURATION_SECONDS

    rates = {}
    for fmt in (1, 2):
        per_series: dict[tuple[str, str], list[float]] = {}
        for meta_path in live_dir.glob("*.meta.json"):
            meta = json.loads(meta_path.read_text())
            if int(meta.get("format", 1)) != fmt or not meta.get("collector_finished"):
                continue
            files = bk.market_files(meta["slug"], live_dir)
            if fmt == 2:
                files = [f for f in files if f.name in (meta.get("segments") or [])]
            else:
                files = files[:1]
            if not files:
                continue
            per_series.setdefault((meta["asset"], meta["duration"]), []).append(sum(f.stat().st_size for f in files))
        total = 0.0
        for (asset, dur), sizes in per_series.items():
            total += float(np.mean(sizes)) / 1e6 * 3600.0 / DURATION_SECONDS.get(dur, 300)
        rates[fmt] = total if per_series else float("nan")
    return rates.get(1, float("nan")), rates.get(2, float("nan"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live-dir", default=str(bk.LIVE_DIR))
    ap.add_argument("--out", default=str(REPORTS_DIR / "polymarket" / "maker_live"))
    ap.add_argument("--size", type=float, default=mk.DEFAULT_SIZE)
    ap.add_argument("--latency-ms", type=int, default=mk.LATENCY_MS)
    ap.add_argument("--B", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--no-refresh", action="store_true", help="ne pas interroger gamma pour les issues manquantes")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t_start = time.time()
    live_dir, out = Path(args.live_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.no_refresh:
        from tradebot.polymarket import PolymarketClient

        try:
            n = bk.refresh_resolutions(PolymarketClient(use_cache=False), live_dir)
            log.info("issues rafraîchies : %d", n)
        except Exception as exc:  # noqa: BLE001
            log.warning("rafraîchissement impossible : %r", exc)
    slugs = bk.list_markets(live_dir)
    now = time.time()
    log.info("%d marchés enregistrés", len(slugs))
    results = []
    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_market, s, str(live_dir), args.size, args.latency_ms, now): s for s in slugs}
            for f in futs:
                try:
                    results.append(f.result())
                except Exception as exc:  # noqa: BLE001
                    log.error("%s : %r", futs[f], exc)
    else:
        for s in slugs:
            try:
                results.append(process_market(s, str(live_dir), args.size, args.latency_ms, now))
            except Exception as exc:  # noqa: BLE001
                log.error("%s : %r", s, exc)
    M = pd.DataFrame([r["info"] for r in results]).sort_values("start_ts").reset_index(drop=True)
    usable = [r for r in results if r["info"]["usable"]]
    series = pd.concat([r["series"] for r in usable], ignore_index=True) if usable else pd.DataFrame()
    trades = pd.concat([r["trades"] for r in usable], ignore_index=True) if usable else pd.DataFrame()
    orders = pd.concat([r["orders"] for r in usable if len(r["orders"])], ignore_index=True) if usable else pd.DataFrame()
    fvc_raw = pd.concat([r["fv_ctl"] for r in usable if len(r["fv_ctl"])], ignore_index=True) if usable else pd.DataFrame()
    log.info("%d marchés exploitables, %d ordres simulés, %d trades", len(usable), len(orders), len(trades))
    ctx = {"markets": M, "series": series, "generated": pd.Timestamp.now(tz="UTC").strftime("%d/%m/%Y %H:%M UTC"), "titles": {}}
    ctx["v1_rate_mb_h"], ctx["v2_rate_mb_h"] = disk_rates(live_dir)
    if len(series):
        depth, queue, med = depth_tables(series)
    else:
        depth, queue, med = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    tstats = trades_stats(trades, args.B, args.seed) if len(trades) else pd.DataFrame()
    fvc = fair_value_control(fvc_raw, args.B, args.seed) if len(fvc_raw) else pd.DataFrame()
    U = M[M["usable"]]
    mid_ts = int(U["start_ts"].median()) if len(U) else 0
    st = strategy_tables(orders, mid_ts, args.B, args.seed) if len(orders) else {"by_label": pd.DataFrame(), "by_strategy": pd.DataFrame(), "by_half": pd.DataFrame(), "by_cell": pd.DataFrame(), "by_side": pd.DataFrame(), "pairs": pd.DataFrame(), "chosen_label": None, "chosen_eval": pd.DataFrame(), "orders": orders}
    ctx.update(depth=depth, queue=queue, med=med, tstats=tstats, fv_ctl=fvc, strat=st)
    # CSV
    M.to_csv(out / "marches.csv", index=False, float_format="%.6g")
    if len(series):
        med.to_csv(out / "carnet_median.csv", index=False, float_format="%.6g")
        depth.to_csv(out / "profondeur.csv", index=False, float_format="%.6g")
        queue.to_csv(out / "file_attente.csv", index=False, float_format="%.6g")
    if len(trades):
        trades.to_csv(out / "trades.csv", index=False, float_format="%.6g")
        tstats.to_csv(out / "trades_stats.csv", index=False, float_format="%.6g")
    if len(orders):
        st["orders"].to_csv(out / "ordres.csv", index=False, float_format="%.6g")
        st["by_label"].to_csv(out / "resume_strategies.csv", index=False, float_format="%.6g")
        st["by_half"].to_csv(out / "resume_par_moitie.csv", index=False, float_format="%.6g")
        st["by_cell"].to_csv(out / "resume_par_cellule.csv", index=False, float_format="%.6g")
        if len(st["pairs"]):
            st["pairs"].to_csv(out / "paires_two_sided.csv", index=False, float_format="%.6g")
    if len(fvc):
        fvc.to_csv(out / "juste_valeur_controle.csv", index=False, float_format="%.6g")
    # figures
    if len(series):
        ctx["titles"]["depth"] = plot_depth(med, depth, len(U), out / "profondeur_autour_de_S.png")
    if len(orders) and len(st["by_label"]):
        ctx["titles"]["pnl"] = plot_pnl(st["by_label"], out / "pnl_maker_par_strategie.png", len(U))
        ctx["titles"]["fill"] = plot_fill_signal(st["by_label"], out / "execution_signal.png")
    ctx["elapsed"] = time.time() - t_start
    write_readme(ctx, out)
    log.info("écrit dans %s (%.0f s)", out, ctx["elapsed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
