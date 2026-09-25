# Sources de données et d'exécution crypto (BTC, SOL, ETH…) : état des lieux

*Rédigé le 25/09/2026. Les mesures notées « mesuré » viennent de requêtes faites depuis cet
environnement le 25/09/2026, entre 16 h 30 et 17 h UTC, avec le venv du projet. Les informations
réglementaires et tarifaires viennent de pages web consultées le même jour ; leur date est indiquée.
Ce conteneur passe par un proxy situé hors de France. Ce qui y est bloqué (451 Binance, 403 Bybit)
peut donc se comporter autrement depuis la France. Cela ne change rien au droit applicable.*

---

## 0. Résumé

1. **Recherche : les fichiers bulk Binance (`data.binance.vision`) restent la meilleure source, gratuite
   et complète.** Aucune barre 1 min ne manque sur BTC, SOL, ETH, XRP, BNB et DOGE de septembre 2025
   à août 2026 (mesuré). Depuis 2022, BTCUSDT n'a qu'un seul trou : **80 min le 24/03/2023** (12 h 40
   → 14 h 00 UTC). Sur 1 000 barres, les valeurs de l'API `data-api.binance.vision` sont identiques à
   celles des fichiers bulk (écart maximal 0).
2. **Pièges de format (mesuré).** Les klines *spot* n'ont **pas d'en-tête** et sont en **µs** depuis
   le 01/01/2025. Les klines *futures* ont un en-tête et sont en ms. Les fichiers `metrics` ont un
   en-tête mais **ne sont pas triés** dans la journée. Dans `fundingRate`, `calc_time` peut dévier de
   ±1 ms. En 2022, les ratios long/short « top traders » sont **vides presque toute l'année**.
   `bookTicker` s'arrête en **mars 2024**.
3. **Délais de publication (médiane mesurée).**
   * Fichiers journaliers spot : **≈ 2,2 h** après minuit UTC.
   * Fichiers journaliers futures (`metrics`, `klines`, `premiumIndex`) : **≈ 7 à 9 h**.
   * Fichiers mensuels spot : **0,3 à 11,6 jours** après la fin du mois. Il faut donc compléter le mois
     précédent avec les fichiers journaliers.
   * `fundingRate` n'existe qu'en fichier **mensuel** : le mois en cours n'est jamais disponible en bulk.
4. **⚠ Exécution : un résident français ne peut plus trader sur Binance.** Binance n'a pas obtenu de
   licence MiCA. Depuis le **1er juillet 2026**, il n'offre plus spot, marge ni futures dans l'UE ; seuls
   les retraits restent possibles. Les coûts de `config.COST_BPS` (Binance VIP0) ne correspondent donc
   à **aucune plateforme accessible légalement**, même si « 10 pb aller-retour taker sur perpétuels »
   reste un ordre de grandeur réaliste chez Kraken et OKX.
5. **Plateformes autorisées en France** (registre ESMA daté du 24/09/2026, avec passeport FR) :
   Coinbase (LU), Kraken (IE, et CY pour les dérivés), OKX (MT), Bybit EU (AT), Bitstamp, Crypto.com,
   Bitvavo, KuCoin EU, Gate… **Binance et Bitget n'y figurent pas.** Bitget a cessé de servir la France
   le 31/03/2026. L'**USDT** n'est plus négociable sur les plateformes MiCA depuis le 31/03/2025 :
   l'exécution passe par des paires **EUR / USDC** ou par des perpétuels réglés en USD.
6. **Coût aller-retour à battre (niveau de base, taker)**, auquel s'ajoute un spread de 0 à 2,5 pb
   (mesuré) :

   | Plateforme | Aller-retour taker |
   |---|---|
   | Kraken spot | **160 pb** |
   | Coinbase spot UE | **100 pb** |
   | OKX spot EEE | **40 pb** (20 pb maker) |
   | Bybit EU spot | **20 pb** |
   | Perpétuels Kraken EEE ou OKX X-Perps | **10 pb** (4 pb maker) |

   Sur juin–août 2026, le mouvement absolu moyen d'un BTC est de 8 pb à 5 min et de 14 pb à 15 min.
   À 10 pb de coût, le hit-rate nécessaire est **≥ 86 % sur BTC à 15 min** et **≥ 73 % sur SOL à
   15 min**. **Seuls les perpétuels, idéalement en ordres maker, laissent une marge
   théorique** (§3.4).
7. **Fiscalité.** Le PFU vaut **31,4 %** en 2026 (et non 30 %), à cause de la hausse de la CSG votée
   dans la LFSS 2026 (§3.6).
8. **Recommandation (§4).**
   * **Recherche** : bulk Binance + `data-api`, sans changement du contrat d'architecture.
   * **Production** : flux WebSocket de la plateforme d'exécution. Binance peut servir de signal
     « leader » en données publiques seulement.
   * **Exécution** : **Kraken, perpétuels EEE** (`PF_XBTUSD`, `PF_SOLUSD`, `PF_ETHUSD`, régulés MiFID
     par la CySEC, API complète et environnement de démo). **OKX EEE** (X-Perps + démo) en second
     choix.

---

## 1. Vérifications en direct

### 1.1 `data.binance.vision` : ce qui existe réellement

On liste les fichiers avec l'API S3 publique, qui renvoie `LastModified`, `ETag` et la taille :
`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/daily/klines/BTCUSDT/1m/`
(1 000 clés par page, paramètre `marker`). La page `https://data.binance.vision/?prefix=…` ne sert
que d'interface JavaScript. Chaque zip est accompagné d'un `.CHECKSUM` (SHA-256).

