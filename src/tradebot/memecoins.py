"""Memecoins : la « rotation » entre community coins bat-elle la simple détention ?

Hypothèse testée (fil X « Ultimate Memecoin Trading Guide », 2026) : parmi des memecoins établis,
vendre celui qui vient de faire « 3x » et acheter celui qui est « en bas de sa fourchette »
rapporterait bien plus que de garder ses positions (« Rotator : 10 k$ → 270 k$, Holder : 27 k$ »).

Données (publiques, lecture seule, aucune clé) :

* univers : tous les contrats perpétuels USDT de Binance que Binance classe « Meme »
  (``underlyingSubType``), **y compris ceux retirés de la cote** (statut ``SETTLING``) : pas de
  biais du survivant par rapport à cette cote. Le prix du perpétuel suit le prix spot à quelques
  pb près ; on ne l'utilise que comme prix (pas de levier, pas de financement) ;
* bougies quotidiennes UTC (00:00 → 23:59:59.999) de ``www.binance.com/fapi/v1/klines``
  (``fapi.binance.com`` répond 451 depuis certains serveurs) ; cache dans
  ``data/cache/memecoins/``.

Tout est **point-in-time** : une décision prise à la clôture du jour ``d`` n'utilise que des
données jusqu'à ``d`` inclus et s'exécute le jour ``d + 1`` (à l'ouverture, ≈ la clôture de
``d`` sur un marché ouvert 24 h/24, ou à la clôture de ``d + 1`` en variante prudente), avec un
coût par échange (frais + glissement) appliqué à chaque vente et à chaque achat.

Recherche sur données historiques uniquement : aucun ordre, aucune recommandation.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import kurtosis as _kurt
from scipy.stats import norm, skew as _skew, spearmanr

from tradebot.config import CACHE_DIR

FAPI = "https://www.binance.com/fapi/v1"
CACHE = CACHE_DIR / "memecoins"
DAY_MS = 86_400_000
EULER = 0.5772156649015329


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def _get_json(url: str, params: dict | None = None, tries: int = 5):
    q = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    err = None
    for k in range(tries):
        try:
            req = urllib.request.Request(q, headers={"User-Agent": "tradebot-research/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 — réseau : on réessaie
            err = exc
            time.sleep(2 ** k)
    raise RuntimeError(f"échec {q} : {err!r}")


def fetch_exchange_info(refresh: bool = False) -> dict:
    path = CACHE / "fapi_exchangeInfo.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    info = _get_json(f"{FAPI}/exchangeInfo")
    path.write_text(json.dumps(info))
    return info


def meme_universe(info: dict) -> pd.DataFrame:
    """Perpétuels USDT classés « Meme » par Binance, retirés de la cote compris."""
    rows = [{"symbol": s["symbol"], "base": s["baseAsset"], "status": s["status"],
             "onboard": pd.Timestamp(s["onboardDate"], unit="ms"),
             "tags": ",".join(s.get("underlyingSubType") or [])}
            for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and "Meme" in (s.get("underlyingSubType") or [])]
    return pd.DataFrame(rows).sort_values("onboard").reset_index(drop=True)


def fetch_daily_klines(symbol: str, refresh: bool = False, now_ms: int | None = None) -> pd.DataFrame:
    """Bougies quotidiennes complètes (la bougie du jour en cours est écartée)."""
    path = CACHE / "klines_1d" / f"{symbol}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["date"])
    now_ms = now_ms or int(time.time() * 1000)
    out, start = [], 1_546_300_800_000   # 2019-01-01
    while True:
        rows = _get_json(f"{FAPI}/klines", {"symbol": symbol, "interval": "1d", "limit": 1500, "startTime": start})
        if not rows:
            break
        out += rows
        if len(rows) < 1500:
            break
        start = rows[-1][0] + DAY_MS
        time.sleep(0.3)
    df = pd.DataFrame(out, columns=["open_ms", "open", "high", "low", "close", "volume", "close_ms",
                                    "qvol", "n_trades", "tb_base", "tb_quote", "ignore"])
    df = df[df["close_ms"] < now_ms].copy()
    df["date"] = pd.to_datetime(df["open_ms"], unit="ms").dt.normalize()
    for c in ("open", "high", "low", "close", "volume", "qvol"):
        df[c] = df[c].astype(float)
    df = df[["date", "open", "high", "low", "close", "volume", "qvol", "n_trades"]].drop_duplicates("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    time.sleep(0.3)
    return df


@dataclass
class Panel:
    """Tableaux jour × symbole (index : date UTC de la bougie, qui clôture à 23:59:59 UTC)."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    qvol: pd.DataFrame
    active: pd.DataFrame          # échangé ce jour-là (volume > 0, avant le retrait de la cote)
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index


