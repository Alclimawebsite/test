"""Runners pump.fun : moments de détection, variables point-in-time, étiquettes et P&L.

Trois **moments** où l'on peut décider d'acheter :

* ``launch_1m``, ``launch_5m``, ``launch_15m`` : 1, 5 ou 15 minutes après la création ;
* ``pre_grad`` : le premier trade qui porte la courbe à 50 % (≈ 70 SOL de market cap) ;
* ``post_grad`` : 5 minutes après la graduation (fin de la courbe, passage sur PumpSwap).

Trois **définitions de runner**, mesurées depuis l'instant t sur un horizon H (1 h, 6 h, 24 h) :

* ``x10`` : le prix touche au moins 10 fois le prix de référence en t (x3 et x5 aussi calculés) ;
* ``grad`` : le token gradue entre t et t + H (sans objet pour ``post_grad``) ;
* ``mcap1m`` : la market cap touche 1 M$ (``mcap100k`` : 100 k$).

Les variables n'utilisent que les trades **strictement antérieurs à t** et ce que l'on
savait du token à sa création. Les étiquettes n'utilisent que ce qui suit t. Une étiquette
dont l'horizon n'est pas encore écoulé vaut NaN (censure), jamais 0.

Le P&L suppose un achat de ``size_sol`` SOL au premier trade observé après t + latence, avec
le glissement de la courbe (produit constant) et les frais, puis une sortie à l'horizon ou dès
qu'une clôture de bougie atteint l'objectif (×2, ×3…). Les mèches (plus haut de bougie) ne
servent qu'aux étiquettes, pas aux sorties : un pic d'un seul trade n'est pas vendable.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import pumpfun as pf
from . import pumpfun_social as ps

log = logging.getLogger(__name__)

MIN_MS = 60_000
HOUR_MS = 3_600_000
LAUNCH_OFFSETS = {"launch_1m": 1, "launch_5m": 5, "launch_15m": 15}      # minutes après la création
MOMENTS = tuple(LAUNCH_OFFSETS) + ("pre_grad", "post_grad")
PRE_GRAD_PROGRESS = 0.50
GRAD_PROGRESS = 0.985                      # au-delà, la courbe est considérée finie
POST_GRAD_DELAY_MS = 5 * MIN_MS
WINDOW_MS = 5 * MIN_MS                     # fenêtre des variables « portefeuilles » avant t (hors lancement)
HORIZONS_H = (1, 6, 24)
MULTS = (3, 5, 10)
MCAPS_USD = {"mcap100k": 100_000, "mcap1m": 1_000_000}
TAKE_PROFITS = (2.0, 3.0, 5.0, 10.0)


@dataclass(frozen=True)
class Costs:
    """Hypothèses d'exécution (tests de sensibilité dans l'étude)."""

    size_sol: float = 0.5          # mise par token
    latency_s: float = 2.0         # délai entre l'instant t et notre achat
    curve_fee: float = 0.0125      # frais pump.fun sur la courbe, par côté
    amm_cost: float = 0.02         # frais + glissement par côté sur PumpSwap (après graduation)
    priority_sol: float = 0.002    # frais de priorité / pourboire Jito par transaction


# ---------------------------------------------------------------------------
# Trajectoire de prix (bougies) et événements
# ---------------------------------------------------------------------------
def splice_candles(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Assemble des bougies de résolutions différentes : la plus fine l'emporte là où elle existe.

    ``parts`` va de la plus fine à la plus grossière ; chaque bougie garde sa durée (``dur``).
    """
    out: list[pd.DataFrame] = []
    covered_from = math.inf
    for df in parts:
        if df is None or df.empty:
            continue
        d = df[df["ts"] + df["dur"] <= covered_from] if math.isfinite(covered_from) else df
        out.append(d)
        covered_from = min(covered_from, float(df["ts"].min()))
    if not out:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "dur"])
    return pd.concat(out).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def first_cross(candles: pd.DataFrame, price: float, after_ms: int = 0) -> float:
    """Début de la première bougie (après ``after_ms``) dont le plus haut atteint ``price`` ; NaN sinon."""
    c = candles[(candles["ts"] >= after_ms) & (candles["high"] >= price)]
    return float(c["ts"].iloc[0]) if len(c) else math.nan


