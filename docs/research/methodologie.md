# Méthodologie d'évaluation : prévoir la direction des cryptos à 5, 10 et 15 minutes

*Phase 1 du projet `tradebot`. Grosses cryptos : BTC et SOL en priorité, puis ETH, XRP, BNB et DOGE
(paires USDT spot Binance). Barres de 1 minute, horizons h = 5, 10 et 15 barres. Marché ouvert 24 h/24,
horodatage UTC. Rédigé le 25/09/2026.*

Ce document fixe **comment juger** un prédicteur de direction : indicateur seul, modèle combiné ou
TimesFM. Il couvre les métriques, les tests statistiques, le traitement du chevauchement des cibles, la
validation hors échantillon, les tests multiples, les baselines, les coûts de transaction et les biais.
Chaque point comporte une formule, une implémentation Python (scipy, statsmodels, scikit-learn) et, dès
que possible, des **chiffres mesurés sur de vraies données Binance**. Il complète
[`indicateurs.md`](indicateurs.md) et [`timesfm.md`](timesfm.md) et respecte le contrat de
`docs/ARCHITECTURE.md` (fonctions `evaluation.indicator_scores`, `backtest.direction_metrics`,
`backtest.toy_strategy`…).

**Données utilisées pour les chiffres de ce document**

| Jeu | Source | Période (UTC) | Taille |
|---|---|---|---|
| « 30 j » | API `https://data-api.binance.vision/api/v3/klines`, 1m, 6 paires | 26/08/2026 16:38 → 25/09/2026 16:37 | 43 200 barres par paire |
| « 12 m » | zips mensuels `data.binance.vision`, spot 1m, BTCUSDT et SOLUSDT | 01/09/2025 → 31/08/2026 | 525 600 barres par paire, **aucun trou** |
| Carnet spot | `data-api.binance.vision` `/ticker/bookTicker` et `/depth?limit=5000` | 25/09/2026 vers 16 h 40, 60 relevés sur environ 4 min | — |
| Carnet futures | `data/futures/um/daily/bookDepth` | 20/09/2026 (un dimanche) | un relevé toutes les ~30 s |
| Métriques futures | `data/futures/um/daily/metrics` + klines futures 1m | 18 → 20/09/2026 | 864 relevés de 5 min par paire |

Les cibles sont celles de `tradebot.targets.future_log_return` : `r_h(t) = ln(C[t+h]/C[t])`,
exprimées en **points de base** (1 pb = 0,01 %).

---

## 0. L'essentiel en 12 points

1. **Payer les coûts demande une précision énorme.** Supposons l'exactitude indépendante de l'amplitude
   du mouvement. Un trade sur chaque signal doit alors atteindre `p* = 1/2 + c / (2·E|r_h|)`. Sur les
   30 derniers jours, E|r_15| vaut 12,7 pb pour BTC et 22,2 pb pour SOL. Pour couvrir **10 pb
   aller-retour** (futures, taker des deux côtés), il faut donc **89 %** de précision sur BTC et
   **72,5 %** sur SOL à 15 min. Pour **20 pb** (spot, taker), c'est impossible sur BTC (p* > 1) et il faut
   95 % sur SOL. À 5 min sur BTC, E|r_5| = 7,4 pb est inférieur à 10 pb : **même un oracle parfait perd
   de l'argent** en tradant chaque barre en taker (§ 8.4).
2. **Seules deux conditions ramènent le seuil sous 60–70 %.** Il faut à la fois **filtrer** (trader
   seulement quand la volatilité est haute) et **réduire les coûts** (ordres maker). Dans le décile
   de volatilité le plus haut, E|r_15| atteint 24,9 pb (BTC) et 38,2 pb (SOL), d'où p* = 70 % et 63 %
   à 10 pb. En maker futures (4 pb), p* tombe à 58 % et 55 % (§ 8.5).
3. **L'accuracy n'est pas ce qui rapporte.** Ce qui compte, c'est l'**accuracy pondérée par
   l'amplitude** `p_w`. Le retournement à 15 min a une accuracy de 51,9 % (BTC) mais un `p_w` de
   seulement 50,9 %. Un logit walk-forward atteint **61 % de réussite sur ses 1 % de signaux les plus
   confiants**, mais ne gagne que **1,75 pb bruts** par trade : il a raison sur les petits mouvements et
   tort sur les grands (§ 8.6).
4. **Des cibles qui se chevauchent faussent tous les tests naïfs.** Nous avons simulé une hypothèse nulle
   vraie (h = 15, n = 20 000 barres, 300 tirages). Un test à 5 % rejette alors à tort **21 %** du temps
   (Pesaran-Timmermann naïf), **22 %** (binomial sur n) et **25 %** (Anatolyev-Gerko naïf). Avec des
   erreurs Newey-West, on revient à 4–5 %. Le binomial sur `n/h` est **trop conservateur** (0,7 %)
   (§ 3.4).
5. **Newey-West avec `lags = h` sous-estime encore un peu la variance.** Mesuré sur 12 mois de BTC, le
   facteur d'inflation de variance (VIF) du label vaut 7,5 avec `lags = h`, contre 8,6 à 8,8 avec `lags ≥ 2h`.
   Nous recommandons de reporter aussi `lags = 2h` et un **t par jours** (§ 3.3).
6. **Le vrai rival est le retournement, pas 50 %.** La règle « parier contre le mouvement des h
   dernières minutes » fait 51,3 à 52,2 % sans aucun modèle, avec |t| ≈ 11 à 16 sur 12 mois. « Toujours
   hausse » fait 49,8 % sur cette année baissière. Un modèle qui fait 52 % ne vaut rien s'il ne bat pas
   `reversal_h` (§ 7).
7. **Statistiquement significatif ne veut pas dire rentable.** Le retournement a un t d'environ 12, mais
   il rapporte 0,1 à 0,5 pb bruts par trade, pour 10 à 20 pb de coûts. Il faut deux seuils distincts
   (§ 10).
8. **Beaucoup de tests font apparaître des gagnants par hasard.** 80 indicateurs × 3 horizons ×
   6 actifs font 1 440 tests. Sur un an de test à h = 15 (n_eff = 35 040), **le meilleur indicateur
   inutile atteint en moyenne 50,9 %**. Sur les 40 % hors échantillon (n_eff = 14 016), il atteint
   51,4 %. Il faut appliquer Benjamini-Hochberg, viser t > 3 (Harvey-Liu-Zhu) et calculer le deflated
   Sharpe (§ 6).
9. **La puissance a un prix.** Détecter 52 % (puissance 80 %, α = 5 % unilatéral) demande
   **3 865 trades non chevauchants**, soit 40 jours de barres à 15 min. Détecter 51 % en demande 15 457,
   soit 161 jours. Un backtest TimesFM de 290 origines non chevauchantes ne détecte rien sous 57 % environ (§ 5).
10. **Il existe un piège réel dans les métriques dérivés Binance.** Dans les fichiers `metrics`, le
    `sum_taker_long_short_vol_ratio` horodaté `T` couvre la fenêtre **[T, T+5 min)** (corrélation 1,00
    avec le flux taker de cette fenêtre, contre 0,13 pour [T−5, T)). Aligné sans décalage, il donne un
    **hit-rate illusoire de 72,5 % et un IC de 0,63** à 5 min. Avec le décalage prudent de +5 min du
    contrat, on retrouve 48,8 % et un IC de −0,05 (§ 9.2).
11. **Les régimes font varier le seuil de rentabilité du simple au double.** Selon le mois, E|r_15| de
    BTC va de 9,8 pb (septembre 2025) à 24,2 pb (février 2026). Il vaut 10,6 pb le week-end contre
    17,0 pb en semaine, 24,0 pb à 14 h UTC contre 11,6 pb à 10 h UTC (§ 8.7).
12. **Protocole minimal** : walk-forward purgé de h barres, puis un mois final « coffre-fort » ouvert une
    seule fois. Il faut les 5 baselines, un PT robuste (HAC), le DM contre `reversal_h`, des IC par
    bootstrap de jours, un rendement net par trade après coûts réalistes, un registre de **tous** les
    essais, et la checklist du § 12.

---

## 1. Cadre et notations

| Symbole | Sens |
|---|---|
| `t` | origine : clôture de la barre `t`. Toutes les variables explicatives n'utilisent que les barres `≤ t` |
| `h` | horizon en barres (5, 10, 15) |
| `r_h(t)` | `ln(C[t+h]/C[t])`, NaN si la barre `t+h` n'est pas exactement h minutes plus tard |
| `y_t` | 1 si `r_h > eps`, 0 si `r_h < −eps`, NaN sinon (`eps = 0` par défaut) |
| `p_t` | probabilité de hausse prédite ; `ŷ_t = 1{p_t > 0,5}` |
| `n` | nombre d'origines évaluées ; `n_eff` : taille effective (≈ n/h si une origine par barre) |
| `c` | coût aller-retour total en pb (frais + spread + slippage) |

**Deux questions différentes**, qui appellent des métriques différentes :

* **Statistique** : le signal contient-il de l'information sur le signe futur ? Pour y répondre :
  PT, AUC, Brier, IC, avec des tests robustes au chevauchement.
* **Économique** : après coûts, le signal gagne-t-il de l'argent, de façon stable et sans
  sur-ajustement ? Pour y répondre : rendement net par trade, `p_w` comparé à `p*`, Sharpe net,
  deflated Sharpe.

Un résultat n'est crédible que s'il passe **les deux** (§ 10).

---

## 2. Métriques de direction

### 2.1 Accuracy, balanced accuracy, taux de base

```
accuracy          = (1/n) Σ 1{ŷ_t = y_t}
balanced_accuracy = ½ · [ P(ŷ=1 | y=1) + P(ŷ=0 | y=0) ]      (moyenne des rappels)
taux de base      = ȳ = part de hausses
```

L'accuracy brute se compare à `max(ȳ, 1−ȳ)`, pas à 0,5. Sur nos 12 mois (une année baissière :
BTC passe de 108 k$ à 79 k$, SOL de 200 $ à 103 $), « toujours hausse » ne fait que **49,75 %** à 5 min. Dans un marché
haussier, il ferait plus de 50 % sans aucune compétence. La balanced accuracy neutralise ce biais : elle
vaut 0,5 pour tout prédicteur constant.

**Égalités.** Sur SOL, 3,2 % des `r_5` sont exactement nuls (tick de 0,01 $ pour un prix de 121 $,
soit 0,82 pb). Sur DOGE, c'est 3,5 %, contre 0,36 % sur BTC. Le contrat les exclut (label NaN). Il faut
**reporter la couverture** (part des origines évaluées), surtout si `eps > 0`.

```python
from sklearn.metrics import accuracy_score, balanced_accuracy_score
acc = accuracy_score(y, y_hat); bacc = balanced_accuracy_score(y, y_hat)
```

### 2.2 AUC

L'AUC est la probabilité qu'une hausse tirée au hasard reçoive un score plus élevé qu'une baisse tirée
au hasard (statistique de Mann-Whitney) :

