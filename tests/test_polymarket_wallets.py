"""Tests des wallets Polymarket (transactions à la seconde, rôles, P&L).

Hors-ligne : un faux ``requests.Session`` rejoue des réponses ``data-api /v2/trades``,
``/v2/activity``, ``/v1/leaderboard`` et ``gamma /events`` calquées sur de vraies réponses
(25/09/2026). Les tests ``@pytest.mark.network`` interrogent les vraies API publiques.
"""

from __future__ import annotations

import copy
import json
import threading

import numpy as np
import pandas as pd
import pytest
import requests

from tradebot import polymarket_wallets as pw
from tradebot.polymarket import PolymarketClient, PolymarketError

UTC = "UTC"
CID = "0x8dbd8c6f5c2a38063c0c63e05f34470116231d73897c24b7460ac7f36eb624fb"
TOKEN_UP = "108854038053444245473342097796220527625982576279215379846006425296752166764709"
TOKEN_DOWN = "94086112935155543873623944622982589425841987761157800428904933343235050159168"
_COMMON = {
    "condition_id": CID,
    "title": "Bitcoin Up or Down - September 25, 3:00PM-3:05PM ET",
    "slug": "btc-updown-5m-1790362800",
    "icon": "https://polymarket-upload.s3.us-east-2.amazonaws.com/BTC+fullsize.png",
    "event_slug": "btc-updown-5m-1790362800",
    "bio": "",
    "profile_image": "",
    "profile_image_optimized": "",
}

# Deux transactions réelles de ``/v2/trades?condition=…&taker_only=false`` (btc-updown-5m-1790362800,
# résolu Down), dans l'ordre de la réponse (la ligne preneur est la première de chaque tx).
# 1) « mint » : le preneur achète Down à 0,9897 (VWAP), deux makers achètent Up à 0,01 et 0,02.
# 2) échange direct : le preneur vend Down à 0,99, un maker achète Down à 0,99.
REAL_ROWS = [
    {
        **_COMMON, "proxy_wallet": "0x70cb32438cb539b7cfe560f0ac2f26d506450b27", "side": "SELL",
        "token_id": TOKEN_DOWN, "size": 1.21, "price": 0.99, "timestamp": 1790363189, "outcome": "Down",
        "outcome_index": 1, "name": "", "pseudonym": "",
        "transaction_hash": "0x480c4499e47e60e1582a904bfc8815b24df797b6f7f78c7d7ec6038d955b9341",
        "_taker": True,
    },
    {
        **_COMMON, "proxy_wallet": "0x55aeeb3eb4e8cc0da6d9e4939caf533bf6c3f5df", "side": "BUY",
        "token_id": TOKEN_DOWN, "size": 1.21, "price": 0.99, "timestamp": 1790363189, "outcome": "Down",
        "outcome_index": 1, "name": "animal00", "pseudonym": "Artistic-Design",
        "transaction_hash": "0x480c4499e47e60e1582a904bfc8815b24df797b6f7f78c7d7ec6038d955b9341",
    },
    {
        **_COMMON, "proxy_wallet": "0xe8bbc29a85262636f804a6ddce1c5fafb3c96fee", "side": "BUY",
        "token_id": TOKEN_DOWN, "size": 60.0167, "price": 0.9897234103, "timestamp": 1790363052,
        "outcome": "Down", "outcome_index": 1, "name": "Ozyman", "pseudonym": "Utilized-Fire",
        "transaction_hash": "0xf11bfa03ce915bc0c1f200fc7da168ce9ca6df90931980e6e9eaf351b7355bc1",
        "_taker": True,
    },
    {
        **_COMMON, "proxy_wallet": "0x55aeeb3eb4e8cc0da6d9e4939caf533bf6c3f5df", "side": "BUY",
        "token_id": TOKEN_UP, "size": 58.3567, "price": 0.01, "timestamp": 1790363052, "outcome": "Up",
        "outcome_index": 0, "name": "animal00", "pseudonym": "Artistic-Design",
        "transaction_hash": "0xf11bfa03ce915bc0c1f200fc7da168ce9ca6df90931980e6e9eaf351b7355bc1",
    },
    {
        **_COMMON, "proxy_wallet": "0xaa1a4f31c010d4a63fc49770d4ca2d69ea370602", "side": "BUY",
        "token_id": TOKEN_UP, "size": 1.66, "price": 0.02, "timestamp": 1790363052, "outcome": "Up",
        "outcome_index": 0, "name": "hdueilqhsdn", "pseudonym": "Downright-Scheduling",
        "transaction_hash": "0xf11bfa03ce915bc0c1f200fc7da168ce9ca6df90931980e6e9eaf351b7355bc1",
    },
]


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("pas de JSON")
        return self._payload


def _items(params):
    if params is None:
        return []
    return list(params.items()) if isinstance(params, dict) else list(params)


