"""Client pump.fun en LECTURE SEULE : lancements, graduations, trades et bougies.

pump.fun lance des tokens Solana sur une *bonding curve* (produit constant, réserves
virtuelles). Quand les ≈ 793 M de tokens vendables de la courbe sont achetés (≈ 85 SOL levés),
le token « gradue » : sa liquidité part sur PumpSwap (programme ``pump_amm``). Ce module sert
à constituer un univers **sans biais du survivant** (tous les lancements, pas seulement ceux
qui ont marché) et à reconstituer, pour chaque token, les trades avant un instant donné.

**Aucune fonction d'achat ou de vente, aucune clé** : uniquement des GET publics.

Faits vérifiés sur les API (27/09/2026)
---------------------------------------
* ``frontend-api-v3.pump.fun/coins`` : liste triable (``sort=created_timestamp``,
  ``order=DESC``, ``includeNsfw``, ``complete=true`` pour les gradués). **``offset`` plafonne
  à 1000** (au-delà : ``[]``) et aucun paramètre de date n'est pris en compte : on ne voit que
  les ≈ 1050 derniers lancements (≈ 55 min) et les ≈ 1050 dernières graduations (≈ 22 h).
  Un historique complet des lancements exige donc un collecteur qui tourne en continu
  (``scripts/pumpfun_collector.py``).
* ``swap-api.pump.fun`` est limité à ≈ 20 requêtes par minute et par adresse IP (au-delà :
  429 et ``Retry-After: 60``) : lire tous les lancements est impossible, l'étude échantillonne.
* ``swap-api.pump.fun/v2/coins/{mint}/trades`` : ``limit`` ≤ 100, du plus récent au plus
  ancien, ``pagination.nextCursor = "{slotIndexId}-{timestamp ms}"``. **Un curseur forgé
  ``"9999999999999999999999-{t ms}"`` saute directement aux trades antérieurs à t** : on peut
  donc relire n'importe quelle fenêtre passée, y compris les premières minutes d'un token.
  Horodatage à la seconde ; ``slotIndexId`` donne l'ordre exact dans la seconde.
* ``swap-api.pump.fun/v2/coins/{mint}/candles`` : ``interval`` 1s, 1m, 5m, 15m, 1h, 4h ;
  ``limit`` ≤ 1000 ; ``createdTs`` obligatoire ; ``currency`` USD ou SOL. Seules les **1000
  dernières bougies** sont servies (aucun paramètre de début ou de fin) : 1m couvre 16,7 h,
  5m 3,5 jours, 1h 41 jours.
* ``priceSol`` est le prix d'un token entier en SOL (≈ 2,8e-8 au lancement) ; market cap en
  SOL = prix × 10⁹ (offre totale). ``program`` vaut ``pump`` sur la courbe, ``pump_amm``
  après la graduation.
* Courbe : réserves virtuelles initiales 30 SOL et 1 073 000 000 tokens (k constant), dont
  793 100 000 vendables ; la courbe finit à ≈ 4,1e-7 SOL par token (≈ 411 SOL de market cap).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .config import CACHE_DIR

log = logging.getLogger(__name__)

FRONTEND_URL = "https://frontend-api-v3.pump.fun"
SWAP_URL = "https://swap-api.pump.fun"
CACHE = CACHE_DIR / "pumpfun"

SOL_MINT = "11111111111111111111111111111111"
TOTAL_SUPPLY = 1_000_000_000              # tokens entiers (6 décimales on-chain)
V_TOKEN0 = 1_073_000_000                  # réserve virtuelle initiale de tokens
V_SOL0 = 30.0                             # réserve virtuelle initiale de SOL
REAL_TOKEN0 = 793_100_000                 # tokens vendables sur la courbe
K_CURVE = V_SOL0 * V_TOKEN0               # produit constant (SOL × tokens)
GRAD_PRICE_SOL = K_CURVE / (V_TOKEN0 - REAL_TOKEN0) ** 2    # ≈ 4,11e-7 SOL par token
LISTING_CAP = 1000                        # offset maximal servi par /coins
FAR_SLOT = "9999999999999999999999"

TRADE_COLUMNS = ["ts", "slot", "side", "program", "price_sol", "price_usd", "sol", "usd", "tokens", "user", "tx"]


# ---------------------------------------------------------------------------
# Courbe
# ---------------------------------------------------------------------------
def curve_progress(price_sol) -> np.ndarray:
    """Part (0 → 1) des tokens vendables déjà achetés, déduite du prix sur la courbe."""
    p = np.asarray(price_sol, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        v_tok = np.sqrt(K_CURVE / p)
    return np.clip((V_TOKEN0 - v_tok) / REAL_TOKEN0, 0.0, 1.0)


def curve_price(progress) -> np.ndarray:
    """Prix (SOL par token) quand une part ``progress`` de la courbe est vendue."""
    v_tok = V_TOKEN0 - np.asarray(progress, float) * REAL_TOKEN0
    return K_CURVE / v_tok ** 2


def curve_buy_price(price_sol: float, sol_in: float, fee: float = 0.0125) -> float:
    """Prix moyen payé (SOL par token) pour acheter ``sol_in`` SOL sur la courbe au prix ``price_sol``.

    Produit constant : le glissement dépend de la taille de l'achat face aux réserves
    virtuelles. ``fee`` est prélevé sur le SOL entrant (pump.fun : 0,95 % protocole +
    0,30 % créateur en 2026 ; paramètre explicite pour les tests de sensibilité).
    """
    v_tok = math.sqrt(K_CURVE / price_sol)
    v_sol = K_CURVE / v_tok
    net = sol_in * (1 - fee)
    tokens = v_tok - K_CURVE / (v_sol + net)
    return sol_in / tokens


def curve_sell_value(price_sol: float, tokens: float, fee: float = 0.0125) -> float:
    """SOL reçu en vendant ``tokens`` sur la courbe au prix ``price_sol`` (frais déduits)."""
    v_tok = math.sqrt(K_CURVE / price_sol)
    v_sol = K_CURVE / v_tok
    out = v_sol - K_CURVE / (v_tok + tokens)
    return out * (1 - fee)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, per_second: float):
        self.min_gap = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_t = 0.0

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.next_t = max(self.next_t, time.monotonic() + seconds)

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.min_gap
        if t > now:
            time.sleep(t - now)


class PumpFunClient:
    """GET publics pump.fun, avec relances et limite de débit (partagée entre threads)."""

    def __init__(self, per_second: float = 6.0, timeout: float = 20.0, tries: int = 6):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "tradebot-research/0.1", "Accept": "application/json",
                               "Origin": "https://pump.fun", "Referer": "https://pump.fun/"})
        self.rl = _RateLimiter(per_second)
        self.timeout = timeout
        self.tries = tries

    def get(self, url: str, params: dict | None = None) -> Any:
        err: Exception | None = None
        fails = throttled = 0
        while fails < self.tries and throttled < 60:
            self.rl.wait()
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (400, 404, 410):
                    raise ValueError(f"{r.status_code} {url} {r.text[:200]}")
                err = RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
                if r.status_code == 429:                 # Cloudflare : on attend ce qu'il demande, pour tout le client
                    throttled += 1
                    self.rl.penalize(float(r.headers.get("Retry-After") or 30) + 1)
                    continue
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 — réseau : on réessaie
                err = exc
            fails += 1
            time.sleep(min(30.0, 1.5 * 2 ** fails))
        raise RuntimeError(f"échec {url} {params} : {err!r}")

    # -- listes ---------------------------------------------------------------
    def coins(self, offset: int = 0, limit: int = 50, complete: bool | None = None,
              sort: str = "created_timestamp", order: str = "DESC") -> list[dict]:
        params: dict[str, Any] = {"offset": offset, "limit": limit, "sort": sort, "order": order,
                                  "includeNsfw": "true"}
        if complete is not None:
            params["complete"] = str(complete).lower()
        return self.get(f"{FRONTEND_URL}/coins", params) or []

    def recent_coins(self, since_ms: int = 0, complete: bool | None = None,
                     known: set[str] | None = None) -> list[dict]:
        """Tokens créés (ou gradués si ``complete``) depuis ``since_ms``, du plus récent au plus ancien.

        S'arrête au plafond d'``offset`` de l'API, ou dès qu'une page ne contient plus rien de
        nouveau (tous plus anciens que ``since_ms`` ou déjà dans ``known``).
        """
        out: list[dict] = []
        for off in range(0, LISTING_CAP + 1, 50):
            page = self.coins(offset=off, limit=50, complete=complete)
            if not page:
                break
            fresh = [c for c in page if c.get("created_timestamp", 0) >= since_ms
                     and (known is None or c["mint"] not in known)]
            out.extend(fresh)
            if not fresh:
                break
        return out

    # -- trades ---------------------------------------------------------------
    def trades(self, mint: str, end_ms: int, start_ms: int = 0, max_pages: int = 200) -> pd.DataFrame:
        """Trades de ``mint`` avec ``start_ms <= ts <= end_ms``, dans l'ordre chronologique.

        ``max_pages`` borne le coût (100 trades par page) : si la fenêtre n'est pas épuisée,
        l'attribut ``df.attrs["truncated"]`` vaut True et seuls les trades les plus récents
        de la fenêtre sont présents.
        """
        rows: list[dict] = []
        cursor = f"{FAR_SLOT}-{int(end_ms)}"
        truncated = False
        for page_no in range(max_pages):
            d = self.get(f"{SWAP_URL}/v2/coins/{mint}/trades", {"limit": 100, "cursor": cursor})
            tr = d.get("trades") or []
            rows.extend(tr)
            pag = d.get("pagination") or {}
            if not tr or not pag.get("hasMore"):
                break
            oldest = _ts_ms(tr[-1]["timestamp"])
            if oldest < start_ms:
                break
            cursor = pag.get("nextCursor")
            if not cursor:
                break
        else:
            truncated = True
        df = parse_trades(rows)
        df = df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].reset_index(drop=True)
        df.attrs["truncated"] = truncated
        return df

    def candles(self, mint: str, created_ms: int, interval: str = "1m", currency: str = "SOL",
                limit: int = 1000) -> pd.DataFrame:
        """Les ``limit`` dernières bougies (aucun historique plus ancien n'est servi)."""
        d = self.get(f"{SWAP_URL}/v2/coins/{mint}/candles",
                     {"interval": interval, "limit": limit, "currency": currency, "createdTs": int(created_ms)})
        df = pd.DataFrame(d or [], columns=["timestamp", "open", "high", "low", "close", "volume"])
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.rename(columns={"timestamp": "ts"}).sort_values("ts").reset_index(drop=True)


def _ts_ms(s: str) -> int:
    return int(pd.Timestamp(s).value // 1_000_000)


def parse_trades(rows: list[dict]) -> pd.DataFrame:
    """Trades bruts de l'API -> DataFrame typé, chronologique, sans doublons."""
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in zip(
            TRADE_COLUMNS, ["int64", "object", "object", "object"] + ["float64"] * 5 + ["object", "object"])})
    df = pd.DataFrame({
        "ts": [_ts_ms(r["timestamp"]) for r in rows],
        "slot": [r.get("slotIndexId", "") for r in rows],
        "side": [r.get("type") for r in rows],
        "program": [r.get("program") for r in rows],
        "price_sol": pd.to_numeric([r.get("priceSol") for r in rows], errors="coerce"),
        "price_usd": pd.to_numeric([r.get("priceUsd") for r in rows], errors="coerce"),
        "sol": pd.to_numeric([r.get("amountSol") for r in rows], errors="coerce"),
        "usd": pd.to_numeric([r.get("amountUsd") for r in rows], errors="coerce"),
        "tokens": pd.to_numeric([r.get("baseAmount") for r in rows], errors="coerce"),
        "user": [r.get("userAddress") for r in rows],
        "tx": [r.get("tx") for r in rows],
    })
    df = df.drop_duplicates(["tx", "slot", "user", "side"])
    return df.sort_values(["ts", "slot"], kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Univers collecté
# ---------------------------------------------------------------------------
LAUNCH_FIELDS = ("mint", "name", "symbol", "description", "image_uri", "metadata_uri", "twitter", "telegram",
                 "website", "creator", "created_timestamp", "complete", "program", "quote_mint", "nsfw",
                 "market_cap", "usd_market_cap", "ath_market_cap", "real_sol_reserves", "real_token_reserves",
                 "reply_count", "is_currently_live", "username", "boost_mode", "is_cashback_enabled",
                 "is_holder_reward", "pool_address", "king_of_the_hill_timestamp", "last_trade_timestamp",
                 "virtual_sol_reserves", "virtual_token_reserves", "mayhem_state", "inverted", "transfer_fee_bps")


def slim(coin: dict, seen_ms: int) -> dict:
    """Instantané point-in-time d'un token : ce que l'on savait à ``seen_ms``."""
    out = {k: coin.get(k) for k in LAUNCH_FIELDS}
    out["seen_ms"] = seen_ms
    return out


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:          # ligne tronquée par un arrêt brutal
                    continue
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@dataclass
class Universe:
    launches: pd.DataFrame       # un token par ligne, premier instantané vu
    graduations: pd.DataFrame    # mint, premier instantané vu après graduation


def load_universe(root: Path = CACHE) -> Universe:
    """Lancements et graduations collectés (premier instantané par token)."""
    def first(path: Path) -> pd.DataFrame:
        df = pd.DataFrame(read_jsonl(path))
        if df.empty:
            return pd.DataFrame(columns=list(LAUNCH_FIELDS) + ["seen_ms"])
        return df.sort_values("seen_ms").drop_duplicates("mint").reset_index(drop=True)
    return Universe(first(root / "launches.jsonl"), first(root / "graduations.jsonl"))


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def is_standard_curve(coin: dict | pd.Series) -> bool:
    """Courbe pump.fun classique cotée en SOL : les constantes de ce module s'y appliquent.

    Exclus : le « mayhem mode » (``mayhem_state`` renseigné : un agent achète et vend avec une
    offre variable, le prix peut baisser sur la courbe et k n'est pas constant), les tokens
    cotés dans une autre monnaie que le SOL (USDC…), et les instantanés antérieurs à l'ajout
    de ces champs (``mayhem_state`` absent : type inconnu).
    """
    if "mayhem_state" not in coin:
        return False
    q = coin.get("quote_mint")
    if not (_missing(q) or q == SOL_MINT) or not _missing(coin.get("mayhem_state")):
        return False
    vs, vt = coin.get("virtual_sol_reserves"), coin.get("virtual_token_reserves")
    if _missing(vs) or _missing(vt):
        return False
    k = (float(vs) / 1e9) * (float(vt) / 1e6)
    return abs(k / K_CURVE - 1) < 0.02
