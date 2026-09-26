# Vigie Up/Down — tableau de bord en direct

Page statique (HTML + JS, aucun serveur) qui se connecte depuis le navigateur à :

- Binance (`data-stream.binance.vision`, trades à la milliseconde, et historique 1 s) ;
- le flux Chainlink BTC/USD (source officielle de résolution), relayé par le RTDS Polymarket ;
- le carnet Polymarket (`ws-subscriptions-clob.polymarket.com`) et les marchés (`gamma-api`).

Elle applique la formule exacte P(Up) de la règle TWAP-60 (`formula.js`, portage vérifié de
`src/tradebot/polymarket_formula.py`, calibrée comme `polymarket_signal.py`), la compare au prix
du marché et affiche l'avantage théorique après frais. Affichage uniquement : aucun ordre.

Test local : ouvrir `index.html` dans un navigateur (ou `python -m http.server` dans ce dossier).
Si un flux est bloqué par le réseau, la page l'indique et continue avec les autres.

## Rejeu (`rejeu/`)

`rejeu/vigie_rejeu.html` est la même interface, autonome et sans connexion réseau. Elle rejoue
seconde par seconde les 22 marchés enregistrés le 26/09/2026 (BTC 5m, ETH 5m, BTC 15m),
avec le bilan des études. La page est générée à partir de `rejeu/template.html` et des sorties de
`scripts/latency_study.py` :

```bash
python scripts/build_replay_artifact.py      # lit reports/latence/, écrit site/rejeu/vigie_rejeu.html
```

La page n'a pas de balises `<html>`/`<head>`/`<body>` : elle est faite pour être publiée comme
artefact, où ces balises sont ajoutées à la publication. Un navigateur l'ouvre aussi directement.