class FakeSession:
    """Routeur minimal data-api v2 / leaderboard / gamma ; enregistre les appels."""

    def __init__(self, trades=(), events=(), activity=None, leaderboard=None, script=None,
                 fail_trades=0, fail_conditions=()):
        self.fail_trades = fail_trades                # nb de requêtes /v2/trades en échec (503)
        self.fail_conditions = set(fail_conditions)   # conditions toujours en échec (503)
        self.trades = [copy.deepcopy(r) for r in trades]
        self.events = {e["slug"]: e for e in events}
        self.activity = activity or {}          # wallet -> [rows]
        self.leaderboard = leaderboard or []    # rows triées par rang
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def count(self, fragment: str) -> int:
        return sum(fragment in url for url, _ in self.calls)

    @staticmethod
    def _page(rows, d, lim):
        off = int(str(d.get("cursor", "c0"))[1:]) if d.get("cursor") else 0
        page = rows[off : off + lim]
        more = off + lim < len(rows)
        return FakeResponse(
            200,
            {
                "data": page,
                "pagination": {"limit": lim, "offset": 0, "has_more": more, "next_cursor": f"c{off + lim}" if more else None},
            },
        )

    def get(self, url, params=None, timeout=None):
        p = _items(params)
        d = dict(p)
        with self.lock:
            self.calls.append((url, d))
            if self.script:
                item = self.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        if url.endswith("/v2/trades"):
            conds = d["condition"].split(",")
            with self.lock:
                if self.fail_trades > 0 or self.fail_conditions & set(conds):
                    self.fail_trades -= 1
                    return FakeResponse(503, {"error": "unavailable"})
            assert len(conds) <= 20
            lim = int(d["limit"])
            assert 0 < lim <= 1000
            taker_only = str(d.get("taker_only", "true")).lower() != "false"
            rows = [r for r in self.trades if r["condition_id"] in conds and (not taker_only or r.get("_taker"))]
            # ordre de l'API : horodatage décroissant, ordre on-chain conservé dans une seconde
            order = sorted(range(len(rows)), key=lambda i: -rows[i]["timestamp"])
            rows = [_public(rows[i]) for i in order]
            return self._page(rows, d, lim)
        if url.endswith("/v2/activity"):
            rows = self.activity.get(d["user"], [])
            rows = [r for r in rows if int(d["start"]) <= r["timestamp"] <= int(d["end"])]
            return self._page(rows, d, int(d["limit"]))
        if url.endswith("/v1/leaderboard"):
            off, lim = int(d.get("offset", 0)), int(d["limit"])
            assert lim <= 50
            return FakeResponse(200, self.leaderboard[off : off + lim])
        if url.endswith("/events"):
            slugs = [v for k, v in p if k == "slug"]
            limit = int(d.get("limit", 20))
            return FakeResponse(200, [self.events[s] for s in slugs if s in self.events][:limit])
        return FakeResponse(404, {"error": "not found"})


class ExplodingSession:
    def get(self, *a, **k):
        raise AssertionError("appel réseau inattendu")


def client_for(session, tmp_path=None, **kw) -> PolymarketClient:
    kw.setdefault("max_rps", 1000.0)
    kw.setdefault("backoff", 0.0)
    if tmp_path is not None:
        kw.setdefault("cache_dir", tmp_path / "w")
    return PolymarketClient(session=session, **kw)


def ts(s: str) -> int:
    return int(pd.Timestamp(s, tz=UTC).timestamp())


def trade_rows(spec, cid="0xc1", t0=1790000000):
    """Lignes façon /v2/trades : spec = [(tx, wallet, side, outcome, size, price, dt, taker)]."""
    rows = []
    for tx, wallet, side, outcome, size, price, dt, taker in spec:
        rows.append(
            {
                "proxy_wallet": wallet, "side": side, "token_id": f"{cid}-{outcome}", "condition_id": cid,
                "size": size, "price": price, "timestamp": t0 + dt, "outcome": outcome,
                "outcome_index": 0 if outcome == "Up" else 1, "slug": "btc-updown-5m-x", "name": "",
                "pseudonym": "", "transaction_hash": tx, "_taker": taker,
            }
        )
    return rows


def frame(spec, role=True, resolved_up=True, cid="0xc1", start=1790000000):
    """DataFrame de trades construit à la main (colonnes de collect_trades)."""
    rows = []
    for wallet, side, outcome, size, price, r, t_rel in spec:
        rows.append(
            {
                "wallet": wallet, "side": side, "outcome": outcome, "size": float(size), "price": float(price),
                "role": r if role else None, "condition_id": cid, "t_rel_s": t_rel,
                "window_start": pd.Timestamp(start, unit="s", tz=UTC),
                "window_end": pd.Timestamp(start + 300, unit="s", tz=UTC),
                "slug": f"btc-updown-5m-{start}", "asset": "btc", "duration": "5m",
                "resolved_up": resolved_up, "name": "", "pseudonym": "",
            }
        )
    df = pd.DataFrame(rows)
    df["resolved_up"] = df["resolved_up"].astype("boolean")
    return df


# ---------------------------------------------------------------------------
# Parsing et pagination de /v2/trades
# ---------------------------------------------------------------------------
def test_trades_v2_frame_parses_real_rows():
    df = pw.trades_v2_frame([_public(r) for r in REAL_ROWS])
    assert list(df.columns) == pw.TRADE_COLUMNS
    assert len(df) == 5
    assert str(df["ts"].dtype) == "datetime64[ns, UTC]"
    assert df["ts"].iloc[0] == pd.Timestamp("2026-09-25 19:06:29", tz=UTC)
    assert (df["ts"].dt.microsecond == 0).all()
    assert df["outcome"].tolist() == ["Down", "Down", "Down", "Up", "Up"]
    assert df["side"].tolist() == ["SELL", "BUY", "BUY", "BUY", "BUY"]
    assert df["wallet"].iloc[2] == "0xe8bbc29a85262636f804a6ddce1c5fafb3c96fee"
    assert df["name"].iloc[2] == "Ozyman" and df["pseudonym"].iloc[2] == "Utilized-Fire"
    assert df["notional"].iloc[2] == pytest.approx(60.0167 * 0.9897234103)
    assert df["token_id"].iloc[3] == TOKEN_UP and df["condition_id"].iloc[3] == CID
    assert df["role"].isna().all()


