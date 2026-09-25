# Catalogue des indicateurs : prévoir la direction des cryptos à 5, 10 et 15 minutes

*État des lieux de la phase 1 : grosses cryptos BTC, SOL, puis ETH, XRP, BNB et DOGE, paires USDT sur Binance.*
*Barres de 1 minute, marché ouvert 24 h/24 et 7 j/7, horodatage UTC. Rédigé le 25/09/2026.*

Ce document recense les indicateurs utiles pour prévoir le **signe** de
`r_h(t) = ln(C[t+h] / C[t])` pour h = 5, 10 et 15 barres de 1 minute. Pour chaque
indicateur, il donne la formule causale, des paramètres adaptés aux barres de 1 minute,
la normalisation, le sens attendu (continuation ou retournement) et l'état de la
littérature empirique, en privilégiant les études sur la crypto. Il sert de référence
pour vérifier et compléter `src/tradebot/indicators.py`, en respectant le contrat de
`docs/ARCHITECTURE.md` : indicateurs strictement causaux, normalisés, jamais un
niveau de prix brut.

---

## 0. L'essentiel en 10 points

1. **À 5–15 minutes, le phénomène le plus solide est le retournement de court terme**
   (*mean reversion*), pas le momentum. Kitron et Wengrowicz (2026) trouvent un
   retournement directionnel significatif à 15 minutes sur **90 % des 183 paires
   Binance**, contre 2,7 % des actions américaines [K&W26]. Notre vérification locale
   sur BTC, ETH et SOL (juin–août 2026, § 2) le confirme : toutes les mesures de
   « prix trop haut ou trop bas » (rendement passé, RSI, z-score, écart au VWAP) ont un
   IC de Spearman **négatif et stable, de −0,02 à −0,06**.
2. **Ce signal est minuscule face aux frais.** Le contrarien pur a un hit-rate de
   51,5 à 52 %. Le mouvement médian sur 15 minutes est de 9 pb sur BTC, 11 pb sur ETH
   et 14 pb sur SOL, alors que l'aller-retour taker futures coûte 10 pb
   (`config.COST_BPS`). Kitron et Wengrowicz mesurent un avantage brut d'environ
   1,3 pb par trade pour 5 pb de coût. Il faudra filtrer (régime, volatilité,
   ampleur attendue) ou exécuter en maker.
3. **Le momentum crypto documenté se situe à d'autres échelles.** La littérature
   académique le trouve aux horizons journaliers et hebdomadaires [L&T21, LTW22]. En
   intraday, il apparaît à l'échelle d'une demi-heure ou d'une séance : la première
   demi-heure prédit la dernière (R² ≈ 1,4 %) [SUW22]. Rien de tout cela ne se
   transpose tel quel à 5–15 minutes. Les cassures de canaux marchent sur plusieurs
   jours [ZPB25], pas sur quelques minutes.
4. **Le flux d'ordres explique énormément le prix sur la même minute** : la
   composante commune du volume signé explique 80 % des rendements du BTC [M&S20], et
   il existe une relation linéaire entre OFI et variation de prix [CKS14, Sil19].
   **Son pouvoir prédictif s'éteint en quelques secondes.** À 5–15 minutes, le
   déséquilibre acheteur/vendeur taker a un IC **négatif** : les mouvements tirés par
   les ordres agressifs se retournent [K&W26, K&H26]. Localement, l'IC va de −0,01
   à −0,03.
