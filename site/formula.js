/* Formule exacte P(Up) des marchés Polymarket « Up or Down » (règle TWAP-60).
 * Portage de src/tradebot/polymarket_formula.py (dt = 1 s, comme Chainlink) et des réglages
 * calibrés de src/tradebot/polymarket_signal.py (σ EWMA 1 s × 1,40, bruit de source 0,5 pb).
 * Tout est en log-prix ; σ par √seconde. */
(function (root) {
  "use strict";
  var L = 60;                  // durée des moyennes TWAP (s)
  var SIGMA_SCALE = 1.40;      // calibration (sur-confiance de la formule brute)
  var BASIS_SD = 0.5e-4;       // écart Binance -> Chainlink (0,5 pb)
  var FEE_RATE = 0.07;         // frais preneur crypto_fees_v2
  var EWMA_HALFLIFE_S = 600;

  // Φ(x) : fonction de répartition de la loi normale (erfc de W. J. Cody via Hart, |err| < 1e-7)
  function normCdf(x) {
    if (!isFinite(x)) return x > 0 ? 1 : 0;
    var z = Math.abs(x) / Math.SQRT2;
    var t = 1 / (1 + 0.5 * z);
    var r = t * Math.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 +
      t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 +
      t * (-0.82215223 + t * 0.17087277)))))))));
    return x >= 0 ? 1 - r / 2 : r / 2;
  }

  function phaseOf(t, S, E) {
    if (t <= S - L) return 1;
    if (t <= S) return 2;
    if (t <= E - L) return 3;
    return 4;
  }

  // Var(F − K | t) / σ², version discrète exacte (points toutes les secondes sur (S−L, S] et (E−L, E])
  function varianceFactor(t, S, E) {
    var n = L, sum = 0;
    var steps = Math.floor(E - t + 1e-9);
    for (var k = 1; k <= steps; k++) {
      var u = t + k;
      // points de fin >= u : (E − L + i) pour i = 1..n
      var firstEnd = Math.max(1, Math.ceil(u - (E - L) - 1e-9));
      var nEnd = firstEnd <= n ? n - firstEnd + 1 : 0;
      // points de départ > t et >= u
      var lo = Math.max(Math.floor(t - (S - L) + 1e-9) + 1, Math.ceil(u - (S - L) - 1e-9), 1);
      var nStart = lo <= n ? n - lo + 1 : 0;
      var g = (nEnd - nStart) / n;
      sum += g * g;
    }
    return sum;
  }

  /* Moyenne et écart-type de F − K sachant l'information à t.
   * o.logPrice : log p(t) ; o.startAvg : moyenne de log p sur (S−L, t] (phase 2) ;
   * o.logK : log K (phases 3-4) ; o.endPartialSum : Σ log p sur (E−L, t] / L (phase 4). */
  function moments(t, S, E, sigma, o) {
    var ph = phaseOf(t, S, E);
    var sd = sigma * Math.sqrt(Math.max(varianceFactor(t, S, E), 0));
    var mean;
    if (ph === 1) mean = 0;
    else if (ph === 2) mean = ((L - (S - t)) / L) * (o.logPrice - o.startAvg);
    else if (ph === 3) mean = o.logPrice - o.logK;
    else mean = o.endPartialSum + (Math.max(E - t, 0) / L) * o.logPrice - o.logK;
    return { mean: mean, sd: sd, phase: ph };
  }

  // P(Up) calibrée : Φ(m / √((1,40·s)² + σ_b²)) ; pure : Φ(m/s)
  function probUp(mom, calibrated) {
    var sd = calibrated === false ? mom.sd : Math.sqrt(Math.pow(SIGMA_SCALE * mom.sd, 2) + BASIS_SD * BASIS_SD);
    if (!(sd > 0)) return mom.mean >= 0 ? 1 : 0;
    return normCdf(mom.mean / sd);
  }

  // σ par √s : EWMA des carrés des rendements 1 s (log-prix à la seconde, sans trou)
  function ewmaSigma(logPrices, halflife) {
    var lam = Math.pow(0.5, 1 / (halflife || EWMA_HALFLIFE_S));
    var num = 0, den = 0, w = 1;
    for (var i = logPrices.length - 1; i >= 1; i--) {
      var r = logPrices[i] - logPrices[i - 1];
      if (isFinite(r)) { num += w * r * r; den += w; }
      w *= lam;
    }
    return den > 0 ? Math.sqrt(num / den) : NaN;
  }

  function takerCost(ask) { return ask + FEE_RATE * ask * (1 - ask); }

  // Espérance par part d'un achat preneur de chaque côté et côté conseillé (ou null)
  function takerEdge(p, askUp, askDown) {
    var evUp = isFinite(askUp) ? p - takerCost(askUp) : NaN;
    var evDown = isFinite(askDown) ? (1 - p) - takerCost(askDown) : NaN;
    var side = null;
    var best = Math.max(isFinite(evUp) ? evUp : -Infinity, isFinite(evDown) ? evDown : -Infinity);
    if (best > 0) side = (isFinite(evUp) && evUp >= (isFinite(evDown) ? evDown : -Infinity)) ? "up" : "down";
    return { evUp: evUp, evDown: evDown, side: side };
  }

  var api = { L: L, SIGMA_SCALE: SIGMA_SCALE, BASIS_SD: BASIS_SD, FEE_RATE: FEE_RATE,
    EWMA_HALFLIFE_S: EWMA_HALFLIFE_S, normCdf: normCdf, phaseOf: phaseOf, varianceFactor: varianceFactor,
    moments: moments, probUp: probUp, ewmaSigma: ewmaSigma, takerCost: takerCost, takerEdge: takerEdge };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Formula = api;
})(typeof window !== "undefined" ? window : this);
