# Polymarket : suivre les wallets en temps réel, à la seconde

*Note de recherche rédigée le 25 septembre 2026. Les chiffres marqués **[mesuré]** proviennent de
trois sessions de mesure lancées ce jour-là entre 21:57 et 22:30 UTC avec
`scripts/polymarket_latency.py` (lecture seule), sur les marchés BTC « Up or Down » 5 min (fenêtre
en cours et suivantes). Les documents cités ont été consultés le même jour.*

> **Rappel.** Lecture de données publiques pour une recherche privée et une simulation papier.
> Aucun ordre, aucune clé, aucun identifiant API. Il ne faut pas publier les cotes : voir
> `polymarket.md` §4.

---

## 0. Réponse courte

**Oui, on peut suivre les trades d'un wallet donné à la seconde, en temps réel, avec des sources
publiques et gratuites.** Le délai dépend surtout de la source choisie. Le tableau part de
l'**instant d'appariement** par le CLOB (horodatage en ms du WebSocket de marché). Les délais sont
corrigés du décalage de l'horloge locale (−0,26 s, voir §2).

| Source (toutes publiques, sans clé) | Identité du wallet | Délai médian | p90 | Complète ? |
|---|---|---|---|---|
| WebSocket de marché CLOB, `last_trade_price` | **non** (seulement le hash de tx) | **0,09 s** | 0,22 s | 99 % des tx |
| Mempool Polygon : tx `matchOrders` en attente (WSS `newPendingTransactions`), calldata décodée | **oui** | **0,65 s** | 1,13 s | 92 % des tx ; tx pas encore minée |
| RTDS `activity` (`orders_matched`, puis `trades`) | oui (+ pseudo) | 1,9 s / 2,1 s | 2,6 s / 2,9 s | 99,7 % **si** l'on envoie `PING` toutes les 5 s (§6.2) |
| Logs `OrderFilled` on-chain, `eth_subscribe` (WSS) | **oui** | **2,2 s** | 3,1 s | 100 %, confirmé dans un bloc |
| Logs `OrderFilled`, `eth_getLogs` interrogé toutes les 1 s (HTTP) | oui | 2,2 s | 3,8 s | 100 % ; erreurs du nœud public |
| Data API `/v2/trades?user=W` (avec *cache-buster*), interrogé toutes les 1 s | oui | ≈ 5,4 s | ≈ 6 à 7 s | 100 % |
| Data API `/v2/trades?condition=…` (avec *cache-buster*), interrogé toutes les 1 s | oui | 9,0 s | 11,8 s | 100 % |
| Data API, **même URL sans *cache-buster*** | oui | **124 s** | 226 s | trous (page limitée à 1 000) |

Délais mesurés **[mesuré]** lors de la session 2 (10 min, 1 669 trades BTC 5 min). La session 1
donne les mêmes ordres de grandeur. Pour la Data API « user », on a ajouté le délai mesuré après le
bloc (3,2 s) à l'écart médian entre l'appariement et le bloc (2,2 s).

**Recommandation.**

1. **Chemin le plus rapide pour un wallet donné, avec confirmation :** s'abonner en WSS aux logs
   `OrderFilled` des deux exchanges CLOB V2, filtrés sur le topic `maker` = wallet(s). On voit
   alors le trade **≈ 2 à 3 s après l'appariement**, soit à peu près au moment de l'horodatage du
   bloc. On obtient tous les trades du wallet, qu'il soit preneur ou maker.
2. **Alerte précoce, non confirmée :** décoder les tx `matchOrders` en attente dans la mempool
   publique. On voit le wallet **≈ 0,6 à 1,1 s après l'appariement**, 1,7 s avant le bloc, pour
   92 % des trades. Il faut la compléter par le point 1.
3. **Variante la plus simple :** le WebSocket RTDS `activity` de Polymarket. Il pousse chaque trade
   avec `proxyWallet` et pseudo, au même moment que les logs on-chain (± 0,3 s), sans nœud ni
   décodage. Il faut envoyer `PING` toutes les 5 s, sinon le flux se tait au bout de 6 min, et
   réconcilier avec la chaîne.
4. **Data API :** uniquement pour l'historique et la réconciliation, **toujours avec un paramètre
   anti-cache** (§3.2). Pour le temps réel, c'est de 5 à 9 s de retard, et de plusieurs minutes si
   on tombe dans le cache CloudFront.

Délai réaliste de détection d'un trade d'un wallet suivi : **≈ 1 s** avec la mempool, **≈ 2 à 3 s**
confirmé on-chain ou par RTDS, **≈ 5 s** avec la Data API par wallet, **≈ 9 s** avec la Data API par
marché.

---

## 1. Chronologie d'un trade (BTC 5 min, médianes **[mesuré]**)

