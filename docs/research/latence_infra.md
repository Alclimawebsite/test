# Temps de réaction : combien il en faut, et comment l'atteindre

*Note de recherche rédigée le 26 septembre 2026. Les chiffres **[mesuré]** viennent de
`scripts/latency_probe.py`, lancé ce jour-là entre 10:45 et 11:20 UTC depuis ce conteneur, et des
enregistrements des deux collecteurs en cours (Binance, Coinbase, RTDS et CLOB Polymarket, 26/09
de 10:29 à 11:17 UTC). **[publié]** = source publique citée en fin de note, consultée le
26/09/2026. **[estimé]** = calcul ou hypothèse dont le détail est donné. Pendant les mesures, les
4 vCPU du conteneur étaient saturés par d'autres calculs (charge 9 à 11) : les médianes réseau
sont fiables, les queues de distribution (p90, p99) sont gonflées.*

> **Cadre légal, à lire d'abord.** Polymarket (plateforme internationale) est bloqué en France
> par l'ANJ. La France figure dans la liste « close-only » de Polymarket, **sur le site comme sur
> l'API** : depuis la France, on peut seulement fermer des positions existantes, pas en ouvrir.
> Tout ce qui suit relève de la recherche et de la simulation papier. Les architectures B et C
> (§ 3) sont décrites de façon **générique** : où sont les serveurs, d'où viennent les
> millisecondes. **Elles ne sont pas utilisables légalement depuis la France.** Ce document ne
> donne **aucune** méthode de contournement d'un géoblocage : pas de VPN, pas de serveur loué à
> l'étranger pour trader depuis la France, pas de prête-nom ni de compte d'un tiers.
> « Peu importe les moyens » s'entend donc ici des **moyens techniques licites**. Depuis ce
> conteneur, situé aux États-Unis, le CLOB refuse d'ailleurs déjà tout ordre :
> `403 {"error":"Trading restricted in your region…"}` **[mesuré]** (§ 1.3).

---

## 0. Réponse courte

**Le temps de réaction à battre (ℓ\*).** Quand Binance bouge, le carnet Polymarket se remet à
jour en quelques centaines de ms. Les délais ci-dessous sont comptés en heure des serveurs, du trade
Binance jusqu'au message du CLOB :

| Part du mouvement déjà intégrée par le carnet | 11 % | 25 % | 50 % | 75 % | ≈ 100 % |
|---|---|---|---|---|---|
| Délai après le trade Binance **[mesuré]** (BTC 5m et 15m, 14 marchés) | 100 ms | 150 ms | 225–250 ms | 300 ms | 400 ms |

Les deux autres workflows et la littérature concordent :

