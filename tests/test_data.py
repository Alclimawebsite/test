"""Tests de ``tradebot.data``.

Hors-ligne : zips Binance fabriqués en mémoire, HTTP simulé (``FakeBinance``), horloge figée,
cache dans ``tmp_path``. Réseau (``@pytest.mark.network``) : BTCUSDT 1 min sur 2 jours,
dérivés sur 2 jours, Yahoo en secondaire.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile

import numpy as np
import pandas as pd
import pytest

from tradebot import data as D

MIN = pd.Timedelta(minutes=1)
HOUR = pd.Timedelta(hours=1)


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


# ---------------------------------------------------------------------------
# Fabrique de klines / zips
# ---------------------------------------------------------------------------

def _epoch(t: pd.Timestamp, unit: str) -> int:
    return t.value // (1_000_000 if unit == "ms" else 1_000)


def _price(t: pd.Timestamp) -> float:
    """Prix synthétique déterministe (fonction de la minute)."""
    return 100.0 + (t.value // 60_000_000_000 % 97) * 0.01


def kline_rows(start: pd.Timestamp, n: int, unit: str = "ms", bar: pd.Timedelta = MIN,
               skip: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> list[list]:
    rows = []
    for i in range(n):
        t = start + i * bar
        if skip is not None and skip[0] <= t < skip[1]:
            continue
        o, c = _price(t), _price(t + bar)
        rows.append([_epoch(t, unit), f"{o:.8f}", f"{max(o, c) + 0.05:.8f}",
                     f"{min(o, c) - 0.05:.8f}", f"{c:.8f}", "1.50000000",
                     _epoch(t + bar, unit) - 1, f"{150 * o:.8f}", 10 + i % 7, "0.75000000",
                     f"{75 * o:.8f}", "0"])
    return rows


def make_zip(rows, header: tuple[str, ...] | None = None, name: str = "data.csv") -> bytes:
    text = io.StringIO()
    if header:
        text.write(",".join(header) + "\n")
    for r in rows:
        text.write(",".join(str(x) for x in r) + "\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text.getvalue())
    return buf.getvalue()


def parse_zip(content: bytes) -> pd.DataFrame:
    return D.klines_to_frame(D._read_csv(D._csv_bytes_from_zip(content), D.KLINE_FIELDS))


class FakeResponse:
    def __init__(self, content: bytes = b"", payload=None, headers: dict | None = None):
        self.content = content
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


METRICS_HEADER = D.METRICS_CSV_FIELDS
FUNDING_HEADER = D.FUNDING_CSV_FIELDS


class FakeBinance:
    """Simule data.binance.vision (klines spot, metrics, fundingRate) et data-api (klines)."""

    def __init__(self, now: pd.Timestamp, interval: str = "1m", published_days=(),
                 published_months=(), header_days=(), us_from: pd.Timestamp = ts("2025-01-01"),
                 api_ignores_end: bool = False, gap=None, metrics_days=(), funding_months=()):
        self.now = now
        self.interval = interval
        self.bar = D.interval_to_timedelta(interval)
        self.published_days = set(published_days)
        self.published_months = set(published_months)
        self.header_days = set(header_days)
        self.us_from = us_from
        self.api_ignores_end = api_ignores_end
        self.gap = gap
        self.metrics_days = set(metrics_days)
        self.funding_months = set(funding_months)
        self.calls: list[tuple[str, dict]] = []

    # -- helpers pour les assertions --
    def bulk_calls(self, kind: str = "klines") -> list[str]:
        return [u for u, _ in self.calls if f"/{kind}/" in u]

    def api_calls(self) -> list[dict]:
        return [p for u, p in self.calls if u == D.API_KLINES_URL]

    def __call__(self, url: str, params: dict | None = None, allow_404: bool = True):
        self.calls.append((url, dict(params or {})))
        if url == D.API_KLINES_URL:
            return self._api(params)
        m = re.search(r"/spot/(monthly|daily)/klines/([A-Z]+)/(\w+)/\2-\3-([\d-]+)\.zip$", url)
        if m:
            return self._kline_zip(m.group(1), m.group(4))
        m = re.search(r"/metrics/[A-Z]+/[A-Z]+-metrics-([\d-]+)\.zip$", url)
        if m:
            return self._metrics_zip(m.group(1))
        m = re.search(r"/fundingRate/[A-Z]+/[A-Z]+-fundingRate-([\d-]+)\.zip$", url)
        if m:
            return self._funding_zip(m.group(1))
        raise AssertionError(f"URL inattendue : {url}")

    def _api(self, params):
        start = pd.Timestamp(params["startTime"], unit="ms", tz="UTC")
        end = pd.Timestamp(params["endTime"], unit="ms", tz="UTC")
        if self.api_ignores_end:  # renvoie aussi la barre en cours, comme l'API sans endTime
            end = self.now
        t = D._floor(start, self.bar)
        if t < start:
            t += self.bar
        rows = []
        while t <= end and t <= self.now and len(rows) < params["limit"]:
            rows += kline_rows(t, 1, "ms", self.bar, skip=self.gap)
            t += self.bar
        return FakeResponse(payload=rows, headers={"x-mbx-used-weight-1m": "2"})

    def _kline_zip(self, freq: str, period: str):
        if freq == "daily":
            if period not in self.published_days:
                return None
            start = ts(period)
            n = int(pd.Timedelta(days=1) / self.bar)
        else:
            if period not in self.published_months:
                return None
            start = ts(period + "-01")
            n = int((start + pd.offsets.MonthBegin(1) - start) / self.bar)
        unit = "us" if start >= self.us_from else "ms"
        header = D.KLINE_FIELDS if period in self.header_days else None
        return FakeResponse(content=make_zip(kline_rows(start, n, unit, self.bar, skip=self.gap),
                                             header=header))

    def _metrics_zip(self, day: str):
        if day not in self.metrics_days:
            return None
        rows = []
        for k in range(288):
            t = ts(day) + k * pd.Timedelta(minutes=5)
            rows.append([t.strftime("%Y-%m-%d %H:%M:%S"), "BTCUSDT", 1000 + k, 1e8 + k,
                         1.1, 1.9, 0.95, 1.0 + k / 1000])
        rows = [rows[i] for i in np.random.default_rng(0).permutation(len(rows))]  # non trié
        return FakeResponse(content=make_zip(rows, header=METRICS_HEADER))

    def _funding_zip(self, month: str):
        if month not in self.funding_months:
            return None
        m0 = ts(month + "-01")
        n = int((m0 + pd.offsets.MonthBegin(1) - m0) / pd.Timedelta(hours=8))
        rows = []
        for k in range(n):
            ms = _epoch(m0 + k * pd.Timedelta(hours=8), "ms") + (1 if k == 0 else 0)
            rows.append([ms, 8, f"{0.0001 * (k + 1):.8f}"])
        return FakeResponse(content=make_zip(rows, header=FUNDING_HEADER))


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Installe un FakeBinance, fige l'horloge et redirige le cache vers tmp_path."""

    def install(fake: FakeBinance) -> FakeBinance:
        monkeypatch.setattr(D, "_http_get", fake)
        monkeypatch.setattr(D, "_utcnow", lambda: fake.now)
        monkeypatch.setattr(D, "CACHE_DIR", tmp_path)
        return fake

    return install