def graduation_ms(candles: pd.DataFrame, trades: pd.DataFrame | None = None) -> float:
    """Instant de graduation : premier trade ``pump_amm`` si on l'a vu, sinon première bougie en fin de courbe."""
    if trades is not None and len(trades):
        amm = trades.loc[trades["program"] == "pump_amm", "ts"]
        if len(amm):
            return float(amm.iloc[0])
    return first_cross(candles, float(pf.curve_price(GRAD_PROGRESS)))


# ---------------------------------------------------------------------------
# Variables point-in-time
# ---------------------------------------------------------------------------
def wallet_features(tr: pd.DataFrame, t_ms: int, creator: str | None, created_ms: int,
                    prefix: str = "") -> dict[str, float]:
    """Activité et concentration des portefeuilles dans les trades ``tr`` (tous antérieurs à t)."""
    f: dict[str, float] = {}
    n = len(tr)
    buys = tr[tr["side"] == "buy"]
    sells = tr[tr["side"] == "sell"]
    f["n_trades"] = n
    f["n_buys"] = len(buys)
    f["n_sells"] = len(sells)
    f["n_buyers"] = buys["user"].nunique()
    f["n_sellers"] = sells["user"].nunique()
    f["n_traders"] = tr["user"].nunique()
    f["buy_sol"] = float(buys["sol"].sum())
    f["sell_sol"] = float(sells["sol"].sum())
    f["net_sol"] = f["buy_sol"] - f["sell_sol"]
    f["sell_ratio"] = f["sell_sol"] / f["buy_sol"] if f["buy_sol"] > 0 else math.nan
    f["buy_med_sol"] = float(buys["sol"].median()) if len(buys) else math.nan
    f["buy_max_sol"] = float(buys["sol"].max()) if len(buys) else math.nan
    f["small_buy_frac"] = float((buys["sol"] < 0.01).mean()) if len(buys) else math.nan
    f["trades_per_trader"] = n / f["n_traders"] if f["n_traders"] else math.nan
    f["buyers_who_sold"] = (len(set(buys["user"]) & set(sells["user"])) / f["n_buyers"]) if f["n_buyers"] else math.nan
    # détention nette par portefeuille (tokens), part de l'offre
    signed = np.where(tr["side"] == "buy", tr["tokens"], -tr["tokens"])
    net = pd.Series(signed, index=tr.index).groupby(tr["user"]).sum().clip(lower=0).sort_values(ascending=False)
    held = float(net.sum())
    f["held_supply"] = held / pf.TOTAL_SUPPLY
    f["top1_share"] = float(net.iloc[0]) / held if held > 0 else math.nan
    f["top10_share"] = float(net.iloc[:10].sum()) / held if held > 0 else math.nan
    f["hhi"] = float(((net / held) ** 2).sum()) if held > 0 else math.nan
    # créateur
    if creator:
        c = tr[tr["user"] == creator]
        cb = c[c["side"] == "buy"]
        f["dev_buy_sol"] = float(cb["sol"].sum())
        f["dev_sold"] = int((c["side"] == "sell").any())
        f["dev_held_supply"] = float(net.get(creator, 0.0)) / pf.TOTAL_SUPPLY
    # dynamique récente (dernière minute avant t)
    last = tr[tr["ts"] >= t_ms - MIN_MS]
    f["n_trades_1m"] = len(last)
    f["buy_sol_1m"] = float(last.loc[last["side"] == "buy", "sol"].sum())
    f["sell_sol_1m"] = float(last.loc[last["side"] == "sell", "sol"].sum())
    f["idle_s"] = (t_ms - float(tr["ts"].iloc[-1])) / 1000 if n else math.nan
    p = tr["price_sol"].to_numpy(float)
    if n:
        f["price_sol"] = p[-1]
        f["mult_first"] = p[-1] / p[0]
        f["dd_from_peak"] = p[-1] / np.nanmax(p) - 1
        p1 = tr.loc[tr["ts"] < t_ms - MIN_MS, "price_sol"]
        f["ret_1m"] = p[-1] / float(p1.iloc[-1]) - 1 if len(p1) else math.nan
    return {prefix + k: v for k, v in f.items()}


