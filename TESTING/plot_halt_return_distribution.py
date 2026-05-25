"""
Clean $100/trade halt resumption backtest.

Loads the full 5-year halt_events.parquet (built by export_clean_5yr_data.py),
computes returns from resumption to a configurable exit (default: 4pm ET close),
and saves a professional equity curve + distribution chart.

Run from the Trade Halts root directory:
    python TESTING/plot_halt_return_distribution.py

Bar format expected:
  - Index named 'timestamp_utc', timezone-naive UTC datetime64
  - Columns: open, high, low, close, volume
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
HALT_EVENTS_PATH = Path("TESTING/halt_events.parquet")
DATA_ROOT        = Path("data_massive/Halt_model")
NY_TZ            = ZoneInfo("America/New_York")

START_EQUITY     = 30_000.0
PER_TRADE        = 100.0          # fixed dollar amount per trade
SLIP_BPS         = 10             # one-way slippage in basis points (10 bps = 0.10%)
MIN_PRICE        = 1.00           # skip stocks below this price at entry (post-slip)
EXIT_HOUR        = 16             # exit hour in ET (16 = 4pm, matches live MOC)
EXIT_MINUTE      = 0
OUT_PNG          = Path("TESTING/halt_equity_curve_professional.png")

# ── Bar file resolver (same logic as luld_sim) ────────────────────────────────
_bar_cache: dict = {}

def load_bars(bars_file) -> pd.DataFrame | None:
    key = str(bars_file or "").strip()
    if key in _bar_cache:
        return _bar_cache[key]
    if not key:
        _bar_cache[key] = None
        return None

    raw = key
    candidates = [Path(raw)]
    norm = raw.replace("\\", "/")
    for prefix in ["data_massive/Halt_model/", "data_massive\\Halt_model\\"]:
        pnorm = prefix.replace("\\", "/")
        if norm.startswith(pnorm):
            candidates.append(DATA_ROOT / norm[len(pnorm):])
        elif norm.startswith("data_massive/"):
            candidates.append(DATA_ROOT / norm[len("data_massive/"):])
    # Fallback: filename only under data_root (won't work for symbol subdirs, just a safety net)
    candidates.append(DATA_ROOT / Path(raw).name)

    for c in candidates:
        if not c.exists():
            continue
        try:
            b = pd.read_parquet(c)
            # Bars are stored with timestamp_utc as the index (timezone-naive UTC)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            b = b.sort_index()
            _bar_cache[key] = b
            return b
        except Exception:
            pass

    _bar_cache[key] = None
    return None

# ── Load events ───────────────────────────────────────────────────────────────
if not HALT_EVENTS_PATH.exists():
    raise FileNotFoundError(
        f"{HALT_EVENTS_PATH} not found.\n"
        "Run:  python TESTING/export_clean_5yr_data.py"
    )

ev = pd.read_parquet(HALT_EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
print(f"Loaded {len(ev)} events from {HALT_EVENTS_PATH}")

# ── Main backtest loop ────────────────────────────────────────────────────────
slip_mult_entry = 1 + SLIP_BPS / 10_000
slip_mult_exit  = 1 - SLIP_BPS / 10_000

trades   = []
skipped  = 0
executed = 0

for _, row in ev.iterrows():
    ts     = row["anchor_ts_utc"]
    symbol = row.get("symbol_resolved", "")

    b = load_bars(row.get("bars_file"))
    if b is None or b.empty or "close" not in b.columns:
        skipped += 1
        continue

    # ── Entry: first bar at or after resumption ───────────────────────────────
    p0 = b.index.searchsorted(ts, side="left")
    if p0 >= len(b):
        skipped += 1
        continue

    entry_ts = b.index[p0]
    entry_px = float(pd.to_numeric(b.iloc[p0]["close"], errors="coerce"))
    if not np.isfinite(entry_px) or entry_px <= 0:
        skipped += 1
        continue

    entry_exec = entry_px * slip_mult_entry
    if entry_exec < MIN_PRICE:
        skipped += 1
        continue

    # ── Exit: last bar at or before EXIT_HOUR:EXIT_MINUTE ET ─────────────────
    event_date = pd.Timestamp(str(row["event_date"]))
    exit_utc   = pd.Timestamp(
        year=event_date.year, month=event_date.month, day=event_date.day,
        hour=EXIT_HOUR, minute=EXIT_MINUTE, tz=NY_TZ
    ).tz_convert("UTC")
    pend = b.index.searchsorted(exit_utc, side="right") - 1
    if pend < p0:
        skipped += 1
        continue

    exit_ts = b.index[pend]
    exit_px = float(pd.to_numeric(b.iloc[pend]["close"], errors="coerce"))
    if not np.isfinite(exit_px) or exit_px <= 0:
        skipped += 1
        continue

    exit_exec = exit_px * slip_mult_exit

    # ── PnL: fixed $100 notional ──────────────────────────────────────────────
    position = PER_TRADE / entry_exec
    ret      = exit_exec / entry_exec - 1.0
    pnl      = PER_TRADE * ret

    trades.append({
        "entry_ts": entry_ts,
        "exit_ts":  exit_ts,
        "symbol":   symbol,
        "entry_px": entry_exec,
        "exit_px":  exit_exec,
        "ret":      ret,
        "pnl":      pnl,
    })
    executed += 1

print(f"Executed: {executed}   Skipped: {skipped}")

if not trades:
    print("No trades — check that halt_events.parquet is populated and bar files are accessible.")
    raise SystemExit(1)

# ── Build equity curve (sorted by exit time) ──────────────────────────────────
tr = pd.DataFrame(trades).sort_values("exit_ts").reset_index(drop=True)
tr["exit_ts"]  = pd.to_datetime(tr["exit_ts"], utc=True).dt.tz_localize(None)
tr["entry_ts"] = pd.to_datetime(tr["entry_ts"], utc=True).dt.tz_localize(None)

curve_y = START_EQUITY + tr["pnl"].cumsum().values
curve_x = tr["exit_ts"].values

# ── Statistics ────────────────────────────────────────────────────────────────
wr       = (tr["ret"] > 0).mean()
avg_ret  = tr["ret"].mean() * 100
med_ret  = tr["ret"].median() * 100
total_pnl = tr["pnl"].sum()
roll_max = np.maximum.accumulate(curve_y)
drawdown = curve_y - roll_max

print()
print(f"  Trades:        {len(tr):,}")
print(f"  Win rate:      {wr:.1%}")
print(f"  Avg return:    {avg_ret:+.2f}%")
print(f"  Median return: {med_ret:+.2f}%")
print(f"  Total PnL:     ${total_pnl:,.0f}")
print(f"  Final equity:  ${curve_y[-1]:,.0f}")
print(f"  Max drawdown:  ${drawdown.min():,.0f}")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(14, 8))
gs  = fig.add_gridspec(2, 2, height_ratios=[2, 1])

# Equity curve
ax1 = fig.add_subplot(gs[0, :])
ax1.plot(curve_x, curve_y, color="#348abd", lw=1.5, label=f"Equity ($100/trade, {SLIP_BPS}bps slip)")
ax1.axhline(START_EQUITY, color="#d62728", linestyle="--", lw=1, label="Start equity")
ax1.set_title(f"Cumulative Equity: Buy $100 on LULD Resumption, Exit at {EXIT_HOUR}:{'%02d'%EXIT_MINUTE} ET",
              fontsize=13, fontweight="bold")
ax1.set_ylabel("Equity ($)")
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax1.legend(loc="upper left", fontsize=9)
ax1.grid(True, alpha=0.3)
ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
ax1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

# Drawdown
ax2 = fig.add_subplot(gs[1, 0])
ax2.fill_between(curve_x, drawdown, 0, color="#e24a33", alpha=0.4)
ax2.plot(curve_x, drawdown, color="#e24a33", lw=1, label="Drawdown ($)")
ax2.axhline(0, color="#888", linestyle=":", lw=1)
ax2.set_title("Drawdown ($)", fontsize=12)
ax2.set_ylabel("Drawdown ($)")
ax2.set_xlabel("Date")
ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax2.grid(True, alpha=0.3)
ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
ax2.legend(fontsize=9)

# Return distribution (clipped to [-50%, +100%] so outliers don't compress the chart)
ax3 = fig.add_subplot(gs[1, 1])
rets_pct = (tr["ret"] * 100).clip(-50, 100)
ax3.hist(rets_pct, bins=80, color="#348abd", alpha=0.85, edgecolor="#222", linewidth=0.3)
ax3.axvline(0, color="#d62728", lw=1, linestyle="--")
ax3.set_title(f"Trade Return Distribution  (WR={wr:.0%}, Avg={avg_ret:+.1f}%)", fontsize=12)
ax3.set_xlabel("Return per Trade (%)")
ax3.set_ylabel("Count")
ax3.grid(True, alpha=0.2)

fig.suptitle("LULD Halt Resumption Strategy — $100/Trade Fixed Notional",
             fontsize=15, fontweight="bold")
plt.tight_layout()
fig.subplots_adjust(top=0.93)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight")
print(f"\nSaved chart: {OUT_PNG}")