def build_panel(frames: dict[str, pd.DataFrame], meta: pd.DataFrame | None = None) -> Panel:
    """Assemble les bougies ; un symbole est inactif avant sa 1re bougie, les jours sans volume et
    après son dernier jour échangé (retrait de la cote : les perpétuels « SETTLING » continuent
    d'afficher un prix figé sans volume)."""
    fields = {k: {} for k in ("open", "high", "low", "close", "qvol")}
    for sym, df in frames.items():
        d = df.set_index("date").sort_index()
        for k in fields:
            fields[k][sym] = d[k]
    tabs = {k: pd.DataFrame(v).sort_index() for k, v in fields.items()}
    idx = pd.date_range(tabs["close"].index.min(), tabs["close"].index.max(), freq="D")
    tabs = {k: v.reindex(idx) for k, v in tabs.items()}
    traded = tabs["qvol"].fillna(0) > 0
    last = traded[::-1].cummax()[::-1]          # vrai jusqu'au dernier jour échangé
    active = traded & last & tabs["close"].notna()
    for k in ("open", "high", "low", "close"):
        tabs[k] = tabs[k].where(last)            # rien après le retrait de la cote
    return Panel(tabs["open"], tabs["high"], tabs["low"], tabs["close"], tabs["qvol"], active,
                 meta if meta is not None else pd.DataFrame())


# ---------------------------------------------------------------------------
# Signaux (point-in-time : tout ce qui est calculé au jour d n'utilise que d et avant)
# ---------------------------------------------------------------------------
def rolling_low(close: pd.DataFrame, w: int) -> pd.DataFrame:
    return close.rolling(w, min_periods=w).min()


def range_position(close: pd.DataFrame, w: int) -> pd.DataFrame:
    """(prix − plus bas w j) / (plus haut w j − plus bas w j), fenêtre finissant au jour d inclus."""
    lo = close.rolling(w, min_periods=w).min()
    hi = close.rolling(w, min_periods=w).max()
    return (close - lo) / (hi - lo).where(hi > lo)


def community_flag(close: pd.DataFrame, crash: float = 0.70, rebound: float = 2.0) -> pd.DataFrame:
    """Critère « community coin » de l'auteur, point-in-time : a déjà pris au moins −70 % depuis un
    sommet puis a au moins doublé depuis le creux (état acquis définitivement ensuite)."""
    out = pd.DataFrame(False, index=close.index, columns=close.columns)
    for s in close.columns:
        x = close[s].to_numpy()
        peak, trough, crashed, ok = -np.inf, np.inf, False, False
        flags = np.zeros(x.size, dtype=bool)
        for i, v in enumerate(x):
            if not np.isfinite(v):
                flags[i] = ok
                continue
            if not ok:
                peak = max(peak, v)
                if not crashed and v <= (1 - crash) * peak:
                    crashed, trough = True, v
                if crashed:
                    trough = min(trough, v)
                    if v >= rebound * trough:
                        ok = True
            flags[i] = ok
        out[s] = flags
    return out


