# État des lieux des indicateurs — direction à 5, 10, 15 minutes

*Généré le 2026-09-25 22:54 UTC par `python -m tradebot study --tickers BTCUSDT SOLUSDT --days 30` (durée totale : 67 s ; commit `9892f5a` + modifications locales). Actifs : BTCUSDT, SOLUSDT ; 30 jours de barres 1m ; apprentissage = 0,60 premiers de la période.*

> Recherche uniquement. Un indicateur « significatif » n'est pas une stratégie rentable : à 1 minute, couvrir 10 pb de frais aller-retour demande 72 à 89 % de bonnes directions (docs/research/methodologie.md § 0), alors que le meilleur hit-rate hors échantillon mesuré ici est de 53,9 %.

## 0. En bref

* **501 couples (indicateur, horizon)** mesurés sur 2 actif(s), soit **975 tests** : il faut une correction pour tests multiples (q-valeurs BH et BY) et viser |t| > 3.
* Plus fort signal : **`ret_60`** (momentum) à h = 15 : IC moyen −0,076, z de Stouffer −6,5, q BY 0,000, hit-rate hors échantillon 53,9 %, AUC 0,550.
* Sens : 100 % des 20 plus forts |IC| sont négatifs (IC < 0 : l'indicateur annonce un **retournement** ; > 0 : une continuation).
* **236 couples sur 501** ont à la fois q BY < 5 % et |z| > 3. Le z combine les actifs comme s'ils étaient indépendants (optimiste : les cryptos sont corrélées).
* Stabilité du top 20 : même signe sur tous les actifs pour 20/20 ; même signe entre apprentissage et test pour 20/20.
* Meilleur hit-rate hors échantillon : 53,9 % (`ret_vol_60`, h = 15) ; médiane des couples : 51,1 %.
* Modèle combiné, BTCUSDT : h = 5 : logit 51,4 % (AUC 0,518) contre retournement 50,7 % ; h = 10 : hgb 51,2 % (AUC 0,512) contre retournement 51,2 % ; h = 15 : logit 51,2 % (AUC 0,510) contre retournement 51,1 %.
* Modèle combiné, SOLUSDT : h = 5 : hgb 52,1 % (AUC 0,522) contre retournement 50,3 % ; h = 10 : logit 51,2 % (AUC 0,519) contre retournement 51,3 % ; h = 15 : logit 51,9 % (AUC 0,519) contre retournement 51,1 %.

## 1. Données

| ticker | n_bars | start | end | missing_bars | split_time | n_indicators | n_skipped | benchmark | deriv_coverage |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 43 199 | 2026-08-26 22:54 UTC | 2026-09-25 22:52 UTC | 0 | 2026-09-13 22:53 UTC | 158 | 9 | — | 97,0 % |
| SOLUSDT | 43 199 | 2026-08-26 22:55 UTC | 2026-09-25 22:53 UTC | 0 | 2026-09-13 22:54 UTC | 167 | 0 | BTCUSDT | 97,0 % |

## 2. Les indicateurs les plus informatifs

![Carte de chaleur des IC](ic_heatmap.png)

Les 20 couples (indicateur, horizon) de plus grand |IC moyen| (`top_20.csv`) :

| indicator | family | horizon | ic_mean | frac_same_sign | ic_train_mean | ic_test_mean | hit_rate_oos_mean | auc_oos_mean | z_combined | q_value_by |
|---|---|---|---|---|---|---|---|---|---|---|
| `ret_60` | momentum | 15 | −0,076 | 1,00 | −0,064 | −0,096 | 53,9 % | 0,550 | −6,5 | 0,000 |
| `ret_vol_60` | momentum | 15 | −0,073 | 1,00 | −0,063 | −0,089 | 53,9 % | 0,548 | −6,4 | 0,000 |
| `lin_slope_60` | tendance | 15 | −0,073 | 1,00 | −0,056 | −0,099 | 53,6 % | 0,549 | −6,2 | 0,000 |
| `ret_skip_60` | momentum | 15 | −0,071 | 1,00 | −0,061 | −0,088 | 53,9 % | 0,547 | −6,3 | 0,000 |
| `rel_ret_60` | marche | 15 | −0,071 | 1,00 | −0,056 | −0,098 | 52,7 % | 0,550 | −4,4 | 0,000 |
| `ema_gap_15_60` | tendance | 15 | −0,071 | 1,00 | −0,058 | −0,091 | 53,6 % | 0,548 | −6,1 | 0,000 |
| `ichimoku_cloud_dist` | tendance | 15 | −0,070 | 1,00 | −0,053 | −0,096 | 53,7 % | 0,553 | −6,3 | 0,000 |
| `ret_60` | momentum | 10 | −0,069 | 1,00 | −0,056 | −0,087 | 53,7 % | 0,548 | −7,1 | 0,000 |
| `zscore_60` | statistique | 15 | −0,066 | 1,00 | −0,049 | −0,093 | 53,5 % | 0,552 | −6,3 | 0,000 |
| `tick_imb_60` | flux | 15 | −0,066 | 1,00 | −0,066 | −0,072 | 52,5 % | 0,537 | −5,8 | 0,000 |
| `ret_vol_60` | momentum | 10 | −0,066 | 1,00 | −0,055 | −0,082 | 53,7 % | 0,548 | −7,0 | 0,000 |
| `sma_dist_60` | tendance | 15 | −0,065 | 1,00 | −0,049 | −0,090 | 53,5 % | 0,550 | −5,9 | 0,000 |
| `ichimoku_cloud_dist` | tendance | 10 | −0,064 | 1,00 | −0,048 | −0,088 | 53,5 % | 0,550 | −7,0 | 0,000 |
| `lin_slope_60` | tendance | 10 | −0,064 | 1,00 | −0,045 | −0,092 | 53,8 % | 0,550 | −6,6 | 0,000 |
| `obv_slope_60` | volume | 15 | −0,064 | 1,00 | −0,065 | −0,068 | 52,3 % | 0,535 | −5,7 | 0,000 |
| `ret_skip_60` | momentum | 10 | −0,064 | 1,00 | −0,053 | −0,081 | 53,7 % | 0,547 | −6,9 | 0,000 |
| `zscore_60` | statistique | 10 | −0,063 | 1,00 | −0,048 | −0,085 | 53,5 % | 0,550 | −7,0 | 0,000 |
| `ema_dist_60` | tendance | 15 | −0,063 | 1,00 | −0,052 | −0,080 | 53,2 % | 0,546 | −5,8 | 0,000 |
| `mom_z_60` | momentum | 15 | −0,063 | 1,00 | −0,045 | −0,088 | 53,3 % | 0,546 | −5,4 | 0,000 |
| `rel_ret_60` | marche | 10 | −0,063 | 1,00 | −0,049 | −0,083 | 52,6 % | 0,547 | −4,6 | 0,000 |

## 3. Les moins informatifs

Les 20 couples de plus petit |IC moyen| parmi ceux mesurés sur tous les actifs (`bottom_20.csv`) : ces indicateurs n'apportent rien seuls à ces horizons.

| indicator | family | horizon | ic_mean | frac_same_sign | ic_train_mean | ic_test_mean | hit_rate_oos_mean | auc_oos_mean | z_combined | q_value_by |
|---|---|---|---|---|---|---|---|---|---|---|
| `range_atr_14` | volatilite | 15 | +0,000 | 0,50 | −0,006 | +0,008 | 49,8 % | 0,499 | −0,1 | 1,000 |
| `session_asia` | calendrier | 5 | −0,000 | 0,50 | −0,002 | +0,001 | 49,8 % | 0,502 | +0,0 | 1,000 |
| `ls_ratio_accounts_log` | derives | 10 | −0,000 | 0,50 | +0,012 | −0,003 | 49,7 % | 0,498 | +0,1 | 1,000 |
| `roll_spread_60` | microstructure | 15 | +0,000 | 0,50 | +0,026 | −0,036 | 48,9 % | 0,487 | −0,2 | 1,000 |
| `ls_ratio_top_z_1440` | derives | 10 | −0,000 | 0,50 | +0,008 | −0,016 | 49,9 % | 0,497 | +0,6 | 1,000 |
| `ls_ratio_accounts_log` | derives | 5 | −0,000 | 1,00 | +0,006 | −0,001 | 49,9 % | 0,502 | +0,0 | 1,000 |
| `atr_pct_14` | volatilite | 5 | −0,001 | 0,50 | −0,006 | +0,003 | 49,9 % | 0,500 | +0,0 | 1,000 |
| `session_asia` | calendrier | 10 | −0,001 | 1,00 | −0,002 | −0,001 | 49,1 % | 0,496 | +0,0 | 1,000 |
| `ls_ratio_top_z_1440` | derives | 15 | −0,001 | 0,50 | +0,009 | −0,019 | 49,9 % | 0,494 | +0,5 | 1,000 |
| `bb_width_rel_20` | volatilite | 10 | +0,001 | 0,50 | −0,008 | +0,012 | 49,6 % | 0,494 | +0,1 | 1,000 |
| `rv_ratio_15_240` | volatilite | 15 | −0,001 | 1,00 | −0,001 | −0,001 | 49,5 % | 0,493 | −0,1 | 1,000 |
| `vr_5_240` | statistique | 15 | −0,001 | 0,50 | +0,001 | −0,007 | 50,5 % | 0,503 | −0,1 | 1,000 |
| `rv_60` | volatilite | 5 | +0,001 | 1,00 | −0,007 | +0,006 | 49,6 % | 0,498 | +0,1 | 1,000 |
| `rel_volume_1440` | volume | 5 | +0,001 | 0,50 | −0,007 | +0,009 | 49,7 % | 0,500 | +0,1 | 1,000 |
| `trades_rel_60` | flux | 5 | +0,001 | 0,50 | +0,001 | +0,001 | 49,9 % | 0,499 | +0,2 | 1,000 |
| `ls_ratio_accounts_log` | derives | 15 | −0,001 | 0,50 | +0,010 | +0,001 | 49,6 % | 0,500 | +0,2 | 1,000 |
| `session_asia` | calendrier | 15 | −0,001 | 1,00 | +0,001 | −0,007 | 48,9 % | 0,497 | −0,0 | 1,000 |
| `garman_klass_15` | volatilite | 5 | −0,001 | 0,50 | −0,008 | +0,005 | 49,9 % | 0,500 | +0,0 | 1,000 |
| `bb_width_20_2` | volatilite | 10 | −0,001 | 0,50 | −0,012 | +0,010 | 49,4 % | 0,495 | −0,1 | 1,000 |
| `roll_spread_60` | microstructure | 5 | −0,001 | 0,50 | +0,018 | −0,027 | 48,5 % | 0,483 | −0,3 | 1,000 |

## 4. Synthèse par famille

![Meilleur IC par famille](familles.png)

| family | n_indicators | n_pairs | median_abs_ic | max_abs_ic | best_indicator | best_horizon | best_ic | frac_q_by_05 | mean_hit_rate_oos | mean_auc_oos |
|---|---|---|---|---|---|---|---|---|---|---|
| momentum | 30 | 90 | 0,029 | 0,076 | `ret_60` | 15 | −0,076 | 84 % | 51,7 % | 0,523 |
| tendance | 33 | 99 | 0,034 | 0,073 | `lin_slope_60` | 15 | −0,073 | 77 % | 51,8 % | 0,524 |
| marche | 9 | 27 | 0,022 | 0,071 | `rel_ret_60` | 15 | −0,071 | 22 % | 51,0 % | 0,516 |
| statistique | 10 | 30 | 0,015 | 0,066 | `zscore_60` | 15 | −0,066 | 40 % | 50,8 % | 0,512 |
| flux | 12 | 36 | 0,029 | 0,066 | `tick_imb_60` | 15 | −0,066 | 61 % | 51,5 % | 0,520 |
| volume | 15 | 45 | 0,036 | 0,064 | `obv_slope_60` | 15 | −0,064 | 71 % | 51,2 % | 0,519 |
| volatilite | 19 | 57 | 0,007 | 0,059 | `donchian_pos_60` | 15 | −0,059 | 23 % | 50,5 % | 0,507 |
| calendrier | 14 | 42 | 0,009 | 0,037 | `session_us` | 15 | +0,037 | 2 % | 50,2 % | 0,504 |
| derives | 14 | 42 | 0,011 | 0,034 | `funding_rate_bp` | 15 | +0,034 | 5 % | 50,2 % | 0,502 |
| microstructure | 11 | 33 | 0,007 | 0,022 | `amihud_rel_60` | 15 | −0,022 | 0 % | 50,0 % | 0,501 |

## 5. Modèles combinés (walk-forward purgé) contre baselines

Tous les indicateurs de l'actif, `logit` (régression logistique régularisée) et `hgb` (gradient boosting), walk-forward croissant en 5 plis purgés de h barres, une origine toutes les 5 barres ; baselines sur les mêmes barres. Détail : `combined_summary.csv` ; bornes des plis : `combined_folds.csv`.

![Modèles combinés contre baselines](modeles_combines.png)

| ticker | model | acc h5 | acc h10 | acc h15 | AUC h5 | AUC h10 | AUC h15 | acc_w h5 | acc_w h10 | acc_w h15 | n_eff (h max) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | `logit` | 51,4 % | 51,2 % | 51,2 % | 0,518 | 0,516 | 0,510 | 51,4 % | 50,2 % | 50,0 % | 2 396 |
| BTCUSDT | `hgb` | 51,2 % | 51,2 % | 50,1 % | 0,520 | 0,512 | 0,515 | 48,9 % | 49,8 % | 50,0 % | 2 396 |
| BTCUSDT | `reversal_h` | 50,7 % | 51,2 % | 51,1 % | 0,508 | 0,512 | 0,510 | 49,7 % | 50,8 % | 50,1 % | 2 396 |
| BTCUSDT | `momentum_h` | 49,2 % | 48,8 % | 49,0 % | 0,492 | 0,488 | 0,490 | 50,3 % | 49,3 % | 49,9 % | 2 396 |
| BTCUSDT | `always_up` | 50,3 % | 50,0 % | 50,1 % | 0,500 | 0,500 | 0,500 | 50,6 % | 50,9 % | 51,1 % | 2 396 |
| BTCUSDT | `majority_prev_day` | 50,2 % | 50,5 % | 48,0 % | 0,503 | 0,505 | 0,480 | 48,6 % | 49,9 % | 46,5 % | 2 396 |
| SOLUSDT | `logit` | 50,8 % | 51,2 % | 51,9 % | 0,512 | 0,519 | 0,519 | 50,3 % | 51,5 % | 51,9 % | 2 357 |
| SOLUSDT | `hgb` | 52,1 % | 50,6 % | 51,9 % | 0,522 | 0,514 | 0,522 | 50,8 % | 51,1 % | 52,1 % | 2 357 |
| SOLUSDT | `reversal_h` | 50,3 % | 51,3 % | 51,1 % | 0,503 | 0,512 | 0,512 | 49,9 % | 51,0 % | 50,9 % | 2 357 |
| SOLUSDT | `momentum_h` | 49,7 % | 49,0 % | 48,8 % | 0,497 | 0,488 | 0,488 | 50,1 % | 49,0 % | 49,2 % | 2 357 |
| SOLUSDT | `always_up` | 50,0 % | 50,3 % | 50,8 % | 0,500 | 0,500 | 0,500 | 50,9 % | 51,3 % | 51,7 % | 2 357 |
| SOLUSDT | `majority_prev_day` | 50,2 % | 50,5 % | 50,8 % | 0,502 | 0,505 | 0,509 | 50,4 % | 50,1 % | 50,4 % | 2 357 |

`acc` : accuracy ; `acc_w` : accuracy pondérée par |rendement| (ce qui compte pour le P&L). Les baselines binaires valent 0,52 / 0,48 : leur AUC égale leur balanced accuracy.

## 6. Fichiers

| fichier | contenu |
|---|---|
| `scores_<ACTIF>.csv` | scores de chaque indicateur × horizon pour un actif (`indicator_scores`) |
| `aggregate.csv` | agrégat entre actifs, trié par |ic_mean| (`aggregate_scores`) ; sert à choisir les covariables TimesFM (`ic_train_mean`) |
| `top_20.csv, bottom_20.csv` | extraits de l'agrégat (plus grands / plus petits |ic_mean|) |
| `familles.csv` | synthèse par famille |
| `combined_summary.csv` | métriques hors échantillon des modèles combinés et des baselines |
| `combined_folds.csv` | bornes de chaque pli (vérification de la purge) |
| `catalogue_indicateurs.csv` | nom, famille et description de chaque indicateur |
| `donnees.csv` | couverture des données par actif |
| `timings.csv` | temps de chaque étape |
| `run.json` | paramètres, dates de coupure, versions, commit |
| `ic_heatmap.png, familles.png, modeles_combines.png` | figures |

## 7. Dictionnaire des colonnes

### scores_<ACTIF>.csv

| colonne | signification |
|---|---|
| `ticker` | Actif (paire Binance contre USDT). |
| `indicator` | Nom de l'indicateur (voir `catalogue_indicateurs.csv` pour sa description). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `n` | Nombre de barres où l'indicateur et le rendement futur sont tous deux définis. |
| `ic_spearman` | IC (coefficient d'information) : corrélation de Spearman entre l'indicateur en t et le rendement futur à h barres, sur toute la période. Négatif = l'indicateur annonce un retournement ; positif = une continuation. \|IC\| de 0,05 est déjà « fort » à 1 minute. |
| `ic_daily_mean` | Moyenne des IC calculés jour par jour (jours UTC d'au moins 100 barres). |
| `ic_daily_t` | t de Student des IC journaliers : moyenne / (écart type / √nombre de jours). Robuste au chevauchement des cibles à l'intérieur d'une journée. |
| `n_days` | Nombre de jours retenus pour l'IC journalier. |
| `frac_months_same_sign` | Part des mois dont l'IC journalier moyen a le même signe que l'IC global (stabilité ; viser ≥ 0,7). |
| `ic_nw_t` | t de l'IC corrigé de Newey-West (retards = h) : tient compte du chevauchement des cibles. |
| `ic_nw_t_2h` | Idem avec 2h retards (plus prudent, recommandé par la méthodologie). |
| `p_value` | p-valeur bilatérale de `ic_nw_t`. |
| `t_cons` | t le plus prudent parmi `ic_nw_t`, `ic_nw_t_2h` et `ic_daily_t` (0 si leurs signes divergent). C'est celui qu'il faut regarder ; viser \|t\| > 3. |
| `p_value_cons` | p-valeur bilatérale de `t_cons`. |
| `ic_train` | IC sur la partie apprentissage (les `train_frac` premiers pourcents de la période, moins h barres de purge). |
| `ic_test` | IC sur la partie test (après la coupure) : l'IC d'apprentissage se maintient-il ? |
| `train_sign` | Signe de `ic_train` (+1 / −1) : sens dans lequel on utilise l'indicateur sur le test. |
| `median_train` | Médiane de l'indicateur sur l'apprentissage (seuil de décision sur le test). |
| `hit_rate_oos` | Hors échantillon (test) : part des barres où « hausse si (x − médiane) × signe > 0 » donne la bonne direction. 0,5 = hasard. |
| `auc_oos` | AUC hors échantillon de signe × indicateur contre la direction réalisée (0,5 = hasard). |
| `up_rate_oos` | Part de hausses sur le test (taux de base). |
| `n_oos` | Barres de test dont la direction est définie (hors égalités et trous). |
| `n_eff_oos` | Taille effective du test : n_oos / h (les cibles de barres voisines se chevauchent). |
| `p_hit` | p-valeur binomiale unilatérale de `hit_rate_oos` > 0,5 sur n_eff_oos essais (prudente). |

### aggregate.csv, top_20.csv, bottom_20.csv

| colonne | signification |
|---|---|
| `indicator` | Nom de l'indicateur (voir `catalogue_indicateurs.csv` pour sa description). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `family` | Famille de l'indicateur (tendance, momentum, volatilite, volume, flux, microstructure, statistique, calendrier, marche, derives). |
| `n_tickers` | Nombre d'actifs où l'IC est défini. |
| `ic_mean` | IC moyen entre actifs (critère de classement). |
| `ic_std` | Écart type de l'IC entre actifs. |
| `ic_t_cross` | t inter-actifs : ic_mean / (ic_std / √n_tickers) (défini dès 2 actifs, peu de degrés de liberté). |
| `frac_same_sign` | Part des actifs dont l'IC a le signe de `ic_mean` (viser au moins 4 actifs sur 6). |
| `ic_daily_t_mean` | Moyenne entre actifs du t journalier. |
| `ic_train_mean` | IC moyen sur les parties apprentissage seulement : c'est lui qui sert à choisir des covariables TimesFM sans regarder le test. |
| `ic_test_mean` | IC moyen sur les parties test. |
| `hit_rate_oos_mean` | Hit-rate hors échantillon moyen entre actifs. |
| `auc_oos_mean` | AUC hors échantillon moyenne entre actifs. |
| `z_combined` | Z de Stouffer : somme des t prudents (`t_cons`) des actifs / √nombre d'actifs. |
| `p_combined` | p-valeur bilatérale de `z_combined` (optimiste : les cryptos sont corrélées entre elles). |
| `q_value` | q-valeur de Benjamini-Hochberg sur toutes les lignes (indicateurs × horizons) : taux de fausses découvertes attendu si l'on retient cette ligne et les meilleures. |
| `q_value_by` | q-valeur de Benjamini-Yekutieli : valable même si les tests sont dépendants (indicateurs très corrélés). Plus sévère ; à privilégier. |

### familles.csv

| colonne | signification |
|---|---|
| `family` | Famille de l'indicateur (tendance, momentum, volatilite, volume, flux, microstructure, statistique, calendrier, marche, derives). |
| `n_indicators` | Nombre d'indicateurs de la famille présents dans l'agrégat. |
| `n_pairs` | Nombre de couples (indicateur, horizon). |
| `median_abs_ic` | Médiane de \|ic_mean\| sur les couples de la famille. |
| `max_abs_ic` | Plus grand \|ic_mean\| de la famille. |
| `best_indicator` | Indicateur qui atteint `max_abs_ic`. |
| `best_horizon` | Horizon de ce meilleur couple. |
| `best_ic` | ic_mean (signé) de ce meilleur couple. |
| `frac_q_by_05` | Part des couples de la famille avec q_value_by < 0,05. |
| `mean_hit_rate_oos` | Moyenne de `hit_rate_oos_mean` sur la famille. |
| `mean_auc_oos` | Moyenne de `auc_oos_mean` sur la famille. |

### combined_summary.csv

| colonne | signification |
|---|---|
| `ticker` | Actif (paire Binance contre USDT). |
| `model` | Modèle : `logit` et `hgb` (gradient boosting) combinent tous les indicateurs en walk-forward purgé ; `reversal_h`, `momentum_h`, `always_up`, `majority_prev_day` sont les baselines naïves évaluées sur exactement les mêmes barres ; `timesfm` = TimesFM zero-shot, `timesfm_cal` = sa probabilité recalibrée (isotonique). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `n` | Nombre de barres où l'indicateur et le rendement futur sont tous deux définis. |
| `n_eff` | Taille effective : n divisé par le chevauchement des cibles (ceil(h / pas entre origines)). |
| `accuracy` | Part des directions correctement prévues (hausse prévue si P(hausse) > 0,5). |
| `balanced_accuracy` | Moyenne des taux de réussite sur les hausses et sur les baisses (insensible au taux de base). |
| `auc` | AUC de la probabilité de hausse (0,5 = aucun pouvoir de classement). |
| `brier` | Score de Brier : moyenne de (P(hausse) − y)². 0,25 = toujours 0,5 ; plus bas = mieux. |
| `log_loss` | Log-loss (entropie croisée). ln 2 ≈ 0,693 = toujours 0,5 ; plus bas = mieux. |
| `bss` | Brier skill score contre la climatologie hors échantillon (taux de hausse du test) : > 0 = mieux que la climatologie. |
| `acc_w` | Accuracy pondérée par \|rendement\| : c'est elle qui compte pour gagner de l'argent. |
| `up_rate` | Part de hausses réalisées. |
| `p_binom` | p-valeur binomiale unilatérale (accuracy > 0,5) sur n_eff essais. |

### combined_folds.csv

| colonne | signification |
|---|---|
| `ticker` | Actif (paire Binance contre USDT). |
| `model` | Modèle : `logit` et `hgb` (gradient boosting) combinent tous les indicateurs en walk-forward purgé ; `reversal_h`, `momentum_h`, `always_up`, `majority_prev_day` sont les baselines naïves évaluées sur exactement les mêmes barres ; `timesfm` = TimesFM zero-shot, `timesfm_cal` = sa probabilité recalibrée (isotonique). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `fold` | Numéro du pli du walk-forward (0 = le plus ancien). |
| `n_train` | Lignes d'apprentissage du pli (après purge et éclaircissement). |
| `train_start` | Première ligne d'apprentissage. |
| `train_end` | Dernière ligne d'apprentissage (au moins h barres avant le test : purge). |
| `train_end_row` | Position de `train_end` dans les données. |
| `test_start` | Première ligne de test du pli. |
| `test_end` | Dernière ligne de test du pli. |
| `test_start_row` | Position de `test_start` dans les données. |

### donnees.csv, timings.csv

| colonne | signification |
|---|---|
| `ticker` | Actif (paire Binance contre USDT). |
| `n_bars` | Barres de 1 minute chargées (clôturées). |
| `start` | Première barre (ouverture, UTC). |
| `end` | Dernière barre (ouverture, UTC). |
| `missing_bars` | Barres absentes de la grille régulière (pannes de l'exchange) : jamais inventées ; les cibles qui les traversent valent NaN. |
| `split_time` | Première barre de la partie test (coupure à `train_frac`). |
| `n_indicators` | Nombre d'indicateurs de la famille présents dans l'agrégat. |
| `n_skipped` | Indicateurs ignorés faute de colonnes (ex. force relative pour BTC lui-même). |
| `benchmark` | Référence de marché pour la force relative (BTCUSDT pour les altcoins). |
| `deriv_coverage` | Part des barres avec un open interest disponible (dérivés futures). |
| `step` | Étape chronométrée. |
| `seconds` | Durée de l'étape en secondes (horloge murale, machine partagée : ordre de grandeur). |

## 8. Temps de calcul

| ticker | chargement | référence de marché | indicateurs | cibles | scores des indicateurs | modèle combiné logit | modèle combiné hgb | baselines (mêmes barres) | agrégat et rapport | total |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 2,1 | 0,0 | 0,6 | 0,0 | 4,1 | 17,6 | 5,4 | 0,3 | — | — |
| SOLUSDT | 0,8 | 0,0 | 0,7 | 0,0 | 4,6 | 22,7 | 6,0 | 0,2 | — | — |
| (global) | — | — | — | — | — | — | — | — | 1,8 | 67,4 |

## 9. Méthode et limites

* **Coupure apprentissage / test** : `split = round(train_frac × n)` barres ; apprentissage = barres < split − h (purge de h barres : leurs cibles chevauchent le test), test = barres ≥ split. Signe et médiane de chaque indicateur appris sur l'apprentissage seulement.
* **Chevauchement** : à h barres, les cibles de barres voisines partagent h − 1 minutes ; t de Newey-West (h et 2h retards), t par jours et binomial sur n / h en tiennent compte.
* **Tests multiples** : Benjamini-Hochberg et Benjamini-Yekutieli sur toutes les lignes ; Stouffer suppose des actifs indépendants (optimiste).
* **Pas de coffre-fort** : la méthodologie (§ 4.4) réserve les 30 derniers jours à une validation finale unique ; cette commande utilise toute la période demandée.
* **Modèles combinés** : NaN imputés par la médiane de l'apprentissage du pli ; apprentissage éclairci au-delà de 100000 lignes ; arrêt précoce chronologique pour `hgb`.
* **Dérivés** alignés avec un décalage de publication prudent (+5 min) ; le funding du mois en cours n'existe pas en fichiers bulk (NaN).
