# Réseaux sociaux : peut-on voir les changements de profil, et à quel délai ?

*Note de recherche rédigée le 27 septembre 2026. Les chiffres marqués **[mesuré]** proviennent de
`scripts/social_profile_probe.py` (lecture seule, sans clé), lancé ce jour-là entre 16:05 et 16:35 UTC
depuis l'environnement du projet. Les documents cités ont été consultés le même jour ; les tarifs et
quotas changent souvent, la date de chaque page est indiquée dans les sources.*

> **Cadre.** Comptes **publics** d'organisations et de personnalités publiques (plateformes
> d'échange, projets, fondateurs, influenceurs), jamais de particuliers. Lecture de données publiques
> pour une recherche privée et une simulation papier. Aucune connexion, aucun compte, aucun contournement
> de protection. Voir le cadre légal au § 7 avant tout stockage durable.

---

## 0. Réponse courte

**Oui, on peut voir les changements d'un profil public, mais la façon de les voir dépend entièrement du
réseau.** Il y a trois régimes :

| Régime | Réseaux | Ce qu'on voit | Délai | Coût |
|---|---|---|---|---|
| **Flux poussé** : le réseau publie chaque modification de profil de tous ses utilisateurs | **Bluesky** (Jetstream / firehose), **Farcaster** (journal d'événements des hubs, webhooks Neynar par FID), **Nostr** (événements `kind 0`) | nom, bio, avatar, bannière, épinglé, handle, statut du compte (Bluesky) ; chaque champ avec sa date (Farcaster, Nostr) ; abonnements datés (Farcaster) | **≈ 1 s** ; l'AppView public Bluesky reflète un changement **0,25 s** (médiane) après le firehose **[mesuré]** | gratuit : sans clé (Bluesky, Nostr) ou avec une clé gratuite Neynar (Farcaster ; le seul hub sans clé joignable d'ici a 10 mois de retard, § 3.2) |
| **Interrogation + comparaison** (*poll + diff*) : on relit le profil et on compare à l'instantané précédent | **X**, **Instagram / Threads / Facebook**, **TikTok**, **YouTube**, **Telegram**, **Reddit**, **Mastodon**, API du client Farcaster | les champs que l'API expose ; jamais la date du changement | l'intervalle d'interrogation, borné par le quota : de **30 s** (Bluesky via CDN, Telegram) à **plusieurs heures** (Instagram, TikTok) | X : **0,01 $ par compte et par jour** (plafond, dédoublonnage 24 h) ; Meta / TikTok : gratuit mais accès sous conditions (compte pro, App Review, ou API recherche académique) ; les scrapers tiers coûtent 0,2 à 2 $ pour 1 000 profils mais violent les CGU |
| **Horodatage fourni** : la plateforme dit *quand* chaque champ a changé, sans qu'on ait observé le changement | **Farcaster** (par champ), **Bluesky** (`indexedAt` du profil ; historique des handles dans le journal PLC), **GitHub** (`updated_at`) | historique reconstituable | — | gratuit |

Ce qui **n'est pas** observable sans être « ami » du compte : la **présence** (en ligne / vu à …) sur
Telegram, Discord, Instagram ; les *Notes* Instagram ; le statut personnalisé Discord. Ce qui n'est
observable qu'en **déduisant** : la **suspension** ou la **suppression** d'un compte (erreur de l'API ou
événement `account` sur Bluesky), la mise en **privé** (`protected` sur X, `locked` sur Mastodon).

**Trois conséquences pour le projet.**

1. Pour les comptes qui comptent en crypto (X avant tout, puis Telegram, Instagram, TikTok, YouTube),
   il n'existe **aucun flux de changements de profil** : c'est du *poll + diff*, et le délai de détection
   est l'intervalle d'interrogation. Sur X, 300 requêtes par 15 min et par application donnent au mieux
   **une lecture par compte toutes les 15 min pour 300 comptes**, ou plus vite avec les requêtes
   groupées, pour 0,01 $ par compte et par jour.
2. Les protocoles ouverts (Bluesky, Farcaster, Nostr) donnent le flux **en une seconde** et gratuitement,
   mais les comptes crypto influents y sont peu nombreux (Farcaster excepté pour l'écosystème Base /
   Ethereum). Ils servent surtout de **banc d'essai** : on peut y mesurer, sans frais, ce qu'un signal
   « changement de bio / d'avatar » vaut avant de payer X.
3. Un « changement » ne vaut un signal que s'il est **daté à la seconde** et rapproché du prix. Le § 8
   montre que les épisodes connus (Musk et DOGE, renommages avant lancement de memecoins) se jouent en
   **minutes**, pas en heures : un *poll* toutes les 15 min est déjà trop lent pour la moitié d'entre
   eux, et il faut tester le reste en simulation papier, comme pour Polymarket.

---

## 1. De quoi parle-t-on ? Les « statuts » d'un profil

Le mot « statut » recouvre plusieurs choses selon le réseau. On distingue :

| Famille | Champs | Exemples de réseaux |
|---|---|---|
| **Identité affichée** | nom affiché, handle / nom d'utilisateur, avatar, bannière, bio, lien, localisation, message épinglé | tous |
| **Compteurs** | abonnés, abonnements, publications, « likes » | tous (arrondis sur YouTube) |
| **Relations** | liste des abonnements (qui le compte vient de suivre), liste des abonnés | X (payant), TikTok (API recherche), Bluesky, Farcaster, Mastodon |
| **Statut du compte** | existe / supprimé, suspendu, privé, vérifié (type de badge), désactivé | déduit des erreurs (X, Instagram, TikTok), événements `account` (Bluesky), `verification_status` (Facebook), `verified_type` (X) |
| **Présence** | en ligne, vu à …, « statut » Discord, Notes Instagram, statut emoji Telegram | **non accessibles** sans être contact / membre du même serveur |
| **Statut au sens « publication »** | *status* Mastodon, story, live en cours | flux de publications, hors de cette note (sauf `roomId` TikTok, `last_status_at` Mastodon) |

Cette note traite des trois premières familles et du statut du compte. La présence n'est accessible sur
aucune API publique pour un compte tiers, et la collecter reviendrait à surveiller une personne : hors
périmètre.

---

## 2. Méthode

`scripts/social_profile_probe.py` (module `tradebot.social_profiles`) fait quatre choses :

| Commande | Ce qu'elle mesure |
|---|---|
| `snapshot` | un instantané normalisé par compte (mêmes champs pour tous les réseaux : `PROFILE_FIELDS`), avec la **date de dernière modification par champ** quand le réseau la donne |
| `watch` | *poll + diff* : relit les comptes toutes les `--interval` s, journalise chaque champ modifié dans `changes.csv` ; c'est le prototype du suivi pour les plateformes fermées |
| `stream` | échantillonne les **flux poussés** (Jetstream Bluesky, journal d'événements d'un hub Farcaster, relais Nostr), compte les changements de profil par minute et mesure le délai avant que l'AppView public Bluesky reflète un changement vu sur le firehose |
| `diff` | compare deux instantanés enregistrés |

Sessions **[mesuré]** du 27/09/2026 (UTC) :

| Session | Commande | Détail |
|---|---|---|
| A | sondes `curl` sur 21 points d'accès | joignabilité et codes HTTP depuis l'environnement (§ 2.1) |
| B | `stream --seconds 180 --confirm 12 --nostr` | 16:23 → 16:26 |
| C | `stream --seconds 150 --confirm 40 --no-farcaster` | 16:29 → 16:32, confirmation AppView sur plus de commits |
| D | `watch`, 7 comptes, toutes les 30 s pendant 4 min | 16:23 → 16:27 : 63 lectures |
| E | rafales de 10 à 60 lectures par point d'accès | temps de réponse, en-têtes de cache et de quota |
| F | `stream --seconds 600 --confirm 20 --nostr --no-farcaster` | 16:34 → 16:44, référence des débits Bluesky et Nostr |

Aucune installation n'a été nécessaire hors `websockets` (17.1) et `requests`, déjà utilisés par
`scripts/polymarket_latency.py`.

### 2.1 Ce qui est joignable depuis l'environnement (session A) **[mesuré]**

| Point d'accès | Code | Lecture |
|---|---|---|
| `public.api.bsky.app/xrpc/app.bsky.actor.getProfile` | **200** | profil complet, sans clé |
| `hub.pinata.cloud/v1/userDataByFid`, `/v1/events`, `/v1/info` | **200** | hub Farcaster public, **mais en retard** (§ 3.2) |
| `api.farcaster.xyz/v2/user-by-username` (alias `api.warpcast.com`, `client.farcaster.xyz`) | **200** | API du client Farcaster, en direct, non documentée |
| `mastodon.social/api/v1/accounts/lookup` | **200** | profil complet |
| `t.me/<canal>` | **200** | titre, description, photo, abonnés d'un canal public |
| `api.lens.xyz/graphql` | 200 | joignable (non exploré) |
| `www.tiktok.com/@…` | 200 (370 ko) | page HTML avec `"followerCount"` et `"signature"` dans le JSON embarqué |
| `www.youtube.com/@…/about` | 200 (2,1 Mo) | page HTML avec `subscriberCountText` |
| `api.x.com/2/users/by/username/…` | **401** | joignable, jeton obligatoire |
| `www.reddit.com/user/…/about.json` | **403** | bloqué sans navigateur (même avec un `User-Agent` descriptif) |
| `www.instagram.com/api/v1/users/web_profile_info/` | **401** « Please wait a few minutes », `require_login` | bloqué |
| `graph.facebook.com/v21.0/me` | 400 | jeton obligatoire |
| `api.neynar.com`, `snapchain-api.neynar.com`, `hub-api.neynar.com` | **402** (`x402`, paiement à la requête) | payant |
| hubs Farcaster sur ports `:2281` / `:3381` (`hoyt`, `nemes`, `lamia`, `standardcrypto`, `snap.farcaster.xyz`) | connexion réinitialisée | **ports non standard bloqués par notre proxy**, pas forcément par les hubs |
| `api.github.com/users/…` | 403 | bloqué par la politique de la session (pas par GitHub) |
| `nitter.net` | connexion réinitialisée | — |
| `discord.com/api/v10/users/@me` | 401 | jeton obligatoire, et l'API ne donne pas la bio d'un tiers (§ 4.7) |

---

## 3. Les protocoles ouverts : un flux de changements, gratuit **[mesuré]**

### 3.1 Bluesky (AT Protocol)

**Lecture d'un profil.** `GET https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=<handle|did>`,
sans authentification, renvoie `handle, displayName, description, avatar, banner, followersCount,
followsCount, postsCount, pinnedPost, labels, verification, createdAt, indexedAt`. `getProfiles` accepte
**25 comptes par requête**. Temps de réponse **0,05 s** (médiane sur 60 lectures, p90 0,07 s).

Trois points à connaître :

- **Cache CDN de 30 s.** Les réponses passent par BunnyCDN avec `Cache-Control: public, max-age=30` et
  `CDN-Cache: HIT` : la **même URL renvoie la même réponse pendant 30 s**. C'est le même piège que la
  Data API Polymarket (`polymarket_temps_reel.md` § 3.2). Un paramètre anti-cache (`&_=<ns>`) donne
  `CDN-Cache: MISS` à chaque appel. Le client `SocialClient.bluesky()` l'ajoute par défaut. Le PDS
  annonce son quota : `ratelimit-policy: 3000;w=300` (3 000 requêtes par 5 min et par IP, chiffre
  documenté pour les hôtes PDS `*.host.bsky.network`) ; l'AppView public n'envoie pas d'en-tête de
  quota et sa documentation parle de limites « généreuses », sans chiffre.
- **L'avatar et la bannière portent le CID de l'image** dans leur URL
  (`cdn.bsky.app/img/avatar/plain/<did>/<cid>@jpeg`) : l'URL change si et seulement si l'image change.
  Pas besoin de télécharger l'image.
- **`indexedAt`** est la date de dernière indexation du profil par l'AppView : une date de dernière
  modification pour le profil entier (pas par champ). `com.atproto.repo.getRecord` (collection
  `app.bsky.actor.profile`, rkey `self`) donne le **CID de l'enregistrement de profil**, qui change à chaque
  modification : comparer deux CID suffit à savoir si *quelque chose* a changé.

**Historique des handles.** `https://plc.directory/<did>/log/audit` liste les opérations sur l'identité :
pour `bsky.app`, 4 entrées, dont le passage de `bluesky-team.bsky.social` à `bsky.app` le 12/04/2023.
C'est gratuit et complet depuis la création du compte.

**Flux poussé : Jetstream.** `wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.actor.profile`
pousse, pour **tous** les comptes du réseau, chaque écriture de l'enregistrement de profil (`commit`,
opérations `create` / `update` / `delete`, avec l'enregistrement complet), plus les événements
`identity` (changement de handle ou de document DID : il faut re-résoudre le handle, l'événement ne le
porte pas) et `account` (`active`, `deactivated`, `takendown`, `suspended`, `deleted`, `desynchronized`,
`throttled`). La version 2 (`wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents?collections=app.bsky.actor.profile&dids=…`)
accepte un **filtre côté serveur jusqu'à 10 000 DID** et 100 collections : on ne reçoit alors que les
comptes suivis. D'ici **[mesuré]** : 24 messages en 15 s sans filtre de DID, connexion acceptée et
silencieuse avec un filtre sur deux comptes inactifs ; chaque message v2 (`payload` de type
`network.bsky.jetstream.subscribeEvents#c`) porte `did, operation, record, cid, rev, seq` et l'heure
serveur de l'événement (`time`, `witnessedAt`), donc la **date exacte du changement**. Pas d'authentification pour le direct, non facturé ;
fenêtre de rattrapage par curseur de 36 h (72 h sur le relais brut `com.atproto.sync.subscribeRepos`,
`wss://relay1.us-east.bsky.network`, qui livre tout le réseau en binaire sans filtre). Au-delà, *Network
Replay* (clé d'API, facturé au volume, quota non publié) permet de rejouer l'historique des événements de
DID choisis. Les dépôts eux-mêmes ne gardent que l'état courant : **pas d'anciennes versions du profil**
hors ce rejeu. Le statut d'un compte se lit aussi à la demande : `com.atproto.sync.getRepoStatus?did=`
(`active`, `status`), ou l'erreur de `getProfile` (« Account has been suspended », « Account is
deactivated », « Profile not found »). `plc.directory/export/stream` pousse toutes les opérations
d'identité du réseau (changements de handle et de PDS).

| Session F (600 s) | Compte | Par minute | Par jour (extrapolé) |
|---|---|---|---|
| `commit` profil, `update` | 688 | 69 | ≈ 99 000 |
| `commit` profil, `create` | 171 | 17 | ≈ 25 000 |
| `identity` | 206 | 21 | ≈ 30 000 |
| `account` : `active` / `deleted` / `deactivated` / `takendown` | 187 / 32 / 22 / 10 | 25 | ≈ 36 000 |

Les sessions B (180 s) et C (150 s) donnent les mêmes ordres de grandeur (87 et 80 commits de profil par
minute). Sur les 688 mises à jour de la session F, l'enregistrement portait un avatar dans 663 cas, une
bannière dans 358, une bio dans 465, un message épinglé dans 5. Le flux complet (toutes collections) est
bien plus volumineux ; filtrer sur `app.bsky.actor.profile` le ramène à **≈ 2 messages par seconde**,
tenable sur n'importe quelle machine.

**Délai avant que le profil lu reflète le changement.** Pour chaque `update` reçu, le script relit
`getProfile` (avec anti-cache) jusqu'à ce que nom et bio correspondent à l'enregistrement du commit.
Session C (150 s) : **40 commits sur 40 reflétés**, après **0,25 s** en médiane, 0,25 s au p90, **1,75 s**
au maximum ; session F (600 s) : 20 sur 20, 0,24 s en médiane, 0,37 s au p90, 0,60 s au maximum ; aucune
erreur de lecture, aucun cas non reflété en 30 s (66 commits vérifiés en tout). Autrement dit, Jetstream
*est* le temps réel, et un *poll* de l'AppView derrière son CDN de 30 s ne fait que le rattraper. Aucun des
241 événements `identity` des sessions C et F ne portait de handle : il faut re-résoudre le compte après
un tel événement.

### 3.2 Farcaster (Snapchain)

**Chaque champ de profil est un message horodaté.** `GET https://hub.pinata.cloud/v1/userDataByFid?fid=3`
renvoie un message par champ, `USER_DATA_TYPE_{PFP, DISPLAY, BIO, URL, USERNAME, LOCATION, TWITTER, GITHUB,
BANNER}` et `USER_DATA_PRIMARY_ADDRESS_{ETHEREUM, SOLANA}`, chacun avec un `timestamp` en secondes depuis le
1er janvier 2021 00:00 UTC (époque Farcaster). C'est la **date de dernière modification du champ**. Exemple
(fid 3, `dwr`) : nom affiché inchangé depuis le 31/08/2023, bio modifiée le 24/05/2025, avatar et lien le
26/06/2025, handle et adresse Ethereum le 23/09/2025, localisation le 24/11/2025.

**Le journal d'événements est un flux de changements pour tout le réseau.** `GET /v1/events?shard_index=S&from_event_id=N`
pagine (1 000 par page ; `reverse=true` pour la queue) les événements du hub. Une modification de profil
est un `HUB_EVENT_TYPE_MERGE_MESSAGE` contenant un `MESSAGE_TYPE_USER_DATA_ADD`. En lisant 39 000 événements
d'affilée (journal de décembre 2025, voir ci-dessous) : **326 changements de profil, soit 0,8 % des
événements** ; répartition : avatar 84, nom affiché 64, handle 49, bio 40, adresse Ethereum 36, adresse
Solana 26, localisation 11, compte X lié 8, lien 7, bannière 1. Les réactions (37 %), casts (16 %) et
abonnements (`LINK_ADD`, 11 %) dominent le flux. Le protocole est gratuit et public par construction ; en
gRPC (`SubscribeEvents`) le même journal arrive en push.

Le même journal donne les **abonnements** (`MESSAGE_TYPE_LINK_ADD` / `LINK_REMOVE`, horodatés ; à la
demande : `/v1/linksByFid`, `/v1/linksByTargetFid`) : « tel fonds vient de suivre tel projet » est un
événement daté, gratuit. Les transferts de noms d'utilisateur sont un journal public horodaté
(`fnames.farcaster.xyz/transfers`) : historique complet des handles. Le protocole ne connaît ni compte
privé ni suspension : un bannissement est propre à chaque application (Farcaster app, Neynar) et invisible
aux tiers ; la suppression se voit indirectement (messages retirés, transfert du FID on-chain, expiration
du stockage). Les nœuds ne conservent les événements que **3 jours** : un historique long se construit
soi-même, ou via les jeux de données Neynar / Dune (rafraîchis toutes les ≈ 12 h).

**Mais le seul hub sans clé joignable d'ici est en retard de dix mois.** `/v1/info` de `hub.pinata.cloud`
(version 0.14.2) annonce `blockDelay ≈ 25,5 millions de blocs` par shard ; la queue de son journal porte
des messages horodatés du **6 décembre 2025** et avance de **≈ 50 événements par minute** (session B :
147 événements en 180 s, dont 0 changement de profil), soit une resynchronisation au compte-gouttes. La
première mesure avait d'ailleurs pris ce retard pour du direct : 39 000 événements en 60 s, exactement
1 000 par page, c'est un arriéré qu'on rembobine, pas un flux. **Vérifier `blockDelay` avant toute
mesure** (`SocialClient.farcaster_hub_lag()`). La documentation officielle ne recense d'ailleurs **aucun
hub public sans clé en 2026** (`hoyt.farcaster.xyz` est protégé par mot de passe ; le hub Pinata n'est plus
documenté) et renvoie vers Neynar ou vers son propre nœud. Les autres hubs écoutent sur des ports non
standard (2281, 3381) que notre proxy bloque. Options pour du direct :

- **Neynar** (propriétaire de Farcaster depuis janvier 2026) : plan **gratuit** avec clé d'API (10 M
  crédits par mois, 600 requêtes par minute et par point d'accès, 10 webhooks ; page « Plan update (June
  2026) »), qui inclut le hub hébergé (`snapchain-api.neynar.com`, gRPC `hub-grpc-api.neynar.com`) et des
  **webhooks `user.updated` filtrés par FID** (10 crédits par événement) et `follow.created` /
  `follow.deleted` avec `target_fids` (15 crédits) : un push ciblé, sans consentement de la cible. Palier
  Scale 249 $ par mois ; paiement à l'appel possible en x402 (0,001 USDC). C'est le 402 que nous avons reçu
  sans clé. Le flux Kafka de Neynar a été retiré en août 2026 au profit des webhooks.
