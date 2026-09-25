# TimesFM (Google) pour la direction des cryptos à 5, 10 et 15 minutes : état de l'art

*Rédigé le 25/09/2026. Environnement : `timesfm` 3.0.2 (PyTorch CPU) installé dans `.venv`,
poids `google/timesfm-3.0-pytorch` en cache. Tous les chiffres « mesurés » ci-dessous ont été
obtenus sur cette machine (4 CPU, pas de GPU) avec de vraies barres 1 min Binance.*

---

## 0. Résumé

1. **Licence.** Les poids de TimesFM 3.0 sont publiés sous `timesfm-non-commercial-license-v1.0`.
   La licence exclut explicitement toute activité *génératrice de revenus* et tout *système de production*,
   et interdit d'utiliser les *sorties* du modèle à des fins commerciales ou de production.
   Un bot qui trade de l'argent réel entre dans ces exclusions. TimesFM 3.0 sert donc uniquement à la
   **recherche** (notre phase 1). Pour du trading réel, il faut utiliser TimesFM 2.5 (Apache-2.0) ou un
   autre modèle sous licence permissive (Chronos-2/Bolt, Toto, Kronos…), ou obtenir une licence commerciale
   auprès de Google (§1.2).
2. **Ce que montre la littérature.** Utilisés en zero-shot sur des prix ou des rendements, les modèles
   fondation de séries temporelles (TSFM) prédisent la direction à **environ 50–52 %**. Ils ne battent
   la marche aléatoire que marginalement, et de façon rarement significative. Leur précision
   directionnelle apparente reflète souvent la dérive haussière du marché (le taux de base).
   Les gains signalés disparaissent en général une fois les frais comptés. Les modèles pré-entraînés
   **sur des données financières** (Kronos, pré-entraînement « from scratch ») font mieux, avec des IC
   de l'ordre de 0,02 à 0,07 (§4).
3. **Notre mini-test sur BTC/SOL/ETH en 1 min.** Il porte sur environ 290 origines par actif et par
   horizon, sur 3,5 jours. Les hit-rates vont de 44 à 53 % et les AUC de 0,44 à 0,55 : **rien de
   significatif**. Les quantiles sont en revanche **bien calibrés en amplitude** (couverture de
   [q10, q90] entre 0,76 et 0,84 pour une cible de 0,80). En pratique, la P(hausse) de TimesFM se
   comporte surtout comme un **signal de retour à la moyenne sur la dernière heure** : ρ ≈ −0,4 à −0,55
   avec le rendement des 60 dernières minutes (§3).
4. **La médiane reste très proche de la dernière valeur.** L'écart |médiane − dernier prix| vaut en
   médiane 1 à 6 pb, alors que l'intervalle q90 − q10 fait 25 à 40 pb et que l'aller-retour coûte 10 pb
   (futures taker) ou 20 pb (spot). La direction de la médiane se joue donc à quelques pb.
5. **Piège numérique découvert : ne jamais passer le log-prix brut.** Avec log(BTC) ≈ 11,3, le calcul en
   float32 de la variance dans la détendance linéaire interne s'effondre par annulation catastrophique.
   La décision de détendre est alors fausse dans **27 %** des fenêtres, et P(hausse) change jusqu'à
   0,25. Il faut **centrer le contexte** (par exemple `log(p / p[-1])`) en float64 avant l'appel (§2.4, §3.2).
6. **Les covariables « past-only » de TimesFM 3.0 sont, dans le code, des variates cibles
   supplémentaires.** Nous l'avons vérifié bit à bit (écart de 0,0). Elles agissent via l'attention entre
   variates. Chaque covariable ajoutée multiplie le coût par (1 + k) (§2.3).
7. **Sensibilité au contexte.** P(hausse) dépend fortement de la longueur de contexte : la corrélation
   entre les P(hausse) obtenues avec 512 barres et avec 128, 256, 1024 ou 2048 barres ne vaut que
   0,27 à 0,58. Il faut donc traiter la longueur de contexte comme un hyperparamètre choisi sur la
   période d'apprentissage, voire moyenner plusieurs contextes.

---

## 1. Versions de TimesFM

### 1.1 Tableau comparatif

| Version | Date | Paramètres | Contexte max | Horizon / patchs | Quantiles | Covariables | Multivarié | Licence des poids |
|---|---|---|---|---|---|---|---|---|
| **1.0** (`timesfm-1.0-200m`) | mai 2024 | 200 M | 512 | patch entrée 32, sortie 128, horizon quelconque (autorégressif) | têtes 0,1…0,9 « expérimentales », non calibrées | non | non | Apache-2.0 |
| **2.0** (`timesfm-2.0-500m`) | déc. 2024 | 500 M (50 couches) | 2048 | entrée 32, sortie 128 | 10 têtes non calibrées | non (indicateur de fréquence 0/1/2) | non | Apache-2.0 |
| **2.5** (`timesfm-2.5-200m`) | sept. 2025 | 200 M (+ tête quantile continue ≈ 30 M, 231 M au total) | 16 384 (contexte + horizon) | entrée 32, sortie 128, tête quantile continue jusqu'à 1024 pas | moyenne + déciles 0,1…0,9 | via **XReg** (régression linéaire en contexte, dépend de **JAX**), réintroduit en oct. 2025 | non | **Apache-2.0** |
| **3.0** (`timesfm-3.0-pytorch`) | 24–31 août 2026 | **330,7 M** (vérifié : 330 710 976) | **15 360** (`_MAX_CONTEXT_LENGTH`) | entrée **32**, sortie **64**, horizon arrondi au multiple de 64 avec assemblage (« stitching ») des patchs | déciles **0,1…0,9** (médiane = indice 4) | **natives** : `past_only_covariates` et `past_future_covariates` | **natif** (≤ 32 variates en entraînement, `max_variates: 32`) | **`timesfm-non-commercial-license-v1.0`** |

Architecture de TimesFM 3.0, lue dans `config.json` en cache : 20 couches « Mixing Transformer »,
d = 1280, 16 têtes, RMSNorm, RoPE sur l'axe temporel (`use_rope_seq: true`), **aucun encodage de
position entre variates** (`use_rope_var: false`), RevIN itératif « CPM », détendance linéaire et
stitching activés.

