"""
Realistic backtest — $900 starting equity, 10% sizing, $100k cap.

Rules:
  - Position size = min(BOD_equity * 0.10, $100,000, available_capital)
  - Available capital resets each day from BOD equity
  - Concurrent positions allowed — each uses a slice of the day's capital
  - Never deploy more than BOD equity total on any given day
  - IBKR commissions: max($1, min(shares * $0.005, trade_value * 0.01)) each way
  - Slippage already baked into entry_px / exit_px in the CSV (50bps in, 25bps out)
  - Minimum position: $5 (below this, skip — can't trade meaningfully)

Run from Trade Halts root:
    python TESTING/realistic_backtest.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from pathlib import Path

TRADES_CSV   = Path("TESTING/halt_trades_rvol.csv")
OUT_PNG      = Path("TESTING/realistic_backtest.png")
OUT_CSV      = Path("TESTING/realistic_backtest_trades.csv")

START_EQUITY  = 900.0
FRACTION      = 0.10
MAX_POS_USD   = 100_000.0
MIN_POS_USD   = 5.0
COMM_PER_SHARE = 0.005
COMM_MIN       = 1.00
COMM_MAX_PCT   = 0.01

# Milestones to annotate on the chart
MILESTONES = [1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]

# ── Load & sort by entry time ─────────────────────────────────────────────────
df = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
df["date"] = pd.to_datetime(df["date"]).dt.date
df = df.sort_values(["date", "entry_ts"]).reset_index(drop=True)

print(f"Loaded {len(df)} trades  ({df['date'].min()} to {df['date'].max()})")
print(f"Starting equity: ${START_EQUITY:,.2f}")
print(f"Sizing: {FRACTION:.0%} of BOD equity, max ${MAX_POS_USD:,.0f}/trade\n")

# ── Commission helper ─────────────────────────────────────────────────────────
def calc_comm(shares: float, price: float) -> float:
    raw = shares * COMM_PER_SHARE
    cap = shares * price * COMM_MAX_PCT
    return max(COMM_MIN, min(raw, cap))

# ── Simulate day by day ───────────────────────────────────────────────────────
equity        = START_EQUITY
trade_log     = []
skipped_log   = []
equity_curve  = []   # (date, equity) at EOD each trading day
milestone_hit = {}   # milestone $ → (trade_num, date, equity)
trade_num     = 0

days = df.groupby("date")

for date, day_trades in days:
    BOD_equity       = equity
    available        = BOD_equity       # capital available to deploy today
    position_size_tgt = min(BOD_equity * FRACTION, MAX_POS_USD)

    day_pnl   = 0.0
    day_comm  = 0.0
    positions_taken = 0
    positions_skipped = 0

    for _, t in day_trades.iterrows():
        # --- Sanity check: enough capital left today? ---
        if available < MIN_POS_USD:
            skipped_log.append({
                "date":   str(date),
                "symbol": t["symbol"],
                "reason": f"no capital left (available=${available:.2f})",
            })
            positions_skipped += 1
            continue

        # Actual position size: target, but never more than available
        pos_size = min(position_size_tgt, available)

        # Commission (both legs)
        shares_bought = pos_size / t["entry_px"]
        shares_sold   = pos_size / t["entry_px"]   # same share count, different px
        comm_in  = calc_comm(shares_bought, t["entry_px"])
        comm_out = calc_comm(shares_sold,   t["exit_px"])
        total_comm = comm_in + comm_out

        # P&L
        gross_pnl = pos_size * (t["ret_pct"] / 100)
        net_pnl   = gross_pnl - total_comm

        # Deploy capital (locked until EOD)
        available -= pos_size
        day_pnl   += net_pnl
        day_comm  += total_comm
        positions_taken += 1
        trade_num += 1

        trade_log.append({
            "trade_num":     trade_num,
            "date":          str(date),
            "symbol":        t["symbol"],
            "entry_ts":      t["entry_ts"],
            "exit_ts":       t["exit_ts"],
            "entry_px":      round(t["entry_px"], 4),
            "exit_px":       round(t["exit_px"],  4),
            "ret_pct":       round(t["ret_pct"],  4),
            "rvol":          t["rvol"],
            "BOD_equity":    round(BOD_equity, 2),
            "pos_size":      round(pos_size, 2),
            "gross_pnl":     round(gross_pnl, 2),
            "commission":    round(total_comm, 2),
            "net_pnl":       round(net_pnl, 2),
            "equity_after":  round(equity + day_pnl, 2),  # running (approx)
            "positions_today": positions_taken,
        })

        # Check milestones
        running_eq = equity + day_pnl
        for ms in MILESTONES:
            if ms not in milestone_hit and running_eq >= ms:
                milestone_hit[ms] = {
                    "trade_num": trade_num,
                    "date":      str(date),
                    "equity":    running_eq,
                }

    # EOD: apply all PnL
    equity += day_pnl
    equity_curve.append({
        "date":              pd.Timestamp(str(date)),
        "equity":            round(equity, 2),
        "BOD_equity":        round(BOD_equity, 2),
        "day_pnl":           round(day_pnl, 2),
        "day_comm":          round(day_comm, 2),
        "positions_taken":   positions_taken,
        "positions_skipped": positions_skipped,
        "capital_deployed":  round(BOD_equity - available - day_pnl, 2),
    })

# ── Results ───────────────────────────────────────────────────────────────────
tlog = pd.DataFrame(trade_log)
ecurve = pd.DataFrame(equity_curve)
tlog.to_csv(OUT_CSV, index=False)

rets      = tlog["ret_pct"].values / 100
net_pnls  = tlog["net_pnl"].values
wr        = (net_pnls > 0).mean()
total_comm = tlog["commission"].sum()

eq_vals   = ecurve["equity"].values
roll_max  = np.maximum.accumulate(eq_vals)
dd_pct    = (eq_vals / roll_max - 1.0) * 100
max_dd    = dd_pct.min()
years     = (ecurve["date"].iloc[-1] - ecurve["date"].iloc[0]).days / 365.25
cagr      = ((equity / START_EQUITY) ** (1 / years) - 1) * 100

print("=" * 55)
print(f"  REALISTIC BACKTEST RESULTS")
print("=" * 55)
print(f"  Trades taken:      {len(tlog):>10,}")
print(f"  Trades skipped:    {len(skipped_log):>10,}  (capital exhausted)")
print(f"  Win rate (net):    {wr:>10.1%}")
print(f"  Total commissions: ${total_comm:>10,.2f}")
print(f"  Starting equity:   ${START_EQUITY:>10,.2f}")
print(f"  Final equity:      ${equity:>10,.2f}")
print(f"  Total return:      {(equity/START_EQUITY-1)*100:>+10.1f}%")
print(f"  CAGR:              {cagr:>+10.1f}%")
print(f"  Max drawdown:      {max_dd:>+10.1f}%")
print(f"  Total PnL (net):   ${equity - START_EQUITY:>10,.2f}")
print()
print("  MILESTONES:")
for ms in MILESTONES:
    if ms in milestone_hit:
        m = milestone_hit[ms]
        print(f"    ${ms:>10,.0f}  ->  trade #{m['trade_num']}  on {m['date']}")
    else:
        print(f"    ${ms:>10,.0f}  →  not reached")
print()
print(f"  Max positions in one day: {ecurve['positions_taken'].max()}")
print(f"  Avg positions per day:    {ecurve['positions_taken'].mean():.1f}")
print(f"  Days with 0 trades:       {(ecurve['positions_taken'] == 0).sum()}")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(16, 12))
fig.patch.set_facecolor("#0F172A")
gs  = fig.add_gridspec(3, 2, height_ratios=[3, 1, 1], hspace=0.42, wspace=0.28)

ax1 = fig.add_subplot(gs[0, :])     # equity curve — full width
ax2 = fig.add_subplot(gs[1, :])     # drawdown — full width
ax3 = fig.add_subplot(gs[2, 0])     # return distribution
ax4 = fig.add_subplot(gs[2, 1])     # positions per day histogram

for ax in [ax1, ax2, ax3, ax4]:
    ax.set_facecolor("#1E293B")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.tick_params(colors="#94A3B8", labelsize=9)
    ax.yaxis.label.set_color("#94A3B8")
    ax.xaxis.label.set_color("#94A3B8")
    ax.grid(True, alpha=0.15, color="#475569")

dates_all = ecurve["date"].values
eq_all    = ecurve["equity"].values

# — Equity curve (log scale) —
ax1.semilogy(dates_all, eq_all, color="#2563EB", lw=2, zorder=3)
ax1.fill_between(dates_all, START_EQUITY, eq_all,
                 where=(eq_all >= START_EQUITY),
                 color="#2563EB", alpha=0.12, zorder=2)

# Milestone annotations
ms_colors = ["#94A3B8","#94A3B8","#F59E0B","#F59E0B","#16A34A",
             "#16A34A","#DC2626","#DC2626","#A855F7"]
for ms, color in zip(MILESTONES, ms_colors):
    if ms in milestone_hit:
        m    = milestone_hit[ms]
        mdate = pd.Timestamp(m["date"])
        ax1.axhline(ms, color=color, lw=0.8, linestyle=":", alpha=0.6)
        ax1.annotate(
            f"${ms:,.0f}",
            xy=(mdate, ms),
            xytext=(6, 0), textcoords="offset points",
            color=color, fontsize=7.5, va="center", fontweight="bold",
        )
        ax1.plot(mdate, ms, "o", color=color, ms=5, zorder=5)

ax1.set_title(
    f"Realistic Backtest -- \\$900 Start | 10% Sizing | \\$100k Cap | "
    f"IBKR Commissions | {len(tlog):,} Trades",
    color="white", fontsize=12, fontweight="bold", pad=8,
)
ax1.set_ylabel("Portfolio Value (log scale)", color="#94A3B8")
ax1.yaxis.set_major_formatter(mticker.FuncFormatter(
    lambda x, _: f"\\${x:,.0f}" if x < 1e6 else f"\\${x/1e6:.1f}M"))
ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
ax1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

# Stat box
stat_txt = (f"Final: \\${equity:,.0f}\nCAGR: {cagr:+.0f}%\n"
            f"WR: {wr:.1%}  MaxDD: {max_dd:.1f}%\n"
            f"Commissions: \\${total_comm:,.0f}")
ax1.text(0.01, 0.97, stat_txt, transform=ax1.transAxes,
         color="white", fontsize=9, va="top",
         bbox=dict(boxstyle="round", fc="#0F172A", alpha=0.8))

# — Drawdown —
ax2.fill_between(dates_all, dd_pct, 0, color="#DC2626", alpha=0.4)
ax2.plot(dates_all, dd_pct, color="#DC2626", lw=1.2)
ax2.axhline(0, color="#475569", lw=0.8)
ax2.set_ylabel("Drawdown %", color="#94A3B8")
ax2.set_xlabel("Date", color="#94A3B8")
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
ax2.set_title(f"Drawdown  (Max: {max_dd:.1f}%)", color="white", fontsize=10, fontweight="bold")

# — Return distribution —
rets_pct = tlog["ret_pct"].clip(-60, 200)
ax3.hist(rets_pct, bins=70, color="#2563EB", alpha=0.8, edgecolor="none")
ax3.axvline(0,                       color="#475569", lw=1)
ax3.axvline(tlog["ret_pct"].median(),color="#F59E0B", lw=2, linestyle="--",
            label=f"Median {tlog['ret_pct'].median():+.1f}%")
ax3.axvline(tlog["ret_pct"].mean(),  color="#16A34A", lw=2, linestyle="--",
            label=f"Mean {tlog['ret_pct'].mean():+.1f}%")
ax3.set_title("Trade Return Distribution (clipped +200%)",
              color="white", fontsize=10, fontweight="bold")
ax3.set_xlabel("Return %", color="#94A3B8")
ax3.set_ylabel("Count", color="#94A3B8")
ax3.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# — Positions per day —
pos_counts = ecurve["positions_taken"]
max_pos = int(pos_counts.max())
bins_pos = np.arange(-0.5, max_pos + 1.5, 1)
ax4.hist(pos_counts, bins=bins_pos, color="#16A34A", alpha=0.8, edgecolor="#334155")
ax4.set_title(f"Positions Taken Per Day  (max={max_pos})",
              color="white", fontsize=10, fontweight="bold")
ax4.set_xlabel("Positions", color="#94A3B8")
ax4.set_ylabel("Days", color="#94A3B8")
ax4.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))

fig.suptitle(
    "LULD Halt Resumption -- Realistic Simulation  "
    "(\\$900 start | 10% sizing | \\$100k cap | Commissions included)",
    color="white", fontsize=13, fontweight="bold", y=1.01,
)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Chart saved: {OUT_PNG}")
print(f"Trade log:   {OUT_CSV}  ({len(tlog)} rows)")
