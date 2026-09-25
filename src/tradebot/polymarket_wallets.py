"""Portefeuilles (wallets) actifs sur les marchés Polymarket « Up or Down » : transactions à la
seconde avec l'identité du wallet, rôle preneur / maker, P&L par marché et par heure.

**Lecture seule** de données publiques (``data-api``, ``gamma-api``) pour de la recherche et
de la simulation papier : aucun ordre, aucune clé privée. Réutilise :class:`PolymarketClient`
(``tradebot.polymarket``) pour la liste des marchés, les retentatives et la limitation de débit.

Faits vérifiés sur l'API (25/09/2026)
-------------------------------------
* ``GET data-api/v2/trades?condition=c1,c2,…(≤ 20)&limit=1000[&cursor=…][&taker_only=false]``
  renvoie ``{data: [...], pagination: {has_more, next_cursor}}`` ; ``limit`` > 1000 -> 400.
  Un lot de 20 conditions renvoie exactement l'union des requêtes individuelles (vérifié sur
  5 marchés, 3 271 lignes preneur et 9 515 lignes au total). Le curseur (base64) encode
  ``{ts, sq}`` : pagination « keyset » par (horodatage, séquence on-chain) décroissants.
  ``sq`` commence par le numéro de bloc Polygon : l'ordre de la réponse est l'ordre on-chain.
* ``timestamp`` = **horodatage du bloc Polygon, à la seconde** (vérifié : identique au
  ``timestamp`` du bloc renvoyé par ``eth_getTransactionReceipt`` + ``eth_getBlockByNumber`` sur
  ``polygon-bor-rpc.publicnode.com``, 8 transactions sur 8). Un bloc toutes les ~1,5 s (mesuré
  sur 148 blocs) : c'est la résolution temporelle maximale d'une exécution on-chain. La colonne
  ``seq`` conserve l'ordre on-chain au sein d'une même seconde.
* **Sémantique de ``taker_only``** (marché ``btc-updown-5m-1790362800`` : 1 591 lignes en
  ``taker_only=true``, 4 336 en ``false``) :

  - ``taker_only=true`` (défaut) : une ligne par transaction (1 591 ``transaction_hash``
    distincts pour 1 591 lignes), la ligne du **preneur**, avec son prix moyen d'exécution ;
  - ``taker_only=false`` : les mêmes transactions (aucune en plus ou en moins) avec, en plus,
    une ligne par **ordre maker** exécuté. Chaque transaction contient exactement 1 ligne
    preneur (toujours la première du groupe dans la réponse) et 1 à 50 lignes maker ; la somme
    des tailles maker est égale à la taille du preneur dans 100 % des transactions ; le prix du
    preneur est le VWAP des prix makers (convertis sur son jeton) à 1e-10 près ; le preneur
    n'apparaît jamais comme maker dans sa propre transaction ;
  - appariements : preneur BUY Up contre maker SELL Up (échange direct) ou contre maker
    BUY Down (« mint » : les deux paient 1 $ à eux deux) ; preneur SELL contre maker BUY du
    même jeton ou contre maker SELL de l'autre jeton (« merge ») ;
  - quelques lignes identiques (même wallet, côté, jeton, taille, prix, tx) existent : ce sont
    des ordres maker distincts du même wallet (la somme des tailles ne s'équilibre qu'en les
    gardant) : **elles sont conservées**.

  Le rôle est donc identifiable : ``role='taker'`` pour les lignes présentes dans la réponse
  ``taker_only=true`` (jointure sur tx, wallet, jeton, côté, taille, prix), ``'maker'`` pour
  les autres lignes d'une transaction qui a un preneur, ``None`` sinon (non observé).
* Frais : ``GET data-api/v2/activity?user=…`` donne ``usdc_size`` par exécution. Pour les fills
  preneur, ``usdc_size = p·C + 0,07·p(1−p)·C`` (achat) ou ``p·C − 0,07·p(1−p)·C`` (vente) ; pour
  les fills maker, ``usdc_size = p·C`` (aucun frais) : cela confirme à la fois la formule
  ``crypto_fees_v2`` et l'identification du rôle. Une minorité de fills preneur (≈ 20 % sur un
  échantillon de 123, surtout de petits ordres) porte un surcoût supplémentaire de 0,01 à 0,09 $,
  probablement des frais d'intégrateur (« builder ») : ils ne sont pas modélisés ici.
* ``GET data-api/v1/leaderboard?timePeriod=day|week|month|all&orderBy=PNL|VOL&category=crypto``
  : 50 lignes au plus par page, pagination par ``offset``.
* ``GET data-api/v2/activity?user=…&start=…&end=…&limit=…`` : types ``TRADE``, ``SPLIT``,
  ``MERGE``, ``REDEEM``… ; ``start``/``end`` honorés ; pagination par ``cursor``.
* Pas d'historique de carnet ; le WebSocket public de marché ne donne pas l'identité des
  wallets. Le subgraph Goldsky Polymarket est déprécié (non utilisé).

Comptabilité du P&L (:func:`wallet_market_pnl`)
-----------------------------------------------
Positions signées par jeton : un achat ajoute des parts, une vente en retire, même sans
position préalable. Une position négative correspond à des parts issues d'un **SPLIT**
(1 $ -> 1 Up + 1 Down) puis vendues : vendre ``C`` parts Up à ``p`` après un split coûte
``−C + p·C`` et rapporte ``C`` si Down gagne, soit ``p·C − C·1{Up}`` — exactement ce que donne
la position signée (−C Up, +p·C de trésorerie, paiement −C·1{Up}). De même un **MERGE**
(1 Up + 1 Down -> 1 $) équivaut au paiement de la paire à la résolution, et **REDEEM** au
paiement final. Le P&L à la résolution est donc exact pour tout wallet dont toutes les
exécutions sont dans les trades, y compris les market makers, à ces limites près :

* remises maker (20 % des frais preneur du marché, versées chaque jour) : seulement
  **estimées** (``rebate_est``), les récompenses de liquidité ne sont pas comptées ;
* transferts de jetons entre wallets (ERC-1155) hors trades, frais d'intégrateur, gaz ;
* la trésorerie immobilisée (split) n'apparaît pas : le P&L est juste, le capital engagé non.

:func:`enrich_with_activity` lit ``/v2/activity`` pour recouper ces montants (SPLIT, MERGE,
REDEEM effectifs) sur quelques wallets. **Vérifié** (fenêtres du 23/09 21h au 25/09 21h UTC,
16 wallets = 8 meilleurs P&L + 8 plus gros volumes, dont des market makers avec 748 SPLIT,
3 897 MERGE et 9 685 REDEEM) : sur 9 263 couples (wallet, marché) réglés,
``|activity_cash − pnl| ≤ 0,005 $`` dans 100 % des cas. Le P&L « trades » est donc exact, frais
preneur compris, y compris pour les market makers.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import union_categoricals

from . import polymarket as pm
from .config import CACHE_DIR
from .polymarket import DATA_URL, PolymarketClient, PolymarketError

log = logging.getLogger(__name__)

TRADES_V2_URL = f"{DATA_URL}/v2/trades"
ACTIVITY_V2_URL = f"{DATA_URL}/v2/activity"
LEADERBOARD_URL = f"{DATA_URL}/v1/leaderboard"

MAX_CONDITIONS = 20          # conditions par requête /v2/trades (limite API)
PAGE_LIMIT = 1000            # lignes par page /v2/trades (limite API)
ACTIVITY_PAGE_LIMIT = 500
LEADERBOARD_PAGE = 50        # limite API de /v1/leaderboard
MAX_PAGES = 100_000          # garde-fou contre une pagination sans fin
DEFAULT_MAX_RPS = 12.0       # < 30 req/s autorisées sur /v2/trades (300 / 10 s) : marge pour les autres
CRYPTO_FEE_RATE = 0.07       # feeSchedule.rate de crypto_fees_v2
MAKER_REBATE_RATE = 0.20     # part des frais preneur reversée aux makers du marché
SETTLE_DELAY_S = 600         # un marché n'est mis en cache que 10 min après sa fin
CACHE_VERSION = 1
WALLETS_CACHE_DIR = CACHE_DIR / "polymarket" / "wallets"

TRADE_COLUMNS = [
    "ts", "wallet", "side", "outcome", "outcome_index", "token_id", "condition_id", "price", "size",
    "notional", "tx_hash", "name", "pseudonym", "slug", "seq", "role",
]
MARKET_INFO_COLUMNS = ["asset", "duration", "window_start", "window_end", "t_rel_s", "resolved_up"]
_CATEGORICAL = (
    "wallet", "side", "outcome", "token_id", "condition_id", "name", "pseudonym", "slug", "role",
    "asset", "duration",
)
PNL_COLUMNS = [
    "wallet", "condition_id", "slug", "asset", "duration", "window_start", "window_end", "resolved_up",
    "name", "pseudonym", "n_trades", "n_taker", "n_maker", "n_role_unknown", "volume", "shares",
    "buy_notional", "sell_notional", "taker_share", "pos_up", "pos_down", "net_up", "direction",
    "cash", "fees", "payout", "pnl", "rebate_est", "pnl_incl_rebate", "first_t_rel_s",
    "last_t_rel_s", "median_t_rel_s",
]
HOURLY_COLUMNS = [
    "hour", "wallet", "name", "pseudonym", "rank_pnl", "pnl", "pnl_incl_rebate", "fees", "rebate_est",
    "volume", "n_markets", "n_trades", "n_wins", "win_rate", "pnl_per_market", "taker_share",
    "first_t_rel_median", "t_rel_median", "assets",
]
LEADERBOARD_COLUMNS = [
    "rank", "wallet", "user_name", "x_username", "verified", "vol", "pnl", "period", "order_by",
    "category", "fetched_at",
]
ACTIVITY_COLUMNS = [
    "wallet", "ts", "type", "condition_id", "token_id", "side", "outcome", "size", "usdc_size", "price",
    "tx_hash", "slug",
]


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def _as_client(obj: Any = None, max_rps: float = DEFAULT_MAX_RPS, **kw: Any) -> PolymarketClient:
    """Un :class:`PolymarketClient` (retentatives, backoff, débit partagé) depuis un client, une
    session ``requests`` (ou un faux) ou ``None``."""
    if isinstance(obj, PolymarketClient):
        return obj
    kw.setdefault("retries", 6)
    kw.setdefault("backoff", 1.0)
    kw.setdefault("use_cache", False)
    return PolymarketClient(session=obj, max_rps=max_rps, **kw)


def _pick(row: Mapping, *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return default


def _norm_outcome(value: Any, index: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        v = value.strip().lower()
        if v in ("up", "down"):
            return v.capitalize()
        return value.strip()
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    return {0: "Up", 1: "Down"}.get(i)


def _to_categories(df: pd.DataFrame) -> pd.DataFrame:
    for c in _CATEGORICAL:
        if c in df.columns and not isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = df[c].astype("category")
    return df


def _concat(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """``pd.concat`` qui conserve les colonnes catégorielles (union des catégories) : évite de
    matérialiser des millions de chaînes lors de la fusion de fichiers de cache."""
    frames = [f for f in frames if f is not None and len(f.columns)]
    if not frames:
        return pd.DataFrame()
    non_empty = [f for f in frames if len(f)]
    if not non_empty:
        return frames[0].iloc[0:0].copy()
    if len(non_empty) == 1:
        return non_empty[0].reset_index(drop=True)
    cols = list(non_empty[0].columns)
    for f in non_empty[1:]:
        cols += [c for c in f.columns if c not in cols]
    out: dict[str, Any] = {}
    for c in cols:
        parts = [f[c] if c in f.columns else pd.Series([None] * len(f)) for f in non_empty]
        if all(isinstance(p.dtype, pd.CategoricalDtype) for p in parts):
            try:
                out[c] = pd.Categorical(union_categoricals(parts, ignore_order=True))
            except TypeError:  # catégories de types différents (ex. colonne entièrement vide)
                vals = np.concatenate([np.asarray(p.astype(object)) for p in parts])
                out[c] = pd.Categorical(vals)
        else:
            out[c] = pd.concat(parts, ignore_index=True)
    return pd.DataFrame(out)


def _positions(keys: pd.Series, index: pd.Index) -> np.ndarray:
    """Position de chaque clé de ``keys`` dans ``index`` (-1 si absente), rapide pour une
    colonne catégorielle de plusieurs millions de lignes."""
    if isinstance(keys.dtype, pd.CategoricalDtype):
        cat_pos = index.get_indexer(keys.cat.categories.astype(str))
        codes = keys.cat.codes.to_numpy()
        return np.where(codes >= 0, cat_pos[np.clip(codes, 0, None)] if len(cat_pos) else -1, -1)
    return index.get_indexer(keys.astype(str))


# ---------------------------------------------------------------------------
# /v2/trades
# ---------------------------------------------------------------------------
def trades_v2_frame(rows: Sequence[Mapping]) -> pd.DataFrame:
    """DataFrame des lignes ``/v2/trades`` (accepte aussi les clés camelCase de la v1).

    Colonnes : ``ts`` (UTC, à la seconde : horodatage du bloc), ``wallet``, ``side`` (BUY/SELL
    de ce wallet), ``outcome`` ('Up'/'Down'), ``outcome_index``, ``token_id``, ``condition_id``,
    ``price``, ``size`` (parts), ``notional`` ($ = prix × parts), ``tx_hash``, ``name``,
    ``pseudonym``, ``slug``, ``seq`` (rang dans la réponse, décroissant dans le temps ; voir
    :func:`fetch_market_trades`), ``role`` (non renseigné ici).
    """
    n = len(rows)
    ts = np.fromiter((int(_pick(r, "timestamp", default=0) or 0) for r in rows), dtype="int64", count=n)
    price = pd.to_numeric(pd.Series([_pick(r, "price") for r in rows], dtype=object), errors="coerce")
    size = pd.to_numeric(pd.Series([_pick(r, "size") for r in rows], dtype=object), errors="coerce")
    price = price.to_numpy(dtype="float64", na_value=np.nan)
    size = size.to_numpy(dtype="float64", na_value=np.nan)
    oidx = [_pick(r, "outcome_index", "outcomeIndex") for r in rows]
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime(ts, unit="s", utc=True).as_unit("ns"),
            "wallet": [str(_pick(r, "proxy_wallet", "proxyWallet", default="")).lower() for r in rows],
            "side": [str(_pick(r, "side", default="")).upper() for r in rows],
            "outcome": [_norm_outcome(_pick(r, "outcome"), i) for r, i in zip(rows, oidx)],
            "outcome_index": pd.array([int(i) if i is not None else None for i in oidx], dtype="Int8"),
            "token_id": [str(_pick(r, "token_id", "asset", default="")) for r in rows],
            "condition_id": [str(_pick(r, "condition_id", "conditionId", default="")) for r in rows],
            "price": price,
            "size": size,
            "notional": price * size,
            "tx_hash": [str(_pick(r, "transaction_hash", "transactionHash", default="")) for r in rows],
            "name": [str(_pick(r, "name", default="")) for r in rows],
            "pseudonym": [str(_pick(r, "pseudonym", default="")) for r in rows],
            "slug": [str(_pick(r, "slug", "event_slug", "eventSlug", default="")) for r in rows],
            "seq": np.arange(n, dtype="int64"),
            "role": pd.Series([None] * n, dtype="object"),
        },
        columns=TRADE_COLUMNS,
    )
    return df


def _trade_pages(client: PolymarketClient, condition_ids: Sequence[str], taker_only: bool) -> list[pd.DataFrame]:
    """Toutes les pages ``/v2/trades`` d'un lot de conditions (pagination par curseur)."""
    params: dict[str, Any] = {"condition": ",".join(condition_ids), "limit": PAGE_LIMIT}
    if not taker_only:
        params["taker_only"] = "false"
    frames: list[pd.DataFrame] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = client._get(TRADES_V2_URL, p)
        if data is None:
            break
        if isinstance(data, list):  # format inattendu (sans enveloppe) : une seule page
            frames.append(trades_v2_frame(data))
            break
        rows = data.get("data") or []
        if rows:
            frames.append(trades_v2_frame(rows))
        pg = data.get("pagination") or {}
        nxt = pg.get("next_cursor")
        if not pg.get("has_more") or not nxt:
            break
        if nxt in seen:
            raise PolymarketError("pagination /v2/trades en boucle (curseur répété)")
        seen.add(nxt)
        cursor = nxt
    else:
        raise PolymarketError(f"/v2/trades : plus de {MAX_PAGES} pages pour {len(condition_ids)} conditions")
    if not frames:
        return [trades_v2_frame([])]
    # seq global au lot : 0 = ligne la plus récente de la réponse
    offset = 0
    for f in frames:
        f["seq"] = f["seq"].to_numpy() + offset
        offset += len(f)
    return frames