Données d'entraînement de 3.0 (carte HF) : GiftEvalPretrain (hors jeux recoupant fev-bench), pages
vues Wikipédia (jusqu'à nov. 2023), Google Trends (jusqu'à fin 2022), données synthétiques et
augmentées. D'après la presse spécialisée, cela représente « plus d'un billion de points ». Il n'y a
**pas de données crypto 1 min déclarées**. Kronos estime la part de données financières dans le corpus
de TimesFM (1.0) à environ 0,01 %. Le risque de contamination par nos données de test 2025-2026 est donc
faible, mais le domaine est aussi très éloigné.

### 1.2 Licence : conséquences concrètes pour un bot qui trade de l'argent réel

Le texte de la licence (fichier `LICENSE` du dépôt HF `google/timesfm-3.0-pytorch`) dit notamment :

* *« Non-Commercial Purpose » means use for testing, evaluation, or research **not tied to commercial
  gain, production deployment, or revenue generation** […] provided the results are **not used in
  commercial decision-making** […]. use (a) for any **revenue-generating activity**, (b) in direct or
  indirect interactions with end users or **production systems**, or (c) to train, fine-tune, or
  **distill other models for commercial use** […] is not a Non-Commercial Purpose.*
* Il est interdit de *« use […] the TimesFM Model (or any Derivative thereof, **or any Outputs** and
  data produced by the TimesFM Model) […] for any commercial or production purposes »*.
* Un modèle fine-tuné est un « Derivative » soumis aux mêmes restrictions. La licence est **révocable**.
  Tout autre usage exige une licence commerciale accordée par Google à sa discrétion.

En pratique :

| Usage | TimesFM 3.0 | TimesFM 2.5 |
|---|---|---|
| Étude d'indicateurs, backtests, rapports (phase 1) | autorisé (recherche non commerciale) | autorisé |
| Bot en **paper trading** (sans argent) | zone grise : « production system » ? À éviter | autorisé |
| Bot qui passe des **ordres réels**, même sur un compte perso | **interdit** (activité génératrice de revenus, système de production, usage des *sorties*) | autorisé (Apache-2.0 : conserver la notice de licence) |
| Fine-tuner les poids pour du trading | interdit (dérivé) | autorisé |
| Utiliser TimesFM 3.0 pour choisir des paramètres ensuite déployés en réel | zone grise (« commercial decision-making ») : **revalider avec un modèle sous licence permissive** | — |

Le code (`timesfm`, `timesfm3`) est sous Apache-2.0, **mais la restriction porte sur les poids**.
Les autres modèles « ouverts » ne sont pas tous utilisables : Moirai 1.x/2.0 est sous CC-BY-NC-4.0
(même problème), TiRex sous licence communautaire NX-AI (à lire). Les modèles sous licence
permissive sont Chronos-Bolt et Chronos-2 (Apache-2.0), Toto (Apache-2.0), Lag-Llama (Apache-2.0)
et Kronos (MIT). *Je ne suis pas juriste : faites relire ce point avant tout déploiement.*

---

## 2. API exacte telle qu'installée (`timesfm` 3.0.2)

Deux paquets sont installés : `timesfm3/` (TimesFM 3.0, backends `torch` et `mlx`) et `timesfm/`
(TimesFM 2.5 et configs ; `timesfm.TimesFM3Forecaster` réexporte la classe de 3.0).

### 2.1 TimesFM 3.0 : `timesfm3.TimesFM3Forecaster`

```python
from timesfm3 import TimesFM3Forecaster          # = timesfm3.torch.timesfm3_forecaster
m = TimesFM3Forecaster.from_pretrained(
    "google/timesfm-3.0-pytorch", device="cpu", per_core_batch_size=32)   # kwargs -> ModelConfig
out = m.predict(context, horizon=15, return_quantiles=True)               # -> ForecastOutput
outs = list(m.predict_batch(contexts, horizon=15, return_quantiles=True)) # GÉNÉRATEUR : list() obligatoire
```

Signature commune de `predict` et `predict_batch` (les arguments sont des listes pour le batch) :

```
predict_batch(contexts, horizon, past_only_covariates=None, past_future_covariates=None,
              ts_ids=None, return_quantiles=False, use_symmetric_averaging=False,
              make_positive=False, sort_quantiles=True, use_znorm=False, padding_mode="none")
```

**Sorties** (`ForecastOutput(ts_id, forecast, quantiles)`) :

* contexte 1D de longueur L : `forecast` de forme `(horizon,)` et `quantiles` de forme `(horizon, 9)` ;
* contexte 2D de forme `(V, L)` (multivarié) : `forecast` de forme `(V, horizon)` et `quantiles` de
  forme `(V, horizon, 9)` ;
* `quantiles` vaut `None` si `return_quantiles=False`. Les colonnes correspondent aux niveaux
  0,1, 0,2, …, 0,9 ;
* **`forecast` est exactement la médiane** `quantiles[..., 4]`, prise *après* tri des quantiles
  (vérifié : `np.allclose` renvoie vrai). Ce n'est pas une moyenne.

**Options**, lues dans le code :

| Option | Effet réel |
|---|---|
| `use_znorm` | z-normalise **chaque ligne** (cible et chaque covariable) avec la moyenne et l'écart-type du contexte (seuil σ < 1e-7 remplacé par 1), puis dénormalise la sortie. Causal, puisque seul le contexte est utilisé. |
| `use_symmetric_averaging` | exécute aussi le modèle sur `-x` (et `-covariables`), puis renvoie `(f(x) − inverse(f(−x)))/2` sur l'axe des quantiles. La prévision devient **exactement antisymétrique** : P(hausse \| x) = P(baisse \| −x), ce qui supprime tout biais directionnel appris. Coût mesuré : +50 % (2,3 s au lieu de 1,6 s pour 32 séries). C'est le défaut de `TimesFM3Evaluator`. |
| `make_positive` | met la sortie à 0 si le contexte est entièrement ≥ 0. Sans effet sur un contexte centré (il contient des négatifs). |
| `sort_quantiles` | trie les 9 quantiles (supprime les croisements). À laisser à `True`. |
| `padding_mode` | `"edge"` complète la partie future des `past_future_covariates` jusqu'au multiple de 64. Avec `"none"`, l'horizon est déduit de leur largeur. |

`TimesFM3Evaluator` (sous-classe, utilisée dans le README) change les valeurs par défaut
(`return_quantiles=True`, `use_symmetric_averaging=True`, `make_positive=True`), découpe les entrées de
plus de 32 variates et propose un mode `univariate=True`.