```
AUC = (1/(n₁n₀)) Σ_{i: y=1} Σ_{j: y=0} [ 1{p_i > p_j} + ½·1{p_i = p_j} ]
```

* Elle est insensible au taux de base et au seuil. Elle mesure le **classement**, pas la calibration.
* Pour une prévision binaire (p ∈ {0,48 ; 0,52}), **AUC = balanced accuracy** : nous l'avons vérifié
  numériquement, 0,51890 dans les deux cas pour `reversal_15` sur BTC.
* L'erreur type de Hanley et McNeil (1982) et le test de DeLong (1988) supposent l'indépendance. Avec
  des cibles chevauchantes, il faut **bootstrapper par jours entiers** (§ 3.5). Exemple mesuré (logit
  walk-forward, BTC, h = 15) : AUC = 0,5297, IC à 95 % par bootstrap de jours [0,5253 ; 0,5336].

```python
from sklearn.metrics import roc_auc_score
auc = roc_auc_score(y, p)
lo, hi = day_block_bootstrap(lambda y, p: roc_auc_score(y, p), idx, y=y, p=p)   # § 3.5
```

### 2.3 Brier, log-loss, calibration

Ce sont des **règles de score strictement propres** (Gneiting et Raftery 2007) : elles récompensent une
probabilité *honnête*, pas seulement un bon côté.

```
Brier    BS  = (1/n) Σ (p_t − y_t)²                 référence climatologique : ȳ(1−ȳ) ≈ 0,25
Log-loss LL  = −(1/n) Σ [ y ln p + (1−y) ln(1−p) ]   référence : ln 2 = 0,6931
Skill    BSS = 1 − BS / BS_ref                      (> 0 : mieux que le taux de base appris sur l'apprentissage)
Murphy (1973) : BS = fiabilité − résolution + incertitude
```

**Ordre de grandeur.** Un signal calibré p = 0,5 ± δ, toujours du bon côté avec la probabilité
0,5 + δ, donne `BS = 0,25 − δ²`. Un signal à 52 % correspond donc à BSS ≈ 0,16 %. Notre logit
walk-forward à 15 min a BS = 0,2493 contre 0,2500 pour la climatologie : **BSS = 0,28 %** sur BTC et
0,29 % sur SOL, log-loss 0,6918 contre 0,6931. Voilà à quoi ressemble un vrai signal de 52 % : des
écarts au 3e ou 4e chiffre après la virgule. Pour TimesFM, `prob_up` est bornée à [0,01 ; 0,99]. Une
P(hausse) de 0,9 fausse coûte très cher en log-loss. **Vérifier la calibration** avec
`sklearn.calibration.calibration_curve` avant d'utiliser un seuil de 0,55 dans `toy_strategy`.

```python
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.calibration import calibration_curve

bs = brier_score_loss(y, p); bs_ref = brier_score_loss(y, np.full_like(p, y_train.mean()))
bss = 1 - bs / bs_ref
frac_pos, mean_pred = calibration_curve(y, p, n_bins=10, strategy="quantile")

def brier_decomposition(y, p, bins=10):
    """Décomposition de Murphy (1973) : Brier ≈ fiabilité − résolution + incertitude."""
    y = np.asarray(y, float); p = np.asarray(p, float); ybar = y.mean()
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    k = np.clip(np.searchsorted(edges, p, "right") - 1, 0, bins - 1)
    rel = res = 0.0
    for b in range(bins):
        m = k == b
        if m.any():
            w = m.mean(); rel += w * (p[m].mean() - y[m].mean()) ** 2; res += w * (y[m].mean() - ybar) ** 2
    return dict(reliability=rel, resolution=res, uncertainty=ybar * (1 - ybar))
```

### 2.4 Test binomial

`H0 : P(ŷ = y) = 0,5`, et k succès sur n_eff essais. On l'utilise avec `scipy.stats.binomtest(k,
n_eff, 0.5, alternative="greater")`. Il a deux défauts :

* **H0 est mal posée** quand le taux de base diffère de 0,5 et que le prédicteur est biaisé : « toujours
  hausse » en marché haussier passe le test sans aucune compétence. Le test PT (§ 2.5) corrige cela.
* **Il suppose l'indépendance.** Sur n, il rejette à tort 22 % du temps au lieu de 5 % (§ 3.4). Sur
  `n_eff = n // h`, comme le prévoit le contrat pour `p_hit` et `p_binom`, il devient valide mais
  **conservateur** (0,7 % de rejets au lieu de 5 %, donc une perte de puissance).

**Cas du backtest TimesFM avec `stride > 1`.** Avec une origine toutes les `s` barres, le chevauchement
entre origines vaut `h/s`. Il faut alors passer `h_eff = ceil(h / stride)` à `direction_metrics`, et pas
`h`. Par exemple, `stride = 5` et h = 15 donnent h_eff = 3. Passer h reviendrait à diviser n_eff par
5 de trop.

### 2.5 Test de Pesaran et Timmermann (1992)

Le test PT compare l'accuracy observée à celle attendue **si prévision et réalisation étaient
indépendantes**, compte tenu de leurs fréquences marginales :

```
P̂  = (1/n) Σ 1{ŷ_t = y_t},   P_y = ȳ,   P_x = mean(ŷ)
P* = P_y·P_x + (1−P_y)(1−P_x)
V(P̂)  = P*(1−P*)/n
V(P*)  = (2P_y−1)² P_x(1−P_x)/n + (2P_x−1)² P_y(1−P_y)/n + 4 P_y P_x (1−P_y)(1−P_x)/n²
PT = (P̂ − P*) / sqrt( V(P̂) − V(P*) )   ~  N(0,1) sous H0 (unilatéral)
```

Le test n'est pas défini si ŷ est constant (P_x ∈ {0, 1}) : c'est normal, un prédicteur constant n'a
aucun pouvoir de timing. PT suppose des observations **indépendantes**. Pesaran et Timmermann (2009)
l'étendent aux séries autocorrélées. En pratique, on utilise l'équivalent asymptotique : le t de β dans
la régression `y_t = α + β·ŷ_t + u_t`, avec des **erreurs Newey-West**.

```python
import numpy as np, statsmodels.api as sm
from scipy import stats

def pesaran_timmermann(y_true, y_pred):
    """Test PT (1992) ; H0 : indépendance. Retourne (stat, p unilatérale)."""
    y = np.asarray(y_true, float); x = np.asarray(y_pred, float); n = len(y)
    p_hat, py, px = np.mean(y == x), y.mean(), x.mean()
    p_star = py * px + (1 - py) * (1 - px)
    v_p = p_star * (1 - p_star) / n
    v_star = ((2 * py - 1) ** 2 * px * (1 - px) / n + (2 * px - 1) ** 2 * py * (1 - py) / n
              + 4 * py * px * (1 - py) * (1 - px) / n ** 2)
    stat = (p_hat - p_star) / np.sqrt(v_p - v_star)
    return float(stat), float(stats.norm.sf(stat))

def pt_hac(y_true, y_pred, lags):
    """PT robuste à l'autocorrélation : t de b dans 1{hausse} = a + b·1{prévu hausse} + u (Newey-West)."""
    X = sm.add_constant(np.asarray(y_pred, float))
    res = sm.OLS(np.asarray(y_true, float), X).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return float(res.tvalues[1]), float(stats.norm.sf(res.tvalues[1]))
```

**Mesuré (BTC, 12 mois, `reversal_15`, n = 524 794)** : PT naïf = 27,4 et PT HAC (lags = 2h) = 12,1.
Le naïf surestime le t d'un facteur ≈ √(VIF) ≈ 2,3.

### 2.6 Test « économique » d'Anatolyev et Gerko (2005)

Le test EP (*excess profitability*) mesure le rendement normalisé de la stratégie `sign(ŷ)·r`, par
rapport à celui d'une stratégie de même exposition moyenne mais sans timing. Il est plus puissant que PT
quand le signal sait aussi « choisir les gros mouvements » :

```
A_T = (1/T) Σ s_t r_t,   B_T = [(1/T) Σ s_t]·[(1/T) Σ r_t],   s_t = ±1
p̂  = ½ (1 + (1/T) Σ s_t)
V̂  = (4/T²) p̂(1−p̂) Σ (r_t − r̄)²
EP  = (A_T − B_T)/sqrt(V̂)  ~  N(0,1)
```

Comme PT, EP suppose l'indépendance : il rejette à tort 25 % du temps avec h = 15 (§ 3.4). La version
robuste est le t HAC de β dans `r_t = α + β·s_t + u_t`.

```python
def anatolyev_gerko(ret, y_pred):
    y = np.asarray(ret, float); s = np.where(np.asarray(y_pred) > 0.5, 1.0, -1.0); T = len(y)
    a = np.mean(s * y); b = np.mean(s) * np.mean(y); p = 0.5 * (1 + np.mean(s))
    v = 4 / T ** 2 * p * (1 - p) * np.sum((y - y.mean()) ** 2)
    stat = (a - b) / np.sqrt(v)
    return float(stat), float(stats.norm.sf(stat))
```

### 2.7 Comparer deux prévisions : Diebold-Mariano, HLN, Clark-West

**Diebold et Mariano (1995).** On prend un écart de pertes `d_t = L(e_A,t) − L(e_B,t)`, par exemple la
perte de Brier `(p−y)²`, la log-loss ou l'erreur quadratique sur `r_h` :

```
DM = d̄ / sqrt( LRV̂(d) / n )     LRV = variance de long terme (Newey-West)
Correction HLN (Harvey, Leybourne, Newbold 1997) : DM* = DM · sqrt( (n + 1 − 2h + h(h−1)/n) / n ),
loi de Student à n−1 degrés de liberté
```

Pour une prévision à h pas, `d_t` est au moins un MA(h−1). Diebold et Mariano utilisent h−1
autocovariances. Avec le noyau de Bartlett, prendre `lags ≥ 2h` (§ 3.3). Avec nos n de plusieurs
centaines de milliers, la correction HLN est négligeable (7,290 contre 7,290). Elle compte pour les
backtests TimesFM de quelques centaines d'origines.

**Modèles emboîtés** (marche aléatoire contre modèle) : sous H0, le grand modèle a mécaniquement une
MSPE plus élevée, car il estime des paramètres nuls. Le DM est alors biaisé contre lui. Clark et West
(2007) ajustent la MSPE avec
`f_t = (y − ŷ₁)² − [(y − ŷ₂)² − (ŷ₁ − ŷ₂)²]`, suivi d'un test unilatéral sur la moyenne.

**Attention aux pertes quadratiques sur `r_h`.** Elles sont dominées par les queues : la kurtosis de
`r_15` (non chevauchant) vaut **18,9 sur BTC et 45,8 sur SOL**. Le même signal de retournement donne un
PT HAC de 12,1 mais un Clark-West de seulement 1,11 (p = 0,13). Pour la direction, il faut comparer
des pertes **sur le label** (Brier, log-loss) et des rendements nets de stratégie, pas seulement des
MSE. Si les modèles sont réestimés sur des fenêtres glissantes, le cadre de Giacomini et White (2006),
qui teste la capacité prédictive *conditionnelle*, reste valide en présence d'incertitude
d'estimation.