def sniper_features(tr: pd.DataFrame, created_ms: int, creator: str | None) -> dict[str, float]:
    """Achats groupés à la création (bundles, snipers) : même seconde et 3 premières secondes."""
    b = tr[(tr["side"] == "buy") & (tr["user"] != creator)]
    same = b[b["ts"] <= created_ms]
    first3 = b[b["ts"] <= created_ms + 3000]
    return {
        "snipe_n0": same["user"].nunique(),
        "snipe_n3": first3["user"].nunique(),
        "snipe_sol3": float(first3["sol"].sum()),
        "snipe_supply3": float(first3["tokens"].sum()) / pf.TOTAL_SUPPLY,
    }


def features_at(moment: str, t_ms: int, coin: dict, tr_before: pd.DataFrame, candles: pd.DataFrame,
                orders: pd.DataFrame | None = None) -> dict[str, float]:
    """Toutes les variables d'un token à l'instant t (``tr_before`` : trades antérieurs à t)."""
    created = int(coin["created_timestamp"])
    creator = coin.get("creator")
    tr_before = tr_before[tr_before["ts"] < t_ms]
    f: dict[str, float] = {"age_min": (t_ms - created) / MIN_MS}
    f.update(ps.link_features(coin))
    if moment.startswith("launch"):
        f.update(wallet_features(tr_before, t_ms, creator, created))
        f.update(sniper_features(tr_before, created, creator))
    else:
        win = tr_before[tr_before["ts"] >= t_ms - WINDOW_MS]
        f.update(wallet_features(win, t_ms, creator, created))
        c = candles[candles["ts"] + candles["dur"] <= t_ms]
        f["life_vol_sol"] = float(c["volume"].sum())
        f["life_max_mult"] = float(c["high"].max() / c["open"].iloc[0]) if len(c) else math.nan
        f["life_n_candles"] = len(c)
    p = f.get("price_sol", math.nan)
    f["progress"] = float(pf.curve_progress(p)) if np.isfinite(p) else math.nan
    f.update(ps.dex_features(orders, t_ms) if orders is not None else
             {"dex_paid_profile": math.nan, "dex_boost_amount": math.nan, "dex_n_orders": math.nan})
    hour = pd.Timestamp(t_ms, unit="ms", tz="UTC").hour
    f["hour_sin"], f["hour_cos"] = math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24)
    return f


# ---------------------------------------------------------------------------
# Étiquettes et P&L
# ---------------------------------------------------------------------------
def _sell_value(price: float, tokens: float, c: Costs) -> float:
    if price <= 0 or not np.isfinite(price):
        return 0.0
    if price < pf.GRAD_PRICE_SOL * 0.999:
        return pf.curve_sell_value(price, tokens, c.curve_fee) - c.priority_sol
    return tokens * price * (1 - c.amm_cost) - c.priority_sol


def entry_fill(p_entry: float, c: Costs, on_curve: bool) -> tuple[float, float]:
    """(tokens reçus, SOL dépensé) pour un achat de ``c.size_sol`` au prix ``p_entry``."""
    spend = c.size_sol + c.priority_sol
    if on_curve and p_entry < pf.GRAD_PRICE_SOL * 0.999:
        avg = pf.curve_buy_price(p_entry, c.size_sol, c.curve_fee)
        return c.size_sol / avg, spend
    return c.size_sol * (1 - c.amm_cost) / p_entry, spend