def days_between(a: str, b: str) -> list[str]:
    return [f"{d:%Y-%m-%d}" for d in pd.date_range(a, b, freq="D")]


def assert_contract(df: pd.DataFrame) -> None:
    assert list(df.columns) == list(D.CRYPTO_COLUMNS)
    assert all(dt == np.float64 for dt in df.dtypes)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing and not df.index.has_duplicates
    assert df.index.name == "open_time"


# ---------------------------------------------------------------------------
# Parsing des klines
# ---------------------------------------------------------------------------

def test_epoch_unit_detection_ms_and_us():
    idx = D._epoch_to_datetime([1733011200000, 1790121600000000, 1790121600])
    assert list(idx) == [ts("2024-12-01"), ts("2026-09-23"), ts("2026-09-23")]
    assert str(idx.dtype) == "datetime64[ns, UTC]"


@pytest.mark.parametrize("unit", ["ms", "us"])
@pytest.mark.parametrize("header", [False, True])
def test_parse_kline_zip_units_and_header(unit, header):
    start = ts("2026-09-23 00:00")
    rows = kline_rows(start, 5, unit)
    df = parse_zip(make_zip(rows, header=D.KLINE_FIELDS if header else None))
    assert_contract(df)
    assert list(df.index) == list(pd.date_range(start, periods=5, freq="1min", tz="UTC"))
    assert df["close"].iloc[0] == pytest.approx(_price(start + MIN))
    assert df["taker_buy_volume"].iloc[0] == 0.75
    assert df["trades"].iloc[0] == 10.0
    assert df["quote_volume"].iloc[0] == pytest.approx(150 * _price(start))