```python
def diebold_mariano(loss_a, loss_b, h, lags=None):
    """DM (1995) + correction HLN (1997). d = loss_a − loss_b ; stat < 0 => A meilleur. p bilatérale."""
    d = np.asarray(loss_a, float) - np.asarray(loss_b, float); n = len(d)
    res = sm.OLS(d, np.ones(n)).fit(cov_type="HAC", cov_kwds={"maxlags": lags if lags is not None else h})
    hln = np.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    stat = float(res.tvalues[0] * hln)
    return stat, float(2 * stats.t.sf(abs(stat), df=n - 1))

def clark_west(y, f_small, f_big, h):
    """MSPE ajustée de Clark-West (2007), modèles emboîtés (marche aléatoire : f_small = 0). Unilatéral."""
    y, f1, f2 = (np.asarray(v, float) for v in (y, f_small, f_big))
    adj = (y - f1) ** 2 - ((y - f2) ** 2 - (f1 - f2) ** 2)
    res = sm.OLS(adj, np.ones(len(adj))).fit(cov_type="HAC", cov_kwds={"maxlags": h})
    return float(res.tvalues[0]), float(stats.norm.sf(res.tvalues[0]))

# Exemple : modèle contre climatologie, perte de Brier
dm_stat, dm_pvalue = diebold_mariano((y - p_model) ** 2, (y - p_clim) ** 2, h=15, lags=30)
```

### 2.8 Coefficient d'information (IC)

L'IC est la corrélation de Spearman entre un indicateur et `r_h`. Le contrat (`indicator_scores`)
demande quatre versions : l'IC global, l'**IC journalier moyen** et son t entre jours (à la manière de
Fama-MacBeth), et le t Newey-West :

```
IC_d = Spearman(x_t, r_h(t)) pour t dans le jour d ;  t_jour = mean(IC_d) / ( sd(IC_d)/√D )
```

```python
def ic_newey_west(x: pd.Series, r: pd.Series, h: int):
    """IC de Spearman et t Newey-West (régression des rangs standardisés)."""
    d = pd.concat([x, r], axis=1).dropna()
    a = d.iloc[:, 0].rank().to_numpy(); b = d.iloc[:, 1].rank().to_numpy()
    a = (a - a.mean()) / a.std(); b = (b - b.mean()) / b.std()
    res = sm.OLS(b, a).fit(cov_type="HAC", cov_kwds={"maxlags": h})
    return float(res.params[0]), float(res.tvalues[0])
```

Mesuré (BTC, 12 mois, `x = −r_15 passé`) : IC = 0,040 avec t_NW = 10,2. Cela concorde avec les IC
de −0,02 à −0,06 de [`indicateurs.md`](indicateurs.md) pour le momentum.

### 2.9 Quelle métrique pour quelle question

| Question | Métrique principale | Test | Remarque |
|---|---|---|---|
| Le signe est-il prévisible ? | balanced accuracy, AUC | PT HAC, t par jours | comparer à `reversal_h`, pas à 0,5 |
| Les probabilités sont-elles bonnes ? | Brier, log-loss, BSS | DM (HAC) contre climatologie et contre baseline | vérifier la calibration |
| Le classement est-il stable ? | IC, IC journalier | t_NW, t par jours | signe identique entre actifs et entre mois |
| Le signal vaut-il de l'argent ? | `p_w`, pb nets par trade | EP/HAC, IC bootstrap par jours | coûts réalistes (§ 8) |
| Est-ce du sur-ajustement ? | nombre d'essais, DSR, PBO | BH/BY, max-t bootstrap | registre de tous les essais |

---

## 3. Chevauchement des cibles

### 3.1 Pourquoi c'est grave

`r_h(t)` et `r_h(t+1)` partagent h−1 des h rendements d'une minute. Si les rendements d'une minute sont
non corrélés, la corrélation vaut :

```
corr( r_h(t), r_h(t+k) ) = (h − k)/h      pour 0 ≤ k < h,  0 au-delà
corr( sign r_h(t), sign r_h(t+k) ) = (2/π)·arcsin((h − k)/h)       (cas gaussien, formule de Sheppard)
VIF = Var(moyenne) / Var_iid(moyenne) = 1 + 2 Σ_{k=1}^{h−1} ρ_k
```

Pour la **série des succès** `1{ŷ = y}`, avec ŷ et y codés ±1 et indépendants sous H0, on a
`hit − ½ = ŷ·y/2`. L'autocorrélation des succès vaut donc `ρ_y(k)·ρ_x(k) ≤ ρ_y(k)`. Conséquence
utile : **sous H0, le VIF des succès ne dépasse jamais celui du label**, quelle que soit l'inertie du
prédicteur. `n_eff = n/h` est donc conservateur sous H0.

### 3.2 Mesures sur BTC (12 mois)

| h | VIF théorique du label (gaussien) | VIF empirique du label (NW, lags = 3h) | VIF des succès `reversal_h` (NW, lags = 60) | VIF des succès, blocs d'un jour |
|---|---|---|---|---|
| 5 | 3,78 | 3,18 | 2,12 | 2,59 |
| 10 | 7,38 | 6,01 | — | — |
| 15 | 10,99 | 8,75 | 5,27 | 5,59 |

Le VIF réel est **inférieur à h**, ce qui confirme que `n/h` est conservateur. Il reste pourtant de 3 à
9 : un test qui l'ignore surestime le t d'un facteur 1,8 à 3. Les blocs journaliers donnent un VIF un peu
plus grand que Newey-West. C'est la trace de **régimes intrajournaliers** : le hit-rate varie d'un jour à
l'autre plus que ne le prévoit l'autocorrélation à court terme.

### 3.3 Newey-West : combien de retards ?

```
LRV̂ = γ̂₀ + 2 Σ_{k=1}^{L} (1 − k/(L+1)) γ̂_k        (noyau de Bartlett)
```

Le noyau de Bartlett **sous-pondère** les retards proches de L. Avec `L = h`, les autocorrélations aux
retards h−1 et h−2 comptent à peine. Mesures sur BTC :

| VIF mesuré | L = h−1 | L = h | L = 2h | L = 3h | L = 5h |
|---|---|---|---|---|---|
| label, h = 5 | 2,62 | 2,78 | 3,09 | 3,17 | 3,18 |
| label, h = 15 | 7,32 | 7,50 | 8,57 | 8,75 | 8,76 |
| succès `reversal_15` | 4,62 | 4,69 | 5,11 | 5,22 | 5,30 |

Le contrat fixe `ic_nw_t` avec `lags = h`. C'est un minimum, qui sous-estime la variance de 12 à 15 %
(t gonflé de 6 à 8 %). **Recommandation** : garder `lags = h` pour la colonne du contrat, mais reporter
aussi `lags = 2h` ou le t par jours, et **prendre le plus conservateur** pour les décisions. Deux
alternatives : le noyau rectangulaire de Hansen et Hodrick (1980) avec h−1 retards, exact pour un MA(h−1)
mais dont la variance peut sortir négative, ou la sélection automatique de Newey et West (1994).

**Coût de calcul.** `statsmodels` avec HAC sur 525 000 lignes et 30 retards prend quelques secondes.
Pour une grille de 1 440 tests, on peut sous-échantillonner une barre sur h, ce qui supprime l'essentiel
du chevauchement, puis appliquer un HAC léger. Il faut le documenter, comme le demande le contrat.

### 3.4 Simulation : taux de rejet sous H0

Nous avons simulé deux marches aléatoires indépendantes, avec h = 15, n = 20 000 barres et 300 tirages.
Le signal est le « momentum » sur h de l'autre série, donc lui aussi chevauchant et inerte. Le test est
nominal à 5 %, unilatéral.

| Test | Taux de rejet observé |
|---|---|
| PT naïf (1992) | **21,3 %** |
| binomial sur n | **21,7 %** |
| Anatolyev-Gerko naïf | **24,7 %** |
| PT HAC, lags = h | 5,3 % |
| PT HAC, lags = 2h | 4,3 % |
| t sur les moyennes journalières (13 jours seulement) | 3,7 % |
| binomial sur n/h | 0,7 % (trop conservateur, perte de puissance) |

**Exemple réel** : `majority_prev_day` (prévoir la direction de la veille) sur BTC à h = 10. Le test
naïf donne p = 0,003 (PT = 2,74), ce qui paraît « significatif à 1 % ». Le PT HAC donne 1,21, soit
p = 0,11. Sur SOL à h = 10, on passe de PT = 5,27 (p ≈ 10⁻⁷) à 2,32 (p = 0,01).

### 3.5 Inférence par jours : t journalier et bootstrap de blocs

Un jour UTC compte 1 440 barres, soit ≫ h, et les jours sont presque indépendants. Deux outils simples
en découlent :

```python
def daily_block_t(values: pd.Series):
    """t de Student sur les moyennes journalières UTC : chaque jour = 1 observation ~ indépendante."""
    daily = values.groupby(values.index.floor("D")).mean()
    return float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))), len(daily)

def day_block_bootstrap(stat_fn, idx: pd.DatetimeIndex, n_boot=500, seed=0, **arrays):
    """IC percentile d'une statistique en ré-échantillonnant des jours entiers (avec remise)."""
    rng = np.random.default_rng(seed)
    days, code = np.unique(idx.floor("D"), return_inverse=True)
    groups = [np.flatnonzero(code == k) for k in range(len(days))]
    out = []
    for _ in range(n_boot):
        sel = np.concatenate([groups[k] for k in rng.integers(0, len(days), len(days))])
        out.append(stat_fn(**{k: v[sel] for k, v in arrays.items()}))
    return np.percentile(out, [2.5, 97.5])
```

Mesuré sur `reversal_15` (BTC, 365 jours) : t par jours = 11,4, t_NW(2h) = 12,1, IC à 95 % de
l'accuracy [51,57 % ; 52,10 %]. Le bootstrap *stationnaire* de Politis et Romano (1994) est une variante
à blocs de longueur aléatoire. Des blocs d'un jour suffisent ici (h ≤ 15 ≪ 1 440).

---

## 4. Validation hors échantillon

### 4.1 Walk-forward purgé (le défaut du projet)

Le contrat prévoit `combined_model_walkforward` (fenêtre croissante, purge de h barres) et
`indicator_scores` (split chronologique `train_frac = 0,6`). La purge sert à ceci : la cible de la
ligne d'apprentissage `t` utilise `C[t+h]`. Les h dernières lignes d'apprentissage « voient » donc le
début de la période de test.

```
fold k :  apprentissage = [0, te0 − h)   (purge des h dernières lignes)   test = [te0, te1)
```

`sklearn.model_selection.TimeSeriesSplit(n_splits, gap=h)` fait exactement cela, et `max_train_size`
donne une fenêtre glissante. Nous avons vérifié que ses découpages sont identiques à ceux de la fonction
maison ci-dessous.

