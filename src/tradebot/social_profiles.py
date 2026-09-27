"""Profils publics de réseaux sociaux : instantanés normalisés, différences, flux de changements.

Module annexe en **lecture seule** (comme ``polymarket*.py``) : uniquement des GET publics, sans
clé ni compte, sur des API ouvertes. Il sert à répondre à la question « peut-on voir les
changements d'un profil (nom, bio, photo, bannière, lien, épinglé, compteurs, statut du compte) ? »
et à mesurer avec quel délai. L'étude est dans ``docs/research/reseaux_sociaux.md``.

Trois façons de voir un changement, par ordre de coût décroissant pour nous :

1. **Flux poussé (push)** : le réseau publie chaque modification de profil de *tous* ses
   utilisateurs. C'est le cas des protocoles ouverts : Farcaster (événements du hub,
   ``MESSAGE_TYPE_USER_DATA_ADD``), Bluesky / AT Protocol (firehose et Jetstream, collection
   ``app.bsky.actor.profile``), Nostr (événements ``kind 0``). Délai : de l'ordre de la seconde.
2. **Interrogation et comparaison (poll + diff)** : on relit le profil à intervalle régulier et
   on compare à l'instantané précédent. C'est la seule voie pour X, Instagram, TikTok, YouTube,
   Telegram, Reddit… Le délai est l'intervalle d'interrogation, borné par les quotas.
3. **Horodatage fourni par la plateforme** : certaines API disent *quand* un champ a changé, ce qui
   permet de reconstituer un historique sans avoir observé le changement. Farcaster horodate
   chaque champ ; Bluesky donne ``indexedAt`` (profil entier) et l'historique des handles via le
   journal PLC ; GitHub donne ``updated_at`` ; X, Instagram, TikTok ne donnent rien.

Faits vérifiés depuis l'environnement (27/09/2026, voir ``scripts/social_profile_probe.py``)
------------------------------------------------------------------------------------------
* Bluesky : ``public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=…`` répond sans
  authentification avec ``displayName, description, avatar, banner, followersCount,
  followsCount, postsCount, indexedAt, createdAt, pinnedPost, labels, verification``.
  ``getProfiles`` accepte jusqu'à 25 ``actors``. Les réponses passent par un CDN qui les garde
  **30 s par URL** (``Cache-Control: public, max-age=30``, ``CDN-Cache: HIT``) : pour interroger
  plus souvent, ajouter un paramètre anti-cache (``fresh=True``, par défaut). Le PDS annonce
  ``ratelimit-policy: 3000;w=300`` (3 000 requêtes par 5 min et par IP) ; l'AppView public
  n'envoie pas d'en-tête de quota. L'URL de l'avatar contient le CID de l'image :
  elle change si et seulement si l'image change. ``com.atproto.repo.getRecord`` (collection
  ``app.bsky.actor.profile``, rkey ``self``) donne le CID de l'enregistrement de profil, qui
  change à chaque modification. ``plc.directory/{did}/log/audit`` donne l'historique des handles.
* Farcaster : ``hub.pinata.cloud/v1/userDataByFid?fid=…`` renvoie un message par champ
  (``PFP, DISPLAY, BIO, URL, USERNAME, LOCATION, TWITTER, GITHUB, BANNER,
  PRIMARY_ADDRESS_ETHEREUM…``), chacun avec un ``timestamp`` en secondes depuis l'époque
  Farcaster (01/01/2021 00:00 UTC) = **date de dernière modification du champ**.
  ``/v1/events?from_event_id=…`` pagine le journal d'événements du hub (1 000 par page,
  ``reverse=true`` pour la queue) : les modifications de profil y apparaissent comme
  ``HUB_EVENT_TYPE_MERGE_MESSAGE`` / ``MESSAGE_TYPE_USER_DATA_ADD``.
  **Attention à la fraîcheur du hub** : ``/v1/info`` donne par shard ``maxHeight`` et
  ``blockDelay`` ; le 27/09/2026 ``hub.pinata.cloud`` avait ≈ 25 millions de blocs de retard
  (≈ 10 mois, queue du journal horodatée du 06/12/2025). Les hubs sur ports non standard
  (``:2281``, ``:3381``) et l'API Neynar (402, paiement) n'étaient pas joignables d'ici.
* API du client Farcaster (ex-Warpcast, non documentée, en direct) :
  ``api.farcaster.xyz/v2/user-by-username?username=…`` (alias ``api.warpcast.com``) renvoie
  ``displayName, username, pfp.url, profile.bio.text, profile.location.description, profile.url,
  profile.bannerImageUrl, followerCount, followingCount``. Sans horodatage.
* Mastodon : ``/api/v1/accounts/lookup?acct=…`` public ; pas d'horodatage par champ.
* Telegram : la page ``t.me/<canal>`` d'un canal public contient le titre, la description, la
  photo et le nombre d'abonnés (``tgme_page_extra``) sans connexion. Pour un utilisateur ou un
  bot, la page « Contact » ne donne que le nom et la photo.
* X : ``api.x.com/2/users/by/username/…`` répond 401 sans jeton ; le parseur ci-dessous suit
  le schéma documenté de l'objet ``user`` (``user.fields``). Aucun horodatage de modification.

Contrat : aucune écriture, aucun stockage de données au-delà de ce que l'appelant fait des
instantanés. On ne suit que des comptes publics d'organisations ou de personnalités publiques.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import requests

FARCASTER_EPOCH = 1_609_459_200  # 2021-01-01T00:00:00Z

BLUESKY_PUBLIC_URL = "https://public.api.bsky.app/xrpc"
FARCASTER_HUB_URL = "https://hub.pinata.cloud/v1"
FARCASTER_CLIENT_URL = "https://api.farcaster.xyz/v2"  # alias : https://api.warpcast.com/v2
WARPCAST_URL = FARCASTER_CLIENT_URL
TELEGRAM_WEB_URL = "https://t.me"
X_API_URL = "https://api.x.com/2"
PLC_URL = "https://plc.directory"
JETSTREAM_URL = "wss://jetstream2.us-east.bsky.network/subscribe"

USER_AGENT = "tradebot-research/0.1 (lecture seule, profils publics)"

#: Champs normalisés, communs à toutes les plateformes (None si la plateforme ne l'expose pas).
PROFILE_FIELDS: tuple[str, ...] = (
    "handle",
    "display_name",
    "bio",
    "avatar",
    "banner",
    "url",
    "location",
    "pinned",
    "followers",
    "following",
    "posts",
    "verified",
    "status",
)
#: Compteurs : ils bougent sans cesse, on les compare à part.
COUNTER_FIELDS: tuple[str, ...] = ("followers", "following", "posts")

X_USER_FIELDS = (
    "created_at,description,entities,location,name,pinned_tweet_id,profile_banner_url,"
    "profile_image_url,protected,public_metrics,url,username,verified,verified_type,withheld"
)

_FARCASTER_TYPE_TO_FIELD = {
    "USER_DATA_TYPE_PFP": "avatar",
    "USER_DATA_TYPE_DISPLAY": "display_name",
    "USER_DATA_TYPE_BIO": "bio",
    "USER_DATA_TYPE_URL": "url",
    "USER_DATA_TYPE_USERNAME": "handle",
    "USER_DATA_TYPE_LOCATION": "location",
    "USER_DATA_TYPE_BANNER": "banner",
    "USER_DATA_TYPE_TWITTER": "twitter",
    "USER_DATA_TYPE_GITHUB": "github",
    "USER_DATA_PRIMARY_ADDRESS_ETHEREUM": "eth_address",
    "USER_DATA_PRIMARY_ADDRESS_SOLANA": "sol_address",
}


class SocialError(RuntimeError):
    """Réponse inattendue d'une API publique."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def farcaster_time(ts: int | float) -> dt.datetime:
    """Convertit un horodatage Farcaster (s depuis le 01/01/2021 UTC) en datetime UTC."""
    return dt.datetime.fromtimestamp(float(ts) + FARCASTER_EPOCH, dt.timezone.utc)


