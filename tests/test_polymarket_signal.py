"""Tests hors ligne de tradebot.polymarket_signal (commande ``polymarket-signal``) : faux client
Polymarket, fausses klines Binance et horloge simulée ; aucun accès réseau.

On vérifie la convention de prix (p(u) = close de la bougie 1 s ouverte à u − 1), la phase, les
moyennes (TWAP partiel, K, somme finale), la décision preneur, la boucle (bascule de marché,
appels incrémentaux), le journal CSV et son second passage, et l'intégration dans la CLI."""

from __future__ import annotations

import io
import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from tradebot import cli
from tradebot import polymarket_signal as ps
from tradebot.polymarket import book_summary, parse_market
from tradebot.polymarket_formula import fair_prob_up, taker_edge

S0 = 1_790_417_700            # début d'un créneau 5m (multiple de 300)
D = 300
FEES = {"rate": 0.07, "exponent": 1, "takerOnly": True, "rebateRate": 0.2}


def wiggle(u):
    """Prix sans tendance : sinusoïde + petit bruit déterministe (σ > 0 sur les bougies)."""
    return 84_000.0 * math.exp(2e-4 * math.sin(u / 17.0) + 2e-5 * (((u * 7919) % 13) - 6))


def rising(u):
    """Hausse régulière de 0,2 pb/s à partir de S0 − 120 s, plus la même ondulation."""
    return wiggle(u) * math.exp(2e-5 * max(u - (S0 - 120), 0))


class Clock:
    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


class FakeBinance:
    """Imite /api/v3/klines : 1 s -> seulement les bougies closes ; 1 m -> la bougie en cours
    est renvoyée aussi (elle doit être écartée). ``lag_s`` : flux en retard."""

    def __init__(self, clock, price=wiggle, lag_s: float = 0.0):
        self.clock, self.price, self.lag_s = clock, price, lag_s
        self.calls: list[tuple] = []

    def _bar(self, o: int, step: int) -> list:
        path = [self.price(u) for u in range(o, o + step + 1)]
        return [o * 1000, str(path[0]), str(max(path)), str(min(path)), str(path[-1]), "1.0",
                (o + step) * 1000 - 1, "1.0", 10, "0.5", "0.5", "0"]

    def __call__(self, symbol, interval, *, limit=120, start_ms=None, end_ms=None):
        self.calls.append((symbol, interval, limit, start_ms, end_ms))
        step = 1 if interval == "1s" else 60
        now = self.clock() - self.lag_s
        last_open = int(now // step) * step - (step if interval == "1s" else 0)
        if start_ms is not None:
            o0 = -(-(start_ms // 1000) // step) * step
            opens = [o for o in range(o0, last_open + 1, step) if end_ms is None or o * 1000 <= end_ms][:limit]
        else:
            opens = list(range(last_open - (limit - 1) * step, last_open + 1, step))
        return [self._bar(o, step) for o in opens]


def make_market(start: int, accepting: bool = True):
    iso = lambda s: pd.Timestamp(s, unit="s", tz="UTC").isoformat()  # noqa: E731
    return parse_market({
        "slug": f"btc-updown-5m-{start}", "eventStartTime": iso(start), "endDate": iso(start + D),
        "outcomes": '["Up", "Down"]', "clobTokenIds": f'["U{start}", "D{start}"]', "outcomePrices": '["0.5", "0.5"]',
        "feesEnabled": True, "feeSchedule": FEES, "acceptingOrders": accepting, "closed": False,
    })


def raw_book(bids, asks, ts_s=S0):
    return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks], "timestamp": str(int(ts_s * 1000))}


class FakeClient:
    """current_and_next_markets + order_book ; carnets par défaut : Up 0,55/0,60, Down 0,40/0,45."""

    def __init__(self, books: dict | None = None):
        self.books = books or {}
        self.market_calls: list[int] = []
        self.book_calls: list[str] = []

    def current_and_next_markets(self, asset, duration, n_next=1, now=None):
        t = int(now.timestamp())
        self.market_calls.append(t)
        s = t // D * D
        return [make_market(s), make_market(s + D)]

    def order_book(self, token_id):
        self.book_calls.append(token_id)
        default = raw_book([(0.55, 100)], [(0.60, 50)]) if token_id.startswith("U") else \
            raw_book([(0.40, 50)], [(0.45, 100)])
        return book_summary(self.books.get(token_id, default), token_id=token_id)


def logp_of(price, a: int, b: int) -> pd.Series:
    """log p(u) pour u dans [a, b]."""
    u = np.arange(a, b + 1)
    return pd.Series(np.log([price(x) for x in u]), index=u)


# ---------------------------------------------------------------------------
# Convention de prix, phase, moyennes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("t,expected", [
    (S0 - 61, 1), (S0 - 60, 1), (S0 - 59, 2), (S0, 2), (S0 + 1, 3), (S0 + D - 60, 3), (S0 + D - 59, 4),
    (S0 + D, 4)])