5. **La volatilité est très prévisible, la direction presque pas.** Le regroupement
   de la volatilité, la saisonnalité horaire (pic à l'ouverture américaine, 13–15 h UTC)
   et la baisse de 25 à 45 % de \|r\| le week-end sont robustes [Ero19, Bau19, EOYZ24]. Ces
   indicateurs servent à **conditionner** et à normaliser, rarement à signer une
   prévision.
6. **Le retournement est bien plus fort le week-end.** Localement, l'IC de `ret_15`
   vaut −0,08 le week-end contre −0,016 en semaine sur BTC (−0,09 contre −0,03 sur
   ETH). C'est cohérent avec le lien entre liquidité et prévisibilité [WBXZ22]. Les
   interactions entre régime et retournement sont prioritaires.
7. **Côté dérivés, aux horizons de 5–15 minutes :**
   - Le **basis perp–spot**, c'est-à-dire le perp qui mène le spot [ACPS20], a un IC
     de +0,02 à +0,04 à 5 minutes. C'est le meilleur signal dérivé mesuré, mais sa
     négociabilité reste à vérifier.
   - La variation d'open interest et les ratios long/short ont des effets faibles
     (IC compris entre ±0,01 et ±0,03).
   - Le **funding est nul** à ces horizons. Il ne compte qu'aux horizons de plusieurs
     jours, où un carry élevé annonce des krachs [SST23].
8. **Les données disponibles en masse sont plus riches que ce que prévoit le
   contrat actuel.** `data.binance.vision` fournit aussi :
   - les klines **futures** 1 m, qui contiennent le flux taker du perp ;
   - `premiumIndexKlines` 1 m, pour le basis et le funding prévu ;
   - `bookDepth`, la profondeur cumulée à ±0,2 %, ±1 % … ±5 % toutes les 30 s, soit
     un **proxy de L2**. Localement, le déséquilibre de profondeur à ±1 % a un IC de
     +0,05 à +0,06 sur BTC en août 2026 ;
   - les `aggTrades` (flux au tick près) ;
   - l'indice de volatilité implicite `BVOLIndex`.
9. **Les effets calendaires sur la direction sont instables.** L'« effet de fin de
   bougie » (rendements positifs aux minutes 0, 15, 30 et 45) était net en 2021
   [SVS23]. Il est **négatif** à l'été 2026 dans nos données, alors que l'activité à
   ces minutes reste plus forte (+17 % de |r|) [K&H26]. Il faut éviter les variables
   muettes de direction non vérifiées hors échantillon.
10. **TimesFM sans covariables ne prévoit pas la direction.** Sur les rendements
    journaliers, TimesFM et Chronos en zéro-shot font moins bien qu'un modèle à base
    d'arbres (direction ≈ 50 %) [RNW25, NF26]. Pour tirer parti de TimesFM 3.0, il
    faut lui fournir les covariables ci-dessous (flux, basis, calendrier) et le
    comparer à la **baseline de retournement**, pas seulement à la marche aléatoire.

---

## 1. Conventions

### 1.1 Notations

- Barre `t` : horodatée à l'ouverture et connue à sa clôture. Elle contient
  `O, H, L, C`, `V` (volume en base), `Q` (volume en quote), `N` (nombre de trades) et
  `B` (volume taker acheteur en base), d'où `S = V − B` (volume taker vendeur).
- `r_t = ln(C_t / C_{t−1})` et `R_k(t) = ln(C_t / C_{t−k})`.
- `σ_n(t)` : écart-type des `r` sur les `n` dernières barres, `t` compris.
  `Σ_k x` est une somme glissante sur `k` barres. `EMA_n`, `SMA_n` : moyennes
  glissantes.
- `z_n(x) = (x − moyenne_n(x)) / écart-type_n(x)`, toujours sur une fenêtre
  **glissante et causale**, jamais sur l'échantillon complet.
- **IC** : corrélation de Spearman entre l'indicateur à `t` et `r_h(t)`.
  - Le **signe attendu** est celui de l'IC : « − » pour un retournement
    (contrarien), « + » pour une continuation, « 0 » pour aucun lien.
  - « \|r\| » signale une variable qui prédit l'**ampleur** du mouvement mais pas son
    signe. Elle sert de conditionnement.
  - « ± » signale un signe qui dépend du régime.

### 1.2 Paramètres à 1 minute

Les fenêtres standard (14, 20, 26…) ont été conçues pour des barres journalières. Nous
utilisons des fenêtres **multi-échelles** exprimées en minutes :
`{1, 5, 15, 30, 60, 240, 1440}`, soit de 1 minute à 1 jour.

Deux règles guident le choix :

- la fenêtre de l'indicateur doit être au moins égale à l'horizon, sauf pour les
  variables de choc (1 à 5 barres) ;
- les z-scores de normalisation utilisent 1440 barres (1 jour), ou un profil
  saisonnier par minute de la semaine calculé sur les 4 semaines passées.

### 1.3 Niveaux de preuve (tableau final)

Le niveau de preuve porte sur la **stabilité du signe hors échantillon**, pas sur la
rentabilité. Presque rien n'est rentable seul après frais.

| Niveau | Critère |
|---|---|
| **fort** | Plusieurs études crypto à un horizon comparable (≤ 1 h), et une mesure locale de même signe sur les 3 actifs et les 3 mois. |
| **moyen** | Une étude solide à un horizon comparable, ou plusieurs études à des horizons voisins, ou une mesure locale nette et stable sans appui académique direct. |
| **faible** | Preuve seulement à d'autres horizons (jour, semaine), source de praticiens, ou mesure locale petite ou instable. |
| **nul** | Les études, ou notre mesure, ne trouvent rien (IC ≈ 0), ou l'indicateur est jugé inadapté aux barres de 1 minute. |

---

## 2. Faits stylisés et vérification locale rapide

> **Protocole** (hors dépôt, scripts dans le répertoire temporaire de la session).
> - Données : klines spot 1 m Binance (`data.binance.vision`) pour BTCUSDT, ETHUSDT et
>   SOLUSDT, du 1er juin au 31 août 2026 (132 480 barres par actif, aucun trou).
> - Dérivés : fichiers `metrics` 5 min, décalés de +5 min pour la disponibilité comme
>   dans le contrat, `fundingRate`, klines futures et `premiumIndexKlines` 1 m.
> - `bookDepth` : août 2026 seulement.
> - IC de Spearman plein échantillon (n ≈ 132 000), avec le détail mois par mois.
>   Comme les cibles se chevauchent, n_eff ≈ n/h et l'erreur-type de l'IC vaut environ
>   0,006 à h = 5 et 0,011 à h = 15.
> - C'est un **contrôle de cohérence**, pas l'étude elle-même (voir `evaluation.py`).

### 2.1 Statistiques de base

| | BTC | ETH | SOL |
|---|---|---|---|
| Autocorrélation de r (1 m) aux retards 1 / 2 / 3 / 5 | +0,002 / −0,015 / −0,006 / −0,006 | +0,002 / −0,015 / −0,005 / −0,004 | +0,002 / −0,015 / −0,008 / −0,004 |
| Ratio de variance VR(5) / VR(15) / VR(60) | 0,97 / 0,92 / 0,98 | 0,97 / 0,93 / 0,98 | 1,01 / 0,93 / 0,90 |
| \|r_5\| moyen, en pb | 8,1 | 10,5 | 12,7 |
| \|r_15\| moyen / médian, en pb | 14,0 / 9,0 | 18,1 / 11,4 | 21,9 / 14,1 |
| Hit-rate du contrarien `signe(−R_15)` → `signe(r_15)` | 51,5 % | 52,1 % | 51,7 % |
| \|r_1m\| week-end / semaine, en pb | 2,28 / 4,04 | 3,34 / 5,19 | 4,57 / 5,99 |
| IC de `ret_15` → r_15, week-end / semaine | **−0,081** / −0,016 | **−0,090** / −0,030 | −0,043 / −0,017 |
| Rendement moyen d'une barre aux minutes 0/15/30/45 / aux autres minutes, en pb | −0,15 / +0,02 | −0,26 / +0,04 | −0,33 / +0,06 |
| \|r\| aux minutes 0/15/30/45 rapporté aux autres minutes | 1,17 | 1,16 | 1,17 |

Sur BTC, le \|r_1m\| moyen par heure UTC culmine à 13 h (5,1 pb), 14 h (5,8 pb) et 15 h
(5,0 pb), soit autour de l'ouverture de New York à 13 h 30 UTC en été. Il est au plus
bas à 10 h (2,7 pb). À ±2 minutes des fixings de funding (00 h, 08 h, 16 h), il n'y a
pas de pic de \|r\| (3,65 pb contre 3,54 pb en moyenne).

**Lecture.** Le retard 1 est presque nul, mais VR(15) < 1 et l'autocorrélation au
retard 2 est négative : le retournement est diffus sur plusieurs minutes. Kitron et
Wengrowicz font le même constat : « lag-one autocorrelation near zero on the major
coins, yet betting against the previous candle captures most of the effect » [K&W26].

### 2.2 IC de quelques indicateurs (h = 5 | h = 15)

| Indicateur (défini aux §§ 3–12) | BTC | ETH | SOL | Stabilité mensuelle (h = 15) |
|---|---|---|---|---|
| `ret_1` | −0,017 \| −0,012 | −0,027 \| −0,018 | −0,019 \| −0,011 | signe − sur 9/9 mois×actifs |
| `ret_5` | −0,037 \| −0,025 | −0,051 \| −0,034 | −0,033 \| −0,020 | 9/9 |
| `ret_15` | −0,032 \| −0,029 | −0,048 \| −0,041 | −0,031 \| −0,022 | 9/9 |
| `ret_60` | −0,031 \| −0,037 | −0,049 \| −0,058 | −0,032 \| −0,038 | 9/9 |
| `rsi_14` | −0,037 \| −0,034 | −0,057 \| −0,051 | −0,036 \| −0,028 | 9/9 |
| `zscore_60` (prix contre SMA 60) | −0,035 \| −0,035 | −0,055 \| −0,055 | −0,034 \| −0,030 | 9/9 |
| `vwap_dev_60` | −0,034 \| −0,036 | −0,054 \| −0,054 | −0,032 \| −0,030 | 9/9 |
| `taker_imb_5` (spot) | −0,022 \| −0,019 | −0,024 \| −0,015 | −0,017 \| −0,008 | 8/9 |
| `taker_imb_15` (spot) | −0,023 \| −0,027 | −0,018 \| −0,020 | −0,017 \| −0,008 | 8/9 |
| `taker_imb_60` (spot) | −0,022 \| −0,029 | −0,031 \| −0,045 | −0,010 \| −0,007 | 8/9 |
| `taker_imb_resid_15` (orthogonalisé à `ret_15`) | −0,012 \| −0,019 | −0,002 \| −0,007 | −0,005 \| +0,003 | instable hors BTC |
| `perp_taker_imb_15` (klines futures) | −0,026 \| −0,027 | −0,027 \| −0,020 | −0,020 \| −0,013 | — |
| `vol_z_60` | 0,000 \| +0,003 | +0,009 \| +0,013 | +0,010 \| +0,012 | faible |
| `avg_trade_size_z_60` | −0,005 \| −0,009 | +0,005 \| +0,004 | +0,002 \| +0,001 | nul |
| `rv_ratio_60_1440` | −0,007 \| −0,006 | −0,002 \| +0,002 | +0,002 \| +0,007 | nul |
| `clv_1` | +0,010 \| +0,010 | −0,008 \| −0,001 | −0,018 \| −0,009 | instable |
| `btc_ret_1` (pour les alts) | — | −0,023 \| −0,016 | −0,011 \| −0,008 | − |
| `btc_ret_5` (pour les alts) | — | −0,043 \| −0,029 | −0,026 \| −0,018 | − |
| `rel_strength_15` (alt contre BTC) | — | −0,032 \| −0,032 | −0,018 \| −0,009 | − |
| `basis_perp_spot` | **+0,039** \| +0,025 | +0,019 \| +0,013 | +0,023 \| +0,012 | — |
| `premium_idx` (Binance) | +0,003 \| +0,002 | −0,016 \| −0,007 | −0,003 \| −0,002 | nul |
| `funding_rate` | +0,003 \| +0,002 | −0,004 \| −0,009 | +0,007 \| +0,008 | nul |
| `oi_chg_15` | −0,005 \| −0,006 | −0,022 \| −0,030 | −0,004 \| −0,021 | − sauf BTC |
| `oi_chg_60` | +0,005 \| +0,011 | −0,026 \| −0,034 | −0,010 \| −0,022 | instable sur BTC |
| `taker_ls_ratio` (log, metrics 5 min) | −0,010 \| −0,014 | −0,011 \| −0,008 | −0,012 \| −0,007 | − |
| `ls_top_acc_chg_60` | +0,013 \| +0,017 | +0,023 \| +0,026 | +0,014 \| +0,025 | + (9/9) |
| `ls_global_acc_chg_60` | +0,010 \| +0,014 | +0,016 \| +0,017 | +0,004 \| +0,016 | + (8/9) |
| `ls_top_pos_z_1440` | +0,011 \| +0,016 | +0,004 \| +0,005 | −0,002 \| +0,003 | instable |
| `depth_imb_1` (bookDepth ±1 %, août seulement) | **+0,047** \| **+0,064** | n.d. | +0,016 \| +0,021 | 1 mois |
| `depth_imb_0p2` (±0,2 %) | +0,034 \| +0,037 | n.d. | −0,013 \| −0,017 | 1 mois, signe opposé sur SOL |

**Réserves.**
- Trois mois d'été 2026, sur un régime unique.
- La plupart des indicateurs « prix » et « flux » sont corrélés entre eux. Il faudra
  mesurer des IC **partiels**, conditionnés à `ret_h`, pour isoler l'information
  nouvelle.
- Le positif des ratios long/short « comptes » vient probablement du retournement :
  les particuliers achètent les baisses, puis le prix remonte. C'est à vérifier.

---

## 3. Tendance

| Nom proposé | Formule (causale) | Params 1 m | Normalisation | Lecture à 5–15 min |
|---|---|---|---|---|
| `ema_dist_{n}` | `(C − EMA_n(C)) / (C · σ_60)` | n = 5, 15, 60, 240 | unités de volatilité | niveau contre moyenne : **retournement (−)** pour n ≤ 60 ; ± pour n ≥ 240 |
| `ma_cross_{f}_{s}` | `(EMA_f − EMA_s) / (C · σ_60)` | (5, 20), (15, 60), (60, 240) | vol | momentum lissé : − à court terme, ± à long terme |
| `macd_hist_{f}_{s}_{g}` | `(MACD − EMA_g(MACD)) / (C · σ_60)` avec `MACD = EMA_f − EMA_s` | (12, 26, 9) ; version lente (60, 130, 45) | vol | oscillateur de court terme : − |
| `lin_slope_t_{n}` | statistique t de la pente de `ln C` régressé sur le temps (n barres) | 15, 60, 240 | sans dimension | − (court) / ± (long) |
| `adx_{n}` / `di_diff_{n}` | ADX de Wilder ; `(DI+ − DI−) / 100` | 14, 60 | [0, 1] ; [−1, 1] | ADX = régime (force de tendance, sans signe) ; `di_diff` : − |
| `aroon_osc_{n}` | `(AroonUp − AroonDown) / 100` | 25, 60 | [−1, 1] | − |
| `efficiency_ratio_{n}` | `|C_t − C_{t−n}| / Σ_n |ΔC|` (Kaufman) | 15, 60 | [0, 1] | régime : bruit contre tendance, à croiser avec `ret_n` |
| `kama_dist_{n}` | `(C − KAMA) / (C · σ_60)` | KAMA(10, 2, 30) | vol | − |
| `donchian_pos_{n}` | `(C − min L_n) / (max H_n − min L_n) − 0,5` | 20, 60, 240, 1440 | [−0,5 ; 0,5] | − pour n ≤ 60 ; les cassures sur n = 1440 relèvent de la tendance multi-jours [ZPB25] |
| `supertrend_dist`, `psar_dist` | `(C − niveau du stop) / ATR_n` | ATR 10 × 3 ; SAR (0,02 ; 0,2) | unités d'ATR | faible |
| `trix_{n}` | variation sur une barre de `EMA_n(EMA_n(EMA_n(C)))` | 15 | ×10⁴ / σ | − |
| `hma_slope_{n}` | pente de la Hull MA sur une barre / (C · σ_60) | 20, 60 | vol | − |
| `vortex_diff_{n}` | `VI+ − VI−` | 14 | ≈ [−1, 1] | − |
| `chop_{n}` | Choppiness Index | 14, 60 | /100 | régime |
| Ichimoku | — | paramètres (9, 26, 52) calibrés en jours | — | **déconseillé** à 1 m |

**Littérature.**
- Detzel et al. (2021) : le rapport du prix à ses moyennes mobiles de 5 à 100 jours
  prédit le rendement **journalier** du BTC, en échantillon et hors échantillon
  [DLSZZ21].
- Hudson et Urquhart (2021) : près de 15 000 règles techniques, avec correction du
  data-snooping ; prévisibilité significative, surtout en journalier [HU21].
- Grobys et al. (2020) : les règles de moyenne mobile fonctionnent surtout pour BTC
  [GAS20].
- Corbet et al. (2019) : sur des **barres de 1 minute** de BTC, soutien significatif
  aux stratégies « oscillateur de moyennes mobiles », moins aux cassures de supports et
  résistances [CELS19].
- Zarattini, Pagani et Barbon (2025) : un ensemble de canaux de Donchian
  multi-fenêtres est profitable sur plusieurs jours, et une version intraday est
  surtout rentable du dimanche soir au lundi, à l'ouverture asiatique [ZPB25].

**Pour nous.** À 5–15 minutes, un indicateur de « tendance » calculé sur une courte
fenêtre mesure surtout un **écart** qui tend à se résorber. Localement, `zscore_60` a
un IC de −0,03 à −0,055. On garde quelques versions longues (240, 1440) comme
variables de régime et pour le modèle combiné.

---

## 4. Momentum et oscillateurs

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture |
|---|---|---|---|---|
| `ret_{k}` | `R_k / (σ_60 · √k)` | k = 1, 2, 3, 5, 10, 15, 30, 60, 240, 1440 | unités de vol | **− (retournement)** pour k ≤ 60 ; ± pour k ≥ 240 |
| `ret_skip_{k}` | `ln(C_{t−1} / C_{t−k})`, sans la dernière barre | k = 5, 15, 60 | vol | sépare le rebond bid-ask de la dernière barre du retournement plus lent |
| `rsi_{n}` | RSI de Wilder, `(RSI − 50) / 50` | 6, 14, 30 | [−1, 1] | − (IC local de −0,03 à −0,06) |
| `stoch_k_{n}`, `stoch_d_{n}` | `%K = (C − min L_n) / (max H_n − min L_n)`, `%D = SMA_3(%K)`, centrés | (14, 3), (60, 3) | [−0,5 ; 0,5] | − |
| `williams_r_{n}` | `(max H_n − C) / (max H_n − min L_n)` | 14 | [0, 1] | + (miroir du stochastique) |
| `cci_{n}` | `(TP − SMA_n(TP)) / (0,015 · MAD_n)` avec `TP = (H + L + C) / 3` | 20 | /100, écrêté à ±3 | − |
| `tsi_{r}_{s}` | `EMA_s(EMA_r(ΔC)) / EMA_s(EMA_r(|ΔC|))` | (25, 13) | [−1, 1] | − |
| `ppo_{f}_{s}` | `(EMA_f − EMA_s) / EMA_s` | (12, 26) | ÷ σ_60 | − |
| `stoch_rsi_{n}` | stochastique appliqué au RSI | 14 | [0, 1] | − (plus bruité) |
| `ultimate_osc` | oscillateur de Williams (7, 14, 28) | idem | [0, 1] | − |
| `fisher_{n}` | transformée de Fisher de la position dans le range | 10 | ≈ [−3, 3] | − |
| `connors_rsi` | moyenne de RSI(3), RSI des séries (2) et rang centile de r (100) | idem | [0, 1] | − |
| `up_frac_{n}` | part des barres haussières sur n | 15, 60 | [0, 1] | − |
| `run_len` | longueur signée de la série de barres de même signe | — | écrêtée à ±10 | faible |
| `session_ret` | `ln(C / C_{00:00 UTC})` ; rendement depuis l'ouverture américaine | — | vol | ± (momentum de séance [SUW22]) |
| `ret_since_qh` | rendement depuis la dernière frontière de quart d'heure | — | vol | − [K&H26] |

**Littérature.**
- **Momentum journalier et hebdomadaire.** Liu et Tsyvinski (2021) : un écart-type de
  rendement du jour prédit +0,33 % le lendemain sur BTC ; en hebdomadaire, le quintile
  haut rapporte 11,22 % par semaine contre 2,60 % pour le bas [L&T21]. Liu, Tsyvinski
  et Wu (2022) : le facteur momentum fait partie du modèle à trois facteurs de la
  crypto [LTW22].
- **Momentum intraday à l'échelle de la séance.** Shen, Urquhart et Wang (2022) : la
  première demi-heure, définie par le volume, prédit la dernière demi-heure, avec un
  R² de 1,44 % en échantillon et un résultat confirmé hors échantillon. L'effet est
  plus fort quand la première séance est la plus active ou la plus volatile ; il
  s'explique par la fourniture de liquidité [SUW22].
- **Momentum et retournement coexistent.** Wen, Bouri, Xu et Zhao (2022, BTC
  2013–2020) : la prévisibilité change avec les sauts de prix, les annonces du FOMC et
  le niveau de liquidité ; le retournement propre à la crypto est attribué à la
  sur-réaction [WBXZ22].
- **Retournement à 15 minutes.** Kitron et Wengrowicz (2026) : significatif sur 90 %
  des paires Binance ; il se concentre après les mouvements tirés par les takers et
  croît avec l'intensité du flux ; avantage brut d'environ 1,3 pb pour 5 pb de coût
  [K&W26].
- **Quarts d'heure.** Kim et Hansen (2026, perps Binance BTC, ETH, XRP, SOL, DOGE,
  ADA de 2021 à 2024) : autocorrélation négative au retard 1 aux frontières de quart
  d'heure ; le flux d'ouverture prédit un rendement cumulé **négatif sur les 30
  premières minutes**, puis positif de 4 à 12 heures [K&H26].
