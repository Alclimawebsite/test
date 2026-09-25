# Backtest TimesFM (zero-shot) contre baselines — direction à 5, 10 et 15 minutes

*Mis à jour le 2026-09-25 22:56 UTC ; 2 run(s). Chaque `python -m tradebot timesfm-backtest` ajoute ou remplace ses runs.*

> **Licence** : les poids de TimesFM 3.0 (`timesfm3`) sont sous licence non commerciale (`timesfm-non-commercial-license-v1.0`) : recherche uniquement, aucun trading réel. TimesFM 2.5 (`timesfm2p5`) est sous Apache-2.0.
>
> **Puissance** : quelques centaines d'origines ne permettent de détecter qu'une accuracy vraie de 57 % ou plus ; trois horizons testés = trois essais. Ne rien conclure d'un écart de quelques points.

## 0. En bref

* **BTCUSDT_timesfm3_none_s30_c512_logprice** (200 origines) — TimesFM ≈ baselines : écarts dans le bruit (n_eff ≈ 199). Accuracy TimesFM contre retournement : h = 5 : 51,5 % contre 51,0 % ; h = 10 : 46,2 % contre 47,7 % ; h = 15 : 46,0 % contre 49,5 %. Avec n_eff ≈ 198 (h = 15), seule une accuracy vraie ≥ 58,8 % serait détectable (puissance 80 %).
  ρ(P(hausse), rendement des 60 min passées) : −0,19 / −0,20 / −0,18 ; IC partiel (hors retournement) : +0,108 / +0,032 / −0,079.
  Mini-stratégie (P brute, seuil 0,55, 10 pb) : −8,3 pb / −9,0 pb / −10,4 pb nets par trade.
* **SOLUSDT_timesfm3_top5_s60_c512_logprice** (60 origines) — TimesFM ≈ baselines : écarts dans le bruit (n_eff ≈ 60). Accuracy TimesFM contre retournement : h = 5 : 53,3 % contre 51,7 % ; h = 10 : 63,3 % contre 58,3 % ; h = 15 : 51,7 % contre 56,7 %. Avec n_eff ≈ 60 (h = 15), seule une accuracy vraie ≥ 66,1 % serait détectable (puissance 80 %).
  ρ(P(hausse), rendement des 60 min passées) : −0,46 / −0,48 / −0,49 ; IC partiel (hors retournement) : +0,214 / +0,258 / +0,208.
  Mini-stratégie (P brute, seuil 0,55, 10 pb) : −6,5 pb / −4,8 pb / −3,4 pb nets par trade.

## 1. Runs

| run | ticker | backend | covariates | stride | context_len | n_origins | first_origin | last_origin | walk_forward_s | origins_per_s |
|---|---|---|---|---|---|---|---|---|---|---|
| `BTCUSDT_timesfm3_none_s30_c512_logprice` | BTCUSDT | timesfm3 | none | 30 | 512 | 200 | 2026-05-02 23:00 UTC | 2026-09-25 22:30 UTC | 20 | 10,2 |
| `SOLUSDT_timesfm3_top5_s60_c512_logprice` | SOLUSDT | timesfm3 | top5 | 60 | 512 | 60 | 2026-09-13 23:00 UTC | 2026-09-25 22:00 UTC | 29 | 2,1 |

## 2. Détail par run

`timesfm` : P(hausse) brute sur toutes les origines ; `timesfm_cal` : recalibrée (isotonique) sur la 2e moitié des origines, apprise sur la 1re (seulement si elle compte au moins 300 labels par horizon) ; baselines sur les mêmes origines. Stratégie : `reversal_h` au seuil 0,5 (trade à chaque signal) sert de référence de coût.

### BTCUSDT_timesfm3_none_s30_c512_logprice

Commande : `python -m tradebot timesfm-backtest --tickers BTCUSDT --covariates none --stride 30 --max-origins 200`  
Backend `timesfm3`, contexte 512 (logprice), pas 30 min, origines du 2026-05-02 23:00 UTC au 2026-09-25 22:30 UTC (200 sur 7008 candidates, 0 écartées pour trou), covariables : aucune. Walk-forward : 20 s (10,2 origines/s).