- **Son propre nœud Snapchain** : 16 Go de RAM, 4 cœurs, 1,5 à 2 To de disque, ports 3381-3383 ouverts ;
  flux complet en local, sans clé ni conditions.

**L'API du client Farcaster est en direct, gratuite et non documentée** (la référence officielle ne liste
que les points d'accès d'invitation aux canaux ; les conditions d'utilisation de l'application n'ont pas pu
être lues, page rendue en JavaScript). `GET https://api.farcaster.xyz/v2/user-by-username?username=dwr`
(ou `/v2/user?fid=3` ; alias `api.warpcast.com`, `client.farcaster.xyz`) renvoie nom, handle, avatar, bio,
localisation, lien, bannière, **abonnés et abonnements**, en **0,07 s** (médiane sur 20 lectures, p90
0,09 s), sans cache (`CF-Cache-Status: DYNAMIC`) et sans en-tête de quota. Aucun horodatage. Elle a
confirmé, à elle seule, un changement que le hub n'avait pas encore vu : la bio de `dwr` est passée de
« Working on Farcaster » (hub, état de décembre 2025) à « Interested in technology and other stuff. »
(client, 27/09/2026). `SocialClient.farcaster(source="both")` combine les deux : valeurs du client, dates du
hub quand elles concordent, sinon la date est marquée `> <date du hub>`.

