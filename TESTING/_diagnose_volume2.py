"""
Pinpoint exactly why _inspect_volume only returned 4,953 events.
All 9,851 files exist and have volume — so the drop must be happening
inside the per-event logic. This script traces each failure reason.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import Counter

NY_TZ       = ZoneInfo("America/New_York")
DATA_ROOT   = Path("data_massive/Halt_model")
EVENTS_PATH = Path("TESTING/halt_events.parquet")
LULD_PAUSE  = 5

_cache = {}
def load_bars(bars_file):
    key = str(bars_file or "").strip()
    if key in _cache: return _cache[key]
    norm = key.replace("\\", "/")
    candidates = [Path(norm)]
    for sep in ["data_massive/Halt_model/", "data_massive/"]:
        if sep in norm:
            candidates.append(DATA_ROOT / norm.split(sep)[-1])
    parts = norm.split("/")
    for i in range(1, len(parts)):
        candidates.append(DATA_ROOT / "/".join(parts[i:]))
    for c in candidates:
        if not c.exists(): continue
        try:
            b = pd.read_parquet(c)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            b = b.sort_index()
            _cache[key] = b; return b
        except Exception: pass
    _cache[key] = None; return None

ev = pd.read_parquet(EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
ev = ev[ev["bar_status"] == "ok"].reset_index(drop=True)

reasons = Counter()
anchor_times = []   # NY hour of anchor for "no pre-halt bars" events
halt_durations = [] # estimated halt duration for those events

for _, row in ev.iterrows():
    bars = load_bars(row.get("bars_file"))
    if bars is None:
        reasons["bars_load_failed"] += 1
        continue
    if "volume" not in bars.columns:
        reasons["no_volume_col"] += 1
        continue

    anchor     = row["anchor_ts_utc"]
    halt_start = anchor - pd.Timedelta(minutes=LULD_PAUSE)

    pre = bars[bars.index < halt_start]
    if pre.empty:
        reasons["no_pre_halt_bars"] += 1
        # record when the halt happened in NY time
        anchor_ny = anchor.astimezone(NY_TZ)
        anchor_times.append(anchor_ny.hour + anchor_ny.minute / 60)
        # how far is the first bar from the anchor?
        if not bars.empty:
            first_bar = bars.index[0]
            gap_min = (anchor - first_bar).total_seconds() / 60
            halt_durations.append(gap_min)
        continue

    reasons["ok"] += 1

print("Failure breakdown:")
total = sum(reasons.values())
for reason, count in reasons.most_common():
    print(f"  {reason:<25}: {count:>5,}  ({count/total:.1%})")

print(f"\nTotal: {total:,}")

if anchor_times:
    at = np.array(anchor_times)
    print(f"\n'no_pre_halt_bars' events — anchor time in NY:")
    print(f"  count  : {len(at):,}")
    print(f"  median : {np.median(at):.2f}h  ({int(np.median(at))}:{int((np.median(at)%1)*60):02d} AM/PM)")
    print(f"  <9:40am: {(at < 9.67).sum():,}  ({(at < 9.67).mean():.1%})  (halted at/near open)")
    print(f"  <10am  : {(at < 10.0).sum():,}  ({(at < 10.0).mean():.1%})")

    # show distribution by hour bucket
    print(f"\n  By NY hour:")
    for h in range(9, 17):
        cnt = ((at >= h) & (at < h+1)).sum()
        bar = "#" * (cnt // 10)
        print(f"    {h:02d}:xx  {cnt:>4,}  {bar}")

if halt_durations:
    hd = np.array(halt_durations)
    # negative = first bar is AFTER anchor (bars start after resumption)
    # positive = first bar is before anchor
    print(f"\n'no_pre_halt_bars' — gap between first bar and anchor (minutes):")
    print(f"  (negative = first bar is AFTER anchor, i.e. no pre-halt bars at all)")
    print(f"  min   : {hd.min():+.1f}")
    print(f"  p25   : {np.percentile(hd,25):+.1f}")
    print(f"  median: {np.median(hd):+.1f}")
    print(f"  p75   : {np.percentile(hd,75):+.1f}")
    print(f"  max   : {hd.max():+.1f}")
    print(f"  First bar AFTER anchor (truly no data): {(hd < 0).sum():,}")
    print(f"  First bar within 10min of anchor      : {((hd >= 0) & (hd < 10)).sum():,}")
    print(f"  First bar >10min before anchor        : {(hd >= 10).sum():,}")