def eligibility(panel: Panel, min_age: int = 60, min_qvol: float = 5e6, qvol_win: int = 30,
                community: bool = False) -> pd.DataFrame:
    """Éligible au jour d : échangé ce jour, coté depuis ≥ min_age jours, volume quotidien médian
    (30 j) ≥ min_qvol USDT, et, en option, critère « community coin »."""
    age = panel.active.cumsum()
    med = panel.qvol.where(panel.active).rolling(qvol_win, min_periods=qvol_win // 2).median()
    e = panel.active & (age >= min_age) & (med >= min_qvol)
    if community:
        e &= community_flag(panel.close)
    return e.fillna(False)


# ---------------------------------------------------------------------------
# Moteur de rotation (K emplacements indépendants)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RotationParams:
    mult: float = 3.0          # « a fait 3x » : clôture >= mult × plus bas des low_win derniers jours
    low_win: int = 30
    range_win: int = 60        # fourchette pour « en bas de la fourchette »
    bottom: float = 0.20       # cible : position dans la fourchette <= bottom
    k: int = 2                 # nombre de pièces détenues en même temps
    target: str = "bottom"     # "bottom" (règle de l'auteur), "random" (placebo), "top" (momentum), "none" (détenteur)
    cost: float = 0.0075       # coût par échange (vente OU achat) : frais + glissement
    execution: str = "open"    # "open" : ouverture de d+1 ; "next_close" : clôture de d+1
    delist_haircut: float = 0.0
    seed: int = 0


@dataclass
class SimResult:
    returns: pd.Series          # rendement quotidien du portefeuille (clôture à clôture)
    trades: pd.DataFrame        # une ligne par rotation / achat / sortie forcée
    holdings: pd.DataFrame      # symbole détenu par emplacement, chaque jour


def _prepare(panel: Panel, elig: pd.DataFrame, p: RotationParams):
    C = panel.close.to_numpy(float)
    O = panel.open.to_numpy(float)
    act = panel.active.to_numpy(bool)
    E = elig.reindex_like(panel.close).fillna(False).to_numpy(bool)
    low = rolling_low(panel.close, p.low_win).to_numpy(float)
    with np.errstate(invalid="ignore"):
        trig = C >= p.mult * low
    rpos = range_position(panel.close, p.range_win).to_numpy(float)
    return C, O, act, E, trig & np.isfinite(low), rpos


def _pick(cands: np.ndarray, rpos_row: np.ndarray, rule: str, rng: np.random.Generator,
          require_bottom: float | None) -> int | None:
    if cands.size == 0:
        return None
    if rule == "random":
        return int(rng.choice(cands))
    r = rpos_row[cands]
    ok = np.isfinite(r)
    if not ok.any():
        return None
    cands, r = cands[ok], r[ok]
    if rule == "top":
        return int(cands[np.argmax(r)])
    j = int(np.argmin(r))                       # "bottom" et "none" (entrée) : plus bas de la fourchette
    if require_bottom is not None and r[j] > require_bottom:
        return None
    return int(cands[j])


def simulate_rotation(panel: Panel, elig: pd.DataFrame, p: RotationParams,
                      start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> SimResult:
    """Simule la rotation. Décision à la clôture du jour i, exécution au jour i + 1.

    * emplacement vide (départ, ou après une sortie forcée) : achète la pièce éligible non détenue
      la plus basse dans sa fourchette (règle ``target`` pour "random"/"top") ;
    * emplacement détenu dont la pièce a « fait mult × » : la vend et achète la cible si elle existe
      (pour "bottom", seulement si sa position dans la fourchette est <= ``bottom``), sinon garde ;
    * pièce retirée de la cote le lendemain : sortie forcée à la dernière clôture échangée
      (moins ``delist_haircut``).
    """
    C, O, act, E, trig, rpos = _prepare(panel, elig, p)
    dates = panel.dates
    n, m = C.shape
    i0 = 0 if start is None else int(dates.searchsorted(start))
    i1 = n - 1 if end is None else min(n - 1, int(dates.searchsorted(end, side="right")) - 1)
    rng = np.random.default_rng(p.seed)
    slots: list[int | None] = [None] * p.k
    vals = np.full(p.k, 1.0 / p.k)
    rets = np.full(n, np.nan)
    hold = np.full((n, p.k), -1, dtype=int)
    trades = []
    c = p.cost
    for i in range(i0, i1):
        held = {s for s in slots if s is not None}
        actions: list[tuple[int, int | None, int | None, str]] = []   # (emplacement, vend, achète, motif)
        for j, s in enumerate(slots):
            if s is not None and not act[i + 1, s]:
                actions.append((j, s, None, "retrait"))
                held.discard(s)
        # emplacements vides ou libérés par un retrait : entrée
        for j in range(p.k):
            freed = slots[j] is None or any(a[0] == j for a in actions)
            if not freed:
                continue
            cands = np.array([x for x in np.flatnonzero(E[i]) if x not in held and act[i + 1, x]], dtype=int)
            rule = "bottom" if p.target in ("bottom", "none") else p.target
            b = _pick(cands, rpos[i], rule, rng, None)
            if b is not None:
                held.add(b)
                prev = [a for a in actions if a[0] == j]
                if prev:
                    actions[actions.index(prev[0])] = (j, prev[0][1], b, "retrait")
                else:
                    actions.append((j, None, b, "entrée"))
        # rotations
        if p.target != "none":
            for j, s in enumerate(slots):
                if s is None or any(a[0] == j for a in actions) or not trig[i, s]:
                    continue
                cands = np.array([x for x in np.flatnonzero(E[i]) if x not in held and act[i + 1, x]], dtype=int)
                b = _pick(cands, rpos[i], p.target, rng, p.bottom if p.target == "bottom" else None)
                if b is None:
                    continue
                held.discard(s)
                held.add(b)
                actions.append((j, s, b, "rotation"))
        # exécution au jour i + 1 et valorisation à la clôture de i + 1
        v0 = vals.sum()
        acted = {a[0] for a in actions}
        for j, s in enumerate(slots):
            if j in acted or s is None:
                continue
            vals[j] *= C[i + 1, s] / C[i, s]
        for j, sell, buy, why in actions:
            v = vals[j]
            if sell is not None:
                if why == "retrait":
                    v *= (1.0 - p.delist_haircut) * (1.0 - c)       # vendu à la dernière clôture échangée
                elif p.execution == "open":
                    v *= O[i + 1, sell] / C[i, sell] * (1.0 - c)
                else:
                    v *= C[i + 1, sell] / C[i, sell] * (1.0 - c)
            if buy is not None:
                v *= 1.0 - c
                if p.execution == "open":
                    v *= C[i + 1, buy] / O[i + 1, buy]
                slots[j] = buy
            else:
                slots[j] = None
            vals[j] = v
            trades.append({"date": dates[i + 1], "slot": j, "sell": None if sell is None else panel.symbols[sell],
                           "buy": None if buy is None else panel.symbols[buy], "why": why,
                           "sell_mult": (float(C[i, sell] / np.nanmin(C[max(0, i - p.low_win + 1): i + 1, sell]))
                                         if sell is not None else np.nan),
                           "buy_rpos": float(rpos[i, buy]) if buy is not None else np.nan})
        rets[i + 1] = vals.sum() / v0 - 1.0
        hold[i + 1] = [(-1 if s is None else s) for s in slots]
    idx = dates[i0 + 1: i1 + 1]
    r = pd.Series(rets[i0 + 1: i1 + 1], index=idx, name="ret")
    h = pd.DataFrame(hold[i0 + 1: i1 + 1], index=idx, columns=[f"slot{j}" for j in range(p.k)])
    h = h.apply(lambda col: col.map(lambda x: None if x < 0 else panel.symbols[x]))
    return SimResult(r, pd.DataFrame(trades), h)


def simulate_basket(panel: Panel, elig: pd.DataFrame, cost: float = 0.0075, rebalance_days: int = 30,
                    delist_haircut: float = 0.0, start: pd.Timestamp | None = None,
                    end: pd.Timestamp | None = None) -> SimResult:
    """Panier équipondéré de toutes les pièces éligibles, rééquilibré tous les ``rebalance_days``
    jours (décision à la clôture de d, exécution ≈ clôture de d) ; coût sur la rotation du panier.
    Une pièce retirée de la cote sort à sa dernière clôture échangée, le produit reste en liquidités
    jusqu'au rééquilibrage suivant."""
    C = panel.close.to_numpy(float)
    act = panel.active.to_numpy(bool)
    E = elig.reindex_like(panel.close).fillna(False).to_numpy(bool)
    dates = panel.dates
    n, m = C.shape
    i0 = 0 if start is None else int(dates.searchsorted(start))
    i1 = n - 1 if end is None else min(n - 1, int(dates.searchsorted(end, side="right")) - 1)
    w = np.zeros(m)
    cash = 1.0
    rets = np.full(n, np.nan)
    turn = []
    for k, i in enumerate(range(i0, i1)):
        v_before = w.sum() + cash
        # retraits du lendemain
        gone = (w > 0) & ~act[i + 1]
        if gone.any():
            cash += (w[gone] * (1 - delist_haircut) * (1 - cost)).sum()
            w[gone] = 0.0
        if k % rebalance_days == 0:
            e = np.flatnonzero(E[i] & act[i + 1])
            tot = w.sum() + cash
            tgt = np.zeros(m)
            if e.size:
                tgt[e] = tot / e.size
            tv = np.abs(tgt - w).sum() + abs((0.0 if e.size else tot) - cash)
            fee = cost * np.abs(tgt - w).sum()
            turn.append({"date": dates[i], "n": int(e.size), "turnover": float(tv / tot) if tot else 0.0})
            if e.size:
                w = tgt * (1 - fee / tot)
                cash = 0.0
            else:
                w = np.zeros(m)
                cash = tot - fee
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(w > 0, C[i + 1] / C[i], 1.0)
        w = w * np.nan_to_num(g, nan=1.0)
        rets[i + 1] = (w.sum() + cash) / v_before - 1.0
    idx = dates[i0 + 1: i1 + 1]
    return SimResult(pd.Series(rets[i0 + 1: i1 + 1], index=idx, name="ret"), pd.DataFrame(turn), pd.DataFrame())


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------
def perf(r: pd.Series, periods: int = 365) -> dict:
    r = r.dropna()
    if r.empty:
        return {}
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1
    years = len(r) / periods
    sd = r.std()
    return {"total_x": float(eq.iloc[-1]), "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan,
            "vol": float(sd * math.sqrt(periods)), "sharpe": float(r.mean() / sd * math.sqrt(periods)) if sd > 0 else np.nan,
            "max_dd": float(dd.min()), "days": int(len(r))}


def block_bootstrap(x: np.ndarray, stat, block: int = 30, n_boot: int = 2000, seed: int = 0) -> np.ndarray:
    """Bootstrap par blocs mobiles (circulaire) d'une série temporelle ; renvoie les statistiques."""
    x = np.asarray(x, float)
    n = x.size
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(n / block))
    out = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        out[b] = stat(x[idx])
    return out


def expected_max_sharpe(sr_trials: np.ndarray) -> float:
    """E[max] des Sharpe (par période) de N essais sans talent (Bailey & López de Prado, 2014) :
    √V[SR] · ((1 − γ) Φ⁻¹(1 − 1/N) + γ Φ⁻¹(1 − 1/(N e)))."""
    sr = np.asarray(sr_trials, float)
    sr = sr[np.isfinite(sr)]
    n = sr.size
    if n < 2:
        return 0.0
    return float(np.std(sr, ddof=1) * ((1 - EULER) * norm.ppf(1 - 1 / n) + EULER * norm.ppf(1 - 1 / (n * math.e))))


def probabilistic_sharpe(r: np.ndarray, sr0: float = 0.0) -> float:
    """PSR : P(vrai Sharpe par période > sr0), avec asymétrie et aplatissement des rendements."""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    t = r.size
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = _skew(r), _kurt(r, fisher=False)
    den = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))
    return float(norm.cdf((sr - sr0) * math.sqrt(t - 1) / den))