### 3.3 Nostr

Le profil d'une clé publique est un événement `kind 0` (`name, about, picture, banner, nip05, lud16`), dont
`created_at` est la date de la modification. Un `REQ` `{"kinds":[0], "since": maintenant}` sur un relais
public renvoie en push chaque modification de profil de tout le réseau : **5,8 par minute** sur
`relay.damus.io` (session F, 600 s, 58 événements ; 5,3 en session B), reçues **1,4 s** après `created_at`
(médiane ; p90 9,3 s ; horloge du client émetteur, parfois fausse : on a vu des `created_at` dans le futur
de 15 min). Le filtre `authors: [<clés>]` d'un `REQ` (NIP-01) cible directement les comptes suivis, et
l'abonnement reste ouvert après `EOSE` : c'est un push ciblé natif, sans relation avec la cible. Les
relais ne gardent que le dernier `kind 0` par clé (événement *remplaçable*) : l'historique dépend de
l'observateur ou du croisement de plusieurs relais. Chaque relais fixe ses limites (NIP-11 : abonnements
et filtres maximaux, frais éventuels) et ses conditions ; il n'y a pas de CGU centrales. Pertinence crypto
réelle mais étroite (communauté Bitcoin).

### 3.4 Mastodon (Fediverse)

`GET https://<instance>/api/v1/accounts/lookup?acct=<user>` : `display_name, note (HTML), avatar, header,
fields (liens vérifiés), followers_count, following_count, statuses_count, locked, last_status_at,
created_at`. Pas de date par champ. Quota annoncé dans les en-têtes : **300 requêtes par 5 min et par IP**
(`x-ratelimit-limit: 300`), cache 15 s (`max-age=15`), réponse en 0,05 s. L'API de streaming ne couvre que
les publications, pas les profils, et exige un jeton depuis Mastodon 4.2 : c'est du *poll + diff* propre,
à 1 requête par compte (`GET /api/v1/accounts?id[]=…` pour plusieurs). Nuances : la documentation
(08/07/2026) marque `lookup` comme réservé à un jeton utilisateur alors que `mastodon.social` le sert sans
authentification **[mesuré]** ; chaque instance peut fermer l'API anonyme
(`DISALLOW_UNAUTHENTICATED_API_ACCESS`) ; la copie d'un compte distant vue depuis une autre instance peut
être périmée, il faut interroger l'instance d'origine (WebFinger). Le statut du compte est explicite :
`suspended: true` (depuis 3.3), `limited: true` (silencié), `moved` (migré), `memorial`, `locked`
(abonnement sur approbation) ; 404 si supprimé.

### 3.5 Lens et GitHub