def test_phase_boundaries(t, expected):
    assert ps.phase_of(t, S0, S0 + D) == expected


def test_parse_klines_drops_open_bars_and_buffer_convention():
    clock = Clock(S0 + 30.4)
    fb = FakeBinance(clock)
    bars = ps.parse_klines(fb("BTCUSDT", "1m", limit=3), clock())
    assert list(bars.index) == [S0 - 60, S0 - 120][::-1]          # la bougie ouverte à S0 est écartée
    b1s = ps.parse_klines(fb("BTCUSDT", "1s", limit=5), clock())
    assert list(b1s.index) == list(range(S0 + 25, S0 + 30))
    buf = ps.PriceBuffer()
    buf.add_bars(b1s.drop(index=S0 + 27))                          # un trou : comblé par le dernier prix
    lp = buf.log_series()
    assert buf.last == S0 + 30 and list(lp.index) == list(range(S0 + 26, S0 + 31))
    assert lp[S0 + 30] == pytest.approx(math.log(wiggle(S0 + 30)))   # p(u) = close de la bougie ouverte à u − 1
    assert lp[S0 + 28] == lp[S0 + 27]


def test_formula_inputs_each_phase():
    S, E = S0, S0 + D
    lp = logp_of(wiggle, S - 200, E)
    x = lambda a, b: np.array([math.log(wiggle(u)) for u in range(a, b + 1)])  # noqa: E731
    i1 = ps.formula_inputs(lp, S - 60, S, E)
    assert i1.phase == 1 and i1.start_avg is None and i1.price_to_beat is None
    i2 = ps.formula_inputs(lp, S - 20, S, E)
    assert i2.phase == 2 and i2.start_avg == pytest.approx(x(S - 59, S - 20).mean(), abs=1e-12)
    k = x(S - 59, S).mean()
    assert ps.formula_inputs(lp, S, S, E).start_avg == pytest.approx(k, abs=1e-12)   # à S : Ā = K
    i3 = ps.formula_inputs(lp, S + 100, S, E)
    assert i3.phase == 3 and i3.price_to_beat == pytest.approx(k, abs=1e-12)
    i4 = ps.formula_inputs(lp, E - 15, S, E)
    assert i4.phase == 4 and i4.end_partial_sum == pytest.approx(x(E - 59, E - 15).sum() / 60, abs=1e-12)
    assert i4.log_price == pytest.approx(math.log(wiggle(E - 15)))
    # K fourni (mis en cache) : prioritaire ; fenêtre non couverte : erreur explicite
    assert ps.formula_inputs(lp, S + 100, S, E, k_log=0.123).price_to_beat == 0.123
    with pytest.raises(ps.MissingPrices):
        ps.formula_inputs(lp.loc[S - 30:], S - 10, S, E)
    with pytest.raises(ps.MissingPrices):
        ps.formula_inputs(lp.loc[S + 10:], S + 100, S, E)


def test_phase2_matches_partial_twap_formula():
    """m = (a/L)(p − Ā) : écart prix − TWAP partiel, pondéré par la part de moyenne réalisée."""
    S, E = S0, S0 + D
    lp = logp_of(rising, S - 200, S)
    t = S - 20
    inp = ps.formula_inputs(lp, t, S, E)
    fv = fair_prob_up(t, S, E, inp.log_price, 1e-4, **inp.kwargs())
    assert fv.phase == 2
    assert fv.mean == pytest.approx((40 / 60) * (lp[t] - lp.loc[S - 59:t].mean()), rel=1e-9)
    assert fv.mean > 0 and fv.prob_up > 0.5                     # prix au-dessus de sa moyenne : Up favorisé


