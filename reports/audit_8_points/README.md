# Audit contradictoire : la grille en 8 points appliquée à nos backtests

*26/09/2026. Grille tirée de l'article « The $200,000 Quant Stack You Can Now Build in an Evening ». Pour chaque étude, un auditeur cite la ligne de code qui montre chaque défaut (ou qui s'en protège), puis un contradicteur refait le travail : il vérifie chaque citation mot pour mot, cherche un contre-exemple à chaque « absent » et tente de réfuter chaque « présent ». Le statut qualifie le **défaut** (présent = le défaut existe). Données brutes : `audit_brut.json`. La relecture dédiée de l'étude memecoins est dans `reports/memecoins/README.md` § 12.*

## Résumé

* Aucun verdict négatif ne change.
* **Arbitrage** : les 3 violations du carnet réel venaient de deux connexions WebSocket décalées de 150 à 300 ms, fusionnées à la réception ; en ordre serveur, il n'y en a aucune. Le « coup sûr » de 45 030 $ contient 21 544 $ de quasi-égalités indécidables, et l'issue n'est connaissable que ≈ 0,8 s avant la clôture.
* **Latence** : la fenêtre positive (≈ 326 ms) dépend de σ × 1,40 (calibré ailleurs) ; le P&L réalisé vaut trois fois l'avantage attendu ; bootstrap par créneau et carnet plus en retard pendant les sauts l'affaiblissent ; une heure calme. Non établi.
* **Formule, modèles Polymarket** : les gains historiques supposent d'acheter au prix d'un bloc apparié ≈ 2,2 s avant la décision.
* **Indicateurs** : aucun frais simulé ; le verdict repose sur un seuil de rentabilité, pas sur un P&L.
* **Point 7** presque partout : échantillons Polymarket courts et calmes.

## Tableau

| étude | 1 information future (look-ahead) | 2 survivants | 3 données futures dans un indicateur | 4 frais et glissement | 5 prix jamais disponible | 6 réglages sur tout l'échantillon | 7 hausse et baisse (régimes) | 8 horloges et alignement |
|---|---|---|---|---|---|---|---|---|
| État des lieux des indicateurs Binance | partiel | partiel | absent | présent | présent | partiel | absent | absent |
| Walk-forward TimesFM contre baselines, mini-stratégie avec frais | absent | partiel | absent | partiel | présent | partiel | partiel | absent |
| Polymarket : nos modèles contre le prix du marché avant l'ouverture | partiel | absent | absent | partiel | présent | présent | partiel | partiel |
| Polymarket : top wallets et copy-trading | partiel | partiel | partiel | partiel | partiel | partiel | présent | partiel |
| Polymarket : simulation maker sur les trades historiques | partiel | absent | partiel | partiel | partiel | partiel | partiel | partiel |
| Polymarket : formule exacte P(Up) contre le marché, P&L preneur selon le délai | partiel | partiel | partiel | partiel | présent | partiel | partiel | partiel |
| Arbitrage entre marchés liés | partiel | partiel | partiel | partiel | présent | partiel | partiel | présent |
| Polymarket : simulation maker sur le carnet réel enregistré | partiel | partiel | absent | absent | partiel | partiel | présent | partiel |
| TimesFM : prévision d'amplitude, juste valeur en cours de fenêtre, filtre de régime | absent | absent | absent | sans objet | sans objet | absent | présent | partiel |
| Temps de réaction : prix périmés, P&L selon la latence, réaction du carnet | absent | absent | partiel | partiel | partiel | partiel | présent | partiel |

## État des lieux des indicateurs Binance (IC, hit-rate hors échantillon, modèles combinés) — reports/etat_des_lieux/README.md (contre-expertise)

**1. information future (look-ahead) : partiel**

* `src/tradebot/targets.py:32` : `ret = log_close.shift(-h) - log_close`
* `src/tradebot/evaluation.py:552` : `pred_up = (xvo - med) * sign > 0`
* `src/tradebot/data.py:158` : `METRICS_AVAILABILITY_LAG = pd.Timedelta(minutes=5)`
* `src/tradebot/data.py:963` : `merged = pd.merge_asof(left, right, on="_t", direction="backward",`

Les 5 citations sont exactes. Les indicateurs sont causaux : j'ai relancé les 169 tests de troncature et de perturbation du futur, qui passent tous. Les dérivés sont alignés sur leur disponibilité (create_time + 5 min, merge_asof backward) avec 1 min de marge. Le défaut porte sur le passage à la décision : la cible part de close[t], la clôture même qui sert au signal, donc sans aucune latence. J'ai remesuré ema_dist_60, h = 15, sur le test. BTC : hit 51,79 % → 51,75 % avec 1 barre de retard (51,71 % avec 2), P&L brut ≈ 0,10 pb/trade inchangé. DOGE : 52,82 % → 52,58 % (52,46 % avec 2 barres), 0,49 → 0,46 pb. Ces chiffres confirment exactement ceux de l'auditeur. Sur le 1 % extrême (0,5 % par queue), le P&L brut tient avec 1 barre de retard : BTC 1,06 → 0,94 pb, DOGE 6,54 → 5,70 pb, SOL 5,53 → 5,69 pb.

*Effet sur la conclusion :* Biais optimiste ≈ 0,05 pt (BTC) à 0,25 pt (DOGE) de hit-rate, et 4 à 10 % de l'IC. Le verdict négatif ne change pas.

**2. survivants : partiel**

* `src/tradebot/config.py:22` : `# Univers crypto : paires spot Binance contre USDT, les plus liquides.`
* `src/tradebot/config.py:25` : `DEFAULT_TICKERS: tuple[str, ...] = CORE_CRYPTO + EXTRA_CRYPTO`
* `src/tradebot/targets.py:35` : `return ret.where(elapsed == h * bar).rename(f"ret_{h}")`
* `src/tradebot/cli.py:421` : `log.error("%s : chargement impossible (%s) ; actif ignoré", ticker, exc)`

Les citations sont exactes. L'univers se limite à 6 grandes capitalisations choisies d'après leur liquidité actuelle, sans aucune paire délistée ni petite. Il existe aussi dans le code un chemin qui ignore en silence un actif dont le chargement échoue (cli.py l. 421), mais il n'a pas servi : les 6 actifs sont présents, avec 525 599 barres chacun et 0 barre manquante. Au niveau des barres, un trou rend la cible NaN : rien n'est inventé. Pour la direction à 5–15 min de ces paires, qui ont toutes existé et été liquides pendant toute la fenêtre, le biais de survie sur l'IC est négligeable. Il limite surtout la généralisation.

*Effet sur la conclusion :* Aucun changement du verdict sur BTC/ETH, les seuls actifs utiles pour Polymarket. Les conclusions ne valent pas pour des actifs moins liquides.

**3. données futures dans un indicateur : absent**

* `src/tradebot/indicators.py:11` : `cumuls). Jamais de ``shift`` négatif, de fenêtre centrée, de ``bfill`` ni de`
* `src/tradebot/indicators.py:552` : `span_a = ((_mid_range(df, 9) + _mid_range(df, 26)) / 2.0).shift(26)`
* `src/tradebot/indicators.py:1556` : `return b.reindex(index).ffill(limit=BENCH_FFILL_LIMIT)`
* `src/tradebot/evaluation.py:526` : `row["median_train"] = float(0.5 * (xvt[(m - 1) // 2] + xvt[m // 2]))`

J'ai cherché un contre-exemple. Dans indicators.py, aucune occurrence de shift(-, center=True, bfill, interpolate, qcut, quantile, rank ni expanding. Le seul calcul « à rebours » est _ratchet_band (Supertrend, minimum.accumulate inversé), et il est couvert par le test de troncature. J'ai relancé ce test sur les 168 indicateurs, plus le test de perturbation du futur : tout passe. Le z-score est glissant (l. 197). _align_targets fait un reindex exact, sans nearest. Les baselines sont causales : majority_prev_day ne garde que les labels complets la veille. Dans les modèles combinés, l'imputation par médiane et le StandardScaler (dans le pipeline) sont ajustés sur le pli d'apprentissage, avec une purge de h ; l'arrêt précoce de hgb est chronologique et purgé. Seule réserve, déjà notée : le classement du top 20 utilise l'IC plein échantillon. C'est une sélection, pas du repainting.

*Effet sur la conclusion :* Aucun effet sur la conclusion.

**4. frais et glissement : présent**

* `src/tradebot/cli.py:381` : `def run_study(tickers: Sequence[str], *, interval: str = DEFAULT_INTERVAL, days: int = 365,`
* `src/tradebot/config.py:86` : `cost_bps: float = COST_BPS["futures_taker"]`
* `reports/etat_des_lieux/README.md:5` : `> Recherche uniquement. Un indicateur « significatif » n'est pas une stratégie rentable : à 1 minute, couvrir 10 pb de frais aller-retour demande 72 à 89 % de bonnes directions (docs/research/methodol`
* `docs/research/methodologie.md:36` : `aller-retour** (futures, taker des deux côtés), il faut donc **89 %** de précision sur BTC et`