def test_parse_kline_ms_and_us_give_identical_frames():
    start = ts("2025-01-01 00:00")
    ref = parse_zip(make_zip(kline_rows(start, 30, "ms")))
    for unit, header in (("us", False), ("us", True), ("ms", True)):
        other = parse_zip(make_zip(kline_rows(start, 30, unit),
                                   header=D.KLINE_FIELDS if header else None))
        pd.testing.assert_frame_equal(ref, other)


def test_futures_style_header_is_detected():
    header = ("open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore")
    df = parse_zip(make_zip(kline_rows(ts("2026-09-23"), 3, "ms"), header=header))
    assert len(df) == 3 and df["trades"].tolist() == [10.0, 11.0, 12.0]


def test_api_rows_drop_unclosed_bar():
    start = ts("2026-09-25 17:00")
    rows = kline_rows(start, 3, "ms")  # 17:00, 17:01, 17:02
    # 17:02:30 : la barre 17:02 n'est pas clôturée
    df = D.klines_to_frame(rows, now=ts("2026-09-25 17:02:30"))
    assert list(df.index) == [ts("2026-09-25 17:00"), ts("2026-09-25 17:01")]
    # exactement à la clôture (17:03:00 > close_time 17:02:59.999) : barre gardée
    df = D.klines_to_frame(rows, now=ts("2026-09-25 17:03:00"))
    assert len(df) == 3
    # close_time en µs : même règle
    rows_us = kline_rows(start, 3, "us")
    assert len(D.klines_to_frame(rows_us, now=ts("2026-09-25 17:02:59.9999"))) == 2
    assert D.klines_to_frame([], now=start).empty


# ---------------------------------------------------------------------------
# Pipeline complet hors-ligne (bulk + API + cache)
# ---------------------------------------------------------------------------

def test_fetch_ohlcv_daily_zips_then_api(offline):
    now = ts("2026-09-02 10:30:30")
    fake = offline(FakeBinance(
        now, published_days={"2026-08-30", "2026-08-31"},  # 1er sept. pas encore publié
        header_days={"2026-08-30"}, us_from=ts("2026-08-31"),  # 30/08 en ms, 31/08 en µs
        api_ignores_end=True))
    df = D.fetch_ohlcv("BTCUSDT", "1m", days=3, cache=False)
    assert_contract(df)
    assert df.index[0] == ts("2026-08-30 10:31")
    assert df.index[-1] == ts("2026-09-02 10:29")  # barre 10:30 en cours supprimée
    assert (df.index.to_series().diff().dropna() == MIN).all()
    expected = np.array([_price(t + MIN) for t in df.index])
    np.testing.assert_allclose(df["close"].to_numpy(), expected)
    assert sorted(fake.bulk_calls()) == sorted(
        D._kline_url("BTCUSDT", "1m", d, daily=True)
        for d in ("2026-08-30", "2026-08-31", "2026-09-01"))
    api = fake.api_calls()
    assert api[0]["startTime"] == _epoch(ts("2026-09-01"), "ms")
    assert len(api) == 3  # 2071 barres -> 3 pages de 1000 max
    assert not (D.CACHE_DIR / "BTCUSDT_1m.parquet").exists()


@pytest.mark.parametrize("monthly_published", [True, False])
def test_fetch_ohlcv_monthly_zip_and_fallback(offline, monthly_published):
    now = ts("2026-09-10 05:20")
    fake = offline(FakeBinance(
        now, interval="1h", published_months={"2026-08"} if monthly_published else set(),
        published_days=set(days_between("2026-08-01", "2026-09-08"))))
    df = D.fetch_ohlcv("SOLUSDT", "1h", days=40, cache=False)
    assert_contract(df)
    assert df.index[0] == ts("2026-08-01 06:00") and df.index[-1] == ts("2026-09-10 04:00")
    assert (df.index.to_series().diff().dropna() == HOUR).all()
    monthly = [u for u in fake.bulk_calls() if "/monthly/" in u]
    daily = [u for u in fake.bulk_calls() if "/daily/" in u]
    assert len(monthly) == 1
    assert len(daily) == (9 if monthly_published else 31 + 9)
    assert fake.api_calls()[0]["startTime"] == _epoch(ts("2026-09-09"), "ms")


