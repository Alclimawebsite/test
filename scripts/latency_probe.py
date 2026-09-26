#!/usr/bin/env python
"""Sondes de latence reproductibles pour le budget « un mouvement sur Binance -> notre ordre apparié
sur Polymarket » (voir ``docs/research/latence_infra.md``).

Lecture seule. **Aucun ordre signé n'est jamais envoyé.** La seule requête d'écriture est un
``POST /order`` avec un corps vide ``{}``, sans en-tête d'authentification ni signature : le CLOB
le refuse (403 géoblocage depuis ce conteneur, 401 ailleurs). Il sert uniquement à chronométrer
l'aller-retour jusqu'au serveur qui traite les ordres. La sous-commande ``compute`` signe des
ordres EIP-712 **en mémoire** avec une clé de test jetable générée à la volée (jamais financée,
jamais écrite sur disque, jamais transmise) pour mesurer le temps de signature.

Sous-commandes (sorties dans ``--out``, par défaut ``/tmp/latency_probe`` : ``results.json`` et un
CSV d'échantillons bruts par sous-commande) :

  locate   IP publique du conteneur (via le proxy HTTPS et en sortie directe), fournisseur, région ;
           IP, ASN et région cloud des cibles (plages publiées par AWS et Google Cloud).
  dns      durée de résolution DNS (getaddrinfo) par cible.
  http     connexions froides (DNS, TCP, CONNECT, TLS, TTFB) et RTT applicatif sur connexion
           chaude : GET /cdn-cgi/trace (répondu par le frontal Cloudflare), GET /time, GET /book,
           POST /order non signé ; Binance data-api ; Coinbase /time. En-têtes de localisation
           (cf-ray, x-amz-cf-pop, server, via, x-cache).
  ws       poignée de main WebSocket et RTT sur connexion chaude : PING/PONG applicatif (CLOB),
           LIST_SUBSCRIPTIONS (Binance), message invalide -> erreur (Coinbase), abonnement ->
           instantané (RTDS), et ping de protocole (RFC 6455) pour chacun.
  clock    décalage de l'horloge locale : SNTP (si UDP/123 sort), Binance serverTime, Coinbase /time.
  feeds    latence de transport des flux enregistrés par les collecteurs (rx - horodatage serveur,
           corrigé du décalage d'horloge), délai de publication Chainlink/RTDS, avance de Binance
           sur Chainlink, délai de réaction du carnet Polymarket aux mouvements de Binance.
  compute  temps de calcul local : décodage JSON, formule P(Up), signature EIP-712 (py-clob-client-v2
           et chemin direct coincurve), HMAC L2, sérialisation, surcoût d'une pile WebSocket
           Python (asyncio / uvloop) et d'un client HTTP (httpx HTTP/2, requests).
  all      dns, locate, clock, http, ws, feeds, compute.
  report   tableaux Markdown à partir de results.json.

Exemples ::

    . .venv/bin/activate
    python scripts/latency_probe.py all --out /tmp/latency_probe
    python scripts/latency_probe.py http --n-warm 200 --modes direct
    # signature EIP-712 : environnement séparé (ne touche pas au venv du projet)
    uv venv /tmp/signenv && uv pip install --python /tmp/signenv/bin/python \\
        py-clob-client-v2 coincurve orjson uvloop numpy scipy websockets
    PYTHONPATH=src /tmp/signenv/bin/python scripts/latency_probe.py compute
    python scripts/latency_probe.py report

« proxy » = sortie par le proxy HTTPS de la variable ``HTTPS_PROXY`` (tunnel CONNECT) ; « direct » =
socket ouvert directement vers l'IP de la cible (sur ce conteneur, intercepté de façon transparente
par une passerelle de sortie : le « TCP connect » y mesure la passerelle, pas la cible). Sur un
serveur sans proxy, seul « direct » est mesuré et il est exact.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.append(str(ROOT / "src"))

DEFAULT_OUT = Path(os.environ.get("LATENCY_PROBE_OUT", "/tmp/latency_probe"))
CEX_DIR = ROOT / "data" / "cache" / "cex_ws"
LIVE_DIR = ROOT / "data" / "cache" / "polymarket" / "live"
UA = "tradebot-latency-probe/1.0 (research, read-only)"
perf = time.perf_counter

# ---------------------------------------------------------------------------------------------
# Cibles
# ---------------------------------------------------------------------------------------------
# (hôte, étiquette, méthode, chemin, corps). {token} = jeton Up du marché BTC 5 min en cours.
HTTP_TARGETS = [
    ("clob.polymarket.com", "clob_edge_trace", "GET", "/cdn-cgi/trace", None),
    ("clob.polymarket.com", "clob_time", "GET", "/time", None),
    ("clob.polymarket.com", "clob_book", "GET", "/book?token_id={token}", None),
    ("clob.polymarket.com", "clob_post_order_unsigned", "POST", "/order", "{}"),
    ("data-api.binance.vision", "binance_ping", "GET", "/api/v3/ping", None),
    ("data-api.binance.vision", "binance_time", "GET", "/api/v3/time", None),
    ("api.exchange.coinbase.com", "coinbase_edge_trace", "GET", "/cdn-cgi/trace", None),
    ("api.exchange.coinbase.com", "coinbase_time", "GET", "/time", None),
]
# Requête utilisée pour les connexions froides, par hôte.
COLD_PATH = {
    "clob.polymarket.com": "/time",
    "data-api.binance.vision": "/api/v3/ping",
    "api.exchange.coinbase.com": "/time",
}
DNS_HOSTS = [
    "clob.polymarket.com", "ws-subscriptions-clob.polymarket.com", "ws-live-data.polymarket.com",
    "data-stream.binance.vision", "data-api.binance.vision", "ws-feed.exchange.coinbase.com",
    "api.exchange.coinbase.com",
]
WS_TARGETS = {
    "clob_market": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    "rtds": "wss://ws-live-data.polymarket.com",
    "binance_stream": "wss://data-stream.binance.vision/ws",
    "coinbase_feed": "wss://ws-feed.exchange.coinbase.com",
}
LOC_HEADERS = ("server", "cf-ray", "cf-cache-status", "x-amz-cf-pop", "x-amz-cf-id", "x-cache", "via",
               "age", "x-served-by", "x-mbx-uuid", "alt-svc", "content-type", "date")


# ---------------------------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------------------------
def stats(values) -> dict:
    a = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {"n": int(a.size), "median": float(np.median(a)), "p10": float(np.percentile(a, 10)),
            "p90": float(np.percentile(a, 90)), "p99": float(np.percentile(a, 99)),
            "min": float(a.min()), "max": float(a.max()), "mean": float(a.mean())}


def load_results(out: Path) -> dict:
    p = out / "results.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return {}
    return {}


def save_section(out: Path, name: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    res = load_results(out)
    res[name] = data
    res.setdefault("_meta", {})[name] = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "host": socket.gethostname()}
    (out / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))


def write_csv(out: Path, name: str, rows: list[dict]) -> None:
    if not rows:
        return
    out.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(out / name, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def proxy_addr() -> tuple[str, int] | None:
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not p:
        return None
    u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
    return (u.hostname or "127.0.0.1", u.port or 80)


def available_modes(requested: list[str] | None) -> list[str]:
    modes = ["proxy", "direct"] if proxy_addr() else ["direct"]
    if requested:
        modes = [m for m in modes if m in requested]
    return modes


def ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def urlopen_json(url: str, mode: str = "proxy", timeout: float = 8.0, headers: dict | None = None):
    handlers = [] if mode == "proxy" else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")


def cf_colo(ray: str | None) -> str | None:
    if ray and "-" in ray:
        return ray.rsplit("-", 1)[1]
    return None


def current_btc5m_token() -> str | None:
    """Jeton Up du marché BTC 5 min en cours (gamma), sinon celui du dernier fichier du collecteur."""
    slot = int(time.time()) // 300 * 300
    for s in (slot, slot + 300):
        try:
            data = urlopen_json(f"https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-{s}", "proxy")
            if data:
                ids = data[0].get("clobTokenIds")
                ids = json.loads(ids) if isinstance(ids, str) else ids
                if ids:
                    return str(ids[0])
        except Exception:  # noqa: BLE001 — repli sur les fichiers locaux
            pass
    metas = sorted(LIVE_DIR.glob("btc-updown-5m-*.meta.json"), key=lambda p: p.stat().st_mtime)
    for p in reversed(metas):
        try:
            return str(json.loads(p.read_text())["token_up"])
        except (ValueError, KeyError):
            continue
    return None


# ---------------------------------------------------------------------------------------------
# Client HTTP/1.1 minimal (sockets bruts : chaque phase est chronométrée séparément)
# ---------------------------------------------------------------------------------------------
class RawHttps:
    def __init__(self, host: str, mode: str, timeout: float = 10.0):
        self.host, self.mode, self.timeout = host, mode, timeout
        self.sock: ssl.SSLSocket | None = None
        self.buf = b""
        self.ctx = ssl_context()

    def connect(self) -> dict:
        t0 = perf()
        ip = None
        if self.mode == "direct":
            ai = socket.getaddrinfo(self.host, 443, socket.AF_INET, socket.SOCK_STREAM)
            ip = ai[0][4][0]
            t_dns = perf()
            raw = socket.create_connection((ip, 443), self.timeout)
        else:
            t_dns = t0  # le proxy résout le nom
            raw = socket.create_connection(proxy_addr(), self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        t_tcp = perf()
        if self.mode == "proxy":
            raw.sendall(f"CONNECT {self.host}:443 HTTP/1.1\r\nHost: {self.host}:443\r\n\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = raw.recv(4096)
                if not chunk:
                    raise ConnectionError("proxy fermé pendant CONNECT")
                resp += chunk
            if b" 200" not in resp.split(b"\r\n", 1)[0]:
                raise ConnectionError(resp.split(b"\r\n", 1)[0].decode(errors="replace"))
        t_tun = perf()
        self.sock = self.ctx.wrap_socket(raw, server_hostname=self.host)
        t_tls = perf()
        self.buf = b""
        cert = self.sock.getpeercert() or {}
        issuer = dict(x[0] for x in cert.get("issuer", ()) if x)
        return {"dns_ms": (t_dns - t0) * 1e3 if self.mode == "direct" else None,
                "tcp_ms": (t_tcp - t_dns) * 1e3, "connect_tunnel_ms": (t_tun - t_tcp) * 1e3 if self.mode == "proxy" else None,
                "tls_ms": (t_tls - t_tun) * 1e3, "setup_ms": (t_tls - t0) * 1e3, "ip": ip,
                "tls_version": self.sock.version(), "cert_issuer": issuer.get("organizationName")}

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("connexion fermée")
        return chunk

    def request(self, method: str, path: str, body: str | None = None, extra: dict | None = None) -> dict:
        if self.sock is None:
            self.connect()
        lines = [f"{method} {path} HTTP/1.1", f"Host: {self.host}", f"User-Agent: {UA}", "Accept: */*",
                 "Connection: keep-alive"]
        data = b""
        if body is not None:
            data = body.encode()
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        req = ("\r\n".join(lines) + "\r\n\r\n").encode() + data
        t0 = perf()
        self.sock.sendall(req)
        if not self.buf:
            self.buf = self._recv()
        t_first = perf()
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._recv()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        hl = head.decode("iso-8859-1").split("\r\n")
        status = int(hl[0].split()[1])
        headers: dict[str, str] = {}
        for h in hl[1:]:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            payload = b""
            while True:
                while b"\r\n" not in self.buf:
                    self.buf += self._recv()
                size_line, self.buf = self.buf.split(b"\r\n", 1)
                size = int(size_line.split(b";")[0], 16)
                while len(self.buf) < size + 2:
                    self.buf += self._recv()
                payload += self.buf[:size]
                self.buf = self.buf[size + 2:]
                if size == 0:
                    break
        elif "content-length" in headers:
            n = int(headers["content-length"])
            while len(self.buf) < n:
                self.buf += self._recv()
            payload, self.buf = self.buf[:n], self.buf[n:]
        else:
            payload = self.buf
            self.buf = b""
        t_end = perf()
        if headers.get("connection", "").lower() == "close":
            self.close()
        return {"status": status, "headers": headers, "body": payload,
                "ttfb_ms": (t_first - t0) * 1e3, "total_ms": (t_end - t0) * 1e3}


# ---------------------------------------------------------------------------------------------
# dns
# ---------------------------------------------------------------------------------------------
def cmd_dns(args) -> dict:
    rows, summary = [], {}
    for host in DNS_HOSTS:
        ms, ips = [], set()
        for i in range(args.n_dns):
            t0 = perf()
            try:
                ai = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
                dt = (perf() - t0) * 1e3
                ips.update(x[4][0] for x in ai)
                ms.append(dt)
                rows.append({"host": host, "trial": i, "ms": dt})
            except OSError as exc:
                rows.append({"host": host, "trial": i, "error": str(exc)})
            time.sleep(0.02)
        summary[host] = {"ms": stats(ms), "ipv4": sorted(ips)}
        print(f"dns {host:40s} médiane {summary[host]['ms'].get('median', float('nan')):6.1f} ms  {sorted(ips)}")
    resolv = Path("/etc/resolv.conf").read_text() if Path("/etc/resolv.conf").exists() else ""
    summary["_resolvers"] = re.findall(r"^nameserver\s+(\S+)", resolv, re.M)
    write_csv(args.out, "dns_samples.csv", rows)
    save_section(args.out, "dns", summary)
    return summary


# ---------------------------------------------------------------------------------------------
# locate
# ---------------------------------------------------------------------------------------------
def _ip_in_ranges(ip: str, ranges: list[tuple]) -> list[dict]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return []
    return [meta for net, meta in ranges if addr.version == net.version and addr in net]


def _aws_ranges() -> list[tuple]:
    data = urlopen_json("https://ip-ranges.amazonaws.com/ip-ranges.json", "proxy", timeout=20)
    out = []
    for p in data.get("prefixes", []):
        out.append((ipaddress.ip_network(p["ip_prefix"]), {"cloud": "aws", "region": p["region"], "service": p["service"]}))
    return out


def _gcp_ranges() -> list[tuple]:
    data = urlopen_json("https://www.gstatic.com/ipranges/cloud.json", "proxy", timeout=20)
    out = []
    for p in data.get("prefixes", []):
        net = p.get("ipv4Prefix") or p.get("ipv6Prefix")
        if net:
            out.append((ipaddress.ip_network(net), {"cloud": "gcp", "region": p.get("scope"), "service": p.get("service")}))
    return out


def _cf_ranges() -> list[tuple]:
    out = []
    for url in ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"):
        txt = urlopen_json(url, "proxy")
        for line in str(txt).split():
            try:
                out.append((ipaddress.ip_network(line.strip()), {"cloud": "cloudflare", "region": "anycast"}))
            except ValueError:
                pass
    return out


def cmd_locate(args) -> dict:
    res: dict = {"container": {}, "targets": {}}
    for mode in available_modes(args.modes):
        info: dict = {}
        for name, url in (("ipinfo", "https://ipinfo.io/json"), ("cf_trace", "https://www.cloudflare.com/cdn-cgi/trace"),
                          ("ipify", "https://api.ipify.org?format=json")):
            try:
                r = urlopen_json(url, mode)
                if isinstance(r, str):
                    r = dict(line.split("=", 1) for line in r.strip().splitlines() if "=" in line)
                info[name] = r
            except Exception as exc:  # noqa: BLE001
                info[name] = {"error": str(exc)[:200]}
        res["container"][mode] = info
        ip = (info.get("ipinfo") or {}).get("ip")
        print(f"conteneur [{mode}] IP {ip} {(info.get('ipinfo') or {}).get('org')} "
              f"{(info.get('ipinfo') or {}).get('city')} ; colo Cloudflare {(info.get('cf_trace') or {}).get('colo')}")
    try:
        md = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/zone",
                                   headers={"Metadata-Flavor": "Google"}), timeout=2).read().decode()
        res["container"]["gcp_metadata_zone"] = md
    except Exception as exc:  # noqa: BLE001
        res["container"]["gcp_metadata_zone"] = f"inaccessible ({str(exc)[:80]})"
    ranges: list[tuple] = []
    for loader in (_aws_ranges, _gcp_ranges, _cf_ranges):
        try:
            ranges += loader()
        except Exception as exc:  # noqa: BLE001
            print(f"plages {loader.__name__} indisponibles : {exc}")
    # IP du conteneur -> région cloud
    for info in res["container"].values():
        if isinstance(info, dict):
            for key in ("ipinfo", "ipify"):
                ip = (info.get(key) or {}).get("ip")
                if ip:
                    info[f"{key}_cloud_ranges"] = _ip_in_ranges(ip, ranges)[:3]
    hosts = sorted({h for h, *_ in HTTP_TARGETS} | set(DNS_HOSTS))
    for host in hosts:
        try:
            ips = sorted({x[4][0] for x in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})
        except OSError as exc:
            res["targets"][host] = {"error": str(exc)}
            continue
        entries = []
        for ip in ips[:3]:
            e = {"ip": ip, "cloud": _ip_in_ranges(ip, ranges)[:2]}
            try:
                who = urlopen_json(f"https://ipinfo.io/{ip}/json", "proxy")
                e.update({k: who.get(k) for k in ("org", "city", "region", "country", "hostname")})
            except Exception as exc:  # noqa: BLE001
                e["ipinfo_error"] = str(exc)[:100]
            entries.append(e)
        res["targets"][host] = {"ipv4": ips, "detail": entries}
        c = entries[0].get("cloud") if entries else None
        print(f"{host:40s} {ips[:3]} {entries[0].get('org') if entries else ''} {c}")
    save_section(args.out, "locate", res)
    return res


# ---------------------------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------------------------
def _sntp_once(server: str, timeout: float = 1.5) -> tuple[float, float]:
    """(décalage serveur - local, délai aller-retour) en ms, requête SNTP v4."""
    ntp_epoch = 2208988800
    pkt = bytearray(48)
    pkt[0] = 0x23  # LI=0, VN=4, Mode=3
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t1 = time.time()
        s.sendto(pkt, (server, 123))
        data, _ = s.recvfrom(512)
        t4 = time.time()
    finally:
        s.close()
    def ts(off):
        sec, frac = struct.unpack("!II", data[off:off + 8])
        return sec - ntp_epoch + frac / 2 ** 32
    t2, t3 = ts(32), ts(40)
    return ((t2 - t1) + (t3 - t4)) / 2 * 1e3, ((t4 - t1) - (t3 - t2)) * 1e3


def _cristian(samples: list[tuple[float, float, float]]) -> dict:
    """samples = (t_envoi local ms, heure serveur ms, t_réception local ms) -> décalage serveur - local."""
    if not samples:
        return {"n": 0}
    a = np.asarray(samples, float)
    rtt = a[:, 2] - a[:, 0]
    off = a[:, 1] - (a[:, 0] + a[:, 2]) / 2
    keep = rtt <= np.percentile(rtt, 20)
    return {"n": int(len(a)), "offset_ms": float(np.median(off[keep])), "offset_all_median_ms": float(np.median(off)),
            "rtt_min_ms": float(rtt.min()), "rtt_median_ms": float(np.median(rtt)),
            "uncertainty_ms": float(rtt.min() / 2), "offset_spread_ms": float(np.percentile(off[keep], 90) - np.percentile(off[keep], 10))}


def cmd_clock(args) -> dict:
    res: dict = {}
    for server in ("time.google.com", "time.cloudflare.com", "pool.ntp.org"):
        offs = []
        for _ in range(8):
            try:
                offs.append(_sntp_once(server))
            except OSError as exc:
                res[f"sntp_{server}"] = {"error": str(exc)[:100]}
                break
            time.sleep(0.2)
        if offs:
            a = np.asarray(offs)
            best = a[a[:, 1] <= np.percentile(a[:, 1], 50)]
            res[f"sntp_{server}"] = {"n": len(offs), "offset_ms": float(np.median(best[:, 0])),
                                     "delay_min_ms": float(a[:, 1].min())}
        print(f"SNTP {server}: {res.get(f'sntp_{server}')}")
    for mode in available_modes(args.modes):
        for host, path, parse in (("data-api.binance.vision", "/api/v3/time", lambda b: float(json.loads(b)["serverTime"]) + 0.5),
                                  ("api.exchange.coinbase.com", "/time", lambda b: float(json.loads(b)["epoch"]) * 1e3)):
            c = RawHttps(host, mode)
            samples = []
            try:
                c.connect()
                for _ in range(args.n_clock):
                    t0 = time.time_ns() / 1e6
                    r = c.request("GET", path)
                    t1 = time.time_ns() / 1e6
                    if r["status"] == 200:
                        samples.append((t0, parse(r["body"]), t1))
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                res[f"{host}_{mode}_error"] = str(exc)[:200]
            finally:
                c.close()
            res[f"{host.split('.')[1] if host.startswith('data-api') else 'coinbase'}_{mode}"] = _cristian(samples)
            print(f"horloge vs {host} [{mode}] : {_cristian(samples)}")
    # meilleure estimation : la source au plus petit aller-retour (incertitude = RTT min / 2)
    cands = [(v.get("delay_min_ms", 1e9), v["offset_ms"], k) for k, v in res.items()
             if k.startswith("sntp_") and "offset_ms" in v]
    cands += [(v.get("rtt_min_ms", 1e9), v["offset_ms"], k) for k, v in res.items()
              if k.startswith(("binance_", "coinbase_")) and "offset_ms" in v]
    if cands:
        cands.sort()
        res["best"] = {"offset_ms": cands[0][1], "source": cands[0][2],
                       "meaning": "heure serveur - heure locale ; ajouter à rx pour corriger"}
    res["utc_local"] = datetime.now(timezone.utc).isoformat()
    save_section(args.out, "clock", res)
    return res


# ---------------------------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------------------------
def cmd_http(args) -> dict:
    token = current_btc5m_token() or "0"
    modes = available_modes(args.modes)
    rows_cold, rows_warm, summary = [], [], {"token": token, "modes": modes, "cold": {}, "warm": {}, "headers": {}}
    hosts = list(dict.fromkeys(h for h, *_ in HTTP_TARGETS))
    # Connexions froides
    for host in hosts:
        for mode in modes:
            acc: dict[str, list] = {}
            for i in range(args.n_cold):
                c = RawHttps(host, mode)
                try:
                    t0 = perf()
                    info = c.connect()
                    r = c.request("GET", COLD_PATH[host])
                    info.update({"ttfb_ms": r["ttfb_ms"], "first_request_total_ms": r["total_ms"],
                                 "cold_total_ms": (perf() - t0) * 1e3, "status": r["status"]})
                except Exception as exc:  # noqa: BLE001
                    info = {"error": str(exc)[:200]}
                finally:
                    c.close()
                rows_cold.append({"host": host, "mode": mode, "trial": i, **info})
                for k, v in info.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "status":
                        acc.setdefault(k, []).append(v)
                time.sleep(args.spacing)
            summary["cold"][f"{host}|{mode}"] = {k: stats(v) for k, v in acc.items()}
            s = summary["cold"][f"{host}|{mode}"]
            print(f"froid {host:28s} [{mode:6s}] tcp {s.get('tcp_ms', {}).get('median', float('nan')):6.1f} "
                  f"tunnel {s.get('connect_tunnel_ms', {}).get('median', float('nan')):6.1f} "
                  f"tls {s.get('tls_ms', {}).get('median', float('nan')):6.1f} "
                  f"ttfb {s.get('ttfb_ms', {}).get('median', float('nan')):6.1f} "
                  f"total {s.get('cold_total_ms', {}).get('median', float('nan')):6.1f} ms")
    # Connexions chaudes : une connexion par (hôte, mode), requêtes en tourniquet
    for host in hosts:
        eps = [t for t in HTTP_TARGETS if t[0] == host]
        for mode in modes:
            c = RawHttps(host, mode)
            acc: dict[str, dict[str, list]] = {t[1]: {"ttfb_ms": [], "total_ms": []} for t in eps}
            status: dict[str, dict] = {t[1]: {} for t in eps}
            reconnects = 0
            try:
                c.connect()
                for i in range(args.n_warm):
                    for _, label, method, path, body in eps:
                        if method == "POST" and i >= args.n_post:
                            continue
                        try:
                            if c.sock is None:
                                c.connect()
                                reconnects += 1
                            r = c.request(method, path.format(token=token), body)
                        except Exception as exc:  # noqa: BLE001
                            c.close()
                            rows_warm.append({"host": host, "mode": mode, "label": label, "trial": i, "error": str(exc)[:120]})
                            continue
                        acc[label]["ttfb_ms"].append(r["ttfb_ms"])
                        acc[label]["total_ms"].append(r["total_ms"])
                        st = str(r["status"])
                        status[label][st] = status[label].get(st, 0) + 1
                        rows_warm.append({"host": host, "mode": mode, "label": label, "trial": i, "status": r["status"],
                                          "ttfb_ms": r["ttfb_ms"], "total_ms": r["total_ms"], "bytes": len(r["body"]),
                                          "cf_ray": r["headers"].get("cf-ray"), "x_amz_cf_pop": r["headers"].get("x-amz-cf-pop")})
                        key = f"{label}|{mode}"
                        if key not in summary["headers"]:
                            h = {k: r["headers"][k] for k in LOC_HEADERS if k in r["headers"]}
                            h["colo"] = cf_colo(r["headers"].get("cf-ray"))
                            if label.endswith("trace"):
                                tr = dict(x.split("=", 1) for x in r["body"].decode(errors="replace").splitlines() if "=" in x)
                                h["trace"] = {k: tr.get(k) for k in ("colo", "ip", "http", "tls", "loc")}
                            if method == "POST":
                                h["body"] = r["body"][:300].decode(errors="replace")
                            summary["headers"][key] = h
                        time.sleep(args.spacing)
            finally:
                c.close()
            for label in acc:
                summary["warm"][f"{label}|{mode}"] = {"ttfb_ms": stats(acc[label]["ttfb_ms"]),
                                                     "total_ms": stats(acc[label]["total_ms"]),
                                                     "status": status[label], "reconnects": reconnects}
                s = summary["warm"][f"{label}|{mode}"]["ttfb_ms"]
                print(f"chaud {label:28s} [{mode:6s}] n={s.get('n', 0):3d} médiane {s.get('median', float('nan')):7.1f} "
                      f"p90 {s.get('p90', float('nan')):7.1f} ms  statuts {status[label]}  "
                      f"colo {summary['headers'].get(f'{label}|{mode}', {}).get('colo')}")
    # Écart frontal -> origine (Cloudflare) : RTT d'une requête servie par l'origine - RTT du frontal
    derived = {}
    for mode in modes:
        for origin, edge in (("clob_time", "clob_edge_trace"), ("clob_book", "clob_edge_trace"),
                             ("clob_post_order_unsigned", "clob_edge_trace"), ("coinbase_time", "coinbase_edge_trace")):
            a = summary["warm"].get(f"{origin}|{mode}", {}).get("ttfb_ms", {})
            b = summary["warm"].get(f"{edge}|{mode}", {}).get("ttfb_ms", {})
            if a.get("n") and b.get("n"):
                derived[f"{origin}-minus-edge|{mode}"] = {"median_ms": a["median"] - b["median"], "min_ms": a["min"] - b["min"]}
    summary["edge_to_origin"] = derived
    for k, v in derived.items():
        print(f"frontal -> origine {k:45s} médiane {v['median_ms']:6.1f} ms  (min {v['min_ms']:6.1f})")
    write_csv(args.out, "http_cold_samples.csv", rows_cold)
    write_csv(args.out, "http_warm_samples.csv", rows_warm)
    save_section(args.out, "http", summary)
    return summary


# ---------------------------------------------------------------------------------------------
# ws
# ---------------------------------------------------------------------------------------------
async def _ws_probe(name: str, url: str, mode: str, args, token: str) -> tuple[dict, list[dict]]:
    import websockets

    kw = {"open_timeout": 15, "max_size": 2 ** 24, "ping_interval": None}
    if mode == "direct":
        kw["proxy"] = None
    rows: list[dict] = []
    hs = []
    for i in range(args.n_ws_cold):
        t0 = perf()
        try:
            ws = await websockets.connect(url, **kw)
            hs.append((perf() - t0) * 1e3)
            await ws.close()
        except Exception as exc:  # noqa: BLE001
            rows.append({"target": name, "mode": mode, "kind": "handshake", "trial": i, "error": str(exc)[:120]})
        await asyncio.sleep(0.2)
    for v in hs:
        rows.append({"target": name, "mode": mode, "kind": "handshake", "ms": v})
    app, proto, snap = [], [], []
    queue: asyncio.Queue = asyncio.Queue()
    try:
        ws = await websockets.connect(url, **kw)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200], "handshake_ms": stats(hs)}, rows

    async def reader():
        try:
            async for m in ws:
                await queue.put((perf(), m))
        except Exception:  # noqa: BLE001
            await queue.put((perf(), None))

    rtask = asyncio.create_task(reader())

    async def wait_for(pred, timeout=5.0):
        end = perf() + timeout
        while True:
            left = end - perf()
            if left <= 0:
                raise asyncio.TimeoutError
            t, m = await asyncio.wait_for(queue.get(), left)
            if m is None:
                raise ConnectionError("fermé")
            if pred(m):
                return t, m

    spacing = 0.25 if name == "binance_stream" else 0.1  # Binance : 5 messages entrants/s max
    try:
        if name == "clob_market":
            await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
            await asyncio.sleep(1.0)
        elif name == "rtds":
            await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}]}))
            await asyncio.sleep(1.0)
        while not queue.empty():
            queue.get_nowait()
        for i in range(args.n_ws):
            # RTT applicatif (réponse produite par le serveur applicatif)
            t0 = perf()
            try:
                if name == "clob_market":
                    await ws.send("PING")
                    t1, _ = await wait_for(lambda m: m == "PONG")
                    app.append((t1 - t0) * 1e3)
                elif name == "binance_stream":
                    await ws.send(json.dumps({"method": "LIST_SUBSCRIPTIONS", "id": i + 1}))
                    t1, _ = await wait_for(lambda m, i=i: f'"id":{i + 1}' in m)
                    app.append((t1 - t0) * 1e3)
                elif name == "coinbase_feed":
                    await ws.send('{"type":"ping"}')
                    t1, _ = await wait_for(lambda m: '"type":"error"' in m)
                    app.append((t1 - t0) * 1e3)
                elif name == "rtds" and i < args.n_rtds_snap:
                    sub = {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"eth/usd"}'}
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [sub]}))
                    t1, _ = await wait_for(lambda m: '"data":[' in m and ("eth" in m[:4000]), timeout=8)
                    snap.append((t1 - t0) * 1e3)
                    await ws.send(json.dumps({"action": "unsubscribe", "subscriptions": [sub]}))
            except (asyncio.TimeoutError, ConnectionError) as exc:
                rows.append({"target": name, "mode": mode, "kind": "app", "trial": i, "error": type(exc).__name__})
            await asyncio.sleep(spacing)
            # Ping de protocole (trame de contrôle RFC 6455)
            t0 = perf()
            try:
                waiter = await ws.ping()
                await asyncio.wait_for(waiter, 5)
                proto.append((perf() - t0) * 1e3)
            except Exception as exc:  # noqa: BLE001
                rows.append({"target": name, "mode": mode, "kind": "proto_ping", "trial": i, "error": str(exc)[:80]})
            await asyncio.sleep(spacing)
    finally:
        rtask.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    rows += [{"target": name, "mode": mode, "kind": "app", "ms": v} for v in app]
    rows += [{"target": name, "mode": mode, "kind": "rtds_subscribe_snapshot", "ms": v} for v in snap]
    rows += [{"target": name, "mode": mode, "kind": "proto_ping", "ms": v} for v in proto]
    return {"handshake_ms": stats(hs), "app_rtt_ms": stats(app), "proto_ping_ms": stats(proto),
            "rtds_subscribe_snapshot_ms": stats(snap)}, rows


def cmd_ws(args) -> dict:
    token = current_btc5m_token() or "0"
    summary, rows = {"token": token}, []
    for name, url in WS_TARGETS.items():
        for mode in available_modes(args.modes):
            s, r = asyncio.run(_ws_probe(name, url, mode, args, token))
            summary[f"{name}|{mode}"] = s
            rows += r
            def med(k, s=s):
                return s.get(k, {}).get("median", float("nan"))
            print(f"ws {name:15s} [{mode:6s}] poignée {med('handshake_ms'):6.1f}  app {med('app_rtt_ms'):6.1f} "
                  f"(p90 {s.get('app_rtt_ms', {}).get('p90', float('nan')):6.1f})  ping {med('proto_ping_ms'):6.1f} "
                  f"snap {med('rtds_subscribe_snapshot_ms'):6.1f} ms {s.get('error', '')}")
    write_csv(args.out, "ws_samples.csv", rows)
    save_section(args.out, "ws", summary)
    return summary


# ---------------------------------------------------------------------------------------------
# feeds : fichiers des collecteurs
# ---------------------------------------------------------------------------------------------
def _gz_lines(path: Path):
    """Lignes complètes d'un gzip en cours d'écriture (fin tronquée tolérée)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    rest = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            try:
                data = d.decompress(chunk)
            except zlib.error:
                break
            while d.unused_data:  # membres gzip multiples (fichier rouvert en ajout)
                tail = d.unused_data
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                try:
                    data += d.decompress(tail)
                except zlib.error:
                    break
            buf = rest + data
            lines = buf.split(b"\n")
            rest = lines.pop()
            for ln in lines:
                yield ln


