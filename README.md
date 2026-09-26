# tradebot — état des lieux : prévoir la direction des cryptos à 5, 10 et 15 minutes

Phase 1 d'un bot de trading : mesurer ce qui prédit si **BTC, SOL, ETH** (et XRP, BNB, DOGE)
vont **monter ou descendre** dans 5, 10 et 15 minutes, avec des indicateurs techniques, **Google
TimesFM**, et les marchés **Polymarket « Up or Down »** comme terrain de test.

> Recherche et simulation papier uniquement : aucun ordre, aucune clé. Les poids de TimesFM 3.0
> sont sous licence **non commerciale** (production : TimesFM 2.5, Apache-2.0). Polymarket est
> bloqué en France (ANJ) : seules les données publiques sont lues.

## Verdict (données jusqu'au 25/09/2026)

| question | réponse | détail |
|---|---|---|
| Quels indicateurs prédisent la direction à 5–15 min ? | Le **retournement** : les 20 plus forts sont tous « le prix revient vers sa moyenne de l'heure » (`ema_dist_60`, `force_index_13`, `zscore_60`, `rsi_14`…). Signal stable sur 6 cryptos et sur 1 an, mais faible : IC ≤ 0,05, ≈ 52 % de bonnes directions hors échantillon. | [`reports/etat_des_lieux/`](reports/etat_des_lieux/README.md) |
| Combiner les 167 indicateurs aide-t-il ? | À peine : gradient boosting 51,8 à 53,3 % selon l'actif et l'horizon, contre ≈ 52 % pour la seule règle « retournement ». Rien de rentable sur Binance : il faudrait ≈ 72 % pour couvrir 10 pb de frais. | idem |
| TimesFM prévoit-il la direction ? | **Non.** 48 à 52 % sur 1 500 prévisions par actif (BTC, SOL, ETH), pas mieux que « retournement », avec ou sans indicateurs en covariables ; −10 pb par trade après frais. Il prévoit bien l'**amplitude** (quantiles calibrés), pas le sens. | [`reports/timesfm/`](reports/timesfm/README.md), [`docs/research/timesfm.md`](docs/research/timesfm.md) |
| Polymarket 5/15 min : le marché est-il battable avant l'ouverture ? | Le prix à S−30 s est quasi aveugle (51,1 %). Nos modèles font un peu mieux (jusqu'à 52,5 % en 5m), grâce au **« TWAP partiel »** (écart spot − moyenne des 30 dernières secondes, mécanique de la règle de résolution). Mais après frais **et** le surcoût réellement payé sur ces positions (1,3 à 3,1 c par part, sélection adverse), **aucun avantage démontré**. | [`reports/polymarket/modeles_vs_marche/`](reports/polymarket/modeles_vs_marche/README.md), [`diagnostic`](reports/polymarket/diagnostic.md) |
| Copier les top wallets Polymarket ? | **Non.** Trades récupérables à la seconde (API et on-chain, ≈ 2–3 s en temps réel), mais le classement horaire persiste à peine (ρ = 0,09) et copier le top 10 de l'heure précédente perd ≈ 1,4 c par part quel que soit le délai (0 à 60 s). Seuls les market makers gagnent durablement. | [`reports/polymarket/top_wallets/`](reports/polymarket/top_wallets/README.md), [`docs/research/polymarket_temps_reel.md`](docs/research/polymarket_temps_reel.md) |

**Piste restante** : le seul avantage mesurable est l'information du TWAP partiel à la seconde,
du même ordre que le coût d'exécution en preneur. Il ne peut devenir exploitable qu'en **maker**
(ordres au repos, sans frais, avec remise) — à tester en papier sur le carnet réel (WebSocket),
en mesurant les exécutions et la sélection adverse.

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
  report.py cli.py            rapports (CSV, Markdown, PNG) et ligne de commande
docs/ARCHITECTURE.md          contrat entre modules
docs/research/                TimesFM, indicateurs, données, méthodologie, Polymarket, temps réel
reports/                      résultats (chaque dossier a son README)
```