def test_fetch_ohlcv_cache_is_incremental(offline):
    now0 = ts("2026-09-02 10:30:30")
    fake = offline(FakeBinance(now0, published_days=set(days_between("2026-08-25", "2026-09-01"))))
    path = D.CACHE_DIR / "BTCUSDT_1m.parquet"

    first = D.fetch_ohlcv("BTCUSDT", "1m", days=1)
    assert path.exists()
    n_calls = len(fake.calls)
    assert fake.bulk_calls() == [D._kline_url("BTCUSDT", "1m", "2026-09-01", daily=True)]

    # même requête : tout vient du cache
    again = D.fetch_ohlcv("BTCUSDT", "1m", days=1)
    assert len(fake.calls) == n_calls
    pd.testing.assert_frame_equal(first, again)

    # 20 minutes plus tard : seule la fin manque -> une requête API à partir de 10:30
    fake.now = ts("2026-09-02 10:50:10")
    later = D.fetch_ohlcv("BTCUSDT", "1m", days=1)
    new_calls = fake.calls[n_calls:]
    assert [u for u, _ in new_calls] == [D.API_KLINES_URL]
    assert new_calls[0][1]["startTime"] == _epoch(ts("2026-09-02 10:30"), "ms")
    assert later.index[-1] == ts("2026-09-02 10:49")
    assert later.index[0] == ts("2026-09-01 10:51")
    n_calls = len(fake.calls)

    # 3 jours : seul le début manque (30 et 31 août), aucune requête API
    longer = D.fetch_ohlcv("BTCUSDT", "1m", days=3)
    new_urls = [u for u, _ in fake.calls[n_calls:]]
    assert sorted(new_urls) == sorted(D._kline_url("BTCUSDT", "1m", d, daily=True)
                                      for d in ("2026-08-30", "2026-08-31"))
    assert longer.index[0] == ts("2026-08-30 10:51") and longer.index[-1] == ts("2026-09-02 10:49")
    assert (longer.index.to_series().diff().dropna() == MIN).all()
    pd.testing.assert_frame_equal(longer.loc[later.index], later)

    # métadonnées : un seul intervalle couvert, du 30/08 00:00 à 10:50 (dernière barre clôturée)
    import pyarrow.parquet as pq
    meta = json.loads(pq.read_table(path).schema.metadata[b"tradebot"])
    assert meta["covered"] == [[ts("2026-08-30").isoformat(), ts("2026-09-02 10:50").isoformat()]]


def test_cache_does_not_refetch_genuine_exchange_gap(offline):
    gap = (ts("2026-09-01 12:40"), ts("2026-09-01 14:00"))  # panne de 80 min
    fake = offline(FakeBinance(ts("2026-09-02 01:00:05"),
                               published_days={"2026-08-31", "2026-09-01"}, gap=gap))
    df = D.fetch_ohlcv("BTCUSDT", "1m", days=1)
    assert not ((df.index >= gap[0]) & (df.index < gap[1])).any()
    assert df.index.to_series().diff().max() == pd.Timedelta(minutes=81)
    n = len(fake.calls)
    D.fetch_ohlcv("BTCUSDT", "1m", days=1)
    assert len(fake.calls) == n


def test_fetch_ohlcv_end_in_the_past(offline):
    fake = offline(FakeBinance(ts("2026-09-10 12:00"),
                               published_days=set(days_between("2026-09-01", "2026-09-09"))))
    df = D.fetch_ohlcv("ETHUSDT", "1m", days=2, end=ts("2026-09-05 06:00"))
    assert df.index[0] == ts("2026-09-03 06:00") and df.index[-1] == ts("2026-09-05 05:59")
    assert not fake.api_calls()


def test_interval_set_helpers():
    a, b, c, d = ts("2026-01-01"), ts("2026-01-02"), ts("2026-01-03"), ts("2026-01-04")
    assert D._ivl_union([(c, d), (a, b), (b, c)]) == [(a, d)]
    assert D._ivl_subtract((a, d), [(b, c)]) == [(a, b), (c, d)]
    assert D._ivl_subtract((a, c), [(a, d)]) == []
    assert D._ivl_from_json(D._ivl_to_json([(a, b)])) == [(a, b)]


# ---------------------------------------------------------------------------
# Dérivés
# ---------------------------------------------------------------------------

