/* Vigie Up/Down — logique temps réel (navigateur uniquement, aucune clé, aucun ordre). */
(function () {
  "use strict";
  var F = window.Formula;
  var SYMBOLS = { btc: "BTCUSDT", eth: "ETHUSDT", sol: "SOLUSDT", xrp: "XRPUSDT" };
  var CHAINLINK = { btc: "btc/usd", eth: "eth/usd", sol: "sol/usd", xrp: "xrp/usd" };
  var DURS = { "5m": 300, "15m": 900 };
  var BINANCE_REST = "https://data-api.binance.vision/api/v3/klines";
  var BINANCE_WS = "wss://data-stream.binance.vision/ws/";
  var GAMMA = "https://gamma-api.polymarket.com/events";
  var CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
  var RTDS_WS = "wss://ws-live-data.polymarket.com";
  var KEEP_S = 1300;
  var MIN_EDGE = 0.005; // avantage minimal affiché : 0,5 c par part

  var nf = function (d) { return new Intl.NumberFormat("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d }); };
  var fmt2 = nf(2), fmt1 = nf(1), fmt0 = nf(0);
  function money(x, d) { return isFinite(x) ? (d === 0 ? fmt0 : fmt2).format(x) + " $" : "—"; }
  function pct(x) { return isFinite(x) ? fmt1.format(100 * x) + " %" : "—"; }
  function cents(x) { return isFinite(x) ? (x >= 0 ? "+" : "−") + fmt1.format(Math.abs(100 * x)) + " c" : "—"; }
  function price2(x) { return isFinite(x) ? fmt2.format(x) : "—"; }
  function hhmm(s) { var d = new Date(s * 1000); return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); }
  function hhmmss(s) { var d = new Date(s * 1000); return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  function mmss(s) { s = Math.max(0, Math.round(s)); var m = Math.floor(s / 60), r = s % 60; return m + ":" + (r < 10 ? "0" : "") + r; }
  function $(id) { return document.getElementById(id); }
  function nowS() { return Date.now() / 1000; }

  var state = {
    asset: "btc", dur: "5m",
    prices: new Map(), lastTradeMs: 0, lastPrice: NaN, bnRx: 0, bnErr: null,
    chain: new Map(), clRx: 0, clErr: null,
    markets: {}, tokenIndex: {}, pmRx: 0, pmErr: null,
    sigma: NaN, sigmaAt: 0,
    samples: [], samplesFor: null,
    history: loadHistory(), checkpoints: {},
  };
  try { var pref = JSON.parse(localStorage.getItem("vigie.pref") || "{}"); if (SYMBOLS[pref.asset]) state.asset = pref.asset; if (DURS[pref.dur]) state.dur = pref.dur; } catch (e) { /* stockage indisponible */ }

  function D() { return DURS[state.dur]; }
  function windows(t) { var d = D(), S = Math.floor(t / d) * d; return { cur: { S: S, E: S + d }, nxt: { S: S + d, E: S + 2 * d } }; }
  function slugOf(S) { return state.asset + "-updown-" + state.dur + "-" + S; }

  /* ---------------- Binance : p(u) = dernier prix échangé avant la seconde u ---------------- */
  var bnWs = null;
  function pAt(u) {
    for (var k = 0; k < 30; k++) { var v = state.prices.get(u - k); if (v !== undefined) return v; }
    return NaN;
  }
  function logWindow(a, b) { // log p(u), u dans (a, b]
    var out = [];
    for (var u = a + 1; u <= b; u++) { var v = pAt(u); if (!isFinite(v)) return null; out.push(Math.log(v)); }
    return out;
  }
  function mean(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s / a.length; }
  function backfill() {
    var sym = SYMBOLS[state.asset];
    return fetch(BINANCE_REST + "?symbol=" + sym + "&interval=1s&limit=1000")
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (rows) {
        var now = Date.now();
        rows.forEach(function (r) { if (+r[6] < now) state.prices.set(Math.floor(+r[0] / 1000) + 1, +r[4]); });
        if (!isFinite(state.lastPrice) && rows.length) state.lastPrice = +rows[rows.length - 1][4];
        state.bnErr = null;
      })
      .catch(function (e) { state.bnErr = "historique Binance indisponible (" + e.message + ")"; });
  }
  function connectBinance() {
    if (bnWs) { bnWs.onclose = null; bnWs.close(); }
    var sym = SYMBOLS[state.asset].toLowerCase();
    var ws = new WebSocket(BINANCE_WS + sym + "@aggTrade");
    bnWs = ws;
    ws.onmessage = function (ev) {
      var d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (!d || d.T === undefined) return;
      var p = +d.p, u = Math.floor(d.T / 1000) + 1;
      state.prices.set(u, p); state.lastPrice = p; state.lastTradeMs = d.T; state.bnRx = Date.now(); state.bnErr = null;
    };
    ws.onerror = function () { state.bnErr = "connexion Binance refusée"; };
    ws.onclose = function () { if (bnWs === ws) setTimeout(connectBinance, 3000); };
  }
  function prunePrices() {
    var lim = Math.floor(nowS()) - KEEP_S;
    state.prices.forEach(function (_, u) { if (u < lim) state.prices.delete(u); });
    state.chain.forEach(function (_, u) { if (u < lim) state.chain.delete(u); });
  }
  function updateSigma() {
    var t = Math.floor(nowS());
    var lp = logWindow(t - 1000, t);
    if (!lp) { // historique partiel : prendre ce qui est disponible
      lp = []; for (var u = t - 1000; u <= t; u++) { var v = pAt(u); if (isFinite(v)) lp.push(Math.log(v)); }
    }
    if (lp.length > 60) state.sigma = F.ewmaSigma(lp, F.EWMA_HALFLIFE_S);
    state.sigmaAt = t;
  }

  /* ---------------- Chainlink via RTDS ---------------- */
  var clWs = null, clPing = null;
  function connectChainlink() {
    if (clWs) { clWs.onclose = null; clWs.close(); }
    clearInterval(clPing);
    var ws = new WebSocket(RTDS_WS);
    clWs = ws;
    ws.onopen = function () {
      ws.send(JSON.stringify({ action: "subscribe", subscriptions: [{ topic: "crypto_prices_chainlink", type: "*", filters: JSON.stringify({ symbol: CHAINLINK[state.asset] }) }] }));
      clPing = setInterval(function () { if (ws.readyState === 1) ws.send("PING"); }, 5000);
    };
    ws.onmessage = function (ev) {
      if (ev.data === "PONG" || !ev.data) return;
      var d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      var pl = d && d.payload; if (!pl) return;
      if (pl.symbol && pl.symbol !== CHAINLINK[state.asset]) return;
      var pts = Array.isArray(pl.data) ? pl.data : (pl.timestamp ? [pl] : []);
      pts.forEach(function (q) { if (isFinite(+q.value)) state.chain.set(Math.round(+q.timestamp / 1000), +q.value); });
      if (pts.length) { state.clRx = Date.now(); state.clErr = null; }
    };
    ws.onerror = function () { state.clErr = "flux Chainlink inaccessible depuis ce réseau"; };
    ws.onclose = function () { if (clWs === ws) setTimeout(connectChainlink, 4000); };
  }
  function chainAvg(a, b) { // moyenne des points Chainlink sur (a, b], si ≥ 90 % présents
    var s = 0, n = 0;
    for (var u = a + 1; u <= b; u++) { var v = state.chain.get(u); if (v !== undefined) { s += v; n++; } }
    return n >= 0.9 * (b - a) ? s / n : NaN;
  }

  /* ---------------- Polymarket : marchés (gamma) et carnet (WebSocket) ---------------- */
  var pmWs = null, pmPing = null, pmTokensKey = "";
  function ensureMarket(S) {
    var slug = slugOf(S);
    if (state.markets[slug]) return state.markets[slug];
    var m = { slug: slug, S: S, E: S + D(), asset: state.asset, dur: state.dur, loading: true, fee: F.FEE_RATE,
      q: { bidUp: NaN, askUp: NaN, bidDown: NaN, askDown: NaN, askUpSize: NaN, askDownSize: NaN, ts: 0 }, outcome: null };
    state.markets[slug] = m;
    fetchMarket(m);
    return m;
  }
  function fetchMarket(m) {
    return fetch(GAMMA + "?slug=" + encodeURIComponent(m.slug))
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (evs) {
        m.loading = false;
        var ev = evs && evs[0], mk = ev && ev.markets && ev.markets[0];
        if (!mk) { m.missing = true; return; }
        var outs = JSON.parse(mk.outcomes || "[]").map(function (o) { return String(o).toLowerCase(); });
        var toks = JSON.parse(mk.clobTokenIds || "[]");
        var iu = outs.indexOf("up"), id = outs.indexOf("down");
        if (iu < 0) { iu = 0; id = 1; }
        m.tokenUp = String(toks[iu]); m.tokenDown = String(toks[id]);
        state.tokenIndex[m.tokenUp] = { m: m, side: "up" }; state.tokenIndex[m.tokenDown] = { m: m, side: "down" };
        if (mk.feeSchedule && isFinite(+mk.feeSchedule.rate)) m.fee = +mk.feeSchedule.rate;
        var prices = JSON.parse(mk.outcomePrices || "[]").map(Number);
        if (mk.closed && prices.length === 2 && (prices[iu] === 1 || prices[iu] === 0)) m.outcome = prices[iu] === 1 ? "up" : "down";
        var meta = ev.eventMetadata || {};
        if (isFinite(+meta.priceToBeat)) m.ptb = +meta.priceToBeat;
        if (isFinite(+meta.finalPrice)) m.finalPrice = +meta.finalPrice;
        state.pmErr = null;
        refreshBookSubscription();
      })
      .catch(function (e) { m.loading = false; m.err = e.message; state.pmErr = "marchés Polymarket inaccessibles depuis ce réseau"; });
  }
  function refreshBookSubscription() {
    var toks = [];
    Object.keys(state.markets).forEach(function (k) {
      var m = state.markets[k];
      if (m.tokenUp && m.asset === state.asset && m.dur === state.dur && m.E > nowS() - 5) toks.push(m.tokenUp, m.tokenDown);
    });
    toks.sort();
    var key = toks.join(",");
    if (!toks.length || key === pmTokensKey) return;
    pmTokensKey = key;
    connectBook(toks);
  }
  function applyQuote(assetId, bid, ask, askSize, ts) {
    var ref = state.tokenIndex[assetId]; if (!ref) return;
    var q = ref.m.q;
    if (ref.side === "up") { if (isFinite(bid)) q.bidUp = bid; if (isFinite(ask)) q.askUp = ask; if (isFinite(askSize)) q.askUpSize = askSize; }
    else { if (isFinite(bid)) q.bidDown = bid; if (isFinite(ask)) q.askDown = ask; if (isFinite(askSize)) q.askDownSize = askSize; }
    q.ts = ts || Date.now();
    state.pmRx = Date.now(); state.pmErr = null;
  }
  function onBookEvent(e) {
    var ts = +e.timestamp || Date.now();
    if (e.event_type === "book") {
      var bids = (e.bids || []).map(function (x) { return [+x.price, +x.size]; });
      var asks = (e.asks || []).map(function (x) { return [+x.price, +x.size]; });
      var bb = NaN, ba = NaN, bas = NaN;
      bids.forEach(function (b) { if (!(b[0] <= bb)) bb = b[0]; });
      asks.forEach(function (a) { if (!(a[0] >= ba)) { ba = a[0]; bas = a[1]; } });
      applyQuote(e.asset_id, bb, ba, bas, ts);
    } else if (e.event_type === "best_bid_ask") {
      applyQuote(e.asset_id, +e.best_bid, +e.best_ask, NaN, ts);
    } else if (e.event_type === "price_change") {
      (e.price_changes || []).forEach(function (c) {
        if (c.best_bid !== undefined || c.best_ask !== undefined) applyQuote(c.asset_id, +c.best_bid, +c.best_ask, NaN, ts);
      });
    } else if (e.event_type === "market_resolved") {
      var ref = state.tokenIndex[e.asset_id] || (e.assets_ids && state.tokenIndex[e.assets_ids[0]]);
      if (ref) setTimeout(function () { fetchMarket(ref.m); }, 2000);
    }
  }
  function connectBook(toks) {
    if (pmWs) { pmWs.onclose = null; pmWs.close(); }
    clearInterval(pmPing);
    var ws = new WebSocket(CLOB_WS);
    pmWs = ws;
    ws.onopen = function () {
      ws.send(JSON.stringify({ assets_ids: toks, type: "market", custom_feature_enabled: true }));
      pmPing = setInterval(function () { if (ws.readyState === 1) ws.send("PING"); }, 10000);
    };
    ws.onmessage = function (ev) {
      if (ev.data === "PONG" || !ev.data) return;
      var d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      (Array.isArray(d) ? d : [d]).forEach(onBookEvent);
    };
    ws.onerror = function () { state.pmErr = "carnet Polymarket inaccessible depuis ce réseau"; };
    ws.onclose = function () { if (pmWs === ws) { pmTokensKey = ""; setTimeout(refreshBookSubscription, 3000); } };
  }

  /* ---------------- Formule pour un marché à l'instant t ---------------- */
  function evaluate(m, tNow) {
    var t = Math.floor(tNow), S = m.S, E = m.E, L = F.L;
    var out = { phase: F.phaseOf(t, S, E), p: NaN, pPure: NaN, mean: NaN, sd: NaN, k: NaN, startAvg: NaN, note: "" };
    var lpNow = Math.log(isFinite(state.lastPrice) ? state.lastPrice : pAt(t));
    if (!isFinite(lpNow)) { out.note = "en attente des prix Binance"; return out; }
    if (!isFinite(state.sigma)) { out.note = "σ en cours d'estimation"; return out; }
    var o = { logPrice: lpNow };
    if (t >= E) { // fenêtre close : issue Binance (proxy)
      var kw = logWindow(S - L, S), fw = logWindow(E - L, E);
      if (kw && fw) { out.k = Math.exp(mean(kw)); out.final = Math.exp(mean(fw)); out.p = mean(fw) >= mean(kw) ? 1 : 0; out.pPure = out.p; }
      out.phase = 5; return out;
    }
    if (out.phase === 2) {
      var sw = logWindow(S - L, t); if (!sw) { out.note = "historique Binance incomplet"; return out; }
      o.startAvg = mean(sw); out.startAvg = Math.exp(o.startAvg);
    }
    if (out.phase >= 3) {
      if (m.logK === undefined) { var k = logWindow(S - L, S); if (!k) { out.note = "prix à battre inconnu (page ouverte après l'ouverture ?)"; return out; } if (t > S + 2) m.logK = mean(k); o.logK = mean(k); }
      else o.logK = m.logK;
      out.k = Math.exp(o.logK);
    }
    if (out.phase === 4) {
      var ew = logWindow(E - L, Math.min(t, E)); if (!ew) { out.note = "historique Binance incomplet"; return out; }
      o.endPartialSum = ew.reduce(function (a, b) { return a + b; }, 0) / L;
    }
    var mom = F.moments(t, S, E, state.sigma, o);
    out.mean = mom.mean; out.sd = mom.sd;
    out.p = F.probUp(mom, true); out.pPure = F.probUp(mom, false);
    return out;
  }
  function decision(m, r) {
    var q = m.q, fresh = Date.now() - q.ts < 15000;
    if (!isFinite(r.p) || r.phase === 5) return { label: "—", cls: "", ev: "", side: null };
    if (!fresh || !isFinite(q.askUp) || !isFinite(q.askDown)) return { label: "Carnet indisponible", cls: "", ev: "prix du marché inconnus", side: null };
    var mirror = Math.abs((q.bidUp + q.askDown) - 1); // bid Up + ask Down ≈ 1 dans un carnet cohérent
    var e = F.takerEdge(r.p, q.askUp, q.askDown);
    e.evUp = r.p - (q.askUp + m.fee * q.askUp * (1 - q.askUp));
    e.evDown = (1 - r.p) - (q.askDown + m.fee * q.askDown * (1 - q.askDown));
    e.side = Math.max(e.evUp, e.evDown) > MIN_EDGE ? (e.evUp >= e.evDown ? "up" : "down") : null;
    var evTxt = "Up " + cents(e.evUp) + " · Down " + cents(e.evDown) + " par part";
    if (mirror > 0.03) return { label: "Carnet en mouvement", cls: "", ev: evTxt, side: null, e: e };
    if (!e.side) return { label: "Rien : pas d'avantage", cls: "", ev: evTxt, side: null, e: e };
    return { label: (e.side === "up" ? "Up " : "Down ") + cents(e.side === "up" ? e.evUp : e.evDown), cls: e.side, ev: evTxt, side: e.side, e: e };
  }

  /* ---------------- Historique de la séance (checkpoints S−10 et E−30) ---------------- */
  function loadHistory() { try { return JSON.parse(localStorage.getItem("vigie.hist") || "[]"); } catch (e) { return []; } }
  function saveHistory() { try { localStorage.setItem("vigie.hist", JSON.stringify(state.history.slice(-200))); } catch (e) { /* ignoré */ } }
  function checkpoint(m, label, r, dec) {
    var key = m.slug + "|" + label;
    if (state.checkpoints[key] || state.history.some(function (h) { return h.key === key; })) return;
    state.checkpoints[key] = true;
    var q = m.q, mid = (q.bidUp + q.askUp) / 2;
    var cost = dec.side === "up" ? q.askUp + m.fee * q.askUp * (1 - q.askUp) : dec.side === "down" ? q.askDown + m.fee * q.askDown * (1 - q.askDown) : NaN;
    state.history.push({ key: key, slug: m.slug, S: m.S, E: m.E, asset: m.asset, dur: m.dur, at: label,
      p: r.p, mid: isFinite(mid) ? mid : null, side: dec.side, cost: isFinite(cost) ? cost : null, outcome: null });
    saveHistory();
  }
  function resolvePending() {
    var t = nowS();
    state.history.forEach(function (h) {
      if (h.outcome || t < h.E + 20) return;
      var m = state.markets[h.slug];
      if (m && m.outcome) { h.outcome = m.outcome; saveHistory(); return; }
      if (!m) { m = { slug: h.slug, S: h.S, E: h.E, asset: h.asset, dur: h.dur, q: {}, fee: F.FEE_RATE }; state.markets[h.slug] = m; }
      if (!m._next || t > m._next) { m._next = t + 20; fetchMarket(m); }
    });
  }

  /* ---------------- Rendu ---------------- */
  var css = function (v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); };
  function setPill(id, rx, err, slowMs) {
    var dot = $(id), txt = $(id + "-t"), age = Date.now() - rx;
    if (err && (!rx || age > 10000)) { dot.className = "dot off"; txt.textContent = "bloqué"; return; }
    if (!rx) { dot.className = "dot"; txt.textContent = "…"; return; }
    dot.className = "dot " + (age < slowMs ? "ok" : age < 15000 ? "slow" : "off");
    txt.textContent = age < 1000 ? fmt0.format(age) + " ms" : fmt1.format(age / 1000) + " s";
  }
  var PHASES = {
    1: ["Attente", "personne ne peut rien savoir : P = 50 %"],
    2: ["Moyenne de départ en cours", "le prix à battre se forme sur les 60 s avant S"],
    3: ["Fenêtre ouverte", "prix à battre connu, la moyenne finale n'a pas commencé"],
    4: ["Moyenne finale en cours", "le résultat se fixe sur les 60 dernières secondes"],
    5: ["Fenêtre close", "résultat en attente de publication"],
  };
  function renderCurrent(m, r, dec, t) {
    $("cur-when").textContent = hhmm(m.S) + " → " + hhmm(m.E);
    var toEnd = m.E - t;
    $("cur-cd").textContent = toEnd > 0 ? mmss(toEnd) : "0:00";
    $("cur-cdl").textContent = toEnd > 0 ? "avant la clôture" : "clôturée";
    var span = m.E - (m.S - 60), x = function (s) { return 100 * (s - (m.S - 60)) / span; };
    $("pb-a1").style.left = x(m.S - 60) + "%"; $("pb-a1").style.width = (x(m.S) - x(m.S - 60)) + "%";
    $("pb-a2").style.left = x(m.E - 60) + "%"; $("pb-a2").style.width = (x(m.E) - x(m.E - 60)) + "%";
    var xn = Math.max(0, Math.min(100, x(t)));
    $("pb-el").style.width = xn + "%"; $("pb-now").style.left = "calc(" + xn + "% - 1px)";
    $("pl-1").style.left = "0%"; $("pl-2").style.left = x(m.S) + "%"; $("pl-3").style.left = x(m.E - 60) + "%"; $("pl-4").style.left = "100%";
    var pxS = $("pb-lbls").clientWidth * x(m.S) / 100;
    $("pl-1").hidden = pxS < 64; $("pl-3").hidden = pxS < 64;
    var ph = PHASES[r.phase] || PHASES[3];
    $("cur-phase").innerHTML = "";
    var b = document.createElement("b"); b.textContent = "Phase " + Math.min(r.phase, 4) + " · " + ph[0];
    var sp = document.createElement("span"); sp.textContent = " — " + ph[1];
    $("cur-phase").append(b, sp);

    var kcl = chainAvg(m.S - 60, m.S), pcl = state.chain.get(Math.floor(t) - 1) || state.chain.get(Math.floor(t) - 2);
    $("cur-k").textContent = isFinite(r.k) ? price2(r.k) : (isFinite(r.startAvg) ? price2(r.startAvg) + "…" : "—");
    $("cur-kcl").textContent = "Chainlink " + (isFinite(m.ptb) ? price2(m.ptb) : isFinite(kcl) ? price2(kcl) : (t > m.S ? "— (page ouverte après S−60 s)" : "—"));
    $("cur-p").textContent = price2(state.lastPrice);
    $("cur-pcl").textContent = "Chainlink " + (pcl ? price2(pcl) : "—");
    var gap = isFinite(r.k) ? 1e4 * Math.log(state.lastPrice / r.k) : NaN;
    $("cur-gap").textContent = isFinite(gap) ? (gap >= 0 ? "+" : "−") + fmt1.format(Math.abs(gap)) + " pb" : "—";
    $("cur-gap").style.color = isFinite(gap) ? (gap >= 0 ? css("--up") : css("--down")) : "";
    var sigWin = isFinite(state.sigma) ? 1e4 * state.sigma * Math.sqrt(D()) : NaN;
    $("cur-sig").textContent = "σ fenêtre " + (isFinite(sigWin) ? fmt1.format(sigWin) + " pb" : "—");

    renderProbBlock("cur", m, r, dec);
    var note = r.note || "";
    if (r.phase === 5) note = r.final !== undefined ? "Binance indique « " + (r.p === 1 ? "Up" : "Down") + " » (moyenne finale " + price2(r.final) + " contre " + price2(r.k) + ")." : note;
    $("cur-note").textContent = note;
    // échelle
    var q = m.q, bid = q.bidUp, ask = q.askUp;
    var mk = $("sc-mkt"), fm = $("sc-fml");
    if (isFinite(bid) && isFinite(ask)) { mk.hidden = false; mk.style.left = (100 * bid) + "%"; mk.style.width = Math.max(0.3, 100 * (ask - bid)) + "%"; } else mk.hidden = true;
    if (isFinite(r.p)) { fm.hidden = false; fm.style.left = (100 * r.p) + "%"; fm.className = "fml" + (r.p < 0.5 ? " dn" : ""); } else fm.hidden = true;
  }
  function renderProbBlock(prefix, m, r, dec) {
    var q = m.q, mid = (q.bidUp + q.askUp) / 2;
    $(prefix + "-pf").textContent = pct(r.p);
    $(prefix + "-pf-box").className = "prob formula" + (isFinite(r.p) && r.p < 0.5 ? " dn" : "");
    var p2 = $(prefix + "-pf2");
    if (p2) p2.textContent = r.phase === 1 ? "pile ou face" : ("pure " + pct(r.pPure));
    $(prefix + "-pm").textContent = pct(mid);
    $(prefix + "-pm2").textContent = "achat " + (isFinite(q.askUp) ? fmt2.format(q.askUp) : "—") + " · vente " + (isFinite(q.bidUp) ? fmt2.format(q.bidUp) : "—");
    var chip = $(prefix + "-dec");
    chip.textContent = dec.label; chip.className = "chip" + (dec.cls ? " " + dec.cls : "");
    $(prefix + "-ev").textContent = dec.ev || "";
  }
  function renderNext(m, r, dec, t) {
    $("nxt-when").textContent = hhmm(m.S) + " → " + hhmm(m.E);
    $("nxt-cd").textContent = mmss(m.S - t);
    var ph = PHASES[r.phase] || PHASES[1];
    $("nxt-phase").innerHTML = "";
    var b = document.createElement("b"); b.textContent = "Phase " + r.phase + " · " + ph[0];
    var sp = document.createElement("span"); sp.textContent = " — " + (r.phase === 2 && isFinite(r.startAvg) ? "moyenne partielle " + price2(r.startAvg) + ", prix " + price2(state.lastPrice) : ph[1]);
    $("nxt-phase").append(b, sp);
    renderProbBlock("nxt", m, r, dec);
    $("nxt-pf2").textContent = r.phase === 1 ? "pile ou face jusqu'à S−60 s" : "pure " + pct(r.pPure);
  }
  function renderHistory() {
    var rows = state.history.filter(function (h) { return h.asset === state.asset && h.dur === state.dur; }).slice(-14).reverse();
    var body = $("hist-body");
    if (!rows.length) return;
    body.innerHTML = "";
    rows.forEach(function (h) {
      var tr = document.createElement("tr");
      var res = h.outcome ? (h.outcome === "up" ? "Up" : "Down") : "en attente";
      var right = h.outcome && isFinite(h.p) ? ((h.p >= 0.5) === (h.outcome === "up")) : null;
      var pnl = h.outcome && h.side && h.cost !== null ? ((h.side === h.outcome ? 1 : 0) - h.cost) : null;
      var cells = [hhmm(h.S) + "–" + hhmm(h.E), h.at, pct(h.p), h.mid !== null ? pct(h.mid) : "—", h.side ? (h.side === "up" ? "Up" : "Down") : "rien", res, pnl !== null ? cents(pnl) : "—"];
      cells.forEach(function (c, i) {
        var td = document.createElement("td");
        if (i === 2 || i === 3 || i === 6) td.className = "r";
        if (i === 4 && h.side) { var s = document.createElement("span"); s.className = "tag " + h.side; s.textContent = c; td.append(s); }
        else if (i === 5) { var s2 = document.createElement("span"); s2.className = "tag " + (right === null ? "wait" : right ? "ok" : "ko"); s2.textContent = c + (right === null ? "" : right ? " · formule juste" : " · formule fausse"); td.append(s2); }
        else td.textContent = c;
        tr.append(td);
      });
      body.append(tr);
    });
    var all = state.history.filter(function (h) { return h.asset === state.asset && h.dur === state.dur && h.outcome; });
    var st = $("hist-stats"); st.innerHTML = "";
    ["S−10 s", "E−30 s"].forEach(function (lbl) {
      var hs = all.filter(function (h) { return h.at === lbl && isFinite(h.p); });
      if (!hs.length) return;
      var ok = hs.filter(function (h) { return (h.p >= 0.5) === (h.outcome === "up"); }).length;
      var trades = hs.filter(function (h) { return h.side && h.cost !== null; });
      var pnl = trades.reduce(function (a, h) { return a + ((h.side === h.outcome ? 1 : 0) - h.cost); }, 0);
      var d = document.createElement("div");
      var bb = document.createElement("b"); bb.textContent = fmt0.format(100 * ok / hs.length) + " % juste";
      d.append(bb, document.createTextNode("à " + lbl + " (" + hs.length + " fenêtres) · P&L papier " + (trades.length ? cents(pnl / 1) + " sur " + trades.length + " achats" : "aucun achat")));
      st.append(d);
    });
    $("hist-n").textContent = all.length + " résolues";
  }

  /* ---------------- Graphiques (canvas) ---------------- */
  function setupCanvas(c) {
    var dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    var g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    return { g: g, w: w, h: h };
  }
  function niceTicks(lo, hi, n) {
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / n))), err = span / n / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var out = []; for (var v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
    return { ticks: out, step: step };
  }
  function drawPrice(m, r, t) {
    var cv = setupCanvas($("cPrice")), g = cv.g, W = cv.w, H = cv.h;
    var padL = 64, padR = 12, padT = 12, padB = 24;
    var x0 = m.S - 90, x1 = m.E + 10;
    var X = function (s) { return padL + (W - padL - padR) * (s - x0) / (x1 - x0); };
    var pts = [], lo = Infinity, hi = -Infinity, tEnd = Math.min(Math.floor(t), m.E + 10);
    for (var u = x0; u <= tEnd; u++) { var v = pAt(u); if (isFinite(v)) { pts.push([u, v]); lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    var cps = []; state.chain.forEach(function (v, u) { if (u >= x0 && u <= x1) { cps.push([u, v]); lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    if (isFinite(r.k)) { lo = Math.min(lo, r.k); hi = Math.max(hi, r.k); }
    if (!pts.length) { g.fillStyle = css("--ink-3"); g.font = "13px " + css("--f-body"); g.fillText("En attente des prix…", padL, H / 2); return; }
    var padv = Math.max((hi - lo) * 0.12, hi * 2e-5); lo -= padv; hi += padv;
    var Y = function (v) { return padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo)); };
    // moyennes 60 s
    g.fillStyle = css("--band");
    g.fillRect(X(m.S - 60), padT, X(m.S) - X(m.S - 60), H - padT - padB);
    g.fillRect(X(m.E - 60), padT, X(m.E) - X(m.E - 60), H - padT - padB);
    // grille et graduations
    var tk = niceTicks(lo, hi, 4), dec = tk.step < 1 ? (tk.step < 0.01 ? 4 : 2) : 0;
    g.strokeStyle = css("--line"); g.lineWidth = 1; g.fillStyle = css("--ink-3"); g.font = "11px " + css("--f-mono");
    g.textAlign = "right"; g.textBaseline = "middle";
    tk.ticks.forEach(function (v) { var y = Math.round(Y(v)) + 0.5; g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke(); g.fillText(nf(dec).format(v), padL - 6, y); });
    g.textAlign = "center"; g.textBaseline = "top";
    var wide = X(m.S) - X(m.S - 60) > 60;
    (wide ? [[m.S - 60, "S−60"], [m.S, "S " + hhmm(m.S)], [m.E - 60, "E−60"], [m.E, "E " + hhmm(m.E)]] : [[m.S, "S"], [m.E, "E"]])
      .forEach(function (a) { g.fillText(a[1], X(a[0]), H - padB + 6); });
    // prix à battre
    if (isFinite(r.k)) {
      g.strokeStyle = css("--now"); g.setLineDash([5, 4]); g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(X(m.S), Y(r.k)); g.lineTo(X(m.E), Y(r.k)); g.stroke(); g.setLineDash([]);
    }
    // Chainlink
    g.fillStyle = css("--ink-3");
    cps.forEach(function (p) { g.beginPath(); g.arc(X(p[0]), Y(p[1]), 1.6, 0, 2 * Math.PI); g.fill(); });
    // Binance
    g.strokeStyle = css("--ink"); g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
    pts.forEach(function (p, i) { var xx = X(p[0]), yy = Y(p[1]); if (i) g.lineTo(xx, yy); else g.moveTo(xx, yy); });
    g.stroke();
    // maintenant
    if (t < x1) { g.strokeStyle = css("--now"); g.lineWidth = 1; g.beginPath(); g.moveTo(X(t), padT); g.lineTo(X(t), H - padB); g.stroke(); }
  }
  function drawProb(m, t) {
    var cv = setupCanvas($("cProb")), g = cv.g, W = cv.w, H = cv.h;
    var padL = 40, padR = 12, padT = 10, padB = 24;
    var x0 = m.S - 90, x1 = m.E + 10;
    var X = function (s) { return padL + (W - padL - padR) * (s - x0) / (x1 - x0); };
    var Y = function (v) { return padT + (H - padT - padB) * (1 - v); };
    g.strokeStyle = css("--line"); g.fillStyle = css("--ink-3"); g.font = "11px " + css("--f-mono"); g.lineWidth = 1;
    g.textAlign = "right"; g.textBaseline = "middle";
    [0, 0.25, 0.5, 0.75, 1].forEach(function (v) { var y = Math.round(Y(v)) + 0.5; g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke(); g.fillText(fmt0.format(100 * v) + " %", padL - 6, y); });
    g.textAlign = "center"; g.textBaseline = "top";
    (X(m.E) - X(m.E - 60) > 40 ? [[m.S, "S"], [m.E - 60, "E−60"], [m.E, "E"]] : [[m.S, "S"], [m.E, "E"]])
      .forEach(function (a) { g.fillText(a[1], X(a[0]), H - padB + 6); });
    var sm = state.samples;
    // bande achat-vente
    g.fillStyle = css("--band"); g.beginPath();
    var band = sm.filter(function (s) { return isFinite(s.bid) && isFinite(s.ask); });
    band.forEach(function (s, i) { var xx = X(s.t); if (i) g.lineTo(xx, Y(s.ask)); else g.moveTo(xx, Y(s.ask)); });
    for (var i = band.length - 1; i >= 0; i--) g.lineTo(X(band[i].t), Y(band[i].bid));
    if (band.length) { g.closePath(); g.fill(); }
    function line(key, color, w) {
      g.strokeStyle = color; g.lineWidth = w; g.beginPath(); var on = false;
      sm.forEach(function (s) { var v = s[key]; if (!isFinite(v)) { on = false; return; } var xx = X(s.t), yy = Y(v); if (on) g.lineTo(xx, yy); else { g.moveTo(xx, yy); on = true; } });
      g.stroke();
    }
    line("mid", css("--ink-2"), 1.5);
    line("p", css("--up"), 2);
    if (!sm.length) { g.fillStyle = css("--ink-3"); g.textAlign = "left"; g.font = "13px " + css("--f-body"); g.fillText("La courbe se remplit à partir de l'ouverture de la page.", padL + 8, Y(0.5) - 18); }
    if (t < x1) { g.strokeStyle = css("--now"); g.lineWidth = 1; g.beginPath(); g.moveTo(X(t), padT); g.lineTo(X(t), H - padB); g.stroke(); }
  }

  /* ---------------- Boucle ---------------- */
  var lastSampleT = 0, lastChartMs = 0;
  function tick() {
    var t = nowS(), w = windows(t);
    var cur = ensureMarket(w.cur.S), nxt = ensureMarket(w.nxt.S);
    if (state.samplesFor !== cur.slug) { state.samples = []; state.samplesFor = cur.slug; }
    var rc = evaluate(cur, t), rn = evaluate(nxt, t);
    var dc = decision(cur, rc), dn = decision(nxt, rn);
    if (Math.floor(t) !== lastSampleT) {
      lastSampleT = Math.floor(t);
      var q = cur.q; state.samples.push({ t: lastSampleT, p: rc.phase === 5 ? NaN : rc.p, mid: (q.bidUp + q.askUp) / 2, bid: q.bidUp, ask: q.askUp });
      // checkpoints : 10 s avant l'ouverture (fenêtre suivante) et 30 s avant la clôture
      var toS = nxt.S - t; if (toS <= 10 && toS > 8 && isFinite(rn.p)) checkpoint(nxt, "S−10 s", rn, dn);
      var toE = cur.E - t; if (toE <= 30 && toE > 28 && isFinite(rc.p)) checkpoint(cur, "E−30 s", rc, dc);
    }
    renderCurrent(cur, rc, dc, t);
    renderNext(nxt, rn, dn, t);
    setPill("st-bn", state.bnRx, state.bnErr, 3000);
    setPill("st-cl", state.clRx, state.clErr, 4000);
    setPill("st-pm", state.pmRx, state.pmErr, 5000);
    var errs = [state.bnErr, state.clErr, state.pmErr].filter(Boolean);
    var ban = $("banner");
    if (errs.length) { ban.hidden = false; ban.textContent = "Flux indisponible : " + errs.join(" · ") + ". La page réessaie toute seule ; la formule fonctionne tant que Binance répond."; } else ban.hidden = true;
    if (Date.now() - lastChartMs > 500) { lastChartMs = Date.now(); drawPrice(cur, rc, t); drawProb(cur, t); }
  }
  function slow() {
    updateSigma(); prunePrices(); resolvePending(); renderHistory(); refreshBookSubscription();
    Object.keys(state.markets).forEach(function (k) { var m = state.markets[k]; if (m.E < nowS() - 3600 && !state.history.some(function (h) { return h.slug === k && !h.outcome; })) delete state.markets[k]; });
  }

  function select(asset, dur) {
    state.asset = asset || state.asset; state.dur = dur || state.dur;
    try { localStorage.setItem("vigie.pref", JSON.stringify({ asset: state.asset, dur: state.dur })); } catch (e) { /* ignoré */ }
    document.querySelectorAll("[data-asset]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.asset === state.asset)); });
    document.querySelectorAll("[data-dur]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.dur === state.dur)); });
    state.prices = new Map(); state.chain = new Map(); state.lastPrice = NaN; state.sigma = NaN;
    state.samples = []; state.samplesFor = null; state.markets = {}; state.tokenIndex = {}; pmTokensKey = "";
    state.bnRx = state.clRx = state.pmRx = 0;
    backfill().then(updateSigma);
    connectBinance(); connectChainlink();
  }
  document.querySelectorAll("[data-asset]").forEach(function (b) { b.addEventListener("click", function () { if (b.dataset.asset !== state.asset) select(b.dataset.asset, null); }); });
  document.querySelectorAll("[data-dur]").forEach(function (b) { b.addEventListener("click", function () { if (b.dataset.dur !== state.dur) select(null, b.dataset.dur); }); });
  window.addEventListener("resize", function () { lastChartMs = 0; });

  select(state.asset, state.dur);
  setInterval(tick, 250);
  setInterval(slow, 5000);
  setTimeout(slow, 1500);
  window.__vigie = state; // pour le débogage dans la console
})();
