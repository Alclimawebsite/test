"""Tests du module ``social_profiles`` (profils publics, lecture seule).

Hors-ligne : les fixtures sont calquées sur de vraies réponses du 27/09/2026 (Bluesky
``getProfile``, hub Farcaster ``userDataByFid``, API du client Farcaster, Mastodon, page t.me).
Les tests ``@pytest.mark.network`` interrogent les vraies API publiques.
"""

from __future__ import annotations

import json

import pytest

from tradebot import social_profiles as sp

# ---------------------------------------------------------------------------
# Fixtures (réponses réelles, abrégées)
# ---------------------------------------------------------------------------

BSKY = {
    "did": "did:plc:z72i7hdynmk6r22z27h6tvur",
    "handle": "bsky.app",
    "displayName": "Bluesky",
    "avatar": "https://cdn.bsky.app/img/avatar/plain/did:plc:z72i7hdynmk6r22z27h6tvur/bafkreihwihm6kpd6zu@jpeg",
    "banner": "https://cdn.bsky.app/img/banner/plain/did:plc:z72i7hdynmk6r22z27h6tvur/bafkreichzyovokfzmy@jpeg",
    "description": "official Bluesky account (check username👆)\n\nBugs: support@bsky.app",
    "followersCount": 35062437,
    "followsCount": 15,
    "postsCount": 864,
    "indexedAt": "2025-10-27T21:05:26.152Z",
    "createdAt": "2023-04-12T04:53:57.057Z",
    "pinnedPost": {"uri": "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3l6oveex3ii2l", "cid": "bafy"},
    "labels": [],
    "verification": {"verifications": [], "verifiedStatus": "none", "trustedVerifierStatus": "valid"},
}