```python
from sklearn.model_selection import TimeSeriesSplit

tss = TimeSeriesSplit(n_splits=5, gap=h)            # expanding ; max_train_size=... pour glissant
for tr, te in tss.split(X):
    model.fit(X[tr], y[tr]); p[te] = model.predict_proba(X[te])[:, 1]

def purged_walk_forward(n, n_splits, h, min_train=None):
    """Fenêtre croissante ; retire les h dernières lignes d'apprentissage (leur cible chevauche le test)."""
    fold = n // (n_splits + 1); start = min_train or fold
    for k in range(n_splits):
        te0 = start + k * (n - start) // n_splits; te1 = start + (k + 1) * (n - start) // n_splits
        yield np.arange(0, max(te0 - h, 0)), np.arange(te0, te1)
```

**Pour les indicateurs seuls**, qui n'ont pas de paramètre ajusté, « l'apprentissage » consiste à
choisir le **signe** (`train_sign`), éventuellement un seuil ou une interaction avec un régime. Ce choix
doit être fait sur la partie apprentissage uniquement. Le hit-rate OOS s'évalue ensuite avec ce signe
figé.

### 4.2 K-fold purgé et embargo (López de Prado 2018, chap. 7)

Le k-fold standard mélange passé et futur : les lignes d'apprentissage *postérieures* au bloc de test ont
des variables calculées sur les prix du test. López de Prado propose deux corrections :

* **purge** : retirer de l'apprentissage toute ligne dont l'intervalle de cible `[t, t+h]` recoupe
  l'intervalle couvert par les cibles du test ;
* **embargo** : retirer en plus une bande de lignes **après** chaque bloc de test. Les variables
  autocorrélées (fenêtres de 60 min, z-scores journaliers) y sont en effet de quasi-copies de celles du
  test. López de Prado suggère un embargo de l'ordre de 0,01·T, soit 5 256 barres (3,65 jours) sur un
  an de barres 1 min. Règle pratique chez nous : `embargo ≥ max(h, plus longue fenêtre des variables)`,
  **au moins 1 440 barres** (un jour).

```python
def purged_kfold(n, n_splits, h, embargo):
    """k-fold purgé + embargo pour des cibles [t, t+h] indexées par ligne."""
    bounds = np.linspace(0, n, n_splits + 1).astype(int); t = np.arange(n)
    for k in range(n_splits):
        te0, te1 = bounds[k], bounds[k + 1]
        keep = (t + h < te0) | (t >= te1 + h + embargo)       # purge avant, purge + embargo après
        yield t[keep], np.arange(te0, te1)
```

**Quand l'utiliser ?** Pour choisir des hyperparamètres ou des variables à l'**intérieur** de la
période d'apprentissage, où la quantité de données compte. L'évaluation finale reste un walk-forward
suivi d'un coffre-fort (§ 4.4) : en crypto, les régimes changent (§ 9.5), et un k-fold qui apprend sur
2026 pour tester 2025 mesure une performance qu'on n'aurait jamais eue en temps réel.

### 4.3 CPCV : validation croisée combinatoire purgée (chap. 12)

On découpe la série en N groupes et on en teste k à la fois. Cela donne `C(N, k)` découpages et
`φ = (k/N)·C(N, k)` chemins de backtest complets. Par exemple, N = 6 et k = 2 donnent 15 découpages et
5 chemins. On obtient une **distribution** de Sharpe hors échantillon au lieu d'un seul chiffre, ce qui
alimente la PBO (§ 6.5).

```python
import itertools
def cpcv_splits(n, n_groups, k_test, h, embargo):
    bounds = np.linspace(0, n, n_groups + 1).astype(int); t = np.arange(n)
    for combo in itertools.combinations(range(n_groups), k_test):
        test = np.zeros(n, bool); keep = np.ones(n, bool)
        for g in combo:
            a, b = bounds[g], bounds[g + 1]; test[a:b] = True
            keep &= (t + h < a) | (t >= b + h + embargo)
        yield t[keep & ~test], t[test]
```

### 4.4 Protocole recommandé pour le projet

1. **Coffre-fort** : les 30 derniers jours disponibles ne servent à rien d'autre qu'à la validation
   finale, **une seule fois**, du ou des candidats retenus. Tout ce qui a été regardé sur ces jours
   compte comme un essai.
2. **Développement** : 12 mois précédents, avec `train_frac = 0,6` pour le signe et la sélection, puis
   un walk-forward de 5 plis purgé de h barres pour les modèles combinés.
3. **TimesFM** est zero-shot : il n'y a pas d'apprentissage, donc pas de fuite par l'ajustement. Mais
   les choix de `context_len`, de la transformation du contexte, du seuil sur `p_up` et des covariables
   sont des **hyperparamètres**. Ils se fixent sur la période de développement, jamais sur le test.
   **Contamination** : ne tester que sur des données postérieures au corpus d'entraînement du modèle. Le
   corpus déclaré de TimesFM 3.0 ne contient pas de crypto 1 min, voir [`timesfm.md`](timesfm.md) § 1.
4. **Actifs** : on sélectionne sur BTC et SOL, et on **confirme sans rien réajuster** sur ETH, XRP, BNB
   et DOGE, qui servent de réplication.

---

## 5. Taille d'échantillon, puissance, stride

Pour une accuracy vraie `p` et un test unilatéral à α = 5 % avec une puissance de 80 % :
`n_eff ≥ ((z_{0,95} + z_{0,80}) · 0,5 / (p − 0,5))²`.

| Accuracy vraie | n_eff requis | Jours de données à h = 15 (1 origine / 15 min) | Jours à h = 5 |
|---|---|---|---|
| 51 % | 15 457 | 161 | 54 |
| 52 % | 3 865 | 40 | 13 |
| 53 % | 1 718 | 18 | 6 |
| 55 % | 619 | 6,4 | 2,1 |
| 60 % | 155 | 1,6 | 0,5 |

**Conséquence pour TimesFM sur CPU**, qui coûte cher par origine : le mini-test de
[`timesfm.md`](timesfm.md) comptait environ 290 origines par actif sur 3,5 jours, avec un pas de 15,
soit n_eff ≈ 290. Il ne peut détecter qu'une accuracy vraie ≥ 57 %. Avec un pas de 5 et h = 15, les
mêmes 290 origines ne vaudraient que n_eff ≈ 100, et le seuil monterait à 62 %. Pour
conclure sur 52 %, il faut **au moins 3 900 origines non chevauchantes par actif et par horizon**. Avec
`stride = h`, cela fait 40 jours à h = 15 et 13 jours à h = 5, en lots de `batch_size = 32`.

```python
from scipy import stats
def n_eff_required(acc, alpha=0.05, power=0.8):
    z = stats.norm.isf(alpha) + stats.norm.isf(1 - power)
    return int(np.ceil((z * 0.5 / (acc - 0.5)) ** 2))
```

---

## 6. Tests multiples et data snooping

### 6.1 Compter les essais

Un « essai » est **toute configuration regardée** : indicateur × paramètre × horizon × actif × `eps` ×
seuil × transformation TimesFM × période. Avec 80 indicateurs, 3 horizons et 6 actifs, on atteint déjà
**1 440 tests**, avant même les variantes de paramètres. Hudson et Urquhart (2021) testent environ
15 000 règles techniques sur la crypto et doivent recourir à des procédures de tests multiples pour
conclure.

### 6.2 Le meilleur des essais inutiles

Sous H0, le maximum de N statistiques z indépendantes a pour espérance (Bailey et López de Prado 2014) :

```
E[max z] ≈ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)),   γ = 0,5772 (Euler-Mascheroni)
N = 10 : 1,57   N = 100 : 2,53   N = 1 000 : 3,26   N = 1 440 : 3,36   N = 10 000 : 3,86
```

Traduction en accuracy, avec un écart type de `0,5/√n_eff` :

