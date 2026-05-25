import pandas as pd
from pathlib import Path

ev = pd.read_parquet('TESTING/halt_events.parquet')

# Find which data roots actually exist
data_roots = list(Path('data_massive').glob('*')) if Path('data_massive').exists() else []
print('data_massive subdirs:', [d.name for d in data_roots[:5]])

# Find an event whose bar file actually exists
found = None
for _, row in ev[(ev['bar_status']=='ok') & (ev['bar_rows_day'] > 5)].iterrows():
    bf_str = str(row['bars_file']).replace('\\', '/')
    # Try several path combos
    candidates = [
        Path(bf_str),
        Path('data_massive') / '/'.join(bf_str.split('/')[2:]),  # strip first 2 path parts
    ]
    # Also try with DATA_ROOT = data_massive/Halt_model
    for dr in data_roots:
        tail = bf_str.split('data_massive/')[-1] if 'data_massive/' in bf_str else bf_str
        candidates.append(dr / tail)
    for c in candidates:
        if c.exists():
            found = (row, c)
            break
    if found:
        break

if found:
    row, bf = found
    print('\nFound file:', bf)
    print('Symbol:', row['symbol_resolved'], 'Date:', row['event_date'])
    print('anchor_ts_utc:', row['anchor_ts_utc'])
    b = pd.read_parquet(bf)
    print('\nBar columns:', b.columns.tolist())
    if 'timestamp_utc' in b.columns:
        b = b.set_index('timestamp_utc')
    b.index = pd.to_datetime(b.index, utc=True)
    b = b.sort_index()

    anchor = pd.to_datetime(row['anchor_ts_utc'], utc=True)
    # Show bars around the halt resumption
    window = b[(b.index >= anchor - pd.Timedelta(minutes=5)) &
               (b.index <= anchor + pd.Timedelta(minutes=10))]
    print(f'\nBars around halt resumption ({anchor}):')
    print(window.to_string())
else:
    print('No file found. Checking raw bars_file paths:')
    for _, row in ev[(ev['bar_status']=='ok')].head(5).iterrows():
        print(' ', row['bars_file'])
