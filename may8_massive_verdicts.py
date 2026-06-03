"""Extract May 8 2026 LULD halts + Massive/backtest filter verdict per halt.
Produces may8_massive_verdicts.csv for cross-checking against IBKR on the server."""
import pandas as pd, numpy as np
from pathlib import Path

UP_MIN=0.02; MIN_RVOL=1.0; RVOL_WIN=5; PAUSE=5
DATA_ROOT=Path('data_massive/Halt_model')
_cache={}

def load_bars(bf):
    k=str(bf or '').strip()
    if k in _cache: return _cache[k]
    norm=k.replace('\\','/'); cands=[Path(norm)]
    for sep in ['data_massive/Halt_model/','data_massive/']:
        if sep in norm: cands.append(DATA_ROOT/norm.split(sep)[-1])
    parts=norm.split('/')
    for i in range(1,len(parts)): cands.append(DATA_ROOT/'/'.join(parts[i:]))
    for c in cands:
        if c.exists():
            try:
                b=pd.read_parquet(c)
                if 'timestamp_utc' in b.columns: b=b.set_index('timestamp_utc')
                b.index=pd.to_datetime(b.index,utc=True); _cache[k]=b.sort_index(); return _cache[k]
            except Exception: pass
    _cache[k]=None; return None

ev=pd.read_parquet('data_massive/Halt_model/events_enriched_all_v3_combined.parquet')
ev['anchor_ts_utc']=pd.to_datetime(ev['anchor_ts_utc'],utc=True)
ev['event_date']=pd.to_datetime(ev['event_date'])
ev=ev[ev['halt_type'].str.contains('LULD',case=False,na=False)]
ev=ev[ev['bar_status'].isin(['ok','cached'])]
day=ev[ev['event_date']=='2026-05-08'].drop_duplicates(
    subset=['symbol_resolved','anchor_ts_utc']).sort_values('anchor_ts_utc')
print('May 8 unique LULD halts:',len(day))

open_utc=pd.Timestamp('2026-05-08 13:30:00',tz='UTC')
cutoff=pd.Timestamp('2026-05-08 19:49:00',tz='UTC')   # 15:49 ET
rows=[]; npass=0
for _,r in day.iterrows():
    sym=r['symbol_resolved']; anchor=r['anchor_ts_utc']
    bars=load_bars(r.get('bars_file'))
    verdict='SKIP'; reason=''; gap=None; rvol=None
    if anchor<open_utc or anchor>=cutoff:
        reason='outside hours'
    elif bars is None or bars.empty:
        reason='no bars'
    else:
        hs=anchor-pd.Timedelta(minutes=PAUSE); ss=hs-pd.Timedelta(minutes=RVOL_WIN)
        bh=bars[bars.index<hs]
        if bh.empty: reason='no pre-halt bar'
        else:
            phc=float(pd.to_numeric(bh.iloc[-1]['close'],errors='coerce'))
            dop=float(pd.to_numeric(bars.iloc[0]['close'],errors='coerce'))
            if not(np.isfinite(phc) and phc>0 and np.isfinite(dop) and dop>0): reason='bad px'
            else:
                gap=(phc-dop)/dop
                if gap<UP_MIN: reason='gap %.2f%%'%(gap*100)
                else:
                    sb=bars[(bars.index>=ss)&(bars.index<hs)]
                    bb=bars[(bars.index>=open_utc)&(bars.index<ss)]
                    if bb.empty or sb.empty: reason='empty rvol window'
                    else:
                        sv=float(sb['volume'].sum()); bv=float(bb['volume'].sum())
                        bm=(ss-open_utc).total_seconds()/60
                        if bv>0 and sv>0:
                            rvol=(sv/RVOL_WIN)/(bv/bm)
                            if rvol>=MIN_RVOL: verdict='PASS'; npass+=1; reason='ok'
                            else: reason='rvol %.2f'%rvol
                        else: reason='zero vol'
    rows.append({'symbol':sym,'resume_utc':anchor.strftime('%Y-%m-%d %H:%M:%S'),
                 'gap_up':round(gap,4) if gap is not None else '',
                 'rvol_massive':round(rvol,2) if rvol is not None else '',
                 'verdict_massive':verdict,'reason':reason})
out=pd.DataFrame(rows)
out.to_csv('may8_massive_verdicts.csv',index=False)
print('MASSIVE PASS count:',npass,'of',len(day),'halts')
print()
print(out[out['verdict_massive']=='PASS'][['symbol','resume_utc','gap_up','rvol_massive']].to_string(index=False))
print()
print('Saved may8_massive_verdicts.csv ({} rows)'.format(len(out)))