def assign_roles(all_df: pd.DataFrame, taker_df: pd.DataFrame) -> np.ndarray:
    """Rôle de chaque ligne ``taker_only=false`` d'après la réponse ``taker_only=true``.

    ``'taker'`` si la ligne (tx, wallet, jeton, côté, taille, prix) figure parmi les lignes
    preneur, ``'maker'`` si sa transaction a une ligne preneur mais pas elle, ``None`` si la
    transaction n'a aucune ligne preneur. Les doublons exacts sont appariés un pour un.
    """
    key = ["tx_hash", "wallet", "token_id", "side", "size", "price"]
    if len(all_df) == 0:
        return np.array([], dtype=object)
    a = pd.DataFrame({k: all_df[k].astype(str) if k not in ("size", "price") else all_df[k] for k in key})
    a = a.reset_index(drop=True)
    a["_occ"] = a.groupby(key, sort=False, dropna=False).cumcount()
    if len(taker_df):
        t = pd.DataFrame({k: taker_df[k].astype(str) if k not in ("size", "price") else taker_df[k] for k in key})
        t["_occ"] = t.groupby(key, sort=False, dropna=False).cumcount()
        t["_hit"] = True
        m = a.merge(t, on=key + ["_occ"], how="left", sort=False)
        hit = m["_hit"].fillna(False).to_numpy(dtype=bool)
    else:
        hit = np.zeros(len(a), dtype=bool)
    tx_has_taker = pd.Series(hit).groupby(a["tx_hash"].to_numpy()).transform("any").to_numpy(dtype=bool)
    return np.where(hit, "taker", np.where(tx_has_taker, "maker", None)).astype(object)