def test_trades_v2_frame_empty_and_v1_keys():
    empty = pw.trades_v2_frame([])
    assert list(empty.columns) == pw.TRADE_COLUMNS and len(empty) == 0
    v1 = {
        "proxyWallet": "0xAbC", "side": "buy", "asset": "tok", "conditionId": "0xc", "size": "5",
        "price": "0.5", "timestamp": 1790000000, "outcome": "", "outcomeIndex": 0, "transactionHash": "0xt",
    }
    df = pw.trades_v2_frame([v1])
    assert df.loc[0, "wallet"] == "0xabc" and df.loc[0, "side"] == "BUY"
    assert df.loc[0, "outcome"] == "Up"  # déduit de outcome_index
    assert df.loc[0, "notional"] == pytest.approx(2.5)


def test_fetch_market_trades_paginates_with_cursor(monkeypatch):
    monkeypatch.setattr(pw, "PAGE_LIMIT", 2)
    sess = FakeSession(trades=REAL_ROWS)
    df = pw.fetch_market_trades(client_for(sess), [CID], taker_only=False)
    # 5 lignes en pages de 2 -> 3 requêtes « all » ; 2 lignes preneur -> 1 requête
    calls = [c for u, c in sess.calls if u.endswith("/v2/trades")]
    all_calls = [c for c in calls if c.get("taker_only") == "false"]
    assert len(all_calls) == 3 and len(calls) == 4
    assert "cursor" not in all_calls[0] and all_calls[1]["cursor"] == "c2" and all_calls[2]["cursor"] == "c4"
    assert all(c["limit"] == 2 and c["condition"] == CID for c in calls)
    assert len(df) == 5
    assert df["ts"].is_monotonic_increasing
    # rôles : 2 preneurs (un par tx), 3 makers
    roles = dict(zip(zip(df["tx_hash"], df["wallet"]), df["role"]))
    assert roles[("0x480c4499e47e60e1582a904bfc8815b24df797b6f7f78c7d7ec6038d955b9341",
                  "0x70cb32438cb539b7cfe560f0ac2f26d506450b27")] == "taker"
    assert roles[("0xf11bfa03ce915bc0c1f200fc7da168ce9ca6df90931980e6e9eaf351b7355bc1",
                  "0xe8bbc29a85262636f804a6ddce1c5fafb3c96fee")] == "taker"
    assert (df["role"] == "maker").sum() == 3
    rc = pw.role_consistency(df)
    assert rc == {"n_tx": 2, "share_one_taker": 1.0, "share_balanced": 1.0, "share_role_unknown": 0.0}


def test_fetch_market_trades_taker_only_and_seq():
    sess = FakeSession(trades=REAL_ROWS)
    df = pw.fetch_market_trades(client_for(sess), [CID])
    assert len(df) == 2 and (df["role"] == "taker").all()
    assert sess.count("/v2/trades") == 1
    assert "taker_only" not in sess.calls[0][1]  # défaut de l'API : taker_only=true
    # seq : rang chronologique dans le marché (0 = plus ancienne exécution)
    assert df["seq"].tolist() == [0, 1]
    assert df["ts"].tolist() == sorted(df["ts"].tolist())


def test_fetch_market_trades_batches_of_20_conditions():
    rows = []
    for i in range(25):
        rows += trade_rows([(f"0xtx{i}", f"0xw{i}", "BUY", "Up", 5, 0.5, i, True)], cid=f"0xc{i:02d}")
    sess = FakeSession(trades=rows)
    cids = [f"0xc{i:02d}" for i in range(25)] + ["0xc00"]  # doublon ignoré
    df = pw.fetch_market_trades(sess, cids, max_rps=1000)
    calls = [c for u, c in sess.calls if u.endswith("/v2/trades")]
    assert [len(c["condition"].split(",")) for c in calls] == [20, 5]
    assert len(df) == 25 and df["condition_id"].nunique() == 25


def test_fetch_market_trades_parallel_batches_same_result():
    rows = []
    for i in range(45):
        rows += trade_rows(
            [(f"0xtx{i}", "0xa", "BUY", "Up", 5, 0.5, i, True), (f"0xtx{i}", "0xb", "SELL", "Up", 5, 0.5, i, False)],
            cid=f"0xc{i:02d}",
        )
    cids = [f"0xc{i:02d}" for i in range(45)]
    a = pw.fetch_market_trades(client_for(FakeSession(trades=rows)), cids, taker_only=False)
    b = pw.fetch_market_trades(client_for(FakeSession(trades=rows)), cids, taker_only=False, max_workers=3)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 90 and (a["role"] == "taker").sum() == 45


def test_fetch_retries_on_429_then_succeeds():
    sess = FakeSession(
        trades=REAL_ROWS,
        script=[FakeResponse(429, {"error": "rate limited"}, headers={"Retry-After": "0"}), requests.ConnectionError("x")],
    )
    df = pw.fetch_market_trades(client_for(sess, retries=3), [CID])
    assert len(df) == 2
    assert sess.count("/v2/trades") == 3


def test_fetch_gives_up_after_retries():
    sess = FakeSession(trades=REAL_ROWS, script=[FakeResponse(503, {"e": 1})] * 3)
    with pytest.raises(PolymarketError):
        pw.fetch_market_trades(client_for(sess, retries=2), [CID])


def test_cursor_loop_is_detected():
    loop = FakeResponse(200, {"data": [], "pagination": {"has_more": True, "next_cursor": "same"}})
    sess = FakeSession(script=[loop, loop, loop])
    with pytest.raises(PolymarketError, match="boucle"):
        pw.fetch_market_trades(client_for(sess), [CID])


