"""
Export the full 5-year combined halt event dataset to TESTING/halt_events.parquet.

Source: data_massive/Halt_model/events_enriched_all_v3_combined.parquet
        (combined from all quarterly halt_model_*_v2 folders)

Bar files are NOT copied — they live in the quarterly data_massive/Halt_model/ folders
and are referenced by the bars_file column in the events parquet. The backtest scripts
resolve those paths directly; there is no need to duplicate ~GB of bar data into TESTING.

Run from the Trade Halts root directory:
    python TESTING/export_clean_5yr_data.py
"""
import pandas as pd
from pathlib import Path
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

# ── Source: the full combined 5-year events file ───────────────────────────────
SRC_EVENT = Path("data_massive/Halt_model/events_enriched_all_v3_combined.parquet")
DST_EVENT = Path("TESTING/halt_events.parquet")

if not SRC_EVENT.exists():
    raise FileNotFoundError(
        f"Source not found: {SRC_EVENT}\n"
        "Run combine_all_v3_events.py first to build the combined parquet."
    )

ev = pd.read_parquet(SRC_EVENT)
print(f"Loaded {len(ev)} raw events from {SRC_EVENT}")

# ── Apply the same filters used by luld_sim and the live bot ──────────────────
ev = ev[ev["resolve_status"].astype(str).eq("ok")]
ev = ev[ev["bar_status"].astype(str).isin(["ok", "cached"])]
ev = ev[ev["halt_type"].str.lower() == "luld pause"].copy()
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
ev = ev.dropna(subset=["anchor_ts_utc"])

# Deduplicate: one event per symbol per anchor timestamp
before = len(ev)
ev = ev.drop_duplicates(subset=["symbol_resolved", "anchor_ts_utc"])
print(f"After dedup: {len(ev)} events (removed {before - len(ev)} duplicates)")

# Market hours only: 9:30am – 4:00pm ET
hour_et = (
    ev["anchor_ts_utc"].dt.tz_convert(NY_TZ).dt.hour
    + ev["anchor_ts_utc"].dt.tz_convert(NY_TZ).dt.minute / 60
)
ev = ev[(hour_et >= 9.5) & (hour_et < 16.0)].copy()

# Sort chronologically
ev = ev.sort_values("anchor_ts_utc").reset_index(drop=True)

print(f"Final clean events: {len(ev)}")
print(f"Date range: {ev['anchor_ts_utc'].min()} to {ev['anchor_ts_utc'].max()}")
print(f"Unique symbols: {ev['symbol_resolved'].nunique()}")
print(f"bar_status counts:\n{ev['bar_status'].value_counts().to_string()}")

DST_EVENT.parent.mkdir(parents=True, exist_ok=True)
ev.to_parquet(DST_EVENT, index=False)
print(f"\nSaved to {DST_EVENT}")