| n_eff | Seuil 5 % unilatéral | t = 3 | **Meilleur de 1 440 indicateurs inutiles (espérance)** | Bonferroni 5 %/1 440 (z = 3,98) |
|---|---|---|---|---|
| 100 (backtest TimesFM court) | 58,2 % | 65,0 % | 66,8 % | 69,9 % |
| 1 000 | 52,6 % | 54,7 % | 55,3 % | 56,3 % |
| 3 500 | 51,4 % | 52,5 % | 52,8 % | 53,4 % |
| 14 016 (40 % OOS d'un an, h = 15) | 50,7 % | 51,3 % | **51,4 %** | 51,7 % |
| 35 040 (un an, h = 15) | 50,4 % | 50,8 % | **50,9 %** | 51,1 % |

Un « meilleur indicateur à 51,2 % OOS » sur 1 440 essais n'est donc **pas** un résultat.

### 6.3 FDR : Benjamini-Hochberg et Benjamini-Yekutieli

Procédure BH (1995) : on trie les m p-valeurs, puis on rejette les k premières, où k est le plus grand
indice tel que `p_(k) ≤ k·α/m`. BH contrôle le taux de fausses découvertes (FDR) sous indépendance ou
dépendance positive. Nos indicateurs sont fortement corrélés entre eux (RSI, z-score et rendement passé
mesurent la même chose). Benjamini-Yekutieli (2001) reste valide sous **n'importe quelle** dépendance,
au prix d'un facteur `c(m) = Σ 1/i ≈ ln m + 0,577`, soit 7,85 pour m = 1 440.

```python
from statsmodels.stats.multitest import multipletests
reject_bh, q_bh, _, _ = multipletests(pvals, alpha=0.05, method="fdr_bh")   # colonne q_value du contrat
reject_by, q_by, _, _ = multipletests(pvals, alpha=0.05, method="fdr_by")   # robuste à la dépendance
```

Les p-valeurs passées à BH doivent venir de tests **robustes au chevauchement** (PT HAC, t par jours).
Des p-valeurs naïves font accepter des centaines de fausses découvertes.

### 6.4 Le seuil « t > 3 » de Harvey, Liu et Zhu (2016)

Harvey, Liu et Zhu recensent plusieurs centaines de facteurs publiés et montrent qu'avec ce niveau de
data mining, le seuil habituel t > 2 n'a plus de sens. Ils recommandent **t > 3** pour un nouveau
facteur. Adaptation au projet : un indicateur ou modèle n'est « candidat » que si **|t_HAC| > 3 et
q_BH < 5 %**, avec le même signe sur BTC et SOL. Harvey et Liu (2015) proposent en outre une
« décote » (*haircut*) du Sharpe en fonction du nombre d'essais.

### 6.5 Reality check, SPA, deflated Sharpe, PBO

**Reality check de White (2000) et SPA de Hansen (2005).** H0 : aucun des M modèles ne bat le
benchmark. La statistique est le max des t, et sa distribution s'obtient par bootstrap. Le SPA
studentise et élimine les modèles très mauvais, ce qui le rend plus puissant. Romano et Wolf (2005)
proposent une version *stepwise* qui identifie les modèles gagnants. La version simplifiée ci-dessous
utilise un bootstrap de jours. Nous avons vérifié son niveau : 6 % de rejets à 5 % nominal, sur 100
simulations de 50 modèles nuls.

```python
def reality_check_max_t(daily_diff: pd.DataFrame, n_boot=2000, seed=0):
    """daily_diff : lignes = jours UTC, colonnes = modèles, valeur = moyenne journalière de
    (perte du benchmark − perte du modèle) ou rendement net. H0 : aucun modèle ne bat le benchmark."""
    X = daily_diff.to_numpy(float); D = len(X); rng = np.random.default_rng(seed)
    mu = X.mean(0); sd = X.std(0, ddof=1) / np.sqrt(D)
    t_obs = np.max(mu / sd); centered = X - mu
    t_boot = np.array([np.max(centered[rng.integers(0, D, D)].mean(0) / sd) for _ in range(n_boot)])
    return float(t_obs), float(np.mean(t_boot >= t_obs))
```

**Probabilistic et deflated Sharpe ratio** (Bailey et López de Prado 2014). SR̂ est le Sharpe **par
trade**, non annualisé, sur T trades, avec skewness γ₃ et kurtosis **brute** γ₄ (qui vaut 3 pour une loi
normale) :

```
PSR(SR*) = Φ( (SR̂ − SR*)·√(T−1) / √(1 − γ₃·SR̂ + (γ₄ − 1)/4 · SR̂²) )
SR₀      = √V[SR_essais] · ( (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) )
DSR      = PSR(SR₀)
```

```python
EULER = 0.5772156649015329
def expected_max_z(n_trials):
    return (1 - EULER) * stats.norm.ppf(1 - 1 / n_trials) + EULER * stats.norm.ppf(1 - 1 / (n_trials * np.e))

def probabilistic_sharpe(sr, T, skew, kurt, sr_ref=0.0):
    return stats.norm.cdf((sr - sr_ref) * np.sqrt(T - 1) / np.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2))

def deflated_sharpe(sr, T, skew, kurt, sr_trials_var, n_trials):
    return probabilistic_sharpe(sr, T, skew, kurt, sr_ref=np.sqrt(sr_trials_var) * expected_max_z(n_trials))
```

**Exemple chiffré.** Une stratégie BTC à 15 min gagne **+1 pb net par trade**, avec un écart type de
23,7 pb (mesuré), soit un SR par trade de 0,042. On prend γ₃ = −0,06, γ₄ = 18,9 (mesurés sur `r_15`
non chevauchant) et `V[SR_essais] = 1/T` (essais nuls). Pour mémoire, un SR de 0,042 par trade donne
environ 7,9 en annualisé ×√35 040. Ce chiffre est trompeur, car les trades ne sont pas tous
réalisables.

| T (trades) | ≈ jours | PSR(0) | DSR, N = 50 essais | DSR, N = 500 |
|---|---|---|---|---|
| 1 000 | 10 | 0,908 | 0,174 | 0,044 |
| 3 000 | 31 | 0,989 | 0,514 | 0,230 |
| 10 000 | 104 | 1,000 | 0,973 | 0,877 |

Avec 31 jours de trades et 50 variantes essayées, un PSR de 99 % tombe à un DSR de 51 % : ce n'est
pas mieux qu'une pièce. **Consigner N honnêtement** : c'est la « troisième loi » de López de Prado, selon
laquelle tout backtest doit être publié avec tous les essais qui l'ont produit.

**PBO** (Bailey, Borwein, López de Prado et Zhu 2017) : on répartit les périodes en S blocs, puis on
évalue toutes les combinaisons moitié apprentissage, moitié test (CSCV). La PBO est la fréquence à
laquelle la meilleure configuration en apprentissage finit sous la médiane en test. **Une PBO > 0,5
signifie que la sélection fait pire que le hasard.** Le calcul est faisable avec `cpcv_splits` et une
matrice (configurations × blocs) de rendements nets.

---

## 7. Baselines indispensables

Toute prévision se compare à ces règles, calculées avec **exactement** les mêmes origines, labels et
coûts. Le contrat les prévoit dans `baseline_predictions` : `momentum_h`, `reversal_h`, `always_up`,
`majority_prev_day`. Il faut y ajouter `random` et la climatologie pour les probabilités.

| Baseline | Règle | Rôle |
|---|---|---|
| `always_up` | ŷ = 1 | révèle un avantage dû au seul taux de base |
| `climatologie` | p = ȳ de l'apprentissage | référence du Brier skill score et du DM sur les probabilités |
| `random` (marche aléatoire) | ŷ ~ Bernoulli(½), ou p = ½ | niveau du hasard ; pour une prévision de prix, la marche aléatoire est « médiane = dernier prix » |
| `momentum_h` | ŷ = 1{r passé sur h > 0} | continuation |
| `reversal_h` | ŷ = 1{r passé sur h < 0} | **retournement : le vrai rival à 5–15 min** |
| `majority_prev_day` | direction de la veille UTC | inertie journalière |

**Mesures sur 12 mois (01/09/2025 → 31/08/2026).** Accuracy sur toutes les origines ; `p_w` =
accuracy pondérée par |r| ; pb bruts = moyenne sur des trades **non chevauchants** (un toutes les h
barres), avec une erreur type d'environ 0,1 à 0,2 pb ; t_HAC avec lags = h ; t par jours sur 365 jours.

| Actif | h | Baseline | Accuracy | p_w | pb bruts / trade | t_HAC | t par jours |
|---|---|---|---|---|---|---|---|
| BTC | 5 | always_up | 49,75 % | 49,83 % | −0,03 | −2,2 | −2,1 |
| BTC | 5 | momentum_5 | 48,72 % | 49,48 % | −0,15 | −13,5 | −11,2 |
| BTC | 5 | **reversal_5** | **51,28 %** | 50,52 % | +0,15 | 13,5 | 11,2 |
| BTC | 5 | majority_prev_day | 50,11 % | 50,32 % | +0,06 | 0,9 | 0,9 |
| BTC | 5 | random | 50,00 % | 50,02 % | −0,06 | −0,1 | −0,1 |
| BTC | 15 | always_up | 49,82 % | 49,70 % | −0,09 | −1,0 | −1,0 |
| BTC | 15 | momentum_15 | 48,11 % | 49,14 % | −0,36 | −12,6 | −11,4 |
| BTC | 15 | **reversal_15** | **51,89 %** | 50,86 % | +0,36 | 12,6 | 11,4 |
| BTC | 15 | majority_prev_day | 50,10 % | 50,61 % | +0,19 | 0,6 | 0,6 |
| SOL | 5 | always_up | 49,91 % | 49,78 % | −0,07 | −0,7 | −0,7 |
| SOL | 5 | **reversal_5** | **51,57 %** | 50,49 % | +0,16 | 15,8 | 13,5 |
| SOL | 5 | majority_prev_day | 50,28 % | 50,30 % | +0,08 | 2,4 | 2,5 |
| SOL | 10 | **reversal_10** | **51,71 %** | 50,42 % | +0,01 | 13,3 | 12,6 |
| SOL | 15 | **reversal_15** | **52,20 %** | 50,81 % | +0,47 | 14,3 | 13,5 |
| SOL | 15 | majority_prev_day | 50,26 % | 50,56 % | +0,31 | 1,3 | 1,4 |
| SOL | 15 | random | 49,88 % | 49,78 % | −0,54 | −1,8 | −1,8 |

Ce qu'il faut en retenir :

* **Barre à franchir** : pour prétendre à une compétence directionnelle, un modèle à 15 min doit faire
  mieux que 51,9 % (BTC) et 52,2 % (SOL), et le montrer par un DM sur Brier et un PT HAC **contre
  `reversal_h`**. [`timesfm.md`](timesfm.md) montre que la P(hausse) de TimesFM est corrélée à −0,4
  ou −0,55 au rendement de la dernière heure. Une bonne partie de ce que TimesFM « sait » est donc
  déjà contenue dans cette baseline gratuite.
* **Le retournement est très significatif et économiquement nul** : +0,15 à +0,47 pb bruts par trade,
  pour 10 à 20 pb de coûts (§ 10).
* **Latence d'une barre.** Si l'on n'exécute qu'à la clôture de la barre suivante, `reversal_15` passe
  de 51,89 % à 51,87 % (BTC) et de 52,20 % à 52,14 % (SOL). `reversal_5` passe de 51,28 % à 51,12 %
  (BTC) et de 51,57 % à 51,25 % (SOL). L'effet est faible à ces horizons, mais il faut le mesurer
  pour chaque signal rapide (flux, microstructure).

---

## 8. Coûts : combien de points de base battre, et quelle précision cela exige

### 8.1 Frais Binance (niveau régulier / VIP 0, vérifiés le 25/09/2026)

| Marché | Maker | Taker | Remise BNB | Aller-retour (pb) |
|---|---|---|---|---|
| Spot | 0,100 % | 0,100 % | −25 % → 0,075 % | taker ou maker : **20** ; avec BNB : **15** |
| Futures USDⓈ-M | 0,020 % | 0,050 % | −10 % → 0,018 / 0,045 % | taker/taker **10** ; maker/maker **4** ; maker puis taker **7** ; taker avec BNB **9** |

Sources : grille spot `binance.com/en/fee/schedule` (« Regular User 0.100 % / 0.100 % », BNB
0,075 %) et FAQ futures Binance (« maker fee is 0.02 % and taker fee is 0.05 % », remise BNB de 10 %).
Ces chiffres correspondent à `config.COST_BPS` (10, 20, 4). **Au niveau VIP 0 spot, maker et taker
coûtent la même chose** : passer par des ordres limites n'y économise que le spread. Les ordres maker ne
sont pas gratuits pour autant : risque de non-exécution et **sélection adverse**, car on est exécuté
surtout quand le prix va contre soi. **Funding** des perpétuels : 8 h ; en août 2026, le taux BTCUSDT
vaut en moyenne 0,0066 % par période (maximum 0,01 %). Un trade de 15 min ne traverse une échéance
que dans 3 % des cas, pour un coût espéré d'environ 0,02 pb : négligeable.

### 8.2 Spread et slippage mesurés

**Spread spot**, relevé sur `bookTicker` le 25/09/2026 vers 16 h 40 UTC. Le spread est d'un tick sur
toutes les paires :

| Paire | Prix | Tick | Spread (pb) |
|---|---|---|---|
| BTCUSDT | 83 930 | 0,01 | **0,0012** |
| ETHUSDT | 2 690 | 0,01 | 0,037 |
| BNBUSDT | 774 | 0,01 | 0,13 |
| XRPUSDT | 1,570 | 0,0001 | 0,64 |
| SOLUSDT | 121,44 | 0,01 | **0,82** |
| DOGEUSDT | 0,0980 | 0,00001 | 1,02 |

**Impact d'un ordre au marché sur un côté**, en pb par rapport au mid : moyenne achat/vente sur 10
instantanés du carnet spot (`depth`, 5 000 niveaux). Un aller-retour coûte le double.

| Paire | 10 k$ | 100 k$ | 1 M$ |
|---|---|---|---|
| BTCUSDT | 0,0006 | 0,0006 | 1,12 |
| SOLUSDT | 0,41 (½ spread) | 2,11 | 8,33 |

**Profondeur futures** (`bookDepth`, dimanche 20/09/2026, médiane) : BTC a 43 M$ de chaque côté à
±0,2 % et environ 190 M$ à ±1 %. SOL a 3,4 M$ à ±0,2 % et environ 18 M$ à ±1 %. En supposant la
liquidité uniforme entre 0 et 0,2 %, un ordre de 100 k$ sur les futures SOL coûte environ 0,3 pb
d'impact plus le demi-spread (≈ 0,4 pb). Un ordre de 1 M$ coûte environ 3 pb. Ce sont des ordres de
grandeur : le carnet futures en temps réel (`fapi`) n'est pas accessible d'ici (erreur 451). D'après les
klines futures, le tick de SOL semble valoir 0,01, soit environ 0,8 pb.

**Budget de coût aller-retour (pb)** pour une taille de 10 k$ à 100 k$ :

| Scénario | BTC | SOL |
|---|---|---|
| Futures, maker des deux côtés (sans compter la sélection adverse) | ≈ 4 | ≈ 4 |
| Futures, taker des deux côtés | ≈ 10 | ≈ 11 (10 + spread 0,8 + impact) |
| Spot, taker, BNB | ≈ 15 | ≈ 16 (10 k$) à 19 (100 k$) |
| Spot, taker | ≈ 20 | ≈ 21 (10 k$) à **24** (100 k$) |

### 8.3 Du coût au seuil de précision

On prend position `s = ±1` à l'origine t et on la tient h barres. Le gain brut est `g = s·r_h` et le
gain net `g − c`. Si l'exactitude est **indépendante de l'amplitude** :

```
E[g] = p·E|r| − (1−p)·E|r| = (2p − 1)·E|r|      ⇒     p* = ½ + c / (2·E|r|)
```

Plus généralement, `E[g] = (2·p_w − 1)·E|r|`, où `p_w = Σ|r|·1{correct} / Σ|r|` est l'accuracy
**pondérée par l'amplitude**. **La condition de rentabilité exacte est `p_w > p*`.** L'accuracy
ordinaire ne l'implique pas.

### 8.4 Mesure sur données réelles (API `data-api.binance.vision`, 30 derniers jours)

| Actif | h | E\|r_h\| (pb) | médiane (pb) | p* à 4 pb (maker fut.) | **p* à 10 pb** (taker fut.) | **p* à 20 pb** (taker spot) | part des \|r\| > 10 pb |
|---|---|---|---|---|---|---|---|
| **BTC** | 5 | 7,4 | 5,0 | 77,0 % | **> 100 %** (117 %) | > 100 % | 24 % |
| **BTC** | 10 | 10,5 | 7,0 | 69,1 % | **97,8 %** | > 100 % | 37 % |
| **BTC** | 15 | 12,7 | 8,5 | 65,7 % | **89,3 %** | > 100 % (129 %) | 44 % |
| **SOL** | 5 | 13,0 | 9,2 | 65,4 % | **88,4 %** | > 100 % | 46 % |
| **SOL** | 10 | 18,3 | 12,8 | 60,9 % | **77,3 %** | > 100 % | 59 % |
| **SOL** | 15 | 22,2 | 15,5 | 59,0 % | **72,5 %** | **95,0 %** | 65 % |
| ETH | 15 | 17,0 | 11,5 | 61,8 % | 79,4 % | > 100 % | 55 % |
| XRP | 15 | 25,6 | 17,6 | 57,8 % | 69,5 % | 89,1 % | 68 % |
| BNB | 15 | 14,9 | 10,5 | 63,4 % | 83,5 % | > 100 % | 52 % |
| DOGE | 15 | 25,7 | 17,1 | 57,8 % | 69,5 % | 89,0 % | 68 % |

Sur 12 mois (septembre 2025 → août 2026), les chiffres sont proches. BTC : E|r| = 8,8, 12,4 et
15,2 pb pour h = 5, 10 et 15, d'où p* à 10 pb de 107 %, 90 % et 83 %. SOL : 14,1, 19,9 et 24,4 pb,
d'où p* de 85 %, 75 % et 70,5 %.

**Lecture.** Une prévision directionnelle **sur toutes les barres** en taker ne peut pas être
rentable : il faudrait 70 à 90 % de précision là où la littérature et nos mesures plafonnent à 52–53 %.
Sur BTC à 5 min, E|r| < 10 pb, donc **un oracle qui connaîtrait toujours le signe perdrait de
l'argent** en taker futures. Trois leviers seulement : **trader rarement** (filtrer sur l'amplitude
attendue), **allonger l'horizon** (E|r| croît comme √h tandis que c reste fixe) et **baisser c**
(ordres maker, niveau VIP). La même mesure sur l'API se reproduit ainsi (en production, passer par
`data.fetch_ohlcv`) :