def test_sigma_methods():
    clock = Clock(S0 + 10.4)
    fb = FakeBinance(clock)
    b1m = ps.parse_klines(fb("BTCUSDT", "1m", limit=61), clock())
    assert len(b1m) == 60
    sp = ps.sigma_from("parkinson", bars1m=b1m)
    buf = ps.PriceBuffer()
    buf.add_bars(ps.parse_klines(fb("BTCUSDT", "1s", limit=1000), clock()))
    se = ps.sigma_from("ewma", logp=buf.log_series())
    assert 0 < sp < 1e-3 and 0 < se < 1e-3
    assert math.isnan(ps.sigma_from("parkinson", bars1m=None))
    with pytest.raises(ValueError):
        ps.sigma_from("garch")


# ---------------------------------------------------------------------------
# Carnet et décision
# ---------------------------------------------------------------------------
def test_decide_rules():
    ev_up, ev_dn, side = ps.decide(0.7, 0.60, 0.42)
    assert ev_up == pytest.approx(0.7 - 0.60 - 0.07 * 0.60 * 0.40)
    assert ev_dn == pytest.approx(0.3 - 0.42 - 0.07 * 0.42 * 0.58)
    assert side == "up"
    assert ps.decide(0.3, 0.30, 0.62)[2] == "down"
    assert ps.decide(0.5, 0.50, 0.51)[2] is None                            # frais : 0,5 ne suffit pas
    assert ps.decide(0.7, 0.60, 0.42, min_edge=0.10)[2] is None             # marge exigée
    ev_up, ev_dn, side = ps.decide(0.2, math.nan, 0.70)                     # pas d'ask Up
    assert math.isnan(ev_up) and side == "down"
    assert ps.decide(math.nan, 0.4, 0.4)[2] is None
    assert math.isnan(ps.decide(0.9, 1.0, 0.2)[0])                          # ask à 1 : exclu


@pytest.mark.parametrize("seed", range(5))
def test_decide_matches_taker_edge(seed):
    rng = np.random.default_rng(seed)
    p, a_up, a_dn = rng.uniform(0.02, 0.98, 3)
    ev_up, ev_dn, side = ps.decide(p, a_up, a_dn)
    ref = taker_edge(p, a_up, a_dn)
    assert ev_up == pytest.approx(ref["ev_up"]) and ev_dn == pytest.approx(ref["ev_down"])
    assert side == ref["side"]


def test_book_quotes_mirror_missing_side():
    up = book_summary(raw_book([(0.99, 100)], []))
    down = book_summary(raw_book([], []))
    q = ps.book_quotes(up, down)
    assert q["bid_up"] == 0.99 and math.isnan(q["ask_up"])
    assert q["ask_down"] == pytest.approx(0.01)                 # bid Up 0,99 = ask Down 0,01
    q2 = ps.book_quotes(book_summary(raw_book([(0.55, 1)], [(0.60, 2)])), None)
    assert q2["ask_down"] == pytest.approx(0.45) and q2["bid_down"] == pytest.approx(0.40)