```
 t = 0      appariement par le CLOB (moteur hors chaîne)
 +0,09 s    WebSocket marché : last_trade_price {asset_id, price, size, side, timestamp ms, transaction_hash}
 +0,65 s    la tx matchOrders (envoyée par un opérateur Polymarket) apparaît dans la mempool publique
 +1,9 s     RTDS activity / orders_matched (wallet preneur, pseudo)
 +2,2 s     horodatage du bloc Polygon (entier, en s)  ≈  logs OrderFilled reçus par eth_subscribe
 +2,2 s     logs OrderFilled lus par eth_getLogs (interrogation toutes les 1 s)
 +5,4 s     Data API /v2/trades?user=W
 +9,0 s     Data API /v2/trades?condition=…   (liste rafraîchie toutes les ~4 s)
 +2 à 5 min Data API sans paramètre anti-cache (CloudFront, max-age=300)
```

Points à connaître :

- **Blocs Polygon** : un bloc toutes les **1,5 s** en moyenne (399 blocs en 10 min **[mesuré]**).
  L'horodatage est un entier (écarts de 1 ou 2 s). La tête de chaîne arrive par WSS **≈ 0,75 s avant**
  son horodatage (horloge corrigée), donc l'horodatage d'un bloc n'est **pas** l'instant du trade.
  La meilleure référence est l'horodatage en ms du WebSocket de marché. Le bloc tombe **2,2 s**
  (médiane ; p90 2,9 s) après l'appariement.
- Le champ `timestamp` de la Data API est **l'horodatage du bloc** (1 662 / 1 662 égalités
  **[mesuré]**). Une « seconde » Data API recouvre donc 1 à 2 blocs et arrive ≈ 2 s après
  l'appariement réel.
- Entre l'appariement et le bloc, le trade est connu du CLOB mais pas encore réglé. Sur la
  session 2, aucune tx `matchOrders` vue en attente pour les marchés suivis n'a manqué d'être
  minée (0 / 1 484), mais un échec reste possible en théorie : le trade n'est acquis qu'au bloc.

---

## 2. Méthode

`scripts/polymarket_latency.py` lance en parallèle, pendant `--minutes` (10 par défaut), sur le
marché BTC 5 min en cours et les 3 suivants (`PolymarketClient.current_and_next_markets`) :

| Collecteur | Détail |
|---|---|
| Data API « condition » | `/v2/trades?condition={4 marchés}&limit=1000&taker_only=true` toutes les 1 s, **avec** paramètre anti-cache `_=<ns>` (`busted`) et **sans** (`naive`). Une variante `single` n'interroge que le marché ouvert. |
| Data API « user » | `/v2/trades?user=W&taker_only=false&limit=200` toutes les 1 s, pour les 2 ou 3 wallets les plus actifs du flux global au démarrage. |
| Chaîne, HTTP | `eth_blockNumber` toutes les 1 s puis `eth_getLogs` (`OrderFilled` + `OrdersMatched`, deux exchanges V2) sur les nouveaux blocs. |
| Chaîne, WSS | `eth_subscribe` `newHeads`, `logs` (`OrderFilled`) et `newPendingTransactions` (objets complets). |
| WebSocket de marché | `wss://ws-subscriptions-clob.polymarket.com/ws/market`, jetons des 4 marchés. |
| RTDS | `wss://ws-live-data.polymarket.com`, topic `activity` (`trades`, `orders_matched`). |
| Sonde mempool HTTP | `eth_getTransactionByHash` sur le hash le plus récent reçu du WebSocket (≤ 80 sondes). |

Les WebSockets utilisent la bibliothèque `websockets` (17.1), déjà installée dans le venv : aucune
installation n'a été nécessaire.

Pour chaque trade, on retient la **première heure locale d'apparition** dans chaque source, puis
on joint par `transaction_hash`. Les trades déjà présents à la première requête servent de ligne
de base et ne sont pas chronométrés. Chaque socket a sa propre boucle d'événements. Dans la
session 1, une boucle unique saturée par le flux du marché (≈ 350 messages/s) avait fait fermer la
connexion par le serveur (`1013 slow consumer`) et faussé une partie des délais WebSocket.

**Horloge locale.** `/time` du CLOB et l'en-tête `Date` de Cloudflare ne donnent que la seconde
entière. On encadre le décalage avec 12 requêtes étalées dans la seconde (aller-retour ≈ 0,15 s) :
l'horloge locale **retarde de 0,11 à 0,42 s** (milieu −0,22 s au début de la session 2, −0,31 s à la
fin ; −0,18 s et −0,31 s pour la session 1) **[mesuré]**. Les délais relatifs à une horloge serveur
(bloc, WebSocket, RTDS) sont donc sous-estimés d'environ 0,25 s dans les sorties brutes du script. Ils
sont corrigés dans ce document. Les écarts entre deux sources lues localement n'ont pas besoin de
correction.