```python
import numpy as np, pandas as pd, requests, time
from tradebot.targets import future_log_return

def klines_api(symbol, days=30):
    end = int(time.time() * 1000) // 60000 * 60000 - 60000; cur = end - days * 86_400_000; rows = []
    while cur < end:
        b = requests.get("https://data-api.binance.vision/api/v3/klines",
                         params=dict(symbol=symbol, interval="1m", startTime=cur, limit=1000), timeout=30).json()
        if not b: break
        rows += b; cur = b[-1][0] + 60_000
    df = pd.DataFrame(rows).iloc[:, :6].astype(float); df.columns = ["t", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("t").astype("int64"), unit="ms", utc=True)
    return df[~df.index.duplicated()].sort_index()

def break_even_accuracy(abs_ret_bps, cost_bps):
    """p* si l'exactitude est indépendante de l'amplitude : 1/2 + c / (2 E|r|)."""
    return 0.5 + cost_bps / (2 * float(np.mean(abs_ret_bps)))

df = klines_api("SOLUSDT")
for h in (5, 10, 15):
    a = future_log_return(df, h).dropna().abs() * 1e4
    print(h, round(a.mean(), 1), [round(break_even_accuracy(a, c), 3) for c in (4, 10, 20)])
```

### 8.5 Le filtre de volatilité change tout

On conditionne sur la volatilité réalisée des 60 dernières minutes, une variable **causale**. E|r_h|
(pb) par décile de volatilité, sur 12 mois :

| | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC h = 5 | 3,0 | 4,6 | 5,5 | 6,3 | 7,2 | 8,2 | 9,4 | 10,9 | 13,1 | **19,4** |
| BTC h = 15 | 5,4 | 8,2 | 9,8 | 11,1 | 12,7 | 14,4 | 16,1 | 18,9 | 22,5 | **32,8** |
| SOL h = 5 | 5,9 | 7,9 | 9,2 | 10,6 | 11,9 | 13,4 | 14,8 | 17,2 | 20,4 | **29,6** |
| SOL h = 15 | 10,4 | 14,1 | 16,0 | 18,5 | 21,0 | 23,2 | 25,6 | 29,9 | 35,0 | **50,2** |

Seuil p* dans le décile de volatilité le plus haut, sur les 30 derniers jours :

| | 4 pb | 10 pb | 20 pb |
|---|---|---|---|
| BTC h = 5 | 63,0 % | 82,5 % | > 100 % |
| BTC h = 15 | 58,0 % | **70,1 %** | 90,2 % |
| SOL h = 5 | 58,3 % | 70,9 % | 91,7 % |
| SOL h = 15 | **55,2 %** | **63,1 %** | 76,1 % |

Un filtre sur la volatilité ou l'amplitude attendue, par exemple l'intervalle q90 − q10 de TimesFM
dont la couverture est bonne selon [`timesfm.md`](timesfm.md), est donc **un préalable**. Le filtre
n'améliore pas la direction : il sélectionne les moments où une précision modeste peut suffire.

### 8.6 Accuracy ou amplitude : l'exemple qui fâche

Nous avons entraîné un logit walk-forward (5 plis, `TimeSeriesSplit(gap=15)`) sur trois z-scores de
rendement passé (5, 15 et 60 min, normalisés par la volatilité sur 60 min), à h = 15 :

| | BTC | SOL |
|---|---|---|
| n OOS | 437 600 | 429 825 |
| Accuracy / balanced accuracy | 52,13 % / 52,13 % | 52,34 % / 52,34 % |
| AUC [IC 95 %, bootstrap de jours] | 0,530 [0,525 ; 0,534] | 0,531 [0,527 ; 0,536] |
| Brier (climatologie 0,2500) / BSS | 0,2493 / 0,28 % | 0,2493 / 0,29 % |
| DM sur Brier contre climatologie (t HAC 2h / t par jours) | 7,3 / 7,2 | 7,1 / 7,3 |
| PT HAC | 11,9 | 13,0 |
| pb bruts / trade, tous signaux (non chevauchants) | +0,15 | +0,39 |
| 10 % les plus confiants : trades / hit-rate / pb bruts / E\|r\| | 7 704 / 54,2 % / **+0,19** / 17,9 | 7 607 / 54,8 % / **+0,19** / 27,7 |
| 1 % les plus confiants : trades / hit-rate / pb bruts / E\|r\| | 1 167 / **61,1 %** / **+1,75** / 22,3 | 1 078 / 55,9 % / +0,90 / 35,1 |

Sur BTC, un hit-rate de 61 % sur des mouvements moyens de 22 pb devrait rapporter (2·0,61 − 1)·22 ≈
4,9 pb si l'exactitude était indépendante de l'amplitude. On observe 1,75 pb. Le signal de
retournement a raison sur les petits mouvements et **tort sur les grands** : les emballements
directionnels sont justement ceux où « parier contre » échoue. Il faut **toujours reporter `p_w`** et
le rendement net par trade, jamais l'accuracy seule.

```python
def magnitude_weighted_accuracy(ret, y_pred):
    """Part de l'amplitude totale |r| captée dans le bon sens : c'est elle qui paie."""
    r = np.asarray(ret, float); s = np.where(np.asarray(y_pred) > 0.5, 1.0, -1.0)
    return float(np.sum(np.abs(r) * (np.sign(r) == s)) / np.sum(np.abs(r)))
```

### 8.7 Le seuil bouge avec le régime

E|r_15| (pb) sur 12 mois :

* **Par mois** : BTC de 9,8 (septembre 2025) à 24,2 (février 2026) ; SOL de 17,0 (mai 2026) à 35,3
  (février 2026). Le p* de BTC à 10 pb va donc de plus de 100 % à 71 % selon le mois.
* **Semaine ou week-end** : BTC 17,0 contre 10,6 ; SOL 26,4 contre 19,4. [`indicateurs.md`](indicateurs.md)
  montre que le retournement est plus fort le week-end, mais les mouvements y sont 25 à 40 % plus petits.
* **Heure UTC** : le maximum est à 14 h (BTC 24,0, SOL 36,1, ouverture américaine), le minimum à
  10–11 h (BTC 11,6, SOL 18,9).

