"""
Inspect volume characteristics of halt events in the backtest universe.
Run from Trade Halts root: python TESTING/_inspect_volume.py
"""
import pandas as pd
import numpy as np
from pathlib import Path
from zoneinfo import ZoneInfo

NY_TZ        = ZoneInfo("America/New_York")
DATA_ROOT    = Path("data_massive/Halt_model")
EVENTS_PATH  = Path("TESTING/halt_events.parquet")
LULD_PAUSE   = 5   # minutes

_cache = {}
def load_bars(bars_file):
    key = str(bars_file or "").strip()
    if key in _cache: return _cache[key]
    if not key: _cache[key] = None; return None
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

print(f"Sampling volume from {len(ev):,} events ...\n")

rows = []
for _, row in ev.iterrows():
    bars = load_bars(row.get("bars_file"))
    if bars is None or bars.empty or "volume" not in bars.columns:
        continue

    anchor     = row["anchor_ts_utc"]
    halt_start = anchor - pd.Timedelta(minutes=LULD_PAUSE)

    # pre-halt bars (before halt started)
    pre = bars[bars.index < halt_start]
    if pre.empty: continue

    # day bars (everything)
    day_vol_shares = float(bars["volume"].sum())
    day_vol_dollar = float((bars["volume"] * bars["close"]).sum())

    # pre-halt dollar volume
    pre_vol_dollar = float((pre["volume"] * pre["close"]).sum())
    pre_vol_shares = float(pre["volume"].sum())

    # first resumption bar
    resume = bars[bars.index >= anchor]
    resume_vol = float(resume.iloc[0]["volume"]) if not resume.empty else 0.0

    rows.append({
        "symbol":          row["symbol_resolved"],
        "event_date":      row["event_date"],
        "pre_vol_dollar":  pre_vol_dollar,
        "pre_vol_shares":  pre_vol_shares,
        "day_vol_dollar":  day_vol_dollar,
        "day_vol_shares":  day_vol_shares,
        "resume_vol":      resume_vol,
    })

df = pd.DataFrame(rows)
print(f"Events with volume data: {len(df):,}\n")

for col, label in [
    ("pre_vol_dollar",  "Pre-halt dollar volume ($)"),
    ("pre_vol_shares",  "Pre-halt share volume"),
    ("resume_vol",      "First resumption bar volume (shares)"),
    ("day_vol_dollar",  "Full day dollar volume ($)"),
]:
    s = df[col]
    print(f"{label}")
    print(f"  min    : {s.min():>15,.0f}")
    print(f"  p5     : {s.quantile(0.05):>15,.0f}")
    print(f"  p10    : {s.quantile(0.10):>15,.0f}")
    print(f"  p25    : {s.quantile(0.25):>15,.0f}")
    print(f"  median : {s.median():>15,.0f}")
    print(f"  p75    : {s.quantile(0.75):>15,.0f}")
    print(f"  p90    : {s.quantile(0.90):>15,.0f}")
    print(f"  p95    : {s.quantile(0.95):>15,.0f}")
    print(f"  max    : {s.max():>15,.0f}")
    print()

# how many events have zero or near-zero volume at resumption
zero_resume = (df["resume_vol"] < 100).sum()
print(f"Events with <100 shares in first resumption bar : {zero_resume:,}  ({zero_resume/len(df):.1%})")

low_pre = (df["pre_vol_dollar"] < 50_000).sum()
print(f"Events with <$50k pre-halt dollar volume       : {low_pre:,}  ({low_pre/len(df):.1%})")

low_pre2 = (df["pre_vol_dollar"] < 250_000).sum()
print(f"Events with <$250k pre-halt dollar volume      : {low_pre2:,}  ({low_pre2/len(df):.1%})")

low_pre3 = (df["pre_vol_dollar"] < 500_000).sum()
print(f"Events with <$500k pre-halt dollar volume      : {low_pre3:,}  ({low_pre3/len(df):.1%})")