@dataclass(frozen=True)
class ProfileSnapshot:
    """Instantané normalisé d'un profil public.

    ``fields`` contient les clés de :data:`PROFILE_FIELDS` (et quelques extras propres à la
    plateforme, préfixés par rien mais absents du diff par défaut). ``changed_at`` donne, quand la
    plateforme le fournit, la date ISO de dernière modification de chaque champ (ou de ``"*"`` pour
    le profil entier). ``raw`` garde la réponse brute, jamais comparée.
    """

    platform: str
    account: str
    fetched_at: str
    fields: dict[str, Any]
    changed_at: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account": self.account,
            "fetched_at": self.fetched_at,
            "fields": dict(self.fields),
            "changed_at": dict(self.changed_at),
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileSnapshot:
        return cls(
            platform=d["platform"],
            account=d["account"],
            fetched_at=d["fetched_at"],
            fields=dict(d.get("fields", {})),
            changed_at=dict(d.get("changed_at", {})),
            raw=d.get("raw", {}),
        )


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any


def _blank_fields(**overrides: Any) -> dict[str, Any]:
    d: dict[str, Any] = {k: None for k in PROFILE_FIELDS}
    d.update(overrides)
    return d


def normalize_avatar(url: str | None) -> str | None:
    """Rend l'URL d'un avatar comparable entre deux lectures.

    X sert la même image sous plusieurs tailles (``_normal``, ``_400x400``, ``_bigger``…) :
    on retire le suffixe de taille. Les autres plateformes encodent déjà le contenu dans l'URL
    (CID Bluesky, identifiant d'upload Farcaster/Mastodon/Telegram).
    """
    if not url:
        return None
    return re.sub(r"_(normal|bigger|mini|200x200|400x400)(\.[a-zA-Z0-9]+)$", r"\2", url)