| Jeu de données (chemin) | En-tête CSV | Unité de temps | Fréquence | Historique (BTCUSDT) | Remarques (mesuré) |
|---|---|---|---|---|---|
| `spot/{monthly,daily}/klines/{SYM}/1m/` | **non** | **µs** depuis 2025-01 (16 chiffres) ; ms avant | 1 min | depuis 2017-08-17 | `close_time = open_time + 59 999 999` µs ; aussi en `1s` |
| `spot/{monthly,daily}/aggTrades/{SYM}/` | **non** | **µs** | tick | depuis 2017 | 8 à 22 Mo zippés par jour pour BTC |
| `spot/{monthly,daily}/trades/{SYM}/` | non | µs | tick | depuis 2017 | — |
| `futures/um/{monthly,daily}/klines/{SYM}/1m/` | **oui** | ms | 1 min | depuis 2019-12 | colonnes `…,count,taker_buy_volume,taker_buy_quote_volume,ignore` |
| `futures/um/daily/metrics/{SYM}/` | **oui** | chaîne `YYYY-MM-DD HH:MM:SS` (UTC) | **5 min** (288 par jour) | depuis 2020-09-01 | **lignes non triées** ; voir §1.3 |
| `futures/um/monthly/fundingRate/{SYM}/` | **oui** | ms | 8 h pour BTC et SOL (`funding_interval_hours`) | depuis 2020-01 | **mensuel uniquement** ; `calc_time` dévie de ±1 ms |
| `futures/um/daily/premiumIndexKlines/{SYM}/1m/` | oui | ms | 1 min | depuis 2019-12-24 | permet d'estimer le funding du mois en cours |
| `futures/um/daily/bookDepth/{SYM}/` | oui | chaîne datetime | ≈ 30 s (2 880 instantanés par jour) | — | profondeur cumulée à ±0,2 %, ±1…5 % (quantité et notionnel) |
| `futures/um/{daily,monthly}/bookTicker/` | — | — | — | **s'arrête le 2024-03-30** (journalier) et en 2024-04 (mensuel) | pas de `bookTicker` spot du tout |
| `futures/um/{daily,monthly}/aggTrades`, `trades`, `markPriceKlines`, `indexPriceKlines` | oui | ms | — | — | — |
| `futures/cm/daily/liquidationSnapshot/` | — | — | — | **s'arrête le 2024-10-14**, coin-M uniquement | aucune liquidation USDⓈ-M en bulk |
| `option/daily/BVOLIndex`, `EOHSummary` | — | — | — | — | indice de volatilité des options |

En-tête des `metrics` :
`create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,
sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio`.
Correspondance avec les colonnes du contrat :

| Colonne `metrics` | Colonne du contrat |
|---|---|
| `sum_open_interest` | `open_interest` |
| `sum_open_interest_value` | `open_interest_value` |
| `sum_toptrader_long_short_ratio` (ratio des positions) | `ls_ratio_top` |
| `count_long_short_ratio` (ratio des comptes) | `ls_ratio_accounts` |
| `sum_taker_long_short_vol_ratio` | `taker_ls_ratio` |

La sémantique « count = comptes, sum = positions » est déduite des noms de colonnes et des ordres de
grandeur (≈ 1,0 contre ≈ 2,0 pour BTC). Binance ne la documente pas.