**Lens (v3)** : `api.lens.xyz/graphql` sert sans authentification les métadonnées de compte (`name, bio,
picture, coverPicture, attributes`) ; des webhooks AWS SNS poussent `AccountCreated`, `AccountFollowed` /
`Unfollowed`, `AccountUsernameAssigned` / `Unassigned`, `AccountBlocked`, `AccountReported`,
`AccountOwnershipTransferred`… mais **aucun sujet « métadonnées modifiées »** n'est documenté ; les
métadonnées étant remplacées on-chain (`setAccountMetadata`), l'horodatage du bloc est la date de fait.
Limites numériques non publiées ; conditions (27/02/2024) interdisant *spider, crawl, scrape* et la
collecte d'informations personnelles : l'API GraphQL est la voie prévue.

**GitHub** (profil « social » des développeurs et des projets) : `GET https://api.github.com/users/<login>`
renvoie `name, company, blog, location, bio, twitter_username, followers, following, created_at` et un
**`updated_at` du profil entier** : une requête suffit à savoir si quelque chose a changé. Quota 60
requêtes par heure sans jeton, 5 000 avec. Ni les événements ni les webhooks ne couvrent les modifications
de profil. Conditions (27/04/2026) : pas d'abus de l'API ; la politique d'usage autorise la recherche sur
des informations publiques **non personnelles** si les publications qui en résultent sont en accès ouvert.
D'ici, l'API est bloquée par la politique de la session, pas par GitHub.

### 3.6 Résumé des protocoles ouverts

| Réseau | Source | Couvre | Dates par champ | Délai | Contrainte |
|---|---|---|---|---|---|
| Bluesky | Jetstream (filtre par collection et par DID) / firehose | tous les comptes | `indexedAt` (profil), heure de l'événement, journal PLC (handle) | ≈ 0,3 s | aucune ; ≈ 2 msg/s après filtre ; CGU sans clause anti-collecte (14/08/2025), interdiction de contourner les limites |
| Farcaster | événements de hub (HTTP paginé ou gRPC) ; webhooks Neynar `user.updated` par FID | tous les comptes | **oui** (par champ, abonnements, handles) | ≈ 1 s (blocs) | hub à jour requis : clé Neynar (plan gratuit) ou nœud propre ; événements gardés 3 jours |
| Nostr | relais, `REQ {kinds:[0], authors:[…]}` | tous les comptes | **oui** (`created_at`, profil entier) | ≈ 1,5 s | limites par relais (NIP-11) ; historique non garanti |
| Mastodon | — (*poll* de l'instance d'origine) | comptes publics | non (statut du compte explicite) | intervalle | 300 req / 5 min / IP ; l'instance peut fermer l'API anonyme |
| Lens | webhooks SNS (abonnements, handle, blocages), pas les métadonnées | tous les comptes | bloc on-chain | ≈ bloc | limites non publiées ; CGU anti-*scraping*, API prévue |
| GitHub | — (*poll*, `updated_at` du profil) | comptes publics | profil entier | intervalle | 60 req/h sans jeton, 5 000 avec ; recherche autorisée sur données non personnelles |

---

## 4. Les plateformes fermées : *poll + diff*, sous conditions

*Cette section repose sur la documentation officielle consultée le 27/09/2026, recoupée par une seconde
lecture contradictoire. Aucune de ces API n'a été appelée depuis l'environnement (pas de jeton).*

### 4.1 X (Twitter)

**Ce qu'on voit.** `GET /2/users/by/username/:username` (ou `/2/users/by` pour plusieurs handles, `/2/users/:id`)
avec `user.fields=` renvoie `name, username, description, profile_image_url, profile_banner_url, url,
location, pinned_tweet_id, public_metrics (followers, following, tweets, listed), verified, verified_type,
protected, withheld, created_at, entities`, et selon les versions `subscription_type, affiliation, parody,
is_identity_verified`. **Aucune date de modification** : seul `created_at` (création du compte) existe. La
liste des abonnements (`GET /2/users/:id/following`, jusqu'à 1 000 par page) permet de voir « qui le compte
vient de suivre » en comparant deux lectures. La suspension se lit dans `errors[].detail` (« User has been
suspended »), la suppression dans un 404, le passage en privé dans `protected`.

**Aucun flux ne pousse les changements de profil.** Le *filtered stream* ne livre que des posts (chaque post
porte toutefois l'objet `user` de l'auteur dans `includes.users` : on voit un changement de profil *quand
le compte publie*). L'*Account Activity API* ne livre d'événements que pour les comptes qui ont autorisé
l'application. Le *Compliance Firehose* (Enterprise) ne porte que les transitions de statut (suspension,
protection, suppression), pas les modifications de bio ou d'avatar.

**Prix et quotas (docs.x.com, pages non datées, consultées le 27/09/2026).** Depuis le 6 février 2026,
l'accès en libre-service est **à l'usage** : **0,010 $ par ressource `User` renvoyée**, une même ressource
n'étant **facturée qu'une fois par jour UTC** ; suivre un compte coûte donc au plus **0,01 $ par jour**,
quelle que soit la fréquence de lecture. Quota : **300 requêtes par 15 min et par application** (900 par
utilisateur) sur les points d'accès de recherche d'utilisateurs ; 0,010 $ par enregistrement de la liste
des abonnements. Les paliers Basic (200 $/mois) et Pro (5 000 $/mois) sont fermés aux nouveaux comptes et
migrés vers l'usage ; le palier gratuit n'existe plus pour les nouveaux développeurs (hors « utilité
publique », au cas par cas) ; Enterprise sur devis. En pratique, 300 requêtes unitaires par 15 min font
**une lecture par compte toutes les 15 min pour 300 comptes**, davantage avec `/2/users/by` (plusieurs
handles par requête ; le maximum, historiquement 100, n'a pas été revérifié).

**Obligations.** Le *Developer Agreement* (dernière mise à jour 27/04/2026) impose de refléter dans les
24 h toute suppression, modification, protection ou suspension côté X dans ce qu'on a stocké, interdit
l'appariement hors X sans consentement, la redistribution et l'entraînement de modèles. Les conditions
d'utilisation (15/01/2026) interdisent le *scraping* (dommages forfaitaires : 15 000 $ par million de posts
et par jour).

