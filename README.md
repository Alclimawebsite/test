# tradebot — état des lieux : prévoir la direction des cryptos à 5, 10 et 15 minutes

Phase 1 d'un bot de trading : mesurer ce qui prédit si **BTC, SOL, ETH** (et XRP, BNB, DOGE)
vont **monter ou descendre** dans 5, 10 et 15 minutes, avec des indicateurs techniques, **Google
TimesFM**, et les marchés **Polymarket « Up or Down »** comme terrain de test.

> Recherche et simulation papier uniquement : aucun ordre, aucune clé. Les poids de TimesFM 3.0
> sont sous licence **non commerciale** (production : TimesFM 2.5, Apache-2.0). Polymarket est
> bloqué en France (ANJ) : seules les données publiques sont lues.

## Verdict (données jusqu'au 26/09/2026)

Aucune des dix études ne trouve de moyen de prévoir Up ou Down qui rapporte après frais et latence. Chaque
résultat positif venait d'une hypothèse d'exécution trop généreuse et a disparu une fois corrigé. Bilan complet,
méthode comprise : document « Prévoir Up ou Down : bilan complet des études » ; audit en 8 points :
[`reports/audit_8_points/`](reports/audit_8_points/README.md).

| question | réponse | détail |
|---|---|---|
| Quels indicateurs prédisent la direction à 5–15 min ? | Le **retournement** vers la moyenne de l'heure : signal réel mais faible, ≈ 52 % de bonnes directions hors échantillon, contre 69,5 à 89 % nécessaires pour payer 10 pb. | [`reports/etat_des_lieux/`](reports/etat_des_lieux/README.md) |
| TimesFM prévoit-il la direction ? | **Non** : 47 à 52 %, environ −10 pb par trade. Il prévoit bien l'**amplitude**, un peu moins bien qu'une moyenne mobile de la volatilité. | [`reports/timesfm/`](reports/timesfm/README.md), [`reports/timesfm_amplitude/`](reports/timesfm_amplitude/README.md) |
| Polymarket 5/15 min : nos modèles battent-ils le marché ? | **Non** : +2,2 à +2,9 c par part au prix supposé, effacés par le surcoût réellement payé (1,3 à 3,1 c). | [`reports/polymarket/modeles_vs_marche/`](reports/polymarket/modeles_vs_marche/README.md) |
| Copier les top wallets ? | **Non** : −1,38 c par part, à tout délai ; le classement horaire persiste à peine (ρ = 0,09). | [`reports/polymarket/top_wallets/`](reports/polymarket/top_wallets/README.md) |
| Tenir le carnet (maker) ? | **Non démontré** : +0,17 c par part (IC −1,30 ; +1,67), sélection adverse. | [`reports/polymarket/maker_live/`](reports/polymarket/maker_live/README.md) |
| Existe-t-il une formule exacte ? | **Oui** : P(Up) = Φ(m/s) pour la règle TWAP-60, calibrée (σ × 1,40). Mais le carnet l'applique en ≈ 0,24 s ; les gains historiques supposaient un prix déjà disparu. | [`reports/polymarket/formule/`](reports/polymarket/formule/README.md) |
| Quel temps de réaction faudrait-il ? | Une fenêtre de 0,1 à 0,3 s, au niveau du plancher d'un preneur (≈ 230 ms) : **non établi** sur une heure calme (audit). | [`reports/latence/`](reports/latence/README.md) |
| Gagner à coup sûr entre marchés liés ? | **Non** : aucune combinaison sûre réelle dans le carnet remis dans l'ordre du serveur ; l'issue n'est connue que ≈ 0,8 s avant la fin. | [`reports/polymarket/arbitrage/`](reports/polymarket/arbitrage/README.md) |
| Rotation de memecoins (« vendre le 3x, acheter le bas de fourchette ») ? | **Non** : sur 336 départs de 12 mois, 98 gagnés, 140 perdus, 98 égalités ; aucun avantage qui se répète. | [`reports/memecoins/`](reports/memecoins/README.md) |
| Peut-on voir les changements de profil sur les réseaux sociaux ? | **Oui, selon le réseau** : flux poussé gratuit à ≈ 1 s sur Bluesky, Farcaster et Nostr (mesuré : l'AppView Bluesky reflète un changement 0,25 s après le firehose) ; sur X, Instagram, TikTok, YouTube, Telegram, seulement en relisant le profil et en comparant (X : 0,01 $ par compte et par jour, 300 requêtes / 15 min ; Meta et TikTok : accès sous conditions). Aucune plateforme fermée ne date les changements. | [`docs/research/reseaux_sociaux.md`](docs/research/reseaux_sociaux.md) |

> Polymarket est bloqué en France (ANJ, 16/07/2026) et Binance n'y offre plus de trading depuis le 01/07/2026 :
> tout ici est lecture seule et simulation papier.

## Installation

```bash
uv venv .venv -p 3.11 && . .venv/bin/activate
uv pip install --index-url https://download.pytorch.org/whl/cpu torch
uv pip install -e ".[timesfm,dev]"
```

Données : klines Binance via `data.binance.vision` / `data-api.binance.vision` (l'API
`api.binance.com` est géo-restreinte depuis certains serveurs), dérivés Binance (funding, open
interest, ratios long/short), API publiques Polymarket. Tout est mis en cache dans `data/cache/`.

## Commandes

```bash
python -m tradebot fetch --days 365                    # cache Binance 1m + dérivés
python -m tradebot study --days 365                    # état des lieux -> reports/etat_des_lieux/
python -m tradebot timesfm-backtest --tickers BTCUSDT --covariates none --stride 15
python -m tradebot predict --ticker SOLUSDT            # P(hausse) à 5/10/15 min, maintenant

python scripts/polymarket_diagnostic.py                # règles, proxies, baselines (7 jours)
python -m tradebot.polymarket_wallets --hours 48       # trades à la seconde, P&L par wallet
python scripts/polymarket_top_wallets.py               # top wallets, persistance, copy-trading
python scripts/polymarket_models_vs_market.py          # nos modèles contre le prix Polymarket
python scripts/social_profile_probe.py stream --seconds 120   # flux de changements de profil (Bluesky, Farcaster, Nostr)
python scripts/social_profile_probe.py watch --bluesky bsky.app --farcaster dwr --telegram durov --interval 60 --minutes 10
pytest -m "not network and not slow"                   # tests hors-ligne
```

## Organisation

```
src/tradebot/
  config.py targets.py        univers, horizons, frais ; cibles (rendement futur, direction)
  data.py indicators.py       Binance (bulk + API + dérivés) ; 167 indicateurs causaux, 10 familles
  evaluation.py               IC (global, par jour, Newey-West), hit-rate/AUC hors échantillon, FDR, modèles combinés
  forecaster.py backtest.py   TimesFM 3.0 / 2.5, P(hausse) depuis les quantiles ; walk-forward vs baselines
  polymarket*.py              client lecture seule, trades par wallet, backtest modèles vs marché
  social_profiles.py          profils publics de réseaux sociaux : instantanés, différences, flux de changements
  report.py cli.py            rapports (CSV, Markdown, PNG) et ligne de commande
docs/ARCHITECTURE.md          contrat entre modules
docs/research/                TimesFM, indicateurs, données, méthodologie, Polymarket, temps réel, réseaux sociaux
reports/                      résultats (chaque dossier a son README)
```
