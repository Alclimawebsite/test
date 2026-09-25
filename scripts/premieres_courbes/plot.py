import numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from scipy.stats import binomtest
D = str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'data' / 'cache' / 'premieres_courbes')
OUT = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "reports" / "polymarket" / "premieres_courbes")
import os; os.makedirs(OUT, exist_ok=True)
SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"
BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#8a8984"
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.titleweight": "bold", "axes.titlesize": 13, "axes.titlelocation": "left"})
meta = pd.read_parquet(f"{D}/meta.parquet"); H = pd.read_parquet(f"{D}/hist.parquet")
meta = meta[meta.resolved_up.notna()].copy(); meta["up"] = meta.resolved_up.astype(bool)
H = H.merge(meta[["slug", "start", "duration", "asset", "up"]], on="slug")
H["tmin"] = (H.t - H.start).dt.total_seconds() / 60
print("marchés résolus:", len(meta), meta.groupby(["asset","duration"]).up.agg(["size","mean"]))

# --- 1. Trajectoire du prix Up
fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True)
for ax, dur, L in zip(axes, ["5m", "15m"], [5, 15]):
    sub = H[(H.duration == dur) & (H.tmin >= -10) & (H.tmin <= L + 0.5)].copy()
    sub["b"] = np.round(sub.tmin).astype(int)
    for up, col, lab in [(True, BLUE, "Résolu « Up »"), (False, ORANGE, "Résolu « Down »")]:
        g = sub[sub.up == up].groupby("b").p
        q = g.quantile([0.25, 0.5, 0.75]).unstack()
        ax.fill_between(q.index, q[0.25], q[0.75], color=col, alpha=0.15, linewidth=0)
        ax.plot(q.index, q[0.5], color=col, lw=2)
        ax.text(q.index[-1] + 0.3, q[0.5].iloc[-1], lab, color=INK2, va="center", fontsize=10)
    ax.axvline(0, color=INK2, lw=1, ls="--"); ax.axvline(L, color=INK2, lw=1, ls="--")
    ax.text(0.2, 0.97, "ouverture", color=INK2, fontsize=9, va="top"); ax.text(L + 0.2, 0.97, "clôture", color=INK2, fontsize=9, va="top")
    ax.set_title(f"Marchés {dur} (BTC, ETH, SOL)"); ax.set_xlabel("minutes depuis l'ouverture de la fenêtre")
    ax.set_xlim(-10, L + 5); ax.set_ylim(0, 1)
axes[0].set_ylabel("prix du jeton « Up » (= probabilité implicite)")
fig.suptitle("Avant l'ouverture le marché est à ~0,50 : il ne sait rien. Il converge ensuite pendant la fenêtre", x=0.01, ha="left", fontsize=12, color=INK2)
fig.text(0.01, -0.02, "Médiane et intervalle interquartile, 3 derniers jours. Source : API publique Polymarket (prices-history, 1 point/min).", fontsize=9, color=INK2)
fig.tight_layout(); fig.savefig(f"{OUT}/1_trajectoire_prix_up.png", dpi=150, bbox_inches="tight"); plt.close(fig)

# --- 2. Calibration du prix de marché
fig, ax = plt.subplots(figsize=(6.4, 5.6))
ax.plot([0, 1], [0, 1], color=GRAY, lw=1, ls="--"); ax.text(0.62, 0.55, "calibration parfaite", color=INK2, fontsize=9, rotation=40)
bins = np.linspace(0, 1, 11)
for k, col in [(1, BLUE), (5, ORANGE), (10, AQUA)]:
    s = H[(H.duration == "15m") & (H.tmin > k - 1) & (H.tmin <= k)].sort_values("t").groupby("slug").tail(1)
    s["bin"] = pd.cut(s.p, bins, include_lowest=True)
    g = s.groupby("bin", observed=True).agg(p=("p", "mean"), f=("up", "mean"), n=("up", "size"))
    g = g[g.n >= 15]
    ax.plot(g.p, g.f, color=col, lw=2, marker="o", ms=5, label=f"T+{k} min")
ax.set_xlabel("prix du jeton « Up » à l'instant T+k"); ax.set_ylabel("fréquence réelle de « Up »")
ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.legend(frameon=False, loc="upper left")
ax.set_title("Le prix du marché est bien calibré (15 min)")
fig.text(0.01, -0.03, "Marchés 15 min BTC/ETH/SOL, 3 derniers jours ; déciles de prix avec au moins 15 marchés.", fontsize=9, color=INK2)
fig.tight_layout(); fig.savefig(f"{OUT}/2_calibration_marche.png", dpi=150, bbox_inches="tight"); plt.close(fig)

