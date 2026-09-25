# Diagnostic empirique — marchés Polymarket crypto « Up or Down » (5 min, 15 min)

*Généré le 25/09/2026 17:19 UTC par `scripts/polymarket_diagnostic.py` (temps d'exécution total : 307 s). Période : du 18/09/2026 00:00 au 25/09/2026 00:00 UTC (7 jours complets) ; actifs : BTC, ETH, SOL ; durées : 5m, 15m.*

> Lecture seule de données publiques. Aucun ordre, aucune clé. Depuis la France, Polymarket est en « close-only » et ces marchés sont `restricted` : ce diagnostic sert uniquement à évaluer nos prévisions contre un marché (simulation papier).

## 0. Résumé

* **8 063 marchés résolus** analysés (8 064 créneaux attendus). Taux de « Up » global : **51,2 %** (IC 95 % 50,1 % – 52,2 %). Égalités Chainlink (`finalPrice == priceToBeat`) : **0**. IC robuste (créneaux de 15 min, actifs corrélés) : 49,0 % – 53,3 % : pas de biais haussier démontré.
* Contrôle de la règle : l'issue officielle est égale à `finalPrice >= priceToBeat` dans 8 057 / 8 057 cas, et le `finalPrice` d'une fenêtre est égal au `priceToBeat` de la suivante dans 8 050 / 8 050 cas. L'issue compare donc **TWAP60(fin) à TWAP60(début)**, où TWAP60(t) est la moyenne sur (t−60 s, t] : on compare la minute qui PRÉCÈDE l'ouverture, et non la première minute de la fenêtre.
* **Meilleur proxy Binance** : `b_twap60_1s` ((b*) TWAP60 exact : moyenne des closes 1s sur (E−60, E] ≥ moyenne sur (S−60, S]) : accord de **98,6 %** (IC 98,4 % – 98,9 %, n = 8 063). Avec des bougies 1m seulement : `b_vwap_last_vs_prev_1m`, **96,5 %**. La comparaison close/open 1m (a) n'atteint que 89,5 %. Exploratoire : en décalant la fenêtre Binance de 4 s vers le passé, l'erreur sur la variation baisse (RMSE 0,38 pb contre 0,68 pb) et l'accord passe à 99,0 %. Le flux Chainlink semble donc en retard d'environ 4 s sur Binance (décalage choisi sur ces données).
* **Avant l'ouverture (S−30 s), le marché a un pouvoir prédictif faible mais mesurable** : prix moyen du jeton Up 0,499 (écart-type 0,017), 98,0 % des prix dans [0,45 ; 0,55], 22,4 % exactement à 0,505 ; Brier 0,2496 (pièce : 0,25) ; AUC 0,525 (IC 0,513 – 0,538).
* **Une fois la fenêtre ouverte, le prix converge vite** (justesse du signe p ≥ 0,5) : 5m S+1 min : 58,7 % (Brier 0,239) ; 5m S+2 min : 68,3 % (Brier 0,201) ; 5m S+5 min : 92,3 % (Brier 0,054) ; 15m S+1 min : 54,5 % (Brier 0,246) ; 15m S+2 min : 58,8 % (Brier 0,235) ; 15m S+5 min : 68,9 % (Brier 0,199) ; 15m S+10 min : 81,1 % (Brier 0,132).
* **Nowcast Binance contre prix du marché au même horodatage** : le nowcast sans paramètre fait mieux que le point `prices-history` (S+1 min, toutes cellules : justesse 60,4 % contre 57,6 %, ΔBrier -0,0063, IC -0,0088 ; -0,0038). Sur le 5m à S+2 min, la corrélation avec le marché est maximale quand le nowcast est calculé 10 s avant l'horodatage du point (0,957 contre 0,913 à 0 s). À ce décalage, ΔBrier vaut 0,0007 (IC -0,0010 ; 0,0024). L'avance du nowcast équivaut donc à environ 10 s d'information. Deux explications sont possibles, et on ne peut pas les départager ici : (i) le point `prices-history` reflète un état du carnet plus ancien que son horodatage ; (ii) le milieu de fourchette réagit avec ce retard sur Binance. Seul un enregistrement du carnet en direct (WebSocket) permettrait de trancher. Même dans le cas (ii), il faudrait payer l'ask réel, et le § 6 bis montre que le côté informé se paie vite.
* **Meilleure baseline pré-ouverture « propre »** (information ≤ S−30 s, choisie in-sample) : `gap_m30:momentum`, justesse 52,2 % (IC robuste 50,6 % – 53,7 %, n = 8 063).
* À titre de comparaison, l'écart spot − TWAP60 **à S** (`gap_m0_1s`, information jusqu'à S : non exécutable au prix de S−30 s) atteint 56,4 % de justesse (IC robuste 54,8 % – 57,9 %) : c'est l'information « mécanique » créée par la règle TWAP. Sur le sous-échantillon de transactions, les preneurs ont payé en moyenne **0,565** pour ce côté entre S et S+5 s (contre 0,507 supposé à S−30 s), pour un taux de gain de 55,6 %. Le P&L au prix payé est de -2,59 c par part (IC -7,97 – 2,80 c, n = 369) : il n'est **pas significativement différent de zéro**. Une bonne partie de l'avantage est payée dès l'ouverture (§ 6 bis).
* **P&L théorique** (achat preneur à S−30 s au prix milieu + 0,005, frais `crypto_fees_v2` inclus) de `gap_m30:momentum` : **-0,17 c par part** (IC robuste -1,71 – 1,38 c), pour un seuil de rentabilité de 52,3 % de réussite contre 52,2 % obtenu. ROI -0,32 %. L'IC contient zéro : **non significatif**. Résultat **in-sample** : la stratégie a été choisie sur ces mêmes données.
* **Hypothèse d'exécution** : dans [S−30 s, S), les preneurs du côté choisi par `gap_m30:momentum` ont réellement payé 0,524 en moyenne, contre 0,507 supposé (milieu + 0,005). L'hypothèse est donc optimiste d'environ 1,7 c par part. Au prix payé, le P&L est de -1,28 c par part (IC -6,20 – 3,65 c, n = 420).
* Contrôle pseudo hors échantillon (choix sur la 1re moitié de la période, test sur la 2e) : `mom15_m60:retournement`, -1,71 c par part (IC -4,14 – 0,72 c, n = 4 032).

## 1. Données et méthode

* **Marchés** : `PolymarketClient.list_updown_markets` (slugs déterministes `{asset}-updown-{5m|15m}-{début}`), issue = `outcomePrices` (`resolved_up`). Niveaux Chainlink : `event.eventMetadata.priceToBeat` / `finalPrice` (lus directement sur `gamma-api /events`, car `UpDownMarket` ne les conserve pas).
* **Prix du jeton Up** : `PolymarketClient.prices_history(token_up, S−900 s, E+60 s, fidelity=1)`. C'est le **milieu de fourchette**, échantillonné environ une fois par minute à des secondes irrégulières. Prix « avant l'ouverture » = dernier point ≤ S−30 s (ancienneté ≤ 5 min) ; prix à S+k min = dernier point ≤ S+k min (ancienneté ≤ 90 s). Contrôle : ces prix sont identiques à ceux de `pm.up_price_at` (0 écart).
* **Binance** (spot USDT) : zips journaliers `data.binance.vision` en **1m et 1s** (repli automatique sur `data-api.binance.vision/api/v3/klines`). Le 1s est utilisé **sur tout l'échantillon**, pas seulement sur un sous-échantillon. Secondes manquantes comblées : BTC 0, ETH 0, SOL 0.
* **Causalité des baselines** : trois coupures d'information. `S−60s (1m)` = bougies 1m closes au plus tard à S−60 s ; `S−30s (1s)` = bougies 1s closes au plus tard à S−30 s ; `S (…) *` = bougies closes au plus tard à S. Seules les deux premières sont comparables au prix du marché pris à S−30 s, et elles seules servent au P&L. Dans les 30 s qui précèdent l'ouverture, les preneurs font déjà bouger le prix (§ 6 bis).
* **Intervalles** : Wilson à 95 % par cellule. « IC robuste » = IC d'une moyenne en groupant par créneau de 15 min, car BTC, ETH et SOL, ainsi que les marchés 5m et 15m d'un même créneau, sont corrélés. Les AUC ont un IC de Hanley-McNeil.

