# Maker sur le carnet réel — Polymarket « Up or Down » BTC/ETH 5m, BTC 15m

*Généré le 26/09/2026 05:18 UTC par `scripts/polymarket_maker_live.py` (temps : 26 s). Marchés collectés par `scripts/polymarket_live_collector.py` (WebSocket CLOB, une connexion par marché) : 24 enregistrés, **18 exploitables** (fenêtre finie, carnet reconstruit dès S−60 s), du 26/09/2026 04:35–05:10 UTC.*

> Simulation papier sur données publiques : aucun ordre, aucune clé. Les ordres simulés n'altèrent pas le carnet enregistré : chaque configuration est une simulation indépendante.

## 0. Résumé

* **n est petit** (18 marchés sur 3 créneaux de 15 min ; 80 % résolus Up sur 15 issues connues) : les IC bootstrap (groupés par créneau) sont larges, et rien ci-dessous n'est démontré. Un taux de Up éloigné de 50 % sur si peu de marchés fait gagner mécaniquement les achats d'Up et perdre les achats de Down : lire les tableaux « par côté » avec cette réserve. C'est un premier passage à relancer quand la collecte aura duré 24 h.
* **Carnet autour de l'ouverture** (médianes) : écart 1,0 c à S−60 s, 1,0 c à S−30 s, 5,0 c à S, 1,0 c à S+30 s ; taille au meilleur bid 43 / 34 / 30 / 96 parts aux mêmes instants (meilleur ask : 56 / 41 / 15 / 82).
* **File d'attente typique** (paramètre `Q_ahead` pour le simulateur historique) : avant S, bid Up à 0,49 : 56 (p25 31 ; p75 148) parts, à 0,50 : 0 (p25 0 ; p75 173) ; bid Down à 0,49 : 45 (p25 0 ; p75 70), à 0,50 : 0 (p25 0 ; p75 21). Après S (S, S+30 s) : bid Up 0,49 : 5 (p25 0 ; p75 135) ; 0,50 : 10 (p25 0 ; p75 128) ; bid Down 0,49 : 0 (p25 0 ; p75 10) ; 0,50 : 0 (p25 0 ; p75 6). Un niveau à 0 part est un niveau vide (le milieu est ailleurs) : la médiane mélange donc « file » et « niveau absent » ; le § 3 sépare les deux.
* **Trades preneurs** : 10 377 trades ; 19,5 % traversent plus d'un niveau (IC 18,1 % ; 21,8 %) ; 9,7 % dépassent la taille affichée au meilleur niveau ; taille médiane 9 parts. Dans [S−60 s, S) : 24,1 % de traversées ; dans [S, S+60 s) : 22,5 %.
* **(a) signal TWAP partiel à S−30 s** : 108 ordres posables sur 14 marchés (54 écartés car ils croiseraient le carnet), exécutés 73 % (délai médian 16,4 s) ; P&L −4,1 c par part placée (IC −34,2 ; +14,7 c), −5,6 c par part exécutée (IC −49,1 ; +17,1 c) ; remise maker estimée +0,3 c ; sélection adverse : taux de gain 43 % si exécuté contre 62 % sinon (−18 points).
* **(b) deux côtés à 0,49** : 93 ordres posables sur 18 marchés (15 écartés car ils croiseraient le carnet), exécutés 73 % (délai médian 38,9 s) ; P&L −1,9 c par part placée (IC −9,7 ; +25,5 c), −2,7 c par part exécutée (IC −13,7 ; +51,0 c) ; remise maker estimée +0,3 c ; sélection adverse : taux de gain 46 % si exécuté contre 95 % sinon (−49 points).
* **(c) juste valeur ± 1 c** : 728 ordres posables sur 18 marchés (38 écartés car ils croiseraient le carnet), exécutés 39 % (délai médian 2,7 s) ; P&L −0,3 c par part placée (IC −10,8 ; +8,7 c), −0,8 c par part exécutée (IC −30,0 ; +17,6 c) ; remise maker estimée +0,3 c ; sélection adverse : taux de gain 49 % si exécuté contre 54 % sinon (−5 points).
* **Juste valeur Φ(d/σ) contre le milieu du carnet** (§ 6) : à S+30 s, Brier 0,244 contre 0,281 pour le marché (ΔBrier −0,037, IC −0,063 ; −0,019) ; à S+240 s, 0,214 contre 0,092 (ΔBrier +0,123) : l'information Binance 1 s vaut quelque chose dans les premières secondes, puis le carnet en sait plus que notre gaussienne.
* **Verdict provisoire** : 0 configuration(s) sur 13 ont un IC entièrement positif (tests multiples : 13 configurations, aucune correction). La meilleure, (a) signal TWAP partiel à S−30 s 0.50/S+60, fait +6,2 c par part placée (IC −50,0 ; +50,0 c) ; choisie a posteriori, elle est optimiste. La règle fixée d'avance (choix sur la 1re moitié, test sur la 2e) est au § 5.