def test_evaluate_market_matches_direct_formula():
    m = make_market(S0)
    lp = logp_of(wiggle, S0 - 300, S0 + 150)
    sigma = 5e-5
    bu = book_summary(raw_book([(0.45, 10)], [(0.47, 10)]))
    bd = book_summary(raw_book([(0.53, 10)], [(0.55, 10)]))
    sig = ps.evaluate_market(m, now=S0 + 150.6, logp=lp, sigma=sigma, sigma_method="parkinson", k_log=None,
                             book_up=bu, book_down=bd)
    k = lp.loc[S0 - 59:S0].mean()
    ref = fair_prob_up(S0 + 150, S0, S0 + D, lp[S0 + 150], sigma, price_to_beat=k)
    assert sig.phase == 3 and sig.p_formula == pytest.approx(ref.prob_up) and sig.sd == pytest.approx(ref.sd)
    assert sig.p_up == pytest.approx(norm.cdf(ref.mean / math.hypot(ref.sd, ps.BASIS_SD)))
    assert sig.k_binance == pytest.approx(math.exp(k))
    assert (sig.ev_up, sig.ev_down) == pytest.approx(ps.decide(sig.p_up, 0.47, 0.55)[:2])
    pure = ps.evaluate_market(m, now=S0 + 150.6, logp=lp, sigma=sigma, sigma_method="parkinson", k_log=None,
                              book_up=bu, book_down=bd, basis_sd=0.0)
    assert pure.p_up == pytest.approx(ref.prob_up) == pytest.approx(pure.p_formula)   # --basis-sd 0 : formule pure
    assert sig.role == "en cours" and sig.s_to_end == pytest.approx(149.4) and sig.binance_age_s == pytest.approx(0.6)
    line = ps.format_line(sig)
    assert "phase 3 K connu" in line and "E dans 149 s" in line and "Up 0,45/0,47" in line


def test_basis_noise_caps_confidence_at_close():
    """Cas vu en direct le 26/09 à E − 2 s : F − K Binance = +0,44 pb, s = 0,006 pb. La formule pure
    dit 100 % ; avec l'erreur du proxy Binance -> Chainlink (σ_b = 0,5 pb) : Φ(0,44/0,50) ≈ 81 %."""
    m, sd = 0.444e-4, 0.006e-4
    assert ps.prob_with_basis(m, sd, 0.0) == pytest.approx(1.0)
    assert ps.prob_with_basis(m, sd) == pytest.approx(norm.cdf(0.444 / math.hypot(0.5, 0.006)), abs=1e-9)
    assert 0.80 < ps.prob_with_basis(m, sd) < 0.82
    assert ps.prob_with_basis(0.0, 0.0, 0.0) == 1.0 and ps.prob_with_basis(-1e-9, 0.0, 0.0) == 0.0
    assert ps.prob_with_basis(0.0, 3e-4) == pytest.approx(0.5)
    # loin de la clôture, s domine : l'ajout de σ_b ne change presque rien
    assert abs(ps.prob_with_basis(2e-4, 2.5e-4) - norm.cdf(2e-4 / 2.5e-4)) < 0.01


def test_evaluate_market_stale_or_closed_means_no_trade():
    m = make_market(S0)
    lp = logp_of(rising, S0 - 300, S0 + 200)
    kw = dict(logp=lp, sigma=5e-5, sigma_method="ewma", k_log=None,
              book_up=book_summary(raw_book([(0.50, 1)], [(0.51, 1)])), book_down=None)
    assert ps.evaluate_market(m, now=S0 + 200.5, **kw).decision == "acheter Up"
    stale = ps.evaluate_market(m, now=S0 + 212, **kw)
    assert stale.decision == "rien" and "retard" in stale.note
    closed = ps.evaluate_market(make_market(S0, accepting=False), now=S0 + 200.5, **kw)
    assert closed.decision == "rien" and "fermé" in closed.note
    no_sigma = ps.evaluate_market(m, now=S0 + 200.5, **{**kw, "sigma": math.nan})
    assert math.isnan(no_sigma.p_up) and no_sigma.decision == "rien" and "σ" in no_sigma.note