def _metrics_raw(rows):
    return D._read_csv(D._csv_bytes_from_zip(make_zip(rows, header=METRICS_HEADER)),
                       D.METRICS_CSV_FIELDS, by_header=True)


def test_metrics_parsing_availability_and_renaming():
    rows = [
        ["2026-09-23 00:10:00", "BTCUSDT", 3.0, 30.0, 1.2, 2.2, 0.92, 1.3],
        ["2026-09-23 00:00:00", "BTCUSDT", 1.0, 10.0, 1.0, 2.0, 0.90, 1.1],
        ["2026-09-23 00:05:00", "BTCUSDT", 2.0, 20.0, "", "", 0.91, 1.2],  # top traders vides
        ["2026-09-23 00:05:00", "BTCUSDT", 2.0, 20.0, "", "", 0.91, 1.2],  # doublon
    ]
    m = D._metrics_to_frame(_metrics_raw(rows))
    assert list(m.index) == [ts("2026-09-23 00:05"), ts("2026-09-23 00:10"), ts("2026-09-23 00:15")]
    assert list(m.columns) == ["open_interest", "open_interest_value", "ls_ratio_top",
                               "ls_ratio_accounts", "ls_ratio_top_accounts", "taker_ls_ratio"]
    assert m["open_interest"].tolist() == [1.0, 2.0, 3.0]
    assert m["ls_ratio_top"].tolist()[0] == 2.0  # sum_toptrader = positions
    assert m["ls_ratio_top_accounts"].tolist()[0] == 1.0  # count_toptrader = comptes
    assert m["ls_ratio_accounts"].tolist()[0] == 0.90  # count_long_short = tous les comptes
    assert np.isnan(m["ls_ratio_top"].iloc[1])


def test_funding_parsing_ceil_to_second():
    rows = [[1785542400001, 8, "0.00004123"], [1785571200000, 8, "0.00003163"]]
    raw = D._read_csv(D._csv_bytes_from_zip(make_zip(rows, header=FUNDING_HEADER)),
                      D.FUNDING_CSV_FIELDS, by_header=True)
    f = D._funding_to_frame(raw)
    assert list(f.index) == [ts("2026-08-01 00:00:01"), ts("2026-08-01 08:00")]
    assert f["funding_rate"].tolist() == [0.00004123, 0.00003163]