FC_MESSAGES = [
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 84041570, "userDataBody": {"type": "USER_DATA_TYPE_DISPLAY", "value": "Dan Romero"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 138592020, "userDataBody": {"type": "USER_DATA_TYPE_BIO", "value": "Working on Farcaster"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 141496553, "userDataBody": {"type": "USER_DATA_TYPE_PFP", "value": "https://imagedelivery.net/x/bc698287/orig"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 141496553, "userDataBody": {"type": "USER_DATA_TYPE_URL", "value": "https://danromero.org"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 149203288, "userDataBody": {"type": "USER_DATA_TYPE_USERNAME", "value": "dwr"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 149208515, "userDataBody": {"type": "USER_DATA_PRIMARY_ADDRESS_ETHEREUM", "value": "0x6Ce0"}}},
    {"data": {"type": "MESSAGE_TYPE_USER_DATA_ADD", "fid": 3, "timestamp": 154566636, "userDataBody": {"type": "USER_DATA_TYPE_LOCATION", "value": ""}}},
]

FC_CLIENT = {
    "result": {
        "user": {
            "fid": 3,
            "username": "dwr",
            "displayName": "Dan Romero",
            "pfp": {"url": "https://imagedelivery.net/x/bc698287/orig", "verified": False},
            "profile": {
                "bio": {"text": "Interested in technology and other stuff.", "mentions": []},
                "location": {"placeId": "", "description": ""},
                "url": "https://danromero.org",
            },
            "followerCount": 121227,
            "followingCount": 77,
        }
    }
}

MASTO = {
    "id": "1",
    "username": "Gargron",
    "acct": "Gargron",
    "display_name": "Eugen Rochko",
    "locked": False,
    "created_at": "2016-03-16T00:00:00.000Z",
    "note": "<p>Founder of <span class=\"h-card\">@<span>Mastodon</span></span>. Film &amp; photography.</p>",
    "url": "https://mastodon.social/@Gargron",
    "avatar_static": "https://files.mastodon.social/accounts/avatars/000/000/001/original/6b2384b33799a0dd.png",
    "header_static": "https://files.mastodon.social/accounts/headers/000/000/001/original/d13e4417706a5fec.jpg",
    "followers_count": 382847,
    "following_count": 743,
    "statuses_count": 82331,
    "last_status_at": "2026-09-27",
    "fields": [{"name": "Patreon", "value": "<a href=\"https://patreon.com/mastodon\">patreon.com/mastodon</a>", "verified_at": "2024-01-01T00:00:00.000+00:00"}],
}

TME_CHANNEL = """<html><head>
<meta property="og:title" content="Pavel Durov">
<meta property="og:image" content="https://cdn4.telesco.pe/file/AbC.jpg">
<meta property="og:site_name" content="Telegram">
<meta property="og:description" content="Founder of Telegram.">
</head><body><div class="tgme_page">
<div class="tgme_page_title" dir="auto"><span dir="auto">Pavel Durov</span></div>
<div class="tgme_page_extra">10 612 280 subscribers</div>
<div class="tgme_page_description" dir="auto">Founder of Telegram.</div>
</div></body></html>"""

TME_CONTACT = """<html><head>
<meta property="og:title" content="Telegram: Contact @binance">
<meta property="og:image" content="https://telegram.org/img/t_logo_2x.png">
<meta property="og:description" content="">
</head><body><div class="tgme_page_wrap"><div class="tgme_page">
<div class="tgme_page_icon"></div>
<div class="tgme_page_description">If you have Telegram, you can contact <a href="tg://resolve?domain=binance">@binance</a> right away.</div>
<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve?domain=binance">Send Message</a></div>
</div></div></body></html>"""

X_USER = {
    "data": {
        "id": "44196397",
        "name": "Elon Musk",
        "username": "elonmusk",
        "description": "",
        "profile_image_url": "https://pbs.twimg.com/profile_images/1/abc_normal.jpg",
        "profile_banner_url": "https://pbs.twimg.com/profile_banners/44196397/1690621312",
        "location": "",
        "pinned_tweet_id": "1234",
        "protected": False,
        "verified": True,
        "verified_type": "blue",
        "created_at": "2009-06-02T20:12:29.000Z",
        "url": "https://t.co/xyz",
        "entities": {"url": {"urls": [{"url": "https://t.co/xyz", "expanded_url": "https://x.com/elonmusk"}]}},
        "public_metrics": {"followers_count": 200000000, "following_count": 1000, "tweet_count": 50000, "listed_count": 1},
    }
}

X_SUSPENDED = {"errors": [{"value": "someone", "detail": "User has been suspended: [someone].", "title": "Forbidden", "resource_type": "user", "parameter": "username", "resource_id": "someone", "type": "https://api.twitter.com/2/problems/resource-not-found"}]}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Rejoue des réponses par (chemin, paramètres) ; journalise les appels."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        for matcher, resp in self.routes:
            if matcher(url, params):
                return resp() if callable(resp) else resp
        return FakeResponse(404, {"error": "no route"}, text="no route")


# ---------------------------------------------------------------------------
# Parseurs
# ---------------------------------------------------------------------------


def test_parse_bluesky_profile_fields_and_indexed_at():
    snap = sp.parse_bluesky_profile(BSKY, fetched_at="2026-09-27T16:00:00+00:00")
    assert snap.platform == "bluesky" and snap.account == BSKY["did"]
    f = snap.fields
    assert f["handle"] == "bsky.app" and f["display_name"] == "Bluesky"
    assert f["bio"].startswith("official Bluesky account")
    assert f["avatar"].endswith("bafkreihwihm6kpd6zu@jpeg")
    assert f["pinned"].endswith("3l6oveex3ii2l")
    assert (f["followers"], f["following"], f["posts"]) == (35062437, 15, 864)
    assert f["verified"] == "none" and f["status"] == "active"
    assert snap.changed_at == {"*": "2025-10-27T21:05:26.152Z"}
    assert set(sp.PROFILE_FIELDS) <= set(f)


def test_parse_bluesky_requires_did():
    with pytest.raises(sp.SocialError):
        sp.parse_bluesky_profile({"handle": "x"})


def test_farcaster_time_epoch():
    assert sp.farcaster_time(0).isoformat() == "2021-01-01T00:00:00+00:00"
    assert sp.farcaster_time(84041570).date().isoformat() == "2023-08-31"


def test_parse_farcaster_user_data_per_field_timestamps():
    snap = sp.parse_farcaster_user_data(FC_MESSAGES, fid=3, fetched_at="2026-09-27T16:00:00+00:00")
    assert snap.platform == "farcaster" and snap.account == "3"
    assert snap.fields["display_name"] == "Dan Romero"
    assert snap.fields["bio"] == "Working on Farcaster"
    assert snap.fields["handle"] == "dwr"
    assert snap.fields["eth_address"] == "0x6Ce0"
    assert snap.fields["location"] == ""
    assert snap.fields["followers"] is None  # le hub n'a pas de compteurs
    assert snap.changed_at["display_name"].startswith("2023-08-31")
    assert snap.changed_at["bio"].startswith("2025-05-24")
    assert snap.changed_at["handle"].startswith("2025-09-23")


def test_parse_farcaster_client_user():
    snap = sp.parse_farcaster_client_user(FC_CLIENT, fetched_at="t")
    assert snap.account == "3" and snap.fields["handle"] == "dwr"
    assert snap.fields["bio"] == "Interested in technology and other stuff."
    assert snap.fields["followers"] == 121227 and snap.fields["following"] == 77
    assert snap.fields["url"] == "https://danromero.org"
    assert snap.changed_at == {}


def test_merge_farcaster_sources_keeps_dates_only_when_values_agree():
    hub = sp.parse_farcaster_user_data(FC_MESSAGES, fid=3, fetched_at="t0")
    client = sp.parse_farcaster_client_user(FC_CLIENT, fetched_at="t1")
    merged = sp.merge_farcaster_sources(hub, client)
    assert merged.fetched_at == "t1"
    # valeur en direct du client, date du hub marquée « > » car la bio a changé depuis
    assert merged.fields["bio"] == "Interested in technology and other stuff."
    assert merged.changed_at["bio"].startswith("> 2025-05-24")
    # valeurs identiques : la date du hub est conservée telle quelle
    assert merged.changed_at["display_name"].startswith("2023-08-31")
    assert merged.changed_at["avatar"].startswith("2025-06-26")
    # champs que le client ne renvoie pas : gardés du hub
    assert merged.fields["eth_address"] == "0x6Ce0" and merged.changed_at["eth_address"].startswith("2025-09-23")
    assert merged.fields["followers"] == 121227
    assert "client" in merged.raw and "messages" in merged.raw
    with pytest.raises(ValueError):
        sp.merge_farcaster_sources(hub, sp.ProfileSnapshot("farcaster", "4", "t", {}))


def test_merge_warpcast_counts_compat():
    hub = sp.parse_farcaster_user_data(FC_MESSAGES, fid=3, fetched_at="t0")
    merged = sp.merge_warpcast_counts(hub, FC_CLIENT)
    assert merged.fields["followers"] == 121227


def test_parse_mastodon_strips_html_and_flags_verified():
    snap = sp.parse_mastodon_account(MASTO, fetched_at="t")
    assert snap.account == "Gargron" and snap.fields["display_name"] == "Eugen Rochko"
    assert snap.fields["bio"] == "Founder of @Mastodon. Film & photography."
    assert snap.fields["profile_fields"] == {"Patreon": "patreon.com/mastodon"}
    assert snap.fields["verified"] is True and snap.fields["status"] == "active"
    assert snap.fields["avatar"].endswith("6b2384b33799a0dd.png")
    assert snap.changed_at == {"last_status_at": "2026-09-27"}


def test_parse_telegram_channel_page():
    snap = sp.parse_telegram_page(TME_CHANNEL, "durov", fetched_at="t")
    assert snap.fields["display_name"] == "Pavel Durov"
    assert snap.fields["bio"] == "Founder of Telegram."
    assert snap.fields["followers"] == 10612280
    assert snap.fields["avatar"] == "https://cdn4.telesco.pe/file/AbC.jpg"
    assert snap.fields["kind"] == "channel" and snap.fields["status"] == "active"


def test_parse_telegram_contact_page_exposes_nothing():
    snap = sp.parse_telegram_page(TME_CONTACT, "binance", fetched_at="t")
    assert snap.fields["kind"] == "user_or_bot"
    assert snap.fields["display_name"] is None  # pas de titre public pour un utilisateur / bot
    assert snap.fields["followers"] is None
    assert snap.fields["avatar"] is None  # logo par défaut ignoré
    assert snap.fields["bio"] is None
    assert snap.fields["status"] == "unknown"


def test_parse_telegram_rejects_non_profile_page():
    with pytest.raises(sp.SocialError):
        sp.parse_telegram_page("<html><body>nothing</body></html>", "x")


def test_parse_x_user_documented_shape():
    snap = sp.parse_x_user(X_USER, fetched_at="t")
    assert snap.platform == "x" and snap.account == "44196397"
    f = snap.fields
    assert f["handle"] == "elonmusk" and f["display_name"] == "Elon Musk"
    assert f["avatar"] == "https://pbs.twimg.com/profile_images/1/abc.jpg"  # suffixe _normal retiré
    assert f["banner"].endswith("/1690621312")
    assert f["url"] == "https://x.com/elonmusk"
    assert f["pinned"] == "1234"
    assert (f["followers"], f["following"], f["posts"]) == (200000000, 1000, 50000)
    assert f["verified"] == "blue" and f["status"] == "active"
    assert snap.changed_at == {}  # X ne donne aucune date de modification
    # l'objet ``data`` seul est accepté aussi
    assert sp.parse_x_user(X_USER["data"], fetched_at="t").account == "44196397"


def test_parse_x_user_error_becomes_status():
    snap = sp.parse_x_user(X_SUSPENDED, fetched_at="t")
    assert snap.fields["status"] == "suspended" and snap.account == "someone"
    snap2 = sp.parse_x_user({"errors": [{"title": "Not Found Error", "value": "nobody"}]}, fetched_at="t")
    assert snap2.fields["status"] == "missing"


def test_normalize_avatar():
    assert sp.normalize_avatar("https://pbs.twimg.com/a/b_400x400.png") == "https://pbs.twimg.com/a/b.png"
    assert sp.normalize_avatar("https://pbs.twimg.com/a/b_normal.jpg") == "https://pbs.twimg.com/a/b.jpg"
    assert sp.normalize_avatar("https://cdn.bsky.app/img/avatar/plain/did/bafk@jpeg") == "https://cdn.bsky.app/img/avatar/plain/did/bafk@jpeg"
    assert sp.normalize_avatar(None) is None


# ---------------------------------------------------------------------------
# Différences
# ---------------------------------------------------------------------------


def test_diff_snapshots_ignores_counters_by_default():
    a = sp.parse_bluesky_profile(BSKY, fetched_at="t0")
    b_raw = dict(BSKY, displayName="Bluesky 🦋", followersCount=35062999, avatar=BSKY["avatar"].replace("bafkreihw", "bafkreiZZ"))
    b = sp.parse_bluesky_profile(b_raw, fetched_at="t1")
    changes = sp.diff_snapshots(a, b)
    assert {(c.field, c.old, c.new) for c in changes} == {
        ("display_name", "Bluesky", "Bluesky 🦋"),
        ("avatar", a.fields["avatar"], b.fields["avatar"]),
    }
    assert sp.counter_deltas(a, b) == {"followers": 562, "following": 0, "posts": 0}
    with_counters = sp.diff_snapshots(a, b, counters=True)
    assert any(c.field == "followers" for c in with_counters)
    assert sp.diff_snapshots(a, b, fields=["bio"]) == []


def test_diff_snapshots_refuses_different_accounts():
    a = sp.parse_bluesky_profile(BSKY, fetched_at="t0")
    b = sp.parse_bluesky_profile(dict(BSKY, did="did:plc:other"), fetched_at="t1")
    with pytest.raises(ValueError):
        sp.diff_snapshots(a, b)


def test_snapshot_roundtrip_dict():
    a = sp.parse_farcaster_user_data(FC_MESSAGES, fid=3, fetched_at="t0")
    d = json.loads(json.dumps(a.to_dict()))
    b = sp.ProfileSnapshot.from_dict(d)
    assert b == a and b.changed_at == a.changed_at


# ---------------------------------------------------------------------------
# Client (session factice)
# ---------------------------------------------------------------------------


def _route(path, **params):
    def m(url, p):
        if not url.endswith(path):
            return False
        pd = dict(p) if isinstance(p, dict) else dict(p or [])
        return all(str(pd.get(k)) == str(v) for k, v in params.items())

    return m


def test_client_farcaster_both_sources():
    sess = FakeSession(
        [
            (_route("/userNameProofByName", name="dwr"), FakeResponse(200, {"fid": 3, "name": "dwr"})),
            (_route("/userDataByFid", fid=3), FakeResponse(200, {"messages": FC_MESSAGES})),
            (_route("/user-by-username", username="dwr"), FakeResponse(200, FC_CLIENT)),
        ]
    )
    c = sp.SocialClient(session=sess)
    snap = c.farcaster(username="dwr")
    assert snap.fields["bio"] == "Interested in technology and other stuff."
    assert snap.fields["followers"] == 121227
    assert snap.changed_at["display_name"].startswith("2023-08-31")
    assert len(sess.calls) == 3
    # hub seul
    hub_only = c.farcaster(fid=3, source="hub")
    assert hub_only.fields["bio"] == "Working on Farcaster" and hub_only.fields["followers"] is None
    # client seul
    client_only = c.farcaster(username="dwr", source="client")
    assert client_only.changed_at == {}


def test_client_farcaster_falls_back_when_hub_down():
    sess = FakeSession(
        [
            (_route("/userNameProofByName", name="dwr"), FakeResponse(503, None, text="down")),
            (_route("/user-by-username", username="dwr"), FakeResponse(200, FC_CLIENT)),
        ]
    )
    snap = sp.SocialClient(session=sess).farcaster(username="dwr")
    assert snap.fields["followers"] == 121227 and snap.changed_at == {}
    sess2 = FakeSession([])
    with pytest.raises(sp.SocialError):
        sp.SocialClient(session=sess2).farcaster(username="dwr")


def test_client_farcaster_hub_lag():
    info = {"version": "0.14.2", "shardInfos": [{"shardId": 0, "maxHeight": 10, "blockDelay": 5}, {"shardId": 1, "maxHeight": 7, "blockDelay": 25469469}]}
    sess = FakeSession([(_route("/info"), FakeResponse(200, info))])
    lag = sp.SocialClient(session=sess).farcaster_hub_lag()
    assert lag["max_block_delay"] == 25469469 and lag["shards"][1]["max_height"] == 7


def test_client_farcaster_events_pagination_and_profile_filter():
    def ev(i, typ, value="v"):
        return {
            "type": "HUB_EVENT_TYPE_MERGE_MESSAGE",
            "id": i,
            "mergeMessageBody": {"message": {"data": {"type": typ, "fid": 42, "timestamp": 180000000, "userDataBody": {"type": "USER_DATA_TYPE_BIO", "value": value}}}},
        }

    page1 = [ev(i, "MESSAGE_TYPE_REACTION_ADD") for i in range(1, 3)] + [ev(3, "MESSAGE_TYPE_USER_DATA_ADD", "new bio")]
    page2 = [{"type": "HUB_EVENT_TYPE_PRUNE_MESSAGE", "id": 4}]
    sess = FakeSession(
        [
            (_route("/events", from_event_id=0, pageSize=1, reverse="true"), FakeResponse(200, {"events": [{"id": 99, "type": "HUB_EVENT_TYPE_BLOCK_CONFIRMED"}]})),
            (_route("/events", from_event_id=1, pageSize=3), FakeResponse(200, {"events": page1})),
            (_route("/events", from_event_id=4, pageSize=3), FakeResponse(200, {"events": page2})),
        ]
    )
    c = sp.SocialClient(session=sess)
    assert c.farcaster_events_tail() == 99
    events = list(c.farcaster_events(1, page_size=3))
    assert [e["id"] for e in events] == [1, 2, 3, 4]
    prof = list(sp.farcaster_profile_events(events))
    assert prof == [{"event_id": 3, "fid": 42, "field": "bio", "value": "new bio", "changed_at": sp.farcaster_time(180000000).isoformat(timespec="seconds")}]


def test_client_bluesky_many_chunks_of_25():
    actors = [f"user{i}.bsky.social" for i in range(26)]
    seen = []

    def resp():
        params = dict(seen[-1]) if False else None  # noqa: F841 - lisibilité
        return FakeResponse(200, {"profiles": [dict(BSKY, did=f"did:plc:{i}", handle=a) for i, a in enumerate(current)]})

    current: list[str] = []

    class Sess(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            current[:] = [v for k, v in params if k == "actors"]
            seen.append(len(current))
            return resp()

    c = sp.SocialClient(session=Sess([]))
    out = c.bluesky_many(actors)
    assert seen == [25, 1] and len(out) == 26 and out[-1].fields["handle"] == "user25.bsky.social"


def test_client_mastodon_requires_instance_and_builds_url():
    sess = FakeSession([(lambda u, p: u == "https://mastodon.social/api/v1/accounts/lookup" and p == {"acct": "Gargron"}, FakeResponse(200, MASTO))])
    c = sp.SocialClient(session=sess)
    snap = c.mastodon("Gargron@mastodon.social")
    assert snap.account == "Gargron@mastodon.social" and snap.fields["followers"] == 382847
    with pytest.raises(ValueError):
        c.mastodon("Gargron")


def test_client_telegram_and_x_paths():
    sess = FakeSession(
        [
            (lambda u, p: u == "https://t.me/durov", FakeResponse(200, None, text=TME_CHANNEL)),
            (lambda u, p: u.endswith("/users/by/username/elonmusk"), FakeResponse(200, X_USER)),
        ]
    )
    c = sp.SocialClient(session=sess)
    assert c.telegram("@durov").fields["followers"] == 10612280
    with pytest.raises(sp.SocialError):
        c.x_user("elonmusk")  # pas de jeton
    c2 = sp.SocialClient(session=sess, x_bearer_token="abc")
    assert c2.x_user("elonmusk").fields["handle"] == "elonmusk"
    url, params = sess.calls[-1]
    assert params["user.fields"] == sp.X_USER_FIELDS


def test_client_bluesky_adds_cache_buster_by_default():
    sess = FakeSession([(lambda u, p: u.endswith("/app.bsky.actor.getProfile"), FakeResponse(200, BSKY))])
    c = sp.SocialClient(session=sess)
    c.bluesky("bsky.app")
    keys = [k for k, _ in sess.calls[-1][1]]
    assert keys == ["actor", "_"]
    c.bluesky("bsky.app", fresh=False)
    assert [k for k, _ in sess.calls[-1][1]] == ["actor"]


def test_client_http_error_raises():
    sess = FakeSession([(lambda u, p: True, FakeResponse(429, None, text="slow down"))])
    with pytest.raises(sp.SocialError):
        sp.SocialClient(session=sess).bluesky("bsky.app")


# ---------------------------------------------------------------------------
# Réseau (API publiques réelles)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_network_bluesky_public_appview():
    snap = sp.SocialClient().bluesky("bsky.app")
    assert snap.account.startswith("did:plc:") and snap.fields["handle"] == "bsky.app"
    assert snap.fields["followers"] > 1_000_000 and "*" in snap.changed_at


@pytest.mark.network
def test_network_farcaster_fid_3():
    snap = sp.SocialClient().farcaster(fid=3)
    assert snap.fields["handle"] == "dwr" and snap.changed_at.get("display_name")
