import time, requests, numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from tradebot.polymarket import PolymarketClient
OUT = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "reports" / "polymarket" / "premieres_courbes")
c = PolymarketClient(); S_ = requests.Session()
end = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta("5min"); start = end - pd.Timedelta("1h")
ms = [m for m in c.list_updown_markets("btc", "5m", start, end, only_closed=True) if m.resolved_up is not None]
rows = []
for m in ms:
    cur = None
    while True:
        p = dict(condition=m.condition_id, limit=1000)
        if cur: p["cursor"] = cur
        r = S_.get("https://data-api.polymarket.com/v2/trades", params=p, timeout=20).json()
        for t in r["data"]:
            t["S"] = m.start; t["up_won"] = m.resolved_up; rows.append(t)
        pg = r.get("pagination", {})
        if not pg.get("has_more"): break
        cur = pg["next_cursor"]
T = pd.DataFrame(rows); T["ts"] = pd.to_datetime(T.timestamp, unit="s", utc=True)
T["t_rel"] = (T.ts - T.S).dt.total_seconds()
win = np.where(T.outcome == "Up", T.up_won, ~T.up_won.astype(bool)).astype(float)
sgn = np.where(T.side == "BUY", 1, -1)
T["pnl"] = sgn * T["size"] * (win - T.price) - T["size"] * 0.07 * T.price * (1 - T.price)   # taker : frais payés
W = T.groupby("proxy_wallet").agg(pnl=("pnl", "sum"), vol=("size", lambda s: (s * T.loc[s.index, "price"]).sum()), n=("pnl", "size"),
                                  t_med=("t_rel", "median")).sort_values("pnl", ascending=False)
print(len(ms), "marchés,", len(T), "trades preneurs,", T.proxy_wallet.nunique(), "wallets")
print(W.head(10).round(1))
top = W.head(10).index
SURF, INK, INK2, GRID, BLUE, GRAY = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de", "#2a78d6", "#b9b8b2"
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold", "axes.titlelocation": "left"})
fig, ax = plt.subplots(figsize=(11, 4.4))
bins = np.arange(-60, 301, 5)
oth = T[~T.proxy_wallet.isin(top)]; tp = T[T.proxy_wallet.isin(top)]
ax.hist(oth.t_rel.clip(-60, 300), bins=bins, weights=np.full(len(oth), 1 / len(oth)) * 100, color="#8a8984", histtype="step", lw=2, label="autres wallets")
ax.hist(tp.t_rel.clip(-60, 300), bins=bins, weights=np.full(len(tp), 1 / len(tp)) * 100, color=BLUE, histtype="step", lw=2, label="top 10 P&L de l'heure")
ax.axvline(0, color=INK2, lw=1, ls="--"); ax.text(2, ax.get_ylim()[1] * 0.95, "ouverture", color=INK2, fontsize=9, va="top")
ax.set_xlabel("secondes depuis l'ouverture de la fenêtre (tranches de 5 s)"); ax.set_ylabel("% des trades du groupe")
ax.set_title(f"BTC 5 min, dernière heure : quand tradent les meilleurs wallets ?"); ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.42, 1.0))
n_tr = f"{len(T):,}".replace(",", " ")
fig.text(0.01, -0.03, f"{len(ms)} marchés ; {n_tr} trades preneurs (data-api v2/trades, horodatage à la seconde) ; {start:%d/%m %H:%M}–{end:%H:%M} UTC ; trades avant −60 s regroupés à −60 s.\nP&L approximatif côté preneur, frais inclus ; top 10 défini sur cette même heure (in-sample).", fontsize=9, color=INK2)
fig.tight_layout(); fig.savefig(f"{OUT}/4_timing_top_wallets_derniere_heure.png", dpi=150, bbox_inches="tight")
