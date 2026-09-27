"""Signaux sociaux d'un token pump.fun, datés pour ne jamais utiliser d'information future.

Ce qui est accessible sans clé (vérifié le 27/09/2026) :

* **Liens déclarés au lancement** (métadonnées pump.fun) : X/Twitter, Telegram, site. Le type
  de lien X compte : un compte (``x.com/handle``), un tweet (``/status/``), une communauté
  (``/i/communities/``) ou une recherche ne signalent pas la même chose.
* **DexScreener** : fiche de token payée (``tokenProfile``), pubs, boosts payants, avec leur
  ``paymentTimestamp`` (``api.dexscreener.com/orders/v1/solana/{mint}``, 60 requêtes/min).
  On sait donc *quand* quelqu'un a payé pour promouvoir le token, y compris dans le passé.

Ce qui exige une clé ou un accès que ce dépôt n'a pas :

* **X** : l'API ``/2/tweets/counts/recent`` (7 derniers jours, par minute) donne le nombre de
  posts citant l'adresse du token ou son ticker avant l'instant t. Offre payante ; activée
  ici si la variable d'environnement ``X_BEARER_TOKEN`` existe (:class:`XCounts`).
* **TikTok, Instagram** : aucune API publique de recherche (l'API recherche de TikTok est
  réservée aux chercheurs académiques, Instagram n'expose que ses propres comptes) ; les
  requêtes directes sont refusées depuis ce serveur (TikTok : réponse vide, Instagram :
  redirection vers la connexion). Seule voie : un prestataire de collecte payant. La
  structure :class:`MentionSource` permet de brancher une telle source sans toucher au reste.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import pandas as pd

log = logging.getLogger(__name__)

DEX_URL = "https://api.dexscreener.com"
X_URL = "https://api.x.com/2"

# Sites générés par des services de lancement (pas un site fait pour le projet).
LAUNCHPAD_HOSTS = ("usepaid.app", "pump.fun", "letsbonk.fun", "bonk.fun", "moonshot", "believe.app",
                   "boop.fun", "linktr.ee")


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def twitter_kind(url: str) -> str:
    """Nature du lien X : none, profile, status, community, search ou other."""
    u = _s(url)
    if not u:
        return "none"
    if not re.match(r"^https?://", u):
        u = "https://" + u
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if host not in ("x.com", "twitter.com"):
        return "other"
    path = [s for s in p.path.split("/") if s]
    if not path:
        return "other"
    if path[0] == "i" and len(path) > 1 and path[1] == "communities":
        return "community"
    if path[0] in ("search", "hashtag") or "q=" in p.query:
        return "search"
    if len(path) >= 3 and path[1] == "status":
        return "status"
    if len(path) == 1:
        return "profile"
    return "other"


def twitter_handle(url: str) -> str:
    u = _s(url)
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", u)
    if not m or m.group(1).lower() in ("i", "search", "hashtag", "home", "intent"):
        return ""
    return m.group(1)


def link_features(coin: dict | pd.Series) -> dict[str, Any]:
    """Signaux sociaux connus au lancement (métadonnées pump.fun)."""
    tw, tg, web = _s(coin.get("twitter")), _s(coin.get("telegram")), _s(coin.get("website"))
    kind = twitter_kind(tw)
    handle = twitter_handle(tw).lower()
    sym = re.sub(r"[^a-z0-9]", "", _s(coin.get("symbol")).lower())
    name = re.sub(r"[^a-z0-9]", "", _s(coin.get("name")).lower())
    host = urlparse(web if re.match(r"^https?://", web) else "https://" + web).netloc.lower() if web else ""
    desc = _s(coin.get("description"))
    return {
        "has_twitter": int(bool(tw)),
        "tw_profile": int(kind == "profile"),
        "tw_status": int(kind == "status"),
        "tw_community": int(kind == "community"),
        "tw_handle_matches": int(bool(handle) and bool(sym or name) and (sym in handle or (len(name) >= 3 and name in handle))),
        "has_telegram": int(bool(tg)),
        "has_website": int(bool(web)),
        "website_launchpad": int(bool(host) and any(h in host for h in LAUNCHPAD_HOSTS)),
        "n_socials": int(bool(tw)) + int(bool(tg)) + int(bool(web)),
        "desc_len": len(desc),
        "desc_has_url": int(bool(re.search(r"https?://|\.com|\.fun|@", desc))),
        "name_len": len(_s(coin.get("name"))),
        "symbol_upper": int(_s(coin.get("symbol")).isupper()),
        "image_ipfs": int("ipfs" in _s(coin.get("image_uri"))),
    }


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------
def dexscreener_feeds(cli) -> dict[str, list[dict]]:
    """Dernières fiches et derniers boosts payés sur Solana (flux « latest » de DexScreener)."""
    out: dict[str, list[dict]] = {}
    for kind, path in (("boosts", "token-boosts/latest/v1"), ("profiles", "token-profiles/latest/v1")):
        try:
            rows = cli.get(f"{DEX_URL}/{path}") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("DexScreener %s : %r", kind, exc)
            rows = []
        out[kind] = [{k: r.get(k) for k in ("chainId", "tokenAddress", "amount", "totalAmount", "links")}
                     for r in rows if r.get("chainId") == "solana"]
    return out


def dexscreener_orders(cli, mint: str) -> pd.DataFrame:
    """Commandes payées (fiche, pubs, boosts) d'un token, avec leur date de paiement (ms)."""
    d = cli.get(f"{DEX_URL}/orders/v1/solana/{mint}") or {}
    rows = [{"kind": o.get("type", "order"), "status": o.get("status"), "amount": None,
             "paid_ms": o.get("paymentTimestamp")} for o in d.get("orders") or []]
    rows += [{"kind": "boost", "status": "approved", "amount": b.get("amount"), "paid_ms": b.get("paymentTimestamp")}
             for b in d.get("boosts") or []]
    return pd.DataFrame(rows, columns=["kind", "status", "amount", "paid_ms"])