**Où l'on mesure.** La sortie réseau passe par un proxy. Le POP CloudFront est IAD (Virginie) et le
nœud publicnode est `ash` (Ashburn). Un serveur à Londres (eu-west-2, près du CLOB) verrait des
délais Data API et WebSocket voisins, puisque ce sont des services publics derrière un CDN, mais
un chemin réseau différent vers les nœuds Polygon.

Sessions **[mesuré]** :

| Session | Début (UTC) | Durée | Trades BTC 5 min chronométrés (Data API) | Remarque |
|---|---|---|---|---|
| 1 | 21:57 | 10 min | 2 363 | pas de RTDS ; WebSocket saturé entre la 1re et la 4e minute |
| 2 | 22:09 | 10 min | 1 669 | référence de ce document ; RTDS muet après 6 min (§6.2) |
| 3 | 22:21 | 8 min | 1 845 | RTDS avec `PING` : complet ; WSS du nœud coupé au bout de 2 min (reconnexion ajoutée ensuite au script) |

Commandes :

```bash
. .venv/bin/activate
python scripts/polymarket_latency.py clock
python scripts/polymarket_latency.py live --minutes 10 --out /tmp/pm_latency   # CSV + summary.json
python scripts/polymarket_latency.py roles --out /tmp/pm_latency              # sémantique taker_only
python scripts/polymarket_latency.py rpc-limits --out /tmp/pm_latency         # limites du nœud public
python scripts/polymarket_latency.py report --out /tmp/pm_latency             # recalcule summary.json
```

---

## 3. Data API (`data-api.polymarket.com`)

### 3.1 Flux par marché : `/v2/trades?condition=…`

| **[mesuré]** | Session 1 | Session 2 | Session 3 |
|---|---|---|---|
| Trades chronométrés | 2 363 | 1 669 | 1 845 |
| Délai après l'horodatage du trade (= bloc) : médiane / p90 / max | **6,7 / 8,8 / 10,8 s** | **6,5 / 9,5 / 10,6 s** | **5,8 / 7,8 / 10,8 s** |
| Idem pour le marché ouvert seul (`single`) | 6,7 / 9,0 / 11,8 s | 6,8 / 8,9 / 10,9 s | 6,0 / 8,2 / 10,1 s |
| Intervalle entre deux changements du trade le plus récent | 4,0 s (médiane) | 4,0 s | 4,0 s |
| Temps de réponse (aller-retour) | 0,28 s | 0,29 s | 0,29 s |
| Lignes identiques au log on-chain (côté, taille, prix, horodatage) | 2 336 / 2 336 | 1 662 / 1 662 | 1 809 / 1 809 |
| Trades on-chain de plus de 30 s absents de l'API | 0 / 2 312 | 0 / 1 617 | 0 / 1 796 |

La liste est **recalculée par lots toutes les ~4 s** : le délai minimal est d'environ 3 s, le
maximal d'environ 11 s. Interroger plus souvent qu'une fois par seconde n'y change rien. Le flux est
**complet** et **exact** : chaque ligne `taker_only=true` correspond à un événement `OrderFilled`
du preneur (même tx, même wallet, même jeton, même côté, même taille).

### 3.2 Piège du cache CloudFront

