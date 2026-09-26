"""Simulateur **maker historique** sur les trades preneurs des marchés Polymarket « Up or Down »
(``data-api /v2/trades``, une ligne par transaction, côté preneur, horodatage du bloc Polygon à la
seconde ; cache de :mod:`tradebot.polymarket_wallets`). Simulation papier : aucun ordre, aucune clé.

Modèle d'exécution (documenté ici, testé dans ``tests/test_polymarket_maker.py``)
--------------------------------------------------------------------------------
Un ordre au repos « acheter ``Q`` parts du jeton ``X`` (Up ou Down) au prix ``L`` » est posé à
``t0`` et annulé à ``t1``. Dans le CLOB de Polymarket, un bid ``X`` à ``L`` est équivalent à un ask
de l'autre jeton ``Y`` à ``1 − L`` (appariement par création : le preneur qui achète ``Y`` à ``1 − L``
et nous qui achetons ``X`` à ``L`` payons 1 $ à nous deux). Les trades preneurs qui peuvent
consommer notre niveau sont donc les **ventes preneurs de X à un prix ≤ L** et les **achats
preneurs de Y à un prix ≥ 1 − L** (« prix de dégagement » ``cp`` = prix de la vente de X, ou
``1 −`` prix de l'achat de Y). L'ordre est exécuté, dans l'ordre chronologique des trades
(horodatage du bloc, puis ``seq``), à la première des deux conditions :

* (i) **traversée** : un trade preneur dépasse strictement le niveau (``cp < L``) : par priorité de
  prix, tout ordre au repos à ``L`` est servi avant que le preneur n'aille plus loin ;
* (ii) **file** : le cumul depuis ``t0`` des tailles des trades consommant le niveau (``cp ≤ L``)
  dépasse ``Q_ahead + Q``, ``Q_ahead`` étant la file devant nous à la pose (paramètre : mesuré sur
  le carnet réel dans ``reports/polymarket/maker_live/``, ou ``0`` / 500 / 2 000 / 5 000 parts).

Variante **conservatrice** : (i) seule ; variante **optimiste** : ``Q_ahead = 0``. L'exécution est
tout ou rien (pas de partiel). Résolution : 1 $ par part si ``X`` gagne. Remise maker estimée à
part : ``0,2 × frais preneur au prix L`` par part (la remise du marché est 20 % des frais preneurs,
répartie au prorata des « frais équivalents » ``0,07·p(1−p)`` des makers), plafonnée à 20 % des
frais preneurs du marché.

**Délai on-chain** : un trade n'est daté que par son bloc (≈ 2,2 s après l'appariement, médiane,
cf. ``docs/research/polymarket_temps_reel.md``) et notre ordre n'est visible qu'après ≈ 0,3 s.
Les deux sont regroupés dans ``delay_s`` (3 s par défaut) : l'ordre ne voit que les trades dont
l'horodatage de bloc est dans ``[t0 + delay, t1 + delay)``. Il ne voit **rien d'autre** que ces
trades (ni le carnet, ni l'issue) : c'est ce que teste ``test_no_leak``.

**Ordres croisants** : sans carnet historique, un bid ``X`` à ``L`` qui serait immédiatement
exécutable (``ask_X ≤ L``, donc un ordre preneur, avec frais) est détecté par les trades des
``cross_lookback_s`` secondes précédentes : un achat preneur de ``X`` à un prix ``≤ L`` (ou une vente
preneur de ``Y`` à ``≥ 1 − L``) prouve que l'ask de ``X`` était ``≤ L``. Ces ordres sont écartés
(``crossing``) et comptés à part ; avant l'ouverture s'y ajoute la règle du milieu
``prices-history`` (``L ≥ milieu_X + 0,005`` -> croisant, comme dans ``modeles_vs_marche``).

Stratégies (fonctions ``orders_*`` : une ligne par ordre) : (a) ``signal`` : côté favorisé par un
modèle à S−30 s, prix 0,47…0,50, annulation à S, S+30 s, S+60 s ou E ; (b) ``two_sided`` : achat
Up et achat Down au même prix avant l'ouverture ; (c) ``fair_value`` : juste valeur
``Φ(d/σ_restant)`` (``d`` = log(spot Binance 1 s / TWAP60(S)), ``σ`` EWMA 1 s ou TimesFM),
bid Up à ``p̂ − 1 c`` et bid Down à ``(1 − p̂) − 1 c`` re-cotés toutes les 10 s de S+30 s à E−60 s ;
(d) = (a) et (b) restreints par quintile d'amplitude TimesFM (filtre appliqué en aval).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from tradebot.config import CACHE_DIR
from tradebot.polymarket import taker_fee
from tradebot.polymarket_wallets import MAKER_REBATE_RATE, WALLETS_CACHE_DIR

__all__ = [
    "MarketTrades", "load_taker_trades", "iter_market_trades", "simulate_orders", "fill_time", "would_cross",
    "ewma_sigma_1s", "fair_value_probs", "quote_times", "orders_fair_value", "orders_signal", "orders_two_sided",
    "model_side", "score_orders", "score_frame", "summarize", "pair_summary", "measured_q_ahead", "fetch_binance_1s_closes",
    "MEASURED_Q_AHEAD", "Q_AHEAD_GRID", "Q_AHEAD_IN_WINDOW", "DEFAULT_SIZE", "ONCHAIN_DELAY_S",
    "CROSS_LOOKBACK_S", "RULE_BOTH", "RULE_TRAVERSAL", "RULE_QUEUE", "SIGNAL_PRICES", "SIGNAL_CANCELS",
    "TWO_SIDED_PRICES", "TWO_SIDED_CANCELS", "FV_STEP_S", "FV_START_S", "FV_END_BEFORE_S", "FV_MARGIN",
    "BINANCE_1S_DIR", "TRADE_COLUMNS",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
DEFAULT_SIZE = 100.0            # parts par ordre
ONCHAIN_DELAY_S = 3             # visibilité de l'ordre (0,3 s) + appariement -> bloc (médiane 2,2 s)
CROSS_LOOKBACK_S = 10           # trades révélant un ask <= L avant la pose
PRICE_EPS = 1e-6
RULE_BOTH, RULE_TRAVERSAL, RULE_QUEUE = 0, 1, 2
RULE_NAMES = {RULE_BOTH: "(i)+(ii)", RULE_TRAVERSAL: "(i) seule", RULE_QUEUE: "(ii) seule"}
Q_AHEAD_GRID = (0.0, 500.0, 2000.0, 5000.0)
SIGNAL_PRICES = (0.47, 0.48, 0.49, 0.50)
SIGNAL_CANCELS = (0, 30, 60, "E")          # annulation à S, S+30 s, S+60 s, E
SIGNAL_PLACE_OFFSET_S = -30
TWO_SIDED_PRICES = (0.49, 0.48)
TWO_SIDED_CANCELS = (0, 30)
TWO_SIDED_PLACE_OFFSET_S = -60
FV_STEP_S = 10
FV_START_S = 30
FV_END_BEFORE_S = 60
FV_MARGIN = 0.01
FV_LEVEL_MIN, FV_LEVEL_MAX = 0.05, 0.95
FV_TAIL_VAR_S = 20.0            # variance (en secondes de σ²) de la moyenne des 60 dernières secondes
SIGMA_FLOOR_1S = 1e-6
BINANCE_1S_DIR = CACHE_DIR / "pm_maker" / "binance_1s"
TRADE_COLUMNS = ("ts", "side", "outcome", "price", "size", "seq", "slug", "condition_id")

# File d'attente mesurée sur le carnet réel (reports/polymarket/maker_live/README.md § 3, 18 marchés
# BTC/ETH 5m et BTC 15m du 26/09/2026) : médiane de la taille affichée au niveau QUAND il est présent,
# avant S (instants S−60 s et S−30 s) et après S (S, S+30 s). Le niveau 0,47 n'a pas été mesuré :
# on lui donne la valeur de 0,48. Tous jetons confondus : après S ≈ 100 parts (Q_AHEAD_IN_WINDOW).
MEASURED_Q_AHEAD: dict[tuple[str, str, float], float] = {
    ("pre", "up", 0.47): 100.0, ("pre", "up", 0.48): 100.0, ("pre", "up", 0.49): 70.0, ("pre", "up", 0.50): 179.0,
    ("pre", "down", 0.47): 91.0, ("pre", "down", 0.48): 91.0, ("pre", "down", 0.49): 59.0, ("pre", "down", 0.50): 25.0,
    ("post", "up", 0.47): 114.0, ("post", "up", 0.48): 114.0, ("post", "up", 0.49): 138.0, ("post", "up", 0.50): 108.0,
    ("post", "down", 0.47): 36.0, ("post", "down", 0.48): 36.0, ("post", "down", 0.49): 25.0, ("post", "down", 0.50): 76.0,
}
Q_AHEAD_IN_WINDOW = 100.0


_EPOCH = pd.Timestamp(0, tz="UTC")


def _unix_seconds(values) -> np.ndarray:
    """Horodatages (toute unité, naïfs = UTC) -> secondes Unix int64."""
    idx = pd.DatetimeIndex(values)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return ((idx.tz_convert("UTC") - _EPOCH) // pd.Timedelta("1s")).to_numpy().astype("int64")


def measured_q_ahead(side: str, level: float, phase: str = "pre",
                     table: Mapping[tuple[str, str, float], float] = MEASURED_Q_AHEAD) -> float:
    """File mesurée (parts) devant un bid ``side`` à ``level`` ; niveau inconnu -> :data:`Q_AHEAD_IN_WINDOW`."""
    return float(table.get((phase, side, round(level, 2)), Q_AHEAD_IN_WINDOW))


# ---------------------------------------------------------------------------
# Trades preneurs d'un marché
# ---------------------------------------------------------------------------
@dataclass
class MarketTrades:
    """Trades preneurs d'un marché, triés par (horodatage du bloc, ``seq``).

    * ``t`` : horodatage du bloc (s Unix) ; ``price`` / ``size`` : prix moyen et taille (parts) du
      preneur, sur son jeton ;
    * ``cp_up`` / ``cp_dn`` : prix de dégagement pour un bid Up / un bid Down (``+inf`` si le trade
      ne peut pas consommer ce côté) : vente preneur du jeton -> son prix ; achat preneur de
      l'autre jeton -> ``1 − prix`` ;
    * ``ap_up`` / ``ap_dn`` : borne supérieure de l'ask Up / Down révélée par le trade (achat preneur
      du jeton -> son prix ; vente preneur de l'autre jeton -> ``1 − prix`` ; ``+inf`` sinon).
    """

    slug: str
    start_ts: int
    end_ts: int
    t: np.ndarray
    price: np.ndarray
    size: np.ndarray
    cp_up: np.ndarray
    cp_dn: np.ndarray
    ap_up: np.ndarray
    ap_dn: np.ndarray

    @classmethod
    def from_frame(cls, df: pd.DataFrame, slug: str, start_ts: int, end_ts: int) -> "MarketTrades":
        """``df`` : colonnes ``ts`` (datetime UTC ou s Unix), ``side`` (BUY/SELL), ``outcome``
        (Up/Down), ``price``, ``size`` et, si présente, ``seq`` (ordre on-chain dans la seconde)."""
        if len(df) == 0:
            e = np.zeros(0)
            return cls(slug, int(start_ts), int(end_ts), np.zeros(0, dtype="int64"), e, e, e, e, e, e)
        ts = df["ts"]
        if isinstance(ts.dtype, pd.DatetimeTZDtype) or np.issubdtype(ts.dtype, np.datetime64):
            t = _unix_seconds(ts)
        else:
            t = np.asarray(ts, dtype="int64")
        seq = df["seq"].to_numpy(dtype="int64") if "seq" in df.columns else np.arange(len(df))
        order = np.lexsort((seq, t))
        t = t[order]
        price = df["price"].to_numpy(dtype="float64")[order]
        size = df["size"].to_numpy(dtype="float64")[order]
        side = df["side"].astype(str).str.upper().to_numpy()[order]
        outcome = df["outcome"].astype(str).str.lower().to_numpy()[order]
        buy = side == "BUY"
        sell = side == "SELL"
        up = outcome == "up"
        dn = outcome == "down"
        inf = np.full(len(t), np.inf)
        cp_up = np.where(sell & up, price, np.where(buy & dn, 1.0 - price, inf))
        cp_dn = np.where(sell & dn, price, np.where(buy & up, 1.0 - price, inf))
        ap_up = np.where(buy & up, price, np.where(sell & dn, 1.0 - price, inf))
        ap_dn = np.where(buy & dn, price, np.where(sell & up, 1.0 - price, inf))
        return cls(slug, int(start_ts), int(end_ts), t, price, size, cp_up, cp_dn, ap_up, ap_dn)

    @property
    def n(self) -> int:
        return int(len(self.t))

    def taker_fees_total(self, fee_schedule: dict | None) -> float:
        """Somme des frais preneurs du marché (USDC), barème ``fee_schedule``."""
        if self.n == 0:
            return 0.0
        return float(np.nansum(taker_fee(self.price, self.size, fee_schedule, decimals=None)))

    def taker_shares_total(self) -> float:
        return float(self.size.sum()) if self.n else 0.0

    def cp(self, side: str) -> np.ndarray:
        return self.cp_up if side == "up" else self.cp_dn

    def ap(self, side: str) -> np.ndarray:
        return self.ap_up if side == "up" else self.ap_dn


def _cache_files(cache_dir: Path, asset: str, duration: str, day: str) -> list[tuple[Path, Path]]:
    base = Path(cache_dir) / "trades"
    out = []
    for mode in ("taker", "all"):
        stem = f"{asset}_{duration}_{day}_{mode}"
        p, m = base / f"{stem}.parquet", base / f"{stem}.json"
        if p.exists() and m.exists():
            out.append((p, m))
    return out


def load_taker_trades(asset: str, duration: str, day: str, cache_dir: Path = WALLETS_CACHE_DIR,
                      columns: Sequence[str] = TRADE_COLUMNS) -> tuple[pd.DataFrame, set[str]]:
    """Trades preneurs d'une série-jour depuis le cache parquet (``_taker`` puis, pour les
    conditions qu'il ne couvre pas, ``_all`` filtré sur ``role == 'taker'``).

    Retourne ``(trades, conditions couvertes)`` ; ``trades`` vide si aucun fichier.
    """
    parts: list[pd.DataFrame] = []
    covered: set[str] = set()
    for data_path, meta_path in _cache_files(cache_dir, asset, duration, day):
        meta = json.loads(meta_path.read_text())
        conds = set(map(str, (meta.get("conditions") or {}).keys()))
        need = conds - covered
        if not need:
            continue
        cols = [c for c in columns if c != "role"] + ["role"]
        df = pd.read_parquet(data_path, columns=cols)
        df = df[df["condition_id"].astype(str).isin(need)]
        if meta.get("mode") == "all":
            df = df[df["role"].astype(str) == "taker"]
        parts.append(df.drop(columns=["role"]))
        covered |= need
    if not parts:
        return pd.DataFrame(columns=list(columns)), covered
    return pd.concat(parts, ignore_index=True), covered


def iter_market_trades(markets: pd.DataFrame, cache_dir: Path = WALLETS_CACHE_DIR,
                       loader: Callable[..., tuple[pd.DataFrame, set[str]]] | None = None,
                       ) -> Iterator[tuple[pd.Series, MarketTrades]]:
    """Itère ``(ligne de marché, MarketTrades)`` sur ``markets`` (colonnes ``slug``, ``asset``,
    ``duration``, ``start_ts``, ``end_ts``), en lisant le cache jour par jour (mémoire bornée).
    Les marchés absents du cache reçoivent un :class:`MarketTrades` vide."""
    loader = loader or load_taker_trades
    m = markets.copy()
    m["_day"] = pd.to_datetime(m["start_ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    for (asset, duration, day), grp in m.groupby(["asset", "duration", "_day"], sort=True):
        df, _ = loader(asset, duration, day, cache_dir)
        groups = {k: v for k, v in df.groupby("slug", observed=True)} if len(df) else {}
        for _, row in grp.iterrows():
            sub = groups.get(row["slug"])
            if sub is None:
                sub = df.iloc[0:0]
            yield row, MarketTrades.from_frame(sub, row["slug"], int(row["start_ts"]), int(row["end_ts"]))


# ---------------------------------------------------------------------------
# Modèle d'exécution
# ---------------------------------------------------------------------------
def fill_time(mt: MarketTrades, side: str, level: float, t0: float, t1: float, q_ahead: float,
              size: float, rule: int = RULE_BOTH) -> tuple[float, int, float]:
    """Instant d'exécution d'un bid ``side`` à ``level`` vivant sur ``[t0, t1)`` (horodatages de
    bloc, déjà décalés du délai on-chain).

    Retourne ``(t_fill, raison, consommé)`` : ``raison`` 0 = non exécuté (``t_fill`` NaN),
    1 = traversée (règle (i)), 2 = file (règle (ii)) ; ``consommé`` = volume preneur ayant
    consommé le niveau depuis ``t0`` jusqu'à l'exécution incluse (ou jusqu'à ``t1``).
    """
    t = mt.t
    i0 = int(np.searchsorted(t, t0, side="left"))
    i1 = int(np.searchsorted(t, t1, side="left"))
    if i1 <= i0:
        return math.nan, 0, 0.0
    cp = mt.cp(side)[i0:i1]
    consuming = cp <= level + PRICE_EPS
    cum = np.cumsum(mt.size[i0:i1] * consuming)
    i_best, reason = -1, 0
    if rule in (RULE_BOTH, RULE_TRAVERSAL):
        beyond = cp < level - PRICE_EPS
        if beyond.any():
            i_best, reason = int(np.argmax(beyond)), 1
    if rule in (RULE_BOTH, RULE_QUEUE):
        need = q_ahead + size - PRICE_EPS
        if cum[-1] >= need:
            iq = int(np.searchsorted(cum, need, side="left"))
            if i_best < 0 or iq < i_best:
                i_best, reason = iq, 2
    if i_best < 0:
        return math.nan, 0, float(cum[-1])
    return float(t[i0 + i_best]), reason, float(cum[i_best])


def would_cross(mt: MarketTrades, side: str, level: float, t0: float, lookback_s: float = CROSS_LOOKBACK_S) -> bool:
    """Vrai si un trade preneur de ``[t0 − lookback, t0)`` révèle un ask du jeton ``≤ level``
    (l'ordre serait immédiatement exécutable : ordre preneur, pas ordre au repos)."""
    t = mt.t
    i0 = int(np.searchsorted(t, t0 - lookback_s, side="left"))
    i1 = int(np.searchsorted(t, t0, side="left"))
    if i1 <= i0:
        return False
    return bool((mt.ap(side)[i0:i1] <= level + PRICE_EPS).any())


def simulate_orders(mt: MarketTrades, orders: pd.DataFrame, *, delay_s: float = ONCHAIN_DELAY_S,
                    cross_lookback_s: float = CROSS_LOOKBACK_S, check_cross: bool = True) -> pd.DataFrame:
    """Exécute des ordres au repos sur les trades de ``mt``.

    ``orders`` : colonnes ``side`` (``"up"``/``"down"``), ``price``, ``t_place``, ``t_cancel``
    (s Unix, temps de décision), ``q_ahead``, ``size`` et, facultatives, ``rule``
    (:data:`RULE_BOTH` par défaut) et ``crossing`` (croisement déjà établi, ex. règle du milieu).
    Ajoute ``crossing``, ``placed``, ``t_fill``, ``delay_fill_s`` (depuis ``t_place``),
    ``fill_reason`` (``""``, ``"traversal"``, ``"queue"``), ``consumed`` et ``executed``.
    L'ordre ne voit que les trades d'horodatage de bloc dans ``[t_place + delay, t_cancel + delay)``.
    """
    n = len(orders)
    out = orders.copy()
    side = out["side"].astype(str).to_numpy()
    price = out["price"].to_numpy(dtype="float64")
    tp = out["t_place"].to_numpy(dtype="float64")
    tc = out["t_cancel"].to_numpy(dtype="float64")
    qa = out["q_ahead"].to_numpy(dtype="float64")
    sz = out["size"].to_numpy(dtype="float64")
    rule = out["rule"].to_numpy(dtype="int64") if "rule" in out.columns else np.zeros(n, dtype="int64")
    cross0 = out["crossing"].to_numpy(dtype=bool) if "crossing" in out.columns else np.zeros(n, dtype=bool)
    t_fill = np.full(n, np.nan)
    reason = np.zeros(n, dtype="int8")
    consumed = np.zeros(n)
    crossing = cross0.copy()
    for i in range(n):
        t0 = tp[i] + delay_s
        if check_cross and not crossing[i] and would_cross(mt, side[i], price[i], t0, cross_lookback_s):
            crossing[i] = True
        if crossing[i] or not (tc[i] > tp[i]):
            continue
        t_fill[i], reason[i], consumed[i] = fill_time(mt, side[i], price[i], t0, tc[i] + delay_s, qa[i], sz[i], int(rule[i]))
    out["crossing"] = crossing
    out["placed"] = ~crossing
    out["t_fill"] = t_fill
    out["delay_fill_s"] = t_fill - tp
    out["fill_reason"] = np.array(["", "traversal", "queue"], dtype=object)[reason]
    out["consumed"] = consumed
    out["executed"] = np.isfinite(t_fill)
    return out


# ---------------------------------------------------------------------------
# Juste valeur Φ(d/σ) à partir des closes Binance 1 s
# ---------------------------------------------------------------------------
def fetch_binance_1s_closes(symbol: str, days: Iterable[pd.Timestamp], cache_dir: Path = BINANCE_1S_DIR,
                            fetch_day: Callable | None = None) -> pd.DataFrame:
    """Closes Binance 1 s des jours UTC ``days`` (zips journaliers ``data.binance.vision`` via
    :func:`tradebot.polymarket_backtest._day_1s`, injectable), cache parquet par jour
    (colonnes ``sec`` s Unix, ``close``). Secondes manquantes : close reporté.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if fetch_day is None:
        from tradebot.polymarket_backtest import _day_1s

        fetch_day = _day_1s
    parts = []
    for day in days:
        day = pd.Timestamp(day).tz_convert("UTC") if pd.Timestamp(day).tzinfo else pd.Timestamp(day).tz_localize("UTC")
        path = cache_dir / f"{symbol}_{day:%Y-%m-%d}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path))
            continue
        raw = fetch_day(symbol, day)
        if raw is None or raw.empty:
            log.warning("%s 1s %s : aucune donnée", symbol, day.date())
            continue
        sec0 = int(day.timestamp())
        s = _unix_seconds(raw.index) - sec0
        ok = (s >= 0) & (s < 86400)
        close = np.full(86400, np.nan)
        close[s[ok]] = raw["close"].to_numpy(dtype="float64")[ok]
        close = pd.Series(close).ffill().bfill().to_numpy()
        df = pd.DataFrame({"sec": sec0 + np.arange(86400, dtype="int64"), "close": close})
        if ok.sum() >= 86400 * 0.95:
            df.to_parquet(path)         # jour complet : immuable
        else:
            log.warning("%s 1s %s : %d secondes seulement (non mis en cache)", symbol, day.date(), int(ok.sum()))
        parts.append(df)
    if not parts:
        return pd.DataFrame({"sec": np.zeros(0, dtype="int64"), "close": np.zeros(0)})
    return pd.concat(parts, ignore_index=True).drop_duplicates("sec").sort_values("sec").reset_index(drop=True)


def ewma_sigma_1s(close: np.ndarray, halflife_s: float, floor: float = SIGMA_FLOOR_1S) -> np.ndarray:
    """Écart-type EWMA (demi-vie ``halflife_s``) des rendements log 1 s, aligné sur ``close``
    (la valeur en ``i`` n'utilise que les closes ``<= i`` : causal)."""
    c = np.asarray(close, dtype="float64")
    r2 = np.diff(np.log(c), prepend=np.nan) ** 2
    var = pd.Series(r2).ewm(halflife=halflife_s, min_periods=10).mean().to_numpy()
    return np.maximum(np.sqrt(np.nan_to_num(var, nan=floor ** 2)), floor)


def fair_value_probs(spot: np.ndarray, K: float, sigma_1s: np.ndarray | float, t: np.ndarray, end_ts: int,
                     tail_var_s: float = FV_TAIL_VAR_S, clip: tuple[float, float] = (0.01, 0.99)) -> np.ndarray:
    """``P(Up)`` = Φ(d / σ_restant) aux instants ``t`` (``t ≤ E − 60``), avec ``d = log(spot / K)``
    (``K`` = TWAP60(S)) et ``var_restant = σ_1s² × ((E − 60 − t) + tail_var_s)`` : marche aléatoire
    jusqu'au début de la dernière minute, puis variance de la moyenne des 60 dernières secondes
    (≈ 20 σ²). ``σ_1s`` : scalaire (TimesFM / EWMA à S) ou tableau aligné sur ``t`` (EWMA 1 s)."""
    spot = np.asarray(spot, dtype="float64")
    t = np.asarray(t, dtype="float64")
    sig = np.broadcast_to(np.asarray(sigma_1s, dtype="float64"), spot.shape)
    remaining = np.maximum(end_ts - 60 - t, 0.0) + tail_var_s
    sd = sig * np.sqrt(remaining)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.log(spot / K)
        p = norm.cdf(np.where(sd > 0, d / np.where(sd > 0, sd, 1.0), np.sign(d) * np.inf))
    p = np.where(np.isfinite(d) & np.isfinite(sig), p, np.nan)
    return np.clip(p, clip[0], clip[1])


def quote_times(start_ts: int, end_ts: int, step_s: int = FV_STEP_S, start_offset_s: int = FV_START_S,
                end_before_s: int = FV_END_BEFORE_S) -> np.ndarray:
    """Instants de cotation ``S + start_offset, …`` (pas ``step_s``), strictement avant ``E − end_before``."""
    return np.arange(start_ts + start_offset_s, end_ts - end_before_s, step_s, dtype="int64")


def _floor_cent(x: float, tick: float = 0.01) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 2)


def orders_fair_value(times: np.ndarray, p_up: np.ndarray, t_stop: float, *, margin: float = FV_MARGIN,
                      size: float = DEFAULT_SIZE, q_ahead: float = Q_AHEAD_IN_WINDOW,
                      level_min: float = FV_LEVEL_MIN, level_max: float = FV_LEVEL_MAX,
                      strategy: str = "fair_value", label: str = "") -> pd.DataFrame:
    """(c) À chaque instant ``times[k]`` : bid Up à ``p̂ − margin`` et bid Down à ``(1 − p̂) − margin``
    (cent inférieur). Un ordre est **conservé** tant que son niveau ne change pas (même file),
    sinon annulé à l'instant suivant et remplacé ; tout est annulé à ``t_stop``."""
    rows = []
    current: dict[str, list] = {}
    times = np.asarray(times, dtype="int64")
    for k, t in enumerate(times):
        p = float(p_up[k]) if k < len(p_up) else math.nan
        want: dict[str, float] = {}
        if np.isfinite(p):
            lu, ld = _floor_cent(p - margin), _floor_cent((1.0 - p) - margin)
            if level_min <= lu <= level_max:
                want["up"] = lu
            if level_min <= ld <= level_max:
                want["down"] = ld
        for side in ("up", "down"):
            cur = current.get(side)
            w = want.get(side)
            if cur is not None and (w is None or abs(w - cur[0]) > 1e-9):
                rows.append({"strategy": strategy, "label": label, "side": side, "price": cur[0], "t_place": cur[1],
                             "t_cancel": float(t), "q_ahead": q_ahead, "size": size, "p_up": cur[2], "n_quotes": cur[3]})
                current.pop(side)
                cur = None
            if cur is None and w is not None:
                current[side] = [w, float(t), p, 1]
            elif cur is not None:
                cur[3] += 1
    for side, (lvl, t0, p, nq) in current.items():
        rows.append({"strategy": strategy, "label": label, "side": side, "price": lvl, "t_place": t0,
                     "t_cancel": float(t_stop), "q_ahead": q_ahead, "size": size, "p_up": p, "n_quotes": nq})
    cols = ["strategy", "label", "side", "price", "t_place", "t_cancel", "q_ahead", "size", "p_up", "n_quotes"]
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Stratégies (a) et (b)
# ---------------------------------------------------------------------------
def model_side(p_up: float, margin: float = 0.0) -> str:
    """Côté favorisé : ``"up"`` si ``p ≥ 0,5 + margin``, ``"down"`` si ``p ≤ 0,5 − margin``, ``""`` sinon
    (avec ``margin = 0`` : Up ssi ``p ≥ 0,5``, sinon Down : toujours en position)."""
    if not np.isfinite(p_up):
        return ""
    if p_up >= 0.5 + margin:
        return "up"
    if p_up <= 0.5 - margin:
        return "down"
    return ""


def _cancel_ts(start_ts: int, end_ts: int, cancel) -> float:
    return float(end_ts) if cancel == "E" else float(start_ts + int(cancel))


def _cancel_label(cancel) -> str:
    return "E" if cancel == "E" else f"S+{int(cancel)}"


def _mid_crossing(side: str, level: float, p_pre: float) -> bool:
    """Règle du milieu : bid ``X`` à ``L`` croisant si ``L ≥ milieu_X + 0,005`` (ask ≈ milieu + 0,005)."""
    if not np.isfinite(p_pre):
        return False
    mid = p_pre if side == "up" else 1.0 - p_pre
    return level >= mid + 0.005 - PRICE_EPS


def orders_signal(start_ts: int, end_ts: int, side: str, *, prices: Sequence[float] = SIGNAL_PRICES,
                  cancels: Sequence = SIGNAL_CANCELS, size: float = DEFAULT_SIZE,
                  place_offset_s: int = SIGNAL_PLACE_OFFSET_S, q_ahead: Callable[[str, float], float] | float | None = None,
                  p_pre: float = math.nan, strategy: str = "signal", label: str = "") -> pd.DataFrame:
    """(a) Un ordre par (prix, annulation) du côté ``side`` (rien si ``side == ""``), posé à
    ``S + place_offset_s``. ``q_ahead`` : fonction ``(side, level) -> parts`` (défaut :
    :func:`measured_q_ahead` avant S) ou scalaire. ``p_pre`` : milieu Up à S−30 s pour la règle
    du milieu (colonne ``crossing``)."""
    cols = ["strategy", "label", "side", "price", "cancel", "t_place", "t_cancel", "q_ahead", "size", "crossing"]
    if not side:
        return pd.DataFrame(columns=cols)
    qf = q_ahead if callable(q_ahead) else (lambda s, lv, _q=q_ahead: measured_q_ahead(s, lv, "pre") if _q is None else float(_q))
    rows = []
    for L in prices:
        for c in cancels:
            rows.append({"strategy": strategy, "label": label or f"{L:.2f}/{_cancel_label(c)}", "side": side,
                         "price": float(L), "cancel": _cancel_label(c), "t_place": float(start_ts + place_offset_s),
                         "t_cancel": _cancel_ts(start_ts, end_ts, c), "q_ahead": qf(side, float(L)), "size": size,
                         "crossing": _mid_crossing(side, float(L), p_pre)})
    return pd.DataFrame(rows, columns=cols)


def orders_two_sided(start_ts: int, end_ts: int, *, prices: Sequence[float] = TWO_SIDED_PRICES,
                     cancels: Sequence = TWO_SIDED_CANCELS, size: float = DEFAULT_SIZE,
                     place_offset_s: int = TWO_SIDED_PLACE_OFFSET_S, q_ahead: Callable[[str, float], float] | float | None = None,
                     p_pre: float = math.nan, strategy: str = "two_sided") -> pd.DataFrame:
    """(b) Achat Up **et** achat Down au même prix, une paire par (prix, annulation) ; colonne
    ``pair`` = identifiant de la paire dans le marché."""
    qf = q_ahead if callable(q_ahead) else (lambda s, lv, _q=q_ahead: measured_q_ahead(s, lv, "pre") if _q is None else float(_q))
    rows = []
    for L in prices:
        for c in cancels:
            lab = f"{L:.2f}/{_cancel_label(c)}"
            for side in ("up", "down"):
                rows.append({"strategy": strategy, "label": lab, "side": side, "price": float(L), "cancel": _cancel_label(c),
                             "t_place": float(start_ts + place_offset_s), "t_cancel": _cancel_ts(start_ts, end_ts, c),
                             "q_ahead": qf(side, float(L)), "size": size, "crossing": _mid_crossing(side, float(L), p_pre),
                             "pair": lab})
    cols = ["strategy", "label", "side", "price", "cancel", "t_place", "t_cancel", "q_ahead", "size", "crossing", "pair"]
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# P&L et résumés
# ---------------------------------------------------------------------------
def score_orders(df: pd.DataFrame, resolved_up: float | bool | None, fee_schedule: dict | None,
                 taker_fees_total: float = math.inf, rebate_rate: float = MAKER_REBATE_RATE) -> pd.DataFrame:
    """Ajoute ``would_win`` (1 si le côté acheté gagne), ``pnl_per_share`` (``1{gagné} − L`` si
    exécuté, NaN sinon), ``pnl_per_placed_share`` (0 si non exécuté), ``pnl_order`` ($) et
    ``rebate_per_share`` (``rebate_rate × frais preneur à L``, la somme par marché étant plafonnée
    à ``rebate_rate × taker_fees_total``)."""
    out = df.copy()
    n = len(out)
    res = math.nan if resolved_up is None or (isinstance(resolved_up, float) and math.isnan(resolved_up)) else float(bool(resolved_up))
    side_up = (out["side"].astype(str) == "up").to_numpy() if n else np.zeros(0, dtype=bool)
    win = np.where(side_up, res, 1.0 - res) if n else np.zeros(0)
    price = out["price"].to_numpy(dtype="float64") if n else np.zeros(0)
    ex = out["executed"].to_numpy(dtype=bool) if n else np.zeros(0, dtype=bool)
    placed = out["placed"].to_numpy(dtype=bool) if n else np.zeros(0, dtype=bool)
    size = out["size"].to_numpy(dtype="float64") if n else np.zeros(0)
    pnl_share = np.where(ex, win - price, np.nan)
    out["would_win"] = win
    out["pnl_per_share"] = pnl_share
    out["pnl_per_placed_share"] = np.where(placed, np.where(ex, pnl_share, 0.0), np.nan)
    out["pnl_order"] = np.where(ex, pnl_share * size, np.where(placed, 0.0, np.nan))
    fee = np.asarray(taker_fee(price, 1.0, fee_schedule, decimals=None), dtype="float64") if n else np.zeros(0)
    rebate = np.where(ex, rebate_rate * fee, 0.0)
    total = float(np.nansum(rebate * size))
    cap = rebate_rate * taker_fees_total
    if np.isfinite(cap) and total > cap > 0:
        rebate = rebate * (cap / total)
    out["rebate_per_share"] = np.where(ex, rebate, np.nan)
    return out


def score_frame(df: pd.DataFrame, rebate_rate: float = MAKER_REBATE_RATE) -> pd.DataFrame:
    """Version vectorisée de :func:`score_orders` pour une table multi-marchés : ``df`` porte les
    colonnes ``y`` (issue officielle 1/0 du marché, NaN si inconnue), ``fee_rate``,
    ``fee_exponent`` et, facultative, ``taker_fees_total`` (plafond de la remise par marché,
    appliqué au prorata sur les ordres exécutés du même ``slug``)."""
    out = df.copy()
    n = len(out)
    y = out["y"].to_numpy(dtype="float64") if n else np.zeros(0)
    side_up = (out["side"].astype(str) == "up").to_numpy() if n else np.zeros(0, dtype=bool)
    win = np.where(side_up, y, 1.0 - y)
    price = out["price"].to_numpy(dtype="float64")
    ex = out["executed"].to_numpy(dtype=bool)
    placed = out["placed"].to_numpy(dtype=bool)
    size = out["size"].to_numpy(dtype="float64")
    pnl_share = np.where(ex, win - price, np.nan)
    out["would_win"] = win
    out["pnl_per_share"] = pnl_share
    out["pnl_per_placed_share"] = np.where(placed, np.where(ex, pnl_share, 0.0), np.nan)
    out["pnl_order"] = np.where(ex, pnl_share * size, np.where(placed, 0.0, np.nan))
    rate = out["fee_rate"].to_numpy(dtype="float64") if "fee_rate" in out.columns else np.full(n, 0.07)
    expo = out["fee_exponent"].to_numpy(dtype="float64") if "fee_exponent" in out.columns else np.ones(n)
    with np.errstate(invalid="ignore"):
        fee = np.nan_to_num(rate, nan=0.0) * np.power(np.clip(price * (1.0 - price), 0.0, None), np.nan_to_num(expo, nan=1.0))
    rebate = np.where(ex, rebate_rate * fee, np.nan)
    if "taker_fees_total" in out.columns and n:
        tot = pd.Series(np.nan_to_num(rebate, nan=0.0) * size).groupby(out["slug"].to_numpy()).transform("sum").to_numpy()
        cap = rebate_rate * out["taker_fees_total"].to_numpy(dtype="float64")
        scale = np.where(np.isfinite(cap) & (tot > cap) & (cap > 0), cap / np.where(tot > 0, tot, 1.0), 1.0)
        rebate = rebate * scale
    out["rebate_per_share"] = rebate
    return out


def summarize(df: pd.DataFrame, by: Sequence[str] = ("strategy", "label"), boot=None, boot_adj=None,
              group_col: str = "slot") -> pd.DataFrame:
    """Par groupe : ordres posables, taux d'exécution, parts de (i)/(ii), P&L par part placée et par
    part exécutée (cents), remise, sélection adverse (gain si exécuté − gain sinon, points), délai.

    ``boot`` : :class:`tradebot.polymarket_backtest.SlotBootstrap` construit sur ``df[group_col]``
    (IC 95 %) ; ``boot_adj`` : idem avec un ``alpha`` corrigé (Bonferroni) -> colonnes ``*_adj``.
    """
    rows = []
    by = list(by)
    for key, g in df.groupby(by, sort=False, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        placed = g[g["placed"]]
        ex = placed[placed["executed"]]
        scored = placed[np.isfinite(placed["pnl_per_placed_share"])]
        ex_s = scored[scored["executed"]]
        row = dict(zip(by, key))
        row.update({
            "n_orders": int(len(placed)), "n_markets": int(placed["slug"].nunique()) if "slug" in placed else math.nan,
            "n_crossing": int(g["crossing"].sum()),
            "fill_rate": placed["executed"].mean() if len(placed) else math.nan,
            "share_traversal": (ex["fill_reason"] == "traversal").mean() if len(ex) else math.nan,
            "share_queue": (ex["fill_reason"] == "queue").mean() if len(ex) else math.nan,
            "delay_median_s": ex["delay_fill_s"].median() if len(ex) else math.nan,
            "delay_p75_s": ex["delay_fill_s"].quantile(0.75) if len(ex) else math.nan,
            "n_scored": int(len(scored)),
            "pnl_per_placed_c": 100 * scored["pnl_per_placed_share"].mean() if len(scored) else math.nan,
            "pnl_per_executed_c": 100 * ex_s["pnl_per_share"].mean() if len(ex_s) else math.nan,
            "rebate_per_executed_c": 100 * ex_s["rebate_per_share"].mean() if len(ex_s) else math.nan,
            "pnl_total_usd": scored["pnl_order"].sum() if len(scored) else math.nan,
            "win_rate_executed": ex_s["would_win"].mean() if len(ex_s) else math.nan,
            "win_rate_not_executed": scored.loc[~scored["executed"], "would_win"].mean() if (~scored["executed"]).any() else math.nan,
        })
        row["adverse_selection_pts"] = 100 * (row["win_rate_executed"] - row["win_rate_not_executed"])
        if boot is not None and len(placed):
            m_placed = df.index.isin(placed.index)
            m_scored = df.index.isin(scored.index)
            _, flo, fhi = boot.mean(df["executed"].to_numpy(dtype=float), m_placed)
            row.update(fill_rate_ic_lo=flo, fill_rate_ic_hi=fhi)
            if len(scored):
                x = 100 * df["pnl_per_placed_share"].to_numpy(dtype=float)
                _, lo, hi = boot.mean(x, m_scored)
                row.update(pnl_per_placed_ic_lo=lo, pnl_per_placed_ic_hi=hi)
                _, lo2, hi2 = boot.mean(100 * df["pnl_per_share"].to_numpy(dtype=float), m_scored & df["executed"].to_numpy())
                row.update(pnl_per_executed_ic_lo=lo2, pnl_per_executed_ic_hi=hi2)
                if boot_adj is not None:
                    _, alo, ahi = boot_adj.mean(x, m_scored)
                    row.update(pnl_per_placed_ic_lo_adj=alo, pnl_per_placed_ic_hi_adj=ahi)
                # sélection adverse : différence de deux moyennes (mêmes tirages)
                w = df["would_win"].to_numpy(dtype=float)
                exm = df["executed"].to_numpy(dtype=bool)
                _, d_ex = boot.ratio_draws(w, 1.0, m_scored & exm)
                _, d_nx = boot.ratio_draws(w, 1.0, m_scored & ~exm)
                dd = 100 * (d_ex - d_nx)
                dd = dd[np.isfinite(dd)]
                if dd.size:
                    lo3, hi3 = np.quantile(dd, [boot.alpha / 2, 1 - boot.alpha / 2])
                    row.update(adverse_selection_ic_lo=float(lo3), adverse_selection_ic_hi=float(hi3))
        rows.append(row)
    return pd.DataFrame(rows)


def pair_summary(df: pd.DataFrame, by: Sequence[str] = ("label",), boot=None, group_col: str = "slot") -> pd.DataFrame:
    """(b) Par paire Up + Down (marchés où les deux ordres sont posables) : part des paires avec les
    deux exécutés / un seul / aucun, P&L par paire (cents par part), IC."""
    d = df[df["strategy"] == "two_sided"]
    keys = ["slug", "pair"] + [c for c in by if c not in ("label",)]
    rows = []
    for key, g in d.groupby(list(by), sort=False, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        pairs = g.groupby(keys, observed=True).agg(n=("placed", "size"), n_placed=("placed", "sum"), n_ex=("executed", "sum"),
                                                   pnl=("pnl_order", "sum"), size=("size", "first"), slot=(group_col, "first"))
        pairs = pairs[(pairs["n"] == 2) & (pairs["n_placed"] == 2)]
        row = dict(zip(by, key))
        row.update({"n_pairs": int(len(pairs)),
                    "share_both": (pairs["n_ex"] == 2).mean() if len(pairs) else math.nan,
                    "share_one": (pairs["n_ex"] == 1).mean() if len(pairs) else math.nan,
                    "share_none": (pairs["n_ex"] == 0).mean() if len(pairs) else math.nan,
                    "pnl_per_pair_c": 100 * (pairs["pnl"] / pairs["size"]).mean() if len(pairs) else math.nan})
        if boot is not None and len(pairs):
            from tradebot.polymarket_backtest import SlotBootstrap

            b = SlotBootstrap(pairs["slot"].to_numpy(), B=boot.B, seed=0, alpha=boot.alpha)
            _, lo, hi = b.mean(100 * (pairs["pnl"] / pairs["size"]).to_numpy(dtype=float))
            row.update(pnl_per_pair_ic_lo=lo, pnl_per_pair_ic_hi=hi)
        rows.append(row)
    return pd.DataFrame(rows)
