#!/usr/bin/env python
"""Collect the historical TAKER fills of the Polymarket crypto "Up or Down" markets (BTC 5m and
BTC 15m by default) for the maker paper simulation: one line per on-chain transaction, the taker
side, with the block timestamp to the second.

Read-only research on public data, paper simulation only: no order, no key. Everything goes
through :func:`tradebot.polymarket_wallets.collect_trades` (``taker_only=True``): batches of
20 conditions on ``data-api /v2/trades``, full cursor pagination, shared rate limiter, retries,
and a parquet cache per (series, UTC day of the window start) in
``data/cache/polymarket/wallets/trades/``. A run can be interrupted and resumed: cached days are
not downloaded again, and days already present in the full cache (takers + makers, mode
``all``) are reused without any request.

Steps
-----
1. collect each required series (``--series``) over ``[start, end)``;
2. if the elapsed time plus a projection leaves room in ``--time-budget``, collect the optional
   series (``--optional-series``) too;
3. cross-check the market list with ``marches_predictions.csv`` (models-vs-market backtest);
4. verify ``--verify`` markets drawn at random against an independent, single-condition, fully
   paginated ``/v2/trades`` query: same number of taker fills, same volume, one line per tx;
5. print a JSON summary on stdout: markets, fills, taker volume, cache size, time, HTTP codes,
   and the per-market distribution of the number (and notional) of taker fills in
   ``[S − 60 s, S)`` and ``[S, S + 60 s)`` (the liquidity a resting order would see).

No report file is written: the outputs are the cache and the JSON on stdout (``--summary`` to
also save it somewhere outside the repository).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from tradebot import polymarket as pm
from tradebot import polymarket_wallets as pw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START = "2026-09-04T00:00Z"
DEFAULT_END = "2026-09-25T00:00Z"      # exclusive: windows starting up to 24/09 23:59 UTC
PREDICTIONS_CSV = ROOT / "reports" / "polymarket" / "modeles_vs_marche" / "marches_predictions.csv"
PRE_WINDOW_S = 60                      # [S − 60 s, S)
POST_WINDOW_S = 60                     # [S, S + 60 s)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)

log = logging.getLogger("collect_taker_trades")


# ---------------------------------------------------------------------------
# HTTP session that counts status codes (the client does not)
# ---------------------------------------------------------------------------
class CountingSession(requests.Session):
    """``requests.Session`` counting the HTTP status codes and network errors, thread-safe."""

    def __init__(self) -> None:
        super().__init__()
        self.headers.update({"User-Agent": "tradebot/0.1 (read-only research)"})
        self.codes: Counter[str] = Counter()
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        try:
            resp = super().get(url, **kwargs)
        except requests.RequestException as exc:
            with self._lock:
                self.codes[type(exc).__name__] += 1
            raise
        with self._lock:
            self.codes[str(resp.status_code)] += 1
        return resp

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self.codes.items()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_series(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        asset, _, duration = item.partition(":")
        if duration not in pm.DURATION_SECONDS:
            raise SystemExit(f"invalid series {item!r}: expected asset:5m or asset:15m")
        out.append((pm._norm_asset(asset), duration))
    return out


def expected_slots(duration: str, start: pd.Timestamp, end: pd.Timestamp) -> int:
    return len(pm.slot_starts(duration, start, end))


def dir_size(path: Path, pattern: str = "*") -> int:
    return sum(f.stat().st_size for f in Path(path).glob(pattern) if f.is_file())


def quantile_dict(values: np.ndarray) -> dict[str, float]:
    v = np.asarray(values, dtype="float64")
    if len(v) == 0:
        return {"n": 0}
    out = {"n": int(len(v)), "mean": float(v.mean())}
    for q in QUANTILES:
        out[f"p{int(round(q * 100))}"] = float(np.quantile(v, q))
    out["share_zero"] = float((v == 0).mean())
    return out


def series_key(asset: str, duration: str) -> str:
    return f"{asset}_{duration}"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def collect_series(
    asset: str,
    duration: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    client: pm.PolymarketClient,
    cache_dir: Path,
    workers: int,
    rps: float,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """``collect_trades`` for one series, taker side only. Returns (markets, trades, seconds)."""
    t0 = time.time()
    markets, trades = pw.collect_trades(
        (asset,), (duration,), start, end,
        cache=True, taker_only=True, client=client, cache_dir=cache_dir,
        max_workers=workers, max_rps=rps, progress=True,
    )
    dt = time.time() - t0
    log.info(
        "%s %s : %d markets, %d taker fills, %.0f $ taker volume, %.0f s",
        asset, duration, len(markets), len(trades), float(trades["notional"].sum()) if len(trades) else 0.0, dt,
    )
    return markets, trades, dt


def series_summary(markets: pd.DataFrame, trades: pd.DataFrame, seconds: float, start: pd.Timestamp,
                   end: pd.Timestamp, duration: str) -> dict[str, Any]:
    n_expected = expected_slots(duration, start, end)
    per_market = (
        trades.groupby("condition_id", observed=True)["notional"].agg(["size", "sum"])
        if len(trades) else pd.DataFrame(columns=["size", "sum"])
    )
    per_market = per_market.reindex(markets["condition_id"].astype(str)).fillna(0)
    return {
        "slots_expected": int(n_expected),
        "markets_resolved": int(len(markets)),
        "markets_with_fills": int((per_market["size"] > 0).sum()),
        "taker_fills": int(len(trades)),
        "taker_volume_usd": float(trades["notional"].sum()) if len(trades) else 0.0,
        "fills_per_market": quantile_dict(per_market["size"].to_numpy()),
        "volume_per_market_usd": quantile_dict(per_market["sum"].to_numpy()),
        "unique_tx_hash": int(trades["tx_hash"].nunique()) if len(trades) else 0,
        "unique_wallets": int(trades["wallet"].nunique()) if len(trades) else 0,
        "first_fill": trades["ts"].min().isoformat() if len(trades) else None,
        "last_fill": trades["ts"].max().isoformat() if len(trades) else None,
        "seconds": round(seconds, 1),
    }


# ---------------------------------------------------------------------------
# Liquidity around the open: fills in [S − 60 s, S) and [S, S + 60 s)
# ---------------------------------------------------------------------------
def liquidity_around_open(markets: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    """Per-market number and notional of taker fills in the minute before and after S, over all
    resolved markets (markets without any fill count as 0)."""
    cids = markets["condition_id"].astype(str)
    out: dict[str, Any] = {}
    if len(trades) == 0:
        zeros = np.zeros(len(cids))
        for name in ("pre_open_60s", "post_open_60s"):
            out[name] = {"n_fills": quantile_dict(zeros), "notional_usd": quantile_dict(zeros)}
        return out
    t_rel = trades["t_rel_s"].to_numpy(dtype="float64", na_value=np.nan)
    windows = {
        "pre_open_60s": (t_rel >= -PRE_WINDOW_S) & (t_rel < 0),
        "post_open_60s": (t_rel >= 0) & (t_rel < POST_WINDOW_S),
    }
    is_buy = (trades["side"] == "BUY").to_numpy(dtype=bool)
    cid = trades["condition_id"].astype(str).to_numpy()
    notional = trades["notional"].to_numpy(dtype="float64")
    for name, mask in windows.items():
        sub = pd.DataFrame(
            {"cid": cid[mask], "n": 1, "notional": notional[mask], "buy": is_buy[mask].astype("int64")}
        )
        g = sub.groupby("cid").agg(n=("n", "sum"), notional=("notional", "sum"), buy=("buy", "sum"))
        g = g.reindex(cids).fillna(0)
        n = g["n"].to_numpy(dtype="float64")
        out[name] = {
            "n_fills": quantile_dict(n),
            "notional_usd": quantile_dict(g["notional"].to_numpy(dtype="float64")),
            "buy_share_of_fills": float(g["buy"].sum() / n.sum()) if n.sum() else math.nan,
            "markets": int(len(cids)),
        }
    # fills before the open at all (S − 5 min for 5m markets, earlier for 15m markets)
    pre = t_rel < 0
    n_pre = pd.Series(1, index=cid[pre]).groupby(level=0).sum().reindex(cids).fillna(0)
    out["before_open_any"] = {"n_fills": quantile_dict(n_pre.to_numpy(dtype="float64"))}
    first = pd.Series(t_rel, index=cid).groupby(level=0).min().reindex(cids)
    out["first_fill_t_rel_s"] = quantile_dict(first.dropna().to_numpy(dtype="float64"))
    return out


# ---------------------------------------------------------------------------
# Verification against an independent single-condition query
# ---------------------------------------------------------------------------
def fetch_single_condition(client: pm.PolymarketClient, condition_id: str) -> pd.DataFrame:
    """All taker lines of one condition, fully paginated, one condition per request (independent
    of the 20-condition batches used by ``collect_trades``)."""
    frames: list[pd.DataFrame] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(pw.MAX_PAGES):
        params: dict[str, Any] = {"condition": condition_id, "limit": pw.PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        data = client._get(pw.TRADES_V2_URL, params)
        if not data:
            break
        rows = data.get("data") or [] if isinstance(data, dict) else data
        if rows:
            frames.append(pw.trades_v2_frame(rows))
        pg = data.get("pagination") or {} if isinstance(data, dict) else {}
        nxt = pg.get("next_cursor")
        if not pg.get("has_more") or not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    if not frames:
        return pw.trades_v2_frame([])
    return pd.concat(frames, ignore_index=True)


def verify_random_markets(
    client: pm.PolymarketClient, markets: pd.DataFrame, trades: pd.DataFrame, n: int, seed: int
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    cids = markets["condition_id"].astype(str).to_numpy()
    if n <= 0 or len(cids) == 0:
        return {"n": 0, "all_match": None, "markets": []}
    sample = rng.choice(cids, size=min(n, len(cids)), replace=False)
    slug_of = dict(zip(markets["condition_id"].astype(str), markets["slug"].astype(str)))
    cached_cid = trades["condition_id"].astype(str).to_numpy()
    results: list[dict[str, Any]] = []
    for cid in sample:
        ref = fetch_single_condition(client, cid)
        ref = ref[ref["condition_id"].astype(str) == cid]
        ours = trades[cached_cid == cid]
        n_ref, n_ours = int(len(ref)), int(len(ours))
        v_ref = float(ref["notional"].sum()) if n_ref else 0.0
        v_ours = float(ours["notional"].sum()) if n_ours else 0.0
        tx_ref = set(ref["tx_hash"].astype(str)) if n_ref else set()
        tx_ours = set(ours["tx_hash"].astype(str)) if n_ours else set()
        match = n_ref == n_ours and math.isclose(v_ref, v_ours, rel_tol=1e-9, abs_tol=1e-6) and tx_ref == tx_ours
        results.append(
            {
                "slug": slug_of.get(cid, ""),
                "n_api": n_ref,
                "n_cache": n_ours,
                "volume_api": round(v_ref, 4),
                "volume_cache": round(v_ours, 4),
                "one_line_per_tx_api": bool(len(tx_ref) == n_ref),
                "one_line_per_tx_cache": bool(len(tx_ours) == n_ours),
                "match": bool(match),
            }
        )
        log.info("verify %s : api %d / cache %d fills, %.2f / %.2f $ -> %s",
                 slug_of.get(cid, cid), n_ref, n_ours, v_ref, v_ours, "OK" if match else "MISMATCH")
    return {
        "n": len(results),
        "n_match": int(sum(r["match"] for r in results)),
        "all_match": bool(all(r["match"] for r in results)),
        "markets": results,
    }


# ---------------------------------------------------------------------------
# Cross-check with the models-vs-market backtest
# ---------------------------------------------------------------------------
def crosscheck_predictions(
    path: Path, markets: pd.DataFrame, trades: pd.DataFrame, series: list[tuple[str, str]],
    start: pd.Timestamp, end: pd.Timestamp,
) -> dict[str, Any] | None:
    if not path.exists():
        log.warning("predictions CSV not found: %s", path)
        return None
    preds = pd.read_csv(path, usecols=["slug", "asset", "duration", "start", "volume"])
    preds["start"] = pd.to_datetime(preds["start"], utc=True)
    keep = preds["start"].ge(start) & preds["start"].lt(end)
    keep &= pd.Series(list(zip(preds["asset"], preds["duration"]))).isin(series).to_numpy()
    preds = preds[keep]
    taker_vol = trades.groupby("condition_id", observed=True)["notional"].sum() if len(trades) else pd.Series(dtype="float64")
    m = markets[["slug", "condition_id", "volume"]].copy()
    m["taker_volume"] = m["condition_id"].astype(str).map(taker_vol).fillna(0.0)
    both = m.merge(preds[["slug", "volume"]].rename(columns={"volume": "csv_volume"}), on="slug", how="inner")
    ratio = both["taker_volume"] / both["csv_volume"].where(both["csv_volume"] > 0)
    out: dict[str, Any] = {
        "path": str(path.relative_to(ROOT)),
        "csv_markets": int(len(preds)),
        "collected_markets": int(len(m)),
        "in_both": int(len(both)),
        "only_in_csv": sorted(set(preds["slug"]) - set(m["slug"]))[:20],
        "n_only_in_csv": int(len(set(preds["slug"]) - set(m["slug"]))),
        "n_only_in_collection": int(len(set(m["slug"]) - set(preds["slug"]))),
        "taker_volume_over_gamma_volume": quantile_dict(ratio.dropna().to_numpy()) if len(both) else None,
    }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=DEFAULT_START, help="first window start (UTC, inclusive)")
    ap.add_argument("--end", default=DEFAULT_END, help="end (UTC, exclusive) of the window starts")
    ap.add_argument("--series", default="btc:5m,btc:15m", help="required series, asset:duration,...")
    ap.add_argument("--optional-series", default="eth:5m",
                    help="collected only if the projection fits in --time-budget ('' to disable)")
    ap.add_argument("--time-budget", type=float, default=45 * 60, help="seconds, total, for the optional series decision")
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--rps", type=float, default=13.0, help="requests/s, shared by all threads (API: 30/s on /v2/trades)")
    ap.add_argument("--cache-dir", default=str(pw.WALLETS_CACHE_DIR))
    ap.add_argument("--verify", type=int, default=20, help="markets drawn at random and re-fetched one by one")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--predictions", default=str(PREDICTIONS_CSV))
    ap.add_argument("--summary", default=None, help="also write the JSON summary to this path (outside the repo)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    start, end = pm._to_utc(args.start), pm._to_utc(args.end)
    required = parse_series(args.series)
    optional = parse_series(args.optional_series) if args.optional_series else []
    cache_dir = Path(args.cache_dir)
    trades_dir = cache_dir / "trades"
    bytes_before = dir_size(trades_dir) if trades_dir.exists() else 0

    session = CountingSession()
    client = pw._as_client(session, args.rps, cache_dir=cache_dir, use_cache=True)
    t0 = time.time()
    per_series: dict[str, dict[str, Any]] = {}
    all_markets: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []

    def run(asset: str, duration: str) -> None:
        markets, trades, dt = collect_series(
            asset, duration, start, end, client=client, cache_dir=cache_dir, workers=args.workers, rps=args.rps
        )
        per_series[series_key(asset, duration)] = series_summary(markets, trades, dt, start, end, duration)
        per_series[series_key(asset, duration)]["liquidity_around_open"] = liquidity_around_open(markets, trades)
        all_markets.append(markets)
        all_trades.append(trades)

    for asset, duration in required:
        run(asset, duration)

    skipped_optional: list[dict[str, Any]] = []
    for asset, duration in optional:
        elapsed = time.time() - t0
        same_dur = [(k, v) for k, v in per_series.items() if k.endswith(f"_{duration}")]
        if same_dur:
            sec_per_market = sum(v["seconds"] for _, v in same_dur) / max(1, sum(v["markets_resolved"] for _, v in same_dur))
        else:
            sec_per_market = 0.15  # rough default: ~1 request per market at 7 req/s effective
        projected = sec_per_market * expected_slots(duration, start, end)
        if elapsed + projected < args.time_budget:
            log.info("optional series %s %s : projected %.0f s, elapsed %.0f s -> collecting", asset, duration, projected, elapsed)
            run(asset, duration)
        else:
            log.info("optional series %s %s skipped : projected %.0f s + elapsed %.0f s > budget %.0f s",
                     asset, duration, projected, elapsed, args.time_budget)
            skipped_optional.append({"series": series_key(asset, duration), "projected_s": round(projected),
                                     "elapsed_s": round(elapsed)})
    t_collect = time.time() - t0

    markets = pd.concat(all_markets, ignore_index=True) if all_markets else pm.markets_to_frame([])
    trades = pw._concat(all_trades) if all_trades else pw.trades_v2_frame([])
    collected = [(a, d) for a, d in required + optional if series_key(a, d) in per_series]

    t1 = time.time()
    verification = verify_random_markets(client, markets, trades, args.verify, args.seed)
    t_verify = time.time() - t1
    crosscheck = crosscheck_predictions(Path(args.predictions), markets, trades, collected, start, end)

    days = pd.date_range(start.floor("D"), (end - pd.Timedelta(seconds=1)).floor("D"), freq="D")
    cache_files = [
        p for p in trades_dir.glob("*.parquet")
        if any(p.name.startswith(f"{a}_{d}_{day.strftime('%Y-%m-%d')}_") for a, d in collected for day in days)
    ]
    summary = {
        "period": [start.isoformat(), end.isoformat()],
        "series": [series_key(a, d) for a, d in collected],
        "skipped_optional": skipped_optional,
        "markets": int(len(markets)),
        "taker_fills": int(len(trades)),
        "taker_volume_usd": float(trades["notional"].sum()) if len(trades) else 0.0,
        "per_series": per_series,
        "cache": {
            "dir": str(trades_dir),
            "files_for_period": len(cache_files),
            "taker_files_for_period": sum(1 for p in cache_files if p.name.endswith("_taker.parquet")),
            "all_files_reused": sum(1 for p in cache_files if p.name.endswith("_all.parquet")),
            "bytes_for_period": int(sum(p.stat().st_size for p in cache_files)),
            "bytes_dir_total": int(dir_size(trades_dir)),
            "bytes_added_this_run": int(dir_size(trades_dir) - bytes_before),
        },
        "http_codes": session.snapshot(),
        "rps": args.rps,
        "workers": args.workers,
        "seconds_collect": round(t_collect, 1),
        "seconds_verify": round(t_verify, 1),
        "seconds_total": round(time.time() - t0, 1),
        "verification": verification,
        "crosscheck_predictions": crosscheck,
    }
    text = json.dumps(summary, indent=1, default=str)
    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
