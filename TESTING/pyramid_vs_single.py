"""
Pyramid vs Single-entry comparison.

Pyramid  — take every halt signal on every ticker, even if already holding
           (current strategy: same stock can be bought 8-13x in one day)

Single   — only take the FIRST halt per ticker per day, skip repeats
           (conservative: one position per stock per day max)

Both simulated at 10% sizing with $100k cap.
Shows: trade count, equity curve, drawdown, return distribution, key stats.

Run from Trade Halts root:
    python TESTING/pyramid_vs_single.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
TRADES_CSV   = Path("TESTING/halt_trades_rvol.csv")
START_EQUITY = 30_000.0
MAX_POS_USD  = 100_000.0
FRACTION     = 0.10          # 10% sizing for both

OUT_PNG      = Path("TESTING/pyramid_vs_single.png")
OUT_CSV      = Path("TESTING/pyramid_vs_single_trades.csv")

# ── Load & sort by entry time ─────────────────────────────────────────────────
df = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
df["date"] = pd.to_datetime(df["date"]).dt.date
df = df.sort_values("entry_ts").reset_index(drop=True)

# ── Build the two trade sets ──────────────────────────────────────────────────

# Pyramid: all 550 trades as-is
pyramid_df = df.copy()
pyramid_df["strategy"] = "pyramid"

# Single: first halt per ticker per day only
seen = set()
single_rows = []
for _, row in df.iterrows():
    key = (row["symbol"], row["date"])
    if key not in seen:
        seen.add(key)
        single_rows.append(row)
single_df = pd.DataFrame(single_rows).reset_index(drop=True)
single_df["strategy"] = "single"

print(f"Pyramid trades:  {len(pyramid_df):,}")
print(f"Single  trades:  {len(single_df):,}  (removed {len(pyramid_df)-len(single_df):,} repeat-ticker halts)")
print()

# Days breakdown
pyr_by_day  = pyramid_df.groupby("date")["symbol"].count()
sing_by_day = single_df.groupby("date")["symbol"].count()
print("Trades-per-day distribution (pyramid vs single):")
print(f"{'Trades/day':<12} {'Pyramid days':>14} {'Single days':>13}")
all_counts = sorted(set(pyr_by_day.values) | set(sing_by_day.values))
for c in all_counts:
    p = (pyr_by_day == c).sum()
    s = (sing_by_day == c).sum()
    if p > 0 or s > 0:
        print(f"  {c:<10} {p:>14}  {s:>12}")
print()

# ── Simulate equity curve ─────────────────────────────────────────────────────
def simulate(trades: pd.DataFrame, label: str) -> tuple[pd.DataFrame, dict]:
    trades = trades.sort_values("exit_ts").reset_index(drop=True)
    rets   = trades["ret_pct"].values / 100
    dates  = trades["exit_ts"].values
    n      = len(rets)

    equity     = START_EQUITY
    curve      = np.empty(n)
    cap_hit    = None

    rows = []
    for i, (_, row) in enumerate(trades.iterrows()):
        r        = row["ret_pct"] / 100
        pos      = min(equity * FRACTION, MAX_POS_USD)
        pnl      = pos * r
        eq_before = equity
        equity   += pnl
        equity    = max(equity, 0.01)
        curve[i]  = equity

        if cap_hit is None and pos >= MAX_POS_USD * 0.999:
            cap_hit = (i, row["exit_ts"])

        rows.append({
            "strategy":      label,
            "trade_num":     i + 1,
            "date":          row["date"],
            "symbol":        row["symbol"],
            "entry_ts":      row["entry_ts"],
            "exit_ts":       row["exit_ts"],
            "ret_pct":       round(row["ret_pct"], 4),
            "rvol":          row["rvol"],
            "equity_before": round(eq_before, 2),
            "pos_size":      round(pos, 2),
            "pnl":           round(pnl, 2),
            "equity_after":  round(equity, 2),
        })

    roll_max   = np.maximum.accumulate(curve)
    dd_pct     = (curve / roll_max - 1.0) * 100
    years      = (trades["exit_ts"].iloc[-1] - trades["exit_ts"].iloc[0]).days / 365.25
    total_ret  = (curve[-1] / START_EQUITY - 1.0) * 100
    cagr       = ((curve[-1] / START_EQUITY) ** (1 / max(years, 0.01)) - 1) * 100
    max_dd     = dd_pct.min()
    wr         = (rets > 0).mean()
    med_r      = np.median(rets) * 100
    mean_r     = np.mean(rets) * 100
    worst_r    = rets.min() * FRACTION * 100

    # PnL with returns capped at +100% (realistic execution)
    rets_capped = np.minimum(rets, 1.0)
    equity_c    = START_EQUITY
    for r in rets_capped:
        pos      = min(equity_c * FRACTION, MAX_POS_USD)
        equity_c += pos * r
        equity_c  = max(equity_c, 0.01)

    stats = dict(
        n=n, final=curve[-1], final_capped=equity_c,
        total_ret=total_ret, cagr=cagr,
        max_dd=max_dd, wr=wr,
        med_r=med_r, mean_r=mean_r,
        worst_r=worst_r,
        cap_hit=cap_hit,
        dates=dates, curve=curve, dd=dd_pct, rets=rets,
    )
    return pd.DataFrame(rows), stats

pyr_trades_out, pyr_stats  = simulate(pyramid_df, "pyramid")
sing_trades_out, sing_stats = simulate(single_df,  "single")

# Save combined trade log
pd.concat([pyr_trades_out, sing_trades_out]).to_csv(OUT_CSV, index=False)
print(f"Trade log saved: {OUT_CSV}")

# ── Print summary ─────────────────────────────────────────────────────────────
for label, s in [("PYRAMID (all halts)", pyr_stats), ("SINGLE (first halt/ticker/day)", sing_stats)]:
    cap_str = (f"trade #{s['cap_hit'][0]+1}, {pd.Timestamp(s['cap_hit'][1]).date()}"
               if s["cap_hit"] else "never")
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")
    print(f"  Trades:              {s['n']:,}")
    print(f"  Win rate:            {s['wr']:.1%}")
    print(f"  Median return:       {s['med_r']:+.2f}%")
    print(f"  Mean return:         {s['mean_r']:+.2f}%")
    print(f"  Hits $100k cap:      {cap_str}")
    print(f"  Final equity:        ${s['final']:,.0f}")
    print(f"  Final (100% cap):    ${s['final_capped']:,.0f}")
    print(f"  Max drawdown:        {s['max_dd']:+.1f}%")
    print(f"  Worst single trade:  {s['worst_r']:+.1f}% of equity")

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

PYR_COL  = "#DC2626"   # red — pyramid
SING_COL = "#2563EB"   # blue — single

# — Equity curves (log scale) —
ax1.semilogy(pyr_stats["dates"],  pyr_stats["curve"],  color=PYR_COL,  lw=2.2,
             label=f"Pyramid  ({pyr_stats['n']} trades)", alpha=0.9)
ax1.semilogy(sing_stats["dates"], sing_stats["curve"], color=SING_COL, lw=2.2,
             label=f"Single   ({sing_stats['n']} trades)", alpha=0.9)

# Mark cap hits
for stats, color in [(pyr_stats, PYR_COL), (sing_stats, SING_COL)]:
    if stats["cap_hit"]:
        idx, dt = stats["cap_hit"]
        ax1.axvline(stats["dates"][idx], color=color, lw=1.2, linestyle="--", alpha=0.6)

ax1.set_title(
    f"Pyramid vs Single-Entry  (10% sizing, $100k cap, $30k start, log scale)",
    color="white", fontsize=13, fontweight="bold", pad=10,
)
ax1.set_ylabel("Portfolio Value (log scale)", color="#94A3B8")
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
ax1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
ax1.legend(loc="upper left", fontsize=11, framealpha=0.3,
           facecolor="#1E293B", labelcolor="white")

# — Drawdown —
ax2.plot(pyr_stats["dates"],  pyr_stats["dd"],  color=PYR_COL,  lw=2, label="Pyramid", alpha=0.9)
ax2.plot(sing_stats["dates"], sing_stats["dd"], color=SING_COL, lw=2, label="Single",  alpha=0.9)
ax2.fill_between(pyr_stats["dates"],  pyr_stats["dd"],  0, color=PYR_COL,  alpha=0.08)
ax2.fill_between(sing_stats["dates"], sing_stats["dd"], 0, color=SING_COL, alpha=0.08)
ax2.axhline(0, color="#475569", lw=0.8)
ax2.set_title("Drawdown (% from peak)", color="white", fontsize=11, fontweight="bold")
ax2.set_ylabel("Drawdown (%)", color="#94A3B8")
ax2.set_xlabel("Date", color="#94A3B8")
ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
ax2.legend(fontsize=10, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# — Return distribution —
bins = np.linspace(-60, 200, 80)
ax3.hist((pyr_stats["rets"]  * 100).clip(-60, 200), bins=bins,
         color=PYR_COL,  alpha=0.55, label=f"Pyramid (n={pyr_stats['n']})",  edgecolor="none")
ax3.hist((sing_stats["rets"] * 100).clip(-60, 200), bins=bins,
         color=SING_COL, alpha=0.55, label=f"Single  (n={sing_stats['n']})", edgecolor="none")
ax3.axvline(0,                      color="white",   lw=1,   linestyle="--", alpha=0.4)
ax3.axvline(pyr_stats["med_r"],     color=PYR_COL,  lw=1.5, linestyle=":",
            label=f"Pyramid median {pyr_stats['med_r']:+.0f}%")
ax3.axvline(sing_stats["med_r"],    color=SING_COL, lw=1.5, linestyle=":",
            label=f"Single  median {sing_stats['med_r']:+.0f}%")
ax3.set_title("Return Distribution (clipped at -60%/+200%)",
              color="white", fontsize=11, fontweight="bold")
ax3.set_xlabel("Return per Trade (%)", color="#94A3B8")
ax3.set_ylabel("Count", color="#94A3B8")
ax3.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# Stat annotations
pyr_txt  = (f"Pyramid\n{pyr_stats['n']} trades  WR={pyr_stats['wr']:.0%}\n"
            f"Median {pyr_stats['med_r']:+.0f}%  Mean {pyr_stats['mean_r']:+.0f}%\n"
            f"Max DD {pyr_stats['max_dd']:.1f}%")
sing_txt = (f"Single\n{sing_stats['n']} trades  WR={sing_stats['wr']:.0%}\n"
            f"Median {sing_stats['med_r']:+.0f}%  Mean {sing_stats['mean_r']:+.0f}%\n"
            f"Max DD {sing_stats['max_dd']:.1f}%")

ax1.text(0.72, 0.08, pyr_txt,  transform=ax1.transAxes, color=PYR_COL,
         fontsize=9, va="bottom", bbox=dict(boxstyle="round", fc="#1E293B", alpha=0.7))
ax1.text(0.85, 0.08, sing_txt, transform=ax1.transAxes, color=SING_COL,
         fontsize=9, va="bottom", bbox=dict(boxstyle="round", fc="#1E293B", alpha=0.7))

fig.suptitle(
    "LULD Halt Strategy — Pyramid vs Single Entry  (10% sizing | $100k cap | $30k start)",
    color="white", fontsize=13, fontweight="bold", y=0.99,
)

plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"\nSaved: {OUT_PNG}")