![Accuracy BTCUSDT_timesfm3_none_s30_c512_logprice](BTCUSDT_timesfm3_none_s30_c512_logprice_accuracy.png)

| model | sample | horizon | n | n_eff | accuracy | balanced_accuracy | auc | brier | log_loss | acc_w | p_binom | pt_hac_t | dm_brier_vs_reversal_t |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `timesfm` | toutes | 5 | 198 | 198 | 51,5 % | 51,5 % | 0,554 | 0,2466 | 0,6858 | 49,1 % | 0,361 | +0,44 | −0,59 |
| `timesfm` | toutes | 10 | 199 | 199 | 46,2 % | 46,3 % | 0,495 | 0,2607 | 0,7161 | 52,0 % | 0,872 | −1,09 | +1,37 |
| `timesfm` | toutes | 15 | 200 | 200 | 46,0 % | 46,0 % | 0,456 | 0,2690 | 0,7344 | 49,9 % | 0,885 | −1,25 | +2,55 |
| `reversal_h` | toutes | 5 | 198 | 198 | 51,0 % | 51,0 % | 0,513 | 0,2499 | 0,6929 | 53,1 % | 0,416 | +0,28 | — |
| `reversal_h` | toutes | 10 | 199 | 199 | 47,7 % | 47,8 % | 0,478 | 0,2513 | 0,6958 | 40,6 % | 0,761 | −0,63 | — |
| `reversal_h` | toutes | 15 | 200 | 200 | 49,5 % | 49,5 % | 0,495 | 0,2506 | 0,6943 | 48,5 % | 0,584 | −0,13 | — |
| `momentum_h` | toutes | 5 | 198 | 198 | 48,5 % | 48,5 % | 0,487 | 0,2509 | 0,6950 | 46,0 % | 0,691 | −0,41 | +0,34 |
| `momentum_h` | toutes | 10 | 199 | 199 | 52,3 % | 52,2 % | 0,522 | 0,2495 | 0,6921 | 59,4 % | 0,285 | +0,63 | −0,65 |
| `momentum_h` | toutes | 15 | 200 | 200 | 50,5 % | 50,5 % | 0,505 | 0,2502 | 0,6935 | 51,6 % | 0,472 | +0,13 | −0,13 |
| `always_up` | toutes | 5 | 198 | 198 | 50,5 % | 50,0 % | 0,500 | 0,2502 | 0,6935 | 50,4 % | 0,472 | — | +0,14 |
| `always_up` | toutes | 10 | 199 | 199 | 48,2 % | 50,0 % | 0,500 | 0,2511 | 0,6954 | 45,3 % | 0,715 | — | −0,10 |
| `always_up` | toutes | 15 | 200 | 200 | 50,0 % | 50,0 % | 0,500 | 0,2504 | 0,6939 | 44,1 % | 0,528 | — | −0,09 |
| `majority_prev_day` | toutes | 5 | 198 | 198 | 44,4 % | 44,6 % | 0,448 | 0,2525 | 0,6982 | 47,5 % | 0,949 | −1,54 | +1,28 |
| `majority_prev_day` | toutes | 10 | 199 | 199 | 51,3 % | 50,9 % | 0,509 | 0,2499 | 0,6929 | 55,9 % | 0,388 | +0,27 | −0,77 |
| `majority_prev_day` | toutes | 15 | 200 | 200 | 50,0 % | 50,0 % | 0,497 | 0,2505 | 0,6941 | 55,5 % | 0,528 | +0,00 | −0,05 |

| horizon | n | coverage_q10_q90 | rho_past_60 | ic_p_up | ic_partial | median_abs_pred_bps | median_abs_ret_bps | frac_pred_gt_cost |
|---|---|---|---|---|---|---|---|---|
| 5 | 200 | 0,805 | −0,19 | +0,107 | +0,108 | 0,9 | 5,5 | 1,0 % |
| 10 | 200 | 0,790 | −0,20 | +0,028 | +0,032 | 1,5 | 6,8 | 3,0 % |
| 15 | 200 | 0,715 | −0,18 | −0,077 | −0,079 | 2,0 | 8,6 | 6,0 % |