### Contrôles de cohérence

| contrôle | résultat |
|---|---|
| créneaux attendus / marchés trouvés / résolus | 8 064 / 8 063 / 8 063 |
| issue officielle == (finalPrice >= priceToBeat) | 8 057 / 8 057 |
| finalPrice(n) == priceToBeat(n+1) (fenêtres consécutives) | 8 050 / 8 050 |
| priceToBeat 5m == priceToBeat 15m au même début | 2 016 / 2 016 |
| règle de résolution (client) / fenêtre TWAP | twap / 60 s : 8 063 |
| barème de frais (fee_type, rate, exposant) | crypto_fees_v2 (0,07, 1) : 8 063 |
| marchés résolus sans `finalPrice` (exclus des comparaisons Chainlink) | 6 (btc-updown-15m-1790015400, btc-updown-5m-1790016600, eth-updown-15m-1790015400, eth-updown-5m-1790016600, sol-updown-15m-1790015400, sol-updown-5m-1790016600) |
| marchés sans historique de prix / erreurs | 0 / 0 |
| marchés avec prix à S−30 s | 8 063 |
| ancienneté médiane du point S−30 s (s) | 16 |
| `restricted` = true | 8 063 / 8 063 |

## 2. Taux de « Up » (base rate) et égalités

| cellule | résolus | up | taux_up | ic95_bas | ic95_haut | p_binom | égalités_chainlink | mouvement_médian_pb | volume_médian_usd | volume_total_usd |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC 5m | 2 016 | 1 018 | 50,5 % | 48,3 % | 52,7 % | 0,672 | 0 | 5,6 | 45 308 | 97 864 494 |
| ETH 5m | 2 015 | 1 044 | 51,8 % | 49,6 % | 54,0 % | 0,109 | 0 | 8,0 | 3 627 | 5 839 864 |
| SOL 5m | 2 016 | 1 012 | 50,2 % | 48,0 % | 52,4 % | 0,876 | 0 | 10,5 | 1 346 | 1 842 195 |
| BTC 15m | 672 | 347 | 51,6 % | 47,9 % | 55,4 % | 0,418 | 0 | 10,4 | 25 576 | 20 009 217 |
| ETH 15m | 672 | 352 | 52,4 % | 48,6 % | 56,1 % | 0,232 | 0 | 13,4 | 3 638 | 2 813 137 |
| SOL 15m | 672 | 352 | 52,4 % | 48,6 % | 56,1 % | 0,232 | 0 | 18,2 | 1 229 | 960 490 |
| Tous 5m | 6 047 | 3 074 | 50,8 % | 49,6 % | 52,1 % | 0,198 | 0 | 7,8 | 5 270 | 105 546 554 |
| Tous 15m | 2 016 | 1 051 | 52,1 % | 50,0 % | 54,3 % | 0,058 | 0 | 13,6 | 3 700 | 23 782 845 |
| Tous | 8 063 | 4 125 | 51,2 % | 50,1 % | 52,2 % | 0,038 | 0 | 8,8 | 4 698 | 129 329 398 |

*`mouvement_médian_pb` : |log(finalPrice / priceToBeat)| médian en points de base. Les égalités Chainlink sont comptées à l'égalité exacte des flottants ; elles iraient à « Up ».*

## 3. Proxies Binance de la résolution officielle

Pour chaque proxy, on prédit « Up » si niveau(fin) ≥ niveau(début), puis on mesure le taux d'accord avec l'issue officielle.