# ---------------------------------------------------------------------------
# Parseurs (purs, sans réseau)
# ---------------------------------------------------------------------------


def parse_bluesky_profile(d: dict[str, Any], fetched_at: str | None = None) -> ProfileSnapshot:
    """``app.bsky.actor.getProfile`` (ou un élément de ``getProfiles``)."""
    if "did" not in d:
        raise SocialError(f"réponse Bluesky sans did : {list(d)[:6]}")
    ver = d.get("verification") or {}
    labels = [lab.get("val") for lab in d.get("labels") or [] if isinstance(lab, dict)]
    fields = _blank_fields(
        handle=d.get("handle"),
        display_name=d.get("displayName"),
        bio=d.get("description"),
        avatar=normalize_avatar(d.get("avatar")),
        banner=d.get("banner"),
        pinned=(d.get("pinnedPost") or {}).get("uri") if isinstance(d.get("pinnedPost"), dict) else d.get("pinnedPost"),
        followers=d.get("followersCount"),
        following=d.get("followsCount"),
        posts=d.get("postsCount"),
        verified=ver.get("verifiedStatus") or ver.get("trustedVerifierStatus"),
        status="active",
        labels=sorted(x for x in labels if x),
    )
    changed = {}
    if d.get("indexedAt"):
        changed["*"] = d["indexedAt"]
    return ProfileSnapshot("bluesky", d["did"], fetched_at or utc_now(), fields, changed, d)


def parse_farcaster_user_data(
    messages: Sequence[dict[str, Any]], fid: int, fetched_at: str | None = None
) -> ProfileSnapshot:
    """``/v1/userDataByFid`` : un message par champ, horodaté (dernière modification)."""
    fields = _blank_fields(status="active")
    changed: dict[str, str] = {}
    for m in messages:
        data = m.get("data", {})
        body = data.get("userDataBody") or {}
        name = _FARCASTER_TYPE_TO_FIELD.get(body.get("type", ""), body.get("type", "").lower())
        if not name:
            continue
        value = body.get("value")
        fields[name] = normalize_avatar(value) if name == "avatar" else value
        if data.get("timestamp") is not None:
            changed[name] = farcaster_time(data["timestamp"]).isoformat(timespec="seconds")
    return ProfileSnapshot("farcaster", str(int(fid)), fetched_at or utc_now(), fields, changed, {"messages": list(messages)})


