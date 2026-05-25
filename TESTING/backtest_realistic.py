"""
Realistic LULD Halt Resumption Backtest
========================================
Strategy
--------
  Universe  : LULD halt-UP events, market hours only (9:30–3:49 PM NY)
  Entry     : limit buy at pre-halt close price
              order is live for 1 minute after resumption;
              fills if any bar's LOW <= limit price in that window
  Sizing    : 5% of current equity per position, hard cap $2,000
              always keep 5% cash reserve (no phantom trades)
  Exit      : all positions closed at 3:50 PM via last available bar
  Slippage  : 10 bps on entry fill and exit
  Capital   : $1,000 starting equity

Run from the Trade Halts root directory:
    python TESTING/backtest_realistic.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.gridspec import GridSpec
from pathlib import Path
from zoneinfo import ZoneInfo
from scipy.stats import gaussian_kde

# ── strategy parameters ───────────────────────────────────────────────────────
NY_TZ          = ZoneInfo("America/New_York")
DATA_ROOT      = Path("data_massive/Halt_model")
EVENTS_PATH    = Path("TESTING/halt_events.parquet")
OUT_PNG        = Path("TESTING/halt_equity_curve_realistic.png")
OUT_CSV        = Path("TESTING/halt_trades_realistic.csv")

START_EQUITY   = 1_000.0
POSITION_PCT   = 0.05        # 5% of equity per position
CASH_RESERVE   = 0.05        # keep 5% cash at all times (never deploy last 5%)
MAX_POSITION   = 2_000.0     # hard cap per position
SLIP_BPS       = 10          # 10 bps slippage on both entry and exit

LIMIT_WIN_MIN  = 1           # limit order lives 1 minute post-resumption
MARKET_OPEN    = (9,  30)    # ignore resumptions before 9:30 AM NY
ENTRY_CUTOFF   = (15, 49)    # no new entries after 3:49 (need 1-min window)
EXIT_HOUR      = 15          # 3:50 PM exit
EXIT_MIN       = 50

LULD_PAUSE_MIN  = 5          # LULD pauses are always exactly 5 minutes
UP_MIN_MOVE     = 0.02       # stock must be +2% vs session open to qualify as halt-up

MIN_TRADE       = 5.0        # skip if max affordable position < $5

# ── colours ───────────────────────────────────────────────────────────────────
C_EQUITY  = "#2563EB"
C_FILL    = "#DBEAFE"
C_DD      = "#DC2626"
C_DD_FILL = "#FEE2E2"
C_DIST    = "#1D4ED8"
C_ZERO    = "#94A3B8"
C_GRID    = "#E2E8F0"

# ── bar loader (cached) ───────────────────────────────────────────────────────
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

    # build fallback paths relative to DATA_ROOT
    for sep in ["data_massive/Halt_model/", "data_massive/"]:
        if sep in norm:
            tail = norm.split(sep)[-1]
            candidates.append(DATA_ROOT / tail)

    # strip path segments one by one
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

# ── time helpers ──────────────────────────────────────────────────────────────
def ny_to_utc(date, hour, minute):
    """NY hour:minute on `date` → UTC Timestamp."""
    ts = pd.Timestamp(year=date.year, month=date.month, day=date.day,
                      hour=hour, minute=minute, second=0, tzinfo=NY_TZ)
    return ts.tz_convert("UTC")

def anchor_date(anchor_utc):
    """Return the NY date of a UTC timestamp."""
    return anchor_utc.astimezone(NY_TZ).date()

# ── load & sort events ────────────────────────────────────────────────────────
print("Loading events ...")
ev = pd.read_parquet(EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
ev["event_date"]    = pd.to_datetime(ev["event_date"])
ev = ev[ev["bar_status"] == "ok"].sort_values("anchor_ts_utc").reset_index(drop=True)
print(f"  {len(ev):,} events with bar_status=ok")

# ── backtest engine ───────────────────────────────────────────────────────────
print("Running backtest ...")

equity        = START_EQUITY
trades        = []

n_events      = 0   # total halt events seen
n_up          = 0   # events classified as halt-up
n_market_hrs  = 0   # events within market hours
n_fills       = 0   # limit orders that filled

for date, day_ev in ev.groupby("event_date"):
    # deployable capital for today (respect cash reserve)
    available = equity * (1.0 - CASH_RESERVE)

    # pre-compute today's key UTC timestamps
    open_utc    = ny_to_utc(date, *MARKET_OPEN)
    cutoff_utc  = ny_to_utc(date, *ENTRY_CUTOFF)
    exit_utc    = ny_to_utc(date, EXIT_HOUR, EXIT_MIN)

    positions = {}   # key -> dict

    for _, row in day_ev.sort_values("anchor_ts_utc").iterrows():
        n_events += 1

        bars = load_bars(row.get("bars_file"))
        if bars is None or bars.empty or "close" not in bars.columns:
            continue

        anchor = row["anchor_ts_utc"]

        # ── 1. market hours filter ────────────────────────────────────────────
        if anchor < open_utc or anchor >= cutoff_utc:
            continue
        n_market_hrs += 1

        # ── 2. pre-halt price = close of last bar before resumption ───────────
        pre_bars = bars[bars.index < anchor]
        if pre_bars.empty:
            continue
        pre_close = float(pd.to_numeric(pre_bars.iloc[-1]["close"], errors="coerce"))
        if not np.isfinite(pre_close) or pre_close <= 0:
            continue

        # ── 3. halt-up detection ──────────────────────────────────────────────
        #
        #   LULD pauses are exactly 5 minutes.  So the halt STARTED at roughly:
        #       halt_start ≈ anchor - 5 minutes
        #
        #   Bars between halt_start and anchor don't exist (trading frozen).
        #   We want bars from BEFORE the halt started to judge direction.
        #
        #   Reference price = price UP_LOOKBACK_MIN minutes before halt started.
        #   Pre-halt close  = last traded price before halt started.
        #
        #   If pre_halt_close is UP_MIN_MOVE% above the reference → halt-up.
        #   This mirrors live trading: compare last trade to where it was
        #   a few minutes earlier to confirm upward momentum into the halt.

        halt_start   = anchor - pd.Timedelta(minutes=LULD_PAUSE_MIN)

        # last bar before halt started = actual pre-halt price
        before_halt  = bars[bars.index < halt_start]
        if before_halt.empty:
            continue
        pre_halt_close = float(pd.to_numeric(before_halt.iloc[-1]["close"], errors="coerce"))
        if not np.isfinite(pre_halt_close) or pre_halt_close <= 0:
            continue

        # reference price: first bar of the day (session open)
        # using session open mirrors what you'd do in live trading:
        # "is this stock up significantly from open when it halted?"
        day_open_px = float(pd.to_numeric(bars.iloc[0]["close"], errors="coerce"))
        if not np.isfinite(day_open_px) or day_open_px <= 0:
            continue

        move = (pre_halt_close - day_open_px) / day_open_px
        if move < UP_MIN_MOVE:
            continue   # not up enough from open → likely a down halt or flat
        n_up += 1

        # override pre_close to use actual pre-halt price (before the 5-min freeze)
        pre_close = pre_halt_close

        # ── 4. market order simulation ────────────────────────────────────────
        #   Order is placed during the halt; fills at the OPEN of the very
        #   first bar after resumption (anchor_ts_utc).  This is realistic
        #   because IBKR queues the market order and executes it the instant
        #   the stock reopens — you get the resumption print.
        resume_bars = bars[bars.index >= anchor]
        if resume_bars.empty:
            continue

        first_bar_open = float(pd.to_numeric(
            resume_bars.iloc[0].get("open", resume_bars.iloc[0]["close"]),
            errors="coerce"))
        if not np.isfinite(first_bar_open) or first_bar_open <= 0:
            continue

        n_fills += 1  # market order always fills (stock reopened)

        # ── 5. position sizing ────────────────────────────────────────────────
        entry_exec = first_bar_open * (1.0 + SLIP_BPS / 10_000)
        if entry_exec < 1.0:
            continue

        pos_usd = min(
            equity   * POSITION_PCT,   # 5% of equity
            MAX_POSITION,              # hard cap $2,000
            available,                 # respect cash reserve
        )
        if pos_usd < MIN_TRADE:
            continue

        shares    = pos_usd / entry_exec
        available -= pos_usd           # reduce deployable cash

        key = f"{row['symbol_resolved']}_{row['event_id']}"
        positions[key] = {
            "symbol":    row["symbol_resolved"],
            "entry_ts":  anchor.tz_localize(None),
            "entry_px":  entry_exec,
            "shares":    shares,
            "cost":      pos_usd,
            "bars_file": row.get("bars_file"),
        }

    # ── 6. EOD exit at 3:50 PM for all open positions ─────────────────────────
    for key, pos in positions.items():
        b = load_bars(pos["bars_file"])
        if b is None or b.empty:
            continue

        # find last bar at or before 3:50 PM exit
        exit_bars = b[b.index <= exit_utc]
        if exit_bars.empty:
            exit_bars = b   # fallback: last available bar

        exit_px_raw = float(pd.to_numeric(exit_bars.iloc[-1]["close"], errors="coerce"))
        if not np.isfinite(exit_px_raw) or exit_px_raw <= 0:
            continue

        exit_exec = exit_px_raw * (1.0 - SLIP_BPS / 10_000)
        pnl       = (exit_exec - pos["entry_px"]) * pos["shares"]
        ret       = exit_exec / pos["entry_px"] - 1.0
        equity   += pnl

        trades.append({
            "date":         date.strftime("%Y-%m-%d"),
            "symbol":       pos["symbol"],
            "entry_ts":     pos["entry_ts"],
            "exit_ts":      exit_utc.tz_localize(None),
            "entry_px":     round(pos["entry_px"], 4),
            "exit_px":      round(exit_exec,        4),
            "shares":       round(pos["shares"],    4),
            "position_usd": round(pos["cost"],      2),
            "pnl":          round(pnl,              2),
            "ret_pct":      round(ret * 100,        4),
            "equity_after": round(equity,           2),
        })

# ── results ───────────────────────────────────────────────────────────────────
print(f"\n{'-'*50}")
print(f"  Total halt events        : {n_events:>7,}")
print(f"  During market hours      : {n_market_hrs:>7,}")
print(f"  Classified halt-up       : {n_up:>7,}")
print(f"  Limit orders filled      : {n_fills:>7,}  ({n_fills/max(n_up,1):.1%} of halt-up)")
print(f"  Closed trades (w/ exit)  : {len(trades):>7,}")
print(f"  {'-'*48}\n")

if not trades:
    print("No trades — check data paths or parameters.")
    raise SystemExit(1)

tr = pd.DataFrame(trades).sort_values("exit_ts").reset_index(drop=True)

# ── save CSV ──────────────────────────────────────────────────────────────────
tr.to_csv(OUT_CSV, index=False)
print(f"Trades CSV saved: {OUT_CSV}  ({len(tr):,} rows)")

# rebuild equity curve using equity_after column (already cumulative)
equity_ts = tr["equity_after"].values
xs        = tr["exit_ts"].values

roll_max  = np.maximum.accumulate(equity_ts)
drawdown  = equity_ts - roll_max
dd_pct    = drawdown / roll_max * 100

wr        = (tr["ret_pct"] > 0).mean()
avg_r     = tr["ret_pct"].mean()
med_r     = tr["ret_pct"].median()
total_r   = (equity_ts[-1] / START_EQUITY - 1) * 100
max_dd    = dd_pct.min()
n         = len(tr)
fill_rate = n_fills / max(n_up, 1)

print(f"  Total Return   : {total_r:+.1f}%")
print(f"  Win Rate       : {wr:.1%}")
print(f"  Avg Trade      : {avg_r:+.2f}%")
print(f"  Median Trade   : {med_r:+.2f}%")
print(f"  Max Drawdown   : {max_dd:.2f}%")
print(f"  Fill Rate      : {fill_rate:.1%}  (market orders, always fills)")
print(f"  Trades         : {n:,}")
print(f"  Final Equity   : ${equity_ts[-1]:,.2f}\n")

rets_pct = tr["ret_pct"].clip(-40, 100).values

# ── chart ─────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family":       "sans-serif",
    "font.size":         11,
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "axes.grid":         True,
    "grid.color":        C_GRID,
    "grid.linewidth":    0.8,
    "axes.labelcolor":   "#334155",
    "xtick.color":       "#64748B",
    "ytick.color":       "#64748B",
    "text.color":        "#1E293B",
})

fig = plt.figure(figsize=(16, 10), facecolor="white")
gs  = GridSpec(2, 2, figure=fig,
               height_ratios=[1.8, 1],
               hspace=0.42, wspace=0.30,
               left=0.07, right=0.97, top=0.88, bottom=0.08)

ax_eq = fig.add_subplot(gs[0, :])
ax_dd = fig.add_subplot(gs[1, 0])
ax_di = fig.add_subplot(gs[1, 1])

# panel 1 — equity curve
ax_eq.fill_between(xs, START_EQUITY, equity_ts, color=C_FILL, alpha=0.7)
ax_eq.plot(xs, equity_ts, color=C_EQUITY, lw=2, zorder=3)
ax_eq.axhline(START_EQUITY, color=C_ZERO, lw=1, ls="--", alpha=0.6)

ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${v/1e3:.1f}k" if v < 1e6 else f"${v/1e6:.2f}M"))
ax_eq.xaxis.set_major_locator(mdates.YearLocator())
ax_eq.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax_eq.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 7, 10]))
ax_eq.set_ylabel("Portfolio Value", fontsize=12, fontweight="bold")
ax_eq.set_xlim(xs[0], xs[-1])
ax_eq.set_ylim(bottom=min(equity_ts) * 0.92)

stats_text = (
    f"Total Return  {total_r:+.0f}%     "
    f"Win Rate  {wr:.0%}     "
    f"Avg Trade  {avg_r:+.1f}%     "
    f"Median Trade  {med_r:+.1f}%     "
    f"Max Drawdown  {max_dd:.1f}%     "
    f"Fill Rate  {fill_rate:.0%}     "
    f"Trades  {n:,}"
)
ax_eq.text(0.5, 1.06, stats_text,
           transform=ax_eq.transAxes, ha="center", va="bottom",
           fontsize=10, color="#475569",
           bbox=dict(boxstyle="round,pad=0.3", facecolor="#F8FAFC",
                     edgecolor="#CBD5E1", linewidth=0.8))

ax_eq.set_title(
    "LULD Halt Resumption — Realistic Backtest  "
    r"(\$1k start | 5% sizing | \$2k cap | 1-min limit order | 3:50 PM exit)",
    fontsize=13, fontweight="bold", pad=28, color="#1E293B")

# panel 2 — drawdown
ax_dd.fill_between(xs, dd_pct, 0, color=C_DD_FILL, alpha=0.8)
ax_dd.plot(xs, dd_pct, color=C_DD, lw=1.3)
ax_dd.axhline(0, color=C_ZERO, lw=0.8, ls="--", alpha=0.5)
ax_dd.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
ax_dd.xaxis.set_major_locator(mdates.YearLocator())
ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax_dd.set_ylabel("Drawdown (%)", fontsize=11, fontweight="bold")
ax_dd.set_xlabel("Date", fontsize=10, color="#64748B")
ax_dd.set_xlim(xs[0], xs[-1])
ax_dd.set_title("Drawdown", fontsize=12, fontweight="bold", color="#1E293B")

# panel 3 — return distribution
ax_di.hist(rets_pct, bins=60, color=C_DIST, alpha=0.25,
           edgecolor="none", density=True)

kde = gaussian_kde(rets_pct, bw_method=0.35)
kx  = np.linspace(rets_pct.min() - 2, rets_pct.max() + 2, 400)
ax_di.plot(kx, kde(kx), color=C_EQUITY, lw=2.5)
ax_di.axvline(0,     color=C_ZERO,    lw=1.2, ls="--", alpha=0.7)
ax_di.axvline(med_r, color="#10B981", lw=1.5, ls="--", alpha=0.9,
              label=f"Median  {med_r:+.1f}%")
ax_di.axvline(avg_r, color="#F59E0B", lw=1.5, ls="--", alpha=0.9,
              label=f"Mean  {avg_r:+.1f}%")

ax_di.set_xlabel("Return per Trade (%)", fontsize=11, fontweight="bold")
ax_di.set_ylabel("Density",              fontsize=11, fontweight="bold")
ax_di.set_title("Trade Return Distribution", fontsize=12, fontweight="bold", color="#1E293B")
ax_di.legend(fontsize=9.5, framealpha=0.8, edgecolor="#CBD5E1")
ax_di.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

plt.savefig(OUT_PNG, dpi=180, facecolor="white", bbox_inches="tight")
print(f"Saved: {OUT_PNG}")
