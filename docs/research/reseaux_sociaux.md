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
| **Flux poussé** : le réseau publie chaque modification de profil de tous ses utilisateurs | **Bluesky** (Jetstream / firehose), **Farcaster** (journal d'événements des hubs), **Nostr** (événements `kind 0`) | nom, bio, avatar, bannière, épinglé, handle, statut du compte (Bluesky) ; chaque champ avec sa date (Farcaster, Nostr) | **≈ 1 s** ; l'AppView public Bluesky reflète un changement **0,25 s** (médiane) après le firehose **[mesuré]** | gratuit, sans clé (Farcaster : hub public en retard de 10 mois d'ici, voir § 3.2) |
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
  annonce son quota : `ratelimit-policy: 3000;w=300` (3 000 requêtes par 5 min et par IP) ; l'AppView
  public n'envoie pas d'en-tête de quota.
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
porte pas) et `account` (`active`, `deactivated`, `takendown`, `suspended`, `deleted`).

| Session B (180 s) | Compte | Par minute | Par jour (extrapolé) |
|---|---|---|---|
| `commit` profil, `update` | 203 | 68 | ≈ 97 000 |
| `commit` profil, `create` | 59 | 20 | ≈ 28 000 |
| `identity` | 69 | 23 | ≈ 33 000 |
| `account` : `active` / `deactivated` / `takendown` / `deleted` | 65 / 4 / 3 / 5 | 26 | ≈ 37 000 |

Sur les 203 mises à jour, l'enregistrement portait un avatar dans 197 cas, une bannière dans 93, une bio
dans 128, aucun message épinglé. Le flux complet (toutes collections) est bien plus volumineux ; filtrer
sur `app.bsky.actor.profile` le ramène à **≈ 1,4 message par seconde**, tenable sur n'importe quelle
machine.

**Délai avant que le profil lu reflète le changement.** Pour chaque `update` reçu, le script relit
`getProfile` (avec anti-cache) jusqu'à ce que nom et bio correspondent à l'enregistrement du commit.
Session C (150 s, 174 mises à jour et 26 créations, soit 80 commits de profil par minute) : **40 commits
sur 40 reflétés**, après **0,25 s** en médiane, 0,25 s au p90, **1,75 s** au maximum, aucune erreur de
lecture (session B, n = 6 : 0,25 s / 0,70 s). Autrement dit, Jetstream *est* le temps réel, et un *poll*
de l'AppView derrière son CDN de 30 s ne fait que le rattraper. Les 35 événements `identity` de la
session C ne portaient pas de handle : il faut re-résoudre le compte après un tel événement.

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

**Mais le seul hub public joignable d'ici est en retard de dix mois.** `/v1/info` de `hub.pinata.cloud`
(version 0.14.2) annonce `blockDelay ≈ 25,5 millions de blocs` par shard ; la queue de son journal porte
des messages horodatés du **6 décembre 2025** et avance de **≈ 50 événements par minute** (session B :
147 événements en 180 s, dont 0 changement de profil), soit une resynchronisation au compte-gouttes. La
première mesure avait d'ailleurs pris ce retard pour du direct : 39 000 événements en 60 s, exactement
1 000 par page, c'est un arriéré qu'on rembobine, pas un flux. **Vérifier `blockDelay` avant toute
mesure** (`SocialClient.farcaster_hub_lag()`). Les autres hubs publics écoutent sur des ports non standard
(2281, 3381) que notre proxy bloque ; Neynar facture chaque requête (HTTP 402). Pour du direct, il faut donc
soit un serveur sans ce filtrage, soit un hub payant, soit faire tourner son propre nœud Snapchain.

**L'API du client Farcaster est en direct, gratuite et non documentée.** `GET https://api.farcaster.xyz/v2/user-by-username?username=dwr`
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
public renvoie en push chaque modification de profil de tout le réseau : **5,3 par minute** sur
`relay.damus.io` (session B, 180 s), reçues **1,5 s** après `created_at` (médiane ; horloge du client
émetteur, parfois fausse : on a vu des `created_at` dans le futur de 15 min). Les relais ne gardent que le
dernier `kind 0` par clé (événement *remplaçable*) : l'historique dépend des archives. Pertinence crypto
réelle mais étroite (communauté Bitcoin).

### 3.4 Mastodon (Fediverse)

`GET https://<instance>/api/v1/accounts/lookup?acct=<user>` : `display_name, note (HTML), avatar, header,
fields (liens vérifiés), followers_count, following_count, statuses_count, locked, last_status_at,
created_at`. Pas de date par champ. Quota annoncé dans les en-têtes : **300 requêtes par 5 min et par IP**
(`x-ratelimit-limit: 300`), cache 15 s (`max-age=15`), réponse en 0,05 s. L'API de streaming publique ne
couvre que les publications, pas les profils : c'est du *poll + diff* propre, à 1 requête par compte.

### 3.5 Résumé des flux poussés

| Réseau | Source | Couvre | Dates par champ | Délai | Contrainte |
|---|---|---|---|---|---|
| Bluesky | Jetstream / firehose | tous les comptes | `indexedAt` (profil), journal PLC (handle) | ≈ 0,3 s | aucune ; ≈ 1,4 msg/s après filtre |
| Farcaster | événements de hub (HTTP paginé ou gRPC) | tous les comptes | **oui** | ≈ 1 s (blocs) | hub à jour requis (payant ou nœud propre, ou ports ouverts) |
| Nostr | relais, `kind 0` | tous les comptes | **oui** (`created_at`) | ≈ 1,5 s | historique non garanti |
| Mastodon | — | — | non | *poll* | 300 req / 5 min / IP |

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

### 4.7 Reddit, Discord, LinkedIn

*À compléter avec la lecture contradictoire.* D'ici : `reddit.com/user/<u>/about.json` répond 403 sans
navigateur ; `discord.com/api` exige un jeton et ne livre la bio ou le statut d'un tiers qu'à un bot présent
dans un serveur commun ; LinkedIn n'offre aucune API vers les profils tiers et interdit le *scraping*.

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

*À compléter avec la lecture contradictoire (revendeurs de données, produits « alerte de changement de
profil » utilisés par les traders de memecoins, Wayback Machine / CDX pour reconstituer des états passés,
outils OSINT).*

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

- Bluesky : `public.api.bsky.app` (`app.bsky.actor.getProfile`, `getProfiles`, `com.atproto.repo.getRecord`), `plc.directory/<did>/log/audit`, Jetstream `wss://jetstream2.us-east.bsky.network/subscribe` — interrogés le 27/09/2026.
- Farcaster : `hub.pinata.cloud/v1/{info,userDataByFid,events,userNameProofByName}`, `api.farcaster.xyz/v2/{user-by-username,user}` — interrogés le 27/09/2026.
- Nostr : `wss://relay.damus.io` — interrogé le 27/09/2026.
- Mastodon : `mastodon.social/api/v1/accounts/lookup` — interrogé le 27/09/2026.
- Telegram : `t.me/durov`, `t.me/binance_announcements`, `t.me/binance` — interrogés le 27/09/2026.
- X : https://docs.x.com/x-api/getting-started/pricing ; https://docs.x.com/x-api/fundamentals/rate-limits ; https://docs.x.com/x-api/users/user-lookup-by-username ; https://docs.x.com/x-api/posts/filtered-stream/introduction ; https://docs.x.com/x-api/account-activity/introduction ; https://docs.x.com/x-api/enterprise-gnip-2.0/fundamentals/firehouse ; https://docs.x.com/changelog (paiement à l'usage, 06/02/2026).
- Instagram / Threads : https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery ; https://developers.facebook.com/docs/instagram-platform/webhooks ; https://developers.facebook.com/docs/threads/threads-profiles ; https://developers.facebook.com/docs/threads/webhooks ; https://transparency.meta.com/researchtools/meta-content-library (mis à jour le 30/04/2026).
- Facebook : https://developers.facebook.com/docs/features-reference/page-public-content-access ; https://developers.facebook.com/docs/features-reference/page-public-metadata-access ; https://developers.facebook.com/docs/graph-api/webhooks/reference/page/ ; https://developers.facebook.com/docs/content-library-api/data.
- TikTok : https://developers.tiktok.com/doc/research-api-specs-query-user-info (01/09/2026) ; https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info (04/08/2026) ; https://developers.tiktok.com/doc/webhooks-events (04/08/2026) ; https://apify.com/clockworks/tiktok-profile-scraper.
- YouTube : https://developers.google.com/youtube/v3/docs/channels/list (14/09/2026) ; https://developers.google.com/youtube/v3/docs/channels (16/09/2026) ; https://developers.google.com/youtube/v3/getting-started (quota) ; https://developers.google.com/youtube/v3/revision_history (01/06/2026, 31/01/2024) ; https://developers.google.com/youtube/v3/guides/push_notifications ; https://developers.google.com/youtube/terms/developer-policies (14/09/2026).
- Telegram : https://core.telegram.org/bots/api (Bot API 10.3, 24/08/2026) ; https://core.telegram.org/api/updates ; https://core.telegram.org/constructor/updateUserName ; https://core.telegram.org/method/photos.getUserPhotos ; https://telegram.org/blog/privacy-discussions-web-bots (31/05/2019, aperçu `t.me`) ; https://telegram.org/tos ; https://core.telegram.org/bots/terms.