def parse_farcaster_client_user(d: dict[str, Any], fetched_at: str | None = None) -> ProfileSnapshot:
    """``user-by-username`` de l'API du client Farcaster (ex-Warpcast) : valeurs en direct, sans dates."""
    u = d.get("result", {}).get("user", d) if isinstance(d, dict) else d
    if "fid" not in u:
        raise SocialError(f"réponse Farcaster (client) sans fid : {list(u)[:6]}")
    prof = u.get("profile") or {}
    bio = prof.get("bio")
    loc = prof.get("location")
    pfp = u.get("pfp")
    fields = _blank_fields(
        handle=u.get("username"),
        display_name=u.get("displayName"),
        bio=bio.get("text") if isinstance(bio, dict) else bio,
        avatar=normalize_avatar(pfp.get("url") if isinstance(pfp, dict) else pfp),
        banner=prof.get("bannerImageUrl"),
        url=prof.get("url"),
        location=loc.get("description") if isinstance(loc, dict) else loc,
        followers=u.get("followerCount"),
        following=u.get("followingCount"),
        status="active",
    )
    return ProfileSnapshot("farcaster", str(int(u["fid"])), fetched_at or utc_now(), fields, {}, {"client": u})


def merge_farcaster_sources(hub: ProfileSnapshot, client: ProfileSnapshot) -> ProfileSnapshot:
    """Combine le hub (dates de modification, peut être en retard) et l'API du client (valeurs en direct).

    Les valeurs viennent du client. La date de modification du hub n'est gardée que si le hub
    a la même valeur que le client ; sinon le champ a changé après l'instantané du hub et la
    date est marquée ``"> <date du hub>"``.
    """
    if hub.account != client.account:
        raise ValueError("fid différents")
    fields = dict(hub.fields)
    for k, v in client.fields.items():
        if v is not None or k in COUNTER_FIELDS:
            fields[k] = v
    changed: dict[str, str] = {}
    for k, when in hub.changed_at.items():
        if k not in client.fields or client.fields.get(k) is None or hub.fields.get(k) == client.fields.get(k):
            changed[k] = when
        else:
            changed[k] = f"> {when}"
    raw = dict(hub.raw)
    raw.update(client.raw)
    return ProfileSnapshot("farcaster", hub.account, client.fetched_at, fields, changed, raw)


def merge_warpcast_counts(snap: ProfileSnapshot, warpcast_user: dict[str, Any]) -> ProfileSnapshot:
    """Compatibilité : ajoute les compteurs de l'API du client à un instantané de hub."""
    return merge_farcaster_sources(snap, parse_farcaster_client_user(warpcast_user, snap.fetched_at))


def parse_mastodon_account(d: dict[str, Any], fetched_at: str | None = None) -> ProfileSnapshot:
    """``/api/v1/accounts/lookup`` ou ``/api/v1/accounts/:id``. ``note`` est du HTML."""
    if "acct" not in d:
        raise SocialError(f"réponse Mastodon sans acct : {list(d)[:6]}")
    note = html.unescape(re.sub(r"<[^>]+>", "", d.get("note") or ""))
    extra = {f.get("name"): html.unescape(re.sub(r"<[^>]+>", "", f.get("value") or "")) for f in d.get("fields") or []}
    fields = _blank_fields(
        handle=d.get("acct"),
        display_name=d.get("display_name"),
        bio=note,
        avatar=d.get("avatar_static") or d.get("avatar"),
        banner=d.get("header_static") or d.get("header"),
        url=d.get("url"),
        followers=d.get("followers_count"),
        following=d.get("following_count"),
        posts=d.get("statuses_count"),
        verified=any(f.get("verified_at") for f in d.get("fields") or []) or None,
        status="locked" if d.get("locked") else ("suspended" if d.get("suspended") else "active"),
        profile_fields=extra,
    )
    changed = {}
    if d.get("last_status_at"):
        changed["last_status_at"] = str(d["last_status_at"])
    return ProfileSnapshot("mastodon", d.get("acct") or str(d.get("id")), fetched_at or utc_now(), fields, changed, d)