def _iso_ms(s: str) -> float:
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1e3


def _cex_files(src: str, t0: float, t1: float) -> list[Path]:
    out = []
    for p in sorted(CEX_DIR.glob(f"{src}_*.jsonl.gz")):
        m = re.search(r"_(\d{10})\.jsonl\.gz$", p.name)
        if not m:
            continue
        h = datetime.strptime(m.group(1), "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp()
        if h + 3600 >= t0 and h <= t1:
            out.append(p)
    return out


def _read_cex(t0: float, t1: float) -> dict[str, np.ndarray]:
    """Séries utiles des collecteurs CEX/RTDS entre t0 et t1 (s UTC, horloge locale)."""
    bn_trade = {"BTCUSDT": [], "ETHUSDT": []}      # (rx_ms, E, T, price)
    cb = {"BTC-USD": [], "ETH-USD": []}            # (rx_ms, time_ms, time_ms, price)
    cl = {"btc/usd": [], "eth/usd": []}            # (rx_ms, send_ms, obs_ms, value)
    cp = {"btcusdt": [], "ethusdt": []}            # idem, crypto_prices (relais Binance)
    for p in _cex_files("binance", t0, t1):
        for ln in _gz_lines(p):
            try:
                d = json.loads(ln)
                rx = d["rx"] / 1e6
                if not (t0 * 1e3 <= rx <= t1 * 1e3):
                    continue
                m = json.loads(d["msg"])
            except (ValueError, KeyError, TypeError):
                continue
            x = m.get("data") or {}
            if x.get("e") == "aggTrade" and x.get("s") in bn_trade:
                bn_trade[x["s"]].append((rx, x["E"], x["T"], float(x["p"])))
    for p in _cex_files("coinbase", t0, t1):
        for ln in _gz_lines(p):
            try:
                d = json.loads(ln)
                rx = d["rx"] / 1e6
                if not (t0 * 1e3 <= rx <= t1 * 1e3):
                    continue
                m = json.loads(d["msg"])
                if m.get("type") == "ticker" and m.get("time") and m.get("product_id") in cb:
                    tm = _iso_ms(m["time"])
                    cb[m["product_id"]].append((rx, tm, tm, float(m["price"])))
            except (ValueError, KeyError, TypeError):
                continue
    for p in _cex_files("rtds", t0, t1):
        for ln in _gz_lines(p):
            try:
                d = json.loads(ln)
                rx = d["rx"] / 1e6
                if not (t0 * 1e3 <= rx <= t1 * 1e3):
                    continue
                m = json.loads(d["msg"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(m, dict) or m.get("type") != "update":
                continue
            pl = m.get("payload") or {}
            sym = pl.get("symbol")
            row = (rx, float(m.get("timestamp") or np.nan), float(pl.get("timestamp") or np.nan), float(pl.get("value") or np.nan))
            if m.get("topic") == "crypto_prices_chainlink" and sym in cl:
                cl[sym].append(row)
            elif m.get("topic") == "crypto_prices" and sym in cp:
                cp[sym].append(row)
    out = {f"bn_{k}": np.asarray(v, float).reshape(-1, 4) for k, v in bn_trade.items()}
    out.update({f"cb_{k}": np.asarray(v, float).reshape(-1, 4) for k, v in cb.items()})
    out.update({f"cl_{k}": np.asarray(v, float).reshape(-1, 4) for k, v in cl.items()})
    out.update({f"cp_{k}": np.asarray(v, float).reshape(-1, 4) for k, v in cp.items()})
    return out


def _lead_lag_seconds(bn: np.ndarray, cl: np.ndarray, max_lag: int = 12) -> dict:
    """Décalage (s) qui maximise la corrélation des rendements 1 s Binance (par T) et Chainlink
    (par horodatage d'observation) : lag > 0 = Chainlink en retard sur Binance."""
    if len(bn) < 100 or len(cl) < 100:
        return {"n": 0}
    sec_b = np.floor(bn[:, 2] / 1000).astype(np.int64)
    s0, s1 = max(sec_b.min(), int(cl[:, 2].min() // 1000)), min(sec_b.max(), int(cl[:, 2].max() // 1000))
    if s1 - s0 < 120:
        return {"n": 0}
    grid = np.arange(s0, s1 + 1)
    # dernier prix de trade Binance de chaque seconde (report du précédent)
    idx = np.searchsorted(bn[:, 2], (grid + 1) * 1000, side="left") - 1
    pb = np.where(idx >= 0, bn[np.clip(idx, 0, None), 3], np.nan)
    cl_s = {int(o // 1000): v for o, v in zip(cl[:, 2], cl[:, 3])}
    pc = np.array([cl_s.get(int(s), np.nan) for s in grid])
    pc = _ffill(pc)
    rb, rc = np.diff(np.log(pb)), np.diff(np.log(pc))
    res = {}
    for lag in range(-3, max_lag + 1):
        if lag >= 0:
            a, b = rb[: len(rb) - lag], rc[lag:]
        else:
            a, b = rb[-lag:], rc[: len(rc) + lag]
        ok = np.isfinite(a) & np.isfinite(b)
        res[lag] = float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() > 50 else np.nan
    best = max((k for k in res if np.isfinite(res[k])), key=lambda k: res[k])
    return {"n_seconds": int(len(grid)), "corr_by_lag_s": res, "best_lag_s": int(best), "best_corr": res[best]}


def _lead_lag_ms(a: np.ndarray, b: np.ndarray, bin_ms: int = 50, max_lag_ms: int = 1000) -> dict:
    """Corrélation des rendements (pas bin_ms) de a et de b décalé : lag > 0 = b en retard sur a.
    a, b : colonnes (rx, E, T, prix) ; temps = T (horloge de chaque plateforme)."""
    if len(a) < 500 or len(b) < 500:
        return {"n": 0}
    lo, hi = max(a[0, 2], b[0, 2]), min(a[-1, 2], b[-1, 2])
    if hi - lo < 120_000:
        return {"n": 0}
    grid = np.arange(lo, hi, bin_ms)
    pa = np.log(a[np.searchsorted(a[:, 2], grid, side="right") - 1, 3])
    pb = np.log(b[np.searchsorted(b[:, 2], grid, side="right") - 1, 3])
    ra, rb = np.diff(pa), np.diff(pb)
    res = {}
    for k in range(-max_lag_ms // bin_ms, max_lag_ms // bin_ms + 1):
        x, y = (ra[: len(ra) - k], rb[k:]) if k >= 0 else (ra[-k:], rb[: len(rb) + k])
        res[int(k * bin_ms)] = float(np.corrcoef(x, y)[0, 1])
    best = max(res, key=res.get)
    pos = sum(v for k, v in res.items() if k > 0)
    neg = sum(v for k, v in res.items() if k < 0)
    return {"n_bins": int(len(ra)), "bin_ms": bin_ms, "best_lag_ms": best, "best_corr": res[best],
            "sum_corr_b_lags": pos, "sum_corr_b_leads": neg,
            "corr_by_lag_ms": {k: v for k, v in res.items() if abs(k) <= 500}}


def _ffill(x: np.ndarray) -> np.ndarray:
    x = x.copy()
    for i in range(1, len(x)):
        if not np.isfinite(x[i]):
            x[i] = x[i - 1]
    return x


def _poly_mid_series(t0: float, t1: float, asset: str = "btc") -> list[np.ndarray]:
    """[(ts_serveur_ms, rx_ms, mid Up)] par marché, à partir des price_change (meilleurs prix Up)."""
    try:
        from tradebot.polymarket_book import UP, load_market
    except Exception:  # noqa: BLE001
        return []
    out = []
    for meta_p in sorted(LIVE_DIR.glob(f"{asset}-updown-*.meta.json")):
        try:
            meta = json.loads(meta_p.read_text())
            s = datetime.fromisoformat(meta["start"]).timestamp()
            e = datetime.fromisoformat(meta["end"]).timestamp()
        except (ValueError, KeyError):
            continue
        if e < t0 or s > t1:
            continue
        try:
            mk = load_market(meta["slug"])
        except Exception:  # noqa: BLE001
            continue
        rows = []
        for rx, ts, kind, a, pl in mk.events:
            if kind == "pc" and ts > 0:
                for (ai, _p, _sz, _side, bb, ba) in pl:
                    if ai == UP and np.isfinite(bb) and np.isfinite(ba) and 0 < bb < ba < 1:
                        rows.append((ts, rx / 1e6, (bb + ba) / 2, s, e))
                        break
            elif kind == "bba" and a == UP and ts > 0:
                bb, ba = pl
                if np.isfinite(bb) and np.isfinite(ba) and 0 < bb < ba < 1:
                    rows.append((ts, rx / 1e6, (bb + ba) / 2, s, e))
        if rows:
            out.append(np.asarray(rows, float))
    return out


def _reaction_lag(bn: np.ndarray, poly: list[np.ndarray], clock: str, bin_ms: int = 50, max_lag_ms: int = 3000) -> dict:
    """Réponse du milieu Up Polymarket aux rendements Binance : coefficient de régression de
    Δmid(t) sur Δlog prix Binance(t - lag), cumulé ; délai médian = lag où la réponse cumulée atteint
    50 % de la réponse à max_lag. clock = "server" (E/T Binance vs ts CLOB) ou "rx" (heure locale)."""
    if len(bn) < 1000 or not poly:
        return {"n": 0}
    col_b = 2 if clock == "server" else 0
    col_p = 0 if clock == "server" else 1
    lags = np.arange(0, max_lag_ms + bin_ms, bin_ms) // bin_ms
    xs, ys = [], []
    for m in poly:
        s, e = m[0, 3], m[0, 4]
        lo, hi = (s + 10) * 1000, (e - 60) * 1000  # phase 3 : K connu, moyenne finale pas commencée
        if hi - lo < 30_000:
            continue
        grid = np.arange(lo, hi, bin_ms)
        ib = np.searchsorted(bn[:, col_b], grid, side="right") - 1
        ip = np.searchsorted(m[:, col_p], grid, side="right") - 1
        if (ib < 0).any() or (ip < 0).any():
            keep = (ib >= 0) & (ip >= 0)
            grid, ib, ip = grid[keep], ib[keep], ip[keep]
        if len(grid) < 600:
            continue
        lb = np.log(bn[ib, 3])
        pm = m[ip, 2]
        db, dp = np.diff(lb), np.diff(pm)
        L = len(lags)
        X = np.stack([db[L - 1 - k: len(db) - k] for k in lags], axis=1)
        xs.append(X)
        ys.append(dp[L - 1:])
    if not xs:
        return {"n": 0}
    X, y = np.vstack(xs), np.concatenate(ys)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    cum = np.cumsum(beta)
    total = cum[-1]
    frac = cum / total if total != 0 else cum * np.nan
    def first(th):
        k = np.argmax(frac >= th) if np.any(frac >= th) else None
        return int(lags[k] * bin_ms) if k is not None else None
    return {"n_bins": int(len(y)), "n_markets": len(xs), "bin_ms": bin_ms, "clock": clock,
            "beta_total": float(total), "lag_ms_25pct": first(0.25), "lag_ms_50pct": first(0.5),
            "lag_ms_75pct": first(0.75), "lag_ms_90pct": first(0.9),
            "peak_lag_ms": int(lags[int(np.argmax(beta))] * bin_ms),
            "cum_fraction": {int(g * bin_ms): float(f) for g, f in zip(lags[::2], frac[::2], strict=True)}}


def cmd_feeds(args) -> dict:
    res = load_results(args.out)
    off = args.clock_offset_ms
    if off is None:
        off = ((res.get("clock") or {}).get("best") or {}).get("offset_ms", 0.0)
    t1 = time.time()
    t0 = t1 - args.minutes * 60
    d = _read_cex(t0, t1)
    summary: dict = {"window_utc": [datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"),
                                    datetime.fromtimestamp(t1, timezone.utc).isoformat(timespec="seconds")],
                     "clock_offset_ms_applied": off,
                     "note": "rx corrigé = rx + décalage (heure serveur - heure locale)"}
    for sym in ("BTCUSDT", "ETHUSDT"):
        a = d[f"bn_{sym}"]
        if len(a):
            summary[f"binance_aggTrade_{sym}"] = {"rx_minus_E_ms": stats(a[:, 0] + off - a[:, 1]),
                                                  "E_minus_T_ms": stats(a[:, 1] - a[:, 2]), "n": int(len(a))}
    for prod in ("BTC-USD", "ETH-USD"):
        a = d[f"cb_{prod}"]
        if len(a):
            summary[f"coinbase_ticker_{prod}"] = {"rx_minus_time_ms": stats(a[:, 0] + off - a[:, 1]), "n": int(len(a))}
    # Qui mène : Binance (Tokyo) ou Coinbase (us-east-1) ? rendements 50 ms, horloges des plateformes
    summary["lead_binance_over_coinbase_btc"] = _lead_lag_ms(d["bn_BTCUSDT"], d["cb_BTC-USD"])
    summary["lead_binance_over_coinbase_eth"] = _lead_lag_ms(d["bn_ETHUSDT"], d["cb_ETH-USD"])
    for key, label in (("cl_btc/usd", "rtds_chainlink_btc"), ("cl_eth/usd", "rtds_chainlink_eth"),
                       ("cp_btcusdt", "rtds_crypto_prices_btcusdt"), ("cp_ethusdt", "rtds_crypto_prices_ethusdt")):
        a = d[key]
        if len(a):
            summary[label] = {"publish_delay_ms (send - obs)": stats(a[:, 1] - a[:, 2]),
                              "transport_ms (rx - send)": stats(a[:, 0] + off - a[:, 1]),
                              "total_ms (rx - obs)": stats(a[:, 0] + off - a[:, 2]), "n": int(len(a))}
    # Avance de Binance sur l'observation Chainlink (rendements 1 s)
    summary["lead_binance_over_chainlink_btc"] = _lead_lag_seconds(d["bn_BTCUSDT"], d["cl_btc/usd"])
    summary["lead_binance_over_chainlink_eth"] = _lead_lag_seconds(d["bn_ETHUSDT"], d["cl_eth/usd"])
    # Transport du WebSocket de marché CLOB (collecteur Polymarket)
    poly = _poly_mid_series(t0, t1, "btc")
    if poly:
        allm = np.vstack(poly)
        summary["clob_market_ws_btc"] = {"rx_minus_ts_ms": stats(allm[:, 1] + off - allm[:, 0]),
                                         "n_markets": len(poly)}
        bn = d["bn_BTCUSDT"]
        summary["reaction_poly_mid_to_binance_btc_server"] = _reaction_lag(bn, poly, "server")
        summary["reaction_poly_mid_to_binance_btc_rx"] = _reaction_lag(bn, poly, "rx")
        summary["reaction_poly_mid_to_coinbase_btc_server"] = _reaction_lag(d["cb_BTC-USD"], poly, "server")
    for k, v in summary.items():
        if isinstance(v, dict):
            flat = {kk: (vv.get("median") if isinstance(vv, dict) and "median" in vv else vv)
                    for kk, vv in v.items() if kk not in ("corr_by_lag_s", "cum_fraction", "corr_by_lag_ms")}
            print(f"{k}: {flat}")
    save_section(args.out, "feeds", summary)
    return summary


# ---------------------------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------------------------
def _bench(fn, n: int, warmup: int = 50) -> dict:
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter_ns()
        fn()
        ts.append((time.perf_counter_ns() - t0) / 1e3)
    s = stats(ts)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}  # µs


async def _ws_loopback(n: int, use_uvloop: bool) -> dict:
    """Surcoût de la pile WebSocket Python : serveur local qui envoie n messages horodatés."""
    import websockets

    lat = []
    msg_tpl = ('{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1790418592302,"s":"BTCUSDT",'
               '"a":4074409497,"p":"84143.54000000","q":"0.00029000","f":6714977585,"l":6714977585,'
               '"T":1790418592302,"m":true,"M":true},"t":%d}')

    async def handler(ws):
        for _ in range(n):
            await ws.send(msg_tpl % time.perf_counter_ns())
            await asyncio.sleep(0.001)
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            async for m in ws:
                t = time.perf_counter_ns()
                sent = int(json.loads(m)["t"])
                lat.append((t - sent) / 1e3)
    return stats(lat)


def cmd_compute(args) -> dict:
    res: dict = {"python": sys.version.split()[0], "cpu_count": os.cpu_count()}
    try:
        res["cpu_model"] = next((x.split(":", 1)[1].strip() for x in open("/proc/cpuinfo") if x.startswith("model name")), None)
    except OSError:
        pass
    n = args.n_compute
    raw = ('{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1790418592302,"s":"BTCUSDT","a":4074409497,'
           '"p":"84143.54000000","q":"0.00029000","f":6714977585,"l":6714977585,"T":1790418592302,"m":true,"M":true}}')
    res["json_loads_aggTrade_us"] = _bench(lambda: json.loads(raw), n)
    try:
        import orjson
        res["orjson_loads_aggTrade_us"] = _bench(lambda: orjson.loads(raw), n)
    except ImportError:
        res["orjson_loads_aggTrade_us"] = "orjson non installé"
    # Formule
    try:
        from tradebot.polymarket_formula import fair_prob_up, taker_edge
        S, E = 1_000_000.0, 1_000_300.0
        kw3 = {"price_to_beat": math.log(84000.0)}
        res["fair_prob_up_phase3_dt1_us"] = _bench(lambda: fair_prob_up(S + 100, S, E, math.log(84010.0), 2e-5, **kw3), n)
        res["fair_prob_up_phase3_continuous_us"] = _bench(
            lambda: fair_prob_up(S + 100, S, E, math.log(84010.0), 2e-5, dt=None, **kw3), n)
        res["fair_prob_up_phase4_dt1_us"] = _bench(lambda: fair_prob_up(
            E - 30, S, E, math.log(84010.0), 2e-5, price_to_beat=math.log(84000.0), end_partial_sum=math.log(84005.0) * 0.5), n)
        res["taker_edge_us"] = _bench(lambda: taker_edge(0.62, 0.58, 0.44), n)
    except Exception as exc:  # noqa: BLE001
        res["formula_error"] = str(exc)[:200]
    # Chemin rapide : Φ(m/s) avec math.erf (phase 3, forme fermée)
    sig, L = 2e-5, 60.0

    def fast_phase3(p=math.log(84010.0), k=math.log(84000.0), tau=200.0):
        s = sig * math.sqrt(tau - 2 * L / 3)
        return 0.5 * (1 + math.erf((p - k) / (s * math.sqrt(2))))
    res["fast_phase3_erf_us"] = _bench(fast_phase3, n)
    # HMAC L2 (secret factice)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    body = '{"order":{"salt":1},"owner":"x","orderType":"FOK","deferExec":false,"postOnly":false}'

    def l2():
        msg = f"{int(time.time())}POST/order{body}"
        return base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(secret), msg.encode(), hashlib.sha256).digest())
    res["hmac_l2_header_us"] = _bench(l2, n)
    # Signature EIP-712 d'un ordre V2 avec une clé JETABLE (jamais financée, jamais envoyée)
    try:
        from eth_account import Account
        from py_clob_client_v2.clob_types import CreateOrderOptions, OrderArgsV2
        from py_clob_client_v2.order_builder.builder import OrderBuilder
        from py_clob_client_v2.order_utils.model.order_data_v2 import order_to_json_v2
        from py_clob_client_v2.signer import Signer

        acct = Account.create(extra_entropy=secrets.token_hex(16))
        signer = Signer(acct.key.hex(), 137)
        ob = OrderBuilder(signer)
        token = "92356134280402951609803044321850718495257050204729987500297880367831595504020"
        oa = OrderArgsV2(token_id=token, price=0.55, size=10.0, side="BUY")
        opts = CreateOrderOptions(tick_size="0.01", neg_risk=False)
        res["eip712_build_and_sign_py_clob_client_v2_us"] = _bench(lambda: ob.build_order(oa, opts), max(200, n // 10), 20)
        signed = ob.build_order(oa, opts)
        payload = order_to_json_v2(signed, "00000000-0000-0000-0000-000000000000", "FOK")
        res["serialize_order_json_us"] = _bench(lambda: json.dumps(payload, separators=(",", ":")), n)
        try:
            import orjson
            res["serialize_order_orjson_us"] = _bench(lambda: orjson.dumps(payload), n)
        except ImportError:
            pass
        # Chemin direct : séparateur de domaine précalculé + keccak + ECDSA libsecp256k1 (coincurve)
        try:
            import coincurve
            from eth_abi import encode as abi_encode
            from eth_utils import keccak
            from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ORDER_TYPE_HASH

            from eth_account.messages import encode_typed_data
            from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (ExchangeOrderBuilderV2,
                                                                                  _hash_message)

            domain_sep = keccak(abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                 keccak(text="Polymarket CTF Exchange"), keccak(text="2"), 137, "0xE111180000d2663C0091e4f400237545B87B996B"]))
            pk = coincurve.PrivateKey(bytes(acct.key))
            maker = bytes.fromhex(acct.address[2:]).rjust(32, b"\x00")
            zero32 = b"\x00" * 32
            tok = int(token)

            def u256(x: int) -> bytes:
                return x.to_bytes(32, "big")

            def fast_digest(salt: int, ts_ms: int, maker_amt: int = 5_500_000, taker_amt: int = 10_000_000) -> bytes:
                enc = (ORDER_TYPE_HASH + u256(salt) + maker + maker + u256(tok) + u256(maker_amt) + u256(taker_amt)
                       + u256(0) + u256(0) + u256(ts_ms) + zero32 + zero32)   # side BUY=0, signatureType EOA=0
                return keccak(b"\x19\x01" + domain_sep + keccak(enc))

            def fast_sign():
                sig = pk.sign_recoverable(fast_digest(secrets.randbits(64), time.time_ns() // 1_000_000), hasher=None)
                return sig[:64] + bytes([sig[64] + 27])
            # Contrôle : même condensat que la bibliothèque officielle pour un ordre identique
            xb = ExchangeOrderBuilderV2("0xE111180000d2663C0091e4f400237545B87B996B", 137, signer)
            ref = signed
            lib_digest = _hash_message(encode_typed_data(full_message=xb.build_order_typed_data(ref)))
            mine = fast_digest(int(ref.salt), int(ref.timestamp), int(ref.makerAmount), int(ref.takerAmount))
            sig = pk.sign_recoverable(mine, hasher=None)
            res["eip712_fast_path_matches_library"] = bool(
                lib_digest == mine and ("0x" + (sig[:64] + bytes([sig[64] + 27])).hex()) == ref.signature)
            res["eip712_fast_path_coincurve_us"] = _bench(fast_sign, n)
            res["keccak256_us"] = _bench(lambda: keccak(b"x" * 416), n)
            res["secp256k1_sign_only_us"] = _bench(lambda: pk.sign_recoverable(b"\x11" * 32, hasher=None), n)
        except ImportError as exc:
            res["eip712_fast_path_coincurve_us"] = f"indisponible ({exc})"
        # la clé jetable ne vit que dans cette fonction : ni écrite, ni affichée, ni transmise
    except ImportError as exc:
        res["eip712_build_and_sign_py_clob_client_v2_us"] = (
            f"indisponible ({exc}) : installer py-clob-client-v2 dans un environnement séparé (voir l'en-tête)")
    # Surcoût d'une pile WebSocket Python en local (asyncio puis uvloop)
    try:
        res["ws_loopback_asyncio_us"] = asyncio.run(_ws_loopback(args.n_loopback, False))
        try:
            import uvloop
            res["ws_loopback_uvloop_us"] = uvloop.run(_ws_loopback(args.n_loopback, True))
        except ImportError:
            res["ws_loopback_uvloop_us"] = "uvloop non installé"
    except Exception as exc:  # noqa: BLE001
        res["ws_loopback_error"] = str(exc)[:200]
    # Surcoût d'un client HTTP (connexion chaude) vs socket brut : même hôte, même chemin, requêtes
    # entrelacées (une par client à chaque tour) pour que la variance du réseau soit commune.
    if not args.no_network:
        url = "https://clob.polymarket.com/time"
        clients: dict = {}
        rows: dict = {}
        try:
            import httpx
            cl = httpx.Client(http2=True, timeout=10, headers={"User-Agent": UA})
            clients["httpx_http2_clob_time_ms"] = (cl.get, cl.close)
        except Exception as exc:  # noqa: BLE001
            rows["httpx_http2_clob_time_ms"] = f"indisponible ({str(exc)[:80]})"
        try:
            import requests
            se = requests.Session()
            se.headers["User-Agent"] = UA
            clients["requests_clob_time_ms"] = (lambda u, se=se: se.get(u, timeout=10), se.close)
        except Exception as exc:  # noqa: BLE001
            rows["requests_clob_time_ms"] = f"indisponible ({str(exc)[:80]})"
        raw = RawHttps("clob.polymarket.com", "proxy" if proxy_addr() else "direct")
        clients["raw_socket_clob_time_ms"] = (lambda u, raw=raw: raw.request("GET", "/time"), raw.close)
        samples: dict[str, list] = {k: [] for k in clients}
        try:
            for get, _ in clients.values():
                get(url)  # connexion ouverte hors mesure
            for _ in range(args.n_client):
                for k, (get, _) in clients.items():
                    t0 = perf()
                    get(url)
                    samples[k].append((perf() - t0) * 1e3)
                    time.sleep(0.02)
        except Exception as exc:  # noqa: BLE001
            rows["error"] = str(exc)[:200]
        finally:
            for _, close in clients.values():
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        rows.update({k: stats(v) for k, v in samples.items()})
        if samples.get("raw_socket_clob_time_ms"):
            base = np.asarray(samples["raw_socket_clob_time_ms"])
            for k, v in samples.items():
                if k != "raw_socket_clob_time_ms" and len(v) == len(base):
                    rows[f"{k}_minus_raw_paired"] = stats(np.asarray(v) - base)
        res["http_client_overhead"] = rows
    for k, v in res.items():
        if isinstance(v, dict) and "median" in v:
            print(f"{k:50s} médiane {v['median']:10.2f}  p90 {v['p90']:10.2f}")
        elif isinstance(v, dict):
            for kk, vv in v.items():
                print(f"{k}.{kk}: {vv.get('median') if isinstance(vv, dict) else vv}")
        else:
            print(f"{k}: {v}")
    save_section(args.out, "compute", res)
    return res


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------
def _fmt(s: dict | None, key: str = "median") -> str:
    if not isinstance(s, dict) or not s.get("n"):
        return "—"
    return f"{s[key]:.1f}"


def cmd_report(args) -> None:
    res = load_results(args.out)
    if not res:
        print("results.json introuvable : lancer d'abord une mesure.")
        return
    print(f"# Sondes de latence ({args.out}/results.json)\n")
    for sec, meta in (res.get("_meta") or {}).items():
        print(f"- {sec} : {meta['utc']} sur {meta['host']}")
    if "dns" in res:
        print("\n## DNS (getaddrinfo, ms)\n\n| hôte | médiane | p90 | IPv4 |\n|---|---|---|---|")
        for h, v in res["dns"].items():
            if not h.startswith("_"):
                print(f"| {h} | {_fmt(v['ms'])} | {_fmt(v['ms'], 'p90')} | {', '.join(v['ipv4'][:3])} |")
    if "http" in res:
        print("\n## HTTP : connexion froide (ms, médiane / p90)\n\n| hôte | mode | TCP | CONNECT | TLS | TTFB | total |\n|---|---|---|---|---|---|---|")
        for k, v in res["http"]["cold"].items():
            h, m = k.split("|")
            print(f"| {h} | {m} | {_fmt(v.get('tcp_ms'))} / {_fmt(v.get('tcp_ms'), 'p90')} | {_fmt(v.get('connect_tunnel_ms'))} | "
                  f"{_fmt(v.get('tls_ms'))} / {_fmt(v.get('tls_ms'), 'p90')} | {_fmt(v.get('ttfb_ms'))} / {_fmt(v.get('ttfb_ms'), 'p90')} | "
                  f"{_fmt(v.get('cold_total_ms'))} / {_fmt(v.get('cold_total_ms'), 'p90')} |")
        print("\n## HTTP : connexion chaude, requête -> premier octet (ms)\n\n| requête | mode | n | médiane | p90 | min | statuts | colo |\n|---|---|---|---|---|---|---|---|")
        for k, v in res["http"]["warm"].items():
            lab, m = k.split("|")
            hd = res["http"]["headers"].get(k, {})
            print(f"| {lab} | {m} | {v['ttfb_ms'].get('n', 0)} | {_fmt(v['ttfb_ms'])} | {_fmt(v['ttfb_ms'], 'p90')} | "
                  f"{_fmt(v['ttfb_ms'], 'min')} | {v['status']} | {hd.get('colo') or hd.get('x-amz-cf-pop') or hd.get('server')} |")
        for k, v in res["http"].get("edge_to_origin", {}).items():
            print(f"- {k} : {v['median_ms']:.1f} ms (min {v['min_ms']:.1f})")
    if "ws" in res:
        print("\n## WebSocket (ms, médiane / p90)\n\n| cible | mode | poignée | RTT applicatif | ping protocole | abonnement -> instantané |\n|---|---|---|---|---|---|")
        for k, v in res["ws"].items():
            if "|" not in k:
                continue
            t, m = k.split("|")
            print(f"| {t} | {m} | {_fmt(v.get('handshake_ms'))} / {_fmt(v.get('handshake_ms'), 'p90')} | "
                  f"{_fmt(v.get('app_rtt_ms'))} / {_fmt(v.get('app_rtt_ms'), 'p90')} | "
                  f"{_fmt(v.get('proto_ping_ms'))} / {_fmt(v.get('proto_ping_ms'), 'p90')} | "
                  f"{_fmt(v.get('rtds_subscribe_snapshot_ms'))} |")
    for sec in ("clock", "feeds", "compute", "locate"):
        if sec in res:
            print(f"\n## {sec}\n\n```json\n{json.dumps(res[sec], indent=1, ensure_ascii=False, default=str)[:6000]}\n```")


# ---------------------------------------------------------------------------------------------
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["dns", "locate", "clock", "http", "ws", "feeds", "compute", "all", "report"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--modes", nargs="*", choices=["proxy", "direct"], help="chemins réseau à mesurer (défaut : tous)")
    ap.add_argument("--n-dns", type=int, default=50)
    ap.add_argument("--n-cold", type=int, default=50, help="connexions froides par hôte et par mode")
    ap.add_argument("--n-warm", type=int, default=100, help="requêtes par point d'accès sur connexion chaude")
    ap.add_argument("--n-post", type=int, default=50, help="POST /order non signés (refusés) par mode")
    ap.add_argument("--n-ws-cold", type=int, default=20, help="poignées de main WebSocket par cible et par mode")
    ap.add_argument("--n-ws", type=int, default=100, help="allers-retours WebSocket par cible et par mode")
    ap.add_argument("--n-rtds-snap", type=int, default=30, help="cycles abonnement -> instantané RTDS")
    ap.add_argument("--n-clock", type=int, default=60)
    ap.add_argument("--n-compute", type=int, default=5000)
    ap.add_argument("--n-loopback", type=int, default=2000)
    ap.add_argument("--n-client", type=int, default=60)
    ap.add_argument("--spacing", type=float, default=0.05, help="pause entre deux requêtes HTTP (s)")
    ap.add_argument("--minutes", type=float, default=60.0, help="fenêtre analysée par feeds (minutes)")
    ap.add_argument("--clock-offset-ms", type=float, default=None,
                    help="décalage heure serveur - heure locale (défaut : résultat de clock)")
    ap.add_argument("--no-network", action="store_true", help="compute : ne pas mesurer les clients HTTP")
    ap.add_argument("--skip", nargs="*", default=[], help="all : sous-commandes à sauter (ex. compute)")
    args = ap.parse_args(argv)
    cmds = {"dns": cmd_dns, "locate": cmd_locate, "clock": cmd_clock, "http": cmd_http, "ws": cmd_ws,
            "feeds": cmd_feeds, "compute": cmd_compute, "report": cmd_report}
    if args.command == "all":
        for c in ("dns", "locate", "clock", "http", "ws", "feeds", "compute"):
            if c in args.skip:
                continue
            print(f"\n=== {c} ===")
            try:
                cmds[c](args)
            except Exception as exc:  # noqa: BLE001 — une sonde en échec ne bloque pas les autres
                print(f"{c} : échec {exc!r}")
    else:
        cmds[args.command](args)


if __name__ == "__main__":
    main()
