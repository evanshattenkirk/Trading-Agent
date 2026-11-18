"""Round 6 post-hoc diagnostics (never a verdict): buy NO on the modal bracket, and Kalshi mid vs XSP option-implied
odds on every priced S&P market at 12:00 ET. Writes research/round6_posthoc.json. Run from the repo root."""
import sys, json, math
sys.path.insert(0, 'research')
import round6 as r
from collections import defaultdict
import numpy as np, pandas as pd

def modal_no(series, events_fn, entry, pick=None):
    rows=[]
    for ev in events_fn:
        ms=[m for m in r.markets_of(series, ev) if r.settled(m) and (k:=r.kind(m)) and k[0]=='between']
        if pick: ms=pick(ev, ms)
        if (len(ms)<5 and not pick) or not ms: continue
        e=entry(ms[0]); q=[]
        for m in ms:
            c=r.candles_of(series,m['ticker']); qq=r.quote_at(c,e) if c else None
            if qq: q.append((m,qq))
        if not q: continue
        q.sort(key=lambda x: r.num(x[0]['floor_strike']))
        mids=[(a+b)/2 for _,(a,b) in q]; i=max(range(len(q)),key=lambda i:(mids[i],-i))
        m,(bid,ask)=q[i]; p=1-bid
        if not r.buyable(p): continue
        pay=0 if m['result']=='yes' else 1
        rows.append({'event':ev,'close':r.ts(m['close_time']),'p':p,'ret':r.ret(pay,p,r.fee(p)),'spread':ask-bid})
    return rows

def idx_events(series):
    out=[]
    for ev in r.index_events(series):
        ms=[m for m in r.markets_of(series,ev) if r.settled(m)]
        if ms and r.is_daily_close_1600(ms[0]): out.append(ev)
    return out

res={}
for s in ('KXINX','KXNASDAQ100'):
    rows=modal_no(s, idx_events(s), r.at_et(12,0)); res[f'{s} modal NO 12:00']=r.summarize(rows)
    res[f'{s} modal spread']=float(np.mean([x['spread'] for x in rows]))
def pick(ev, ms):
    if not ms: return []
    ss=r.spot_sigma(r.ts(ms[0]['close_time'])-15*60,60)
    return r.nearest(ms, ss[0], lambda m:(r.num(m['floor_strike'])+r.num(m['cap_strike']))/2) if ss else []
rows=modal_no('KXBTC',[r.btc_range_event(e) for e in r.btc_events()], lambda m:r.ts(m['close_time'])-15*60, pick)
res['KXBTC modal NO -15m']=r.summarize(rows); res['KXBTC modal spread']=float(np.mean([x['spread'] for x in rows]))

# PM2 diagnostics: every priced, quoted market at 12:00 - Kalshi mid vs option fair vs outcome
diag=[]
for ev in r.index_events('KXINX'):
    ms=[m for m in r.markets_of('KXINX',ev) if r.settled(m)]
    if not ms or not r.is_daily_close_1600(ms[0]): continue
    if sum(1 for m in ms if (k:=r.kind(m)) and k[0]=='between')<5: continue
    from datetime import datetime
    day=datetime.fromtimestamp(r.ts(ms[0]['close_time']),r.ET).date()
    q=r.xsp_quotes(day,11,59)
    if q is None: continue
    cv=r.digital_curve(q)
    if cv is None: continue
    e=int(datetime(day.year,day.month,day.day,12,0,tzinfo=r.ET).timestamp())
    for m in ms:
        k=r.kind(m)
        if not k: continue
        f=r.fair_index(cv,k)
        if f is None: continue
        c=r.candles_of('KXINX',m['ticker']); qq=r.quote_at(c,e) if c else None
        if not qq: continue
        diag.append({'ev':ev,'fair':f,'mid':(qq[0]+qq[1])/2,'spr':qq[1]-qq[0],'y':1 if m['result']=='yes' else 0})
d=pd.DataFrame(diag)
res['PM2 priced markets']=len(d); res['PM2 days']=int(d.ev.nunique())
res['PM2 mean |fair-mid|']=float((d.fair-d.mid).abs().mean()); res['PM2 mean kalshi spread']=float(d.spr.mean())
res['PM2 brier kalshi mid']=float(((d.mid-d.y)**2).mean()); res['PM2 brier options']=float(((d.fair-d.y)**2).mean())
res['PM2 share |fair-mid|>spread/2+0.03']=float(((d.fair-d.mid).abs()>d.spr/2+0.03).mean())
print(json.dumps(res,indent=1,default=str))
json.dump(res,open('research/round6_posthoc.json','w'),indent=1,default=str)