# ---------------------------------------------------------------------------
# Boucle, journal, second passage
# ---------------------------------------------------------------------------
def test_tick_current_and_next(tmp_path):
    clock = Clock(S0 + 250.4)                 # t = S0 + 250 : phase 4 (en cours), phase 2 (suivant)
    books = {f"U{S0 + D}": raw_book([(0.49, 10)], [(0.50, 10)]), f"D{S0 + D}": raw_book([(0.50, 10)], [(0.51, 10)])}
    fb, client, out = FakeBinance(clock, price=rising), FakeClient(books), io.StringIO()
    logp = tmp_path / "signal.csv"
    r = ps.SignalRunner("btc", "5m", client=client, klines=fb, log_path=logp, clock=clock, out=out)
    sigs = r.tick()
    r.close()
    cur, nxt = sigs
    assert (cur.slug, cur.role, cur.phase, cur.t_info_s) == (f"btc-updown-5m-{S0}", "en cours", 4, S0 + 250)
    assert (nxt.role, nxt.phase) == ("suivant", 2)
    assert cur.p_up > 0.999 and cur.decision == "acheter Up"          # +50 pb de hausse : Up acquis
    assert cur.ev_up == pytest.approx(cur.p_up - 0.60 - 0.07 * 0.60 * 0.40)
    assert 0.5 < nxt.p_up < 0.52 and nxt.decision == "rien"
    # moyennes : K sur (S−60, S], somme finale sur (E−60, t]
    lr = np.log([rising(u) for u in range(S0 - 59, S0 + 1)])
    assert math.log(cur.k_binance) == pytest.approx(lr.mean(), abs=1e-12)
    le = np.log([rising(u) for u in range(S0 + D - 59, S0 + 251)])    # (E − 60, t] = (S' − 60, t]
    assert cur.end_partial_sum == pytest.approx(le.sum() / 60, abs=1e-12)
    assert math.log(nxt.start_avg_price) == pytest.approx(le.mean(), abs=1e-12)
    # sortie en français et journal
    text = out.getvalue()
    assert "phase 4 final" in text and "phase 2 départ" in text and "acheter Up" in text
    assert "E dans 50 s" in text and "S dans 50 s" in text and f"K btc-updown-5m-{S0}" in text
    df = ps.read_log(logp)
    assert list(df.columns) == ps.LOG_COLUMNS and len(df) == 2
    assert df["p_up"].iloc[0] == pytest.approx(cur.p_up) and df["decision"].tolist() == ["acheter Up", "rien"]
    # appels : 1 s initial (1000 bougies), 1 m pour Parkinson, 2 carnets par marché
    assert [c[1:3] for c in fb.calls[:2]] in ([("1s", 1000), ("1m", 61)], [("1m", 61), ("1s", 1000)])
    assert sorted(client.book_calls) == sorted([f"U{S0}", f"D{S0}", f"U{S0 + D}", f"D{S0 + D}"])


def test_run_loop_rolls_over_and_fetches_incrementally():
    clock = Clock(S0 + 296.4)
    fb, client, out = FakeBinance(clock), FakeClient(), io.StringIO()
    r = ps.SignalRunner("btc", "5m", client=client, klines=fb, sigma_method="ewma", clock=clock, out=out)
    n = r.run(2.0, max_seconds=9.0, sleep=clock.sleep)
    assert n == 5                                   # 296,4 · 298,4 · 300,4 · 302,4 · 304,4
    text = out.getvalue()
    for s in (S0, S0 + D, S0 + 2 * D):
        assert f"marché btc-updown-5m-{s}" in text
    assert text.count(f"K btc-updown-5m-{S0 + D}") == 1        # K calculé une fois, après S
    assert len(client.market_calls) == 2                        # gamma : au démarrage et à la bascule
    s1 = [c for c in fb.calls if c[1] == "1s"]
    assert s1[0][2] == 1000 and s1[0][3] is None
    assert all(c[3] is not None for c in s1[1:])                # ensuite : depuis la dernière seconde connue
    assert not [c for c in fb.calls if c[1] == "1m"]            # EWMA : pas de bougies 1 min
    lines = [ln for ln in text.splitlines() if "| BTC 5m" in ln]
    assert len(lines) == 10
    assert "phase 2 départ" in lines[4] and "en cours" in lines[4]   # à 300,4 : t = S, K pas encore connu


def test_run_signal_once_prints_paper_notice():
    clock = Clock(S0 + 100.4)
    out = io.StringIO()
    n = ps.run_signal("btc", "5m", once=True, client=FakeClient(), klines=FakeBinance(clock), clock=clock, out=out)
    text = out.getvalue()
    assert n == 1
    assert "SIMULATION PAPIER" in text and "aucun ordre" in text and "France" in text
    assert text.count("| BTC 5m") == 2


