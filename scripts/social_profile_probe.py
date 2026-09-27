#!/usr/bin/env python
"""Can we see social media profile changes, and how fast? Read-only feasibility probe.

Scope: PUBLIC accounts only (exchanges, projects, founders, influencers). Nothing here logs in,
uses a key (except an optional X bearer token you provide) or writes anywhere but ``--out``.
Findings are written up in French in ``docs/research/reseaux_sociaux.md``.

Sub-commands
------------
``snapshot``
    Fetch one normalised snapshot per target and write ``<platform>_<account>.json`` to ``--out``.
    Targets: ``--bluesky handle``, ``--farcaster fid|fname``, ``--mastodon user@instance``,
    ``--telegram channel``, ``--x username`` (needs ``X_BEARER_TOKEN``). Repeatable.
``diff OLD.json NEW.json``
    Print the fields that changed between two snapshots of the same account (counters apart).
``watch``
    Poll the same targets every ``--interval`` seconds for ``--minutes``; append every change to
    ``changes.csv`` and keep the latest snapshot per target. This is the poll-and-compare tracker
    that every closed platform (X, Instagram, TikTok, YouTube, Telegram…) forces on us.
``stream``
    Sample the PUSH sources for ``--seconds``: Bluesky Jetstream (collection
    ``app.bsky.actor.profile`` + identity/account events), the Farcaster hub event log (per shard,
    ``MESSAGE_TYPE_USER_DATA_ADD``), optionally Nostr ``kind 0`` from a public relay. Counts
    events per minute, and for Bluesky measures how long the public AppView takes to reflect a
    profile commit seen on the firehose (``--confirm N`` commits). Writes ``stream_summary.json``
    and per-source CSVs.

Usage::

    . .venv/bin/activate
    python scripts/social_profile_probe.py snapshot --bluesky bsky.app --farcaster dwr \
        --mastodon Gargron@mastodon.social --telegram durov --out /tmp/social
    python scripts/social_profile_probe.py watch --bluesky bsky.app --farcaster 3 --interval 30 --minutes 5 --out /tmp/social
    python scripts/social_profile_probe.py stream --seconds 120 --confirm 10 --out /tmp/social
    python scripts/social_profile_probe.py diff /tmp/social/bluesky_did:plc:x.json /tmp/social/new.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot import social_profiles as sp  # noqa: E402

NOSTR_RELAYS = ("wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _client(args: argparse.Namespace) -> sp.SocialClient:
    return sp.SocialClient(hub_url=args.hub, x_bearer_token=os.environ.get("X_BEARER_TOKEN"))


def _targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for plat in ("bluesky", "farcaster", "mastodon", "telegram", "x"):
        for t in getattr(args, plat, None) or []:
            out.append((plat, t))
    return out


def _fetch(client: sp.SocialClient, platform: str, target: str) -> sp.ProfileSnapshot:
    if platform == "bluesky":
        return client.bluesky(target)
    if platform == "farcaster":
        return client.farcaster(fid=int(target)) if target.isdigit() else client.farcaster(username=target)
    if platform == "mastodon":
        return client.mastodon(target)
    if platform == "telegram":
        return client.telegram(target)
    if platform == "x":
        return client.x_user(target)
    raise ValueError(platform)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]", "_", s)


def _write_snapshot(out: Path, snap: sp.ProfileSnapshot) -> Path:
    p = out / f"{snap.platform}_{_safe(snap.account)}.json"
    p.write_text(json.dumps(snap.to_dict(), ensure_ascii=False, indent=1))
    return p


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _print_snapshot(snap: sp.ProfileSnapshot) -> None:
    print(f"[{snap.platform}] {snap.account}  ({snap.fetched_at})")
    for k in sp.PROFILE_FIELDS:
        v = snap.fields.get(k)
        if v is None:
            continue
        v = str(v).replace("\n", " ")
        when = snap.changed_at.get(k)
        print(f"    {k:13s} {v[:90]}" + (f"   [modifié {when}]" if when else ""))
    if "*" in snap.changed_at:
        print(f"    (profil indexé {snap.changed_at['*']})")


# ---------------------------------------------------------------------------
# snapshot / diff / watch
# ---------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = _client(args)
    rc = 0
    for plat, target in _targets(args):
        try:
            snap = _fetch(client, plat, target)
        except Exception as e:  # noqa: BLE001 - on veut continuer sur les autres cibles
            print(f"[{plat}] {target}: ERREUR {e}")
            rc = 1
            continue
        _print_snapshot(snap)
        print(f"    -> {_write_snapshot(out, snap)}")
    return rc


def cmd_diff(args: argparse.Namespace) -> int:
    old = sp.ProfileSnapshot.from_dict(json.loads(Path(args.old).read_text()))
    new = sp.ProfileSnapshot.from_dict(json.loads(Path(args.new).read_text()))
    changes = sp.diff_snapshots(old, new)
    deltas = sp.counter_deltas(old, new)
    print(f"{old.platform} {old.account} : {old.fetched_at} -> {new.fetched_at}")
    if not changes:
        print("  aucun changement de champ")
    for c in changes:
        print(f"  {c.field}: {c.old!r} -> {c.new!r}")
    if deltas:
        print("  compteurs :", ", ".join(f"{k} {v:+d}" for k, v in deltas.items()))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = _client(args)
    targets = _targets(args)
    if not targets:
        print("aucune cible")
        return 2
    changes_path = out / "changes.csv"
    new_file = not changes_path.exists()
    fh = changes_path.open("a", newline="")
    w = csv.writer(fh)
    if new_file:
        w.writerow(["detected_at", "platform", "account", "field", "old", "new", "prev_fetched_at"])
    last: dict[tuple[str, str], sp.ProfileSnapshot] = {}
    t_end = time.time() + args.minutes * 60
    n_polls = 0
    errors: collections.Counter[str] = collections.Counter()
    latencies: list[float] = []
    print(f"suivi de {len(targets)} compte(s), toutes les {args.interval} s pendant {args.minutes} min")
    while True:
        t0 = time.time()
        for plat, target in targets:
            t1 = time.time()
            try:
                snap = _fetch(client, plat, target)
            except Exception as e:  # noqa: BLE001
                errors[f"{plat}:{target}"] += 1
                print(f"  {sp.utc_now()} [{plat}] {target}: erreur {str(e)[:120]}")
                continue
            latencies.append(time.time() - t1)
            key = (snap.platform, snap.account)
            prev = last.get(key)
            if prev is None:
                _print_snapshot(snap)
            else:
                for c in sp.diff_snapshots(prev, snap):
                    print(f"  {snap.fetched_at} CHANGEMENT [{plat}] {target} {c.field}: {c.old!r} -> {c.new!r}")
                    w.writerow([snap.fetched_at, snap.platform, snap.account, c.field, c.old, c.new, prev.fetched_at])
                deltas = sp.counter_deltas(prev, snap)
                if any(deltas.values()):
                    print(f"  {snap.fetched_at} compteurs [{plat}] {target}: " + ", ".join(f"{k} {v:+d}" for k, v in deltas.items() if v))
                    for k, v in deltas.items():
                        if v:
                            w.writerow([snap.fetched_at, snap.platform, snap.account, k, prev.fields.get(k), snap.fields.get(k), prev.fetched_at])
            fh.flush()
            last[key] = snap
            _write_snapshot(out, snap)
        n_polls += 1
        if time.time() >= t_end:
            break
        time.sleep(max(0.0, args.interval - (time.time() - t0)))
    fh.close()
    summary = {
        "targets": [f"{p}:{t}" for p, t in targets],
        "polls": n_polls,
        "interval_s": args.interval,
        "errors": dict(errors),
        "request_latency_s": {"median": _pct(latencies, 0.5), "p90": _pct(latencies, 0.9), "n": len(latencies)},
        "changes_csv": str(changes_path),
    }
    (out / "watch_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# stream : push sources
# ---------------------------------------------------------------------------


async def _jetstream(seconds: float, confirm: int, out: Path, client: sp.SocialClient) -> dict[str, Any]:
    import websockets  # dépendance locale au script (comme polymarket_latency.py)

    url = sp.JETSTREAM_URL + "?wantedCollections=app.bsky.actor.profile"
    counts: collections.Counter[str] = collections.Counter()
    rows: list[list[Any]] = []
    confirm_delays: list[float] = []
    confirm_fail = 0
    confirm_error = 0
    confirm_started = 0
    t_start = time.time()

    async def check_appview(did: str, record: dict[str, Any], t_seen: float) -> None:
        """Interroge l'AppView jusqu'à ce qu'il reflète le commit (au plus 30 s)."""
        nonlocal confirm_fail, confirm_error
        loop = asyncio.get_running_loop()
        deadline = t_seen + 30
        last_err: Exception | None = None
        try:
            while time.time() < deadline:
                try:
                    prof = await loop.run_in_executor(None, client.bluesky, did)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    await asyncio.sleep(0.5)
                    continue
                if prof.fields.get("display_name") == record.get("displayName") and prof.fields.get("bio") == record.get("description"):
                    confirm_delays.append(time.time() - t_seen)
                    return
                await asyncio.sleep(0.5)
            if last_err is not None:
                confirm_error += 1  # ex. profil introuvable sur l'AppView (compte tout neuf, désactivé)
            else:
                confirm_fail += 1
        except Exception:  # noqa: BLE001
            confirm_error += 1

    tasks: list[asyncio.Task[None]] = []
    try:
        async with websockets.connect(url, open_timeout=10, max_size=None) as ws:
            t_end = t_start + seconds
            while time.time() < t_end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.time()))
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                now = time.time()
                kind = m.get("kind")
                if kind == "commit":
                    c = m.get("commit", {})
                    key = f"commit:{c.get('collection')}:{c.get('operation')}"
                    counts[key] += 1
                    rec = c.get("record") or {}
                    rows.append([now, m.get("did"), key, c.get("rev"), c.get("cid"), (rec.get("displayName") or "")[:60], len(rec.get("description") or ""), "avatar" in rec, "banner" in rec, "pinnedPost" in rec])
                    if c.get("operation") == "update" and confirm_started < confirm:
                        confirm_started += 1
                        tasks.append(asyncio.create_task(check_appview(m["did"], rec, now)))
                elif kind in ("identity", "account"):
                    body = m.get(kind) or {}
                    if kind == "account":
                        counts[f"account:{body.get('status') or 'active'}:{'active' if body.get('active', True) else 'inactive'}"] += 1
                    else:
                        counts["identity:with_handle" if body.get("handle") else "identity:no_handle"] += 1
                    status = body.get("status") if kind == "account" else "identity"
                    rows.append([now, m.get("did"), f"{kind}:{status}", None, None, body.get("handle") or "", None, None, None, None])
                else:
                    counts[f"other:{kind}"] += 1
    except Exception as e:  # noqa: BLE001
        counts["error"] += 1
        print(f"[jetstream] erreur {e!r}")
    if tasks:
        await asyncio.wait(tasks, timeout=35)
    elapsed = max(1e-9, time.time() - t_start)
    with (out / "jetstream_events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["received_unix", "did", "event", "rev", "cid", "display_name", "bio_len", "has_avatar", "has_banner", "has_pinned"])
        w.writerows(rows)
    profile_commits = sum(v for k, v in counts.items() if k.startswith("commit:app.bsky.actor.profile"))
    return {
        "source": sp.JETSTREAM_URL,
        "seconds": round(elapsed, 1),
        "events": sum(counts.values()),
        "counts": dict(counts),
        "profile_commits_per_minute": round(profile_commits * 60 / elapsed, 1),
        "profile_commits_per_day_extrapolated": int(profile_commits * 86400 / elapsed),
        "appview_reflects_commit_after_s": {
            "started": confirm_started,
            "n": len(confirm_delays),
            "median": _pct(confirm_delays, 0.5),
            "p90": _pct(confirm_delays, 0.9),
            "max": max(confirm_delays) if confirm_delays else None,
            "not_reflected_within_30s": confirm_fail,
            "lookup_errors": confirm_error,
        },
    }


def _farcaster_stream(seconds: float, out: Path, client: sp.SocialClient) -> dict[str, Any]:
    """Suit la queue du journal d'événements de chaque shard, toutes les ~1 s."""
    import requests

    try:
        info = client._json(f"{client.hub_url}/info")  # noqa: SLF001 - script de mesure
    except sp.SocialError as e:
        return {"source": client.hub_url, "error": str(e)}
    shards = [s["shardId"] for s in info.get("shardInfos", []) if s.get("shardId", 0) >= 1]
    tails: dict[int, int] = {}
    freshness: dict[int, float | None] = {}
    for sh in shards:
        try:
            d = client._json(f"{client.hub_url}/events", params={"shard_index": sh, "from_event_id": 0, "pageSize": 1, "reverse": "true"})  # noqa: SLF001
        except sp.SocialError:
            continue
        ev = (d.get("events") or [None])[0]
        if not ev:
            continue
        tails[sh] = int(ev["id"])
        ts = None
        if ev.get("type") == "HUB_EVENT_TYPE_MERGE_MESSAGE":
            ts = ev["mergeMessageBody"]["message"]["data"].get("timestamp")
        freshness[sh] = round(time.time() - (ts + sp.FARCASTER_EPOCH), 1) if ts is not None else None
    if not tails:
        return {"source": client.hub_url, "error": "aucune queue de shard lisible", "info": info}
    counts: collections.Counter[str] = collections.Counter()
    ud_types: collections.Counter[str] = collections.Counter()
    lat: list[float] = []
    rows: list[list[Any]] = []
    t0 = time.time()
    polls = 0
    errors = 0
    last = dict(tails)
    while time.time() - t0 < seconds:
        for sh in list(last):
            try:
                r = client.session.get(f"{client.hub_url}/events", params={"shard_index": sh, "from_event_id": last[sh] + 1, "pageSize": 1000}, timeout=client.timeout)
                polls += 1
                evs = r.json().get("events", []) if r.status_code == 200 else []
                if r.status_code != 200:
                    errors += 1
            except (requests.RequestException, ValueError):
                errors += 1
                continue
            now = time.time()
            for e in evs:
                last[sh] = max(last[sh], int(e["id"]))
                counts[e.get("type", "?")] += 1
                if e.get("type") == "HUB_EVENT_TYPE_MERGE_MESSAGE":
                    d = e["mergeMessageBody"]["message"]["data"]
                    counts["msg:" + d.get("type", "?")] += 1
            for pe in sp.farcaster_profile_events(evs):
                ud_types[pe["field"] or "?"] += 1
                rows.append([now, sh, pe["event_id"], pe["fid"], pe["field"], (str(pe["value"]) if pe["value"] is not None else "")[:80], pe["changed_at"]])
                if pe["changed_at"]:
                    import datetime as dt

                    lat.append(now - dt.datetime.fromisoformat(pe["changed_at"]).timestamp())
        time.sleep(1.0)
    elapsed = max(1e-9, time.time() - t0)
    with (out / "farcaster_profile_events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["received_unix", "shard", "event_id", "fid", "field", "value", "changed_at"])
        w.writerows(rows)
    n_ud = sum(ud_types.values())
    return {
        "source": client.hub_url,
        "hub_version": info.get("version"),
        "shards_followed": sorted(tails),
        "tail_age_at_start_s": freshness,
        "seconds": round(elapsed, 1),
        "polls": polls,
        "errors": errors,
        "events": sum(v for k, v in counts.items() if not k.startswith("msg:")),
        "events_per_minute": round(sum(v for k, v in counts.items() if not k.startswith("msg:")) * 60 / elapsed),
        "counts": dict(counts.most_common(20)),
        "profile_changes": n_ud,
        "profile_changes_per_minute": round(n_ud * 60 / elapsed, 1),
        "profile_changes_per_day_extrapolated": int(n_ud * 86400 / elapsed),
        "profile_change_fields": dict(ud_types),
        "receipt_minus_message_timestamp_s": {"n": len(lat), "median": _pct(lat, 0.5), "p10": _pct(lat, 0.1), "p90": _pct(lat, 0.9)},
    }


async def _nostr(seconds: float, out: Path) -> dict[str, Any]:
    import websockets

    for relay in NOSTR_RELAYS:
        n = 0
        lat: list[float] = []
        rows: list[list[Any]] = []
        t0 = time.time()
        try:
            async with websockets.connect(relay, open_timeout=10) as ws:
                await ws.send(json.dumps(["REQ", "profiles", {"kinds": [0], "since": int(time.time()) - 5}]))
                t_end = time.time() + seconds
                while time.time() < t_end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    m = json.loads(raw)
                    if m[0] == "EVENT":
                        n += 1
                        ev = m[2]
                        lat.append(time.time() - ev["created_at"])
                        try:
                            content = json.loads(ev.get("content") or "{}")
                        except ValueError:
                            content = {}
                        rows.append([time.time(), ev.get("pubkey"), ev.get("created_at"), (content.get("name") or "")[:40], sorted(content)[:8]])
        except Exception as e:  # noqa: BLE001
            print(f"[nostr] {relay}: {e!r}")
            continue
        elapsed = max(1e-9, time.time() - t0)
        with (out / "nostr_profile_events.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["received_unix", "pubkey", "created_at", "name", "keys"])
            w.writerows(rows)
        return {
            "source": relay,
            "seconds": round(elapsed, 1),
            "kind0_events": n,
            "per_minute": round(n * 60 / elapsed, 1),
            "receipt_minus_created_at_s": {"median": _pct(lat, 0.5), "p90": _pct(lat, 0.9), "note": "created_at = horloge du client, parfois fausse"},
        }
    return {"error": "aucun relais joignable"}


async def _run_stream(args: argparse.Namespace, out: Path, client: sp.SocialClient) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    jobs: dict[str, Any] = {}
    if not args.no_jetstream:
        jobs["bluesky_jetstream"] = asyncio.create_task(_jetstream(args.seconds, args.confirm, out, client))
    if not args.no_farcaster:
        jobs["farcaster_hub"] = loop.run_in_executor(None, _farcaster_stream, args.seconds, out, client)
    if args.nostr:
        jobs["nostr"] = asyncio.create_task(_nostr(args.seconds, out))
    results: dict[str, Any] = {}
    for name, job in jobs.items():
        try:
            results[name] = await job
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": repr(e)}
    return results


def cmd_stream(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = _client(args)
    print(f"échantillonnage des flux pendant {args.seconds} s …")
    results = asyncio.run(_run_stream(args, out, client))
    results["measured_at"] = sp.utc_now()
    (out / "stream_summary.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(json.dumps(results, indent=1, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------


def _add_targets(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bluesky", action="append", metavar="HANDLE")
    p.add_argument("--farcaster", action="append", metavar="FID|FNAME")
    p.add_argument("--mastodon", action="append", metavar="USER@INSTANCE")
    p.add_argument("--telegram", action="append", metavar="CHANNEL")
    p.add_argument("--x", action="append", metavar="USERNAME", help="nécessite X_BEARER_TOKEN")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", default=sp.FARCASTER_HUB_URL, help="URL HTTP d'un hub Farcaster (Snapchain)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot")
    _add_targets(p)
    p.add_argument("--out", default="/tmp/social")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("diff")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("watch")
    _add_targets(p)
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--out", default="/tmp/social")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("stream")
    p.add_argument("--seconds", type=float, default=120.0)
    p.add_argument("--confirm", type=int, default=10, help="commits Bluesky à confirmer sur l'AppView")
    p.add_argument("--no-jetstream", action="store_true")
    p.add_argument("--no-farcaster", action="store_true")
    p.add_argument("--nostr", action="store_true")
    p.add_argument("--out", default="/tmp/social")
    p.set_defaults(func=cmd_stream)

    args = ap.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