| proxy | définition | BTC 5m | ETH 5m | SOL 5m | BTC 15m | ETH 15m | SOL 15m | Tous 5m | Tous 15m | Tous |
|---|---|---|---|---|---|---|---|---|---|---|
| a_close_vs_open_1m | (a) close de la bougie 1m finissant à la fin ≥ open de la bougie 1m commençant au début | 88,2 % | 87,2 % | 88,9 % | 94,8 % | 93,0 % | 93,6 % | 88,1 % | 93,8 % | 89,5 % |
| a2_close_vs_close_1m | (a') close 1m à la fin ≥ close 1m de la bougie précédant l'ouverture (= label r_h actuel) | 88,2 % | 87,2 % | 88,6 % | 94,8 % | 93,2 % | 93,6 % | 88,0 % | 93,8 % | 89,5 % |
| b_vwap_last_vs_first_1m | (b) VWAP 1m de la dernière minute ≥ VWAP 1m de la PREMIÈRE minute de la fenêtre | 86,0 % | 87,0 % | 88,3 % | 92,3 % | 92,0 % | 93,5 % | 87,1 % | 92,6 % | 88,5 % |
| b_ohlc4_last_vs_first_1m | (b) (O+H+L+C)/4 dernière minute ≥ idem première minute de la fenêtre | 87,5 % | 87,9 % | 89,7 % | 92,7 % | 92,7 % | 94,2 % | 88,4 % | 93,2 % | 89,6 % |
| b_vwap_last_vs_prev_1m | (b*) VWAP 1m de la dernière minute ≥ VWAP 1m de la minute PRÉCÉDANT l'ouverture | 95,9 % | 95,7 % | 96,6 % | 97,6 % | 97,8 % | 97,6 % | 96,0 % | 97,7 % | 96,5 % |
| b_ohlc4_last_vs_prev_1m | (b*) (O+H+L+C)/4 dernière minute ≥ idem minute précédant l'ouverture | 95,0 % | 95,2 % | 96,2 % | 97,6 % | 97,8 % | 98,4 % | 95,5 % | 97,9 % | 96,1 % |
| b_twap60_last_vs_first_1s | (b) moyenne des closes 1s sur (E−60, E] ≥ moyenne sur (S, S+60] (première minute) | 86,8 % | 87,3 % | 88,7 % | 92,3 % | 92,3 % | 93,6 % | 87,6 % | 92,7 % | 88,9 % |
| b_twap60_1s | (b*) TWAP60 exact : moyenne des closes 1s sur (E−60, E] ≥ moyenne sur (S−60, S] | 98,0 % | 98,6 % | 98,9 % | 98,5 % | 99,4 % | 99,4 % | 98,5 % | 99,1 % | 98,6 % |
| b_vwap60_1s | (b*) VWAP 60 s (1s) sur (E−60, E] ≥ VWAP 60 s sur (S−60, S] | 95,9 % | 95,7 % | 96,6 % | 97,6 % | 97,8 % | 97,6 % | 96,0 % | 97,7 % | 96,5 % |
| b_twap30_1s | (contrôle) TWAP 30 s (1s) fin ≥ début — pour vérifier la fenêtre de 60 s | 93,8 % | 93,6 % | 94,2 % | 96,9 % | 97,2 % | 96,4 % | 93,9 % | 96,8 % | 94,6 % |
| x_twap60_1s_lag4 | (exploratoire) TWAP60 1s décalé de 4 s : moyenne sur (t−64, t−4] — décalage choisi sur ces données | 98,2 % | 99,3 % | 99,4 % | 98,5 % | 99,3 % | 99,3 % | 99,0 % | 99,0 % | 99,0 % |
| c_window_twap_vs_open_1m | (c) moyenne des closes 1m de toute la fenêtre ≥ open 1m au début | 85,7 % | 85,9 % | 86,7 % | 83,6 % | 85,4 % | 84,2 % | 86,1 % | 84,4 % | 85,7 % |
| c_window_twap_vs_open_1s | (c) moyenne des closes 1s de toute la fenêtre ≥ open 1s au début | 84,2 % | 84,8 % | 85,3 % | 83,3 % | 85,3 % | 84,1 % | 84,8 % | 84,2 % | 84,6 % |

Désaccords (toutes cellules) : taille du mouvement Chainlink dans les cas de désaccord.

| proxy | n | désaccords | égalités_binance | mouvement_cl_médian_désaccords_pb | mouvement_cl_médian_tous_pb |
|---|---|---|---|---|---|
| a_close_vs_open_1m | 8 063 | 843 | 53 | 2,27 | 8,79 |
| a2_close_vs_close_1m | 8 063 | 849 | 66 | 2,29 | 8,79 |
| b_vwap_last_vs_first_1m | 8 063 | 930 | 0 | 2,41 | 8,79 |
| b_ohlc4_last_vs_first_1m | 8 063 | 840 | 9 | 2,12 | 8,79 |
| b_vwap_last_vs_prev_1m | 8 063 | 286 | 0 | 0,70 | 8,79 |
| b_ohlc4_last_vs_prev_1m | 8 063 | 316 | 10 | 0,81 | 8,79 |
| b_twap60_last_vs_first_1s | 8 063 | 897 | 0 | 2,26 | 8,79 |
| b_twap60_1s | 8 063 | 109 | 0 | 0,24 | 8,79 |
| b_vwap60_1s | 8 063 | 286 | 0 | 0,70 | 8,79 |
| b_twap30_1s | 8 063 | 433 | 1 | 1,36 | 8,79 |
| x_twap60_1s_lag4 | 8 063 | 82 | 0 | 0,16 | 8,79 |
| c_window_twap_vs_open_1m | 8 063 | 1 155 | 14 | 2,74 | 8,79 |
| c_window_twap_vs_open_1s | 8 063 | 1 239 | 1 | 2,91 | 8,79 |

### Quel niveau Binance colle aux niveaux Chainlink ?

Écart de niveau log(Binance / Chainlink), en pb, sur les points S (priceToBeat) et E (finalPrice). `rmse_variation_pb` mesure l'erreur sur la variation fin − début, qui seule compte pour l'issue.

| cellule | niveau | n | écart_moyen_pb | écart_sd_pb | rmse_variation_pb | corr_variation | accord_signe |
|---|---|---|---|---|---|---|---|
| BTC 5m | spot_1s | 2 015 | 3,12 | 3,83 | 4,33 | 0,9329 | 88,2 % |
| BTC 5m | twap60_1s | 2 015 | 3,05 | 2,17 | 0,53 | 0,9989 | 98,0 % |
| BTC 5m | twap60_open_1s | 2 015 | 3,05 | 2,15 | 0,44 | 0,9993 | 98,1 % |
| BTC 5m | vwap60_1s | 2 015 | 3,05 | 2,36 | 1,43 | 0,9926 | 95,9 % |
| BTC 5m | twap30_1s | 2 015 | 3,09 | 2,76 | 2,35 | 0,9796 | 93,8 % |
| BTC 5m | vwap_1m | 2 015 | 3,05 | 2,36 | 1,43 | 0,9926 | 95,9 % |
| BTC 5m | ohlc4_1m | 2 015 | 3,05 | 2,41 | 1,51 | 0,9913 | 95,0 % |
| BTC 5m | close_1m | 2 015 | 3,12 | 3,83 | 4,33 | 0,9329 | 88,2 % |
| ETH 5m | spot_1s | 2 014 | 3,18 | 4,81 | 5,98 | 0,9252 | 87,2 % |
| ETH 5m | twap60_1s | 2 014 | 3,13 | 2,14 | 0,70 | 0,9989 | 98,6 % |
| ETH 5m | twap60_open_1s | 2 014 | 3,13 | 2,12 | 0,57 | 0,9993 | 99,1 % |
| ETH 5m | vwap60_1s | 2 014 | 3,10 | 2,78 | 2,54 | 0,9869 | 95,7 % |
| ETH 5m | twap30_1s | 2 014 | 3,17 | 3,15 | 3,30 | 0,9765 | 93,6 % |
| ETH 5m | vwap_1m | 2 014 | 3,10 | 2,78 | 2,54 | 0,9869 | 95,7 % |
| ETH 5m | ohlc4_1m | 2 014 | 3,08 | 2,75 | 2,39 | 0,9876 | 95,2 % |
| ETH 5m | close_1m | 2 014 | 3,18 | 4,81 | 5,98 | 0,9252 | 87,2 % |
| SOL 5m | spot_1s | 2 015 | 3,12 | 5,60 | 7,18 | 0,9318 | 88,6 % |
| SOL 5m | twap60_1s | 2 015 | 3,06 | 2,13 | 0,81 | 0,9991 | 98,9 % |
| SOL 5m | twap60_open_1s | 2 015 | 3,05 | 2,10 | 0,66 | 0,9994 | 99,1 % |
| SOL 5m | vwap60_1s | 2 015 | 2,97 | 2,75 | 2,52 | 0,9916 | 96,6 % |
| SOL 5m | twap30_1s | 2 015 | 3,08 | 3,54 | 4,03 | 0,9777 | 94,2 % |
| SOL 5m | vwap_1m | 2 015 | 2,97 | 2,75 | 2,52 | 0,9916 | 96,6 % |
| SOL 5m | ohlc4_1m | 2 015 | 2,99 | 2,82 | 2,59 | 0,9905 | 96,2 % |
| SOL 5m | close_1m | 2 015 | 3,12 | 5,60 | 7,18 | 0,9318 | 88,6 % |
| Tous | spot_1s | 8 057 | 3,20 | 4,71 | 5,80 | 0,9554 | 89,5 % |
| Tous | twap60_1s | 8 057 | 3,08 | 2,14 | 0,69 | 0,9993 | 98,7 % |
| Tous | twap60_open_1s | 8 057 | 3,08 | 2,12 | 0,57 | 0,9996 | 98,8 % |
| Tous | vwap60_1s | 8 057 | 3,05 | 2,59 | 2,13 | 0,9941 | 96,5 % |
| Tous | twap30_1s | 8 057 | 3,13 | 3,12 | 3,24 | 0,9859 | 94,6 % |
| Tous | vwap_1m | 8 057 | 3,05 | 2,59 | 2,13 | 0,9941 | 96,5 % |
| Tous | ohlc4_1m | 8 057 | 3,06 | 2,64 | 2,17 | 0,9936 | 96,1 % |
| Tous | close_1m | 8 057 | 3,20 | 4,71 | 5,80 | 0,9554 | 89,5 % |

Décalage temporel : on déplace la fenêtre TWAP60 Binance de −10 à +10 s. Le meilleur décalage par actif (RMSE de la variation minimale) est le suivant (détail dans `level_lag_scan.csv`) :

| actif | décalage_s | rmse_variation_pb | accord_issue | rmse_décalage_0 | accord_décalage_0 |
|---|---|---|---|---|---|
| BTC | -4 | 0,36 | 98,3 % | 0,54 | 98,1 % |
| ETH | -4 | 0,38 | 99,3 % | 0,70 | 98,8 % |
| SOL | -4 | 0,41 | 99,4 % | 0,81 | 99,0 % |

## 4. Le prix du marché : avant l'ouverture et pendant la fenêtre

| cellule | instant | n | prix_moyen | prix_sd | part_0.45_0.55 | écart_moyen_à_0.5 | part_décidé_0.1_0.9 | accuracy | brier | auc | auc_ic_bas | auc_ic_haut |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC 5m | S−30 s (avant ouverture) | 2 016 | 0,499 | 0,021 | 96,8 % | 0,014 | 0,0 % | 50,8 % | 0,2494 | 0,525 | 0,499 | 0,550 |
| BTC 5m | S (dernier point ≤ S) | 2 016 | 0,499 | 0,021 | 96,8 % | 0,014 | 0,0 % | 51,0 % | 0,2494 | 0,527 | 0,502 | 0,552 |
| BTC 5m | S+1 min | 2 015 | 0,499 | 0,116 | 33,4 % | 0,092 | 0,0 % | 59,3 % | 0,2383 | 0,625 | 0,601 | 0,650 |
| BTC 5m | S+2 min | 2 016 | 0,500 | 0,215 | 16,8 % | 0,181 | 2,7 % | 67,9 % | 0,2050 | 0,744 | 0,723 | 0,765 |
| BTC 5m | S+5 min | 2 016 | 0,504 | 0,440 | 2,8 % | 0,421 | 76,4 % | 92,6 % | 0,0535 | 0,981 | 0,975 | 0,987 |
| ETH 5m | S−30 s (avant ouverture) | 2 015 | 0,498 | 0,016 | 99,0 % | 0,011 | 0,0 % | 50,6 % | 0,2498 | 0,523 | 0,498 | 0,548 |
| ETH 5m | S (dernier point ≤ S) | 2 015 | 0,498 | 0,016 | 99,0 % | 0,011 | 0,0 % | 50,6 % | 0,2497 | 0,523 | 0,498 | 0,549 |
| ETH 5m | S+1 min | 2 014 | 0,499 | 0,117 | 34,1 % | 0,093 | 0,0 % | 59,1 % | 0,2379 | 0,626 | 0,602 | 0,651 |
| ETH 5m | S+2 min | 2 015 | 0,499 | 0,224 | 15,4 % | 0,188 | 4,9 % | 68,8 % | 0,1977 | 0,763 | 0,743 | 0,784 |
| ETH 5m | S+5 min | 2 015 | 0,510 | 0,443 | 3,3 % | 0,424 | 76,8 % | 92,9 % | 0,0506 | 0,982 | 0,976 | 0,988 |
| SOL 5m | S−30 s (avant ouverture) | 2 016 | 0,500 | 0,012 | 99,3 % | 0,005 | 0,0 % | 49,8 % | 0,2501 | 0,509 | 0,484 | 0,534 |
| SOL 5m | S (dernier point ≤ S) | 2 016 | 0,500 | 0,012 | 99,3 % | 0,005 | 0,0 % | 49,8 % | 0,2501 | 0,510 | 0,484 | 0,535 |
| SOL 5m | S+1 min | 2 015 | 0,496 | 0,123 | 34,1 % | 0,097 | 0,0 % | 57,6 % | 0,2404 | 0,615 | 0,590 | 0,639 |
| SOL 5m | S+2 min | 2 016 | 0,501 | 0,225 | 15,5 % | 0,190 | 4,7 % | 68,3 % | 0,2007 | 0,755 | 0,734 | 0,776 |
| SOL 5m | S+5 min | 2 016 | 0,501 | 0,441 | 3,4 % | 0,422 | 77,2 % | 91,6 % | 0,0569 | 0,978 | 0,971 | 0,984 |
| BTC 15m | S−30 s (avant ouverture) | 672 | 0,495 | 0,024 | 93,5 % | 0,018 | 0,0 % | 50,4 % | 0,2498 | 0,522 | 0,478 | 0,565 |
| BTC 15m | S (dernier point ≤ S) | 672 | 0,495 | 0,024 | 93,5 % | 0,018 | 0,0 % | 50,4 % | 0,2498 | 0,522 | 0,478 | 0,565 |
| BTC 15m | S+1 min | 672 | 0,505 | 0,062 | 62,1 % | 0,048 | 0,0 % | 55,2 % | 0,2464 | 0,566 | 0,523 | 0,609 |
| BTC 15m | S+2 min | 672 | 0,509 | 0,125 | 31,4 % | 0,102 | 0,0 % | 60,6 % | 0,2331 | 0,645 | 0,603 | 0,686 |
| BTC 15m | S+5 min | 672 | 0,510 | 0,212 | 13,7 % | 0,180 | 2,7 % | 68,6 % | 0,2010 | 0,755 | 0,718 | 0,791 |
| BTC 15m | S+10 min | 672 | 0,520 | 0,323 | 6,7 % | 0,290 | 28,9 % | 79,9 % | 0,1362 | 0,889 | 0,864 | 0,914 |
| ETH 15m | S−30 s (avant ouverture) | 672 | 0,498 | 0,021 | 97,0 % | 0,016 | 0,0 % | 54,6 % | 0,2485 | 0,557 | 0,514 | 0,600 |
| ETH 15m | S (dernier point ≤ S) | 672 | 0,498 | 0,021 | 97,0 % | 0,016 | 0,0 % | 54,6 % | 0,2485 | 0,557 | 0,514 | 0,601 |
| ETH 15m | S+1 min | 672 | 0,506 | 0,068 | 58,6 % | 0,053 | 0,0 % | 56,1 % | 0,2438 | 0,588 | 0,545 | 0,631 |
| ETH 15m | S+2 min | 672 | 0,515 | 0,133 | 30,1 % | 0,107 | 0,3 % | 58,9 % | 0,2337 | 0,632 | 0,590 | 0,674 |
| ETH 15m | S+5 min | 672 | 0,515 | 0,233 | 14,0 % | 0,198 | 5,1 % | 68,9 % | 0,2005 | 0,756 | 0,719 | 0,792 |
| ETH 15m | S+10 min | 672 | 0,523 | 0,342 | 7,6 % | 0,308 | 39,0 % | 82,6 % | 0,1265 | 0,902 | 0,879 | 0,926 |
| SOL 15m | S−30 s (avant ouverture) | 672 | 0,500 | 0,011 | 100,0 % | 0,009 | 0,0 % | 54,6 % | 0,2491 | 0,560 | 0,517 | 0,603 |
| SOL 15m | S (dernier point ≤ S) | 672 | 0,500 | 0,011 | 100,0 % | 0,009 | 0,0 % | 54,6 % | 0,2491 | 0,560 | 0,516 | 0,603 |
| SOL 15m | S+1 min | 672 | 0,503 | 0,069 | 59,7 % | 0,052 | 0,0 % | 52,2 % | 0,2484 | 0,537 | 0,493 | 0,580 |
| SOL 15m | S+2 min | 672 | 0,514 | 0,130 | 29,0 % | 0,107 | 0,0 % | 56,8 % | 0,2383 | 0,615 | 0,573 | 0,657 |
| SOL 15m | S+5 min | 672 | 0,519 | 0,233 | 14,1 % | 0,199 | 6,2 % | 69,2 % | 0,1953 | 0,768 | 0,733 | 0,804 |
| SOL 15m | S+10 min | 672 | 0,524 | 0,343 | 7,0 % | 0,310 | 38,1 % | 80,8 % | 0,1319 | 0,894 | 0,870 | 0,919 |
| Tous 5m | S−30 s (avant ouverture) | 6 047 | 0,499 | 0,016 | 98,4 % | 0,010 | 0,0 % | 50,4 % | 0,2498 | 0,518 | 0,504 | 0,533 |
| Tous 5m | S (dernier point ≤ S) | 6 047 | 0,499 | 0,016 | 98,4 % | 0,010 | 0,0 % | 50,5 % | 0,2497 | 0,519 | 0,505 | 0,534 |
| Tous 5m | S+1 min | 6 044 | 0,498 | 0,119 | 33,9 % | 0,094 | 0,0 % | 58,7 % | 0,2389 | 0,622 | 0,608 | 0,636 |
| Tous 5m | S+2 min | 6 047 | 0,500 | 0,221 | 15,9 % | 0,186 | 4,1 % | 68,3 % | 0,2011 | 0,754 | 0,742 | 0,766 |
| Tous 5m | S+5 min | 6 047 | 0,505 | 0,441 | 3,2 % | 0,422 | 76,8 % | 92,3 % | 0,0536 | 0,980 | 0,977 | 0,984 |
| Tous 15m | S−30 s (avant ouverture) | 2 016 | 0,498 | 0,020 | 96,8 % | 0,014 | 0,0 % | 53,2 % | 0,2491 | 0,544 | 0,519 | 0,569 |
| Tous 15m | S (dernier point ≤ S) | 2 016 | 0,498 | 0,020 | 96,8 % | 0,014 | 0,0 % | 53,2 % | 0,2491 | 0,544 | 0,519 | 0,569 |
| Tous 15m | S+1 min | 2 016 | 0,504 | 0,066 | 60,1 % | 0,051 | 0,0 % | 54,5 % | 0,2462 | 0,563 | 0,538 | 0,588 |
| Tous 15m | S+2 min | 2 016 | 0,513 | 0,130 | 30,2 % | 0,105 | 0,1 % | 58,8 % | 0,2350 | 0,630 | 0,606 | 0,654 |
| Tous 15m | S+5 min | 2 016 | 0,515 | 0,226 | 13,9 % | 0,192 | 4,7 % | 68,9 % | 0,1989 | 0,759 | 0,739 | 0,780 |
| Tous 15m | S+10 min | 2 016 | 0,523 | 0,336 | 7,1 % | 0,303 | 35,3 % | 81,1 % | 0,1316 | 0,895 | 0,881 | 0,909 |
| Tous | S−30 s (avant ouverture) | 8 063 | 0,499 | 0,017 | 98,0 % | 0,011 | 0,0 % | 51,1 % | 0,2496 | 0,525 | 0,513 | 0,538 |
| Tous | S (dernier point ≤ S) | 8 063 | 0,499 | 0,017 | 98,0 % | 0,011 | 0,0 % | 51,1 % | 0,2496 | 0,526 | 0,514 | 0,539 |
| Tous | S+1 min | 8 060 | 0,500 | 0,108 | 40,4 % | 0,083 | 0,0 % | 57,6 % | 0,2407 | 0,610 | 0,598 | 0,623 |
| Tous | S+2 min | 8 063 | 0,503 | 0,202 | 19,5 % | 0,166 | 3,1 % | 65,9 % | 0,2096 | 0,729 | 0,718 | 0,739 |
| Tous | S+5 min | 8 063 | 0,507 | 0,399 | 5,9 % | 0,365 | 58,8 % | 86,5 % | 0,0900 | 0,951 | 0,946 | 0,955 |
| Tous | S+10 min | 2 016 | 0,523 | 0,336 | 7,1 % | 0,303 | 35,3 % | 81,1 % | 0,1316 | 0,895 | 0,881 | 0,909 |

*`part_décidé_0.1_0.9` : part des prix ≤ 0,1 ou ≥ 0,9. `S` = dernier point ≤ S (ancienneté ≤ 90 s). Pour le 5m, « S+5 min » tombe à la clôture de la fenêtre, et le prix est alors presque certain.*

### Calibration par déciles (toutes cellules d'une même durée)

Fréquence réalisée de « Up » par décile de prix (effectif entre parenthèses) :

| décile | 5m S+1 min | 5m S+2 min | 5m S+5 min | 15m S+1 min | 15m S+2 min | 15m S+5 min | 15m S+10 min |
|---|---|---|---|---|---|---|---|
| 0,0–0,1 | — | 0,10 (121) | 0,01 (2 284) | — | — | 0,06 (31) | 0,03 (309) |
| 0,1–0,2 | 0,15 (27) | 0,16 (479) | 0,16 (236) | — | 0,11 (9) | 0,18 (165) | 0,15 (208) |
| 0,2–0,3 | 0,35 (247) | 0,28 (734) | 0,25 (177) | — | 0,24 (98) | 0,25 (216) | 0,21 (157) |
| 0,3–0,4 | 0,38 (942) | 0,37 (804) | 0,39 (141) | 0,40 (98) | 0,39 (300) | 0,36 (279) | 0,42 (153) |
| 0,4–0,5 | 0,46 (1 824) | 0,44 (885) | 0,46 (122) | 0,48 (856) | 0,49 (509) | 0,47 (291) | 0,38 (125) |
| 0,5–0,6 | 0,54 (1 793) | 0,54 (879) | 0,53 (194) | 0,54 (909) | 0,54 (556) | 0,55 (266) | 0,56 (138) |
| 0,6–0,7 | 0,66 (936) | 0,63 (790) | 0,71 (129) | 0,67 (144) | 0,61 (399) | 0,64 (247) | 0,68 (136) |
| 0,7–0,8 | 0,72 (242) | 0,75 (737) | 0,73 (177) | 0,75 (8) | 0,75 (122) | 0,76 (254) | 0,74 (173) |
| 0,8–0,9 | 0,76 (33) | 0,89 (491) | 0,88 (232) | 1,00 (1) | 1,00 (21) | 0,86 (208) | 0,83 (222) |
| 0,9–1,0 | — | 0,95 (127) | 0,99 (2 355) | — | 1,00 (2) | 0,92 (59) | 0,97 (395) |

### Le marché contre un nowcast Binance au même instant

Au moment précis de chaque point de prix (horodatage du point, donc même information), on calcule P(Up) = Φ((log m − log TWAP60_Binance(S)) / σ√·). Ici m est le prix spot Binance, corrigé de la partie déjà connue du TWAP final, et σ la volatilité 1m de l'heure précédente. Ce modèle utilise Binance seul, sans paramètre ajusté.

| cellule | instant | n | âge_moyen_point_s | acc_marché | acc_nowcast | brier_marché | brier_nowcast | ΔBrier (IC robuste) | auc_marché | auc_nowcast | corr_marché_nowcast |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Tous 5m | S+1 min | 6 044 | 45 | 58,7 % | 61,6 % | 0,2389 | 0,2314 | -0,0075 (-0,0104 ; -0,0046) | 0,622 | 0,659 | 0,784 |
| Tous 5m | S+2 min | 6 047 | 46 | 68,3 % | 69,8 % | 0,2011 | 0,1962 | -0,0049 (-0,0076 ; -0,0021) | 0,754 | 0,768 | 0,913 |
| Tous 5m | S+5 min | 6 047 | 45 | 92,3 % | 95,7 % | 0,0536 | 0,0345 | -0,0191 (-0,0230 ; -0,0152) | 0,980 | 0,992 | 0,949 |
| Tous 15m | S+1 min | 2 016 | 44 | 54,5 % | 56,7 % | 0,2462 | 0,2434 | -0,0028 (-0,0059 ; 0,0003) | 0,563 | 0,589 | 0,705 |
| Tous 15m | S+2 min | 2 016 | 46 | 58,8 % | 59,1 % | 0,2350 | 0,2348 | -0,0003 (-0,0037 ; 0,0032) | 0,630 | 0,634 | 0,896 |
| Tous 15m | S+5 min | 2 016 | 46 | 68,9 % | 69,6 % | 0,1989 | 0,1960 | -0,0030 (-0,0062 ; 0,0002) | 0,759 | 0,769 | 0,960 |
| Tous 15m | S+10 min | 2 016 | 45 | 81,1 % | 81,6 % | 0,1316 | 0,1287 | -0,0029 (-0,0064 ; 0,0006) | 0,895 | 0,900 | 0,974 |
| Tous | S+1 min | 8 060 | 45 | 57,6 % | 60,4 % | 0,2407 | 0,2344 | -0,0063 (-0,0088 ; -0,0038) | 0,610 | 0,644 | 0,777 |
| Tous | S+2 min | 8 063 | 46 | 65,9 % | 67,1 % | 0,2096 | 0,2059 | -0,0037 (-0,0062 ; -0,0012) | 0,729 | 0,740 | 0,911 |
| Tous | S+5 min | 8 063 | 46 | 86,5 % | 89,2 % | 0,0900 | 0,0749 | -0,0151 (-0,0182 ; -0,0120) | 0,951 | 0,966 | 0,950 |
| Tous | S+10 min | 2 016 | 45 | 81,1 % | 81,6 % | 0,1316 | 0,1287 | -0,0029 (-0,0064 ; 0,0006) | 0,895 | 0,900 | 0,974 |

*ΔBrier = Brier(nowcast) − Brier(marché), apparié marché par marché ; négatif = le nowcast Binance fait mieux. `âge_moyen_point_s` : S+k min − horodatage du point. Le prix `prices-history` est un milieu de fourchette, pas un prix exécutable. Un nowcast meilleur que le marché au même instant ne prouve donc pas qu'un gain soit exploitable.*

**Robustesse : le point `prices-history` est-il en retard sur son horodatage ?** On recalcule le nowcast Δ secondes AVANT l'horodatage du point, sur les mêmes marchés. Si le nowcast décalé rejoint le marché (ΔBrier ≈ 0, corrélation maximale), le point reflète un état antérieur à son horodatage, et l'avance du nowcast vient de là, pas d'une inefficience.

ΔBrier (nowcast décalé − marché), IC robuste :

| Δ (s) | 5m S+2 min | 5m S+5 min | 15m S+2 min | 15m S+5 min | 15m S+10 min |
|---|---|---|---|---|---|
| 0 | -0,0049 (-0,0076 ; -0,0021) | -0,0191 (-0,0230 ; -0,0152) | -0,0003 (-0,0037 ; 0,0032) | -0,0030 (-0,0062 ; 0,0002) | -0,0029 (-0,0064 ; 0,0006) |
| 10 | 0,0007 (-0,0010 ; 0,0024) | -0,0031 (-0,0057 ; -0,0006) | 0,0002 (-0,0021 ; 0,0025) | 0,0014 (-0,0011 ; 0,0038) | -0,0007 (-0,0035 ; 0,0020) |
| 20 | 0,0086 (0,0056 ; 0,0116) | 0,0118 (0,0084 ; 0,0151) | 0,0024 (-0,0007 ; 0,0056) | 0,0035 (0,0001 ; 0,0069) | 0,0023 (-0,0010 ; 0,0056) |
| 30 | 0,0135 (0,0096 ; 0,0175) | 0,0246 (0,0204 ; 0,0287) | 0,0029 (-0,0015 ; 0,0072) | 0,0058 (0,0017 ; 0,0099) | 0,0051 (0,0010 ; 0,0092) |
| 45 | 0,0206 (0,0158 ; 0,0254) | 0,0402 (0,0352 ; 0,0452) | 0,0037 (-0,0017 ; 0,0091) | 0,0079 (0,0029 ; 0,0129) | 0,0078 (0,0026 ; 0,0130) |
| 60 | 0,0306 (0,0250 ; 0,0362) | 0,0541 (0,0485 ; 0,0597) | 0,0080 (0,0019 ; 0,0141) | 0,0099 (0,0044 ; 0,0155) | 0,0113 (0,0053 ; 0,0172) |

Corrélation marché / nowcast décalé :

| Δ (s) | 5m S+2 min | 5m S+5 min | 15m S+2 min | 15m S+5 min | 15m S+10 min |
|---|---|---|---|---|---|
| 0 | 0,913 | 0,949 | 0,896 | 0,960 | 0,974 |
| 10 | 0,957 | 0,976 | 0,946 | 0,976 | 0,981 |
| 20 | 0,909 | 0,954 | 0,900 | 0,962 | 0,974 |
| 30 | 0,838 | 0,925 | 0,827 | 0,943 | 0,965 |
| 45 | 0,725 | 0,884 | 0,694 | 0,919 | 0,948 |
| 60 | 0,603 | 0,844 | 0,573 | 0,890 | 0,933 |

## 5. Baselines Binance calculées AVANT l'ouverture

Momentum : « Up » si le rendement (ou l'écart) est ≥ 0. Retournement : l'inverse. En cas d'égalité, on prédit « Up ». Les stratégies marquées `*` utilisent l'information jusqu'à S, donc 30 s de plus que le prix du marché retenu.

| label | BTC 5m | ETH 5m | SOL 5m | BTC 15m | ETH 15m | SOL 15m | Tous 5m | Tous 15m | Tous | IC robuste (Tous) | AUC (Tous) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| always_up [—] | 50,5 % | 51,8 % | 50,2 % | 51,6 % | 52,4 % | 52,4 % | 50,8 % | 52,1 % | 51,2 % | 49,0 % – 53,3 % | — |
| always_down [—] | 49,5 % | 48,2 % | 49,8 % | 48,4 % | 47,6 % | 47,6 % | 49,2 % | 47,9 % | 48,8 % | 46,7 % – 51,0 % | — |
| market_pre [S−30s (marché)] | 50,8 % | 50,6 % | 49,8 % | 50,4 % | 54,6 % | 54,6 % | 50,4 % | 53,2 % | 51,1 % | 49,6 % – 52,6 % | 0,525 |
| mom5_m60:momentum [S−60s (1m)] | 50,9 % | 48,7 % | 50,0 % | 47,6 % | 48,7 % | 51,2 % | 49,9 % | 49,2 % | 49,7 % | 48,1 % – 51,3 % | 0,492 |
| mom5_m60:retournement [S−60s (1m)] | 49,2 % | 51,1 % | 49,7 % | 52,2 % | 51,2 % | 50,0 % | 50,0 % | 51,1 % | 50,3 % | 48,7 % – 51,9 % | 0,508 |
| mom15_m60:momentum [S−60s (1m)] | 48,9 % | 48,1 % | 47,9 % | 49,0 % | 47,0 % | 48,7 % | 48,3 % | 48,2 % | 48,3 % | 46,6 % – 50,0 % | 0,476 |
| mom15_m60:retournement [S−60s (1m)] | 51,1 % | 51,9 % | 51,8 % | 51,0 % | 53,0 % | 51,5 % | 51,6 % | 51,8 % | 51,7 % | 50,0 % – 53,4 % | 0,524 |
| mom5_m30:momentum [S−30s (1s)] | 51,2 % | 50,2 % | 51,9 % | 48,4 % | 47,8 % | 51,8 % | 51,1 % | 49,3 % | 50,7 % | 49,1 % – 52,2 % | 0,502 |
| mom5_m30:retournement [S−30s (1s)] | 48,8 % | 50,0 % | 48,7 % | 51,6 % | 52,2 % | 48,7 % | 49,2 % | 50,8 % | 49,6 % | 48,0 % – 51,2 % | 0,498 |
| mom15_m30:momentum [S−30s (1s)] | 49,4 % | 49,5 % | 49,7 % | 49,7 % | 49,3 % | 50,3 % | 49,5 % | 49,8 % | 49,6 % | 47,8 % – 51,3 % | 0,486 |
| mom15_m30:retournement [S−30s (1s)] | 50,6 % | 50,5 % | 50,1 % | 50,3 % | 50,7 % | 50,1 % | 50,4 % | 50,4 % | 50,4 % | 48,7 % – 52,1 % | 0,514 |
| gap_m30:momentum [S−30s (1s)] | 52,7 % | 53,9 % | 51,4 % | 50,1 % | 50,0 % | 51,3 % | 52,7 % | 50,5 % | 52,2 % | 50,6 % – 53,7 % | 0,528 |
| gap_m30:retournement [S−30s (1s)] | 47,3 % | 46,1 % | 48,5 % | 49,9 % | 50,0 % | 48,7 % | 47,3 % | 49,5 % | 47,8 % | 46,3 % – 49,4 % | 0,472 |
| mom5_m0:momentum [S (1m) *] | 53,4 % | 51,7 % | 52,9 % | 52,1 % | 50,9 % | 51,5 % | 52,6 % | 51,5 % | 52,4 % | 50,8 % – 53,9 % | 0,528 |
| mom5_m0:retournement [S (1m) *] | 46,7 % | 48,4 % | 47,4 % | 47,8 % | 49,0 % | 49,0 % | 47,5 % | 48,6 % | 47,8 % | 46,2 % – 49,3 % | 0,472 |
| mom15_m0:momentum [S (1m) *] | 51,1 % | 50,4 % | 51,2 % | 51,6 % | 49,7 % | 50,9 % | 50,9 % | 50,7 % | 50,9 % | 49,1 % – 52,6 % | 0,504 |
| mom15_m0:retournement [S (1m) *] | 48,9 % | 49,6 % | 48,7 % | 48,4 % | 50,4 % | 49,1 % | 49,1 % | 49,3 % | 49,1 % | 47,4 % – 50,9 % | 0,496 |
| gap_m0_1m:momentum [S (1m) *] | 58,0 % | 58,3 % | 56,9 % | 55,1 % | 54,2 % | 49,4 % | 57,7 % | 52,9 % | 56,5 % | 55,0 % – 58,1 % | 0,588 |
| gap_m0_1m:retournement [S (1m) *] | 42,0 % | 41,7 % | 43,1 % | 44,9 % | 45,8 % | 50,6 % | 42,3 % | 47,1 % | 43,5 % | 41,9 % – 45,0 % | 0,412 |
| gap_m0_1s:momentum [S (1s) *] | 58,4 % | 57,9 % | 56,5 % | 54,8 % | 54,0 % | 49,3 % | 57,6 % | 52,7 % | 56,4 % | 54,8 % – 57,9 % | 0,587 |
| gap_m0_1s:retournement [S (1s) *] | 41,6 % | 42,1 % | 43,4 % | 45,2 % | 46,0 % | 50,6 % | 42,4 % | 47,3 % | 43,6 % | 42,1 % – 45,2 % | 0,413 |

*n par cellule : voir `baselines.csv`. Plus de 21 règles sont testées sur 6 cellules : sans correction, quelques p < 0,05 sont attendus par hasard (tests multiples).*

## 6. P&L théorique de la stratégie naïve (in-sample)

Règle : à S−30 s, acheter 100 parts du côté prédit (Up ou Down) au prix ask ≈ milieu + 0.005 (milieu = dernier point `prices-history` ≤ S−30 s ; Down = 1 − milieu). Les frais preneur sont calculés avec `pm.taker_fee(ask, parts, feeSchedule du marché)`, soit `crypto_fees_v2` : 0,07 × p × (1 − p) par part. On garde la position jusqu'à la résolution. Seules les stratégies dont l'information s'arrête au plus tard à S−30 s sont évaluées. Le carnet réel avant l'ouverture (≈ 0,50/0,51) limite la taille à quelques centaines de parts, et la profondeur n'est pas modélisée.

| stratégie | n_trades | taux_gain | seuil_rentabilité | pnl_moyen_par_part | ic95_bas | ic95_haut | roi |
|---|---|---|---|---|---|---|---|
| gap_m30:momentum | 8 063 | 52,2 % | 52,3 % | -0,17 c | -1,71 c | 1,38 c | -0,32 % |
| always_up | 8 063 | 51,2 % | 52,1 % | -0,97 c | -3,09 c | 1,15 c | -1,86 % |
| mom15_m60:retournement | 8 063 | 51,7 % | 52,9 % | -1,21 c | -2,93 c | 0,52 c | -2,29 % |
| mom5_m30:momentum | 8 063 | 50,7 % | 52,0 % | -1,33 c | -2,91 c | 0,26 c | -2,55 % |
| mom15_m30:momentum | 8 063 | 49,6 % | 51,6 % | -2,06 c | -3,80 c | -0,31 c | -3,98 % |
| mom5_m60:retournement | 8 063 | 50,3 % | 52,5 % | -2,22 c | -3,81 c | -0,63 c | -4,23 % |
| mom5_m60:momentum | 8 063 | 49,7 % | 52,0 % | -2,29 c | -3,87 c | -0,70 c | -4,40 % |
| mom15_m30:retournement | 8 063 | 50,4 % | 52,9 % | -2,46 c | -4,21 c | -0,70 c | -4,64 % |
| mom5_m30:retournement | 8 063 | 49,6 % | 52,5 % | -2,93 c | -4,51 c | -1,34 c | -5,57 % |
| mom15_m60:momentum | 8 063 | 48,3 % | 51,6 % | -3,34 c | -5,06 c | -1,61 c | -6,46 % |
| always_down | 8 063 | 48,8 % | 52,4 % | -3,53 c | -5,65 c | -1,41 c | -6,74 % |
| gap_m30:retournement | 8 063 | 47,8 % | 52,2 % | -4,34 c | -5,88 c | -2,80 c | -8,32 % |

Détail par cellule pour la meilleure baseline (`gap_m30:momentum`) :

| échantillon | cellule | n_trades | taux_gain | seuil_rentabilité | pnl_moyen_par_part | ic95_bas | ic95_haut | pnl_total_usd_100_parts | roi |
|---|---|---|---|---|---|---|---|---|---|
| in-sample | BTC 5m | 2 016 | 52,7 % | 52,3 % | 0,46 c | -1,69 c | 2,60 c | 921 $ | 0,87 % |
| in-sample | BTC 15m | 672 | 50,1 % | 52,3 % | -2,18 c | -5,96 c | 1,60 c | -1 463 $ | -4,16 % |
| in-sample | ETH 5m | 2 015 | 53,9 % | 52,3 % | 1,60 c | -0,56 c | 3,77 c | 3 231 $ | 3,06 % |
| in-sample | ETH 15m | 672 | 50,0 % | 52,5 % | -2,47 c | -6,23 c | 1,30 c | -1 657 $ | -4,70 % |
| in-sample | SOL 5m | 2 016 | 51,4 % | 52,3 % | -0,87 c | -3,01 c | 1,27 c | -1 757 $ | -1,67 % |
| in-sample | SOL 15m | 672 | 51,3 % | 52,3 % | -0,93 c | -4,70 c | 2,84 c | -625 $ | -1,78 % |
| in-sample | Tous 5m | 6 047 | 52,7 % | 52,3 % | 0,40 c | -1,21 c | 2,00 c | 2 395 $ | 0,76 % |
| in-sample | Tous 15m | 2 016 | 50,5 % | 52,4 % | -1,86 c | -4,70 c | 0,99 c | -3 744 $ | -3,55 % |
| in-sample | Tous | 8 063 | 52,2 % | 52,3 % | -0,17 c | -1,71 c | 1,38 c | -1 349 $ | -0,32 % |
| 2e moitié (choix sur la 1re) | BTC 5m | 1 008 | 49,6 % | 53,0 % | -3,39 c | -6,45 c | -0,33 c | -3 418 $ | -6,40 % |
| 2e moitié (choix sur la 1re) | BTC 15m | 336 | 49,7 % | 53,0 % | -3,31 c | -8,65 c | 2,04 c | -1 111 $ | -6,24 % |
| 2e moitié (choix sur la 1re) | ETH 5m | 1 008 | 51,9 % | 52,9 % | -1,03 c | -4,10 c | 2,04 c | -1 033 $ | -1,94 % |
| 2e moitié (choix sur la 1re) | ETH 15m | 336 | 51,8 % | 53,0 % | -1,19 c | -6,51 c | 4,13 c | -399 $ | -2,24 % |
| 2e moitié (choix sur la 1re) | SOL 5m | 1 008 | 52,0 % | 52,6 % | -0,60 c | -3,63 c | 2,42 c | -607 $ | -1,15 % |
| 2e moitié (choix sur la 1re) | SOL 15m | 336 | 51,8 % | 52,8 % | -1,00 c | -6,33 c | 4,33 c | -337 $ | -1,90 % |
| 2e moitié (choix sur la 1re) | Tous 5m | 3 024 | 51,2 % | 52,8 % | -1,67 c | -4,01 c | 0,67 c | -5 059 $ | -3,17 % |
| 2e moitié (choix sur la 1re) | Tous 15m | 1 008 | 51,1 % | 52,9 % | -1,83 c | -6,03 c | 2,36 c | -1 847 $ | -3,46 % |
| 2e moitié (choix sur la 1re) | Tous | 4 032 | 51,1 % | 52,9 % | -1,71 c | -4,14 c | 0,72 c | -6 906 $ | -3,24 % |

## 6 bis. Contrôle d'exécution avec les transactions réelles (sous-échantillon)

Sous-échantillon aléatoire (graine fixe) de 100 marchés par cellule, soit 600 marchés. On lit toutes les transactions preneuses avec `PolymarketClient.trades`. Une transaction BUY X au prix p donne une exposition X payée p ; une transaction SELL X au prix p donne le côté opposé payé 1 − p. Le « prix payé » est le VWAP de ce coût, pour le côté choisi par la stratégie, dans la fenêtre d'exécution : `pre30` = [S−30 s, S), `open5` = [S, S+5 s]. Horodatage à la seconde (bloc). On compare au P&L calculé avec l'hypothèse ask ≈ milieu(S−30 s) + 0,005, sur les MÊMES marchés.

| stratégie | fenêtre_exécution | cellule | n_échantillon | n_avec_transaction | taux_gain | prix_payé_moyen | ask_supposé_moyen | pnl_par_part_prix_payé | ic95_bas | ic95_haut | pnl_par_part_ask_supposé |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gap_m30:momentum | pre30 | Tous | 600 | 420 | 52,9 % | 0,524 | 0,507 | -1,28 c | -6,20 c | 3,65 c | 0,44 c |
| gap_m30:momentum | pre30 | Tous 5m | 300 | 250 | 50,0 % | 0,528 | 0,506 | -4,51 c | -10,61 c | 1,60 c | -2,35 c |
| gap_m30:momentum | pre30 | Tous 15m | 300 | 170 | 57,1 % | 0,518 | 0,508 | 3,48 c | -4,16 c | 11,11 c | 4,53 c |
| gap_m0_1s:momentum | open5 | Tous | 600 | 369 | 55,6 % | 0,565 | 0,507 | -2,59 c | -7,97 c | 2,80 c | 3,15 c |
| gap_m0_1s:momentum | open5 | Tous 5m | 300 | 210 | 58,1 % | 0,588 | 0,507 | -2,39 c | -9,24 c | 4,46 c | 5,66 c |
| gap_m0_1s:momentum | open5 | Tous 15m | 300 | 159 | 52,2 % | 0,533 | 0,506 | -2,85 c | -11,01 c | 5,31 c | -0,16 c |
| gap_m30:momentum | open5 | Tous | 600 | 345 | 53,9 % | 0,528 | 0,506 | -0,60 c | -5,91 c | 4,71 c | 1,60 c |
| gap_m30:momentum | open5 | Tous 5m | 300 | 197 | 50,8 % | 0,539 | 0,507 | -4,83 c | -11,70 c | 2,03 c | -1,65 c |
| gap_m30:momentum | open5 | Tous 15m | 300 | 148 | 58,1 % | 0,513 | 0,504 | 5,04 c | -3,09 c | 13,16 c | 5,92 c |

*Le prix payé par d'autres preneurs est un proxy optimiste de notre propre exécution : il ne compte ni la latence, ni la file d'attente, ni l'impact de notre ordre. Si aucune transaction n'a eu lieu du côté voulu dans la fenêtre, le marché est exclu (voir `n_avec_transaction`).*

## 7. Conséquences pour nos backtests

1. **Étiquette** : utiliser `b_twap60_1s` (klines 1s), soit 98,6 % d'accord. Si l'on n'a que du 1m : `b_vwap_last_vs_prev_1m` (96,5 %). Il ne faut PAS utiliser close[t+h]/close[t], qui ne donne que 89,5 % d'accord : c'est le label `r_h` actuel de `targets.py`. Variante exploratoire : fenêtre décalée de 4 s, soit 99,0 %, à valider sur une autre période.
2. **Origine** : l'origine d'une prévision est le début de fenêtre S (multiple de 300 ou 900 s en UTC). La référence est TWAP60(S), une moyenne sur (S−60 s, S] : elle est fixée à S et en partie connue dès S−30 s.
3. **Comparaison au marché** : avant l'ouverture, le prix du marché est plat (0,499 en moyenne, entre 0,45 et 0,55 dans 98,0 % des cas). Il suffit en théorie de dépasser le seuil ask + frais (≈ 52,75 % pour acheter Up à 0,51). En revanche, pour le côté indiqué par l'écart spot − TWAP à S, le prix payé passe de 0,507 (ask supposé à S−30 s) à 0,565 dès [S, S+5 s] (§ 6 bis). Il faut toujours comparer le modèle au prix **au même instant d'information**, et jamais un modèle qui a vu la bougie [S−1m, S) à un prix de S−30 s.
4. **Taille d'échantillon** : l'IC à 95 % d'une justesse autour de 50 % vaut ±1,1 points sur les 8 063 marchés, ±1,3 points sur les 6 047 marchés 5m, et ±3,8 points pour la plus petite cellule (672). Les actifs étant corrélés, l'IC robuste est plus large. Pour démontrer un avantage de 2 à 3 points, il faut plusieurs semaines de données et une sélection de stratégie faite hors échantillon.

## 8. Limites et remarques sur le client `tradebot.polymarket`

* `UpDownMarket` ne conserve pas `event.eventMetadata` (`priceToBeat`, `finalPrice`), c'est-à-dire les niveaux Chainlink exacts qui décident de l'issue. Le script les relit avec `client._get` sur `/events`. Ce n'est pas bloquant, mais cela vaudrait un champ `price_to_beat` / `final_price` dans le client.
* `prices-history` renvoie un milieu de fourchette, environ un point par minute : ce n'est pas un prix exécutable. L'ask est approché par milieu + 0,005, ce qui correspond au carnet 0,50/0,51 observé avant l'ouverture.
* `resolution_rule` ne distingue pas le TWAP-30 du TWAP-60. Vérifié sur btc 5m : TWAP-30 du 07/08 au 13/08/2026 23:55 UTC (`btc-5m-twap-30`), TWAP-60 à partir du 14/08 00:00. La docstring du module annonce « TWAP 60 s depuis le ~7 août » pour toutes les durées, ce qui est inexact pour le 5m. `twap_lookback_s` est correct (30 ou 60) : c'est lui qu'il faut utiliser pour filtrer. Sur la période étudiée, tous les marchés sont en TWAP-60 (voir les contrôles).
* Créneaux absents : 1 (eth-updown-5m-1789944900). Contre-vérification directe sur gamma (`/events` et `/markets`) : 1 / 1 sont bien inconnus de Polymarket. Ce n'est donc pas un bug du client.
* Marchés résolus sans `finalPrice` dans `eventMetadata` : 6 (btc-updown-15m-1790015400, btc-updown-5m-1790016600, eth-updown-15m-1790015400, eth-updown-5m-1790016600, sol-updown-15m-1790015400, sol-updown-5m-1790016600). Ils sont exclus des comparaisons de niveaux Chainlink, mais gardés pour l'issue officielle.
* Les points `prices-history` sont en retard d'environ 10 s sur l'information Binance (voir la robustesse du nowcast). Pour comparer un modèle au marché à la seconde près, il faut le carnet (`order_book`, WebSocket) ou les transactions (`trades`), pas `prices_history`.
* Aucun bug bloquant trouvé : `list_updown_markets`, `prices_history`, `up_price_at`, `taker_fee` et `markets_to_frame` ont été utilisés tels quels, avec un contrôle croisé pour `up_price_at`.

## 9. Temps d'exécution

Caches disque **désactivés** (`--no-cache`) : exécution à froid, toutes les données ont été retéléchargées.

| étape | secondes |
|---|---|
| 1. liste des marchés (client.list_updown_markets) | 12,3 |
| 2. eventMetadata (priceToBeat / finalPrice) | 5,0 |
| 3. historique de prix du jeton Up (8063 marchés) | 204,4 |
| 4. klines Binance 1m + 1s (zips journaliers) | 11,3 |
| 5. variables et analyses | 4,2 |
| 5b. transactions preneuses autour de S (600 marchés) | 68,2 |
| 6. écriture des CSV et du rapport | 0,8 |
| total | 306,8 |

## 10. Fichiers

* `reports/polymarket/markets.csv` : une ligne par marché résolu : issue, niveaux Chainlink, prix du jeton Up, niveaux et variables Binance, proxies
* `reports/polymarket/base_rates.csv` : taux de Up, IC de Wilson, égalités, volumes par cellule
* `reports/polymarket/proxy_agreement.csv` : taux d'accord de chaque proxy Binance avec l'issue officielle
* `reports/polymarket/level_check.csv` : niveaux Binance contre niveaux Chainlink (écart, RMSE de la variation)
* `reports/polymarket/level_lag_scan.csv` : décalage temporel Binance/Chainlink de −10 à +10 s
* `reports/polymarket/market_price_power.csv` : pouvoir prédictif du prix du jeton Up (S−30 s, S, S+k min)
* `reports/polymarket/calibration.csv` : calibration par déciles du prix du jeton Up
* `reports/polymarket/nowcast_vs_market.csv` : marché contre nowcast Binance au même instant
* `reports/polymarket/nowcast_lag.csv` : robustesse : nowcast calculé 0 à 60 s avant l'horodatage du point de marché
* `reports/polymarket/baselines.csv` : justesse des baselines pré-ouverture (toutes cellules)
* `reports/polymarket/pnl.csv` : P&L théorique des baselines propres (in-sample) et détail de la meilleure (+ pseudo hors échantillon)
* `reports/polymarket/trades_windows.csv` : sous-échantillon : prix payés par les preneurs dans [S−30 s, S) et [S, S+5 s] par côté
* `reports/polymarket/execution_check.csv` : P&L au prix réellement payé par les preneurs contre l'hypothèse ask ≈ milieu + 0,005
* `reports/polymarket/runtime.csv` : temps d'exécution par étape

Reproduire : `. .venv/bin/activate && python scripts/polymarket_diagnostic.py --end 2026-09-25 --days 7` (les caches rendent la 2e exécution rapide).
