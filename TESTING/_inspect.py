import pandas as pd
from pathlib import Path

ev = pd.read_parquet('TESTING/halt_events.parquet')

print('halt_type values:')
print(ev['halt_type'].value_counts())
print('\nTotal events:', len(ev))

# Find a good sample with bars
sample = ev[(ev['bar_status']=='ok') & (ev['bar_rows_day'] > 10)].iloc[10]
print('\nSymbol:', sample['symbol_resolved'])
print('anchor_ts_utc:', sample['anchor_ts_utc'])

bf_str = str(sample['bars_file']).replace('\\', '/')
bf = Path(bf_str)
print('file exists:', bf.exists())

if bf.exists():
    b = pd.read_parquet(bf)
    print('\nBar columns:', b.columns.tolist())
    print('Bar shape:', b.shape)
    if 'timestamp_utc' in b.columns:
        b = b.set_index('timestamp_utc')
    b.index = pd.to_datetime(b.index, utc=True)
    b = b.sort_index()
    print(b.head(20).to_string())
else:
    # Try data_massive root
    parts = bf_str.split('data_massive/')
    if len(parts) > 1:
        bf2 = Path('data_massive') / parts[-1]
        print('trying:', bf2)
        print('exists:', bf2.exists())

# Also check what columns have prehalt price info
print('\nAll columns:', ev.columns.tolist())
print('\nSample halt event (good bars):')
print(sample[['symbol_resolved','event_date','halt_type','anchor_ts_utc','ret_1m','ret_5m','bar_rows_day']].to_dict())