def test_fill_outcomes_and_summary(tmp_path):
    path = tmp_path / "signal.csv"
    clock = Clock(S0 + 250.4)
    r = ps.SignalRunner("btc", "5m", client=FakeClient(), klines=FakeBinance(clock, price=rising),
                        log_path=path, clock=clock, out=io.StringIO())
    r.tick()
    clock.t += 2
    r.tick()
    r.close()
    asked: list[str] = []

    def resolver(slug):
        asked.append(slug)
        return {"resolved_up": True, "price_to_beat": 84_000.0, "final_price": 84_050.0}

    # trop tôt (E + 60 s pas atteint) : rien
    assert ps.fill_outcomes(path, resolver, now=S0 + D + 30) == 0 and not asked
    assert ps.fill_outcomes(path, resolver, now=S0 + D + 61) == 1
    assert asked == [f"btc-updown-5m-{S0}"]                      # le marché suivant n'est pas fini
    df = ps.read_log(path)
    cur = df[df["slug"] == f"btc-updown-5m-{S0}"]
    assert (cur["resolved_up"] == 1).all() and (cur["price_to_beat_cl"] == 84_000.0).all()
    assert cur["pnl"].to_numpy() == pytest.approx(1 - (0.60 + 0.07 * 0.60 * 0.40))
    assert df.loc[df["slug"] != f"btc-updown-5m-{S0}", "resolved_up"].isna().all()
    assert ps.fill_outcomes(path, resolver, now=S0 + D + 61) == 0   # déjà résolu : pas de nouvel appel
    s = ps.summarize_log(df)
    assert s["n_resolved_markets"] == 1 and s["n_decision_markets"] == 1 and s["n_decision_rows"] == 2
    assert s["mean_pnl_first"] == pytest.approx(1 - 0.6168)
    assert math.isnan(s["ci_pnl_first"][0])                        # un seul créneau : pas d'IC
    txt = ps.format_summary(s)
    assert "Brier" in txt and "Première décision" in txt and "trop peu de créneaux" in txt


def test_grouped_bootstrap_ci():
    v = np.array([1.0, 1.2, -0.5, -0.4, 0.3, 0.2])
    g = np.array([0, 0, 1, 1, 2, 2])
    lo, hi = ps._grouped_boot_ci(v, g, n_boot=500, seed=1)
    assert lo <= v.mean() <= hi and lo >= v.min() and hi <= v.max()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_parser_and_dispatch(monkeypatch, tmp_path):
    p = cli.build_parser()
    a = p.parse_args(["polymarket-signal"])
    assert (a.asset, a.duration, a.interval, a.sigma, a.once, a.log, a.min_edge) == \
        ("btc", "5m", 1.0, "parkinson", False, None, 0.0)
    b = p.parse_args(["polymarket-signal", "--asset", "eth", "--duration", "15m", "--interval", "2",
                      "--sigma", "ewma", "--once", "--log", "x.csv", "--min-edge", "0.5"])
    assert (b.asset, b.duration, b.interval, b.sigma, b.once, b.log) == ("eth", "15m", 2.0, "ewma", True, "x.csv")
    for bad in (["polymarket-signal", "--interval", "0"], ["polymarket-signal", "--sigma", "garch"],
                ["polymarket-signal", "--duration", "1h"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)
    with pytest.raises(SystemExit) as e:
        cli.main(["polymarket-signal", "--fill-outcomes"])
    assert e.value.code == 2

    seen = {}

    def fake_run(asset, duration, **kw):
        seen.update(asset=asset, duration=duration, **kw)
        return 1

    monkeypatch.setattr(ps, "run_signal", fake_run)
    log_path = str(tmp_path / "s.csv")
    assert cli.main(["-q", "polymarket-signal", "--asset", "eth", "--interval", "2", "--once",
                     "--log", log_path, "--min-edge", "0.5", "--basis-sd", "0.3"]) == 0
    assert seen["asset"] == "eth" and seen["interval"] == 2.0 and seen["once"] is True
    assert seen["log_path"] == log_path and seen["min_edge"] == pytest.approx(0.005)
    assert seen["basis_sd"] == pytest.approx(0.3e-4)
    assert a.basis_sd == 0.5
    with pytest.raises(SystemExit):
        cli.main(["polymarket-signal", "--basis-sd", "-1"])

    def failing(*a, **k):
        raise OSError("réseau coupé")

    monkeypatch.setattr(ps, "run_signal", failing)
    assert cli.main(["-q", "polymarket-signal", "--once"]) == 2        # erreur propre, sans trace
