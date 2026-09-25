#!/usr/bin/env python
"""Top wallets of the Polymarket crypto "Up or Down" markets (BTC/ETH/SOL, 5m and 15m), ranked on
one-hour windows: who wins, what they look like, whether it persists, and whether copying them
(out of sample, with a delay) or following their order flow would have paid.

Read-only research on public data, paper simulation only: no order, no key. The trades come from
the cache written by :mod:`tradebot.polymarket_wallets` (``collect_trades``: every fill of
``data-api /v2/trades`` with the wallet, the maker/taker role and the block timestamp to the
second). Nothing is downloaded if the cache covers the period (the default period is cached).

Outputs (``reports/polymarket/top_wallets/``): CSV tables and PNG charts; the French write-up
``README.md`` is written by hand from these tables. A JSON summary is printed on stdout.

Conventions
-----------
* "hour h" = the UTC hour of the window start S of a market (same as ``hourly_wallet_table``);
  every market starting in hour h ends at h+1:00 at the latest.
* Out-of-sample leaders for a decision taken at time t: hour ``H = floor_hour(t - 120 s)``;
  leaders = top K wallets by P&L summed over the markets starting in ``[H - L, H)`` (L = 1 h or
  6 h). All those markets ended at H:00 <= t - 120 s, so their outcome and fills were public
  (resolution ~50 s after the end, Data API delay ~10 s). The current hour is never used.
* Evaluation period for questions 4-5: decision hours from start + 6 h (so that L = 1 h and
  L = 6 h are compared on the same trades).
* Copy trade: for each BUY fill (maker or taker) of a leader at block time tau (fills of the same
  wallet, token and second merged into one event), buy the same token at tau + d as a taker. The
  price is read from the tape of taker BUY fills of that token: ``first`` = first one at
  >= tau + d (for d = 0: strictly after the leader's fills in on-chain order), ``vwap`` = VWAP of
  those in [tau + d, tau + d + 2 s] (falls back to ``first``). Events with no taker BUY within
  10 s are discarded and counted. Taker fee 0.07 p (1 - p) per share, payout at resolution.
* Confidence intervals: bootstrap resampling 15-minute window slots (the 5m/15m markets and the
  three assets of a slot are correlated), or hours for hourly statistics.
"""

from __future__ import annotations

import argparse
import json
import math
import textwrap
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from scipy.stats import norm, rankdata, spearmanr  # noqa: E402

from tradebot import polymarket_wallets as pw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "polymarket" / "top_wallets"
DEFAULT_START = "2026-09-23T21:00Z"
DEFAULT_END = "2026-09-25T21:00Z"

FEE_RATE = 0.07
DELAYS = (0, 1, 2, 3, 5, 10, 20, 30, 60)
KS = (5, 10, 20)
LOOKBACKS = (1, 6)
MAX_WAIT_S = 10          # no taker BUY of the token within 10 s after tau + d -> discarded
VWAP_SPAN_S = 2          # VWAP estimator over [tau + d, tau + d + 2 s]
SAFETY_S = 120           # leaders are ranked on markets that ended >= 120 s before the decision
WARMUP_H = 6             # first evaluation hour = start + 6 h (same sample for L = 1 h and 6 h)
SLOT_S = 900             # bootstrap cluster = 15-minute window slot
TOP_N = 20
FLOW_CUTS = (30, 60)     # order-flow signal measured up to S + 30 s / S + 60 s
PRICE_LOOKBACK_S = 10    # market price at S + c = VWAP of taker fills in (S + c - 10 s, S + c]
N_CV_BLOCKS = 6

# measured latencies (docs/research/polymarket_temps_reel.md), converted to a delay d between
# the leader's block timestamp and ours: d ~ (time we learn the fill, counted from the leader's
# match) + ~0.3 s (our order, 150 ms taker delay); block time ~ match + 2.2 s for both trades.
LATENCY_MARKS = {
    "mempool": 1.0,
    "logs on-chain / RTDS": (2.0, 3.0),
    "Data API par wallet": 5.5,
    "Data API par marché": 9.0,
}

# chart style
BG, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
SOURCE = "Source : data-api.polymarket.com /v2/trades (preneurs et makers), marchés Up/Down BTC, ETH, SOL 5m et 15m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fee_per_share(p: np.ndarray | float) -> np.ndarray | float:
    return FEE_RATE * p * (1.0 - p)