| signal | h | threshold | cost_bps | n_trades | hit_rate | gross_bps_mean | net_bps_mean | net_bps_se |
|---|---|---|---|---|---|---|---|---|
| `timesfm` | 5 | 0,55 | 10 | 97 | 59,8 % | +1,66 | −8,34 | 1,26 |
| `reversal_h` | 5 | 0,50 | 10 | 198 | 51,0 % | +0,58 | −9,42 | 0,91 |
| `timesfm` | 10 | 0,55 | 10 | 104 | 51,0 % | +0,99 | −9,01 | 1,45 |
| `reversal_h` | 10 | 0,50 | 10 | 199 | 47,7 % | −2,01 | −12,01 | 1,22 |
| `timesfm` | 15 | 0,55 | 10 | 124 | 44,4 % | −0,41 | −10,41 | 1,93 |
| `reversal_h` | 15 | 0,50 | 10 | 198 | 49,5 % | −0,45 | −10,45 | 1,64 |

### SOLUSDT_timesfm3_top5_s60_c512_logprice

Commande : `python -m tradebot timesfm-backtest --tickers SOLUSDT --covariates top --top-k 5 --stride 60 --max-origins 60`  
Backend `timesfm3`, contexte 512 (logprice), pas 60 min, origines du 2026-09-13 23:00 UTC au 2026-09-25 22:00 UTC (60 sur 288 candidates, 0 écartées pour trou), covariables : tick_imb_60, obv_slope_60, ema_dist_240, ret_60, ret_vol_60. Walk-forward : 29 s (2,1 origines/s).

![Accuracy SOLUSDT_timesfm3_top5_s60_c512_logprice](SOLUSDT_timesfm3_top5_s60_c512_logprice_accuracy.png)

| model | sample | horizon | n | n_eff | accuracy | balanced_accuracy | auc | brier | log_loss | acc_w | p_binom | pt_hac_t | dm_brier_vs_reversal_t |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `timesfm` | toutes | 5 | 60 | 60 | 53,3 % | 53,4 % | 0,532 | 0,2518 | 0,6972 | 66,0 % | 0,349 | +0,50 | +0,12 |
| `timesfm` | toutes | 10 | 60 | 60 | 63,3 % | 64,0 % | 0,668 | 0,2270 | 0,6440 | 65,3 % | 0,026 | +2,01 | −1,26 |
| `timesfm` | toutes | 15 | 60 | 60 | 51,7 % | 52,1 % | 0,559 | 0,2449 | 0,6800 | 68,6 % | 0,449 | +0,32 | −0,18 |
| `reversal_h` | toutes | 5 | 60 | 60 | 51,7 % | 51,8 % | 0,511 | 0,2501 | 0,6933 | 52,5 % | 0,449 | +0,23 | — |
| `reversal_h` | toutes | 10 | 60 | 60 | 58,3 % | 58,1 % | 0,603 | 0,2464 | 0,6859 | 61,3 % | 0,123 | +1,27 | — |
| `reversal_h` | toutes | 15 | 60 | 60 | 56,7 % | 57,6 % | 0,576 | 0,2477 | 0,6886 | 66,1 % | 0,183 | +1,18 | — |
| `momentum_h` | toutes | 5 | 60 | 60 | 50,0 % | 49,8 % | 0,489 | 0,2507 | 0,6946 | 50,0 % | 0,551 | −0,02 | +0,11 |
| `momentum_h` | toutes | 10 | 60 | 60 | 38,3 % | 38,2 % | 0,397 | 0,2544 | 0,7019 | 33,8 % | 0,974 | −2,07 | +1,67 |
| `momentum_h` | toutes | 15 | 60 | 60 | 43,3 % | 42,4 % | 0,424 | 0,2531 | 0,6993 | 33,9 % | 0,877 | −1,18 | +1,00 |
| `always_up` | toutes | 5 | 60 | 60 | 48,3 % | 50,0 % | 0,500 | 0,2511 | 0,6953 | 38,5 % | 0,651 | — | +0,27 |
| `always_up` | toutes | 10 | 60 | 60 | 45,0 % | 50,0 % | 0,500 | 0,2524 | 0,6979 | 43,7 % | 0,817 | — | +1,85 |
| `always_up` | toutes | 15 | 60 | 60 | 40,0 % | 50,0 % | 0,500 | 0,2544 | 0,7020 | 41,3 % | 0,954 | — | +2,22 |
| `majority_prev_day` | toutes | 5 | 60 | 60 | 45,0 % | 45,2 % | 0,452 | 0,2524 | 0,6979 | 55,1 % | 0,817 | −0,73 | +0,58 |
| `majority_prev_day` | toutes | 10 | 60 | 60 | 48,3 % | 49,0 % | 0,490 | 0,2511 | 0,6953 | 56,7 % | 0,651 | −0,18 | +1,30 |
| `majority_prev_day` | toutes | 15 | 60 | 60 | 56,7 % | 58,3 % | 0,583 | 0,2477 | 0,6886 | 55,4 % | 0,183 | +1,44 | +0,00 |

