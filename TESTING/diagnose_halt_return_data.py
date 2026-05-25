"""
Diagnose data alignment between halt_events.parquet and bar files.

Run from the Trade Halts root directory:
    python TESTING/diagnose_halt_return_data.py

Checks:
  1. Event file row count, columns, date range
  2. For a sample of events, attempts to load the bar file and verifies alignment
  3. Reports how many events have a loadable bar file vs missing
  4. Verifies anchor_ts_utc actually falls within the bar data range
"""
import pandas as pd
import numpy as np
from pathlib import Path
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
HALT_EVENTS_PATH = Path("TESTING/halt_events.parquet")
DATA_ROOT = Path("data_massive/Halt_model")

if not HALT_EVENTS_PATH.exists():
    raise FileNotFoundError(
        f"{HALT_EVENTS_PATH} not found. Run export_clean_5yr_data.py first."
    )

ev = pd.read_parquet(HALT_EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)

print("=" * 60)
print(f"HALT EVENTS: {HALT_EVENTS_PATH}")
print("=" * 60)
print(f"  Rows:           {len(ev)}")
print(f"  Columns:        {list(ev.columns)}")
print(f"  Date range:     {ev['anchor_ts_utc'].min()} to {ev['anchor_ts_utc'].max()}")
print(f"  Unique symbols: {ev['symbol_resolved'].nunique()}")
print(f"  halt_type:      {ev['halt_type'].value_counts().to_dict()}")
print(f"  bar_status:     {ev['bar_status'].value_counts().to_dict()}")
print(f"  anchor nulls:   {ev['anchor_ts_utc'].isna().sum()}")
print(f"  bars_file nulls:{ev['bars_file'].isna().sum()}")
print()


def resolve_bars_file(raw):
    """Try the same path resolution as the backtest scripts."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    candidates = [
        Path(raw),
        DATA_ROOT / Path(raw).name,
    ]
    for prefix in ["data_massive\\Halt_model\\", "data_massive/Halt_model/"]:
        norm = raw.replace("\\", "/")
        pnorm = prefix.replace("\\", "/")
        if norm.startswith(pnorm):
            candidates.append(DATA_ROOT / norm[len(pnorm):])
        elif norm.startswith("data_massive/"):
            candidates.append(DATA_ROOT / norm[len("data_massive/"):])
    for c in candidates:
        if c.exists():
            return c
    return None


# ── Check how many bar files are actually loadable ────────────────────────────
print("Checking bar file accessibility (sample of up to 200 events)...")
sample = ev.dropna(subset=["bars_file"]).head(200)
found = 0
missing = 0
anchor_ok = 0
anchor_gap = 0

for _, row in sample.iterrows():
    path = resolve_bars_file(row["bars_file"])
    if path is None:
        missing += 1
        continue
    found += 1
    try:
        b = pd.read_parquet(path)
        b.index = pd.to_datetime(b.index, utc=True)
        ts = row["anchor_ts_utc"]
        if b.index.min() <= ts <= b.index.max():
            anchor_ok += 1
        else:
            anchor_gap += 1
    except Exception:
        pass

print(f"  Bar files found:  {found} / {len(sample)}")
print(f"  Bar files missing:{missing} / {len(sample)}")
print(f"  Anchor within bar range: {anchor_ok}")
print(f"  Anchor outside bar range:{anchor_gap}")
print()

# ── Inspect one concrete event end-to-end ────────────────────────────────────
print("=" * 60)
print("SAMPLE EVENT END-TO-END CHECK")
print("=" * 60)
for _, row in ev.dropna(subset=["bars_file", "anchor_ts_utc"]).iterrows():
    path = resolve_bars_file(row["bars_file"])
    if path is None:
        continue
    try:
        b = pd.read_parquet(path)
        b.index = pd.to_datetime(b.index, utc=True)
        ts = row["anchor_ts_utc"]
        p0 = b.index.searchsorted(ts, side="left")
        if p0 >= len(b):
            continue
        print(f"  Symbol:       {row['symbol_resolved']}")
        print(f"  Event date:   {row['event_date']}")
        print(f"  anchor_ts:    {ts}")
        print(f"  Bar file:     {path}")
        print(f"  Bar rows:     {len(b)}")
        print(f"  Bar index:    {b.index.dtype}, tz={b.index.tz}")
        print(f"  Bar range:    {b.index.min()} to {b.index.max()}")
        print(f"  Entry bar[{p0}]: ts={b.index[p0]}  close={b.iloc[p0]['close']}")
        print(f"  Columns:      {list(b.columns)}")
        break
    except Exception as e:
        print(f"  Error: {e}")
        continue
