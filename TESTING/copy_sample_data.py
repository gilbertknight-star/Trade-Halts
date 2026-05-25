"""
Copy a sample of halt event data and a sample of second-level bar parquet files into the TESTING folder for a clean, local backtest workspace.
- Copies the latest halt event CSV or parquet to TESTING/halt_events_sample.csv (or .parquet)
- Copies up to 10 bar parquet files from known bar directories to TESTING/bars_by_symbol_day/
"""
import shutil
import glob
import os

TESTING_DIR = "TESTING"
BARS_DIR = os.path.join(TESTING_DIR, "bars_by_symbol_day")

# 1. Copy a halt event file (CSV or parquet)
halt_event_candidates = [
    "Trade_Halts_5_8_2026.csv",
    "Corrected Trade halts new ready.csv",
    "data_massive/Halt_model/events_enriched_all_v3_combined.parquet",
]
for src in halt_event_candidates:
    if os.path.exists(src):
        dst = os.path.join(TESTING_DIR, os.path.basename(src))
        shutil.copy2(src, dst)
        print(f"Copied halt event file: {src} -> {dst}")
        break

# 2. Copy up to 10 bar parquet files from known bar directories
bar_dirs = [
    "trade_halts_5_6_2026/bars_by_symbol_day",
    "data_massive/halt_model_5_8_2026/bars_by_symbol_day",
    "data/halt_model_oos_2025Q2/bars_by_symbol_day",
]
copied = 0
for bar_dir in bar_dirs:
    if os.path.exists(bar_dir):
        for f in glob.glob(os.path.join(bar_dir, "*.parquet")):
            shutil.copy2(f, os.path.join(BARS_DIR, os.path.basename(f)))
            copied += 1
            print(f"Copied bar file: {f} -> {BARS_DIR}")
            if copied >= 10:
                break
    if copied >= 10:
        break
if copied == 0:
    print("No bar parquet files found to copy.")
else:
    print(f"Copied {copied} bar parquet files to {BARS_DIR}.")
