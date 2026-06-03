"""
run_full_backtest.py
====================
Runs the LULD halt resumption strategy on three datasets side by side:
  1. TESTING subset (what the README backtest used)
  2. Full combined dataset (complete 5-year picture)
  3. Out-of-sample only (honest unbiased result)

Usage:
    python run_full_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Strategy parameters (match live bot exactly) ──────────────────────────────
NY_TZ           = ZoneInfo("America/New_York")
START_EQUITY    = 1_000.0
POSITION_PCT    = 0.10       # 10% per trade (matches live bot)
MAX_POSITION    = 100_000.0  # $100k cap (matches live bot)
CASH_RESERVE    = 0.0
UP_MIN_MOVE     = 0.02       # 2% gap-up
MIN_RVOL        = 1.0
RVOL_WINDOW_MIN = 5
MARKET_OPEN     = (9, 30)
ENTRY_CUTOFF    = (15, 49)
EXIT_HOUR       = 15
EXIT_MIN        = 50
LULD_PAUSE_MIN  = 5
MIN_TRADE       = 5.0

# ── Dataset paths ─────────────────────────────────────────────────────────────
DATASETS = {
    "TESTING subset (README backtest)":
        "TESTING/halt_events.parquet",
    "Full combined 2021-2026":
        "data_massive/Halt_model/events_enriched_all_v3_combined.parquet",
    "Out-of-sample 2025Q1-Q3":
        "data/halt_model_oos_2025Q1Q3_combined/events_enriched.parquet",
    "Out-of-sample 2025Q4-2026Q1":
        "data/halt_model_oos_2025Q4_2026Q1/events_enriched.parquet",
}

DATA_ROOT = Path("data_massive/Halt_model")
_cache: dict = {}


def load_bars(bars_file):
    key = str(bars_file or "").strip()
    if key in _cache:
        return _cache[key]
    if not key:
        _cache[key] = None
        return None

    norm = key.replace("\\", "/")
    candidates = [Path(norm)]
    for sep in ["data_massive/Halt_model/", "data_massive/"]:
        if sep in norm:
            tail = norm.split(sep)[-1]
            candidates.append(DATA_ROOT / tail)
    parts = norm.split("/")
    for i in range(1, len(parts)):
        candidates.append(DATA_ROOT / "/".join(parts[i:]))

    for c in candidates:
        if not c.exists():
            continue
        try:
            b = pd.read_parquet(c)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            b = b.sort_index()
            _cache[key] = b
            return b
        except Exception:
            pass

    _cache[key] = None
    return None


def ny_to_utc(date, hour, minute):
    ts = pd.Timestamp(year=date.year, month=date.month, day=date.day,
                      hour=hour, minute=minute, second=0, tzinfo=NY_TZ)
    return ts.tz_convert("UTC")


def run_backtest(events_path: str, label: str) -> dict:
    p = Path(events_path)
    if not p.exists():
        print(f"  SKIPPED: {events_path} not found")
        return {}

    ev = pd.read_parquet(p)
    ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
    ev["event_date"]    = pd.to_datetime(ev["event_date"])

    # Filter to LULD pauses with bar data only
    luld_mask = ev["halt_type"].str.contains("LULD", case=False, na=False)
    ok_mask   = ev["bar_status"].isin(["ok", "cached"])
    ev = ev[luld_mask & ok_mask].sort_values("anchor_ts_utc").reset_index(drop=True)

    print(f"\n  {label}")
    print(f"  Events (LULD + bar ok): {len(ev):,}")
    print(f"  Date range: {ev['event_date'].min().date()} to {ev['event_date'].max().date()}")

    equity   = START_EQUITY
    trades   = []
    n_up     = 0
    n_fills  = 0

    for date, day_ev in ev.groupby("event_date"):
        available  = equity * (1.0 - CASH_RESERVE)
        open_utc   = ny_to_utc(date, *MARKET_OPEN)
        cutoff_utc = ny_to_utc(date, *ENTRY_CUTOFF)
        exit_utc   = ny_to_utc(date, EXIT_HOUR, EXIT_MIN)
        positions  = {}

        for _, row in day_ev.sort_values("anchor_ts_utc").iterrows():
            bars = load_bars(row.get("bars_file"))
            if bars is None or bars.empty or "close" not in bars.columns:
                continue

            anchor = row["anchor_ts_utc"]
            if anchor < open_utc or anchor >= cutoff_utc:
                continue

            # Pre-halt price
            pre_bars = bars[bars.index < anchor]
            if pre_bars.empty:
                continue
            pre_close = float(pd.to_numeric(pre_bars.iloc[-1]["close"], errors="coerce"))
            if not np.isfinite(pre_close) or pre_close <= 0:
                continue

            # Gap-up filter
            halt_start  = anchor - pd.Timedelta(minutes=LULD_PAUSE_MIN)
            before_halt = bars[bars.index < halt_start]
            if before_halt.empty:
                continue
            pre_halt_close = float(pd.to_numeric(before_halt.iloc[-1]["close"], errors="coerce"))
            if not np.isfinite(pre_halt_close) or pre_halt_close <= 0:
                continue

            day_open_px = float(pd.to_numeric(bars.iloc[0]["close"], errors="coerce"))
            if not np.isfinite(day_open_px) or day_open_px <= 0:
                continue

            move = (pre_halt_close - day_open_px) / day_open_px
            if move < UP_MIN_MOVE:
                continue
            n_up += 1
            pre_close = pre_halt_close

            # RVOL filter
            surge_start   = halt_start - pd.Timedelta(minutes=RVOL_WINDOW_MIN)
            surge_bars    = bars[(bars.index >= surge_start) & (bars.index < halt_start)]
            baseline_bars = bars[(bars.index >= open_utc) & (bars.index < surge_start)]

            if baseline_bars.empty or surge_bars.empty:
                continue

            surge_vol    = float(surge_bars["volume"].sum())
            baseline_vol = float(baseline_bars["volume"].sum())
            baseline_min = (surge_start - open_utc).total_seconds() / 60

            if baseline_rate := baseline_vol / max(baseline_min, 1.0):
                rvol = (surge_vol / RVOL_WINDOW_MIN) / baseline_rate
            else:
                continue

            if baseline_vol <= 0 or rvol < MIN_RVOL:
                continue

            # Entry
            resume_bars = bars[bars.index >= anchor]
            if resume_bars.empty:
                continue
            first_bar_open = float(pd.to_numeric(
                resume_bars.iloc[0].get("open", resume_bars.iloc[0]["close"]),
                errors="coerce"))
            if not np.isfinite(first_bar_open) or first_bar_open <= 0:
                continue

            n_fills += 1
            entry_exec = first_bar_open  # no slippage model
            pos_usd    = min(equity * POSITION_PCT, MAX_POSITION, available)
            if pos_usd < MIN_TRADE:
                continue

            shares     = pos_usd / entry_exec
            available -= pos_usd

            key = f"{row['symbol_resolved']}_{row['event_id']}"
            positions[key] = {
                "symbol":    row["symbol_resolved"],
                "entry_ts":  anchor,
                "entry_px":  entry_exec,
                "shares":    shares,
                "cost":      pos_usd,
                "rvol":      round(rvol, 2),
                "bars_file": row.get("bars_file"),
            }

        # EOD exit
        for key, pos in positions.items():
            b = load_bars(pos["bars_file"])
            if b is None or b.empty:
                continue
            exit_bars = b[b.index <= exit_utc]
            if exit_bars.empty:
                exit_bars = b
            exit_px_raw = float(pd.to_numeric(exit_bars.iloc[-1]["close"], errors="coerce"))
            if not np.isfinite(exit_px_raw) or exit_px_raw <= 0:
                continue

            gross_pnl = (exit_px_raw - pos["entry_px"]) * pos["shares"]
            equity   += gross_pnl

            trades.append({
                "date":         date.strftime("%Y-%m-%d"),
                "symbol":       pos["symbol"],
                "entry_px":     round(pos["entry_px"], 4),
                "exit_px":      round(exit_px_raw, 4),
                "shares":       round(pos["shares"], 4),
                "pos_usd":      round(pos["cost"], 2),
                "pnl":          round(gross_pnl, 2),
                "ret_pct":      round((exit_px_raw / pos["entry_px"] - 1) * 100, 4),
                "equity_after": round(equity, 2),
                "rvol":         pos["rvol"],
            })

    if not trades:
        print("  No trades generated.")
        return {}

    tr  = pd.DataFrame(trades)
    wr  = (tr["ret_pct"] > 0).mean()
    avg = tr["ret_pct"].mean()
    med = tr["ret_pct"].median()
    eq  = tr["equity_after"].values
    rm  = np.maximum.accumulate(eq)
    dd  = ((eq - rm) / rm * 100).min()
    per_day = tr.groupby("date").size()

    # Save trades CSV
    out_path = Path(f"TESTING/backtest_{label.replace(' ', '_').replace('/', '-')}.csv")
    tr.to_csv(out_path, index=False)

    print(f"  Trades:        {len(tr):,}")
    print(f"  Win rate:      {wr:.1%}")
    print(f"  Avg trade:     {avg:+.2f}%")
    print(f"  Median trade:  {med:+.2f}%")
    print(f"  Max drawdown:  {dd:.1f}%")
    print(f"  Start equity:  ${START_EQUITY:,.2f}")
    print(f"  End equity:    ${eq[-1]:,.2f}")
    print(f"  Total return:  {(eq[-1]/START_EQUITY - 1)*100:+,.0f}%")
    print(f"  Trades/day:    {per_day.mean():.2f} avg, {per_day.max()} max")
    print(f"  Saved to:      {out_path}")

    return {"label": label, "trades": len(tr), "wr": wr, "median": med,
            "final_eq": eq[-1], "max_dd": dd, "df": tr}


# ── Run all backtests ──────────────────────────────────────────────────────────
print("=" * 60)
print("LULD HALT RESUMPTION - FULL BACKTEST SUITE")
print("Parameters: 10% sizing, $100k cap, no slippage model")
print("=" * 60)

results = {}
for label, path in DATASETS.items():
    results[label] = run_backtest(path, label)

# ── Comparison summary ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("SUMMARY COMPARISON")
print("=" * 60)
print(f"{'Dataset':<35} {'Trades':>7} {'Win%':>6} {'Med%':>7} {'FinalEq':>12} {'MaxDD':>7}")
print("-" * 60)
for label, r in results.items():
    if r:
        print(f"{label[:35]:<35} {r['trades']:>7,} {r['wr']:>6.1%} "
              f"{r['median']:>7.2f}% ${r['final_eq']:>11,.0f} {r['max_dd']:>6.1f}%")

print("\nDone. Trade CSVs saved to TESTING/")