def _bars(start: str, n: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC", name="open_time")
    return pd.DataFrame({"close": np.linspace(100, 101, n)}, index=idx)


def test_attach_derivatives_boundary_no_leak():
    df = _bars("2026-09-23 10:00", 10)
    # fenêtre metrics [10:00, 10:05) -> disponible à 10:05 ; une autre disponible à 10:06:30
    deriv = pd.DataFrame({"open_interest": [1.0, 2.0]},
                         index=pd.DatetimeIndex([ts("2026-09-23 10:05"),
                                                 ts("2026-09-23 10:06:30")]))
    out = D.attach_derivatives(df, deriv)
    oi = out["open_interest"]
    # barre 10:04 (clôture 10:05) : la mesure disponible à 10:05 est interdite (convention prudente)
    assert np.isnan(oi[ts("2026-09-23 10:04")])
    assert oi[ts("2026-09-23 10:05")] == 1.0
    # disponible en cours de barre 10:06 -> utilisable seulement à partir de la barre 10:07
    assert oi[ts("2026-09-23 10:06")] == 1.0
    assert oi[ts("2026-09-23 10:07")] == 2.0
    assert set(D.DERIVATIVE_COLUMNS) <= set(out.columns)
    assert out["funding_rate"].isna().all()


def test_attach_derivatives_random_measurements_never_leak():
    rng = np.random.default_rng(42)
    df = _bars("2026-09-23 00:00", 720)
    secs = np.sort(rng.choice(np.arange(-3600, 13 * 3600), size=120, replace=False))
    avail = ts("2026-09-23") + pd.to_timedelta(secs, unit="s")
    # valeur = instant de disponibilité (secondes epoch) : permet de vérifier l'absence de fuite
    deriv = pd.DataFrame({"open_interest": avail.asi8 / 1e9, "funding_rate": np.nan},
                         index=avail)
    out = D.attach_derivatives(df, deriv)
    t_sec = df.index.asi8 / 1e9
    v = out["open_interest"].to_numpy()
    ok = ~np.isnan(v)
    assert ok.any()
    assert (v[ok] <= t_sec[ok]).all()  # jamais une mesure disponible après l'ouverture de la barre
    assert (t_sec[ok] - v[ok] <= 3600).all()  # tolérance 1 h
    a_sec = avail.asi8 / 1e9
    for i in range(len(df)):  # c'est bien la dernière mesure disponible
        eligible = a_sec[a_sec <= t_sec[i]]
        if len(eligible) and t_sec[i] - eligible[-1] <= 3600:
            assert v[i] == eligible[-1]
        else:
            assert np.isnan(v[i])
    # causalité : ajouter des mesures futures ou tronquer les barres ne change rien au passé
    t_cut = df.index[300]
    future = pd.DataFrame({"open_interest": [-1.0]}, index=[t_cut + pd.Timedelta(seconds=1)])
    out2 = D.attach_derivatives(df.loc[:t_cut], pd.concat([deriv, future]))
    np.testing.assert_array_equal(out2["open_interest"].to_numpy(), v[:301])


def test_attach_derivatives_tolerances_and_nan_not_propagated():
    df = _bars("2026-09-23 00:00", 12 * 60)
    deriv = pd.DataFrame(
        {"funding_rate": [0.0001, np.nan], "open_interest": [np.nan, 5.0],
         "ls_ratio_top": [np.nan, np.nan]},
        index=pd.DatetimeIndex([ts("2026-09-23 00:00:01"), ts("2026-09-23 01:00")]))
    deriv = pd.concat([deriv, pd.DataFrame({"open_interest": [np.nan], "ls_ratio_top": [2.0]},
                                           index=[ts("2026-09-23 01:30")])])
    out = D.attach_derivatives(df, deriv)
    fr = out["funding_rate"]
    assert np.isnan(fr[ts("2026-09-23 00:00")])  # 00:00:01 > ouverture 00:00
    assert fr[ts("2026-09-23 00:01")] == 0.0001
    assert fr[ts("2026-09-23 09:00")] == 0.0001  # 8 h 59 min 59 s : dans la tolérance de 9 h
    assert np.isnan(fr[ts("2026-09-23 09:01")])
    oi = out["open_interest"]
    assert oi[ts("2026-09-23 01:29")] == 5.0
    assert np.isnan(oi[ts("2026-09-23 01:30")])  # mesure plus récente mais NaN : pas de report
    assert np.isnan(oi[ts("2026-09-23 00:59")])
    assert out["ls_ratio_top"][ts("2026-09-23 02:30")] == 2.0
    assert np.isnan(out["ls_ratio_top"][ts("2026-09-23 02:31")])  # > 1 h
    # idempotent : ré-attacher remplace les colonnes
    again = D.attach_derivatives(out, deriv)
    pd.testing.assert_frame_equal(again, out)
    # sans dérivés : colonnes NaN
    empty = D.attach_derivatives(df, None)
    assert empty[list(D.DERIVATIVE_COLUMNS)].isna().all().all()


def test_fetch_derivatives_offline_cache(offline):
    now = ts("2026-09-03 12:00")
    fake = offline(FakeBinance(now, metrics_days={"2026-08-30", "2026-08-31", "2026-09-01"},
                               funding_months={"2026-08"}))
    deriv = D.fetch_derivatives("BTCUSDT", ts("2026-08-31"), now)
    assert list(deriv.columns) == list(D.DERIVATIVE_COLUMNS)
    assert deriv.index.is_monotonic_increasing and not deriv.index.has_duplicates
    assert deriv.index.name == "available_time"
    metrics_urls = fake.bulk_calls("metrics")
    assert sorted(u.rsplit("-metrics-", 1)[1] for u in metrics_urls) == [
        "2026-08-30.zip", "2026-08-31.zip", "2026-09-01.zip", "2026-09-02.zip"]
    assert len(fake.bulk_calls("fundingRate")) == 1
    # disponibilité = create_time + 5 min : la ligne 00:00 du 31/08 est indexée 00:05
    assert deriv.loc[ts("2026-08-31 00:05"), "open_interest"] == 1000.0
    # lignes funding (le 1er août 00:00:00.001 -> 00:00:01 : hors fenêtre demandée)
    fr = deriv["funding_rate"].dropna()
    assert fr.index.max() == ts("2026-08-31 16:00") and fr.index.min() >= ts("2026-08-30 15:00")
    # une même seconde porte metrics + funding (31/08 16:00) : une seule ligne fusionnée
    row = deriv.loc[ts("2026-08-31 16:00")]
    assert not np.isnan(row["funding_rate"]) and not np.isnan(row["open_interest"])

    # 2e appel : seul le 02/09 (pas encore publié) est retenté ; funding d'août en cache
    n = len(fake.calls)
    fake.metrics_days.add("2026-09-02")
    deriv2 = D.fetch_derivatives("BTCUSDT", ts("2026-08-31"), now)
    assert [u.rsplit("-metrics-", 1)[1] for u, _ in fake.calls[n:]] == ["2026-09-02.zip"]
    assert deriv2.index.max() == ts("2026-09-03 00:00")  # 02/09 23:55 + 5 min
    n = len(fake.calls)
    D.fetch_derivatives("BTCUSDT", ts("2026-08-31"), now)
    assert len(fake.calls) == n

    # alignement sur les barres : funding d'août valable jusqu'à 01:00 le 1er sept.
    bars = _bars("2026-08-31 23:00", 180)
    out = D.attach_derivatives(bars, deriv2)
    assert out.loc[ts("2026-09-01 01:00"), "funding_rate"] == pytest.approx(0.0001 * 93)
    assert np.isnan(out.loc[ts("2026-09-01 01:01"), "funding_rate"])
    assert out["open_interest"].notna().all()


def test_fetch_derivatives_old_missing_file_is_not_retried(offline):
    fake = offline(FakeBinance(ts("2026-09-20 12:00"), metrics_days=set(), funding_months=set()))
    D.fetch_derivatives("NEWUSDT", ts("2026-08-01"), ts("2026-08-03"))
    n = len(fake.calls)
    assert n > 0
    D.fetch_derivatives("NEWUSDT", ts("2026-08-01"), ts("2026-08-03"))
    assert len(fake.calls) == n  # fichiers absents depuis > 7 jours : période considérée traitée


# ---------------------------------------------------------------------------
# Sessions, nettoyage, univers
# ---------------------------------------------------------------------------

def test_add_session_utc_for_crypto():
    idx = pd.DatetimeIndex([ts("2026-09-23 23:58"), ts("2026-09-23 23:59"),
                            ts("2026-09-24 00:00"), ts("2026-09-24 00:01")])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    import datetime as dt

    s = D.add_session(df, "BTCUSDT")["session"].tolist()
    assert s == [dt.date(2026, 9, 23)] * 2 + [dt.date(2026, 9, 24)] * 2
    assert D.add_session(df)["session"].tolist() == s  # index UTC sans ticker
    # même crypto avec un index affiché à Paris : la session reste la date UTC
    paris = df.tz_convert("Europe/Paris")
    assert D.add_session(paris, "SOLUSDT")["session"].tolist() == s
    # actions : date locale de la place
    ny = pd.DataFrame({"close": [1.0]},
                      index=pd.DatetimeIndex([pd.Timestamp("2026-09-23 20:30", tz="America/New_York")]))
    assert D.add_session(ny, "SPY")["session"].tolist() == [dt.date(2026, 9, 23)]


def test_clean_ohlcv():
    idx = pd.DatetimeIndex(["2026-09-23 00:02", "2026-09-23 00:00", "2026-09-23 00:01",
                            "2026-09-23 00:01", "2026-09-23 00:03"])
    df = pd.DataFrame({"open": [1, 1, 1, 2, np.nan], "high": [1, 1, 0.5, 2, 1],
                       "low": [1, 1, 0.9, 2, 1], "close": [1, 1, 1.1, 2, 1],
                       "volume": [1, -1, 2, 3, 4], "session": list("abcde")}, index=idx)
    out = D.clean_ohlcv(df)
    assert str(out.index.tz) == "UTC" and out.index.is_monotonic_increasing
    assert len(out) == 3  # doublon 00:01 (garde le dernier) + ligne NaN supprimés
    assert out.loc[ts("2026-09-23 00:01"), "close"] == 2.0
    assert np.isnan(out.loc[ts("2026-09-23 00:00"), "volume"])
    assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()
    assert out["open"].dtype == np.float64 and "session" in out.columns


def test_load_universe_logs_errors_per_ticker(monkeypatch, caplog):
    good = _bars("2026-09-23 00:00", 30).assign(open=100.0, high=102.0, low=99.0, volume=1.0)

    def fake_fetch(ticker, interval="1m", days=365, cache=True, end=None):
        if ticker == "BADUSDT":
            raise D.DataSourceError("symbole inconnu")
        return good.copy()

    def broken_derivs(*a, **k):
        raise D.DataSourceError("futures indisponibles")

    monkeypatch.setattr(D, "fetch_ohlcv", fake_fetch)
    monkeypatch.setattr(D, "fetch_derivatives", broken_derivs)
    with caplog.at_level(logging.WARNING, logger="tradebot.data"):
        out = D.load_universe(["BTCUSDT", "BADUSDT"], "1m", 1, cache=False)
    assert list(out) == ["BTCUSDT"]
    assert "BADUSDT" in caplog.text and "dérivés indisponibles" in caplog.text
    df = out["BTCUSDT"]
    assert "session" in df.columns and set(D.DERIVATIVE_COLUMNS) <= set(df.columns)
    assert df["open_interest"].isna().all()


def test_yahoo_multiindex_columns_are_flattened():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-24 13:30", tz="UTC")])
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open", "Volume"], ["SPY"]],
                                      names=["Price", "Ticker"])
    raw = pd.DataFrame([[1.0, 2.0, 0.5, 1.5, 10]], index=idx, columns=cols)
    out = D._normalize_yahoo(raw, "SPY")
    assert list(out.columns) == list(D.OHLCV_COLUMNS)
    assert str(out.index.tz) == "America/New_York"
    assert out.index[0] == pd.Timestamp("2026-09-24 09:30", tz="America/New_York")


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_network_btc_1m_two_days(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "CACHE_DIR", tmp_path)
    df = D.fetch_ohlcv("BTCUSDT", "1m", days=2)
    now = pd.Timestamp.now(tz="UTC")
    assert_contract(df)
    assert 2870 <= len(df) <= 2880
    assert df.index[-1] + MIN <= now  # dernière barre clôturée
    assert now - df.index[-1] < pd.Timedelta(minutes=3)  # complément API bien fait
    assert (df.index.to_series().diff().dropna() == MIN).all()
    assert (df["high"] >= df["low"]).all() and (df["close"] > 0).all()
    assert (df["taker_buy_volume"] <= df["volume"] + 1e-12).all()
    assert (tmp_path / "BTCUSDT_1m.parquet").exists()
    # 2e appel : cache + au plus quelques minutes d'API
    df2 = D.fetch_ohlcv("BTCUSDT", "1m", days=2)
    common = df.index.intersection(df2.index)
    assert len(common) > 2800
    pd.testing.assert_frame_equal(df.loc[common], df2.loc[common])