| horizon | n | coverage_q10_q90 | rho_past_60 | ic_p_up | ic_partial | median_abs_pred_bps | median_abs_ret_bps | frac_pred_gt_cost |
|---|---|---|---|---|---|---|---|---|
| 5 | 60 | 0,800 | −0,46 | +0,200 | +0,214 | 2,8 | 12,4 | 6,7 % |
| 10 | 60 | 0,833 | −0,48 | +0,282 | +0,258 | 4,3 | 18,7 | 16,7 % |
| 15 | 60 | 0,883 | −0,49 | +0,257 | +0,208 | 5,9 | 18,6 | 28,3 % |

| signal | h | threshold | cost_bps | n_trades | hit_rate | gross_bps_mean | net_bps_mean | net_bps_se |
|---|---|---|---|---|---|---|---|---|
| `timesfm` | 5 | 0,55 | 10 | 38 | 44,7 % | +3,54 | −6,46 | 3,49 |
| `reversal_h` | 5 | 0,50 | 10 | 59 | 50,8 % | +0,43 | −9,57 | 2,91 |
| `timesfm` | 10 | 0,55 | 10 | 43 | 60,5 % | +5,19 | −4,81 | 4,57 |
| `reversal_h` | 10 | 0,50 | 10 | 58 | 60,3 % | +6,37 | −3,63 | 4,06 |
| `timesfm` | 15 | 0,55 | 10 | 44 | 47,7 % | +6,58 | −3,42 | 4,51 |
| `reversal_h` | 15 | 0,50 | 10 | 60 | 56,7 % | +7,66 | −2,34 | 4,31 |

## 3. Fichiers

| fichier | contenu |
|---|---|
| `runs.csv` | un run par ligne (paramètres, période, débit) |
| `<run>_predictions.csv` | une ligne par (origine, horizon) : prévision TimesFM, baselines, rendement réalisé |
| `<run>_metrics.csv` | métriques de direction par modèle × horizon |
| `<run>_diagnostics.csv` | calibration des déciles, lien avec le rendement passé |
| `<run>_strategy.csv` | mini-stratégie non chevauchante avec frais |
| `<run>_accuracy.png` | accuracy − 50 % de TimesFM et des baselines |
| `timings_last_run.csv` | temps des étapes du dernier appel |

## 4. Dictionnaire des colonnes

### runs.csv