Confirmé : aucune métrique de l'étude n'est nette de frais ni de spread. run_study ne reçoit pas cost_bps. Les coûts n'apparaissent que dans la phrase du README, qui compare le hit-rate de toutes les barres à un p* calculé ailleurs : E|r| des 30 derniers jours, libellé « à 1 minute » au lieu de h = 15. Je réfute en partie la conclusion implicite « aucun avantage exploitable ». Ce cadrage (trader chaque barre en taker) est pessimiste pour une stratégie sélective. J'ai mesuré ema_dist_60, h = 15, sur le 1 % extrême du test (seuils fixés sur l'apprentissage). BTC : 1,06 pb brut, hit 60,0 %. ETH : 0,45 pb, hit 60,7 %. SOL : 5,53 pb, hit 58,5 %. DOGE : 6,54 pb, hit 58,1 %. Avec 1 barre de retard, SOL et DOGE restent à ≈ 5,7 pb. C'est au-dessus des 4 pb maker futures, mais sous les 10 pb taker. Ce résultat n'est ni net d'antisélection maker ni significatif : ≈ 2 000 signaux groupés en grappes, erreur type ≈ 3 pb. Sur toutes les barres, l'auditeur a raison : 0,1 à 0,5 pb brut, loin de tout coût.

*Effet sur la conclusion :* Pour une exécution taker sur Binance, les frais rendent le verdict encore plus négatif, et la conclusion tient. En revanche, la phrase « aucun avantage » n'est pas démontrée pour une stratégie maker limitée aux queues sur SOL/DOGE : le rapport ne l'examine pas (biais pessimiste possible ≈ +1 à +2 pb net avant antisélection, non significatif). Elle ne se transpose pas non plus à Polymarket, dont la cible (TWAP Chainlink) et les coûts diffèrent.

**5. prix jamais disponible : présent**

* `src/tradebot/targets.py:32` : `ret = log_close.shift(-h) - log_close`
* `src/tradebot/targets.py:41` : `out[ret > eps] = 1.0`

Confirmé : l'exécution implicite se fait à close[t] spot, c'est-à-dire au dernier trade de la barre du signal. Il n'y a ni latence, ni côté bid/ask, ni profondeur limitée. Les coûts sont exprimés en futures (10 pb), alors que les prix sont spot (taker spot : 20 pb). J'ai remesuré les rendements nuls sur le test, un indice de tick grossier et donc de rebond bid-ask. À h = 5 : SOL 4,3 %, XRP 3,3 %, DOGE 4,3 %, BTC 0,4 %. À h = 15 : 1,9 à 2,5 % pour les altcoins, 0,1 % pour BTC. C'est un peu plus que les chiffres de l'auditeur, avec le même ordre de grandeur. Le recul du hit-rate avec 1 barre de retard (−0,24 pt sur DOGE contre −0,04 sur BTC) correspond à ce rebond.

*Effet sur la conclusion :* Biais optimiste de ≈ 0,05 à 0,25 pt de hit-rate. Sur les altcoins, il est comparable à tout l'avantage brut sur l'ensemble des barres, mais il n'efface pas les queues (voir point 4). Le verdict négatif en taker est renforcé et reste inchangé.

**6. réglages sur tout l'échantillon : partiel**

* `docs/research/indicateurs.md:23` : `sur BTC, ETH et SOL (juin–août 2026, § 2) le confirme : toutes les mesures de`
* `docs/research/indicateurs.md:53` : `interactions entre régime et retournement sont prioritaires.`
* `docs/research/indicateurs.md:811` : `§ 1.3. « IC loc. » = mesure locale de juin à août 2026 (§ 2).`
* `src/tradebot/evaluation.py:778` : `return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000))`

Les citations sont exactes. J'ajoute la l. 53 d'indicateurs.md, qui justifie les interactions par des IC mesurés de juin à août 2026. Ces mois sont entièrement dans le test des indicateurs, qui commence le 2026-05-02. J'atténue toutefois l'effet. Le top 20 est composé d'indicateurs classiques à fenêtres standard (ema_dist_60, rsi_14, macd 12/26…), pas des interactions guidées par juin–août : la meilleure, ret15_x_taker_abs15, n'a qu'un IC de −0,040 et reste hors du top 20. Le choix a posteriori logit/hgb (l. 969) : hgb gagne 15 cellules sur 18. L'écart hgb − logit vaut en moyenne +0,18 pt, et −0,20 pt au pire. Le gain de sélection est donc ≤ 0,2 pt dans 3 cellules et ≈ 0 ailleurs.

*Effet sur la conclusion :* Biais optimiste faible, ≤ 0,1–0,2 pt sur les chiffres mis en avant. Le verdict négatif ne change pas. Il rend seulement encore moins crédibles les petites « victoires » de hgb sur le retournement.

**7. hausse et baisse (régimes) : absent**

* `reports/etat_des_lieux/README.md:26` : `| BTCUSDT | 525 599 | 2025-09-25 23:00 UTC | 2026-09-25 22:58 UTC | 0 | 2026-05-02 22:59 UTC | 158 | 9 | — | 99,7 % |`
* `src/tradebot/evaluation.py:615` : `split = int(round(float(train_frac) * n))`

J'ai vérifié dans le cache pour BTC, du 2026-05-02 22:59 au 2026-09-25 22:58. Le cours passe de 78 833 à 84 074 (+6,6 %), avec un creux à 57 882 et un drawdown de −30,1 %. Par mois : mai −6,5 % (et non −3,5 % comme l'écrit l'auditeur), juin −20,4 %, juillet +7,3 %, août +25,0 %, septembre +7,0 %. Volatilité 1 min annualisée : 39,0 % sur le test contre 49,7 % sur l'apprentissage (−28 %). E|r15| : 13,2 contre 16,8 pb. Pour ETH, le test fait +15,9 %, avec juin −21,7 % et août +32,4 %. Le test contient donc bien une phase baissière et une phase haussière nettes. Seule réserve : il est moins volatil que l'apprentissage.

*Effet sur la conclusion :* Aucun défaut de régime, et la conclusion ne change pas. La petite erreur de chiffre sur mai n'a pas d'effet.

**8. horloges et alignement : absent**

* `src/tradebot/data.py:442` : `out = out[np.asarray(close_time < _to_utc(now))]`
* `src/tradebot/data.py:963` : `merged = pd.merge_asof(left, right, on="_t", direction="backward",`
* `src/tradebot/indicators.py:1556` : `return b.reindex(index).ffill(limit=BENCH_FFILL_LIMIT)`
* `src/tradebot/targets.py:34` : `elapsed = ts.shift(-h) - ts`

Confirmé. Tout est en UTC et indexé à l'ouverture de la barre Binance : les features en t utilisent close[t], et la cible part de close[t], donc la convention est cohérente. Les metrics sont indexées à create_time + 5 min : data.py documente que create_time marque le début de la fenêtre de 5 min, ce qui a été vérifié sur les ratios taker. Le funding est indexé à calc_time arrondi à la seconde supérieure. Les deux sont alignés en as-of backward sur l'ouverture de la barre, avec 1 min de marge. Le seul désalignement est mineur : les chargements sont décalés de 0 à 19 min, donc les ≈ 15 dernières barres des altcoins ont bench_close NaN. C'est négligeable et causal.

*Effet sur la conclusion :* Aucun effet sur la conclusion.

**Autres défauts confirmés**

* **Top 20 et meilleur hit-rate choisis sur des statistiques qui incluent le test** (confirmé, gravité basse, optimiste) — `src/tradebot/report.py:952`. Confirmé : les 52,1 % sont le maximum des 501 hit-rates de test (ichimoku_cloud_dist, 52,13 %), et le top 20 est trié sur ic_mean, qui inclut le test. L'ampleur est en revanche surestimée par l'auditeur. Si l'on classe par ic_train_mean (sans fuite), le premier est ema_gap_5_20 à 51,79 %, et la moyenne des 10 premiers vaut ≈ 51,85 %. L'inflation est donc ≈ 0,3 pt, pas 0,5.
* **Le hit-rate masque un avantage pondéré par |r| quasi nul** (confirmé, gravité basse, optimiste) — `src/tradebot/evaluation.py:553`. Confirmé avec combined_summary.csv : pour ETH h = 15, hgb a un acc_w de 50,60 % contre 50,51 % pour reversal_h, malgré une accuracy de 53,3 %. Le README affiche bien acc_w pour les modèles (section 5), mais pas pour les indicateurs. Sur toutes les barres, ema_dist_60 rapporte 0,10 pb brut (BTC) et 0,49 pb (DOGE).
* **Stouffer et BY supposent des actifs indépendants** (confirmé, gravité basse, optimiste) — `src/tradebot/evaluation.py:719`. Recalculé à partir d'aggregate.csv : en divisant z par 2,23, le nombre de couples avec q BY < 5 % et |z| > 3 passe de 364 à 327. Le README signale lui-même ce caractère optimiste. Le verdict n'est pas touché.
* **Meilleur modèle (logit ou hgb) choisi sur l'accuracy de test** (confirmé, gravité basse, optimiste) — `src/tradebot/report.py:969`. hgb l'emporte dans 15 cellules sur 18. Le gain de sélection n'existe que dans 3 cellules (≤ 0,2 pt) ; il est ≈ 0,03 pt en moyenne, soit moins que les 0,1–0,2 pt avancés par l'auditeur. Plusieurs écarts avec le retournement restent inférieurs à 2 erreurs types.
* **Seuil de rentabilité mal libellé et tiré d'une autre fenêtre** (confirmé, gravité basse, neutre) — `reports/etat_des_lieux/README.md:5`. methodologie.md (l. 35–37) situe le seuil à 15 min, calculé sur E|r15| des 30 derniers jours (BTC 12,7 pb, SOL 22,2 pb). Sur le test, BTC E|r15| = 13,2 pb, d'où p* ≈ 88 %. L'ordre de grandeur est inchangé.
* **Égalités (rendement nul) exclues du hit-rate** (confirmé, gravité basse, neutre) — `src/tradebot/targets.py:40`. Les égalités existent (sur le test : 3,3 à 4,3 % à h = 5 pour SOL/XRP/DOGE, 0,4 % pour BTC). Mais une égalité rapporte 0 en brut : l'exclure ne gonfle pas le P&L brut. Elle ne coûte que les frais, déjà traités au point 4. L'effet sur le hit-rate, tel qu'il est interprété, est négligeable ; je le classe neutre plutôt qu'optimiste.
* **Pas de coffre-fort ; catalogue conçu sur des mois du test** (confirmé, gravité basse, optimiste) — `reports/etat_des_lieux/README.md:307`. Confirmé. Combiné à indicateurs.md (IC loc. de juin à août 2026), aucune période n'a été vierge. L'effet sur le top 20 (indicateurs classiques) reste faible.
* **Le verdict compare le hit-rate de toutes les barres au p* d'un trading systématique, sans examiner les queues du signal (biais pessimiste)** (nouveau, gravité moyenne, pessimiste) — `reports/etat_des_lieux/README.md:14`. Le seuil est fixé par le quantile de l'apprentissage et le test couvre toutes les barres (evaluation.py l. 552). J'ai mesuré ema_dist_60, h = 15, sur le 1 % extrême du test. Hit-rate : BTC 60,0 %, ETH 60,7 %, SOL 58,5 %, DOGE 58,1 %. P&L brut : 1,06 / 0,45 / 5,53 / 6,54 pb par trade. Avec 1 barre de retard, SOL tient à 5,69 pb et DOGE à 5,70 pb. C'est au-dessus du coût maker futures (4 pb, COST_BPS) mais sous le taker (10 pb). Le rapport ne regarde ni le rendement conditionnel ni un p* propre aux queues (DOGE : p* maker ≈ 55 %). Réserves : environ 2 000 signaux groupés en grappes (erreur type ≈ 3 pb, non significatif), antisélection maker non modélisée, et un indicateur retenu parmi les meilleurs. Ce n'est donc pas un avantage démontré, mais la conclusion « aucun avantage » n'est pas démontrée non plus pour une exécution maker sélective sur altcoins.
* **Funding absent (NaN) sur le mois en cours, soit ≈ 17 % du test des indicateurs** (nouveau, gravité basse, pessimiste) — `reports/etat_des_lieux/README.md:309`. Dans le cache, funding_rate s'arrête le 2026-08-31 16:00. Avec la tolérance de 9 h, les indicateurs funding sont NaN du 1er au 25 septembre, soit ≈ 25 jours sur ≈ 146 de test. Leur hit-rate et leur IC de test portent donc sur un sous-échantillon. Dans le dernier pli des modèles combinés, ces variables sont imputées par une médiane constante. La famille « derives » a déjà un |IC| ≤ 0,018 ; l'effet est très faible et plutôt pessimiste pour cette famille seulement.

## Walk-forward TimesFM contre baselines, mini-stratégie avec frais (src/tradebot/backtest.py, forecaster.py, targets.py, cli.py timesfm-backtest ; conclusions dans reports/timesfm/README.md) — contre-audit

**1. information future (look-ahead) : absent**

* `src/tradebot/backtest.py:194` : `ctxs = [transform_context(close_grid[p - context_len + 1:p + 1], kind) for p in sel]`
* `src/tradebot/backtest.py:195` : `covs = None if cov_arr is None else [cov_arr[:, p - context_len + 1:p + 1] for p in sel]`
* `src/tradebot/backtest.py:273` : `past = fut.shift(h)                     # log(close[t] / close[t-h]), NaN si trou`
* `src/tradebot/backtest.py:277` : `k = pd.DatetimeIndex(daily.index).searchsorted(org_day, side="left") - 1   # veille disponible`

Toutes les citations de l'auditeur sont exactes (vérifiées avec sed -n). J'ai cherché un contre-exemple sans en trouver. (a) Le contexte et les covariables s'arrêtent à la barre t incluse, dont la clôture est connue à t + 1 min. (b) momentum_h et reversal_h utilisent fut.shift(h) = log(close[t]/close[t−h]). (c) majority_prev_day ne garde que les labels dont la barre t + h tombe dans le même jour UTC (ligne 275) et lit la veille strictement antérieure (searchsorted − 1). (d) Le benchmark BTC des altcoins est réindexé à l'horodatage exact, avec un ffill causal (indicators.py, _align_benchmark). (e) Les dérivés sont joints en backward sur l'horodatage de disponibilité, et les runs top5 publiés n'en utilisent d'ailleurs aucun : leurs covariables sont ema_dist_60, ema_dist_30, sma_dist_30, di_diff_14 et ema_gap_5_20 (runs.csv). (f) Le sous-échantillonnage max_origins (linspace, ligne 156) et le filtre des trous ne dépendent que du calendrier et du contexte passé. Le test test_no_lookahead_leakage perturbe tout ce qui suit T et vérifie aussi le cas des données tronquées. Contamination : la carte du modèle ne déclare aucune donnée crypto à 1 min (docs/research/timesfm.md, ligne 67). Le délai entre la clôture de t et l'ordre relève bien du point 5.

*Effet sur la conclusion :* Aucun biais de look-ahead mesurable. Le verdict « aucun avantage après frais » n'est pas affecté.

**2. survivants : partiel**

* `src/tradebot/cli.py:61` : `DEFAULT_TIMESFM_TICKERS: tuple[str, ...] = ("BTCUSDT", "SOLUSDT")`
* `reports/timesfm/README.md:135` : `Commande : `python -m tradebot timesfm-backtest --tickers BTCUSDT SOLUSDT ETHUSDT --covariates none --stride 15 --max-origins 1500``
* `src/tradebot/backtest.py:152` : `gap_bad = max_run[cand_pos] > max_gap_bars`

C'est confirmé. L'univers TimesFM se limite à BTC, ETH et SOL, trois grandes paires toujours cotées. XRP, BNB et DOGE sont en cache (data/cache/*_1m.parquet), et l'étude etat_des_lieux les a traitées (run.json : 6 tickers), mais aucun run TimesFM ne les couvre. Sur cinq mois et pour des paires majeures, aucun actif n'a disparu : le défaut ne crée donc pas de biais directionnel au sens classique (survivants surperformants), puisque la question porte sur la prévisibilité à 5–15 min, pas sur le rendement de détention. Le rapport indique 0 origine écartée pour trou. Les cibles NaN (trou, égalité : ≈ 0,4 % à 5 min sur BTC et ETH, 4,3 % sur SOL, mesuré sur le cache) sont retirées de l'accuracy, sans lien évident avec le signal. Le défaut porte sur la représentativité : les paires moins liquides, où le retournement de microstructure est plus fort, ne sont pas testées. Cela pourrait masquer un avantage, donc dans le sens pessimiste, mais ce n'est pas quantifiable.

*Effet sur la conclusion :* Biais sur l'accuracy négligeable (< 0,5 point). Le verdict ne change pas pour BTC, ETH et SOL, mais il ne s'étend pas aux autres paires.

**3. données futures dans un indicateur : absent**

* `src/tradebot/cli.py:604` : `t_mid = times[len(times) // 2]`
* `src/tradebot/cli.py:606` : `fit = g[pd.DatetimeIndex(g["time"]) + (int(h) + 1) * bar <= t_mid]`
* `src/tradebot/cli.py:607` : `app = g[pd.DatetimeIndex(g["time"]) >= t_mid]`
* `src/tradebot/forecaster.py:29` : `5. Covariables past-only : centrées et réduites sur la fenêtre de contexte uniquement`

C'est vérifié. (1) L'isotonique est apprise sur les origines dont le label est clos avant t_mid : label à t + h, connu à t + h + 1 barre. La purge est même prudente d'une barre. Elle est appliquée seulement à partir de t_mid. (2) prepare_covariates calcule le z-score sur la fenêtre de contexte. (3) Le rolling des trous se fait sur une grille triée (_clean_close refuse un index non trié ou avec doublons). (4) J'ai cherché resample, shift(-, center=, bfill et merge_asof dans indicators.py : rien de non causal (seuls rolling, ewm et groupby.cumsum/cummax). (5) Le benchmark est aligné par reindex + ffill limité. (6) La recherche d'indicateurs à l'étude (ic_train_mean) ne sert qu'au choix des covariables, et le début des origines est reculé après la fin de l'apprentissage (cli.py, ligne 728). La seule statistique « globale », t_mid, vient du calendrier des origines.

*Effet sur la conclusion :* Pas de repainting. Le verdict n'est pas affecté.

**4. frais et glissement : partiel**

* `src/tradebot/backtest.py:464` : `net = gross - cost_bps`
* `src/tradebot/config.py:61` : `COST_BPS: dict[str, float] = {"futures_taker": 10.0, "spot_taker": 20.0, "futures_maker": 4.0}`
* `src/tradebot/cli.py:671` : `cost_bps: float = COST_BPS["futures_taker"], threshold: float = 0.55,`
* `reports/timesfm/README.md:530` : `* Frais seuls (pas de spread ni de glissement) : le coût réel est plus élevé.`

C'est confirmé. Chaque trade paie 10 pb aller-retour, soit 2 × 5 pb en taker USDⓈ-M VIP0, mais aucun spread ni impact n'est compté, ce que le rapport admet (ligne 530). Si on paie le spread entier sur l'aller-retour, l'oubli vaut ≈ 0,01 pb sur BTC, ≈ 0,04 pb sur ETH et ≈ 0,8 à 1,7 pb sur SOL (tick de 0,01 pour un prix de 60 à 122 sur la période). Autre incohérence : les prix viennent du spot, mais le coût est celui du perp. Un trade vraiment spot coûterait 20 pb (config ligne 61) et ne permettrait pas de vendre à découvert simplement. Dans l'autre sens, 10 pb est le pire cas taker : en maker (4 pb), le coût serait plus faible. Le brut par trade de TimesFM reste pourtant compris entre −0,74 et +0,24 pb sur les runs à 1 500 origines, avec un se de 0,5 à 2 pb. Le verdict ne dépend donc pas du niveau de coût.

*Effet sur la conclusion :* Biais optimiste inférieur à 1 pb par trade (SOL), ≈ 0 sur BTC et ETH. Le coût est peut-être surestimé de 6 pb en maker. Comme le brut ≈ 0, le verdict « aucun avantage après frais » ne change pas.

**5. prix jamais disponible : présent**

* `src/tradebot/backtest.py:427` : `barres (de close[t] à close[t+h]) et la suivante ne peut s'ouvrir qu'à ``t + h```
* `src/tradebot/backtest.py:460` : `trades_ret.append(r[i])`
* `src/tradebot/targets.py:32` : `ret = log_close.shift(-h) - log_close`
* `src/tradebot/data.py:449` : `return f"{BULK_URL}/spot/{freq}/klines/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"`

C'est confirmé. L'entrée se fait au close[t], le dernier trade de la barre qui a servi au signal, et la sortie au close[t+h], sur des klines spot. Il n'y a ni latence (arrivée de la kline, puis inférence de 0,1 à 1,7 s par origine), ni bid/ask, ni base spot/perp. J'ai refait la vérification de l'auditeur à partir des predictions.csv des runs none s15 et j'obtiens les mêmes chiffres. Entrée décalée de +1 min, brut TimesFM au seuil 0,55 : BTC −0,26/−0,66/−0,03 → −0,26/−0,67/+0,11 pb ; ETH +0,24/−0,38/−0,74 → +0,75/−0,30/−0,08 pb ; SOL −0,23/−0,21/−0,39 → +0,37/+0,07/−0,16 pb. À +2 min : BTC −0,26/−0,48/+0,13, ETH −0,17/−0,65/−0,23, SOL −0,05/+0,15/+0,24. Aucune dégradation systématique, alors que TimesFM est en partie un signal de retournement (ρ(passé 60 min) ≈ −0,25). Un rebond bid/ask capturé à tort aurait dû faire baisser le brut quand on décale l'entrée.

*Effet sur la conclusion :* Le défaut existe, mais son effet mesuré est d'au plus ±0,7 pb par trade, sans signe, face à 10 pb de frais. Il ne change pas le verdict négatif. En revanche, il rendrait peu fiable un éventuel gain brut de quelques pb, comme les +3,5 à +6,6 pb du run SOL s60.

**6. réglages sur tout l'échantillon : partiel**

* `src/tradebot/cli.py:669` : `stride: int = 15, context_len: int = 512, backend: str = "timesfm3",`
* `src/tradebot/cli.py:672` : `batch_size: int = 32, start=None, end=None, symmetric: bool = True,`
* `docs/research/timesfm.md:43` : `0,27 à 0,58. Il faut donc traiter la longueur de contexte comme un hyperparamètre choisi sur la`
* `docs/research/timesfm.md:247` : ``data-api.binance.vision/api/v3/klines`, du 22/09/2026 04:52 au 25/09/2026 16:10 UTC, sans trou.`

C'est confirmé. Il y a une quinzaine de paramètres, aucun optimisé sur la performance du test. Les covariables top5 sont classées par ic_train_mean d'une étude dont l'apprentissage s'arrête le 2026-05-02 entre 22:59 et 23:18 UTC (run.json), et les origines commencent après. En revanche, le mini-test du 22 au 25/09/2026, dans la période de test, a guidé des choix de conception : centrage float64, logprice, moyenne symétrique (symmetric=True par défaut). Il y a donc un léger espionnage des données, qui pousserait plutôt vers l'optimisme. context_len = 512 n'est pas réglé, alors que la doc elle-même demande de le choisir sur l'apprentissage et mesure une corrélation de 0,27 à 0,58 entre contextes. C'est un risque pessimiste. La moyenne symétrique imposée (prévision antisymétrique) retire aussi toute asymétrie hausse/baisse que le modèle aurait pu apprendre : autre risque pessimiste, faible (voir new_issues).

*Effet sur la conclusion :* L'espionnage n'a pas d'effet, puisque le verdict est négatif. Un meilleur contexte pourrait gagner 1 à 2 points d'accuracy au plus (IC ≈ 0). Il faudrait près de 80–90 % d'accuracy pour couvrir 10 pb avec |rendement| médian de 5 à 19 pb : le verdict économique ne change pas. Le verdict statistique « TimesFM ≈ baselines » ne vaut que pour context = 512 et symmetric = True.

**7. hausse et baisse (régimes) : partiel** (le contradicteur corrige l'auditeur)

* `reports/timesfm/README.md:42` : `| `BTCUSDT_timesfm3_none_s15_c512_logprice` | BTCUSDT | timesfm3 | none | 15 | 512 | 1 500 | 2026-05-02 23:30 UTC | 2026-09-25 23:15 UTC | 184 | 8,2 |`
* `reports/timesfm/README.md:96` : `Backend `timesfm3`, contexte 512 (logprice), pas 60 min, origines du 2026-09-13 23:00 UTC au 2026-09-25 22:00 UTC (60 sur 288 candidates, 0 écartées pour trou), covariables : tick_imb_60, obv_slope_60`
* `reports/timesfm/README.md:103` : `| `timesfm` | toutes | 10 | 60 | 60 | 63,3 % | 64,0 % | 0,668 | 0,2270 | 0,6440 | 65,3 % | 0,026 | +2,01 | −1,26 |`
* `src/tradebot/cli.py:719` : `split_row = min(max(int(round(train_frac * n)), 0), n - 1)`

J'ai refait le calcul sur data/cache. Pour les 7 runs du 02/05 au 25/09, les chiffres de l'auditeur sont exacts. BTC : de 78 790 à 84 091 (+6,7 %), vol de 39 %, creux à 57 882 le 2026-07-01 01:12. Mois par mois : −6,5 % / −20,4 % / +7,1 % / +24,9 % / +7,0 %. ETH : +16,0 %, avec −13,6 % et −21,7 % en mai–juin puis +18,4 % / +32,4 % / +9,2 %. SOL : +44,5 %, avec −2,4 % / −10,6 % / −1,1 % puis +41,4 % / +18,6 %. Ces runs couvrent donc une phase baissière et une phase haussière : sur ce périmètre, le défaut est absent. Mais le run SOLUSDT top5 s60 est cité dans « En bref », et c'est le seul qui paraît positif (63,3 % à h = 10, p = 0,026 ; IC partiel ≈ +0,2 ; brut de +3,5 à +6,6 pb). Or il ne couvre que 12 jours (du 13/09 23:00 au 25/09 22:00) : un seul régime, haussier (SOL de 99,8 à 122,15, soit +22,4 %, vol de 64 %). Aucune ventilation par régime n'est publiée.

*Effet sur la conclusion :* Les runs longs ne sont pas biaisés par le régime, et leur verdict négatif tient. Les signes positifs du run SOL s60 sont mesurés sur un seul régime haussier de 12 jours avec 60 origines : il ne faut rien en conclure (biais optimiste possible). Le verdict global ne change pas.

**8. horloges et alignement : absent**

* `src/tradebot/data.py:20` : `doublon, horodaté à l'**ouverture** de la barre), colonnes float64 ``open, high, low, close,`
* `reports/timesfm/README.md:449` : `| `time` | Origine t : horodatage d'ouverture de la dernière barre connue (sa clôture est le dernier prix du contexte). |`
* `src/tradebot/targets.py:35` : `return ret.where(elapsed == h * bar).rename(f"ret_{h}")`
* `src/tradebot/backtest.py:145` : `cand = present & (pos >= context_len - 1) & ((pos + offset) % stride == 0)`

C'est vérifié. Il n'y a qu'une source de prix (klines spot 1 min, index open_time UTC). _epoch_to_datetime détecte l'unité valeur par valeur (ms avant 2025, µs ensuite), donc les fichiers mensuels ne se mélangent pas. La cible exige un écart exact de h barres. Les baselines, past_ret (cli.py, _past_return) et TimesFM sont joints sur (time, horizon) avec des index identiques. Le benchmark est aligné à l'horodatage exact dans le même fuseau (tz_convert). Aucune horloge locale, ni Chainlink, ni Polymarket n'intervient. Seule nuance : le prix spot sert à valoriser un trade supposé sur le perp, ce qui relève du point 5.

*Effet sur la conclusion :* Pas de désalignement. Le verdict n'est pas affecté.

**Autres défauts confirmés**

* **Verdict comparé à la MEILLEURE des 4 baselines, choisie après coup (biais contre TimesFM)** (confirmé, gravité basse, pessimiste) — `src/tradebot/report.py:524`. Confirmé, avec une nuance que l'auditeur a omise : le seuil de bruit (ligne 527 : noise = 1.96 * math.sqrt(2.0) * 0.5 / math.sqrt(max(ne, 1.0))) vaut ≈ 5,7 points pour n_eff = 598. Il compense largement l'excès attendu du maximum de 4 baselines (≈ +2 points). Le titre « moins bien qu'une baseline à h = 10 » du run BTC top5 vient d'un écart de 9 points (majority_prev_day 56,0 % contre TimesFM 47,0 %, README ligne 296), que la même baseline ne reproduit pas sur le run à 1 500 origines (49,0 %, ligne 158). Seul le titre est touché, pas la conclusion économique.
* **n_eff surestimé pour majority_prev_day (prévision constante sur la journée)** (confirmé, gravité basse, pessimiste) — `src/tradebot/backtest.py:375`. Le mécanisme est confirmé : la prévision est constante sur un jour UTC, et les labels d'un même jour sont corrélés par la dérive intrajournalière. Ordre de grandeur : une dérive quotidienne d'environ 2 % représente ≈ 2 pb par 15 min face à σ15 ≈ 15 pb, soit un décalage de P(hausse) de ≈ 5 points. Avec ≈ 4 à 10 origines par jour, la variance est gonflée de ≈ 5 à 10 %, et le se est sous-estimé d'environ 3 à 5 %. p_binom = 0,002 deviendrait ≈ 0,003–0,005 : l'effet est plus faible que ne le suggère l'auditeur. Il ne touche que les baselines.
* **Tests multiples non corrigés** (confirmé, gravité basse, neutre) — `reports/timesfm/README.md:7`. Confirmé : 8 runs × 3 horizons × 6 signaux, sans correction. Les « succès » isolés (TimesFM 63,3 % à h = 10 sur SOL s60, p = 0,026, ligne 103 ; majority_prev_day 56 %, p = 0,002) sont compatibles avec le hasard sur ≈ 144 tests. La conclusion négative n'en dépend pas.
* **Puissance volontairement réduite : 1 500 origines sur 14 016 disponibles** (confirmé, gravité basse, pessimiste) — `src/tradebot/cli.py:670`. Confirmé : le défaut du CLI est de 1 000 origines, et les runs publiés en ont 1 500, 600, 200 et 60 pour 14 016 (ou 7 008, ou 288) candidates. L'effet minimal détectable passe d'environ 51 % à 53,2–55,2 %. Un avantage de 1 à 2 points serait invisible, mais il ne vaudrait que ≈ 0,1 à 0,4 pb par trade face à 10 pb. Le verdict économique n'est pas touché ; le verdict statistique est seulement moins puissant.
* **Chiffres de stratégie timesfm_cal publiés sur 0 à 3 trades** (confirmé, gravité basse, incertain) — `reports/timesfm/README.md:265`. Confirmé pour certaines cellules : 0 trade (lignes 216, 268, 399), 3 (ligne 265), 7 (ligne 219), 23 (ligne 222). L'affirmation « P(hausse) ne franchit presque jamais 0,55 » est toutefois fausse ailleurs : 314 trades pour ETH none h = 5 (ligne 262), 299 pour BTC top5 h = 10 (ligne 311), où l'isotonique s'est réduite à un taux de base constant (voir new_issues). La contradiction avec le §5 (ligne 529) est réelle, mais elle n'affecte pas le verdict négatif.
* **Égalités et signaux nuls des baselines comptés comme prévision « baisse »** (confirmé, gravité basse, incertain) — `src/tradebot/backtest.py:237`. Le mécanisme est confirmé : p = 0,5 puis pred = (p > 0.5) (ligne 378) donne « baisse » ; même chose dans _acc_w (cli.py, ligne 574). J'ai mesuré la part de rendements à 5 min nuls sur le cache : 4,3 % pour SOL, 0,44 % pour BTC, 0,35 % pour ETH. L'effet sur l'accuracy vaut environ la part d'égalités × (écart d'accuracy entre cas nuls et autres cas), soit ≈ 0,04 × 2 pts ≈ 0,1 point sur SOL et ≈ 0,01 point sur BTC et ETH. L'estimation de l'auditeur (±0,5 à 1 point) est exagérée : l'effet est négligeable.
* **Run SOL s60 non reproductible (agrégat de covariables d'une étude remplacée depuis)** (confirmé, gravité basse, optimiste) — `reports/timesfm/README.md:96`. Confirmé par runs.csv : test_part_start = 2026-05-02 22:55, mais first_origin = 2026-09-13 23:00. Le début a donc été repoussé à un study_train_end du 13/09, alors que le run.json actuel donne des splits au 2026-05-02 (22:59 à 23:18) et que l'étude actuelle retient d'autres covariables (ema_dist_60, ema_dist_30, …). Le choix reste causal pour ce run, mais il n'est pas reproductible. C'est aussi le seul run qui paraît positif, sur un seul régime haussier (point 7).
* **L'isotonique s'effondre en taux de base : timesfm_cal = pari directionnel constant (BTC top5)** (nouveau, gravité basse, incertain) — `reports/timesfm/README.md:284`. Sur BTC top5, la balanced accuracy de timesfm_cal vaut 49,3 % aux trois horizons, et la stratégie ouvre 299 trades sur ≈ 298 origines à h = 10 (ligne 311 : « | `timesfm_cal` | 10 | 0,55 | 10 | 299 | 44,1 % | −1,28 | −11,28 | 0,75 | »). Avec ≈ 300 labels, la régression isotonique apprise sur la 1re moitié (mai–juillet) a rendu une fonction presque constante (le taux de base de la période). La « stratégie recalibrée » revient alors à parier toujours dans le même sens, ce qui ne mesure pas le signal TimesFM. Le §5 (ligne 529) présente pourtant timesfm_cal comme la seule stratégie qui ait du sens.
* **timesfm_cal évalué sur la 2e moitié mais comparé à des baselines évaluées sur toutes les origines** (nouveau, gravité basse, neutre) — `src/tradebot/cli.py:631`. Le README (ligne 51) affirme « baselines sur les mêmes origines ». C'est vrai pour timesfm, pas pour timesfm_cal : sa ligne porte sur la 2e moitié (≈ août–septembre, période haussière), alors que les baselines sont mesurées sur toute la période. Les tableaux comparent donc des échantillons différents. Le taux de base diffère de quelques points entre les deux moitiés, et la comparaison timesfm_cal contre baselines peut être faussée d'environ 1 à 3 points dans un sens ou dans l'autre.
* **Brier et DM-Brier : P brute sur-confiante comparée à des baselines bloquées à 0,52** (nouveau, gravité basse, pessimiste) — `src/tradebot/cli.py:639`. Les baselines émettent 0,52 ou 0,48 (BASELINE_P, backtest.py ligne 42), donc un Brier toujours ≈ 0,2500. La P brute de TimesFM est sur-confiante, comme le code le reconnaît : son Brier est pénalisé par la calibration, pas par la discrimination (exemple : BTC s30 h = 15, Brier 0,2690, DM t = +2,55). Le DM-Brier compare donc calibration et information pêle-mêle, au détriment de TimesFM. Ce n'est pas le critère principal du verdict (accuracy, AUC, stratégie), mais la colonne peut faire croire à tort que TimesFM est pire que le retournement.
* **Moyenne symétrique imposée : toute asymétrie hausse/baisse du modèle est supprimée** (nouveau, gravité basse, pessimiste) — `src/tradebot/forecaster.py:22`. symmetric=True par défaut (cli.py, ligne 672) force f(−x) = −f(x). Cela élimine tout biais haussier appris, mais aussi toute réponse asymétrique réelle (chute plus rapide que la hausse, retournement différent après une baisse et après une hausse). Ce choix a été fait a priori, après le mini-test du 22 au 25/09 situé dans la période de test, et n'a jamais été confronté à symmetric=False sur l'apprentissage. Il pourrait masquer un petit avantage, sans doute inférieur à 1 point d'accuracy vu les IC ≈ 0. Il ne peut pas couvrir 10 pb.

## Polymarket : nos modèles contre le prix du marché avant l'ouverture (scripts/polymarket_models_vs_market.py, src/tradebot/polymarket_backtest.py, src/tradebot/polymarket.py, scripts/polymarket_diagnostic.py ; rapports reports/polymarket/modeles_vs_marche/README.md et reports/polymarket/diagnostic.md) : contre-expertise

**1. information future (look-ahead) : partiel**

* `src/tradebot/polymarket_backtest.py:231` : `"a_open": O[:, 0], "a_close": C[:, 29], "a_twap": C[:, a].mean(1), "a_vwap": vwap_a,`
* `src/tradebot/polymarket_backtest.py:333` : `vals = bar_lookup(ind, s - 120, list(ind.columns))`
* `src/tradebot/polymarket_backtest.py:418` : `hi = int(train_end_s) - D - int(gap_s)            # dernière origine admise (purge du test)`
* `src/tradebot/data.py:963` : `merged = pd.merge_asof(left, right, on="_t", direction="backward",`

J'ai vérifié les citations : elles sont toutes exactes. J'ai aussi cherché un contre-exemple de fuite dans tout le chemin de code, sans en trouver.
* Indicateurs 1m : ils sont lus à la bougie ouverte à S−120 s, close à S−60 s (ligne 333), et tradebot/indicators.py n'a ni décalage négatif, ni fenêtre centrée, ni bfill.
* Variables 1s : a_close = C[:, 29], c'est-à-dire la bougie ouverte à S−31 s et close à S−30 s. Les minutes antérieures sont entières. ffill seulement.
* σ et volume moyen : rolling sur un index trié, lu à S−120 s.
* Dérivés : merge_asof backward sur la date de disponibilité (≤ ouverture de barre).
* Étiquette d'apprentissage : purgée avant le test (ligne 418).
* Horodatages Binance en µs : détectés dans data._epoch_to_datetime.

Les deux réserves de l'auditeur sont exactes.
(1) La latence est nulle : la bougie qui se ferme à S−30,000 s sert à un ordre placé à S−30 s.
(2) Le prix de référence est périmé. D'après marches_predictions.csv, l'ancienneté médiane du point est de 17 s (quartiles 15–18 s ; le point tombe vers la 13e seconde de chaque minute). S'y ajoutent environ 10 s de retard interne (diagnostic, ligne 368). Le modèle dispose donc d'environ 27 s d'information Binance de plus que le prix « d'exécution ». C'est d'abord un défaut d'exécution (point 5) et de comparaison (point 8), pas une fuite dans les variables.

Je corrige l'ordre de grandeur de la latence. Dans la simulation, elle ne change rien au prix payé : l'ordre s'exécute au point périmé p_pre, quelle que soit la latence. Sur le signal, perdre 0,3 à 1 s de tw_gap30 est négligeable, de l'ordre de moins de 0,1 point de justesse. L'estimation de l'auditeur (0,1 à 0,5 c par part) me paraît donc haute ; moins de 0,2 c est plus plausible.

*Effet sur la conclusion :* Biais optimiste. La latence nulle pèse moins de 0,2 c par part, peut-être 0 dans la simulation. L'avance d'information d'environ 27 s sur le prix de référence est la vraie source du gain brut de 1 à 3 c par part, et elle est déjà absorbée par le surcoût mesuré (point 5). Corriger ne change pas le verdict « pas d'avantage démontré après frais », cela le renforce. En revanche, les phrases qui présentent un gain brut positif (« 4 modèles sur 7 … IC qui exclut 0 ») et le titre du § 3, « au même instant », sont trompeuses.

**2. survivants : absent**

* `scripts/polymarket_models_vs_market.py:1839` : `base = mk[mk["closed"] & mk["y"].notna()].copy()`
* `scripts/polymarket_models_vs_market.py:1849` : `M = M[np.isfinite(M["p_pre"]) & np.isfinite(M[f"p_{models[0]}"])].reset_index(drop=True)`
* `scripts/polymarket_models_vs_market.py:117` : `expected = {(a, d): len(pm.slot_starts(d, args.start, args.end)) for a in args.assets for d in args.durations}`
* `reports/polymarket/modeles_vs_marche/README.md:22` : `Créneaux attendus 48 384, trouvés 48 380, résolus 48 380, en TWAP-60 48 380`

Les citations de l'auditeur sont exactes. Les nombres du README contiennent des espaces fines insécables (U+202F), que j'ai reproduites.

L'univers est bien construit à partir de tous les créneaux déterministes (slot_starts, ligne 117), et non à partir des marchés encore listés. list_updown_markets ne filtre les marchés fermés que si on le lui demande (only_closed), ce que market_table ne fait pas. Les chiffres :
* 48 380 marchés trouvés sur 48 384 ;
* 48 380 résolus, aucun annulé ni résolu à 50/50 ;
* 57 exclus faute de point prices-history dans les 5 min avant S−30 s, soit 0,12 %, avec une couverture de 99,9 % dans chaque cellule. Ce critère ne dépend pas de l'issue, et ces marchés n'étaient de toute façon pas négociables sans cotation ;
* aucun marché exclu faute de prévision (48 380 − 57 = 48 323).

Seule nuance : les 4 créneaux absents n'ont pas été vérifiés un par un dans ce backtest. Le diagnostic ne l'a fait que pour 1 créneau, sur sa semaine. L'effet est de toute façon nul (4 marchés sur 48 384).

L'échantillon sélectionné du contrôle d'exécution (marchés ayant au moins une transaction du côté choisi) relève des other_issues, pas de l'univers principal.

*Effet sur la conclusion :* Pas de biais mesurable : moins de 0,1 point de justesse et moins de 0,1 c par part. Aucun effet sur le verdict.

**3. données futures dans un indicateur : absent**

* `src/tradebot/polymarket_backtest.py:426` : `cal = (s >= b2) & (s <= hi)`
* `src/tradebot/polymarket_backtest.py:517` : `lo, hi = np.quantile(raw, [self.winsor, 1.0 - self.winsor]) if self.winsor > 0 else (0.0, 1.0)`
* `src/tradebot/polymarket_backtest.py:474` : `med = np.nanmedian(X, axis=0)`
* `src/tradebot/polymarket_backtest.py:211` : `close = pd.Series(close).ffill().to_numpy()`

Toutes les citations sont exactes. J'ai cherché un contre-exemple dans tout le chemin de code, sans en trouver :
* L'isotonique et les bornes de winsorisation sont apprises sur le segment cal, qui se termine avant le 14/08 moins D moins 1 h. calibrate_isotonic extrapole à plat.
* La médiane d'imputation et le StandardScaler (dans le pipeline) sont appris sur l'apprentissage seulement. Les bacs du HGB aussi.
* Côté 1s, seul ffill est utilisé, jamais bfill.
* Les rolling (σ 60 min, volume moyen) portent sur un index trié et sont lus à S−120 s.
* Les dérivés passent par un merge_asof backward avec tolérance.
* bar_lookup ne fait aucun report.
* La calibration TimesFM utilise 4 semaines avant le 14/08.
* Les déciles du § 4 sont purement descriptifs.

Une précision sur l'argument (7) de l'auditeur (« p_stack = NaN sur la 1re »). C'est vrai pour les métriques. Mais le P&L de stack utilise p_stack_pnl, et donc ses probabilités EN ÉCHANTILLON sur la 1re moitié pour choisir la marge (ligne 1887). Ce n'est pas un défaut de réécriture : c'est un choix de paramètre en échantillon, traité au point 6 et dans les nouveaux défauts.

*Effet sur la conclusion :* Aucun biais de réécriture. Aucun effet sur le verdict.

**4. frais et glissement : partiel**

* `src/tradebot/polymarket_backtest.py:703` : `ask_up = m + half_spread
    ask_dn = 1.0 - m + half_spread
    fee_up = _fee_per_share(ask_up, fee_rate, fee_exponent)
    fee_dn = _fee_per_share(ask_dn, fee_rate, fee_exponent)`
* `src/tradebot/polymarket_backtest.py:72` : `HALF_SPREAD = 0.005        # ask ≈ milieu + 0,005, bid ≈ milieu − 0,005`
* `src/tradebot/polymarket.py:839` : `fee = c * rate * np.power(np.clip(p * (1.0 - p), 0.0, None), exponent)`
* `reports/polymarket/modeles_vs_marche/README.md:23` : `Le diagnostic a mesuré que les preneurs ont en réalité payé ≈ 1,7 c de plus dans [S−30 s, S) pour la baseline `gap_m30`, et 1,3 c à 3,1 c de plus sur les positions des modèles (§ 5 bis) : l'hypothèse `

Les citations sont exactes.

Les frais sont justes. Le calcul est shares × 0,07 × p(1−p), soit 1,75 c à 0,50. Il est vérifié contre la table officielle citée dans la docstring de taker_fee, et appliqué marché par marché au prix d'achat, sur chaque entrée. Il n'y a pas de sortie : la position est gardée jusqu'à la résolution.

Le glissement, lui, est un demi-écart fixe de 0,005 ajouté à un milieu périmé. Il n'y a ni profondeur, ni impact, ni file d'attente, et ce demi-écart est incompatible avec les 28,6 % de milieux tombant sur un cent entier. Le surcoût réel (1,3 à 3,1 c) n'est intégré qu'en robustesse (§ 5 bis), sur un sous-échantillon étroit (voir other_issues).

La composante pessimiste relevée par l'auditeur existe. Le « prix payé » est un VWAP pondéré par la taille (script ligne 677 et diagnostic ligne 941), donc plus élevé que le meilleur ask qu'aurait payé un ordre d'une part. Mais il n'y a aucune donnée par transaction en cache pour la chiffrer : l'estimation « 0,3 à 1 c » de l'auditeur est une conjecture, pas une mesure.

Je nuance aussi la critique de l'auditeur sur « il n'en reste rien de démontrable ». La formule parle bien d'absence de preuve, pas d'absence d'avantage : elle est exacte.

*Effet sur la conclusion :* Au prix supposé, le P&L du § 5 comporte un biais optimiste d'environ 1,3 à 3,1 c par part (selon le modèle ; 1,7 c pour gap_m30). Le verdict le corrige déjà : de −1,89 à +0,74 c par part après surcoût, aucun IC entièrement positif. La surestimation du surcoût par la pondération est d'ampleur inconnue, probablement inférieure à 1 c. Même en l'enlevant entièrement, les IC d'environ ±1,7 à 3 c contiendraient encore 0. Le verdict « pas d'avantage démontré après frais » tient.

**5. prix jamais disponible : présent**

* `src/tradebot/polymarket_backtest.py:29` : `Exécution simulée (preneur) : ``ask_Up = p_Up(S−30 s) + 0,005`` et
``ask_Down = 1 − p_Up(S−30 s) + 0,005`` (carnet ≈ 0,50 / 0,51 avant l'ouverture ; le point`
* `src/tradebot/polymarket.py:796` : `i = idx.searchsorted(t, side="right") - 1`
* `reports/polymarket/modeles_vs_marche/README.md:261` : `le point `prices-history` est un milieu de fourchette (ancienneté médiane 17 s, et en retard d'environ 10 s sur Binance d'après le diagnostic)`
* `reports/polymarket/modeles_vs_marche/README.md:76` : `coefficients 0,610, −0,153, 0,004`

Les citations sont exactes, et le défaut est confirmé : on exécute au milieu du dernier point prices-history ≤ S−30 s, plus 0,005. Ce point a 17 s d'ancienneté médiane et environ 10 s de retard interne.

La sélection sur les cotations périmées est visible dans les données :
* surcoût mesuré de 2,02 à 3,14 c pour les modèles, contre 0,45 c pour « toujours Up » ;
* le surcoût est déjà présent dans [S−30 s, S−20 s) (2,07 c pour logit_tw), donc il ne vient pas des seules transactions tardives ;
* le coefficient négatif de stack sur logit(p_pre), −0,153, montre qu'il parie contre le milieu périmé ;
* dans la 1re ligne de trades_windows.csv, p_pre vaut 0,515 alors que les preneurs Up ont payé 0,595 dans [S−30 s, S).

S'y ajoutent une profondeur illimitée (1 part sans vérifier la taille à l'ask), aucune file d'attente et un maker toujours exécuté. Le proxy du prix payé (autres preneurs, horodatés au bloc) ignore aussi notre latence.

Je tempère une seule formule de l'auditeur. Il est exagéré d'écrire que « tous les P&L +2,20 à +2,88 c sont des artefacts d'exécution ». Il est seulement établi qu'ils ne survivent pas au surcoût mesuré (−1,89 à +0,74 c) : la part d'artefact n'est pas mesurée à 100 %.

*Effet sur la conclusion :* Biais optimiste principal, d'environ 1,3 à 3,1 c par part, soit l'ordre de grandeur de tout le gain brut. Le verdict écrit le prend déjà en compte et reste valable. Les phrases positives du § 0 (« IC qui exclut 0 ») et la borne maker (+2,79 c, autant que gap_m30 à +2,74 c) ne doivent pas être lues comme des résultats.

**6. réglages sur tout l'échantillon : présent**

* `reports/polymarket/diagnostic.md:15` : `* **Meilleure baseline pré-ouverture « propre »** (information ≤ S−30 s, choisie in-sample) : `gap_m30:momentum`, justesse 52,2 % (IC robuste 50,6 % – 53,7 %, n = 8 063).`
* `reports/polymarket/diagnostic.md:3` : `Période : du 18/09/2026 00:00 au 25/09/2026 00:00 UTC (7 jours complets)`
* `scripts/polymarket_models_vs_market.py:274` : `FRACS = (0.75, 0.10, 0.15)   # apprentissage / arrêt précoce / calibration isotonique (dans le temps)`
* `src/tradebot/polymarket_backtest.py:446` : `des observations, au lieu de quelques points poussés à 0,01 / 0,99 (surconfiance mesurée
    sans cette précaution sur un segment de calibration de ≈ 8 000 origines).`

Les citations sont exactes et le décompte de l'auditeur est juste, à une correction près.

La variable centrale gap_m30 et l'instant S−30 s viennent du diagnostic. Or ce diagnostic porte sur le 18–24/09, compris dans la 2e moitié de test, et il a choisi cette variable en échantillon parmi plus de 21 règles.

La correction : FRACS n'a probablement PAS été fixé a priori. Le diff git b5107c2 → 5cdb2b3 montre que la winsorisation (winsor = 0,01) a été ajoutée après une première exécution. Sa justification mentionne un segment de calibration de ≈ 8 000 origines. Or c'est la taille qu'aurait eue le segment cal des modèles 1s avec la valeur par défaut, 10 %, contre n_cal = 11 784 avec 15 % dans train_5m_…_v2.json. FRACS (0,8 / 0,1 / 0,1 → 0,75 / 0,10 / 0,15) et la winsorisation ont donc très probablement été changés ensemble entre deux exécutions (cache « _v2 »). On ne sait pas si les résultats de test avaient été vus.

Autres points :
* la marge de stack est choisie sur ses probabilités en échantillon (ligne 1887) ;
* les métriques « période complète » incluent la 1re moitié, qui sert au choix du modèle ;
* contre-argument de l'auditeur, vérifié ligne 102 du README : gap_m30 fait 52,6 % sur la 1re moitié et 52,3 % sur la 2e, ce n'est donc pas un artefact de la semaine du diagnostic.

*Effet sur la conclusion :* Biais optimiste modeste : 0 à 0,3 point de justesse, moins de 0,5 c par part. Le modèle désigné d'avance par la règle P&L, logit_tw, ne fait que +0,94 c sans significativité. Corriger ne change pas le verdict négatif, cela le renforce. Les modèles mis en avant parce que leur IC exclut 0 (hgb_ind1s, stack) sont les plus exposés à ce biais de sélection.

**7. hausse et baisse (régimes) : partiel** (le contradicteur corrige l'auditeur)

* `reports/polymarket/modeles_vs_marche/README.md:3` : `fenêtres du 14/08/2026 au 24/09/2026 23:59 UTC (régime TWAP-60 homogène)`
* `reports/polymarket/modeles_vs_marche/README.md:52` : `La période est coupée en deux moitiés : la 1re (14/08–03/09) sert de **validation** (choix de la marge, apprentissage de l'empilement), la 2e (04/09–24/09) de **test final**.`
* `scripts/polymarket_models_vs_market.py:1860` : `mid = args.split if args.split is not None else args.start + (args.end - args.start) / 2`
* `reports/polymarket/modeles_vs_marche/README.md:263` : `La relation s'est affaiblie entre la calibration (fin juillet – mi-août) et le test.`

J'ai recalculé les chiffres de l'auditeur sur data/cache/BTCUSDT_1m.parquet. Ils sont exacts :
* période complète : +32,9 %, volatilité 39,3 % ;
* 1re moitié : +28,0 %, volatilité 42,5 % ;
* 2e moitié : +3,9 %, volatilité 35,9 % ;
* apprentissage 1m : −44 % ; 1s : −21,7 % ;
* segment de calibration (24/07–13/08) : −2,5 %, volatilité 26,3 %.

En revanche, « AUCUN régime baissier » est trop fort. La 2e moitié contient une baisse d'environ 10 jours : clôtures de 80 342 le 06/09 à 75 644 le 15/09, soit −5,8 %, avec un creux intrajournalier de −8,6 % le 15/09 à 18:49. Au total, 17 jours sur 41 sont négatifs.

La volatilité hebdomadaire varie aussi, de 31 à 53 % pour BTC (hors semaine partielle), de 46 à 79 % pour ETH et de 51 à 93 % pour SOL. Il y a donc une variété modérée de régimes, mais :
* aucun marché baissier prolongé ;
* aucun régime calme comparable à celui de la calibration (26 %) ;
* un seul régime de résolution (TWAP-60), sur 6 semaines.

D'où PARTIAL plutôt que PRESENT. Sur 5–15 min, la tendance compte peu : le taux de Up reste d'environ 50 %. Ce qui compte, c'est la rupture de volatilité entre calibration et test. L'auditeur l'a bien relevée (AUC 0,56 en calibration contre 0,54 en test).

*Effet sur la conclusion :* Le biais est incertain, avec probablement une composante pessimiste : une isotonique apprise sur un segment calme donne des probabilités trop dispersées, donc des positions à faible avantage. Le verdict « pas d'avantage démontré » reste valable sur cette période, mais ni une conclusion positive ni une conclusion négative n'est généralisable à un régime calme ou baissier prolongé.

**8. horloges et alignement : partiel**

* `reports/polymarket/diagnostic.md:11` : `Le flux Chainlink semble donc en retard d'environ 4 s sur Binance (décalage choisi sur ces données).`
* `reports/polymarket/modeles_vs_marche/README.md:261` : `le point `prices-history` est un milieu de fourchette (ancienneté médiane 17 s, et en retard d'environ 10 s sur Binance d'après le diagnostic)`
* `src/tradebot/polymarket.py:770` : `"time": pd.to_datetime(df["timestamp"].astype("int64"), unit="s", utc=True).dt.as_unit("ns"),`
* `scripts/polymarket_models_vs_market.py:646` : `SUBWINDOWS = {"w10": (-30, -20), "w30": (-30, 0)}                  # secondes relatives à S`

Les citations sont exactes.

Ce qui est cohérent : tout est en UTC et en secondes Unix. La convention d'ouverture des bougies Binance est respectée. Les horodatages µs des zips 1s sont détectés par _epoch_to_datetime. Polymarket et les transactions sont en secondes.

Les trois horloges mal alignées relevées par l'auditeur sont confirmées :
(a) Le point prices-history est traité comme une information à S−30 s. Il est horodaté vers S−47 s (ancienneté médiane 17 s) et reflète environ S−57 s. La comparaison modèle contre marché du § 3 (« au même instant ») favorise donc le modèle.
(b) Chainlink retarde d'environ 4 s sur Binance. À S−30 s, environ 34 s du TWAP60 Chainlink sont connues, et non 30. Ce décalage est ignoré, ce qui est un peu pessimiste.
(c) Les transactions sont horodatées au bloc, à la seconde entière, environ 2 s après l'appariement. Les sous-fenêtres du contrôle d'exécution sont donc décalées d'environ 2 s.

*Effet sur la conclusion :* (a) : biais optimiste de 0,2 à 0,3 point de justesse. Sur le P&L au prix supposé, il se confond avec le point 5 (1 à 3 c par part), déjà absorbé par le surcoût mesuré. (b) : biais pessimiste, inférieur à 0,2 point. (c) : effet négligeable. Aucun ne change le verdict.

**Autres défauts confirmés**

* **Mise en avant de résultats positifs hors règle fixée d'avance ; correction pour tests multiples incomplète** (confirmé, gravité moyenne, optimiste) — `reports/polymarket/modeles_vs_marche/README.md:18`. La citation est exacte. La bande simultanée (robustness_table) ne couvre que les 7 modèles en mode taker. Elle ignore les 2 règles de choix, les 3 variantes d'exécution, les cellules du § 5 (par exemple stack ETH 5m à +5,33 c) et les comparaisons de justesse et de Brier sur la période complète. Le verdict final reste pourtant négatif et explicite : c'est un défaut de présentation, qui ne change pas la conclusion.
* **Surcoût d'exécution mesuré par un VWAP pondéré par la taille (surestimé pour 1 part)** (confirmé, gravité moyenne, pessimiste) — `scripts/polymarket_models_vs_market.py:677`. La citation est exacte, et la même pondération se trouve dans le diagnostic (scripts/polymarket_diagnostic.py, ligne 941), d'où vient le surcoût principal pre30. Le mécanisme est réel : les gros ordres qui balaient le carnet pèsent davantage que le meilleur ask. Aucune donnée par transaction n'est en cache pour le chiffrer : l'estimation « 0,3 à 1 c » n'est pas vérifiée. Même corrigé, les IC après surcoût contiendraient encore 0.
* **Surcoût estimé sur un sous-échantillon étroit, non représentatif et sélectionné** (confirmé, gravité moyenne, incertain) — `scripts/polymarket_models_vs_market.py:720`. La citation est exacte. Le sous-échantillon compte 600 marchés du 18–24/09 (100 par cellule), réduits à ceux qui ont une transaction du côté choisi.
* Taux de NaN pre30_up_vwap dans trades_windows.csv : BTC 5m 3 %, SOL 5m 40 %, ETH 15m 56 %, SOL 15m 70 %.
* Le P&L au prix supposé y diffère fortement de celui de toute la 2e moitié : hgb_ind1s −5,48 c contre +2,20 c, hgb_tw −8,77 c contre +0,97 c (README, lignes 229 et 231).
* L'IC après surcoût combine deux erreurs types par approximation normale, en supposant les deux estimations indépendantes.
L'estimation du surcoût (IC étroits, par exemple 1,39–3,05 c) reste cohérente d'un modèle à l'autre. Le sens du biais est incertain.
* **Marge non ré-optimisée sous le coût réel** (confirmé, gravité basse, incertain) — `scripts/polymarket_models_vs_market.py:785`. La citation est exacte : le P&L après surcoût retranche un surcoût moyen des positions prises à une marge choisie sous l'ask optimiste. Ce n'est pas une stratégie réellement suivie. Relever la marge réduirait le nombre de positions, mais le surcoût croît justement quand le signal s'écarte du prix. Le sens est incertain et l'effet probablement inférieur à 1 c par part.
* **IC par blocs d'un jour plus étroits que les IC par créneau (21 grappes)** (confirmé, gravité basse, optimiste) — `reports/polymarket/modeles_vs_marche/README.md:218`. La citation est exacte (les espaces des nombres sont des U+202F). Avec 21 grappes, l'IC par jour est plus étroit que l'IC par créneau et exclut 0. Le README le reconnaît : il présente ce bootstrap comme un contrôle, pas comme la référence (ligne 207). Les IC par créneau ignorent la corrélation entre créneaux d'une même heure ; ils sont probablement un peu trop étroits.
* **Étiquette d'entraînement VWAP 1m alors que le TWAP60 1s exact est disponible pour les modèles 1s** (confirmé, gravité basse, pessimiste) — `scripts/polymarket_models_vs_market.py:261`. La citation est exacte. y1s (TWAP60 1s) est calculé aux lignes 262–264, mais ne sert que de contrôle. L'accord avec l'issue est de 96,1 % pour l'étiquette VWAP 1m, contre 98,1 % pour y1s : environ 2 points de bruit d'étiquette en plus pour les modèles *_tw et *_ind1s. L'effet pessimiste est faible, de l'ordre de 0,1 point de justesse.
* **TimesFM évalué sur une cible différente de la règle de résolution** (confirmé, gravité basse, pessimiste) — `scripts/polymarket_models_vs_market.py:430`. La citation est exacte. La cible est close(E) ≥ close(S−60 s), en accord à environ 89,5 % avec l'issue. Le contexte 1m s'arrête à S−60 s, et l'isotonique est apprise sur environ 750 origines. Le biais est défavorable à TimesFM, mais l'AUC brute de 0,51 laisse peu d'espoir.
* **FRACS et winsorisation modifiés après une première exécution (données de test probablement vues)** (nouveau, gravité basse, optimiste) — `src/tradebot/polymarket_backtest.py:446`. Le diff git b5107c2 → 5cdb2b3 montre que la winsorisation à 1 % a été ajoutée après une première exécution. Les « ≈ 8 000 origines » correspondent au segment cal des modèles 1s avec la valeur par défaut de fracs, 10 %. L'exécution publiée utilise FRACS = (0.75, 0.10, 0.15) (script, ligne 274) et n_cal = 11 784 (train_5m_…_v2.json), avec le cache marqué « _v2 ». Deux hyperparamètres de calibration ont donc été changés entre deux exécutions complètes. L'auditeur range FRACS parmi les paramètres « fixés a priori ». On ne sait pas si le test avait été regardé. Effet probable : moins de 0,5 c par part. Le verdict négatif ne change pas.
* **Marge de l'empilement choisie sur ses probabilités en échantillon** (nouveau, gravité basse, optimiste) — `scripts/polymarket_models_vs_market.py:1887`. Pour le P&L, stack utilise sur la 1re moitié des probabilités ajustées sur cette même moitié (ligne 1928 : M.assign(p_stack=M["p_stack_pnl"])). Sa marge de 1,25 c est donc choisie en échantillon. Le README le signale (ligne 156), mais le § 0 cite quand même stack parmi les deux modèles dont l'IC simultané exclut 0. Avec 4 paramètres seulement, le surajustement est faible (probablement moins de 0,3 c par part). Le résultat après surcoût (−0,89 c) reste négatif.
* **Conventions d'égalité incohérentes entre la justesse du marché et la baseline mkt_gt_05** (nouveau, gravité basse, neutre) — `scripts/polymarket_models_vs_market.py:1895`. La justesse du marché compte p_pre = 0,500 comme « Up » (ligne 496, p ≥ 0,5), alors que la baseline mkt_gt_05 le compte comme « Down » (p > 0,5). Cela concerne 17,5 % des marchés. D'où l'écart entre « marché » (51,1 %) et « marché > 0,5 » (51,3 %) dans le tableau du § 3. C'est sans conséquence sur le verdict, mais les deux lignes ne mesurent pas la même règle.

## Polymarket : top wallets et copy-trading

**1. information future (look-ahead) : partiel**

* `scripts/polymarket_top_wallets.py:680` : `h_dec = (ev["ts"].to_numpy() - SAFETY_S - t_start) // 3600`
* `scripts/polymarket_top_wallets.py:290` : `s = csum[h] - csum[h - lookback]`
* `scripts/polymarket_top_wallets.py:661` : `lo_key = (tok << 39) | ((ts + d - t0) << 20) | (seq_max + 1 if d == 0 else 0)`
* `reports/polymarket/top_wallets/README.md:44` : `| Mempool (`newPendingTransactions`, décodage de `matchOrders`) | oui | — | 0,65 s après l'appariement (92 % des trades) | ≈ 1 s |`

Toutes les citations de l'auditeur sont exactes. Les protections sont réelles. Le classement est causal : somme sur les heures [H−L, H), l. 290. La décision est décalée de 120 s (l. 680) : les marchés classés ont fini à H:00, leur résultat est public vers H:00:50 et la décision tombe à H:02 au plus tôt. Aucun autre look-ahead dans le chemin de copie : le prix et les frais viennent de trades réellement postérieurs, et `won` ne sert qu'au P&L. Deux corrections de magnitude. (a) Contrairement à ce qu'écrit l'auditeur, d = 1 s n'est pas « physiquement impossible » : le README (l. 44) donne d ≈ 1 s par le mempool. Même d = 0 n'est pas exclu dans ce cas, puisqu'on apprend le trade 0,65 s après l'appariement, avant qu'il n'entre dans un bloc (≈ 2,2 s). En revanche, d = 0 place la copie dans la MÊME seconde que le leader dans 51 % des copies de la cellule top 10 / 1 h (mesuré sur le cache), ce qui est optimiste. (b) J'ai mesuré ce biais sur l'échantillon apparié (25 137 événements exécutables à tous les délais) : P&L(d = 3) − P&L(d = 0) = −0,12 c (IC −0,20 ; −0,05). Le biais optimiste de d = 0 vaut donc ≈ 0,12 c/part, soit plus que l'estimation « 0 à 0,05 c » de l'auditeur. (c) Fuite des cash-out après E+120 s : < 0,2 % du volume de classement, négligeable. (d) Q5 : le flux et le prix sont tous deux lus en temps de bloc, jusqu'à S+c. La fuite (2 à 6 s de publication) est symétrique.

*Effet sur la conclusion :* Biais optimiste ≈ 0,1 c/part sur d = 0 seulement, nul sur la cellule principale d = 3 s (délai atteignable avec les logs ou le RTDS). Toutes les cellules restent négatives : corriger ne change pas le verdict « la copie perd ». La comparaison flux/prix de Q5 ne change pas non plus.

**2. survivants : partiel**

* `src/tradebot/polymarket_wallets.py:562` : `markets += [m for m in ms if m.resolved_up is not None]`
* `reports/polymarket/top_wallets/README.md:3` : `Elle porte sur les fenêtres qui commencent entre le 23/09/2026 21:00 et le 25/09/2026 21:00 UTC : 48 heures, 2 304 marchés résolus`
* `scripts/polymarket_top_wallets.py:553` : `both = act[h] & act[h + k]`
* `scripts/polymarket_top_wallets.py:581` : `nxt = g & act[h + k]`

Vérifié sur le cache : 2 304 marchés, soit 576 de 5m et 192 de 15m pour chacun de BTC, ETH et SOL. C'est exactement 48 h × 16 créneaux × 3 actifs : le filtre de la l. 562 n'a rien retiré. Aucune exécution n'a de rôle inconnu (0 ligne). Les 13 129 wallets sont tous candidats au classement. Dans le test de copie, un leader devenu inactif ne produit simplement pas d'événement, ce qui est réaliste : pas de biais du survivant sur le P&L de copie. La seule sélection sur la survie concerne Q3 : le Spearman (l. 553) et le P&L à h+1 (l. 581) ne portent que sur les wallets actifs les deux heures. C'est en partie signalé (« s'ils sont actifs », l. 186), mais le résumé (l. 12) présente « +69 $ » et « positif dans 50 % des cas » sans cette condition.

*Effet sur la conclusion :* Effet faible sur les statistiques de persistance : en comptant les 14,5 % d'inactifs avec un P&L nul, la moyenne à h+1 tombe de +69 $ à ≈ +59 $. Aucun effet sur le P&L de copie. Ne change ni le verdict de non-persistance ni celui de copie.

**3. données futures dans un indicateur : partiel**

* `scripts/polymarket_top_wallets.py:873` : `tr_, te = bl != blk, bl == blk`
* `scripts/polymarket_top_wallets.py:818` : `blocks = np.minimum(((mk["S"] - t_all0) * N_CV_BLOCKS) // (mk["S"].max() - t_all0 + 1), N_CV_BLOCKS - 1).to_numpy()`
* `scripts/polymarket_top_wallets.py:868` : `ff = np.arcsinh(f / 100.0)`
* `scripts/polymarket_top_wallets.py:821` : `sel = a["taker"] & (t_rel <= c) & (t_rel > c - PRICE_LOOKBACK_S)`

J'ai relu tout le chemin de copie (Q4) : build_tape, simulate_fills et q4_copy ne contiennent ni transformation centrée, ni bfill, ni normalisation sur l'échantillon. La bande est triée par (jeton, seconde, seq) et lue vers l'avant ; le classement est un cumul passé. Q5 : le prix est un VWAP causal sur (S+c−10, S+c] (l. 821), avec repli sur le dernier trade ≤ S+c. Le seul point non causal est la validation croisée « leave-one-block-out » sur 6 blocs (l. 873) : les logistiques sont ajustées sur des blocs futurs. Ces modèles sont minuscules (2 à 3 coefficients sur environ 1 000 fenêtres) et servent aux deux bras comparés.

*Effet sur la conclusion :* Fuite négligeable qui profite aux deux modèles. Le ΔBrier +0,0001 (IC −0,0003 ; +0,0005) resterait nul ou deviendrait légèrement plus défavorable au flux en walk-forward. Ne change pas « le flux n'ajoute rien au prix ». Sans objet pour le P&L de copie.

**4. frais et glissement : partiel**

* `scripts/polymarket_top_wallets.py:713` : `pnl_s = gross - fee_per_share(p)`
* `scripts/polymarket_top_wallets.py:97` : `return FEE_RATE * p * (1.0 - p)`
* `src/tradebot/polymarket_wallets.py:47` : `probablement des frais d'intégrateur (« builder ») : ils ne sont pas modélisés ici.`
* `reports/polymarket/top_wallets/README.md:263` : `les ajouter ne ferait qu'aggraver la perte de la copie.`

Les frais preneur 0,07·p(1−p) sont appliqués à chaque copie (l. 713), soit ≈ 1,23 c/part dans la cellule principale. Le spread est inclus implicitement, puisque le prix vient de vrais achats preneur (au prix de l'ask). L'estimateur `first` reprend le VWAP d'un tiers qui a pu balayer plusieurs niveaux (biais pessimiste ≈ 0,05 c, cf. `best` −1,33 contre −1,38 c). Je nuance l'auditeur sur les frais d'intégrateur. (i) Ils sont facturés par une interface tierce (« builder ») : un robot qui passe ses ordres directement sur le CLOB ne les paie pas. Pour le bot visé, c'est donc plutôt sans objet qu'un biais optimiste. (ii) S'ils s'appliquaient, ce serait un montant fixe de 0,01 à 0,09 $ par ordre : exprimé en c/part pour une copie d'1 part, il vaudrait bien plus que 0,6 c (2 à 18 c sur les ≈ 20 % d'ordres concernés), et presque rien pour une grosse copie. L'unité « c/part » n'a donc pas de sens pour ce coût. L'impact de marché est nul pour 1 part ; la profondeur n'est pas modélisée pour de grosses tailles.

*Effet sur la conclusion :* Biais pessimiste ≈ 0,05 c/part (`first`). Frais d'intégrateur : sans objet pour une API directe, sinon ils aggravent la perte. Dans tous les cas, corriger ne change pas le verdict négatif.

**5. prix jamais disponible : partiel**

* `scripts/polymarket_top_wallets.py:664` : `ok = (i < n) & (tape["tok"][ic] == tok) & (tape["ts"][ic] <= ts + d + MAX_WAIT_S)`
* `reports/polymarket/top_wallets/README.md:198` : `Si aucun achat preneur n'a lieu dans les 10 s, l'événement est écarté et compté : 6,5 % des événements à d = 0, 7,2 % à 3 s, 10 % à 10 s et 26 % à 60 s, surtout en fin de fenêtre.`
* `reports/polymarket/top_wallets/README.md:194` : `Pour d = 0, on prend le premier strictement postérieur, dans l'ordre on-chain, aux exécutions du leader.`
* `reports/polymarket/top_wallets/README.md:261` : `La profondeur, la file d'attente et l'impact de notre propre ordre ne sont pas modélisés`

Constat confirmé : faute de carnet, le prix est celui du prochain achat preneur d'un tiers, jusqu'à 10 s après τ+d. L'attente moyenne est de 0,9 s à d = 3 s et de 1,3 à 1,5 s aux autres délais, à cause de la cadence de 1,5 s des blocs ; la profondeur est supposée illimitée au niveau observé. Deux compléments mesurés sur le cache. (1) La sélection des événements écartés n'est PAS de signe inconnu, elle est optimiste. Sur les événements écartés à d = 3 s, le P&L propre des leaders vaut −1,40 c/part, contre +0,04 c sur les événements retenus : les écartés sont de mauvais achats. Avec 7,2 % d'écartés, cela représente ≈ 0,1 c/part. (2) L'auditeur écrit qu'une exécution au prix du leader donnerait « ≈ 0 c » : c'est inexact. Sans frais, won − prix du leader vaut +0,58 c/part sur l'ensemble des copies de la cellule principale, et +0,24 c sur les seuls achats maker des leaders (21 800 événements), plus la remise. Ce contrefactuel est cependant inatteignable : on ne peut pas être servi au prix du leader après lui, et un maker subit la sélection adverse. Le biais pessimiste du « preneur seulement » est donc borné par ≈ 0,6 c/part.

*Effet sur la conclusion :* Biais optimiste ≈ 0,1 c/part (événements écartés) et ≈ 0,1 c à d = 0. Biais pessimiste borné par ≈ 0,6 c/part si une copie en maker au prix du leader était possible, ce qui donnerait au mieux +0,2 à +0,6 c non significatif. Le verdict « la copie preneur perd » ne change pas. La phrase générale « pas de stratégie copy-trading » n'est établie que pour la copie en preneur.

**6. réglages sur tout l'échantillon : partiel**

* `scripts/polymarket_top_wallets.py:64` : `DELAYS = (0, 1, 2, 3, 5, 10, 20, 30, 60)`
* `scripts/polymarket_top_wallets.py:65` : `KS = (5, 10, 20)`
* `scripts/polymarket_top_wallets.py:66` : `LOOKBACKS = (1, 6)`
* `scripts/polymarket_top_wallets.py:67` : `MAX_WAIT_S = 10          # no taker BUY of the token within 10 s after tau + d -> discarded`

Décompte confirmé. La grille publiée compte 6 couples (K, L) × 9 délais × 3 estimateurs = 162 cellules, avec la correction de Bonferroni ; j'ai vérifié les chiffres dans copy_trading.csv : 54/54 négatives, 52/54 avec un IC entièrement négatif, 23 après Bonferroni. S'y ajoutent une dizaine de constantes fixées à la main sur les mêmes 48 h (MAX_WAIT_S, VWAP_SPAN_S, SAFETY_S, WARMUP_H, TOP_N, SLOT_S, fusion par seconde, classement en $ hors remises), plus les paramètres de Q5 (FLOW_CUTS, PRICE_LOOKBACK_S, N_CV_BLOCKS, l2, échelle arcsinh). Aucune n'a été optimisée. Le pré-enregistrement de la cellule principale est invérifiable : le premier commit qui contient le README (ca7f786, « version de travail ») contient déjà les résultats et la phrase.

*Effet sur la conclusion :* Aucun surajustement optimiste possible, puisque toutes les cellules sont négatives (−0,87 à −1,91 c). Pour un verdict négatif, le risque est plutôt une grille trop étroite (classement en $, L ≤ 6 h, achats seulement, preneur seulement). Vu un P&L brut ≈ 0 et ≈ 1,2 c de frais, il est improbable que cela inverse le verdict.

**7. hausse et baisse (régimes) : présent**

* `scripts/polymarket_top_wallets.py:60` : `DEFAULT_START = "2026-09-23T21:00Z"`
* `scripts/polymarket_top_wallets.py:61` : `DEFAULT_END = "2026-09-25T21:00Z"`
* `reports/polymarket/top_wallets/README.md:30` : `À partir de la 7e heure (24/09 03:00 UTC)`
* `reports/polymarket/top_wallets/README.md:260` : `dans un seul régime (TWAP 60 s, `crypto_fees_v2`)`

Recalculé depuis data/cache/BTCUSDT_1m.parquet. Sur les 48 h, BTC passe de 84 232,86 à 83 886,00 (−0,41 %) ; sur la période d'évaluation, −0,57 %. Le range va de 82 874,93 à 85 255, 24 heures sur 48 sont haussières, la volatilité réalisée vaut 38,4 % annualisés et la volatilité horaire va de 15,9 à 72,2 %. Sur les 30 jours précédents : +6,7 % (et non +7,7 % comme l'écrit l'auditeur) et 36,7 % de volatilité. La volatilité quotidienne de juin à septembre a une médiane de 36 % et monte jusqu'à 100 %. L'échantillon est donc un seul régime plat, à volatilité médiane, sans tendance ni choc. On trouve bien une certaine variété de volatilité horaire, mais aucune analyse n'est découpée par régime.

*Effet sur la conclusion :* La conclusion n'est établie que pour 42 h plates à volatilité médiane. Des leaders directionnels pourraient mieux faire lors de journées de tendance ou de choc, ce qui n'a pas été testé. Rien ne suggère une inversion, mais « le classement reflète surtout la chance » est une extrapolation.

**8. horloges et alignement : partiel**

* `scripts/polymarket_top_wallets.py:249` : `a["t_rel"] = a["ts"] - a["S"]`
* `reports/polymarket/top_wallets/README.md:47` : `Les deux trades entrent dans un bloc environ 2,2 s après leur appariement.`
* `reports/polymarket/top_wallets/README.md:264` : `L'instantané est pris à 22:00 UTC, alors que nos fenêtres finissent à 21:00.`

Toutes les séries sont en UTC. ts est la seconde de bloc Polygon, S vient de gamma, et `hour` = floor_h(S), aligné sur t_start qui tombe sur une heure pleine (DEFAULT_START). Le délai d se mesure de bloc à bloc, pour le leader comme pour le copieur : la convention est cohérente, et le décalage bloc/appariement de ≈ 2,2 s est commun aux deux. La résolution vient de gamma, pas d'un Chainlink recalculé. Seul désalignement réel : le recoupement avec le leaderboard compare un instantané pris à 22:00 à des fenêtres qui finissent à 21:00. Il est signalé et reste descriptif. Le profil de timing est en temps de bloc, décalé d'environ 2 s par rapport à l'instant d'appariement.

*Effet sur la conclusion :* Aucun effet sur le P&L de copie ni sur Q5. Le décalage d'1 h ne touche que la section descriptive du leaderboard. Ne change aucune conclusion.

**Autres défauts confirmés**

* **Affirmation fausse : « aucune variante ne fait mieux que le témoin »** (confirmé, gravité basse, pessimiste) — `reports/polymarket/top_wallets/README.md:14`. Vérifié dans copy_trading.csv (estimateur first), au même délai que le témoin : K5 L6 d60 fait −0,87 c contre −1,10 c ; K20 L1 d60 −0,91 ; K10 L1 d60 −1,04 ; K20 L1 d20 −1,03 contre −1,10 ; K20 L1 d30 −1,06 contre −1,07. Cinq cellules font donc mieux, de 0,01 à 0,23 c. Aucune n'est significative, mais la phrase est fausse telle qu'écrite.
* **La courbe « plate » selon le délai vient de la composition de l'échantillon ; sur un échantillon apparié, elle monte** (confirmé, gravité basse, neutre) — `reports/polymarket/top_wallets/README.md:235`. J'ai mesuré la courbe sur l'échantillon apparié : les 25 137 événements de la cellule top 10 / 1 h exécutables aux 9 délais. Le P&L passe de −2,07 c (d = 0) à −2,20 c (d = 3), puis à −1,32 c (d = 60). L'écart d60 − d3 vaut +0,88 c (IC bootstrap par créneau : +0,32 ; +1,40). La courbe n'est donc pas plate : attendre améliore la copie. Le témoin a exactement la même pente (−1,97 → −1,10 c, +0,87 c). C'est donc un effet général (réversion après un achat, ou composition intra-fenêtre), pas une information propre aux leaders. La conclusion « pas d'information chez les leaders » tient. En revanche, la phrase « courbe plate » et le niveau publié à d = 3 s (−1,38 c, contre −2,20 c sur l'échantillon apparié) dépendent de la composition.
* **Significativité dépendante de la pondération : pondéré par la taille, l'IC contient 0** (confirmé, gravité moyenne, incertain) — `reports/polymarket/top_wallets/README.md:238`. Vérifié : en pondérant par la taille (l. 717 du script), l'IC à 95 % contient 0 dans 32 des 54 cellules hors échantillon, dont toutes les cellules K = 10 / 1 h. Le résumé (« un gain supérieur à ~0,1 c par part est exclu », l. 237) ne vaut que pour la copie équipondérée d'1 part par événement. Les points estimés restent négatifs.
* **Spécification de copie étroite : achats maker copiés, ventes ignorées, classement en dollars** (confirmé, gravité moyenne, pessimiste) — `reports/polymarket/top_wallets/README.md:193`. Vérifié : leader_taker_share = 0,553 dans la cellule principale, donc ≈ 45 % de la taille copiée vient d'achats maker. Sous-groupe « leader preneur » à d = 3 s : prix 0,5620, taux de gain 0,5636, soit +0,16 c brut avant ≈ 1,2 c de frais. Les ventes ne sont jamais copiées. La conclusion générale va au-delà de ce qui a été testé, mais le brut ≈ 0 rend une inversion improbable.
* **P&L des leaders calculé en gardant la position jusqu'à la résolution** (confirmé, gravité basse, incertain) — `scripts/polymarket_top_wallets.py:650`. La colonne « P&L des leaders sur ces achats » est un contrefactuel où le leader garde la position jusqu'à la résolution. Ce n'est pas son P&L réalisé (qui figure à part dans copy_leaders_pnl_horaire.csv). La comparaison leader/copieur reste valable comme comparaison de prix d'entrée.
* **Contrôle de somme nulle quasi tautologique** (confirmé, gravité basse, neutre) — `reports/polymarket/top_wallets/README.md:10`. Avec des positions signées et des transactions équilibrées (taille preneur = somme des tailles maker), Σ pnl = −Σ frais est une identité comptable (zero_sum_check, l. 1203 du module). Elle vérifie seulement que les données sont complètes et équilibrées (0 rôle inconnu, vérifié), pas les prix ni les résolutions. La vraie validation est /v2/activity.
* **Variante de contrôle non reproductible** (confirmé, gravité basse, neutre) — `reports/polymarket/top_wallets/README.md:256`. Aucun code ni fichier du dépôt ne produit cette variante (« flux preneur seulement »). Elle est invérifiable, mais ne sert qu'à conforter un verdict déjà négatif.
* **Pré-enregistrement de la cellule principale invérifiable** (confirmé, gravité basse, neutre) — `reports/polymarket/top_wallets/README.md:32`. git log : le premier commit du README (ca7f786, « version de travail ») contient déjà les résultats et cette phrase. Il n'existe aucune trace antérieure. C'est sans conséquence, puisque les 54 cellules sont négatives.
* **Les événements écartés (aucun achat preneur dans les 10 s) sont les mauvais achats des leaders : sélection optimiste** (nouveau, gravité basse, optimiste) — `scripts/polymarket_top_wallets.py:664`. Mesuré sur le cache, cellule top 10 / 1 h à d = 3 s : sur les 2 864 événements écartés, le P&L propre des leaders vaut −1,40 c/part, contre +0,04 c sur les 37 150 retenus. Les écartés sont aussi plus souvent en dernière minute (11 % contre 8 %). Les exclure améliore donc le P&L de copie d'environ 7 % × 1,4 c ≈ 0,1 c/part, et davantage aux grands délais (26 % d'écartés à 60 s). Le README présente cette exclusion comme neutre (« écarté et compté »).
* **L'« avantage durable des makers » repose sur des wallets choisis après coup** (nouveau, gravité basse, optimiste) — `reports/polymarket/top_wallets/README.md:18`. Ce résultat positif s'appuie sur les wallets gagnants sur 48 h, choisis ex post (l. 89 : « Les wallets qui restent gagnants sur la durée sont des **makers** »), et sur le ρ ≈ 0,09. Aucun test hors échantillon d'une stratégie maker n'est fait ici. Sur les achats maker copiés, le gain des leaders n'est que de +0,23 c/part (IC ≈ −1,0 ; +1,4), avant sélection adverse. L'affirmation est plausible, mais elle n'est pas démontrée par ce backtest.
* **Résumé : P&L et part gagnante à h+1 présentés sans préciser « parmi les actifs »** (nouveau, gravité basse, incertain) — `reports/polymarket/top_wallets/README.md:12`. Ces chiffres ne portent que sur les top 10 encore actifs à h+1 (85,5 %, contre 72,6 % pour les autres). La comparaison 50 % contre 54 % compare deux populations filtrées différemment. En comptant les inactifs avec un P&L nul, la moyenne serait ≈ +59 $. La précision figure dans le tableau de la l. 186, pas dans le résumé.

## Polymarket : simulation maker sur les trades historiques (src/tradebot/polymarket_maker.py, scripts/polymarket_maker_backtest.py, scripts/polymarket_collect_taker_trades.py ; rapport reports/polymarket/maker/README.md). Contre-expertise.

**1. information future (look-ahead) : partiel**

* `src/tradebot/polymarket_maker.py:341` : `t0 = tp[i] + delay_s`
* `src/tradebot/polymarket_maker.py:342` : `if check_cross and not crossing[i] and would_cross(mt, side[i], price[i], t0, cross_lookback_s):`
* `src/tradebot/polymarket_maker.py:308` : `i0 = int(np.searchsorted(t, t0 - lookback_s, side="left"))`
* `src/tradebot/polymarket_maker.py:82` : `ONCHAIN_DELAY_S = 3             # visibilité de l'ordre (0,3 s) + appariement -> bloc (médiane 2,2 s)`

Toutes les citations de l'auditeur sont exactes. Le signal est causal : p_hgb_ind1s et tw_gap30 à S−30 s, p_pre = dernier point <= S−30 s, p_pre60 pour (b), spot (c) = bougie ouverte à tq−1 (index open_time vérifié dans data.py), TWAP60 à l'index S−60 = closes des bougies [S−60, S), utilisé dès S+30 s. J'ai reproduit la configuration (a) choisie (1 433 ordres, +0,170 c ; 1re moitié 1 102, +2,114 c) et le chiffre de l'auditeur : fenêtre de croisement terminée à t_place au lieu de t_place+3 s, 17 ordres réintégrés, −16,8 c chacun, P&L −0,029 c. Mais son interprétation est trop forte. Le filtre doit représenter l'état du carnet à l'ARRIVÉE de l'ordre (≈ t_place+0,3 s), que le trader voit en direct. Avec un retard bloc médian de 2,2 s, un bloc daté t_place+0 ou +1 correspond à un appariement vers t_place−2,2 à −0,2 s, donc AVANT la décision. Écarter ces ordres revient à un rejet « post-only », ce qui est légitime. Seul le bloc t_place+2 (appariement ≈ t_place−0,2 à +0,8 s) est en partie postérieur. Répartition des 17 ordres par premier bloc révélateur : +0 s : 7 ; +1 s : 6 ; +2 s : 4. Fenêtre terminée à t_place+2 s : +0,103 c (2e moitié). Fenêtre à t_place+1 s : +0,070 c. La part imputable à une information postérieure à l'arrivée est donc ≈ 0,07 c, l'écart total de 0,2 c étant une borne haute. Le signe n'est pas stable : en 1re moitié, les mêmes réintégrations font +2,11 → +2,14 c (fin à +0 s) et +2,28 c (fin à +1 s). Avec 17 ordres, c'est du bruit (écart-type ≈ 12 c sur leur moyenne). Symétriquement, la fenêtre d'exécution [t0+3, t1+3) ne suit pas la distribution du retard (auditeur exact), mais c'est un bruit de bord de ±1 s sur des délais médians de 16 s. Le même filtre s'applique à (c), qui écarte 102 139 ordres croisants ; non mesuré, mais tout biais y irait dans le sens optimiste, alors que (c) est déjà perdante.

*Effet sur la conclusion :* Biais optimiste réel ≈ 0,07 c par part placée sur (a) ; 0,2 c au plus si l'on compte toute la fenêtre [t_place, t_place+3 s), et avec un signe instable d'une moitié à l'autre. Le seul P&L positif publié (+0,17 c) devient ≈ +0,10 c, voire ≈ 0. Le verdict « Non, pas de façon démontrable » ne change pas (IC −1,30 ; +1,67).

**2. survivants : absent**

* `reports/polymarket/maker/README.md:3` : `8 064 marchés résolus sur 8 064 créneaux attendus`
* `scripts/polymarket_maker_backtest.py:133` : `M = M[np.isfinite(M["y"])].reset_index(drop=True)`
* `src/tradebot/polymarket_wallets.py:559` : `unresolved = [m.slug for m in ms if m.resolved_up is None]`
* `src/tradebot/polymarket_maker.py:254` : `Les marchés absents du cache reçoivent un :class:`MarketTrades` vide."""`

J'ai cherché un contre-exemple sans en trouver. Les 8 064 créneaux attendus (21 jours × 384) sont tous résolus : les filtres des lignes 133 et 559 n'excluent rien. Les marchés sans trades restent dans l'échantillon. marches.csv confirme n_trades = 0 pour 12 marchés, tous consécutifs le 19/09 de 17:45 à 19:55 UTC : panne de la plateforme ou trou de collecte, ils sont comptés « placés, non exécutés ». Six marchés n'ont ni p_hgb_ind1s, ni p_pre, ni p_pre60 (vérifié) : pas d'ordre (a) pour eux, et (b) y est posé sans la règle du milieu. twap60_S et quintile_train ne manquent pour aucun marché. Le collecteur pagine complètement par curseur et recoupe un échantillon de marchés par requête individuelle : pas de troncature silencieuse des trades. Aucune sélection de wallets n'intervient. ETH et SOL sont exclus par choix de périmètre annoncé, et non en fonction des résultats.

*Effet sur la conclusion :* Aucun biais du survivant mesurable. Les 12 marchés vides et les 6 marchés sans prédiction pèsent moins de 0,01 c. Le verdict ne change pas.

**3. données futures dans un indicateur : partiel**

* `src/tradebot/polymarket_maker.py:388` : `close = pd.Series(close).ffill().bfill().to_numpy()`
* `src/tradebot/polymarket_maker.py:390` : `if ok.sum() >= 86400 * 0.95:`
* `src/tradebot/polymarket_maker.py:405` : `var = pd.Series(r2).ewm(halflife=halflife_s, min_periods=10).mean().to_numpy()`
* `scripts/polymarket_maker_backtest.py:249` : `best = tr.sort_values("log_loss").iloc[0]["variant"] if len(tr) else "ewma1s_h60"`

Je confirme l'analyse de l'auditeur. L'EWMA est causale sur une série triée et continue (22 jours 03/09–24/09 en cache, sans trou). La demi-vie est choisie sur la 1re moitié ; les quintiles σ_TimesFM viennent de l'ECDF d'apprentissage 14/08–03/09 (timesfm_amplitude.py l. 1186-1187). TimesFM « prévu à S » a pour contexte la barre close à S−1 min. La calibration isotonique de hgb_ind1s date d'avant le 14/08. Il n'y a ni merge_asof, ni fenêtre centrée. Le seul transfert du futur vers le passé est le bfill de la ligne 388. Il ne remplit que les secondes manquantes au tout début d'un jour UTC, et les jours mis en cache sont complets à au moins 95 % : effet nul en pratique. La table Q_ahead est une statistique mesurée le 26/09, APRÈS la période, mais sur le carnet et non sur les issues. La sensibilité publiée borne son effet : de 0 à 5 000 parts, (a) passe de +0,50 à +0,09 c.

*Effet sur la conclusion :* Effet négligeable (< 0,01 c pour le bfill). Q_ahead peut valoir ±0,4 c sur (a), sans pouvoir rendre le résultat significatif. Le verdict ne change pas.

**4. frais et glissement : partiel** (le contradicteur corrige l'auditeur)

* `src/tradebot/polymarket_maker.py:595` : `pnl_share = np.where(ex, win - price, np.nan)`
* `src/tradebot/polymarket_maker.py:598` : `out["pnl_per_placed_share"] = np.where(placed, np.where(ex, pnl_share, 0.0), np.nan)`
* `src/tradebot/polymarket_maker.py:604` : `rebate = np.where(ex, rebate_rate * fee, np.nan)`
* `reports/polymarket/maker/README.md:312` : `une partie des « traversées » sont sans doute des ordres qui auraient été preneurs (exécution immédiate avec frais).`

Je ne suis pas d'accord avec ABSENT. L'absence de frais est juste pour un vrai fill maker, ce que /v2/activity vérifie. Mais toute exécution est comptée sans frais (ligne 595, chemin score_frame utilisé pour (a) et (b) ; ligne 566 pour (c)), y compris celles qui, faute de carnet, auraient en réalité été des ordres PRENEURS : croisants non détectés, exécutés au prix de l'ask avec des frais de 0,07·p(1−p). Le README l'admet (l. 312) sans le corriger. Mesure sur (a) choisie, 2e moitié : 48 des 444 exécutions ont lieu dans les blocs t_place+3 ou +4 s, c'est-à-dire un appariement dans la seconde qui suit l'arrivée, un profil de croisant. Leur appliquer les frais preneur (1,75 c à 0,49) coûte ≈ 0,06 c par part placée, et autant en 1re moitié (39 exécutions). C'est une borne haute, car tous ces ordres ne sont pas croisants. En sens inverse, la remise maker est exclue de tous les P&L. Je confirme l'estimation de l'auditeur : 0,35 c × 31 % ≈ +0,11 c pour (a), ≈ +0,18 c pour (b), ≈ +0,08 c pour (c). Il n'y a pas de glissement de prix, puisque l'exécution se fait à L ; l'impact et le partiel relèvent du point 5.

*Effet sur la conclusion :* Deux écarts de sens opposé : frais omis sur les faux makers, optimiste, ≤ 0,06 c ; remise omise, pessimiste, ≈ 0,1 à 0,2 c. Solde net pessimiste de ≈ 0,05 c pour (a). (a) reste non significatif ; (b), à −1,25 c, et (c), à −0,78 c, restent perdants. Le verdict ne change pas.

**5. prix jamais disponible : partiel**

* `src/tradebot/polymarket_maker.py:290` : `beyond = cp < level - PRICE_EPS`
* `src/tradebot/polymarket_maker.py:286` : `consuming = cp <= level + PRICE_EPS`
* `src/tradebot/polymarket_maker.py:176` : `price = df["price"].to_numpy(dtype="float64")[order]`
* `reports/polymarket/maker/README.md:309` : `* **Tout ou rien** : pas d'exécution partielle ; un ordre de 100 parts est soit entièrement servi, soit pas du tout (les vrais ordres sont souvent servis par morceaux).`

Citations exactes. Personne ne s'exécute au milieu du carnet ni à un prix jamais traité : il faut un trade preneur réel. (1) Tout ou rien : j'ai reproduit l'effet sur (a) choisie. En 2e moitié : +0,17 c en tout ou rien, +0,14 c en partiel au moment de la traversée, +0,19 c en partiel jusqu'à l'annulation. Les parts exécutées passent de 31 % à 18–29 %, et 72 % des traversées consomment moins de 100 parts. En 1re moitié : +2,11 c devient +0,92 / +1,71 c, conforme aux chiffres de l'auditeur. (2) La file (ii) sous-estime Q_ahead, qui ne mesure qu'un côté ; l'écart est borné par la règle (i) seule : +0,09 contre +0,17 c. (3) Défaut que l'auditeur n'a pas vu : le prix d'un trade est le VWAP du preneur. La règle (i) ne donne donc pas de faux positifs, mais elle a des FAUX NÉGATIFS : un preneur qui balaie 0,50 puis 0,48 a un VWAP ≥ 0,49 et n'est pas compté comme traversée, alors que notre bid à 0,49 aurait été servi. Mesure sur les 432 marchés des caches « _all » du 23 au 24/09 (prix maker individuels), où 13,5 % des transactions ont un pire prix différent du VWAP. Avec le pire prix, pour (a) choisie (123 ordres), le taux d'exécution passe de 28,5 % à 30,9 % et le P&L de −0,12 à +0,31 c : trois ordres, donc du bruit, de sens incertain. (4) Pas d'impact de nos ordres ni de réaction des autres participants ; ces effets ne sont pas mesurables sans carnet.

*Effet sur la conclusion :* Biais résiduels de ±0,1 à 0,2 c par part placée sur (a) en 2e moitié, dans des sens opposés : file optimiste ≤ 0,08 c, VWAP qui fait manquer des exécutions (sens incertain). Le tout-ou-rien gonfle la 1re moitié de 0,4 à 1,2 c et fausse donc la SÉLECTION (voir 6), sans changer le test. Les pertes de (b) et (c) dépassent tous ces biais. Le verdict ne change pas.

**6. réglages sur tout l'échantillon : partiel**

* `scripts/polymarket_maker_backtest.py:51` : `MODELS = {"hgb_m0": ("p_hgb_ind1s", 0.0), "hgb_m2": ("p_hgb_ind1s", 0.02), "hgb_m5": ("p_hgb_ind1s", 0.05),`
* `scripts/polymarket_maker_backtest.py:430` : `best = tr.sort_values("pnl_per_placed_c", ascending=False).iloc[0]`
* `scripts/polymarket_maker_backtest.py:50` : `HALFLIFE_GRID = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0)`
* `src/tradebot/polymarket_maker.py:104` : `# File d'attente mesurée sur le carnet réel (reports/polymarket/maker_live/README.md § 3, 18 marchés`

Le décompte de l'auditeur (≈ 30 paramètres, 64 + 4 + 8 choix faits sur la 1re moitié, malédiction du vainqueur de +2,11 → +0,17 c) est juste. Je réfute en partie un argument : hgb_ind1s n'a pas été « retenu après lecture des résultats » du 04–24/09. Dans modeles_vs_marche, il est « choisi sans regarder le test », par la meilleure log-loss sur le 14/08–03/09 (README l. 11). Le risque résiduel est plus mince : l'auteur du maker connaissait les résultats de test de modeles_vs_marche (hgb_ind1s à +2,20 c, gap_m30 à 0 c) et a retenu hgb_ind1s et gap_m30, pas logit_tw, le modèle choisi par la règle de P&L. C'est un biais de sélection possible, optimiste, et non démontré. Q_ahead (26/09, après la période) est un paramètre de microstructure, pas un ajustement sur le P&L. Point supplémentaire : la règle « meilleur P&L sur la 1re moitié » classe les 64 cellules sur une mesure gonflée par le tout-ou-rien (+2,11 contre +0,92 c en exécution partielle), ce qui ajoute du bruit à la sélection.

*Effet sur la conclusion :* Les biais de sélection vont dans le sens optimiste, et le résultat testé est quand même nul. Les corriger ne pourrait que baisser (a). Le verdict négatif est renforcé, pas remis en cause.

**7. hausse et baisse (régimes) : partiel**

* `reports/polymarket/maker/README.md:23` : `2e moitié = du 14/09 au 24/09`
* `scripts/polymarket_maker_backtest.py:1029` : `ap.add_argument("--mid", default="2026-09-14", help="début de la 2e moitié (test)")`
* `reports/polymarket/maker/README.md:315` : `Une seule période de 21 jours, un seul actif.`

Chiffres vérifiés sur data/cache/pm_maker/binance_1s. Apprentissage (04–13/09) : 81 265 → 76 842, soit −5,44 % simple (−5,6 % en log), écart-type 5 min de 9,6 pb (≈ 31 % annualisé). Test (14–24/09) : 76 842 → 84 410, soit +9,85 % simple (+9,4 % en log), min 74 968, max 87 395, écart-type 5 min de 12,4 pb (≈ 40 %). Jours du test : −3,25 % (15/09), +5,85 % (18/09), +6,70 % (21/09), −2,10 % (23/09). Le test mêle donc des jours baissiers et deux chocs haussiers ; sa volatilité est plus haute de 30 % et sa tendance opposée à celle de l'apprentissage. La période compte 21 jours, un seul actif, et aucun épisode de crise. Le point faible est la représentativité, pas l'absence de régimes.

*Effet sur la conclusion :* Le verdict « pas de façon démontrable » est juste pour cette fenêtre, mais ne se généralise pas. Avec un IC de ±1,5 c pour (a), un avantage maker d'environ +1 c dans un autre régime n'est pas exclu : risque pessimiste. Les pertes de (b) et (c), qui persistent dans les deux moitiés, sont plus robustes.

**8. horloges et alignement : partiel**

* `scripts/polymarket_maker_backtest.py:195` : `# information close à tq : la bougie 1 s ouverte à tq − 1 (close à tq) ; la bougie ouverte à tq`
* `scripts/polymarket_maker_backtest.py:160` : `M.loc[m, "twap60_S"] = pb.bar_lookup(agg, M.loc[m, "start_ts"].to_numpy(dtype="int64") - 60, "twap60")`
* `src/tradebot/polymarket_wallets.py:16` : `* ``timestamp`` = **horodatage du bloc Polygon, à la seconde** (vérifié : identique au`
* `reports/polymarket/maker/README.md:32` : `l'ordre ne voit que les trades dont le bloc est dans [t0 + 3 s, t1 + 3 s).`

Toutes les séries sont en secondes Unix UTC. Les bougies Binance sont indexées sur leur ouverture (open_time, vérifié dans data.py) et le code prend bien tq−1. TWAP60 à l'index S−60 est connu à S. Les trades portent l'horodatage du bloc à la seconde, avec un décalage fixe de 3 s (point 1). Je corrige un argument de l'auditeur : l'écart-type de 3 pb de l'écart de NIVEAU Binance/Chainlink d'un marché à l'autre ne bruite pas d, puisque spot et K sont tous deux Binance et que le niveau s'annule. Seule la VARIATION de cet écart entre S et E compte, et le diagnostic la dit négligeable (README l. 22). L'avance de Binance d'environ 4 s sur Chainlink n'est pas modélisée (sens incertain). Le milieu prices-history est en retard (ancienneté médiane 17 s) : c'est une information périmée, pas une fuite. Q_ahead vient d'une autre horloge (WebSocket local) mais sert de constante.

*Effet sur la conclusion :* Pour (a) et (b), rien de plus qu'au point 1. Pour (c), le biais pessimiste lié à Chainlink est plus faible que ne l'estimait l'auditeur : il ne peut pas combler −0,78 c (IC −0,98 ; −0,59). Le verdict ne change pas.

**Autres défauts confirmés**

* **IC de Bonferroni tirés des quantiles extrêmes d'un bootstrap à 2 000 tirages** (confirmé, gravité basse, neutre) — `src/tradebot/polymarket_backtest.py:630`. Mécanisme confirmé (α = 0,05/71, alpha_adj ligne 1089 du script) : le quantile 0,00035 sur B = 2 000 interpole entre les deux plus petits tirages. Effet nul sur les conclusions, toutefois. J'ai recalculé avec une approximation normale (SE = largeur de l'IC 95 %/3,92, z = 3,39) : parmi les 71 cellules, exactement les mêmes 7 sont perdantes après correction (les 4 de (b) et les 3 de (c) « toutes »), et aucune n'est positive, ni en brut ni après correction.
* **Remise maker calculée mais exclue de tous les P&L publiés** (confirmé, gravité basse, pessimiste) — `src/tradebot/polymarket_maker.py:598`. Confirmé : ≈ +0,11 c par part placée pour (a), +0,18 c pour (b), +0,08 c pour (c). Ce biais est en partie compensé par les frais preneur omis sur les exécutions qui auraient en fait été des ordres croisants (≤ 0,06 c, nouveau point). Aucune conclusion ne change.
* **Tout-ou-rien : la performance de la 1re moitié, qui sert à choisir la configuration, est gonflée** (confirmé, gravité basse, optimiste) — `reports/polymarket/maker/README.md:63`. Chiffres reproduits : en 1re moitié, +2,11 c devient +0,92 c en exécution partielle à la traversée et +1,71 c en partiel jusqu'à l'annulation ; en 2e moitié, +0,17 c devient +0,14 / +0,19 c. 70 à 72 % des traversées consomment moins de 100 parts. La ligne du README contient des espaces fines insécables (U+202F) dans « 1 102 », reprises ici telles quelles.
* **Filtre des ordres croisants alimenté par des trades postérieurs à la pose** (confirmé, gravité basse, optimiste) — `src/tradebot/polymarket_maker.py:342`. Le mécanisme existe, mais son ampleur est surestimée : voir point 1. Sur les 17 ordres, 13 ont leur trade révélateur dans les blocs t_place+0 ou +1 s. Ils ont été appariés avant la décision, dans un état du carnet visible en direct, et leur exclusion équivaut à un rejet post-only légitime. La part imputable à des trades postérieurs à l'arrivée vaut ≈ 0,07 c (+0,17 → +0,10 c), et le signe s'inverse en 1re moitié.
* **Marchés sans aucun trade dans le cache comptés comme non-exécutions** (confirmé, gravité basse, neutre) — `src/tradebot/polymarket_maker.py:264`. Confirmé : 12 marchés avec n_trades = 0, tous consécutifs le 19/09 entre 17:45 et 19:55 UTC. C'est une interruption de la plateforme ou de la collecte, pas un hasard. Effet de dilution inférieur à 0,01 c.
* **Règle de traversée appliquée au VWAP du preneur : exécutions manquées quand un preneur balaie le niveau** (nouveau, gravité basse, incertain) — `src/tradebot/polymarket_maker.py:176`. cp repose sur le prix moyen du preneur (README l. 313 : « le prix d'un trade est le prix moyen du preneur. »). Un preneur qui vend à 0,50 puis à 0,48 a un VWAP ≥ 0,49 : la règle (i) (ligne 290, beyond = cp < level) ne voit pas qu'il est passé sous L, alors qu'un bid à 0,49 aurait été servi. Mesure sur les caches « _all » du 23 au 24/09 (432 marchés, prix maker individuels) : 13,5 % des transactions ont un pire prix différent du VWAP. Pour (a) choisie (123 ordres), passer du VWAP au pire prix fait monter le taux d'exécution de 28,5 % à 30,9 % et le P&L de −0,12 à +0,31 c par part placée. Seuls 3 ordres changent : le sens est donc incertain. L'affirmation « pas de faux positifs » tient, mais le modèle fait des faux négatifs.
* **Frais preneur omis sur les exécutions qui auraient été des ordres croisants non détectés** (nouveau, gravité basse, optimiste) — `src/tradebot/polymarket_maker.py:595`. Toute exécution est comptée sans frais. Or le README (l. 312) admet qu'« une partie des « traversées » sont sans doute des ordres qui auraient été preneurs (exécution immédiate avec frais). » Mesure sur (a) choisie : 48 des 444 exécutions en 2e moitié, et 39 des 330 en 1re, se font dans les blocs t_place+3 ou +4 s, soit un appariement dans la seconde qui suit l'arrivée de l'ordre. Leur appliquer les frais preneur (0,07 × 0,49 × 0,51 ≈ 1,75 c) retire au plus ≈ 0,06 c par part placée. Cela compense environ la moitié de la remise omise.

## Polymarket : formule exacte P(Up) contre le marché, P&L preneur selon le délai (src/tradebot/polymarket_formula*.py, scripts/polymarket_formula_backtest.py, src/tradebot/polymarket_signal.py, reports/polymarket/formule/README.md) — contre-audit

**1. information future (look-ahead) : partiel**

* `src/tradebot/polymarket_formula_backtest.py:136` : `"""Entrées de la formule à l'instant ``t`` (vectorisé), à partir des seules données ``<= t``.`
* `src/tradebot/polymarket_formula_backtest.py:150` : `start_avg = np.where(ph2, prices.window_mean(S - L - c, np.where(ph2, t, S - L - c + 1)), nan)`
* `src/tradebot/polymarket_formula_backtest.py:297` : `j = np.searchsorted(tr.ts, t, side="right") - 1`
* `src/tradebot/polymarket_formula_backtest.py:317` : `i = np.searchsorted(ts, t_from, side="left")`

Toutes les citations de l'auditeur sont exactes. Le signal est bien causal : p_t, Ā, K et B ne lisent que des closes Binance d'instant ≤ t, y compris avec le décalage c = 4 (fenêtres (S − 64, t], (S − 64, S − 4], (E − 64, t]). σ EWMA et Parkinson sont causaux, et σ TimesFM est connu à S − 60 (origine S − 120, barre close à S − 60), donc avant S − 45. Le défaut porte sur le passage du signal à l'ordre, et je l'ai vérifié sur last_run.pkl. (a) À δ = 0, 62,8 % des exécutions de la variante corrigée ont lieu dans le bloc de la seconde t. Les deux bornes sont inclusives (searchsorted 'right' pour le VWAP [t − 3, t], 'left' pour la fenêtre [t, t + 2]) : le trade payé peut donc être l'un de ceux qui ont servi à la décision. (b) À δ = 2 s, 44,8 % des exécutions tombent dans le bloc t + 2. Avec un retard de bloc de 2,2 s en médiane (quartiles 1,78–2,61 s, mesurés sur 41 541 trades), l'appariement a lieu vers t − 0,2 s, entre t − 0,6 et t + 0,2 s. δ = 2 s représente donc une latence totale nulle, et même légèrement négative. Aucun délai n'est compté pour la publication de la bougie 1 s ni pour l'aller-retour de l'ordre. (c) Nuance sur le point (c) de l'auditeur : λ, au carnet réel, est une latence TOTALE (du trade dans le moteur Binance à l'appariement sur le serveur Polymarket). Le « 300 ms » du rapport est donc un budget qui inclut déjà le réseau ; ce n'est pas un budget « après réseau ». En revanche, λ < ≈ 100 ms est physiquement inaccessible (Binance Tokyo → CLOB, sans doute hébergé en Europe). Le +7,7 c à λ = 0 mis en avant ligne 15 n'est donc pas atteignable.

*Effet sur la conclusion :* Biais optimiste sur les seuls résultats positifs. +5,0 c (δ = 0) et +3,1 c (δ = 1 s) sont inaccessibles, ce que l'auteur reconnaît déjà. À δ = 2 s, le +0,4 c est optimiste d'environ 0,5 à 1 c : la valeur à δ = 3 s est −0,5 c. Au carnet réel, la partie faisable de la courbe commence vers 100–150 ms : +6,5 à +4,3 c contre le milieu, +4,8 à +2,6 c contre l'issue. Corriger ne change pas le verdict « rien à latence ≥ 1 s ». La fenêtre « 300 ms » reste une latence totale correcte, mais le chiffre de tête +7,7 c devrait être retiré.

**2. survivants : partiel**

* `scripts/polymarket_formula_backtest.py:109` : `M = M[np.isfinite(M["y"])].reset_index(drop=True)`
* `scripts/polymarket_formula_backtest.py:363` : `G["p_mkt"] = np.where(G["age"] <= MAX_AGE_S, G["last"], np.nan)`
* `scripts/polymarket_formula_backtest.py:658` : `if not np.isfinite(last) or last < E - 15:`
* `reports/polymarket/formule/README.md:54` : `* **Carnet réel** : fichiers du collecteur (`data/cache/polymarket/live/`, carnet reconstruit par `tradebot.polymarket_book`) : 82 marchés enregistrés, **72 exploitables** ; écartés : fenêtre en cours`

J'ai vérifié les chiffres sur last_run.pkl. Il y a 8 064 marchés (3 840 d'apprentissage, 4 224 de test), soit exactement 21 jours × (288 + 96) : aucun marché n'a été retiré faute d'issue. 8 052 marchés ont des trades (7 620 depuis les fichiers *_taker, 432 depuis les fichiers *_all). Sur le test, 40 marchés n'ont aucun prix de marché à aucun instant ; ils sont tous du 19/09 (32 créneaux), ce qui correspond à un trou de collecte. Ils restent dans la calibration mais sortent de la comparaison au marché et du P&L. Au carnet réel, 5 marchés sont écartés car leur fenêtre était en cours (sans objet), 3 pour carnet incomplet et 2 parce que l'issue n'était pas encore publiée. Ces derniers sont les marchés les plus récents : c'est un retard de publication, pas une sélection sur les marchés contestés. Aucun de ces trous n'est lié à l'issue.

*Effet sur la conclusion :* Négligeable : environ 1 % des marchés de test, moins de 0,1 c/part. Ne change pas le verdict.

**3. données futures dans un indicateur : partiel** (le contradicteur corrige l'auditeur)

* `src/tradebot/polymarket_maker.py:388` : `close = pd.Series(close).ffill().bfill().to_numpy()`
* `src/tradebot/polymarket_formula_backtest.py:233` : `o = (t // bar_s) * bar_s - bar_s`
* `src/tradebot/polymarket_formula_backtest.py:245` : `num = lfilter([1.0], [1.0, -lam], np.where(ok, r2, 0.0))`
* `scripts/polymarket_formula_backtest.py:307` : `m = train & np.isfinite(p)`

Contre-exemple à l'ABSENT de l'auditeur. Les closes Binance 1 s de l'historique (BN1S_DIR = data/cache/pm_maker/binance_1s) sont produits par fetch_binance_1s_closes, qui applique jour par jour un ffill() puis un bfill(). Si les premières secondes d'un jour UTC n'ont pas de bougie, elles reçoivent le close d'une seconde FUTURE. L'auditeur affirmait « pas de bfill » ; c'est faux pour le chemin de données. L'ampleur est minime : au plus quelques secondes à 00:00 UTC, pour 21 jours, sur BTCUSDT qui trade presque chaque seconde. Seuls peuvent être touchés K et Ā des marchés ouverts à 00:00. Le reste est bien protégé. Parkinson est lu sur la dernière bougie 1 m close (ouverture ≤ t − 60). L'EWMA est un filtre récursif causal. La demi-vie, k, le choix du σ, le décalage c = 4, σ_b = 0,40 pb et l'empilement sont tous ajustés sur la 1re moitié (masque train). Les déciles, pentes et queues servent seulement à l'évaluation.

*Effet sur la conclusion :* Look-ahead de 1 à 2 s au plus, sur quelques secondes par jour : effet non mesurable, bien inférieur à 0,01 c/part. Ne change pas le verdict. Le statut passe tout de même d'ABSENT à PARTIAL, car un bfill existe bien dans le chemin de données.

**4. frais et glissement : partiel**

* `src/tradebot/polymarket_formula_backtest.py:359` : `out = win - price - taker_fee(price, fee_rate)`
* `src/tradebot/polymarket_formula_backtest.py:334` : `return fee_rate * p * (1.0 - p)`
* `src/tradebot/polymarket_formula_backtest.py:63` : `HALF_SPREAD = 0.005`
* `scripts/polymarket_formula_backtest.py:753` : `best, sz, avg, got = fb.fill_price(st, side, q)`

Les frais sont corrects. reports/polymarket/markets.csv donne crypto_fees_v2, taux 0,07, exposant 1 pour les 2 688 marchés BTC présents, et la formule rate·p·(1 − p) reproduit la table officielle citée dans polymarket.taker_fee (1,75 $ pour 100 parts à 0,50). Les frais sont appliqués à chaque achat ; il n'y en a pas au rachat. Dans l'historique, la profondeur et l'impact ne sont pas modélisés (quantité illimitée au prix d'un trade obtenu par un autre). Au carnet réel, le carnet est parcouru pour 10 et 100 parts. Une correction à l'auditeur : il écrit que le demi-écart de 0,5 c « contredit le carnet réel qui montre 1 c ». Or 1 c est l'ÉCART bid–ask médian, soit un demi-écart de 0,5 c, cohérent avec HALF_SPREAD. L'hypothèse n'est optimiste qu'à S − 2 et S (écart de 2 et 3,5 c). Preneur rapide : le markout contre le milieu ne compte aucun coût de sortie. En sortie preneur (demi-écart + frais ≈ 0,5 + 1,7 c), on obtient ≈ +5,5 c à 0 ms, ≈ +2 c à 200 ms et ≈ −0,8 c à 300 ms. En sortie teneur de marché (sans frais), l'écart serait plus faible. Le gain contre l'issue, qui ne demande aucune sortie, est significatif jusqu'à 100 ms sans filtre (+4,8 c, IC +0,4 ; +9,9) et jusqu'à 200 ms avec le filtre « espérance > 0 » (+9,7 c, IC +3,9 ; +15,0). La phrase de l'auditeur « significatif seulement jusqu'à 100 ms » ne vaut donc que sans filtre. Les frais calculés sur le prix moyen (f concave) introduisent un biais pessimiste inférieur à 0,01 c.

*Effet sur la conclusion :* Historique : le biais de profondeur est optimiste mais ne touche qu'un verdict déjà négatif à δ ≥ 2 s. Preneur rapide : le « plusieurs cents par part jusqu'à 200 ms » (ligne 17) est surestimé d'≈ 2 c en cas de sortie preneur. Il reste défendable contre l'issue avec le filtre, mais sur une matinée et 11 créneaux. Le verdict global ne change pas.

**5. prix jamais disponible : présent**

* `reports/polymarket/formule/README.md:74` : `Prix payé : **premier achat preneur du même jeton dont le bloc est dans [t + δ, t + δ + 2 s]**, δ ∈ {0 ; 1 ; 2 ; 3 ; 5 ; 10} s ; sans trade dans la fenêtre, pas d'exécution (comptée à part).`
* `reports/polymarket/formule/README.md:76` : `Le « premier trade de bloc ≥ t + δ » a donc été apparié vers t + δ − 2,2 s : **le P&L historique à un δ donné est optimiste**`
* `reports/polymarket/formule/README.md:329` : `on suppose passer avant les autres preneurs rapides.`
* `scripts/polymarket_formula_backtest.py:542` : `px = np.where(side == 1, G[f"x_up_{delta:g}"].to_numpy(), np.where(side == -1, G[f"x_dn_{delta:g}"].to_numpy(), np.nan))`

Je confirme que tous les résultats positifs reposent sur des prix que le robot n'aurait pas pu obtenir : prix appariés avant la décision à δ = 0–1 s, premier dans la file et latence nulle au carnet réel. J'ai recalculé le biais pessimiste de l'ordre au marché (IC par bootstrap groupé par créneau, 1 000 tirages) et je retrouve les chiffres de l'auditeur. Variante corrigée à δ = 2 s : +0,4 c [−2,0 ; +3,1] sur 1 689 exécutions ; en ne gardant que P − prix − frais > 0, +1,2 c [−1,5 ; +4,1] sur 1 396 ; au-dessus de la marge, +1,6 c [−1,9 ; +5,0] sur 863. À δ = 3 s : −0,5 c, puis +0,8 c [−2,0 ; +3,9], puis +1,6 c [−1,9 ; +5,3]. Brute à δ = 2 s : −1,2 c, puis −1,1 c, puis −0,6 c. Nuance : à δ = 2 s, cette correction garde l'exécution à latence nulle (≈ t − 0,2 s). La lecture honnête est donc celle de δ = 3 s, c'est-à-dire +0,8 à +1,6 c non significatifs. Le filtre « espérance > 0 » du preneur rapide équivaut à un ordre limité ; il est légitime. J'ai aussi chiffré la sélection des non-exécutions, que l'auditeur disait « de signe incertain ». Sur la variante corrigée, 116 décisions sans trade ont un taux de gain de 19,8 %, contre 45,8 % pour les exécutées, avec un ask estimé moyen de 0,198. Comptées à l'ask estimé, elles font passer le P&L de +0,44 à +0,36 c. Pour la brute, on passe de −1,17 à −0,88 c. L'effet reste inférieur à 0,3 c dans les deux sens.

*Effet sur la conclusion :* Le biais optimiste gonfle les chiffres affichés : le « +3 à +5 c » est un artefact d'exécution, et le +7,7 c à λ = 0 est inaccessible. Le biais pessimiste de l'ordre au marché vaut ≈ +1 à +2 c/part, et les IC contiennent toujours 0. Le verdict « aucun avantage démontré à latence ≥ 1 s » tient. La formulation « il ne reste rien » devrait devenir « +1 à +1,6 c non significatif avec un ordre limité (δ = 3 s) ».

**6. réglages sur tout l'échantillon : partiel**

* `scripts/polymarket_formula_backtest.py:60` : `HL_GRID = (15, 30, 60, 120, 300, 600, 1800)`
* `scripts/polymarket_formula_backtest.py:61` : `MARGIN_GRID = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.25, 0.30)`
* `scripts/polymarket_formula_backtest.py:62` : `K_GRID = tuple(np.round(np.arange(0.8, 2.21, 0.05), 2))`
* `scripts/polymarket_formula_backtest.py:65` : `MARGIN_DELTA = 0`

L'inventaire de l'auditeur est exact : demi-vie (7 valeurs → 600 s), estimateur de σ (3), k (29 valeurs → 1,40), décalage c (0–7 → 4 s), σ_b (0,40 pb), marge (12 valeurs par variante → 10 c et 3 c, choisie à δ = 0) et 34 × 3 coefficients d'empilement. Tous sont ajustés sur la 1re moitié, ce que confirment les masques train. Une réserve sur la preuve de « méta-fuite » : dans git, k, c_best et σ_b figurent déjà dans le premier instantané (2bf9432), si bien qu'on ne peut pas prouver qu'ils ont été ajoutés APRÈS avoir vu le test. En revanche, le docstring de polymarket_signal montre que la 2e moitié a été consultée plusieurs fois : pentes du test, effet du décalage « sur le test ». Élément nouveau : snipe_latency, equal_information et BLOCK_LAG_S n'apparaissent que dans le dernier commit (15c07c0). Ils sont absents de 2bf9432, ac242ac et aeb88e9. L'analyse du preneur rapide et son filtre ont donc été ajoutés après les versions précédentes du rapport. Ses seuils (saut de 5 points, fenêtre 1 s, silence 3 s, markout 10 s) sont choisis et évalués sur les mêmes 72 marchés, sans validation.

*Effet sur la conclusion :* Biais optimiste modéré sur la calibration « corrigée » (pentes 0,91–1,17), sans effet sur le verdict « ne bat pas le marché ». La fenêtre de 300 ms et les +13,4 c / +5,7 c avec filtre ne sont pas établis hors échantillon. Les paramètres sont BTC mais ont été appliqués à ETH. Le verdict négatif n'en dépend pas.

**7. hausse et baisse (régimes) : partiel**

* `scripts/polymarket_formula_backtest.py:55` : `START = pd.Timestamp("2026-09-04", tz="UTC")`
* `scripts/polymarket_formula_backtest.py:56` : `MID = pd.Timestamp("2026-09-14", tz="UTC")`
* `scripts/polymarket_formula_backtest.py:57` : `END = pd.Timestamp("2026-09-25", tz="UTC")`
* `reports/polymarket/formule/README.md:254` : `**n est petit** : 72 marchés BTC 5m / BTC 15m / ETH 5m ouverts entre le 26/09/2026 04:30 et le 26/09/2026 10:55 UTC (aucun marché exploitable entre 06:00 et 10:00), 11 créneaux de 15 min.`

J'ai remesuré les régimes sur le cache pm_maker/binance_1s. Apprentissage (04/09–13/09) : BTC passe de 81 265 à 76 842 (−5,44 %), σ 5 min = 9,6 pb, soit 31 % annualisé. Test (14/09–24/09) : de 76 842 à 84 410 (+9,85 %), plage 74 968–87 395, σ 5 min = 12,4 pb, soit 40 % annualisé. On y trouve des jours baissiers (15/09 −3,25 %, 23/09 −2,10 %), des jours de forte hausse (18/09 +5,85 %, 21/09 +6,70 %) et des jours calmes (19/09 à 7,3 pb, 20/09 à 8,4 pb) ; la volatilité journalière va de 7 à 16 pb/5 min. Correction à l'auditeur : le test dure 11 jours et l'échantillon 21 jours au total, et non « un mois et 11 jours ». Carnet réel (pm_formula/binance_1s, 04:30–11:00) : BTC +0,34 %, σ 5 min = 4,7 pb (15 % annualisé) ; ETH +0,05 %, 6,4 pb (21 %). C'est un seul régime, calme.

*Effet sur la conclusion :* Historique : la couverture des régimes de hausse, de baisse et de volatilité est acceptable pour un verdict négatif. Le test, plus volatil que l'apprentissage, est un vrai test hors régime pour k et la marge. Carnet réel : les résultats du preneur rapide (240 ms, fenêtre de 300 ms) ne valent que pour une matinée calme et ne se généralisent pas. Le verdict ne change pas, mais la partie « preneur rapide » n'est pas concluante.

**8. horloges et alignement : partiel**

* `src/tradebot/polymarket_formula_backtest.py:95` : `self.t0 = int(sec_open[0]) + 1                     # instant où x[0] est connu`
* `src/tradebot/polymarket_formula_backtest.py:19` : `* **Trades preneurs** (data-api Polymarket) : horodatés **au bloc Polygon**, ≈ 2 s après`
* `scripts/polymarket_formula_backtest.py:67` : `BLOCK_LAG_S = 2                   # bloc − appariement (médiane mesurée 2,2 s, arrondie à la seconde des prix 1 s)`
* `scripts/polymarket_formula_backtest.py:509` : `t_eff = np.where(ok_age, t - np.nan_to_num(age).astype("int64") - BLOCK_LAG_S, t).astype("int64")`

Tout est en secondes Unix UTC. Les bougies Binance sont indexées à l'ouverture (klines_to_frame, open_time), et le prix est connu à l'ouverture + 1 s : la convention est correcte. Je confirme les trois horloges de l'auditeur. (a) Le retard de bloc a été mesuré sur la seule journée du 26/09, sur 41 541 trades : moyenne 2,21 s, P5 1,28 s, quartiles 1,78–2,61 s, P95 3,15 s. Il est ensuite appliqué comme une constante de 2 s au 04–24/09 et ne corrige que l'analyse « à information égale ». (b) Chainlink est approché par Binance décalé d'un nombre entier de secondes, avec une erreur résiduelle σ_b modélisée. (c) Au carnet réel, l'horloge serveur Polymarket (ms) est comparée à T Binance (ms) sans que leur écart soit mesuré. Point nouveau : book_states_at arrête la relecture au premier événement de carnet dont l'horodatage serveur dépasse g. Or 0,69 % des événements book/pc sont hors ordre (3,08 M événements, 30 marchés vérifiés ; jusqu'à 324 ms en arrière). Le carnet reconstruit peut donc omettre brièvement une mise à jour déjà faite. Le phasage de la variante corrigée sur t au lieu de τ = t + 4 est confirmé.

*Effet sur la conclusion :* Historique : l'effet est déjà contenu dans la lecture « δ − 2,2 s » et ne change pas le verdict. Carnet réel : une erreur d'horloge de quelques dizaines de ms, plus les événements hors ordre rares, déplacent le seuil de 300 ms d'environ 10 à 20 %. La conclusion qualitative (il faut la vitesse d'un teneur de marché) tient ; le chiffre précis n'est pas robuste.

**Autres défauts confirmés**

* **Exécution historique au marché sans prix limite : le P&L à δ ≥ 2 s est sous-estimé** (confirmé, gravité moyenne, pessimiste) — `scripts/polymarket_formula_backtest.py:542`. Recalcul reproduit exactement : corrigée à δ = 2 s, +1,2 c [−1,5 ; +4,1] sur 1 396 et +1,6 c [−1,9 ; +5,0] sur 863, contre +0,4 c. À δ = 3 s, +0,8 c et +1,6 c contre −0,5 c. Brute à δ = 2 s : −0,6 c contre −1,2 c. Réserve : à δ = 2 s, la correction garde une exécution à latence nulle (≈ t − 0,2 s) ; δ = 3 s est la lecture juste. Les IC contiennent 0.
* **Le « preneur rapide » est mesuré contre le milieu, sans coût de sortie** (confirmé, gravité moyenne, optimiste) — `scripts/polymarket_formula_backtest.py:885`. En sortie preneur (demi-écart ≈ 0,5 c + frais ≈ 1,7 c), on obtient ≈ +5,5 c à 0 ms, ≈ +2 c à 200 ms et < 0 à 300 ms. En sortie teneur de marché, le coût serait moindre. Contre l'issue (pnl_issue), le résultat est significatif jusqu'à 100 ms sans filtre, et jusqu'à 200 ms avec le filtre « espérance > 0 » (+9,7 c, IC +3,9 ; +15,0), contrairement à ce que dit l'auditeur. La phrase de la ligne 17, « plusieurs cents par part jusqu'à 200 ms », est trop généreuse si l'on sort en preneur.
* **IC par bootstrap sur 11 grappes seulement (carnet réel), donc trop étroits** (confirmé, gravité moyenne, optimiste) — `reports/polymarket/formule/README.md:15`. Un bootstrap percentile groupé sur G = 11 créneaux d'une même matinée couvre mal l'incertitude. Le rapport le reconnaît (« IC indicatifs ») mais s'appuie quand même sur ces IC pour placer la fenêtre à 300 ms.
* **Le signal « live » n'est pas la variante backtestée et cite des résultats du test** (confirmé, gravité basse, incertain) — `src/tradebot/polymarket_signal.py:106`. Le module live utilise σ_b = 0,50 pb, sans le décalage de 4 s (ligne 40 : « non utilisé »). Le backtest valide σ_b = 0,40 pb avec ce décalage. Les pentes citées dans le docstring (1,01 et 1,12) ne sont pas celles du rapport (0,91 et 1,06). Le docstring montre que la 2e moitié a été consultée. Cela ne touche pas les chiffres du rapport.
* **Marge choisie avec une exécution impossible (δ = 0), puis appliquée à δ ≥ 2 s** (confirmé, gravité basse, neutre) — `scripts/polymarket_formula_backtest.py:583`. MARGIN_DELTA = 0 (ligne 65). Le rapport reconnaît lui-même, ligne 74, qu'à δ = 2 s aucune marge n'est rentable sur la 1re moitié. Le +0,4 c publié ne vient donc pas d'une règle adaptée à ce délai. Aucun effet sur le signe du verdict.
* **Tests multiples non corrigés sur les rares résultats « significatifs »** (confirmé, gravité basse, optimiste) — `reports/polymarket/formule/README.md:78`. Le résidu « à information égale » (0,59–0,62 de S − 20 à S − 5) et l'empilement significatif au seul instant E − 60 ne survivraient pas à une correction de Holm. Le filtre « espérance > 0 » du preneur rapide n'apparaît que dans le dernier commit.
* **Phases de la variante corrigée étiquetées sur t au lieu de τ = t + 4 s** (confirmé, gravité basse, neutre) — `scripts/polymarket_formula_backtest.py:270`. Dans la variante corrigée, S − 2 et S sont en réalité en phase 3, et E − 60 en phase 4. Cela touche 3 instants sur 17 dans les tableaux par phase ; l'effet sur les pentes est faible.
* **Trou de données du 19/09 : 40 marchés test sans prix de marché** (confirmé, gravité basse, neutre) — `scripts/polymarket_formula_backtest.py:363`. Vérifié : 40 marchés du test, tous du 19/09 (32 créneaux), n'ont aucun p_mkt. 12 marchés de la période n'ont aucun trade (8 052 sur 8 064). L'effet est d'environ 1 % et sans lien avec l'issue.
* **bfill journalier dans le cache Binance 1 s de l'historique** (nouveau, gravité basse, optimiste) — `src/tradebot/polymarket_maker.py:388`. load_binance_hist lit data/cache/pm_maker/binance_1s, construit par fetch_binance_1s_closes. Si les premières secondes d'un jour UTC n'ont pas de bougie, elles reçoivent un close futur, ce qui touche K et Ā des marchés ouverts à 00:00. L'effet est au plus de quelques secondes par jour sur BTCUSDT, qui trade presque chaque seconde : négligeable (< 0,01 c), mais c'est bien du look-ahead dans le chemin de données.
* **Carnet réel : décisions écartées quand un côté du carnet est vide (fin de fenêtre quasi exclue)** (nouveau, gravité basse, incertain) — `scripts/polymarket_formula_backtest.py:737`. À E − 30 et E − 10, il ne reste que 20 et 4 marchés sur 72. Sur 40 marchés vérifiés, 34 ont un carnet à un seul côté à E − 10. Le marché y est déjà très sûr, et un côté reste pourtant achetable. Les scores « fin (E−90…E−10) » et le P&L au carnet réel portent donc sur les marchés encore incertains, alors que l'historique n'a pas ce filtre. Le sens de l'effet est incertain et ne change pas le verdict (n petit, IC contenant 0).
* **Reconstruction du carnet arrêtée au premier événement hors ordre** (nouveau, gravité basse, incertain) — `src/tradebot/polymarket_formula_backtest.py:452`. 0,69 % des événements book/pc sont hors ordre selon l'horodatage serveur (jusqu'à 324 ms sur 30 marchés). book_states_at s'arrête au premier événement postérieur à g et omet ainsi une mise à jour déjà faite. À l'échelle de λ = 50–300 ms, le preneur rapide peut parfois voir un ask encore périmé (effet optimiste). Dans mid_path, l'horodatage est remplacé par le maximum cumulé, ce qui retarde le milieu et allonge un peu le délai de réaction mesuré. Effet faible, sens incertain.

## Arbitrage entre marchés liés (15m contre 5m) : historique et carnet réel (scripts/polymarket_arbitrage.py, src/tradebot/polymarket_arbitrage.py, reports/polymarket/arbitrage/README.md)

**1. information future (look-ahead) : partiel**

* `scripts/polymarket_arbitrage.py:372` : `win = "Up" if V[b] >= V[a] else "Down"`
* `scripts/polymarket_arbitrage.py:375` : `for tag, sel in (("apres", buys[buys["t"] >= E]), ("avant", buys[(buys["t"] >= E - 2) & (buys["t"] < E)])):`
* `src/tradebot/polymarket_arbitrage.py:347` : `if n == K4H or t_rel >= FIVE_MIN * n + known_delay_s:`
* `scripts/polymarket_arbitrage.py:514` : `V[k] = vr if np.isfinite(vr) else vm`

Toutes les citations sont exactes. Côté arbitrage (historique et carnet), V2 est pris égal au finalPrice officiel dès S+600 (known_delay_s = 0). C'est défendable : même en attendant le point T−2 pour lever l'ambiguïté 2 s / 3 s, le niveau arrive avant T, à 0,78 s en médiane (0,05 à 1,0 s, recalculé sur le flux RTDS en cache). Côté carnet, les V de source « rtds » sont par construction égaux à gamma (ligne 1061). Pour les 3 violations, |V2−V0| vaut 0,81 pb et 0,54 pb, soit environ 20 fois l'ambiguïté maximale observée (0,34 $ ≈ 0,04 pb) : aucun effet. Dans l'historique, le choix du portefeuille couvrant dépend du signe officiel de V2−V0. Les épisodes à |V2−V0| < 0,04 pb, indécidables en temps réel, pèsent 8,22 $ sur 102,10 $ en variante A et 2,30 $ sur 43,33 $ en variante C (recalcul sur historique_episodes.csv). Le vrai look-ahead est au § 5. Le gagnant y est fixé par le finalPrice officiel, et les blocs datés ≥ E comptent comme « sûrs ». Or le dernier point nécessaire n'arrive qu'environ 0,8 s avant E (voir new_issues), et un bloc daté E peut contenir des appariements de E−2 s. Recalcul sur historique_gagnant_apres_cloture.csv : 13 marchés (et non 14) à |écart| < 0,013 pb portent 12 655 $ ; 44 marchés à < 0,04 pb, soit l'ambiguïté maximale observée, portent 21 544 $ (48 %) ; 22 118 $ sont à < 0,05 pb.

*Effet sur la conclusion :* Aucun effet sur le verdict « pas d'arbitrage exécutable ». Le seul chiffre positif du § 5 (45 030 $) est gonflé : environ 48 % viennent de quasi-égalités qu'on ne peut pas trancher en temps réel. Corriger réduirait le « coup sûr » à environ 23 000 $ au plus, avant même la question de la file d'attente. Cela renforce « pas une stratégie accessible ».

**2. survivants : partiel**

* `scripts/polymarket_arbitrage.py:395` : `if f"btc-updown-15m-{S}" not in by_slug:`
* `scripts/polymarket_arbitrage.py:279` : `sel = d[(d["side"] == "BUY") & (d["outcome"] == side) & (d["t"] >= S - 70) & (d["t"] < S + 900)]`
* `scripts/polymarket_arbitrage.py:1081` : `groups_live = [S for S in groups_live if S + 900 + 125 < now]          # fenêtres terminées (+ fin de collecte)`
* `reports/polymarket/arbitrage/README.md:185` : `* **Carnet réel = court échantillon** (3,3 h, une seule journée, avec une panne de 06:05 à 10:00).`

Rien n'est filtré sur l'issue : 2 011 fenêtres 15m sur 2 016, et 8 043 marchés sur 8 064 au § 5. J'ai vérifié qu'il n'y a pas de perte aux frontières de jour : les fichiers de trades sont découpés par marché, et la 15m de 23:45 du 10/09 contient ses trades jusqu'à 00:00:52. Les exclusions portent sur les fenêtres sans trade, les niveaux NaN, les SELL (14,6 % des lignes vérifié le 10/09 : 16 363 sur 112 379), ETH/SOL et la 4h. Elles retirent des détections. Il n'y a pas de sélection de survivants au sens strict.

*Effet sur la conclusion :* Biais légèrement pessimiste et négligeable (0,25 % des fenêtres). Ne change pas le verdict, qui ne vaut toutefois que pour BTC 5m/15m.

**3. données futures dans un indicateur : partiel** (le contradicteur corrige l'auditeur)

* `scripts/polymarket_arbitrage.py:1061` : `exact = [off for off, e in errs.items() if np.isfinite(e) and abs(e) < 1e-6]`
* `scripts/polymarket_arbitrage.py:1065` : `v, rxk, _ = cand[off_m]`
* `scripts/polymarket_arbitrage.py:1075` : `rtds_known[T] = rxk`
* `src/tradebot/polymarket_arbitrage.py:413` : `pmax = df.groupby("ts")["p"].transform("max")`

Il n'y a ni moyenne centrée, ni bfill, ni merge_asof forward. Le block_max ne regroupe que des secondes déjà écoulées (searchsorted side='right'). En revanche, le décalage TWAP (2 ou 3 s) est choisi niveau par niveau par égalité exacte avec la valeur gamma publiée après coup. Les V « rtds » sont donc gamma par construction, et rtds_known est l'heure d'arrivée du dernier point du décalage *gagnant*. Je conteste le sens donné par l'auditeur. Le cas à 2 s (0,78 s avant T) n'est pas surestimé, il est correct. Ce sont les 7 cas à 3 s qui le sont : en temps réel, on ne sait pas que le bon décalage est 3 s, et il faut attendre le point T−2 pour calculer l'autre candidat. D'après le flux RTDS en cache, ce point arrive 0,74 / 0,05 / 1,01 / 0,51 / 0,94 / 0,78 / 0,88 / 0,79 s avant T, soit 0,78 s en médiane. Le 1,58 s publié surestime donc l'avance d'environ 0,8 s dans 7 cas sur 8. Ce n'est vrai que pour les quasi-égalités, où les deux candidats peuvent désigner des gagnants différents.

*Effet sur la conclusion :* Effet nul sur les 3 violations du carnet (écarts ≥ 0,5 pb) et sur le « 0 cas » du carnet au § 5, dont un biais optimiste ne pourrait qu'augmenter le compte. L'avance publiée (« connu environ 1,6 s avant la fin ») est optimiste d'environ 0,8 s. Cela renforce « course de vitesse inaccessible » sans changer le verdict.

**4. frais et glissement : partiel**

* `src/tradebot/polymarket_arbitrage.py:89` : `fee = rate * np.power(np.clip(p * (1.0 - p), 0.0, None), exponent)`
* `src/tradebot/polymarket_arbitrage.py:302` : `cost_a = taker_cost(price_a, fee_rate, fee_exponent)`
* `scripts/polymarket_arbitrage.py:504` : `st = pa.live_group_states(markets, depth=3)`
* `src/tradebot/polymarket_wallets.py:25` : `distincts pour 1 591 lignes), la ligne du **preneur**, avec son prix moyen d'exécution ;`

Le frais preneur 0,07·p(1−p) est appliqué sur chaque jambe, dans le programme linéaire, dans _fill et au § 5. Il est conforme au fee_schedule enregistré dans les meta.json du carnet (rate 0,07, exponent 1, takerOnly). Le carnet est parcouru sur 3 niveaux avec leurs tailles, donc la profondeur est modélisée. Dans l'historique, le « prix » est le VWAP d'un autre preneur, avec sa taille : l'impact au-delà du VWAP n'est pas modélisé, mais le rapport le présente comme une détection. Je conteste la partie « builder » de l'auditeur. Selon polymarket_wallets.py, il s'agit « probablement » de frais d'intégrateur, observés sur de petits ordres passés par des interfaces tierces. Un robot capable de viser des fenêtres de quelques ms passerait directement par l'API CLOB et ne les paierait pas. Ce ne sont donc pas 1 à 9 c/part de coût omis pour la stratégie étudiée.

*Effet sur la conclusion :* Sur l'historique, biais optimiste de l'ordre de 0 à 1 c/part (VWAP contre pire niveau). L'effet est inexistant sur le carnet, où les violations sont de toute façon fictives (point 8). Le verdict ne change pas.

**5. prix jamais disponible : présent**

* `scripts/polymarket_arbitrage.py:354` : `r = pa.solve_arbitrage(A, list(prices), list(sizes), max_sets=n)`
* `src/tradebot/polymarket_arbitrage.py:414` : `at = df[df["p"] == pmax].groupby("ts").agg(p=("p", "first"), s=("s", "sum"))`
* `scripts/polymarket_arbitrage.py:381` : `row[f"profit_{tag}"] = float((sel["size"].to_numpy() * mg)[pos].sum())`
* `scripts/polymarket_arbitrage.py:608` : `t_arr = rx[i0] + lat * 1_000_000`

Je confirme. Historique : exécution au VWAP d'un trade déjà réalisé, pour la taille exacte consommée. § 5 : 45 030 $ de liquidité déjà prise par d'autres. Carnet à 0 ms : exécution au meilleur ask de l'instant de détection. Deux aggravations. (a) Les états conjoints « exécutés à 0 ms » n'ont jamais existé côté serveur : en réordonnant les messages par ts serveur, les 3 violations disparaissent (point 8). Le 0,723 $ à 0 ms est donc un prix jamais disponible. (b) orderMinSize vaut 5 parts sur les marchés BTC 15m (champ raw de data/cache/polymarket/wallets/markets/btc_15m_2026-09.parquet, 2 100 marchés sur 2 100). La première violation ne porte que 1 part au meilleur niveau : un ordre de 5 parts aurait balayé le niveau suivant. La simulation avec retard ignore en outre la file, l'aller vers le serveur et le décalage de 50 à 300 ms entre horloge locale et serveur.

*Effet sur la conclusion :* Biais optimiste sur tous les chiffres positifs : 0,72 $ à 0 ms (en réalité 0 $), 43 à 102 $ dans l'historique, 45 030 $ au § 5. Corriger renforce le verdict négatif. En revanche, le « gain sûr » du § 5 et l'« exemple réel » sont présentés avec des montants qu'aucun entrant n'aurait pu obtenir.

**6. réglages sur tout l'échantillon : partiel**

* `scripts/polymarket_arbitrage.py:69` : `"A": {"max_age": 5.0, "plus": 0.0, "mode": "last", "label": "dernier achat < 5 s"},`
* `scripts/polymarket_arbitrage.py:71` : `"C": {"max_age": 2.0, "plus": 0.0, "mode": "block_max", "label": "prix max du dernier bloc < 2 s"},`
* `scripts/polymarket_arbitrage.py:657` : `t0 = t_known if np.isfinite(t_known) else E - 1.7`
* `scripts/polymarket_arbitrage.py:1061` : `exact = [off for off, e in errs.items() if np.isfinite(e) and abs(e) < 1e-6]`

Il s'agit d'une mesure descriptive, sans modèle ajusté. L'inventaire d'environ 14 paramètres de l'auditeur est correct. Trois d'entre eux ont été fixés en regardant l'échantillon évalué : la variante C, le décalage TWAP choisi contre gamma et le repli E−1,7 s. Aucun n'est orienté vers un P&L positif, et le rapport montre les trois variantes.

*Effet sur la conclusion :* Faible. Ne change pas le verdict.

**7. hausse et baisse (régimes) : partiel**

* `scripts/polymarket_arbitrage.py:67` : `DAYS = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-09-04", "2026-09-24", freq="D")]`
* `reports/polymarket/arbitrage/README.md:185` : `* **Carnet réel = court échantillon** (3,3 h, une seule journée, avec une panne de 06:05 à 10:00).`

Vérifié sur data/cache/BTCUSDT_1m.parquet. BTC ouvre à 81 270 le 04/09 et clôt à 84 410 le 24/09 (+3,9 %), avec des extrêmes de 74 968 et 87 396 et une volatilité annualisée de 35,9 %. On y trouve des jours baissiers (−3,25 % le 15/09, −2,22 % le 10/09, −2,10 % le 23/09) et haussiers (+5,85 % le 18/09, +6,70 % le 21/09). La volatilité quotidienne va de 12 à 53 %. L'historique couvre donc plusieurs régimes. Le carnet réel du 26/09 est vérifiable uniquement de 10:29 à 11:57 via RTDS : BTC reste entre 84 102 et 84 187, avec une volatilité à la seconde d'environ 6,6 % annualisée, un régime très calme. La plage 04:30 à 06:05 n'est pas vérifiable : le Binance 1m en cache s'arrête à 00:43 le 26/09. Nuance importante : une fois ordonnés par horloge serveur, les 13 fenêtres du carnet ne contiennent aucune violation (point 8). Or la fréquence historique (2,9 à 5,7 % des fenêtres) n'en laissait attendre que 0,4 à 0,7. L'échantillon n'a pas la puissance pour dire quoi que ce soit sur la fréquence.

*Effet sur la conclusion :* Biais potentiellement pessimiste sur « rien d'exécutable », fondé sur une matinée calme de 3,3 h. Le verdict reste plausible, vu les tailles et les durées, mais il n'est pas établi statistiquement. Il est peu probable que cela inverse la conclusion.

**8. horloges et alignement : présent** (le contradicteur corrige l'auditeur)

* `src/tradebot/polymarket_arbitrage.py:509` : `streams = [[(e[0], k, e) for e in mk.events if e[2] in ("book", "pc")] for k, mk in enumerate(markets)]`
* `src/tradebot/polymarket_arbitrage.py:516` : `for rx, k, ev in heapq.merge(*streams, key=lambda x: x[0]):`
* `src/tradebot/polymarket_arbitrage.py:499` : ```1 − bid Up`` (carnet unifié). Renvoie ``rx`` (ns), ``ts`` (ms serveur), ``market`` (indice`
* `scripts/polymarket_live_collector.py:3` : `Une connexion WebSocket par marché (jetons Up et Down), ouverte 2,5 min avant l'ouverture de la`

Je passe de PARTIAL à PRESENT, avec une preuve. Les carnets sont fusionnés sur l'horloge de réception locale rx, une connexion par marché. Le ts serveur est renvoyé, mais le script ne l'utilise jamais (grep st["ts"] ne trouve rien). J'ai relancé live_group sur les 13 fenêtres en réordonnant chaque flux par ts serveur (script dans le scratchpad). Résultat : 0 violation, marge maximale ≤ −0,12 c. La même relance, ordonnée par rx, reproduit bien les 3 violations. Le mécanisme est une fusion d'horloges désynchronisées. Violation 1790419500 : l'ask 0,25 de la 15m Up vient d'un message serveur ts …383, reçu avec 195 ms de retard. L'ask 0,71 de la 5m#3 Down vient d'un message ts …530, reçu avec 48 ms de retard. Côté serveur, l'ask 0,25 avait disparu à ts …387, l'ask passant à 0,46, soit 143 ms avant que 0,71 n'apparaisse. Violation de 75 ms (1790421300) : le flux 15m était en retard d'environ 225 ms, contre 15 à 60 ms pour le 5m#3. L'ask 15m Down était déjà passé de 0,66 à 0,71 environ 136 ms serveur avant que l'ask 5m#3 Up ne tombe à 0,27. L'auditeur jugeait cette violation de 75 ms réelle : elle ne l'est pas. L'historique est en UTC epoch, sans problème de fuseau. En revanche, la datation au bloc (≈ 2 s) décale les frontières E du § 5.

*Effet sur la conclusion :* Biais optimiste fort sur les chiffres publiés du carnet. « 3 violations », « 0,72 $ », « durée de vie médiane 0,7 ms, max 75,1 ms », l'« Exemple réel » à 4,05 c et le tableau de latence sont tous des artefacts : le vrai compte est 0. Le verdict « rien n'est exécutable » en sort renforcé. Mais la phrase « Oui, il existe des instants où une combinaison gagne à coup sûr » n'a plus aucun appui dans le carnet réel : seules restent les détections historiques, que le rapport qualifie lui-même de bruit.

**Autres défauts confirmés**

* **Le « seul coup sûr qui rapporte » (45 030 $) inclut environ 48 % de cas que le rapport juge lui-même indécidables** (confirmé, gravité moyenne, optimiste) — `reports/polymarket/arbitrage/README.md:14`. Recalculé sur historique_gagnant_apres_cloture.csv : 13 marchés (et non 14) totalisent 12 655 $ à |écart| < 0,013 pb, 21 544 $ (48 %) à < 0,04 pb, soit l'ambiguïté 2 s / 3 s maximale observée, et 22 118 $ à < 0,05 pb. Les marchés dont le délai médian dépasse 60 s représentent 7 598 $ (11 marchés). C'est en outre de la liquidité déjà prise par d'autres preneurs.
* **Violations inter-marchés fabriquées par la fusion de connexions WebSocket distinctes** (confirmé, gravité haute, optimiste) — `src/tradebot/polymarket_arbitrage.py:516`. Confirmé et étendu. En ordre serveur (ts), les 3 violations disparaissent, y compris celle de 75,1 ms que l'auditeur jugeait réelle. Le flux 15m accusait 150 à 300 ms de retard de réception, contre 15 à 95 ms pour le 5m#3. Sur les 13 fenêtres, le compte réel est de 0 violation.
* **Trades preneurs SELL ignorés dans l'estimation des prix historiques** (confirmé, gravité basse, incertain) — `scripts/polymarket_arbitrage.py:279`. Vérifié : 16 363 SELL sur 112 379 trades preneurs BTC 15m le 10/09 (14,6 %). Un SELL Up révèle le bid Up, donc l'ask Down par complément. Sens net incertain : l'estimation est à la fois plus rare et plus ancienne. Effet faible sur une partie que le rapport qualifie de bruit.
* **Conclusion forte sans incertitude statistique sur un échantillon de 13 fenêtres** (confirmé, gravité moyenne, pessimiste) — `reports/polymarket/arbitrage/README.md:13`. Avec 0 violation réelle en ordre serveur, et 0,4 à 0,7 fenêtre attendue d'après l'historique, l'échantillon ne distingue pas « rare » de « jamais ». L'argument solide est la taille (quelques parts) et l'absence de toute violation réelle, pas un décompte.
* **Prix historique = VWAP d'une transaction preneur, pas un ask disponible** (confirmé, gravité basse, optimiste) — `src/tradebot/polymarket_wallets.py:25`. Le programme linéaire historique suppose qu'on pouvait acheter la taille du preneur à son VWAP, alors qu'elle a déjà été consommée. Le rapport le reconnaît en partie (« Les tailles utilisées sont celles réellement échangées, pas la profondeur disponible »). Les 42,93 à 102,10 $ ne sont pas une borne de l'exécutable.
* **Stratégie maker (ordres au repos sur les deux jambes) non testée** (confirmé, gravité basse, pessimiste) — `src/tradebot/polymarket_arbitrage.py:302`. Toute l'analyse suppose des ordres preneurs. Les meta.json indiquent takerOnly avec un rebateRate de 0,2 : un maker ne paie rien et touche une remise. La version maker porte toutefois un risque de jambe, donc elle n'est pas « à coup sûr ». Elle sort du périmètre de la question, mais le verdict ne vaut que pour le preneur.
* **L'« exemple réel » (4,05 c, 75,10 ms) et le « Oui » du verdict reposent sur un état conjoint qui n'a jamais existé côté serveur** (nouveau, gravité haute, optimiste) — `reports/polymarket/arbitrage/README.md:16`. La fenêtre 1790421300 a été relancée en ordonnant chaque flux par ts serveur au lieu de rx local. Le flux 15m accusait environ 225 ms de retard de réception, contre 15 à 60 ms pour le 5m#3. Côté serveur, l'ask 15m Down était déjà passé de 0,66 à 0,71 environ 136 ms avant que l'ask 5m#3 Up ne tombe à 0,27 : la combinaison 0,660 / 0,270 n'a jamais coexisté. Sur les 13 fenêtres, l'ordre serveur donne 0 violation (marge maximale −0,12 c), alors que l'ordre local, avec le même code, reproduit les 3 violations. Le ts serveur est renvoyé par live_group_states mais n'est jamais utilisé par le script. La figure carnet_exemple.png, la ligne 15 du README, le tableau de latence et la durée de vie médiane de 0,7 ms sont donc des artefacts.
* **Avance de connaissance du TWAP (« ≈ 1,6 s avant la fin ») calculée avec le décalage choisi a posteriori contre gamma** (nouveau, gravité moyenne, optimiste) — `scripts/polymarket_arbitrage.py:1065`. rtds_known est l'heure d'arrivée du dernier point du décalage qui reproduit gamma, une valeur publiée après la résolution. En temps réel, sans savoir si le décalage est de 2 s ou de 3 s, il faut attendre le point T−2. D'après le flux RTDS en cache, il arrive 0,05 à 1,01 s avant T, soit 0,78 s en médiane, contre 1,58 s publié. Pour une quasi-égalité, l'issue reste en plus indécidable. L'avance est surestimée d'environ 0,8 s dans 7 niveaux sur 8.
* **Taille minimale d'ordre (5 parts) ignorée : la première violation porte 1 part au meilleur niveau** (nouveau, gravité basse, optimiste) — `reports/polymarket/arbitrage/README.md:110`. Le champ orderMinSize vaut 5 pour les 2 100 marchés BTC 15m en cache (colonne raw de data/cache/polymarket/wallets/markets/btc_15m_2026-09.parquet). Le programme linéaire et _fill acceptent des lots d'une part, ce que le CLOB refuse. Montant négligeable (0,012 $), et cette violation est de toute façon fictive.
* **Au § 5, les blocs datés E sont comptés comme « sûrs » alors que l'issue n'est connue qu'environ 0,8 s avant E** (nouveau, gravité moyenne, optimiste) — `scripts/polymarket_arbitrage.py:375`. Un bloc Polygon daté E regroupe des appariements d'environ E−2 s à E. Or le point RTDS T−2, nécessaire pour trancher entre les décalages de 2 s et de 3 s, n'arrive que 0,78 s avant E en médiane. Une partie des achats « après E » à délai 0 s a donc été appariée avant que l'issue soit connaissable. Or ces achats pèsent lourd : 30 360 $ sur 45 030 $ viennent de marchés au délai médian ≤ 2 s.

## Polymarket : simulation maker sur le carnet réel enregistré (WebSocket), reports/polymarket/maker_live/README.md

**1. information future (look-ahead) : partiel** (le contradicteur corrige l'auditeur)

* `src/tradebot/polymarket_maker_live.py:311` : `w = bars1s[(bars1s.index >= a) & (bars1s.index < b)]["close"]`
* `src/tradebot/polymarket_maker_live.py:347` : `i = np.searchsorted(sec, t - 1, side="right") - 1      # dernière bougie ouverte ≤ t−1 (close à t)`
* `src/tradebot/polymarket_maker_live.py:195` : `while pend_i < len(pending) and pending[pend_i].t_place + self.latency_s <= ts:`
* `src/tradebot/polymarket_maker_live.py:444` : `out.append(Order("fair_value", side, cur[0], cur[1], t, size, label=f"± {round(100 * margin)} c",`

Toutes les citations de l'auditeur existent mot pour mot aux lignes indiquées. Sur le fond, je suis d'accord : aucune donnée future n'entre dans le signal. (a) utilise les bougies ouvertes dans [S−60, S−30), la dernière se clôt à S−30, et l'ordre devient actif à S−29,7. (c) utilise la bougie close à t. Le plafond bbo lit le carnet à ts ≤ t. La pose attend le premier événement de ts ≥ t_place + 0,3 s, donc tous les événements déjà appliqués ont ts < t_place + 0,3 s. L'exclusion « crossing » à t + 0,3 s reproduit un ordre post-only : elle est légitime. Selon la convention, le défaut existe pourtant dans une partie du chemin de code, d'où PARTIAL. (i) L'annulation réactive de (c) (t_cancel = t, ligne 444) prend effet à l'instant même de la décision, sans les 300 ms que la pose subit. J'ai réexécuté le simulateur avec 300 ms ajoutées à ces annulations : (c) passe de −1,11 à −1,03 c par part placée. La valeur de l'auditeur est reproduite, et l'effet est nul, voire pessimiste. (ii) Le délai d'arrivée de la bougie Binance 1 s n'est pas compté. (iii) La simulation tourne en horodatage serveur, comme si le carnet arrivait instantanément. Or l'enregistrement montre des retards de réception rx − ts dont le p99 atteint 1 à 3 s sur plusieurs marchés, avec un maximum de 12 à 14 s (btc-15m-1790397900, btc-15m-1790400600), et des coupures « slow consumer » (ligne 126 du journal, à S exactement). Un robot réel aurait donc parfois décidé sur un carnet périmé. Ce dernier point est traité dans new_issues.

*Effet sur la conclusion :* Aucun. L'annulation sans latence déplace (c) de 0,08 c dans le sens pessimiste. Le retard de réception n'a pas de sens établi. Le verdict « pas d'avantage maker » ne change pas.

**2. survivants : partiel**

* `scripts/polymarket_maker_live.py:96` : `if not bool(at.loc[-60.0, "fresh"]):`
* `scripts/polymarket_maker_live.py:88` : `if E + 60 > now:`
* `src/tradebot/polymarket_maker_live.py:500` : `scored = placed[np.isfinite(placed["pnl_per_share"])]`
* `src/tradebot/polymarket_maker_live.py:272` : `finish(lv, "partial" if lv.filled > 0 else "expired")`

Toutes les citations sont exactes. J'ai reproduit le rapport à l'identique : même README, au caractère près, avec les 54 marchés finis et l'issue des deux marchés 1790417400 remise à None. Détail inexact de l'auditeur : 1790417400 correspond à 10:10 UTC, pas à 10:05. Ses chiffres sont confirmés. En réintroduisant ces deux issues Up, (b) 0.49/S+0 passe de −1,61 à −0,91 c, (a) 0.48/S+0 de −0,68 à −1,77 c et (a) 0.50/S+0 de +1,67 à +1,56 c ; (c) reste à −1,11 c. Les exclusions (6 marchés commencés trop tard, 5 fenêtres en cours) ne dépendent pas de l'issue, et le trou de 06:05 à 10:00 est dû à la mort du collecteur, pas à la résolution. Il n'y a donc pas de survivorship au sens strict. Le vrai défaut est ailleurs. Cinq marchés tronqués sont gardés, et les ordres encore vivants à la coupure y sont notés « expired », avec un P&L nul, alors que les exécutions observées avant la coupure sont conservées. On ne garde ainsi que les exécutions qui ont « survécu » à la coupure. Sur les marchés de 06:05 (issue Up), l'achat Up de (b) est exécuté (+0,51), tandis que l'achat Down, perdant, est « expired » (voir new_issues). En retirant les 5 marchés tronqués, (b) 0.49/S+0 passe de −1,6 à −3,7 c. En comptant les ordres « expired » de 06:05 comme exécutés, il passe à −2,2 c, et (a) 0.50/S+0 passe de +1,7 à 0,0 c.

*Effet sur la conclusion :* Le verdict négatif ne change pas et devient même plus net pour (b). Les chiffres publiés dépendent pourtant à ±1 c de l'heure de lancement, et (b) est flatté de 0,6 à 2 c par les marchés tronqués. La « meilleure » configuration, (a) 0.50/S+0 à +1,7 c, tient à un ou deux marchés.

**3. données futures dans un indicateur : absent**

* `src/tradebot/polymarket_maker_live.py:337` : `var_ewm = pd.Series(r ** 2).ewm(halflife=halflife_s, min_periods=10).mean().to_numpy()`
* `src/tradebot/polymarket_maker_live.py:342` : `w = (sec > start_ts - 60) & (sec <= start_ts)     # bougies ouvertes dans (S−60, S] ≈ TWAP60(S)`
* `src/tradebot/polymarket_book.py:455` : `if ev[2] in _KINDS_BOOK and ev[1] > g_ms:`

Les citations sont exactes. J'ai cherché un contre-exemple sans en trouver. L'EWMA de σ est causale, sur des bougies triées. K inclut la bougie ouverte à S, connue à S+1, et n'est utilisé qu'à partir de S+10 pour les ordres et de S+30 pour le Brier. book_series n'applique que les événements de ts ≤ t. Il n'y a ni bfill, ni resample centré, ni merge forward, et aucune calibration n'est estimée sur tout l'échantillon (la moitié mid_ts sert seulement à découper). Deux défauts voisins existent, mais ce ne sont pas des données futures. (1) book_series prolonge le dernier état connu après la coupure d'un fichier : c'est un milieu figé, pas une donnée future. Il biaise pourtant le Brier « marché » du § 6 (voir new_issues). (2) Un milieu NaN quand un côté du carnet est vide retire du § 6 les marchés déjà décidés (voir new_issues).

*Effet sur la conclusion :* Aucun sur le verdict du P&L. Les deux défauts voisins touchent le § 6 et sont traités à part.

**4. frais et glissement : absent**

* `src/tradebot/polymarket_maker_live.py:468` : `pnl_share = (win - o.price) if np.isfinite(win) else math.nan`
* `src/tradebot/polymarket_maker_live.py:484` : `"rebate_per_share": MAKER_REBATE_RATE * float(taker_fee(o.price, 1.0, fee_sched, decimals=None)),`
* `src/tradebot/polymarket_maker_live.py:208` : `live.append(_Live(o, levels.get(level_int, 0.0), t_eff, book.mid()))`
* `src/tradebot/polymarket_maker_live.py:18` : ```1{gagnant} − L`` par part, sans frais ; **remise maker** estimée à part :`

Les citations sont exactes. Le meta.json de chaque marché porte "takerOnly": true, "rate": 0.07, "rebateRate": 0.2 : un maker ne paie aucun frais. L'exécution se fait au prix L de l'ordre, jamais au milieu. Un ordre qui croiserait le carnet est écarté, pas exécuté gratuitement. La sélection adverse est capturée par l'exécution sur les vrais trades preneurs. Le défaut « coûts oubliés » n'existe donc pas. Biais inverse confirmé : la remise (0,2 × 0,07 × p(1 − p) ≈ 0,35 c par part exécutée à 0,50) est calculée mais pas ajoutée. Pondérée par le taux d'exécution (28 à 78 %), elle représente ≈ +0,1 à +0,27 c par part placée, et c'est un biais pessimiste. Réserve : cette remise vient d'un pool redistribué, elle n'est pas garantie.

*Effet sur la conclusion :* Ajouter la remise gagnerait +0,1 à +0,3 c par part placée. Aucun IC ne deviendrait entièrement positif : le verdict ne change pas.

**5. prix jamais disponible : partiel**

* `src/tradebot/polymarket_maker_live.py:262` : `elif beyond:`
* `src/tradebot/polymarket_maker_live.py:263` : `lv.fill_all(ts, "traversal")`
* `src/tradebot/polymarket_maker_live.py:16` : `(pas de double comptage) ; un état croisé transitoire du carnet reconstruit n'exécute rien ;`
* `src/tradebot/polymarket_maker_live.py:272` : `finish(lv, "partial" if lv.filled > 0 else "expired")`

Les citations sont exactes. J'ai refait chaque test sur une copie du simulateur, avec les deux issues tardives incluses. (1) Plafonner l'exécution par traversée à la taille du trade : (a) 0.48/S+0 passe de −1,77 à −0,86 c, (c) de −1,11 à −1,15 c, et les autres configurations bougent de moins de 0,3 c. C'est ±0,9 c sans sens systématique, confirmé. La traversée représente 66 % des exécutions (c), 74 % (b) et 77 % (a). (2) Considérer un ordre comme exécuté quand le carnet le croise pendant au moins 2 s : (c) passe de −1,11 à −0,81 c et (b) 0.49/S+0 de −0,91 à −0,30 c. C'est un biais pessimiste d'au plus ≈ 0,6 c, qui confirme le sens indiqué par l'auditeur ; ma règle donne un peu moins que son +0,3. (3) Latence à 1 000 ms : (a) 0.50/S+0 passe de +1,56 à +3,23 c et (b) de −0,91 à −0,36 c, ce qui reproduit exactement l'auditeur. L'auditeur a omis l'autre sens : à 50 ms, (a) 0.50/S+0 tombe à 0,00 c, (b) à −1,48 c et (c) à −1,38 c. Les 300 ms ne sont donc pas un choix prudent, puisqu'une latence plus réaliste pour un robot rapide aggrave le résultat. Une partie de cette sensibilité tient à un seul marché, qui passe de posé à « crossing » (32 ordres contre 31). (4) Les ordres vivants à la coupure d'un marché tronqué sont marqués « expired », donc non exécutés, alors que l'exécution de l'autre côté est gardée. C'est optimiste pour (b) et pour (a) 0.50/S+0 (voir new_issues).

*Effet sur la conclusion :* Chaque hypothèse déplace le P&L de ±0,3 à ±1,7 c par part placée. Le verdict « pas d'avantage » tient dans tous les cas. En revanche, le seul chiffre positif, (a) 0.50/S+0 à +1,7 c, est ramené à 0,0 c par une latence de 50 ms ou par le traitement des marchés tronqués : il n'a aucune valeur.

**6. réglages sur tout l'échantillon : partiel**

* `src/tradebot/polymarket_maker_live.py:63` : `PRE_TRADE_MS = 200            # la mise à jour du carnet d'un trade précède son message last_trade_price`
* `src/tradebot/polymarket_maker_live.py:64` : `SIGNAL_PRICES = (0.48, 0.49, 0.50)`
* `src/tradebot/polymarket_maker_live.py:65` : `CANCEL_OFFSETS = (0, 30, 60)  # annulation à S, S+30 s, S+60 s`
* `scripts/polymarket_maker_live.py:262` : `chosen = s1.sort_values("pnl", ascending=False).iloc[0]["label"]`

Les citations sont exactes. Je confirme l'égalité : dans resume_par_moitie.csv, 0.50/S+0, 0.50/S+30 et 0.50/S+60 valent exactement 0,00000 c sur la 1re moitié (13 ordres chacun). sort_values, dont le tri par défaut n'est pas stable, a retenu 0.50/S+30. Sur la 2e moitié, les trois donnent respectivement +2,94, −2,94 et 0,00 c. Le commit 2bf9432 retire bien la règle d'exécution « cross » (lignes supprimées `if _crosses(book, lv.o.up_side, lv.level_int):` et `lv.fill_all(ts, "cross")`), et `share_by_cross` en reste comme vestige à la ligne 510. Mon test de cette règle (point 5) montre qu'elle joue dans le sens pessimiste, d'au plus 0,6 c. PRE_TRADE_MS a été calibré sur les mêmes données. La grille publiée (3 prix × 3 annulations, 13 configurations) est entièrement montrée, sans correction pour tests multiples, ce que le rapport reconnaît. Le seuil de signe du signal (gap > 0, sans zone morte) est un paramètre implicite : je le discute dans other_issues.

*Effet sur la conclusion :* Le verdict négatif n'est pas touché, car la sélection a posteriori joue dans le sens optimiste. Le test hors échantillon publié (−2,9 c) est tiré au sort entre +2,9, −2,9 et 0,0 c : il ne prouve rien.

**7. hausse et baisse (régimes) : présent**

* `reports/polymarket/maker_live/README.md:3` : `*Généré le 26/09/2026 10:20 UTC par `scripts/polymarket_maker_live.py` (temps : 57 s). Marchés collectés par `scripts/polymarket_live_collector.py` (WebSocket CLOB, une connexion par marché) : 59 enre`
* `reports/polymarket/maker_live/README.md:199` : `* **n = 48 marchés** sur une seule matinée UTC (régime de volatilité unique) ; 13 configurations testées sans correction pour tests multiples ; la meilleure configuration est choisie a posteriori. À r`
* `logs/live_collector.log:134` : `2026-09-26 06:04:49,549 WARNING eth-updown-5m-1790402700 : reconnexion 1 (ConnectionClosedError(None, None, None))`
* `logs/live_collector.log:139` : `2026-09-26 10:00:49,150 INFO collecteur format 2, séries [('btc', '5m'), ('btc', '15m'), ('eth', '5m')]`

Les citations sont exactes. J'ai vérifié sur le cache Binance 1 s (data/cache/pm_maker_live/binance_1s/). BTC passe de 83 900,00 (04:27:50) à 83 893,19 (06:15), dans une fourchette de 83 848,96 à 84 012,93 (0,2 %) ; puis de 84 002 à 84 113 entre 09:58 et 10:15. ETH reste entre 2 684 et 2 691. L'écart-type des rendements 5 min de BTC vaut 0,044 % (ETH 0,051 %) sur 04:30–06:15, contre 0,113 % sur les 30 jours précédents (cache 1 min), soit environ 40 %. Volatilité annualisée par heure UTC sur 30 jours : 25 à 27 % entre 4 et 7 h, 50 à 57 % entre 12 et 15 h, 36 % en moyenne. Le cache montre même des plages de 30 s où BTC oscille entre deux ticks (83 931,50 et 83 931,51). Il n'y a ni tendance ni régime volatil. Le rapport ne le reconnaît qu'en partie (« régime de volatilité unique »), sans chiffre ni mention du trou de 4 h.

*Effet sur la conclusion :* Le verdict ne vaut que pour une matinée calme de ≈ 1 h 40. La sélection adverse d'un maker et la valeur de la juste valeur Binance croissent avec la volatilité : aucune conclusion ne peut être étendue aux heures actives (12–16 UTC), ni pour rejeter ni pour accepter les stratégies.

**8. horloges et alignement : partiel**

* `src/tradebot/polymarket_maker_live.py:190` : `ts = ev[1] / 1000.0`
* `src/tradebot/polymarket_book.py:193` : `events.sort(key=lambda e: e[0])`
* `src/tradebot/polymarket_maker_live.py:342` : `w = (sec > start_ts - 60) & (sec <= start_ts)     # bougies ouvertes dans (S−60, S] ≈ TWAP60(S)`
* `reports/polymarket/maker_live/README.md:202` : `* Signal (a) : bougies 1 s Binance closes à S−30 s ; le flux Chainlink retarde d'≈ 4 s sur Binance (diagnostic), non modélisé ; juste valeur (c) : TWAP60(S) Binance à la place de `priceToBeat` (écart `

Les citations sont exactes. Pour un fichier unique, l'ordre est celui du fichier, c'est-à-dire l'ordre rx ; le tri de la ligne 193 ne sert qu'à plusieurs segments. J'ai recompté exactement 15 506 inversions de ts sur 3 066 456 événements (0,5 %), comme l'auditeur. J'ajoute un point : le retard médian rx − ts par marché est NÉGATIF, de −7,5 ms en médiane et jusqu'à −46 ms. L'horloge locale est donc décalée par rapport à l'horloge serveur Polymarket, et rx ne peut pas mesurer la latence réelle. La simulation n'utilise que ts, ce qui la rend insensible à ce décalage. Les bougies Binance sont indexées par leur ouverture, et le code en tient compte (close à t = bougie ouverte à t−1). K = bougies ouvertes dans (S−60, S], alors que la règle porte sur Chainlink [S−60, S) : décalage d'une seconde, plus un retard Chainlink d'≈ 4 s et un écart de niveau d'≈ 3 pb, reconnus mais non modélisés. Cela pèse sur (c) et le § 6, pas sur la file.

*Effet sur la conclusion :* Effet faible, de quelques pb sur d. Le verdict du P&L ne change pas. Cela fragilise un peu l'affirmation du § 6, déjà atteinte par le carnet figé (voir new_issues).

**Autres défauts confirmés**

* **Le signal (a) tire le côté à pile ou face sur 18 marchés BTC (|gap_m30| < 1 tick), et ces marchés font le P&L négatif de (a)** (confirmé, gravité moyenne, pessimiste) — `src/tradebot/polymarket_maker_live.py:377`. Confirmé sur marches.csv : 18 des 27 marchés BTC exploitables ont |gap_m30| < 1e−7, et 1 tick vaut 0,01/83 900 ≈ 1,2e−7. Les marchés BTC 5m et 15m qui ouvrent au même S partagent le même gap. Sur les ordres notés (P&L connu), ce sous-ensemble compte 111 ordres sur 15 marchés, avec un taux de gain de 29,7 % et −11,0 c par part placée. Le reste compte 216 ordres, avec +1,1 c. L'auditeur compte 120 et 225 ordres : ce sont les ordres placés, pas les ordres notés. Deux nuances. Ce n'est pas un biais du backtest : la stratégie telle qu'elle est définie trade ces signaux nuls, et le backtest la mesure correctement. Et le +1,1 c du sous-ensemble repose sur un seuil choisi a posteriori, donc il est lui-même optimiste. Le −3,0 c publié ne mesure pas la valeur du signal, mais il ne cache pas non plus un avantage : l'IC du sous-ensemble utile va de −14 à +22 c.
* **Des marchés tronqués sont comptés comme exploitables et le Brier « marché » utilise un carnet figé** (confirmé, gravité moyenne, optimiste) — `scripts/polymarket_maker_live.py:135`. Confirmé, et l'auditeur sous-estime l'effet. Les 46 ordres (c) no_book des marchés de 06:05 et les 15 ordres (a) « expired » sont exacts. Le 15m 1790402400 a aussi 49 ordres (c) no_book. Pour le 15m, p_mid = 0,315 est figé à 450 s et 840 s (y = 0). Le plus important a été manqué : dans les deux marchés 5m de 06:05 (1790402700, coupés à S−13 s), p_mid est figé à 0,485 et 0,405 à t = 30, 60, 150 et 240 s alors que y = 1. Le milieu d'avant l'ouverture est donc comparé à une juste valeur Binance à jour, ce qui gonfle l'avantage publié de la juste valeur. Le sens du biais est optimiste, pas incertain (voir new_issues). Dans le P&L, la troncature flatte aussi (b) (voir new_issues).
* **L'intervalle de confiance bootstrap ne repose que sur 8 grappes : il est trop étroit, et la seule affirmation « significative » ne l'est pas** (confirmé, gravité moyenne, optimiste) — `src/tradebot/polymarket_backtest.py:609`. Confirmé. Le regroupement se fait par créneau S//900, soit G = 8 créneaux (README § 0 : « 8 créneaux de 15 min »), et un bootstrap percentile sur 8 grappes a une couverture nettement inférieure à 95 %. Le seul IC du § 6 qui exclut 0 (5m, t = 30 : −0,065 ; −0,002) est choisi parmi 14 lignes. Il tombe à (−0,041 ; −0,001) quand on retire les deux marchés de 06:05 au carnet figé, et à (−0,034 ; +0,003) quand on retire les 5 marchés tronqués. Remarque : le § 0 (README ligne 16) cite en fait la ligne « toutes » (IC −0,053 ; +0,008), qui n'est pas significative, et en tire quand même « l'information Binance 1 s vaut quelque chose ».
* **Règle « choix sur la 1re moitié » : triple égalité tranchée par un tri non stable** (confirmé, gravité moyenne, incertain) — `scripts/polymarket_maker_live.py:262`. Confirmé dans resume_par_moitie.csv : 0.50/S+0, 0.50/S+30 et 0.50/S+60 valent tous 0,00000 c sur la 1re moitié (13 ordres chacun, P&L de ±0,5 par part, qui se compensent exactement). Le tri retient 0.50/S+30, dont le résultat sur la 2e moitié est −2,94 c. Les deux autres auraient donné +2,94 et 0,00 c.
* **Le rapport n'est pas reproductible : deux issues arrivées après le calcul décalent les résultats d'environ 1 c** (confirmé, gravité basse, incertain) — `src/tradebot/polymarket_maker_live.py:500`. Confirmé après reproduction exacte du README : le meta.json des deux marchés 1790417400 (10:10 UTC, et non 10:05) a été rafraîchi à 10:23 avec resolved_up = true. Avec ces deux issues, (b) 0.49/S+0 passe de −1,61 à −0,91 c, (a) 0.48/S+0 de −0,68 à −1,77 c, (a) 0.50/S+0 de +1,67 à +1,56 c ; (c) passe de −1,12 à −1,11 c. Le rapport lui-même dit que les marchés sans issue ne comptent pas pour le P&L : c'est reconnu mais non chiffré.
* **Le « P&L par part exécutée » n'est pas pondéré par la quantité exécutée** (confirmé, gravité basse, neutre) — `src/tradebot/polymarket_maker_live.py:516`. Confirmé : c'est une moyenne par ordre. L'effet est négligeable, car il n'y a que 16 exécutions partielles (13 en (c), 2 en (a), 1 en (b)) contre 897 complètes.
* **Le README annonce une période continue alors que la collecte a un trou de 4 h** (confirmé, gravité basse, neutre) — `reports/polymarket/maker_live/README.md:3`. Confirmé : le journal s'arrête à 06:04:49 (lignes 134–138) et reprend à 10:00:49 (ligne 139). Les 48 marchés couvrent 04:35–06:05 plus 10:05–10:10 (en heures d'ouverture). C'est un défaut de transparence plus qu'un biais de résultat : le « 8 créneaux » du § 0 est exact.
* **Les marchés tronqués flattent (b) et la meilleure configuration de (a) : on garde l'exécution du côté gagnant, et le côté perdant reste « expired »** (nouveau, gravité moyenne, optimiste) — `src/tradebot/polymarket_maker_live.py:272`. Le filtre « exploitable » ne vérifie que le carnet à S−60 s (scripts/polymarket_maker_live.py ligne 96). Dans eth-updown-5m-1790402700, coupé à S−13 s et résolu Up, (b) a exécuté les 3 achats Up à 0,49 (+0,51 par part), tandis que les 3 achats Down (perdants) restent « expired », avec un P&L nul, parce que les données s'arrêtent. Dans btc-…-1790402700, l'Up est exécuté et le Down « crossing ». En (a), les 15 ordres Down perdants de ces marchés sont « expired », dont celui d'eth à 0,50/S+0. Chiffré sur ordres.csv : en retirant les 5 marchés tronqués, (b) 0.49/S+0 passe de −1,61 à −3,66 c et 0.49/S+30/60 de −2,4/−1,7 à −4,15 c. En comptant les ordres « expired » de 06:05 comme exécutés (le Down perdait, donc ses bids ont probablement été touchés), (b) passe à −2,2 c et (a) 0.50/S+0 de +1,67 à 0,00 c. L'auditeur ne relève que les 15 ordres (a) et conclut à un effet « dans les deux sens ». Pour (b), le sens est clairement optimiste.
* **Le carnet figé de deux marchés coupés avant l'ouverture fabrique une partie de l'avantage publié de la juste valeur Binance à S+30 s** (nouveau, gravité moyenne, optimiste) — `src/tradebot/polymarket_book.py:433`. book_series prolonge le dernier état connu au-delà de la fin des données. Dans btc-updown-5m-1790402700 et eth-updown-5m-1790402700 (coupés à S−13 s, y = 1), p_mid vaut 0,485 et 0,405 à t = 30, 60, 150 et 240 s, figés avant l'ouverture, alors que p_fair utilise Binance à jour (0,72 et 0,60 à t = 30). Ces lignes entrent dans le § 6. En les retirant, ΔBrier 5m à t = 30 passe de −0,028 (IC −0,065 ; −0,002) à −0,019 (IC −0,041 ; −0,001) et « toutes » à t = 30 de −0,018 à −0,010 (IC −0,033 ; +0,008). En retirant les 5 marchés tronqués, 5m à t = 30 tombe à −0,015 (IC −0,034 ; +0,003). Un tiers à la moitié du seul résultat positif du rapport (« l'information Binance 1 s vaut quelque chose dans les premières secondes », README ligne 16) vient donc d'un artefact de collecte.
* **§ 6 : un milieu NaN quand un côté du carnet est vide retire à t = 240 s les 19 marchés déjà décidés** (nouveau, gravité basse, pessimiste) — `scripts/polymarket_maker_live.py:218`. Book.mid() renvoie NaN dès qu'un côté est vide, ce qui arrive quand le prix dépasse 0,99. À t = 240 s, 19 des 42 lignes 5m ont p_mid = NaN. Ce sont exactement les marchés où p_fair vaut ≈ 0 ou 1 et a raison. Le Brier à 240 s (n = 23, ΔBrier +0,044) ne porte donc que sur les marchés encore indécis, où la gaussienne est la plus faible. En les réintégrant avec un milieu ≈ 0,99/0,01, le ΔBrier se diluerait vers ≈ +0,025. L'affirmation « le carnet en sait plus que notre gaussienne » repose donc sur un sous-échantillon choisi selon l'état du marché.
* **Un quart des ordres (c) sont posés à plus de 0,10 du milieu, là où le format v2 n'enregistre plus les mises à jour du carnet** (nouveau, gravité basse, neutre) — `scripts/polymarket_live_collector.py:60`. 448 des 1 776 ordres (c) posés (25 %) ont leur niveau à plus de 0,10 du milieu au moment de la pose. À ces niveaux, la file n'est mise à jour que par les instantanés `book`, ce qui la surestime. Leur taux d'exécution est de 3 %, contre 39 % pour les autres. Ils diluent le P&L par part placée de (c) vers 0 : ≈ −1,5 c sur les seuls ordres proches, au lieu de −1,1 c. L'effet de la file périmée est pessimiste mais faible. L'effet de dilution rend le −1,1 c moins négatif qu'il ne l'est pour les ordres qui s'exécutent réellement.
* **Le retard de réception enregistré (« slow consumer », jusqu'à 14 s) n'est pas modélisé : la simulation décide sur l'horodatage serveur** (nouveau, gravité basse, incertain) — `logs/live_collector.log:126`. Le serveur a coupé la connexion pour saturation à S exactement. Sur plusieurs marchés, rx − ts a un p99 de 1,6 à 3,3 s et un maximum de 12 à 14 s. Le simulateur applique chaque événement à son ts serveur et pose l'ordre à t + 300 ms sur le carnet de ts ≤ t : un robot réel aurait vu un carnet en retard dans les rafales, justement autour de S. Le sens de l'effet est incertain : plus d'ordres (c) seraient croisants ou mal plafonnés, et les annulations arriveraient plus tard. La conclusion négative ne change pas.

## TimesFM : prévision d'amplitude, juste valeur en cours de fenêtre, filtre de régime (src/tradebot/timesfm_amplitude.py, scripts/timesfm_amplitude.py, reports/timesfm_amplitude/README.md)

**1. information future (look-ahead) : absent**

* `scripts/timesfm_amplitude.py:413` : `M["origin_s"] = ta.polymarket_origin(S)`
* `src/tradebot/timesfm_amplitude.py:523` : `"""Origine TimesFM « prévu à S » : ``S − 120 s`` (barre 1m close à ``S − 60 s``)."""`
* `scripts/timesfm_amplitude.py:434` : `M["sigma_pm_ewma_S"] = np.sqrt(pb.bar_lookup(rv, S - 120, "ewma"))`
* `scripts/timesfm_amplitude.py:418` : `M[f"px_k{k}"] = pb.bar_lookup(bars["btc"], S + 60 * k - 60, "close")`

Toutes les citations de l'auditeur existent mot pour mot. J'ai refait le chemin temporel. (a) twap60 est la moyenne des 60 closes 1s de la minute ouverte en S−60 (polymarket_backtest l. 233). La recherche en S−60 donne donc la fenêtre (S−60 s, S], connue à S. (b) px_k est la clôture de la barre 1m ouverte en S+60k−60, connue à S+60k. (c) L'EWMA à S+k lit la même barre. L'EWMA à S et TimesFM lisent la barre ouverte en S−120, close en S−60. (d) Au test 1, le contexte s'arrête à la barre t (context_windows, ts[p] == o). La cible est close[t+h]/close[t] et realized_variance_h commence à t+60. (e) Le signal reversal_h des runs existants est causal : backtest.py utilise past = fut.shift(h). (f) Les signaux hgb_ind1s / gap_m30 du test 3 viennent de modeles_vs_marche, dont le README indique un apprentissage sur les origines antérieures au 14/08 moins D moins 1 h. (g) λ du test 1 et du test 2, w, l'empilement, les bornes de quintiles, hist, encomp et encomp_q sont tous ajustés sur le train. La coupure du test 1 laisse environ 2 h entre la dernière origine d'apprentissage et la première de test, pour des cibles d'au plus 15 min : pas de chevauchement. Je n'ai trouvé aucun contre-exemple. La seule asymétrie d'information (d lu à S+k contre un point de marché vieux d'environ 46 s) avantage le nowcast face au marché. Elle ne touche ni la comparaison TimesFM/EWMA, ni les verdicts, qui la présentent comme non exploitable.

*Effet sur la conclusion :* Aucun look-ahead. Les verdicts (TimesFM ne bat pas l'EWMA, ne sert pas de filtre) ne changent pas.

**2. survivants : absent**

* `scripts/timesfm_amplitude.py:399` : `M = M[np.isfinite(M["y"])].reset_index(drop=True)`
* `scripts/timesfm_amplitude.py:514` : `ok = np.isfinite(te["p_market"]) & np.isfinite(te["p_timesfm_S"]) & np.isfinite(te["p_ewma_k"]) & np.isfinite(te["p_stack"])`
* `reports/timesfm_amplitude/README.md:21` : `(16 127 marchés, issue officielle `outcomePrices`, `priceToBeat`/`finalPrice` d'`eventMetadata`)`
* `reports/timesfm_amplitude/README.md:227` : `0 sans TimesFM : contexte troué`

L'univers Polymarket est la grille complète des marchés BTC 5m et 15m. Sur 42 jours, on attend (288 + 96) × 42 = 16 128 marchés et le README en indique 16 127 : un seul non résolu est écarté. Le filtre « même échantillon » du test 2 retire 11 marchés 5m et 1 marché 15m (6 037/6 048 et 2 015/2 016), faute de point de marché à moins de 90 s. Ce filtre dépend de la liquidité du carnet, pas de l'issue. Au test 1, BTC, ETH et SOL sont des actifs vivants, et sur 5 mois la sélection d'actifs survivants ne joue pas sur une prévision de volatilité à 15 min. Les origines à contexte troué sont écartées, mais leur nombre est nul en Polymarket, et au test 1 les 1 500 origines par actif sont reprises telles quelles des runs existants.

*Effet sur la conclusion :* Moins de 0,2 % des marchés sont exclus. Le verdict ne change pas.

**3. données futures dans un indicateur : absent**

* `src/tradebot/timesfm_amplitude.py:254` : `v = xi if not np.isfinite(v) else lam * v + (1.0 - lam) * xi`
* `src/tradebot/timesfm_amplitude.py:283` : `out[f"rstd{w}"] = s_r.rolling(w, min_periods=int(0.8 * w)).var(ddof=0).to_numpy()`
* `src/tradebot/timesfm_amplitude.py:492` : `"""Bornes intérieures (``n − 1`` quantiles) calculées sur l'apprentissage seulement."""`
* `scripts/timesfm_amplitude.py:545` : `edges = ta.quintile_edges(tr[col])`

Les citations sont exactes. J'ai cherché ailleurs : aucun bfill, center=True, merge_asof ni resample dans timesfm_amplitude.py, dans le script, dans forecaster.transform_context ou dans polymarket_backtest. clean_ohlcv ne comble aucun trou (« Les trous ne sont PAS comblés »). minute_aggregates_1s fait un ffill à l'intérieur de la minute, donc causal. Les rolling tournent sur un index trié. L'interpolation de context_windows ne comble que les trous internes au contexte, et la dernière barre est obligatoire. Les bornes de quintiles, l'ECDF, hist, encomp et encomp_q sont calculés sur l'apprentissage. Les déciles de plot_sigma_vs_realized sont faits sur le test, mais ne servent qu'au graphique descriptif, jamais à un signal.

*Effet sur la conclusion :* Pas de repainting. Le verdict ne change pas.

**4. frais et glissement : sans objet**

* `reports/timesfm_amplitude/README.md:5` : `> Simulation papier sur données publiques : aucune clé, aucun ordre. Les prix Polymarket ne sont montrés qu'en agrégé.`
* `reports/timesfm_amplitude/README.md:153` : `Cette comparaison ne dit rien d'exploitable ; elle sert de référence.`

Aucun P&L n'est calculé. Les scores sont QLIKE, MSE, Brier, log-loss et justesse. La seule supériorité affichée (nowcast contre marché, de −6 à −37 ×10⁻³ de Brier) est déclarée non exploitable. La recommandation au maker (dimensionner l'écart avec σ) n'est pas chiffrée en c/part. Les frais et le glissement ne sont donc pas en jeu.

*Effet sur la conclusion :* Sans objet. Toute monétisation future des écarts de Brier devrait intégrer frais et spread, mais le rapport n'en revendique aucune.

**5. prix jamais disponible : sans objet**

* `scripts/timesfm_amplitude.py:384` : `p, age = ta.market_price_at(hist, int(S) + 60 * k, staleness_s=90)`
* `reports/timesfm_amplitude/README.md:240` : `Le prix du marché est un milieu de fourchette `prices-history` daté à la minute (ancienneté moyenne ≈ 45 s, déjà en retard de ≈ 10 s sur Binance d'après le diagnostic) : battre ce point au même horoda`

Aucune exécution n'est simulée. Le milieu `prices-history` sert de référence de probabilité, pas de prix de remplissage. Son ancienneté (médiane de 45 à 47 s) désavantage le marché, ce que le rapport reconnaît. Ni profondeur, ni file, ni prix d'exécution n'interviennent.

*Effet sur la conclusion :* Sans objet pour les conclusions publiées.

**6. réglages sur tout l'échantillon : absent**

* `scripts/timesfm_amplitude.py:65` : `LAMBDA_GRID = (0.90, 0.94, 0.96, 0.97, 0.98, 0.99, 0.995)`
* `scripts/timesfm_amplitude.py:250` : `lam = ta.choose_ewma_lambda(r2, targets, LAMBDA_GRID)`
* `scripts/timesfm_amplitude.py:480` : `for w in np.linspace(0, 1, 11):`
* `scripts/timesfm_amplitude.py:483` : `g = g_all[g_all["train"] & np.isfinite(g_all["sigma_pm_timesfm"])]`

J'ai vérifié le décompte de l'auditeur, environ 130 nombres ajustés, tous sur l'apprentissage. Aucun n'est ajusté sur le test. Je réfute en revanche sa réserve sur λ_BTC « en bord de grille ». J'ai recalculé la QLIKE d'apprentissage de BTC (mêmes origines, mêmes cibles) : 1,7133 à λ = 0,99, 1,7065 à 0,995, 1,7194 à 0,997, 1,7374 à 0,998 et 1,7820 à 0,999. L'optimum 0,995 est donc intérieur, et élargir la grille n'améliorerait pas l'EWMA. Le vrai déséquilibre de réglage joue dans l'autre sens. L'EWMA reçoit un λ optimisé par QLIKE, alors que la conversion déciles → σ de TimesFM est une constante gaussienne fixe (Z = 2,5631), jamais recalée, alors que les rendements à 5–15 min ont des queues épaisses. C'est un biais pessimiste contre TimesFM : voir new_issues. L'ajout éventuel de encomp_q après coup reste invérifiable (un seul commit, 6b86433) et ne sert pas le verdict négatif.

*Effet sur la conclusion :* Pas de sur-ajustement sur le test : le verdict négatif n'est pas gonflé. Le seul défaut de réglage (σ gaussienne non recalée) pénalise TimesFM et ne peut que rapprocher TimesFM de l'EWMA, pas le faire passer devant.

**7. hausse et baisse (régimes) : présent**

* `reports/timesfm_amplitude/README.md:241` : `* **Un seul régime, 6 semaines** (TWAP-60 depuis le 14/08) ; le test 1 couvre 5 mois. Les IC groupés par créneau ne couvrent pas un changement de régime.`
* `reports/timesfm_amplitude/README.md:18` : `Coupure apprentissage / test à la médiane des origines (14/07/2026 23:22 UTC).`
* `scripts/timesfm_amplitude.py:227` : `split_s = int(np.median(all_o))`
* `scripts/timesfm_amplitude.py:1120` : `p.add_argument("--mid", default="2026-09-04", help="début du test Polymarket (fin de l'apprentissage)")`

J'ai recalculé sur data/cache/*_1m.parquet. L'apprentissage du test 1 va du 02/05 23:30 au 14/07 23:22. BTC y fait −17,6 % (l'auditeur donne −17,0 %, écart de point de départ), avec un drawdown de −30,1 % et une vol 1m annualisée de 43 %. ETH fait −18,8 % et SOL −8,0 %. Le test 1 va du 15/07 au 25/09 : BTC +29,6 % (64 908 → 84 100), drawdown −8,6 %, vol 35 % ; ETH +42,8 % ; SOL +57,2 %. Le test est donc uniquement haussier et plus calme que l'apprentissage. Polymarket : l'apprentissage (14/08–03/09) donne BTC +28,0 %, vol 42 %, vol journalière 2,76 %. Le test (04/09–24/09) donne +3,9 %, vol 36 %, vol journalière 2,32 %, drawdown −7,8 %. J'ai aussi vérifié ΔQLIKE(TimesFM − EWMA) sur la moitié d'apprentissage (baissière) : +0,046, +0,002 et +0,149 pour h = 5, 10, 15, contre +0,106, +0,195 et +0,202 sur le test. Le déficit de TimesFM est donc plus marqué dans le régime haussier et calme. Nuance : la variété de volatilité intrajournalière existe au test (σ de 3,9 à 23 pb entre quintiles en 5m), mais il n'y a qu'un régime macro par test.

*Effet sur la conclusion :* Le verdict « pas mieux que l'EWMA » tient dans les deux régimes. L'ampleur publiée, « un peu moins bien, QLIKE +9,5 % », dépend du régime (à peu près à égalité dans la moitié baissière) et de la conversion gaussienne (voir new_issues) : biais pessimiste sur l'ampleur, pas sur le sens. Les bornes de régime et le fichier maker sont calibrés sur un mois plus agité que le test.

**8. horloges et alignement : partiel**

* `reports/timesfm_amplitude/README.md:240` : `* **Polymarket** : d utilise Binance (spot et TWAP60 1s) alors que l'issue vient de Chainlink (≈ 3 pb de décalage de niveau, sans effet sur d puisque les deux termes sont Binance ; ≈ 4 s de retard du `
* `src/tradebot/polymarket_backtest.py:293` : `"""Valeurs de ``frame`` (index = horodatage d'ouverture) aux instants EXACTS ``times_s```
* `reports/polymarket/diagnostic.md:368` : `* Les points `prices-history` sont en retard d'environ 10 s sur l'information Binance (voir la robustesse du nowcast). Pour comparer un modèle au marché à la seconde près, il faut le carnet (`order_bo`
* `src/tradebot/timesfm_amplitude.py:121` : `tau = np.asarray(tau_min, dtype="float64") - twap_shift_min`

Côté Binance, l'alignement est correct : barres 1m et agrégats 1s en UTC, indexés à l'ouverture, avec des décalages explicites. Les q10/q90 recalculés reproduisent ceux des runs existants à 0,0000 pb près, donc la même convention d'origine. Trois défauts restent, tous communs aux modèles Φ(d/σ). (1) d et la TWAP60(S) sont sur Binance (S−60, S], alors que l'issue et priceToBeat viennent de Chainlink, en retard d'environ 4 s : la fenêtre effective est (S−64, S−4]. Rien n'est modélisé. (2) La variance restante utilise τ − 0,5, alors qu'une moyenne sur la dernière minute donne τ − 2/3. σ est surestimé d'environ 6 % à τ = 2, pour tous les modèles. (3) Je réfute le sous-point de l'auditeur sur la sémantique de l'horodatage `prices-history`. Le diagnostic du dépôt (nowcast_lag.csv : corrélation maximale quand le nowcast est calculé 10 s avant le point) montre que le point reflète un état ANTÉRIEUR à son horodatage. Il ne contient donc pas d'information future : aucun biais optimiste pour le marché.

*Effet sur la conclusion :* Ces défauts touchent de la même façon Φ(d/σ) avec σ_TimesFM ou σ_EWMA. L'écart TimesFM/EWMA (≤ 1 ×10⁻³ de Brier) n'en dépend pas. Le verdict ne change pas.

**Autres défauts confirmés**

* **Titre de graphique erroné : « IC contenant 0 » alors que 2 IC sur 3 excluent 0** (confirmé, gravité basse, optimiste) — `scripts/timesfm_amplitude.py:1212`. C'est confirmé. La branche else s'exécute dès que les 3 IC n'excluent pas tous 0 (l. 1205 et 1208). Le README l. 26 affiche « à peu près comme l'EWMA réalisée (ΔQLIKE +0,167, IC contenant 0) ». Or h = 10 : +0,195 [+0,066 ; +0,354] et h = 15 : +0,202 [+0,060 ; +0,364] excluent 0. Le résumé l. 9 utilise n_worse >= 2 et dit l'inverse.
* **Test 2 : la comparaison mise en avant oppose TimesFM à S et EWMA à S+k (informations inégales)** (confirmé, gravité basse, pessimiste) — `scripts/timesfm_amplitude.py:519`. C'est confirmé par le tableau du README (l. 103–138). La différence timesfm_S − ewma_S, calculée par ligne, vaut +0,52, +0,44, −0,08, +0,47, +0,10 et +0,36 ×10⁻³, ce qui correspond aux chiffres de l'auditeur. Seule 5m k = 1 reste significative, soit 1/6 contre 2/6 annoncés. Le README reconnaît d'ailleurs l. 153 que « L'EWMA à S (même information que TimesFM) et TimesFM à S sont au coude à coude ». Le biais pessimiste du titre est de 0,2 à 0,5 ×10⁻³ de Brier.
* **« σ calibrée » surévaluée : pente de Mincer-Zarnowitz 0,02–0,08 et σ aberrants jusqu'à 459 pb** (confirmé, gravité moyenne, optimiste) — `reports/timesfm_amplitude/README.md:32`. La citation de l'auditeur n'était pas exacte : le README utilise des espaces fines insécables (U+202F) comme séparateurs de milliers. Elle est corrigée ci-dessus. Sur le fond, c'est confirmé par test1_origines.csv : σ_TimesFM max = 266,5 / 373,0 / 459,0 pb au test (h = 5/10/15), contre 102,6 / 145,1 / 177,8 pour l'EWMA. Pentes de Mincer-Zarnowitz : 0,08 / 0,02 / 0,04 contre 0,63 / 0,54 / 0,52. Décile supérieur à h = 15 : 40,7 pb prévu contre 30,2 réalisé. Nuance : l'EWMA surestime aussi son décile supérieur (39,2 contre 31,9 à h = 15 ; 32,0 contre 27,6 à h = 10). Ce qui est propre à TimesFM, ce sont les valeurs aberrantes, qui écrasent la pente MZ et doublent presque le MSE.
* **Test 3 : absence de preuve lue comme preuve d'absence (retournement Binance regroupé)** (confirmé, gravité basse, pessimiste) — `reports/timesfm_amplitude/README.md:223`. J'ai recalculé les moyennes pondérées à partir du tableau l. 204–221 : Q1 48,8 %, Q2 48,0 %, Q3 49,1 %, Q4 53,4 %, Q5 53,5 %. Les chiffres de l'auditeur sont exacts. La tendance est monotone sur Binance mais absente sur Polymarket (hgb_ind1s 15m : −3,7 points). Le regroupement est post hoc et le test compte déjà environ 60 comparaisons sans correction : « trop fort » est juste, mais l'effet reste douteux. 53 % de justesse ne couvre de toute façon pas les frais, et σ_EWMA trie autant.
* **Contexte de 512 barres jamais réglé pour l'amplitude ; pas de comparateur saisonnier** (confirmé, gravité basse, pessimiste) — `scripts/timesfm_amplitude.py:1123`. Les faits sont confirmés : context_len par défaut 512 (environ 8,5 h), aucun essai à 1 024 ou 2 048, aucune EWMA avec profil diurne. L'ampleur est inconnue. Le constat joue contre TimesFM comme contre l'EWMA, puisque ni l'un ni l'autre n'a de saisonnalité.
* **Origines du test 1 décrites « toutes les 15 min » alors qu'elles sont espacées d'environ 2 h 20** (confirmé, gravité basse, neutre) — `reports/timesfm_amplitude/README.md:18`. C'est confirmé sur BTCUSDT_timesfm3_none_s15_c512_logprice_predictions.csv : 1 500 origines du 02/05 23:30 au 25/09 23:15, avec un écart médian de 2 h 15 (moyenne 2 h 20, de 2 h 15 à 2 h 30). Le texte est inexact. Chaque créneau de bootstrap de 15 min ne contient donc qu'une origine par actif, et l'effet sur les IC est faible.
* **Le fichier maker situe chaque marché dans la distribution d'un mois d'apprentissage plus agité** (confirmé, gravité basse, neutre) — `reports/timesfm_amplitude/README.md:169`. C'est confirmé. Effectifs 5m des quintiles au test : 1 102 / 1 648 / 1 635 / 980 / 683, pour 1 210 attendus ; Q5 15m = 213. L'apprentissage (vol journalière 2,76 %) est plus agité que le test (2,32 %). amplitude_quantile_train n'est pas stationnaire.
* **Conversion gaussienne déciles → σ non recalée : l'essentiel du « QLIKE +9,5 % » est un biais d'échelle, pas de discrimination** (nouveau, gravité moyenne, pessimiste) — `src/tradebot/timesfm_amplitude.py:56`. σ_TimesFM = (q90 − q10)/2,5631 suppose des queues normales. Avec les queues épaisses des rendements à 5–15 min, cette lecture sous-estime la variance, et la QLIKE pénalise fortement la sous-estimation. L'EWMA, elle, reçoit un λ optimisé par QLIKE sur l'apprentissage. J'ai ajusté un seul facteur d'échelle sur la moitié d'apprentissage, par QLIKE, sur s2_timesfm de test1_origines.csv : ×1,49 / ×1,28 / ×1,57 sur σ² pour h = 5/10/15. Appliqué au test, ΔQLIKE(TimesFM recalé − EWMA) vaut +0,015 [−0,068 ; +0,103], +0,083 [+0,001 ; +0,177] et +0,035 [−0,026 ; +0,089] (bootstrap par créneau, 2 000 tirages). Cela fait environ +2,5 % en moyenne au lieu de +9,5 %, et 1 IC sur 3 excluant 0 au lieu de 2. Même en recalant aussi l'EWMA (×1,21 / 1,02 / 1,14), l'écart vaut +0,022 / +0,086 / +0,054. Le verdict du README (l. 14 : « un peu moins discriminante qu'une EWMA à la minute (QLIKE +9,5 %, test 1) ») attribue à la discrimination ce qui relève surtout de l'échelle. La variante IQR aggrave le problème au lieu de le contrôler.
* **Titre de figure et affirmation « calibrée décile par décile » codés en dur, jamais testés** (nouveau, gravité basse, optimiste) — `scripts/timesfm_amplitude.py:1214`. Ce titre (README l. 93) est écrit quel que soit le résultat. Les données le contredisent en partie : décile supérieur de σ_TimesFM à h = 10, 33,0 pb prévu contre 25,7 réalisé ; à h = 15, 40,7 contre 30,2. Il sert d'appui au verdict « σ calibrée » et à la recommandation au maker. Le verdict l. 1246 code lui aussi en dur « couverture ≈ 0,79 pour 0,80 » : c'est numériquement juste aujourd'hui, mais ce ne serait pas recalculé si les données changeaient.
* **« donne le même classement » (QLIKE sur Σ r²_1min) codé en dur et inexact à h = 10** (nouveau, gravité basse, neutre) — `scripts/timesfm_amplitude.py:1067`. La phrase n'est pas calculée. À h = 10, le meilleur modèle en QLIKE est `ewma` (1,750 contre 1,785 pour encomp_q), mais en qlike_rv c'est `encomp_q` (0,487 contre 0,490). `timesfm` et `encomp` s'inversent aussi : 1,945 contre 1,883 en QLIKE, 0,600 contre 0,627 en qlike_rv. L'effet sur le verdict est faible : TimesFM reste derrière l'EWMA dans les deux cas.

## Temps de réaction : prix périmés, P&L selon la latence, réaction du carnet (src/tradebot/latency.py, scripts/latency_study.py, collecteurs ; conclusions reports/latence/README.md, version régénérée le 26/09 16:01, commit 02ae552)

**1. information future (look-ahead) : absent**

* `src/tradebot/latency.py:212` : `return np.searchsorted(np.asarray(times, dtype="float64"), np.asarray(query, dtype="float64"), side="right") - 1`
* `src/tradebot/latency.py:358` : `nB = np.clip(fl - (self.E - self.L), 0, self.L)`
* `src/tradebot/latency.py:360` : `sum_b = self._cB[nB] + (self.L - nB) * x`
* `src/tradebot/latency.py:640` : `jb = np.searchsorted(T, T - lookback_s, side="right") - 1`

Toutes les citations de l'auditeur sont exactes. J'ai cherché un contre-exemple dans tout le chemin de code, sans en trouver. (a) P(t) n'utilise que le dernier milieu bookTicker reçu à rx ≤ t et un σ EWMA causal, dont l'a priori vient de bougies 1 s des 3 h précédentes. Pour les moyennes TWAP, mean() ne somme que les points b ≤ floor(t + lag), qui valent Binance à b − lag ≤ t. (b) La consolidation des messages de même ts dans book_timeline garde l'état après le dernier message du groupe, horodaté à son propre rx : pas d'anticipation. (c) Le classement « saut » et le délai de 1 s (cooldown_mask, parcours chronologique qui garde le premier ordre) sont causaux. (d) Le filtre « un niveau encore vivant n'ouvre pas de nouvelle opportunité » se sert de t_end, une date future. Mais à T[i], la présence continue du niveau est déjà connue : c'est un simple dédoublonnage. Le seul effet vient d'un niveau censuré (live = inf), qui ne peut plus rouvrir : effet pessimiste et négligeable. (e) L'exécution a lieu à t0 + ℓ sur le carnet reçu à ce moment, avec une limite fixée à t0. Le cas ℓ = 0, où l'on exécute sur l'état qui a produit le signal, relève du point 5.

*Effet sur la conclusion :* Aucun biais d'anticipation mesurable ; le verdict ne change pas pour cette raison.

**2. survivants : absent**

* `scripts/latency_study.py:1423` : `if S - PRE_S < first_ok[asset] or S - 60 - abs(lag_s) < since or E > min(bn_end, until) - 2:`
* `scripts/latency_study.py:283` : `res["skip"] = "carnet vide"`
* `scripts/latency_study.py:430` : `gg = g0 if value == "markout" else g0[g0["pnl"].notna()]`
* `reports/latence/README.md:3` : `dont 22 résolus ; 0,86 h de marché.`

La citation de l'auditeur en README ligne 3 (« dont 21 résolus ») n'existe plus. Le rapport a été régénéré à 16:01 et dit maintenant « dont 22 résolus » : le marché btc-5m-1790421600 est désormais résolu, via gamma. Le reste est confirmé. list_markets contient 35 créneaux autour de la fenêtre. Seuls sont exclus ceux qui ne sont pas couverts : 10:30 (5m et 15m), car S − 90 < since, puis btc/eth-5m-1790421900 et btc-15m-1790421300, car E > bn_end. Les 22 marchés retenus sont tous analysés (run.json n_markets = 22) et aucun n'est sauté pour « carnet vide ». Les opportunités censurées et les ordres non exécutés (qty = 0) restent au dénominateur. ETH 15m est absent, mais parce que le collecteur ne l'a jamais enregistré (choix de l'univers, --series), pas parce qu'on aurait gardé les survivants. Un seul marché s'est reconnecté (btc-5m-1790420400 : 2 connect, 1 error) : il est gardé.

*Effet sur la conclusion :* Pas de biais du survivant ; le verdict ne change pas.

**3. données futures dans un indicateur : partiel**

* `scripts/latency_study.py:227` : `best = g.loc[g["corr"].idxmax()]`
* `scripts/latency_study.py:1410` : `lag_ms = float(row["lag_ms"].iloc[0]) if len(row) else 0.0`
* `scripts/latency_study.py:1469` : `mm = (tr_["rx"] - tr_["E"]).groupby((tr_["rx"] // 60) * 60).median()`
* `src/tradebot/latency.py:16` : `:func:`ewma_var_1s` : variance EWMA **causale** des rendements 1 s.`

Confirmé. Le retard Chainlink de 700 ms est l'argmax de la corrélation calculée sur toute l'heure testée, puis injecté dans P. La médiane par minute calendaire de rx − E ne sert qu'à take_after_move_ms, une statistique descriptive. Aucune autre statistique ne regarde vers l'avant : l'erreur de suivi n'est pas utilisée (moins de 10 marchés, donc repli sur 0,40 pb), le lissage isotone porte sur l'axe des latences, et il n'y a ni bfill ni resample centré. J'ai relancé le script dans le scratchpad sur la même fenêtre, et le run de base reproduit exactement le README. Valorisation à +10 s : avec lag = 0, +196 pUSD/h à 100 ms et démontré jusqu'à 300 ms ; avec lag = 4 s, +207 et 300 ms ; avec 0,7 s, +212 et 300 ms. Les chiffres de l'auditeur sont donc justes. Il a en revanche omis la sensibilité du P&L à l'issue officielle : démontré jusqu'à 1 000 ms avec 0,7 s, 750 ms avec lag = 0, mais seulement 100 ms avec lag = 4 s.

*Effet sur la conclusion :* Effet faible sur le chiffre principal (±10 % sur le P&L/h ; limite du gain démontré inchangée à 300 ms). Le chiffre « à l'issue, démontré jusqu'à 1 000 ms » n'est pas robuste au choix du retard : il tombe à 100 ms avec 4 s. Le verdict ne change pas.

**4. frais et glissement : partiel**

* `src/tradebot/latency.py:505` : `return a + fee_rate * a * (1.0 - a)`
* `src/tradebot/latency.py:770` : `qty = np.where(filled, np.minimum(np.nan_to_num(s, nan=0.0), max_shares), 0.0)`
* `src/tradebot/latency.py:772` : `mid_later = tl.mid_at(tf + markout_s)`
* `src/tradebot/latency.py:781` : `mk = np.where(filled, qty * (mid_side - cost), 0.0)`

Le diagnostic est juste. Les frais preneur 0,07·p(1−p) sont appliqués à chaque achat et correspondent au fee_schedule des meta.json (rate 0,07, exposant 1). L'achat se fait au meilleur ask, donc le spread est payé à l'entrée. En revanche, la valorisation à +10 s, qui fonde le verdict « atteignable », se fait au milieu du carnet sans coût de sortie. Les ordres de grandeur de l'auditeur viennent de l'ancien README (+3,8 c à 100 ms) et sont périmés ; je les ai recalculés sur les 80 ordres « saut » retenus. Avec une sortie au bid du côté et des frais preneur à la revente, le gain par part passe : de 8,70 à 6,85 c à 100 ms ; de 10,21 à 8,30 c à 200 ms ; de 6,76 à 4,86 c à 300 ms. Cela fait ≈ −1,9 c/part, soit −20 à −30 %, et non une division par 2. Sur le P&L/h à 300 ms : +91 → +65 pUSD/h, et la borne basse de l'IC (bootstrap par marché) passe de +26 à ≈ +1/+3. Aucun ordre n'est valorisé à l'issue dans ces mesures, puisqu'aucun n'a tf + 10 s ≥ E. La réserve (2) de l'auditeur, « concurrence : 41 % des niveaux consommés par d'autres », est surtout réfutée : le carnet historique vu à t + ℓ contient déjà les exécutions des preneurs appariés avant nous. Seul manque l'impact de nos 50 parts sur le comportement ultérieur des teneurs. Le plafond à la taille du meilleur niveau est prudent.

*Effet sur la conclusion :* Biais optimiste ≈ 1,9 c/part sur la valorisation. La limite du gain démontré reste à 300 ms, mais de justesse (borne basse ≈ +1 à +3 pUSD/h). Le verdict « atteignable » tient formellement, sans aucune marge. Contrairement à ce qu'écrit l'auditeur, la limite ne descend pas à 200 ms.

**5. prix jamais disponible : partiel**

* `src/tradebot/latency.py:405` : `"""(meilleur ask, taille) du jeton ``side`` (+1 Up, −1 Down) vus localement à ``t``."""`
* `src/tradebot/latency.py:762` : `tf = t0 + lat / 1e3`
* `reports/latence/README.md:10` : `gain démontré jusqu'à ≈ 326 ms`
* `scripts/polymarket_live_collector.py:96` : `if mid is not None and abs(float(c["price"]) - mid) > band + 1e-9:`

La citation de l'auditeur (README ligne 9, « jusqu'à ℓ = 100 ms ») n'existe plus : le README actuel dit 300 ms. Sa conclusion (« en retirant ℓ < 150 ms, plus aucun gain démontré ») est donc périmée. Dans la version actuelle, ℓ est défini comme le délai entre la détection et l'appariement. Le verdict compare ℓ + 26 ms (soit 326 ms) au plancher de 230 à 280 ms. Les points ℓ < 150 ms, physiquement impossibles, sont affichés mais ne fondent pas le verdict. (a) File d'attente : réfuté pour l'essentiel. Un preneur parti avant nous est apparié avant nous, et son trade apparaît dans le carnet vu à t + ℓ. (b) Niveaux fantômes : vérifié. 3 des 212 niveaux « saut » ont un ask local différent du meilleur ask annoncé par le serveur (+1, +3 et +14 c), et 1 seul des 80 ordres retenus est touché. (c) Défaut manqué par l'auditeur : le carnet local est plus en retard pendant les sauts. rx − ts médian des messages CLOB vaut −32 ms en moyenne sur l'heure. Dans les 500 ms qui suivent une détection « saut », il vaut −9,6 ms (p75 +69, p90 +139). Pour le dernier message reçu avant t + ℓ, la médiane vaut −19/−18 ms à 200/300 ms, et p75 +45/+67 ms. Un niveau vu à t + ℓ peut donc déjà avoir été annulé côté serveur. Le côté Binance, lui, ne bouge pas (rx − E proche des sauts : −6 à −7 ms, contre −7,4 ms en tout).

*Effet sur la conclusion :* Biais optimiste modéré. Le « temps réel » disponible est surestimé d'environ 15 ms en médiane et de 60 à 100 ms dans un quart des cas. Le « gain démontré jusqu'à ≈ 326 ms » se rapproche donc de ≈ 300 ms, alors que la marge au-dessus du plancher de 230 à 280 ms n'est que de 20 à 70 ms. Cela ne renverse pas le verdict à lui seul, mais le rend très fragile. Niveaux fantômes : moins de 2 % des ordres.

**6. réglages sur tout l'échantillon : partiel**

* `scripts/latency_study.py:41` : `SIGMA_HALFLIFE_S = 600.0         # demi-vie EWMA (meilleure log-loss historique, reports/polymarket/formule)`
* `scripts/latency_study.py:43` : `SIGMA_FACTOR = 1.40              # σ × 1,40 : calibration historique (rendements 1 s autocorrélés, même rapport)`
* `scripts/latency_study.py:45` : `SOURCE_NOISE_PB = 0.40           # erreur de suivi Chainlink − Binance de F − K par défaut (même rapport)`
* `scripts/latency_study.py:52` : `ASK_RANGE = (0.05, 0.95)         # asks retenus pour les opportunités (queues = surtout erreur de modèle)`

Le décompte de l'auditeur (≈ 16 paramètres) est juste. J'ai relancé les sensibilités ; le critère est la limite du gain démontré en valorisation à +10 s. σ × 1,0 : 200 ms au lieu de 300, et le P&L à l'issue n'est démontré à aucune latence (confirmé). Délai entre ordres : 0 s → 100 ms, 0,25 s → 200 ms, 0,5 à 10 s → 300 ms. Le choix de 1 s, fait après une revue, n'est donc pas ajusté finement, mais le passage de « un ordre par niveau » à « un ordre par saut » double la limite. ASK_RANGE (0 ; 1) → 300 ms, inchangé : l'hypothèse de l'auditeur (« probablement choisi après les résultats ») n'a aucun effet. Saut de 0,02 ou 0,005 → 300 ms ; à l'issue, 50 ms ou 1 000 ms. Lag de 0 ou 4 s → 300 ms. Par ailleurs, σ × 1,40 a été calibré avec Binance décalé de 4 s (reports/polymarket/formule/README.md ligne 9) et il est appliqué ici avec 0,7 s. Ce paramètre, incohérent, est aussi celui qui fait basculer le verdict. Avec σ × 1,0, la limite en temps réel vaut 200 + 26 = 226 ms, sous le plancher de 230 ms : le code écrirait pourtant « atteignable », parce que son seuil est 150 ms (voir new_issues).

*Effet sur la conclusion :* Le verdict « atteignable » dépend de σ × 1,40, un facteur calibré dans un autre cadre (décalage de 4 s). Avec σ × 1,0, la limite tombe à 226 ms en temps réel, sous le plancher physique : le verdict s'inverse. Le résultat reste robuste à ASK_RANGE, au seuil de saut, au retard Chainlink et au délai entre ordres dès 0,5 s. Le verdict n'est pas robuste.

**7. hausse et baisse (régimes) : présent**

* `reports/latence/README.md:3` : `dont 22 résolus ; 0,86 h de marché.`
* `reports/latence/README.md:18` : `* **Échantillon petit** : 0,9 h, 22 marchés (22 résolus) ; IC larges.`

Les citations de l'auditeur (« 21 résolus », ligne 17) viennent de l'ancien README. Dans le README actuel, c'est « 22 résolus », aux lignes 3 et 18. J'ai revérifié les faits sur le bookTicker en cache. BTC passe de 84 143,5 à 84 172,1 (+0,034 %), dans une fourchette de 84 122,1 à 84 200,0 ; l'écart-type des rendements vaut 0,154 pb sur 1 s et 1,44 pb sur 1 min. Sur l'année précédente (BTCUSDT_1m.parquet), l'écart-type horaire des rendements 1 min a une médiane de 4,45 pb : l'heure testée tombe au 3e centile. ETH fait −0,002 % sur la période (1,44 pb… plus exactement 2,04 pb d'écart-type 1 min, et non 2,2). C'est un samedi, sur 58 minutes : un seul régime, plat et extrêmement calme. Il n'y a ni tendance ni forte volatilité, et donc aucun contraste de régimes.

*Effet sur la conclusion :* Le sens du biais est incertain : plus de sauts en régime volatil, mais des teneurs peut-être plus prudents. Aucun chiffre (ℓ½ ≈ 240 ms, gain démontré jusqu'à 300 ms, « atteignable ») n'est généralisable au-delà de cette heure au 3e centile de volatilité. Le verdict positif n'est pas établi.

**8. horloges et alignement : partiel**

* `src/tradebot/latency.py:3` : `Données : les deux collecteurs temps réel, **même horloge locale** (``rx``, ns) :`
* `src/tradebot/latency.py:120` : ```book`` : ``rx`` (s), ``bid``, ``ask``, ``bid_sz``, ``ask_sz``, ``mid`` (bookTicker, sans horodatage`
* `scripts/latency_study.py:895` : `rel = d_bn - pm_med                                  # d_Binance − d_Polymarket (horloge annulée)`
* `scripts/latency_study.py:319` : `o["take_after_move_ms"] = (o["first_take_ts"] - (o["t_start"] - off)) * 1e3`

La citation README ligne 16 de l'auditeur (128 ms, 131 ms, 285 ms) n'existe plus : c'est maintenant la ligne 17, avec 133 ms (± 94), 134 ms et 284 ms. Les défauts qu'il décrit sont confirmés. (1) rel est mesuré sur l'aggTrade, alors que le signal vient du bookTicker, qui n'a pas d'horodatage. (2) take_after_move_ms mélange deux horloges serveur. (3) Le décalage d'horloge est mesuré après coup : 133 ms à 16:01, 160 ms à ma relance à 16:29. De plus, derive_horloge.csv montre une dérive de −39 ms de l'horloge locale sur l'heure (rx − E passe de +10 à −29 ms). Ces valeurs corrigées sont purement descriptives. Point plus important, manqué par l'auditeur : rel = d_B − d_P utilise la médiane globale de rx − ts du CLOB. Or, conditionnellement aux sauts, d_P est plus grand de 14 à 60 ms en moyenne, alors que d_B ne change pas. Le rel pertinent tombe à ≈ +11 ms en médiane, et devient négatif dans un quart des cas (voir le point 5). La dérive d'horloge (−39 ms/h) n'a pas d'effet sur le lag Chainlink, estimé avec une grille de 100 ms.

*Effet sur la conclusion :* La conversion en temps réel (ℓ + 26 ms) est optimiste d'environ 15 ms en médiane, et de plus de 60 ms dans un quart des sauts. Le « gain démontré jusqu'à ≈ 326 ms » se rapproche de ≈ 300 ms, au-dessus d'un plancher de 230 à 280 ms. Le verdict ne s'inverse pas à lui seul, mais la marge devient faible.

**Autres défauts confirmés**

* **IC bootstrap par marché alors que les marchés se chevauchent** (confirmé, gravité moyenne, optimiste) — `src/tradebot/latency.py:833`. Le défaut est réel : les 20 marchés « saut » se répartissent en 10 créneaux de 5 minutes, et BTC 5m, BTC 15m et ETH 5m sont simultanés. J'ai refait le bootstrap par créneau de 5 min sur les 80 ordres. Pour le P&L à l'issue, les bornes basses deviennent −39 pUSD/h à 200 ms et −69 à 300 ms (contre +28 et +20 par marché) : le « gain démontré jusqu'à 1 000 ms » à l'issue tombe à 150 ms. Pour la valorisation à +10 s, qui porte le verdict, le groupement par créneau ne change rien (borne basse +35 à 300 ms, contre +26). La prédiction de l'auditeur (« limite probablement sous 300 ms ») n'est donc pas vérifiée pour le chiffre principal. Avec seulement 10 grappes, un bootstrap par percentiles reste cependant sous-couvrant.
* **P&L réalisé très supérieur à l'avantage attendu** (confirmé, gravité haute, optimiste) — `reports/latence/pnl_vs_latence.csv:242`. La ligne 122 citée par l'auditeur vient de l'ancien CSV ; la bonne est la ligne 242. À ℓ = 0 : P&L à l'issue +20,3 c/part et valorisation +8,5 c/part (ma relance), pour un avantage attendu selon la formule de 2,80 c/part. Le chiffre à l'issue est donc surtout du bruit. Il est aussi concentré : à 200 ms, btc-15m-1790420400 et btc-5m-1790419200 font 151 des 262 pUSD. Même la valorisation vaut trois fois l'avantage attendu.
* **Tests multiples non corrigés, sous-ensemble positif choisi après coup** (confirmé, gravité moyenne, optimiste) — `scripts/latency_study.py:424`. La multiplicité des tests est réelle : 12 latences × 3 marges × 4 origines × 2 valorisations × 2 modes de comptage, sans correction. Mes relances montrent toutefois que la limite pour « saut » ne dépend pas finement du seuil de saut (300 ms avec 0,005, 0,01 et 0,02) ni d'ASK_RANGE. Le risque vient donc surtout du choix de l'origine et de la valorisation, pas d'un réglage fin.
* **Verdict tenu par un seul point de grille** (confirmé, gravité moyenne, incertain) — `scripts/latency_study.py:952`. Le contexte a changé : le verdict repose maintenant sur le saut de grille de 300 à 500 ms (borne basse +26 à 300 ms, −28 à 500 ms), et non plus sur 100 → 150 ms. last_significant_latency ne renvoie que des points de la grille, et « 326 ms » veut seulement dire « quelque part entre 326 et 526 ms, ou moins si la borne basse à 300 ms est un coup de chance ». Le seuil du test est de plus faux : voir new_issues.
* **Filtre ± 0,10 : niveaux fantômes** (confirmé, gravité basse, optimiste) — `scripts/polymarket_live_collector.py:96`. Vérifié : 3 des 212 niveaux « saut » ont un ask local différent du meilleur ask annoncé par le serveur (+0,01, +0,03 et +0,14), dont 1 seul parmi les 80 ordres retenus. L'effet est de l'ordre de 1 % du P&L.
* **Parts achetées non retirées du carnet simulé** (confirmé, gravité basse, optimiste) — `src/tradebot/latency.py:724`. Le délai de 1 s limite le double comptage sans le supprimer, pour les niveaux qui vivent plus de 1 s. L'effet est faible : les résultats sont identiques avec un délai de 1, 2 ou 5 s (300 ms, +84 à +94 pUSD/h à 300 ms).
* **σ × 1,40 calibré avec un décalage de 4 s** (confirmé, gravité moyenne, optimiste) — `scripts/latency_study.py:43`. Confirmé par reports/polymarket/formule/README.md ligne 9 : « Avec σ × 1,40, Binance décalé de 4 s ». Ici, le décalage est de 0,7 s. La sévérité est sous-estimée par l'auditeur : avec σ × 1,0, la limite tombe à 200 ms (226 ms en temps réel), sous le plancher physique de 230 ms, et le verdict « atteignable » s'inverse.
* **Le verdict « atteignable » compare au délai de 150 ms, pas au plancher physique de 230 à 280 ms** (nouveau, gravité moyenne, optimiste) — `scripts/latency_study.py:958`. Le code écrit « atteignable » dès que t_sig ≥ 150 ms (ligne 952), alors que le README lui-même dit qu'un preneur ne descend pas sous ≈ 230 ms. Avec σ × 1,0, par exemple, t_sig = 226 ms et le rapport écrirait quand même « atteignable ». Avec les paramètres publiés (326 ms), le texte reste cohérent, mais la règle de décision est trop permissive d'environ 80 à 130 ms.
* **Conversion en temps réel fondée sur le délai médian global du CLOB, alors que le carnet est plus en retard pendant les sauts** (nouveau, gravité moyenne, optimiste) — `scripts/latency_study.py:893`. J'ai mesuré sur les 80 ordres retenus. rx − ts vaut −32 ms en médiane globale, mais −9,6 ms en médiane dans les 500 ms qui suivent un saut (p75 +69, p90 +139). Pour le dernier message reçu avant t + 300 ms : médiane −18 ms, p75 +67 ms. Côté Binance, pas de changement (−6 à −7 ms). Pendant les sauts, le carnet vu d'ici a donc 15 à 100 ms de retard de plus que ce que suppose rel = 26 ms. Des prix périmés « encore présents » localement sont déjà annulés côté serveur, et le temps réel de ≈ 326 ms est surestimé.
* **Carnet local non recoupé avec le meilleur bid/ask du serveur** (nouveau, gravité basse, optimiste) — `src/tradebot/polymarket_book.py:299`. Chaque price_change transporte le meilleur bid/ask du serveur (_bb, _ba), mais ces valeurs sont ignorées. Les niveaux fantômes, laissés par le filtre ± 0,10 ou par des messages perdus, ne sont donc ni détectés ni corrigés. C'est la cause des 3 niveaux fantômes sur 212 : une vérification simple (ask local = _ba) les éliminerait.
* **Le gain démontré à l'issue (jusqu'à 1 000 ms) ne résiste pas à un bootstrap par créneau** (nouveau, gravité moyenne, optimiste) — `reports/latence/README.md:13`. Avec un rééchantillonnage par créneau de 5 min (10 grappes), au lieu des 20 marchés supposés indépendants, les bornes basses du P&L à l'issue deviennent −39 pUSD/h à 200 ms et −69 à 300 ms. La limite du gain démontré passe de 1 000 à 150 ms. Le chiffre à l'issue, présenté comme une confirmation, n'en est pas une.

## Le code de l'article « quant stack »

Un agent a exécuté le code publié et testé ses affirmations ; un second a tenté de réfuter chaque défaut par sa propre expérience.

| id | fonction | défaut | gravité | contre-vérification |
|---|---|---|---|---|
| H1a | `deflated_sharpe` | Mélange d'unités : Sharpe ANNUALISÉ comparé à E[max] en unités d'écart-type (√V[SR_n] omis) puis multiplié par √(n_obs−1) avec n_obs quotidien -> fonction en marche d'escalier au seuil fixe SR_annuel ≈ 2,45, indépendant de la durée | haute | confirmé |
| H1b | `deflated_sharpe` | n_trials = 1 donne expected_max = −inf, donc DSR = 1 et PASS même pour un Sharpe négatif ; n_trials = 0 lève ZeroDivisionError | haute | confirmé |
| H1c | `deflated_sharpe` | Le dénominateur non normal utilise le Sharpe annualisé : (γ4−1)/4·SR² est gonflé d'un facteur 365 | moyenne | confirmé |
| H2 | `walk_forward / metrics` | Avec les valeurs par défaut (test_days = 60), metrics exige 100 observations : chaque pli renvoie {'error': ...} et df['sharpe'] lève KeyError | haute | confirmé |
| H3a | `walk_forward` | signal_fn n'est appliqué qu'au segment de test : la chauffe des indicateurs est perdue, et toute normalisation sur le segment regarde le futur (fuite que shift(1) n'empêche pas) | haute | confirmé |
| H3b | `walk_forward` | Chaque pli est rebacktesté à partir d'une position nulle : la première barre de chaque pli est perdue, l'entrée est facturée mais pas la sortie de fin de pli | basse | confirmé |
| H4a | `backtest / Config` | fee_bps est commenté « round-trip » mais facturé à chaque variation de position : un aller-retour coûte 16 pb au lieu de 5 + slippage | moyenne | confirmé |
| H4b | `backtest` | position × log-rendement puis exp(cumsum) : P&L faux pour les positions fractionnaires ou short (écart (w²−w)·σ²/2 par an), fortement OPTIMISTE pour les shorts | haute | confirmé |
| H4c | `backtest` | Aucun coût de portage : pas de funding sur les perpétuels, pas de coût d'emprunt pour un short spot, pas de rémunération du cash | moyenne | confirmé |
| H5 | `health_check` | Sharpe sur 30 jours comparé à 0,5 × Sharpe du backtest : fausse alerte SHARPE_DECAY 41 % des jours et HALT quasi certain en quelques jours pour une stratégie parfaitement conforme | haute | confirmé |
| H8a | `metrics` | longest_dd_days compte des barres et non des jours ; periods_per_year = 365 est codé en dur (et np.sqrt(365) dans health_check) | moyenne | confirmé |
| H8b | `metrics` | Sharpe et ann_return calculés sur des log-rendements : le Sharpe est sous-estimé d'environ σ/2 (≈ 0,3 à 60 % de vol) et ann_return (log) est présenté comme un % | basse | confirmé |
| H8c | `metrics` | Le capital initial n'est pas pris comme premier pic : une perte dès la première barre n'est pas comptée dans le drawdown | basse | confirmé |
| H8d | `position_size` | Le plafond de 20 % du notionnel rend le risque effectif < 1 % pour tout stop à moins de 5 % ; pas de contrôle du sens du stop ; loss_if_stopped ignore gap, frais et slippage ; position_size n'est pas relié au backtest | basse | confirmé |

**H1a — Mélange d'unités : Sharpe ANNUALISÉ comparé à E[max] en unités d'écart-type (√V[SR_n] omis) puis multiplié par √(n_obs−1) avec n_obs quotidien -> fonction en marche d'escalier au seuil fixe SR_annuel ≈ 2,45, indépendant de la durée**

article.py:77-79 `expected_max = (` / `(1 - euler) * norm.ppf(1 - 1/n_trials)` / `+ euler * norm.ppf(1 - 1/(n_trials * np.e))` ; article.py:85 `(sharpe - expected_max) * np.sqrt(n_obs - 1) / denom`. Exécution (h1.py) : expected_max(N=80) = 2,451 quel que soit n_obs. Escalier avec n_obs = 1825 : DSR = 0,000 à SR 2,3 ; 0,013 à 2,35 ; 0,134 à 2,40 ; 0,491 à 2,45 ; 0,848 à 2,50 ; 0,999 à 2,60 (transition large d'environ 0,1 Sharpe alors que l'erreur type réelle d'un Sharpe annualisé sur 5 ans est 0,45). Bruit pur (N = 80 essais, 5 ans quotidiens, 500 simulations) : meilleur Sharpe annualisé moyen 1,09 ; article PASS 0 %, DSR article aux percentiles 5/50/95 = 0/0/0 ; version correcte (Sharpe par période, SR0 = √V[SR_n]·E[max], PSR avec T) PASS 0 %, DSR médian 0,48. Une vraie stratégie noyée parmi 79 de bruit (300 simulations) : SR vrai 1,5 -> PASS article 1 % contre 14 % pour la version correcte ; SR 2,0 -> 9 % contre 52 % ; SR 2,5 -> 44 % contre 90 % ; SR 3,0 -> 84 % contre 99 %. Seuil de bruit correct en Sharpe annualisé selon la durée : 2,45 (1 an), 1,73 (2 ans), 1,42 (3 ans), 1,10 (5 ans), 0,78 (10 ans) ; celui de l'article reste à 2,45 partout. Si n_obs est donné en années (ambiguïté de la docstring), SR 1,8 -> DSR 0,211, SR 5 -> 0,917 : on n'obtient jamais PASS.

*Correction :* Travailler en Sharpe PAR PÉRIODE : sr = sharpe_ann/√ppy ; SR0 = √(var des Sharpe par période des N essais) × E[max] (ou √(1/T) sous H0) ; z = (sr − SR0)·√(T−1)/√(1 − γ3·sr + (γ4−1)/4·sr²) avec T le nombre de périodes ; exiger en argument la variance des Sharpe des essais.

*Contre-vérification :* Script contre/c1.py (copie vérifiée identique de l'article : contre/art.py). expected_max(N=80) = 2,4511, identique pour n_obs = 30, 365, 1825, 3650 (article.py:77-79). Escalier avec n_obs = 1825 reproduit à l'identique : SR 2,30 -> 0,000 ; 2,35 -> 0,013 ; 2,40 -> 0,134 ; 2,45 -> 0,491 ; 2,50 -> 0,848 ; 2,60 -> 0,999, alors que l'erreur type d'un Sharpe annualisé sur 5 ans vaut √(365/1825) = 0,447. Bruit pur (80 essais, 1825 j, 500 sim.) : meilleur Sharpe moyen 1,085 ; article PASS 0 %, DSR p5/p50/p95 = 0/0/0 ; version correcte (Sharpe par période, SR0 = √V[SR]·E[max]) PASS 0,2 %, DSR médian 0,475. Vraie stratégie + 79 bruits (300 sim.) : SR 1,5 -> PASS article 1,3 % contre 15 % ; 2,0 -> 12 % contre 52 % ; 2,5 -> 49 % contre 90 % ; 3,0 -> 89 % contre 99 % (écarts de quelques points avec l'analyste = bruit Monte-Carlo, même conclusion). Seuil correct selon la durée : 2,45 (1 an), 1,73 (2), 1,42 (3), 1,10 (5), 0,78 (10). Avec n_obs en années (5) : SR 1,8 -> 0,211 ; SR 5 -> 0,917, jamais PASS. Nuance sur la conclusion de l'article (« 80 variantes, un Sharpe de 1,8, presque sûrement pas réel ») : le DSR correct pour SR 1,8 vaut 0,258 (1 an), 0,747 (3 ans), 0,942 (5 ans), 0,999 (10 ans). L'affirmation ne tient donc que pour un historique court. Au-delà d'environ 1 an, la fonction de l'article est PESSIMISTE : elle masque de vrais avantages.

**H1b — n_trials = 1 donne expected_max = −inf, donc DSR = 1 et PASS même pour un Sharpe négatif ; n_trials = 0 lève ZeroDivisionError**

article.py:78 `(1 - euler) * norm.ppf(1 - 1/n_trials)` : norm.ppf(0) = −inf. Exécution : deflated_sharpe(-2.0, 1, 1825) -> {'deflated_sharpe': 1.0, 'expected_max_from_noise': -inf, 'verdict': 'PASS'} ; deflated_sharpe(0.1, 1, 30) -> PASS ; deflated_sharpe(1, 0, 100) -> ZeroDivisionError. Ce défaut est OPTIMISTE : un essai unique à Sharpe −2 est « validé ».

*Correction :* Pour n_trials == 1, poser SR0 = 0 (le DSR se réduit au PSR) ; refuser n_trials < 1 avec une exception explicite.

*Contre-vérification :* contre/c1b.py : deflated_sharpe(-2.0, 1, 1825) -> {'deflated_sharpe': 1.0, 'expected_max_from_noise': -inf, 'verdict': 'PASS'} ; (0.1, 1, 30) -> PASS ; (-5, 1, 10) -> PASS ; deflated_sharpe(1, 0, 100) -> ZeroDivisionError ; témoin n_trials = 2 : Sharpe −2 -> REJECT (E[max] = 0,52). Cause : article.py:78 `(1 - euler) * norm.ppf(1 - 1/n_trials)` donne norm.ppf(0) = −inf. Biais OPTIMISTE total mais limité au cas n_trials = 1. Il n'affecte pas l'exemple de l'article (80 essais).

**H1c — Le dénominateur non normal utilise le Sharpe annualisé : (γ4−1)/4·SR² est gonflé d'un facteur 365**

article.py:82 `1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe**2`. Avec SR = 1,8 annualisé et γ4 = 3 : 1 + 0,5·3,24 = 2,62, soit un dénominateur √2,62 = 1,62 au lieu d'environ 1,002 avec le Sharpe quotidien (1,8/√365 = 0,094). Avec skew ≠ 0, le terme −skew·SR est lui aussi 19 fois trop grand et peut rendre la racine négative (NaN) dès que skew·SR > 1.

*Correction :* Utiliser le Sharpe par période dans le dénominateur, comme dans Bailey et López de Prado (2014).

*Contre-vérification :* contre/c1b.py : avec SR = 1,8 et γ4 = 3, le dénominateur (article.py:82) vaut √2,62 = 1,619, contre 1,002 avec le Sharpe quotidien 1,8/√365. Le terme en skew est gonflé d'un facteur √365 = 19,1. Réserve sur l'affirmation « NaN dès que skew·SR > 1 », qui est INEXACTE : skew = 1, SR = 1,8, γ4 = 3 donne 1 − 1,8 + 1,62 = 0,82 > 0 et un résultat fini (DSR 0, REJECT). La racine ne devient négative que si skew² > γ4 − 1 (pour la valeur de SR la plus défavorable) : NaN obtenu avec skew = 2, SR = 3, γ4 = 3, ou skew = 1, SR = 3, γ4 = 1,2. Avec les kurtosis typiques des cryptos (γ4 ≈ 10), il faudrait skew > 3. Effet sur le verdict : le dénominateur ne déplace pas le seuil de 2,45, il élargit seulement la transition (SR 2,6 : 0,999 avec γ4 = 3, 0,943 avec γ4 = 10). Le défaut est donc réel mais secondaire par rapport à H1a.

**H2 — Avec les valeurs par défaut (test_days = 60), metrics exige 100 observations : chaque pli renvoie {'error': ...} et df['sharpe'] lève KeyError**

article.py:94 `train_days=180, test_days=60):` ; article.py:45 `if len(r) < 100:` ; article.py:115 `'mean_sharpe': round(df['sharpe'].mean(), 2),`. Exécution (h2.py) sur 3 ans de prix quotidiens simulés : walk_forward(prices, strat_ma, cfg) -> KeyError('sharpe') ; metrics sur 60 observations -> {'error': 'insufficient_data'}. Même KeyError si len(prices) < 240 (aucun pli, DataFrame vide). Le code de l'article ne s'exécute donc pas tel quel.

*Correction :* Abaisser le minimum de metrics (ou le rendre paramétrable), ou choisir test_days ≥ 100 par défaut. Mieux : concaténer les rendements hors échantillon de tous les plis et calculer des métriques agrégées, en gérant explicitement les plis en erreur.

*Contre-vérification :* contre/c2.py (3 ans de prix quotidiens simulés, stratégie MA50) : walk_forward avec test_days = 60 -> KeyError 'sharpe' ; avec test_days = 99 -> KeyError ; avec 100 -> OK (mean_sharpe −0,44, 3/9) ; avec 120 -> OK. Moins de 240 barres avec test_days = 120 -> KeyError 'sharpe'. metrics sur 60 observations -> {'error': 'insufficient_data'}. Cause : article.py:45 `if len(r) < 100:` combiné à article.py:94 `train_days=180, test_days=60):` et à article.py:115. Avec ses valeurs par défaut, le code de l'article ne s'exécute pas.

**H3a — signal_fn n'est appliqué qu'au segment de test : la chauffe des indicateurs est perdue, et toute normalisation sur le segment regarde le futur (fuite que shift(1) n'empêche pas)**

article.py:105 `signal = params['signal_fn'](test)   # apply to unseen data`. Exécution (h2.py) : MA50 appliquée sur un pli de test de 120 j -> position non nulle sur 70/120 barres, contre 119/120 si l'on garde l'historique (41 % du pli reste plat, le signal NaN étant mis à 0 par fillna). Fuite : sur une marche aléatoire (vol 60 %, 200 simulations de 4 ans, train 180 j / test 120 j), un signal z-score calculé avec la moyenne et l'écart-type du segment de test donne un mean_sharpe moyen de 2,25, P(mean_sharpe > 1) = 0,99, et 95,4 % de plis positifs. La même idée en version causale (expanding) donne −0,00. Le shift(1) de la ligne 22 est bien présent.

*Correction :* Calculer le signal sur prices.iloc[:fin_test] (train et test avec historique), puis découper le segment de test ; imposer que signal_fn soit causal (rolling/expanding) et que les paramètres de normalisation viennent du train.

*Contre-vérification :* contre/c3.py. Chauffe : signal sign(prix − MA50) sur des plis de test de 120 j (10 plis) -> position non nulle sur 700/1200 barres (58 %) quand le signal est calculé sur le seul segment de test, contre 1190/1200 (99 %) avec l'historique. Sur le premier essai de contre/c2.py, le signal booléen (x > MA) donnait 31/120 dans les deux cas : le NaN de la MA y devient False, donc 0, et le pli était baissier. La perte dépend donc du signal. Fuite : marche aléatoire à 60 % de vol, 200 simulations de 4 ans, train 180 j / test 120 j, signal clip(−(x − moyenne_test)/écart-type_test) -> mean_sharpe moyen 2,21, P(mean_sharpe > 1) = 1,000, 94,6 % de plis positifs ; la version causale (expanding) donne −0,03, P > 1 = 0,03, 49,2 % de plis positifs. Le shift(1) (article.py:22) n'empêche pas cette fuite. Réserve : la fuite suppose un signal_fn qui normalise sur tout le segment. Le code la permet sans la provoquer ; la perte de chauffe, elle, est systématique.

**H3b — Chaque pli est rebacktesté à partir d'une position nulle : la première barre de chaque pli est perdue, l'entrée est facturée mais pas la sortie de fin de pli**

article.py:22 `position = signal.shift(1).fillna(0).clip(` appliqué pli par pli (article.py:106 `bt = backtest(test, signal, cfg)`) ; `returns` du premier jour mis à 0 par fillna (article.py:26). Environ 1 barre sur 60 perdue par pli et un coût d'entrée de 8 pb par pli : effet faible, de l'ordre de 0,1 à 0,3 % par an.

*Correction :* Faire tourner un seul backtest sur la série de signaux hors échantillon concaténée.

*Contre-vérification :* contre/c4.py (10 ans simulés, vol 60 %, dérive 50 %/an, 100 sim.). Le signal est calculé avec l'historique pour isoler l'effet du redémarrage à chaque pli. Écart log/an entre les plis concaténés et un backtest continu : toujours long −1,34 % (test 60 j) et −0,64 % (120 j) ; MA50 −0,26 % et −0,30 % ; MA10 −0,37 % et −0,17 %. Mécanisme vérifié : le rendement entre la dernière barre du train et la première du test n'existe pas dans le pli (article.py:26, fillna(0)), et l'entrée est facturée 8 pb à chaque pli (article.py:22, 29-30) sans facturer de sortie. Le biais est PESSIMISTE et faible, mais l'ordre de grandeur annoncé (0,1 à 0,3 %/an) est sous-estimé pour une stratégie toujours investie avec une forte dérive (environ 1,3 %/an avec des plis de 60 j).

**H4a — fee_bps est commenté « round-trip » mais facturé à chaque variation de position : un aller-retour coûte 16 pb au lieu de 5 + slippage**

article.py:9 `fee_bps: float = 5.0        # round-trip exchange fee` ; article.py:29-30 `turnover = position.diff().abs().fillna(0)` / `costs = turnover * (cfg.fee_bps + cfg.slippage_bps) / 1e4`. Exécution (h4.py) : entrée 0->1 puis sortie 1->0 -> coûts 8 + 8 = 16,0 pb ; séquence +1 -> −1 -> 0 -> 32 pb. Si 5 pb est vraiment un aller-retour, l'aller-retour devrait coûter environ 5 + 2×3 = 11 pb : les frais sont comptés deux fois, soit un biais PESSIMISTE d'environ 5 pb par aller-retour (≈ 18 %/an pour une stratégie qui retourne sa position chaque jour).

*Correction :* Soit renommer le paramètre en frais par côté (fee_bps_per_side), soit facturer fee_bps/2 + slippage par unité de turnover. Documenter l'unité.

*Contre-vérification :* contre/c5.py : un aller-retour 0 -> 1 -> 0 coûte 16,0 pb et la séquence +1 -> −1 -> 0 coûte 32,0 pb (article.py:29-30). Ce comptage contredit le commentaire article.py:9 `fee_bps: float = 5.0        # round-trip exchange fee`. Si 5 pb est un aller-retour, le coût correct est 5 + 2×3 = 11 pb : excès de 5 pb par aller-retour, soit ≈ 18 %/an pour une stratégie qui passe de +1 à −1 chaque jour (16 pb contre 11 pb par jour). Réserve : 5 pb par côté correspond au tarif taker des contrats à terme USDⓈ-M de Binance. Si le commentaire est faux et le code juste, il n'y a pas de biais. L'incohérence entre commentaire et code est certaine ; le sens du biais dépend du barème réel.

**H4b — position × log-rendement puis exp(cumsum) : P&L faux pour les positions fractionnaires ou short (écart (w²−w)·σ²/2 par an), fortement OPTIMISTE pour les shorts**

article.py:26-27 `returns = np.log(prices / prices.shift(1)).fillna(0)` / `gross = position * returns` ; article.py:33 `equity = cfg.initial_capital * np.exp(net.cumsum())`. Exécution sur un BTC simulé (vol 60 %/an, 20 ans), écart code − vrai P&L (log/an, poids constant rebalancé chaque jour) : w = +1 -> 0,000 ; w = +0,5 -> −0,045 (pessimiste) ; w = +0,25 -> −0,034 ; w = −0,5 -> +0,134 ; w = −1 -> +0,364 (optimiste de 36 points log par an, conforme à la théorie σ² = 0,36). Avec une dérive nulle, le code affiche un short à +12,9 %/an alors que le vrai short rebalancé fait −23,6 %/an. Données réelles (cache BTCUSDT, 2025-09-25 -> 2026-09-25, BTC −22,8 %, vol 45 %) : short −1 selon le code = +0,258 log (Sharpe 0,57), contre +0,057 pour le short rebalancé chaque jour et +0,206 pour le short sans rebalancement.

*Correction :* Utiliser des rendements simples : R = prices.pct_change() ; net = position·R − coûts ; equity = capital·(1 + net).cumprod(). Pour un poids constant, facturer aussi le rebalancement dû à la dérive du poids.

*Contre-vérification :* contre/c5.py (vol 60 %, 20 ans, frais nuls), écart log/an entre le code et le vrai P&L à poids constant rebalancé : w = 1 -> 0,000 ; w = 0,5 -> −0,045 ; w = 0,25 -> −0,034 ; w = −0,5 -> +0,137 ; w = −1 -> +0,365 (théorie (w² − w)σ²/2 = 0,36). Résultats identiques avec une dérive de 0,5. Données réelles du cache BTCUSDT_1m clôturé chaque jour du 2025-09-25 au 2026-09-25 (366 barres, BTC −22,8 %, vol 45,1 %) : short −1 selon le code +0,259 log (Sharpe metrics 0,57) ; short rebalancé chaque jour +0,057 ; short sans rebalancement +0,209. Le calcul du code (article.py:26-27, 33) est OPTIMISTE pour les shorts et pessimiste pour les positions longues fractionnaires ; il est exact pour w = +1.

**H4c — Aucun coût de portage : pas de funding sur les perpétuels, pas de coût d'emprunt pour un short spot, pas de rémunération du cash**

article.py:32 `net = gross - costs` (seuls les coûts de turnover). Cache BTCUSDT_derivs (2025-09-01 -> 2026-08-31, 1095 relevés) : funding moyen 3,07e-5 par 8 h, soit 3,4 %/an payés par les longs aux shorts. Sur perpétuels, l'omission est donc OPTIMISTE pour les longs (≈ −3,4 %/an manquants) et PESSIMISTE pour les shorts (≈ +3,4 %/an manquants). Pour un short spot, le coût d'emprunt omis est optimiste.

*Correction :* Ajouter une série de funding (ou de taux d'emprunt) : net -= position × funding_rate cumulé par barre.

*Contre-vérification :* Le cache BTCUSDT_derivs contient 1095 relevés de funding_rate du 2025-09-01 au 2026-08-31 : moyenne 3,067e-5 par 8 h, soit 3,36 %/an (médiane 3,56e-5, 23,7 % des relevés négatifs). Le code ne déduit que les coûts de turnover (article.py:32 `net = gross - costs`). Sur perpétuels, l'omission est OPTIMISTE d'environ 3,4 %/an pour un long permanent et pessimiste d'autant pour un short. Réserve : l'article ne précise ni spot ni perpétuel. En spot long, il n'y a pas de funding et seul le rendement du cash manque. En short spot, le coût d'emprunt manque (non chiffrable avec le cache).

**H5 — Sharpe sur 30 jours comparé à 0,5 × Sharpe du backtest : fausse alerte SHARPE_DECAY 41 % des jours et HALT quasi certain en quelques jours pour une stratégie parfaitement conforme**

article.py:140 `recent = live_returns.tail(window)` ; article.py:142 `recent.mean() / recent.std() * np.sqrt(365)` ; article.py:148 `if live_sharpe < backtest_metrics['sharpe'] * 0.5:`. Écart-type d'un Sharpe annualisé sur 30 j = √(365/30) = 3,49. Simulation (h5.py) avec un vrai Sharpe de 1,5 identique au backtest (vol 30 %) : P(SHARPE_DECAY un jour donné) = 0,410 (théorie Φ(−0,75/3,49) = 0,415) ; P(au moins un HALT en 90 j) = 0,988, en 365 j = 1,000 ; médiane du premier HALT = jour 4. DRAWDOWN_EXCEEDED (seuil 1,5 × −25 %) : 2,3 % sur un an, raisonnable.

*Correction :* Tester la dégradation avec un seuil statistique : par exemple, alerte si le Sharpe glissant est inférieur à SR_bt − 2·√(ppy/window), ou un CUSUM / test séquentiel sur une fenêtre ≥ 180 j. Autre option : un test sur le P&L cumulé contre le cône de confiance du backtest.

*Contre-vérification :* contre/c6.py (1000 simulations, Sharpe réel 1,5 égal à celui du backtest, vol 30 %, max_drawdown du backtest −25 %). Un contrôle direct de health_check confirme l'équivalence avec le calcul vectorisé. P(SHARPE_DECAY un jour donné) = 0,412, contre Φ(−0,75/√(365/30)) = 0,415 en théorie ; P(au moins un HALT en 90 j) = 0,981 ; en 365 j = 1,000 ; médiane du premier HALT = jour 4 ; P(DRAWDOWN_EXCEEDED sur 365 j) = 1,9 %. La règle d'article.py:148 arrête une stratégie parfaitement conforme presque à coup sûr.

**H8a — longest_dd_days compte des barres et non des jours ; periods_per_year = 365 est codé en dur (et np.sqrt(365) dans health_check)**

article.py:58 `longest_dd = underwater.groupby(` ; article.py:66 `'longest_dd_days': int(longest_dd),` ; article.py:12 `periods_per_year: int = 365 # crypto trades 24/7`. BTC horaire (cache) : longest_dd_days = 8502 (heures, soit 354 j) ; avec la configuration par défaut, Sharpe = −0,14 et ann_return = −1,2 %, contre −0,68 et −29,8 % avec periods_per_year = 8760.

*Correction :* Mesurer la durée par l'index temporel ((fin − début).days) et déduire periods_per_year de la fréquence de l'index.

*Contre-vérification :* contre/c7.py (cache BTCUSDT_1m rééchantillonné à l'heure, 8785 barres, long permanent sans frais) : longest_dd_days = 8502, ce qui compte des heures. Durée réelle : du 2025-10-06 19:00 au 2026-09-26, soit 354 j 5 h. Avec la configuration par défaut (periods_per_year = 365, article.py:12) : Sharpe −0,14, ann_return −1,2 % ; avec 8760 : Sharpe −0,68, ann_return −29,7 % (l'analyste annonce −29,8 %, écart d'arrondi ou d'alignement). health_check code aussi np.sqrt(365) en dur (article.py:142).

**H8b — Sharpe et ann_return calculés sur des log-rendements : le Sharpe est sous-estimé d'environ σ/2 (≈ 0,3 à 60 % de vol) et ann_return (log) est présenté comme un %**

article.py:48-49 `ann_return = r.mean() * cfg.periods_per_year` / `ann_vol = r.std() * np.sqrt(cfg.periods_per_year)`. Simulation à 60 % de vol : Sharpe sur rendements simples 1,756 contre Sharpe sur log-rendements (metrics) 1,450, soit −0,31. Un ann_return log de 94 % correspond à +156 % en rendement simple. Calmar mélange un rendement log et un drawdown simple. Biais PESSIMISTE.

*Correction :* Calculer le Sharpe sur les rendements simples et rapporter le CAGR (exp(Σr·ppy/n) − 1).

*Contre-vérification :* contre/c7.py (73 000 jours simulés, vol 60 %) : Sharpe sur rendements simples 1,718 contre 1,42 pour metrics sur log-rendements, soit −0,30 ≈ σ/2. Un ann_return log de 94 % correspond à exp(0,94) − 1 = +156 % en rendement simple. Calmar divise un rendement log par un drawdown simple (article.py:48-49, 67). Biais PESSIMISTE de l'ordre de 0,3 point de Sharpe à 60 % de vol.

**H8c — Le capital initial n'est pas pris comme premier pic : une perte dès la première barre n'est pas comptée dans le drawdown**

article.py:52 `equity = np.exp(r.cumsum())` ; article.py:54 `dd = (equity - peak) / peak`. Exécution : net = [−10 %, puis +0,1 %×120] -> max_drawdown = 0,0. Biais optimiste, limité au début de série.

*Correction :* Préfixer la courbe par 1.0 (capital initial) avant cummax.

*Contre-vérification :* contre/c7.py : net = [−10 %, puis +0,1 % × 120] -> max_drawdown 0,0, longest_dd_days 0, calmar 0 (article.py:52-54 : le premier pic est l'equity après la première barre, pas le capital initial 1,0). Biais optimiste limité aux pertes du tout début de série.

**H8d — Le plafond de 20 % du notionnel rend le risque effectif < 1 % pour tout stop à moins de 5 % ; pas de contrôle du sens du stop ; loss_if_stopped ignore gap, frais et slippage ; position_size n'est pas relié au backtest**

article.py:127 `cap = capital * max_position_pct` ; article.py:122 `risk_per_unit = abs(entry - stop)`. Exécution : position_size(10000, 100, 99) -> notionnel 2000, loss_if_stopped 20 (risque 0,2 % au lieu de 1 %) ; position_size(10000, 100, 110) accepte un stop au-dessus de l'entrée pour un long (loss_if_stopped 100).

*Correction :* Recevoir le sens (long/short) et valider le stop ; signaler quand le plafond réduit le risque ; inclure frais, slippage et marge de gap dans risk_per_unit.

*Contre-vérification :* contre/c7.py : position_size(10000, 100, 99) -> notionnel 2000, loss_if_stopped 20 (0,2 % au lieu de 1 %) ; stop à 95 -> 100, exactement 1 %, seuil du plafond ; stop à 94 -> 100 ; position_size(10000, 100, 110) accepte un stop au-dessus de l'entrée (loss_if_stopped 100, aucun contrôle du sens, article.py:122 `risk_per_unit = abs(entry - stop)`). Le plafond de 20 % (article.py:127) ramène le risque sous 1 % pour tout stop à moins de 5 %. Frais, slippage et gap sont absents du calcul. Aucune fonction n'appelle position_size depuis le backtest.

### Affirmations de l'article

* **trompeur** — « 'That shift(1) is the difference between a strategy and a fantasy. Every AI-generated backtest I've reviewed that showed a Sharpe above 3 was missing it.' » : L'oubli de shift(1) produit bien des Sharpe absurdes : sur BTC quotidien (cache 2025-09 -> 2026-09), un signal égal au signe du rendement du jour donne un Sharpe de 0,24 avec shift(1) et de 17,78 sans. Mais shift(1) ne suffit pas : avec shift(1) présent (article.py:22), une normalisation calculée sur le segment de test donne un mean_sharpe de 2,25 sur une marche aléatoire (99 % des simulations > 1). À l'inverse, un Sharpe > 3 n'implique pas un shift manquant : le meilleur de 80 stratégies de bruit sur un an dépasse 3 dans 11 % des cas, et un pli de 60 j dépasse 3 avec une probabilité de 11,2 % sous H0. L'expérience personnelle (« every … I've reviewed ») est invérifiable.
* **faux** — « 'Sharpe above 2 on daily crypto data is a red flag, not a trophy. It usually means leakage. Above 3 means it's certain.' » : Erreur type d'un Sharpe annualisé sous H0 : 2,47 (60 j), 1,74 (120 j), 1,00 (1 an), 0,45 (5 ans). P(|SR| > 2) = 41,7 % sur 60 j et 4,6 % sur 1 an ; P(SR > 3) = 11,2 % sur 60 j. Meilleur de 80 stratégies de bruit sur 1 an : Sharpe moyen 2,45, P(> 2) = 0,86, P(> 3) = 0,11. Un Sharpe > 2 vient donc le plus souvent du surapprentissage ou de la chance, pas d'une fuite. BTC buy-and-hold sur le cache (−22,8 %, vol 45 %) a un Sharpe glissant sur 90 j compris entre −3,97 et +3,59, et supérieur à 2 pendant 8 % des fenêtres, sans aucune fuite. « Certain » est donc faux. Un drapeau rouge pour un Sharpe > 2 sur plusieurs années reste un conseil raisonnable.
* **faux** — « 'One train/test split can be gotten lucky. Walk-forward can't.' » : 80 familles de stratégies de bruit pur, chacune évaluée en walk-forward (8 plis × 120 j, 500 simulations) : le meilleur mean_sharpe vaut 1,52 en moyenne, P(> 1,5) = 0,48, avec 7/8 plis positifs en médiane pour la stratégie retenue. Le walk-forward réduit la variance mais reste un estimateur bruité, et itérer sur les conceptions de walk-forward le suroptimise comme un split unique. De plus, la version de l'article fuit si signal_fn normalise sur le test (mean_sharpe 2,25 sur du bruit) et ne s'exécute pas avec ses valeurs par défaut (KeyError).
* **trompeur** — « 'Judge a strategy on positive_folds and worst_fold, never on the aggregate.' » : Vraie stratégie à SR = 1,5, 18 plis de 60 j (3 000 simulations) : worst_fold médian −2,91, P(worst_fold < 0) = 1,00, positive_folds médian 13/18. Bruit pur : worst_fold médian −4,40, positive_folds 9/18. Le worst_fold est dominé par le bruit (écart-type par pli 2,47) et se dégrade mécaniquement avec le nombre de plis. L'agrégat a l'erreur type la plus faible (0,58, contre 2,47 par pli) : c'est la statistique à privilégier. Rejeter une stratégie sur worst_fold rejetterait toute vraie stratégie à SR 1,5, d'où un biais PESSIMISTE.
* **trompeur** — « 'twelve losses in a row — which happens to a 45% win rate roughly once a year' » : Calcul exact (DP) de la probabilité d'au moins une série de ≥ 12 pertes en un an, avec p_perte = 0,55 et 0,55^12 = 7,66e-4 : 50 trades/an -> 1,4 % ; 250 -> 8,0 % ; 365 -> 11,6 % ; 1000 -> 29 % ; 2900 -> 63 %. Une série attendue par an exige environ 2 900 trades/an (≈ 8 par jour). Pour un trader quotidien, c'est plutôt une fois tous les 8 à 12 ans. L'affirmation n'est vraie que pour du trading à haute fréquence.
* **trompeur** — « 'At 1% you're down 11% and still trading. At 5% you're down 46% and need to double just to recover.' » : Arithmétique composée exacte : 1 − 0,99^12 = 11,36 % ✓ ; 1 − 0,95^12 = 45,96 % ✓. Mais il faut +85,1 % pour revenir, pas +100 % (« doubler » est exagéré), et +12,8 % dans le cas à 1 %. En risque fixe non composé, les pertes seraient de 12 % et 60 %. De plus, position_size plafonne le notionnel à 20 %, donc le risque effectif est inférieur à 1 % pour tout stop à moins de 5 % (stop à 1 % -> risque 0,2 %).
* **trompeur** — « 'You tested 80 variations. One had a Sharpe of 1.8. Is it real? Almost certainly not.' » : La réponse dépend de la durée, que l'article ignore. DSR correct pour SR = 1,8 et 80 essais de bruit : 0,258 (1 an), 0,538 (2 ans), 0,747 (3 ans), 0,942 (5 ans), 0,999 (10 ans) ; seuil de bruit annualisé 2,45 / 1,73 / 1,42 / 1,10 / 0,78. Sur 5 à 10 ans, 1,8 est très probablement réel. La fonction de l'article renvoie DSR = 0,000 dans tous ces cas, car son seuil est fixé à 2,45 quelle que soit la durée.
* **faux** — « deflated_sharpe (implémentation présentée comme la correction de Bailey et López de Prado) » : expected_max = 2,451 pour N = 80, sans √V[SR_n] ; comparaison avec un Sharpe annualisé ; √(n_obs − 1) quotidien. Résultat : une marche d'escalier (DSR 0,013 à SR 2,35 et 0,999 à SR 2,60). Vraie stratégie à SR 2,0 parmi 79 bruits : PASS 9 % contre 52 % pour la version correcte. n_trials = 1 : PASS même à SR −2 (expected_max = −inf).