- **Apprentissage automatique.**
  - Jaquart, Dann et Weinhardt (2021), BTC de 1 à 60 minutes : les variables
    techniques (rendements passés) dominent, et la précision, modeste, augmente avec
    l'horizon [JDW21].
  - Akyildirim et al. (2021) annoncent 55 à 65 % de précision en journalier et à la
    minute [AGS21]. C'est optimiste face aux études plus récentes.

**Pour nous.**
- Les rendements passés multi-échelles en unités de volatilité sont la **base** du
  modèle.
- Signe attendu « − » à k ≤ 60 minutes (niveau de preuve fort sur le signe).
- Les oscillateurs bornés (RSI, stochastique, CCI) sont des transformations non
  linéaires du même signal : utiles pour les modèles linéaires, largement redondants
  pour les modèles à base d'arbres.

---

## 5. Volatilité

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture |
|---|---|---|---|---|
| `rv_{n}` | `√(Σ_n r²)` | 15, 60, 240 | ÷ `rv_1440` ou ÷ profil saisonnier | \|r\| (prédit l'ampleur) |
| `rv_ratio_{a}_{b}` | `σ_a / σ_b` | (15, 240), (60, 1440) | sans dimension | \|r\| ; montée de la volatilité de court terme |
| `parkinson_{n}` | `√( Σ_n ln(H/L)² / (4 n ln 2) )` | 15, 60 | ÷ `rv_1440` | \|r\| (plus efficace que `rv` à 1 m) |
| `garman_klass_{n}` | `√( (1/n) Σ [½ ln(H/L)² − (2 ln 2 − 1) ln(C/O)²] )` | 15, 60 | idem | \|r\| |
| `rogers_satchell_{n}` | `√( (1/n) Σ [ln(H/C) ln(H/O) + ln(L/C) ln(L/O)] )` | 15, 60 | idem | \|r\| (robuste à la dérive) |
| `yang_zhang_{n}` | combinaison ouverture, clôture et Rogers-Satchell | 60 | idem | \|r\| |
| `atr_pct_{n}` | `ATR_n / C` | 14, 60 | ÷ médiane sur 1440 | \|r\| |
| `bb_pctb_{n}_{k}` | `(C − (SMA_n − kσ)) / (2kσ) − 0,5`, avec σ l'écart-type du prix | (20, 2), (60, 2) | [−0,5 ; 0,5] environ | **−** (niveau dans la bande) |
| `bb_width_{n}` | `4σ / SMA_n` ÷ sa médiane sur 1440 | 20, 60 | ratio | \|r\| (compression puis détente, « squeeze ») |
| `keltner_pos_{n}` | `(C − EMA_n) / (2 · ATR_n)` | 20 | ≈ [−1, 1] | − |
| `jump_ratio_{n}` | `max(RV − BV, 0) / RV`, avec `BV = (π/2) Σ |r_t| |r_{t−1}|` (variation bipuissance) | 60, 240 | [0, 1] | \|r\| ; la prévisibilité change après les sauts [WBXZ22] |
| `rskew_{n}`, `rkurt_{n}` | `√n Σ r³ / RV^{3/2}` ; `n Σ r⁴ / RV²` | 60, 240 | sans dimension | faible |
| `semivar_diff_{n}` | `(Σ r²·1[r>0] − Σ r²·1[r<0]) / RV` | 60 | [−1, 1] | faible, proche d'un momentum |
| `vol_of_vol_{n}` | écart-type de `rv_15` sur n | 240 | ÷ moyenne | \|r\| |
| `range_z_{n}` | `z_n((H − L) / C)` | 60 | z | \|r\| |
| `rv_seasonal_ratio` | `rv_15 / E[rv_15 | minute de la semaine]`, le profil étant estimé sur les 4 semaines passées | — | ratio | \|r\| ; volatilité anormale par rapport à l'heure |
| `bvol`, `vrp` | indice Binance `BVOLIndex` (vol implicite) ; `bvol − rv_1440` annualisé | — | z sur 30 jours | \|r\| ; direction non testée |

**Littérature.**
- Les estimateurs par étendue sont classiques : Parkinson (1980), Garman et Klass
  (1980), Rogers et Satchell (1991), Yang et Zhang (2000). Le modèle HAR de Corsi
  (2009) sert de référence pour prévoir la volatilité réalisée.
- En crypto, Easley, O'Hara, Yang et Zhang ont travaillé sur des barres de 1 minute
  Binance (BTC, ETH, XRP, SOL, ADA) de 2021 à 2023. Les mesures de microstructure
  prédisent **le signe de la variation de la volatilité réalisée** sur la journée
  suivante avec une précision de 0,56 à 0,58. L'asymétrie, elle, n'est pas prévisible
  (0,50) [EOYZ24].
- Pour les prix, Eross et al. (2019) décrivent une volatilité en forme de n au cours
  de la journée, la plus forte aux ouvertures des grandes places boursières [Ero19].

**Pour nous.** La volatilité **ne signe pas** la prévision : localement,
`rv_ratio_60_1440` a un IC ≈ 0. En revanche, elle conditionne l'**ampleur** du
mouvement, donc la probabilité de dépasser les frais, et module l'intensité du
retournement ou du momentum. Chez Shen et al., par exemple, le momentum est plus fort
quand la première séance est volatile [SUW22]. À utiliser en **interaction**
(`ret_k × rv_ratio`) et pour normaliser toutes les autres variables.

---

## 6. Volume

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture |
|---|---|---|---|---|
| `vol_z_{n}` | `z_n(ln(1 + V))` | 60, 1440 | z | \|r\| ; direction ≈ 0 (IC local ≈ 0 à +0,01) |
| `rel_volume_tod` | `V / médiane(V à la même minute de la semaine, 4 semaines passées)` | — | log | \|r\| |
| `obv_slope_{n}` | `(OBV_t − OBV_{t−n}) / Σ_n V`, avec `OBV` le cumul de `signe(r) · V` | 15, 60 | [−1, 1] | − (proche d'un momentum pondéré par le volume) |
| `cmf_{n}` | `Σ_n (CLV · V) / Σ_n V`, avec `CLV = ((C − L) − (H − C)) / (H − L)` | 20, 60 | [−1, 1] | faible |
| `mfi_{n}` | RSI pondéré par `TP · V` | 14 | [−1, 1] après centrage | − |
| `adl_slope_{n}` | pente de la ligne accumulation/distribution de Chaikin ÷ `Σ V` | 60 | ratio | faible |
| `vwap_dev_{n}` | `ln(C / VWAP_n)` avec `VWAP_n = Σ_n Q / Σ_n V` | 60, 240 | ÷ σ_60 | **−** (IC local de −0,03 à −0,055) |
| `vwap_dev_session` | idem avec le VWAP cumulé depuis 00:00 UTC | — | ÷ σ_60 | − |
| `force_index_{n}` | `EMA_n(ΔC · V) / (C · σ_60 · moyenne(V))` | 13 | z | − |
| `eom_{n}` | *Ease of Movement* : `Δ((H + L) / 2) · (H − L) / V`, lissé | 14 | z | faible |
| `pv_corr_{n}` | `corr_n(|r|, ln V)` | 60 | [−1, 1] | régime |
| `amihud_{n}` | `ln(moyenne_n(|r| / Q))` | 60, 1440 | z sur 30 jours | illiquidité : conditionne le retournement (plus fort quand le marché est illiquide) |
| `kyle_lambda_{n}` | pente de `r_t` régressé sur le flux signé `(B − S) · C` sur n barres | 60, 240 | z | impact : régime |
| `vol_shock_signed` | `signe(r) · 1[V > 3 · EMA_60(V)]` | — | {−1, 0, 1} | faible (−) |

**Littérature.**
- Balcilar et al. (2017) concluaient que le volume prédit le rendement du BTC, sauf
  dans les extrêmes haussiers et baissiers. **L'article a été rétracté** (conflit dans
  le processus éditorial) : à ne pas utiliser comme preuve [BBGR17].
- Chez Easley et al., Amihud et Kyle ont une importance faible, et Kyle est presque
  nul pour les prévisions crypto [EOYZ24].
- Shen et al. (2019) : l'attention mesurée par Twitter prédit le volume et la
  volatilité, **pas** le rendement [SUW19].

**Pour nous.** Le volume brut sert au **conditionnement** et à la normalisation, par
exemple pour pondérer le flux. Parmi les indicateurs de volume, seul l'écart au VWAP
est directionnel, parce que c'est un indicateur de niveau.

---

## 7. Flux d'ordres (volume taker acheteur Binance)

Binance fournit dans chaque kline `taker_buy_volume` (**B**), c'est-à-dire le volume
dont l'initiateur est l'acheteur agressif. Il n'est donc **pas nécessaire de deviner
le côté** (Lee-Ready, BVC) : c'est un avantage majeur des données crypto.

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture à 5–15 min |
|---|---|---|---|---|
| `taker_imb_{k}` | `(2 Σ_k B − Σ_k V) / Σ_k V` | 1, 5, 15, 60 | [−1, 1] | **−** (IC local de −0,01 à −0,03) |
| `taker_imb_ema_{n}` | `EMA_n(2B − V) / EMA_n(V)` | 5, 15 | [−1, 1] | − |
| `taker_imb_z_{k}` | `z_1440(taker_imb_k)` | k = 5, 15 | z | − ; corrige le biais structurel acheteur ou vendeur propre à chaque paire |
| `signed_vol_z_{k}` | `Σ_k (B − S) / écart-type_1440(Σ_k (B − S))` | 5, 15, 60 | z | − ; tient compte de la taille |
| `cvd_chg_z_{k}` | CVD = `cumsum(B − S)` ; `(CVD_t − CVD_{t−k}) / écart-type des incréments sur k` | 15, 60, 240 | z | − ; **le niveau du CVD n'est jamais stationnaire : ne pas l'utiliser brut** |
| `cvd_div_{k}` | `z(ΔCVD_k) − z(R_k)` : le prix monte sans flux acheteur, ou l'inverse (absorption) | 15, 60 | z | ± ; aucune étude trouvée |
| `taker_imb_resid_{k}` | résidu de `taker_imb_k` régressé sur `R_k` (régression glissante sur 1440 barres) | 15 | z | flux non absorbé par le prix ; IC −0,02 sur BTC, ≈ 0 ailleurs |
| `trades_z_{n}` | `z_n(ln N)` | 60, 1440 | z | \|r\| |
| `avg_trade_size_z_{n}` | `z_n(ln(V / N))` (ou `Q / N` en quote) | 60, 1440 | z | ≈ 0 (IC local ≈ 0) ; signale une activité « institutionnelle » |
| `vpin_{n}` | `Σ |B − S| / Σ V` sur les n derniers seaux de volume (1 seau = V journalier / 50) ; approximation sur n barres | 50 seaux ; 60 barres | [0, 1] | \|r\| et sauts ; direction ≈ 0 |
| `perp_taker_imb_{k}` | idem sur les **klines futures UM** (colonne taker présente) | 5, 15, 60 | [−1, 1] | − (IC local de −0,01 à −0,03) |
| `flow_div_spot_perp_{k}` | `taker_imb_k(spot) − taker_imb_k(perp)` | 15 | [−2, 2] | ± ; aucune étude |
| `taker_ls_ratio` | `ln(sum_taker_long_short_vol_ratio)` (metrics 5 min, disponible à T + 5 min) | — | z sur 1440 | − (faible) ; redondant avec `perp_taker_imb` |
| `qh_open_imb` | déséquilibre taker de la **première minute** suivant une frontière de quart d'heure | — | [−1, 1] | − sur 30 min, + de 4 à 12 h [K&H26] |

**Littérature.**
- **Lien contemporain.** Cont, Kukanov et Stoikov (2014) : la variation du prix est
  linéaire dans l'OFI (au meilleur prix), avec une pente inversement proportionnelle à
  la profondeur [CKS14]. Silantyev (2019), sur le perp BitMEX, de 1 seconde à 1 heure :
  le **déséquilibre de trades** (TFI) explique mieux la variation contemporaine que
  l'OFI [Sil19]. Makarov et Schoar (2020) : la composante commune du volume signé
  explique 80 % des rendements du BTC [M&S20].
- **Prévision à très court terme.** Bieganowski et Ślepaczuk (2026), perps Binance à
  la seconde de 2022 à 2025 : l'OFI est la variable la plus importante (SHAP) pour le
  rendement à 3 secondes, avec un effet monotone concave aux extrêmes [BS26].
- **Au-delà de quelques minutes, le flux se retourne.**
  - Kim et Hansen (2026) : le flux d'ouverture de quart d'heure prédit un rendement
    cumulé **négatif** sur 30 minutes [K&H26].
  - Kitron et Wengrowicz (2026) : le retournement se concentre après les mouvements
    tirés par les takers [K&W26].
  - Interprétation commune : fourniture de liquidité rémunérée.
- **Horizons plus longs.** Anastasopoulos et al. (2026, JFM) : le flux d'ordres
  mondial explique et prédit les rendements **journaliers et hebdomadaires**, domine les
  fondamentaux hors échantillon et a un effet permanent [AGLMT26].
- **Toxicité.** Le VPIN du BTC prédit les **sauts** [KKLT25]. Le VPIN moyen crypto
  (0,45–0,47) est environ deux fois celui des futures E-mini (0,22–0,23), et il est la
  2ᵉ variable la plus importante pour prévoir les statistiques de marché [EOYZ24].
- **Risque de krach.** Le déséquilibre d'ordres aide à estimer en temps réel le risque
  de krach du BTC [KW23].

**Pour nous.** Signe attendu **négatif** à 5–15 minutes (preuve moyenne). La variable
la plus prometteuse est le flux **en interaction** : le retournement grandit avec
l'intensité du flux [K&W26]. Termes à tester :
- `ret_k × |taker_imb_k|` ;
- `ret_k × signed_vol_z_k`.

---

## 8. Microstructure et price action (OHLCV)

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture |
|---|---|---|---|---|
| `clv_{n}` | moyenne sur n de `((C − L) − (H − C)) / (H − L)` | 1, 5, 15 | [−1, 1] | instable (IC local de −0,02 à +0,01) |
| `body_ratio` | `(C − O) / (H − L)` | 1 | [−1, 1] | faible |
| `upper_wick`, `lower_wick` | `(H − max(O, C)) / (H − L)`, `(min(O, C) − L) / (H − L)` | 1, moyenne sur 5 | [0, 1] | faible (mèche basse → + ?) |
| `range_sigma` | `(H − L) / (C · σ_60)` | 1 | vol | \|r\| |
| `gap_flag` | `|O_t − C_{t−1}| / C > 0` (reprise après maintenance) ou trou d'horodatage | — | {0, 1} | invalide les fenêtres (voir `targets.py`) |
| `roll_spread_{n}` | `2 √max(−cov(Δln C_t, Δln C_{t−1}), 0)` | 60, 240 | pb | liquidité ; 1re variable chez [EOYZ24] (pour la volatilité) |
| `roll_impact_{n}` | `roll_spread / Σ_n Q` | 60 | log, z | faible |
| `cs_spread_{n}` | écart de Corwin-Schultz, estimé à partir de H et L sur 2 barres | 60 | pb | liquidité |
| `ar_spread_{n}` | écart de Abdi-Ranaldo (CHL) | 60 | pb | liquidité |
| `zero_ret_frac_{n}` | part des barres avec r = 0 | 60 | [0, 1] | illiquidité et discrétisation (pas de cotation) |
| `dist_high_{n}`, `dist_low_{n}` | `(C − max H_n) / ATR_14`, `(C − min L_n) / ATR_14` | 60, 240, 1440 | unités d'ATR | − près des extrêmes récents ; cassures ± |
| `dist_day_open` | `ln(C / O_{00:00 UTC})` | — | ÷ σ_1440 | − (niveau) ou ± |
| `dist_prev_day_hl` | distance aux plus-haut et plus-bas de la veille UTC | — | ATR | faible |
| `round_dist_{g}` | distance au multiple de g le plus proche (BTC : 1000 et 500 ; ETH : 100 et 50 ; SOL : 10 et 5) | — | ATR | faible ou nul |
| `candle_*` | motifs (engulfing, marteau, doji…) | — | {0, 1} | nul |
| `drawdown_{n}` | `(C / max C_n − 1) / σ_60` | 240, 1440 | vol | − |

**Littérature.**
- **Mesure de Roll.** Easley et al. trouvent la mesure de Roll la plus importante pour
  prévoir les statistiques de marché, suivie du VPIN puis du Roll impact. Le BTC
  présente une autocorrélation nettement plus forte que les autres cryptos [EOYZ24].
- **Nombres ronds.** Urquhart (2017) : plus de 10 % des prix du BTC se terminent en
  « .00 », mais les rendements **ne suivent aucun motif** après ces nombres ronds
  [Urq17]. Griffin et Shams (2020) notent que certains flux se concentrent juste sous
  les prix ronds [GS20].
- **Minute dans l'heure.** Shanaev, Vasenin et Stepanov (2023) : le rendement se
  concentre aux minutes 0, 15, 30 et 45 (+0,58 pb par minute en 2021, sur 7
  plateformes) et il est négatif les autres minutes ; l'effet apparaît mi-2020 et
  s'affaiblit début 2022 [SVS23]. Localement, en 2026, le signe s'est **inversé**
  (−0,15 pb sur BTC) : la direction est instable.

**Pour nous.** Les estimateurs d'écart (Roll, Corwin-Schultz, Abdi-Ranaldo) et
d'illiquidité sont des **variables de régime** : le retournement devrait être plus fort
quand l'écart est large (compensation de la liquidité). Les motifs de chandeliers et
les nombres ronds sont à exclure du cœur du modèle.

---

## 9. Statistiques

| Nom proposé | Formule | Params 1 m | Normalisation | Lecture |
|---|---|---|---|---|
| `zscore_{n}` | `(C − SMA_n) / écart-type_n(C)` | 15, 60, 240 | z | **−** (IC local de −0,03 à −0,055) |
| `acf1_{n}`, `acf2_{n}` | autocorrélation glissante de r aux retards 1 et 2 | 240, 1440 | [−1, 1] | régime : très négative → retournement plus fort ; interaction avec `ret_k` |
| `vr_{q}_{n}` | `Var(R_q) / (q · Var(r))` sur n barres (Lo-MacKinlay) | q = 5, 15 ; n = 1440 | ≈ 1 | régime : < 1 retournement, > 1 tendance |
| `hurst_{n}` | exposant de Hurst par DFA (ou R/S) | 512, 1024 (recalculé toutes les 15 barres) | [0, 1] | régime : < 0,5 anti-persistant |
| `perm_entropy_{n}` | entropie de permutation d'ordre 4 | 240 | [0, 1] | régime (efficience) |
| `ou_halflife_{n}` | `−ln 2 / ln φ`, avec φ l'AR(1) de l'écart au SMA | 240 | log(minutes) | régime |
| `rskew_{n}`, `rkurt_{n}` | voir § 5 | 60, 240 | — | faible |
| `frac_diff_{d}` | différenciation fractionnaire de `ln C` (d ≈ 0,3–0,5) | fenêtre 1000 | z | alternative stationnaire au niveau de prix ; faible |
| `kalman_slope` | pente d'un filtre de Kalman local | — | vol | ± |

**Littérature.**
- **Méthodes.** Lo et MacKinlay (1988) pour le ratio de variance.
- **Efficience selon la fréquence.**
  - Zargar et Kumar (2019) : le BTC est efficient en journalier mais inefficient aux
    fréquences élevées (de 15 à 120 minutes) [ZK19].
  - Aslan et Sensoy (2020) : exposants de Hurst à 1, 5, 10, 15, 30 et 60 minutes sur
    BTC, ETH, XRP et LTC ; forts écarts de prévisibilité selon la fréquence [AS20].
  - Sensoy (2019) : le BTC devient plus efficient en intraday à partir de 2016 ; la
    liquidité améliore l'efficience, la volatilité la dégrade [Sen19].
  - Bariviera (2017) : les rendements journaliers deviennent efficients après 2014,
    mais la volatilité garde une mémoire longue [Bar17].
- **Dans nos données**, l'autocorrélation au retard 1 est ≈ 0 mais VR(15) vaut
  environ 0,92–0,93. Le retournement est donc **distribué sur plusieurs barres**, ce
  que le ratio de variance capte mieux que l'autocorrélation d'ordre 1.

**Pour nous.** Ce sont avant tout des **variables de régime** pour moduler le
retournement. Elles sont coûteuses à calculer (DFA) : il faut les vectoriser, ou les
calculer toutes les h barres puis les propager (*forward-fill*), ce qui reste causal.

---

## 10. Calendrier 24/7 (UTC)

| Nom proposé | Définition | Lecture |
|---|---|---|
| `hour_sin`, `hour_cos` | heure UTC encodée sur un cercle (période 24 h) | \|r\| fort (pic de 13 h à 15 h UTC) ; direction ≈ 0 |
| `minute_sin`, `minute_cos` | minute de l'heure (période 60) | \|r\| aux frontières |
| `dow_sin`, `dow_cos`, `is_weekend` | jour de la semaine ; samedi et dimanche UTC | week-end : \|r\| de −25 à −45 %, **retournement ×2,5 à ×5** (localement) |
| `is_monday_asia` | de dimanche 22:00 à lundi 02:00 UTC (ouverture de Tokyo à 00:00 UTC) | tendance intraday plus forte [ZPB25-i] |
| `session_asia`, `session_europe`, `session_us` | Tokyo 09:00–15:00 JST, Londres 08:00–16:30, NYSE 09:30–16:00 (heures **locales**, via `zoneinfo`, heure d'été incluse) | conditionnement |
| `us_open_window` | ±30 min autour de 09:30 heure de New York (13:30 UTC en été, 14:30 UTC en hiver) | \|r\| maximal |
| `us_close_window`, `london_open_window` | 16:00 New York ; 08:00 Londres | \|r\| |
| `min_to_daily_close` | minutes jusqu'à 00:00 UTC (clôture de la bougie journalière) | faible |
| `min_to_funding`, `is_funding_window` | minutes jusqu'au prochain fixing (00:00, 08:00 et 16:00 UTC pour BTCUSDT ; lire `funding_interval_hours`, car certains contrats sont sur 4 h) ; ±5 min | ≈ 0 (localement, ni pic de \|r\| ni IC) |
| `is_quarter_hour`, `min_to_quarter` | `minute % 15 == 0` ; `15 − minute % 15` | \|r\| +17 % ; direction **instable** (2021 : + [SVS23] ; 2026 : −) |
| `macro_event_window` | ±30 min autour des annonces américaines : CPI, NFP et PPI à 08:30 ET ; FOMC à 14:00 ET, avec conférence à 14:30 | \|r\| fortement accru ; direction ≈ 0 |
| `options_expiry` | échéances Deribit le vendredi à 08:00 UTC (mensuelles et trimestrielles : dernier vendredi) | faible |
| `month_end`, `quarter_end` | dernier jour du mois ou du trimestre | faible |
| `cme_closed` | *historique* : week-end et pause quotidienne des futures CME avant le 29/05/2026 (« gap CME ») | faible ; le CME est **ouvert 24/7 depuis le 29/05/2026** [CME26] |

**Littérature.**
- **Saisonnalité intraday.**
  - Eross et al. (2019) : volume, écart et volatilité en forme de n, liquidité maximale
    aux ouvertures des grandes places, marché peu liquide tôt le matin [Ero19].
  - Baur et al. (2019), 15 millions d'observations sur 7 plateformes : **pas de
    saisonnalité cohérente des rendements**, mais un volume plus faible le week-end et
    une activité accrue pendant les heures de Wall Street [Bau19].
  - Petukhina et al. (2021) : motifs intraday de volume et de volatilité [PRH21].
- **Jour de la semaine.** Caporale et Plastun (2019) : rendement du lundi plus élevé,
  pour BTC seulement [CP19]. Aharon et Qadan (2019) : même constat [AQ19]. L'effet
  s'est affaibli par la suite.
- **Heures précises.**
  - QuantPedia (Gemini, 2015–2023, source de praticiens) : rendements positifs
    concentrés à 22–23 h UTC [QP-seas]. Nous ne le retrouvons pas en 2026.
  - Zarattini et al. (2025, source de praticiens) : l'intraday tendanciel est surtout
    profitable du dimanche 19 h (heure de New York) au lundi [ZPB25-i].
  - Kim et Hansen (2026) : l'effet de quart d'heure est **indépendant** des heures de
    funding [K&H26].
- **Annonces macro.**
  - Benigno et Rosa (2023, NY Fed) : le BTC est **orthogonal** aux nouvelles
    monétaires et macro, contrairement aux autres classes d'actifs américaines [BR23].
  - Pyo et Lee (2020) : +0,96 % la veille du FOMC et −1 % le jour même, sans effet du
    CPI ni du PPI (données journalières) [PL20].
  - Une étude de FRL (2026) : dans l'heure suivant un communiqué du FOMC, le \|r\|
    moyen du BTC passe de 0,66 % à 1,25 %, et le volume est multiplié par 2,54
    [FRL26-FOMC].
  - La volatilité crypto réagit à environ un tiers des annonces américaines (CPI,
    décision du FOMC…) et commence à monter **avant** l'annonce [IREF25].
  - Wen et al. : le FOMC modifie la prévisibilité intraday [WBXZ22].

**Pour nous.** Le calendrier sert à **conditionner** : saisonnalité de la volatilité,
régime de liquidité (week-end), fenêtres d'annonces où le modèle doit s'abstenir.
Il ne faut **pas** apprendre de variables muettes de direction par heure sans
validation walk-forward. Le calendrier macro doit être une **liste statique**
versionnée (`data/`), car aucun appel réseau n'est permis hors de `data.py`.

---

## 11. Marché et cross-asset (BTC comme référence des altcoins)

| Nom proposé | Formule | Params 1 m | Lecture |
|---|---|---|---|
| `btc_ret_{k}` | `R_k` du BTC (colonne `bench_close`) pour une alt | 1, 5, 15 | grandes caps : **−** (IC local de −0,01 à −0,04 : retournement commun) ; petites caps : + (retard) |
| `rel_strength_{k}` | `R_k(alt) − R_k(BTC)` | 15, 60 | − (retournement relatif, IC local −0,01 à −0,03) |
| `resid_ret_{k}` | `R_k(alt) − β_1440 · R_k(BTC)` | 15, 60 | − |
| `beta_btc_{n}`, `corr_btc_{n}` | β et corrélation glissants | 1440 | régime |
| `btc_taker_imb_{k}` | flux taker du BTC pour l'alt | 5, 15 | − (à tester) |
| `ethbtc_ret_{k}`, `ethbtc_z_{n}` | rendement et z-score du ratio ETH/BTC | 60, 1440 | faible |
| `mkt_ret_{k}`, `dispersion_{k}` | moyenne et écart-type des rendements entre actifs (les 6 cryptos de l'univers) | 5, 15 | faible |
| `btc_dominance` | capitalisation du BTC / total (source externe, journalière) | — | nul à 5–15 min |
| `es_nq_ret_{k}` | futures S&P et Nasdaq (Yahoo, 1 m sur 30 jours, heures américaines) | 5, 15 | faible |
| `coinbase_premium` | `ln(Coinbase BTC-USD / Binance BTCUSDT)` (corrigé de USDT/USD) | — | faible ; proxy de la demande américaine |

**Littérature.**
- **Diffusion lente entre cryptos.** Guo, Sang, Tu et Wang (2024, JEDC), données
  Binance à la minute : les rendements retardés des autres cryptos prédisent le
  rendement d'une crypto donnée hors échantillon, et un portefeuille long-short reste
  rentable après frais. L'explication avancée est une diffusion lente de
  l'information due à l'attention limitée [GSTW24].
- **Taille de la crypto.** Kurihara et Matsumoto (2026) : causalité de Granger
  unidirectionnelle du BTC vers les alts ; les **petites caps** réagissent avec retard,
  alors que les grandes et moyennes réagissent **simultanément** [KM26].
- **BTC et ETH.** Sifat et al. (2019) : pas de relation avance-retard cohérente
  [SMS19].
- **Mesures croisées.** Chez Easley et al., les mesures de Roll et le VPIN du BTC
  comptent pour prévoir les autres cryptos [EOYZ24].

**Pour nous.** Pour ETH, SOL, XRP, BNB et DOGE, l'avance du BTC est absorbée **à
l'intérieur de la minute**. À 1 minute, on observe surtout un **retournement commun**
(IC négatif). Le BTC reste utile comme facteur de marché (résidu, β, flux), et le
retard pourrait exister pour DOGE : à tester, avec un signe attendu + ou −. Prévoir
`needs_benchmark=True` et la colonne `bench_close`, conformément au contrat.

---

## 12. Dérivés (futures USDⓈ-M Binance)

Correspondance des colonnes du fichier `metrics` avec celles du contrat :

- `sum_open_interest` → `open_interest` ;
- `sum_open_interest_value` → `open_interest_value` ;
- `sum_toptrader_long_short_ratio` → `ls_ratio_top` (positions des top traders) ;
- `count_toptrader_long_short_ratio` → comptes des top traders, **absent du contrat** ;
- `count_long_short_ratio` → `ls_ratio_accounts` (ensemble des comptes) ;
- `sum_taker_long_short_vol_ratio` → `taker_ls_ratio`.

*Interprétation « count = comptes, sum = positions » par analogie avec les points
d'API Binance (Top Trader Long/Short **Account** et **Position** Ratio) : à confirmer.*

| Nom proposé | Formule | Params 1 m | Lecture |
|---|---|---|---|
| `funding_rate` | dernier funding connu (à partir de `calc_time`) × 10⁴ | — | **≈ 0** à 5–15 min (IC local ≈ 0) |
| `funding_z_{n}` | z-score sur 90 fixings (30 jours) | — | ≈ 0 ; régime (levier) |
| `funding_ann` | `funding × (24 / funding_interval_hours) × 365` | — | lent : un carry élevé annonce des baisses et des krachs, **à l'horizon de plusieurs jours** [SST23] |
| `premium_idx` | clôture 1 m de `premiumIndexKlines` | — | ≈ 0 (IC local) |
| `funding_pred` | moyenne du `premium_idx` depuis le dernier fixing, avec la borne (*clamp*) de Binance | — | ≈ 0 (non testé) |
| `basis_perp_spot` | `ln(C_perp / C_spot)` aux mêmes minutes | — | **+** (IC local de +0,02 à +0,04 à h = 5) : le perp mène le spot |
| `basis_chg_{k}`, `perp_ret_minus_spot_1` | variation du basis ; `r_perp − r_spot` sur la dernière barre | 1, 5 | + (même mécanisme) |
| `oi_chg_{k}` | `Δ ln OI` sur k minutes (OI à 5 min, disponible à T + 5 min) | 5, 15, 60, 240 | − faible (ETH et SOL), ≈ 0 sur BTC |
| `oi_price_quadrant_{k}` | `signe(ΔOI_k) · signe(R_k)` : +1 = nouvelles positions dans le sens du prix ; −1 = rachats de shorts ou liquidations | 15, 60 | ± (≈ 0 localement) |
| `oi_to_volume` | `OI_value / Σ_1440 Q_perp` | — | régime (levier) |
| `ls_top_pos`, `ls_top_acc`, `ls_global_acc` | log des ratios ; z-score sur 1440 ; variation sur 60 minutes | 60, 1440 | + faible (IC local de +0,01 à +0,03), probablement via le retournement |
| `ls_smart_minus_retail` | `ln ls_top_pos − ln ls_global_acc` | — | ± (aucune étude) |
| `taker_ls_ratio` | voir § 7 | — | − faible |
| `bvol`, `vrp` | voir § 5 | — | \|r\| |
| `quarterly_basis_ann` | basis annualisé des futures datés | — | lent (non vérifié : disponibilité des klines datées sur `data.binance.vision` à confirmer) |

**Littérature.**
- **Prix des perpétuels.**
  - He, Manela, Ross et von Wachter (2022/2024) : prix de non-arbitrage des
    perpétuels ; les écarts sont plus grands qu'en devises, co-évoluent entre cryptos
    et diminuent avec le temps ; une stratégie d'arbitrage a un ratio de Sharpe élevé
    [HMRW22].
  - Ackerer, Hugonnier et Jermann (2026) : valorisation des perpétuels [AHJ26].
- **Carry et krachs.** Schmeling, Schrimpf et Todorov (2023, BIS) : le carry crypto
  peut atteindre 60 % par an ; il est tiré par la demande de levier des particuliers
  qui suivent la tendance et par la rareté du capital d'arbitrage, et un carry élevé
  annonce des **krachs** [SST23].
- **Découverte des prix.** Alexander, Choi, Park et Sohn (2020), données à la minute :
  les dérivés BitMEX **mènent** les prix spot, et les écarts bid-ask et
  inter-plateformes en sont les déterminants [ACPS20].
- **Signaux d'alerte avant les cascades de liquidations.** Garcia Seuma (2026) a
  étudié 7 cascades sur BTCUSDT de 2022 à 2025 (klines 1 m et metrics 5 min), dont
  celle du 10/10/2025, record avec plus de 19 milliards de dollars liquidés. **Aucune
  variable n'est commune à tous les événements** (OI, ratios long/short, variance…).
  Seule la **compression de la variance du flux taker** passe les placebos, et le
  signal du 10/10/2025 ne se réplique pas hors échantillon [GS26].
- Aucune étude académique évaluée par les pairs ne teste les ratios long/short Binance
  à 5–15 minutes. La variation du positionnement des particuliers (données CFTC
  hebdomadaires) prédit les rendements crypto à l'horizon hebdomadaire [FRL23-COT].

**Pour nous.**
- **Priorité** : le basis perp–spot et le flux taker du perp. Il faut ajouter à
  `data.py` les klines futures 1 m et `premiumIndexKlines`, tous deux accessibles en
  masse.
- **Rôle de régime** : funding, OI et ratios long/short. Le funding sert de filtre de
  levier sur plusieurs jours.
- **Attention** : le signal du basis porte sur la **clôture spot**. Si l'on négocie le
  perp, l'avantage peut disparaître, car le perp a déjà bougé. Il faut aussi vérifier
  que les deux clôtures sont synchrones (dernier trade de la minute).

---

## 13. Pistes hors de nos données actuelles

| Piste | Accès | Ce que dit la littérature | Intérêt à 5–15 min |
|---|---|---|---|
| **Profondeur du carnet (`bookDepth`)** : notionnel cumulé à ±0,2 %, ±1 %, ±2 %… ±5 %, toutes les 30 s ; `depth_imb_{x} = (bid_x − ask_x) / (bid_x + ask_x)` | **accessible** : `data.binance.vision/data/futures/um/daily/bookDepth/{SYM}/…` (perps) | Déséquilibre de file (*queue imbalance*) : prédicteur du prochain tick [GB16] ; microprix [Sto18] ; OFI au meilleur prix [CKS14] ; OFI multi-niveaux par apprentissage profond [KTW23] | IC local de **+0,05 à +0,06** sur BTC à ±1 % (1 mois) ; mitigé sur SOL. **À étendre.** |
| **OFI L1 et microprix** (`bookTicker` : meilleurs bid et ask) | `bookTicker` futures introuvable (404) pour 2026 ; websocket en direct | Relation linéaire contemporaine et prévision à la seconde [CKS14, Sto18, BS26] | Faible à 5–15 min (décroît en quelques secondes) |
| **L2 complet ou LOB profond** (DeepLOB…) | websocket en direct uniquement | DeepLOB [ZZR19] ; LOB BTC avec processus de Hawkes (2026) [HawkesBTC26] | Au-delà de la minute : non démontré |
| **`aggTrades`** (tick par tick, `isBuyerMaker`) | **accessible** en masse, spot et futures | Kim et Hansen (flux à 10 s, R² hors échantillon de 3,4 %, AUC 0,60 aux frontières de quart d'heure) [K&H26] | Taille des trades acheteurs contre vendeurs, gros ordres, rafales, flux à 10 s |
| **Liquidations** | fichiers `liquidationSnapshot` introuvables (404) ; websocket `forceOrder` en direct ; agrégateurs payants | Études d'événements [GS26] ; mécanique des cascades | Signal fort pendant les cascades, rare ; à enregistrer en direct |
| **Volatilité implicite** (`BVOLIndex`, DVOL de Deribit, skew 25Δ) | `BVOLIndex` **accessible** (à la seconde) | Pas d'étude à haute fréquence trouvée | \|r\| et régime |
| **On-chain** : flux vers et depuis les plateformes, émission de stablecoins | payant (CryptoQuant, Glassnode) | Les entrées nettes d'USDT sur les plateformes prédisent positivement BTC et ETH, surtout à 1 h ; les entrées nettes de BTC ne prédisent rien sauf à 4 h ; celles d'ETH prédisent négativement [CCH24]. L'émission d'USDT suit les baisses et précède des hausses (2017) [GS20]. | Horizon ≥ 1 h : faible à 5–15 min |
| **Sentiment** (X/Twitter, Reddit, nouvelles, Fear & Greed) | API payantes ; indice F&G journalier | Les tweets prédisent le volume et la volatilité, pas le rendement [SUW19] ; l'attention (Google) prédit à 1–2 semaines [L&T21] | Nul à faible |
| **Écarts entre plateformes** (prime Coinbase, OKX, Kraken) | Coinbase Exchange, OKX et Kraken **accessibles** depuis l'environnement | Arbitrage et volume signé commun [M&S20] ; découverte des prix [ACPS20] | Faible à moyen, à tester |
| **Flux des ETF spot**, futures actions (ES, NQ) | journalier ; Yahoo 1 m sur 30 jours | Déconnexion macro [BR23] | Faible |

---

## 14. Modèles, apprentissage automatique et TimesFM : ce que l'on peut espérer

- **Ordres de grandeur réalistes à ces horizons.**
  - Jaquart et al. (2021, BTC de 1 à 60 min) : précision à peine supérieure à 50 %,
    qui croît avec l'horizon ; les variables techniques dominent [JDW21].
  - Kitron et Wengrowicz (2026) : écart d'AUC de +0,011 entre cryptos et actions
    [K&W26].
  - Kim et Hansen (2026) : AUC de 0,60 et R² hors échantillon de 3,4 %, mais sur des
    fenêtres de **10 secondes** aux frontières de quart d'heure, avec un signal moyen
    d'environ 0,5 pb, soit un dixième des frais taker [K&H26].
- **La volatilité est plus prévisible que la direction** : précision de 0,56 à 0,58
  pour le signe de sa variation [EOYZ24].
- **Modèles de fondation pour séries temporelles (TSFM).**
  - Rahimikia, Ni et Wang (2025), rendements excédentaires journaliers mondiaux :
    TimesFM (500 M) atteint un R² hors échantillon de −2,80 % avec une direction
    légèrement **inférieure à 50 %** ; Chronos-large, −1,37 % avec une direction
    d'environ 51 %. Le réglage fin aide peu ; le pré-entraînement sur des données
    financières aide [RNW25].
  - Noguer i Alonso et Franklin (2026) : gains très faibles face à la marche
    aléatoire, même pour TimesFM-2.5 [NF26].
  - Brini (2026) : pour la volatilité réalisée, aucun TSFM ne domine uniformément le
    modèle HAR [Bri26].
  - Kronos, un modèle de fondation pré-entraîné sur des chandeliers de 45 plateformes,
    revendique un RankIC nettement supérieur aux TSFM génériques [Kro25]. C'est une
    alternative à évaluer.
- **TimesFM 3.0** (330 M de paramètres, sorti le 31/08/2026) accepte nativement les
  covariables passées [TFM3]. Il faut lui fournir en covariables les indicateurs
  retenus ci-dessus : flux taker, basis, volatilité relative, calendrier.

---

## 15. Recommandations pour `src/tradebot/indicators.py`

1. **Noyau obligatoire, par famille.**
   - **momentum** : `ret_{1,5,15,60,240}` en unités de volatilité,
     `ret_skip_{5,15,60}`, `rsi_{6,14,30}`, `stoch_k_14` ;
   - **statistique** : `zscore_{15,60,240}` ;
   - **volume** : `vwap_dev_{60,240}` et `vwap_dev_session` ;
   - **volatilité** : `bb_pctb_20_2`.

   Tous ont un signe attendu **négatif** et restent pertinents seuls.
2. **Flux**, avec `needs_columns=("taker_buy_volume",)` :
   `taker_imb_{1,5,15,60}`, `signed_vol_z_{5,15}`, `cvd_chg_z_{15,60}`,
   `taker_imb_resid_15`, `avg_trade_size_z_60`, `trades_z_60`, `vpin_60`.
3. **Régimes et interactions** (nouvelles familles de variables, à évaluer en
   priorité) :
   - `ret_15 × is_weekend` ;
   - `ret_15 × |taker_imb_15|` ;
   - `ret_15 × rv_ratio_60_1440` ;
   - `ret_15 × acf1_1440` ;
   - `ret_15 × amihud_1440`.
4. **Volatilité et liquidité (conditionnement)** : `rv_{15,60}`, `parkinson_60`,
   `garman_klass_60`, `rv_ratio_60_1440`, `rv_seasonal_ratio`, `atr_pct_14`,
   `bb_width_20`, `jump_ratio_240`, `roll_spread_240`, `cs_spread_60`,
   `amihud_1440`, `zero_ret_frac_60`.
5. **Calendrier**, en UTC via `zoneinfo`, sans variable muette de direction :
   `hour_sin/cos`, `minute_sin/cos`, `dow_sin/cos`, `is_weekend`, `is_monday_asia`,
   `us_open_window`, `session_*`, `is_quarter_hour`, `min_to_funding`,
   `macro_event_window` (à partir d'une liste statique).
6. **Marché** (`needs_benchmark=True`) : `btc_ret_{1,5,15}`, `rel_strength_{15,60}`,
   `resid_ret_15`, `beta_btc_1440`, `corr_btc_1440`.
7. **Dérivés** (`needs_columns` selon la colonne) : `funding_rate`,
   `funding_z_90`, `oi_chg_{15,60,240}`, `oi_price_quadrant_15`,
   `ls_*_chg_60`, `ls_*_z_1440`, `taker_ls_ratio`. Si `data.py` ajoute les colonnes
   `perp_close`, `perp_taker_buy_volume`, `perp_volume` et `premium_idx` :
   `basis_perp_spot`, `basis_chg_{1,5}`, `perp_taker_imb_{5,15,60}`,
   `premium_idx`.
8. **Normalisation.**
   - Toujours diviser par `σ_60` ou `σ_1440` des rendements 1 m, ou utiliser des
     z-scores glissants sur 1440 barres. Aucune statistique calculée sur l'échantillon
     complet.
   - Le volume se normalise **par la minute de la semaine** (profil calculé sur les 4
     semaines passées).
   - Utiliser `min_periods` égal à la fenêtre, pour que les valeurs soient NaN tant
     que l'historique est insuffisant.
9. **Évaluation** (pour `evaluation.py`).
   - Mesurer l'IC **partiel**, conditionné à `ret_h`, en plus de l'IC brut, et
     comparer systématiquement à la baseline `reversal_h`.
   - Utiliser `n_eff = n / h`, contrôler le FDR, et exiger la stabilité du signe mois
     par mois et entre actifs.
   - Séparer week-end et semaine.
10. **Pièges de données constatés**, à transmettre au propriétaire de `data.py` :
    - les lignes des fichiers `metrics` ne sont **pas triées** dans le temps (22
      inversions dans BTCUSDT-metrics-2026-09-01) : trier puis dédupliquer ;
    - en pandas 3, `merge_asof` échoue si les index sont à des résolutions
      différentes (`datetime64[us]` contre `[ms]`) : forcer `.as_unit("us")` des deux
      côtés ;
    - `fundingRate` contient `funding_interval_hours` (8 pour BTCUSDT, parfois 4
      ailleurs) : ne pas coder 00/08/16 en dur ;
    - dans `premiumIndexKlines`, le volume est nul : seules les colonnes OHLC sont
      significatives ;
    - les klines futures ont les mêmes 12 colonnes que le spot (avec une ligne
      d'en-tête), taker compris ;
    - `bookDepth` : une ligne par (horodatage, niveau), ±0,2 %, ±1 % … ±5 %, toutes
      les 30 s.
11. **Économie.** Avec 51–52 % de hit-rate et un \|r_15\| médian de 9 pb sur BTC, une
    règle simple perd contre 10 pb de frais taker. Il faudra un seuil sur
    l'**espérance** (proba × ampleur attendue), une exécution maker (4 pb), ou un
    trading limité aux fenêtres de forte volatilité (ouverture américaine, annonces
    macro exclues).

---

## 16. Tableau récapitulatif

*Signe attendu* : signe de l'IC avec `r_h` à 5–15 minutes (voir § 1.1). *Preuve* : voir
§ 1.3. « IC loc. » = mesure locale de juin à août 2026 (§ 2).

| Indicateur | Famille | Params 1 m | Hypothèse de signe attendue | Niveau de preuve |
|---|---|---|---|---|
| `ret_k` (rendement passé, en vol) | momentum | k = 1, 5, 15, 60 | − (retournement) | **fort** (K&W26, WBXZ22, K&H26 ; IC loc. de −0,01 à −0,06, 9/9) |
| `ret_k` long | momentum | k = 240, 1440 | ± | faible (momentum journalier L&T21, pas à 5–15 min) |
| `ret_skip_k` | momentum | 5, 15, 60 | − | moyen |
| `session_ret` / momentum de séance | momentum | depuis 00:00 UTC ou l'ouverture américaine | + (fin de séance) | faible à 5–15 min (moyen à 30 min, SUW22) |
| `rsi_n` | momentum | 6, 14, 30 | − | **moyen** (IC loc. de −0,03 à −0,06 ; TA à 1 m, CELS19) |
| `stoch_k/d`, `williams_r` | momentum | (14, 3) | − / + | moyen (redondant avec RSI) |
| `cci_20`, `tsi`, `ppo`, `fisher`, `ultimate_osc`, `connors_rsi`, `stoch_rsi` | momentum | voir § 4 | − | faible (redondants) |
| `ret_since_qh` | momentum | frontière de 15 min | − | faible |
| `ema_dist_n` | tendance | 5, 15, 60 | − | moyen |
| `ma_cross_f_s` | tendance | (5, 20), (15, 60), (60, 240) | − court / ± long | faible |
| `macd_hist` | tendance | (12, 26, 9), (60, 130, 45) | − | faible |
| `lin_slope_t_n` | tendance | 15, 60, 240 | − / ± | faible |
| `adx_n`, `chop_n`, `efficiency_ratio_n` | tendance (régime) | 14, 60 | aucun (régime) | faible |
| `di_diff_n`, `aroon_osc_n`, `vortex_diff_n`, `trix_n`, `hma_slope_n`, `kama_dist_n` | tendance | 14–60 | − | faible |
| `donchian_pos_n` | tendance | 20, 60 / 1440 | − court / + cassure longue | faible (multi-jours ZPB25) |
| `supertrend_dist`, `psar_dist` | tendance | ATR 10 × 3 | ± | faible |
| Ichimoku | tendance | — | — | nul (inadapté à 1 m) |
| `rv_n`, `parkinson_n`, `garman_klass_n`, `rogers_satchell_n`, `yang_zhang_n`, `atr_pct_n` | volatilité | 15, 60 | \|r\| (pas de signe) | **fort** pour \|r\| ; nul pour la direction |
| `rv_ratio_a_b`, `rv_seasonal_ratio` | volatilité | (15, 240), (60, 1440) | \|r\| ; interaction | moyen (\|r\|) / nul (direction, IC loc. ≈ 0) |
| `bb_pctb_n_k`, `keltner_pos_n` | volatilité | (20, 2), (60, 2) | − | moyen |
| `bb_width_n` (squeeze) | volatilité | 20, 60 | \|r\| | faible |
| `jump_ratio_n` (RV − BV) | volatilité | 60, 240 | \|r\| ; modifie la prévisibilité | faible |
| `rskew_n`, `rkurt_n`, `semivar_diff_n` | volatilité | 60, 240 | ± | faible |
| `bvol`, `vrp` | volatilité / dérivés | — | \|r\| | faible |
| `vol_z_n`, `rel_volume_tod`, `trades_z_n` | volume | 60, 1440 | \|r\| ; ≈ 0 pour la direction | moyen (\|r\|) / nul (direction) |
| `vwap_dev_n`, `vwap_dev_session` | volume | 60, 240 | − | **moyen** (IC loc. de −0,03 à −0,055, 9/9) |
| `obv_slope_n`, `mfi_n`, `force_index_n` | volume | 14–60 | − | faible |
| `cmf_n`, `adl_slope_n`, `eom_n` | volume | 20–60 | ± | faible |
| `amihud_n`, `kyle_lambda_n` | volume (liquidité) | 60, 1440 | régime (retournement plus fort si illiquide) | faible (EOYZ24 : faible importance) |
| `vol_shock_signed` | volume | EMA 60 × 3 | − | faible |
| `taker_imb_k` (spot) | flux | 1, 5, 15, 60 | − | **moyen** (K&W26, K&H26 ; IC loc. de −0,01 à −0,03) ; + seulement à quelques secondes |
| `signed_vol_z_k`, `cvd_chg_z_k` | flux | 5, 15, 60 | − | moyen |
| `cvd_div_k` (absorption) | flux | 15, 60 | ± | faible (aucune étude) |
| `taker_imb_resid_k` | flux | 15 | − | faible (BTC seulement) |
| `ret_k × |taker_imb_k|` | flux × momentum | 15 | − (retournement accentué) | moyen (K&W26) |
| `avg_trade_size_z_n` | flux | 60, 1440 | ≈ 0 | nul (IC loc. ≈ 0) |
| `vpin_n` | flux | 50 seaux / 60 barres | \|r\|, sauts | moyen (\|r\|, KKLT25, EOYZ24) / nul (direction) |
| `perp_taker_imb_k` | flux / dérivés | 5, 15, 60 | − | moyen (IC loc. de −0,01 à −0,03) |
| `taker_ls_ratio` (metrics) | dérivés / flux | 5 min | − | faible |
| `qh_open_imb` | flux / calendrier | 1ʳᵉ minute du quart d'heure | − (30 min) | moyen (K&H26, horizon comparable) |
| `clv_n`, `body_ratio`, `wicks` | microstructure | 1, 5, 15 | ± | faible (IC loc. instable) |
| `range_sigma`, `range_z_n` | microstructure | 1, 60 | \|r\| | moyen (\|r\|) |
| `roll_spread_n`, `roll_impact_n`, `cs_spread_n`, `ar_spread_n`, `zero_ret_frac_n` | microstructure (liquidité) | 60, 240 | régime | moyen pour la volatilité (EOYZ24) / faible pour la direction |
| `dist_high_n`, `dist_low_n`, `drawdown_n`, `dist_day_open` | microstructure | 60, 240, 1440 | − | faible |
| `round_dist_g` (nombres ronds) | microstructure | 1000, 500 / 100, 50 / 10, 5 | 0 | nul (Urq17) |
| Motifs de chandeliers | microstructure | 1 | 0 | nul |
| `zscore_n` | statistique | 15, 60, 240 | − | **moyen** (IC loc. de −0,03 à −0,055, 9/9) |
| `acf1_n`, `acf2_n`, `vr_q_n` | statistique (régime) | 240, 1440 ; q = 5, 15 | régime (< 1 → retournement) | faible à moyen (ZK19, AS20 ; VR(15) loc. ≈ 0,92) |
| `hurst_n`, `perm_entropy_n`, `ou_halflife_n` | statistique (régime) | 240–1024 | régime | faible |
| `frac_diff_d`, `kalman_slope` | statistique | — | ± | faible |
| `hour_sin/cos`, `us_open_window`, `session_*` | calendrier | UTC, heure d'été gérée | \|r\| | **fort** (\|r\| : Ero19, Bau19, loc.) / nul (direction) |
| `is_weekend` | calendrier | samedi et dimanche UTC | \|r\| en baisse ; retournement renforcé | fort (\|r\|) / moyen (interaction, loc. IC −0,08 contre −0,016) |
| `dow_sin/cos` (effet lundi) | calendrier | — | + lundi (historique) | faible (CP19, AQ19, affaibli) |
| `is_monday_asia` | calendrier | dimanche 22 h – lundi 02 h UTC | + (tendance) | faible (ZPB25-i, praticiens) |
| Heures 22–23 h UTC | calendrier | — | + | faible (QP, non retrouvé en 2026) |
| `is_quarter_hour`, `min_to_quarter` | calendrier | minute % 15 | \|r\| en hausse ; direction instable | moyen (\|r\|, K&H26, SVS23) / faible (direction) |
| `min_to_funding`, `is_funding_window` | calendrier / dérivés | 00, 08, 16 h UTC (`funding_interval_hours`) | 0 | nul (K&H26 ; loc. ≈ 0) |
| `min_to_daily_close` | calendrier | 00:00 UTC | 0 | faible |
| `macro_event_window` (CPI, NFP, FOMC) | calendrier | ±30 min | \|r\| en hausse ; direction 0 | moyen (\|r\|, FRL26, IREF25) / nul (direction, BR23) |
| `options_expiry`, `month_end` | calendrier | vendredi 08:00 UTC | ± | faible |
| `btc_ret_k` → grandes alts (ETH, SOL, BNB, XRP) | marché | 1, 5, 15 | − (retournement commun) | faible (KM26 : simultané ; IC loc. négatif) |
| `btc_ret_k` → petites alts (DOGE ?) | marché | 1, 5 | + (retard) | moyen (GSTW24, KM26) |
| `rel_strength_k`, `resid_ret_k` | marché | 15, 60 | − | faible à moyen (IC loc. de −0,01 à −0,03) |
| `beta_btc_n`, `corr_btc_n`, `dispersion_k` | marché (régime) | 1440 ; 5, 15 | régime | faible |
| `ethbtc_ret_k`, `ethbtc_z_n` | marché | 60, 1440 | ± | faible |
| `btc_dominance` | marché | journalier | 0 | nul (horizon inadapté) |
| `es_nq_ret_k` (futures actions) | marché | 5, 15 | ± | faible (BR23) |
| `coinbase_premium` | marché | 1 | + ? | faible |
| `funding_rate`, `funding_z` | dérivés | 90 fixings | 0 à 5–15 min (− sur plusieurs jours) | nul à 5–15 min / moyen sur plusieurs jours (SST23) |
| `premium_idx`, `funding_pred` | dérivés | 1 m | 0 | nul (IC loc. ≈ 0) |
| `basis_perp_spot`, `basis_chg_k`, `perp_ret_minus_spot_1` | dérivés | 1, 5 | + (le perp mène le spot) | **moyen** (ACPS20 ; IC loc. de +0,02 à +0,04) ; négociabilité à vérifier |
| `oi_chg_k` | dérivés | 15, 60, 240 | − faible | faible (IC loc. de −0,02 à −0,03 sur ETH et SOL, ≈ 0 sur BTC) |
| `oi_price_quadrant_k`, `oi_to_volume` | dérivés | 15, 60 | ± | faible |
| `ls_top_acc`, `ls_global_acc` (variation sur 60 min, z sur 1440) | dérivés | 60, 1440 | + faible (via le retournement ?) | faible (IC loc. de +0,01 à +0,03, pas d'étude ; GS26 : non invariant) |
| `ls_top_pos`, `ls_smart_minus_retail` | dérivés | 60, 1440 | ± | faible |
| `quarterly_basis_ann` | dérivés | — | − sur plusieurs jours | faible (SST23) |
| `depth_imb_x` (bookDepth ±0,2 % / ±1 % / ±5 %) | hors données (accessible) | snapshots de 30 s, EMA 10 | + | moyen à quelques secondes (GB16, Sto18, CKS14) / faible à 5–15 min (IC loc. de +0,05 à +0,06 sur BTC, 1 mois) |
| OFI L1 et microprix | hors données | secondes | + | fort à quelques secondes / non testable à 1 m |
| Flux `aggTrades` (10 s, taille des trades) | hors données (accessible) | 10 s | ± | moyen (K&H26) |
| Liquidations (volume, déséquilibre) | hors données | — | + (cascade) puis − (rebond) | faible (GS26) |
| Entrées nettes on-chain (BTC) | hors données | ≥ 1 h | 0 (sauf à 4 h) | faible (CCH24) |
| Entrées nettes USDT sur les plateformes | hors données | 1 h | + | faible à moyen à 1 h (CCH24, GS20) / nul à 5–15 min |
| Sentiment (tweets, nouvelles, Fear & Greed) | hors données | journalier | 0 | nul pour le rendement (SUW19) |
| Attention (Google Trends) | hors données | hebdomadaire | + | faible (L&T21, 1–2 semaines) |

---

## 17. Références

Les chiffres cités ont été vérifiés dans le texte intégral quand il était accessible :
[SUW22], [L&T21], [EOYZ24], [K&H26], [K&W26], [SVS23], [BS26], [GS26] et [SST23]. Pour
les autres références, ils proviennent du **résumé ou de la page de l'éditeur**. Les
sources de praticiens (QuantPedia, Concretum) sont signalées comme telles.

**Flux d'ordres et microstructure**
- [CKS14] Cont, R., Kukanov, A., Stoikov, S. (2014). *The Price Impact of Order Book Events*. Journal of Financial Econometrics 12(1), 47–88. doi:10.1093/jjfinec/nbt003 — https://ar5iv.labs.arxiv.org/html/1011.6402
- [Sil19] Silantyev, E. (2019). *Order flow analysis of cryptocurrency markets*. Digital Finance 1. doi:10.1007/s42521-019-00007-w — https://link.springer.com/article/10.1007/s42521-019-00007-w
- [M&S20] Makarov, I., Schoar, A. (2020). *Trading and arbitrage in cryptocurrency markets*. Journal of Financial Economics 135(2), 293–319. https://www.sciencedirect.com/science/article/abs/pii/S0304405X19301746
- [AGLMT26] Anastasopoulos, A., Gradojevic, N., Liu, F., Maynard, A., Tsiakas, I. (2026). *Order flow and cryptocurrency returns*. Journal of Financial Markets. https://www.sciencedirect.com/science/article/pii/S1386418126000029 (SSRN 5020002)
- [EOYZ24] Easley, D., O'Hara, M., Yang, S., Zhang, Z. (2024 ; JFM 2026). *Microstructure and Market Dynamics in Crypto Markets*. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4814346 — https://www.sciencedirect.com/science/article/abs/pii/S1386418126000261
- [BS26] Bieganowski, B., Ślepaczuk, R. (2026). *Explainable Patterns in Cryptocurrency Microstructure*. arXiv:2602.00776. https://arxiv.org/abs/2602.00776
- [K&H26] Kim, C., Hansen, P. R. (2026). *The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability in Cryptocurrency Futures*. arXiv:2607.09426. https://arxiv.org/abs/2607.09426
- [K&W26] Kitron, N. A., Wengrowicz, J. M. (2026). *Short-horizon mean reversion in cryptocurrency markets: a matched cross-market measurement*. arXiv:2608.21888. https://arxiv.org/abs/2608.21888
- [KKLT25] Kitvanitphasu, A., Kyaw, K. A. M., Likitapiwat, T., Treepongkaruna, S. (2025). *Bitcoin wild moves: Evidence from order flow toxicity and price jumps*. Research in International Business and Finance 81. https://www.sciencedirect.com/science/article/pii/S0275531925004192
- [KW23] Koutmos, D., Wei, W. C. (2023). *Nowcasting bitcoin's crash risk with order imbalance*. Review of Quantitative Finance and Accounting 61, 125–154. doi:10.1007/s11156-023-01148-1
- [Sto18] Stoikov, S. (2018). *The micro-price: a high-frequency estimator of future prices*. Quantitative Finance 18(12), 1959–1966. doi:10.1080/14697688.2018.1489139
- [GB16] Gould, M. D., Bonart, J. (2016). *Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book*. Market Microstructure and Liquidity. doi:10.1142/S2382626616500064 — arXiv:1512.03492
- [KTW23] Kolm, P. N., Turiel, J., Westray, N. (2023). *Deep order flow imbalance: Extracting alpha at multiple horizons from the limit order book*. Mathematical Finance 33(4). doi:10.1111/mafi.12413
- [ZZR19] Zhang, Z., Zohren, S., Roberts, S. (2019). *DeepLOB: Deep Convolutional Neural Networks for Limit Order Books*. IEEE Transactions on Signal Processing 67(11). doi:10.1109/TSP.2019.2907260
- [HawkesBTC26] *Forecasting Bitcoin price movements using multivariate Hawkes processes and limit order book data*. Decisions in Economics and Finance (2026). https://link.springer.com/article/10.1007/s10203-026-00570-z
- Schmalz, M. *Order Flow Imbalance and Short-Horizon BTC/USDT Returns: A Signal That Kept Needing More Scrutiny*. SSRN 7227998 (OFI de 1 à 10 s sur Binance.US ; texte non consulté). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7227998
- Références méthodologiques :
  - Roll (1984), JF 39(4) ;
  - Kyle (1985), Econometrica 53(6) ;
  - Amihud (2002), JFM 5(1) ;
  - Easley, López de Prado, O'Hara (2012), *Flow toxicity and liquidity in a high-frequency world*, RFS 25(5) ;
  - Corwin, Schultz (2012), JF 67(2) ;
  - Abdi, Ranaldo (2017), RFS 30(12).

**Momentum, retournement, analyse technique**
- [L&T21] Liu, Y., Tsyvinski, A. (2021). *Risks and Returns of Cryptocurrency*. Review of Financial Studies 34(6), 2689–2727. https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024 (NBER w24877)
- [LTW22] Liu, Y., Tsyvinski, A., Wu, X. (2022). *Common Risk Factors in Cryptocurrency*. Journal of Finance 77(2), 1133–1177. doi:10.1111/jofi.13119
- [SUW22] Shen, D., Urquhart, A., Wang, P. (2022). *Bitcoin intraday time series momentum*. Financial Review 57(2), 319–344. doi:10.1111/fire.12290
- [WBXZ22] Wen, Z., Bouri, E., Xu, Y., Zhao, Y. (2022). *Intraday return predictability in the cryptocurrency markets: Momentum, reversal, or both*. North American Journal of Economics and Finance 62. https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833 (SSRN 4080253)
- [DLSZZ21] Detzel, A., Liu, H., Strauss, J., Zhou, G., Zhu, Y. (2021). *Learning and predictability via technical analysis: Evidence from bitcoin and stocks with hard-to-value fundamentals*. Financial Management. doi:10.1111/fima.12310
- [HU21] Hudson, R., Urquhart, A. (2021). *Technical trading and cryptocurrencies*. Annals of Operations Research 297, 191–220. doi:10.1007/s10479-019-03357-1
- [CELS19] Corbet, S., Eraslan, V., Lucey, B., Sensoy, A. (2019). *The effectiveness of technical trading rules in cryptocurrency markets*. Finance Research Letters 31, 32–37. https://www.sciencedirect.com/science/article/abs/pii/S1544612319300315
- [GAS20] Grobys, K., Ahmed, S., Sapkota, N. (2020). *Technical trading rules in the cryptocurrency market*. Finance Research Letters 32, 101396. https://osuva.uwasa.fi/bitstream/handle/10024/11193/Osuva_Ahmed_Grobys_Sapkota_2020.pdf
- [ZPB25] Zarattini, C., Pagani, A., Barbon, A. (2025). *Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins*. SSRN 5209907. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907
- [ZPB25-i] Concretum Group (source de praticiens). *Seasonality in Bitcoin Intraday Trend Trading*. https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- Gao, L., Han, Y., Li, S. Z., Zhou, G. (2018). *Market intraday momentum*. JFE 129(2), 394–414 (référence actions citée par SUW22).
- Moskowitz, T., Ooi, Y. H., Pedersen, L. H. (2012). *Time series momentum*. JFE 104(2).

**Efficience, statistiques**
- [ZK19] Zargar, F. N., Kumar, D. (2019). *Informational inefficiency of Bitcoin: A study based on high-frequency data*. RIBAF 47, 344–353. https://www.sciencedirect.com/science/article/abs/pii/S0275531918303532
- [AS20] Aslan, A., Sensoy, A. (2020). *Intraday efficiency-frequency nexus in the cryptocurrency markets*. Finance Research Letters 35, 101298. https://www.sciencedirect.com/science/article/abs/pii/S1544612319308025
- [Sen19] Sensoy, A. (2019). *The inefficiency of Bitcoin revisited: A high-frequency analysis with alternative currencies*. Finance Research Letters. https://www.sciencedirect.com/science/article/abs/pii/S1544612318302320
- [Bar17] Bariviera, A. F. (2017). *The inefficiency of Bitcoin revisited: A dynamic approach*. Economics Letters. https://arxiv.org/abs/1709.08090
- [Urq17] Urquhart, A. (2017). *Price clustering in Bitcoin*. Economics Letters 159, 145–148. doi:10.1016/j.econlet.2017.07.035
- [BBGR17] Balcilar, M., Bouri, E., Gupta, R., Roubaud, D. (2017). *Can volume predict Bitcoin returns and volatility? A quantiles-based approach*. Economic Modelling 64, 74–81 — **rétracté**. https://www.sciencedirect.com/science/article/abs/pii/S0264999317304558
- Lo, A., MacKinlay, C. (1988). *Stock market prices do not follow random walks*. RFS 1(1), 41–66.
- Estimateurs de volatilité :
  - Parkinson (1980) et Garman, Klass (1980), Journal of Business 53(1) ;
  - Rogers, Satchell (1991), Annals of Applied Probability 1(4) ;
  - Yang, Zhang (2000), Journal of Business 73(3) ;
  - Corsi (2009), *HAR*, Journal of Financial Econometrics 7(2).

**Calendrier et macro**
- [Ero19] Eross, A., McGroarty, F., Urquhart, A., Wolfe, S. (2019). *The intraday dynamics of bitcoin*. RIBAF. doi:10.2139/ssrn.3013699
- [Bau19] Baur, D. G., Cahill, D., Godfrey, K., Liu, Z. (2019). *Bitcoin time-of-day, day-of-week and month-of-year effects in returns and trading volume*. Finance Research Letters. https://www.sciencedirect.com/science/article/abs/pii/S1544612319301710
- [SVS23] Shanaev, S., Vasenin, M., Stepanov, R. (2023). *Turn-of-the-candle effect in bitcoin returns*. Heliyon. doi:10.1016/j.heliyon.2023.e14236 — https://pmc.ncbi.nlm.nih.gov/articles/PMC10015199/
- [CP19] Caporale, G. M., Plastun, A. (2019). *The day of the week effect in the cryptocurrency market*. Finance Research Letters. https://www.sciencedirect.com/science/article/pii/S1544612318304240
- [AQ19] Aharon, D. Y., Qadan, M. (2019). *Bitcoin and the day-of-the-week effect*. Finance Research Letters. https://www.sciencedirect.com/science/article/abs/pii/S1544612317307894
- [PRH21] Petukhina, A., Reule, R., Härdle, W. K. (2021). *Rise of the machines? Intraday high-frequency trading patterns of cryptocurrencies*. European Journal of Finance. https://arxiv.org/abs/2009.04200
- [QP-seas] QuantPedia (source de praticiens). *The Seasonality of Bitcoin*. https://quantpedia.com/the-seasonality-of-bitcoin/
- Algieri, B., Lawuobahsumo, K., Leccadito, A. (2024). *Calendar Effects on Returns, Volatility and Higher Moments: Evidence from Crypto Markets*. https://ideas.repec.org/p/ajf/louvlf/2024001.html
- [BR23] Benigno, G., Rosa, C. (2023). *The Bitcoin–Macro Disconnect*. Federal Reserve Bank of New York Staff Report 1052. https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr1052.pdf
- [PL20] Pyo, S., Lee, J. (2020). *Do FOMC and macroeconomic announcements affect Bitcoin prices?* Finance Research Letters 37, 101386. https://www.sciencedirect.com/science/article/abs/pii/S154461231930159X
- [FRL26-FOMC] *Scheduled FOMC statements and intraday macro event risk in cryptocurrency markets*. Finance Research Letters (2026). https://www.sciencedirect.com/science/article/abs/pii/S1544612326006021
- [IREF25] *Exploring volatility reactions in cryptocurrency markets using intraday macroeconomic news analysis*. International Review of Economics & Finance (2025). https://www.sciencedirect.com/science/article/pii/S1059056025006720
- [CME26] CME Group (2026). *CME Group to Launch 24/7 Cryptocurrency Futures and Options Trading on May 29*. https://www.cmegroup.com/media-room/press-releases/2026/2/19/cme_group_to_launch247cryptocurrencyfuturesandoptionstradingonma.html

**Cross-asset**
- [GSTW24] Guo, L., Sang, B., Tu, J., Wang, Y. (2024). *Cross-cryptocurrency return predictability*. Journal of Economic Dynamics and Control 163, 104863. doi:10.1016/j.jedc.2024.104863
- [KM26] Kurihara, T., Matsumoto, T. (2026). *Price Transmission from Bitcoin to Altcoins: High-Frequency Evidence and Implications for Trading Strategy*. Asia-Pacific Financial Markets. doi:10.1007/s10690-026-09589-z
- [SMS19] Sifat, I. M., Mohamad, A., Shariff, M. S. B. M. (2019). *Lead-Lag relationship between Bitcoin and Ethereum: Evidence from hourly and daily data*. RIBAF. https://www.sciencedirect.com/science/article/abs/pii/S0275531919300522

**Dérivés**
- [HMRW22] He, S., Manela, A., Ross, O., von Wachter, V. (2022). *Fundamentals of Perpetual Futures*. arXiv:2212.06888. https://arxiv.org/abs/2212.06888
- [AHJ26] Ackerer, D., Hugonnier, J., Jermann, U. (2026). *Perpetual Futures Pricing*. Mathematical Finance. doi:10.1111/mafi.70018 (arXiv:2310.11771)
- [SST23] Schmeling, M., Schrimpf, A., Todorov, K. (2023). *Crypto carry*. BIS Working Paper 1087. https://www.bis.org/publ/work1087.pdf
- [ACPS20] Alexander, C., Choi, J., Park, H., Sohn, S. (2020). *BitMEX bitcoin derivatives: Price discovery, informational efficiency, and hedging effectiveness*. Journal of Futures Markets 40(1), 23–43. doi:10.1002/fut.22050
- [GS26] Garcia Seuma, R. M. (2026). *Where does the criticality live? Early-warning signals are event-heterogeneous across seven crypto-perpetual liquidation cascades*. arXiv:2607.27070. https://arxiv.org/abs/2607.27070
- [FRL23-COT] *Role of hedging on crypto returns predictability: A new habit-based explanation*. Finance Research Letters (2023). https://www.sciencedirect.com/science/article/abs/pii/S1544612323003811
- Documentation Binance des ratios long/short des top traders : https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio

**On-chain et sentiment**
- [CCH24] Chi, Y., Chu, Q., Hao, W. (2024, rév. 2025). *Return and Volatility Forecasting Using On-Chain Flows in Cryptocurrency Markets*. arXiv:2411.06327. https://arxiv.org/abs/2411.06327
- [GS20] Griffin, J. M., Shams, A. (2020). *Is Bitcoin Really Untethered?* Journal of Finance 75(4), 1913–1964. doi:10.1111/jofi.12903
- [SUW19] Shen, D., Urquhart, A., Wang, P. (2019). *Does twitter predict Bitcoin?* Economics Letters 174, 118–122. https://www.sciencedirect.com/science/article/abs/pii/S0165176518304634

**Apprentissage automatique et modèles de fondation**
- [JDW21] Jaquart, P., Dann, D., Weinhardt, C. (2021). *Short-term bitcoin market prediction via machine learning*. Journal of Finance and Data Science 7. https://www.sciencedirect.com/science/article/pii/S2405918821000027
- [AGS21] Akyildirim, E., Goncu, A., Sensoy, A. (2021). *Prediction of cryptocurrency returns using machine learning*. Annals of Operations Research 297, 3–36. doi:10.1007/s10479-020-03575-y
- Filippou, I., Rapach, D., Thimsen, C. *Cryptocurrency Return Predictability: A Machine-Learning Analysis*. SSRN 3914414 (journalier).
- [RNW25] Rahimikia, E., Ni, H., Wang, W. (2025). *Re(Visiting) Time Series Foundation Models in Finance*. arXiv:2511.18578. https://arxiv.org/abs/2511.18578
- [NF26] Noguer i Alonso, M., Franklin, R. P. (2026). *Pretrained Time-Series Foundation Models for Financial Return Forecasting*. arXiv:2606.27100. https://arxiv.org/abs/2606.27100
- [Bri26] Brini, A. (2026). *Forecasting Realized Volatility with Time Series Foundation Models: A Comparison with Econometric Benchmarks*. arXiv:2607.05291. https://arxiv.org/abs/2607.05291
- [Kro25] Shi, Y. et al. (2025). *Kronos: A Foundation Model for the Language of Financial Markets*. arXiv:2508.02739 (AAAI 2026). https://arxiv.org/abs/2508.02739
- [TFM3] Google Research (2026). *TimesFM-3: A zero-shot foundation model for multivariate forecasting*. https://research.google/blog/timesfm-3-a-zero-shot-foundation-model-for-multivariate-forecasting/

**Données Binance utilisées ou repérées** (toutes accessibles depuis l'environnement le 25/09/2026)
- spot : `data/spot/monthly/klines/{SYM}/1m/` ; `data/spot/daily/aggTrades/` ;
- futures UM, klines : `data/futures/um/monthly/klines/{SYM}/1m/` ; `premiumIndexKlines/{SYM}/1m/` ; `markPriceKlines/…` ; `indexPriceKlines/…` ;
- futures UM, autres : `data/futures/um/daily/metrics/` ; `data/futures/um/monthly/fundingRate/` ; `data/futures/um/daily/bookDepth/` ; `data/futures/um/daily/aggTrades/` ;
- options : `data/option/daily/BVOLIndex/BTCBVOLUSDT/` ;
- **introuvables (404)** : `futures/um/daily/bookTicker` et `futures/um/daily/liquidationSnapshot` pour 2026-09-01.
