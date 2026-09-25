import os
import time, json, concurrent.futures as cf
import numpy as np, pandas as pd, requests
from tradebot.polymarket import PolymarketClient, markets_to_frame
OUT = str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'data' / 'cache' / 'premieres_courbes')
os.makedirs(OUT, exist_ok=True)
t0 = time.time()
c = PolymarketClient()
end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta("1h")
start = end - pd.Timedelta("3D")
rows = []
for asset in ["btc", "eth", "sol"]:
    for dur in ["15m", "5m"]:
        ms = c.list_updown_markets(asset, dur, start, end, only_closed=True)
        print(asset, dur, len(ms), round(time.time()-t0)); rows += ms
meta = markets_to_frame(rows); meta.to_parquet(f"{OUT}/meta.parquet")
print(meta.columns.tolist())
def hist(m):
    s0 = int(m.start.timestamp()); s1 = int(m.end.timestamp())
    try:
        h = c.prices_history(m.token_up, s0 - 900, s1 + 60, fidelity=1)
    except Exception as e:
        return None
    return m.slug, h
with cf.ThreadPoolExecutor(8) as ex:
    res = [r for r in ex.map(hist, rows) if r is not None]
H = pd.concat({slug: h for slug, h in res}, names=["slug", "t"]).rename("p").reset_index()
H.to_parquet(f"{OUT}/hist.parquet"); print("hist", H.shape, round(time.time()-t0))
# Binance 1m
def kl(sym):
    out, s = [], int((start - pd.Timedelta("1h")).timestamp()*1000)
    e = int((end + pd.Timedelta("20min")).timestamp()*1000)
    while s < e:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params=dict(symbol=sym, interval="1m", startTime=s, limit=1000), timeout=20).json()
        if not r: break
        out += r; s = r[-1][0] + 60000
    df = pd.DataFrame(out).iloc[:, :6]; df.columns = ["t","open","high","low","close","volume"]
    df["t"] = pd.to_datetime(df.t, unit="ms", utc=True); return df.set_index("t").astype(float)
for sym in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
    kl(sym).to_parquet(f"{OUT}/{sym}.parquet")
print("done", round(time.time()-t0))
