# Temps de réaction nécessaire pour prendre les prix périmés (Polymarket « Up or Down »)

*Généré le 26/09/2026 11:28 UTC par `scripts/latency_study.py` (42 s). Données : 26/09 10:29:52 – 26/09 11:27:52 UTC (chevauchement des deux collecteurs), 22 marchés (BTC 15m : 2, BTC 5m : 10, ETH 5m : 10), dont 21 résolus ; 0,86 h de marché.*

> **Cadre légal.** Recherche et simulation papier sur données publiques. Polymarket est bloqué en France (ANJ, 16/07/2026) et la France est en « close-only » sur le site et l'API ; les CGU interdisent de contourner le géoblocage. Rien ici n'est utilisable légalement pour trader depuis la France, et ce document ne décrit **aucun** moyen de contourner un blocage (VPN, serveur à l'étranger piloté depuis la France, prête-nom…).

## Résumé

* **Temps de réaction nécessaire (vu d'ici, détection du saut Binance → ordre au carnet)** : la moitié de l'avantage attendu des prix périmés est perdue à **ℓ½ ≈ 182 ms** ; le gain n'est **démontré** (borne basse de l'IC 95 % > 0) que jusqu'à ℓ = 100 ms ; le point mort (P&L = 0, courbe lissée) est à ℓ* ≈ 833 ms, IC 95 % 124 ms – 4 369 ms : pas encore identifié sur cet échantillon (212 opportunités « saut Binance », valorisation à +10 s). À l'issue officielle (plus bruitée, 212 opportunités des marchés résolus) : ℓ* > 5 000 ms (IC 767 ms – > 5 000 ms), gain démontré jusqu'à 25 ms.
* **En temps réel** (mouvement Binance → appariement, = ℓ + 26 ms d'écart de transport Binance/Polymarket vu d'ici) : moitié de l'avantage à ≈ 209 ms, gain démontré jusqu'à ≈ 126 ms. Tout ordre preneur attend **150 ms** avant appariement, et un preneur qui lit Binance (Tokyo) et envoie à Londres ne peut pas descendre sous ≈ 260–280 ms au total : ℓ½ est plus court que ce plancher, et le seul gain démontré exige d'être apparié avant la fin du délai preneur : **impossible en preneur**.
* **Le carnet suit Binance en quelques centaines de ms** : après un saut de P_formule ≥ 5 points (n = 174), le meilleur prix est retouché dans le bon sens en 197 ms (placebo sans saut : 2 380 ms), le milieu fait 50 % du chemin en 299 ms (médiane ; quartiles 158 ms – 698 ms) et 90 % en 577 ms ; 9 % des sauts ne sont pas suivis dans les 30 s.
* **Durée de vie d'un prix périmé** (saut Binance, 212 niveaux) : médiane 92 ms (p75 323 ms) ; 41 % sont encore là à 150 ms, 27 % à 300 ms ; avantage médian 2,3 c/part pour 20 parts au meilleur ask ; 41 % sont retirés par un preneur plus rapide, le reste annulé par le teneur.
* **Gain attendu sur les prix périmés (saut Binance, marge 0, 50 parts max., valorisé à +10 s)** — 50 ms : +342 (IC +169 ; +526) pUSD/h (exécution 84 %) ; 100 ms : +185 (IC +19 ; +358) pUSD/h (exécution 71 %) ; 200 ms : +112 (IC −60 ; +304) pUSD/h (exécution 56 %) ; 500 ms : +54 (IC −89 ; +261) pUSD/h (exécution 33 %).
* **Toutes opportunités confondues** (5524 ; surtout des désaccords persistants entre la formule et le marché) : pUSD/h 50 ms : −5 133 à +10 s, −7 491 à l'issue ; 100 ms : −5 386 à +10 s, −6 926 à l'issue ; 200 ms : −4 957 à +10 s, −6 747 à l'issue ; 500 ms : −4 359 à +10 s, −5 227 à l'issue. Exécution 100 % à 0 ms et 80 % à 5 s : la vitesse n'y change presque rien, leur rentabilité dépend de la justesse de la formule (voir `reports/polymarket/formule/`).
* **Chainlink** (prix de résolution) retarde de ≈ 600 ms sur Binance (heure serveur ; 700 ms vu d'ici), cote 2,1 pb sous BTCUSDT et n'arrive ici que 1 334 ms après son horodatage : le signal, c'est Binance ; Chainlink n'est que la règle.
* **Transport vu d'ici** : Binance rx − E −7 ms (médiane), carnet Polymarket rx − ts −34 ms ; horloge locale en retard d'environ 128 ms (± 93 ms) ; aller-retour HTTP vers le CLOB depuis ce conteneur 131 ms. D'ici, un ordre aurait ℓ ≈ calcul + aller-retour CLOB + 150 ms ≈ 281 ms.
* **Échantillon petit** : 0,9 h, 22 marchés (21 résolus) ; IC larges. Relancer `python scripts/latency_study.py` quand les collecteurs auront tourné plusieurs jours.

## Graphiques

**Vu d'ici, le carnet Polymarket a fait la moitié du chemin ≈ 299 ms après un saut de Binance (médiane) et 90 % après ≈ 577 ms ; il retouche son meilleur prix dans le bon sens en 197 ms, contre 2 380 ms sans saut (placebo)**

![délai de réaction du carnet](reaction_carnet.png)

**Un prix périmé après un saut de Binance reste 92 ms dans le carnet (médiane, vu d'ici) : 41 % tiennent plus que les 150 ms du délai preneur**

![durée de vie des prix périmés](duree_vie_prix_perimes.png)

**Les prix périmés après un saut de Binance ne rapportent de façon démontrée que si l'ordre touche le carnet en moins de 100 ms (vu d'ici) ; la moitié de l'avantage attendu est perdue à ≈ 182 ms, le point mort est encore incertain (124 ms – 4 369 ms)**

![P&L selon la latence](pnl_vs_latence.png)

**Les prix périmés après un saut de Binance s'évaporent : on n'en touche plus que 63 % à 150 ms et 46 % à 300 ms, contre 96 % des désaccords persistants**

![taux d'exécution selon la latence](execution_vs_latence.png)

**Chainlink, le prix qui sert à la résolution, retarde de ≈ 0,6 s sur Binance (0,7 s vu d'ici) et cote 2,1 pb sous BTCUSDT**

![retard de Chainlink sur Binance](chainlink_vs_binance.png)

**Vu d'ici, Binance et le carnet Polymarket arrivent avec des retards voisins (quelques dizaines de ms d'écart), Chainlink ≈ 1,3 s après son horodatage**

![latences de transport](latences_transport.png)

## Comment atteindre ce temps de réaction

> Description technique générique, **inutilisable légalement depuis la France** (blocage ANJ, close-only, CGU). Aucune méthode de contournement n'est donnée ni envisagée.

Budget « mouvement Binance → ordre apparié », à comparer aux ≈ 209 ms au bout desquels la moitié de l'avantage est perdue (gain démontré jusqu'à ≈ 126 ms) :

| étape | ordre de grandeur | levier |
|---|---|---|
| Binance (moteur à Tokyo, AWS ap-northeast-1) → serveur du robot | 1–5 ms à Tokyo ; ≈ 100–120 ms jusqu'à Londres ; ici rx − E = −7 ms + décalage d'horloge | WebSocket direct (bookTicker, ou flux binaires SBE), sans CDN ni proxy |
| calcul de P_formule et décision | < 1 ms | formule fermée (Φ), moyennes glissantes incrémentales, σ tenu à jour à chaque seconde |
| signature EIP-712 de l'ordre + en-têtes HMAC | ≈ 1–5 ms en Python, < 0,5 ms en natif | clé en mémoire, bibliothèque native, ordres préparés d'avance aux prix probables |
| serveur du robot → CLOB (AWS eu-west-2, Londres) | < 2 ms dans la même région ; ≈ 100–120 ms depuis Tokyo ; aller-retour 131 ms depuis ce conteneur | connexion HTTP/2 déjà ouverte (keep-alive) |
| délai preneur Polymarket (marchés crypto) | **150 ms, incompressible** (depuis le 04/09/2026) | aucun : c'est un ralentisseur qui laisse aux teneurs le temps d'annuler |

Tokyo ↔ Londres coûte ≈ 100–120 ms dans un sens, à payer une fois (sur le flux Binance ou sur l'ordre). Le meilleur total réaliste pour un **preneur** qui lit Binance est donc ≈ 110 + 150 ≈ 260–280 ms. Les **teneurs de marché**, eux, annulent sans délai : ce sont eux qui gagnent la course, et la plupart des prix périmés disparaissent avant qu'un preneur puisse les toucher. Quand le temps utile est sous 150 ms, aucune infrastructure ne suffit en preneur : il faudrait tenir le carnet (être celui qui réévalue ses prix le plus vite), ce qui change de métier (inventaire, sélection adverse, remises maker) — voir `reports/polymarket/maker_live/`.

**Depuis ce conteneur** (derrière un proxy, côte est des États-Unis d'après les délais), ℓ ≈ aller-retour CLOB + 150 ms ≈ 281 ms, contre ℓ½ ≈ 182 ms et un gain démontré jusqu'à 100 ms : on arriverait après la disparition de l'essentiel des prix périmés. Pour descendre plus bas, les seuls leviers réels sont la **géographie** (un serveur dans la région du moteur du CLOB, ou près de Binance, reliés par le chemin réseau le plus court), un **flux Binance direct** et un **code natif** qui signe et envoie l'ordre en moins d'une milliseconde ; le délai de 150 ms, lui, ne se négocie pas.

## Tableaux

### Latence critique ℓ* (P&L = 0) et demi-vie de l'avantage

| origine | marge | valorisation | ℓ* (isotone) | IC bas | IC haut | ℓ* (brut) | gain démontré jusqu'à | ℓ½ avantage | P(P&L > 0 à ℓ = 0) | opportunités | marchés |
|---|---|---|---|---|---|---|---|---|---|---|---|
| saut | 0 c | issue officielle | > 5 000 ms | 767 ms | > 5 000 ms | > 5 000 ms | 25 ms | 182 ms | 99 % | 212 | 20 |
| saut | 1 c | issue officielle | > 5 000 ms | 0 ms | > 5 000 ms | > 5 000 ms | aucune | 189 ms | 94 % | 206 | 20 |
| saut | 2 c | issue officielle | > 5 000 ms | 296 ms | > 5 000 ms | > 5 000 ms | 25 ms | 187 ms | 98 % | 192 | 20 |
| saut | 0 c | valorisé à +10 s | 833 ms | 124 ms | 4 369 ms | 296 ms | 100 ms | 182 ms | 100 % | 212 | 20 |
| saut | 1 c | valorisé à +10 s | 2 075 ms | 128 ms | > 5 000 ms | 2 075 ms | 100 ms | 189 ms | 100 % | 206 | 20 |
| saut | 2 c | valorisé à +10 s | 3 424 ms | 126 ms | > 5 000 ms | 3 424 ms | 100 ms | 187 ms | 100 % | 192 | 20 |
| tous | 0 c | issue officielle | 0 ms | 0 ms | > 5 000 ms | 0 ms | aucune | > 5 000 ms | 18 % | 5 415 | 21 |
| tous | 1 c | issue officielle | 0 ms | 0 ms | > 5 000 ms | 0 ms | aucune | > 5 000 ms | 22 % | 4 854 | 21 |
| tous | 2 c | issue officielle | 0 ms | 0 ms | > 5 000 ms | 0 ms | aucune | > 5 000 ms | 25 % | 4 292 | 21 |
| tous | 0 c | valorisé à +10 s | 0 ms | 0 ms | 0 ms | 0 ms | aucune | > 5 000 ms | 1 % | 5 524 | 22 |
| tous | 1 c | valorisé à +10 s | 0 ms | 0 ms | 0 ms | 0 ms | aucune | > 5 000 ms | 1 % | 4 959 | 22 |
| tous | 2 c | valorisé à +10 s | 0 ms | 0 ms | 1 039 ms | 0 ms | aucune | > 5 000 ms | 2 % | 4 391 | 22 |

ℓ* : première latence où le P&L moyen par opportunité devient ≤ 0 (interpolation linéaire sur la grille 0–5 000 ms), après régression isotone décroissante de la courbe (en espérance, arriver plus tard ne peut pas rapporter plus ; « brut » : sans ce lissage, sensible au bruit) ; « 0 ms » : jamais positif ; « > 5 000 ms » : encore positif à 5 s. « gain démontré jusqu'à » : plus grande latence de la grille jusqu'à laquelle la borne basse de l'IC 95 % reste > 0 (« aucune » : pas même à 0 ms). IC : percentiles 2,5 et 97,5 % sur 2000 tirages bootstrap des marchés. ℓ½ avantage : latence à laquelle l'avantage attendu capturé selon la formule (Σ parts × (P(t) − coût), sans bruit d'issue) tombe à la moitié de sa valeur à ℓ = 0. Origines « persistante » et « carnet » : `latence_critique.csv`.

### P&L selon la latence (marge 0)

| origine | ℓ (ms) | exécution | parts | avantage attendu (c/part) | attendu (pUSD/h) | +10 s (pUSD/h) | IC +10 s | issue (pUSD/h) | IC issue | issue (c/part) |
|---|---|---|---|---|---|---|---|---|---|---|
| saut | 0 | 100 % | 5 448 | 3,8 | +223,8 | +372,5 | +179,7 ; +583,4 | +825,6 | +146,8 ; +1 523,2 | +13,0 |
| saut | 25 | 88 % | 5 170 | 3,6 | +203,5 | +331,9 | +142,6 ; +538,2 | +712,4 | +81,6 ; +1 385,3 | +11,8 |
| saut | 50 | 84 % | 4 943 | 3,5 | +193,9 | +341,9 | +169,3 ; +526,5 | +573,9 | −75,6 ; +1 292,1 | +10,0 |
| saut | 100 | 71 % | 4 197 | 3,4 | +137,8 | +185,3 | +18,7 ; +358,0 | +500,5 | −148,5 ; +1 210,1 | +10,2 |
| saut | 150 | 63 % | 3 828 | 3,4 | +130,9 | +147,3 | −24,9 ; +319,1 | +441,1 | −125,9 ; +966,9 | +9,9 |
| saut | 200 | 56 % | 3 220 | 3,0 | +101,4 | +111,7 | −60,2 ; +303,9 | +520,5 | +49,9 ; +987,9 | +13,9 |
| saut | 300 | 46 % | 2 934 | 2,6 | +83,1 | −4,1 | −179,5 ; +168,4 | +658,0 | +41,8 ; +1 343,0 | +19,3 |
| saut | 500 | 33 % | 2 194 | 2,5 | +61,6 | +53,9 | −88,6 ; +260,7 | +655,9 | +130,4 ; +1 250,2 | +25,7 |
| saut | 750 | 30 % | 2 323 | 2,6 | +64,1 | +18,9 | −147,6 ; +259,4 | +600,9 | +28,9 ; +1 241,9 | +22,2 |
| saut | 1 000 | 32 % | 2 330 | 2,8 | +77,8 | −37,9 | −207,9 ; +178,8 | +452,7 | −63,5 ; +1 024,5 | +16,7 |
| saut | 2 000 | 31 % | 2 563 | 4,0 | +123,7 | −78,7 | −251,6 ; +96,9 | +262,7 | −375,6 ; +984,0 | +8,8 |
| saut | 5 000 | 31 % | 2 475 | 4,8 | +141,4 | −163,2 | −345,0 ; −5,8 | +317,5 | −472,6 ; +1 169,7 | +11,0 |
| tous | 0 | 100 % | 115 712 | 7,1 | +9 537,1 | −3 073,5 | −5 819,4 ; −465,4 | −5 496,7 | −17 414,4 ; +5 446,2 | −4,2 |
| tous | 25 | 98 % | 184 224 | 7,3 | +15 460,6 | −5 214,9 | −8 813,9 ; −1 743,7 | −7 058,5 | −24 470,9 ; +9 955,3 | −3,4 |
| tous | 50 | 98 % | 196 257 | 7,5 | +16 795,9 | −5 132,5 | −9 080,3 ; −1 286,4 | −7 490,5 | −25 970,0 ; +10 458,0 | −3,4 |
| tous | 100 | 96 % | 206 424 | 7,6 | +18 162,5 | −5 386,0 | −9 755,9 ; −1 195,3 | −6 926,2 | −27 222,3 ; +12 823,8 | −2,9 |
| tous | 150 | 96 % | 207 104 | 7,8 | +18 467,7 | −5 327,8 | −9 602,6 ; −1 122,7 | −8 011,3 | −29 087,4 ; +12 087,2 | −3,4 |
| tous | 200 | 95 % | 209 032 | 7,9 | +18 878,5 | −4 957,4 | −9 308,3 ; −404,1 | −6 746,7 | −27 699,8 ; +13 246,5 | −2,8 |
| tous | 300 | 94 % | 209 576 | 8,1 | +19 635,9 | −4 929,1 | −9 564,0 ; −155,9 | −5 842,4 | −27 167,5 ; +15 131,1 | −2,4 |
| tous | 500 | 92 % | 205 372 | 8,3 | +19 530,6 | −4 358,8 | −9 147,2 ; +509,1 | −5 227,3 | −26 081,0 ; +15 410,9 | −2,2 |
| tous | 750 | 90 % | 203 662 | 8,6 | +19 802,8 | −4 034,8 | −8 750,9 ; +950,0 | −6 006,1 | −26 438,3 ; +13 679,4 | −2,6 |
| tous | 1 000 | 90 % | 202 615 | 8,7 | +20 231,3 | −4 115,7 | −8 803,9 ; +802,2 | −5 338,5 | −25 813,6 ; +14 423,3 | −2,3 |
| tous | 2 000 | 86 % | 194 739 | 9,4 | +21 175,1 | −5 618,7 | −10 269,7 ; −1 145,9 | −7 250,8 | −25 953,9 ; +11 897,7 | −3,3 |
| tous | 5 000 | 80 % | 183 716 | 10,9 | +23 903,0 | −5 419,1 | −10 116,8 ; −830,7 | −5 863,6 | −23 429,8 ; +12 320,4 | −2,8 |

« attendu » : avantage selon la formule au prix payé (modèle, sans bruit) ; « +10 s » : valorisé au milieu du carnet 10 s après l'achat ; « issue » : à l'issue officielle, marchés résolus seulement. Les totaux par heure supposent qu'on prend chaque opportunité (50 parts au plus chacune), sans limite de position.

### Réaction du carnet aux sauts de la formule

| saut | actif | n | marchés | 1re retouche bon sens | placebo (3 s avant) | 50 % p25 | 50 % médiane | 50 % p75 | 90 % médiane | non suivis (30 s) | carnet en avance |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 pts | btc | 99 | 12 | 242 | 2 428 | 241 | 394 | 754 | 708 | 9 % | 7 % |
| 5 pts | eth | 75 | 10 | 113 | 2 306 | 96 | 214 | 491 | 457 | 8 % | 16 % |
| 10 pts | btc | 42 | 11 | 221 | 2 580 | 248 | 409 | 597 | 592 | 7 % | 0 % |
| 10 pts | eth | 33 | 9 | 148 | 1 575 | 201 | 249 | 491 | 560 | 12 % | 7 % |
| 5 pts | tous | 174 | 22 | 197 | 2 380 | 158 | 299 | 698 | 577 | 9 % | 11 % |
| 10 pts | tous | 75 | 20 | 179 | 2 377 | 228 | 307 | 500 | 590 | 9 % | 3 % |

Délais en ms, vus d'ici, depuis l'instant où la formule a fait la moitié de son saut. « carnet en avance » : le milieu avait déjà fait la moitié du chemin avant cet instant (il a réagi à une autre source, ou les teneurs voient Binance avant nous). Le placebo mesure la même retouche 3 s avant le saut (fréquence de base des retouches, fenêtre arrêtée au saut) : la « 1re retouche » n'est une réaction que si elle est nettement plus rapide que lui. Quantiles « censure comprise » (pas de réaction = +∞).

### Prix périmés (marge 0)

| origine | actif | n | marchés | vie méd. (ms) | p75 (ms) | > 50 ms | > 150 ms | > 300 ms | > 1 s | avantage > 0 (ms, méd.) | avantage (c/part) | parts au meilleur ask | parts rentables | pris par un preneur |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tous | tous | 5 524 | 22 | 800 | 4 779 | 83 % | 73 % | 67 % | 54 % | 700 | 5,1 | 15 | 444 | 39 % |
| tous | btc | 2 462 | 12 | 1 749 | 8 164 | 90 % | 84 % | 78 % | 66 % | 1 455 | 5,1 | 16 | 1 174 | 66 % |
| tous | eth | 3 062 | 10 | 425 | 2 983 | 77 % | 65 % | 57 % | 44 % | 373 | 5,1 | 15 | 271 | 19 % |
| carnet | tous | 1 083 | 22 | 1 087 | 6 550 | 87 % | 79 % | 72 % | 56 % | 825 | 1,7 | 15 | 99 | 38 % |
| carnet | btc | 421 | 12 | 1 899 | 10 105 | 93 % | 86 % | 81 % | 66 % | 1 418 | 1,7 | 20 | 157 | 63 % |
| carnet | eth | 662 | 10 | 744 | 5 321 | 84 % | 74 % | 66 % | 49 % | 615 | 1,7 | 15 | 75 | 23 % |
| persistante | tous | 4 229 | 22 | 863 | 4 666 | 83 % | 73 % | 67 % | 56 % | 788 | 6,3 | 15 | 654 | 39 % |
| persistante | btc | 1 922 | 12 | 2 095 | 8 413 | 92 % | 86 % | 81 % | 70 % | 1 800 | 6,3 | 15 | 1 694 | 67 % |
| persistante | eth | 2 307 | 10 | 372 | 2 619 | 76 % | 63 % | 56 % | 44 % | 324 | 6,3 | 15 | 384 | 18 % |
| saut | tous | 212 | 20 | 92 | 323 | 64 % | 41 % | 27 % | 13 % | 90 | 2,3 | 20 | 91 | 41 % |
| saut | btc | 119 | 11 | 88 | 302 | 61 % | 42 % | 25 % | 10 % | 88 | 2,2 | 22 | 105 | 55 % |
| saut | eth | 93 | 9 | 106 | 378 | 68 % | 40 % | 30 % | 17 % | 98 | 2,5 | 20 | 80 | 22 % |

Origine : **saut** = P_formule a monté d'au moins 1 point dans la seconde et l'avantage n'existait pas 1 s plus tôt (prix périmé au sens strict) ; **persistante** = l'avantage existait déjà 1 s plus tôt (désaccord durable entre la formule et le marché, ou niveau qui clignote) ; **carnet** = un ask moins cher est apparu sans mouvement de Binance. Marges 1 c et 2 c : `opportunites_resume.csv`.

### Latences de transport (réception locale − horodatage serveur)

| source | mesure | n | p10 | median | p90 | note |
|---|---|---|---|---|---|---|
| Binance ETHUSDT aggTrade | rx − E | 8 202 | −25 | −7 | 11 | réception locale − heure d'événement Binance |
| Binance ETHUSDT aggTrade | E − T | 8 202 | 0 | 1 | 2 | événement − trade (serveur) |
| Binance BTCUSDT aggTrade | rx − E | 16 026 | −26 | −7 | 11 | réception locale − heure d'événement Binance |
| Binance BTCUSDT aggTrade | E − T | 16 026 | 0 | 0 | 1 | événement − trade (serveur) |
| Coinbase ETH-USD ticker | rx − time | 2 493 | −95 | −75 | −20 | réception locale − heure du trade Coinbase |
| Coinbase BTC-USD ticker | rx − time | 12 887 | −95 | −76 | −56 | réception locale − heure du trade Coinbase |
| RTDS crypto_prices btcusdt | rx − timestamp | 3 481 | 205 | 319 | 438 | réception locale − horodatage de l'observation |
| RTDS crypto_prices btcusdt | msg_ts − timestamp | 3 481 | 124 | 133 | 157 | publication RTDS − observation (serveur) |
| RTDS crypto_prices btcusdt | rx − msg_ts | 3 481 | 68 | 181 | 297 | réception locale − publication RTDS |
| Chainlink btc/usd (RTDS) | rx − timestamp | 3 437 | 1 024 | 1 334 | 1 726 | réception locale − horodatage de l'observation |
| Chainlink btc/usd (RTDS) | msg_ts − timestamp | 3 437 | 793 | 1 091 | 1 469 | publication RTDS − observation (serveur) |
| Chainlink btc/usd (RTDS) | rx − msg_ts | 3 437 | 113 | 246 | 357 | réception locale − publication RTDS |
| Polymarket CLOB BTC 15m | rx − ts | 275 293 | −49 | −35 | 3 | réception locale − horodatage du message CLOB |
| Polymarket CLOB BTC 5m | rx − ts | 521 979 | −52 | −34 | 84 | réception locale − horodatage du message CLOB |
| Polymarket CLOB ETH 5m | rx − ts | 387 879 | −53 | −33 | −7 | réception locale − horodatage du message CLOB |

### Chainlink contre Binance

| asset | horloge | horizon_s | lag_ms | corr_max | corr_lag0 | n | basis_median_pb | basis_p10_pb | basis_p90_pb | basis_sd_pb |
|---|---|---|---|---|---|---|---|---|---|---|
| btc | rx | 1,000 | 700 | 0,714 | 0,483 | 3 404 | −2,09 | −2,50 | −1,71 | 0,30 |
| btc | rx | 5,000 | 700 | 0,899 | 0,855 | 3 391 | −2,09 | −2,50 | −1,71 | 0,30 |
| btc | serveur | 1,000 | 600 | 0,676 | 0,485 | 3 405 | −2,09 | −2,50 | −1,71 | 0,30 |
| btc | serveur | 5,000 | 500 | 0,894 | 0,856 | 3 392 | −2,09 | −2,49 | −1,71 | 0,30 |

`lag_ms` : décalage de corrélation maximale entre les rendements Chainlink (sur `horizon_s`) et ceux de Binance décalés ; `horloge` = `rx` (Binance à la réception locale : ce que voit le robot) ou `serveur` (heure d'événement Binance `E`, indépendante de notre réseau). `basis` : log(Chainlink / Binance décalé), en points de base. Le rapport historique `reports/polymarket/formule/` trouvait ≈ 4 s avec une autre méthode (erreur sur F − K entre bougies Binance 1 s et `priceToBeat`) : la corrélation des rendements mesure le retard du signal, l'erreur de niveau inclut aussi le lissage de l'agrégat Chainlink.

## Méthode

1. **Séries alignées sur l'horloge locale** (`rx`) : milieu Binance (`bookTicker`), dernier prix Binance (`aggTrade`, `E`/`T` serveur), Coinbase (`ticker`, `time`), Chainlink (RTDS `crypto_prices_chainlink` : valeur, horodatage de l'observation, heure de publication, `rx`), carnet Polymarket reconstruit après chaque message (meilleurs bid/ask Up et Down, tailles, milieu ; `rx` et `ts` serveur). Valeur « à la date » = dernier point reçu ≤ t.
2. **P_formule** (`tradebot.latency.TwapFormula`) : P(Up | t) de `polymarket_formula` (Up ssi TWAP60 Chainlink à E ≥ TWAP60 à S) calculée pour des moyennes de **points 1 s** : m = E[F − K | t] avec les points déjà réalisés (Binance 1 s reconstruit : dernier milieu reçu ≤ chaque seconde) et le prix Binance courant pour les points futurs ; Var/σ² = ∫ g², exacte entre deux secondes. σ = EWMA causale des rendements 1 s (demi-vie 600 s, initialisée sur les 3 h de bougies Binance 1 s précédant les données) × 1,40 (facteur de calibration du rapport historique `reports/polymarket/formule/` : les rendements 1 s sont autocorrélés). Retard de Chainlink : le point Chainlink `s` est lu sur Binance à `s − 700 ms` (mesuré ici). Erreur de suivi Chainlink − Binance de F − K : 0,40 pb ajoutés en quadrature (valeur du rapport historique : moins de 10 marchés BTC avec Chainlink complet ici (6)). Évaluée toutes les 100 ms, à chaque mise à jour Binance et à chaque message du carnet.
3. **Réaction du carnet** : sauts de P_formule ≥ 5 et ≥ 10 points en ≤ 1 s (puis 2 s sans nouveau saut) ; t0 = instant où la formule a fait la moitié du saut (précisé à la mise à jour Binance près) ; délais jusqu'à la première retouche du meilleur ask/bid Up dans le sens du saut après t0 (placebo : la même mesure 3 s avant le saut), puis jusqu'à ce que le milieu ait parcouru 50 % et 90 % du saut depuis son niveau d'avant ; censure à 30 s.
4. **Prix périmés** : opportunité à t si P_côté(t) − (ask + 0,07·ask·(1 − ask)) > marge (0, 1 c, 2 c), pour un ask entre 0,05 et 0,95 (les queues relèvent surtout de l'erreur de modèle). Une opportunité = un niveau de prix (côté, ask0) ; elle vit jusqu'à ce que le meilleur ask du côté remonte au-dessus de ask0 (niveau annulé ou consommé) ; un niveau encore vivant n'en ouvre pas une nouvelle. Origine : « saut » si P_côté a monté d'au moins 1 point dans la seconde et que l'avantage n'existait pas 1 s plus tôt, « persistante » s'il existait déjà, « carnet » sinon. « Pris par un preneur » : un trade a consommé ce niveau pendant sa vie. Tailles : au meilleur ask, et cumulées sur les niveaux encore rentables. Les messages du CLOB de même horodatage serveur (annulation + nouvel ordre…) sont regroupés : les états intermédiaires de quelques µs ne comptent pas comme des prix disponibles.
5. **Exécution à t + ℓ** : pour chaque opportunité détectée à t, l'ordre arrive au carnet vu à t + ℓ ; achat au meilleur ask de ce carnet si P_côté(t) − coût(ask) > marge (ordre limite calculé à t), quantité min(taille au meilleur ask, 50) ; P&L = parts × (1{côté gagnant} − ask − frais) à l'issue officielle (`meta.json`, sinon gamma en lecture seule), et valorisation au milieu du côté 10 s après l'achat (à l'issue si la clôture tombe avant). IC : bootstrap des marchés (2000 tirages).

### Biais de notre propre latence (et comment il déplace ℓ*)

On voit Binance avec un retard d_B et le carnet avec un retard d_P (≈ 50–150 ms chacun, inséparables sans horloge commune). Un mouvement Binance à l'instant réel τ est détecté ici à τ + d_B ; un état du carnet à l'instant réel x est vu ici à x + d_P. Simuler « ordre au carnet vu à t + ℓ » revient à apparier l'ordre à l'instant réel τ + d_B + ℓ − d_P : **le temps réel disponible vaut T = ℓ* + (d_B − d_P)**, et d_B − d_P ne dépend pas de l'horloge locale : médiane(rx − E Binance) − médiane(rx − ts CLOB) = −7 − (−34) = +26 ms : les latences mesurées ici (ℓ*, ℓ½, gain démontré) sous-estiment donc le temps réel de ≈ 26 ms. Autres biais : (i) des teneurs qui lisent Binance plus près de Tokyo réagissent plus tôt que ce que nous voyons (les délais de réaction « vus d'ici » sont raccourcis de leur avance, ℓ* réel encore plus court) ; (ii) les ordres des autres preneurs sont dans les données (le niveau disparaît), mais notre ordre ne déplace pas le carnet (50 parts max., sans impact) ; (iii) un teneur qui annule dès qu'il voit un ordre preneur arriver (pendant le délai de 150 ms) rendrait l'exécution réelle pire que simulée.

## Limites

* **n petit** : 0,9 h de données communes aux deux collecteurs, 22 marchés, 21 avec issue connue (les autres n'entrent que dans la valorisation à +10 s). Les opportunités d'un même marché partagent la même issue : l'IC groupé par marché est la seule mesure honnête de l'incertitude.
* ETH : le flux RTDS ne renvoie que la première souscription de chaque thème (btc/usd) : **pas de Chainlink ETH** ; le retard et l'erreur de suivi mesurés sur BTC sont appliqués à ETH.
* Carnet : les `price_change` sont filtrés à ± 0,10 du milieu par le collecteur (sans effet sur le meilleur niveau) ; la taille au meilleur ask est celle affichée (d'autres preneurs peuvent la prendre avant nous).
* P_formule suppose un log-prix sans tendance, σ constant jusqu'à E et Chainlink = Binance décalé ; elle se trompe quand Chainlink s'écarte de Binance (sources agrégées), surtout près de F = K. Les désaccords persistants sont surtout des erreurs de modèle ou des différences de σ, pas des prix périmés.
* 150 ms de délai preneur (documenté pour les marchés crypto depuis le 04/09/2026, non mesuré ici) : les latences ℓ < 150 ms ne sont atteignables par personne en preneur ; elles servent de référence. Le décalage d'horloge locale est estimé par une seule requête Binance (± la moitié de l'aller-retour) : seules les différences entre sources sont sûres.

## Relancer

```bash
. .venv/bin/activate
python scripts/latency_study.py              # tout ce qui est disponible
python scripts/latency_study.py --since 2026-09-27T00:00 --until 2026-09-28T00:00 --jobs 3
python scripts/latency_study.py --offline    # sans réseau (ni horloge, ni gamma, ni historique pour σ)
```

Le script relit tous les fichiers des deux collecteurs sur la période, garde les marchés complets (Binance et carnet couvrant [S − 90 s, E]) et réécrit ce dossier. Il ne modifie aucune donnée brute (les issues manquantes sont lues sur gamma sans toucher aux `meta.json`).

## Fichiers

* `marches.csv` : un marché par ligne : couverture, σ à S, F − K Binance contre l'issue officielle, nombres d'événements.
* `latences_transport.csv` : réception locale − horodatages serveur par source (p01, p10, médiane, p90, p99).
* `chainlink_binance_correlation.csv` : corrélation Chainlink / Binance selon le décalage (pas de 100 ms).
* `chainlink_binance_resume.csv` : retard et décalage (pb) de Chainlink sur Binance.
* `chainlink_erreur_suivi.csv` : F − K Chainlink contre Binance décalé, par marché BTC.
* `reaction_carnet.csv` : un saut de la formule par ligne, délais de réaction du carnet (ms).
* `reaction_carnet_resume.csv` : résumé des délais par seuil et actif.
* `opportunites.csv.gz` : une opportunité (niveau périmé) par ligne et par marge : origine, durée de vie, taille, avantage, retrait.
* `opportunites_resume.csv` : résumé par marge, origine et actif (survie à 50/150/300/1 000 ms).
* `pnl_par_opportunite.csv.gz` : P&L, quantité et prix d'exécution de chaque opportunité pour chaque ℓ.
* `pnl_vs_latence.csv` : P&L total / par opportunité / par part / par heure selon ℓ, marge et origine, avec IC.
* `latence_critique.csv` : ℓ*, IC et ℓ½ par marge, origine et valorisation.
* `pnl_par_marche.csv` : P&L par marché, marge, origine et ℓ.
* `pnl_par_heure.csv` : P&L par heure UTC, marge, origine et ℓ.
* `series_alignees_1s.csv.gz` : séries alignées sur l'horloge locale, une ligne par seconde et par marché : Binance (milieu, dernier trade), Coinbase, Chainlink (valeur et horodatage du dernier point reçu), carnet Up (bid, ask, tailles, milieu), P_formule, σ (désactivable : `--no-series`).
* `run.json` : paramètres et métadonnées du calcul.

Les fichiers `.csv.gz` (volumineux) se lisent directement avec `pandas.read_csv`.