### 2.2 Ce que fait réellement le modèle, dans l'ordre (`timesfm3_forecaster.py`, `model.py`)

1. **NaN** : les NaN *initiaux* de la cible sont retirés (covariables tronquées de la même façon) ;
   les NaN *internes* sont remplis par **interpolation linéaire, sans avertissement**, pour la cible et
   les covariables.
2. `use_znorm` puis `use_symmetric_averaging`, qui double le batch.
3. **Arrondis.** L'horizon est arrondi au multiple de 64 (`global_horizon`) : demander 5, 15 ou 64 pas
   **coûte la même chose**. Le contexte du batch est porté au multiple de 32 supérieur et plafonné à
   15 360, les séries plus courtes étant complétées à gauche avec un masque. Au-delà de 15 360 points,
   seuls les plus récents sont gardés.
4. **Détendance linéaire** (`decode`) : pour chaque variate, une droite est ajustée sur le contexte.
   Si `std(résidu) < 0,5 · std(série)` (R² > 0,75), la série est détendue et **la droite est
   extrapolée puis rajoutée à la prévision**. Ce mécanisme produit un effet « momentum » sur les
   fenêtres très tendancielles. Mesure : il se déclenche dans **10,8 %** des fenêtres de 512 min du BTC.
   ⚠ La variance y est calculée comme `E[y²] − E[y]²` en **float32**, d'où le piège décrit en §2.4.
5. **RevIN causal par patch** : chaque patch de 32 points est normalisé avec la moyenne et l'écart-type
   *cumulés* depuis le début du contexte (`get_running_stats`), séparément pour chaque variate. Un
   σ < 1e-6 est remplacé par 1. Le modèle est donc invariant (aux arrondis près) à `a·x + b` avec a > 0.
   Prix, log-prix centré et rendement cumulé donnent presque la même prévision relative sur 512 min.
6. **Jetons** : chaque patch forme un jeton dont l'entrée concatène les 32 valeurs normalisées, les
   64 valeurs *futures* des covariables `past_future` (« roll » ; masquées pour les cibles et les
   covariables past-only) et les masques. Un bloc résiduel produit ensuite des vecteurs de 1280 dimensions.
7. **Transformer** : à chaque couche, attention **causale sur le temps** (par variate), puis **attention
   entre variates** (non causale, par position de patch, sans encodage de position), puis FFN.
8. **Sortie** : la tête produit 64 × 9 valeurs par jeton. Avec le stitching, la prévision vient du
   *dernier patch de contexte* (pour un horizon ≤ 64, une seule passe). Les patchs d'horizon sont
   masqués ; leurs statistiques RevIN sont affinées itérativement (« CPM iterative RevIN ») à partir
   de la médiane prédite. Viennent ensuite la dénormalisation, l'ajout de la tendance, le tri des
   quantiles, la moyenne symétrique et l'inversion du z-score.

### 2.3 Covariables : comment elles sont injectées

* **`past_only_covariates`**, de forme `(k, L)` : dans `decode`, elles sont **concaténées à la cible sur
  l'axe des variates** avec `patch_is_target = True`, exactement comme une cible supplémentaire. Leur
  futur est masqué et le modèle les prévoit aussi, mais ces sorties sont jetées
  (`all_raw_outputs[:, :num_targets_in]`). Elles n'influencent la cible que par **l'attention entre
  variates**, à chaque position de patch. Vérification : `predict(BTC, past_only=SOL)` et
  `predict([BTC, SOL])[0]` donnent un **écart max de 0,0**.
* **`past_future_covariates`**, de forme `(w, L + horizon)` : elles ne sont pas des cibles, et leurs
  valeurs futures entrent dans le jeton du dernier patch de contexte via le « roll » décrit au point 6.
  Cas d'usage pour nous : le **calendrier** (minute du jour et jour de la semaine en sin/cos), connu à
  l'avance. Un indicateur technique n'est **pas** connu dans le futur et ne doit donc jamais être passé ici.
* **Normalisation** : chaque covariable a ses propres statistiques RevIN causales et sa propre
  détendance. Pas besoin de z-scorer les indicateurs pour l'échelle, **mais il faut les centrer**
  (piège float32, §2.4).
* **Coût** : il est linéaire en nombre de variates. Mesures pour un batch de 32 et un contexte de 512 :
  1 variate 1,2 s, 3 variates 3,3 s, 8 variates 8,9 s.
* ⚠ **Ne jamais mélanger dans un même batch des séries avec et sans covariables.** Le code remplace
  la covariable absente par des **zéros**. Ce n'est pas un masque : c'est une vraie variate constante,
  qui modifie la prévision (écart max mesuré 2,8e-4 pour des quantiles d'environ 6,4e-4 en valeur
  absolue).
* Toutes les séries d'un batch doivent avoir le même nombre de variates cibles (sinon `ValueError`).

### 2.4 Piège numérique : le log-prix non centré

Avec y = log(prix) ≈ 11,34, on a `E[y²] − E[y]² ≈ 128,6 − 128,6` alors que la vraie variance sur
512 min est d'environ 5e-6. Le float32 (ε ≈ 6e-8, soit environ 8e-6 à 128) ne peut pas la résoudre :
la variance calculée vaut 1,5e-5 au lieu de 5,4e-6. Sur 629 fenêtres BTC de 512 min, la décision de
détendre est **fausse dans 26,6 %** des cas (32 % de déclenchements au lieu de 10,8 %). **Correctif** :
soustraire en float64 la dernière valeur (ou la moyenne) du contexte avant l'appel, ou utiliser
`use_znorm=True`. Le prix brut (≈ 84 000) et le rendement cumulé n'ont pas ce problème dans nos tests,
mais centrer est gratuit et sans risque.

### 2.5 TimesFM 2.5 : `timesfm.TimesFM_2p5_200M_torch`

```python
import timesfm
model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
    "google/timesfm-2.5-200m-pytorch", torch_compile=False)   # torch_compile=True par défaut (lent à compiler sur CPU)
model.compile(timesfm.ForecastConfig(
    max_context=512, max_horizon=128,          # arrondis aux multiples de 32 / 128 ; contexte + horizon <= 16 384
    normalize_inputs=True, use_continuous_quantile_head=True,
    force_flip_invariance=True,                # = moyenne symétrique de 3.0 (True par défaut)
    infer_is_positive=False, fix_quantile_crossing=True))
point, q = model.forecast(horizon=15, inputs=list(contexts))   # point (n, 15) ; q (n, 15, 10)
```