# --- 3. Signaux simples calculés AVANT l'ouverture vs résultat officiel
K = {s: pd.read_parquet(f"{D}/{s}.parquet") for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]}
sym = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}
def feat(r):
    k = K[sym[r.asset]]; T = r.start
    try:
        last = k.loc[T - pd.Timedelta("1min")]          # bougie [T-1m, T) : close = prix à T
        out = {"mech": np.sign(last.close - (last.open + last.high + last.low + last.close) / 4)}
        for n in (1, 5, 15, 60):
            out[f"mom{n}"] = np.sign(np.log(last.close / k.loc[T - pd.Timedelta(f"{n+1}min")].close))
        return pd.Series(out)
    except KeyError:
        return pd.Series(dtype=float)
F = meta.join(meta.apply(feat, axis=1))
pre = H[(H.tmin <= -2) & (H.tmin > -4)].sort_values("t").groupby("slug").tail(1).set_index("slug").p
F["mkt"] = np.sign(F.slug.map(pre) - 0.5)
sigs = [("Taux de base (toujours « Up »)", None), ("Prix du marché à T−2 min > 0,50", "mkt"), ("Momentum 1 min", "mom1"),
        ("Momentum 5 min", "mom5"), ("Momentum 15 min", "mom15"), ("Momentum 60 min", "mom60"),
        ("Prix à T vs moyenne 60 s (effet TWAP)", "mech")]
fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8), sharex=True)
for ax, dur in zip(axes, ["5m", "15m"]):
    sub = F[F.duration == dur]; ys, labs = [], []
    for i, (lab, c) in enumerate(sigs):
        if c is None: hit = sub.up.astype(float)
        else:
            s = sub[sub[c] != 0]; hit = ((s[c] > 0) == s.up).astype(float)
        n = len(hit); acc = hit.mean(); ci = binomtest(int(hit.sum()), n).proportion_ci()
        col = AQUA if c == "mech" else (GRAY if c is None else BLUE)
        ax.errorbar(acc * 100, i, xerr=[[100 * (acc - ci.low)], [100 * (ci.high - acc)]], fmt="o", color=col, ms=7, capsize=0, elinewidth=2)
        ax.text(ci.high * 100 + 0.6, i, f"{acc*100:.1f} %", va="center", fontsize=9, color=INK2)
        labs.append(lab)
    ax.axvline(50, color=GRAY, lw=1); ax.axvline(52.75, color=ORANGE, lw=1.5, ls="--")
    ax.text(53.1, -0.55, "seuil taker : 52,75 %", color=INK2, fontsize=8.5, va="bottom")
    ax.set_yticks(range(len(sigs))); ax.set_yticklabels(labs if dur == "5m" else [""] * len(sigs)); ax.invert_yaxis()
    ax.set_title(f"Marchés {dur} : n = {len(sub)}"); ax.set_xlabel("% de bonnes prédictions vs résultat officiel (IC 95 %)")
    ax.set_xlim(38, 70); ax.set_ylim(len(sigs) - 0.5, -1.1); ax.grid(axis="y", visible=False)
fig.suptitle("En 5 min, le mouvement de la dernière minute avant T bat le seuil. Reste à savoir si on peut encore acheter à ~0,50", x=0.01, ha="left", fontsize=12, color=INK2)
fig.text(0.01, -0.03, "BTC/ETH/SOL, 3 derniers jours, in-sample. Momentum = signe du rendement Binance 1 min sur n minutes jusqu'à T. Effet TWAP = clôture de la dernière minute vs sa moyenne (O+H+L+C)/4.", fontsize=9, color=INK2)
fig.tight_layout(); fig.savefig(f"{OUT}/3_signaux_vs_resultat.png", dpi=150, bbox_inches="tight"); plt.close(fig)
F.drop(columns=["question"], errors="ignore").to_csv(f"{D}/marches_signaux.csv", index=False)
#print(F.groupby("duration")[["mom1","mom5","mom15","mom60","mech","mkt"]].apply(lambda d: pd.Series({c: ((d[c]>0)==d.up)[d[c]!=0].mean() for c in d.columns})))