Les réponses portent `Cache-Control: public, max-age=300`. CloudFront les met en cache **par URL**,
et les en-têtes de requête `Cache-Control: no-cache`, `max-age=0` ou `Pragma: no-cache` sont
ignorés. On reçoit toujours `x-cache: Hit` **[mesuré]**. Interroger la même URL toutes les secondes
renvoie donc la **même réponse pendant 5 minutes** : `Age` monte de 0 à 300, puis l'URL est
rafraîchie. Session 2 : délai médian **124 s** (après l'appariement), p90 226 s, max 308 s.
Session 3 : 185 s après le bloc en médiane. Comme la première page est limitée à 1 000 lignes, les
trades de 5 minutes d'un marché actif dépassent cette limite et certains ne sont jamais vus.

**Correctif** : ajouter un paramètre de requête inutile et changeant, par exemple
`&_=<horodatage en ns>`. On obtient `x-cache: Miss` à chaque requête (600 / 600). Même contrainte
pour `?user=` (`max-age=300`). `/v2/activity` porte `max-age=15`.

### 3.3 Flux par wallet : `/v2/trades?user=W`

Il est **plus rapide que le flux par marché**. Session 2, trois wallets très actifs choisis dans le
flux global (692 trades, tous marchés confondus) **[mesuré]** :

| Wallet (activité) | Trades | Délai après le bloc : médiane / p90 / max | Délai après le log on-chain (WSS) |
|---|---|---|---|
| `0xc69b…2076` (604 trades, un marché sportif) | 604 | **3,2 / 4,2 / 6,2 s** | 3,1 s |
| `0xd0d9…30f2` | 25 | 3,3 / 4,3 / 4,3 s | 3,6 s |
| `0x2a69…d2f7` | 63 | 2,6 / 4,6 / 5,5 s | 3,3 s |
| Session 3 : `0xc69b…2076` | 474 | **2,4 / 4,4 / 5,4 s** | 2,9 s |
| Session 3 : `0x1b41…0b56` (Up/Down crypto 5 et 15 min) | 65 | 2,6 / 4,6 / 5,6 s | 2,9 s |

Ce flux est aussi rafraîchi toutes les ~4 s, mais avec environ 3 s d'avance sur le flux par marché.
Pour les trades présents dans les deux flux, le flux `user` est en avance : 7,4 s pour l'unique cas
de la session 1, 3,2 s en médiane pour les 4 cas de la session 3. Limites :

- `user` n'accepte **qu'une adresse** : une liste séparée par des virgules renvoie `[]`
  **[mesuré]**. Il faut donc une requête par wallet et par seconde. La limite de `/v2/trades` est de
  300 requêtes par 10 s, soit ≈ 25 wallets à 1 Hz au maximum, sans marge.
- Avec `taker_only=true` (la valeur par défaut), les exécutions **maker** du wallet n'apparaissent
  pas (§3.4). Pour suivre un wallet, il faut `taker_only=false`.
- `start`/`end` ne sont honorés que sur cette forme `user`. C'est utile pour l'historique à la
  seconde d'un wallet sur une fenêtre d'une heure.

### 3.4 Sémantique de `taker_only` (vérifiée on-chain)

Commande `roles`, marché `btc-updown-5m-1790374200` (fenêtre 22:10–22:15 UTC, trades de 21:22 à
22:16), comparé aux 2 281 événements `OrderFilled` des blocs correspondants **[mesuré]** :

| | Lignes | Correspondance on-chain (tx, wallet, jeton, côté, taille) |
|---|---|---|
| `taker_only=true` | 790 | 790 = les 790 exécutions **preneur** ; 0 exécution maker |
| `taker_only=false` | 2 281 | 790 preneur + **1 491 lignes maker** = les 1 491 exécutions maker (100 %) |

- `taker_only=false` renvoie **une ligne par ordre exécuté** : 1 ligne pour le preneur plus 1 par
  ordre maker touché. Par tx : 2 lignes en médiane, 5 au p90 et jusqu'à 30. Ce ne sont **pas des
  doublons** : chaque ligne est l'exécution d'un wallet différent, avec **son** côté, **son** jeton
  et **sa** taille.
- **Aucun champ ne donne le rôle.** Pour le retrouver, on fait une anti-jointure avec
  `taker_only=true`, ou on lit le log on-chain : dans l'événement du preneur, `taker` = l'adresse de
  l'exchange.
- 1 162 des 1 491 lignes maker (78 %) portent **l'autre jeton, avec le même côté** que le preneur.
  C'est l'appariement par création : un preneur « BUY Up à 0,64 » est servi par un maker « BUY Down à
  0,36 ». Additionner les lignes `taker_only=false` compte donc deux fois le volume et mélange les
  issues. **Pour le volume ou le prix payé, utiliser `taker_only=true`. Pour la position d'un wallet,
  utiliser `taker_only=false`.**

---

## 4. On-chain (Polygon PoS)

### 4.1 Contrats en vigueur (CLOB V2, depuis le 28/04/2026)

| Contrat | Adresse | Source |
|---|---|---|
| **CTF Exchange (V2)** — marchés binaires, dont les Up/Down crypto | `0xE111180000d2663C0091e4f400237545B87B996B` | docs Polymarket « Contracts », README `ctf-exchange-v2` |
| **Neg Risk CTF Exchange (V2)** — marchés multi-issues | `0xe2222d279d744050d28e00520010520000310F59` | idem |
| Conditional Tokens (ERC-1155) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | idem |
| pUSD (collatéral, proxy) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | idem |
| Ancien CTF Exchange V1 (plus utilisé par le CLOB) | `0x4bfb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` | PolygonScan |

La page « Contracts » cite aussi un `Exchange (proxy)` `0xe3333700…00Aa` dans la section *Combos*
(paris combinés, depuis le 29/05/2026). Il ne concerne pas les marchés Up/Down. Les tx des marchés
BTC observées vont toutes à `0xE111…996B`.

### 4.2 Événements (source `src/exchange/mixins/Events.sol`, dépôt `Polymarket/ctf-exchange-v2`)

```solidity
event OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
                  uint8 side, uint256 tokenId, uint256 makerAmountFilled,
                  uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata);
// topic0 = 0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee
event OrdersMatched(bytes32 indexed takerOrderHash, address indexed takerOrderMaker, uint8 side,
                    uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled);
// topic0 = 0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c
event FeeCharged(address indexed receiver, uint256 amount);
// topic0 = 0x55bb3cade9d43b798a4fe5ffdd05024b2d7870df53920673bfc7e68047cd0ab1
```

La signature a **changé** par rapport à la V1 (`OrderFilled(bytes32,address,address,uint256,uint256,
uint256,uint256,uint256)`, topic `0xd0a08e8c…`). La V2 donne directement `side` et `tokenId` au lieu
de `makerAssetId`/`takerAssetId`, et ajoute `builder` et `metadata`. Les événements sont émis en
assembleur (`log4`), mais l'encodage reste l'ABI standard : 3 topics indexés et 7 mots de 32 octets
dans `data`.

Règles de décodage (vérifiées en joignant les logs à la Data API) :

- un `OrderFilled` **par ordre maker** exécuté : `maker` = wallet du maker, `taker` = wallet du
  preneur ;
- puis un `OrderFilled` **pour l'ordre preneur** : `maker` = wallet du preneur, **`taker` =
  l'adresse de l'exchange**. C'est ce qui distingue le rôle. Vient ensuite `OrdersMatched` ;
- `side` est le côté **de l'ordre décrit**. BUY : `makerAmountFilled` = pUSD payés,
  `takerAmountFilled` = parts reçues. SELL : l'inverse. Les deux montants ont 6 décimales ;
  prix = pUSD / parts ;
- `maker` (topic 2) est l'adresse qui finance l'ordre, le *proxy wallet*. C'est **exactement le
  `proxy_wallet` de la Data API** (1 662 / 1 662) et de RTDS. `fee` est en pUSD.

Exemple décodé (session 2, un trade BTC 5 min) :

| rôle | wallet | côté | issue | parts | pUSD | prix | frais |
|---|---|---|---|---|---|---|---|
| maker | `0x47d5…44e5` | BUY | Down | 25 | 9,00 | 0,36 | 0 |
| preneur | `0x2ed9…f271` | BUY | Up | 25 | 16,00 | 0,64 | **0,4032** |

Les frais du preneur valent 25 × 0,07 × 0,64 × 0,36 = 0,4032 : la formule `crypto_fees_v2` de
`polymarket.md` se vérifie on-chain.

### 4.3 Latence on-chain **[mesuré]** (heures locales corrigées de +0,26 s)

| | Session 1 | Session 2 |
|---|---|---|
| Tête reçue par `eth_subscribe newHeads`, par rapport à l'horodatage du bloc | −0,65 s | **−0,75 s** (avant l'horodatage) |
| Premier log `OrderFilled` d'un bloc reçu en WSS, par rapport à l'horodatage | +0,08 s | **−0,12 s** (p90 +0,51 s) |
| Log du trade suivi reçu en WSS, par rapport à l'appariement | 2,5 s | **2,2 s** (p90 3,1 s) |
| Log lu par `eth_getLogs` interrogé toutes les 1 s, par rapport à l'appariement | 2,4 s | **2,2 s** (p90 3,8 s, max 9,2 s) |
| Erreurs `eth_getLogs` en 10 min | 112 « invalid block range params » | 64 « invalid block range params » + 6 délais dépassés |