_TG_META = re.compile(r'<meta\s+property="og:(?P<k>title|description|image)"\s+content="(?P<v>[^"]*)"', re.S)
_TG_EXTRA = re.compile(r'tgme_page_extra">\s*([^<]*?)\s*<', re.S)
_TG_TITLE = re.compile(r'tgme_page_title[^>]*>\s*(?:<span[^>]*>)?\s*([^<]*)', re.S)
_TG_NUM = re.compile(r"(\d[\d\s  ]*)\s*(subscribers?|members?|abonn)", re.I)


def parse_telegram_page(page: str, username: str, fetched_at: str | None = None) -> ProfileSnapshot:
    """Page publique ``t.me/<username>`` (canal, groupe, utilisateur ou bot).

    Sans connexion, Telegram sert les balises OpenGraph (titre, description, photo) et, pour un
    canal ou un groupe public, la ligne ``tgme_page_extra`` (« 10 612 280 subscribers »). Une page
    « Telegram: Contact @x » désigne un utilisateur ou un bot : nom et photo seulement.
    """
    meta = {m.group("k"): html.unescape(m.group("v")) for m in _TG_META.finditer(page)}
    title = meta.get("title")
    kind = "channel"
    if title and title.startswith("Telegram: Contact @"):
        # Utilisateur ou bot : Telegram ne publie ni titre, ni description, ni compteur ;
        # seule une éventuelle balise tgme_page_title (rare) donne le nom.
        kind = "user_or_bot"
        t2 = _TG_TITLE.search(page)
        title = html.unescape(t2.group(1).strip()) if t2 and t2.group(1).strip() else None
    extra = _TG_EXTRA.search(page)
    extra_txt = html.unescape(extra.group(1).strip()) if extra else None
    subs = None
    if extra_txt:
        m = _TG_NUM.search(extra_txt)
        if m:
            subs = int(re.sub(r"\D", "", m.group(1)))
    photo = meta.get("image")
    if photo and photo.endswith("t_logo_2x.png"):
        photo = None  # logo par défaut : pas de photo publique
    if not title and "tgme_page" not in page:
        raise SocialError(f"page t.me/{username} sans profil public")
    fields = _blank_fields(
        handle=username,
        display_name=title,
        bio=meta.get("description") or None,
        avatar=photo,
        followers=subs,
        status="active" if (title or extra_txt) else "unknown",
        kind=kind,
        extra=extra_txt,
    )
    return ProfileSnapshot("telegram", username, fetched_at or utc_now(), fields, {}, {"og": meta, "extra": extra_txt})


def parse_x_user(d: dict[str, Any], fetched_at: str | None = None) -> ProfileSnapshot:
    """Objet ``user`` de l'API X v2 (``GET /2/users/by/username/:u?user.fields=…``).

    Accepte la réponse complète (``{"data": {...}}``) ou l'objet ``data`` seul. Les erreurs
    ``{"errors": [{"title": "Not Found Error" | "Forbidden", …}]}`` (compte inexistant ou
    suspendu) deviennent un instantané au statut ``missing`` ou ``suspended``.
    """
    if "data" in d or "errors" in d:
        if not d.get("data"):
            err = (d.get("errors") or [{}])[0]
            title = str(err.get("title", "")).lower()
            status = "suspended" if "forbidden" in title or "suspended" in str(err.get("detail", "")).lower() else "missing"
            acct = str(err.get("value") or err.get("resource_id") or "?")
            return ProfileSnapshot("x", acct, fetched_at or utc_now(), _blank_fields(handle=acct, status=status), {}, d)
        d = d["data"]
    if "id" not in d:
        raise SocialError(f"objet user X sans id : {list(d)[:6]}")
    pm = d.get("public_metrics") or {}
    ent_url = None
    urls = ((d.get("entities") or {}).get("url") or {}).get("urls") or []
    if urls:
        ent_url = urls[0].get("expanded_url") or urls[0].get("url")
    fields = _blank_fields(
        handle=d.get("username"),
        display_name=d.get("name"),
        bio=d.get("description"),
        avatar=normalize_avatar(d.get("profile_image_url")),
        banner=d.get("profile_banner_url"),
        url=ent_url or d.get("url"),
        location=d.get("location"),
        pinned=d.get("pinned_tweet_id"),
        followers=pm.get("followers_count"),
        following=pm.get("following_count"),
        posts=pm.get("tweet_count"),
        verified=d.get("verified_type") if d.get("verified") else (False if d.get("verified") is not None else None),
        status="protected" if d.get("protected") else ("withheld" if d.get("withheld") else "active"),
        created_at=d.get("created_at"),
    )
    return ProfileSnapshot("x", str(d["id"]), fetched_at or utc_now(), fields, {}, d)