def deflated_sharpe(r_best: np.ndarray, sr_trials: np.ndarray) -> dict:
    """DSR = PSR du meilleur essai contre le maximum attendu de N essais sans talent
    (tout en Sharpe PAR PÉRIODE, jamais annualisé)."""
    sr0 = expected_max_sharpe(sr_trials)
    return {"sr0_per_period": sr0, "dsr": probabilistic_sharpe(r_best, sr0), "n_trials": int(np.isfinite(sr_trials).sum())}


def forward_stats(panel: Panel, horizon: int) -> dict[str, pd.DataFrame]:
    """Pour chaque (jour d, pièce) : rendement de la clôture de d à la clôture de d + h, plus haut et
    plus bas atteints (clôtures) sur (d, d + h]. Une pièce retirée de la cote est figée à sa dernière
    clôture échangée."""
    c = panel.close.where(panel.active).ffill()     # figé à la dernière clôture échangée après un retrait
    fut = [c.shift(-k) for k in range(1, horizon + 1)]
    stack = np.stack([f.to_numpy(float) for f in fut])
    base = c.to_numpy(float)
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # pièces pas encore cotées : tranches vides
        ret = stack[-1] / base - 1
        mx = np.nanmax(stack, axis=0) / base - 1
        mn = np.nanmin(stack, axis=0) / base - 1
    # horizon incomplet en fin d'échantillon : NaN
    incomplete = np.zeros_like(base, dtype=bool)
    incomplete[-horizon:] = True
    mk = lambda a: pd.DataFrame(np.where(incomplete, np.nan, a), index=c.index, columns=c.columns)  # noqa: E731
    return {"ret": mk(ret), "max": mk(mx), "min": mk(mn)}


