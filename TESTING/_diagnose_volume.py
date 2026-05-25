"""
Diagnose why volume data is missing for ~half the events.
Run from Trade Halts root: python TESTING/_diagnose_volume.py
"""
import pandas as pd
import numpy as np
from pathlib import Path
from collections import Counter

DATA_ROOT   = Path("data_massive/Halt_model")
EVENTS_PATH = Path("TESTING/halt_events.parquet")

ev = pd.read_parquet(EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
ev["event_date"]    = pd.to_datetime(ev["event_date"])

print(f"Total events      : {len(ev):,}")
print(f"bar_status counts :")
print(ev["bar_status"].value_counts().to_string())
print()

# Work only with bar_status == ok
ok = ev[ev["bar_status"] == "ok"].copy()
print(f"bar_status=ok     : {len(ok):,}")

# ── check 1: can we even find the file? ───────────────────────────────────────
def resolve_path(bars_file):
    if not bars_file or pd.isna(bars_file):
        return None, "no_path"
    norm = str(bars_file).replace("\\", "/")
    candidates = [Path(norm)]
    for sep in ["data_massive/Halt_model/", "data_massive/"]:
        if sep in norm:
            candidates.append(DATA_ROOT / norm.split(sep)[-1])
    parts = norm.split("/")
    for i in range(1, len(parts)):
        candidates.append(DATA_ROOT / "/".join(parts[i:]))
    for c in candidates:
        if c.exists():
            return c, "found"
    return None, "not_found"

print("\nChecking file existence for all bar_status=ok events...")
statuses = []
not_found_examples = []
for _, row in ok.iterrows():
    path, status = resolve_path(row.get("bars_file"))
    statuses.append(status)
    if status == "not_found" and len(not_found_examples) < 5:
        not_found_examples.append(str(row.get("bars_file")))

status_counts = Counter(statuses)
print(f"  File found     : {status_counts['found']:,}")
print(f"  File NOT found : {status_counts['not_found']:,}")
print(f"  No path        : {status_counts['no_path']:,}")

print("\nExample not-found paths:")
for p in not_found_examples:
    print(f"  {p}")

# ── check 2: for files that exist, do they have volume? ───────────────────────
print("\nChecking column contents of found files (sample of 200)...")
found_rows = ok[[resolve_path(r)[1] == "found" for r in ok["bars_file"]]].head(200)

has_vol   = 0
empty     = 0
no_vol_col = 0
zero_vol  = 0

for _, row in found_rows.iterrows():
    path, _ = resolve_path(row.get("bars_file"))
    try:
        b = pd.read_parquet(path)
        if b.empty:
            empty += 1
        elif "volume" not in b.columns:
            no_vol_col += 1
        elif b["volume"].sum() == 0:
            zero_vol += 1
        else:
            has_vol += 1
    except Exception as e:
        print(f"  ERROR reading {path}: {e}")

print(f"  Has volume data  : {has_vol}")
print(f"  Empty file       : {empty}")
print(f"  No volume column : {no_vol_col}")
print(f"  Zero volume      : {zero_vol}")

# ── check 3: which DATA directories exist and what date ranges? ───────────────
print("\nData directories under data_massive/Halt_model:")
if DATA_ROOT.exists():
    subdirs = sorted([d for d in DATA_ROOT.iterdir() if d.is_dir()])
    for d in subdirs:
        parquets = list(d.rglob("*.parquet"))
        print(f"  {d.name:<60} {len(parquets):>6,} parquet files")
else:
    print("  DATA_ROOT does not exist!")

# ── check 4: date range of events vs date range of found files ────────────────
print("\nEvent date range in dataset:")
print(f"  earliest: {ev['event_date'].min().date()}")
print(f"  latest  : {ev['event_date'].max().date()}")

print("\nEvents by year:")
print(ev.groupby(ev["event_date"].dt.year).size().to_string())

# check which years have missing files
ok["resolved"] = [resolve_path(r)[1] for r in ok["bars_file"]]
ok["year"]     = ok["event_date"].dt.year
pivot = ok.groupby(["year","resolved"]).size().unstack(fill_value=0)
print("\nFile found/not_found by year:")
print(pivot.to_string())
