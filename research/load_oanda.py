import glob, pandas as pd, numpy as np
fs = sorted(glob.glob("data/oanda/oanda-SPX500_USD-*.csv"))
df = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
df["time"] = pd.to_datetime(df["time"], utc=True)
df = df.drop_duplicates("time").sort_values("time")
df["et"] = df["time"].dt.tz_convert("America/New_York")
df["hm"] = df["et"].dt.hour * 60 + df["et"].dt.minute
prof = df.groupby(df["hm"] // 30 * 30)["volume"].mean()
print("volume by ET half-hour (tick count):"); print((prof.loc[[480,510,540,570,600,720,900,930,960,990]]).round(1).to_string())
rth = df[(df.hm >= 570) & (df.hm < 960) & (df.et.dt.weekday < 5)].copy()
rth["day"] = rth["et"].dt.date
cnt = rth.groupby("day").size()
good = cnt[cnt >= 370].index
rth = rth[rth.day.isin(good)]
print("sessions", len(good), "from", good[0], "to", good[-1], "minutes/session median", int(cnt.median()))
rth[["et","day","hm","open","high","low","close","volume"]].to_pickle("data/spx_rth_1m.pkl")
