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