- **`reports/latence/`** (même journée) :
  - un prix périmé après un saut de Binance reste en médiane **92 ms** dans le carnet ;
  - 41 % sont pris par un preneur plus rapide, le reste est annulé par le teneur de marché ;
  - la moitié de l'avantage est perdue à **ℓ½ ≈ 209 ms** en temps réel (182 ms vu d'ici) ;
  - le gain n'est démontré que jusqu'à **≈ 127 ms** en temps réel (100 ms vu d'ici) ;
  - le point mort n'est pas encore identifié (IC 124 ms – 4,4 s).
- **`reports/polymarket/formule/`** : le carnet fait la moitié du chemin en 240 ms (quartiles 120
  et 360 ms). Un preneur qui suit les sauts de la formule, en passant premier, gagne +7,7 c par part
  à 0 ms, +6,5 c à 100 ms, +4,3 c à 200 ms, +1,4 c (non significatif) à 300 ms, puis perd à 500 ms.
- **OpenMarket** : 347 ms **[publié]**.

**ℓ\* se situe entre ≈ 130 ms et ≈ 200 ms**, comptés du trade Binance à l'appariement de notre
ordre. Jusqu'à ≈ 130 ms, le gain est démontré (`reports/latence/`) ; il reste significatif jusqu'à
≈ 200 ms (rapport sur la formule) ; au-delà de ≈ 210 ms, la moitié de l'avantage est perdue.
Et cela ne suffit pas : il faut aussi arriver **avant les autres preneurs rapides**, qui vident le
meilleur ask (≈ 20 à 67 parts en médiane selon les rapports).

**Ce qui ne se compresse pas :**

- **La distance Tokyo (Binance) → Londres (CLOB).** Aller simple : ≥ 47 ms en théorie (fibre en
  ligne droite, qui n'existe pas), ≈ 70 ms sur le meilleur réseau privé publié, ≈ 105 ms sur le
  réseau d'AWS.
- **Le délai preneur imposé par le CLOB sur les marchés crypto : 150 ms depuis le 04/09/2026 à
  14:00 UTC** (journal des changements de Polymarket). Il valait 50 ms entre le 17/08 et le 04/09,
  250 ms auparavant. Le drapeau `itode: true` est vérifié sur le marché BTC 5 min **[mesuré]**.

Le plancher réaliste d'un preneur est donc de **≈ 225 ms** (70 + 150 + quelques ms), au-dessus de
ℓ½. Le plancher physique est de ≈ 200 ms.

| Architecture | Trade Binance → ordre apparié : médiane / p90 | Part du repricing déjà faite à l'arrivée | Gain de la formule à ce délai *si l'on passe premier* | Coût mensuel | Complexité |
|---|---|---|---|---|---|
| **A.** Ce conteneur, Python actuel (hypothétique : l'ordre est refusé, 403) | **316 / 335 ms** | ≈ 81 % | ≈ +1,3 c (non significatif) | 0 | faible |
| **B.** Serveur dédié dans la région du CLOB (AWS eu-west-2), code asynchrone, ordres pré-signés, connexions chaudes | **≈ 265–270 / 280 ms** **[estimé]** | ≈ 66 % | ≈ +2,3 c | ≈ 100–200 $ | moyenne |
| **C.** Le mieux possible : nœud à Tokyo + réseau privé à faible latence + nœud à Londres, Chainlink Data Streams | **≈ 228–232 / 240 ms** **[estimé]** | ≈ 52 % | ≈ +3,4 c | ≈ 1 000–1 300 $ + réseau privé sur devis | élevée |
| Plancher physique (fibre en ligne droite + 150 ms) | ≈ 200 ms | ≈ 40 % | ≈ +4,3 c | — | — |

La colonne « gain » est la borne haute du rapport sur la formule, **en supposant qu'on passe avant
tous les autres preneurs**. `reports/latence/`, plus prudent, ne démontre rien au-delà de 127 ms.

**En preneur, aucune architecture n'atteint ℓ\*.** Le délai de 150 ms suffit à mettre tout
preneur au-delà de la zone de gain démontré, même C, même au plancher physique.

- A arrive quand 80 % du repricing est fait : inutile, et de toute façon refusée (403).
- B arrive après la médiane des teneurs de marché.
- C arrive à peu près à la médiane, pour quelques cents par part au mieux, frais preneur déduits
  (jusqu'à 1,75 c par part à 0,50), en concurrence avec les autres preneurs de même classe.

La conclusion rejoint celle de `reports/latence/` : « impossible en preneur ».

**Le levier qui reste n'est pas la prise mais l'annulation.** Le délai preneur retient chaque
ordre preneur **150 ms, non annulable**, avant de le confronter au carnet.

- Un teneur de marché en B voit Binance ≈ 110 ms après le trade et fait annuler ses ordres vers
  **≈ 116 ms**.
- Le preneur le plus rapide (classe C), arrivé à ≈ 78 ms, ne peut pas être apparié avant
  **≈ 228 ms**.
- Même depuis ce conteneur (A), une annulation arrive vers ≈ 165 ms, avant tout preneur.

Pour nous, **la vitesse utile est celle d'un maker qui se protège, pas celle d'un preneur qui
chasse** (§ 4.2). Cela ne dit rien de la rentabilité : le maker simulé sur le carnet réel n'a rien
montré de significatif (`reports/polymarket/maker_live/`). La sélection adverse qui reste vient
sans doute surtout de flux informés qui ne dépendent pas de la latence (hypothèse).

---

## 1. Mesures depuis ce conteneur [mesuré]

Commande : `python scripts/latency_probe.py all --skip compute`, puis `compute` dans un
environnement séparé (§ 6). Échantillons : 50 résolutions DNS par hôte, 50 connexions froides par
hôte et par chemin, 100 requêtes par point d'accès sur connexion chaude (50 pour le `POST /order`),
20 poignées de main et 100 allers-retours par WebSocket.

### 1.1 Où est ce conteneur, et par où il sort

| | Sortie « directe » (socket vers l'IP de la cible) | Sortie par le proxy HTTPS (`HTTPS_PROXY`, tunnel CONNECT) |
|---|---|---|
| IP publique vue de l'extérieur | 34.172.54.233 (et d'autres 34.x) | 160.79.106.x |
| Réseau | AS396982 Google LLC, **plages Google Cloud `us-central1`** (Council Bluffs, Iowa) | AS396982, hors des plages Google Cloud publiées ; géolocalisée à Chicago ou Columbus |
| Frontal Cloudflare atteint | **ORD** (Chicago) | **IAD** (Ashburn, Virginie) |
| Certificat TLS reçu | émis par « Anthropic Egress Gateway » : TLS déchiffré puis rechiffré par une passerelle transparente | certificat réel de la cible (Google Trust Services pour Polymarket) |
| Qui l'emprunte | tests uniquement | **les deux collecteurs** (la bibliothèque `websockets` lit `HTTPS_PROXY`) |

Machine : 4 vCPU Intel Xeon à 2,8 GHz, Python 3.11. Les métadonnées Google Cloud ne sont pas
accessibles. Sur le chemin direct, « TCP connect » (0,3 ms) et « TLS » (3 ms) mesurent la
passerelle locale, pas la cible. La première requête d'une connexion paie la vraie poignée de main
en amont : TTFB à froid de 156 ms pour le CLOB et de 603 ms pour Binance, contre 122 ms et 154 ms
sur connexion chaude.

**Horloge.** Le SNTP (UDP 123) est bloqué. On estime le décalage par la méthode de Cristian sur
`/time` de Coinbase, en sortie directe, avec 60 requêtes et un RTT minimal de 46,7 ms : l'horloge
locale **retarde de 101,7 ms (± 23 ms)**. Binance et Coinbase, par les deux chemins, donnent entre
96 et 102 ms. Le 25/09, la note `polymarket_temps_reel.md` trouvait un retard de 110 à 420 ms :
l'horloge dérive. Toute latence « réception − horodatage serveur » doit donc être corrigée, ce
que fait `latency_probe.py feeds`.

### 1.2 DNS

`getaddrinfo` via 8.8.8.8 prend **3,1 à 3,4 ms** en médiane (p90 jusqu'à 16 ms) pour les sept
hôtes. C'est négligeable : on résout une fois et on garde la connexion. `clob`,
`ws-subscriptions-clob` et `ws-live-data.polymarket.com` résolvent vers les mêmes IP anycast de
Cloudflare (104.18.34.205, 172.64.153.51). `data-stream.binance.vision` et
`data-api.binance.vision` pointent **directement** vers des IP EC2 d'AWS `ap-northeast-1`
(Tokyo), sans CDN : le contrôle est fait sur les plages publiées par AWS. Coinbase passe par
Cloudflare.

### 1.3 HTTP : RTT applicatif sur connexion chaude, et localisation des origines

Le délai mesuré va de l'envoi de la requête au premier octet de la réponse (ms). Chemin direct :
frontal ORD. Chemin proxy : frontal IAD.

| Requête | Qui répond | Direct : médiane / p90 | Proxy : médiane / p90 | Statut |
|---|---|---|---|---|
| `GET clob…/cdn-cgi/trace` | le frontal Cloudflare lui-même | **20,0** / 21,5 | **42,9** / 51,0 | 200 |
| `GET clob…/time` | l'origine du CLOB | **122,0** / 125,3 | **132,8** / 150,7 | 200 |
| `GET clob…/book?token_id=…` | l'origine du CLOB | 118,8 / 130,0 | 131,7 / 155,0 | 200 |
| `POST clob…/order`, corps `{}`, sans en-tête ni signature | **Cloudflare** : géoblocage appliqué au frontal | 26,5 / 28,8 | 49,3 / 57,2 | **403** |
| `GET data-api.binance.vision/api/v3/ping` | nginx, AWS Tokyo | 153,9 / 155,1 | 187,0 / 192,5 | 200 |
| `GET api.exchange.coinbase.com/cdn-cgi/trace` | frontal Cloudflare | 20,1 / 21,9 | 41,7 / 44,2 | 200 |
| `GET api.exchange.coinbase.com/time` | l'origine de Coinbase | 46,1 / 47,5 | 92,5 / 97,2 | 200 |

Lecture :

- **Du frontal à l'origine du CLOB**, on ajoute **102 ms depuis ORD et 90 ms depuis IAD**. C'est
  trop pour une origine aux États-Unis. Pour une origine en Europe de l'Ouest, c'est cohérent :
  us-east-1 ↔ eu-west-2 = 77 ms **[publié]**, plus quelques ms de traitement, et Chicago est plus
  loin de l'Europe que la Virginie (+12 ms mesurés). La mesure ne départage pas Londres,
  Amsterdam et Dublin. Les sources indépendantes désignent **AWS eu-west-2 (Londres)** (§ 2).
- **Le `POST /order` refusé revient 6 ms seulement après le frontal.** Le géoblocage est appliqué
  par Cloudflare : la requête n'atteint jamais le moteur. Le RTT jusqu'au moteur d'un ordre
  autorisé vaut donc à peu près celui de `/time` ou de `/book`, plus la validation de l'ordre
  (inconnue : non mesurable sans ordre signé, et nous n'en envoyons pas). Corps de la réponse :
  `{"error":"Trading restricted in your region, please refer to available regions - https://docs.polymarket.com/developers/CLOB/geoblock"}`.
- **À froid, c'est 3 à 4 fois plus lent.** Par le proxy : CONNECT 224 ms + TLS 65 ms + premier
  octet 136 ms = 441 ms (p90 564). En direct : 164 ms pour le CLOB, 611 ms pour Binance. Règle :
  **ne jamais ouvrir de connexion sur le chemin critique.**
- **Le client HTTP coûte peu.** `httpx` en HTTP/2 (le client de `py-clob-client-v2`) ajoute
  **2,2 ms** en médiane par rapport à un socket brut : 135,8 ms contre 133,5 ms sur `/time`,
  requêtes entrelacées et comparées deux à deux, n = 60.
- Pour Coinbase, l'écart entre frontal et origine (26 ms depuis ORD, 51 ms depuis IAD) ne colle
  pas avec une origine « à côté » d'IAD. La doc de Coinbase indique pourtant us-east-1. Ce
  calcul mêle donc routage interne de Cloudflare et traitement : il sert d'ordre de grandeur, pas
  de géolocalisation au kilomètre.

### 1.4 WebSocket

En ms, médiane / p90. Le RTT applicatif dépend de la cible :

- CLOB : `PING` → `PONG` ;
- RTDS : abonnement → instantané (le serveur ne répond pas à `PING`) ;
- Binance : `LIST_SUBSCRIPTIONS` ;
- Coinbase : message invalide → `error`.

| Cible | Chemin | Poignée de main | RTT applicatif | Ping de protocole (RFC 6455) |
|---|---|---|---|---|
| `ws-subscriptions-clob…/ws/market` | direct | 328 / 374 | **116,5** / 118,3 | 116,3 / 117,1 |
| | proxy | 350 / 392 | 125,3 / 131,1 | 125,0 / 130,2 |
| `ws-live-data.polymarket.com` (RTDS) | direct | 447 / 508 | 138,7 (abonnement) | 110,4 / 112,4 |
| | proxy | 465 / 507 | 151,2 (abonnement) | 122,0 / 127,2 |
| `data-stream.binance.vision/ws` | direct | 684 | **150,1** / 247,8 | 150,1 |
| | proxy | 851 | 250,6 / 259,7 | 249,5 |
| `ws-feed.exchange.coinbase.com` | direct | 235 | **32,0** / 35,6 | 31,7 |
| | proxy | 252 | 45,4 / 55,5 | 45,4 |

- Sur le CLOB, le ping de protocole égale le `PING/PONG` applicatif : Cloudflare transmet les
  trames de contrôle jusqu'à l'origine. Le WebSocket de marché est donc aussi loin que l'API
  REST, et RTDS est à la même distance (Europe).
- Vers Tokyo, le proxy ajoute 100 ms à l'aller-retour. **Les collecteurs passent par ce chemin.**
- Une poignée de main coûte 0,25 à 0,85 s : il faut des reconnexions **en parallèle** (nouvelle
  connexion ouverte avant la fermeture de l'ancienne), jamais sur le chemin critique.

### 1.5 Transport des flux enregistrés par les collecteurs

Latence = réception locale corrigée (+101,7 ms) − horodatage de la source. Données du 26/09,
10:29–11:17 UTC, via le proxy.

| Flux | n | Médiane | p10 | p90 | p99 |
|---|---|---|---|---|---|
| Binance `aggTrade` BTCUSDT : réception − `E` (Tokyo → ici) | 13 574 | **97 ms** | 84 | 113 | 130 |
| Binance `aggTrade` : `E` − `T` (trade → événement, dans Binance) | 13 574 | 0 ms | 0 | 1 | 30 |
| Coinbase `ticker` BTC-USD : réception − `time` | 10 630 | **30 ms** | 15 | 46 | 108 |
| CLOB Polymarket (WebSocket de marché, BTC, 22 marchés) : réception − `timestamp` | 1,75 M | **84 ms** | 58 | 235 | 6 s |
| RTDS `crypto_prices_chainlink` btc/usd : observation Chainlink → horodatage RTDS | 2 783 | **1 091 ms** | 794 | 1 470 | 1 839 |
| … horodatage RTDS → réception | 2 783 | 348 ms | 214 | 460 | 536 |
| … **observation Chainlink → réception** | 2 783 | **1 436 ms** | 1 125 | 1 834 | 2 227 |
| RTDS `crypto_prices` btcusdt (relais de Binance) : horodatage Binance → réception | 2 819 | **418 ms** | 312 | 545 | 636 |

- **Les queues du collecteur CLOB** (p90 235 ms, p99 6 s) viennent du processus Python partagé
  sur des CPU saturés, pas du réseau. C'est la meilleure illustration de ce qu'il ne faut pas
  faire en A.
- **Le relais RTDS est lent.** Son horodatage est posé ≈ 350 ms avant la réception, alors que le
  RTT réseau vers RTDS est de 110–120 ms : il y a une file interne. Le prix Binance relayé par
  RTDS arrive donc 418 ms après le trade, contre 97 ms en direct.
- **Qui mène.** Corrélation des rendements décalés :

  | Paire | Pas | Résultat **[mesuré]** |
  |---|---|---|
  | Binance → Chainlink (heure d'observation) | 1 s | Binance mène de **1 à 2 s** (corrélation 0,52 à +2 s, 0,46 à +1 s, 0,10 à 0) |
  | Binance → Coinbase | 50 ms | Binance mène de **50 à 100 ms** (0,23 à +50 ms, 0,12 à +100 ms, 0,05 à 0, ≤ 0,02 si Coinbase menait) |

  Le retard de Chainlink dépend de la méthode : ≈ 0,6 s en heure des serveurs pour
  `reports/latence/`, 1 à 2 s ici (corrélation des rendements 1 s), ≈ 4 s dans le diagnostic du
  25/09 (alignement des niveaux). Par RTDS, qui ajoute 1,44 s, un mouvement de Binance devient
  visible dans Chainlink **2 à 3,5 s** plus tard.
- **Réaction du carnet Polymarket.** Méthode : régression du milieu Up sur les rendements Binance
  décalés, pas de 50 ms, phase 3, heure des serveurs. Le milieu intègre **25 / 50 / 75 / 90 %**
  du mouvement à **150 / 250 / 300 / 400 ms**. Face à Coinbase : 150 / 200 / 300 / 600 ms, soit
  50 ms de moins, ce qui colle avec l'avance de Binance sur Coinbase.
- Remarque pour le collecteur `cex_ws` (hors de cette tâche) : sur RTDS, seules les mises à jour
  `btc/usd` et `btcusdt` arrivent (3 376 et 3 416 messages). Pour `eth/usd` et `ethusdt`, on ne
  reçoit que l'instantané initial, sans doute parce qu'un seul filtre est retenu par topic.

### 1.6 Calcul local (en µs, médiane / p90 ; CPU partagé)

| Étape | Médiane | p90 | Remarque |
|---|---|---|---|
| Décodage JSON d'un `aggTrade` (`json` / `orjson`) | 2,8 / 1,1 | 3,0 / 1,1 | |
| Formule `fair_prob_up` (phase 3, `dt = 1`, scipy) | 89 | 143 | forme continue : 52 µs |
| Forme fermée Φ(m/s) avec `math.erf` | **0,31** | 0,33 | à préférer sur le chemin critique |
| `taker_edge` | 0,6 | 0,7 | |
| **Signature EIP-712 d'un ordre V2** avec `py-clob-client-v2` 1.2.0 (`OrderBuilder.build_order`) | **540** | 618 | clé de test jetable, en mémoire, jamais envoyée |
| Même signature par un chemin direct (séparateur de domaine précalculé, keccak, libsecp256k1 via `coincurve`) | **54** | 99 | signature identique octet pour octet à celle de la bibliothèque (contrôle automatique) |
| … dont ECDSA secp256k1 seul / keccak-256 | 32 / 8 | 45 / 10 | |
| En-tête HMAC L2 (`POLY_SIGNATURE`) | 3,9 | 4,0 | |
| Sérialisation du corps de l'ordre (`json` / `orjson`) | 5,3 / 0,5 | 8,1 / 0,8 | |
| Pile WebSocket Python en local (bibliothèque `websockets` 17, boucle locale) : asyncio / uvloop | 109 / 97 | 161 / 141 | |

**Tout ce qui se passe dans notre processus prend moins de 1 ms, même en Python**, soit moins de
0,5 % du budget. Le réseau représente plus de 99 %. Mais les **queues** (ramasse-miettes,
contention CPU : p99 de 6 s sur le collecteur) pèsent bien plus que les médianes.

---

## 2. Où sont les serveurs ?

### 2.1 Carte

| Service | Emplacement | Éléments |
|---|---|---|
| **CLOB Polymarket** : moteur d'appariement, API REST d'ordres | **AWS eu-west-2 (Londres)**, derrière Cloudflare (anycast) | Moniteur « Prediction Market Latency » de Glassnode : « AWS eu-west-2 (London, UK) » ; triangulation publique de W. Entriken (X, avril 2026) ; guides d'hébergeurs (NYCServers, 07/04/2026 ; QuantVPS, maj. 18/09/2026) **[publié]**. Frontal → origine : +90 à +102 ms depuis les États-Unis **[mesuré]**, compatible. |
| WebSocket de marché (`ws-subscriptions-clob`) | même région que le CLOB, derrière Cloudflare | ping ≈ `/time` **[mesuré]** |
| RTDS (`ws-live-data`) | Europe, derrière Cloudflare | ping 110–122 ms, comme le CLOB **[mesuré]** |
| PolyBolt (`ws-live-v2`) | derrière CloudFront (IP 13.226.x) | **[mesuré]** ; clé CLOB obligatoire |
| **Binance** spot : moteur et flux publics | **AWS ap-northeast-1 (Tokyo)** | IP de `data-stream.binance.vision` et `data-api.binance.vision` dans les plages EC2 `ap-northeast-1` **[mesuré]** ; articles Zenlayer et Alpha Book **[publié]** |
| **Coinbase Exchange** | **AWS us-east-1**, zone `use1-az4` (passerelles, moteur, flux de marché) | doc Coinbase « Systems & Operations » **[publié]** |
| **Chainlink Data Streams** | réseau d'agrégation multi-sites, « active-active », emplacements non publiés | doc Chainlink (architecture) **[publié]** |

**Piège fréquent.** Un article dev.to (04/06/2026) conclut à une origine à Amsterdam parce qu'un
ping y donne 1,2 ms. C'est le **frontal Cloudflare anycast** qui répond, présent dans toutes les
grandes villes. La latence utile est celle du **frontal à l'origine**. Pour l'isoler, on compare
une requête servie par le frontal (`/cdn-cgi/trace`) à une requête servie par l'origine (`/time`).
C'est la méthode de `latency_probe.py http`.

### 2.2 Distances

| Trajet | Grand cercle | Fibre en ligne droite (aller) | RTT médian AWS entre régions | Meilleur réseau privé publié |
|---|---|---|---|---|
| Tokyo ↔ Londres | 9 559 km | 46,8 ms | **213,7 ms** (cloudping ; 210,0 ms selon AWS) | **139,6 ms** (Avelacom via Direct Connect, gigue < 90 µs) |
| Virginie ↔ Londres | 5 917 km | 29,0 ms | 77,2 ms | — |
| Tokyo ↔ Virginie | 10 872 km | 53,2 ms | 149,1 ms (Avelacom : 135,4) | — |
| Dublin ↔ Londres | 463 km | 2,3 ms | 11,9 ms | — |

Aucun câble ne suit le grand cercle Tokyo–Londres. Le réseau d'AWS fait ≈ 105 ms en aller simple,
un réseau privé optimisé ≈ 70 ms. Il n'existe pas de liaison hertzienne sur ce trajet.

### 2.3 API d'ordres du CLOB (V2) : ce qui compte pour la latence [publié]

- **Envoi.** `POST /order` (un ordre) ou `POST /orders` (1 à 15 ordres signés). En-têtes L2
  `POLY_ADDRESS`, `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE` (HMAC) et `POLY_TIMESTAMP`.
  Ordre signé en EIP-712, domaine `Polymarket CTF Exchange`, version `2`, chaîne 137, contrat
  `0xE111…996B`. Champs signés : `salt, maker, signer, tokenId, makerAmount, takerAmount, side,
  signatureType, timestamp (ms), metadata, builder`. L'expiration n'est pas signée en V2.
- **Types d'ordre.** GTC et GTD (à cours limité ; un GTD expire une minute avant l'échéance
  indiquée), **FOK** et **FAK** (exécution immédiate, le reste est tué). Option `postOnly` :
  l'ordre est refusé s'il croiserait le carnet.
- **Délai preneur** (« taker delay »), sur les marchés crypto Up/Down marqués `itode: true` dans
  `GET /clob-markets/{condition_id}` (vrai sur le BTC 5 min du 26/09 **[mesuré]**). L'ordre
  preneur reçoit le statut `delayed`. Il est **tenu, non annulable** (depuis le 05/06/2026), puis
  revalidé et confronté au carnet. Historique :

  | Date | Délai |
  |---|---|
  | jusqu'en février 2026 | 500 ms, retiré sans annonce vers le 20/02/2026 (Protos) |
  | au plus tard juin 2026 | 250 ms, ordres verrouillés à partir du 05/06/2026 (Bitget) |
  | 17/08/2026, 11:00 UTC | 50 ms (journal des changements ; annonce de @PolymarketDevs) |
  | **04/09/2026, 14:00 UTC** | **150 ms**, en hausse (journal des changements) : **valeur en vigueur** |

  La page « Order lifecycle » affiche encore 250 ms. Le budget du § 3 utilise les **150 ms** du
  journal des changements. Les 50 ms de la période du 17/08 au 04/09 sont donnés pour mémoire.
- **Limites de débit** (par IP, lissées par Cloudflare : mises en file plutôt que rejetées, plus
  un seau de jetons par signataire) :

  | Point d'accès | Rafale | Soutenu |
  |---|---|---|
  | `POST /order` | 5 000 / 10 s | 120 000 / 10 min |
  | `POST /orders` | 2 000 / 10 s | 21 000 / 10 min |
  | `DELETE /order` | 5 000 / 10 s | 120 000 / 10 min |
  | `DELETE /cancel-all` | 250 / 10 s | 6 000 / 10 min |
  | `/book` | 1 500 / 10 s | — |
  | toute l'API | 9 000 / 10 s | — |

  Ce n'est pas une contrainte pour quelques ordres par minute.
- **Coupe-circuit.** Battement de cœur (`/v1/heartbeats` dans `py-clob-client-v2`) : sans
  battement pendant 10 s, tous les ordres ouverts sont annulés (contrôle toutes les 5 s).
- **Taille minimale : vraisemblablement 5 parts** (champ `mos: 5` de `/clob-markets`)
  **[mesuré]**. Frais preneur : 0,07·a·(1 − a) par part, 1,75 c à 0,50. Les makers ne paient rien
  et reçoivent une remise (`rebateRate 0,2`).

### 2.4 Retours publics sur les robots « Binance → Polymarket » (2025–2026) [publié]

- **Janvier 2026** : un portefeuille passe de 313 $ à 414 000 $ en un mois sur les marchés crypto
  15 min. Polymarket introduit des **frais preneur dynamiques** sur ces marchés pour freiner
  l'arbitrage de latence (Finance Magnates, 07/01/2026).
- **Février 2026** : retrait silencieux du délai de 500 ms. « Latency is now the only moat » ;
  les arbitragistes colocalisés dominent (Protos). Un développeur rapporte que ses stratégies
  « dépendantes du timing de la plateforme » ont cessé de marcher, tandis que les stratégies maker
  ont survécu (dev.to).
- **Juin, août puis septembre 2026** : délai de 250 ms non annulable, puis 50 ms, puis 150 ms.
  Après un mois à 50 ms, Polymarket est remonté à 150 ms.
- **Mesures d'hébergeurs (Dublin, 04/06/2026)** : flux WebSocket ≈ 13–15 ms, aller-retour chaud
  sur `/book` ≈ 21–23 ms en médiane, « bot-vs-bot races decided in the 37–55 ms p99 tails »
  (TradoxVPS). Soustraction faite du réseau Dublin–Londres (≈ 12 ms), Cloudflare et l'origine
  ajoutent ≈ 10 ms à l'aller-retour.
- Les tutoriels qui parlent d'un décalage de « quelques secondes » (Indie Hackers, 22/04/2026 ;
  dépôt `learningworship/polymarket-latency-bot`) décrivent un état révolu. Nous mesurons
  ≈ 250 ms.

---

## 3. Budget de latence : du trade Binance à l'ordre apparié

### 3.1 Décomposition (ms, médiane ; p90 entre parenthèses quand il est connu)

| Étape | A. Ce conteneur, Python | B. Londres, code optimisé | C. Tokyo + réseau privé + Londres |
|---|---|---|---|
| 1. Trade Binance → événement (`E` − `T`) | 0 (p99 30) **[mesuré]** | 0 | 0 |
| 2. Propagation Tokyo → notre processus (poussée WebSocket comprise) | **97 (113)** **[mesuré]** via le proxy, horloge ± 23 ms | **≈ 107–110** : RTT AWS / 2 + 2–3 ms de poussée **[estimé]** | **≈ 72** : 139,6 / 2 + 2 ms **[estimé]** |
| 3. Décodage | inclus en 2 (collecteur) ; 0,003 | ≈ 0,01 (Rust/Go ou `orjson`) | ≈ 0,01 |
| 4. Décision (formule) | 0,09 (scipy) | 0,0003 : seuils de prix précalculés, une comparaison | idem |
| 5. Signature EIP-712 | **0,54 (0,62)** **[mesuré]** | 0 : ordres **pré-signés** (0,05 s'il faut signer au vol, `coincurve`) | 0 |
| 6. HMAC L2 + sérialisation | 0,01 | 0,005 | 0,005 |
| 7. Envoi → frontal Cloudflare → origine du CLOB (connexion chaude) | **66 (75)** : RTT `/time` / 2 via le proxy **[mesuré]** | **≈ 5–6 (p99 ≈ 20)** : 1 ms jusqu'au frontal de Londres + ≈ 5 ms Cloudflare et origine, d'après les mesures de Dublin **[estimé]** | idem B |
| 8. Validation de l'ordre par le CLOB (signature, solde) | inconnue ; hypothèse 1–5 | idem | idem |
| 9. **Délai preneur** | **150** (depuis le 04/09/2026) | 150 | 150 |
| **Total jusqu'à l'appariement** | **316 (335)** : Monte-Carlo sur les distributions mesurées 2 et 7, hors étape 8 | **≈ 265–270 (≈ 280)** | **≈ 228–232 (≈ 240)** |
| Pour mémoire : total avec 50 ms (17/08–04/09) | 216 (235) | ≈ 165–170 | ≈ 128–132 |
| Pour une **annulation** maker (pas de délai) : trade Binance → annulation reçue | ≈ 165 | **≈ 116** | ≈ 78 |

La réponse HTTP (≈ 1 RTT de plus) n'entre pas dans le total : l'appariement a lieu dans le moteur
avant qu'elle ne revienne. On apprend l'exécution par la réponse ou par le canal utilisateur
authentifié.

### 3.2 Comment on obtient B et C (et ce qui est inutile)

**B. Serveur dédié à Londres (AWS eu-west-2), le moins cher des gains sérieux :**

1. **Emplacement.** Une instance dans la région de l'origine. Cloudflare y a un frontal à ≈ 1 ms.
   Aucun programme de colocation ni d'interconnexion privée avec Polymarket n'est public : tout
   passe par Cloudflare, comme pour tout le monde.
2. **Flux Binance.**
   - `bookTicker` (meilleur prix, sans délai d'agrégation) et `aggTrade`, sur deux connexions
     redondantes ; le premier message arrivé gagne.
   - Ou le flux **SBE** `bestBidAsk` (`stream-sbe.binance.com`, clé Ed25519, champ `eventTime`,
     les événements périmés sont abandonnés sous charge).
   - Éviter les bougies 1 s (≈ 0,5 s de retard moyen) et `@depth@100ms`.
   - Reconnecter en parallèle avant l'échéance des 24 h.
3. **Décision précalculée.** Pour chaque marché ouvert, σ et K sont connus. La règle
   « acheter Up si P − coût(ask) > marge » se réduit à « prix Binance > seuil(ask) ». On
   recalcule les seuils à chaque changement de l'ask. Sur le chemin critique, il ne reste qu'une
   comparaison.
4. **Ordres pré-signés.** Avant l'ouverture, on signe une échelle d'ordres FAK : Up et Down ×
   prix limites au centime (0,30 → 0,70) × 2–3 tailles, soit ≈ 250 ordres. Cela prend 13 ms avec
   `coincurve`, 0,14 s avec `py-clob-client-v2`. Au signal, on choisit l'ordre, on calcule le HMAC
   (4 µs) et on envoie. Aucun champ signé n'expire, mais le `salt` est unique et un ordre utilisé
   est consommé.

   *À vérifier avant toute conclusion* : le CLOB peut rejeter un `timestamp` trop ancien, et il
   rejette les doublons pendant le délai preneur.
5. **Connexions chaudes.**
   - HTTP/2 persistant vers `clob.polymarket.com` (`httpx`, `h2` ou client Rust) ; 2 à 4
     connexions en parallèle, pour ne pas dépendre d'une seule ;
   - une requête légère ou le battement de cœur toutes les 5 s, pour garder la connexion ouverte
     (sinon la première requête coûte +34 ms en médiane) ;
   - `TCP_NODELAY`, résolution DNS faite une fois.
6. **Code.** Python avec `uvloop` et `orjson` suffit pour la **médiane** : moins de 1 ms en
   interne **[mesuré]**. Rust ou Go sert la **queue** : pas de ramasse-miettes ni de GIL, un cœur
   dédié (`isolcpus`, `taskset`). Si l'on reste en Python : `gc.freeze()` après l'initialisation,
   un processus par flux, rien d'autre sur la machine.
7. **Horloge.** `chrony` sur le service de temps d'AWS (`169.254.169.123`, précision
   sub-milliseconde). Elle sert à **mesurer**, pas à trader.

**C. « Le mieux possible » :**

1. **Deux nœuds.** L'un à Tokyo, dans la même région AWS que Binance, lit le flux en ≈ 1–2 ms et
   calcule le signal. Il envoie **un seul octet utile** (« Up/Down, niveau ») au nœud de Londres
   par un **réseau privé à faible latence** : AWS Direct Connect aux deux bouts + opérateur
   spécialisé, 139,6 ms d'aller-retour publié contre 210 ms sur le réseau d'AWS. Le nœud de
   Londres tient les ordres pré-signés et les connexions chaudes, et envoie. Gain par rapport à
   B : ≈ 35 ms.

   Variante à tester : envoyer l'ordre **depuis Tokyo** vers le frontal Cloudflare de Tokyo, et
   laisser Cloudflare le transporter jusqu'à l'origine. Rien ne dit que son réseau soit plus
   rapide que celui d'AWS, et on ne peut pas le mesurer d'ici.
2. **Signaux qui mènent le comptant.**
   - Les perpétuels Binance, mêmes serveurs à Tokyo. Dans 15 % des cas, le carnet bouge **avant**
     la formule calculée sur le comptant (rapport sur la formule) : les meilleurs teneurs de marché
     suivent un signal plus précoce.
   - Plusieurs places à la fois (Binance, Coinbase à 38 ms de Londres, OKX et Bybit en Asie) ; le
     premier qui bouge déclenche.
3. **Chainlink Data Streams en direct.** Abonnement payant, à partir de 150 $ par mois et par
   flux ; deux origines en parallèle. Il donne le prix qui sert à la **résolution** plus tôt que
   RTDS, qui arrive 1,44 s après l'observation **[mesuré]**. Il ne sert pas à la course contre
   Binance (Chainlink a 0,6 à 2 s de retard sur Binance), mais à connaître **K et le TWAP final
   exacts plus tôt**. C'est ce qui compte dans la course de fin de fenêtre : le TWAP final est
   figé 3 s avant la clôture et connu ≈ 1,7 s avant la fin par RTDS
   (`reports/polymarket/arbitrage/`).
4. **Contournement du noyau (kernel bypass : DPDK, Onload, FPGA) : inutile.** Il fait gagner
   ≈ 5 à 20 µs par paquet. Le budget se compte en centaines de ms :
   - 70 à 105 ms de propagation ;
   - 150 ms de délai preneur imposé ;
   - plusieurs ms de Cloudflare (terminaison TLS, pare-feu applicatif), qu'aucune technique
     côté client ne contourne.

   Le gain serait de 0,01 %, pour une complexité et un coût d'un autre ordre. Même conclusion
   pour les groupes de placement (placement groups) et ENA Express : ils n'agissent que dans un
   même VPC, or le CLOB est derrière Cloudflare.

### 3.3 Coût et complexité [estimé]

| | A | B | C |
|---|---|---|---|
| Calcul | 0 (existant) | 1 instance c7i.large ou xlarge à Londres : ≈ 75–150 $ par mois (0,179 $/h en us-east-1 pour c7i.xlarge, Londres ≈ +10–20 %) | 2 instances (Tokyo + Londres) : ≈ 300 $ par mois |
| Réseau | 0 | trafic entrant gratuit, sortant de quelques Go (< 5 $) | 2 ports Direct Connect 1 Gbit/s : 0,30 $/h et 0,285 $/h au Japon, ≈ 420 $ par mois ; **opérateur privé sur devis (non publié)** |
| Données | flux publics gratuits | flux publics gratuits (Binance SBE : clé gratuite) | Chainlink Data Streams : ≥ 150 $ par mois et par flux, soit 300–600 $ pour BTC et ETH, comptant et TWAP |
| **Total** | 0 | **≈ 100–200 $ par mois** | **≈ 1 000–1 300 $ par mois + opérateur** |
| Complexité | faible | moyenne : client asynchrone, échelles pré-signées, battement de cœur, supervision, reconnexions | élevée : deux sites, réseau privé, Rust/C++, bascule, synchronisation des horloges, conformité |

### 3.4 Face à ℓ\* : ce que chaque architecture rapporterait

On croise les totaux du § 3.1 avec deux courbes de l'autre workflow : la réaction du carnet et le
gain selon le délai λ. Le gain est donné par part, contre le milieu 10 s plus tard, frais inclus,
en supposant qu'on passe premier (borne haute, une matinée de données).

| | Délai | Repricing déjà fait | Gain « si premier » (tous sauts) | Rang probable parmi les preneurs |
|---|---|---|---|---|
| A | 316 ms | ≈ 81 % | ≈ +1,3 c (non significatif) | derrière les classes B et C : l'ask périmé est déjà pris |
| B | ≈ 268 ms | ≈ 66 % | ≈ +2,3 c | derrière les classes C (les « 37–55 ms p99 » de Dublin se jouent entre robots de même classe) |
| C | ≈ 230 ms | ≈ 52 % | ≈ +3,4 c | à égalité avec les meilleurs ; départage sur la queue (gigue, p99) |
| Pour mémoire, délai de 50 ms (17/08–04/09) | 130–216 ms | 20–46 % | +5,8 c à +3,8 c | — |

`reports/latence/` est plus prudent : gain démontré jusqu'à ≈ 127 ms en temps réel seulement, et
41 % des prix périmés encore présents à 150 ms (27 % à 300 ms), vu d'ici. Aucune des trois
architectures n'entre dans la zone démontrée. Leur seul gain possible serait sur les prix périmés
qui survivent plus de 230 à 320 ms, c'est-à-dire ceux que les teneurs de marché n'ont pas jugé
utile de retirer.

Ordre de grandeur **[estimé]** : le rapport sur la formule compte 391 sauts de plus de 5 points
en 2,75 h sur trois séries (une matinée, 11 créneaux), avec ≈ 67 parts au meilleur ask. En passant
**toujours premier** avec 10 parts à ≈ +3 c (niveau de C), la borne haute serait d'environ 40 $
de l'heure. La
réalité dépend entièrement du **rang** parmi les preneurs, que rien ici ne permet d'estimer : un
preneur de classe A ou B arrive après ceux de classe C et ne récupère que ce qu'ils laissent. Ce
rang, pas la moyenne, décide si B ou C paient leur coût.

---

## 4. Autres leviers que la vitesse

### 4.1 Chainlink/RTDS plutôt que Binance ?

**Non, pas pour la vitesse.** Mesures du § 1.5 :

- l'observation Chainlink suit Binance de 0,6 à 2 s selon la méthode (§ 1.5) ;
- RTDS la livre 1,44 s plus tard ;
- total : 2 à 3,5 s de retard sur Binance, dix fois ℓ\*.

**Oui, pour l'exactitude.** L'issue se décide sur le TWAP60 **Chainlink**. Le suivre point par
point supprime l'erreur de niveau de Binance, que l'autre workflow chiffre à 1,8–4,3 pb. Surtout,
en phase 4 (les 60 dernières secondes), la part déjà réalisée de F est connue exactement. La bonne
architecture utilise **Binance pour le signal rapide et Chainlink pour le niveau**.
Chainlink Data Streams direct (C) fait gagner ≈ 1 s sur RTDS dans la course de fin de fenêtre.

### 4.2 Être maker : la course à l'annulation

Le délai preneur protège les makers : un ordre preneur reste **150 ms en attente, non
annulable**, puis il est revalidé contre le carnet **du moment**. Un maker qui annule avant cette
échéance n'est pas touché.

| Qui | Annulation ou arrivée d'ordre (après le trade Binance) | Appariement possible au plus tôt |
|---|---|---|
| Maker de classe A (ce conteneur) | ≈ 165 ms | — |
| Maker de classe B (Londres) | **≈ 116 ms** | — |
| Preneur de classe C (le plus rapide) | arrivée ≈ 78 ms | **≈ 228 ms** |
| Preneur de classe B | arrivée ≈ 116 ms | ≈ 266 ms |

**Un maker B gagne la course contre le preneur le plus rapide, avec ≈ 110 ms de marge.** Un
maker A, depuis ce conteneur (≈ 165 ms), la gagne aussi, avec ≈ 60 ms de marge. Avec les 50 ms de
la période du 17/08 au 04/09, la marge d'un maker B n'était que de ≈ 12 ms : la hausse du
4 septembre a rendu la protection des makers presque gratuite.

Cela explique la courte durée de vie des prix périmés (92 ms en médiane, `reports/latence/`) : les
makers ont largement le temps d'annuler. La sélection adverse qui leur reste vient sans doute
surtout de flux informés qui ne dépendent pas de la latence (hypothèse, non testée ici).

Mise en œuvre :

- ordres `postOnly` autour de la juste valeur, en GTC ou en GTD (le CLOB fait expirer un GTD une
  minute avant l'échéance indiquée) ;
- annulation au signal Binance : `DELETE /order` par ordre, ou `DELETE /orders` en lot ;
  `cancel-all` est limité à 250 / 10 s ;
- battement de cœur toutes les 5 s comme coupe-circuit ;
- une taille par niveau qu'on accepte de voir prise.

Limites :

- **La sélection adverse** ne vient pas que des arbitragistes de latence. Le maker simulé sur le
  carnet réel (`maker_live`) montre un taux de gain de 44 à 46 % quand l'ordre est exécuté, contre
  48 à 72 % sinon, et aucun P&L significatif.
- **La file d'attente.** On est derrière les ordres déjà posés au même prix : 26 à 61 parts
  en médiane avant S, selon le côté et le prix (`maker_live`).
- **Le délai peut changer à tout moment** : quatre changements en 2026.

### 4.3 Pré-positionner, découper, regrouper

- **Échelles pré-signées** (§ 3.2) : on supprime la signature du chemin critique. Pour un preneur,
  un **FAK** au prix limite « juste valeur − marge » balaie tous les niveaux utiles en un ordre.
  Mieux qu'un FOK, qui échoue si la taille n'est pas entièrement disponible.
- **`POST /orders` (≤ 15)** : Up à un prix et Down à un autre en un aller-retour, ou plusieurs
  marchés à la fois (BTC 5m et BTC 15m bougent ensemble) : un aller-retour au lieu de deux, et la
  réponse donne le sort de chaque ordre.
- **Taille.** La profondeur au meilleur ask est de ≈ 30 à 100 parts selon l'instant, et 100 parts
  coûtent ≈ 0,8 c de plus que 10. Au-delà, le gain par part baisse vite, et l'on entre en
  concurrence directe avec les teneurs de marché.

### 4.4 Risques

- **Règles mouvantes.** Délai preneur (500 → 0 → 250 → 50 → 150 ms), frais dynamiques
  (janvier 2026), résolution par TWAP (août 2026, fenêtre de 30 puis 60 s pour le 5 min) : une
  infrastructure optimisée pour une règle peut perdre son intérêt du jour au lendemain.
- **Pannes.**
  - Coupures WebSocket, avec des poignées de main de 0,3 à 0,9 s.
  - Consommateur lent coupé par le serveur (`1013 slow consumer`, constaté le 25/09).
  - RTDS muet sans `PING` (note du 25/09).
  - Délestage du flux SBE de Binance.
  - Cloudflare qui met les requêtes en file au-delà des limites.
  - Contention CPU : p99 de 6 s mesuré ici.
- **Horloges.** Le retard de la nôtre varie de 100 à 420 ms d'un jour à l'autre. Celles de
  Binance et de Polymarket ne sont pas comparables ici à mieux que quelques dizaines de ms près.
  Un budget mal mesuré conduit à croire qu'on est rapide.
- **Sélection adverse et concurrence.** Dans 15 % des cas, le carnet bouge avant le comptant
  Binance : d'autres ont un meilleur signal. Face à eux, notre « avance » est un piège.
- **Règlement.** Le trade n'est réglé qu'au bloc Polygon, ≈ 2,2 s après l'appariement. Un échec
  est rare mais possible.
- **Juridique.** Voir § 5. C'est le risque qui prime.

---

## 5. Rappel légal

- **France.** Polymarket international est bloqué par l'ANJ et classé « close-only » (FR) dans la
  documentation de géoblocage de Polymarket : on peut fermer des positions, pas en ouvrir, **sur
  le site comme sur l'API**. Le Royaume-Uni, où se trouve le CLOB, est lui aussi « close-only ».
  Ce n'est donc pas la même chose d'**héberger un serveur** dans une région et d'**avoir le droit
  d'y trader**. L'éligibilité dépend de la personne et de sa juridiction, pas de l'adresse IP d'une
  machine.
- **Ce document ne décrit que la physique et l'ingénierie d'un système de trading à faible
  latence.** Il ne contient et ne contiendra aucune méthode de contournement : VPN, serveur à
  l'étranger pour trader depuis la France, prête-nom, compte ou identité d'un tiers. Les
  architectures B et C ne sont pas utilisables légalement depuis la France.
- **Moyens techniques exclus quel que soit le lieu :**
  - la manipulation de marché (ordres fictifs, spoofing, wash trading) ;
  - l'exploitation de failles ;
  - toute action visant à dégrader le service ou les autres participants ;
  - le contournement des limites de débit par multiplication d'identités.
- Toutes les mesures de cette note sont en **lecture seule**. Le seul `POST /order` envoyé est un
  corps vide, sans authentification ni signature, refusé par Cloudflare (403). La clé qui a servi
  à chronométrer la signature a été générée en mémoire, n'a jamais été financée, écrite ni
  transmise, et aucun ordre signé n'a quitté la machine.

---

## 6. Reproduire

```bash
. .venv/bin/activate
python scripts/latency_probe.py all --skip compute --out /tmp/latency_probe   # ≈ 25 min
python scripts/latency_probe.py feeds --minutes 120 --out /tmp/latency_probe   # flux des collecteurs
python scripts/latency_probe.py report --out /tmp/latency_probe                # tableaux Markdown

# Signature EIP-712 : environnement séparé, pour ne pas toucher au venv du projet
uv venv /tmp/signenv && uv pip install --python /tmp/signenv/bin/python \
    py-clob-client-v2 coincurve orjson uvloop numpy scipy websockets
PYTHONPATH=src /tmp/signenv/bin/python scripts/latency_probe.py compute --out /tmp/latency_probe
```

- Sorties : `results.json` (toutes les statistiques), plus les CSV d'échantillons bruts :
  `dns_samples.csv`, `http_cold_samples.csv`, `http_warm_samples.csv` et `ws_samples.csv`.
- Options : `--modes direct` sur une machine sans proxy, `--n-warm 200` pour plus d'échantillons,
  `--clock-offset-ms` pour imposer un décalage d'horloge.
- Sur un serveur candidat (B ou C), la même commande donne directement les étapes 2 et 7 du
  budget. Pour la propagation Binance → serveur, il faut y faire tourner un collecteur Binance
  (`scripts/cex_ws_collector.py`) avant `feeds`.

---

## Sources (consultées le 26/09/2026)

**Polymarket**

- Polymarket, *Create order* (types, `POST /orders` 1–15, en-têtes L2, domaine EIP-712 V2, `postOnly`, statut `delayed`) : https://docs.polymarket.com/developers/CLOB/orders/create-order
- Polymarket, *Predictions Changelog* (04/09/2026 : « The taker delay on crypto markets is now `150ms`, up from `50ms` », 14:00 UTC ; 17/08/2026 : 50 ms, en baisse depuis 250 ms) : https://docs.polymarket.com/changelog/predictions
- Polymarket, *Order lifecycle* (page non mise à jour : 250 ms ; drapeau `itode`, non annulable) : https://docs.polymarket.com/concepts/order-lifecycle
- Polymarket, *Rate limits* : https://docs.polymarket.com/api-reference/rate-limits
- Polymarket, *Manage orders* (battement de cœur) : https://docs.polymarket.com/trading/orders/overview
- Polymarket, *Geoblock* (France et Royaume-Uni « close-only », site et API) : https://docs.polymarket.com/developers/CLOB/geoblock
- Polymarket, *Real-time data* (TWAP Chainlink, PolyBolt) : https://docs.polymarket.com/market-data/realtime-data
- @PolymarketDevs, délai preneur ramené de 250 à 50 ms le 17/08/2026 (page X inaccessible au téléchargement, texte cité par les résultats de recherche) : https://x.com/PolymarketDevs/status/2089295325660172578 ; synthèse : https://polybot.trading/blog/polymarket-taker-delay-250ms-to-50ms
- Bitget News, 03/06/2026, ordres preneurs non annulables pendant le délai de 250 ms (effet au 05/06/2026) : https://www.bitget.com/news/detail/12560605442364
- Protos, *Polymarket ends trading loophole for bitcoin quants* (retrait du délai de 500 ms, février 2026) : https://protos.com/polymarket-ends-trading-loophole-for-bitcoin-quants/
- Finance Magnates, 07/01/2026, frais dynamiques contre l'arbitrage de latence : https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/
- dev.to (lkto1m), *February 2026 Changed Polymarket Forever* : https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5
- PyPI, `py-clob-client-v2` 1.2.0 (code de signature utilisé pour la mesure) : https://pypi.org/project/py-clob-client-v2/

**Localisation et latence du CLOB**

- Glassnode, *Prediction Market Latency : About* (« AWS eu-west-2 (London, UK) ») : https://latency.glassnode.com/prediction-markets/about
- W. Entriken, triangulation du CLOB en eu-west-2 (X, avril 2026 ; page inaccessible, extrait cité par les résultats de recherche) : https://x.com/fulldecent/status/2043928173620945299
- NYCServers, 07/04/2026 : https://newyorkcityservers.com/blog/polymarket-server-location-latency-guide
- QuantVPS, 04/07/2026, mis à jour le 18/09/2026 : https://www.quantvps.com/blog/polymarket-servers-location
- TradoxVPS, banc d'essai à Dublin du 04/06/2026 : https://tradoxvps.com/best-polymarket-vps/
- dev.to (BlueWhale-Quant-Lab), 04/06/2026, conclusion « Amsterdam » tirée du frontal Cloudflare : https://dev.to/bluewhale-quant-lab/i-measured-polymarkets-api-latency-from-5-regions-heres-the-script-and-the-results-3mjm
- Indie Hackers, 22/04/2026 : https://www.indiehackers.com/post/latency-arbitrage-in-15-minute-crypto-markets-building-a-polymarket-trading-edge-2026-f77cc226c0

**Réseaux et places de cotation**

- cloudping.co, RTT médians entre régions AWS (P50, 1 jour) : https://www.cloudping.co/
- AWS Industries, 12/06/2026, *Ultra-low-latency cross-Region crypto trading with Avelacom and AWS* : https://aws.amazon.com/blogs/industries/ultra-low-latency-cross-region-crypto-trading-with-avelacom-and-aws/
- AWS Direct Connect, tarifs : https://aws.amazon.com/directconnect/pricing/
- Vantage, c7i.xlarge : https://instances.vantage.sh/aws/ec2/c7i.xlarge
- Coinbase, *Exchange Systems & Operations* (us-east-1, `use1-az4`) : https://docs.cdp.coinbase.com/exchange/introduction/systems-operations
- Binance, *SBE Market Data Streams* : https://developers.binance.com/docs/binance-spot-api-docs/sbe-market-data-streams
- Zenlayer, *Low-Latency Crypto Trading Servers in Tokyo* : https://cloud.zenlayer.com/blog/crypto-trading-latency-tokyo ; Alpha Book, *Why Exchanges Cram Into AWS Tokyo* : https://book.longcipher.com/en/blog/aws-tokyo-digital-wall-street/

**Chainlink**

- Chainlink, *Data Streams Architecture* : https://docs.chain.link/data-streams/architecture ; *Billing* : https://docs.chain.link/data-streams/billing
- Coinpaprika, 02/08/2026 (TWAP Chainlink, « from $150 per month for select feeds ») : https://coinpaprika.com/news/chainlink-twap-feeds-live-polymarket-close/
- Genfinity, 12/08/2026 (TWAP Data Streams pour les marchés 5 et 15 min) : https://genfinity.io/2026/08/12/chainlink-twap-data-streams-polymarket-crypto-markets/

**Recherche**

- G. Young, *OpenMarket: A Synchronized Polymarket-Binance Dataset…*, arXiv 2607.26245, 28/07/2026 (réaction médiane de 347 ms) : https://arxiv.org/abs/2607.26245

**Dépôt**

- `reports/polymarket/formule/README.md` (réaction du carnet, gain selon λ), `reports/polymarket/arbitrage/README.md` (issue connue avant la clôture), `reports/polymarket/maker_live/README.md`, `docs/research/polymarket_temps_reel.md`.
- Points d'accès interrogés directement le 26/09/2026 : `clob.polymarket.com` (`/time`, `/book`, `/cdn-cgi/trace`, `/clob-markets/{id}`, `POST /order` non signé), `ws-subscriptions-clob.polymarket.com`, `ws-live-data.polymarket.com`, `data-stream.binance.vision`, `data-api.binance.vision`, `ws-feed.exchange.coinbase.com`, `api.exchange.coinbase.com`, `ipinfo.io`, `api.ipify.org`, `www.cloudflare.com/cdn-cgi/trace`, `ip-ranges.amazonaws.com`, `www.gstatic.com/ipranges/cloud.json`.