Il faut évaluer la rentabilité **par régime** et vérifier que le gain ne vient pas d'un seul mois
agité.

---

## 9. Biais

### 9.1 Look-ahead (fuite du futur)

| Piège | Symptôme | Parade |
|---|---|---|
| Normaliser avec des statistiques de tout l'échantillon (z-score global, `StandardScaler` ajusté sur tout) | IC gonflé et instable hors échantillon | fenêtres glissantes causales ; scaler ajusté dans le pli d'apprentissage (`Pipeline`) |
| `rolling(..., center=True)`, `shift(-k)`, `bfill`, interpolation | accuracy anormale, souvent > 55 % | test de causalité du contrat : tronquer après t, la valeur en t doit être identique |
| `resample` avec des mauvais `label`/`closed` | décalage d'une barre | horodatage à l'**ouverture**, comme dans le contrat ; vérifier sur un exemple |
| Barre en cours non clôturée | la dernière valeur change en direct | `data.py` la supprime |
| Unité d'horodatage (µs dans les zips spot 2025+, ms dans l'API) | dates en 1970 ou en 55 000 | détecter l'unité par la magnitude (voir `ARCHITECTURE.md`) |
| Exécution au prix même qui a produit le signal | gain illusoire sur les signaux rapides | mesurer aussi avec une barre de latence (§ 7) |
| Sélection de variables ou d'hyperparamètres sur la période de test | la performance OOS s'effondre en direct | coffre-fort (§ 4.4) ; registre des essais |

### 9.2 Décalage de publication des données dérivées : vérifié

Le contrat impose un décalage prudent : une métrique horodatée `T` n'est utilisable qu'à partir de la
barre qui s'ouvre à `T + 5 min`. **Nous l'avons vérifié empiriquement** sur 3 jours (18 au
20/09/2026, 864 relevés) en recoupant les fichiers `metrics` avec les klines futures 1m. On a calculé la
corrélation de `log(sum_taker_long_short_vol_ratio)` au temps T avec `log(achats taker / ventes taker)`
sur différentes fenêtres :

| Fenêtre | [T−10, T−5) | [T−5, T) | **[T, T+5)** | [T+5, T+10) |
|---|---|---|---|---|
| BTC | 0,056 | 0,132 | **1,000** | 0,132 |
| SOL | 0,044 | 0,093 | **0,999** | 0,092 |

Le ratio taker horodaté T **résume les 5 minutes qui suivent T**. L'horodatage marque le début de la
fenêtre. Une jointure *as-of* sans décalage donne donc au modèle le flux des 5 prochaines minutes :

| Alignement du ratio taker | BTC hit-rate / IC sur r_5 | SOL hit-rate / IC |
|---|---|---|
| as-of à T (**fuite**) | **72,5 % / +0,63** | **72,7 % / +0,60** |
| as-of à T + 5 min (contrat) | 48,8 % / −0,05 | 47,2 % / −0,05 |

Tout résultat de plus de 60 % à 5 minutes impliquant une variable dérivée doit faire soupçonner **cette
fuite en premier**. Deux autres points :

* **Disponibilité en production.** Les fichiers journaliers `metrics` sont publiés **environ 7 h après
  la fin du jour** : le fichier du 24/09 est modifié pour la dernière fois le 25/09 à 06:56 UTC. En
  direct, il faudrait l'API `fapi` (`/futures/data/openInterestHist`, qui ne remonte que 30 jours),
  **bloquée ici (erreur 451)**. Une variable disponible en backtest mais pas en direct est un biais en
  soi. Il faut le noter dans chaque résultat qui en dépend.
* **Funding** : `last_funding_rate` n'est connu qu'à `calc_time`. Le taux *prévisionnel* diffusé en
  continu par l'exchange n'existe pas dans les fichiers en masse. Ne pas le reconstruire avec des
  valeurs futures.

### 9.3 Survivorship et choix de l'univers

L'univers (BTC, SOL, ETH, XRP, BNB, DOGE) est choisi **aujourd'hui**, parmi les survivants devenus
gros. Ammann, Burdorf, Liebi et Stöckl (2022) mesurent un biais annualisé de 0,93 % pour les
portefeuilles pondérés par la capitalisation et de **62 %** pour les équipondérés (3 904 cryptos,
2014–2021). Une partie des anomalies publiées disparaît après correction. À 5–15 minutes, le biais porte
moins sur le rendement moyen que sur la **liquidité** : nos six paires ont un spread d'un tick et des
carnets profonds, ce qui n'est pas vrai de toute la cote. Pour généraliser aux altcoins :

* inclure des paires **radiées ou effondrées**. `data.binance.vision` conserve leur historique (les
  fichiers `LUNAUSDT` d'avril 2022 et `FTTUSDT` d'octobre 2022 répondent 200) ;