def _fetch_batch(client: PolymarketClient, batch: Sequence[str], taker_only: bool, roles: bool) -> pd.DataFrame:
    frames = _trade_pages(client, batch, taker_only)
    df = pd.concat(frames, ignore_index=True)
    if taker_only:
        df["role"] = "taker"
    elif roles:
        takers = pd.concat(_trade_pages(client, batch, True), ignore_index=True)
        df["role"] = assign_roles(df, takers)
    # seq : rang chronologique au sein de chaque condition (0 = plus ancienne ligne), dans
    # l'ordre on-chain de la réponse (tri décroissant par (bloc, séquence)).
    if len(df):
        rev = df["seq"].max() - df["seq"]
        df["seq"] = rev.groupby(df["condition_id"].to_numpy()).rank(method="first").astype("int64") - 1
    return df


def fetch_market_trades(
    client_or_session: Any = None,
    condition_ids: Iterable[str] = (),
    taker_only: bool = True,
    *,
    roles: bool = True,
    max_workers: int = 1,
    max_rps: float = DEFAULT_MAX_RPS,
    batch_size: int = MAX_CONDITIONS,
) -> pd.DataFrame:
    """Toutes les exécutions des marchés ``condition_ids`` via ``data-api /v2/trades``.

    * lots de ≤ 20 conditions, pagination complète par curseur, retentatives avec backoff
      exponentiel (429/5xx/réseau, ``Retry-After`` respecté) et débit plafonné (``max_rps``,
      partagé entre threads) : ceux du :class:`PolymarketClient` fourni ou créé ;
    * ``taker_only=True`` : une ligne par exécution (côté preneur), ``role='taker'`` ;
    * ``taker_only=False`` : preneur **et** makers. Avec ``roles=True`` (défaut) les lignes
      preneur sont aussi téléchargées (≈ +30 % de requêtes) et ``role`` vaut 'taker'/'maker'
      (voir :func:`assign_roles`) ; avec ``roles=False``, ``role`` est vide.

    Colonnes : voir :func:`trades_v2_frame`. ``seq`` est le rang chronologique on-chain de la
    ligne dans son marché (départage les lignes d'une même seconde). Tri : ``ts``, marché, ``seq``.
    """
    client = _as_client(client_or_session, max_rps)
    cids = list(dict.fromkeys(str(c) for c in condition_ids if c))
    if not cids:
        return trades_v2_frame([])
    bs = max(1, min(int(batch_size), MAX_CONDITIONS))
    batches = [cids[i : i + bs] for i in range(0, len(cids), bs)]
    if max_workers <= 1 or len(batches) == 1:
        frames = [_fetch_batch(client, b, taker_only, roles) for b in batches]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as ex:
            frames = list(ex.map(lambda b: _fetch_batch(client, b, taker_only, roles), batches))
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["ts", "condition_id", "seq"], kind="stable").reset_index(drop=True)


def role_consistency(trades: pd.DataFrame, atol: float = 1e-3) -> dict[str, float]:
    """Contrôle des rôles par transaction : part des tx avec exactement une ligne preneur, et
    part des tx « équilibrées » (taille preneur == somme des tailles maker)."""
    if len(trades) == 0 or "role" not in trades.columns:
        return {"n_tx": 0, "share_one_taker": math.nan, "share_balanced": math.nan, "share_role_unknown": math.nan}
    is_t = (trades["role"] == "taker").to_numpy(dtype=bool)
    is_m = (trades["role"] == "maker").to_numpy(dtype=bool)
    size = trades["size"].to_numpy(dtype="float64")
    w = pd.DataFrame(
        {"tx": trades["tx_hash"].to_numpy(), "nt": is_t, "ts": size * is_t, "ms": size * is_m, "nm": is_m}
    )
    g = w.groupby("tx", sort=False).sum()
    one = g["nt"] == 1
    bal = one & (g["nm"] > 0) & np.isclose(g["ts"], g["ms"], atol=atol, rtol=1e-6)
    unknown = ~(is_t | is_m)
    return {
        "n_tx": int(len(g)),
        "share_one_taker": float(one.mean()),
        "share_balanced": float(bal.mean()),
        "share_role_unknown": float(unknown.mean()),
    }


# ---------------------------------------------------------------------------
# Collecte avec cache
# ---------------------------------------------------------------------------
def _cache_paths(cache_dir: Path, asset: str, duration: str, day: str, mode: str) -> tuple[Path, Path]:
    stem = f"{asset}_{duration}_{day}_{mode}"
    base = Path(cache_dir) / "trades"
    return base / f"{stem}.parquet", base / f"{stem}.json"


def _load_cache(paths: tuple[Path, Path]) -> tuple[pd.DataFrame | None, dict[str, int]]:
    data_path, meta_path = paths
    if not (data_path.exists() and meta_path.exists()):
        return None, {}
    try:
        meta = json.loads(meta_path.read_text())
        if int(meta.get("version", 0)) != CACHE_VERSION:
            return None, {}
        df = pd.read_parquet(data_path)
        covered = {str(k): int(v) for k, v in (meta.get("conditions") or {}).items()}
        return _to_categories(df), covered
    except Exception as exc:  # cache illisible : on refait
        log.warning("cache wallets illisible %s : %s", data_path, exc)
        return None, {}