def cluster_boot(num, den, clusters, *, n_boot=2000, seed=0, alpha=0.05) -> dict:
    """Ratio estimator sum(num) / sum(den) with a cluster bootstrap (percentile CI, bootstrap SE)."""
    num = np.asarray(num, dtype="float64")
    den = np.asarray(den, dtype="float64")
    if len(num) == 0 or den.sum() == 0:
        return {"est": math.nan, "lo": math.nan, "hi": math.nan, "se": math.nan, "n_clusters": 0}
    _, inv = np.unique(np.asarray(clusters), return_inverse=True)
    sn = np.bincount(inv, weights=num)
    sd = np.bincount(inv, weights=den)
    c = len(sn)
    rng = np.random.default_rng(seed)
    w = rng.multinomial(c, np.full(c, 1.0 / c), size=n_boot).astype("float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        est = (w @ sn) / (w @ sd)
    lo, hi = np.nanpercentile(est, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"est": float(sn.sum() / sd.sum()), "lo": float(lo), "hi": float(hi),
            "se": float(np.nanstd(est, ddof=1)), "n_clusters": int(c)}


def block_boot_mean(values: np.ndarray, block: int = 6, n_boot=2000, seed=0) -> tuple[float, float, float]:
    """Mean of a time series with a moving-block bootstrap CI (for overlapping hour pairs)."""
    v = np.asarray(values, dtype="float64")
    v = v[~np.isnan(v)]
    n = len(v)
    if n == 0:
        return math.nan, math.nan, math.nan
    b = min(block, n)
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / b))
    starts = rng.integers(0, n - b + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = v[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(v.mean()), float(lo), float(hi)


def auc(score: np.ndarray, y: np.ndarray) -> float:
    y = np.asarray(y, dtype=bool)
    n1, n0 = y.sum(), (~y).sum()
    if n1 == 0 or n0 == 0:
        return math.nan
    r = rankdata(score)
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return mid - half, mid + half


def logit_fit(x: np.ndarray, y: np.ndarray, l2: float = 1e-3, iters: int = 100) -> np.ndarray:
    """Logistic regression by Newton-Raphson (tiny ridge), intercept included."""
    x1 = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(x1.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(x1 @ w, -30, 30)))
        g = x1.T @ (y - p) - l2 * w
        h = (x1 * (p * (1 - p))[:, None]).T @ x1 + l2 * np.eye(x1.shape[1])
        step = np.linalg.solve(h, g)
        w += step
        if np.abs(step).max() < 1e-9:
            break
    return w


def logit_predict(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    x1 = np.column_stack([np.ones(len(x)), x])
    return 1.0 / (1.0 + np.exp(-np.clip(x1 @ w, -30, 30)))


def short(addr: str) -> str:
    a = str(addr)
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def new_fig(w: float = 8.0, h: float = 4.6):
    fig, ax = plt.subplots(figsize=(w, h), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
        ax.spines[s].set_linewidth(0.8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=8.5, length=0)
    return fig, ax


def fnum(n: float) -> str:
    """Thousands separated by a space (French style)."""
    return f"{int(round(n)):,}".replace(",", " ")


def finish(fig, ax, title: str, note: str, path: Path, *, legend: bool = True, legend_loc: str = "best",
           legend_kw: dict | None = None) -> None:
    width_in = fig.get_size_inches()[0]
    title = textwrap.fill(title, width=int(width_in * 9.4))
    fig.suptitle(title, x=0.012, y=0.985, ha="left", va="top", color=INK, fontsize=12, fontweight="semibold")
    ax.xaxis.label.set_color(INK2)
    ax.yaxis.label.set_color(INK2)
    ax.xaxis.label.set_size(9)
    ax.yaxis.label.set_size(9)
    if legend and ax.get_legend_handles_labels()[0]:
        kw = {"loc": legend_loc, "frameon": False, "fontsize": 8.5, "labelcolor": INK}
        kw.update(legend_kw or {})
        leg = ax.legend(**kw)
        if kw.get("frameon"):
            leg.get_frame().set_facecolor(BG)
            leg.get_frame().set_edgecolor(GRID)
            leg.get_frame().set_alpha(0.95)
    note = "\n".join(textwrap.fill(par, width=int(width_in * 17.5)) for par in note.split("\n"))
    n_lines = note.count("\n") + 1
    bottom = 0.035 + 0.032 * n_lines
    fig.text(0.012, 0.012, note, color=INK2, fontsize=7.2, ha="left", va="bottom", linespacing=1.35)
    fig.tight_layout(rect=(0, bottom, 1, 1))
    fig.savefig(path, dpi=150, facecolor=BG)
    plt.close(fig)


def pct_fmt(decimals: int = 0):
    return FuncFormatter(lambda v, _: f"{v * 100:.{decimals}f} %".replace(".", ","))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load(start: str, end: str):
    markets, trades = pw.collect_trades(start=start, end=end, cache=True)
    pnl = pw.wallet_market_pnl(trades)
    hourly = pw.hourly_wallet_table(trades, pnl)
    return markets, trades, pnl, hourly


def trade_arrays(trades: pd.DataFrame, t_start: pd.Timestamp) -> dict[str, np.ndarray]:
    """Plain numpy views of the trade table (integer codes for wallets, tokens, markets)."""
    a: dict[str, np.ndarray] = {}
    a["ts"] = pd.DatetimeIndex(trades["ts"]).as_unit("s").asi8
    a["S"] = pd.DatetimeIndex(trades["window_start"]).as_unit("s").asi8
    a["E"] = pd.DatetimeIndex(trades["window_end"]).as_unit("s").asi8
    a["t_rel"] = a["ts"] - a["S"]
    a["wallet"] = trades["wallet"].cat.codes.to_numpy().astype("int64")
    a["token"] = trades["token_id"].cat.codes.to_numpy().astype("int64")
    a["cond"] = trades["condition_id"].cat.codes.to_numpy().astype("int64")
    a["seq"] = trades["seq"].to_numpy().astype("int64")
    a["price"] = trades["price"].to_numpy(dtype="float64")
    a["size"] = trades["size"].to_numpy(dtype="float64")
    a["notional"] = a["price"] * a["size"]
    a["buy"] = (trades["side"] == "BUY").to_numpy(dtype=bool)
    a["taker"] = (trades["role"] == "taker").to_numpy(dtype=bool)
    a["up"] = (trades["outcome"] == "Up").to_numpy(dtype=bool)
    ru = trades["resolved_up"].to_numpy(dtype=bool, na_value=False)
    a["won"] = a["up"] == ru
    a["resolved_up"] = ru
    a["fee"] = np.where(a["taker"], np.round(fee_per_share(a["price"]) * a["size"], 5), 0.0)
    a["h_S"] = (a["S"] - int(t_start.timestamp())) // 3600          # hour index of S
    a["slot"] = a["S"] // SLOT_S
    a["asset"] = trades["asset"].astype(str).to_numpy()
    a["duration"] = trades["duration"].astype(str).to_numpy()
    a["up_price"] = np.where(a["up"], a["price"], 1.0 - a["price"])  # Up-equivalent price
    return a


def pnl_matrix(hourly: pd.DataFrame, wallet_index: pd.Index, t_start: pd.Timestamp, n_hours: int, col: str = "pnl"):
    """Dense (hour x wallet) matrix of hourly P&L (0 when inactive) and activity mask."""
    hi = ((pd.DatetimeIndex(hourly["hour"]) - t_start) // pd.Timedelta(hours=1)).to_numpy()
    wi = wallet_index.get_indexer(hourly["wallet"].astype(str))
    ok = (hi >= 0) & (hi < n_hours) & (wi >= 0)
    m = np.zeros((n_hours, len(wallet_index)))
    act = np.zeros((n_hours, len(wallet_index)), dtype=bool)
    m[hi[ok], wi[ok]] = hourly[col].to_numpy(dtype="float64")[ok]
    act[hi[ok], wi[ok]] = True
    return m, act


def leader_ranks(pm_: np.ndarray, lookback: int, top: int = TOP_N) -> np.ndarray:
    """rank[H, w] in 1..top for the top wallets by P&L over hours [H - lookback, H); 0 otherwise."""
    n_h, n_w = pm_.shape
    csum = np.vstack([np.zeros((1, n_w)), np.cumsum(pm_, axis=0)])
    rank = np.zeros((n_h, n_w), dtype="int16")
    for h in range(lookback, n_h):
        s = csum[h] - csum[h - lookback]
        idx = np.argpartition(-s, top)[:top]
        idx = idx[np.argsort(-s[idx], kind="stable")]
        idx = idx[s[idx] > 0]
        rank[h, idx] = np.arange(1, len(idx) + 1)
    return rank


def insample_ranks(hourly: pd.DataFrame, wallet_index: pd.Index, t_start: pd.Timestamp, n_hours: int) -> np.ndarray:
    hi = ((pd.DatetimeIndex(hourly["hour"]) - t_start) // pd.Timedelta(hours=1)).to_numpy()
    wi = wallet_index.get_indexer(hourly["wallet"].astype(str))
    r = hourly["rank_pnl"].to_numpy()
    ok = (hi >= 0) & (hi < n_hours) & (wi >= 0) & (r <= TOP_N)
    rank = np.zeros((n_hours, len(wallet_index)), dtype="int16")
    rank[hi[ok], wi[ok]] = r[ok]
    return rank


# ---------------------------------------------------------------------------
# Q1: who wins
# ---------------------------------------------------------------------------
def q1_concentration(hourly: pd.DataFrame, n_boot: int) -> tuple[pd.DataFrame, dict]:
    rows = []
    for h, g in hourly.groupby("hour", sort=True):
        pnl_sorted = np.sort(g["pnl"].to_numpy())[::-1]
        pos_total = np.clip(pnl_sorted, 0, None).sum()
        n1 = max(1, int(math.ceil(0.01 * len(g))))
        rows.append({
            "hour": h,
            "n_wallets": len(g),
            "n_winners": int((g["pnl"] > 0).sum()),
            "pnl_positive_total": pos_total,
            "pnl_negative_total": float(np.clip(pnl_sorted, None, 0).sum()),
            "fees_total": float(g["fees"].sum()),
            "top10_pnl": float(pnl_sorted[:10].sum()),
            "top10_share_of_positive": float(np.clip(pnl_sorted[:10], 0, None).sum() / pos_total),
            "top1pct_n": n1,
            "top1pct_share_of_positive": float(np.clip(pnl_sorted[:n1], 0, None).sum() / pos_total),
            "top10_volume_share": float(g.nsmallest(10, "rank_pnl")["volume"].sum() / g["volume"].sum()),
        })
    c = pd.DataFrame(rows)
    cl = np.arange(len(c))
    summary = {}
    for col in ("top10_share_of_positive", "top1pct_share_of_positive", "top10_volume_share"):
        b = cluster_boot(c[col].to_numpy(), np.ones(len(c)), cl, n_boot=n_boot, seed=1)
        summary[col] = {"mean": b["est"], "lo": b["lo"], "hi": b["hi"], "median": float(c[col].median()),
                        "min": float(c[col].min()), "max": float(c[col].max()), "n_hours": len(c)}
    summary["share_winners_mean"] = float((c["n_winners"] / c["n_wallets"]).mean())
    return c, summary


def q1_top_tables(hourly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    cols = ["hour", "rank", "wallet", "name", "pseudonym", "pnl", "pnl_incl_rebate", "fees", "rebate_est", "volume",
            "n_markets", "n_trades", "win_rate", "taker_share", "first_t_rel_median", "t_rel_median", "assets"]
    by_pnl = hourly[hourly["rank_pnl"] <= TOP_N].copy()
    by_pnl["rank"] = by_pnl["rank_pnl"]
    h2 = hourly.copy()
    h2["rank"] = h2.groupby("hour")["volume"].rank(ascending=False, method="first").astype("int64")
    by_vol = h2[h2["rank"] <= TOP_N].copy()
    return (by_pnl.sort_values(["hour", "rank"])[cols].reset_index(drop=True),
            by_vol.sort_values(["hour", "rank"])[cols].reset_index(drop=True))


def q1_frequency(hourly: pd.DataFrame, pnl: pd.DataFrame) -> pd.DataFrame:
    """Wallets that appear in an hourly top 10 (by P&L): how often, and what they do over 48 h."""
    t10 = hourly[hourly["rank_pnl"] <= 10]
    f = t10.groupby("wallet").agg(n_hours_top10=("hour", "nunique"), pnl_in_top10_hours=("pnl", "sum"))
    act = hourly.groupby("wallet").agg(n_hours_active=("hour", "nunique"), pnl_48h=("pnl", "sum"),
                                       pnl_incl_rebate_48h=("pnl_incl_rebate", "sum"), volume_48h=("volume", "sum"),
                                       markets_per_active_hour=("n_markets", "mean"))
    tk = pnl.assign(tn=pnl["volume"] * pnl["taker_share"].fillna(0)).groupby("wallet")[["tn", "volume"]].sum()
    names = hourly.drop_duplicates("wallet").set_index("wallet")[["name", "pseudonym"]]
    out = f.join(act).join(names)
    out["taker_share_48h"] = (tk["tn"] / tk["volume"]).reindex(out.index)
    out["pnl_outside_top10_hours"] = out["pnl_48h"] - out["pnl_in_top10_hours"]
    return out.sort_values(["n_hours_top10", "pnl_48h"], ascending=False).reset_index()


def q1_leaderboard(pnl: pd.DataFrame, hourly: pd.DataFrame, t_end: pd.Timestamp, cache_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Overlap between the official crypto leaderboard (day) and our last 24 h of 5m/15m markets."""
    last = pnl[pd.to_datetime(pnl["window_start"], utc=True) >= t_end - pd.Timedelta(hours=24)]
    ours = last.groupby("wallet").agg(pnl=("pnl", "sum"), volume=("volume", "sum")).reset_index()
    ours["rank_pnl"] = ours["pnl"].rank(ascending=False, method="first")
    ours["rank_vol"] = ours["volume"].rank(ascending=False, method="first")
    hourly_top10 = set(hourly.loc[(hourly["rank_pnl"] <= 10) & (pd.to_datetime(hourly["hour"], utc=True) >= t_end - pd.Timedelta(hours=24)), "wallet"])
    rows, detail = [], []
    for order, our_rank, our_col in (("pnl", "rank_pnl", "pnl"), ("vol", "rank_vol", "volume")):
        files = sorted(cache_dir.glob(f"leaderboard_crypto_day_{order}_*.parquet"))
        if not files:
            continue
        lb = pd.read_parquet(files[-1])
        m = lb.merge(ours, on="wallet", how="left")
        present = m["pnl_y"].notna() if "pnl_y" in m.columns else m[our_col].notna()
        m = m.rename(columns={"pnl_x": "lb_pnl", "pnl_y": "our_pnl", "vol": "lb_vol", "volume": "our_volume"})
        for n_lb in (20, 50, 100):
            sub = m.head(n_lb)
            pres = sub["our_pnl"].notna()
            for n_ours in (20, 50, 100):
                rows.append({
                    "leaderboard": f"day_{order}", "snapshot": files[-1].stem.split("_")[-1],
                    "lb_top": n_lb, "our_top": n_ours, "our_ranking": our_col,
                    "n_present_in_our_trades": int(pres.sum()),
                    "n_in_our_top": int((sub[our_rank] <= n_ours).sum()),
                    "n_in_any_hourly_top10_last24h": int(sub["wallet"].isin(hourly_top10).sum()),
                    "spearman_lb_vs_ours": float(spearmanr(sub.loc[pres, "lb_" + ("pnl" if order == "pnl" else "vol")],
                                                           sub.loc[pres, "our_" + ("pnl" if order == "pnl" else "volume")]).statistic)
                    if pres.sum() >= 5 else math.nan,
                    "median_share_of_lb_value_in_our_markets": float(
                        (sub.loc[pres, "our_pnl"] / sub.loc[pres, "lb_pnl"]).median() if order == "pnl"
                        else (sub.loc[pres, "our_volume"] / sub.loc[pres, "lb_vol"].replace(0, np.nan)).median()
                    ) if pres.sum() else math.nan,
                })
        d = m.head(50)[["rank", "wallet", "user_name", "lb_pnl", "lb_vol", "our_pnl", "our_volume", "rank_pnl", "rank_vol"]].copy()
        d.insert(0, "leaderboard", f"day_{order}")
        detail.append(d)
    return pd.DataFrame(rows), (pd.concat(detail, ignore_index=True) if detail else pd.DataFrame())


# ---------------------------------------------------------------------------
# Q2: profile of the top wallets (descriptive, in-sample)
# ---------------------------------------------------------------------------
def q2_profile(a: dict, hourly: pd.DataFrame, is_rank: np.ndarray, pnl: pd.DataFrame, wallet_index: pd.Index,
               n_hours: int) -> dict[str, pd.DataFrame]:
    h_s = a["h_S"]
    okh = (h_s >= 0) & (h_s < n_hours)
    top = np.zeros(len(h_s), dtype=bool)
    top[okh] = (is_rank[h_s[okh], a["wallet"][okh]] >= 1) & (is_rank[h_s[okh], a["wallet"][okh]] <= 10)
    t_rel = a["t_rel"]
    dur = a["duration"]

    # timing: share of each group's fills per second around S, then per 5 s up to the window end
    zoom = np.arange(-60, 61)
    rows = []
    for name, g in (("top10_heure", top), ("autres", ~top)):
        tot = g.sum()
        cnt = np.bincount(t_rel[g & (t_rel >= -60) & (t_rel <= 60)] + 60, minlength=121)
        vol = np.bincount(t_rel[g & (t_rel >= -60) & (t_rel <= 60)] + 60,
                          weights=a["notional"][g & (t_rel >= -60) & (t_rel <= 60)], minlength=121)
        for s, n, v in zip(zoom, cnt, vol):
            rows.append({"group": name, "t_rel_s": int(s), "n_fills": int(n), "share_of_group_fills": n / tot,
                         "notional": v, "share_of_group_notional": v / a["notional"][g].sum()})
    timing_zoom = pd.DataFrame(rows)

    rows = []
    for d_name, d_len in (("5m", 300), ("15m", 900)):
        edges = np.arange(-60, d_len + 5, 5)
        for name, g in (("top10_heure", top), ("autres", ~top)):
            sel = g & (dur == d_name)
            tot = sel.sum()
            cnt, _ = np.histogram(t_rel[sel], bins=edges)
            for lo, n in zip(edges[:-1], cnt):
                rows.append({"duration": d_name, "group": name, "bin_start_s": int(lo), "n_fills": int(n),
                             "share_of_group_fills": n / tot})
    timing_window = pd.DataFrame(rows)

    # summary per group (fills and wallet-hours)
    hr = hourly.copy()
    hr["top10"] = hr["rank_pnl"] <= 10
    prof = []
    btc5 = (a["asset"] == "btc") & (dur == "5m")
    for name, g, hg in (("top10_heure", top, hr["top10"]), ("autres", ~top, ~hr["top10"])):
        buy = g & a["buy"]
        bp = a["price"][buy]
        bw = a["notional"][buy]
        h_ = hr[hg]
        vol = a["notional"][g]
        prof.append({
            "group": name,
            "n_wallet_hours": int(len(h_)),
            "n_wallets": int(h_["wallet"].nunique()),
            "n_fills": int(g.sum()),
            "pnl_per_wallet_hour_mean": float(h_["pnl"].mean()),
            "pnl_per_wallet_hour_median": float(h_["pnl"].median()),
            "volume_per_wallet_hour_median": float(h_["volume"].median()),
            "roi_pnl_over_volume": float(h_["pnl"].sum() / h_["volume"].sum()),
            "fees_over_volume": float(h_["fees"].sum() / h_["volume"].sum()),
            "win_rate_markets_median": float(h_["win_rate"].median()),
            "markets_per_wallet_hour_median": float(h_["n_markets"].median()),
            "share_wallet_hours_24plus_markets": float((h_["n_markets"] >= 24).mean()),
            "fills_per_wallet_hour_median": float(h_["n_trades"].median()),
            "taker_share_of_volume": float(a["notional"][g & a["taker"]].sum() / vol.sum()),
            "share_wallet_hours_maker_dominant": float((h_["taker_share"] < 0.2).mean()),
            "share_wallet_hours_taker_dominant": float((h_["taker_share"] > 0.8).mean()),
            "fill_usd_median": float(np.median(vol)),
            "fill_shares_median": float(np.median(a["size"][g])),
            "buy_price_vw_mean": float((bp * bw).sum() / bw.sum()),
            "buy_price_median": float(np.median(bp)),
            "buy_volume_share_price_ge_0.90": float(bw[bp >= 0.9].sum() / bw.sum()),
            "buy_volume_share_price_le_0.10": float(bw[bp <= 0.1].sum() / bw.sum()),
            "buy_volume_share_price_0.40_0.60": float(bw[(bp >= 0.4) & (bp <= 0.6)].sum() / bw.sum()),
            "share_fills_pre_open": float((t_rel[g] < 0).mean()),
            "share_fills_first_10s": float(((t_rel[g] >= 0) & (t_rel[g] < 10)).mean()),
            "share_fills_first_60s": float(((t_rel[g] >= 0) & (t_rel[g] < 60)).mean()),
            "share_fills_last_60s": float((a["E"][g] - a["ts"][g] <= 60).mean()),
            "taker_fills_share_5_40s": float(((t_rel[g & a["taker"]] >= 5) & (t_rel[g & a["taker"]] < 40)).mean()),
            "taker_fills_share_first_60s": float(((t_rel[g & a["taker"]] >= 0) & (t_rel[g & a["taker"]] < 60)).mean()),
            "taker_fills_share_5_40s_btc_5m": float(((t_rel[g & a["taker"] & btc5] >= 5) & (t_rel[g & a["taker"] & btc5] < 40)).mean()),
            "t_rel_median_s_5m": float(np.median(t_rel[g & (dur == "5m")])),
            "t_rel_median_s_15m": float(np.median(t_rel[g & (dur == "15m")])),
            "volume_share_btc": float(vol[a["asset"][g] == "btc"].sum() / vol.sum()),
            "volume_share_eth": float(vol[a["asset"][g] == "eth"].sum() / vol.sum()),
            "volume_share_sol": float(vol[a["asset"][g] == "sol"].sum() / vol.sum()),
            "volume_share_5m": float(vol[dur[g] == "5m"].sum() / vol.sum()),
        })
    profile = pd.DataFrame(prof)

    # 48 h top 20 wallets (by P&L): one row each
    w48 = pnl.groupby("wallet").agg(
        pnl=("pnl", "sum"), pnl_incl_rebate=("pnl_incl_rebate", "sum"), fees=("fees", "sum"),
        rebate_est=("rebate_est", "sum"), volume=("volume", "sum"), n_markets=("condition_id", "nunique"),
        n_fills=("n_trades", "sum"), wins=("pnl", lambda s: int((s > 0).sum())),
    ).sort_values("pnl", ascending=False)
    top20 = w48.head(TOP_N).copy()
    names = hourly.drop_duplicates("wallet").set_index("wallet")[["name", "pseudonym"]]
    hr_act = hourly.groupby("wallet").agg(hours_active=("hour", "nunique"),
                                          hours_in_top10=("rank_pnl", lambda s: int((s <= 10).sum())),
                                          markets_per_active_hour=("n_markets", "mean"),
                                          first_t_rel_median=("first_t_rel_median", "median"))
    top20 = top20.join(names).join(hr_act)
    wi = wallet_index.get_indexer(top20.index.astype(str))
    extra = []
    for w_code in wi:
        g = a["wallet"] == w_code
        v = a["notional"][g]
        b = g & a["buy"]
        extra.append({
            "taker_share": a["notional"][g & a["taker"]].sum() / v.sum(),
            "fill_usd_median": float(np.median(v)),
            "t_rel_median_s": float(np.median(t_rel[g])),
            "share_fills_pre_open": float((t_rel[g] < 0).mean()),
            "buy_price_vw_mean": float((a["price"][b] * a["notional"][b]).sum() / a["notional"][b].sum()) if b.any() else math.nan,
            "volume_share_btc": float(v[a["asset"][g] == "btc"].sum() / v.sum()),
            "volume_share_5m": float(v[dur[g] == "5m"].sum() / v.sum()),
        })
    top20 = pd.concat([top20.reset_index(), pd.DataFrame(extra)], axis=1)
    top20["win_rate_markets"] = top20["wins"] / top20["n_markets"]
    top20["profile"] = np.where(top20["taker_share"] < 0.2, "maker",
                                np.where(top20["taker_share"] > 0.8, "preneur", "mixte"))
    top20["bot_like"] = (top20["markets_per_active_hour"] >= 12) & (top20["hours_active"] >= 24)
    top20.insert(0, "rank_48h", np.arange(1, len(top20) + 1))

    # markets per wallet-hour distribution (bots?)
    bins = [1, 2, 3, 6, 11, 25, 37, 49]      # integer ranges 1, 2, 3-5, 6-10, 11-24, 25-36, 37-48 (48 markets per hour)
    mk = []
    for name, hg in (("top10_heure", hr["top10"]), ("autres", ~hr["top10"])):
        cnt, _ = np.histogram(hr.loc[hg, "n_markets"], bins=bins)
        for lo, hi_, n in zip(bins[:-1], bins[1:], cnt):
            mk.append({"group": name, "n_markets_from": lo, "n_markets_to": hi_ - 1, "n_wallet_hours": int(n),
                       "share": n / hg.sum()})
    return {"timing_zoom": timing_zoom, "timing_window": timing_window, "profile": profile, "top20_48h": top20,
            "markets_per_hour": pd.DataFrame(mk)}


# ---------------------------------------------------------------------------
# Q3: persistence
# ---------------------------------------------------------------------------
def q3_persistence(pm_: np.ndarray, act: np.ndarray, vol: np.ndarray, is_rank: np.ndarray, hours: pd.DatetimeIndex,
                   n_boot: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n_h = pm_.shape[0]
    rows, per_pair = [], []
    for k in range(1, 13):
        rs, rs_roi, ns = [], [], []
        for h in range(n_h - k):
            both = act[h] & act[h + k]
            n = int(both.sum())
            if n < 30:
                continue
            r = spearmanr(pm_[h, both], pm_[h + k, both]).statistic
            roi_a = pm_[h, both] / np.maximum(vol[h, both], 1e-9)
            roi_b = pm_[h + k, both] / np.maximum(vol[h + k, both], 1e-9)
            r2 = spearmanr(roi_a, roi_b).statistic
            rs.append(r)
            rs_roi.append(r2)
            ns.append(n)
            per_pair.append({"k": k, "hour": hours[h], "n_wallets_both": n, "spearman_pnl": r, "spearman_roi": r2})
        m, lo, hi = block_boot_mean(np.array(rs), n_boot=n_boot, seed=k)
        m2, lo2, hi2 = block_boot_mean(np.array(rs_roi), n_boot=n_boot, seed=100 + k)
        rows.append({"k_hours": k, "n_hour_pairs": len(rs), "median_n_wallets": float(np.median(ns)),
                     "spearman_pnl_mean": m, "lo": lo, "hi": hi,
                     "spearman_roi_mean": m2, "roi_lo": lo2, "roi_hi": hi2,
                     "share_pairs_positive": float(np.mean(np.array(rs) > 0))})
    decay = pd.DataFrame(rows)

    # top 10 of hour h -> hour h + k
    out = []
    for k in (1, 2, 6):
        recs = []
        for h in range(n_h - k):
            t10 = (is_rank[h] >= 1) & (is_rank[h] <= 10)
            rest = act[h] & ~t10
            for name, g in (("top10", t10), ("reste", rest)):
                nxt = g & act[h + k]
                recs.append({"hour": h, "group": name, "n": int(g.sum()), "n_active_next": int(nxt.sum()),
                             "pnl_next_sum": float(pm_[h + k, nxt].sum()),
                             "n_positive_next": int((pm_[h + k, nxt] > 0).sum()),
                             "n_top10_next": int(((is_rank[h + k] >= 1) & (is_rank[h + k] <= 10) & g).sum()),
                             "vol_next_sum": float(vol[h + k, nxt].sum())})
        r = pd.DataFrame(recs)
        for name in ("top10", "reste"):
            s = r[r["group"] == name]
            cl = s["hour"].to_numpy()
            b_pnl = cluster_boot(s["pnl_next_sum"], s["n_active_next"], cl, n_boot=n_boot, seed=7)
            b_pos = cluster_boot(s["n_positive_next"], s["n_active_next"], cl, n_boot=n_boot, seed=8)
            b_act = cluster_boot(s["n_active_next"], s["n"], cl, n_boot=n_boot, seed=9)
            b_top = cluster_boot(s["n_top10_next"], s["n"], cl, n_boot=n_boot, seed=10)
            b_roi = cluster_boot(s["pnl_next_sum"], s["vol_next_sum"], cl, n_boot=n_boot, seed=11)
            out.append({
                "k_hours": k, "group": name, "n_wallet_hours": int(s["n"].sum()), "n_active_next": int(s["n_active_next"].sum()),
                "share_active_next": b_act["est"], "share_active_next_lo": b_act["lo"], "share_active_next_hi": b_act["hi"],
                "share_top10_next": b_top["est"], "share_top10_next_lo": b_top["lo"], "share_top10_next_hi": b_top["hi"],
                "mean_pnl_next": b_pnl["est"], "mean_pnl_next_lo": b_pnl["lo"], "mean_pnl_next_hi": b_pnl["hi"],
                "share_positive_next": b_pos["est"], "share_positive_next_lo": b_pos["lo"], "share_positive_next_hi": b_pos["hi"],
                "roi_next": b_roi["est"], "roi_next_lo": b_roi["lo"], "roi_next_hi": b_roi["hi"],
            })
    return decay, pd.DataFrame(out), pd.DataFrame(per_pair)


# ---------------------------------------------------------------------------
# Q4: copy trading
# ---------------------------------------------------------------------------
def build_tape(a: dict, t0: int) -> dict[str, np.ndarray]:
    """Taker BUY fills per token, sorted by (token, block second, on-chain seq); also the best maker
    level of each such fill (lowest price available to a small buyer in that transaction)."""
    assert a["seq"].max() < 2**20 and (a["ts"].max() - t0 + 200) < 2**19 and a["token"].max() < 2**23
    sel = a["taker"] & a["buy"]
    tok, ts, seq = a["token"][sel], a["ts"][sel], a["seq"][sel]
    key = (tok << 39) | ((ts - t0) << 20) | seq
    order = np.argsort(key, kind="stable")
    price = a["price"][sel][order]
    size = a["size"][sel][order]
    tape = {"key": key[order], "tok": tok[order], "ts": ts[order], "price": price, "size": size,
            "cum_n": np.concatenate([[0.0], np.cumsum(price * size)]), "cum_s": np.concatenate([[0.0], np.cumsum(size)])}
    # best level: makers of a taker BUY tx sell the same token (price p) or buy the other one (1 - q)
    tx = a["tx_code"]
    taker_tok = pd.Series(a["token"][a["taker"]], index=tx[a["taker"]])
    taker_tok = taker_tok[~taker_tok.index.duplicated()]
    mk = ~a["taker"]
    tt = taker_tok.reindex(tx[mk]).to_numpy()
    conv = np.where(a["token"][mk] == tt, a["price"][mk], 1.0 - a["price"][mk])
    best = pd.Series(conv).groupby(tx[mk]).min()
    b = best.reindex(tx[sel]).to_numpy()[order]
    tape["best"] = np.where(np.isnan(b), price, np.minimum(b, price))
    return tape


def copy_events(a: dict) -> pd.DataFrame:
    """BUY fills merged by (wallet, token, block second)."""
    sel = a["buy"]
    df = pd.DataFrame({
        "wallet": a["wallet"][sel], "token": a["token"][sel], "ts": a["ts"][sel], "size": a["size"][sel],
        "notional": a["notional"][sel], "fee": a["fee"][sel], "seq": a["seq"][sel],
        "taker_size": a["size"][sel] * a["taker"][sel], "S": a["S"][sel], "E": a["E"][sel], "won": a["won"][sel],
        "slot": a["slot"][sel], "h_S": a["h_S"][sel], "cond": a["cond"][sel],
    })
    ev = df.groupby(["wallet", "token", "ts"], sort=False).agg(
        size=("size", "sum"), notional=("notional", "sum"), fee=("fee", "sum"), seq_max=("seq", "max"),
        taker_size=("taker_size", "sum"), S=("S", "first"), E=("E", "first"), won=("won", "first"),
        slot=("slot", "first"), h_S=("h_S", "first"), cond=("cond", "first"), n_fills=("size", "size"),
    ).reset_index()
    ev["leader_price"] = ev["notional"] / ev["size"]
    ev["leader_pnl_share"] = ev["won"].astype(float) - ev["leader_price"] - ev["fee"] / ev["size"]
    return ev


def simulate_fills(ev: pd.DataFrame, tape: dict, t0: int) -> dict[int, dict[str, np.ndarray]]:
    tok = ev["token"].to_numpy().astype("int64")
    ts = ev["ts"].to_numpy().astype("int64")
    seq_max = ev["seq_max"].to_numpy().astype("int64")
    key, n = tape["key"], len(tape["key"])
    res = {}
    for d in DELAYS:
        lo_key = (tok << 39) | ((ts + d - t0) << 20) | (seq_max + 1 if d == 0 else 0)
        i = np.searchsorted(key, lo_key, side="left")
        ic = np.minimum(i, n - 1)
        ok = (i < n) & (tape["tok"][ic] == tok) & (tape["ts"][ic] <= ts + d + MAX_WAIT_S)
        first = np.where(ok, tape["price"][ic], np.nan)
        best = np.where(ok, tape["best"][ic], np.nan)
        hi_key = (tok << 39) | ((ts + d + VWAP_SPAN_S + 1 - t0) << 20)
        j = np.searchsorted(key, hi_key, side="left")
        has_win = ok & (j > i)
        with np.errstate(invalid="ignore", divide="ignore"):
            vw = (tape["cum_n"][j] - tape["cum_n"][i]) / (tape["cum_s"][j] - tape["cum_s"][i])
        vwap = np.where(has_win, vw, first)
        wait = np.where(ok, tape["ts"][ic] - (ts + d), np.nan)
        res[d] = {"ok": ok, "first": first, "vwap": vwap, "best": best, "wait": wait}
    return res


def q4_copy(ev: pd.DataFrame, fills: dict, ranks: dict[int, np.ndarray], is_rank: np.ndarray, t_start: int,
            n_hours: int, n_boot: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    h_dec = (ev["ts"].to_numpy() - SAFETY_S - t_start) // 3600
    in_eval = (h_dec >= WARMUP_H) & (h_dec < n_hours)
    hd = np.clip(h_dec, 0, n_hours - 1)
    w = ev["wallet"].to_numpy()
    won = ev["won"].to_numpy().astype(float)
    slot = ev["slot"].to_numpy()
    size = ev["size"].to_numpy()
    groups: list[tuple[str, int, int, np.ndarray]] = []
    for L in LOOKBACKS:
        r = ranks[L][hd, w]
        for K in KS:
            groups.append(("hors_echantillon", K, L, in_eval & (r >= 1) & (r <= K)))
    r1 = ranks[1][hd, w]
    groups.append(("temoin_autres_wallets", 0, 1, in_eval & (r1 == 0)))
    hs = np.clip(ev["h_S"].to_numpy(), 0, n_hours - 1)
    ok_hs = (ev["h_S"].to_numpy() >= 0) & (ev["h_S"].to_numpy() < n_hours)
    ris = np.where(ok_hs, is_rank[hs, w], 0)
    groups.append(("in_sample_fuite", 10, 0, in_eval & (ris >= 1) & (ris <= 10)))

    n_cells = len([g for g in groups if g[0] == "hors_echantillon"]) * len(DELAYS) * 2
    z_bonf = float(norm.ppf(1 - 0.05 / (2 * n_cells)))
    rows = []
    for name, K, L, mask in groups:
        lp = ev["leader_pnl_share"].to_numpy()
        lb = cluster_boot(lp[mask], np.ones(mask.sum()), slot[mask], n_boot=n_boot, seed=3)
        for d in DELAYS:
            f = fills[d]
            ok = mask & f["ok"]
            for est in ("first", "vwap", "best"):
                p = f[est][ok]
                gross = won[ok] - p
                pnl_s = gross - fee_per_share(p)
                cost = p + fee_per_share(p)
                b = cluster_boot(pnl_s, np.ones(len(p)), slot[ok], n_boot=n_boot, seed=d * 7 + len(est))
                roi = cluster_boot(pnl_s, cost, slot[ok], n_boot=n_boot, seed=d * 7 + len(est))
                bw = cluster_boot(pnl_s * size[ok], size[ok], slot[ok], n_boot=n_boot, seed=d * 11 + len(est))
                lo_same = cluster_boot(lp[ok], np.ones(ok.sum()), slot[ok], n_boot=n_boot, seed=5)
                rows.append({
                    "group": name, "K": K, "lookback_h": L, "delay_s": d, "price_estimator": est,
                    "n_events": int(mask.sum()), "n_filled": int(ok.sum()), "n_discarded_no_trade_10s": int((mask & ~f["ok"]).sum()),
                    "n_wallets": int(len(np.unique(w[ok]))), "n_slots": b["n_clusters"],
                    "mean_price": float(p.mean()) if len(p) else math.nan,
                    "mean_wait_s": float(np.nanmean(f["wait"][ok])) if ok.any() else math.nan,
                    "win_rate": float(won[ok].mean()) if ok.any() else math.nan,
                    "pnl_gross_per_share": float(gross.mean()) if len(gross) else math.nan,
                    "mean_fee_per_share": float(fee_per_share(p).mean()) if len(p) else math.nan,
                    "pnl_per_share": b["est"], "pnl_lo": b["lo"], "pnl_hi": b["hi"], "pnl_se": b["se"],
                    "pnl_bonf_lo": b["est"] - z_bonf * b["se"], "pnl_bonf_hi": b["est"] + z_bonf * b["se"],
                    "roi": roi["est"], "roi_lo": roi["lo"], "roi_hi": roi["hi"],
                    "pnl_per_share_size_weighted": bw["est"], "pnl_sw_lo": bw["lo"], "pnl_sw_hi": bw["hi"],
                    "leader_own_pnl_per_share_same_events": lo_same["est"], "leader_own_lo": lo_same["lo"], "leader_own_hi": lo_same["hi"],
                    "leader_own_pnl_per_share_all_events": lb["est"],
                    "leader_price_mean": float(ev["leader_price"].to_numpy()[ok].mean()) if ok.any() else math.nan,
                    "leader_taker_share": float(ev["taker_size"].to_numpy()[ok].sum() / size[ok].sum()) if ok.any() else math.nan,
                })
    tab = pd.DataFrame(rows)
    tab.attrs["z_bonf"] = z_bonf
    tab.attrs["n_cells"] = n_cells

    # breakdown of the main cell (top 10, 1 h lookback) by market, leader role, price and timing
    main = groups[[g[:3] for g in groups].index(("hors_echantillon", 10, 1))][3]
    t_rel = ev["ts"].to_numpy() - ev["S"].to_numpy()
    to_end = ev["E"].to_numpy() - ev["ts"].to_numpy()
    lpx = ev["leader_price"].to_numpy()
    role_t = ev["taker_size"].to_numpy() / size > 0.5
    splits = {
        "actif": ev["asset"].to_numpy(), "duree": ev["duration"].to_numpy(),
        "role_du_leader": np.where(role_t, "preneur", "maker"),
        "prix_du_leader": np.select([lpx < 0.3, lpx <= 0.7], ["< 0,30", "0,30–0,70"], "> 0,70"),
        "moment": np.select([t_rel < 0, t_rel < 60, to_end > 60], ["avant S", "S à S+60 s", "milieu"], "dernière minute"),
    }
    det = []
    for d in (0, 3, 10):
        f = fills[d]
        for dim, lab in splits.items():
            for v in sorted(set(lab[main])):
                ok = main & f["ok"] & (lab == v)
                if ok.sum() < 30:
                    continue
                p = f["first"][ok]
                pnl_s = won[ok] - p - fee_per_share(p)
                b = cluster_boot(pnl_s, np.ones(len(p)), slot[ok], n_boot=n_boot, seed=40 + d)
                lo_ = cluster_boot(ev["leader_pnl_share"].to_numpy()[ok], np.ones(ok.sum()), slot[ok], n_boot=n_boot, seed=41)
                det.append({"K": 10, "lookback_h": 1, "delay_s": d, "dimension": dim, "value": v, "n_filled": int(ok.sum()),
                            "share_of_events": float(ok.sum() / (main & f["ok"]).sum()), "mean_price": float(p.mean()),
                            "win_rate": float(won[ok].mean()), "pnl_per_share": b["est"], "pnl_lo": b["lo"], "pnl_hi": b["hi"],
                            "leader_own_pnl_per_share": lo_["est"], "leader_own_lo": lo_["lo"], "leader_own_hi": lo_["hi"]})
    return tab, pd.DataFrame(det)


def q4_leaders_hourly(pm_: np.ndarray, act: np.ndarray, vol: np.ndarray, ranks: dict[int, np.ndarray], n_boot: int) -> pd.DataFrame:
    """Realised P&L, in the evaluation hour H, of the leaders chosen on [H - L, H) vs the other active wallets."""
    n_h = pm_.shape[0]
    rows = []
    for L in LOOKBACKS:
        for K in KS + (0,):
            recs = []
            for h in range(WARMUP_H, n_h):
                r = ranks[L][h]
                g = (r >= 1) & (r <= K) if K else (r == 0)
                a_ = g & act[h]
                recs.append((h, int(g.sum()), int(a_.sum()), float(pm_[h, a_].sum()), int((pm_[h, a_] > 0).sum()),
                             float(vol[h, a_].sum())))
            r = pd.DataFrame(recs, columns=["h", "n", "n_active", "pnl", "n_pos", "vol"])
            b = cluster_boot(r["pnl"], r["n_active"], r["h"], n_boot=n_boot, seed=21)
            bp = cluster_boot(r["n_pos"], r["n_active"], r["h"], n_boot=n_boot, seed=22)
            ba = cluster_boot(r["n_active"], r["n"], r["h"], n_boot=n_boot, seed=23)
            br = cluster_boot(r["pnl"], r["vol"], r["h"], n_boot=n_boot, seed=24)
            rows.append({"lookback_h": L, "K": K if K else "autres", "n_hours": len(r), "n_leader_hours": int(r["n"].sum()),
                         "share_active": ba["est"], "share_active_lo": ba["lo"], "share_active_hi": ba["hi"],
                         "mean_pnl_active": b["est"], "mean_pnl_lo": b["lo"], "mean_pnl_hi": b["hi"],
                         "share_positive": bp["est"], "share_positive_lo": bp["lo"], "share_positive_hi": bp["hi"],
                         "roi": br["est"], "roi_lo": br["lo"], "roi_hi": br["hi"]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Q5: leaders' order flow as a signal
# ---------------------------------------------------------------------------
def q5_flow(a: dict, markets: pd.DataFrame, trades: pd.DataFrame, ranks: dict[int, np.ndarray], t_start: int,
            n_hours: int, n_boot: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    cats = trades["condition_id"].cat.categories.astype(str)
    mk = markets.copy()
    mk["cond"] = pd.Index(cats).get_indexer(mk["condition_id"].astype(str))
    mk = mk[mk["cond"] >= 0]
    mk["S"] = pd.DatetimeIndex(mk["start"]).as_unit("s").asi8
    mk["slot"] = mk["S"] // SLOT_S
    h_first = (mk["S"] + min(FLOW_CUTS) - SAFETY_S - t_start) // 3600
    mk = mk[(h_first >= WARMUP_H) & (h_first < n_hours)].reset_index(drop=True)
    y = mk["resolved_up"].astype(bool).to_numpy()
    n_mk = int(mk["cond"].max()) + 1 if len(mk) else 0
    t_rel = a["t_rel"]
    signed = np.where(a["buy"], 1.0, -1.0) * np.where(a["up"], 1.0, -1.0) * a["notional"]   # $ towards Up

    rows, per_market = [], []
    t_all0 = mk["S"].min()
    blocks = np.minimum(((mk["S"] - t_all0) * N_CV_BLOCKS) // (mk["S"].max() - t_all0 + 1), N_CV_BLOCKS - 1).to_numpy()
    for c in FLOW_CUTS:
        # market price (Up) at S + c: VWAP of taker fills in (S + c - 10, S + c], else last taker fill <= S + c
        sel = a["taker"] & (t_rel <= c) & (t_rel > c - PRICE_LOOKBACK_S)
        num = np.bincount(a["cond"][sel], weights=a["up_price"][sel] * a["size"][sel], minlength=n_mk)
        den = np.bincount(a["cond"][sel], weights=a["size"][sel], minlength=n_mk)
        sel2 = a["taker"] & (t_rel <= c) & (t_rel > -300)
        last = pd.DataFrame({"cond": a["cond"][sel2], "ts": a["ts"][sel2], "seq": a["seq"][sel2], "p": a["up_price"][sel2]})
        last = last.sort_values(["cond", "ts", "seq"]).groupby("cond")["p"].last()
        with np.errstate(invalid="ignore", divide="ignore"):
            p_vw = num / den
        cond = mk["cond"].to_numpy()
        p_mkt = np.where(den[cond] > 0, p_vw[cond], last.reindex(cond).to_numpy())
        h_dec = ((mk["S"] + c - SAFETY_S - t_start) // 3600).to_numpy()
        # leaders' rows up to S + c
        sel3 = t_rel <= c
        rc = a["cond"][sel3]
        pos = pd.Series(np.arange(len(mk)), index=cond).reindex(rc).to_numpy()
        okp = ~np.isnan(pos)
        rows_idx = np.where(sel3)[0][okp]
        mpos = pos[okp].astype("int64")
        hrow = h_dec[mpos]
        wrow = a["wallet"][rows_idx]
        for L in LOOKBACKS:
            rk = ranks[L][np.clip(hrow, 0, n_hours - 1), wrow]
            for K in KS:
                g = (rk >= 1) & (rk <= K)
                flow = np.bincount(mpos[g], weights=signed[rows_idx][g], minlength=len(mk))
                gross = np.bincount(mpos[g], weights=a["notional"][rows_idx][g], minlength=len(mk))
                has = (gross > 0) & ~np.isnan(p_mkt) & (flow != 0)
                cover = float(has.mean())
                f, p, yy, sl, bl = flow[has], p_mkt[has], y[has], mk["slot"].to_numpy()[has], blocks[has]
                nz = f != 0
                acc_flow = float(((f > 0) == yy)[nz].mean())
                acc_mkt = float(((p >= 0.5) == yy).mean())
                auc_f, auc_p = auc(f, yy), auc(p, yy)
                # cluster bootstrap for AUCs and accuracy difference
                uniq, inv = np.unique(sl, return_inverse=True)
                members = [np.where(inv == k)[0] for k in range(len(uniq))]
                rng = np.random.default_rng(c + 10 * K + L)
                boots = []
                for _ in range(max(200, n_boot // 4)):
                    pick = rng.integers(0, len(uniq), len(uniq))
                    idx = np.concatenate([members[k] for k in pick])
                    boots.append((auc(f[idx], yy[idx]), auc(p[idx], yy[idx]),
                                  np.mean((f[idx] > 0) == yy[idx]) - np.mean((p[idx] >= 0.5) == yy[idx])))
                boots = np.array(boots)
                q = np.nanpercentile(boots, [2.5, 97.5], axis=0)
                # Brier: market price vs cross-validated logistic models (blocked by time)
                lp = np.log(np.clip(p, 0.01, 0.99) / (1 - np.clip(p, 0.01, 0.99)))
                ff = np.arcsinh(f / 100.0)
                pred_a = np.full(len(p), np.nan)
                pred_b = np.full(len(p), np.nan)
                pred_c = np.full(len(p), np.nan)
                for blk in range(N_CV_BLOCKS):
                    tr_, te = bl != blk, bl == blk
                    if te.sum() == 0 or tr_.sum() < 50:
                        continue
                    pred_a[te] = logit_predict(logit_fit(lp[tr_, None], yy[tr_].astype(float)), lp[te, None])
                    xb = np.column_stack([lp, ff])
                    pred_b[te] = logit_predict(logit_fit(xb[tr_], yy[tr_].astype(float)), xb[te])
                    pred_c[te] = logit_predict(logit_fit(ff[tr_, None], yy[tr_].astype(float)), ff[te, None])
                ok = ~np.isnan(pred_b)
                br_mkt = (p - yy) ** 2
                br_a = (pred_a - yy) ** 2
                br_b = (pred_b - yy) ** 2
                br_c = (pred_c - yy) ** 2
                d_ba = cluster_boot((br_b - br_a)[ok], np.ones(ok.sum()), sl[ok], n_boot=n_boot, seed=31)
                d_bm = cluster_boot((br_b - br_mkt)[ok], np.ones(ok.sum()), sl[ok], n_boot=n_boot, seed=32)
                dis = nz & ((f > 0) != (p >= 0.5))
                k_dis = int(((f > 0) == yy)[dis].sum())
                lo_dis, hi_dis = wilson(k_dis, int(dis.sum()))
                rows.append({
                    "cut_s": c, "K": K, "lookback_h": L, "n_windows_eval": int(len(mk)), "n_windows_with_leader_flow": int(has.sum()),
                    "coverage": cover, "n_flow_nonzero": int(nz.sum()), "n_slots": len(uniq),
                    "accuracy_flow_sign": acc_flow, "accuracy_market": acc_mkt,
                    "accuracy_diff": acc_flow - acc_mkt,
                    "accuracy_diff_lo": q[0, 2], "accuracy_diff_hi": q[1, 2],
                    "auc_flow": auc_f, "auc_flow_lo": q[0, 0], "auc_flow_hi": q[1, 0],
                    "auc_market": auc_p, "auc_market_lo": q[0, 1], "auc_market_hi": q[1, 1],
                    "brier_market": float(br_mkt[ok].mean()), "brier_market_recal_cv": float(br_a[ok].mean()),
                    "brier_market_plus_flow_cv": float(br_b[ok].mean()), "brier_flow_only_cv": float(br_c[ok].mean()),
                    "delta_brier_flow_vs_recal": d_ba["est"], "delta_brier_lo": d_ba["lo"], "delta_brier_hi": d_ba["hi"],
                    "delta_brier_flow_vs_market": d_bm["est"], "delta_brier_vs_market_lo": d_bm["lo"], "delta_brier_vs_market_hi": d_bm["hi"],
                    "n_disagree": int(dis.sum()), "accuracy_flow_when_disagree": k_dis / dis.sum() if dis.sum() else math.nan,
                    "disagree_lo": lo_dis, "disagree_hi": hi_dis,
                    "median_abs_flow_usd": float(np.median(np.abs(f))),
                })
                if K == 10 and L == 1:
                    per_market.append(pd.DataFrame({"cut_s": c, "slug": mk["slug"].to_numpy()[has], "flow_usd": f,
                                                    "p_market": p, "resolved_up": yy}))
    return pd.DataFrame(rows), (pd.concat(per_market, ignore_index=True) if per_market else pd.DataFrame())


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def fmt_period(t_start: pd.Timestamp, t_end: pd.Timestamp) -> str:
    return f"fenêtres du {t_start:%d/%m/%Y %H:%M} au {t_end:%d/%m/%Y %H:%M} UTC"


def chart_concentration(conc: pd.DataFrame, summary: dict, period: str, path: Path) -> None:
    import matplotlib.dates as mdates

    fig, ax = new_fig()
    x = pd.to_datetime(conc["hour"])
    n1 = int(round(conc["top1pct_n"].median()))
    ax.plot(x, conc["top1pct_share_of_positive"], color=BLUE, lw=2, label=f"top 1 % des wallets de l'heure (≈ {n1} wallets)")
    ax.plot(x, conc["top10_share_of_positive"], color=ORANGE, lw=2, label="top 10 wallets de l'heure")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(pct_fmt())
    ax.set_ylabel("part du P&L positif de l'heure")
    ax.set_xlabel("heure UTC du début de fenêtre S")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %Hh"))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
    t10 = summary["top10_share_of_positive"]
    t1 = summary["top1pct_share_of_positive"]
    note = (f"{SOURCE} ; {period}.\n"
            f"n = {len(conc)} heures, {fnum(conc['n_wallets'].mean())} wallets actifs par heure en moyenne. Moyenne horaire : "
            f"top 10 = {t10['mean']*100:.0f} % (IC 95 % {t10['lo']*100:.0f}–{t10['hi']*100:.0f} %), top 1 % = "
            f"{t1['mean']*100:.0f} % ({t1['lo']*100:.0f}–{t1['hi']*100:.0f} %) ; bootstrap par heure.")
    title = f"Chaque heure, ~{n1} wallets captent {t1['mean']*100:.0f} % des gains"
    finish(fig, ax, title, note, path, legend_loc="upper right")


def chart_timing_zoom(tz: pd.DataFrame, period: str, path: Path, bin_s: int = 3) -> None:
    fig, ax = new_fig()
    for name, color, label in (("autres", BLUE, "autres wallets"), ("top10_heure", ORANGE, "top 10 de l'heure (P&L, in-sample)")):
        s = tz[tz["group"] == name].copy()
        s["bin"] = (s["t_rel_s"] + 60) // bin_s * bin_s - 60
        b = s.groupby("bin")["share_of_group_fills"].sum()
        b = b[b.index + bin_s <= 61]
        edges = np.append(b.index.to_numpy(), b.index[-1] + bin_s)
        ax.stairs(b.to_numpy(), edges, color=color, lw=2, label=label, baseline=None)
    ax.axvline(0, color=INK2, lw=0.8)
    ax.set_xlim(-60, 60)
    ax.set_ylim(0, None)
    ax.text(-1.5, ax.get_ylim()[1] * 0.55, "ouverture S", color=INK2, fontsize=8, va="center", ha="right")
    ax.yaxis.set_major_formatter(pct_fmt(1))
    ax.set_xlabel(f"secondes depuis l'ouverture S (horodatage du bloc, tranches de {bin_s} s)")
    ax.set_ylabel(f"part des exécutions du groupe, par {bin_s} s")
    n_top = int(tz.loc[tz["group"] == "top10_heure", "n_fills"].sum())
    n_oth = int(tz.loc[tz["group"] == "autres", "n_fills"].sum())
    note = (f"{SOURCE} ; {period}.\n"
            f"Exécutions (preneur et maker) entre S−60 s et S+60 s : top 10 de l'heure n = {fnum(n_top)}, autres n = {fnum(n_oth)} ; "
            "chaque courbe est rapportée au total des exécutions du groupe sur toute la fenêtre. Polygon produit un bloc toutes les "
            "1,5 s : à la seconde, une seconde sur trois n'a pas de bloc, d'où des tranches de 3 s (le détail à la seconde est dans "
            "timing_par_seconde.csv).")
    finish(fig, ax, "Les top wallets ne tradent pas plus tôt que les autres autour de l'ouverture", note, path,
           legend_kw={"loc": "lower left", "bbox_to_anchor": (0.0, 1.0), "ncol": 2})


def chart_timing_window(tw: pd.DataFrame, duration: str, period: str, path: Path) -> None:
    fig, ax = new_fig()
    bin_s = 5 if duration == "5m" else 10
    for name, color, label in (("autres", BLUE, "autres wallets"), ("top10_heure", ORANGE, "top 10 de l'heure (P&L, in-sample)")):
        s = tw[(tw["group"] == name) & (tw["duration"] == duration)].copy()
        s["bin"] = (s["bin_start_s"] + 60) // bin_s * bin_s - 60
        b = s.groupby("bin")["share_of_group_fills"].sum()
        edges = np.append(b.index.to_numpy(), b.index[-1] + bin_s)
        ax.stairs(b.to_numpy(), edges, color=color, lw=1.6, label=label, baseline=None)
    ax.axvline(0, color=INK2, lw=0.8)
    end = 300 if duration == "5m" else 900
    ax.set_xlim(-60, end)
    ax.set_ylim(0, None)
    ax.yaxis.set_major_formatter(pct_fmt(1))
    ax.set_xlabel(f"secondes depuis l'ouverture S (tranches de {bin_s} s)")
    ax.set_ylabel(f"part des exécutions du groupe, par {bin_s} s")
    n_top = int(tw.loc[(tw["group"] == "top10_heure") & (tw["duration"] == duration), "n_fills"].sum())
    n_oth = int(tw.loc[(tw["group"] == "autres") & (tw["duration"] == duration), "n_fills"].sum())
    note = (f"{SOURCE} ; {period}.\n"
            f"Marchés {duration} seulement ; exécutions de S−60 s à la fin de la fenêtre : top 10 de l'heure n = {fnum(n_top)}, "
            f"autres n = {fnum(n_oth)}.")
    title = {"5m": "5 min : profils quasi identiques, l'activité retombe dans les 50 dernières secondes",
             "15m": "15 min : profils proches, pics à l'ouverture et dans les 5 dernières minutes"}[duration]
    finish(fig, ax, title, note, path, legend_kw={"loc": "lower left", "bbox_to_anchor": (0.0, 1.0), "ncol": 2})


def chart_persistence(decay: pd.DataFrame, period: str, path: Path) -> None:
    fig, ax = new_fig()
    d = decay[decay["k_hours"] <= 6]
    for col, lo, hi, color, label in (
        ("spearman_pnl_mean", "lo", "hi", BLUE, "P&L de l'heure"),
        ("spearman_roi_mean", "roi_lo", "roi_hi", ORANGE, "P&L rapporté au volume de l'heure"),
    ):
        ax.fill_between(d["k_hours"], d[lo], d[hi], color=color, alpha=0.12, lw=0)
        ax.plot(d["k_hours"], d[col], color=color, lw=2, marker="o", ms=5, mec=BG, mew=1.5, label=label)
        first = d.iloc[0]
        ax.annotate(f"{first[col]:.2f}".replace(".", ","), (first["k_hours"], first[col]), textcoords="offset points",
                    xytext=(-14, 0), ha="right", va="center", color=INK, fontsize=8.5)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xlabel("écart entre les deux heures comparées (h → h+k), en heures")
    ax.set_ylabel("corrélation de rang (Spearman)")
    ax.set_ylim(-0.05, 0.25)
    ax.set_xlim(0.5, 6.3)
    ax.set_xticks(range(1, 7))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}".replace(".", ",")))
    note = (f"{SOURCE} ; {period}.\n"
            f"Wallets actifs les deux heures (médiane n = {fnum(d['median_n_wallets'].median())} par paire), moyenne sur les "
            f"{int(d['n_hour_pairs'].iloc[-1])} à {int(d['n_hour_pairs'].iloc[0])} paires d'heures ; bandes : IC 95 % par "
            "bootstrap en blocs de 6 h.")
    finish(fig, ax, "D'une heure à l'autre, le rang ne persiste que faiblement, sans décroître", note, path,
           legend_loc="upper right")


def _delay_axis(ax) -> None:
    ax.set_xticks([math.log1p(d) for d in DELAYS])
    ax.set_xticklabels([str(d) for d in DELAYS])
    ax.set_xlabel("délai d entre le trade du leader et le nôtre (s, horodatage du bloc ; échelle log(1+d))")


def _latency_marks(ax, band_label_y: float, line_label_y: float) -> None:
    lo_r, hi_r = LATENCY_MARKS["logs on-chain / RTDS"]
    ax.axvspan(math.log1p(lo_r), math.log1p(hi_r), color=GRID, alpha=0.7, lw=0, zorder=0)
    ax.text(math.log1p((lo_r + hi_r) / 2), band_label_y, "délai réaliste\n(logs on-chain, RTDS)", ha="center", va="top",
            fontsize=7.3, color=INK2)
    for lab, d in (("mempool", LATENCY_MARKS["mempool"]), ("Data API\npar wallet", LATENCY_MARKS["Data API par wallet"]),
                   ("Data API\npar marché", LATENCY_MARKS["Data API par marché"])):
        ax.axvline(math.log1p(d), color=INK2, lw=0.7, ls=(0, (2, 3)), zorder=0)
        ax.text(math.log1p(d) + 0.03, line_label_y, lab, ha="left", va="top", fontsize=7, color=INK2)


def _cells(tab, group, K, L, est="first"):
    return tab[(tab["group"] == group) & (tab["K"] == K) & (tab["lookback_h"] == L)
               & (tab["price_estimator"] == est)].sort_values("delay_s")


def chart_copy(tab: pd.DataFrame, period: str, path: Path, est: str = "first") -> None:
    fig, ax = new_fig(8.4, 5.2)
    x = np.array([math.log1p(d) for d in DELAYS])
    series = [
        (("hors_echantillon", 10, 1), BLUE, "leaders hors échantillon : top 10 de l'heure précédente"),
        (("hors_echantillon", 10, 6), ORANGE, "leaders hors échantillon : top 10 des 6 h précédentes"),
        (("temoin_autres_wallets", 0, 1), AQUA, "témoin : achats de tous les autres wallets"),
        (("in_sample_fuite", 10, 0), YELLOW, "top 10 de l'heure même (in-sample, avec fuite)"),
    ]
    ymin, ymax = 0.0, 0.0
    for (g, K, L), color, label in series:
        s = _cells(tab, g, K, L, est)
        ax.fill_between(x, s["pnl_lo"], s["pnl_hi"], color=color, alpha=0.13, lw=0)
        ax.plot(x, s["pnl_per_share"], color=color, lw=2, marker="o", ms=4.5, mec=BG, mew=1.2, label=label)
        ymin, ymax = min(ymin, s["pnl_lo"].min()), max(ymax, s["pnl_hi"].max())
    ax.axhline(0, color=INK2, lw=0.8)
    rng_ = ymax - ymin
    ax.set_ylim(ymin - 0.08 * rng_, ymax + 0.30 * rng_)
    top = ax.get_ylim()[1]
    _latency_marks(ax, band_label_y=top - 0.11 * rng_, line_label_y=top)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: (f"{v * 100:+.0f} c" if abs(v) > 1e-9 else "0 c")))
    ax.set_ylabel("P&L moyen par part copiée (cents, frais preneur inclus)")
    _delay_axis(ax)
    prim = _cells(tab, "hors_echantillon", 10, 1, est)
    note = (f"{SOURCE} ; {period} ; évaluation à partir de la 7e heure (leaders choisis sur les heures passées, marge de 120 s).\n"
            f"Achat preneur du même jeton au prix du premier achat preneur observé à τ+d ou après (écarté si aucun dans les 10 s), "
            f"frais 0,07·p(1−p), payé à la résolution. n = {fnum(prim['n_events'].iloc[0])} achats de leaders (top 10, 1 h). "
            "Bandes : IC 95 % par bootstrap des créneaux de 15 min. Zone grisée : délai atteignable avec les logs on-chain ou le RTDS.")
    finish(fig, ax, "Copier les top wallets perd ~1,4 c par part, quel que soit le délai", note, path,
           legend_kw={"loc": "center left", "bbox_to_anchor": (0.0, 0.47), "frameon": True, "fontsize": 8})


def chart_copy_k(tab: pd.DataFrame, period: str, path: Path, lookback: int = 1, est: str = "first") -> None:
    fig, ax = new_fig(8.4, 4.9)
    x = np.array([math.log1p(d) for d in DELAYS])
    ymin, ymax = 0.0, 0.0
    for off, K, color in zip((-0.045, 0.0, 0.045), KS, (BLUE, ORANGE, AQUA)):
        s = _cells(tab, "hors_echantillon", K, lookback, est)
        v = s["roi"].to_numpy()
        ax.errorbar(x + off, v, yerr=[v - s["roi_lo"].to_numpy(), s["roi_hi"].to_numpy() - v], fmt="none",
                    ecolor=color, elinewidth=1, capsize=0, alpha=0.8)
        ax.plot(x + off, v, color=color, lw=2, marker="o", ms=4.5, mec=BG, mew=1.2, label=f"top {K}")
        ymin, ymax = min(ymin, s["roi_lo"].min()), max(ymax, s["roi_hi"].max())
    ax.axhline(0, color=INK2, lw=0.8)
    rng_ = ymax - ymin
    ax.set_ylim(ymin - 0.05 * rng_, ymax + 0.22 * rng_)
    top = ax.get_ylim()[1]
    _latency_marks(ax, band_label_y=top - 0.01 * rng_, line_label_y=top - 0.01 * rng_)
    ax.yaxis.set_major_formatter(pct_fmt(0))
    ax.set_ylabel("ROI de la copie (P&L / mise, frais inclus)")
    _delay_axis(ax)
    note = (f"{SOURCE} ; {period} ; évaluation à partir de la 7e heure.\n"
            "Même simulation que copy_pnl_delai.png ; ROI = Σ P&L / Σ (prix + frais) ; barres : IC 95 % par bootstrap des créneaux "
            "de 15 min (points décalés horizontalement pour la lisibilité).")
    finish(fig, ax, "Top 5, 10 ou 20 de l'heure précédente : la copie perd 2 à 3 % de la mise à tout délai", note, path,
           legend_kw={"loc": "lower left", "bbox_to_anchor": (0.0, 1.0), "ncol": 3})


def chart_copy_variants(tab: pd.DataFrame, period: str, path: Path, delay: int = 3, est: str = "first") -> None:
    rows = []
    for L in LOOKBACKS:
        for K in KS:
            rows.append((f"top {K}, classement sur {L} h", _cells(tab, "hors_echantillon", K, L, est), BLUE))
    rows.append(("témoin : tous les autres wallets", _cells(tab, "temoin_autres_wallets", 0, 1, est), AQUA))
    rows.append(("top 10 de l'heure même (in-sample)", _cells(tab, "in_sample_fuite", 10, 0, est), YELLOW))
    fig, ax = new_fig(8.0, 4.8)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    for i, (label, s, color) in enumerate(rows):
        r = s[s["delay_s"] == delay].iloc[0]
        y = len(rows) - 1 - i
        ax.plot([r["pnl_lo"], r["pnl_hi"]], [y, y], color=color, lw=2, solid_capstyle="round")
        ax.plot([r["pnl_bonf_lo"], r["pnl_bonf_hi"]], [y, y], color=color, lw=0.8, alpha=0.7)
        ax.plot(r["pnl_per_share"], y, "o", color=color, ms=7, mec=BG, mew=1.5)
        ax.text(max(r["pnl_hi"], r["pnl_bonf_hi"]) + 0.002, y, f"{r['pnl_per_share'] * 100:+.1f} c  (n = {fnum(r['n_filled'])})".replace(".", ",").replace("-", "−"),
                va="center", ha="left", fontsize=7.8, color=INK)
    ax.axvline(0, color=INK2, lw=0.8)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows][::-1])
    lo = min(tab.loc[tab["delay_s"] == delay, "pnl_bonf_lo"].min(), -0.01)
    hi = tab.loc[tab["delay_s"] == delay, "pnl_bonf_hi"].max()
    ax.set_xlim(lo - 0.005, hi + 0.04)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: (f"{v * 100:+.0f} c" if abs(v) > 1e-9 else "0 c")))
    ax.set_xlabel(f"P&L moyen par part copiée à d = {delay} s (cents, frais preneur inclus)")
    z = tab.attrs.get("z_bonf", float("nan"))
    note = (f"{SOURCE} ; {period} ; évaluation à partir de la 7e heure.\n"
            f"Trait épais : IC 95 % (bootstrap des créneaux de 15 min) ; trait fin : IC corrigé de Bonferroni pour les "
            f"{tab.attrs.get('n_cells', 108)} cellules K × fenêtre × délai × estimateur (± {z:.2f} erreurs-types).")
    finish(fig, ax, f"À {delay} s de délai, aucune variante hors échantillon n'est rentable", note, path, legend=False)


def chart_flow(flow: pd.DataFrame, period: str, path: Path) -> None:
    fig, ax = new_fig(7.8, 4.8)
    sub = flow[(flow["K"] == 10) & (flow["lookback_h"] == 1)].sort_values("cut_s")
    xs = np.arange(len(sub))
    wbar = 0.3
    for off, col, lo, hi, color, label in (
        (-wbar / 2 - 0.02, "auc_market", "auc_market_lo", "auc_market_hi", BLUE, "prix du marché au même instant"),
        (wbar / 2 + 0.02, "auc_flow", "auc_flow_lo", "auc_flow_hi", ORANGE, "flux net des leaders (top 10 de l'heure précédente)"),
    ):
        vals = sub[col].to_numpy()
        ax.bar(xs + off, vals - 0.5, bottom=0.5, width=wbar, color=color, label=label)
        ax.errorbar(xs + off, vals, yerr=[vals - sub[lo].to_numpy(), sub[hi].to_numpy() - vals], fmt="none",
                    ecolor=INK2, elinewidth=1, capsize=3)
        for xx, v, h in zip(xs + off, vals, sub[hi].to_numpy()):
            ax.text(xx, h + 0.006, f"{v:.2f}".replace(".", ","), ha="center", va="bottom", fontsize=8.5, color=INK)
    ax.axhline(0.5, color=INK2, lw=0.8)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"à S+{c} s" for c in sub["cut_s"]])
    ax.set_ylim(0.45, max(0.8, float(sub[["auc_market_hi", "auc_flow_hi"]].max().max()) + 0.06))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}".replace(".", ",")))
    ax.set_ylabel("AUC pour l'issue Up/Down (0,5 = hasard)")
    n = int(sub["n_windows_with_leader_flow"].iloc[0])
    note = (f"{SOURCE} ; {period} ; évaluation à partir de la 7e heure.\n"
            f"Fenêtres où les leaders ont tradé avant S+30 s : n = {fnum(n)} sur {fnum(sub['n_windows_eval'].iloc[0])}. "
            "Prix = VWAP (équivalent Up) des preneurs dans les 10 s précédentes ; flux = $ achetés côté Up − côté Down par les "
            "leaders depuis la mise en ligne du marché. IC 95 % par bootstrap des créneaux de 15 min.")
    finish(fig, ax, "Le flux des leaders prédit l'issue moins bien que le prix", note, path, legend_loc="upper left")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap resamples")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t_start, t_end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    n_hours = int((t_end - t_start) / pd.Timedelta(hours=1))
    period = fmt_period(t_start, t_end)
    timings: dict[str, float] = {}
    t0 = time.time()

    markets, trades, pnl, hourly = load(args.start, args.end)
    timings["load"] = time.time() - t0
    a = trade_arrays(trades, t_start)
    a["tx_code"] = pd.factorize(trades["tx_hash"])[0]
    wallet_index = pd.Index(trades["wallet"].cat.categories.astype(str))
    hours = pd.date_range(t_start, periods=n_hours, freq="h")
    pm_, act = pnl_matrix(hourly, wallet_index, t_start, n_hours)
    vol, _ = pnl_matrix(hourly, wallet_index, t_start, n_hours, col="volume")
    ranks = {L: leader_ranks(pm_, L) for L in LOOKBACKS}
    is_rank = insample_ranks(hourly, wallet_index, t_start, n_hours)
    summary: dict = {"period": [str(t_start), str(t_end)], "markets": len(markets), "fills": len(trades),
                     "wallets": int(trades["wallet"].nunique()), "wallet_hours": len(hourly)}

    # Q1
    conc, csum = q1_concentration(hourly, args.boot)
    conc.to_csv(out / "concentration_par_heure.csv", index=False)
    top_pnl, top_vol = q1_top_tables(hourly)
    top_pnl.to_csv(out / "top20_par_heure_pnl.csv", index=False)
    top_vol.to_csv(out / "top20_par_heure_volume.csv", index=False)
    pos48 = pnl.groupby("wallet")["pnl"].sum().sort_values(ascending=False)
    n1 = int(math.ceil(0.01 * len(pos48)))
    csum["48h"] = {"n_wallets": len(pos48), "share_winners": float((pos48 > 0).mean()),
                   "top10_share_of_positive": float(pos48.head(10).clip(lower=0).sum() / pos48.clip(lower=0).sum()),
                   "top1pct_share_of_positive": float(pos48.head(n1).clip(lower=0).sum() / pos48.clip(lower=0).sum()),
                   "top1pct_n": n1, "pnl_total": float(pos48.sum()), "fees_total": float(pnl["fees"].sum()),
                   "rebate_est_total": float(pnl["rebate_est"].sum())}
    csum["distinct_wallets_in_hourly_top20"] = int(top_pnl["wallet"].nunique())
    csum["distinct_wallets_in_hourly_top10"] = int(top_pnl.loc[top_pnl["rank"] <= 10, "wallet"].nunique())
    freq = top_pnl.loc[top_pnl["rank"] <= 10, "wallet"].value_counts()
    csum["hourly_top10_appearances_max"] = int(freq.max())
    csum["hourly_top10_wallets_once"] = int((freq == 1).sum())
    q1_frequency(hourly, pnl).to_csv(out / "top10_horaire_frequence.csv", index=False)
    lb, lb_detail = q1_leaderboard(pnl, hourly, t_end, pw.WALLETS_CACHE_DIR)
    lb.to_csv(out / "leaderboard_recoupement.csv", index=False)
    lb_detail.to_csv(out / "leaderboard_detail_top50.csv", index=False)
    summary["q1"] = csum
    chart_concentration(conc, csum, period, out / "concentration_horaire.png")
    timings["q1"] = time.time() - t0

    # Q2
    prof = q2_profile(a, hourly, is_rank, pnl, wallet_index, n_hours)
    prof["timing_zoom"].to_csv(out / "timing_par_seconde.csv", index=False)
    prof["timing_window"].to_csv(out / "timing_fenetre_5s.csv", index=False)
    prof["profile"].to_csv(out / "profil_top10_vs_autres.csv", index=False)
    prof["top20_48h"].to_csv(out / "top20_48h_profil.csv", index=False)
    prof["markets_per_hour"].to_csv(out / "marches_par_heure.csv", index=False)
    chart_timing_zoom(prof["timing_zoom"], period, out / "timing_ouverture.png")
    chart_timing_window(prof["timing_window"], "5m", period, out / "timing_fenetre_5m.png")
    chart_timing_window(prof["timing_window"], "15m", period, out / "timing_fenetre_15m.png")
    timings["q2"] = time.time() - t0

    # Q3
    decay, nxt, pairs = q3_persistence(pm_, act, vol, is_rank, hours, args.boot)
    decay.to_csv(out / "persistance_spearman.csv", index=False)
    nxt.to_csv(out / "persistance_top10.csv", index=False)
    pairs.to_csv(out / "persistance_paires_heures.csv", index=False)
    chart_persistence(decay, period, out / "persistance_spearman.png")
    timings["q3"] = time.time() - t0

    # Q4
    t_origin = int(a["ts"].min()) - 100
    tape = build_tape(a, t_origin)
    ev = copy_events(a)
    fills = simulate_fills(ev, tape, t_origin)
    cond_asset = pd.Series(a["asset"]).groupby(a["cond"]).first()
    cond_dur = pd.Series(a["duration"]).groupby(a["cond"]).first()
    ev["asset"] = cond_asset.reindex(ev["cond"]).to_numpy()
    ev["duration"] = cond_dur.reindex(ev["cond"]).to_numpy()
    tab, detail = q4_copy(ev, fills, ranks, is_rank, int(t_start.timestamp()), n_hours, args.boot)
    tab.to_csv(out / "copy_trading.csv", index=False)
    detail.to_csv(out / "copy_trading_detail_top10_1h.csv", index=False)
    lh = q4_leaders_hourly(pm_, act, vol, ranks, args.boot)
    lh.to_csv(out / "copy_leaders_pnl_horaire.csv", index=False)
    chart_copy(tab, period, out / "copy_pnl_delai.png")
    chart_copy_k(tab, period, out / "copy_roi_K.png")
    chart_copy_variants(tab, period, out / "copy_variantes_3s.png")
    summary["q4"] = {"z_bonferroni": tab.attrs["z_bonf"], "n_cells": tab.attrs["n_cells"], "n_events_all": len(ev),
                     "tape_taker_buys": len(tape["key"])}
    timings["q4"] = time.time() - t0

    # Q5
    flow, flow_mk = q5_flow(a, markets, trades, ranks, int(t_start.timestamp()), n_hours, args.boot)
    flow.to_csv(out / "flux_leaders_signal.csv", index=False)
    flow_mk.to_csv(out / "flux_leaders_par_fenetre_K10_1h.csv", index=False)
    chart_flow(flow, period, out / "flux_auc.png")
    timings["q5"] = time.time() - t0
    summary["timings_s"] = {k: round(v, 1) for k, v in timings.items()}
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