* `q[..., 0]` est la **moyenne** et `q[..., 1:10]` les déciles 0,1…0,9. `point` est `q[..., 5]`,
  c'est-à-dire la médiane.
* ⚠ `forecast()` **modifie la liste passée** : il la complète avec des séries factices
  (`inputs += …`). Il faut passer une copie.
* Contexte plus court que `max_context` : complété de zéros avec un masque. Plus long : tronqué.
* Covariables : `forecast_with_covariates(inputs, dynamic_numerical_covariates=…,
  xreg_mode="xreg + timesfm" | "timesfm + xreg", ridge=…)` exige `return_backcast=True` à la
  compilation, **importe JAX** (non installé : `ModuleNotFoundError: jax`) et surtout exige des
  covariables dynamiques de longueur **contexte + horizon**, donc avec des valeurs futures. **XReg ne
  convient pas à des indicateurs past-only.** Deux contournements : décaler les indicateurs de ≥ h
  barres, ou empiler TimesFM et les indicateurs dans un modèle de 2e niveau
  (`evaluation.combined_model_walkforward`).
* Les poids de 2.5 ne sont **pas** dans le cache HF (environ 0,9 Go à télécharger ; téléchargement
  autorisé par le contrat d'architecture).

---

## 3. Essais sur CPU avec de vraies données

**Données** : BTCUSDT, SOLUSDT et ETHUSDT spot en 1 min, 4 999 barres chacune, via
`data-api.binance.vision/api/v3/klines`, du 22/09/2026 04:52 au 25/09/2026 16:10 UTC, sans trou.
**Modèle** : TimesFM 3.0, contexte 512, contexte transformé `log(p / p_dernier)` (sauf mention
contraire), horizon 15 lu aux pas 5, 10 et 15.
**Avertissement** : 3,5 jours et environ 290 origines par cellule. Ce sont des **tests de bon
fonctionnement, pas des conclusions** : l'IC à 95 % d'un hit-rate vaut environ ±5,8 points.

### 3.1 Débit (4 threads CPU)

| Configuration | Temps |
|---|---|
| Chargement du modèle | 6,7 s |
| Batch 32, contexte 512, horizon 15 | 1,2 à 1,6 s (≈ 20–25 séries/s) |
| Idem avec `use_symmetric_averaging=True` | 2,3 s |
| Batch 32, contexte 2048 | 4,4 s |
| Séries seules ou en batch | identiques (écart 6e-9) |

Conséquence pour le backtest : un an de barres 1 min avec un pas de 5 donne environ 105 000 origines,
soit **environ 1,5 h par actif** (2,2 h en symétrique) au contexte 512. Un pas de 15 (étiquettes h = 15
sans chevauchement) ou `max_origins` est donc recommandé pour les premières passes.

### 3.2 Espace du contexte (32 fenêtres BTC)

| Contexte | P(hausse) moyenne h5/h10/h15 | Écart max de P(hausse) par rapport à `cumret` |
|---|---|---|
| prix brut | 0,474 / 0,471 / 0,471 | ≈ 0 |
| `cumret` = log(p/p_dernier) | 0,474 / 0,471 / 0,471 | — |
| prix + `use_znorm` | 0,474 / 0,471 / 0,471 | ≈ 0 |
| **log-prix brut** | 0,460 / 0,453 / 0,450 | **jusqu'à 0,19 / 0,22 / 0,25** (même signe de médiane dans 96,9 % des cas) |

Le log-prix centré (en float64) redonne `cumret` à 1e-8 près, ce qui confirme que la seule cause est
numérique (§2.4). Le choix entre prix, log-prix et rendement cumulé est **presque indifférent** sur
512 min une fois centré, grâce au RevIN. En revanche, **un contexte de rendements non cumulés est à
éviter** : les quantiles par pas sont marginaux, et la somme des quantiles de chaque pas n'est pas le
quantile de la somme. Pour obtenir P(r_h > 0), il faut prévoir un **niveau** (prix ou rendement cumulé).

### 3.3 Direction : mini-test hors échantillon (pas de 15, environ 290 origines par cellule)

| Actif | Moy. sym. | h | Hit-rate (signe de la médiane) | AUC de P(hausse) | Taux de hausse réel | Hit-rate momentum | ρ(P(hausse), rendement 60 min passé) |
|---|---|---|---|---|---|---|---|
| BTC | non | 5 / 10 / 15 | 0,459 / 0,474 / 0,509 | 0,446 / 0,506 / 0,532 | 0,534 / 0,512 / 0,464 | 0,562 / 0,526 / 0,454 | −0,51 / −0,52 / −0,52 |
| BTC | oui | 5 / 10 / 15 | 0,442 / 0,485 / 0,529 | 0,441 / 0,513 / 0,534 | idem | idem | −0,52 / −0,55 / −0,54 |
| SOL | non | 5 / 10 / 15 | 0,497 / 0,481 / 0,510 | 0,505 / 0,509 / 0,553 | 0,552 / 0,509 / 0,524 | 0,521 / 0,445 / 0,485 | −0,39 / −0,44 / −0,47 |
| SOL | oui | 5 / 10 / 15 | 0,469 / 0,477 / 0,503 | 0,500 / 0,505 / 0,545 | idem | idem | −0,42 / −0,45 / −0,48 |
| ETH | non | 5 / 10 / 15 | 0,512 / 0,512 / 0,512 | 0,504 / 0,507 / 0,547 | 0,519 / 0,509 / 0,478 | 0,505 / 0,498 / 0,461 | −0,45 / −0,48 / −0,48 |
| ETH | oui | 5 / 10 / 15 | 0,478 / 0,485 / 0,532 | 0,496 / 0,503 / 0,543 | idem | idem | −0,45 / −0,49 / −0,49 |

Lecture :

* **Aucune cellule n'est significative.** Certaines sont sous 50 % : c'est du bruit.
* La P(hausse) de TimesFM est **fortement anti-corrélée au rendement de la dernière heure**. Le modèle
  agit comme un **signal de retour à la moyenne**. Toute évaluation doit donc le comparer à
  `reversal_h` et mesurer son **apport incrémental** (IC partiel, ou TimesFM comme variable d'un
  modèle combiné).
* La moyenne symétrique change peu P(hausse) (corrélation de 0,983 à 0,986 avec la version non
  symétrique).
* **Égalités de la cible** (r_h = 0 exactement) : 1,7 à 2,0 % pour SOL à h = 5 (un tick de 0,01 sur
  environ 120 $ fait 0,83 pb), 0,3 % pour BTC, 0 % pour ETH. Elles seront plus fréquentes sur DOGE et
  XRP. `targets.direction_label` les met à NaN.

### 3.4 Calibration et amplitude

| Actif, h | Couverture [q10, q90] (cible 0,80) | Couverture [q40, q60] (cible 0,20) | Brier de P(hausse) (0,25 pour une constante à 0,5) | ρ(q90−q10, \|r_h\|) | ρ(vol. réalisée 60 min, \|r_h\|) |
|---|---|---|---|---|---|
| BTC, 5 | 0,809 | 0,181 | 0,266 | 0,31 | 0,27 |
| BTC, 15 | 0,758 | 0,232 | 0,257 | 0,15 | 0,20 |
| SOL, 5 | 0,840 | 0,164 | 0,255 | 0,24 | 0,22 |
| SOL, 15 | 0,771 | 0,201 | 0,254 | 0,19 | 0,19 |

* L'**amplitude** est bien calibrée : l'écart q90 − q10 prévoit |r_h| à peu près aussi bien qu'une
  simple volatilité réalisée.
* La **P(hausse) brute est trop confiante** : le Brier dépasse 0,25, alors que P(hausse) varie entre
  environ 0,30 et 0,70 (du 5e au 95e centile) sans pouvoir prédictif. Il faudra la **recalibrer**
  (isotonique ou Platt sur la période d'apprentissage) ou la ramener vers 0,5.

### 3.5 Écart médiane − dernier prix comparé aux frais (moyenne symétrique)

| Actif, h | \|médiane\| p50 / p95 | Part > 10 pb | Part > 20 pb | \|r_h\| réalisé p50 |
|---|---|---|---|---|
| BTC, 5 | 1,3 / 5,9 pb | 1,0 % | 0,3 % | 6,3 pb |
| BTC, 15 | 3,1 / 12,8 pb | 8,5 % | 1,7 % | 10,3 pb |
| SOL, 5 | 2,4 / 10,4 pb | 5,5 % | 1,4 % | 10,3 pb |
| SOL, 15 | 6,0 / 28,5 pb | 29,4 % | 9,2 % | 19,5 pb |
| ETH, 5 | 1,6 / 6,9 pb | 2,0 % | 0,7 % | 7,8 pb |
| ETH, 15 | 3,7 / 15,3 pb | 14,3 % | 3,1 % | 14,0 pb |

Même en prenant le modèle au mot, le mouvement qu'il anticipe est **presque toujours inférieur au coût
d'un aller-retour** (10 pb en futures taker, 20 pb en spot, cf. `config.COST_BPS`). Une règle « ne
trader que si |médiane| > coût » ne se déclencherait presque jamais à 5 min.

### 3.6 Longueur de contexte (64 origines BTC, h = 15)

| Contexte | 128 | 256 | 512 | 1024 | 2048 |
|---|---|---|---|---|---|
| Écart-type de P(hausse) | 0,159 | 0,114 | 0,113 | 0,115 | 0,085 |
| Corrélation de P(hausse) avec le contexte 512 | 0,27 | 0,40 | 1 | 0,58 | 0,47 |
| ρ(P(hausse), rendement 60 min passé) | −0,12 | −0,47 | −0,46 | −0,47 | −0,51 |

La P(hausse) est **instable selon le contexte**. Ce n'est pas un bon signe pour un signal
directionnel robuste. Le rôle de retour à la moyenne se retrouve à partir de 256 barres.

### 3.7 Multivarié

Ajouter SOL (en covariable past-only, ce qui revient à une seconde cible) change la P(hausse) du BTC de
0,04 à 0,05 en moyenne absolue. Le modèle exploite donc bien l'autre série, mais nous n'avons pas assez
de données pour dire si cela améliore la prévision.

---

## 4. Littérature et retours d'expérience (prix et rendements)

### 4.1 Études principales

| Étude | Modèles et données | Résultat clé |
|---|---|---|
| **Rahimikia, Ni, Wang (2025)**, *Re(Visiting) TSFMs in Finance*, arXiv 2511.18578 | Chronos (tiny→large), TimesFM (8 M→500 M) et 10 autres TSFM en annexe ; rendements excédentaires **journaliers** de ~10 000 actions US (et 94 pays), 1990–2023 | **Zero-shot faible** : TimesFM-500M R² hors échantillon −2,80 %, précision ≈ 50 %, rendement −1,47 % ; Chronos-large R² −1,37 %, précision 51 %. Référence CatBoost : R² −0,03 %, **précision 51,16 %**. Le fine-tuning aide peu. Un **pré-entraînement « from scratch » sur données financières** atteint 51,74 % (Chronos-small) au niveau de CatBoost. |
| **Fu, Hirano, Imajo (2024)**, *Financial Fine-tuning a Large Time Series Model*, arXiv 2412.09880 (IEEE) | TimesFM (architecture 1.0, 200 M, contexte 512) en **pré-entraînement continu** sur 90 M points, dont **50,7 M de crypto horaires** (Binance) ; perte MSE sur log-prix | Le TimesFM d'origine est **sous le hasard sur 4 des 7 horizons**. Après fine-tuning, les Sharpe *sans frais* sont : S&P 500 1,68 (original 0,42, AR(1) 1,58) ; **crypto journalière 0,26** (original −0,03, **AR(1) 0,17**). Coût neutre (seuil de rentabilité) en crypto : 0,44 % contre **0,88 % pour AR(1)**. Les auteurs reconnaissent que le modèle fine-tuné fait « significativement moins bien qu'AR(1) » sur devises et crypto. |
| **Alonso et Franklin (2026)**, *Pretrained TSFMs for Financial Return Forecasting*, arXiv 2606.27100 | TimeGPT, **TimesFM-2.5**, Moirai-2.0, Chronos, Chronos-2 en zero-shot ; 5 actions US journalières 2014–2026, H = 20 | Les TSFM gagnent 8 tâches sur 10 en MAE, mais « les gains par rapport à la marche aléatoire sont **faibles et épars** » : le test de Diebold-Mariano n'est significatif que dans 2 cas sur 10, avec un skill score de l'ordre de **1e-3**. Pas de frais modélisés. |
| *When Directional Accuracy Lies* (2026), arXiv 2607.12248 | **TimesFM-2.5 avec LoRA** (r = 32), NASDAQ-100 et S&P 500 journaliers 2005–2026, contexte 512, h de 2 à 128 | Les ~80 % de précision apparente correspondent au **taux de base « toujours hausse » (~0,70)**. En walk-forward, l'excès de précision à h = 128 vaut −0,151 (NASDAQ) et −0,125 (S&P) en zero-shot, et −0,081 et −0,017 avec LoRA. Les auteurs recommandent de publier `acc − acc(toujours hausse)`, avec tests de McNemar et de Diebold-Mariano et un contrôle FDR. |
| **Kronos** (Shi et al., 2025, AAAI 2026), arXiv 2508.02739 | Modèle fondation **spécifique aux chandeliers** (tokeniseur BSQ + Transformer autorégressif), 12 Md de barres de 45 places, dont **Binance 1 min (1,24 Md d'observations, 2021 → juin 2024)** ; licence MIT ; mini 4,1 M (contexte 2048), small 24,7 M et base 102,3 M (contexte 512) | RankIC **+93 %** par rapport au meilleur TSFM générique et +87 % par rapport au meilleur modèle non pré-entraîné. Les niveaux absolus restent petits (ablation : IC prix ≈ 0,043, RankIC ≈ 0,025, IC rendement ≈ 0,067). Backtest à 0,15 % de frais sur actions chinoises (CSI300/800). **Tests de prévision à partir de juillet 2024**, ce qui évite toute contamination. |
| **Lee et al. (2026)**, *FinVerse*, arXiv 2608.03259 | 43 TSFM, 116 897 séries financières dont crypto journalières | « De bonnes performances sur les critères génériques **ne se traduisent pas** nécessairement en prévisions financières utiles. » Les hit-ratios d'environ 0,65–0,67 (TimesFM-2.5 0,653, Chronos-2 0,659, Toto-2.0 0,661) mélangent macro, fondamentaux et variations annuelles, sans référence naïve : **non comparables** à nos rendements à 1 min. |
| Goel et al. (2025), arXiv 2505.11163 | TimesFM 2.0, **volatilité réalisée** de 21 indices | En zero-shot, pas meilleur de façon constante que HAR ; un fine-tuning incrémental est nécessaire. |
| *Forecasting RV with TSFMs* (2026), arXiv 2607.05291 | 9 TSFM en zero-shot, 50 actifs | Seul TTM bat Log-HAR à tous les horizons, **avec des marges étroites**. |
| *918 Experiments* (2026), arXiv 2603.16886 | 9 architectures profondes (hors TSFM), **horaire**, crypto (BTC, ETH, BNB, ADA), FX, indices | Précision directionnelle moyenne de **50,08 %** sur 54 combinaisons, « équivalente à pile ou face ». |
| *ML Bitcoin Trading Under Transaction Costs* (2026), arXiv 2606.00060 | XGBoost, LSTM, iTransformer, BTC **horaire** 2017–2026, 10 pb par rotation | Brut → net : XGBoost long-only **+73,5 % → −64 %** ; iTransformer long-short **+181,8 % → −98,6 %**. Un filtre « ne trader que si la prévision dépasse le coût » rétablit la rentabilité selon les auteurs (à confirmer). |
| *Chronos multivariate finance* (2026), arXiv 2605.21504 | Chronos-2 multivarié contre univarié ; 7 grandes actions tech US (« Magnificent 7 ») et taux US, mensuel | Le multivarié bat l'univarié sur des séries **apparentées**. **Mélanger des classes d'actifs différentes dégrade** la prévision. |
| Revue *AI in Equity and Crypto Markets* (2026), arXiv 2609.04917 | Synthèse | Les preuves de rentabilité directe des TSFM sont « beaucoup plus faibles » que leurs performances sur les benchmarks. Mise en garde contre la **contamination temporelle** : un checkpoint dont les données couvrent la période de test invalide le backtest. |

### 4.2 Synthèse honnête

* **Zero-shot contre marche aléatoire.** Les TSFM génériques (TimesFM, Chronos, Moirai, TimeGPT,
  Toto, Lag-Llama) **ne battent pas de façon fiable la marche aléatoire** sur des rendements. La
  précision directionnelle se situe autour de 50–52 %, souvent sous le taux de base « toujours
  hausse ». Leur force, prouvée sur GIFT-Eval, fev-bench et TIME, concerne des séries à structure
  (saisonnalité, tendance) que les rendements n'ont presque pas.
* **Fine-tuning financier.** Il aide un peu : Fu et al. en crypto journalière, LoRA de 2.5 sur les
  actions. Mais il reste souvent **au niveau d'un AR(1) ou d'un gradient boosting**. Le
  pré-entraînement sur données financières (Kronos, Rahimikia « from scratch ») est la seule piste
  avec des gains nets en IC, et ces IC restent faibles (0,02 à 0,07).
* **Là où les TSFM servent.** La **volatilité et l'incertitude** : les quantiles sont utiles pour
  dimensionner les positions et poser les stops. Notre mesure le confirme : les quantiles sont bien
  calibrés, pas la direction.
* **Les frais.** Aucune étude ne montre un gain *net de frais* robuste à des horizons de quelques
  minutes. À 5–15 min, le mouvement médian réalisé (6 à 20 pb) est du même ordre que le coût d'un
  aller-retour (10 à 20 pb).
* **Aucune étude publiée** n'évalue TimesFM 3.0 sur des rendements crypto en 1 min. Notre phase 1 comble
  précisément ce trou, avec des attentes réalistes : un résultat nul est le scénario le plus probable.

### 4.3 Autres TSFM (pour des comparaisons futures)

| Modèle | Taille | Licence | Point notable |
|---|---|---|---|
| Chronos-Bolt (Amazon, nov. 2024) | 9 M → 205 M | Apache-2.0 | rapide, quantiles directs |
| Chronos-2 (oct. 2025) | 120 M | Apache-2.0 | multivarié et covariables via « group attention » |
| Moirai 2.0 (Salesforce, août 2025) | 11 M (small) | **CC-BY-NC-4.0** | non commercial, comme TimesFM 3.0 |
| Toto (Datadog, mai 2025) | 151 M | Apache-2.0 | pré-entraîné sur de l'observabilité |
| Lag-Llama (2023-24) | 2,4 M | Apache-2.0 | faible en crypto horaire (MAPE 7,1 % contre 1,8 % pour Chronos, arXiv 2609.05235) |
| TimeGPT (Nixtla) | — | API commerciale fermée | données envoyées à un tiers ; même constat « faibles et épars » (2606.27100) |
| Kronos (2025) | 4 M → 102 M publiés | **MIT** | seul modèle **entraîné sur des chandeliers crypto 1 min** ; comparateur naturel |

---

## 5. Recommandations pour notre wrapper (`forecaster.py`, `backtest.py`)

1. **Centrer systématiquement le contexte en float64 avant l'appel**, quel que soit `kind`.
   `transform_context(prices, kind)` peut renvoyer `log(p)`, `p` ou `log(p/p[0])`, mais `forecast()`
   doit soustraire `ctx[-1]` en float64, appeler le modèle, puis rajouter `ctx[-1]` aux quantiles.
   `ForecastBatch.last_value` reste ainsi dans l'espace transformé, conformément au contrat. Pour le
   défaut de `TimesFMConfig.context_transform="logprice"`, c'est **indispensable** (§2.4).
   Faire de même pour chaque covariable (soustraire sa moyenne), ou passer `use_znorm=True`.
2. **Un seul appel pour les trois horizons** : `horizon=max(horizons)` (15), puis lire les pas `h−1`
   = 4, 9 et 14. Jusqu'à 64 pas, le coût est identique.
3. **`use_symmetric_averaging=True` par défaut pour 3.0** (antisymétrie, aucun biais haussier appris,
   défaut de l'évaluateur officiel) ; `force_flip_invariance=True` pour 2.5. Coût +50 %.
4. **P(hausse) à partir des déciles** : fonction de répartition linéaire par morceaux passant par les
   points (q_k, k/10), avec des **queues prolongées linéairement** (q₀ = q₁ − (q₂ − q₁) → 0,
   q₁₀ = q₉ + (q₉ − q₈) → 1), puis bornage dans [0,01 ; 0,99] :
   ```python
   def prob_up(q, last, levels=np.arange(1, 10) / 10):
       # q: (n, 9) déciles triés à l'horizon h ; last: (n,) dernière valeur dans le même espace
       lo = q[:, :1] - (q[:, 1:2] - q[:, :1]); hi = q[:, -1:] + (q[:, -1:] - q[:, -2:-1])
       xs = np.concatenate([lo, q, hi], axis=1); ps = np.r_[0.0, levels, 1.0]
       cdf = np.array([np.interp(l, x, ps) for l, x in zip(last, xs)])   # vectorisable par recherche
       return np.clip(1.0 - cdf, 0.01, 0.99)
   ```
   Propriété exacte : **P(hausse) > 0,5 équivaut à médiane > dernière valeur**. Au seuil 0,5,
   P(hausse) n'apporte donc rien de plus que le signe de la médiane ; elle ne sert qu'au classement
   (AUC) et à la confiance. Autre approche : une loi normale de moyenne q0,5 et d'écart-type
   (q0,9 − q0,1)/2,563. **Recalibrer** P(hausse) sur la période d'apprentissage (isotonique) avant
   d'utiliser un seuil comme 0,55 dans `toy_strategy`, car le Brier brut dépasse 0,25.
5. **Longueur de contexte** : garder 512 par défaut (bon rapport coût/stabilité), mais **tester 256,
   512 et 1024 sur la période d'apprentissage**. Envisager la moyenne des P(hausse) de 2 ou 3 contextes
   pour réduire la variance (coût ×2 ou ×3). Au-delà de 2048, le gain est douteux et le coût élevé
   (environ 7 séries/s).
6. **Covariables past-only (indicateurs)** :
   * en passer **au plus 4 à 8**, parce que le coût est linéaire et que l'entraînement va jusqu'à
     32 variates ;
   * **les choisir sur la période d'apprentissage uniquement**, par exemple les meilleurs
     `ic_train_mean` de `evaluation.aggregate_scores` (`StudyConfig.train_frac`) ;
   * privilégier des séries **persistantes et lisses** : RSI, z-score de l'écart au VWAP, déséquilibre
     taker cumulé, variation d'open interest. Un bruit blanc n'apporte rien après le RevIN ;
   * les **centrer**, **couper le contexte à la première barre où toutes sont valides** (pas de NaN
     initiaux, qui seraient remplis silencieusement par la valeur constante), vérifier la causalité
     (valeur en t calculée sur les barres ≤ t) ;
   * **ne jamais mélanger** dans un même batch des séries avec et sans covariables (§2.3).
   * Cas particulier : passer le **calendrier** (sin/cos de la minute du jour et du jour de la
     semaine) en `past_future_covariates`, avec les valeurs futures connues.
7. **Multivarié** : tester en A/B (a) le contexte joint `(3, L)` BTC+SOL+ETH et (b) BTC en covariable
   past-only pour chaque altcoin, qui revient au même calcul que (a). Aligner les séries sur des
   horodatages communs. Le coût par actif est inchangé en (a) : un appel produit trois prévisions.
   Ne pas mélanger crypto et actions (cf. arXiv 2605.21504).
8. **Trous de données** : TimesFM **interpole linéairement** les NaN internes sans le signaler.
   `walk_forward_timesfm` doit **écarter les origines dont le contexte contient un trou** au-delà de
   quelques barres, par cohérence avec `targets.future_log_return`, qui met NaN sur les trous.
9. **Budget de calcul** : `torch.set_num_threads(4)`, `per_core_batch_size` entre 32 et 64. Environ
   20 séries/s au contexte 512. Pour un premier passage, `stride=15` et 30 à 90 jours par actif.
10. **Évaluation** (dans `backtest.direction_metrics`) :
    * comparer aux références `always_up`, `momentum_h` et **`reversal_h`** : TimesFM se comporte comme
      un signal de retour à la moyenne ;
    * publier l'excès de précision `acc − max(taux_de_base, 1 − taux_de_base)`, l'AUC, le Brier et
      le p-value binomial sur `n_eff = n/h` ;
    * mesurer l'**IC partiel** de P(hausse) une fois le rendement passé de 60 min et de h barres
      neutralisé ;
    * n'annoncer un gain qu'**après frais** : 10 pb en futures taker, 20 pb en spot, en y ajoutant le
      spread et le glissement.
11. **Backend 2.5** : c'est la seule option de production chez Google. L'intégrer derrière la même
    interface : `quantiles = q[..., 1:]` donne les déciles, et `point` est la médiane. Pas de XReg
    (JAX absent et covariables futures requises). Comparer 2.5 et 3.0 sur les mêmes origines pour
    savoir si 3.0 apporte quoi que ce soit ; sinon, 2.5 suffit.

### Pièges à retenir

* **La médiane colle à la dernière valeur** : quelques pb d'écart, donc une direction dictée par des
  détails numériques (arrondi float32, contexte, moyenne symétrique).
* **Égalités** : r_h = 0 arrive 1 à 2 % du temps sur SOL (plus sur DOGE et XRP). Les labels valent NaN
  quand `eps = 0`. En trading réel, une égalité coûte les frais.
* **Normalisation interne** : RevIN causal et **extrapolation d'une tendance linéaire** quand R² > 0,75
  sur le contexte. Une fenêtre très tendancielle produit donc une prévision de type momentum,
  les autres une prévision de retour à la moyenne.
* **Chevauchement des étiquettes** : avec un pas de 1 et h = 15, les observations ne sont pas
  indépendantes. Utiliser `n_eff = n/h`.
* **`predict_batch` est un générateur** : il faut le consommer avec `list()`.
* **`forecast()` de 2.5 modifie la liste passée en entrée** : passer une copie.
* **Contamination** : tester Kronos uniquement sur des données postérieures à **juin 2024**. Pour
  TimesFM 3.0, pas de crypto déclarée dans le corpus.

---

## 6. Questions ouvertes

* Sur plusieurs mois, P(hausse) de TimesFM apporte-t-elle quelque chose **au-delà d'un simple
  signal de retour à la moyenne** ? À mesurer par IC partiel ou dans un modèle combiné.
* Les covariables past-only (indicateurs choisis sur la période d'apprentissage) et le contexte
  multivarié BTC/SOL/ETH améliorent-ils l'AUC de façon significative (après correction FDR) ?
* Quelle longueur de contexte retenir, et la moyenne de plusieurs contextes vaut-elle son coût ×2
  ou ×3 ?
* TimesFM 2.5 (Apache-2.0) fait-il aussi bien que 3.0 sur cette tâche ? Si oui, la question de licence
  disparaît.
* Faut-il ajouter **Kronos** (MIT, entraîné sur Binance 1 min) comme comparateur spécifique à la
  finance ? Son code est sur GitHub (github.com renvoie 403 ici ; à récupérer autrement) et ses poids
  sont sur HF.
* Les quantiles restent-ils calibrés selon les régimes (week-end, annonces macro, liquidations) ? Si
  oui, TimesFM pourrait servir au **dimensionnement du risque** plutôt qu'à la direction.
* La licence de TimesFM 3.0 doit être relue par un juriste avant tout usage qui ne serait pas
  purement de la recherche.

---

## 7. Sources

* Dépôt et README TimesFM (versions, notice de licence 3.0, exemples d'API) : https://github.com/google-research/timesfm (et `timesfm-3.0.2.dist-info/METADATA` installé)
* Carte du modèle TimesFM 3.0 : https://huggingface.co/google/timesfm-3.0-pytorch
* Licence non commerciale TimesFM 3.0 : https://huggingface.co/google/timesfm-3.0-pytorch/blob/main/LICENSE
* TimesFM 2.5 : https://huggingface.co/google/timesfm-2.5-200m-pytorch ; TimesFM 2.0 : https://huggingface.co/google/timesfm-2.0-500m-pytorch ; TimesFM 1.0 : https://huggingface.co/google/timesfm-1.0-200m
* Article TimesFM (ICML 2024) : https://arxiv.org/abs/2310.10688
* Annonce de TimesFM 3.0 (31/08/2026) : https://www.marktechpost.com/2026/08/31/google-ai-releases-timesfm-3-a-330m-parameter-zero-shot-foundation-model-for-multivariate-time-series-forecasting/ ; https://datanorth.ai/news/google-releases-timesfm-3
* Rahimikia, Ni, Wang, *Re(Visiting) Time Series Foundation Models in Finance* : https://arxiv.org/abs/2511.18578
* Fu, Hirano, Imajo, *Financial Fine-tuning a Large Time Series Model* : https://arxiv.org/abs/2412.09880
* *Pretrained Time-Series Foundation Models for Financial Return Forecasting* : https://arxiv.org/abs/2606.27100
* *When Directional Accuracy Lies: A Base-Rate-Honest Benchmark for LoRA-Adapted TimesFM* : https://arxiv.org/abs/2607.12248
* Kronos : https://arxiv.org/abs/2508.02739 ; poids : https://huggingface.co/NeoQuasar/Kronos-base
* FinVerse : https://arxiv.org/abs/2608.03259
* Goel et al., *Foundation Time-Series AI Model for Realized Volatility Forecasting* : https://arxiv.org/abs/2505.11163
* *Forecasting Realized Volatility with TSFMs* : https://arxiv.org/abs/2607.05291
* *A Controlled Comparison of Deep Learning Architectures for Multi-Horizon Financial Forecasting (918 experiments)* : https://arxiv.org/abs/2603.16886
* *Machine Learning-Based Bitcoin Trading Under Transaction Costs* : https://arxiv.org/abs/2606.00060
* *Multivariate Financial Forecasting using the Chronos TSFMs* : https://arxiv.org/abs/2605.21504
* *Artificial Intelligence in Equity and Crypto Markets* (revue) : https://arxiv.org/abs/2609.04917
* *PRICE: LLM Adaptation Choices for Bitcoin Price Forecasting* (Lag-Llama, Chronos) : https://arxiv.org/abs/2609.05235
* Chronos-2 : https://arxiv.org/abs/2510.15821 ; https://huggingface.co/amazon/chronos-2
* Toto : https://arxiv.org/abs/2505.14766 ; Moirai 2.0 : https://huggingface.co/Salesforce/moirai-2.0-R-small ; Lag-Llama : https://arxiv.org/abs/2310.08278
* Données des essais : https://data-api.binance.vision/api/v3/klines (BTCUSDT, SOLUSDT, ETHUSDT, 1 min)