# ---------------------------------------------------------------------------
# Différences
# ---------------------------------------------------------------------------


def diff_snapshots(
    old: ProfileSnapshot,
    new: ProfileSnapshot,
    *,
    counters: bool = False,
    fields: Iterable[str] | None = None,
) -> list[FieldChange]:
    """Champs dont la valeur diffère entre deux instantanés du même compte.

    Par défaut on compare :data:`PROFILE_FIELDS` sans les compteurs (``counters=False``), qui
    changent en permanence sur un grand compte. ``fields`` restreint la comparaison.
    """
    if (old.platform, old.account) != (new.platform, new.account):
        raise ValueError("les deux instantanés ne décrivent pas le même compte")
    names = list(fields) if fields is not None else [f for f in PROFILE_FIELDS if counters or f not in COUNTER_FIELDS]
    out: list[FieldChange] = []
    for name in names:
        a, b = old.fields.get(name), new.fields.get(name)
        if a != b:
            out.append(FieldChange(name, a, b))
    return out


def counter_deltas(old: ProfileSnapshot, new: ProfileSnapshot) -> dict[str, int]:
    """Variation des compteurs (abonnés, abonnements, publications) entre deux instantanés."""
    out = {}
    for name in COUNTER_FIELDS:
        a, b = old.fields.get(name), new.fields.get(name)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[name] = int(b - a)
    return out


# ---------------------------------------------------------------------------
# Client lecture seule (session injectable pour les tests)
# ---------------------------------------------------------------------------