En WSS, le log arrive **pendant la seconde de l'horodatage du bloc**. L'interrogation HTTP à 1 Hz
ajoute en moyenne 0,5 s et souffre du répartiteur de charge du nœud public : un `eth_getLogs`
jusqu'à la tête annoncée par `eth_blockNumber` tombe parfois sur un nœud en retard, qui répond
« invalid block range ». Il faut alors réessayer à la seconde suivante.

Pour **un wallet précis**, le filtre se fait côté nœud :
`topics = [OrderFilled, null, <wallet complété à 32 octets>]`. L'index 1 est `orderHash`, l'index 2
`maker`. Cela capte **ses deux rôles**, puisque le preneur est aussi `maker` de son propre événement.
Une liste en index 2 (`[OrderFilled, null, [w1, w2, …]]`) fait un OU. Avec
`eth_subscribe("logs", …)`, on reçoit donc uniquement les trades des wallets suivis, en push.

### 4.4 Limites du nœud public `polygon-bor-rpc.publicnode.com`

Mesures du 25/09/2026 (commande `rpc-limits`) **[mesuré]** :

| Test | Résultat |
|---|---|
| `eth_getLogs` `OrderFilled`, deux exchanges, 1 / 10 / 100 blocs | 40 / 270 / 4 008 logs en 0,06 / 0,08 / 0,32 s |
| idem, 1 000 blocs (≈ 25 min) | 48 000 logs, 52 Mo, 5,8 s |
| idem, 5 000 blocs (≈ 2 h) | 287 000 logs, **312 Mo**, 42 à 45 s (accepté) |
| filtre wallet (`maker` = un wallet très actif), 1 000 blocs | 1 502 logs en 2,0 s |
| filtre wallet, 10 000 blocs | `-32002 request timed out` au bout de 30 s |
| filtre wallet, 20 000 blocs | `-32701 exceed maximum block range: 10000` |
| Historique disponible | ≈ **1,56 jour** (≈ 90 000 blocs). Au-delà : `History has been pruned for this block` |
| Débit | 17,6 requêtes/s avec 8 connexions parallèles, 0 erreur ; aucune limite atteinte |
| En continu (10 min, 1 requête `eth_blockNumber` + 1 `eth_getLogs` par seconde) | 64 à 112 erreurs « invalid block range params » (nœud en retard derrière le répartiteur), jusqu'à 6 délais dépassés |
| WSS | `newHeads`, `logs` et `newPendingTransactions` (objets complets) acceptés ; connexion fermée sans préavis une fois (session 3, au bout de 2 min) |