def _save_cache(paths: tuple[Path, Path], df: pd.DataFrame, covered: Mapping[str, int], mode: str) -> None:
    data_path, meta_path = paths
    keep = df[df["condition_id"].isin(list(covered))] if len(df) else df
    keep = keep.sort_values(["ts", "condition_id", "seq"], kind="stable").reset_index(drop=True)
    pm._atomic_parquet(keep[[c for c in TRADE_COLUMNS if c in keep.columns]], data_path)
    meta = {
        "version": CACHE_VERSION,
        "mode": mode,
        "conditions": dict(sorted(covered.items())),
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    pm._atomic_write_text(meta_path, json.dumps(meta))


def _attach_market_info(trades: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    """Ajoute slug, asset, duration, window_start (S), window_end (E), t_rel_s et resolved_up."""
    info = markets.drop_duplicates("condition_id").copy()
    info.index = pd.Index(info["condition_id"].astype(str))
    pos = _positions(trades["condition_id"], info.index)
    ok = pos >= 0
    safe = np.where(ok, pos, 0)

    def take(col: str) -> np.ndarray:
        vals = info[col].to_numpy()[safe] if len(info) else np.full(len(trades), None, dtype=object)
        return vals

    out = trades
    out["slug"] = pd.Categorical(np.where(ok, take("slug"), None))
    out["asset"] = pd.Categorical(np.where(ok, take("asset"), None))
    out["duration"] = pd.Categorical(np.where(ok, take("duration"), None))
    start = pd.DatetimeIndex(info["start"]).as_unit("ns") if len(info) else pd.DatetimeIndex([], tz="UTC")
    end = pd.DatetimeIndex(info["end"]).as_unit("ns") if len(info) else pd.DatetimeIndex([], tz="UTC")
    s_ns = np.where(ok, start.asi8[safe] if len(info) else 0, np.iinfo("int64").min)
    e_ns = np.where(ok, end.asi8[safe] if len(info) else 0, np.iinfo("int64").min)
    out["window_start"] = pd.to_datetime(s_ns, utc=True).as_unit("ns")
    out["window_end"] = pd.to_datetime(e_ns, utc=True).as_unit("ns")
    t_ns = pd.DatetimeIndex(out["ts"]).as_unit("ns").asi8
    rel = np.where(ok, (t_ns - s_ns) // 1_000_000_000, 0)
    out["t_rel_s"] = pd.array(np.where(ok, rel, 0), dtype="Int64")
    out.loc[~ok, "t_rel_s"] = pd.NA
    ru = info["resolved_up"].map(lambda v: pd.NA if v is None or (isinstance(v, float) and math.isnan(v)) else bool(v))
    ru_vals = ru.to_numpy(dtype=object)[safe] if len(info) else np.full(len(trades), pd.NA, dtype=object)
    out["resolved_up"] = pd.array(np.where(ok, ru_vals, pd.NA), dtype="boolean")
    return out


def _market_stats(markets_df: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    out = markets_df.copy()
    if len(trades) == 0:
        for c in ("n_fills", "n_taker_fills", "n_wallets", "taker_volume_usd"):
            out[c] = 0
        return out
    is_t = (trades["role"] == "taker").to_numpy(dtype=bool)
    w = pd.DataFrame(
        {
            "condition_id": trades["condition_id"],
            "wallet": trades["wallet"],
            "n_fills": np.ones(len(trades), dtype="int64"),
            "n_taker_fills": is_t.astype("int64"),
            "taker_volume_usd": trades["notional"].to_numpy() * is_t,
        }
    )
    grp = w.groupby("condition_id", observed=True)
    g = grp[["n_fills", "n_taker_fills", "taker_volume_usd"]].sum()
    g.index = g.index.astype(str)
    nw = grp["wallet"].nunique()
    nw.index = nw.index.astype(str)
    cid = out["condition_id"].astype(str)
    out["n_fills"] = cid.map(g["n_fills"]).fillna(0).astype("int64")
    out["n_taker_fills"] = cid.map(g["n_taker_fills"]).fillna(0).astype("int64")
    out["n_wallets"] = cid.map(nw).fillna(0).astype("int64")
    out["taker_volume_usd"] = cid.map(g["taker_volume_usd"]).fillna(0.0)
    return out


def collect_trades(
    assets: Sequence[str] = ("btc", "eth", "sol"),
    durations: Sequence[str] = ("5m", "15m"),
    start: Any = None,
    end: Any = None,
    cache: bool = True,
    *,
    taker_only: bool = False,
    client: Any = None,
    cache_dir: Path = WALLETS_CACHE_DIR,
    max_workers: int = 6,
    max_rps: float = DEFAULT_MAX_RPS,
    progress: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Exécutions de tous les marchés Up/Down résolus dont la fenêtre commence dans ``[start, end)``.

    * ``end`` par défaut : heure pleine UTC précédente ; ``start`` par défaut : ``end − 48 h`` ;
    * marchés : ``PolymarketClient.list_updown_markets(only_closed=True)``, seuls les marchés
      **résolus** sont gardés ; ``markets_df`` = :func:`tradebot.polymarket.markets_to_frame`
      + ``n_fills``, ``n_taker_fills``, ``n_wallets``, ``taker_volume_usd`` ;
    * ``taker_only=False`` (défaut) : preneurs **et** makers avec ``role`` (deux passes
      ``/v2/trades``) ; ``taker_only=True`` : preneurs seulement (réutilise le cache complet s'il
      existe) ;
    * cache parquet par (série, jour UTC du début de fenêtre) dans ``cache_dir/trades`` + fichier
      JSON des marchés couverts. Un marché n'est mis en cache que s'il est résolu et fini depuis
      plus de 10 min (exécutions immuables). Les métadonnées gamma sont mises en cache dans
      ``cache_dir/markets`` (mécanisme du :class:`PolymarketClient`) ;
    * chaque exécution reçoit ``slug``, ``asset``, ``duration``, ``window_start`` (S),
      ``window_end`` (E), ``t_rel_s`` = secondes depuis S (négatif avant l'ouverture) et
      ``resolved_up``.

    Retourne ``(markets_df, trades_df)`` ; ``trades_df`` est trié par ``ts``. Colonnes texte en
    ``category`` (plusieurs millions de lignes sur 48 h).
    """
    t_end = pm._to_utc(end) if end is not None else pd.Timestamp.now(tz="UTC").floor("h")
    t_start = pm._to_utc(start) if start is not None else t_end - pd.Timedelta(hours=48)
    if not isinstance(client, PolymarketClient):
        client = _as_client(client, max_rps, cache_dir=Path(cache_dir), use_cache=cache)
    mode = "taker" if taker_only else "all"

    markets: list[pm.UpDownMarket] = []
    for asset in assets:
        for duration in durations:
            ms = client.list_updown_markets(asset, duration, t_start, t_end, only_closed=True)
            unresolved = [m.slug for m in ms if m.resolved_up is None]
            if unresolved:
                log.warning("%d marché(s) fermé(s) non résolu(s) ignoré(s) : %s", len(unresolved), unresolved[:5])
            markets += [m for m in ms if m.resolved_up is not None]
    markets_df = pm.markets_to_frame(markets)
    if not markets:
        empty = _attach_market_info(trades_v2_frame([]), markets_df)
        return _market_stats(markets_df, empty), empty

    now = time.time()
    groups: dict[tuple[str, str, str], list[pm.UpDownMarket]] = {}
    for m in markets:
        day = m.start.strftime("%Y-%m-%d")
        groups.setdefault((m.asset, m.duration, day), []).append(m)

    state: dict[tuple[str, str, str], dict[str, Any]] = {}
    tasks: list[tuple[tuple[str, str, str], list[str]]] = []
    for key, ms in groups.items():
        asset, duration, day = key
        paths = _cache_paths(Path(cache_dir), asset, duration, day, mode)
        cached, covered = _load_cache(paths) if cache else (None, {})
        parts: list[pd.DataFrame] = []
        wanted = [m.condition_id for m in ms]
        reused: set[str] = set()
        if taker_only and cache:
            # le cache complet (preneurs + makers) contient déjà les lignes preneur
            full, full_cov = _load_cache(_cache_paths(Path(cache_dir), asset, duration, day, "all"))
            reuse = [c for c in wanted if c in full_cov and c not in covered]
            if full is not None and reuse:
                parts.append(full[full["condition_id"].isin(reuse) & (full["role"] == "taker")])
                reused = set(reuse)
        need = [c for c in wanted if c not in covered and c not in reused]
        state[key] = {
            "paths": paths,
            "cached": cached,
            "covered": dict(covered),
            "parts": parts,
            "new": [],
            "need": need,
            "immutable": {
                m.condition_id
                for m in ms
                if m.is_resolved and m.end.timestamp() + SETTLE_DELAY_S < now
            },
        }
        for i in range(0, len(need), MAX_CONDITIONS):
            tasks.append((key, need[i : i + MAX_CONDITIONS]))

    def save_group(key: tuple[str, str, str]) -> None:
        """Écrit le cache d'une série-jour dès que tous ses lots sont arrivés (collecte reprenable)."""
        st = state[key]
        if not cache or not st["new"] or st["saved"]:
            return
        new = _concat(st["new"])
        counts = new["condition_id"].astype(str).value_counts()
        covered = dict(st["covered"])
        for c in st["need"]:
            if c in st["immutable"]:
                covered[c] = int(counts.get(c, 0))
        if covered:
            _save_cache(st["paths"], _concat([p for p in [st["cached"], new] if p is not None]), covered, mode)
        st["saved"] = True

    for key, st in state.items():
        st["pending"] = sum(1 for k, _ in tasks if k == key)
        st["saved"] = False

    t0 = time.time()
    if tasks:
        log.info("wallets : %d lot(s) /v2/trades à télécharger (%s)", len(tasks), mode)
        done = 0
        failed: list[tuple[tuple[str, str, str], list[str]]] = []

        def finish(key: tuple[str, str, str], df: pd.DataFrame) -> None:
            nonlocal done
            state[key]["new"].append(_to_categories(df))
            state[key]["pending"] -= 1
            done += 1
            if state[key]["pending"] == 0:
                save_group(key)
            if progress and (done % 10 == 0 or done == len(tasks)):
                print(f"  lots {done}/{len(tasks)}  {time.time() - t0:6.0f} s", file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(tasks)))) as ex:
            futs = {ex.submit(_fetch_batch, client, batch, taker_only, True): (key, batch) for key, batch in tasks}
            for fut in as_completed(futs):
                key, batch = futs[fut]
                try:
                    finish(key, fut.result())
                except (PolymarketError, ValueError) as exc:
                    log.warning("lot %s en échec (%s) : nouvel essai à la fin", key, exc)
                    failed.append((key, batch))
        for key, batch in failed:  # second essai, séquentiel ; une erreur persistante remonte
            finish(key, _fetch_batch(client, batch, taker_only, True))

    frames: list[pd.DataFrame] = []
    for key, st in state.items():
        new = _concat(st["new"]) if st["new"] else None
        pieces = []
        if st["cached"] is not None and len(st["cached"]):
            pieces.append(st["cached"])
        pieces += st["parts"]
        if new is not None:
            pieces.append(new)
        merged = _concat(pieces) if pieces else trades_v2_frame([])
        wanted = {m.condition_id for m in groups[key]}
        if len(merged):
            merged = merged[merged["condition_id"].isin(list(wanted))]
        frames.append(_to_categories(merged))

    trades = _concat(frames)
    if len(trades) == 0:
        trades = trades_v2_frame([])
    trades = _to_categories(trades)
    trades = _attach_market_info(trades, markets_df)
    trades = trades.sort_values(["ts", "condition_id", "seq"], kind="stable").reset_index(drop=True)
    return _market_stats(markets_df, trades), trades


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------
def _bool_or_nan(v: Any) -> float:
    if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)):
        return math.nan
    return float(bool(v))


def _is_up(df: pd.DataFrame) -> np.ndarray:
    up = (df["outcome"] == "Up").to_numpy(dtype=bool)
    if "outcome_index" in df.columns:
        missing = df["outcome"].isna().to_numpy(dtype=bool)
        if missing.any():
            idx = pd.to_numeric(df["outcome_index"], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            up = np.where(missing, idx == 0, up)
    return up


def wallet_market_pnl(
    trades_df: pd.DataFrame,
    fee_rate: float = CRYPTO_FEE_RATE,
    *,
    rebate_rate: float = MAKER_REBATE_RATE,
    resolved: Mapping[str, bool] | pd.Series | None = None,
) -> pd.DataFrame:
    """P&L de chaque wallet sur chaque marché (une ligne par (wallet, condition)).

    * positions signées ``pos_up`` / ``pos_down`` (parts ; négatif = parts issues d'un SPLIT puis
      vendues, voir la docstring du module), ``net_up = pos_up − pos_down`` et ``direction``
      ('Up', 'Down', 'flat') ;
    * ``cash`` = ventes − achats (prix × parts), avant frais ;
    * ``fees`` = ``fee_rate × p × (1 − p) × parts`` arrondi à 5 décimales, **seulement sur les
      exécutions où le wallet est preneur** (``role == 'taker'``) ; rôle inconnu : pas de frais,
      compté dans ``n_role_unknown`` ;
    * ``payout`` = ``pos_up × 1{Up} + pos_down × 1{Down}`` à la résolution (colonne
      ``resolved_up`` des trades ou ``resolved``: {condition_id: bool}) ; NaN si non résolu ;
    * ``pnl = cash − fees + payout`` ; ``rebate_est`` : part estimée du pool de remises maker du
      marché (``rebate_rate`` × frais preneur du marché, au prorata des ``fee_rate·p(1−p)·C``
      exécutés en maker) — n'a de sens que si ``trades_df`` contient toutes les exécutions du
      marché (``taker_only=False``) ; ``pnl_incl_rebate = pnl + rebate_est`` ;
    * ``volume`` ($, prix × parts), ``shares``, ``n_trades``, ``n_taker``, ``n_maker``,
      ``taker_share`` (part du volume en preneur), premier / dernier / médian ``t_rel_s``.

    Limites : remises estimées, récompenses de liquidité, transferts de jetons et frais
    d'intégrateur non comptés (voir :func:`enrich_with_activity` pour recouper).
    """
    if trades_df is None or len(trades_df) == 0:
        return pd.DataFrame(columns=PNL_COLUMNS)
    df = trades_df
    price = df["price"].to_numpy(dtype="float64")
    size = df["size"].to_numpy(dtype="float64")
    buy = (df["side"] == "BUY").to_numpy(dtype=bool)
    sign = np.where(buy, 1.0, -1.0)
    up = _is_up(df)
    notional = price * size
    if "role" in df.columns:
        is_t = (df["role"] == "taker").to_numpy(dtype=bool)
        is_m = (df["role"] == "maker").to_numpy(dtype=bool)
    else:
        is_t = np.zeros(len(df), dtype=bool)
        is_m = np.zeros(len(df), dtype=bool)
    fee_equiv = fee_rate * price * (1.0 - price) * size
    fee = np.where(is_t, np.round(fee_equiv, 5), 0.0)
    has_t = "t_rel_s" in df.columns
    t_rel = (
        pd.to_numeric(df["t_rel_s"], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
        if has_t
        else np.full(len(df), np.nan)
    )
    work = pd.DataFrame(
        {
            "wallet": df["wallet"].to_numpy() if not isinstance(df["wallet"].dtype, pd.CategoricalDtype) else df["wallet"],
            "condition_id": df["condition_id"].to_numpy()
            if not isinstance(df["condition_id"].dtype, pd.CategoricalDtype)
            else df["condition_id"],
            "d_up": sign * size * up,
            "d_down": sign * size * ~up,
            "cash": -sign * notional,
            "fee": fee,
            "notional": notional,
            "size": size,
            "taker": is_t.astype("int64"),
            "maker": is_m.astype("int64"),
            "unknown": (~(is_t | is_m)).astype("int64"),
            "taker_notional": notional * is_t,
            "maker_fee_equiv": fee_equiv * is_m,
            "buy_notional": notional * buy,
            "sell_notional": notional * ~buy,
            "t_rel_s": t_rel,
        }
    )
    work = work.reset_index(drop=True)
    g = work.groupby(["wallet", "condition_id"], observed=True, sort=False)
    out = g.agg(
        pos_up=("d_up", "sum"),
        pos_down=("d_down", "sum"),
        cash=("cash", "sum"),
        fees=("fee", "sum"),
        volume=("notional", "sum"),
        shares=("size", "sum"),
        n_trades=("size", "size"),
        n_taker=("taker", "sum"),
        n_maker=("maker", "sum"),
        n_role_unknown=("unknown", "sum"),
        taker_notional=("taker_notional", "sum"),
        maker_fee_equiv=("maker_fee_equiv", "sum"),
        buy_notional=("buy_notional", "sum"),
        sell_notional=("sell_notional", "sum"),
        first_t_rel_s=("t_rel_s", "min"),
        last_t_rel_s=("t_rel_s", "max"),
        median_t_rel_s=("t_rel_s", "median"),
    ).reset_index()
    for c in ("wallet", "condition_id"):
        out[c] = out[c].astype(str)
    out["pos_up"] = out["pos_up"].round(6)
    out["pos_down"] = out["pos_down"].round(6)

    # informations par marché
    cond = work.groupby("condition_id", observed=True, sort=False).agg(
        m_fees=("fee", "sum"), m_maker_equiv=("maker_fee_equiv", "sum")
    )
    cond.index = cond.index.astype(str)
    info_cols = [c for c in ("slug", "asset", "duration", "window_start", "window_end", "resolved_up") if c in df.columns]
    if info_cols:
        first = df.drop_duplicates("condition_id")[["condition_id"] + info_cols].copy()
        first["condition_id"] = first["condition_id"].astype(str)
        first = first.set_index("condition_id")
    else:
        first = pd.DataFrame(index=cond.index)
    ru = pd.Series(np.nan, index=cond.index, dtype="float64")
    if "resolved_up" in first.columns:
        ru = first["resolved_up"].astype(object).map(_bool_or_nan).astype("float64").reindex(cond.index)
    if resolved is not None:
        extra = pd.Series(resolved, dtype="object")
        extra.index = extra.index.astype(str)
        extra = extra.map(_bool_or_nan).astype("float64")
        ru = ru.where(ru.notna(), extra.reindex(ru.index))
    cid = out["condition_id"]
    r = cid.map(ru).to_numpy(dtype="float64")
    out["resolved_up"] = pd.array([pd.NA if np.isnan(v) else bool(v) for v in r], dtype="boolean")
    out["payout"] = out["pos_up"] * r + out["pos_down"] * (1.0 - r)
    out["pnl"] = out["cash"] - out["fees"] + out["payout"]
    pool = rebate_rate * cid.map(cond["m_fees"]).to_numpy(dtype="float64")
    tot_equiv = cid.map(cond["m_maker_equiv"]).to_numpy(dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(tot_equiv > 0, out["maker_fee_equiv"].to_numpy() / tot_equiv, 0.0)
    out["rebate_est"] = pool * share
    out["pnl_incl_rebate"] = out["pnl"] + out["rebate_est"]
    with np.errstate(invalid="ignore", divide="ignore"):
        out["taker_share"] = np.where(out["volume"] > 0, out["taker_notional"] / out["volume"], np.nan)
    out["net_up"] = (out["pos_up"] - out["pos_down"]).round(6)
    out["direction"] = np.where(out["net_up"] > 1e-6, "Up", np.where(out["net_up"] < -1e-6, "Down", "flat"))
    for c in info_cols:
        if c == "resolved_up":
            continue
        out[c] = cid.map(first[c]).to_numpy()
        if c in ("window_start", "window_end"):
            out[c] = pd.to_datetime(out[c], utc=True)
    for c in ("slug", "asset", "duration", "window_start", "window_end"):
        if c not in out.columns:
            out[c] = None
    # nom / pseudonyme : première valeur non vide par wallet
    for c in ("name", "pseudonym"):
        if c in df.columns:
            pairs = df[["wallet", c]].drop_duplicates()
            vals = pairs[c].astype(object)
            pairs = pairs[vals.notna() & (vals.astype(str) != "")]
            names = pd.Series(pairs[c].astype(str).to_numpy(), index=pairs["wallet"].astype(str).to_numpy())
            names = names[~names.index.duplicated()]
            out[c] = out["wallet"].map(names).fillna("")
        else:
            out[c] = ""
    out = out[PNL_COLUMNS]
    sort_cols = ["window_start", "pnl"] if out["window_start"].notna().any() else ["pnl"]
    asc = [True, False] if len(sort_cols) == 2 else [False]
    return out.sort_values(sort_cols, ascending=asc, kind="stable").reset_index(drop=True)


def hourly_wallet_table(trades_df: pd.DataFrame | None, pnl_df: pd.DataFrame) -> pd.DataFrame:
    """Agrégat par (heure UTC du début de fenêtre S, wallet).

    Colonnes : ``pnl``, ``pnl_incl_rebate``, ``fees``, ``rebate_est``, ``volume``, ``n_markets``,
    ``n_trades``, ``n_wins`` / ``win_rate`` (marchés à pnl > 0), ``pnl_per_market``,
    ``taker_share`` (part du volume en preneur), ``first_t_rel_median`` (médiane, sur les
    marchés, de la première exécution en s depuis S), ``t_rel_median`` (médiane sur toutes les
    exécutions, depuis ``trades_df``), ``assets`` ; ``rank_pnl`` = rang du wallet dans l'heure
    (1 = meilleur P&L). Tri : heure, rang.
    """
    if pnl_df is None or len(pnl_df) == 0:
        return pd.DataFrame(columns=HOURLY_COLUMNS)
    p = pnl_df
    hour = pd.to_datetime(p["window_start"], utc=True).dt.floor("h")
    w = pd.DataFrame(
        {
            "hour": hour,
            "wallet": p["wallet"].astype(str),
            "pnl": p["pnl"],
            "pnl_incl_rebate": p["pnl_incl_rebate"],
            "fees": p["fees"],
            "rebate_est": p["rebate_est"],
            "volume": p["volume"],
            "taker_notional": p["volume"] * p["taker_share"].fillna(0.0),
            "n_trades": p["n_trades"],
            "win": (p["pnl"] > 0).astype("int64"),
            "first_t_rel_s": p["first_t_rel_s"],
            "condition_id": p["condition_id"],
            "asset": p["asset"].astype(str),
        }
    )
    g = w.groupby(["hour", "wallet"], sort=True)
    out = g.agg(
        pnl=("pnl", "sum"),
        pnl_incl_rebate=("pnl_incl_rebate", "sum"),
        fees=("fees", "sum"),
        rebate_est=("rebate_est", "sum"),
        volume=("volume", "sum"),
        taker_notional=("taker_notional", "sum"),
        n_markets=("condition_id", "nunique"),
        n_trades=("n_trades", "sum"),
        n_wins=("win", "sum"),
        first_t_rel_median=("first_t_rel_s", "median"),
        assets=("asset", lambda s: ",".join(sorted(set(s)))),
    ).reset_index()
    out["win_rate"] = out["n_wins"] / out["n_markets"]
    out["pnl_per_market"] = out["pnl"] / out["n_markets"]
    with np.errstate(invalid="ignore", divide="ignore"):
        out["taker_share"] = np.where(out["volume"] > 0, out["taker_notional"] / out["volume"], np.nan)
    out["t_rel_median"] = np.nan
    if trades_df is not None and len(trades_df) and {"window_start", "t_rel_s"} <= set(trades_df.columns):
        t = pd.DataFrame(
            {
                "hour": pd.to_datetime(trades_df["window_start"], utc=True).dt.floor("h"),
                "wallet": trades_df["wallet"],
                "t_rel_s": pd.to_numeric(trades_df["t_rel_s"], errors="coerce").astype("float64"),
            }
        )
        med = t.groupby(["hour", "wallet"], observed=True)["t_rel_s"].median()
        med.index = pd.MultiIndex.from_arrays(
            [med.index.get_level_values(0), med.index.get_level_values(1).astype(str)]
        )
        key = pd.MultiIndex.from_arrays([out["hour"], out["wallet"]])
        out["t_rel_median"] = med.reindex(key).to_numpy()
    out["rank_pnl"] = out.groupby("hour")["pnl"].rank(ascending=False, method="first").astype("int64")
    names = pnl_df.drop_duplicates("wallet").copy()
    names.index = pd.Index(names["wallet"].astype(str))
    for c in ("name", "pseudonym"):
        out[c] = out["wallet"].map(names[c]).fillna("") if c in names.columns else ""
    out = out[HOURLY_COLUMNS]
    return out.sort_values(["hour", "rank_pnl"], kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------
def fetch_leaderboard(
    period: str = "day",
    order_by: str = "PNL",
    category: str = "crypto",
    limit: int = 100,
    *,
    client: Any = None,
) -> pd.DataFrame:
    """Classement officiel ``data-api /v1/leaderboard`` (pages de 50, pagination ``offset``).

    ``period`` : day | week | month | all ; ``order_by`` : PNL | VOL. Colonnes : ``rank``,
    ``wallet``, ``user_name``, ``x_username``, ``verified``, ``vol``, ``pnl``, ``period``,
    ``order_by``, ``category``, ``fetched_at`` (UTC).
    """
    c = _as_client(client)
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        n = min(LEADERBOARD_PAGE, limit - len(rows))
        params = {"timePeriod": period, "orderBy": order_by, "category": category, "limit": n, "offset": offset}
        data = c._get(LEADERBOARD_URL, params) or []
        if isinstance(data, dict):
            data = data.get("data") or []
        rows.extend(data)
        if len(data) < n:
            break
        offset += len(data)
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame(
        {
            "rank": pd.array([int(_pick(r, "rank", default=0)) for r in rows], dtype="int64"),
            "wallet": [str(_pick(r, "proxyWallet", "user_id", "proxy_wallet", default="")).lower() for r in rows],
            "user_name": [str(_pick(r, "userName", "user_name", default="")) for r in rows],
            "x_username": [str(_pick(r, "xUsername", "x_username", default="")) for r in rows],
            "verified": [bool(_pick(r, "verifiedBadge", "verified", default=False)) for r in rows],
            "vol": [pm._f(_pick(r, "vol", "volume")) for r in rows],
            "pnl": [pm._f(_pick(r, "pnl")) for r in rows],
        }
    )
    df["period"] = period
    df["order_by"] = order_by
    df["category"] = category
    df["fetched_at"] = now
    return df[LEADERBOARD_COLUMNS].head(limit).reset_index(drop=True)


def save_leaderboard_snapshots(
    periods: Sequence[str] = ("day", "week"),
    orders: Sequence[str] = ("PNL", "VOL"),
    category: str = "crypto",
    limit: int = 100,
    *,
    client: Any = None,
    cache_dir: Path = WALLETS_CACHE_DIR,
) -> list[Path]:
    """Écrit ``leaderboard_{category}_{period}_{order}_{AAAAMMJJTHHMMZ}.parquet`` dans ``cache_dir``."""
    c = _as_client(client)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%MZ")
    paths = []
    for period in periods:
        for order in orders:
            df = fetch_leaderboard(period, order, category, limit, client=c)
            path = Path(cache_dir) / f"leaderboard_{category}_{period}_{order.lower()}_{stamp}.parquet"
            pm._atomic_parquet(df, path)
            paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Activité (SPLIT / MERGE / REDEEM) des wallets
# ---------------------------------------------------------------------------
def fetch_activity(client_or_session: Any, wallet: str, start: Any, end: Any) -> pd.DataFrame:
    """Activité d'un wallet (``/v2/activity``) entre ``start`` et ``end`` (pagination par curseur).

    Colonnes : ``wallet``, ``ts``, ``type`` (TRADE, SPLIT, MERGE, REDEEM…), ``condition_id``,
    ``token_id``, ``side``, ``outcome``, ``size``, ``usdc_size`` (pour TRADE : frais preneur
    inclus), ``price``, ``tx_hash``, ``slug``.
    """
    c = _as_client(client_or_session)
    params: dict[str, Any] = {
        "user": wallet,
        "start": pm._to_unix(start),
        "end": pm._to_unix(end),
        "limit": ACTIVITY_PAGE_LIMIT,
    }
    rows: list[dict] = []
    cursor = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = c._get(ACTIVITY_V2_URL, p)
        if data is None:
            break
        if isinstance(data, list):
            rows.extend(data)
            break
        rows.extend(data.get("data") or [])
        pg = data.get("pagination") or {}
        nxt = pg.get("next_cursor")
        if not pg.get("has_more") or not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    df = pd.DataFrame(
        {
            "wallet": [str(_pick(r, "proxy_wallet", "proxyWallet", default=wallet)).lower() for r in rows],
            "ts": pd.to_datetime(
                np.array([int(_pick(r, "timestamp", default=0)) for r in rows], dtype="int64"), unit="s", utc=True
            ).as_unit("ns"),
            "type": [str(_pick(r, "type", default="")).upper() for r in rows],
            "condition_id": [str(_pick(r, "condition_id", "conditionId", default="")) for r in rows],
            "token_id": [str(_pick(r, "token_id", "asset", default="")) for r in rows],
            "side": [str(_pick(r, "side", default="")).upper() for r in rows],
            "outcome": [str(_pick(r, "outcome", default="")) for r in rows],
            "size": [pm._f(_pick(r, "size")) for r in rows],
            "usdc_size": [pm._f(_pick(r, "usdc_size", "usdcSize")) for r in rows],
            "price": [pm._f(_pick(r, "price")) for r in rows],
            "tx_hash": [str(_pick(r, "transaction_hash", "transactionHash", default="")) for r in rows],
            "slug": [str(_pick(r, "slug", default="")) for r in rows],
        },
        columns=ACTIVITY_COLUMNS,
    )
    return df


def enrich_with_activity(
    wallets: Iterable[str],
    start: Any,
    end: Any,
    *,
    client: Any = None,
    condition_ids: Iterable[str] | None = None,
    max_workers: int = 4,
) -> pd.DataFrame:
    """Montants SPLIT / MERGE / REDEEM / TRADE par (wallet, condition) depuis ``/v2/activity``.

    Colonnes : ``n_trade``, ``trade_buy_usdc``, ``trade_sell_usdc`` (frais preneur inclus :
    achat = p·C + frais, vente = p·C − frais), ``n_split``/``split_usdc``, ``n_merge``/
    ``merge_usdc``, ``n_redeem``/``redeem_usdc``, ``n_other``/``other_usdc`` et
    ``activity_cash = ventes − achats − splits + merges + redeems``. Pour un marché dont le
    wallet a tout réglé (REDEEM/MERGE faits), ``activity_cash`` doit égaler le ``pnl`` de
    :func:`wallet_market_pnl` aux frais d'intégrateur près (voir :func:`compare_with_activity`).
    Prévoir ``end`` quelques heures après la fin des marchés : les REDEEM arrivent plus tard.
    """
    c = _as_client(client)
    ws = list(dict.fromkeys(str(w).lower() for w in wallets))
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ws) or 1))) as ex:
        acts = list(ex.map(lambda w: fetch_activity(c, w, start, end), ws))
    act = pd.concat(acts, ignore_index=True) if acts else pd.DataFrame(columns=ACTIVITY_COLUMNS)
    cols = [
        "wallet", "condition_id", "n_trade", "trade_buy_usdc", "trade_sell_usdc", "n_split", "split_usdc",
        "n_merge", "merge_usdc", "n_redeem", "redeem_usdc", "n_other", "other_usdc", "activity_cash",
    ]
    if condition_ids is not None:
        act = act[act["condition_id"].isin(set(map(str, condition_ids)))]
    if len(act) == 0:
        return pd.DataFrame(columns=cols)
    typ = act["type"].to_numpy()
    usdc = act["usdc_size"].fillna(0.0).to_numpy()
    is_trade = typ == "TRADE"
    buy = is_trade & (act["side"].to_numpy() == "BUY")
    sell = is_trade & (act["side"].to_numpy() == "SELL")
    known = np.isin(typ, ["TRADE", "SPLIT", "MERGE", "REDEEM"])
    w = pd.DataFrame(
        {
            "wallet": act["wallet"].to_numpy(),
            "condition_id": act["condition_id"].to_numpy(),
            "n_trade": is_trade.astype("int64"),
            "trade_buy_usdc": usdc * buy,
            "trade_sell_usdc": usdc * sell,
            "n_split": (typ == "SPLIT").astype("int64"),
            "split_usdc": usdc * (typ == "SPLIT"),
            "n_merge": (typ == "MERGE").astype("int64"),
            "merge_usdc": usdc * (typ == "MERGE"),
            "n_redeem": (typ == "REDEEM").astype("int64"),
            "redeem_usdc": usdc * (typ == "REDEEM"),
            "n_other": (~known).astype("int64"),
            "other_usdc": usdc * ~known,
        }
    )
    out = w.groupby(["wallet", "condition_id"], sort=False).sum().reset_index()
    out["activity_cash"] = (
        out["trade_sell_usdc"] - out["trade_buy_usdc"] - out["split_usdc"] + out["merge_usdc"] + out["redeem_usdc"]
    )
    return out[cols]


def compare_with_activity(pnl_df: pd.DataFrame, activity_df: pd.DataFrame, tol: float = 0.01) -> pd.DataFrame:
    """Rapproche ``pnl`` (trades) et ``activity_cash`` (activité réglée) par (wallet, condition).

    ``settled`` : le wallet n'a plus de position ouverte côté activité (REDEEM présent, ou
    positions nettes nulles / perdantes). ``diff = activity_cash − pnl`` ; ``match`` si
    ``|diff| ≤ tol + 1 % du volume``.
    """
    a = activity_df.copy()
    p = pnl_df.copy()
    for d in (a, p):
        d["wallet"] = d["wallet"].astype(str).str.lower()
        d["condition_id"] = d["condition_id"].astype(str)
    m = p.merge(a, on=["wallet", "condition_id"], how="inner")
    if len(m) == 0:
        return m.assign(settled=pd.Series(dtype=bool), diff=pd.Series(dtype=float), match=pd.Series(dtype=bool))
    win_pos = np.where(m["resolved_up"].astype("boolean").fillna(False), m["pos_up"], m["pos_down"])
    m["settled"] = (m["n_redeem"] > 0) | (np.asarray(win_pos, dtype=float) <= 1e-6)
    m["diff"] = m["activity_cash"] - m["pnl"]
    m["match"] = m["diff"].abs() <= tol + 0.01 * m["volume"]
    return m


# ---------------------------------------------------------------------------
# Ligne de commande : collecte + instantanés
# ---------------------------------------------------------------------------
def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collecte des exécutions et du P&L des wallets Polymarket Up/Down")
    ap.add_argument("--hours", type=float, default=48.0, help="durée collectée (fenêtres commençant dans [fin − h, fin))")
    ap.add_argument("--end", default=None, help="fin (UTC) ; défaut : heure pleine UTC précédente")
    ap.add_argument("--assets", default="btc,eth,sol")
    ap.add_argument("--durations", default="5m,15m")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rps", type=float, default=DEFAULT_MAX_RPS)
    ap.add_argument("--cache-dir", default=str(WALLETS_CACHE_DIR))
    ap.add_argument("--no-leaderboard", action="store_true")
    ap.add_argument("--validate-activity", type=int, default=0, help="recoupe le P&L de N wallets avec /v2/activity")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cache_dir = Path(args.cache_dir)
    end = pm._to_utc(args.end) if args.end else pd.Timestamp.now(tz="UTC").floor("h")
    start = end - pd.Timedelta(hours=args.hours)
    t0 = time.time()
    client = _as_client(None, args.rps, cache_dir=cache_dir, use_cache=True)
    markets, trades = collect_trades(
        tuple(args.assets.split(",")), tuple(args.durations.split(",")), start, end,
        cache=True, client=client, cache_dir=cache_dir, max_workers=args.workers, progress=True,
    )
    t_collect = time.time() - t0
    pnl = wallet_market_pnl(trades)
    hourly = hourly_wallet_table(trades, pnl)
    stamp = f"{start.strftime('%Y%m%dT%H')}_{end.strftime('%Y%m%dT%H')}"
    derived = cache_dir / "derived"
    pm._atomic_parquet(markets, derived / f"markets_{stamp}.parquet")
    pm._atomic_parquet(pnl, derived / f"wallet_market_pnl_{stamp}.parquet")
    pm._atomic_parquet(hourly, derived / f"hourly_wallets_{stamp}.parquet")
    if not args.no_leaderboard:
        save_leaderboard_snapshots(client=client, cache_dir=cache_dir)
    rc = role_consistency(trades)
    is_t = trades["role"] == "taker"
    summary = {
        "period": [start.isoformat(), end.isoformat()],
        "markets": int(len(markets)),
        "markets_by_series": {f"{a}_{d}": int(n) for (a, d), n in markets.groupby(["asset", "duration"]).size().items()},
        "fee_schedules": {
            f"{t}|rate={r}|exp={e}": int(n)
            for (t, r, e), n in markets.groupby(["fee_type", "fee_rate", "fee_exponent"], dropna=False).size().items()
        },
        "fills": int(len(trades)),
        "taker_fills": int(is_t.sum()),
        "maker_fills": int((trades["role"] == "maker").sum()),
        "wallets": int(trades["wallet"].nunique()),
        "taker_wallets": int(trades.loc[is_t, "wallet"].nunique()),
        "taker_volume_usd": float(trades.loc[is_t, "notional"].sum()),
        "role_consistency": rc,
        "cache_bytes": _dir_size(cache_dir),
        "seconds_collect": round(t_collect, 1),
        "zero_sum_check": float(pnl["pnl"].sum() + pnl["fees"].sum()),
    }
    if args.validate_activity:
        by_wallet = pnl.groupby("wallet")[["pnl", "volume"]].sum()
        half = max(1, args.validate_activity // 2)
        top = list(dict.fromkeys(
            list(by_wallet["pnl"].nlargest(half).index)
            + list(by_wallet["volume"].nlargest(args.validate_activity - half).index)
        ))
        act = enrich_with_activity(top, start - pd.Timedelta(hours=1), pd.Timestamp.now(tz="UTC"), client=client,
                                   condition_ids=markets["condition_id"])
        cmp = compare_with_activity(pnl[pnl["wallet"].isin(top)], act)
        st = cmp[cmp["settled"]]
        summary["activity_check"] = {
            "wallets": int(len(top)),
            "pairs": int(len(cmp)),
            "settled_pairs": int(len(st)),
            "share_match": float(st["match"].mean()) if len(st) else math.nan,
            "median_abs_diff": float(st["diff"].abs().median()) if len(st) else math.nan,
            "n_split": int(act["n_split"].sum()) if len(act) else 0,
            "n_merge": int(act["n_merge"].sum()) if len(act) else 0,
            "n_redeem": int(act["n_redeem"].sum()) if len(act) else 0,
        }
        pm._atomic_parquet(cmp, derived / f"activity_check_{stamp}.parquet")
    summary["seconds_total"] = round(time.time() - t0, 1)
    print(json.dumps(summary, indent=1, default=str))
    return 0


__all__ = [
    "CRYPTO_FEE_RATE",
    "MAKER_REBATE_RATE",
    "WALLETS_CACHE_DIR",
    "assign_roles",
    "collect_trades",
    "compare_with_activity",
    "enrich_with_activity",
    "fetch_activity",
    "fetch_leaderboard",
    "fetch_market_trades",
    "hourly_wallet_table",
    "role_consistency",
    "save_leaderboard_snapshots",
    "trades_v2_frame",
    "wallet_market_pnl",
]


if __name__ == "__main__":
    raise SystemExit(main())
