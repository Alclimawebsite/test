"""Configuration pytest commune : enregistrement des marqueurs.

* ``network`` : le test accède à Internet (Binance, Yahoo, Polymarket…).
  Exécution hors-ligne : ``pytest -m "not network"``.
* ``slow`` : test long (gros téléchargement, TimesFM sur CPU…).
  Exclusion : ``pytest -m "not slow"``.
"""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: test qui accède à Internet (désélection : -m 'not network')")
    config.addinivalue_line(
        "markers", "slow: test lent (désélection : -m 'not slow')")
