"""
Position sizing comparison with $100k hard cap per trade.

For each sizing level:
  - position size = min(equity * fraction, $100,000)
  - tracks when strategy "matures" to full $100k/trade
  - exports a full trade log CSV so you can verify no overlapping trades

Run from the Trade Halts root:
    python TESTING/sizing_comparison.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
TRADES_CSV   = Path("TESTING/halt_trades_rvol.csv")
START_EQUITY = 30_000.0
MAX_POS_USD  = 100_000.0     # hard cap per trade

SIZING_LEVELS = [0.05, 0.10, 0.25, 0.50]
COLORS        = ["#2563EB", "#16A34A", "#D97706", "#DC2626"]
LABELS        = ["5%", "10%", "25%", "50%"]

OUT_PNG      = Path("TESTING/sizing_comparison.png")
OUT_TRADE_CSV = Path("TESTING/sizing_trade_log.csv")

# ── Load trades ───────────────────────────────────────────────────────────────
df = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
df = df.sort_values("exit_ts").reset_index(drop=True)

rets   = (df["ret_pct"] / 100).values
dates  = df["exit_ts"].values
n      = len(rets)

wr     = (rets > 0).mean()
med_r  = np.median(rets) * 100
mean_r = np.mean(rets) * 100

print(f"Loaded {n} filtered trades")
print(f"  Win rate:      {wr:.1%}")
print(f"  Median return: {med_r:+.2f}%")
print(f"  Mean return:   {mean_r:+.2f}%")
print(f"  Date range:    {df['exit_ts'].min().date()} to {df['exit_ts'].max().date()}")

# ── Overlap check ─────────────────────────────────────────────────────────────
print("\nOverlap check (trades open simultaneously):")
overlaps = 0
for i in range(len(df)):
    entry_i = df.iloc[i]["entry_ts"]
    exit_i  = df.iloc[i]["exit_ts"]
    for j in range(i + 1, len(df)):
        entry_j = df.iloc[j]["entry_ts"]
        if entry_j >= exit_i:
            break   # sorted by exit_ts — no further overlaps possible
        overlaps += 1
        if overlaps <= 5:
            print(f"  OVERLAP: {df.iloc[i]['symbol']} ({entry_i} - {exit_i}) "
                  f"vs {df.iloc[j]['symbol']} ({entry_j} - {df.iloc[j]['exit_ts']})")
if overlaps == 0:
    print("  None — all trades are sequential, no concurrent positions.")
else:
    print(f"  Total overlapping pairs found: {overlaps}")

# ── Simulate with $100k hard cap ──────────────────────────────────────────────
def simulate(fraction: float, label: str) -> tuple[np.ndarray, np.ndarray, dict, pd.DataFrame]:
    equity       = START_EQUITY
    curve        = np.empty(n)
    cap_hit_idx  = None
    cap_hit_date = None
    trade_rows   = []

    for i, r in enumerate(rets):
        pos_size    = min(equity * fraction, MAX_POS_USD)
        pnl         = pos_size * r
        equity_before = equity
        equity     += pnl
        equity      = max(equity, 0.01)
        curve[i]    = equity

        if cap_hit_idx is None and pos_size >= MAX_POS_USD * 0.999:
            cap_hit_idx  = i
            cap_hit_date = df.iloc[i]["exit_ts"]

        trade_rows.append({
            "sizing":        label,
            "trade_num":     i + 1,
            "date":          df.iloc[i]["date"],
            "symbol":        df.iloc[i]["symbol"],
            "entry_ts":      df.iloc[i]["entry_ts"],
            "exit_ts":       df.iloc[i]["exit_ts"],
            "ret_pct":       round(df.iloc[i]["ret_pct"], 4),
            "rvol":          df.iloc[i]["rvol"],
            "equity_before": round(equity_before, 2),
            "pos_size_usd":  round(pos_size, 2),
            "pct_of_equity": round(pos_size / equity_before * 100, 2),
            "pnl":           round(pnl, 2),
            "equity_after":  round(equity, 2),
            "at_cap":        pos_size >= MAX_POS_USD * 0.999,
        })

    trade_df = pd.DataFrame(trade_rows)

    roll_max   = np.maximum.accumulate(curve)
    dd_pct     = (curve / roll_max - 1.0) * 100
    years      = (df["exit_ts"].iloc[-1] - df["exit_ts"].iloc[0]).days / 365.25
    total_ret  = (curve[-1] / START_EQUITY - 1.0) * 100
    cagr       = ((curve[-1] / START_EQUITY) ** (1 / max(years, 0.01)) - 1) * 100
    max_dd_pct = dd_pct.min()
    calmar     = abs(cagr / max_dd_pct) if max_dd_pct != 0 else 0
    worst_pct  = rets.min() * fraction * 100

    # Equity when cap first hit
    eq_at_cap = curve[cap_hit_idx] if cap_hit_idx is not None else None
    # PnL generated after cap was hit
    pnl_after_cap = (
        curve[-1] - curve[cap_hit_idx]
        if cap_hit_idx is not None else 0
    )
    trades_at_cap = n - cap_hit_idx if cap_hit_idx is not None else 0

    stats = dict(
        final=curve[-1], total_ret=total_ret, cagr=cagr,
        max_dd_pct=max_dd_pct, calmar=calmar,
        worst_trade_pct=worst_pct,
        cap_hit_idx=cap_hit_idx,
        cap_hit_date=cap_hit_date,
        eq_at_cap=eq_at_cap,
        pnl_after_cap=pnl_after_cap,
        trades_after_cap=trades_at_cap,
    )
    return curve, dd_pct, stats, trade_df


results    = {}
all_trades = []
print()
for frac, label in zip(SIZING_LEVELS, LABELS):
    curve, dd, stats, tdf = simulate(frac, label)
    results[label] = {"curve": curve, "dd": dd, "stats": stats}
    all_trades.append(tdf)

    cap_str = (
        f"trade #{stats['cap_hit_idx']+1} on {stats['cap_hit_date'].date()}"
        if stats["cap_hit_idx"] is not None else "NEVER reached"
    )
    eq_str  = f"${stats['eq_at_cap']:,.0f}" if stats["eq_at_cap"] else "—"
    print(f"  {label} sizing  (cap = ${MAX_POS_USD:,.0f}/trade):")
    print(f"    Hits $100k/trade:   {cap_str}")
    print(f"    Equity at cap:      {eq_str}")
    print(f"    Trades after cap:   {stats['trades_after_cap']}")
    print(f"    PnL after cap:      ${stats['pnl_after_cap']:,.0f}")
    print(f"    Final equity:       ${stats['final']:,.0f}")
    print(f"    Max drawdown:       {stats['max_dd_pct']:+.1f}%")
    print(f"    Worst single trade: {stats['worst_trade_pct']:+.1f}% of equity")
    print()

# ── Save trade log CSV ────────────────────────────────────────────────────────
trade_log = pd.concat(all_trades, ignore_index=True)
trade_log.to_csv(OUT_TRADE_CSV, index=False)
print(f"Trade log saved: {OUT_TRADE_CSV}  ({len(trade_log)} rows, {n} trades x {len(SIZING_LEVELS)} strategies)")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(16, 11))
fig.patch.set_facecolor("#0F172A")
gs  = fig.add_gridspec(2, 2, height_ratios=[2, 1], hspace=0.38, wspace=0.28)

ax1 = fig.add_subplot(gs[0, :])
ax2 = fig.add_subplot(gs[1, 0])
ax3 = fig.add_subplot(gs[1, 1])

for ax in [ax1, ax2, ax3]:
    ax.set_facecolor("#1E293B")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.tick_params(colors="#94A3B8", labelsize=9)
    ax.yaxis.label.set_color("#94A3B8")
    ax.xaxis.label.set_color("#94A3B8")
    ax.grid(True, alpha=0.15, color="#475569")

# — Equity curves (log scale) —
for label, color in zip(LABELS, COLORS):
    d  = results[label]
    lw = 2.2 if label == "50%" else 1.8
    ax1.semilogy(dates, d["curve"], color=color, lw=lw, label=label, alpha=0.92)

    # Mark when cap is first hit
    idx = results[label]["stats"]["cap_hit_idx"]
    if idx is not None:
        ax1.axvline(dates[idx], color=color, lw=1, linestyle="--", alpha=0.5)
        ax1.annotate(
            f"{label} hits cap",
            xy=(dates[idx], d["curve"][idx]),
            xytext=(8, 0), textcoords="offset points",
            color=color, fontsize=7.5, va="center",
        )

ax1.set_title(
    f"Equity Curve — $100k/trade cap  ($30k start, {n} trades, log scale)",
    color="white", fontsize=13, fontweight="bold", pad=10,
)
ax1.set_ylabel("Portfolio Value (log scale)", color="#94A3B8")
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
ax1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
ax1.legend(loc="upper left", fontsize=11, framealpha=0.3,
           facecolor="#1E293B", labelcolor="white")

# — Drawdown —
for label, color in zip(LABELS, COLORS):
    d  = results[label]
    lw = 2.2 if label == "50%" else 1.8
    ax2.plot(dates, d["dd"], color=color, lw=lw, label=label, alpha=0.92)
ax2.fill_between(dates, results["50%"]["dd"], 0, color="#DC2626", alpha=0.10)
ax2.axhline(0, color="#475569", lw=0.8)
ax2.set_title("Drawdown (% from peak)", color="white", fontsize=11, fontweight="bold")
ax2.set_ylabel("Drawdown (%)", color="#94A3B8")
ax2.set_xlabel("Date", color="#94A3B8")
ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

# — Stats table —
ax3.axis("off")
ax3.set_title("Summary  ($100k/trade cap)", color="white", fontsize=11, fontweight="bold", pad=8)

col_labels = ["Size", "Hits Cap", "Eq @ Cap", "Max DD", "Final Eq"]
rows = []
for label, color in zip(LABELS, COLORS):
    s = results[label]["stats"]
    cap_date = s["cap_hit_date"].strftime("%b %Y") if s["cap_hit_date"] else "Never"
    eq_cap   = f"${s['eq_at_cap']:,.0f}" if s["eq_at_cap"] else "—"
    rows.append([
        label,
        cap_date,
        eq_cap,
        f"{s['max_dd_pct']:.1f}%",
        f"${s['final']:,.0f}",
    ])

tbl = ax3.table(cellText=rows, colLabels=col_labels, cellLoc="center", loc="center")
tbl.auto_set_font_size(False)
tbl.set_fontsize(9.5)
tbl.scale(1.15, 2.1)

for (row, col), cell in tbl.get_celld().items():
    cell.set_facecolor("#0F172A" if row == 0 else "#1E293B")
    cell.set_text_props(color="white" if row == 0 else "#E2E8F0")
    cell.set_edgecolor("#334155")
    if row > 0:
        if col == 0:
            cell.set_text_props(color=COLORS[row - 1], fontweight="bold")
        if col == 3:
            cell.set_text_props(color="#F87171")
        if col == 4:
            cell.set_text_props(color="#4ADE80")

fig.suptitle(
    f"LULD Halt Strategy — Sizing Comparison  "
    f"(RVOL-filtered | WR={wr:.0%} | Median={med_r:+.1f}% | $100k cap/trade | {n} trades)",
    color="white", fontsize=13, fontweight="bold", y=0.99,
)

plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Saved chart: {OUT_PNG}")