def labels_at(t_ms: int, now_ms: int, p_ref: float, sol_usd: float, candles: pd.DataFrame,
              grad_ms: float, moment: str) -> dict[str, float]:
    """Étiquettes des trois définitions, pour chaque horizon (NaN si l'horizon n'est pas écoulé)."""
    out: dict[str, float] = {}
    for h in HORIZONS_H:
        end = t_ms + h * HOUR_MS
        if end > now_ms or not np.isfinite(p_ref) or p_ref <= 0:
            for k in [f"maxmult_{h}h", f"grad_{h}h", *[f"x{m}_{h}h" for m in MULTS], *[f"{k}_{h}h" for k in MCAPS_USD]]:
                out[k] = math.nan
            continue
        w = candles[(candles["ts"] >= t_ms) & (candles["ts"] < end)]      # bougies entièrement après t
        mx = float(w["high"].max()) if len(w) else p_ref
        mx = max(mx, p_ref)
        out[f"maxmult_{h}h"] = mx / p_ref
        for m in MULTS:
            out[f"x{m}_{h}h"] = float(mx / p_ref >= m)
        for k, usd in MCAPS_USD.items():
            out[f"{k}_{h}h"] = float(mx * pf.TOTAL_SUPPLY * sol_usd >= usd)
        out[f"grad_{h}h"] = math.nan if moment == "post_grad" else float(np.isfinite(grad_ms) and t_ms < grad_ms <= end)
    return out


def pnl_at(t_ms: int, now_ms: int, p_entry: float, candles: pd.DataFrame, costs: Costs) -> dict[str, float]:
    """Multiple net de la mise (1 = on récupère sa mise) : détention jusqu'à H, ou objectif ×k sur clôture."""
    out: dict[str, float] = {}
    if not np.isfinite(p_entry) or p_entry <= 0:
        return out
    on_curve = p_entry < pf.GRAD_PRICE_SOL * 0.999
    tokens, spend = entry_fill(p_entry, costs, on_curve)
    for h in HORIZONS_H:
        end = t_ms + h * HOUR_MS
        if end > now_ms:
            continue
        w = candles[(candles["ts"] > t_ms) & (candles["ts"] + candles["dur"] <= end)]
        before = candles[candles["ts"] + candles["dur"] <= end]
        last = float(before["close"].iloc[-1]) if len(before) else p_entry
        out[f"pnl_hold_{h}h"] = _sell_value(last, tokens, costs) / spend
        for tp in TAKE_PROFITS:
            hit = w[w["close"] >= tp * p_entry]
            px = float(hit["close"].iloc[0]) if len(hit) else last
            out[f"pnl_tp{tp:g}_{h}h"] = _sell_value(px, tokens, costs) / spend
    return out


# ---------------------------------------------------------------------------
# Assemblage pour un token (réseau)
# ---------------------------------------------------------------------------
INTERVAL_MS = {"1m": MIN_MS, "5m": 5 * MIN_MS, "1h": HOUR_MS}


def fetch_candles(cli: pf.PumpFunClient, mint: str, created_ms: int) -> pd.DataFrame:
    """Trajectoire complète en SOL : bougies 1m, complétées par 5m puis 1h si la vie du token dépasse 1000 bougies."""
    parts = []
    for iv in ("1m", "5m", "1h"):
        c = cli.candles(mint, created_ms, iv, "SOL")
        c["dur"] = INTERVAL_MS[iv]
        parts.append(c)
        if c.empty or len(c) < 1000 or c["ts"].min() <= created_ms + INTERVAL_MS[iv]:
            break
    return splice_candles(parts)


@dataclass
class TokenData:
    coin: dict
    candles: pd.DataFrame
    trades: dict[str, pd.DataFrame] = field(default_factory=dict)   # fenêtre -> trades
    orders: pd.DataFrame | None = None
    grad_ms: float = math.nan
    pre_ms: float = math.nan
    notes: list[str] = field(default_factory=list)
    post_ms: float = math.nan          # instant de décision « après graduation »