## 1. Données : le collecteur et ce qu'il enregistre

* `scripts/polymarket_live_collector.py` ouvre une connexion `wss://ws-subscriptions-clob.polymarket.com/ws/market` par marché (jetons Up et Down), 2,5 min avant l'ouverture, fermée 2 min après la clôture ou dès l'événement `market_resolved`. Messages : `book` (instantané complet, émis à chaque trade), `price_change` (nouvelle taille d'un niveau), `best_bid_ask`, `last_trade_price` (côté, prix, taille du preneur, horodatage serveur ms), `tick_size_change`, `market_resolved`.
* **Débit** mesuré avec le format brut (v1, messages tels quels) le 26/09 à 04:45 UTC : 55,5 Mo en 13,4 min, soit ≈ 250 Mo/h pour BTC 5m + BTC 15m + ETH 5m (≈ 6 Go / 24 h ; un marché BTC 5m actif pèse 16 Mo), dont 80 % de `price_change` (identifiants de jetons de 77 caractères répétés, niveaux lointains). Estimation en régime établi d'après la taille moyenne par marché fini : v1 ≈ 223 Mo/h. Le collecteur a été remplacé à 04:51 UTC (recouvrement de 3 min, sans perte) par un **format compact v2** : jetons codés 0/1, `price_change` filtrés aux niveaux à ± 0,10 du milieu, vidage gzip toutes les 5 s, chien de garde (reconnexion si 45 s sans message), arrêt propre sur SIGINT/SIGTERM, segments `<slug>.s<k>.jsonl.gz` quand un fichier existe déjà, rafraîchissement de l'issue et de `priceToBeat`/`finalPrice` pendant 6 h après la clôture. Débit v2 estimé de la même façon : ≈ 35 Mo/h (≈ 0,8 Go / 24 h ; un marché BTC 5m ≈ 1,3 Mo, ETH 5m ≈ 0,4 à 1,2 Mo, BTC 15m ≈ 2,5 Mo). Mémoire du processus : ≈ 140 Mo.
* **Reconstruction** (`tradebot.polymarket_book`) : carnet dans le repère du jeton Up (les messages Up et Down sont exactement symétriques : bid Down à p = ask Up à 1 − p, vérifié sur 290 000 `price_change`, 0 écart) ; instantané puis deltas ; contrôle avec `best_bid_ask` : accord médian 98,5 % par marché (le serveur émet `best_bid_ask` juste avant le `price_change` correspondant ; on accepte 200 ms de retard). Les fichiers tronqués (collecteur tué) sont lus jusqu'à la coupure.
* **Marchés** : 24 enregistrés, 18 exploitables. Écartés : pas de carnet à S−60 s (collecte commencée trop tard) (3); fenêtre en cours (3).
* **Binance 1 s** (`data-api.binance.vision`, cache `data/cache/pm_maker_live/binance_1s/`) : signal `gap_m30` = log(spot(S−30) / moyenne des closes 1 s sur [S−60, S−30)) et juste valeur pendant la fenêtre.

## 2. Le carnet autour de l'ouverture

![Avant l'ouverture, le carnet reste serré (écart médian 1,0 c à S−30 s, 5,0 c à S) mais mince : 34 parts au meilleur bid à S−30 s (43 à S−60 s, 30 à S), 44 parts devant un bid Up à 0,49](profondeur_autour_de_S.png)

Profondeur par instant (médiane ; p25 ; p75 sur les marchés exploitables), tous marchés :

| mesure | S-60 s | S-30 s | S | S+30 s |
|---|---|---|---|---|
| écart bid–ask | 1,0 c (1,0 ; 1,0) | 1,0 c (1,0 ; 1,0) | 5,0 c (2,0 ; 13,5) | 1,0 c (1,0 ; 1,0) |
| taille au meilleur bid (Up) | 43 (30 ; 153) | 34 (17 ; 89) | 30 (15 ; 39) | 96 (52 ; 175) |
| taille au meilleur ask (Up) | 56 (18 ; 87) | 41 (22 ; 70) | 15 (10 ; 39) | 82 (51 ; 279) |
| cumul bids à ± 0,10 du milieu | 1 630 (600 ; 1 959) | 967 (442 ; 1 316) | 347 (39 ; 511) | 2 016 (1 538 ; 4 729) |
| cumul asks à ± 0,10 du milieu | 1 493 (654 ; 1 801) | 788 (471 ; 1 246) | 264 (105 ; 415) | 2 062 (1 450 ; 4 009) |

Par cellule, à S−30 s (médianes) :

| cellule | marchés | écart (c) | meilleur bid | meilleur ask | bid Up 0,49 | bid Up 0,50 | bid Down 0,49 | bid Down 0,50 |
|---|---|---|---|---|---|---|---|---|
| Tous | 18 | 1,0 | 34 | 41 | 44 | 5 | 25 | 0 |
| BTC 15m | 2 | 1,0 | 174 | 58 | 144 | 174 | 58 | 0 |
| BTC 5m | 8 | 1,0 | 55 | 76 | 66 | 85 | 2 | 3 |
| ETH 5m | 8 | 1,0 | 21 | 25 | 27 | 0 | 25 | 10 |

## 3. File d'attente aux niveaux 0,48–0,52 et traversées

Taille affichée (parts) à chaque niveau, avant S (instants S−60 s et S−30 s) et après S (S et S+30 s) ; « niveau présent » = niveau non vide (le milieu est proche). La médiane sur les niveaux présents est la file `Q_ahead` qu'un ordre posé à ce prix aurait devant lui.

| phase | niveau | part niveau présent | médiane (tous) | médiane (présents) | p25 (présents) | p75 (présents) |
|---|---|---|---|---|---|---|
| avant S | bid Up 0,48 | 94 % | 98 | 100 | 61 | 153 |
| avant S | bid Up 0,49 | 92 % | 56 | 70 | 38 | 158 |
| avant S | bid Up 0,50 | 47 % | 0 | 179 | 60 | 261 |
| avant S | bid Up 0,51 | 28 % | 0 | 81 | 35 | 116 |
| avant S | bid Up 0,52 | 17 % | 0 | 66 | 47 | 86 |
| avant S | bid Down 0,48 | 78 % | 82 | 91 | 75 | 147 |
| avant S | bid Down 0,49 | 67 % | 45 | 59 | 48 | 92 |
| avant S | bid Down 0,50 | 44 % | 0 | 25 | 15 | 53 |
| avant S | bid Down 0,51 | 6 % | 0 | 52 | 41 | 64 |
| avant S | bid Down 0,52 | 6 % | 0 | 43 | 36 | 50 |
| après S | bid Up 0,48 | 58 % | 23 | 114 | 35 | 189 |
| après S | bid Up 0,49 | 50 % | 5 | 138 | 62 | 185 |
| après S | bid Up 0,50 | 53 % | 10 | 108 | 30 | 242 |
| après S | bid Up 0,51 | 39 % | 0 | 161 | 76 | 399 |
| après S | bid Up 0,52 | 36 % | 0 | 117 | 61 | 143 |
| après S | bid Down 0,48 | 50 % | 5 | 36 | 20 | 81 |
| après S | bid Down 0,49 | 36 % | 0 | 25 | 10 | 350 |
| après S | bid Down 0,50 | 28 % | 0 | 76 | 19 | 98 |
| après S | bid Down 0,51 | 25 % | 0 | 70 | 33 | 123 |
| après S | bid Down 0,52 | 19 % | 0 | 74 | 26 | 151 |

Trades preneurs (tous marchés exploitables) : « traverse » = le prix du trade dépasse strictement le meilleur niveau d'avant (au moins deux niveaux consommés) ; « dépasse la profondeur » = taille > taille affichée au meilleur niveau. IC bootstrap par créneau.

| fenêtre | trades | traverse > 1 niveau | IC bas | IC haut | dépasse la profondeur | taille médiane | taille p90 | part d'achats |
|---|---|---|---|---|---|---|---|---|
| toutes | 10 377 | 19,5 % | 18,1 % | 21,8 % | 9,7 % | 9 | 56 | 92 % |
| avant S−60 s | 61 | 8,2 % | 0,0 % | 23,1 % | 13,1 % | 6 | 50 | 100 % |
| [S−60 s, S) | 473 | 24,1 % | 19,4 % | 28,1 % | 15,0 % | 8 | 49 | 99 % |
| [S, S+60 s) | 1 483 | 22,5 % | 17,7 % | 27,3 % | 10,1 % | 9 | 50 | 97 % |
| après S+60 s | 8 360 | 18,8 % | 17,3 % | 22,2 % | 9,3 % | 9 | 59 | 90 % |

## 4. Le simulateur maker

Un ordre au repos (achat de 10 parts de Up ou de Down à L) posé à t0 (+ 300 ms de latence) a devant lui la taille affichée à L à t0. La file ne dépasse jamais la taille affichée (les annulations devant nous la réduisent). Chaque trade preneur qui consomme ce niveau (`last_trade_price` du côté opposé, en tenant compte de la complémentarité : un achat preneur de Down à 1 − L consomme les bids Up à L) sert d'abord la file, puis nous ; un trade au-delà du niveau (traversée) nous exécute entièrement ; si un ordre adverse entre dans notre niveau (le carnet nous « croise »), il nous exécute. Un ordre qui croiserait le carnet dès la pose serait un ordre preneur : il est écarté (`crossing`) et compté à part. Annulation à t_cancel (exécution partielle conservée). Paiement à la résolution : 1{gagnant} − L par part, sans frais ; remise maker estimée à part (0,2 × frais preneur au même prix). Tests : `tests/test_polymarket_maker_live.py`.

Stratégies : (a) `signal` : côté favorisé par `gap_m30` (> 0 : Up), prix 0,48 / 0,49 / 0,50, pose à S−30 s, annulation à S, S+30 s, S+60 s ; (b) `two_sided` : achat Up et achat Down à 0,49 posés à S−60 s, mêmes annulations ; (c) `fair_value` : de S+10 s à E−10 s, toutes les 10 s, juste valeur p̂ = Φ(d/σ) avec d = log(TWAP final estimé / TWAP60(S) Binance) et σ = EWMA 1 s (variance du TWAP restant), bid Up à p̂ − 1 c et bid Down à (1 − p̂) − 1 c, plafonnés à 1 c sous le meilleur prix opposé (jamais croisants), conservés tant que le niveau ne change pas.

## 5. Résultats par stratégie

![Aucune configuration maker n'a un P&L significativement positif sur 18 marchés (au mieux +6,2 c par part placée, IC −50,0 ; +50,0 c)](pnl_maker_par_strategie.png)

| stratégie | config. | ordres | marchés | croisants (écartés) | exécutés | via la file | via traversée | via croisement | file médiane | délai médian (s) | P&L / part placée (c) | IC bas | IC haut | P&L / part exécutée (c) | remise (c) | gain si exécuté | gain sinon |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| (a) signal TWAP partiel à S−30 s | 0.48/S+0 | 14 | 14 | 4 | 57 % | 62 % | 12 % | 25 % | 71 | 15,3 | −11,3 | −24,0 | +0,0 | −19,4 | 0,35 | 29 % | 60 % |
| (a) signal TWAP partiel à S−30 s | 0.48/S+30 | 14 | 14 | 4 | 71 % | 50 % | 20 % | 30 % | 71 | 17,4 | −7,0 | −24,0 | +1,7 | −10,5 | 0,35 | 38 % | 50 % |
| (a) signal TWAP partiel à S−30 s | 0.48/S+60 | 14 | 14 | 4 | 79 % | 45 % | 18 % | 36 % | 71 | 18,5 | −2,7 | −24,0 | +9,1 | −3,6 | 0,35 | 44 % | 33 % |
| (a) signal TWAP partiel à S−30 s | 0.49/S+0 | 13 | 13 | 5 | 54 % | 57 % | 43 % | 0 % | 50 | 12,6 | −8,5 | −24,5 | +0,7 | −15,7 | 0,35 | 33 % | 60 % |
| (a) signal TWAP partiel à S−30 s | 0.49/S+30 | 13 | 13 | 5 | 85 % | 55 % | 36 % | 9 % | 50 | 14,2 | −3,7 | −36,8 | +17,7 | −4,6 | 0,35 | 44 % | 50 % |
| (a) signal TWAP partiel à S−30 s | 0.49/S+60 | 13 | 13 | 5 | 92 % | 50 % | 33 % | 17 % | 50 | 16,3 | −8,2 | −49,0 | +17,7 | −9,0 | 0,35 | 40 % | 100 % |
| (a) signal TWAP partiel à S−30 s | 0.50/S+0 | 9 | 9 | 9 | 44 % | 0 % | 50 % | 50 % | 50 | 13,4 | +0,0 | −33,3 | +25,0 | +0,0 | 0,35 | 50 % | 75 % |
| (a) signal TWAP partiel à S−30 s | 0.50/S+30 | 9 | 9 | 9 | 89 % | 12 % | 38 % | 50 % | 50 | 24,4 | +6,2 | −50,0 | +50,0 | +7,1 | 0,35 | 57 % | 100 % |
| (a) signal TWAP partiel à S−30 s | 0.50/S+60 | 9 | 9 | 9 | 89 % | 12 % | 38 % | 50 % | 50 | 24,4 | +6,2 | −50,0 | +50,0 | +7,1 | 0,35 | 57 % | 100 % |
| (b) deux côtés à 0,49 | 0.49/S+0 | 31 | 18 | 5 | 65 % | 50 % | 40 % | 10 % | 70 | 35,7 | −3,4 | −10,8 | +25,5 | −5,2 | 0,35 | 44 % | 89 % |
| (b) deux côtés à 0,49 | 0.49/S+30 | 31 | 18 | 5 | 77 % | 54 % | 33 % | 12 % | 70 | 39,4 | −1,2 | −11,8 | +25,5 | −1,6 | 0,35 | 47 % | 100 % |
| (b) deux côtés à 0,49 | 0.49/S+60 | 31 | 18 | 5 | 77 % | 54 % | 33 % | 12 % | 70 | 39,4 | −1,2 | −11,8 | +25,5 | −1,6 | 0,35 | 47 % | 100 % |
| (c) juste valeur ± 1 c | ± 1 c | 728 | 18 | 38 | 39 % | 20 % | 3 % | 77 % | 147 | 2,7 | −0,3 | −10,8 | +8,7 | −0,8 | 0,28 | 49 % | 54 % |

(b) par **paire** Up + Down, sur les marchés où les deux ordres sont posables (quand le milieu n'est pas à 0,50, l'un des deux croise le carnet et le tableau précédent ne garde que l'autre) : P&L de la paire en cents par part (les deux exécutés = écart de 2 c capté, un seul = position directionnelle, le plus souvent du côté perdant) :

| config. | paires | les deux exécutés | un seul | aucun | P&L / paire (c) | IC bas | IC haut |
|---|---|---|---|---|---|---|---|
| 0.49/S+0 | 13 | 38 % | 62 % | 0 % | −18,6 | −23,5 | −11,3 |
| 0.49/S+30 | 13 | 69 % | 31 % | 0 % | −13,3 | −23,5 | −6,5 |
| 0.49/S+60 | 13 | 69 % | 31 % | 0 % | −13,3 | −23,5 | −6,5 |

Par côté acheté (a et b) :

| stratégie | côté | ordres | croisants | exécutés | P&L / part placée (c) | IC bas | IC haut | gain si exécuté | gain sinon |
|---|---|---|---|---|---|---|---|---|---|
| signal | down | 75 | 42 | 80 % | −20,8 | −40,0 | −5,0 | 23 % | 58 % |
| two_sided | up | 54 | 0 | 61 % | +10,6 | +0,5 | +25,5 | 67 % | 100 % |
| two_sided | down | 39 | 15 | 90 % | −20,8 | −21,3 | −19,9 | 26 % | 67 % |
| signal | up | 33 | 12 | 58 % | +26,3 | −8,2 | +51,0 | 95 % | 64 % |

![(a) Signal maker posé à S−30 s : exécuté dans 52 % des cas avant l'annulation à S ; attendre jusqu'à S+60 s ajoute 35 points d'exécution ; le prix (0,48 à 0,50) change peu (8 points)](execution_signal.png)

### 1re moitié / 2e moitié

Règle fixée d'avance pour (a) : la configuration (prix, annulation) au meilleur P&L par part placée sur la 1re moitié des marchés (au moins 5 ordres) est **0.49/S+30** ; son résultat sur la 2e moitié :

| moitié | ordres | marchés | exécutés | P&L / part placée (c) | IC bas | IC haut | gain si exécuté | gain sinon |
|---|---|---|---|---|---|---|---|---|
| 1re moitié | 5 | 5 | 80 % | +0,8 | +0,0 | +1,0 | 50 % | 100 % |
| 2e moitié | 8 | 8 | 88 % | −7,5 | −36,8 | +51,0 | 40 % | 0 % |

Toutes configurations, par moitié (`fichier resume_par_moitie.csv`) : P&L par part placée (c) et taux d'exécution.

| stratégie | config. | moitié | ordres | exécutés | P&L / part placée (c) | IC bas | IC haut |
|---|---|---|---|---|---|---|---|
| signal | 0.48/S+0 | 1re moitié | 6 | 83 % | −6,7 | −8,0 | +0,0 |
| signal | 0.48/S+30 | 1re moitié | 6 | 83 % | −6,7 | −8,0 | +0,0 |
| signal | 0.48/S+60 | 1re moitié | 6 | 83 % | −6,7 | −8,0 | +0,0 |
| signal | 0.49/S+0 | 1re moitié | 5 | 80 % | +0,8 | +0,0 | +1,0 |
| signal | 0.49/S+30 | 1re moitié | 5 | 80 % | +0,8 | +0,0 | +1,0 |
| signal | 0.49/S+60 | 1re moitié | 5 | 80 % | +0,8 | +0,0 | +1,0 |
| signal | 0.50/S+0 | 1re moitié | 3 | 67 % | +33,3 | +0,0 | +50,0 |
| signal | 0.50/S+30 | 1re moitié | 3 | 67 % | +33,3 | +0,0 | +50,0 |
| signal | 0.50/S+60 | 1re moitié | 3 | 67 % | +33,3 | +0,0 | +50,0 |
| two_sided | 0.49/S+0 | 1re moitié | 13 | 69 % | +4,5 | −4,8 | +25,5 |
| two_sided | 0.49/S+30 | 1re moitié | 13 | 69 % | +4,5 | −4,8 | +25,5 |
| two_sided | 0.49/S+60 | 1re moitié | 13 | 69 % | +4,5 | −4,8 | +25,5 |
| fair_value | ± 1 c | 1re moitié | 391 | 39 % | +3,9 | +1,4 | +8,7 |
| signal | 0.48/S+0 | 2e moitié | 8 | 38 % | −16,0 | −24,0 | +0,0 |
| signal | 0.48/S+30 | 2e moitié | 8 | 62 % | −7,3 | −24,0 | +26,0 |
| signal | 0.48/S+60 | 2e moitié | 8 | 75 % | +1,3 | −24,0 | +52,0 |
| signal | 0.49/S+0 | 2e moitié | 8 | 38 % | −16,3 | −24,5 | +0,0 |
| signal | 0.49/S+30 | 2e moitié | 8 | 88 % | −7,5 | −36,8 | +51,0 |
| signal | 0.49/S+60 | 2e moitié | 8 | 100 % | −15,7 | −49,0 | +51,0 |
| signal | 0.50/S+0 | 2e moitié | 6 | 33 % | −20,0 | −33,3 | +0,0 |
| signal | 0.50/S+30 | 2e moitié | 6 | 100 % | −10,0 | −50,0 | +50,0 |
| signal | 0.50/S+60 | 2e moitié | 6 | 100 % | −10,0 | −50,0 | +50,0 |
| two_sided | 0.49/S+0 | 2e moitié | 18 | 61 % | −11,9 | −24,5 | −5,6 |
| two_sided | 0.49/S+30 | 2e moitié | 18 | 83 % | −7,5 | −11,8 | +1,0 |
| two_sided | 0.49/S+60 | 2e moitié | 18 | 83 % | −7,5 | −11,8 | +1,0 |
| fair_value | ± 1 c | 2e moitié | 337 | 38 % | −8,3 | −10,8 | −4,0 |

## 6. La juste valeur Φ(d/σ) contre le prix du marché pendant la fenêtre

Brier de la juste valeur (Binance 1 s, information close à t) et du milieu du carnet au même instant, contre l'issue officielle ; ΔBrier < 0 = la juste valeur fait mieux ; IC bootstrap par créneau.

| durée | t_rel_s | n | Brier juste valeur | Brier marché | ΔBrier | IC bas | IC haut | justesse juste valeur | justesse marché |
|---|---|---|---|---|---|---|---|---|---|
| toutes | 30 | 15 | 0,244 | 0,281 | −0,037 | −0,063 | −0,019 | 53 % | 47 % |
| toutes | 60 | 15 | 0,271 | 0,244 | +0,027 | −0,045 | +0,084 | 40 % | 60 % |
| toutes | 150 | 14 | 0,209 | 0,166 | +0,043 | −0,075 | +0,208 | 57 % | 79 % |
| toutes | 240 | 12 | 0,214 | 0,092 | +0,123 | +0,001 | +0,205 | 75 % | 92 % |
| 5m | 30 | 14 | 0,249 | 0,288 | −0,040 | −0,063 | −0,019 | 50 % | 43 % |
| 5m | 60 | 14 | 0,279 | 0,248 | +0,030 | −0,045 | +0,102 | 36 % | 57 % |
| 5m | 150 | 14 | 0,209 | 0,166 | +0,043 | −0,075 | +0,208 | 57 % | 79 % |
| 5m | 240 | 12 | 0,214 | 0,092 | +0,123 | +0,001 | +0,205 | 75 % | 92 % |

## 7. À quoi servirait l'amplitude prévue par TimesFM ?

TimesFM ne prédit pas le sens (AUC ≈ 0,51), mais ses quantiles sont calibrés en amplitude (couverture [q10, q90] de 0,76 à 0,84 pour 0,80 visé). Dans ce cadre, l'amplitude a exactement une place : **le σ de la juste valeur** Φ(d/σ) de la stratégie (c), donc le **prix auquel un maker doit coter**. Quand l'écart d entre le TWAP estimé et le seuil est connu (il l'est mécaniquement dès S−30 s, et de plus en plus pendant la fenêtre), la probabilité de Up ne dépend plus que de la dispersion attendue du reste de la fenêtre : un σ trop petit fait coter 0,90 ce qui vaut 0,70, un σ trop grand laisse de l'argent sur la table. Ici σ vient d'une EWMA de rendements 1 s (60 s de demi-vie) ; TimesFM (contexte 1 min, horizon 5 ou 15 pas) fournirait un σ **conditionnel** à l'horizon exact de la fenêtre, utile surtout sur le 15m où l'EWMA 1 s extrapole mal, et pour dimensionner l'ordre (taille, distance au milieu) plutôt que pour choisir le côté. Le test à faire : remplacer σ_EWMA par (q90 − q10)/2,56 de TimesFM 2.5 (Apache-2.0) dans (c) et comparer le Brier du § 6 et le P&L du § 5 ; le § 6 donne déjà la référence à battre (le milieu du carnet). Rien de tout cela ne crée un avantage sur le sens : cela ne fait que cotiser correctement ce que l'on sait.

## 8. Limites

* **n = 18 marchés** sur une seule matinée UTC (régime de volatilité unique) ; 13 configurations testées sans correction pour tests multiples ; la meilleure configuration est choisie a posteriori. À relancer après 24 h de collecte.
* File d'attente : borne supérieure (les annulations devant nous ne sont vues que si le niveau affiché passe sous notre file) ; exécution par croisement supposée totale (optimiste si l'ordre adverse est plus petit que nous) ; le prix d'un `last_trade_price` est le niveau touché (prix moyen du preneur dans 93 % des cas) : une traversée est comptée à partir de ce prix.
* Nos ordres n'influencent pas les autres participants (pas de réaction des makers concurrents, pas de retrait du preneur). La latence est fixée à 300 ms.
* Signal (a) : bougies 1 s Binance closes à S−30 s ; le flux Chainlink retarde d'≈ 4 s sur Binance (diagnostic), non modélisé ; juste valeur (c) : TWAP60(S) Binance à la place de `priceToBeat` (écart de niveau Binance–Chainlink ≈ 3 pb), gaussienne, pas de saut.
* Issues : `resolved_up` lu sur gamma (rafraîchi par le collecteur pendant 6 h) ou sur l'événement `market_resolved` du WebSocket ; les marchés sans issue comptent pour l'exécution mais pas pour le P&L.

## 9. Fichiers et relance

| fichier | contenu |
|---|---|
| `marches.csv` | un marché par ligne : issue, `priceToBeat`, `gap_m30`, couverture, accord `best_bid_ask`, milieu / écart / profondeur à S−60, S−30, S, S+30 |
| `carnet_median.csv` | séries médianes (et quartiles) du carnet par cellule, de S−150 s à S+120 s |
| `profondeur.csv`, `file_attente.csv` | profondeur par instant et cellule ; file typique aux niveaux 0,48–0,52 avant / après S |
| `trades_stats.csv`, `trades.csv` | traversées, dépassements de profondeur, tailles ; tous les trades preneurs avec l'état du carnet d'avant |
| `ordres.csv`, `resume_strategies.csv`, `resume_par_moitie.csv`, `resume_par_cellule.csv`, `paires_two_sided.csv` | ordres simulés et résumés avec IC |
| `juste_valeur_controle.csv` | Brier juste valeur contre milieu du carnet |

Relancer (le collecteur peut tourner en même temps ; les marchés en cours sont ignorés) :

```bash
. .venv/bin/activate
pgrep -af live_collector || nohup python scripts/polymarket_live_collector.py --series btc:5m,btc:15m,eth:5m >> logs/live_collector.log 2>&1 &
python scripts/polymarket_maker_live.py               # --workers 2 --B 2000 par défaut ; --no-refresh pour ne pas interroger gamma
```