Source officielle du passage aux µs : [binance-public-data README](https://github.com/binance/binance-public-data)
(« from January 1st 2025 onwards » pour le spot ; le futures reste en ms). Vérifié ici : le fichier
2024-12 est en ms (13 chiffres), les fichiers 2025-01 à 2026-09 sont en µs (16 chiffres).

### 1.2 Délais de publication (mesurés sur `LastModified`)

Méthode : 97 derniers fichiers journaliers et 20 derniers fichiers mensuels. `LastModified` donne la
date de la **dernière** écriture ; un fichier republié plus tard fausse donc les queues de
distribution.

| Fichier | Min | Médiane | P90 | Max |
|---|---|---|---|---|
| spot klines journalier (BTC, SOL) | 1,4 h | **2,2 h** | 2,9 h | 64 h (rare) |
| futures `premiumIndexKlines` journalier | 7,4 h | **9,4 h** | 11 h | — |
| futures `metrics` journalier | 6,5 h | **7,3 h** | ≫ (republications ≈ 24 jours plus tard) | — |
| futures klines / aggTrades / bookDepth journaliers | ≈ 7 h | ≈ 7,5–8,5 h | — | — |
| spot klines **mensuel** | 0,3 j | ≈ 3,4 j | ≈ 7 j | **11,6 j** (2025-03) |
| `fundingRate` mensuel | le 1er du mois suivant, vers 08 h – 09 h 45 UTC | | | |

Conséquences pour `data.py` :

* Les **mois complets** viennent du fichier mensuel s'il existe. Sinon on prend les **journaliers**,
  qui existent pour tous les jours depuis 2017 (3 326 fichiers BTC).
* Pour **aujourd'hui et hier avant ≈ 03 h UTC**, on prend l'API klines. Il faut au plus ≈ 27 h, soit
  2 requêtes.
* On invalide le cache d'un fichier quand son `ETag` change : les `metrics` sont parfois republiées.
* Le **funding du mois en cours** n'est pas en bulk (voir §1.5 pour les alternatives).
* En production, les `metrics` bulk arrivent avec **≈ 7 h de retard** : elles ne servent pas en temps
  réel.

### 1.3 Trous de données

**Klines spot 1 min**, en comparant chaque mois à une grille complète à la minute (mesuré) :

| Période | BTC | SOL | ETH | XRP | BNB | DOGE |
|---|---|---|---|---|---|---|
| 2025-09 → 2026-08 (12 mois) | **0 manquante** | 0 | 0 | 0 | 0 | 0 (11 barres à volume nul) |
| Août 2026 (le mois demandé) | 44 640 / 44 640 | idem | idem | — | — | — |

Minutes manquantes sur BTCUSDT, année par année :

| Année | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|---|---|---|
| Minutes manquantes | 20 897 | 5 177 | 1 764 | 1 252 | 993 | **0** | **80** | **0** | **0** |

Avant 2022, les trous correspondent à des maintenances nocturnes de 1 à 10 h, qui démarrent
typiquement à 01 h 59 UTC. Le trou de 2023 est la panne du 24/03/2023.

Aucun doublon n'apparaît. `targets.future_log_return` rend la cible NaN en cas de trou : l'effet est
négligeable après 2022. **Pour l'étude, commencer en 2023 au plus tôt.**

**`metrics` futures (5 min).**

* Du 25/08 au 24/09/2026 : 8 928 lignes sur 8 928 pour BTC et SOL, écart toujours de 5 min, aucun NaN.
* Sur tout l'historique BTC (2 215 fichiers, aucun jour absent) :
  * 2021 : 63 jours incomplets (468 pas de 5 min manquants) et des doublons (fichiers de 436 à
    576 lignes) ;
  * 2024-02-16 : 125 pas manquants ;
  * 2023 et 2025 : 3 pas manquants chacune ; 2026 : aucun.
* Colonnes vides :
  * `*_toptrader_*` : vides de **janvier à mi-décembre 2022** (sauf une partie de juin) ;
  * `sum_taker_long_short_vol_ratio` : vide de **décembre 2021 à mi-mai 2022** ;
  * l'open interest est complet.
* ⇒ Les indicateurs « dérivés » ne sont exploitables qu'**à partir de 2023**.

### 1.4 `data-api.binance.vision` (API REST données de marché seules)

| Point | Mesuré / documenté |
|---|---|
| `GET /api/v3/klines` | **1 000 barres maximum** (`limit=1500` renvoie 1 000). **Poids 2** d'après la [doc](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints) |
| Plafond (`exchangeInfo`) | `REQUEST_WEIGHT` **6 000 par minute et par IP**. `RAW_REQUESTS` 300 000 par 5 min |
| Suivi | en-têtes `x-mbx-used-weight`, `x-mbx-used-weight-1m`. Ce compteur est **partagé par l'IP de sortie** : d'autres agents l'avaient déjà porté à 116 |
| Dépassement | 429 avec `Retry-After`, puis ban 418 de 2 min à 3 jours ([doc](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information)) |
| Profondeur | `startTime=1500000000000` renvoie la première barre du 2017-08-17. Un an de 1 min ≈ 526 requêtes ≈ 1 052 de poids : **faisable en une minute**, en secours du bulk |
| Barre en cours | **incluse** (`close_time` dans le futur) : la supprimer |
| Unités | millisecondes. L'en-tête `X-MBX-TIME-UNIT: MICROSECOND` passe en µs |
| Autres endpoints OK | `depth`, `ticker/bookTicker`, `aggTrades`, `uiKlines` |
| Futures | `data-api…/fapi/*` renvoie 404 ; `fapi.binance.com` renvoie **451** ; `api.binance.com` renvoie 451 |
| WebSocket spot | `wss://data-stream.binance.vision/ws/btcusdt@kline_1m` **OK**, y compris en flux combinés `/stream?streams=…`. `stream.binance.com` renvoie 451 |
| WebSocket futures | `wss://fstream.binance.com/market/ws/btcusdt@markPrice` **OK** : mark price, index et **funding `r`** en temps réel, alors que le REST `fapi` est bloqué |
| Testnets | `testnet.binance.vision` renvoie 451 ; `testnet.binancefuture.com` répond 200 (sans intérêt pour un résident FR) |

Le module `websockets` 17.1 est déjà présent dans le venv. Dans ce conteneur, **seul le port 443
passe** : les endpoints en `:8443` ou `:9443` sont coupés par le proxy, pas par les plateformes.

### 1.5 Autres plateformes (mesuré)

**OKX** — `www.okx.com` et `eea.okx.com` répondent tous deux.

* `GET /api/v5/market/history-candles` : **300 barres par requête** (la doc indique 100). Historique
  1 min :
  * BTC-USDT : jusqu'à **janvier 2018** ;
  * SOL-USDT : depuis 2021 ;
  * BTC-USDT-SWAP : depuis 2020 au moins.
* Limite documentée : 20 requêtes / 2 s. 25 requêtes en 6 s sont passées sans erreur.
* `funding-rate-history` : environ **3 mois** (287 points).
* Statistiques « rubik » (OI et volumes, pas de 5 min) : ≈ **2 jours** (576 points).
* WebSocket : `wss://ws.okx.com/ws/v5/public` et `wss://wseea.okx.com/ws/v5/public` (port 443) sont OK.
* Bulk : `https://static.okx.com/cdn/okex/traderecords/trades/daily/YYYYMMDD/{INST}-trades-YYYY-MM-DD.zip` est OK.
* **X-Perps (EEE)** : `BTC-USD_UM_XPERP-310404`, `SOL-USD_UM_XPERP-310404` et `ETH-…`.
  * Cotés depuis le 30/03 – 01/04/2026, échéance 04/04/2031.
  * Volume 24 h : ≈ 3 200 BTC et ≈ 350 000 SOL.
  * Spread : 1 tick.

**Coinbase Exchange** (`api.exchange.coinbase.com`)

* Au plus **300 bougies par requête** (au-delà : erreur 400).
* Historique : BTC-USD depuis **2015**, SOL-USD depuis fin 2021.
* **Une minute sans trade n'a pas de bougie** : en 2015, 229 bougies sur 241 attendues.
* L'API Advanced publique donne 350 bougies par requête.
* WebSocket `ws-feed` OK ; sandbox Exchange OK.

**Kraken spot**

* `OHLC` ne renvoie **que les 720 dernières barres** (12 h en 1 min) : `since` est ignoré au-delà.
* L'historique complet passe par l'endpoint `Trades` (depuis 2013, 1 000 trades par appel) ou par les
  CSV OHLCVT téléchargeables, mis à jour chaque trimestre
  ([article support](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data)).

**Kraken Futures** (`futures.kraken.com`)

* API *charts* : `/api/charts/v1/trade/PF_XBTUSD/1m?from=&to=`, 1 min disponible **au moins depuis
  2022**.
* `historicalfundingrates` : funding **horaire** sur 1 an (8 785 points). Utiliser
  `relativeFundingRate` : `fundingRate` est une valeur absolue.
* WebSocket `wss://futures.kraken.com/ws/v1` OK : ticker avec `funding_rate_prediction`.
* Liquidité :
  * `PF_XBTUSD` : spread 0,12 pb, ≈ 360 M$ sur 24 h ;
  * `PF_SOLUSD` : spread 2,5 pb, ≈ 87 M$ ;
  * `PF_ETHUSD` : spread 0,4 pb, ≈ 118 M$.

**Autres sources**

| Source | Résultat |
|---|---|
| Bybit | REST **403** (« CloudFront… block access from your country »), aussi sur `api.bybit.eu`. WebSocket OK. Bulk `public.bybit.com/trading/` et `/spot/` OK |
| Bitget | REST OK ; 1 min futures jusqu'en 2021 |
| Bitvavo | REST OK |
| CoinDesk Data / CryptoCompare | **401** (clé requise) |
| Coinalyze | 401 (clé gratuite requise) |
| CoinGlass | clé requise |
| Tardis.dev | API et **jeux gratuits du 1er de chaque mois** OK (`datasets.tardis.dev/v1/binance/trades/2026/09/01/BTCUSDT.csv.gz`) |

### 1.6 Binance est-il représentatif des plateformes où l'on exécutera ?

Test mesuré sur les 12 à 14 dernières heures en 1 min, en comparant BTCUSDT et SOLUSDT Binance aux
paires EUR et USDC d'autres plateformes.

**Corrélation des rendements 1 min**

| Plateforme | BTC | SOL |
|---|---|---|
| Kraken (paires EUR) | 0,93 | 0,93 |
| OKX (BTC-EUR, SOL-EUR) | 0,86 | 0,88 |
| OKX (BTC-USDC, SOL-USDC) | 0,91 | 0,98 |
| Coinbase (paires EUR) | 0,91 | 0,91 |

**Accord de signe à 5 min** : **88 à 98 %** selon les paires.
**Corrélation retardée** corr(r_plateforme(t), r_Binance(t−1)) : **0,08 à 0,22**.

Les carnets EUR et USDC **suivent Binance avec un retard partiel d'environ une minute**. Ce retard est
en partie un artefact du « dernier prix » dans des carnets moins actifs. C'est une piste d'indicateur
(`lead_lag_binance`) à confirmer sur un historique long. Sur une seule demi-journée, ce n'est pas
concluant.

---

## 2. Comparatif des sources : historique et temps réel

| Source | Coût | Historique 1 min (grosses paires) | Dérivés (funding, OI, L/S, liquidations) | Temps réel | Limites / remarques |
|---|---|---|---|---|---|
| **Binance bulk** `data.binance.vision` | **gratuit**, sans clé ni limite pratique | 2017 → J−1 (spot), 2019 → J−1 (futures), klines 1 s, trades, aggTrades | funding (mensuel), metrics 5 min (OI, L/S, taker), premium index, bookDepth 30 s. **Pas de liquidations USDⓈ-M** | non (J−1, avec 2 à 9 h de retard) | formats hétérogènes (§1.1). Données d'une plateforme **inaccessible pour trader** depuis la France |
| **Binance API** `data-api.binance.vision` + `data-stream.binance.vision` | gratuit | 1 000 barres par requête, historique complet | spot seulement (REST). Funding en temps réel via `fstream…/market/ws` | **oui** (WebSocket) | 6 000 de poids par minute et par IP. Accès depuis la France non garanti à terme |
| **OKX** REST / WebSocket (`eea.okx.com`) | gratuit | 1 min depuis 2018 (300 barres par requête, 20 req / 2 s) | funding 3 mois, OI et taker ≈ 2 jours à 5 min. Bulk trades journaliers | **oui** | [doc v5](https://www.okx.com/docs-v5/en/). Plateforme **d'exécution possible** |
| **Coinbase** Exchange / Advanced | gratuit | 1 min depuis 2015 (300 ou 350 par requête), bougies creuses | aucun côté public | oui (`ws-feed`) | paires USD/EUR/USDC |
| **Kraken** spot + Futures | gratuit | spot : 720 barres en REST, CSV trimestriels. **Perpétuels : charts 1 min depuis 2022** | funding horaire sur 1 an, OI via ticker | oui | Plateforme **d'exécution recommandée** |
| **Bybit** | gratuit | bulk trades (`public.bybit.com`) | via REST (bloqué ici) | WebSocket OK | REST géo-bloqué depuis ce conteneur |
| **CoinDesk Data** (ex-CryptoCompare) | **l'offre gratuite a disparu le 21/05/2026** ; tarifs sur devis ([annonce du 17/04/2026](https://data.coindesk.com/blogs/changes-to-coindesk-data-indices-api-free-tier-access)) | minute limitée à 7 jours hors offre entreprise | agrégats | oui | sans intérêt pour nous |
| **Kaiko** | sur devis, ≈ 9,5 k$ à 55 k$ par an ([Vendr](https://www.vendr.com/buyer-guides/kaiko)) ; [page tarifs](https://www.kaiko.com/about-kaiko/pricing-and-contracts) | tick et carnets, institutionnel | oui | oui | surdimensionné |
| **Tardis.dev** | Academic **350 $/mois** (perpétuels) à 650 $ (toutes plateformes) ; Solo 700 à 1 200 $/mois ; 300 $ minimum par mois ([tarifs](https://tardis.dev/)). **1er jour de chaque mois gratuit** | tick par tick, carnets L2 complets, depuis 2019 | funding, OI, **liquidations**, ratios L/S Binance | replay et temps réel (client open source) | la référence pour la microstructure, si un jour on en a besoin |
| **Amberdata** | Startup ou Enterprise, sur devis ([pricing](https://www.amberdata.io/pricing)) | oui | oui (outil d'analyse des dérivés gratuit) | oui | institutionnel |
| **CoinAPI** | 25 $ de crédits gratuits par mois, puis Startup 79 $/mois, Streamer 249 $, Pro 599 $ ([tarifs](https://www.coinapi.io/market-data-api/pricing)) | OHLCV multi-plateformes | taux | oui | facturation au crédit ou au Go |
| **Coinalyze** | **gratuit** avec clé, 40 appels/min ([doc](https://api.coinalyze.net/v1/doc/)) | **seulement 1 500 à 2 000 points** en intraday (1 min à 12 h), purgés chaque jour | OI, funding, **funding prédit**, **liquidations**, L/S, agrégés par plateforme | quasi temps réel | à **collecter en continu** si on veut les liquidations |
| **CoinGlass** | Hobbyist 29 $/mois, Startup 79 $, Standard 299 $, Pro 699 $ ([tarifs](https://www.coinglass.com/pricing)) | 1 min limité à 6 jours (Hobbyist) ou 12 jours (Startup) | liquidations, OI, funding, L/S sur plus de 30 plateformes | ≤ 1 min | payant et historique intraday court |

**Lecture.** Pour **l'historique**, rien ne bat le bulk Binance, qui est gratuit, complet et contient
le flux taker. Pour le **temps réel en production**, il faut les WebSocket de la plateforme
d'exécution, plus éventuellement Binance comme signal leader. Pour les **liquidations**, aucune source
gratuite ne fournit d'historique intraday : on peut collecter Coinalyze nous-mêmes à partir de
maintenant, ou payer Tardis ou CoinGlass.

---

## 3. Exécution pour un particulier résident en France (septembre 2026)

### 3.1 Cadre : fin du régime PSAN le 1er juillet 2026

* Le régime transitoire PSAN s'est terminé le **1er juillet 2026**. Un prestataire sans agrément MiCA
  (« CASP ») doit cesser ses services en France, sous peine de 2 ans d'emprisonnement et de 30 000 €
  d'amende
  ([AMF](https://www.amf-france.org/en/news-publications/news/amf-reminds-digital-asset-service-providers-transitional-period-allowing-them-continue-providing),
  [Crassula](https://crassula.io/guides/licenses/france-psan-mica/)).
* **Registre intérimaire ESMA** (`CASPS.csv`, `Last-Modified` du 24/09/2026, 362 CASP, dont 36
  agréés par l'AMF) :
  [page ESMA MiCA](https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/markets-crypto-assets-regulation-mica),
  [CSV](https://www.esma.europa.eu/sites/default/files/2024-12/CASPS.csv).
  Liste blanche AMF : [amf-france.org](https://www.amf-france.org/en/warnings/white-lists).

| Plateforme | Entité agréée MiCA | Autorité, date | Passeport FR | Services clés |
|---|---|---|---|---|
| **Kraken** | Payward Europe Solutions Ltd | CBI (IE), 25/06/2025 | oui | conservation, échange, exécution d'ordres… (la plateforme de négociation elle-même est portée par Payward Global Solutions : IE et CY) |
| **Coinbase** | Coinbase Luxembourg S.A. | CSSF (LU), 20/06/2025 | oui | échange, exécution d'ordres |
| **OKX** | OKX Europe Ltd | MFSA (MT), 27/01/2025 | oui | plateforme de négociation, échange, exécution |
| **Bybit** | Bybit EU GmbH | FMA (AT), 28/05/2025 | oui | échange ; **pas de dérivés** |
| Bitstamp | Bitstamp Europe S.A. | CSSF, 15/05/2025 | oui | plateforme de négociation |
| Crypto.com | Foris DAX MT Ltd | MFSA, 27/01/2025 | oui | — |
| Bitvavo | Bitvavo B.V. | AFM (NL), 26/06/2025 | oui | paires EUR |
| KuCoin EU, Gate, Gemini, Bitpanda, eToro, Revolut, Trade Republic… | — | — | oui | — |
| Coinhouse, Paymium, Meria, Deblock | agrément **AMF** | 2025–2026 | — | — |
| **Binance** | **absent** | — | — | — |
| **Bitget** | **absent** (dossier en cours auprès de la FMA) | — | — | — |

* **Binance**
  * A retiré sa demande en Grèce le 24/06/2026.
  * Depuis le **1er juillet 2026**, les utilisateurs de l'UE, France comprise, n'ont plus accès au
    spot, à la marge ni aux futures. **Seuls les retraits restent ouverts**
    ([CoinDesk, 26/06/2026](https://www.coindesk.com/policy/2026/06/26/binance-tells-eu-users-it-will-no-longer-provide-services-after-failing-to-secure-mica-license),
    [Cryptonomist, 06/07/2026](https://en.cryptonomist.ch/2026/07/06/binance-mica-license-failure/)).
  * Binance vise désormais un agrément AMF
    ([Cointribune, septembre 2026](https://fr.tradingview.com/news/cointribune:7e53713eab858:0/)).
  * **Ne pas construire l'exécution sur Binance.**
* **Bitget** : plus de nouveaux ordres pour les résidents français depuis le 16/03/2026, positions
  liquidées le 31/03/2026
  ([avis Bitget du 16/01/2026](https://www.bitget.com/support/articles/12560603848109)).
* **USDT** : délisté de toutes les plateformes MiCA de l'EEE depuis le 31/03/2025. Tether n'est pas
  émetteur agréé
  ([LeoDex](https://leodex.io/learn/delistings/usdt-delisted-europe)).
  ⇒ Il faut exécuter sur **BTC-EUR, BTC-USDC ou des perpétuels réglés en USD**, pas sur BTCUSDT
  (voir §1.6 pour la transférabilité des signaux).

### 3.2 Dérivés (perpétuels) : accessibles, mais sous MiFID II

* MiCA ne couvre pas les dérivés : un perpétuel est un **instrument financier MiFID II**.
* **ESMA, déclaration du 24/02/2026** : les dérivés vendus comme « perpetual futures » relèvent
  probablement des mesures d'intervention sur les **CFD**. Celles-ci imposent un effet de levier
  plafonné à **2:1 pour les sous-jacents crypto en retail**, un avertissement de risque, une clôture
  de position sur marge et une protection contre le solde négatif
  ([PDF ESMA35-243228190-8024](https://www.esma.europa.eu/sites/default/files/2026-02/ESMA35-243228190-8024_-_Public_statement_on_derivatives_in_scope_of_the_CFD_product_intervention_measures.pdf),
  [analyse Finance Magnates](https://www.financemagnates.com/forex/10x-down-to-2x-has-europe-killed-crypto-perps-even-before-it-started/)).
* La Commission européenne consulte sur le rattachement des perpétuels à MiFID ou à MiCA. Les réponses
  sont attendues le **30/09/2026**
  ([consultation](https://finance.ec.europa.eu/document/download/62be7015-f066-4fac-b74e-71bacdbcc9f5_en?filename=2026-mica-review-targeted-consultation-document_en.pdf)).
  Le cadre peut donc encore bouger.

Offres accessibles depuis la France (septembre 2026) :

| Offre | Entité / licence | Contrats | Levier retail | Funding | Frais de base (maker/taker) |
|---|---|---|---|---|---|
| **Kraken Pro, perpétuels EEE** | Payward Europe Digital Solutions (CY) Ltd, **CySEC 342/17**, MiFID II | `PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD` (+ échéances fixes) | jusqu'à **10x**, après test d'adéquation | **horaire** | **0,02 % / 0,05 %** ([grille](https://www.kraken.com/features/fee-schedule)) |
| **OKX X-Perps** | OKX, entité MiFID maltaise | BTC, ETH, SOL (`*-USD_UM_XPERP-310404`), futures à 5 ans | **2x** en retail (plus sur statut professionnel) d'après [cex101](https://cex101.com/en/articles/okx-x-perps-europe-regulated-trading-guide/) | 8 h | **0,02 % / 0,05 %** ([OKX, maj. 01/09/2026](https://www.okx.com/en-eu/help/okx-x-perps-eea-fees-overview)) |
| **Coinbase, futures « perpetual-style » UE** | Coinbase Financial Services Europe (CY), MiFID | BTC, SOL… futures à 5 ans | 10x annoncé en mars 2026 | horaire | non trouvés pour l'UE ([annonce du 09/03/2026](https://www.coinbase.com/blog/futures-contracts-europe)) |
| Bybit EU | MiCA seulement | **pas de perpétuels** | — | — | — |

Contrats Kraken EEE et levier : [spécifications, maj. 24/09/2026](https://support.kraken.com/articles/perpetual-contract-specifications-for-clients-in-the-eea).

Pour un bot de direction, l'intérêt des perpétuels **n'est pas le levier**. Ils permettent de **vendre
à découvert** une prévision de baisse, ce que le spot sans marge ne permet pas, et ils coûtent 4 à
8 fois moins cher que le spot chez ces plateformes. Un plafond à 2x suffit largement. Le funding
horaire vaut ≈ 0,1 pb par heure en régime normal : il est négligeable sur 15 min.

### 3.3 Frais réels au niveau de base et coût aller-retour

| Plateforme / produit | Maker | Taker | **Aller-retour taker** | Aller-retour maker | Source (date) |
|---|---|---|---|---|---|
| Kraken Pro spot (palier 1, 0 $ ; depuis le 09/07/2026) | 0,40 % | 0,80 % | **160 pb** | 80 pb | [Kraken, juillet 2026](https://support.kraken.com/articles/cross-platform-fee-tier-changes) |
| Kraken Pro spot, palier 2 (≥ 2,5 k$ de volume sur 30 jours) | 0,30 % | 0,60 % | 120 pb | 60 pb | idem |
| Coinbase Advanced spot, UE (depuis le 16/09/2026) | 0,25 % | 0,50 % | **100 pb** | 50 pb | [CryptoDaily, septembre 2026](https://cryptodaily.co.uk/2026/09/coinbase-advanced-fees-usdc-vip-tiers) (aide Coinbase inaccessible : 403) |
| OKX spot EEE, compte spot seul, Regular (depuis le 25/09/2026) | 0,10 % | 0,20 % | **40 pb** | 20 pb | [OKX, avis du 11/09/2026](https://www.okx.com/en-eu/help/important-notice-upcoming-spot-fee-adjustment-eea) |
| Bybit EU spot, crypto/crypto (paires fiat : 0,15 / 0,20 %) | 0,10 % | 0,10 % | **20 pb** | 20 pb | [Bybit EU](https://learn.bybit.eu/en-EU/essential-guides/bybit-trading-fees) (page en 503 lors de la vérification ; chiffres repris d'une recherche d'août 2026) |
| Bitvavo (paires EUR, < 100 k€ par mois) | 0,15 % | 0,25 % | 50 pb | 30 pb | [Bitvavo](https://bitvavo.com/en/fees) |
| **Kraken, perpétuels EEE** | 0,02 % | 0,05 % | **10 pb** | **4 pb** | [Kraken](https://www.kraken.com/features/fee-schedule) |
| **OKX X-Perps** | 0,02 % | 0,05 % | **10 pb** | **4 pb** | [OKX](https://www.okx.com/en-eu/help/okx-x-perps-eea-fees-overview) |
| *(pour mémoire) Binance futures VIP0, inaccessible* | 0,02 % | 0,05 % | 10 pb | 4 pb | `config.COST_BPS` |

**Spread** (mesuré, médiane de 8 relevés sur 16 s) :

| Carnet | Spread médian |
|---|---|
| BTC sur OKX, Kraken, Bitvavo | 0,01 à 0,14 pb |
| BTC-EUR sur Coinbase | 1,1 pb |
| SOL (tous les carnets) | 0,8 à 1,9 pb (≈ 1 tick) |
| `PF_SOLUSD` | 2,5 pb |

Aux tailles d'un particulier, le spread est **de second ordre** face aux frais, sauf sur SOL en perpétuel.

### 3.4 Ce que ces coûts imposent au modèle (mesuré sur juin–août 2026, Binance 1 min)

Mouvement absolu |log(close[t+h] / close[t])| :

| Actif | h = 5 min (médiane / moyenne) | h = 10 min | h = 15 min |
|---|---|---|---|
| BTC | 5,1 / 8,1 pb | 7,3 / 11,4 pb | 9,0 / 14,0 pb |
| ETH | 6,6 / 10,5 pb | 9,3 / 14,7 pb | 11,4 / 18,1 pb |
| SOL | 8,2 / 12,7 pb | 11,7 / 17,9 pb | 14,1 / 21,9 pb |

On suppose que le fait d'avoir raison ne dépend pas de l'amplitude. L'espérance nette d'un trade vaut
alors (2p − 1)·E|r| − c. Le **hit-rate de rentabilité** vaut donc p\* = 0,5 + c / (2·E|r|) :

| Coût aller-retour c | BTC 5 / 10 / 15 min | ETH 5 / 10 / 15 min | SOL 5 / 10 / 15 min |
|---|---|---|---|
| 4 pb (perpétuel maker/maker) | 75 % / 68 % / 64 % | 69 % / 64 % / 61 % | 66 % / 61 % / 59 % |
| 10 pb (perpétuel taker/taker) | impossible / 94 % / 86 % | 98 % / 84 % / 78 % | 89 % / 78 % / 73 % |
| 20 pb (Bybit EU spot) | impossible | impossible | impossible, sauf SOL à 15 min : 96 % |
| ≥ 40 pb (OKX, Coinbase, Kraken spot) | impossible | impossible | impossible |

Conséquences :

* Un signal inconditionnel à 52–55 % (ordre de grandeur de la littérature, cf. `timesfm.md`) **ne
  couvre aucun de ces coûts**.
* Il faut de la **sélectivité** : ne trader que quand l'amplitude prévue est grande (quantiles
  TimesFM, volatilité), car E|r| conditionnel est alors bien plus élevé.
* Il faut aussi de l'**exécution maker**, avec les risques de non-exécution et de sélection adverse
  qu'elle implique.
* La mini-stratégie (`backtest.toy_strategy`) devrait donc tester **4, 10 et 20 pb**, et le seuil de
  probabilité devrait être balayé.

### 3.5 Testnet / paper trading

| Plateforme | Environnement de test | Remarques |
|---|---|---|
| **Kraken Futures** | **démo en libre-service** `demo-futures.kraken.com`, identifiants séparés, mêmes endpoints que la production ([support](https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives)) | idéal pour roder le bot sur `PF_XBTUSD` et `PF_SOLUSD` |
| Kraken spot | pas de sandbox publique : environnement **sur demande**, ou `validate=true` sur `AddOrder` ([support](https://support.kraken.com/hc/en-us/articles/360000919926-Does-Kraken-offer-an-API-test-environment-)) | — |
| **OKX** (EEE) | **Demo trading** : en-tête `x-simulated-trading: 1`, clé API de démo ; REST sur `eea.okx.com`, WebSocket sur `wss://wseeapap.okx.com:8443/ws/v5/{public,private}` ([aide OKX Europe](https://www.okx.com/en-eu/help/how-to-use-demo-trading), [endpoints EEE](https://github.com/nautechsystems/nautilus_trader/issues/4250)) | ⚠ une clé créée sur le sous-domaine EEE ne marche que sur celui-ci |
| Coinbase Advanced | sandbox à **réponses statiques** (`api-sandbox.coinbase.com`, comptes et ordres seulement) ([doc](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox)) | ne simule pas de marché |
| Bybit | testnet et démo pour l'entité globale ; rien trouvé pour Bybit EU | — |

Dans tous les cas, on recommande un **paper trading « maison »** : flux WebSocket réel de la
plateforme cible, simulateur de frais maker/taker et de latence, journal des ordres. Il complète la
démo de la plateforme, dont la file d'attente et les exécutions ne sont pas réalistes.

### 3.6 Fiscalité (particulier, 2026)

* **PFU de 31,4 %** (12,8 % d'IR et 18,6 % de prélèvements sociaux, après la hausse de la CSG de la
  LFSS 2026). Il s'applique aux cessions de crypto contre des euros ou contre des biens et services
  (art. 150 VH bis), au-delà de 305 € de cessions par an. L'option pour le barème reste possible
  ([impots.gouv](https://www.impots.gouv.fr/particulier/questions/jai-realise-une-plus-value-mobiliere-comment-est-elle-imposee),
  [Cryptoactu](https://cryptoactu.com/fiscalite-cryptomonnaies-declaration-impot/)).
* Les **échanges crypto contre crypto, stablecoins compris, sont en sursis d'imposition**. Les gains
  sur perpétuels relèvent en principe du régime des instruments financiers à terme, lui aussi au PFU
  (à faire confirmer). Il faut déclarer les comptes à l'étranger (formulaire 3916-bis). Les données
  remontent automatiquement au fisc via DAC8 depuis le 01/01/2026.
* **Risque de requalification en BNC** en cas de trading « habituel » : le recours à un bot est l'un
  des critères qualitatifs, même s'il ne suffit pas seul. Le taux marginal plus les prélèvements
  sociaux peut alors dépasser 60 %. Consulter un fiscaliste avant de passer en réel.

---

## 4. Recommandations

### 4.1 Recherche (maintenant, phase 1)

1. **Garder le contrat de `ARCHITECTURE.md`** : bulk Binance spot 1 min, `data-api` pour les
   dernières heures, `metrics` et `fundingRate` futures. Points d'implémentation vérifiés :
   * **Unité** : détecter à la magnitude (> 1e15 → µs, sinon ms) pour chaque fichier.
   * **En-tête** : tester si la première cellule est numérique (spot : pas d'en-tête ; futures,
     `metrics`, `fundingRate` : en-tête).
   * **`metrics`** : `pd.to_datetime(create_time, utc=True)`, **trier**, dédoublonner (2021).
     Traiter les NaN de 2022 comme « indisponible ». Décalage de disponibilité de +5 min (contrat).
   * **`fundingRate`** : `calc_time` en ms, arrondi à la seconde (±1 ms). Garder
     `funding_interval_hours`, qui peut passer à 4 h ou 1 h.
   * **Couverture** : mensuel sinon journaliers ; journaliers pour le mois en cours ; API pour
     J et J−1 avant ≈ 03 h UTC. Supprimer la barre non clôturée.
   * **Découverte des fichiers** : listing S3 (§1.1) plutôt que des 404 à l'aveugle. Invalider le
     cache sur changement d'`ETag`. Vérifier les `.CHECKSUM`.
   * **Débit API** : lire `x-mbx-used-weight-1m`, respecter `Retry-After`, rester sous ≈ 3 000 de
     poids par minute, car l'IP est partagée.
2. **Fenêtre d'étude** :
   * **2023-01 → aujourd'hui** au plus large. Il n'y a presque aucun trou, et les dérivés sont
     complets à partir de 2023.
   * Pour limiter le calcul : les **12 derniers mois** (≈ 525 000 barres par actif).
3. **Ajouter à l'étude** :
   * un indicateur `lead_lag` Binance → plateforme d'exécution (§1.6), sous réserve de charger aussi
     BTC-EUR ou `PF_XBTUSD` en historique (charts Kraken Futures depuis 2022) ;
   * l'évaluation de la mini-stratégie à **4, 10 et 20 pb** (§3.4).
4. **Liquidations** : pas d'historique gratuit. Si l'on veut cette famille d'indicateurs, lancer
   **dès maintenant** une collecte Coinalyze (gratuit, 40 appels/min, fenêtre intraday glissante de
   1 500 à 2 000 points) ou prévoir un mois de Tardis Academic (350 $).

### 4.2 Données du bot en production

* **Flux principal = la plateforme d'exécution.** Pour Kraken : WebSocket `wss://futures.kraken.com/ws/v1`
  (ticker, trades, carnet, funding prédit). Barres 1 min reconstruites localement, puis recalées sur
  l'API charts.
* **Flux secondaire = Binance public**, comme référence de prix leader et pour les features de flux
  taker : `wss://data-stream.binance.vision` (klines 1 min, aggTrades) et
  `wss://fstream.binance.com/market/ws/…@markPrice` (funding). Ce flux doit rester **optionnel** :
  l'accès public depuis la France n'est pas garanti durablement, vu le départ de Binance de l'UE.
  Le modèle doit se dégrader proprement sans lui.
* **Dérivés en temps réel** : les `metrics` bulk arrivent avec 7 h de retard. En production, prendre
  l'OI et le funding de la plateforme d'exécution (ticker Kraken : `openInterest`, `fundingRate`),
  OKX (`/api/v5/public/open-interest`, rubik 5 min) ou Coinalyze (agrégé).
* **Cohérence recherche / production** : entraîner sur Binance, mais **valider hors échantillon sur
  les barres de la plateforme d'exécution** (`PF_XBTUSD` depuis 2022, OKX BTC-EUR ou BTC-USDC depuis
  2018) avant de passer en réel.

### 4.3 Plateforme d'exécution

1. **Kraken, perpétuels EEE (recommandé).**
   * Réglementation : MiFID II via la CySEC pour les dérivés, MiCA via la CBI pour la conservation.
   * Contrats : `PF_XBTUSD`, `PF_SOLUSD`, `PF_ETHUSD`, les trois actifs prioritaires.
   * Coûts : **10 pb aller-retour taker, 4 pb maker**. Funding horaire.
   * Outils : **démo en libre-service**, historique 1 min public depuis 2022, API REST et WebSocket
     complète. La vente à découvert est possible.
   * Réserve : le levier retail pourrait être ramené à 2x si l'analyse « CFD » de l'ESMA s'applique.
     Ce n'est pas bloquant pour nous.
2. **OKX EEE (second choix, ou venue redondante).**
   * X-Perps BTC/SOL/ETH aux mêmes frais (0,02 / 0,05 %), levier retail 2x, funding sur 8 h, démo.
     Historique court : cotés depuis le 30/03/2026.
   * Le **spot en maker (20 pb aller-retour)** est la seule option spot à peu près jouable, mais sans
     vente à découvert.
3. **À éviter pour ce bot :**
   * spot Kraken (160 pb) et spot Coinbase (100 pb) ;
   * **Binance, Bitget et toute plateforme absente du registre ESMA** : illégal pour la plateforme,
     et risque de gel des fonds ;
   * Bybit EU : spot seulement et API géo-bloquée depuis ce conteneur, à retester depuis la France.
4. **Mettre à jour la configuration** (hors de mon périmètre : `config.py` ne doit pas être modifié
   ici).
   * `futures_taker = 10` et `futures_maker = 4` restent valables pour Kraken et OKX X-Perps.
   * `spot_taker = 20` ne vaut que pour Bybit EU.
   * Coûts réalistes à ajouter : OKX spot EEE 40 pb taker / 20 pb maker ; Coinbase UE 100 pb ;
     Kraken spot 160 pb.
   * Documenter que les frais « Binance VIP0 » ne sont qu'une référence.

---

## 5. Limites de cette note

* Vérifier directement la grille de frais sur la plateforme avant tout passage en réel. Deux
  vérifications n'ont pas pu être faites en direct :
  * les frais Bybit EU (page en 503) ;
  * les frais Coinbase UE (aide Coinbase en 403 ; chiffres repris de la presse du 16/09/2026).
* Le **levier retail réel** sur perpétuels (2x ou 10x) change vite depuis la déclaration ESMA de
  février 2026. La consultation de la Commission se clôt le 30/09/2026.
* Le test de transférabilité Binance → EUR/USDC (§1.6) ne porte que sur ≈ 12 h. Les délais de
  publication (§1.2) se fondent sur `LastModified`, qui peut refléter une republication.
* Ce conteneur est géolocalisé hors de France : les accès bloqués ici (Binance REST 451, Bybit 403)
  peuvent différer depuis la France. Cela ne change rien au droit applicable.