def fetch_post_grad(cli: pf.PumpFunClient, td: TokenData, ctx_pages: int = 3) -> None:
    """Fenêtre de décision après la graduation, lue en remontant depuis t.

    La bougie où la courbe finit situe la graduation à la minute près ; t = fin de cette bougie
    + 5 min est donc au moins 5 min après le premier trade PumpSwap. La lecture part de t + 20 s
    (pour le prix d'entrée) et remonte : si le token est très actif et la fenêtre tronquée, ce
    sont les trades les plus anciens de la fenêtre qui manquent, jamais ceux qui précèdent t.
    """
    gc = graduation_ms(td.candles, td.trades.get("launch"))
    if not np.isfinite(gc):
        return
    dur = td.candles.loc[td.candles["ts"] == gc, "dur"]
    t = gc + (float(dur.iloc[0]) if len(dur) else MIN_MS) + POST_GRAD_DELAY_MS
    tr = cli.trades(td.coin["mint"], end_ms=int(t + 20_000), start_ms=int(t - WINDOW_MS), max_pages=ctx_pages)
    amm = tr.loc[tr["program"] == "pump_amm", "ts"]
    if not len(amm):
        td.notes.append("post_grad_not_located")
        return
    curve = tr.loc[tr["program"] == "pump", "ts"]
    td.grad_ms = float(amm.iloc[0]) if len(curve) else gc      # graduation vue dans la fenêtre, sinon la bougie
    td.post_ms = t
    td.trades["post_grad"] = tr
    if tr.attrs.get("truncated"):
        td.notes.append("post_grad_truncated")


def moment_times(td: TokenData) -> dict[str, float]:
    created = int(td.coin["created_timestamp"])
    t = {m: float(created + k * MIN_MS) for m, k in LAUNCH_OFFSETS.items()}
    t["pre_grad"] = td.pre_ms + 1000 if np.isfinite(td.pre_ms) else math.nan
    t["post_grad"] = getattr(td, "post_ms", math.nan)
    return t


def fetch_token(cli: pf.PumpFunClient, coin: dict, moments: tuple[str, ...] = MOMENTS,
                dex_cli: pf.PumpFunClient | None = None, dex_min_trades: int = 20,
                max_pages: int = 30, ctx_pages: int = 3) -> TokenData:
    """Télécharge ce qu'il faut pour évaluer un token à chacun des ``moments``."""
    mint, created = coin["mint"], int(coin["created_timestamp"])
    td = TokenData(coin=coin, candles=fetch_candles(cli, mint, created))
    launch = [m for m in moments if m.startswith("launch")]
    if launch:
        end = created + max(LAUNCH_OFFSETS[m] for m in launch) * MIN_MS + MIN_MS
        tr = cli.trades(mint, end_ms=end, start_ms=created, max_pages=max_pages)
        if tr.attrs.get("truncated"):
            td.notes.append("launch_truncated")
        td.trades["launch"] = tr
    grad_seen = td.trades.get("launch")
    td.grad_ms = graduation_ms(td.candles, grad_seen)
    p50 = float(pf.curve_price(PRE_GRAD_PROGRESS))
    cross = first_cross(td.candles, p50)
    if "pre_grad" in moments and np.isfinite(cross) and not (np.isfinite(td.grad_ms) and cross >= td.grad_ms):
        # une seule lecture : la minute du franchissement, la fenêtre qui la précède et une minute après
        dur = float(td.candles.loc[td.candles["ts"] == cross, "dur"].iloc[0])
        tr = cli.trades(mint, end_ms=int(cross + dur + 20_000), start_ms=int(cross - WINDOW_MS), max_pages=ctx_pages)
        hit = tr[(tr["price_sol"] >= p50) & (tr["program"] == "pump") & (tr["ts"] >= cross)]
        if len(hit):                                   # tronqué : on garde les trades lus (les plus proches de t)
            td.pre_ms = float(hit["ts"].iloc[0])
            td.trades["pre_grad"] = tr
            if tr.attrs.get("truncated"):
                td.notes.append("pre_grad_truncated")
        else:
            td.notes.append("pre_grad_not_located")
    if "post_grad" in moments:
        fetch_post_grad(cli, td, ctx_pages)
    n_launch = len(td.trades.get("launch", []))
    if dex_cli is not None and (n_launch >= dex_min_trades or np.isfinite(cross)):
        try:
            td.orders = ps.dexscreener_orders(dex_cli, mint)
        except Exception as exc:  # noqa: BLE001
            td.notes.append(f"dex_error:{exc!r}"[:80])
    elif dex_cli is not None:
        td.orders = pd.DataFrame(columns=["kind", "status", "amount", "paid_ms"])   # jamais actif : pas de promotion
    return td