def dex_features(orders: pd.DataFrame, t_ms: int) -> dict[str, float]:
    """Promotion payée sur DexScreener avant ``t_ms`` (point-in-time)."""
    if orders is None or orders.empty:
        return {"dex_paid_profile": 0, "dex_boost_amount": 0.0, "dex_n_orders": 0}
    o = orders[pd.to_numeric(orders["paid_ms"], errors="coerce") <= t_ms]
    return {
        "dex_paid_profile": int((o["kind"] == "tokenProfile").any()),
        "dex_boost_amount": float(pd.to_numeric(o.loc[o["kind"] == "boost", "amount"], errors="coerce").fillna(0).sum()),
        "dex_n_orders": int(len(o)),
    }


# ---------------------------------------------------------------------------
# Mentions sur les réseaux (X, et toute source branchée plus tard)
# ---------------------------------------------------------------------------
class MentionSource(Protocol):
    name: str

    def mentions(self, mint: str, symbol: str, start_ms: int, end_ms: int) -> int | None:
        """Nombre de posts citant le token entre ``start_ms`` et ``end_ms`` (None si inconnu)."""


@dataclass
class XCounts:
    """Nombre de posts X citant l'adresse du token (``/2/tweets/counts/recent``, 7 derniers jours).

    L'adresse (« CA ») est précise ; un ticker court est ambigu et n'est ajouté que s'il fait
    au moins 4 caractères.
    """

    token: str
    name: str = "x"

    @classmethod
    def from_env(cls) -> "XCounts | None":
        tok = os.environ.get("X_BEARER_TOKEN")
        return cls(tok) if tok else None

    def mentions(self, mint: str, symbol: str, start_ms: int, end_ms: int) -> int | None:
        import requests
        q = f'"{mint}"'
        if symbol and len(symbol) >= 4 and symbol.isascii() and symbol.isalnum():
            q = f"({q} OR ${symbol})"
        iso = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        try:
            r = requests.get(f"{X_URL}/tweets/counts/recent", timeout=20,
                             headers={"Authorization": f"Bearer {self.token}"},
                             params={"query": q, "start_time": iso(start_ms), "end_time": iso(end_ms),
                                     "granularity": "minute"})
            if r.status_code != 200:
                log.warning("X counts %s : HTTP %s", mint, r.status_code)
                return None
            return int(r.json().get("meta", {}).get("total_tweet_count", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("X counts %s : %r", mint, exc)
            return None


def mention_sources() -> list[MentionSource]:
    """Sources de mentions configurées (X si ``X_BEARER_TOKEN`` est défini)."""
    return [s for s in (XCounts.from_env(),) if s is not None]