@pytest.mark.network
def test_network_derivatives_two_days(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "CACHE_DIR", tmp_path)
    now = pd.Timestamp.now(tz="UTC")
    deriv = D.fetch_derivatives("BTCUSDT", now - pd.Timedelta(days=2), now)
    assert list(deriv.columns) == list(D.DERIVATIVE_COLUMNS)
    assert deriv["open_interest"].notna().sum() >= 288  # au moins un jour publié
    assert (deriv.index < now).all() and deriv.index.is_monotonic_increasing
    assert (deriv["open_interest_value"] / deriv["open_interest"]).dropna().between(1e3, 1e6).all()
    bars = D.fetch_ohlcv("BTCUSDT", "1m", days=2)
    out = D.attach_derivatives(bars, deriv)
    assert out["open_interest"].notna().mean() > 0.3
    # funding : fenêtre de 2 jours dans un mois révolu (fichier mensuel publié)
    m0 = D._month_start(now) - pd.Timedelta(days=20)
    fr = D.fetch_derivatives("BTCUSDT", m0, m0 + pd.Timedelta(days=2))["funding_rate"].dropna()
    assert len(fr) >= 6 and fr.abs().max() < 0.01


@pytest.mark.network
def test_network_yahoo_spy_1m():
    df = D.fetch_ohlcv("SPY", "1m", days=5, cache=False)
    assert len(df) > 100
    assert str(df.index.tz) == "America/New_York"
    assert list(df.columns) == list(D.OHLCV_COLUMNS)
    mins = df.index.hour * 60 + df.index.minute
    assert ((mins >= 9 * 60 + 30) & (mins < 16 * 60)).all()  # prepost=False
    s = D.add_session(df, "SPY")["session"]
    assert s.nunique() >= 1