* se méfier des **symboles réutilisés** ou renommés (LUNA après l'effondrement de Terra en mai 2022) et
  vérifier la continuité des prix ;
* fixer l'univers selon une règle datée, par exemple le top N en volume **à la date de l'origine**, et
  non selon le classement actuel.

### 9.4 Data snooping

Voir § 6. En pratique, le « jardin des sentiers qui bifurquent » compte autant que le nombre
d'indicateurs : `eps`, le seuil de `p_up`, `context_len`, la transformation du contexte, la fenêtre de
volatilité, le choix des mois et des actifs sont autant de degrés de liberté. **Tout choix fait après
avoir vu un résultat OOS transforme l'OOS en apprentissage.**

### 9.5 Changements de régime propres à la crypto

* **Microstructure de Binance** : du 8 juillet 2022 au 22 mars 2023, les paires BTC spot étaient sans
  frais. Les échanges sans frais ont représenté 70 à 85 % du volume hebdomadaire (Kaiko), puis le volume
  BTC-USDT s'est effondré à la fin de la promotion (CoinDesk). Les variables `volume`, `trades` et flux
  taker ne sont **pas comparables** de part et d'autre. Les ticks, les niveaux VIP et les frais évoluent
  aussi.
* **Chocs** : Terra/LUNA (mai 2022), FTX (novembre 2022), ETF spot américains (janvier 2024), halving
  (avril 2024). Ce sont des ruptures de volatilité, de corrélation BTC/altcoins et de liquidité.
* **Volatilité** : E|r_15| de BTC varie d'un facteur 2,5 d'un mois à l'autre (§ 8.7).
* **Parade** : reporter les métriques **par mois**, par actif et par période semaine/week-end. Exiger
  le même signe dans au moins 70 % des mois et sur au moins 4 actifs sur 6. Tester la stabilité avec
  des fenêtres glissantes (`max_train_size`) et non seulement croissantes.

### 9.6 Prix de transaction, rebond bid-ask et égalités

Le `close` d'une kline est le **dernier prix échangé**, qui alterne entre bid et ask. Ce rebond crée
un faux retournement de l'ordre du spread (Roll 1984). Nous l'avons mesuré sur 12 mois : l'autocorrélation
des rendements 1 min vaut −0,005 sur BTC et +0,003 sur SOL, donc le rebond est faible à 1 min sur ces
paires. Mais sur SOL et DOGE, le tick de 0,8 à 1 pb n'est pas négligeable face à un avantage brut de
0,2 pb. Pour les résultats économiques, **évaluer aussi sur des prix exécutables** : acheter au ask,
vendre au bid, ou appliquer la pénalité de spread du § 8.2. Il faut aussi reporter la part des
égalités (`r = 0`) exclues : 3,2 % sur SOL à 5 min.

### 9.7 Contamination des modèles pré-entraînés

Un modèle fondation dont le corpus couvre la période de test l'a potentiellement « vue ». Pour
TimesFM 3.0, aucun jeu crypto 1 min n'est déclaré ([`timesfm.md`](timesfm.md) § 1). Pour Kronos,
entraîné sur Binance 1 min jusqu'en juin 2024, il faut tester après juin 2024. Règle générale : tester
sur des données **postérieures** à la date de fin du corpus déclaré.

---

## 10. Seuil statistique contre seuil économique

### 10.1 Deux verrous distincts

Leitch et Tanner (1991) montrent que les métriques d'erreur classiques (RMSE) sont peu liées au profit,
et que la **précision directionnelle** l'est davantage. Granger et Pesaran (2000) plaident pour évaluer
une prévision selon la **décision** qu'elle sert. Notre cas l'illustre dans les deux sens :

* **Statistiquement fort, économiquement nul** : `reversal_15` sur BTC a un t par jours de 11,4, un PT
  HAC de 12,1 et une p-valeur inférieure à 10⁻³⁰. Il rapporte **+0,36 pb brut par trade** pour 10 pb
  de coût.
* **Économiquement séduisant, statistiquement fragile** : un backtest court (300 origines) qui affiche
  +3 pb nets par trade a un IC à 95 % de ±5 à 7 pb (écart type de r_15 de 24 pb pour BTC à 38 pb pour SOL,
  n_eff ≈ 100).
  Ce n'est qu'une anecdote.

### 10.2 Grille de décision proposée pour la phase 1

Un indicateur, un modèle ou TimesFM est **retenu pour la phase 2** seulement si **toutes** les
conditions suivantes tiennent sur la période OOS de développement, puis sur le coffre-fort.

**Statistique**

1. PT HAC (lags = 2h) : t > 3 sur BTC **et** sur SOL. q_BH < 5 % sur la grille complète des essais.
2. DM sur Brier (ou log-loss) contre `reversal_h` **et** contre la climatologie : t < −2 (le modèle
   fait mieux), HAC et t par jours.
3. Même signe d'effet sur au moins 4 actifs sur 6 et dans au moins 70 % des mois.

**Économique**

4. `p_w > p*` pour le scénario de coût visé : 10 pb en taker futures, ou 4 pb plus la sélection adverse
   en maker.
5. Rendement net moyen par trade (non chevauchant, après spread et slippage du § 8.2) : borne basse de
   l'IC à 95 % (bootstrap de jours) > 0.
6. DSR > 0,95 calculé avec le **nombre réel d'essais** N ; PBO < 0,2 si une sélection a eu lieu.
7. Au moins 3 865 trades non chevauchants OOS : c'est la taille qui permet de détecter 52 % (§ 5).

Si seules les conditions 1 à 3 tiennent, l'indicateur est **informatif mais non tradable seul**. Il
devient une variable candidate du modèle combiné, ou un filtre.

---

## 11. Assemblage : `direction_metrics` de référence

Implémentation conforme au contrat de `backtest.direction_metrics`. On passe `h = ceil(h / stride)`
quand les origines sont espacées de `stride` barres (§ 2.4).

```python
import numpy as np
from scipy import stats
from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, log_loss, roc_auc_score

def direction_metrics(y_true, p_up, h: int) -> dict:
    y = np.asarray(y_true, float); p = np.clip(np.asarray(p_up, float), 1e-6, 1 - 1e-6)
    ok = ~(np.isnan(y) | np.isnan(p)); y, p = y[ok], p[ok]
    pred = (p > 0.5).astype(float); n = len(y); acc = accuracy_score(y, pred)
    n_eff = max(n // h, 1)
    p_binom = stats.binomtest(int(round(acc * n_eff)), n_eff, 0.5, alternative="greater").pvalue
    pt_stat, pt_p = pesaran_timmermann(y, pred) if 0 < pred.mean() < 1 else (np.nan, np.nan)
    return dict(n=n, accuracy=acc, balanced_accuracy=balanced_accuracy_score(y, pred),
                auc=roc_auc_score(y, p), brier=brier_score_loss(y, p), log_loss=log_loss(y, p),
                p_binom=p_binom, pt_stat=pt_stat, pt_pvalue=pt_p)
```

Colonnes à ajouter au rapport, **hors contrat** mais recommandées : `pt_hac_t` (lags = 2h),
`t_daily`, `acc_w` (`p_w`), `bss`, `dm_vs_reversal_t`, `net_bps_per_trade` et son IC par bootstrap de
jours, `coverage` (part d'origines non NaN), `n_trials`.

Toutes les fonctions de ce document ont été exécutées sur les données décrites en tête, avec le venv du
projet (numpy 2, pandas 3, scipy, statsmodels, scikit-learn).

---

## 12. Checklist « avant de croire un résultat »

**Données et causalité**

- [ ] Le test de causalité passe pour chaque variable : tronquer après t ne change pas la valeur en t.
- [ ] Les variables dérivées sont alignées *as-of* **T + 5 min** ; aucun résultat > 60 % à 5 min ne
      reste inexpliqué (§ 9.2).
- [ ] Les normalisations, scalers et seuils sont ajustés **dans** le pli d'apprentissage.
- [ ] Les trous de données donnent des cibles NaN ; la couverture et la part d'égalités sont
      reportées.
- [ ] La variable existe **en production**, avec le même délai : pas de fichier en masse publié à
      J+1 pour un signal en direct.

**Statistique**

- [ ] n_eff est annoncé (n/h, ou n·stride/h) et la puissance est suffisante (§ 5).
- [ ] Les tests sont robustes au chevauchement : PT HAC (lags ≥ 2h), t par jours, bootstrap de
      jours. Les p-valeurs naïves sont bannies.
- [ ] La comparaison se fait contre **les 5 baselines**, surtout `reversal_h`, avec un DM sur Brier.
- [ ] Balanced accuracy et AUC sont reportées, pas seulement l'accuracy ; les probabilités sont
      calibrées.
- [ ] Le nombre total d'essais N est consigné ; q_BH (ou BY) < 5 % ; |t| > 3 ; le résultat est
      au-dessus du « meilleur des inutiles » du § 6.2.
- [ ] Le signe est stable : BTC **et** SOL, au moins 4 actifs sur 6, au moins 70 % des mois ;
      semaine et week-end sont regardés séparément.

**Économique**

- [ ] Les coûts réalistes sont inclus : frais (§ 8.1) + spread + slippage pour la taille visée
      (§ 8.2), et une barre de latence.
- [ ] `p_w > p*` : l'accuracy seule ne suffit pas.
- [ ] Le rendement net par trade (trades non chevauchants) a une borne basse d'IC à 95 % > 0.
- [ ] DSR > 0,95 avec le vrai N ; PBO < 0,2 si une sélection a eu lieu.
- [ ] Le résultat tient sur le **coffre-fort**, ouvert une seule fois.

**Hygiène**

- [ ] Pour un modèle pré-entraîné, la période de test est postérieure à son corpus.
- [ ] L'univers est défini par une règle datée ; les paires radiées sont incluses si l'on généralise.
- [ ] TimesFM 3.0 reste en recherche uniquement (licence non commerciale, voir `ARCHITECTURE.md`).
- [ ] Le script, la graine, les dates et le commit sont archivés dans `reports/<étude>/`.

---

## 13. Références

**Tests de prévision**

- **[PT92]** Pesaran, M. H. et Timmermann, A. (1992). *A Simple Nonparametric Test of Predictive
  Performance*. Journal of Business & Economic Statistics 10(4), 461–465.
  https://www.tandfonline.com/doi/abs/10.1080/07350015.1992.10509922
- **[PT09]** Pesaran, M. H. et Timmermann, A. (2009). *Testing Dependence Among Serially Correlated
  Multicategory Variables*. JASA 104(485), 325–337. https://www.tandfonline.com/doi/abs/10.1198/jasa.2009.0113
- **[AG05]** Anatolyev, S. et Gerko, A. (2005). *A Trading Approach to Testing for Predictability*.
  JBES 23(4), 455–461. https://www.tandfonline.com/doi/abs/10.1198/073500104000000640
- **[DM95]** Diebold, F. X. et Mariano, R. S. (1995). *Comparing Predictive Accuracy*. JBES 13(3), 253–263.
- **[HLN97]** Harvey, D., Leybourne, S. et Newbold, P. (1997). *Testing the equality of prediction mean
  squared errors*. International Journal of Forecasting 13(2), 281–291.
  https://ideas.repec.org/a/eee/intfor/v13y1997i2p281-291.html
- **[CW07]** Clark, T. E. et West, K. D. (2007). *Approximately normal tests for equal predictive accuracy
  in nested models*. Journal of Econometrics 138(1), 291–311.
  https://ideas.repec.org/a/eee/econom/v138y2007i1p291-311.html
- **[GW06]** Giacomini, R. et White, H. (2006). *Tests of Conditional Predictive Ability*. Econometrica
  74(6), 1545–1578.

**Autocorrélation et bootstrap**

- **[NW87]** Newey, W. K. et West, K. D. (1987). *A Simple, Positive Semi-Definite, Heteroskedasticity and
  Autocorrelation Consistent Covariance Matrix*. Econometrica 55(3), 703–708. Et (1994), *Automatic Lag
  Selection in Covariance Matrix Estimation*, Review of Economic Studies 61(4), 631–653.
- **[HH80]** Hansen, L. P. et Hodrick, R. J. (1980). *Forward Exchange Rates as Optimal Predictors of
  Future Spot Rates*. Journal of Political Economy 88(5), 829–853 (cibles chevauchantes).
- **[PR94]** Politis, D. N. et Romano, J. P. (1994). *The Stationary Bootstrap*. JASA 89(428), 1303–1313.

**Scores de probabilité et AUC**

- **[Bri50]** Brier, G. W. (1950). *Verification of forecasts expressed in terms of probability*.
  Monthly Weather Review 78(1), 1–3.
- **[Mur73]** Murphy, A. H. (1973). *A new vector partition of the probability score*. Journal of Applied
  Meteorology 12(4), 595–600.
- **[GR07]** Gneiting, T. et Raftery, A. E. (2007). *Strictly Proper Scoring Rules, Prediction, and
  Estimation*. JASA 102(477), 359–378.
- **[HM82]** Hanley, J. A. et McNeil, B. J. (1982). *The meaning and use of the area under a ROC curve*.
  Radiology 143(1), 29–36. Et **[DeL88]** DeLong, E. R., DeLong, D. M. et Clarke-Pearson, D. L.
  (1988), Biometrics 44(3), 837–845.

**Validation, sur-ajustement et tests multiples**

- **[LdP18]** López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. Chap. 7
  (validation croisée purgée, embargo), chap. 11–12 (dangers du backtest, CPCV), chap. 14 (statistiques
  de backtest).
- **[BLdP14]** Bailey, D. H. et López de Prado, M. (2014). *The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting and Non-Normality*. Journal of Portfolio Management 40(5), 94–107.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- **[BBLZ17]** Bailey, D. H., Borwein, J., López de Prado, M. et Zhu, Q. J. (2017). *The Probability of
  Backtest Overfitting*. Journal of Computational Finance 20(4), 39–69.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- **[HLZ16]** Harvey, C. R., Liu, Y. et Zhu, H. (2016). *…and the Cross-Section of Expected Returns*.
  Review of Financial Studies 29(1), 5–68. https://www.nber.org/papers/w20592
- **[HL15]** Harvey, C. R. et Liu, Y. (2015). *Backtesting*. Journal of Portfolio Management 42(1), 13–28.
- **[BH95]** Benjamini, Y. et Hochberg, Y. (1995). *Controlling the False Discovery Rate*. JRSS B 57(1),
  289–300. Et **[BY01]** Benjamini, Y. et Yekutieli, D. (2001), Annals of Statistics 29(4), 1165–1188.
- **[W00]** White, H. (2000). *A Reality Check for Data Snooping*. Econometrica 68(5), 1097–1126.
- **[H05]** Hansen, P. R. (2005). *A Test for Superior Predictive Ability*. JBES 23(4), 365–380.
  https://ideas.repec.org/a/bes/jnlbes/v23y2005p365-380.html
- **[RW05]** Romano, J. P. et Wolf, M. (2005). *Stepwise Multiple Testing as Formalized Data Snooping*.
  Econometrica 73(4), 1237–1282.

**Valeur économique des prévisions**

- **[LT91]** Leitch, G. et Tanner, J. E. (1991). *Economic Forecast Evaluation: Profits versus the
  Conventional Error Measures*. American Economic Review 81(3), 580–590.
- **[GP00]** Granger, C. W. J. et Pesaran, M. H. (2000). *Economic and Statistical Measures of Forecast
  Accuracy*. Journal of Forecasting 19(7), 537–560. https://ideas.repec.org/p/cam/camdae/9910.html
- **[Roll84]** Roll, R. (1984). *A Simple Implicit Measure of the Effective Bid-Ask Spread*. Journal of
  Finance 39(4), 1127–1139.

**Crypto**

- **[HU21]** Hudson, R. et Urquhart, A. (2021). *Technical trading and cryptocurrencies*. Annals of
  Operations Research 297, 191–220. https://link.springer.com/article/10.1007/s10479-019-03357-1
- **[ABLS22]** Ammann, M., Burdorf, T., Liebi, L. et Stöckl, S. (2022). *Survivorship and Delisting Bias
  in Cryptocurrency Markets*. SSRN 4287573. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4287573
- Kaiko Research, *The Impact of Binance's Zero-Fee Volume*.
  https://www.kaiko.com/resources/the-impact-of-binances-zero-fee-volume ; CoinDesk (27/03/2023),
  *Binance's Bitcoin Trading Volume Hits Lowest Level in 8 Months Following Termination of Zero-Fee
  Trading*.

**Documentation Binance et outils**

- Binance, grille des frais spot : https://www.binance.com/en/fee/schedule ; FAQ des frais futures
  USDⓈ-M : https://www.binance.com/en/support/faq/detail/360033544231 ; API *Open Interest Statistics*
  (30 jours de profondeur) :
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- scikit-learn : `TimeSeriesSplit(gap=…)`, `roc_auc_score`, `brier_score_loss`, `calibration_curve`.
  statsmodels : `OLS(...).fit(cov_type="HAC")`, `stats.multitest.multipletests`. SciPy : `stats.binomtest`.
- Documents voisins : [`indicateurs.md`](indicateurs.md) (catalogue des indicateurs, IC locaux) et
  [`timesfm.md`](timesfm.md) (TimesFM, licence, mini-backtest).