def test_assign_roles_duplicates_and_unknown():
    rows = trade_rows(
        [
            ("0xt1", "0xa", "BUY", "Up", 10, 0.5, 0, True),
            ("0xt1", "0xm", "SELL", "Up", 5, 0.5, 0, False),
            ("0xt1", "0xm", "SELL", "Up", 5, 0.5, 0, False),  # doublon exact : 2 ordres maker distincts
            ("0xt2", "0xb", "BUY", "Down", 3, 0.4, 5, False),  # tx sans ligne preneur
        ]
    )
    all_df = pw.trades_v2_frame([_public(r) for r in rows])
    taker_df = pw.trades_v2_frame([_public(r) for r in rows if r["_taker"]])
    roles = pw.assign_roles(all_df, taker_df)
    assert list(roles) == ["taker", "maker", "maker", None]
    df = all_df.assign(role=roles)
    rc = pw.role_consistency(df)
    assert rc["n_tx"] == 2 and rc["share_one_taker"] == 0.5 and rc["share_balanced"] == 0.5
    assert rc["share_role_unknown"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# P&L construit à la main
# ---------------------------------------------------------------------------
def test_pnl_buy_up_winner_taker_fee_and_zero_sum():
    # Exemple de docs/research/polymarket.md §2.1 : 100 parts Up à 0,50 -> +48,25 $ si Up gagne.
    df = frame([("0xa", "BUY", "Up", 100, 0.50, "taker", 5), ("0xb", "SELL", "Up", 100, 0.50, "maker", -20)])
    pnl = pw.wallet_market_pnl(df).set_index("wallet")
    a, b = pnl.loc["0xa"], pnl.loc["0xb"]
    assert a["fees"] == pytest.approx(1.75)
    assert a["cash"] == pytest.approx(-50.0) and a["payout"] == pytest.approx(100.0)
    assert a["pnl"] == pytest.approx(48.25)
    assert a["pos_up"] == 100 and a["direction"] == "Up" and a["taker_share"] == 1.0
    # le maker a vendu sans détenir de parts : position négative = SPLIT puis vente
    assert b["fees"] == 0.0 and b["pos_up"] == -100 and b["pnl"] == pytest.approx(-50.0)
    assert b["taker_share"] == 0.0 and b["direction"] == "Down"
    # jeu à somme nulle : gains des wallets + frais = 0
    assert pnl["pnl"].sum() + pnl["fees"].sum() == pytest.approx(0.0)
    # remise maker estimée : 20 % des frais preneur du marché, tout pour l'unique maker
    assert b["rebate_est"] == pytest.approx(0.35) and a["rebate_est"] == 0.0
    assert b["pnl_incl_rebate"] == pytest.approx(-49.65)
    assert a["first_t_rel_s"] == 5 and b["first_t_rel_s"] == -20
    assert bool(a["resolved_up"]) is True and a["slug"] == "btc-updown-5m-1790000000"


def test_pnl_buy_up_loser():
    df = frame([("0xa", "BUY", "Up", 100, 0.50, "taker", 5)], resolved_up=False)
    row = pw.wallet_market_pnl(df).iloc[0]
    assert row["pnl"] == pytest.approx(-51.75) and row["payout"] == 0.0


@pytest.mark.parametrize("resolved_up, expected", [(False, 7.0), (True, -3.0)])
def test_short_sell_equals_split_then_sell(resolved_up, expected):
    # SPLIT 10 $ -> 10 Up + 10 Down ; vente de 10 Up à 0,70 ; on garde 10 Down.
    split_equivalent = -10 + 10 * 0.70 + (0 if resolved_up else 10)
    assert split_equivalent == pytest.approx(expected)
    df = frame([("0xc", "SELL", "Up", 10, 0.70, "maker", 30)], resolved_up=resolved_up)
    row = pw.wallet_market_pnl(df).iloc[0]
    assert row["pos_up"] == -10 and row["cash"] == pytest.approx(7.0)
    assert row["pnl"] == pytest.approx(expected)


def test_short_both_sides_equals_split_and_sell_both():
    # vendre 10 Up à 0,55 et 10 Down à 0,47 sans rien détenir = split puis vente des deux côtés
    df = frame([("0xc", "SELL", "Up", 10, 0.55, "maker", 1), ("0xc", "SELL", "Down", 10, 0.47, "maker", 2)])
    row = pw.wallet_market_pnl(df).iloc[0]
    assert row["pnl"] == pytest.approx(10 * (0.55 + 0.47) - 10)
    assert row["direction"] == "flat" and row["n_maker"] == 2


def test_mint_match_and_fees_only_for_taker():
    # preneur achète Up à 0,60 ; maker achète Down à 0,40 (mint). Up gagne.
    df = frame([("0xd", "BUY", "Up", 10, 0.60, "taker", 0), ("0xe", "BUY", "Down", 10, 0.40, "maker", 0)])
    pnl = pw.wallet_market_pnl(df).set_index("wallet")
    fee = 0.07 * 0.6 * 0.4 * 10
    assert pnl.loc["0xd", "fees"] == pytest.approx(fee)
    assert pnl.loc["0xd", "pnl"] == pytest.approx(-6 - fee + 10)
    assert pnl.loc["0xe", "fees"] == 0.0 and pnl.loc["0xe", "pnl"] == pytest.approx(-4.0)
    assert pnl["pnl"].sum() + pnl["fees"].sum() == pytest.approx(0.0)
    # même exécution en maker : aucun frais
    as_maker = frame([("0xd", "BUY", "Up", 10, 0.60, "maker", 0)])
    assert pw.wallet_market_pnl(as_maker).iloc[0]["fees"] == 0.0


def test_round_trip_two_taker_fees_and_unknown_role():
    df = frame(
        [
            ("0xa", "BUY", "Up", 10, 0.40, "taker", 10),
            ("0xa", "SELL", "Up", 10, 0.60, "taker", 100),
            ("0xa", "BUY", "Down", 1, 0.50, None, 200),
        ],
        resolved_up=True,
    )
    df.loc[2, "role"] = np.nan
    row = pw.wallet_market_pnl(df).iloc[0]
    fees = 2 * round(0.07 * 0.4 * 0.6 * 10, 5)
    assert row["fees"] == pytest.approx(fees)
    assert row["n_role_unknown"] == 1 and row["n_taker"] == 2 and row["n_trades"] == 3
    assert row["pnl"] == pytest.approx(-4 + 6 - 0.5 - fees)
    assert row["pos_up"] == 0 and row["direction"] == "Down"
    assert row["first_t_rel_s"] == 10 and row["last_t_rel_s"] == 200 and row["median_t_rel_s"] == 100
    assert row["volume"] == pytest.approx(4 + 6 + 0.5)


def test_pnl_resolved_mapping_and_unresolved():
    df = frame([("0xa", "BUY", "Up", 10, 0.40, "taker", 0)], resolved_up=None)
    row = pw.wallet_market_pnl(df).iloc[0]
    assert pd.isna(row["resolved_up"]) and np.isnan(row["payout"]) and np.isnan(row["pnl"])
    row = pw.wallet_market_pnl(df, resolved={"0xc1": True}).iloc[0]
    assert bool(row["resolved_up"]) is True and row["payout"] == 10.0
    assert pw.wallet_market_pnl(df.iloc[0:0]).empty


def test_rebate_split_pro_rata_between_makers():
    df = frame(
        [
            ("0xt", "BUY", "Up", 100, 0.50, "taker", 0),
            ("0xm1", "SELL", "Up", 75, 0.50, "maker", 0),
            ("0xm2", "SELL", "Up", 25, 0.50, "maker", 0),
        ]
    )
    pnl = pw.wallet_market_pnl(df).set_index("wallet")
    assert pnl.loc["0xm1", "rebate_est"] == pytest.approx(0.2 * 1.75 * 0.75)
    assert pnl.loc["0xm2", "rebate_est"] == pytest.approx(0.2 * 1.75 * 0.25)
    assert pw.wallet_market_pnl(df, fee_rate=0.0)["fees"].sum() == 0.0


# ---------------------------------------------------------------------------
# collect_trades : marchés gamma + trades + t_rel_s + cache
# ---------------------------------------------------------------------------
S0 = ts("2026-09-24 10:00")


def gamma_event(start: int, up_wins: bool, duration: str = "5m") -> dict:
    dur = {"5m": 300, "15m": 900}[duration]
    slug = f"btc-updown-{duration}-{start}"
    market = {
        "id": str(start),
        "question": "Bitcoin Up or Down - September 24, 6:00AM-6:05AM ET",
        "conditionId": f"0x{start:064x}",
        "slug": slug,
        "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "endDate": pd.Timestamp(start + dur, unit="s", tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eventStartTime": pd.Timestamp(start, unit="s", tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]' if up_wins else '["0", "1"]',
        "clobTokenIds": f'["{start}1", "{start}2"]',
        "volume": "1000", "volumeNum": 1000.0, "active": True, "closed": True, "restricted": True,
        "umaResolutionStatus": "resolved", "automaticallyResolved": True, "acceptingOrders": False,
        "feesEnabled": True, "feeType": "crypto_fees_v2",
        "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
        "cryptoMarketConfig": {"asset": "btc", "duration": duration, "twapEnabled": True, "twapLookbackSeconds": 60},
    }
    return {"slug": slug, "startTime": market["eventStartTime"], "endDate": market["endDate"], "closed": True, "markets": [market]}


def _collect_fixture():
    events = [gamma_event(S0 + 300 * i, up_wins=(i % 2 == 0)) for i in range(12)]
    c0, c1 = f"0x{S0:064x}", f"0x{S0 + 300:064x}"
    rows = trade_rows(
        [
            ("0xt1", "0xa", "BUY", "Up", 10, 0.50, -30, True),     # avant l'ouverture (S − 30 s)
            ("0xt1", "0xm", "SELL", "Up", 10, 0.50, -30, False),
            ("0xt2", "0xa", "BUY", "Up", 4, 0.70, 10, True),
            ("0xt2", "0xm", "BUY", "Down", 4, 0.30, 10, False),    # mint
        ],
        cid=c0,
        t0=S0,
    )
    rows += trade_rows(
        [
            ("0xt3", "0xb", "BUY", "Down", 20, 0.55, 250, True),
            ("0xt3", "0xm", "SELL", "Down", 20, 0.55, 250, False),
        ],
        cid=c1,
        t0=S0 + 300,
    )
    return events, rows, c0, c1


def test_collect_trades_enrichment_and_cache(tmp_path):
    events, rows, c0, c1 = _collect_fixture()
    sess = FakeSession(trades=rows, events=events)
    start, end = pd.Timestamp(S0, unit="s", tz=UTC), pd.Timestamp(S0 + 3600, unit="s", tz=UTC)
    markets, trades = pw.collect_trades(("btc",), ("5m",), start, end, client=sess, cache_dir=tmp_path, max_rps=1000)
    assert len(markets) == 12 and markets["resolved_up"].tolist() == [i % 2 == 0 for i in range(12)]
    assert markets.set_index("condition_id").loc[c0, "n_fills"] == 4
    assert markets.set_index("condition_id").loc[c0, "n_taker_fills"] == 2
    assert markets.set_index("condition_id").loc[c1, "n_wallets"] == 2
    assert len(trades) == 6
    t = trades.set_index("tx_hash")
    first = trades[trades["tx_hash"] == "0xt1"]
    assert (first["t_rel_s"] == -30).all()
    assert (first["window_start"] == pd.Timestamp(S0, unit="s", tz=UTC)).all()
    assert (first["window_end"] == pd.Timestamp(S0 + 300, unit="s", tz=UTC)).all()
    assert t.loc["0xt3", "t_rel_s"].tolist() == [250, 250]
    assert trades.loc[trades["condition_id"] == c1, "resolved_up"].eq(False).all()
    assert trades.loc[trades["condition_id"] == c0, "resolved_up"].eq(True).all()
    assert set(trades["slug"].astype(str)) == {f"btc-updown-5m-{S0}", f"btc-updown-5m-{S0 + 300}"}
    assert (trades["asset"] == "btc").all() and (trades["duration"] == "5m").all()
    assert trades["ts"].is_monotonic_increasing
    # deux passes /v2/trades (taker_only=false puis true) sur un seul lot de 12 conditions
    tr_calls = [c for u, c in sess.calls if u.endswith("/v2/trades")]
    assert len(tr_calls) == 2 and len(tr_calls[0]["condition"].split(",")) == 12
    cache_files = sorted(p.name for p in (tmp_path / "trades").iterdir())
    assert cache_files == ["btc_5m_2026-09-24_all.json", "btc_5m_2026-09-24_all.parquet"]
    meta = json.loads((tmp_path / "trades" / "btc_5m_2026-09-24_all.json").read_text())
    assert len(meta["conditions"]) == 12 and meta["conditions"][c0] == 4

    # P&L de bout en bout : c0 Up gagne, c1 Down gagne
    pnl = pw.wallet_market_pnl(trades)
    a0 = pnl[(pnl["wallet"] == "0xa") & (pnl["condition_id"] == c0)].iloc[0]
    fee = round(0.07 * 0.25 * 10, 5) + round(0.07 * 0.7 * 0.3 * 4, 5)
    assert a0["pnl"] == pytest.approx(-5 - 2.8 - fee + 14)
    assert pnl.groupby("condition_id")[["pnl", "fees"]].sum().sum(axis=1).abs().max() < 1e-9

    # second appel : tout vient du cache (métadonnées gamma et trades)
    markets2, trades2 = pw.collect_trades(
        ("btc",), ("5m",), start, end, client=ExplodingSession(), cache_dir=tmp_path, max_rps=1000
    )
    pd.testing.assert_frame_equal(trades, trades2)
    assert len(markets2) == 12

    # taker_only=True : réutilise le cache complet, sans réseau
    _, takers = pw.collect_trades(
        ("btc",), ("5m",), start, end, client=ExplodingSession(), cache_dir=tmp_path, max_rps=1000, taker_only=True
    )
    assert len(takers) == 3 and (takers["role"] == "taker").all()


def test_collect_trades_partial_cache_fetches_only_missing(tmp_path):
    events, rows, c0, c1 = _collect_fixture()
    s = pd.Timestamp(S0, unit="s", tz=UTC)
    sess = FakeSession(trades=rows, events=events)
    pw.collect_trades(("btc",), ("5m",), s, s + pd.Timedelta("5min"), client=sess, cache_dir=tmp_path, max_rps=1000)
    sess2 = FakeSession(trades=rows, events=events)
    _, trades = pw.collect_trades(
        ("btc",), ("5m",), s, s + pd.Timedelta("10min"), client=sess2, cache_dir=tmp_path, max_rps=1000
    )
    tr_calls = [c for u, c in sess2.calls if u.endswith("/v2/trades")]
    assert all(c["condition"] == c1 for c in tr_calls) and len(tr_calls) == 2
    assert len(trades) == 6
    meta = json.loads((tmp_path / "trades" / "btc_5m_2026-09-24_all.json").read_text())
    assert set(meta["conditions"]) == {c0, c1}


def test_collect_trades_retries_failed_batch(tmp_path):
    events, rows, c0, c1 = _collect_fixture()
    s = pd.Timestamp(S0, unit="s", tz=UTC)
    sess = FakeSession(trades=rows, events=events, fail_trades=1)
    client = client_for(sess, tmp_path, retries=0, cache_dir=tmp_path)
    _, trades = pw.collect_trades(("btc",), ("5m",), s, s + pd.Timedelta("10min"), client=client, cache_dir=tmp_path)
    assert len(trades) == 6
    assert sess.count("/v2/trades") == 3  # 1 échec + 2 passes (all, taker)


def test_collect_trades_resumes_after_persistent_failure(tmp_path):
    # trois créneaux à cheval sur deux jours UTC -> deux fichiers de cache (série, jour)
    s1 = ts("2026-09-24 23:50")
    events = [gamma_event(s1 + 300 * i, up_wins=True) for i in range(3)]
    cids = [f"0x{s1 + 300 * i:064x}" for i in range(3)]
    rows = []
    for i, c in enumerate(cids):
        rows += trade_rows([(f"0xt{i}", "0xa", "BUY", "Up", 5, 0.5, 20, True),
                            (f"0xt{i}", "0xb", "SELL", "Up", 5, 0.5, 20, False)], cid=c, t0=s1 + 300 * i)
    start, end = pd.Timestamp(s1, unit="s", tz=UTC), pd.Timestamp(s1 + 900, unit="s", tz=UTC)
    bad = FakeSession(trades=rows, events=events, fail_conditions={cids[2]})
    with pytest.raises(PolymarketError):
        pw.collect_trades(("btc",), ("5m",), start, end, client=client_for(bad, retries=0, cache_dir=tmp_path),
                          cache_dir=tmp_path, max_workers=1)
    # le jour complet (24/09) est déjà en cache ; la reprise ne télécharge que le 25/09
    assert (tmp_path / "trades" / "btc_5m_2026-09-24_all.parquet").exists()
    good = FakeSession(trades=rows, events=events)
    _, trades = pw.collect_trades(("btc",), ("5m",), start, end, client=client_for(good, cache_dir=tmp_path),
                                  cache_dir=tmp_path)
    assert {c["condition"] for u, c in good.calls if u.endswith("/v2/trades")} == {cids[2]}
    assert len(trades) == 6 and trades["condition_id"].nunique() == 3


def test_collect_trades_no_market(tmp_path):
    s = pd.Timestamp(S0, unit="s", tz=UTC)
    markets, trades = pw.collect_trades(
        ("btc",), ("5m",), s, s + pd.Timedelta("10min"), client=FakeSession(), cache_dir=tmp_path, max_rps=1000
    )
    assert markets.empty and trades.empty
    assert {"t_rel_s", "window_start", "resolved_up", "role"} <= set(trades.columns)


# ---------------------------------------------------------------------------
# Agrégat horaire
# ---------------------------------------------------------------------------
def test_hourly_wallet_table():
    h0 = ts("2026-09-24 10:00")
    parts = [
        frame([("0xa", "BUY", "Up", 10, 0.5, "taker", 10), ("0xb", "SELL", "Up", 10, 0.5, "maker", -5)],
              resolved_up=True, cid="0xm1", start=h0),
        frame([("0xa", "BUY", "Up", 10, 0.5, "taker", 30), ("0xb", "SELL", "Up", 10, 0.5, "maker", 40)],
              resolved_up=False, cid="0xm2", start=h0 + 300),
        frame([("0xa", "BUY", "Down", 10, 0.5, "maker", 100), ("0xb", "SELL", "Down", 10, 0.5, "taker", 100)],
              resolved_up=False, cid="0xm3", start=h0 + 3600),
    ]
    trades = pd.concat(parts, ignore_index=True)
    pnl = pw.wallet_market_pnl(trades)
    hourly = pw.hourly_wallet_table(trades, pnl)
    assert list(hourly.columns) == pw.HOURLY_COLUMNS
    assert len(hourly) == 4
    first = hourly[hourly["hour"] == pd.Timestamp("2026-09-24 10:00", tz=UTC)].set_index("wallet")
    fee = 0.07 * 0.25 * 10
    # 0xa : +5 − frais, puis −5 − frais ; 0xb : −5 puis +5
    assert first.loc["0xa", "pnl"] == pytest.approx(-2 * fee)
    assert first.loc["0xb", "pnl"] == pytest.approx(0.0)
    assert first.loc["0xb", "rank_pnl"] == 1 and first.loc["0xa", "rank_pnl"] == 2
    assert first.loc["0xa", "n_markets"] == 2 and first.loc["0xa", "n_wins"] == 1
    assert first.loc["0xa", "win_rate"] == 0.5
    assert first.loc["0xa", "taker_share"] == 1.0 and first.loc["0xb", "taker_share"] == 0.0
    assert first.loc["0xa", "t_rel_median"] == 20 and first.loc["0xb", "t_rel_median"] == 17.5
    assert first.loc["0xa", "first_t_rel_median"] == 20
    assert first.loc["0xa", "volume"] == pytest.approx(10.0) and first.loc["0xa", "assets"] == "btc"
    second = hourly[hourly["hour"] == pd.Timestamp("2026-09-24 11:00", tz=UTC)].set_index("wallet")
    assert second.loc["0xa", "pnl"] == pytest.approx(5.0) and second.loc["0xa", "rank_pnl"] == 1
    assert pw.hourly_wallet_table(trades, pnl.iloc[0:0]).empty


# ---------------------------------------------------------------------------
# Leaderboard et activité
# ---------------------------------------------------------------------------
LB_ROW = {
    "rank": "1", "proxyWallet": "0x1465b79bff7992bc703e1aafb3683b1089647072", "userName": "JnStrtPrdctnMrkts",
    "xUsername": "", "verifiedBadge": False, "vol": 1000, "pnl": 30296.655391402543, "profileImage": "",
}


def test_fetch_leaderboard_offset_pagination():
    rows = [{**LB_ROW, "rank": str(i + 1), "proxyWallet": f"0x{i:040x}", "pnl": 1000.0 - i} for i in range(120)]
    sess = FakeSession(leaderboard=rows)
    df = pw.fetch_leaderboard("week", "VOL", "crypto", limit=100, client=client_for(sess))
    calls = [c for u, c in sess.calls if u.endswith("/v1/leaderboard")]
    assert [(c["offset"], c["limit"]) for c in calls] == [(0, 50), (50, 50)]
    assert calls[0]["timePeriod"] == "week" and calls[0]["orderBy"] == "VOL" and calls[0]["category"] == "crypto"
    assert list(df.columns) == pw.LEADERBOARD_COLUMNS
    assert len(df) == 100 and df["rank"].tolist() == list(range(1, 101))
    assert (df["period"] == "week").all() and df["pnl"].iloc[0] == 1000.0
    short = pw.fetch_leaderboard(limit=3, client=client_for(FakeSession(leaderboard=[LB_ROW])))
    assert len(short) == 1 and short.loc[0, "user_name"] == "JnStrtPrdctnMrkts"


def test_save_leaderboard_snapshots(tmp_path):
    sess = FakeSession(leaderboard=[LB_ROW])
    paths = pw.save_leaderboard_snapshots(client=client_for(sess), cache_dir=tmp_path)
    assert len(paths) == 4
    assert all(p.name.startswith("leaderboard_crypto_") and p.suffix == ".parquet" for p in paths)
    assert pd.read_parquet(paths[0])["wallet"].iloc[0] == LB_ROW["proxyWallet"]


ACT = [
    # lignes calquées sur /v2/activity (usdc_size des TRADE preneur frais inclus)
    {"proxy_wallet": "0xa", "timestamp": 1790000010, "condition_id": "0xc1", "type": "TRADE", "size": 20.0,
     "usdc_size": 19.44074, "transaction_hash": "0xt1", "price": 0.97, "token_id": "d", "side": "BUY",
     "outcome_index": 1, "slug": "btc-updown-5m-1790000000", "outcome": "Down"},
    {"proxy_wallet": "0xa", "timestamp": 1790000020, "condition_id": "0xc1", "type": "SPLIT", "size": 10.0,
     "usdc_size": 10.0, "transaction_hash": "0xt2", "price": 0.0, "token_id": "", "side": "",
     "outcome_index": 999, "slug": "btc-updown-5m-1790000000", "outcome": ""},
    {"proxy_wallet": "0xa", "timestamp": 1790000030, "condition_id": "0xc1", "type": "TRADE", "size": 10.0,
     "usdc_size": 0.30, "transaction_hash": "0xt3", "price": 0.03, "token_id": "u", "side": "SELL",
     "outcome_index": 0, "slug": "btc-updown-5m-1790000000", "outcome": "Up"},
    {"proxy_wallet": "0xa", "timestamp": 1790000400, "condition_id": "0xc1", "type": "REDEEM", "size": 30.0,
     "usdc_size": 30.0, "transaction_hash": "0xt4", "price": 0.0, "token_id": "d", "side": "",
     "outcome_index": 1, "slug": "btc-updown-5m-1790000000", "outcome": "Down"},
    {"proxy_wallet": "0xa", "timestamp": 1790000500, "condition_id": "0xother", "type": "MERGE", "size": 5.0,
     "usdc_size": 5.0, "transaction_hash": "0xt5", "price": 0.0, "token_id": "", "side": "",
     "outcome_index": 999, "slug": "x", "outcome": ""},
]


def test_enrich_with_activity_and_compare():
    sess = FakeSession(activity={"0xa": ACT})
    import tradebot.polymarket_wallets as mod

    old = mod.ACTIVITY_PAGE_LIMIT
    mod.ACTIVITY_PAGE_LIMIT = 2  # force la pagination par curseur
    try:
        act = pw.enrich_with_activity(["0xA"], 1790000000, 1790001000, client=client_for(sess))
    finally:
        mod.ACTIVITY_PAGE_LIMIT = old
    assert sess.count("/v2/activity") == 3
    row = act.set_index("condition_id").loc["0xc1"]
    assert row["n_trade"] == 2 and row["n_split"] == 1 and row["n_redeem"] == 1
    assert row["trade_buy_usdc"] == pytest.approx(19.44074) and row["trade_sell_usdc"] == pytest.approx(0.30)
    assert row["activity_cash"] == pytest.approx(0.30 - 19.44074 - 10 + 30)
    assert act.set_index("condition_id").loc["0xother", "merge_usdc"] == 5.0
    only = pw.enrich_with_activity(["0xa"], 1790000000, 1790001000, client=client_for(FakeSession(activity={"0xa": ACT})),
                                   condition_ids=["0xc1"])
    assert only["condition_id"].tolist() == ["0xc1"]

    # mêmes opérations vues par les trades : achat preneur 20 Down à 0,97, vente maker 10 Up à 0,03
    # (issus du split) ; Down gagne. P&L = −19,40 − frais + 0,30 + 20 + 10 − 10 (split) = activité.
    trades = frame([("0xa", "BUY", "Down", 20, 0.97, "taker", 10), ("0xa", "SELL", "Up", 10, 0.03, "maker", 30)],
                   resolved_up=False)
    pnl = pw.wallet_market_pnl(trades)
    cmp = pw.compare_with_activity(pnl, act)
    assert len(cmp) == 1 and bool(cmp["settled"].iloc[0]) and bool(cmp["match"].iloc[0])
    assert cmp["diff"].iloc[0] == pytest.approx(0.0, abs=1e-6)


def test_concat_keeps_categories():
    a = pd.DataFrame({"w": pd.Categorical(["x", "y"]), "v": [1, 2]})
    b = pd.DataFrame({"w": pd.Categorical(["z"]), "v": [3]})
    c = pd.DataFrame({"w": pd.Categorical([None]), "v": [4]})
    out = pw._concat([a, b, c])
    assert isinstance(out["w"].dtype, pd.CategoricalDtype)
    assert out["w"].tolist()[:3] == ["x", "y", "z"] and pd.isna(out["w"].iloc[3])
    assert out["v"].tolist() == [1, 2, 3, 4]


def test_module_is_read_only():
    import re

    src = open(pw.__file__, encoding="utf-8").read()
    for pattern in (r"\.post\(", r"private_key", r"create_order", r"\bsign\(", r"\.put\(", r"\.delete\("):
        assert not re.search(pattern, src), pattern


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------
@pytest.mark.network
def test_network_roles_on_real_market(tmp_path):
    client = PolymarketClient(cache_dir=tmp_path, max_rps=8)
    end = pd.Timestamp.now(tz=UTC).floor("h") - pd.Timedelta(hours=2)
    markets = client.list_updown_markets("btc", "5m", end - pd.Timedelta("10min"), end, only_closed=True)
    assert markets
    df = pw.fetch_market_trades(client, [markets[0].condition_id], taker_only=False)
    assert len(df) > 100
    rc = pw.role_consistency(df)
    assert rc["share_one_taker"] == 1.0 and rc["share_balanced"] > 0.99
    assert set(df["outcome"].dropna()) <= {"Up", "Down"}


@pytest.mark.network
def test_network_leaderboard():
    df = pw.fetch_leaderboard("day", "PNL", "crypto", limit=60)
    assert len(df) == 60 and df["rank"].is_monotonic_increasing
    assert df["wallet"].str.startswith("0x").all()