| colonne | signification |
|---|---|
| `run` | Identifiant du run : actif_backend_covariables_pas_contexte_transformation. |
| `ticker` | Actif (paire Binance contre USDT). |
| `generated_at` | Date du run (UTC). |
| `command` | Commande exacte. |
| `backend` | `timesfm3` (licence non commerciale) ou `timesfm2p5` (Apache-2.0). |
| `covariates` | Mode de covariables : `none`, `topK` (K meilleurs \|ic_train_mean\|), `all`. |
| `covariate_names` | Indicateurs passés en covariables past-only (séparés par `;`). |
| `stride` | Pas entre origines, en barres (grille alignée sur l'horloge UTC). |
| `context_len` | Longueur du contexte TimesFM, en barres. |
| `transform` | Espace du contexte : `logprice`, `price` ou `cumret` (toujours centré en float64). |
| `symmetric` | Moyenne symétrique (prévision exactement antisymétrique : pas de biais haussier). |
| `max_origins` | Plafond du nombre d'origines (réparties régulièrement sur la période). |
| `n_candidates` | Origines possibles sur la période avant plafond. |
| `n_excluded_gaps` | Origines écartées car leur contexte contient un trou de plus de 3 barres. |
| `n_origins` | Origines effectivement prévues. |
| `first_origin` | Première origine. |
| `last_origin` | Dernière origine. |
| `test_part_start` | Début de la partie test de l'actif (coupure à `train_frac`). |
| `days` | Jours d'historique chargés. |
| `train_frac` | Part d'apprentissage : les origines sont prises après. |
| `cost_bps` | Coût aller-retour déduit de chaque trade (points de base ; 10 = futures taker). |
| `threshold` | Seuil : long si P(hausse) ≥ seuil, short si ≤ 1 − seuil. |
| `walk_forward_s` | Durée du walk-forward TimesFM (s). |
| `origins_per_s` | Débit : origines prévues par seconde (CPU, 3 horizons en un appel). |

### <run>_predictions.csv

| colonne | signification |
|---|---|
| `time` | Origine t : horodatage d'ouverture de la dernière barre connue (sa clôture est le dernier prix du contexte). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `last_close` | close[t], dernier prix du contexte. |
| `pred_median` | Médiane prévue du close à t + h (en prix). |
| `p_up` | P(close[t+h] > close[t]) déduite des déciles TimesFM (brute, non recalibrée). |
| `p_up_cal` | P(hausse) recalibrée (isotonique apprise sur la 1re moitié des origines) ; vide sur la 1re moitié. |
| `ret` | Rendement réalisé log(close[t+h] / close[t]) (vide si trou de données). |
| `y_true` | Direction réalisée : 1 hausse, 0 baisse, vide si égalité. |
| `pred_ret` | Médiane prévue en log-rendement. |
| `q10_ret` | Décile 10 % prévu en log-rendement. |
| `q90_ret` | Décile 90 % prévu en log-rendement. |
| `reversal_h` | Baseline « retournement » : 0,52 si le rendement des h dernières barres est négatif (on parie contre le mouvement), 0,48 s'il est positif, 0,5 sinon. C'est le vrai rival à 5–15 min. |
| `momentum_h` | Baseline « momentum » : l'inverse du retournement (0,52 après une hausse). |
| `always_up` | Baseline « toujours hausse » (0,52) : mesure l'avantage dû au seul taux de base. |
| `majority_prev_day` | Baseline « majorité de la veille » : direction majoritaire des labels à h barres du jour UTC précédent (connue à minuit : causale). |
| `past_ret_60` | Rendement des 60 minutes précédant t (log). |

### <run>_metrics.csv

| colonne | signification |
|---|---|
| `model` | Modèle : `logit` et `hgb` (gradient boosting) combinent tous les indicateurs en walk-forward purgé ; `reversal_h`, `momentum_h`, `always_up`, `majority_prev_day` sont les baselines naïves évaluées sur exactement les mêmes barres ; `timesfm` = TimesFM zero-shot, `timesfm_cal` = sa probabilité recalibrée (isotonique). |
| `sample` | Échantillon : `toutes` les origines, ou `2e moitié` (recalibration apprise sur la 1re). |
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `h_eff` | Chevauchement des cibles en nombre d'origines : ceil(h / stride). |
| `n` | Nombre de barres où l'indicateur et le rendement futur sont tous deux définis. |
| `n_eff` | Taille effective : n divisé par le chevauchement des cibles (ceil(h / pas entre origines)). |
| `accuracy` | Part des directions correctement prévues (hausse prévue si P(hausse) > 0,5). |
| `balanced_accuracy` | Moyenne des taux de réussite sur les hausses et sur les baisses (insensible au taux de base). |
| `auc` | AUC de la probabilité de hausse (0,5 = aucun pouvoir de classement). |
| `brier` | Score de Brier : moyenne de (P(hausse) − y)². 0,25 = toujours 0,5 ; plus bas = mieux. |
| `log_loss` | Log-loss (entropie croisée). ln 2 ≈ 0,693 = toujours 0,5 ; plus bas = mieux. |
| `p_binom` | p-valeur binomiale unilatérale (accuracy > 0,5) sur n_eff essais. |
| `pt_stat` | Statistique de Pesaran-Timmermann (1992) : la direction prévue est-elle indépendante de la direction réalisée ? > 1,64 ≈ significatif à 5 % (unilatéral, sans correction). |
| `pt_pvalue` | p-valeur unilatérale de `pt_stat`. |
| `base_rate` | Part de hausses réalisées (taux de base). |
| `excess_accuracy` | accuracy − max(taux de base, 1 − taux de base) : gain sur la meilleure prévision constante. |
| `pt_hac_t` | PT robuste à l'autocorrélation : t Newey-West (retards 2 × h_eff) de la régression y = a + b × prévision. Viser t > 3. |
| `pt_hac_pvalue` | p-valeur unilatérale de `pt_hac_t`. |
| `acc_w` | Accuracy pondérée par \|rendement\| : c'est elle qui compte pour gagner de l'argent. |
| `dm_brier_vs_reversal_t` | Diebold-Mariano sur le Brier contre `reversal_h` (t Newey-West) : t < −2 = le modèle fait mieux que le retournement. |

### <run>_diagnostics.csv

| colonne | signification |
|---|---|
| `horizon` | Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]). |
| `n` | Nombre de barres où l'indicateur et le rendement futur sont tous deux définis. |
| `coverage_q10_q90` | Part des rendements réalisés dans l'intervalle prévu [q10, q90] (cible 0,80). |
| `rho_past_60` | Corrélation de Spearman entre P(hausse) et le rendement des 60 dernières minutes (fortement négative = TimesFM agit comme un signal de retour à la moyenne). |
| `ic_p_up` | IC de Spearman entre P(hausse) et le rendement réalisé. |
| `ic_partial` | Même IC après neutralisation (sur les rangs) du rendement des 60 dernières minutes et des h dernières barres : ce que TimesFM apporte au-delà du retournement. |
| `median_abs_pred_bps` | \|médiane prévue − dernier prix\| médian, en points de base. |
| `median_abs_ret_bps` | \|rendement réalisé\| médian, en points de base. |
| `frac_pred_gt_cost` | Part des origines où \|médiane prévue\| dépasse le coût aller-retour. |