def token_rows(td: TokenData, now_ms: int, costs: Costs = Costs(),
               moments: tuple[str, ...] = MOMENTS) -> list[dict]:
    """Une ligne (variables + étiquettes + P&L) par moment atteint par le token."""
    coin = td.coin
    rows = []
    times = moment_times(td)
    for m in moments:
        t = times.get(m, math.nan)
        if not np.isfinite(t) or t > now_ms:
            continue
        t = int(t)
        key = "launch" if m.startswith("launch") else m
        tr = td.trades.get(key)
        if tr is None:
            continue
        if key == "launch" and "launch_truncated" in td.notes:
            continue
        if m.startswith("launch") and np.isfinite(td.grad_ms) and td.grad_ms <= t:
            continue                                       # déjà gradué : ce n'est plus un lancement
        before = tr[tr["ts"] < t]
        if not m.startswith("launch") and before.empty:
            continue                                       # aucun trade lu avant t : pas de prix de référence
        after = tr[tr["ts"] >= t + 1000 * costs.latency_s]
        feats = features_at(m, t, coin, before, td.candles, td.orders)
        p_ref = feats.get("price_sol", math.nan)
        if not np.isfinite(p_ref):                         # aucun trade avant t : prix de départ de la courbe
            p_ref = float(pf.curve_price(0.0))
            feats["price_sol"] = p_ref
            feats["progress"] = 0.0
        p_entry = float(after["price_sol"].iloc[0]) if len(after) else p_ref
        usd = before[before["price_sol"] > 0]
        sol_usd = float((usd["price_usd"] / usd["price_sol"]).median()) if len(usd) else math.nan
        if not np.isfinite(sol_usd) and len(tr):
            sol_usd = float((tr["price_usd"] / tr["price_sol"]).median())
        row = {"mint": coin["mint"], "symbol": coin.get("symbol"), "creator": coin.get("creator"),
               "created_ms": int(coin["created_timestamp"]), "moment": m, "t_ms": t,
               "p_ref": p_ref, "p_entry": p_entry, "entry_slip": p_entry / p_ref - 1, "sol_usd": sol_usd,
               "grad_ms": td.grad_ms}
        row.update(feats)
        row.update(labels_at(t, now_ms, p_ref, sol_usd, td.candles, td.grad_ms, m))
        row.update(pnl_at(t, now_ms, p_entry, td.candles, costs))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Score d'un modèle exporté par l'étude (détecteur live)
# ---------------------------------------------------------------------------
def score_logit(model: dict, feats: dict[str, float]) -> float:
    """Probabilité de la logistique exportée en JSON (même prétraitement qu'à l'entraînement)."""
    x = np.array([feats.get(f, math.nan) for f in model["features"]], float)
    x = np.where(np.isfinite(x), x, np.asarray(model["median"], float))
    x = np.sign(x) * np.log1p(np.abs(x))
    z = (x - np.asarray(model["mean"], float)) / np.asarray(model["scale"], float)
    s = float(np.dot(z, np.asarray(model["coef"], float)) + model["intercept"])
    return 1.0 / (1.0 + math.exp(-s))
