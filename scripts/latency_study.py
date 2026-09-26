"""Temps de réaction nécessaire pour prendre les prix périmés des marchés Polymarket « Up or Down ».

Relançable à volonté (l'étude grossit avec les données des collecteurs) :

    . .venv/bin/activate
    python scripts/latency_study.py                  # tout ce qui est disponible -> reports/latence/
    python scripts/latency_study.py --since 2026-09-26T10:29 --until 2026-09-26T18:00 --jobs 2

Entrées (lecture seule) : ``data/cache/cex_ws/`` (Binance, Coinbase, RTDS Chainlink) et
``data/cache/polymarket/live/`` (carnets CLOB), même horloge locale. Sorties : ``reports/latence/``
(README.md, CSV, PNG). Voir :mod:`tradebot.latency` pour les définitions.

Simulation papier uniquement : Polymarket est bloqué en France (ANJ) et « close-only » pour la France.
"""

from __future__ import annotations

import argparse
import logging
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import latency as lt
from tradebot.config import REPORTS_DIR
from tradebot.polymarket_book import LIVE_DIR, list_markets, load_market, resolution_of

log = logging.getLogger("latency_study")

OUT_DIR = REPORTS_DIR / "latence"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
CHAINLINK = {"btc": "btc/usd", "eth": "eth/usd"}
TAKER_DELAY_MS = 150            # délai preneur Polymarket (marchés crypto, depuis le 04/09/2026)
GRID_S = 0.1
PRE_S = 90                      # analyse à partir de S − 90 s
SIGMA_HALFLIFE_S = 600.0         # demi-vie EWMA (meilleure log-loss historique, reports/polymarket/formule)
SIGMA_MIN_OBS = 600              # sans historique réseau : 10 min de chauffe
SIGMA_FACTOR = 1.40              # σ × 1,40 : calibration historique (rendements 1 s autocorrélés, même rapport)
SIGMA_WARMUP_H = 3.0             # historique Binance 1 s (klines) avant le début, pour initialiser l'EWMA
SOURCE_NOISE_PB = 0.40           # erreur de suivi Chainlink − Binance de F − K par défaut (même rapport)
TRIGGERS = ("tous", "saut", "persistante", "carnet")
TRIGGER_COLORS = {"tous": "#2a78d6", "saut": "#eb6834", "persistante": "#1baf7a", "carnet": "#eda100"}
TRIGGER_LABELS = {"tous": "toutes", "saut": "saut Binance (prix périmé)", "persistante": "désaccord persistant",
                  "carnet": "nouvel ask moins cher"}
JUMPS = (0.05, 0.10)
MARKOUT_S = 10.0
ASK_RANGE = (0.05, 0.95)         # asks retenus pour les opportunités (queues = surtout erreur de modèle)

C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#4a3aa7"
MARGIN_COLORS = {0.0: C_BLUE, 0.01: C_ORANGE, 0.02: C_AQUA}
MARGIN_LABELS = {0.0: "avantage > 0", 0.01: "avantage > 1 c", 0.02: "avantage > 2 c"}

# état partagé avec les processus de calcul (fork)
G: dict = {}


def fr(x, d: int = 1, signed: bool = False) -> str:
    from tradebot.report import fmt_number

    return fmt_number(x, d, signed=signed)


def utc(t: float) -> str:
    return pd.Timestamp(t, unit="s", tz="UTC").strftime("%d/%m %H:%M:%S")


# ---------------------------------------------------------------------------
# Réseau (facultatif) : décalage d'horloge, aller-retour vers le CLOB, issues
# ---------------------------------------------------------------------------
def clock_offset_binance(n: int = 15) -> dict:
    """Décalage horloge serveur Binance − horloge locale (ms), requête d'aller-retour minimal."""
    import requests

    s = requests.Session()
    best = None
    for _ in range(n):
        t0 = time.time_ns()
        r = s.get("https://data-api.binance.vision/api/v3/time", timeout=5)
        t1 = time.time_ns()
        st = r.json()["serverTime"]
        rtt = (t1 - t0) / 1e6
        off = st - (t0 + t1) / 2e6
        if best is None or rtt < best[0]:
            best = (rtt, off)
    return {"rtt_ms": best[0], "offset_ms": best[1], "uncertainty_ms": best[0] / 2.0}


def rtt_clob(n: int = 10) -> float:
    """Aller-retour HTTP minimal vers ``clob.polymarket.com/time`` depuis ce conteneur (ms)."""
    import requests

    s = requests.Session()
    out = []
    for _ in range(n):
        t0 = time.time_ns()
        s.get("https://clob.polymarket.com/time", timeout=5)
        out.append((time.time_ns() - t0) / 1e6)
    return float(min(out))


def fetch_klines_1s(symbol: str, start_s: float, end_s: float) -> np.ndarray:
    """Closes 1 s Binance (``data-api.binance.vision``) sur [start_s, end_s) : le close de la bougie ouverte
    à u − 1 est le prix à u. Sert seulement à initialiser σ (données antérieures au début de l'étude)."""
    import requests

    s = requests.Session()
    out = []
    t = int(start_s) * 1000
    end = int(end_s) * 1000
    while t < end:
        r = s.get("https://data-api.binance.vision/api/v3/klines",
                  params={"symbol": symbol, "interval": "1s", "startTime": t, "endTime": end - 1, "limit": 1000},
                  timeout=10)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out.extend(float(k[4]) for k in rows)
        t = int(rows[-1][0]) + 1000
    return np.asarray(out, dtype="float64")


def fetch_outcome(slug: str) -> bool | None:
    """Issue officielle via gamma (lecture seule, sans toucher aux meta.json du collecteur)."""
    from tradebot.polymarket import GAMMA_URL, PolymarketClient, parse_market

    try:
        client = PolymarketClient(max_rps=4.0, timeout=10.0, retries=1)
        ev = client._get(f"{GAMMA_URL}/events", [("slug", slug), ("limit", 1)])
        ev = (ev or [{}])[0]
        if not ev:
            return None
        return parse_market((ev.get("markets") or [{}])[0], ev).resolved_up
    except Exception as exc:  # noqa: BLE001
        log.warning("%s : gamma injoignable (%r)", slug, exc)
        return None


# ---------------------------------------------------------------------------
# 1. Séries et latences de transport
# ---------------------------------------------------------------------------
def load_cex(since: float | None, until: float | None) -> dict:
    t = time.time()
    bn = lt.load_binance(lt.cex_files("binance", since, until))
    cb = lt.load_coinbase(lt.cex_files("coinbase", since, until))
    rt = lt.load_rtds(lt.cex_files("rtds", since, until))

    def clip(df: pd.DataFrame, col: str = "rx") -> pd.DataFrame:
        m = np.ones(len(df), bool)
        if since is not None:
            m &= df[col].to_numpy() >= since
        if until is not None:
            m &= df[col].to_numpy() <= until
        return df.loc[m].reset_index(drop=True)

    bn = {s: {k: clip(v) for k, v in d.items()} for s, d in bn.items()}
    cb = {s: clip(v) for s, v in cb.items()}
    rt = {s: clip(v) for s, v in rt.items()}
    log.info("flux CEX lus en %.1f s : %s", time.time() - t,
             {s: len(d["book"]) for s, d in bn.items()})
    return {"binance": bn, "coinbase": cb, "rtds": rt}


def transport_table(cex: dict, pm_delays: dict[str, np.ndarray], clock: dict | None) -> pd.DataFrame:
    rows = []

    def add(source: str, what: str, d_ms: np.ndarray, note: str = "") -> None:
        r = lt.summarize_latency(d_ms)
        rows.append({"source": source, "mesure": what, **r, "note": note})

    for sym, d in cex["binance"].items():
        tr = d["trades"]
        if len(tr):
            add(f"Binance {sym} aggTrade", "rx − E", (tr["rx"] - tr["E"]).to_numpy() * 1e3,
                "réception locale − heure d'événement Binance")
            add(f"Binance {sym} aggTrade", "E − T", (tr["E"] - tr["T"]).to_numpy() * 1e3, "événement − trade (serveur)")
    for prod, df in cex["coinbase"].items():
        add(f"Coinbase {prod} ticker", "rx − time", (df["rx"] - df["time"]).to_numpy() * 1e3,
            "réception locale − heure du trade Coinbase")
    for sym, df in cex["rtds"].items():
        u = df[~df["snapshot"]]
        if not len(u):
            continue
        name = f"Chainlink {sym} (RTDS)" if "/" in sym else f"RTDS crypto_prices {sym}"
        add(name, "rx − timestamp", (u["rx"] - u["ts"]).to_numpy() * 1e3, "réception locale − horodatage de l'observation")
        add(name, "msg_ts − timestamp", (u["msg_ts"] - u["ts"]).to_numpy() * 1e3, "publication RTDS − observation (serveur)")
        add(name, "rx − msg_ts", (u["rx"] - u["msg_ts"]).to_numpy() * 1e3, "réception locale − publication RTDS")
    for key, d in pm_delays.items():
        add(f"Polymarket CLOB {key}", "rx − ts", d, "réception locale − horodatage du message CLOB")
    df = pd.DataFrame(rows)
    if clock:
        df["offset_horloge_ms"] = clock["offset_ms"]
        df["corrige_median_ms"] = np.where(df["mesure"].str.startswith("rx"), df["median"] + clock["offset_ms"], np.nan)
    return df


# ---------------------------------------------------------------------------
# Retard et décalage Chainlink / Binance
# ---------------------------------------------------------------------------
def chainlink_study(cex: dict, prices: dict[str, lt.PriceSeries], trade_prices: dict[str, lt.PriceSeries]) -> dict:
    out = {"scan": [], "summary": []}
    lags = np.round(np.arange(-3.0, 10.0001, 0.1), 3)
    for asset, sym in CHAINLINK.items():
        cl = cex["rtds"].get(sym)
        if cl is None or len(cl) < 120 or asset not in prices:
            continue
        ts = cl["ts"].to_numpy()
        x = np.log(cl["value"].to_numpy())
        for clock_name, ser in (("rx", prices[asset]), ("serveur", trade_prices.get(asset))):
            if ser is None:
                continue
            for h in (1.0, 5.0):
                sc = lt.lag_scan(ts, x, ser, lags, horizon_s=h)
                sc["asset"], sc["horloge"], sc["horizon_s"] = asset, clock_name, h
                out["scan"].append(sc)
        s1 = pd.concat([d for d in out["scan"] if d["asset"].iloc[0] == asset], ignore_index=True)
        for (clk, h), g in s1.groupby(["horloge", "horizon_s"]):
            g = g.dropna(subset=["corr"])
            if g.empty:
                continue
            best = g.loc[g["corr"].idxmax()]
            lag_s = best["lag_ms"] / 1e3
            ser = prices[asset] if clk == "rx" else trade_prices[asset]
            basis = (x - ser.at(ts - lag_s)) * 1e4
            basis = basis[np.isfinite(basis)]
            out["summary"].append({"asset": asset, "horloge": clk, "horizon_s": h, "lag_ms": int(best["lag_ms"]),
                                   "corr_max": best["corr"], "corr_lag0": float(g.loc[g["lag_ms"] == 0, "corr"].iloc[0])
                                   if (g["lag_ms"] == 0).any() else math.nan, "n": int(best["n"]),
                                   "basis_median_pb": float(np.median(basis)) if basis.size else math.nan,
                                   "basis_p10_pb": float(np.percentile(basis, 10)) if basis.size else math.nan,
                                   "basis_p90_pb": float(np.percentile(basis, 90)) if basis.size else math.nan,
                                   "basis_sd_pb": float(np.std(basis)) if basis.size else math.nan})
    out["scan"] = pd.concat(out["scan"], ignore_index=True) if out["scan"] else pd.DataFrame()
    out["summary"] = pd.DataFrame(out["summary"])
    return out