class SocialClient:
    """GET publics, sans clé. ``session`` injectable (``requests.Session`` ou faux)."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 15.0,
        bluesky_url: str = BLUESKY_PUBLIC_URL,
        hub_url: str = FARCASTER_HUB_URL,
        warpcast_url: str = WARPCAST_URL,
        telegram_url: str = TELEGRAM_WEB_URL,
        x_url: str = X_API_URL,
        x_bearer_token: str | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.bluesky_url = bluesky_url.rstrip("/")
        self.hub_url = hub_url.rstrip("/")
        self.warpcast_url = warpcast_url.rstrip("/")
        self.telegram_url = telegram_url.rstrip("/")
        self.x_url = x_url.rstrip("/")
        self.x_bearer_token = x_bearer_token

    # -- utilitaires ------------------------------------------------------------------------
    def _get(self, url: str, *, params: dict[str, Any] | list[tuple[str, Any]] | None = None, headers: dict[str, str] | None = None) -> requests.Response:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}
        if headers:
            h.update(headers)
        r = self.session.get(url, params=params, headers=h, timeout=self.timeout)
        return r

    def _json(self, url: str, **kw: Any) -> Any:
        r = self._get(url, **kw)
        if r.status_code != 200:
            raise SocialError(f"HTTP {r.status_code} sur {url} : {r.text[:200]}")
        return r.json()

    # -- Bluesky -----------------------------------------------------------------------------
    @staticmethod
    def _cache_buster() -> list[tuple[str, Any]]:
        """L'AppView public est derrière un CDN (BunnyCDN, ``Cache-Control: public, max-age=30``,
        ``CDN-Cache: HIT`` mesuré le 27/09/2026) : la même URL rend la même réponse pendant 30 s.
        Un paramètre changeant force un ``MISS``."""
        return [("_", time.time_ns())]

    def bluesky(self, actor: str, *, fresh: bool = True) -> ProfileSnapshot:
        params: list[tuple[str, Any]] = [("actor", actor)] + (self._cache_buster() if fresh else [])
        d = self._json(f"{self.bluesky_url}/app.bsky.actor.getProfile", params=params)
        return parse_bluesky_profile(d)

    def bluesky_many(self, actors: Sequence[str], *, fresh: bool = True) -> list[ProfileSnapshot]:
        """``getProfiles`` par lots de 25."""
        out: list[ProfileSnapshot] = []
        for i in range(0, len(actors), 25):
            chunk = list(actors[i : i + 25])
            params: list[tuple[str, Any]] = [("actors", a) for a in chunk] + (self._cache_buster() if fresh else [])
            d = self._json(f"{self.bluesky_url}/app.bsky.actor.getProfiles", params=params)
            now = utc_now()
            out.extend(parse_bluesky_profile(p, now) for p in d.get("profiles", []))
        return out

    def bluesky_profile_record(self, did: str) -> dict[str, Any]:
        """Enregistrement ``app.bsky.actor.profile`` (``uri, cid, value``) : le CID change à chaque modification."""
        return self._json(
            f"{self.bluesky_url}/com.atproto.repo.getRecord",
            params={"repo": did, "collection": "app.bsky.actor.profile", "rkey": "self"},
        )

    def bluesky_handle_history(self, did: str) -> list[tuple[str, list[str]]]:
        """Journal d'audit PLC : ``[(date ISO, alsoKnownAs), …]`` (changements de handle)."""
        log = self._json(f"{PLC_URL}/{did}/log/audit")
        return [(e.get("createdAt", ""), list((e.get("operation") or {}).get("alsoKnownAs") or [])) for e in log]

    # -- Farcaster ---------------------------------------------------------------------------
    def farcaster_fid(self, username: str) -> int:
        """Résout un fname en fid via le hub (``userNameProofByName``)."""
        d = self._json(f"{self.hub_url}/userNameProofByName", params={"name": username})
        try:
            return int(d["fid"])
        except (KeyError, TypeError, ValueError) as e:
            raise SocialError(f"fname inconnu : {username}") from e

    def farcaster_client(self, username: str | None = None, fid: int | None = None) -> ProfileSnapshot:
        """API du client Farcaster (en direct, non documentée) : ``user-by-username`` ou ``user?fid=``."""
        if username:
            d = self._json(f"{self.warpcast_url}/user-by-username", params={"username": username})
        elif fid is not None:
            d = self._json(f"{self.warpcast_url}/user", params={"fid": int(fid)})
        else:
            raise ValueError("fid ou username requis")
        return parse_farcaster_client_user(d)

    def farcaster_hub_lag(self) -> dict[str, Any]:
        """Retard de synchronisation du hub (``/v1/info``) : blocs de retard par shard."""
        info = self._json(f"{self.hub_url}/info")
        shards = {int(s["shardId"]): {"max_height": s.get("maxHeight"), "block_delay": s.get("blockDelay")} for s in info.get("shardInfos", [])}
        return {"version": info.get("version"), "shards": shards, "max_block_delay": max((s.get("block_delay") or 0) for s in shards.values()) if shards else None}

    def farcaster(
        self,
        fid: int | None = None,
        username: str | None = None,
        *,
        source: str = "both",
    ) -> ProfileSnapshot:
        """Instantané Farcaster.

        ``source`` : ``"hub"`` (champs horodatés, mais le hub peut être en retard), ``"client"``
        (valeurs en direct, sans dates) ou ``"both"`` (valeurs du client, dates du hub quand elles
        concordent ; si le hub est injoignable on retombe sur le client, et inversement).
        """
        if fid is None and not username:
            raise ValueError("fid ou username requis")
        hub_snap = client_snap = None
        hub_err = client_err = None
        if source in ("hub", "both"):
            try:
                real_fid = int(fid) if fid is not None else self.farcaster_fid(username or "")
                d = self._json(f"{self.hub_url}/userDataByFid", params={"fid": real_fid})
                hub_snap = parse_farcaster_user_data(d.get("messages", []), real_fid)
            except (SocialError, ValueError) as e:
                hub_err = e
        if source in ("client", "both"):
            try:
                handle = username or (hub_snap.fields.get("handle") if hub_snap else None)
                client_snap = self.farcaster_client(username=handle, fid=None if handle else fid)
            except (SocialError, ValueError) as e:
                client_err = e
        if hub_snap and client_snap:
            return merge_farcaster_sources(hub_snap, client_snap)
        if client_snap:
            return client_snap
        if hub_snap:
            return hub_snap
        raise SocialError(f"Farcaster injoignable : hub={hub_err!r} client={client_err!r}")

    def farcaster_events_tail(self) -> int:
        """Identifiant du dernier événement du hub (pour démarrer un suivi « à partir de maintenant »)."""
        d = self._json(f"{self.hub_url}/events", params={"from_event_id": 0, "pageSize": 1, "reverse": "true"})
        ev = d.get("events") or []
        if not ev:
            raise SocialError("journal d'événements vide")
        return int(ev[0]["id"])

    def farcaster_events(self, from_event_id: int, *, page_size: int = 1000, max_pages: int = 10) -> Iterator[dict[str, Any]]:
        """Événements du hub à partir de ``from_event_id`` (inclus), page par page."""
        next_id = int(from_event_id)
        for _ in range(max_pages):
            d = self._json(f"{self.hub_url}/events", params={"from_event_id": next_id, "pageSize": page_size})
            events = d.get("events") or []
            yield from events
            if len(events) < page_size:
                return
            next_id = int(events[-1]["id"]) + 1

    # -- Mastodon ----------------------------------------------------------------------------
    def mastodon(self, acct: str) -> ProfileSnapshot:
        """``acct`` = ``utilisateur@instance`` (l'instance sert d'hôte)."""
        if "@" not in acct.strip("@"):
            raise ValueError("format attendu : utilisateur@instance")
        user, instance = acct.strip("@").rsplit("@", 1)
        d = self._json(f"https://{instance}/api/v1/accounts/lookup", params={"acct": user})
        snap = parse_mastodon_account(d)
        return ProfileSnapshot(snap.platform, f"{user}@{instance}", snap.fetched_at, snap.fields, snap.changed_at, snap.raw)

    # -- Telegram ----------------------------------------------------------------------------
    def telegram(self, username: str) -> ProfileSnapshot:
        r = self._get(f"{self.telegram_url}/{username.lstrip('@')}", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) " + USER_AGENT})
        if r.status_code != 200:
            raise SocialError(f"HTTP {r.status_code} sur t.me/{username}")
        return parse_telegram_page(r.text, username.lstrip("@"))

    # -- X -----------------------------------------------------------------------------------
    def x_user(self, username: str) -> ProfileSnapshot:
        """Nécessite un jeton Bearer (``x_bearer_token``) : l'API répond 401 sans."""
        if not self.x_bearer_token:
            raise SocialError("jeton X requis (variable X_BEARER_TOKEN)")
        r = self._get(
            f"{self.x_url}/users/by/username/{username.lstrip('@')}",
            params={"user.fields": X_USER_FIELDS},
            headers={"Authorization": f"Bearer {self.x_bearer_token}"},
        )
        if r.status_code in (401, 402, 403, 429):
            raise SocialError(f"HTTP {r.status_code} X : {r.text[:200]}")
        return parse_x_user(r.json())


def farcaster_profile_events(events: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Filtre les événements de hub qui sont des modifications de profil et les aplatit.

    Rend des dicts ``{event_id, fid, field, value, changed_at}``.
    """
    for e in events:
        if e.get("type") != "HUB_EVENT_TYPE_MERGE_MESSAGE":
            continue
        data = (e.get("mergeMessageBody") or {}).get("message", {}).get("data", {})
        if data.get("type") != "MESSAGE_TYPE_USER_DATA_ADD":
            continue
        body = data.get("userDataBody") or {}
        yield {
            "event_id": e.get("id"),
            "fid": data.get("fid"),
            "field": _FARCASTER_TYPE_TO_FIELD.get(body.get("type", ""), body.get("type")),
            "value": body.get("value"),
            "changed_at": farcaster_time(data["timestamp"]).isoformat(timespec="seconds") if data.get("timestamp") is not None else None,
        }