Conséquences : le temps réel tient sans problème sur le nœud public, à condition de gérer les
reconnexions et les « invalid block range ». L'historique on-chain est limité à ≈ 36 h, avec des
plages ≤ 10 000 blocs, voire bien moins si le filtre est peu sélectif. Au-delà, il faut la Data API
(3 ans d'historique par marché) ou un nœud d'archive payant. Le volume est élevé : 40 à 48
`OrderFilled` par bloc en médiane (p90 78 à 124 **[mesuré]**), soit ≈ 50 Mo de JSON par tranche de
1 000 blocs sans filtre.

---

## 5. Mempool : le wallet avant le bloc

Les tx de règlement sont des appels `matchOrders(bytes32 conditionId, Order takerOrder,
Order[] makerOrders, uint256 takerFillAmount, uint256[] makerFillAmounts, uint256 takerFeeAmount,
uint256[] makerFeeAmounts)` (sélecteur `0x3c2b4399`). Elles sont envoyées **directement à
l'exchange** par une réserve d'opérateurs Polymarket : 2 000 adresses d'expéditeur distinctes en
10 min (session 1) **[mesuré]**. Chaque `Order` contient `maker` (le proxy wallet), `signer`, `tokenId`, `side`,
les montants et `timestamp` (création de l'ordre, en ms). Le calldata donne donc le wallet preneur
et les wallets makers **avant** l'inclusion dans un bloc.

- publicnode accepte `eth_subscribe("newPendingTransactions", true)` (objets complets) : ≈ 38 tx/s
  en tout, dont ≈ 10 `matchOrders` par seconde (tous marchés) **[mesuré]**.
- Couverture : **1 524 / 1 658 (92 %)** des trades BTC de la session 2 ont été vus en attente (94 %
  en session 1). Les autres sont arrivés directement dans un bloc sans passer par la mempool de
  ce nœud.
- Exactitude : wallet preneur décodé = wallet preneur on-chain dans **1 526 / 1 526** cas
  (2 209 / 2 209 en session 1). Aucune tx vue en attente n'est restée non minée (0 / 1 484).
- Délai : **0,65 s** après l'appariement (p90 1,13 s), **1,7 s avant** l'horodatage du bloc,
  **1,2 s** avant le log on-chain. Entre la création de l'ordre (champ `timestamp`, horloge du client) et
  son passage dans la mempool, il s'écoule ≈ 1,1 à 1,4 s en médiane.
- La sonde HTTP `eth_getTransactionByHash`, lancée 0,3 s (médiane) après le message WebSocket,
  trouve la tx dans 51 cas sur 80, dont 40 encore en attente. L'abonnement WSS est plus fiable que
  la sonde.

Limites : une tx en attente n'est pas un trade réglé, puisqu'elle peut échouer (aucun cas observé).
La mempool publique n'est pas garantie : 8 % des trades n'y passent pas, et un autre nœud peut en
voir d'autres. C'est aussi le flux le plus lourd : tout Polygon, ≈ 23 000 tx en 10 min, calldata
compris.

---

## 6. WebSockets

### 6.1 Canal marché du CLOB (`/ws/market`, public)

- Événements reçus en 10 min pour 8 jetons **[mesuré]** : `price_change` 229 256, `book` 3 328,
  `last_trade_price` 1 658, `tick_size_change` 12. Avec `custom_feature_enabled`, on reçoit en plus
  `best_bid_ask`, `new_market` (pour **tous** les marchés) et `market_resolved`.
- **Latence de transport** : réception − horodatage serveur = **0,09 s** en médiane (p90 0,22 s)
  après correction d'horloge. `last_trade_price` arrive **2,2 s avant** le bloc.
- **Pas d'identité de wallet.** Les champs sont exactement `asset_id, event_type, fee_rate_bps,
  market, price, side, size, timestamp, transaction_hash`. Il y a **un message par tx de
  règlement** : jeton, côté et taille du **preneur**, qui correspondent au log on-chain dans 1 655 cas
  sur 1 655 ; `price` est le prix du niveau touché, égal au prix moyen du preneur dans 93 % des cas
  et à 0,005 près dans 99,9 %. `fee_rate_bps` vaut toujours 0 : c'est un champ hérité, les frais
  étant fixés par le protocole. Le **hash de tx** permet de joindre ce
  message au wallet : via la mempool (§5) ≈ 0,55 s plus tard, via le log ≈ 2,1 s plus tard.
- Un client lent se fait couper (`1013 slow consumer: send buffer full`, `keepalive ping timeout`).
  Cela est arrivé en sessions 1 et 3, quand le même processus Python décodait aussi toute la mempool.
  Il faut un processus (ou au moins une boucle) dédié à ce socket et le `PING` toutes les 10 s.

### 6.2 RTDS `activity` (`wss://ws-live-data.polymarket.com`, public)

La doc de migration RTDS → PolyBolt dit : « *Keep using RTDS for the `activity` topic. It has no
PolyBolt channel.* » Abonnement :
`{"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"},{"topic":"activity","type":"orders_matched"}]}`.
On peut filtrer, par exemple avec `"filters":"{\"event_slug\":\"btc-updown-5m-…\"}"`.

- Charge utile : `proxyWallet`, `name`, `pseudonym`, `side`, `size`, `price`, `asset`, `outcome`,
  `conditionId`, `slug`, `timestamp` (s), `transactionHash`. C'est **la seule source push officielle
  avec le wallet**.
- Rôles, comparés au log on-chain **[mesuré]** : `orders_matched` = 1 ligne par tx, le **preneur**
  (828 / 828) ; `trades` = preneur + makers (833 preneur, 1 584 maker), comme
  `taker_only=false`.
- Délai : `orders_matched` **1,9 s** après l'appariement (p90 2,6 s), soit ≈ 0,3 s **avant** le log
  WSS ; `trades` arrive 0,2 s après `orders_matched`. Le `timestamp` de la charge utile est en
  général l'horodatage du bloc moins 1 s (2 756 / 3 596 lignes).
- **Fiabilité** : en session 2, sans `PING` applicatif, le serveur a **cessé d'envoyer au bout de
  6 min** (360 s), sans fermer la connexion. Couverture des tx : 100 % pendant les 6 premières
  minutes, puis 0 %. En session 3, avec `PING` toutes les 5 s, le flux est resté actif pendant
  8 min : **1 790 / 1 796** tx on-chain retrouvées (99,7 %), `orders_matched` 2,1 s après
  l'appariement (p90 2,9 s), `trades` 2,5 s (p90 3,5 s), contre 2,35 s pour le log lu en HTTP
  **[mesuré]**. Le serveur répond `""` à `PING`. Il faut aussi un chien de garde : s'il n'arrive
  aucune donnée pendant 15 s, on se reconnecte. Le flux global (tous marchés) représente
  ≈ 50 messages/s.
- Ce n'est pas une API documentée comme un contrat de service : aucune garantie de complétude ni
  de rejeu. Il faut garder la chaîne ou la Data API comme référence.

### 6.3 PolyBolt (`wss://ws-live-v2.polymarket.com/ws`)

Prix de référence seulement (`price.crypto`, `price.equity`, `price.crypto.twap`), avec des
**identifiants CLOB obligatoires**. Il n'est pas utile ici, et hors de portée sans clé.

---

## 7. Conclusion : comment suivre les « top wallets » d'une heure

1. **Choisir les wallets** sur l'historique : Data API `/v2/trades?condition=…` (toutes les pages,
   `taker_only=true`, horodatage à la seconde = bloc) sur la dernière heure. C'est ce que fait
   `scripts/premieres_courbes/top_wallets_derniere_heure.py`. Pour l'historique d'un wallet :
   `?user=W&taker_only=false&start=…&end=…`.
2. **Suivre en direct** : un seul `eth_subscribe("logs", {address: [CTF V2, NegRisk V2], topics:
   [OrderFilled, null, [w1, …, wn]]})` sur le WSS public, avec reconnexion automatique. On reçoit
   tous les trades des wallets, dans les deux rôles, confirmés, **≈ 2 à 3 s après l'appariement**. Le décodage (≈ 30 lignes) et le
   `topic0` sont dans le script.
3. **Si l'on veut gagner 1,5 s** : ajouter `newPendingTransactions` (objets complets), filtrer
   `to` ∈ exchanges, décoder `matchOrders`, puis confirmer par le log. Délai ≈ 0,65 s (p90 1,1 s),
   pour 92 % des trades.
4. **RTDS** pour le pseudo et un prototype rapide, avec `PING` toutes les 5 s, un chien de garde
   et une réconciliation par la chaîne.
5. **Data API** : réconciliation à T+30 s et historique, **avec paramètre anti-cache**. Elle est
   complète et exacte, mais a 5 à 10 s de retard.

**Délai réaliste : ≈ 2 à 3 s (confirmé), ≈ 1 s (mempool).** À titre de repère, le carnet réagit en
0,1 s (WebSocket de marché), le délai preneur imposé est de 150 ms, et `polymarket.md` §5 montre que
l'information de l'ouverture est payée en 4 s. Copier un wallet 2 à 3 s après son trade revient donc
à payer un prix déjà ajusté. Le suivi en temps réel sert surtout à **mesurer** le comportement des
meilleurs wallets (timing, taille, côté), pas à les répliquer avec profit. Cela reste à tester en
simulation papier, frais compris.

---

## 8. Alternatives payantes (non testées)

| Offre | Ce qu'elle apporte | Latence annoncée | Source |
|---|---|---|---|
| **Goldsky** Turbo / Mirror, jeux de données Polymarket V2 (`order_filled`, `orders_matched`, positions, soldes) | pipelines vers une base, un webhook ou Kafka, sans nœud à gérer ; subgraphs Polymarket **abandonnés** au 28/04/2026 | « real-time », sans chiffre publié | docs.goldsky.com/chains/polymarket ; goldsky.com/blog/polymarket-dataset. Nous n'avons pas trouvé de page publique sur une « Edge Data API » Polymarket : à demander à Goldsky. |
| **Bitquery** | GraphQL (abonnements WebSocket) sur les trades Polymarket avec acheteur et vendeur, filtre par liste de wallets ; flux Kafka `matic.predictions.proto` et **`matic.broadcasted.predictions.proto` (mempool)** | Kafka qualifié de « low-latency », sans chiffre | docs.bitquery.io (Polymarket Bitcoin Up or Down API) |
| **Allium** Datastreams (Kafka, Pub/Sub, SNS) | flux décodés multi-chaînes, livraison garantie | p50 de 1 à 3 s annoncé (générique, pas spécifique à Polymarket) | allium.so, docs.allium.so/datastreams/kafka |
| **Dune** (`polymarket_polygon.market_trades`, …) | tables SQL propres, V1 + V2 + combos | rafraîchissement **≈ 1 h** : pas du temps réel | docs.dune.com (Polymarket overview) |
| Nœud RPC dédié (Allnodes, Chainstack, QuickNode, Alchemy…) | historique complet (`eth_getLogs` au-delà de l'élagage du nœud public), débit garanti, WSS stable | celle de la chaîne (≈ bloc) | message d'erreur publicnode ; blog Chainstack « Polymarket API for developers » |

Aucune de ces offres ne peut battre un abonnement WSS direct aux logs, ni la mempool. Leur intérêt
est la fiabilité (reconnexion, rejeu, pas de trous), l'historique au-delà de l'élagage et l'absence
d'infrastructure à maintenir. Pour un test papier sur quelques wallets, le nœud public suffit.

---

## Sources

- Polymarket, *Contracts* (adresses V2, audits de mars 2026) : https://docs.polymarket.com/resources/contracts
- Polymarket, dépôt `ctf-exchange-v2` (README avec adresses ; `src/exchange/interfaces/ITrading.sol`, `src/exchange/mixins/Events.sol`, `src/exchange/CTFExchange.sol`) : https://github.com/Polymarket/ctf-exchange-v2
- PolygonScan, *Polymarket: CTF Exchange V2* : https://polygonscan.com/address/0xe111180000d2663c0091e4f400237545b87b996b ; *CTF Exchange* (V1) : https://polygonscan.com/address/0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e
- Polymarket, *List trades* (`/v2/trades`, `taker_only` : « `false` includes the maker rows too ») : https://docs.polymarket.com/api-reference/feeds/list-trades
- Polymarket, *Real-Time Data* : https://docs.polymarket.com/market-data/realtime-data ; *Migrate from RTDS to PolyBolt* (« Keep using RTDS for the `activity` topic ») : https://docs.polymarket.com/migrate/rtds-to-polybolt ; *PolyBolt* : https://docs.polymarket.com/api-reference/live-data/overview
- Polymarket, *Market Channel* : https://docs.polymarket.com/api-reference/wss/market
- Goldsky, *Indexing Polymarket* : https://docs.goldsky.com/chains/polymarket ; *Official Polymarket Datasets* : https://goldsky.com/blog/polymarket-dataset
- Bitquery, *Polymarket Bitcoin Up or Down API & Websocket* : https://docs.bitquery.io/docs/examples/polymarket-api/bitcoin-polymarket-api/ ; *Polymarket API* : https://docs.bitquery.io/docs/examples/polymarket-api/
- Allium, *Apache Kafka* (Datastreams) : https://docs.allium.so/datastreams/kafka ; *Real-time streaming* : https://www.allium.so/product/real-time-streaming
- Dune, *Polymarket* (tables, « Refresh: ~1 hour ») : https://docs.dune.com/data-catalog/curated/prediction-markets/polymarket/overview
- Chainstack, *Polymarket API for Developers: Gamma API, Data, and Polygon RPC* : https://chainstack.com/polymarket-api-for-developers/
- Points d'accès interrogés directement (25/09/2026) : https://data-api.polymarket.com/v2/trades , https://clob.polymarket.com/time ,
  https://polygon-bor-rpc.publicnode.com (HTTP et WSS), wss://ws-subscriptions-clob.polymarket.com/ws/market , wss://ws-live-data.polymarket.com