**Voies non officielles.** Les points d'accès de syndication (`syndication.twitter.com/srv/timeline-profile/…`,
`cdn.syndication.twimg.com`) répondent sans jeton mais sont non documentés, limités (429 signalés) et
relèvent du *scraping* au sens des CGU (d'ici, `info.json` répond 200 mais vide). Les revendeurs
(`twitterapi.io` ≈ 0,18 $ pour 1 000 profils, `socialdata.tools` 0,20 $, Apify 1,7 à 2 $, Bright Data 1,5 $)
sont dix à cinquante fois moins chers que l'API mais reposent sur du *scraping* : X poursuit Bright Data
depuis 2023 (procédure suspendue depuis juin 2025 en vue d'un accord), sans succès jusqu'ici sur le terrain
contractuel ; le risque pèse sur le revendeur, et le client dépend d'une source qui peut disparaître du jour
au lendemain.

### 4.2 Instagram et Threads (Meta)

**Instagram.** La seule voie officielle vers le profil d'un compte tiers est l'arête **`business_discovery`**
de l'API Instagram *avec connexion Facebook* : `GET /{ig-user-id}?fields=business_discovery.username(<cible>){username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url}`.
Conditions : la cible doit être un compte **professionnel** (Business ou Creator) public ; l'appelant doit
lui-même avoir un compte professionnel relié à une Page Facebook, les permissions `instagram_basic`,
`instagram_manage_insights`, `pages_read_engagement`, et pour servir autre chose que ses propres comptes,
l'**App Review** avec **vérification d'entreprise**. Quota : limite de plateforme, **200 appels par heure
× nombre d'utilisateurs de l'application** (fenêtre glissante) ; une application à un seul utilisateur
dispose donc de ≈ 200 lectures par heure. Pas de badge de vérification, pas de bannière, pas de liste
d'abonnements, **aucune date de modification**. L'API *avec connexion Instagram* ne donne que le compte
de l'appelant (`/me`). Les webhooks Instagram ne concernent que le compte qui a autorisé l'application et
n'ont aucun champ « profil ». Les *Notes* et le statut d'activité ne sont visibles que dans la messagerie,
entre comptes qui se suivent. La page web `instagram.com/<compte>` et son JSON interne exigent une
connexion (401 « Please wait a few minutes » mesuré).

**Threads.** `GET https://graph.threads.net/v1.0/profile_lookup?username=<cible>` renvoie `username, name,
profile_picture_url, biography, is_verified, follower_count, likes_count, quotes_count, reposts_count,
views_count` pour les profils publics de **100 abonnés ou plus** (seuil abaissé de 1 000 à 100 le
20/11/2025), avec l'*Advanced Access* (App Review) ; en accès standard, seuls `@meta`, `@threads`,
`@instagram`, `@facebook`. Quota : **1 000 requêtes par 24 h glissantes et par utilisateur**, soit un
compte toutes les 87 s ou 40 comptes par heure. Pas de push, pas de dates.

**Meta Content Library** (chercheurs) expose des champs de compte Instagram (abonnés, bio, vérification,
date de création) et, pour les Pages Facebook, l'**historique des changements de nom** ; mais l'accès est
réservé aux chercheurs d'institutions académiques ou à but non lucratif, dans un environnement fermé, sans
export de données brutes : hors de portée d'un projet de trading.

**CGU.** Les conditions d'Instagram et les *Automated Data Collection Terms* de Meta (révision du
07/10/2024) interdisent la collecte automatisée « que l'on soit connecté ou non » et exigent une permission
écrite ; Meta a perdu contre Bright Data (N.D. Cal., 23/01/2024) sur le terrain contractuel pour le
*scraping* hors connexion, mais bloque et poursuit activement. Les scrapers tiers (Apify ≈ 1,6 à 2,7 $ pour
1 000 profils, Bright Data 1,5 $) existent ; ils ne sont pas une voie conforme.

### 4.3 Pages Facebook (Meta)

Le nœud `Page` de la Graph API (`name, username, about, description, category, fan_count, followers_count,
picture, cover, website, location, verification_status, is_published, is_permanently_closed`) est lisible
pour des Pages que l'on ne gère pas **après App Review** de la fonctionnalité *Page Public Content Access*
(ou *Page Public Metadata Access*, en voie de remplacement), qui exige la **vérification d'entreprise**.
Quota : 200 appels par heure × utilisateurs (jeton d'application) ou 4 800 × utilisateurs engagés par 24 h
(jeton système, recommandé). Les webhooks de Page poussent bien `name, picture, description, website,
category`, **mais seulement pour les Pages dont un administrateur a installé l'application**. L'onglet
« Transparence de la Page » (date de création, **historique des changements de nom**, pays des
administrateurs) n'est **pas dans l'API** : interface web et Content Library uniquement. Pas de date de
modification dans l'API.

### 4.4 TikTok

Aucun push. Trois voies :

- **Research API** (`POST /v2/research/user/info/` : `display_name, bio_description, avatar_url, is_verified,
  follower_count, following_count, likes_count, video_count, bio_url` ; listes d'abonnés / abonnements par
  100). Gratuite, mais réservée aux chercheurs d'institutions académiques ou à but non lucratif (États-Unis,
  EEE, Royaume-Uni, Canada, Suisse), ≈ 4 semaines d'instruction, **1 000 requêtes par jour**, et les
  compteurs « peuvent mettre jusqu'à 10 jours à se mettre à jour » (FAQ du 01/09/2026). Les conditions
  (22/01/2026) interdisent l'usage commercial et le profilage individuel.
- **Display API** (`/v2/user/info/`, 600 requêtes par minute) : uniquement le compte connecté.
- **Page web** `tiktok.com/@<handle>` : le JSON embarqué `__UNIVERSAL_DATA_FOR_REHYDRATION__` contient
  `user.nickname, signature (bio), verified, avatarLarger, bioLink, privateAccount, roomId` et
  `stats.followerCount / followingCount / heartCount / videoCount` (entiers exacts) ; d'ici, la page répond
  200 avec `"followerCount":95900000` pour `@tiktok` **[mesuré]**. Mais les CGU (01/12/2025) interdisent
  « les scripts automatisés pour collecter des informations », et les protections (interstitiel JavaScript,
  empreinte TLS, blocages par IP, CAPTCHA) rendent la collecte fragile : ce n'est pas une voie conforme.
  Un changement de handle rend l'ancienne URL introuvable (`statusCode 10221`) : il faut suivre l'identifiant
  numérique. L'URL de l'avatar est signée et tourne : comparer un condensé de l'image, pas l'URL.

Revendeurs : Apify `clockworks/tiktok-profile-scraper` 1 $ pour 1 000 profils, EnsembleData dès 100 $/mois ;
même statut que ci-dessus.

### 4.5 YouTube

**La voie propre existe et elle est gratuite.** `GET https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics,brandingSettings,status&id=…`
(jusqu'à **50 chaînes par appel**, ou `forHandle=@nom`) avec une simple **clé d'API** (OAuth uniquement pour
les données privées). Champs : `snippet.title` (nom), `snippet.customUrl` (handle), `snippet.description`
(bio), `snippet.thumbnails` (avatar), `snippet.country`, `snippet.publishedAt` (création),
`statistics.subscriberCount` (**arrondi vers le bas à 3 chiffres significatifs**, masquable par le
propriétaire : `hiddenSubscriberCount`), `viewCount`, `videoCount`, `brandingSettings.channel.keywords`,
`brandingSettings.image.bannerExternalUrl` (bannière), `status.privacyStatus`. **Pas dans l'API** : liens
du « À propos », publications de la communauté, badge de vérification, abonnements de la chaîne,
historique des noms, date de modification. Une chaîne supprimée ou clôturée disparaît simplement de
`items` (aucun code de raison).

**Quota.** 1 unité par appel `channels.list` quel que soit le nombre d'identifiants ; **10 000 unités par
jour** et par projet Google Cloud par défaut (depuis le 01/06/2026, `search.list` et `videos.insert` ont
leurs propres enveloppes de 100 appels par jour). En théorie **500 chaînes toutes les 86 s** ou 5 000
toutes les 14 min ; au-delà, un audit de conformité (formulaire d'extension) est nécessaire, le quota ne
s'achète pas. L'ETag / `If-None-Match` (304) signale qu'un champ a changé sans dire lequel.

**Pas de push pour le profil.** Le hub PubSubHubbub de YouTube ne notifie que l'envoi d'une vidéo et la
modification d'un titre ou d'une description de vidéo. Un changement de nom « peut mettre quelques jours »
à se propager partout (aide YouTube) : la fraîcheur du champ n'est pas garantie même en *poll* serré.

**Obligations.** *Developer Policies* (page du 14/09/2026) : les données publiques non autorisées (par
exemple les compteurs d'abonnés) ne se conservent **pas plus de 30 jours** sans autorisation du
propriétaire de la chaîne (III.E.4.d), pas de métriques dérivées des données de l'API (III.E.4.h), pas
d'agrégation entre chaînes de propriétaires différents (III.E.2.1), *scraping* de youtube.com interdit
(III.E.6, III.I.14). Un historique long de changements de profil YouTube doit donc se limiter aux
champs d'identité et à leurs dates, pas aux séries de compteurs. Les sites d'historique (Social Blade)
et la Wayback Machine gardent d'anciens états ; Social Blade a refusé toutes nos lectures (403) et ses
conditions n'ont pas pu être vérifiées.

### 4.6 Telegram

Telegram est la seule grande plateforme fermée où **un vrai flux poussé de changements de profil existe**,
mais il passe par l'API cliente MTProto, c'est-à-dire par **un compte utilisateur réel** (Telethon,
Pyrogram, TDLib). Trois voies, du plus propre au plus risqué :

1. **Page publique `t.me/<canal>`** (aperçu officiel depuis 2019). Sans connexion : titre, description,
   photo, badge, nombre d'abonnés, et `t.me/s/<canal>` pour les publications avec compteurs de vues.
   D'ici **[mesuré]** : **0,14 s** par lecture (médiane sur 10, p90 0,57 s), `Cache-control: no-store`,
   nombre d'abonnés **exact** sur la carte du canal (`10 612 280 subscribers` pour `durov`, qui bouge à
   chaque lecture ; la documentation tierce parle d'un arrondi, non observé ici). Pour un utilisateur ou
   un bot, la page « Contact @… » ne livre rien : ni nom, ni photo, ni description. Limite : c'est du HTML
   non documenté, et les conditions d'utilisation (*Content Licensing and AI Scraping Terms*) réservent
   l'accès au contenu à « l'usage ordinaire, légitime et prévu de la plateforme ». Une lecture par minute
   d'une poignée de canaux publics d'échanges reste dans l'esprit de l'aperçu ; un moissonnage massif non.
2. **Bot API** (`api.telegram.org/bot<jeton>/getChat?chat_id=@canal`, gratuit, ≈ 30 requêtes par seconde
   au total) : `title, username, active_usernames, photo, description, pinned_message, linked_chat_id,
   location, emoji_status`, et `getChatMemberCount` pour les abonnés, **sans que le bot soit membre**. Un
   bot ne voit **rien d'un utilisateur** qui ne lui a pas écrit (`getChat`, `getUserProfilePhotos` : « chat
   not found »), ne reçoit **aucune mise à jour** quand un canal ou un utilisateur modifie son profil, et
   n'a jamais accès à la présence. Conditions des bots (§ 4.3) : ne collecter que l'essentiel au service
   rendu. C'est du *poll + diff* honnête pour des canaux.
3. **MTProto (compte utilisateur)**. Le serveur pousse `updateUserName` (nom, handles), `updateUser`
   (tout champ, avec le nouvel objet `user`), `updateUserEmojiStatus`, `updateChannel` (titre, photo,
   handle d'un canal) et `updateUserStatus` (présence, selon la confidentialité) pour les pairs que le
   compte a « vus » (contacts, dialogues ouverts, canaux rejoints ; pour les autres, la documentation ne
   garantit rien). `users.getFullUser` / `channels.getFullChannel` (mis en cache 60 s côté serveur) servent
   de filet, sous `FLOOD_WAIT` non documentés : `contacts.resolveUsername` peut imposer jusqu'à 24 h
   d'attente, donc résoudre une fois et garder les identifiants. Seule cette voie donne **l'historique des
   photos de profil avec leur date d'envoi** (`photos.getUserPhotos` → `photo.date`). Risque : tout compte
   connecté via un client non officiel est « placé sous observation » et un comportement automatisé mène au
   bannissement ; et cette voie ouvre la présence (`was_online`), que nous excluons.

Aucun horodatage de modification pour le nom, le handle, la bio ou la description, quelle que soit la
voie ; seules les photos sont datées. La **liste des abonnements d'un utilisateur n'existe pas** sur
Telegram. Le statut du compte se déduit : `deleted`, `USERNAME_NOT_OCCUPIED`, `CHANNEL_PRIVATE`,
`restricted` + `restriction_reason`, drapeaux `scam` / `fake`.

### 4.7 Reddit

*Poll* uniquement, et la porte se referme. `GET https://oauth.reddit.com/user/<u>/about` (jeton OAuth
obligatoire, `User-Agent` descriptif) renvoie `subreddit.title` (nom affiché), `subreddit.public_description`
(bio), `icon_img` / `snoovatar_img` (avatar), `subreddit.banner_img`, `subreddit.subscribers` (abonnés,
non documenté), karmas, `created_utc`, `verified`, `is_gold`, `is_employee`, `accept_followers`. Le handle
ne change jamais sur Reddit (pas de renommage). Aucune date de modification. Un compte suspendu répond
200 avec un objet tronqué (`is_suspended: true`) ; un compte supprimé, banni de l'ombre ou inexistant
répond 404, sans qu'on puisse les distinguer.

**Accès.** Les points d'accès `.json` sans authentification répondent **403 depuis le 28/05/2026**
(annonce r/modnews « Protecting communities from scrapers and platform abuse » ; confirmé d'ici
**[mesuré]**). Le palier gratuit est de **100 requêtes par minute** et par client OAuth (moyenne sur
10 min), mais la *Responsible Builder Policy* (05/06/2026) exige désormais **une approbation préalable**
avant tout accès aux données par l'API, la recherche passe par le programme *Reddit for Researchers*
(universitaires accrédités, BigQuery, gratuit), et tout usage commercial exige un contrat sans tarif
public (les 0,24 $ pour 1 000 appels souvent cités datent de 2023 et ne figurent sur aucune page
officielle). Le 05/08/2026, Reddit a annoncé sur r/redditdev qu'il « restreindra progressivement toutes
les nouvelles demandes » d'accès à la Data API publique au profit de sa plateforme Devvit.

**Règles.** Conditions d'utilisation (en vigueur au 01/07/2026, § 7) : toute collecte automatisée hors
accord écrit est interdite ; `robots.txt` interdit tout (`Disallow: /`) et Reddit a bloqué la Wayback
Machine le 12/08/2025 ; la *Public Content Policy* interdit le « profilage individuel » ; Reddit poursuit
les collecteurs (Anthropic, SerpApi, Oxylabs, Perplexity). Pour ce projet, Reddit n'est pas une source
de changements de profil : ni flux, ni accès garanti, et les comptes crypto qui comptent n'y changent
pas de profil, ils y publient.

### 4.8 Discord

**Pour un compte quelconque**, un bot (jeton gratuit, aucun consentement de la cible) peut lire
`GET /users/{id}` : `username, global_name` (nom affiché), `avatar, banner, accent_color, public_flags,
avatar_decoration_data, primary_guild` (étiquette de serveur). **Jamais** la bio (« À propos de moi »), les
pronoms, le statut personnalisé, la présence ni les activités, et **aucun événement** ne pousse les
changements d'un utilisateur hors de vos serveurs (`USER_UPDATE` ne concerne que le compte du bot). C'est
donc du *poll + diff* sur les seuls champs d'identité, sous **50 requêtes par seconde** par bot (limites
par route dans les en-têtes `X-RateLimit-*`, bannissement d'IP au-delà de 10 000 requêtes invalides par
10 min). Un compte supprimé répond 404 ; il n'existe ni notion de compte privé (la confidentialité de
profil masque bio, statut et badges aux non-amis, jamais le nom, l'avatar et la bannière), ni champ de
suspension, ni horodatage.

**Dans un serveur où le bot est installé** (par exemple le Discord officiel d'un projet), un flux poussé
existe : `GUILD_MEMBER_UPDATE` (intent privilégié `GUILD_MEMBERS`) part à chaque changement de pseudo, de
rôle, d'avatar de serveur, **et quand l'objet utilisateur d'un membre change** (nom, nom affiché, avatar,
bannière) ; `PRESENCE_UPDATE` (intent `GUILD_PRESENCES`) donne en ligne / absent / ne pas déranger et les
activités, dont le statut personnalisé (omis depuis le 17/09/2026 quand l'utilisateur le réserve à ses
amis). Ces deux intents se cochent librement sous 10 000 utilisateurs uniques ; au-delà, examen par Discord
(règle du 10/06/2026, qui remplace celle des 100 serveurs). Nous excluons la présence de toute façon.

**Règles.** *Developer Policy* (en vigueur depuis le 08/07/2024) : interdiction d'extraire ou de
moissonner des données (clause 20), d'utiliser les données de l'API pour **profiler des utilisateurs**
(clause 16) ou au-delà de la fonction déclarée de l'application (clause 15), de les vendre (18) ; les
*self-bots* (automatiser un compte utilisateur, seule façon d'obtenir la bio d'un tiers) sont interdits
sous peine de fermeture du compte. Un suivi des comptes officiels d'échanges et de projets **dans leurs
propres serveurs**, avec une fonction déclarée et une politique de confidentialité, est le seul schéma
conforme ; un pipeline de surveillance de personnes ne l'est pas.

### 4.9 LinkedIn

**Aucune voie conforme vers le profil d'un tiers.** L'API *Profile* ne renvoie que le membre authentifié
(`/v2/me`, `/v2/userinfo`) ; les profils d'autres membres exigent des API à accès restreint, et la
documentation interdit de **stocker** leurs données (cache de 24 h au plus dans les règles Marketing). Pour
les **pages d'organisation**, la *Community Management API* (entité juridique enregistrée, examen en deux
temps, palier Development plafonné à 500 appels par jour et par application) donne tous les champs, le
total et les gains quotidiens d'abonnés **uniquement pour les pages que le membre administre** ; pour les
autres organisations, `Organization Lookup` ne rend que `id, name, vanityName, website, logo, locations,
type` et un total d'abonnés (`networkSizes`), avec interdiction de conserver autre chose que le nom et
l'URL du logo pendant 30 jours. Le seul webhook (`ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS`) porte sur les
réactions aux publications des pages administrées, pas sur les champs de profil. Pas de dates de
modification (`versionTag`, opaque, réservé aux administrateurs), pas de statut de compte, pas de présence.

**Le *scraping* est interdit et poursuivi.** Conditions d'utilisation (03/11/2025, § 8.2) : interdiction
des robots, scripts et extensions qui copient les profils. LinkedIn a obtenu des jugements et injonctions
permanentes avec destruction des données contre hiQ (décembre 2022, 500 000 $), Mantheos (2022),
Proxycurl/Nubela (assigné en janvier 2025, service fermé le 04/07/2025) et ProAPIs (assigné en octobre
2025, jugement d'accord rapporté le 21/09/2026). En France, la **CNIL a sanctionné Kaspr de 240 000 €
(05/12/2024)** pour la collecte de coordonnées sur LinkedIn (art. 5-1-e, 6, 14 et 15 du RGPD). Les
revendeurs (Bright Data 1,5 $ pour 1 000 enregistrements, Apify dès 0,30 $, People Data Labs par crédits)
n'effacent ni la rupture contractuelle ni l'exposition RGPD de l'acheteur. **Conclusion : LinkedIn est
hors périmètre**, sauf pour suivre les pages que l'on administre soi-même.

---

## 5. Le *poll + diff* en pratique **[mesuré]**

Session D : 7 comptes (2 Bluesky, 2 Farcaster, 1 Mastodon, 2 canaux Telegram), une lecture toutes les
30 s pendant 4 min.

| | Valeur |
|---|---|
| Lectures | 63 (9 tours × 7 comptes), **0 erreur** |
| Temps de réponse par lecture | **0,14 s** (médiane), 0,25 s (p90) |
| Champs de profil modifiés en 4 min | **0** (nom, bio, avatar, bannière, lien, épinglé, statut) |
| Compteurs modifiés | abonnés de `bsky.app` : +9, +4, +7, +10, +8, +4, +12, +4 par tour ; abonnés de `t.me/durov` : de −8 à +3 par tour |

Deux enseignements. **Les compteurs bougent à chaque lecture** sur un grand compte : il faut les comparer à
part (`counter_deltas`), avec un seuil, et ne pas les mélanger aux champs d'identité. **Le coût d'un tour
est négligeable** (≈ 1 s pour 7 comptes) : la limite est le quota et le cache de chaque réseau, pas le
réseau lui-même. Avec les quotas ci-dessus, un poste unique peut lire à la minute environ **3 000 comptes
Bluesky** (25 par requête, 3 000 requêtes par 5 min), **300 comptes Mastodon par instance**, et sur X
300 comptes par quart d'heure en unitaire.

---

## 6. Prestataires, archives et outils

*Pages consultées le 27/09/2026, pour la plupart non datées. Aucun de ces prestataires ne se réclame de
l'API officielle de X ou de Meta : tous reposent sur du **scraping**, et tous reportent le risque juridique
sur le client (clauses d'indemnisation).*

**Il existe un push tiers pour les profils X.** `SocialData.tools` propose un *User Profile Monitor* : un
webhook `profile_update` avec **anciennes et nouvelles valeurs** de `name, screen_name, location, url,
description, profile_banner_url, profile_image_url`, « généralement sous 30 s », facturé à l'heure et par
compte suivi : **0,0069 $/h (≈ 4,99 $ par mois) de 1 à 10 comptes**, dégressif jusqu'à 0,0035 $/h
(≈ 2,49 $) au-delà de 1 000. Un *User Following Monitor* pousse les **nouveaux abonnements** (pas les
désabonnements) au même tarif. Ne déclenchent rien : les compteurs, la vérification, l'épinglé, la
suspension ; pour ceux-là, *poll* à **0,0002 $ le profil** (120 requêtes par minute). `twitterapi.io`
(0,18 $ pour 1 000 profils ; l'objet `user` porte `unavailable` / `unavailableReason` = `suspended`) et
`Sorsa` (ex-TweetScout, orienté crypto, « dès 0,02 $ pour 1 000 ») ne font que du *poll*. Comparé à l'API
officielle (0,01 $ par compte et par jour, § 4.1), le *poll* tiers coûte dix à cinquante fois moins, mais
la source peut disparaître du jour au lendemain.

**Les traders de memecoins ont déjà leurs outils.** `TweetStream` vend des événements `profile-change,
follow, unfollow, pin, delete` sur les comptes X que l'on enregistre, en WebSocket et Discord : 199 $ par
mois pour 50 comptes, 499 $ pour 250, « Ultra Speed » dès 1 500 $ par mois pour 10 comptes, avec une
latence annoncée de 172 ms (p50, source des données non divulguée, paiement en USDC accepté). Le *Tweet
Monitor* d'Axiom (gratuit au-delà de 5 SOL de volume) et les bots Telegram (`Phanes`, « Crypto Tweet
Tracker ») relaient des tweets ; leur documentation ne mentionne pas le suivi des changements de profil.
L'existence même d'un marché à 199–1 500 $ par mois pour ces alertes indique que le signal est **déjà
disputé** : ce qui est visible par tous en 172 ms n'est un avantage que pour celui qui exécute plus vite
que les autres abonnés, exactement le constat de `polymarket_temps_reel.md`.

**Autres plateformes : *poll* par scrapers.** Apify (X 0,15 $, TikTok 1 $, Instagram dès 1,60 $ pour
1 000 profils ; conditions du 09/07/2026 : le client est « seul responsable de la légalité » des données),
Bright Data (1,5 $ pour 1 000 enregistrements, 5 000 gratuits par mois ; a gagné contre Meta le 23/01/2024
et contre X le 10/05/2024 devant le tribunal fédéral de Californie du Nord, pour du *scraping* **hors
connexion** de données publiques), EnsembleData (dès 100 $ par mois), Data365 (dès 300 € par mois),
HikerAPI (Instagram, 0,0006 $ la requête), Phantombuster (69 à 439 $ par mois, mais **avec la session
connectée du client** : c'est précisément ce qui distingue les affaires perdues par Meta et X). Ces
décisions protègent le prestataire, pas son client, sont de première instance, et ne disent rien du RGPD.

**Archives : faibles pour ce besoin.** L'API CDX de la Wayback Machine (gratuite, 150 000 lignes par
requête) date des instantanés de pages entières, mais X impose une connexion depuis le 30/06/2023 et la
Wayback Machine signale « des limitations » sur ce site ; les pages de profil Instagram archivées « ne
fonctionnent pas » (ArchiveTeam). `archive.today` sauve des pages `x.com` mais sans API fiable, et sa
valeur probante est contestée (bannissement par Wikipédia le 20/02/2026). Pour Bluesky et Farcaster, le
rejeu des événements (§ 3.1, § 3.2) remplace avantageusement les archives. Les outils OSINT (Sherlock,
Maigret, Social Analyzer, socialscan) ne testent que l'**existence** d'un nom d'utilisateur à un instant
donné : aucun suivi de changement.

---

## 7. Cadre légal (France, UE)

*À compléter avec la lecture contradictoire (RGPD : donnée publique = donnée personnelle, intérêt légitime,
information, minimisation, durée ; recommandations CNIL sur le moissonnage ; DSA art. 40 ; opposabilité des
CGU ; jurisprudence).* Règles déjà appliquées ici : comptes publics d'organisations ou de personnalités
publiques uniquement ; champs strictement nécessaires ; aucune donnée de présence ; aucune republication.

---

## 8. Le signal vaut-il quelque chose ? Précédents et littérature

*À compléter avec la lecture contradictoire (épisodes Musk / DOGE, renommages avant lancements de
memecoins, études d'événement et vitesse de réaction).*

---

## 9. Ce que l'on ferait ensuite

1. **Banc d'essai gratuit** : brancher `stream` en continu sur Jetstream (et un hub Farcaster à jour) pour
   constituer, sans frais ni clé, un journal daté à la seconde des changements de profil des comptes crypto
   présents sur ces réseaux, puis le rapprocher des prix (`data.py`) par une étude d'événement, avec la
   méthodologie du projet (`methodologie.md` § 3, § 6 : chevauchement, tests multiples).
2. **X en *poll + diff*** : 100 à 300 comptes (échanges, fondateurs, projets), une lecture par compte toutes
   les 15 min via `/2/users/by`, ≈ 1 à 3 $ par jour ; ne stocker que les champs d'identité et leurs dates de
   changement, effacer le reste sous 24 h (Developer Agreement).
3. **Ne pas** scraper Instagram, TikTok, LinkedIn ; **ne pas** collecter la présence. Telegram : canaux
   publics par la page `t.me`, à 1 lecture par minute au plus.
4. Avant d'investir : mesurer sur le banc d'essai la **latence nécessaire** (§ 8). Si le marché réagit en
   moins de 5 min, le *poll* à 15 min sur X ne sert qu'à documenter, pas à trader — exactement la
   conclusion de l'étude Polymarket sur la copie de wallets.

---

## Sources

*(complétées avec la lecture contradictoire)*

- Bluesky : `public.api.bsky.app` (`app.bsky.actor.getProfile`, `getProfiles`, `com.atproto.repo.getRecord`), `plc.directory/<did>/log/audit`, Jetstream `wss://jetstream2.us-east.bsky.network/subscribe` et `wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents` — interrogés le 27/09/2026 ; https://bsky.network/docs/jetstream/ ; https://bsky.network/docs/jetstream-replay ; https://bsky.network/docs/rate-limits/ ; https://atproto.com/blog/relay-rollout (24/01/2026) ; lexiques `com/atproto/sync/subscribeRepos.json`, `app/bsky/actor/getProfiles.json` (github.com/bluesky-social/atproto) ; https://bsky.social/about/support/tos (14/08/2025) ; https://bsky.social/about/support/community-guidelines (19/09/2025) ; https://bsky.network/docs/developer-guidelines.
- Farcaster : `hub.pinata.cloud/v1/{info,userDataByFid,events,userNameProofByName}`, `api.farcaster.xyz/v2/{user-by-username,user}` — interrogés le 27/09/2026 ; https://docs.neynar.com/snapchain/httpapi/userdata ; https://docs.neynar.com/snapchain/datatypes/events (rétention 3 jours) ; https://docs.neynar.com/reference/what-are-the-rate-limits-on-neynar-apis (« Plan update (June 2026) ») ; https://docs.neynar.com/reference/compute-units ; https://docs.neynar.com/reference/publish-webhook ; https://docs.farcaster.xyz/reference/fname/api ; https://docs.farcaster.xyz/reference/farcaster/api ; https://snapchain.farcaster.xyz/getting-started ; https://docs.farcaster.xyz/developers/guides/basics/hello-world (hoyt protégé par mot de passe) ; https://docs.dune.com/data-catalog/community/farcaster/user_data.
- Nostr : `wss://relay.damus.io` — interrogé le 27/09/2026 ; NIP-01 https://github.com/nostr-protocol/nips/blob/master/01.md ; NIP-11 https://github.com/nostr-protocol/nips/blob/master/11.md.
- Mastodon : `mastodon.social/api/v1/accounts/lookup` — interrogé le 27/09/2026 ; https://docs.joinmastodon.org/methods/accounts/ (08/07/2026) ; https://docs.joinmastodon.org/entities/Account/ (06/08/2026) ; https://docs.joinmastodon.org/api/rate-limits/ ; https://docs.joinmastodon.org/methods/streaming/ (01/05/2026) ; https://docs.joinmastodon.org/admin/config/ (16/04/2026).
- Lens : https://lens.xyz/docs/protocol/getting-started/graphql ; https://lens.xyz/docs/protocol/tools/sns-notifications ; https://lens.xyz/terms (27/02/2024).
- GitHub : https://docs.github.com/en/rest/users/users ; https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api ; https://docs.github.com/en/site-policy/github-terms/github-terms-of-service (27/04/2026) ; https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies.
- Telegram : `t.me/durov`, `t.me/binance_announcements`, `t.me/binance` — interrogés le 27/09/2026.
- Prestataires et archives : https://docs.socialdata.tools/monitoring/create-user-profile-monitor.md ; https://docs.socialdata.tools/monitoring/pricing/ ; https://docs.socialdata.tools/getting-started/pricing/ ; https://twitterapi.io/pricing ; https://api.sorsa.io/ ; https://tweetstream.io/pricing ; https://docs.axiom.trade/tweet-monitor.md ; https://apify.com/xquik/x-profile-scraper ; https://docs.apify.com/legal/general-terms-and-conditions (09/07/2026) ; https://brightdata.com/pricing/web-scraper (10/09/2026) ; https://ensembledata.com/pricing ; https://data365.co/pricing ; https://hikerapi.com/ ; https://phantombuster.com/blog/ai-automation/phantombuster-pricing-explained/ (21/07/2026) ; https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server ; https://wiki.archiveteam.org/index.php/Twitter ; https://en.wikipedia.org/wiki/Archive.today ; https://github.com/soxoj/maigret.
- X : https://docs.x.com/x-api/getting-started/pricing ; https://docs.x.com/x-api/fundamentals/rate-limits ; https://docs.x.com/x-api/users/user-lookup-by-username ; https://docs.x.com/x-api/posts/filtered-stream/introduction ; https://docs.x.com/x-api/account-activity/introduction ; https://docs.x.com/x-api/enterprise-gnip-2.0/fundamentals/firehouse ; https://docs.x.com/changelog (paiement à l'usage, 06/02/2026).
- Instagram / Threads : https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery ; https://developers.facebook.com/docs/instagram-platform/webhooks ; https://developers.facebook.com/docs/threads/threads-profiles ; https://developers.facebook.com/docs/threads/webhooks ; https://transparency.meta.com/researchtools/meta-content-library (mis à jour le 30/04/2026).
- Facebook : https://developers.facebook.com/docs/features-reference/page-public-content-access ; https://developers.facebook.com/docs/features-reference/page-public-metadata-access ; https://developers.facebook.com/docs/graph-api/webhooks/reference/page/ ; https://developers.facebook.com/docs/content-library-api/data.
- TikTok : https://developers.tiktok.com/doc/research-api-specs-query-user-info (01/09/2026) ; https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info (04/08/2026) ; https://developers.tiktok.com/doc/webhooks-events (04/08/2026) ; https://apify.com/clockworks/tiktok-profile-scraper.
- YouTube : https://developers.google.com/youtube/v3/docs/channels/list (14/09/2026) ; https://developers.google.com/youtube/v3/docs/channels (16/09/2026) ; https://developers.google.com/youtube/v3/getting-started (quota) ; https://developers.google.com/youtube/v3/revision_history (01/06/2026, 31/01/2024) ; https://developers.google.com/youtube/v3/guides/push_notifications ; https://developers.google.com/youtube/terms/developer-policies (14/09/2026).
- Reddit : https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki (11/05/2026) ; https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy (05/06/2026) ; https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data (28/05/2026) ; https://redditinc.com/policies/data-api-terms (20/07/2026) ; https://redditinc.com/policies/user-agreement (01/07/2026) ; https://support.reddithelp.com/hc/en-us/articles/26410290525844-Public-Content-Policy ; https://www.reddit.com/dev/api/ ; https://praw.readthedocs.io/en/stable/code_overview/models/redditor.html.
- LinkedIn : https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api (30/04/2026) ; https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/organization-lookup-api?view=li-lms-2026-09 (28/04/2026) ; https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/organization-social-action-notifications?view=li-lms-2026-09 (10/06/2026) ; https://www.linkedin.com/legal/user-agreement (03/11/2025) ; CNIL, sanction Kaspr (05/12/2024) : https://www.cnil.fr/fr/prospection-commerciale-et-collecte-de-donnees-sur-linkedin-sanction-de-240-000-euros-lencontre-de-kaspr.
- Discord : https://discord.com/developers/docs/resources/user (Get User) ; https://discord.com/developers/docs/events/gateway-events (`GUILD_MEMBER_UPDATE`, `PRESENCE_UPDATE`, `USER_UPDATE`) ; https://discord.com/developers/docs/topics/rate-limits ; https://discord.com/developers/docs/policies-and-agreements/developer-policy (08/07/2024) ; https://support.discord.com/hc/en-us/articles/115002192352 (self-bots).
- Telegram : https://core.telegram.org/bots/api (Bot API 10.3, 24/08/2026) ; https://core.telegram.org/api/updates ; https://core.telegram.org/constructor/updateUserName ; https://core.telegram.org/method/photos.getUserPhotos ; https://telegram.org/blog/privacy-discussions-web-bots (31/05/2019, aperçu `t.me`) ; https://telegram.org/tos ; https://core.telegram.org/bots/terms.