### <run>_strategy.csv

| colonne | signification |
|---|---|
| `signal` | Probabilité utilisée pour trader (`timesfm`, `timesfm_cal`, ou `reversal_h` en référence). |
| `h` | Horizon (durée de détention en barres). |
| `threshold` | Seuil : long si P(hausse) ≥ seuil, short si ≤ 1 − seuil. |
| `cost_bps` | Coût aller-retour déduit de chaque trade (points de base ; 10 = futures taker). |
| `n_signals` | Origines où le seuil est franchi. |
| `n_trades` | Trades effectivement ouverts (non chevauchants : un seul à la fois). |
| `n_long` | Trades acheteurs. |
| `n_short` | Trades vendeurs. |
| `hit_rate` | Part des trades gagnants avant frais. |
| `gross_bps_mean` | Gain brut moyen par trade (pb). |
| `net_bps_mean` | Gain net moyen par trade, frais déduits (pb). |
| `net_bps_se` | Erreur type de `net_bps_mean` (pb) : IC à 95 % ≈ ± 2 × se. |
| `gross_bps_sum` | Gain brut cumulé (pb). |
| `net_bps_sum` | Gain net cumulé (pb). |

## 5. Méthode et limites

* Origines sur une grille alignée sur l'horloge UTC tous les `stride` minutes, dans la partie **test** (après `train_frac`) ; contexte = les `context_len` dernières clôtures jusqu'à t inclus, centré en float64 ; un seul appel à horizon 15, lu aux pas 5, 10, 15.
* Covariables `top` : choisies par |ic_train_mean| de `reports/etat_des_lieux/aggregate.csv` (apprentissage seulement) ; les origines commencent après la fin de l'apprentissage de cette étude.
* `n_eff = n / ceil(h / stride)` ; PT HAC avec 2 × h_eff retards ; Diebold-Mariano sur le Brier contre le retournement.
* La P(hausse) brute est trop confiante : la stratégie au seuil 0,55 n'a de sens qu'avec `timesfm_cal` (recalibrée sur une période antérieure).
* Frais seuls (pas de spread ni de glissement) : le coût réel est plus élevé.