def tracking_error(markets: list[dict], cex: dict, prices: dict[str, lt.PriceSeries], lag_s: float) -> pd.DataFrame:
    """Erreur de suivi de F − K : (F − K) Chainlink − (F − K) Binance décalé de ``lag`` (log, pb)."""
    rows = []
    for mk in markets:
        cl = cex["rtds"].get(CHAINLINK[mk["asset"]])
        if cl is None or mk["asset"] not in prices:
            continue
        S, E = mk["S"], mk["E"]
        ser = cl.set_index("ts")["value"]
        A = np.arange(S - 59, S + 1, dtype=float)
        B = np.arange(E - 59, E + 1, dtype=float)
        if not (np.isin(A, ser.index).all() and np.isin(B, ser.index).all()):
            continue
        k_cl, f_cl = np.log(ser.loc[A].to_numpy()).mean(), np.log(ser.loc[B].to_numpy()).mean()
        pb = prices[mk["asset"]]
        k_bn, f_bn = np.mean(pb.at(A - lag_s)), np.mean(pb.at(B - lag_s))
        rows.append({"slug": mk["slug"], "fk_chainlink_pb": (f_cl - k_cl) * 1e4, "fk_binance_pb": (f_bn - k_bn) * 1e4,
                     "diff_pb": ((f_cl - k_cl) - (f_bn - k_bn)) * 1e4, "k_chainlink": float(np.exp(np.mean(np.log(ser.loc[A])))),
                     "k_chainlink_arith": float(ser.loc[A].mean())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Par marché
# ---------------------------------------------------------------------------
def analyse_market(slug: str) -> dict:
    t_start = time.time()
    m = load_market(slug)
    S, E = m.start_ts, m.end_ts
    asset = m.asset
    px: lt.PriceSeries = G["prices"][asset]
    sig: lt.SigmaSeries = G["sigma"][asset]
    lag = G["lag_s"]
    extra_sd = G["extra_sd"]
    outcome = G["outcomes"].get(slug)
    t_lo, t_hi = S - PRE_S, float(E)
    tl = lt.book_timeline(m, S - 150, E + 30)
    res = {"slug": slug, "asset": asset, "duration": m.duration, "S": S, "E": E, "outcome_up": outcome}
    if tl.rx.size < 10:
        res["skip"] = "carnet vide"
        return res
    eng = lt.TwapFormula(S, E, lag_s=lag).set_realized(px)

    def prob_at(t):
        t = np.asarray(t, dtype="float64")
        return eng.prob(t, px.at(t), sig.at(t), extra_sd)

    grid = np.round(np.arange(t_lo, t_hi + 1e-9, GRID_S), 3)
    Pg = prob_at(grid)
    phase_g = eng.phase(grid)
    # chronologie fusionnée : grille 100 ms + messages du carnet + mises à jour Binance (détection au tick près)
    rxs = tl.rx[(tl.rx >= t_lo) & (tl.rx <= t_hi)]
    bn_t = px.t[(px.t >= t_lo - 2) & (px.t <= t_hi)]
    T = np.union1d(np.union1d(grid, rxs), bn_t[bn_t >= t_lo])
    P = prob_at(T)
    sp = lt.side_prices(tl, T)
    # 2. réaction du carnet
    reac = []
    for jump in JUMPS:
        r = lt.reaction_events(grid, Pg, tl, prob_at, bn_t, jump=jump, phase=phase_g)
        r["jump"] = jump
        reac.append(r)
    reac = pd.concat(reac, ignore_index=True)
    # 3-4. opportunités et exécutions
    opps_l = []
    for margin in lt.MARGINS:
        o = lt.find_opportunities(T, P, sp["ask_up"], sp["size_up"], sp["ask_dn"], sp["size_dn"], margin=margin,
                                  ask_range=ASK_RANGE)
        if o.empty:
            continue
        o = lt.removal_cause(o, tl.trades)
        o["phase"] = eng.phase(o["t_start"].to_numpy())
        o["margin"] = margin
        opps_l.append(o)
    opps = pd.concat(opps_l, ignore_index=True) if opps_l else pd.DataFrame()
    fills_l = []
    if len(opps):
        # taille cumulée des niveaux avec avantage > marge au moment de la détection
        lad = lt.ladders_at(m, opps["t_start"].to_numpy())
        opps["size_edge"] = [sum(sz for p, sz in (L["up"] if s == "up" else L["down"])
                                 if ps - float(lt.taker_cost(p)) > mg + 1e-9)
                             for L, s, ps, mg in zip(lad, opps["side"], opps["p_side"], opps["margin"])]
        for margin, o in opps.groupby("margin", sort=True):
            f = lt.simulate_fills(o.reset_index(drop=True), tl, lt.LATENCIES_MS, margin=margin, outcome_up=outcome,
                                  markout_s=MARKOUT_S, end_s=float(E))
            f["trigger"] = o["trigger"].to_numpy()[f["opp_id"].to_numpy()]
            f["margin"] = margin
            fills_l.append(f)
    fills = pd.concat(fills_l, ignore_index=True) if fills_l else pd.DataFrame()
    # contrôles
    fk_bn = float(eng.mean(E - lag + 1.0, np.nan)) if np.isfinite(eng.xA).all() and np.isfinite(eng.xB).all() else math.nan
    pm_d = (tl.rx - tl.ts) * 1e3
    in_win = (tl.rx >= S - 60) & (tl.rx <= E)
    res.update({
        "n_book_events": int(tl.rx.size), "first_book_rx": float(tl.rx[0]), "last_book_rx": float(tl.rx[-1]),
        "bn_max_gap_s": px.max_gap(S - 60 - lag, E - lag), "sigma_at_S_bp_per_s": float(sig.at(S)[()] * 1e4),
        "fk_binance_pb": fk_bn * 1e4, "binance_says_up": bool(fk_bn >= 0) if np.isfinite(fk_bn) else None,
        "spread_median_c": float(np.nanmedian((tl.ask - tl.bid)[in_win]) * 100) if in_win.any() else math.nan,
        "n_trades": int(len(tl.trades)), "pm_rx_ts_median_ms": float(np.median(pm_d)),
        "n_react_5": int((reac["jump"] == 0.05).sum()), "n_react_10": int((reac["jump"] == 0.10).sum()),
        "runtime_s": time.time() - t_start,
    })
    for margin in lt.MARGINS:
        res[f"n_opp_{int(round(margin * 100))}c"] = int((opps["margin"] == margin).sum()) if len(opps) else 0
    for df in (reac, opps, fills):
        if len(df):
            df.insert(0, "market", slug)
            df["asset"] = asset
            df["duration"] = m.duration
    res.update({"reaction": reac, "opps": opps, "fills": fills, "pm_delays": pm_d[in_win]})
    if G.get("series"):
        res["series"] = aligned_series(slug, asset, S, E, tl, px, sig, prob_at)
    return res


def aligned_series(slug: str, asset: str, S: int, E: int, tl: lt.BookTimeline, px: lt.PriceSeries,
                   sig: lt.SigmaSeries, prob_at) -> pd.DataFrame:
    """Séries alignées sur l'horloge locale, une ligne par seconde de [S − 90 s, E] : dernière valeur reçue
    ≤ t de chaque source (Binance milieu et dernier trade, Coinbase, Chainlink + son horodatage), carnet
    Polymarket (repère Up), P_formule et σ."""
    t = np.arange(S - PRE_S, E + 1, 1.0)
    a = G["aux"].get(asset, {})
    out = pd.DataFrame({"market": slug, "t": t, "t_rel_s": t - S, "binance_mid": np.exp(px.at(t))})
    if "bn_last" in a:
        out["binance_last"] = lt.asof_values(a["bn_last"][0], a["bn_last"][1], t)
    if "cb" in a:
        out["coinbase_mid"] = lt.asof_values(a["cb"][0], a["cb"][1], t)
    if "cl" in a:
        out["chainlink"] = lt.asof_values(a["cl"][0], a["cl"][1], t)
        out["chainlink_ts"] = lt.asof_values(a["cl"][0], a["cl"][2], t)
    out["up_bid"], out["up_ask"] = lt.asof_values(tl.rx, tl.bid, t), lt.asof_values(tl.rx, tl.ask, t)
    out["up_bid_size"], out["up_ask_size"] = lt.asof_values(tl.rx, tl.bid_sz, t), lt.asof_values(tl.rx, tl.ask_sz, t)
    out["up_mid"] = (out["up_bid"] + out["up_ask"]) / 2.0
    out["p_formule"] = prob_at(t)
    out["sigma_bp_per_s"] = sig.at(t) * 1e4
    return out


# ---------------------------------------------------------------------------
# Agrégats
# ---------------------------------------------------------------------------
def pnl_tables(fills: pd.DataFrame, hours: float, n_boot: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """P&L selon ℓ (par marge, origine de l'opportunité, issue officielle et valorisation +10 s) avec IC
    bootstrap par marché, et latence critique ℓ*."""
    rows, crit = [], []
    for (margin, trig), g in [((m, "tous"), g) for m, g in fills.groupby("margin")] + \
            [(k, g) for k, g in fills.groupby(["margin", "trigger"])]:
        for value in ("pnl", "markout"):
            gg = g if value == "markout" else g[g["pnl"].notna()]
            if gg.empty:
                continue
            h = hours if value == "markout" else G["hours_resolved"]
            curve = lt.pnl_curve(gg, value=value)
            bs_opp = lt.bootstrap_by_group(gg, value=value, n_boot=n_boot)
            bs_h = lt.bootstrap_by_group(gg, value=value, n_boot=n_boot, denom=h)
            nanpair = (np.full(len(curve), np.nan), np.full(len(curve), np.nan))
            lo_o, hi_o = np.nanpercentile(bs_opp["reps"], [2.5, 97.5], axis=0) if len(bs_opp["reps"]) else nanpair
            lo_h, hi_h = np.nanpercentile(bs_h["reps"], [2.5, 97.5], axis=0) if len(bs_h["reps"]) else nanpair
            curve["margin"], curve["trigger"], curve["valeur"] = margin, trig, value
            curve["per_opp_lo"], curve["per_opp_hi"] = lo_o, hi_o
            curve["per_hour"] = curve[f"{value}_total"] / h
            curve["per_hour_lo"], curve["per_hour_hi"] = lo_h, hi_h
            curve["hours"] = h
            curve["mean_edge_fill_c"] = [100 * gg.loc[(gg["latency_ms"] == lat) & gg["filled"], "edge_fill"].mean()
                                         for lat in curve["latency_ms"]]
            # avantage attendu capturé selon la formule (sans bruit d'issue) : Σ parts × (P(t) − coût)
            exp_edge = (gg["qty"] * gg["edge_fill"].fillna(0.0)).groupby(gg["latency_ms"]).sum()
            curve["edge_per_hour"] = exp_edge.reindex(curve["latency_ms"]).to_numpy() / h
            curve = curve.rename(columns={f"{value}_total": "total", f"{value}_per_opp": "per_opp",
                                          f"{value}_per_share": "per_share"})
            rows.append(curve)
            lstar = lt.breakeven_latency(bs_opp["latencies"], bs_opp["point"], monotone=True)
            lstar_raw = lt.breakeven_latency(bs_opp["latencies"], bs_opp["point"])
            l_sig = lt.last_significant_latency(curve["latency_ms"].to_numpy(), lo_o)
            reps = np.array([lt.breakeven_latency(bs_opp["latencies"], r, monotone=True) for r in bs_opp["reps"]])
            reps = reps[~np.isnan(reps)]
            q = (np.quantile(reps, [0.025, 0.5, 0.975], method="inverted_cdf") if reps.size else [math.nan] * 3)
            e = curve["edge_per_hour"].to_numpy()
            l_half = lt.breakeven_latency(curve["latency_ms"].to_numpy(), e - e[0] / 2.0) if e.size and e[0] > 0 else math.nan
            crit.append({"margin": margin, "trigger": trig, "valeur": value, "l_star_ms": lstar, "l_star_lo_ms": q[0],
                         "l_star_med_boot_ms": q[1], "l_star_hi_ms": q[2],
                         "p_positive_at_0": float(np.mean(bs_opp["reps"][:, 0] > 0)) if len(bs_opp["reps"]) else math.nan,
                         "n_opp": int((gg["latency_ms"] == gg["latency_ms"].min()).sum()),
                         "n_markets": int(gg["market"].nunique()), "l_half_edge_ms": l_half,
                         "l_star_raw_ms": lstar_raw, "l_sig_ms": l_sig})
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(crit)


def wide_fills(opps: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par (marché, marge, opportunité) : P&L, quantité et prix pour chaque ℓ."""
    if opps.empty:
        return pd.DataFrame()
    o = opps.copy()
    o["opp_id"] = o.groupby(["market", "margin"]).cumcount()
    f = fills.copy()
    piv = f.pivot_table(index=["market", "margin", "opp_id"], columns="latency_ms", values=["pnl", "qty", "fill_ask"],
                        aggfunc="first", dropna=False)
    piv.columns = [f"{a}_{b}ms" for a, b in piv.columns]
    o = o.merge(piv.reset_index(), on=["market", "margin", "opp_id"], how="left")
    o["t_start_utc"] = pd.to_datetime(o["t_start"], unit="s", utc=True)
    return o


# ---------------------------------------------------------------------------
# Résumés
# ---------------------------------------------------------------------------
def reaction_summary(reac: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if reac.empty:
        return pd.DataFrame()
    groups = list(reac.groupby(["jump", "asset"])) + [((j, "tous"), reac[reac["jump"] == j]) for j in JUMPS]
    for (jump, asset), g in groups:
        if g.empty:
            continue
        row = {"jump": jump, "asset": asset, "n": len(g), "n_markets": g["market"].nunique()}
        for col in ("delay_first_ms", "delay_placebo_ms", "delay_50_ms", "delay_90_ms"):
            # quantiles « censure comprise » : pas de réaction dans la fenêtre = +∞
            d = g[col].to_numpy(dtype=float)
            dd = np.where(np.isfinite(d), d, np.inf)
            for q, name in ((25, "p25"), (50, "median"), (75, "p75"), (90, "p90")):
                row[f"{col}_{name}"] = float(np.percentile(dd, q, method="inverted_cdf")) if dd.size else math.nan
            row[f"{col}_censored"] = float(np.mean(~np.isfinite(d)))
            fin = d[np.isfinite(d)]
            row[f"{col}_neg"] = float(np.mean(fin < 0)) if fin.size else math.nan
        rows.append(row)
    return pd.DataFrame(rows)


def opportunity_summary(opps: pd.DataFrame) -> pd.DataFrame:
    if opps.empty:
        return pd.DataFrame()
    rows = []
    o = opps.copy()
    parts = []
    for trig in ("tous",) + tuple(sorted(o["trigger"].unique())):
        sub = o if trig == "tous" else o[o["trigger"] == trig]
        for asset in ("tous",) + tuple(sorted(sub["asset"].unique())):
            sub2 = sub if asset == "tous" else sub[sub["asset"] == asset]
            for margin, g in sub2.groupby("margin"):
                parts.append((margin, trig, asset, g))
    for margin, trig, asset, g in parts:
        fin = g[~g["censored"]]
        life = fin["life_book_ms"].to_numpy(dtype=float)
        n_all = len(g)

        def surv(x: float) -> float:
            return float((life > x).sum() + g["censored"].sum()) / n_all if n_all else math.nan

        rows.append({
            "margin": margin, "trigger": trig, "asset": asset, "n": n_all, "n_markets": g["market"].nunique(),
            "life_median_ms": float(np.median(life)) if life.size else math.nan,
            "life_p25_ms": float(np.percentile(life, 25)) if life.size else math.nan,
            "life_p75_ms": float(np.percentile(life, 75)) if life.size else math.nan,
            "life_p90_ms": float(np.percentile(life, 90)) if life.size else math.nan,
            "surv_50ms": surv(50), "surv_150ms": surv(150), "surv_300ms": surv(300), "surv_1s": surv(1000),
            "edge_life_median_ms": float(np.median(fin["life_edge_ms"])) if len(fin) else math.nan,
            "edge0_median_c": float(100 * g["edge0"].median()), "edge0_mean_c": float(100 * g["edge0"].mean()),
            "size0_median": float(g["size0"].median()), "size_edge_median": float(g["size_edge"].median()),
            "share_removed_by_taker": float(np.mean(fin["removed_by"] == "preneur")) if len(fin) else math.nan,
            "censored": int(g["censored"].sum()),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Graphiques
# ---------------------------------------------------------------------------
def _fig(W: float, title: str, sub: str, body_h: float, ncols: int = 1, sharey: bool = True):
    from tradebot.report import BG, _draw_header, _header, _pyplot

    plt = _pyplot()
    t, s, hh = _header(W, title, sub)
    H = hh + body_h
    fig, axes = plt.subplots(1, ncols, figsize=(W, H), facecolor=BG, sharey=sharey, squeeze=False)
    fig.subplots_adjust(left=0.085, right=0.98, top=1 - (hh + 0.15) / H, bottom=0.8 / H, wspace=0.08)
    _draw_header(fig, t, s)
    return plt, fig, axes[0]


def _pct_axis(ax) -> None:
    ax.yaxis.set_major_formatter(lambda v, _: f"{int(round(v * 100))} %")


def fms(v: float, d: int = 0) -> str:
    if v is None:
        return "—"
    if v == math.inf:
        return "> 5 000 ms"
    return f"{fr(v, d)} ms" if np.isfinite(v) else "—"


def plot_reaction(reac: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    r5 = reac[reac["jump"] == 0.05]
    def cmed(col: str) -> float:
        d = r5[col].to_numpy(dtype=float)
        return float(np.percentile(np.where(np.isfinite(d), d, np.inf), 50, method="inverted_cdf")) if d.size else math.nan

    med50, medf, medw, med90 = cmed("delay_50_ms"), cmed("delay_first_ms"), cmed("delay_placebo_ms"), cmed("delay_90_ms")
    title = (f"Vu d'ici, le carnet Polymarket a fait la moitié du chemin ≈ {fms(med50)} après un saut de Binance "
             f"(médiane) et 90 % après ≈ {fms(med90) if np.isfinite(med90) else 'plus de 30 s'} ; il retouche son meilleur "
             f"prix dans le bon sens en {fms(medf)}, "
             f"contre {fms(medw) if np.isfinite(medw) else '> 3 s'} sans saut (placebo)")
    sub = (f"Sauts de P_formule (calculée avec Binance) d'au moins 5 points (gauche, n = {len(r5)}) et 10 points (droite, "
           f"n = {int((reac['jump'] == 0.10).sum())}) en 1 s au plus. Part des sauts suivis par le carnet x ms après "
           "l'instant où la formule a fait la moitié du saut (horloge locale ; négatif : le carnet avait déjà bougé). "
           "Gris pointillé : placebo, même retouche mesurée 3 s avant le saut. Trait vertical : délai preneur de 150 ms.")
    plt, fig, axes = _fig(11.0, title, sub, 4.4, ncols=2)
    series = [("delay_first_ms", "1re retouche ask/bid (bon sens)", C_AQUA, "-"),
              ("delay_placebo_ms", "placebo (3 s avant le saut)", "#9a9994", (0, (3, 2))),
              ("delay_50_ms", "milieu : 50 % du saut", C_BLUE, "-"), ("delay_90_ms", "milieu : 90 % du saut", C_ORANGE, "-")]
    for ax, jump in zip(axes, JUMPS):
        _style_axes(ax, ygrid=True, xgrid=True)
        g = reac[reac["jump"] == jump]
        n = len(g)
        for col, lab, c, ls in series:
            d = np.sort(g[col].to_numpy(dtype=float))
            d = d[np.isfinite(d)]
            if n == 0 or d.size == 0:
                continue
            x = np.concatenate([[-1000.0], np.clip(d, -1000, 5000), [5000.0]])
            y = np.concatenate([[0.0], np.arange(1, d.size + 1) / n, [d.size / n]])
            ax.step(x, y, where="post", color=c, lw=2.0 if ls == "-" else 1.4, ls=ls, label=lab)
        ax.axvline(TAKER_DELAY_MS, color=TEXT_2, lw=0.9)
        ax.axvline(0, color=TEXT_2, lw=0.6, ls=(0, (2, 3)))
        ax.set_xlim(-1000, 5000)
        ax.set_ylim(0, 1.0)
        ax.set_title(f"sauts ≥ {int(round(jump * 100))} points (n = {n})", color=TEXT, fontsize=10, loc="left")
        ax.set_xlabel("délai après le saut de la formule (ms, vu d'ici)", color=TEXT_2, fontsize=9)
        _pct_axis(ax)
    axes[0].set_ylabel("part des sauts déjà suivis", color=TEXT_2, fontsize=9)
    axes[0].legend(loc="lower right", frameon=False, fontsize=8, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_lifetime(opps: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    o0 = opps[opps["margin"] == 0.0]
    xs = np.logspace(0, 5, 400)

    def surv_curve(g: pd.DataFrame) -> np.ndarray:
        life = g.loc[~g["censored"], "life_book_ms"].to_numpy(dtype=float)
        cens = int(g["censored"].sum())
        return np.array([((life > x).sum() + cens) / len(g) for x in xs]) if len(g) else np.full(xs.size, np.nan)

    js = o0[o0["trigger"] == "saut"]
    js_life = js.loc[~js["censored"], "life_book_ms"]
    med = float(js_life.median()) if len(js_life) else math.nan
    s150 = float(((js_life > TAKER_DELAY_MS).sum() + js["censored"].sum()) / len(js)) if len(js) else math.nan
    title = (f"Un prix périmé après un saut de Binance reste {fms(med)} dans le carnet (médiane, vu d'ici) : "
             f"{fr(100 * s150, 0)} % tiennent plus que les 150 ms du délai preneur")
    sub = ("Opportunités preneur (P_formule − ask − frais > 0), une par niveau de prix, selon leur origine. Part des "
           "niveaux encore affichés x ms après leur détection, jusqu'à ce que le meilleur ask du côté remonte au-dessus "
           "du niveau (annulé ou consommé). Échelle x logarithmique ; trait vertical : 150 ms.")
    plt, fig, axes = _fig(11.0, title, sub, 4.6)
    ax = axes[0]
    _style_axes(ax, ygrid=True, xgrid=True)
    for trig in TRIGGERS:
        g = o0 if trig == "tous" else o0[o0["trigger"] == trig]
        if g.empty:
            continue
        ax.plot(xs, surv_curve(g), color=TRIGGER_COLORS[trig], lw=2.4 if trig in ("saut", "tous") else 1.5,
                label=f"{TRIGGER_LABELS[trig]} (n = {len(g)})")
    ax.set_xscale("log")
    ax.axvline(TAKER_DELAY_MS, color=TEXT_2, lw=0.9)
    ax.text(TAKER_DELAY_MS * 1.06, 0.98, "150 ms", color=TEXT_2, fontsize=8.5, va="top")
    ax.set_ylim(0, 1.0)
    ax.set_xlim(1, 1e5)
    ax.set_xticks([1, 10, 100, 1000, 10000, 100000])
    ax.set_xticklabels(["1 ms", "10 ms", "100 ms", "1 s", "10 s", "100 s"])
    _pct_axis(ax)
    ax.set_ylabel("part des prix encore disponibles", color=TEXT_2, fontsize=9)
    ax.set_xlabel("temps depuis la détection (vu d'ici)", color=TEXT_2, fontsize=9)
    ax.legend(loc="lower left", frameon=False, fontsize=8.5, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_pnl(curve: pd.DataFrame, crit: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    lats = list(lt.LATENCIES_MS)
    xpos = {lat: i for i, lat in enumerate(lats)}

    def cget(trig: str, value: str, margin: float = 0.0):
        c = crit[(crit["margin"] == margin) & (crit["valeur"] == value) & (crit["trigger"] == trig)]
        return c.iloc[0] if len(c) else None

    cm = cget("saut", "markout")
    ls, lo, hi = (cm["l_star_ms"], cm["l_star_lo_ms"], cm["l_star_hi_ms"]) if cm is not None else (math.nan,) * 3
    lsig = cm["l_sig_ms"] if cm is not None else math.nan
    lhalf = cm["l_half_edge_ms"] if cm is not None else math.nan
    if np.isfinite(ls) and ls <= 0:
        title = ("Prendre les prix périmés après un saut de Binance ne rapporte rien, même à latence nulle "
                 f"(valorisation à +{int(MARKOUT_S)} s)")
    else:
        title = ("Les prix périmés après un saut de Binance ne rapportent de façon démontrée que si l'ordre touche le "
                 f"carnet en moins de {fms(lsig) if np.isfinite(lsig) else '—'} (vu d'ici) ; la moitié de l'avantage "
                 f"attendu est perdue à ≈ {fms(lhalf)}, le point mort est encore incertain ({fms(lo)} – {fms(hi)})")
    sub = ("Opportunités « saut Binance » (P_formule a monté d'au moins 1 point dans la seconde et l'avantage vient "
           "d'apparaître). P&L par heure (pUSD ; au plus 50 parts par opportunité, frais inclus) selon la latence ℓ "
           f"et la marge exigée. Gauche : valorisé au milieu du carnet {int(MARKOUT_S)} s après l'achat ; droite : à "
           "l'issue officielle (marchés résolus, plus bruité). Bandes : IC 95 % bootstrap par marché (marge 0). Tirets "
           "gris : avantage attendu selon la formule au prix payé (sans bruit d'issue).")
    plt, fig, axes = _fig(11.5, title, sub, 4.8, ncols=2, sharey=True)
    for ax, value, name in zip(axes, ("markout", "pnl"), (f"valorisé à +{int(MARKOUT_S)} s", "à l'issue officielle")):
        _style_axes(ax)
        g = curve[(curve["valeur"] == value) & (curve["trigger"] == "saut")]
        for margin in lt.MARGINS:
            gm = g[g["margin"] == margin].sort_values("latency_ms")
            if gm.empty:
                continue
            x = [xpos[lat] for lat in gm["latency_ms"]]
            c = MARGIN_COLORS[margin]
            if margin == 0.0:
                ax.fill_between(x, gm["per_hour_lo"], gm["per_hour_hi"], color=c, alpha=0.12, lw=0)
            ax.plot(x, gm["per_hour"], color=c, lw=2.2 if margin == 0 else 1.5, marker="o", ms=3,
                    label=f"{MARGIN_LABELS[margin]} (n = {int(gm['n_opp'].iloc[0])})")
        if value == "markout":
            ge = g[g["margin"] == 0.0].sort_values("latency_ms")
            ax.plot([xpos[lat] for lat in ge["latency_ms"]], ge["edge_per_hour"], color=TEXT_2, lw=1.3,
                    ls=(0, (4, 3)), label="avantage attendu selon la formule (marge 0)")
        ax.axhline(0, color=TEXT_2, lw=0.9)
        ax.axvline(xpos[150], color=TEXT_2, lw=0.8, ls=(0, (3, 3)))
        ax.set_xticks(range(len(lats)))
        ax.set_xticklabels([str(lat) for lat in lats], fontsize=8)
        ax.set_xlabel("latence ℓ (ms, vue d'ici)", color=TEXT_2, fontsize=9)
        ax.set_title(name, color=TEXT, fontsize=10, loc="left")
        ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=TEXT)
    axes[0].set_ylabel("P&L par heure (pUSD)", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


def plot_fill(curve: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    lats = list(lt.LATENCIES_MS)
    xpos = {lat: i for i, lat in enumerate(lats)}
    g = curve[(curve["valeur"] == "markout") & (curve["margin"] == 0.0)]
    js = g[g["trigger"] == "saut"].set_index("latency_ms")
    f150 = float(js.loc[150, "fill_rate"]) if 150 in js.index else math.nan
    f300 = float(js.loc[300, "fill_rate"]) if 300 in js.index else math.nan
    ps = g[g["trigger"] == "persistante"].set_index("latency_ms")
    p300 = float(ps.loc[300, "fill_rate"]) if 300 in ps.index else math.nan
    title = (f"Les prix périmés après un saut de Binance s'évaporent : on n'en touche plus que {fr(100 * f150, 0)} % à "
             f"150 ms et {fr(100 * f300, 0)} % à 300 ms, contre {fr(100 * p300, 0)} % des désaccords persistants")
    sub = ("Taux d'exécution selon la latence ℓ : part des opportunités (marge 0) où le meilleur ask du côté, dans le carnet "
           "vu à t + ℓ, est encore rentable au regard de P_formule(t) (sinon l'ordre limite n'est pas exécuté).")
    plt, fig, axes = _fig(10.5, title, sub, 4.3)
    ax = axes[0]
    _style_axes(ax)
    for trig in TRIGGERS:
        gm = g[g["trigger"] == trig].sort_values("latency_ms")
        if gm.empty:
            continue
        x = [xpos[lat] for lat in gm["latency_ms"]]
        ax.plot(x, gm["fill_rate"], color=TRIGGER_COLORS[trig], lw=2.4 if trig in ("saut", "tous") else 1.5, marker="o",
                ms=3, label=f"{TRIGGER_LABELS[trig]} (n = {int(gm['n_opp'].iloc[0])})")
    ax.axvline(xpos[150], color=TEXT_2, lw=0.8, ls=(0, (3, 3)))
    ax.set_ylim(0, 1.02)
    _pct_axis(ax)
    ax.set_xticks(range(len(lats)))
    ax.set_xticklabels([str(lat) for lat in lats], fontsize=8)
    ax.set_xlabel("latence ℓ (ms, vue d'ici)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("opportunités encore exécutables", color=TEXT_2, fontsize=9)
    ax.legend(loc="lower left", frameon=False, fontsize=8.5, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_chainlink(scan: pd.DataFrame, summary: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    s = summary[(summary["asset"] == "btc") & (summary["horizon_s"] == 1.0)]
    lag_sv = s.loc[s["horloge"] == "serveur", "lag_ms"]
    lag_rx = s.loc[s["horloge"] == "rx", "lag_ms"]
    basis = s.loc[s["horloge"] == "rx", "basis_median_pb"]
    title = (f"Chainlink, le prix qui sert à la résolution, retarde de ≈ {fr(lag_sv.iloc[0] / 1e3 if len(lag_sv) else math.nan, 1)} s "
             f"sur Binance ({fr(lag_rx.iloc[0] / 1e3 if len(lag_rx) else math.nan, 1)} s vu d'ici) et cote "
             f"{fr(abs(basis.iloc[0]) if len(basis) else math.nan, 1)} pb {'sous' if len(basis) and basis.iloc[0] < 0 else 'au-dessus de'} BTCUSDT")
    sub = ("Corrélation entre les rendements 1 s du flux Chainlink BTC/USD (RTDS, horodatage de l'observation) et ceux de "
           "Binance BTCUSDT décalés de −lag. Bleu : Binance à l'heure de réception locale (bookTicker) ; orange : à "
           "l'heure serveur Binance (aggTrade, E). Le pic donne le retard.")
    plt, fig, axes = _fig(10.5, title, sub, 4.2)
    ax = axes[0]
    _style_axes(ax, ygrid=True, xgrid=True)
    for clk, c, lab in (("rx", C_BLUE, "Binance, heure de réception locale"),
                        ("serveur", C_ORANGE, "Binance, heure serveur (E)")):
        g = scan[(scan["asset"] == "btc") & (scan["horloge"] == clk) & (scan["horizon_s"] == 1.0)].sort_values("lag_ms")
        if g.empty or g["corr"].isna().all():
            continue
        ax.plot(g["lag_ms"] / 1e3, g["corr"], color=c, lw=2.0, label=lab)
        b = g.loc[g["corr"].idxmax()]
        ax.plot([b["lag_ms"] / 1e3], [b["corr"]], "o", color=c, ms=5)
        ax.text(b["lag_ms"] / 1e3 + 0.15, b["corr"] + (0.02 if clk == "rx" else -0.05), f"{fr(b['lag_ms'] / 1e3, 1)} s",
                color=TEXT, fontsize=8.5)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    ax.set_xlabel("retard supposé de Chainlink sur Binance (s)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("corrélation des rendements 1 s", color=TEXT_2, fontsize=9)
    lo = float(np.nanmin(scan["corr"])) if len(scan) else 0.0
    ax.set_ylim(min(0.0, lo), 1.0)
    ax.legend(loc="upper right", frameon=False, fontsize=8.5, labelcolor=TEXT)
    _save(fig, path)
    return title


def plot_transport(tr: pd.DataFrame, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    keep = tr[tr["mesure"].isin(["rx − E", "rx − time", "rx − timestamp", "rx − ts"])].copy()
    keep = keep[~keep["source"].str.contains("crypto_prices")].sort_values("median", ascending=False)
    cl = keep.loc[keep["source"].str.startswith("Chainlink"), "median"]
    title = ("Vu d'ici, Binance et le carnet Polymarket arrivent avec des retards voisins (quelques dizaines de ms "
             f"d'écart), Chainlink ≈ {fr(cl.iloc[0] / 1e3 if len(cl) else math.nan, 1)} s après son horodatage")
    sub = ("Réception locale − horodatage serveur (ms) : barre = médiane, trait = p10–p90. Horloge locale non corrigée : "
           "son décalage (voir README) s'ajoute à toutes les barres ; les écarts entre barres n'en dépendent pas.")
    plt, fig, axes = _fig(10.5, title, sub, 0.42 * len(keep) + 1.1)
    fig.subplots_adjust(left=0.27)
    ax = axes[0]
    _style_axes(ax, ygrid=False, xgrid=True)
    y = np.arange(len(keep))
    ax.barh(y, keep["median"], color=C_BLUE, height=0.55)
    ax.hlines(y, keep["p10"], keep["p90"], color=TEXT, lw=1.2)
    for yi, v, hi in zip(y, keep["median"], keep["p90"]):
        ax.text(max(hi, v, 0) + 20, yi, f"{fr(v, 0)} ms", color=TEXT, fontsize=8.5, va="center")
    ax.set_yticks(y)
    ax.set_yticklabels(keep["source"], fontsize=8.5)
    ax.axvline(0, color=TEXT_2, lw=0.8)
    ax.set_xlim(min(-50, float(keep["p10"].min()) - 20), float(keep["p90"].max()) * 1.15 + 50)
    ax.set_xlabel("réception locale − horodatage serveur (ms)", color=TEXT_2, fontsize=9)
    _save(fig, path)
    return title


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
def md(df: pd.DataFrame, fmts=None) -> str:
    from tradebot.report import to_markdown

    return to_markdown(df, fmts)


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    ts = pd.Timestamp(s)
    return float((ts.tz_localize("UTC") if ts.tzinfo is None else ts).timestamp())


def render_readme(meta, tr, cls, mk_df, reac_sum, opp_sum, curve, crit, figs) -> str:  # noqa: C901
    from tradebot.report import fmt_number as fn

    ok = mk_df[mk_df["skip"].isna()] if "skip" in mk_df else mk_df
    n_mk = len(ok)
    n_res = int(ok["official_up"].notna().sum())
    cells = ok.groupby(["asset", "duration"]).size()
    cells_txt = ", ".join(f"{a.upper()} {d} : {n}" for (a, d), n in cells.items())
    period = f"{utc(meta['bn_start'])} – {utc(meta['bn_end'])} UTC"
    clock = meta["clock"] or {}

    def cget(trig="tous", value="markout", margin=0.0):
        g = crit[(crit["valeur"] == value) & (crit["margin"] == margin) & (crit["trigger"] == trig)]
        return g.iloc[0] if len(g) else None

    def cval(trig, value, lat, col="per_hour", margin=0.0):
        g = curve[(curve["valeur"] == value) & (curve["margin"] == margin) & (curve["latency_ms"] == lat)
                  & (curve["trigger"] == trig)]
        return g.iloc[0] if len(g) else None

    def ci(row, col="per_hour", d=0):
        if row is None:
            return "—"
        return f"{fn(row[col], d, signed=True)} (IC {fn(row[col + '_lo'], d, signed=True)} ; {fn(row[col + '_hi'], d, signed=True)})"

    js_mk, js_is = cget("saut", "markout"), cget("saut", "pnl")
    r5 = reac_sum[(reac_sum["jump"] == 0.05) & (reac_sum["asset"] == "tous")] if len(reac_sum) else reac_sum
    o_all = opp_sum[(opp_sum["margin"] == 0.0) & (opp_sum["asset"] == "tous")] if len(opp_sum) else opp_sum
    o_js = o_all[o_all["trigger"] == "saut"] if len(o_all) else o_all
    bn_rx = tr[(tr["source"].str.contains("BTCUSDT")) & (tr["mesure"] == "rx − E")]["median"]
    pm_rx = tr[(tr["source"].str.startswith("Polymarket")) & (tr["mesure"] == "rx − ts")]
    pm_med = float(np.average(pm_rx["median"], weights=pm_rx["n"])) if len(pm_rx) else math.nan
    d_bn = float(bn_rx.iloc[0]) if len(bn_rx) else math.nan
    rel = d_bn - pm_med                                  # d_Binance − d_Polymarket (horloge annulée)
    cl_sum = cls["summary"]
    cl_sv = cl_sum[(cl_sum["asset"] == "btc") & (cl_sum["horloge"] == "serveur") & (cl_sum["horizon_s"] == 1.0)] \
        if len(cl_sum) else cl_sum
    cl_rx = cl_sum[(cl_sum["asset"] == "btc") & (cl_sum["horloge"] == "rx") & (cl_sum["horizon_s"] == 1.0)] \
        if len(cl_sum) else cl_sum
    cl_lat = tr[tr["source"].str.startswith("Chainlink") & (tr["mesure"] == "rx − timestamp")]
    lstar = float(js_mk["l_star_ms"]) if js_mk is not None else math.nan
    lsig = float(js_mk["l_sig_ms"]) if js_mk is not None else math.nan
    lhalf = float(js_mk["l_half_edge_ms"]) if js_mk is not None else math.nan
    t_sig = lsig + rel if np.isfinite(lsig) else math.nan
    t_half = lhalf + rel if np.isfinite(lhalf) else math.nan
    rtt = meta["rtt_clob_ms"]
    l_here = rtt + TAKER_DELAY_MS if np.isfinite(rtt) else math.nan
    js_fill = {lat: cval("saut", "markout", lat) for lat in lt.LATENCIES_MS}

    L = ["# Temps de réaction nécessaire pour prendre les prix périmés (Polymarket « Up or Down »)", ""]
    L.append(f"*Généré le {pd.Timestamp(meta['genere']).strftime('%d/%m/%Y %H:%M')} UTC par `scripts/latency_study.py` "
             f"({fn(meta['runtime_s'], 0)} s). Données : {period} (chevauchement des deux collecteurs), {n_mk} marchés "
             f"({cells_txt}), dont {n_res} résolus ; {fn(meta['hours'], 2)} h de marché.*")
    L.append("")
    L.append("> **Cadre légal.** Recherche et simulation papier sur données publiques. Polymarket est bloqué en France "
             "(ANJ, 16/07/2026) et la France est en « close-only » sur le site et l'API ; les CGU interdisent de contourner "
             "le géoblocage. Rien ici n'est utilisable légalement pour trader depuis la France, et ce document ne décrit "
             "**aucun** moyen de contourner un blocage (VPN, serveur à l'étranger piloté depuis la France, prête-nom…).")
    L.append("")
    L.append("## Résumé")
    L.append("")
    bl = []
    if js_mk is not None:
        sig_txt = (f"le gain n'est **démontré** (borne basse de l'IC 95 % > 0) que jusqu'à ℓ = {fms(lsig)}"
                   if np.isfinite(lsig) else "le gain n'est démontré à **aucune** latence")
        bl.append(f"**Temps de réaction nécessaire (vu d'ici, détection du saut Binance → ordre au carnet)** : la moitié de "
                  f"l'avantage attendu des prix périmés est perdue à **ℓ½ ≈ {fms(lhalf)}** ; {sig_txt} ; le point mort "
                  f"(P&L = 0, courbe lissée) est à ℓ* ≈ {fms(lstar)}, IC 95 % {fms(js_mk['l_star_lo_ms'])} – "
                  f"{fms(js_mk['l_star_hi_ms'])} : pas encore identifié sur cet échantillon ({int(js_mk['n_opp'])} "
                  f"opportunités « saut Binance », valorisation à +{int(MARKOUT_S)} s)."
                  + (f" À l'issue officielle (plus bruitée, {int(js_is['n_opp'])} opportunités des marchés résolus) : "
                     f"ℓ* ≈ {fms(js_is['l_star_ms'])} (IC {fms(js_is['l_star_lo_ms'])} – {fms(js_is['l_star_hi_ms'])}), "
                     + (f"gain démontré jusqu'à {fms(js_is['l_sig_ms'])}." if np.isfinite(js_is["l_sig_ms"])
                        else "gain démontré à aucune latence.") if js_is is not None else ""))
        if np.isfinite(t_half):
            if np.isfinite(t_sig) and t_sig < TAKER_DELAY_MS:
                verdict = ("le seul gain démontré exige d'être apparié avant la fin du délai preneur : **impossible en "
                           "preneur**")
            elif not np.isfinite(t_sig):
                verdict = "aucun gain n'est démontré, quelle que soit la vitesse"
            else:
                verdict = "le gain démontré reste atteignable avec une infrastructure dédiée"
            floor_txt = ("plus court que" if t_half < 260 else "comparable à" if t_half < 300 else "plus long que")
            bl.append(f"**En temps réel** (mouvement Binance → appariement, = ℓ {'+' if rel >= 0 else '−'} {fn(abs(rel), 0)} ms "
                      f"d'écart de transport Binance/Polymarket vu d'ici) : moitié de l'avantage à ≈ {fms(t_half)}, gain "
                      f"démontré jusqu'à ≈ {fms(t_sig) if np.isfinite(t_sig) else '—'}. Tout ordre preneur attend "
                      f"**150 ms** avant appariement, et un preneur qui lit Binance (Tokyo) et envoie à Londres ne peut pas "
                      f"descendre sous ≈ 260–280 ms au total : ℓ½ est {floor_txt} ce plancher, et {verdict}.")
    def fd(v):
        return "plus de 30 s" if v == math.inf else fms(v)

    if len(r5):
        r = r5.iloc[0]
        bl.append(f"**Le carnet suit Binance en quelques centaines de ms** : après un saut de P_formule ≥ 5 points "
                  f"(n = {int(r['n'])}), le meilleur prix est retouché dans le bon sens en {fd(r['delay_first_ms_median'])} "
                  f"(placebo sans saut : {'plus de 3 s' if r['delay_placebo_ms_median'] == math.inf else fms(r['delay_placebo_ms_median'])}), "
                  f"le milieu fait 50 % du chemin en {fd(r['delay_50_ms_median'])} (médiane ; quartiles "
                  f"{fd(r['delay_50_ms_p25'])} – {fd(r['delay_50_ms_p75'])}) et 90 % en {fd(r['delay_90_ms_median'])} ; "
                  f"{fn(100 * r['delay_50_ms_censored'], 0)} % des sauts ne sont pas suivis dans les 30 s.")
    if len(o_js):
        o = o_js.iloc[0]
        bl.append(f"**Durée de vie d'un prix périmé** (saut Binance, {int(o['n'])} niveaux) : médiane {fms(o['life_median_ms'])} "
                  f"(p75 {fms(o['life_p75_ms'])}) ; {fn(100 * o['surv_150ms'], 0)} % sont encore là à 150 ms, "
                  f"{fn(100 * o['surv_300ms'], 0)} % à 300 ms ; avantage médian {fn(o['edge0_median_c'], 1)} c/part pour "
                  f"{fn(o['size0_median'], 0)} parts au meilleur ask ; {fn(100 * o['share_removed_by_taker'], 0)} % sont "
                  "retirés par un preneur plus rapide, le reste annulé par le teneur.")
    parts = []
    for lat in (50, 100, 200, 500):
        a = js_fill[lat]
        if a is not None:
            parts.append(f"{lat} ms : {ci(a)} pUSD/h (exécution {fn(100 * a['fill_rate'], 0)} %)")
    if parts:
        bl.append("**Gain attendu sur les prix périmés (saut Binance, marge 0, 50 parts max., valorisé à +10 s)** — "
                  + " ; ".join(parts) + ".")
    parts = []
    for lat in (50, 100, 200, 500):
        a, b = cval("tous", "markout", lat), cval("tous", "pnl", lat)
        if a is not None:
            parts.append(f"{lat} ms : {fn(a['per_hour'], 0, signed=True)} à +10 s, "
                         f"{fn(b['per_hour'], 0, signed=True) if b is not None else '—'} à l'issue")
    if parts:
        a0, a5 = cval("tous", "markout", 0), cval("tous", "markout", 5000)
        bl.append(f"**Toutes opportunités confondues** ({int(a0['n_opp']) if a0 is not None else '—'} ; surtout des désaccords "
                  "persistants entre la formule et le marché) : pUSD/h " + " ; ".join(parts) + ". Exécution "
                  f"{fn(100 * a0['fill_rate'], 0) if a0 is not None else '—'} % à 0 ms et "
                  f"{fn(100 * a5['fill_rate'], 0) if a5 is not None else '—'} % à 5 s : la vitesse n'y change presque rien, "
                  "leur rentabilité dépend de la justesse de la formule (voir `reports/polymarket/formule/`).")
    if len(cl_sv) and len(cl_lat):
        bl.append(f"**Chainlink** (prix de résolution) retarde de ≈ {fms(cl_sv['lag_ms'].iloc[0])} sur Binance (heure serveur ; "
                  f"{fms(cl_rx['lag_ms'].iloc[0]) if len(cl_rx) else '—'} vu d'ici), cote {fn(abs(cl_rx['basis_median_pb'].iloc[0]), 1)} pb "
                  f"{'sous' if cl_rx['basis_median_pb'].iloc[0] < 0 else 'au-dessus de'} BTCUSDT et n'arrive ici que {fms(cl_lat['median'].iloc[0])} après son horodatage : le signal, c'est "
                  "Binance ; Chainlink n'est que la règle.")
    clock_txt = (f"horloge locale {'en retard' if clock.get('offset_ms', 0) > 0 else 'en avance'} d'environ "
                 f"{fms(abs(clock['offset_ms']))} (± {fms(clock.get('uncertainty_ms', math.nan))})" if clock
                 else "décalage de l'horloge locale non mesuré (--offline)")
    here_txt = (f"aller-retour HTTP vers le CLOB depuis ce conteneur {fms(rtt)}. D'ici, un ordre aurait ℓ ≈ calcul + "
                f"aller-retour CLOB + 150 ms ≈ {fms(l_here)}." if np.isfinite(rtt) else "aller-retour vers le CLOB non mesuré.")
    bl.append(f"**Transport vu d'ici** : Binance rx − E {fms(d_bn)} (médiane), carnet Polymarket rx − ts {fms(pm_med)} ; "
              f"{clock_txt} ; {here_txt}")
    bl.append(f"**Échantillon petit** : {fn(meta['hours'], 1)} h, {n_mk} marchés ({n_res} résolus) ; IC larges. Relancer "
              "`python scripts/latency_study.py` quand les collecteurs auront tourné plusieurs jours.")
    L += [f"* {b}" for b in bl]
    L.append("")
    L.append("## Graphiques")
    L.append("")
    for key, fname, alt in (("reaction", "reaction_carnet.png", "délai de réaction du carnet"),
                            ("lifetime", "duree_vie_prix_perimes.png", "durée de vie des prix périmés"),
                            ("pnl", "pnl_vs_latence.png", "P&L selon la latence"),
                            ("fill", "execution_vs_latence.png", "taux d'exécution selon la latence"),
                            ("chainlink", "chainlink_vs_binance.png", "retard de Chainlink sur Binance"),
                            ("transport", "latences_transport.png", "latences de transport")):
        if figs.get(key):
            L += [f"**{figs[key]}**", "", f"![{alt}]({fname})", ""]

    L += ["## Comment atteindre ce temps de réaction", ""]
    L.append("> Description technique générique, **inutilisable légalement depuis la France** (blocage ANJ, close-only, "
             "CGU). Aucune méthode de contournement n'est donnée ni envisagée.")
    L.append("")
    L.append(f"Budget « mouvement Binance → ordre apparié », à comparer aux ≈ {fms(t_half)} au bout desquels la moitié de "
             f"l'avantage est perdue (gain démontré jusqu'à ≈ {fms(t_sig)}) :")
    L.append("")
    L.append("| étape | ordre de grandeur | levier |")
    L.append("|---|---|---|")
    L.append("| Binance (moteur à Tokyo, AWS ap-northeast-1) → serveur du robot | 1–5 ms à Tokyo ; ≈ 100–120 ms jusqu'à "
             f"Londres ; ici rx − E = {fms(d_bn)} + décalage d'horloge | WebSocket direct (bookTicker, ou flux binaires "
             "SBE), sans CDN ni proxy |")
    L.append("| calcul de P_formule et décision | < 1 ms | formule fermée (Φ), moyennes glissantes incrémentales, σ "
             "tenu à jour à chaque seconde |")
    L.append("| signature EIP-712 de l'ordre + en-têtes HMAC | ≈ 1–5 ms en Python, < 0,5 ms en natif | clé en mémoire, "
             "bibliothèque native, ordres préparés d'avance aux prix probables |")
    L.append("| serveur du robot → CLOB (AWS eu-west-2, Londres) | < 2 ms dans la même région ; ≈ 100–120 ms depuis "
             f"Tokyo ; aller-retour {fms(rtt)} depuis ce conteneur | connexion HTTP/2 déjà ouverte (keep-alive) |")
    L.append("| délai preneur Polymarket (marchés crypto) | **150 ms, incompressible** (depuis le 04/09/2026) | aucun : "
             "c'est un ralentisseur qui laisse aux teneurs le temps d'annuler |")
    L.append("")
    L.append("Tokyo ↔ Londres coûte ≈ 100–120 ms dans un sens, à payer une fois (sur le flux Binance ou sur l'ordre). Le "
             "meilleur total réaliste pour un **preneur** qui lit Binance est donc ≈ 110 + 150 ≈ 260–280 ms. Les **teneurs "
             "de marché**, eux, annulent sans délai : ce sont eux qui gagnent la course, et la plupart des prix périmés "
             "disparaissent avant qu'un preneur puisse les toucher. Quand le temps utile est sous 150 ms, aucune "
             "infrastructure ne suffit en preneur : il faudrait tenir le carnet (être celui qui réévalue ses prix le plus vite), ce qui "
             "change de métier (inventaire, sélection adverse, remises maker) — voir `reports/polymarket/maker_live/`.")
    L.append("")
    if np.isfinite(lhalf) and np.isfinite(l_here):
        L.append(f"**Depuis ce conteneur** (derrière un proxy, côte est des États-Unis d'après les délais), ℓ ≈ aller-retour "
                 f"CLOB + 150 ms ≈ {fms(l_here)}, contre ℓ½ ≈ {fms(lhalf)} et un gain démontré jusqu'à "
                 f"{fms(lsig) if np.isfinite(lsig) else '— (aucun)'} : "
                 + ("on arriverait après la disparition de l'essentiel des prix périmés." if l_here > lhalf else
                    "en théorie suffisant, mais sans marge.")
                 + " Pour descendre plus bas, les seuls leviers réels sont la **géographie** (un serveur dans la région du "
                 "moteur du CLOB, ou près de Binance, reliés par le chemin réseau le plus court), un **flux Binance direct** "
                 "et un **code natif** qui signe et envoie l'ordre en moins d'une milliseconde ; le délai de 150 ms, lui, "
                 "ne se négocie pas.")
        L.append("")

    L += ["## Tableaux", "", "### Latence critique ℓ* (P&L = 0) et demi-vie de l'avantage", ""]
    if len(crit):
        c = crit.copy()
        c["valeur"] = c["valeur"].map({"pnl": "issue officielle", "markout": "valorisé à +10 s"})
        c["margin"] = (100 * c["margin"]).round(0).astype(int).astype(str) + " c"
        for col in ("l_star_ms", "l_star_lo_ms", "l_star_hi_ms", "l_half_edge_ms", "l_star_raw_ms", "l_sig_ms"):
            c[col] = c[col].map(lambda v: "aucune" if isinstance(v, float) and math.isnan(v) else fms(v))
        c = c[c["trigger"].isin(["saut", "tous"])].sort_values(["trigger", "valeur", "margin"])
        L.append(md(c[["trigger", "margin", "valeur", "l_star_ms", "l_star_lo_ms", "l_star_hi_ms", "l_star_raw_ms",
                       "l_sig_ms", "l_half_edge_ms", "p_positive_at_0", "n_opp", "n_markets"]].rename(columns={
            "trigger": "origine", "margin": "marge", "valeur": "valorisation", "l_star_ms": "ℓ* (isotone)",
            "l_star_lo_ms": "IC bas", "l_star_hi_ms": "IC haut", "l_star_raw_ms": "ℓ* (brut)",
            "l_sig_ms": "gain démontré jusqu'à", "l_half_edge_ms": "ℓ½ avantage", "p_positive_at_0": "P(P&L > 0 à ℓ = 0)",
            "n_opp": "opportunités", "n_markets": "marchés"}), {"P(P&L > 0 à ℓ = 0)": "0%"}))
        L.append("")
        L.append("ℓ* : première latence où le P&L moyen par opportunité devient ≤ 0 (interpolation linéaire sur la grille "
                 "0–5 000 ms), après régression isotone décroissante de la courbe (en espérance, arriver plus tard ne peut "
                 "pas rapporter plus ; « brut » : sans ce lissage, sensible au bruit) ; « 0 ms » : jamais positif ; "
                 "« > 5 000 ms » : encore positif à 5 s. « gain démontré jusqu'à » : plus grande latence de la grille "
                 "jusqu'à laquelle la borne basse de l'IC 95 % reste > 0 (« aucune » : pas même à 0 ms). IC : percentiles 2,5 et "
                 f"97,5 % sur {meta['boot']} tirages bootstrap des marchés. ℓ½ avantage : latence à laquelle l'avantage "
                 "attendu capturé selon la formule (Σ parts × (P(t) − coût), sans bruit d'issue) tombe à la moitié de sa "
                 "valeur à ℓ = 0. Origines « persistante » et « carnet » : `latence_critique.csv`.")
        L.append("")
    L += ["### P&L selon la latence (marge 0)", ""]
    if len(curve):
        rows = []
        for trig in ("saut", "tous"):
            for lat in lt.LATENCIES_MS:
                a, b = cval(trig, "markout", lat), cval(trig, "pnl", lat)
                if a is None:
                    continue
                rows.append({"origine": trig, "ℓ (ms)": lat, "exécution": a["fill_rate"], "parts": a["shares"],
                             "avantage attendu (c/part)": a["mean_edge_fill_c"], "attendu (pUSD/h)": a["edge_per_hour"],
                             "+10 s (pUSD/h)": a["per_hour"],
                             "IC +10 s": f"{fn(a['per_hour_lo'], 1, signed=True)} ; {fn(a['per_hour_hi'], 1, signed=True)}",
                             "issue (pUSD/h)": b["per_hour"] if b is not None else math.nan,
                             "IC issue": (f"{fn(b['per_hour_lo'], 1, signed=True)} ; {fn(b['per_hour_hi'], 1, signed=True)}"
                                          if b is not None else "—"),
                             "issue (c/part)": 100 * b["per_share"] if b is not None else math.nan})
        L.append(md(pd.DataFrame(rows), {"exécution": "0%", "parts": 0, "avantage attendu (c/part)": 1,
                                         "attendu (pUSD/h)": "+1", "+10 s (pUSD/h)": "+1", "issue (pUSD/h)": "+1",
                                         "issue (c/part)": "+1"}))
        L.append("")
        L.append("« attendu » : avantage selon la formule au prix payé (modèle, sans bruit) ; « +10 s » : valorisé au milieu du "
                 "carnet 10 s après l'achat ; « issue » : à l'issue officielle, marchés résolus seulement. Les totaux par "
                 "heure supposent qu'on prend chaque opportunité (50 parts au plus chacune), sans limite de position.")
        L.append("")
    L += ["### Réaction du carnet aux sauts de la formule", ""]

    def cens30(v):
        return "> 30 s" if v == math.inf else fn(v, 0)

    def cens3(v):
        return "> 3 s" if v == math.inf else fn(v, 0)

    if len(reac_sum):
        rs = reac_sum.copy()
        rs["jump"] = (100 * rs["jump"]).round(0).astype(int).astype(str) + " pts"
        keep = ["jump", "asset", "n", "n_markets", "delay_first_ms_median", "delay_placebo_ms_median",
                "delay_50_ms_p25", "delay_50_ms_median", "delay_50_ms_p75", "delay_90_ms_median", "delay_50_ms_censored",
                "delay_50_ms_neg"]
        L.append(md(rs[keep].rename(columns={
            "jump": "saut", "asset": "actif", "n_markets": "marchés", "delay_first_ms_median": "1re retouche bon sens",
            "delay_placebo_ms_median": "placebo (3 s avant)", "delay_50_ms_p25": "50 % p25",
            "delay_50_ms_median": "50 % médiane", "delay_50_ms_p75": "50 % p75", "delay_90_ms_median": "90 % médiane",
            "delay_50_ms_censored": "non suivis (30 s)", "delay_50_ms_neg": "carnet en avance"}),
            {"1re retouche bon sens": cens30, "placebo (3 s avant)": cens3, "50 % p25": cens30, "50 % médiane": cens30,
             "50 % p75": cens30, "90 % médiane": cens30, "non suivis (30 s)": "0%", "carnet en avance": "0%"}))
        L.append("")
        L.append("Délais en ms, vus d'ici, depuis l'instant où la formule a fait la moitié de son saut. « carnet en avance » : "
                 "le milieu avait déjà fait la moitié du chemin avant cet instant (il a réagi à une autre source, ou les "
                 "teneurs voient Binance avant nous). Le placebo mesure la même retouche 3 s avant le saut (fréquence de "
                 "base des retouches, fenêtre arrêtée au saut) : la « 1re retouche » n'est une réaction que si elle est "
                 "nettement plus rapide que lui. Quantiles « censure comprise » (pas de réaction = +∞).")
        L.append("")
    L += ["### Prix périmés (marge 0)", ""]
    if len(opp_sum):
        os_ = opp_sum[(opp_sum["margin"] == 0.0)].copy()
        keep = ["trigger", "asset", "n", "n_markets", "life_median_ms", "life_p75_ms", "surv_50ms", "surv_150ms",
                "surv_300ms", "surv_1s", "edge_life_median_ms", "edge0_median_c", "size0_median", "size_edge_median",
                "share_removed_by_taker"]
        L.append(md(os_[keep].rename(columns={
            "trigger": "origine", "asset": "actif", "n_markets": "marchés", "life_median_ms": "vie méd. (ms)",
            "life_p75_ms": "p75 (ms)", "surv_50ms": "> 50 ms", "surv_150ms": "> 150 ms", "surv_300ms": "> 300 ms",
            "surv_1s": "> 1 s", "edge_life_median_ms": "avantage > 0 (ms, méd.)", "edge0_median_c": "avantage (c/part)",
            "size0_median": "parts au meilleur ask", "size_edge_median": "parts rentables",
            "share_removed_by_taker": "pris par un preneur"}),
            {"vie méd. (ms)": 0, "p75 (ms)": 0, "> 50 ms": "0%", "> 150 ms": "0%", "> 300 ms": "0%", "> 1 s": "0%",
             "avantage > 0 (ms, méd.)": 0, "avantage (c/part)": 1, "parts au meilleur ask": 0, "parts rentables": 0,
             "pris par un preneur": "0%"}))
        L.append("")
        L.append("Origine : **saut** = P_formule a monté d'au moins 1 point dans la seconde et l'avantage n'existait pas "
                 "1 s plus tôt (prix périmé au sens strict) ; **persistante** = l'avantage existait déjà 1 s plus tôt "
                 "(désaccord durable entre la formule et le marché, ou niveau qui clignote) ; **carnet** = un ask moins cher "
                 "est apparu sans mouvement de Binance. Marges 1 c et 2 c : `opportunites_resume.csv`.")
        L.append("")
    L += ["### Latences de transport (réception locale − horodatage serveur)", ""]
    L.append(md(tr[["source", "mesure", "n", "p10", "median", "p90", "note"]], {"p10": 0, "median": 0, "p90": 0}))
    L.append("")
    if len(cl_sum):
        L += ["### Chainlink contre Binance", ""]
        L.append(md(cl_sum, {"corr_max": 3, "corr_lag0": 3, "basis_median_pb": 2, "basis_p10_pb": 2, "basis_p90_pb": 2,
                             "basis_sd_pb": 2}))
        L.append("")
        L.append("`lag_ms` : décalage de corrélation maximale entre les rendements Chainlink (sur `horizon_s`) et ceux de "
                 "Binance décalés ; `horloge` = `rx` (Binance à la réception locale : ce que voit le robot) ou `serveur` "
                 "(heure d'événement Binance `E`, indépendante de notre réseau). `basis` : log(Chainlink / Binance décalé), "
                 "en points de base. Le rapport historique `reports/polymarket/formule/` trouvait ≈ 4 s avec une autre "
                 "méthode (erreur sur F − K entre bougies Binance 1 s et `priceToBeat`) : la corrélation des rendements "
                 "mesure le retard du signal, l'erreur de niveau inclut aussi le lissage de l'agrégat Chainlink.")
        L.append("")

    L += ["## Méthode", ""]
    L.append("1. **Séries alignées sur l'horloge locale** (`rx`) : milieu Binance (`bookTicker`), dernier prix Binance "
             "(`aggTrade`, `E`/`T` serveur), Coinbase (`ticker`, `time`), Chainlink (RTDS `crypto_prices_chainlink` : valeur, "
             "horodatage de l'observation, heure de publication, `rx`), carnet Polymarket reconstruit après chaque message "
             "(meilleurs bid/ask Up et Down, tailles, milieu ; `rx` et `ts` serveur). Valeur « à la date » = dernier point "
             "reçu ≤ t.")
    L.append(f"2. **P_formule** (`tradebot.latency.TwapFormula`) : P(Up | t) de `polymarket_formula` (Up ssi TWAP60 Chainlink "
             "à E ≥ TWAP60 à S) calculée pour des moyennes de **points 1 s** : m = E[F − K | t] avec les points déjà réalisés "
             "(Binance 1 s reconstruit : dernier milieu reçu ≤ chaque seconde) et le prix Binance courant pour les points "
             "futurs ; Var/σ² = ∫ g², exacte entre deux secondes. σ = EWMA causale des rendements 1 s (demi-vie "
             f"{int(SIGMA_HALFLIFE_S)} s, initialisée sur les {fn(SIGMA_WARMUP_H, 0)} h de bougies Binance 1 s précédant les "
             f"données) × {fn(meta['sigma_factor'], 2)} (facteur de calibration du rapport historique "
             "`reports/polymarket/formule/` : les rendements 1 s sont autocorrélés). Retard de Chainlink : le point "
             f"Chainlink `s` est lu sur Binance à `s − {fn(meta['lag_ms'], 0)} ms` (mesuré ici). Erreur de suivi "
             f"Chainlink − Binance de F − K : {fn(meta['extra_sd_pb'], 2)} pb ajoutés en quadrature "
             f"({meta['extra_sd_source']}). Évaluée toutes les 100 ms, à chaque mise à jour Binance et à chaque message du carnet.")
    L.append("3. **Réaction du carnet** : sauts de P_formule ≥ 5 et ≥ 10 points en ≤ 1 s (puis 2 s sans nouveau saut) ; "
             "t0 = instant où la formule a fait la moitié du saut (précisé à la mise à jour Binance près) ; délais jusqu'à la "
             "première retouche du meilleur ask/bid Up dans le sens du saut après t0 (placebo : la même mesure 3 s avant "
             "le saut), puis jusqu'à ce que le milieu ait parcouru 50 % et 90 % du saut depuis son niveau d'avant ; "
             "censure à 30 s.")
    L.append(f"4. **Prix périmés** : opportunité à t si P_côté(t) − (ask + 0,07·ask·(1 − ask)) > marge (0, 1 c, 2 c), pour "
             f"un ask entre {fn(ASK_RANGE[0], 2)} et {fn(ASK_RANGE[1], 2)} (les queues relèvent surtout de l'erreur de "
             "modèle). Une opportunité = un niveau de prix (côté, ask0) ; elle vit jusqu'à ce que le meilleur ask du côté "
             "remonte au-dessus de ask0 (niveau annulé ou consommé) ; un niveau encore vivant n'en ouvre pas une nouvelle. "
             "Origine : « saut » si P_côté a monté d'au moins 1 point dans la seconde et que l'avantage n'existait pas "
             "1 s plus tôt, « persistante » s'il existait déjà, « carnet » sinon. « Pris par un preneur » : un trade a "
             "consommé ce niveau pendant sa vie. Tailles : au meilleur ask, et cumulées sur les niveaux encore rentables. "
             "Les messages du CLOB de même horodatage serveur (annulation + nouvel ordre…) sont regroupés : les états "
             "intermédiaires de quelques µs ne comptent pas comme des prix disponibles.")
    L.append(f"5. **Exécution à t + ℓ** : pour chaque opportunité détectée à t, l'ordre arrive au carnet vu à t + ℓ ; achat au "
             "meilleur ask de ce carnet si P_côté(t) − coût(ask) > marge (ordre limite calculé à t), quantité min(taille au "
             "meilleur ask, 50) ; P&L = parts × (1{côté gagnant} − ask − frais) à l'issue officielle (`meta.json`, sinon "
             f"gamma en lecture seule), et valorisation au milieu du côté {int(MARKOUT_S)} s après l'achat (à l'issue si la "
             f"clôture tombe avant). IC : bootstrap des marchés ({meta['boot']} tirages).")
    L.append("")
    L += ["### Biais de notre propre latence (et comment il déplace ℓ*)", ""]
    L.append(f"On voit Binance avec un retard d_B et le carnet avec un retard d_P (≈ 50–150 ms chacun, inséparables sans "
             "horloge commune). Un mouvement Binance à l'instant réel τ est détecté ici à τ + d_B ; un état du carnet à "
             "l'instant réel x est vu ici à x + d_P. Simuler « ordre au carnet vu à t + ℓ » revient à apparier l'ordre à "
             "l'instant réel τ + d_B + ℓ − d_P : **le temps réel disponible vaut T = ℓ* + (d_B − d_P)**, et d_B − d_P ne "
             f"dépend pas de l'horloge locale : médiane(rx − E Binance) − médiane(rx − ts CLOB) = {fn(d_bn, 0)} − "
             f"({fn(pm_med, 0)}) = {fn(rel, 0, signed=True)} ms : les latences mesurées ici (ℓ*, ℓ½, gain démontré) "
             f"{'sous-estiment' if rel > 0 else 'surestiment'} donc le temps réel de ≈ {fn(abs(rel), 0)} ms. Autres biais : "
             "(i) des teneurs qui lisent Binance plus près de Tokyo réagissent plus tôt que ce que nous voyons (les délais de "
             "réaction « vus d'ici » sont raccourcis de leur avance, ℓ* réel encore plus court) ; (ii) les ordres des autres "
             "preneurs sont dans les données (le niveau disparaît), mais notre ordre ne déplace pas le carnet (50 parts max., "
             "sans impact) ; (iii) un teneur qui annule dès qu'il voit un ordre preneur arriver (pendant le délai de 150 ms) "
             "rendrait l'exécution réelle pire que simulée.")
    L.append("")
    L += ["## Limites", ""]
    L.append(f"* **n petit** : {fn(meta['hours'], 1)} h de données communes aux deux collecteurs, {n_mk} marchés, {n_res} avec "
             "issue connue (les autres n'entrent que dans la valorisation à +10 s). Les opportunités d'un même marché "
             "partagent la même issue : l'IC groupé par marché est la seule mesure honnête de l'incertitude.")
    L.append("* ETH : le flux RTDS ne renvoie que la première souscription de chaque thème (btc/usd) : **pas de Chainlink "
             "ETH** ; le retard et l'erreur de suivi mesurés sur BTC sont appliqués à ETH.")
    L.append("* Carnet : les `price_change` sont filtrés à ± 0,10 du milieu par le collecteur (sans effet sur le meilleur "
             "niveau) ; la taille au meilleur ask est celle affichée (d'autres preneurs peuvent la prendre avant nous).")
    L.append("* P_formule suppose un log-prix sans tendance, σ constant jusqu'à E et Chainlink = Binance décalé ; elle se "
             "trompe quand Chainlink s'écarte de Binance (sources agrégées), surtout près de F = K. Les désaccords "
             "persistants sont surtout des erreurs de modèle ou des différences de σ, pas des prix périmés.")
    L.append("* 150 ms de délai preneur (documenté pour les marchés crypto depuis le 04/09/2026, non mesuré ici) : les "
             "latences ℓ < 150 ms ne sont atteignables par personne en preneur ; elles servent de référence. Le décalage "
             "d'horloge locale est estimé par une seule requête Binance (± la moitié de l'aller-retour) : seules les "
             "différences entre sources sont sûres.")
    L.append("")
    L += ["## Relancer", ""]
    L.append("```bash\n. .venv/bin/activate\npython scripts/latency_study.py              # tout ce qui est disponible\n"
             "python scripts/latency_study.py --since 2026-09-27T00:00 --until 2026-09-28T00:00 --jobs 3\n"
             "python scripts/latency_study.py --offline    # sans réseau (ni horloge, ni gamma, ni historique pour σ)\n```")
    L.append("")
    L.append("Le script relit tous les fichiers des deux collecteurs sur la période, garde les marchés complets (Binance et "
             "carnet couvrant [S − 90 s, E]) et réécrit ce dossier. Il ne modifie aucune donnée brute (les issues manquantes "
             "sont lues sur gamma sans toucher aux `meta.json`).")
    L.append("")
    L += ["## Fichiers", ""]
    files = [("marches.csv", "un marché par ligne : couverture, σ à S, F − K Binance contre l'issue officielle, nombres d'événements"),
             ("latences_transport.csv", "réception locale − horodatages serveur par source (p01, p10, médiane, p90, p99)"),
             ("chainlink_binance_correlation.csv", "corrélation Chainlink / Binance selon le décalage (pas de 100 ms)"),
             ("chainlink_binance_resume.csv", "retard et décalage (pb) de Chainlink sur Binance"),
             ("chainlink_erreur_suivi.csv", "F − K Chainlink contre Binance décalé, par marché BTC"),
             ("reaction_carnet.csv", "un saut de la formule par ligne, délais de réaction du carnet (ms)"),
             ("reaction_carnet_resume.csv", "résumé des délais par seuil et actif"),
             ("opportunites.csv.gz", "une opportunité (niveau périmé) par ligne et par marge : origine, durée de vie, taille, avantage, retrait"),
             ("opportunites_resume.csv", "résumé par marge, origine et actif (survie à 50/150/300/1 000 ms)"),
             ("pnl_par_opportunite.csv.gz", "P&L, quantité et prix d'exécution de chaque opportunité pour chaque ℓ"),
             ("pnl_vs_latence.csv", "P&L total / par opportunité / par part / par heure selon ℓ, marge et origine, avec IC"),
             ("latence_critique.csv", "ℓ*, IC et ℓ½ par marge, origine et valorisation"),
             ("pnl_par_marche.csv", "P&L par marché, marge, origine et ℓ"),
             ("pnl_par_heure.csv", "P&L par heure UTC, marge, origine et ℓ"),
             ("series_alignees_1s.csv.gz", "séries alignées sur l'horloge locale, une ligne par seconde et par marché : "
              "Binance (milieu, dernier trade), Coinbase, Chainlink (valeur et horodatage du dernier point reçu), carnet "
              "Up (bid, ask, tailles, milieu), P_formule, σ (désactivable : `--no-series`)"),
             ("run.json", "paramètres et métadonnées du calcul")]
    L += [f"* `{f}` : {d}." for f, d in files]
    L.append("")
    L.append("Les fichiers `.csv.gz` (volumineux) se lisent directement avec `pandas.read_csv`.")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default=None, help="début (UTC, ex. 2026-09-26T10:29) ; défaut : début des flux CEX")
    ap.add_argument("--until", default=None, help="fin (UTC) ; défaut : maintenant")
    ap.add_argument("--series", default="btc:5m,btc:15m,eth:5m")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--lag-ms", type=float, default=None, help="retard Chainlink imposé (défaut : mesuré)")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--offline", action="store_true", help="pas de requête réseau (horloge, gamma, historique σ)")
    ap.add_argument("--sigma-factor", type=float, default=SIGMA_FACTOR)
    ap.add_argument("--no-series", action="store_true", help="ne pas écrire series_alignees_1s.csv.gz")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t_run = time.time()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    since, until = parse_time(args.since), parse_time(args.until)
    until = until or time.time()

    # --- 1. flux CEX -------------------------------------------------------
    cex = load_cex(since, until)
    if not cex["binance"]:
        log.error("aucune donnée Binance dans data/cache/cex_ws/")
        return 1
    bn_start = max(float(d["book"]["rx"].iloc[0]) for d in cex["binance"].values() if len(d["book"]))
    bn_end = min(float(d["book"]["rx"].iloc[-1]) for d in cex["binance"].values() if len(d["book"]))
    since = since or bn_start
    prices, trade_prices, sigma = {}, {}, {}
    for asset, sym in SYMBOL.items():
        d = cex["binance"].get(sym)
        if d is None or len(d["book"]) < 1000:
            continue
        prices[asset] = lt.PriceSeries(d["book"]["rx"].to_numpy(), d["book"]["mid"].to_numpy())
        if len(d["trades"]):
            trade_prices[asset] = lt.PriceSeries(d["trades"]["E"].to_numpy(), d["trades"]["price"].to_numpy())
    v_prior = {}
    if not args.offline:
        for asset, sym in SYMBOL.items():
            if asset not in prices:
                continue
            try:
                k = fetch_klines_1s(sym, prices[asset].t[0] - SIGMA_WARMUP_H * 3600, prices[asset].t[0])
                v = lt.ewma_var_1s(np.log(k), SIGMA_HALFLIFE_S, 120)
                v_prior[asset] = float(v[-1]) if k.size > 200 and np.isfinite(v[-1]) else None
            except Exception as exc:  # noqa: BLE001
                log.warning("historique Binance 1 s indisponible pour %s (%r)", sym, exc)
    for asset in prices:
        sigma[asset] = lt.SigmaSeries.from_prices(prices[asset], SIGMA_HALFLIFE_S, SIGMA_MIN_OBS,
                                                  v_init=v_prior.get(asset), factor=args.sigma_factor)
    warm = {a: (v_prior.get(a) is not None) for a in prices}

    clock, rtt = None, math.nan
    if not args.offline:
        try:
            clock = clock_offset_binance()
            rtt = rtt_clob()
        except Exception as exc:  # noqa: BLE001
            log.warning("mesure d'horloge impossible (%r)", exc)

    # --- Chainlink ---------------------------------------------------------
    cls = chainlink_study(cex, prices, trade_prices)
    s1 = cls["summary"]
    lag_ms = args.lag_ms
    if lag_ms is None:
        row = s1[(s1["asset"] == "btc") & (s1["horloge"] == "rx") & (s1["horizon_s"] == 1.0)] if len(s1) else s1
        lag_ms = float(row["lag_ms"].iloc[0]) if len(row) else 0.0
    lag_s = lag_ms / 1e3

    # --- marchés -----------------------------------------------------------
    series = {tuple(s.split(":")) for s in args.series.split(",") if s}
    first_ok = {a: since + (1 if warm[a] else SIGMA_MIN_OBS + 5) for a in prices}   # σ disponible dès S − 90 s
    markets = []
    for slug in list_markets(LIVE_DIR):
        parts = slug.split("-")
        asset, dur, S = parts[0], parts[2], int(parts[3])
        E = S + (300 if dur == "5m" else 900)
        if (asset, dur) not in series or asset not in prices:
            continue
        if S - PRE_S < first_ok[asset] or S - 60 - abs(lag_s) < since or E > min(bn_end, until) - 2:
            continue
        markets.append({"slug": slug, "asset": asset, "duration": dur, "S": S, "E": E})
    log.info("%d marchés dans la période", len(markets))
    if not markets:
        log.error("aucun marché complet dans la période couverte par les deux collecteurs")
        return 1

    # issues
    import json

    outcomes = {}
    for mk in markets:
        meta = json.loads((LIVE_DIR / f"{mk['slug']}.meta.json").read_text())
        r = resolution_of(meta)
        if r is None and not args.offline and time.time() - mk["E"] > 180:
            r = fetch_outcome(mk["slug"])
        outcomes[mk["slug"]] = r

    # erreur de suivi Chainlink − Binance de F − K
    te = tracking_error(markets, cex, prices, lag_s)
    if len(te) >= 10:
        extra_sd = float(np.sqrt(np.mean(te["diff_pb"] ** 2)) / 1e4)
        extra_src = f"écart quadratique moyen mesuré ici sur {len(te)} marchés BTC"
    else:
        extra_sd = SOURCE_NOISE_PB / 1e4
        extra_src = (f"valeur du rapport historique : moins de 10 marchés BTC avec Chainlink complet ici ({len(te)})")

    aux = {}
    for asset in prices:
        a = {}
        tr_ = cex["binance"][SYMBOL[asset]]["trades"]
        if len(tr_):
            a["bn_last"] = (tr_["rx"].to_numpy(), tr_["price"].to_numpy())
        cb = cex["coinbase"].get(f"{asset.upper()}-USD")
        if cb is not None and len(cb):
            a["cb"] = (cb["rx"].to_numpy(), cb["mid"].to_numpy())
        cl = cex["rtds"].get(CHAINLINK[asset])
        if cl is not None and len(cl):
            u = cl[~cl["snapshot"]].sort_values("rx")
            a["cl"] = (u["rx"].to_numpy(), u["value"].to_numpy(), u["ts"].to_numpy())
        aux[asset] = a
    G.update(prices=prices, sigma=sigma, lag_s=lag_s, extra_sd=extra_sd, outcomes=outcomes, aux=aux,
             series=not args.no_series)
    slugs = [mk["slug"] for mk in markets]
    t_m = time.time()
    if args.jobs > 1:
        ctx = mp.get_context("fork")
        with ctx.Pool(args.jobs) as pool:
            results = pool.map(analyse_market, slugs, chunksize=1)
    else:
        results = [analyse_market(s) for s in slugs]
    log.info("marchés analysés en %.0f s", time.time() - t_m)

    ok = [r for r in results if "skip" not in r]
    reac = pd.concat([r["reaction"] for r in ok if len(r["reaction"])], ignore_index=True)
    opps = pd.concat([r["opps"] for r in ok if len(r["opps"])], ignore_index=True)
    fills = pd.concat([r["fills"] for r in ok if len(r["fills"])], ignore_index=True)
    mk_df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("reaction", "opps", "fills", "pm_delays", "series")}
                          for r in results])
    mk_df["official_up"] = mk_df["slug"].map(outcomes)
    mk_df["binance_agrees"] = np.where(mk_df["official_up"].notna() & mk_df["binance_says_up"].notna(),
                                       mk_df["official_up"] == mk_df["binance_says_up"], np.nan)
    if len(te):
        mk_df = mk_df.merge(te[["slug", "fk_chainlink_pb", "diff_pb"]], on="slug", how="left")

    # heures couvertes
    S_all, E_all = mk_df.loc[mk_df.get("skip").isna() if "skip" in mk_df else slice(None), ["S", "E"]].to_numpy().T
    hours = (E_all.max() - (S_all.min() - PRE_S)) / 3600.0
    res_mk = mk_df[mk_df["official_up"].notna()]
    hours_res = ((res_mk["E"].max() - (res_mk["S"].min() - PRE_S)) / 3600.0) if len(res_mk) else math.nan
    G["hours_resolved"] = hours_res

    # --- latences de transport ----------------------------------------------
    pm_d = {}
    for r in ok:
        key = f"{r['asset'].upper()} {r['duration']}"
        pm_d.setdefault(key, []).append(r["pm_delays"])
    pm_d = {k: np.concatenate(v) for k, v in pm_d.items()}
    tr = transport_table(cex, pm_d, clock)

    # --- P&L ----------------------------------------------------------------
    if len(fills):
        fills["hour"] = pd.to_datetime(fills["t_start"], unit="s", utc=True).dt.floor("h")
    curve, crit = pnl_tables(fills, hours, args.boot) if len(fills) else (pd.DataFrame(), pd.DataFrame())
    by_market = pd.DataFrame()
    by_hour = pd.DataFrame()
    if len(fills):
        agg = dict(n_opp=("opp_id", "size"), n_filled=("filled", "sum"), shares=("qty", "sum"),
                   pnl=("pnl", lambda v: v.sum(min_count=1)), markout=("markout", "sum"))
        keys = ["margin", "trigger", "latency_ms"]
        by_market = fills.groupby(keys + ["market"]).agg(**agg).reset_index()
        by_hour = fills.groupby(keys + ["hour"]).agg(**agg).reset_index()
        by_hour["n_markets"] = fills.groupby(keys + ["hour"])["market"].nunique().to_numpy()

    # --- sorties -------------------------------------------------------------
    from tradebot.report import write_csv, write_json

    write_csv(tr, out / "latences_transport.csv")
    if len(cls["scan"]):
        write_csv(cls["scan"], out / "chainlink_binance_correlation.csv")
    write_csv(cls["summary"], out / "chainlink_binance_resume.csv")
    if len(te):
        write_csv(te, out / "chainlink_erreur_suivi.csv")
    write_csv(mk_df, out / "marches.csv")
    write_csv(reac, out / "reaction_carnet.csv")
    reac_sum = reaction_summary(reac)
    write_csv(reac_sum, out / "reaction_carnet_resume.csv")
    opp_cols = ["market", "asset", "duration", "margin", "trigger", "side", "phase", "t_start", "ask0", "size0",
                "size_edge", "p_side", "edge0", "p_side_1s", "edge_1s", "life_book_ms", "life_edge_ms", "censored",
                "removed_by", "taken_size"]
    big = dict(index=False, float_format="%.13g", compression="gzip")
    (opps[[c for c in opp_cols if c in opps]] if len(opps) else opps).to_csv(out / "opportunites.csv.gz", **big)
    opp_sum = opportunity_summary(opps)
    write_csv(opp_sum, out / "opportunites_resume.csv")
    wide = wide_fills(opps, fills)
    wide.drop(columns=["idx"], errors="ignore").to_csv(out / "pnl_par_opportunite.csv.gz", **big)
    write_csv(curve, out / "pnl_vs_latence.csv")
    write_csv(crit, out / "latence_critique.csv")
    write_csv(by_market, out / "pnl_par_marche.csv")
    write_csv(by_hour, out / "pnl_par_heure.csv")
    ser = [r["series"] for r in ok if "series" in r]
    if ser:
        pd.concat(ser, ignore_index=True).to_csv(out / "series_alignees_1s.csv.gz", index=False,
                                                 float_format="%.10g", compression="gzip")

    figs = {}
    figs["reaction"] = plot_reaction(reac, out / "reaction_carnet.png") if len(reac) else ""
    figs["lifetime"] = plot_lifetime(opps, out / "duree_vie_prix_perimes.png") if len(opps) else ""
    figs["pnl"] = plot_pnl(curve, crit, out / "pnl_vs_latence.png") if len(curve) else ""
    figs["fill"] = plot_fill(curve, out / "execution_vs_latence.png") if len(curve) else ""
    figs["chainlink"] = (plot_chainlink(cls["scan"], cls["summary"], out / "chainlink_vs_binance.png")
                         if len(cls["scan"]) else "")
    figs["transport"] = plot_transport(tr, out / "latences_transport.png")

    meta = {"genere": pd.Timestamp.now(tz="UTC").isoformat(), "since": since, "until": until, "bn_start": bn_start,
            "bn_end": bn_end, "n_markets": len(ok), "hours": hours, "hours_resolved": hours_res, "lag_ms": lag_ms,
            "extra_sd_pb": extra_sd * 1e4, "extra_sd_source": extra_src, "sigma_factor": args.sigma_factor,
            "sigma_warm_start": warm, "clock": clock, "rtt_clob_ms": rtt, "runtime_s": time.time() - t_run,
            "jobs": args.jobs, "boot": args.boot, "markets": [r["slug"] for r in ok]}
    write_json(out / "run.json", meta)
    from tradebot.report import write_text

    write_text(out / "README.md", render_readme(meta, tr, cls, mk_df, reac_sum, opp_sum, curve, crit, figs))
    log.info("terminé en %.0f s -> %s", time.time() - t_run, out)
    return 0



if __name__ == "__main__":
    sys.exit(main())