def cross_sectional_ic(signal: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame, min_n: int = 6) -> pd.Series:
    """Corrélation de Spearman, jour par jour, entre un signal et le rendement futur, sur les pièces
    éligibles."""
    out = {}
    for d in signal.index:
        m = mask.loc[d].to_numpy(bool) & np.isfinite(signal.loc[d].to_numpy(float)) & np.isfinite(fwd.loc[d].to_numpy(float))
        if m.sum() >= min_n:
            out[d] = spearmanr(signal.loc[d].to_numpy(float)[m], fwd.loc[d].to_numpy(float)[m]).statistic
    return pd.Series(out, dtype=float)


def newey_west_t(x: pd.Series, lags: int) -> float:
    """t de Student de la moyenne avec variance HAC (Newey-West, noyau de Bartlett)."""
    v = x.dropna().to_numpy(float)
    n = v.size
    if n < 3:
        return float("nan")
    e = v - v.mean()
    s = e @ e / n
    for l in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - l / (lags + 1)) * (e[l:] @ e[:-l]) / n
    return float(v.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def with_params(p: RotationParams, **kw) -> RotationParams:
    return replace(p, **kw)


def load_all(refresh: bool = False, extra: tuple[str, ...] = ("BTCUSDT", "SOLUSDT", "ETHUSDT")) -> tuple[Panel, pd.DataFrame, Panel]:
    """(panel memecoins, univers, panel de référence BTC/SOL/ETH), depuis le cache ou le réseau."""
    info = fetch_exchange_info(refresh=refresh)
    uni = meme_universe(info)
    frames = {s: fetch_daily_klines(s, refresh=refresh) for s in uni["symbol"]}
    frames = {s: f for s, f in frames.items() if len(f)}
    ref = {s: fetch_daily_klines(s, refresh=refresh) for s in extra}
    return build_panel(frames, uni.set_index("symbol")), uni, build_panel(ref)


def save_path(name: str) -> Path:
    return CACHE / name
