"""
Slippage Monte Carlo Analysis
==============================
Loads halt_trades_realistic.csv, reverse-engineers the raw fill prices,
then re-runs the P&L at many slippage levels.

For each level two things are shown:
  1. Deterministic line  — every trade pays exactly N bps
  2. Monte Carlo band    — slippage is drawn per-trade from a half-normal
                           distribution centred on N bps (sigma = N * 0.8)
                           so some trades fill tight, some fill wide.

Output
------
  TESTING/slippage_overview.png   — all curves overlaid + summary stats
  TESTING/slippage_mc_grid.png    — one panel per slippage level with MC fan

Run from Trade Halts root:
    python TESTING/slippage_montecarlo.py
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.dates as mdates
import matplotlib.cm as cm
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
CSV_PATH     = Path("TESTING/halt_trades_realistic.csv")
OUT_OVERVIEW = Path("TESTING/slippage_overview.png")
OUT_GRID     = Path("TESTING/slippage_mc_grid.png")

START_EQUITY = 1_000.0
BASE_SLIP    = 10        # bps used when the CSV was generated

# Slippage levels to stress-test (bps)
SLIP_LEVELS  = [0, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 750]

N_SIMS       = 300       # Monte Carlo paths per level
SIGMA_FACTOR = 0.8       # per-trade sigma = mean_slip * SIGMA_FACTOR

# ── load trades ───────────────────────────────────────────────────────────────
print("Loading trades ...")
tr = pd.read_csv(CSV_PATH, parse_dates=["exit_ts", "entry_ts"])
tr = tr.sort_values("exit_ts").reset_index(drop=True)
n  = len(tr)
print(f"  {n:,} trades loaded")

xs = tr["exit_ts"].values   # shared x-axis for all curves

# ── reverse-engineer raw (pre-slippage) fill prices ──────────────────────────
# entry_px = raw_open  * (1 + BASE_SLIP/10000)
# exit_px  = raw_close * (1 - BASE_SLIP/10000)
raw_entry = tr["entry_px"].values / (1 + BASE_SLIP / 10_000)
raw_exit  = tr["exit_px"].values  / (1 - BASE_SLIP / 10_000)
pos_usd   = tr["position_usd"].values   # keep position sizes from original run

# ── simulation kernel ─────────────────────────────────────────────────────────
def run_sim(slip_arr):
    """
    slip_arr : 1-D array of per-trade slippage in bps (length = n trades).
    Returns  : 1-D equity curve array (length = n trades).
    """
    entry  = raw_entry * (1.0 + slip_arr / 10_000)
    exit_  = raw_exit  * (1.0 - slip_arr / 10_000)
    shares = pos_usd / entry
    pnl    = (exit_ - entry) * shares
    return START_EQUITY + np.cumsum(pnl)

def max_dd(eq):
    roll = np.maximum.accumulate(eq)
    dd   = (eq - roll) / roll * 100
    return dd.min()

# ── run all levels ────────────────────────────────────────────────────────────
print("Running simulations ...")
results = {}

for slip in SLIP_LEVELS:
    # deterministic: every trade gets exactly `slip` bps
    det_eq = run_sim(np.full(n, float(slip)))

    # Monte Carlo: per-trade slippage drawn from half-normal around `slip`
    sigma  = slip * SIGMA_FACTOR
    paths  = np.zeros((N_SIMS, n))
    for i in range(N_SIMS):
        if slip == 0:
            per_trade = np.zeros(n)
        else:
            # abs(Normal(slip, sigma)) keeps it non-negative
            per_trade = np.abs(np.random.normal(slip, sigma, n))
        paths[i] = run_sim(per_trade)

    # per-trade win rate at deterministic slippage
    entry_d = raw_entry * (1 + slip / 10_000)
    exit_d  = raw_exit  * (1 - slip / 10_000)
    wr      = float((exit_d > entry_d).mean())
    ret_arr = exit_d / entry_d - 1
    median_r = float(np.median(ret_arr) * 100)

    results[slip] = dict(
        det    = det_eq,
        p10    = np.percentile(paths, 10,  axis=0),
        p25    = np.percentile(paths, 25,  axis=0),
        p50    = np.percentile(paths, 50,  axis=0),
        p75    = np.percentile(paths, 75,  axis=0),
        p90    = np.percentile(paths, 90,  axis=0),
        final  = det_eq[-1],
        ret    = (det_eq[-1] / START_EQUITY - 1) * 100,
        dd     = max_dd(det_eq),
        wr     = wr,
        med_r  = median_r,
    )
    print(f"  {slip:>4} bps | final=${det_eq[-1]:>12,.0f} | "
          f"ret={results[slip]['ret']:>+9.0f}% | "
          f"dd={results[slip]['dd']:>6.1f}% | wr={wr:.0%}")

print()

# ── colour map: green (0 bps) → red (750 bps) ────────────────────────────────
cmap   = plt.get_cmap("RdYlGn_r")
colors = {s: cmap(i / (len(SLIP_LEVELS) - 1)) for i, s in enumerate(SLIP_LEVELS)}

# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 1 — Overview: all deterministic curves overlaid + summary bar charts
# ══════════════════════════════════════════════════════════════════════════════
plt.rcParams.update({
    "font.family":       "sans-serif",
    "font.size":         10,
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "axes.grid":         True,
    "grid.color":        "#E2E8F0",
    "grid.linewidth":    0.7,
})

fig1, axes = plt.subplots(
    2, 3, figsize=(18, 11),
    gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.45, "wspace": 0.32},
    facecolor="white"
)
ax_eq = fig1.add_subplot(2, 1, 1)   # will overlay on top half manually
fig1.clear()

from matplotlib.gridspec import GridSpec
gs = GridSpec(2, 3, figure=fig1,
              height_ratios=[2.2, 1],
              hspace=0.48, wspace=0.30,
              left=0.07, right=0.97, top=0.92, bottom=0.08)

ax_eq  = fig1.add_subplot(gs[0, :])          # full-width equity fan
ax_ret = fig1.add_subplot(gs[1, 0])          # total return vs slip
ax_dd  = fig1.add_subplot(gs[1, 1])          # max drawdown vs slip
ax_wr  = fig1.add_subplot(gs[1, 2])          # win rate vs slip

# equity curves — draw MC p25-p75 band for each level, then deterministic line
for slip in SLIP_LEVELS:
    r   = results[slip]
    c   = colors[slip]
    lbl = f"{slip} bps"
    ax_eq.fill_between(xs, r["p25"], r["p75"], color=c, alpha=0.08)
    ax_eq.plot(xs, r["det"], color=c, lw=1.6 if slip in (0, 10, 100, 500) else 0.9,
               label=lbl, zorder=3)

ax_eq.axhline(START_EQUITY, color="#94A3B8", lw=1, ls="--", alpha=0.6)
ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${v/1e3:.0f}k" if v < 1e6 else f"${v/1e6:.2f}M"))
ax_eq.xaxis.set_major_locator(mdates.YearLocator())
ax_eq.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax_eq.set_ylabel("Portfolio Value", fontsize=11, fontweight="bold")
ax_eq.set_xlim(xs[0], xs[-1])
ax_eq.set_title("P&L Degradation Across Slippage Levels  (shaded band = MC p25-p75)",
                fontsize=13, fontweight="bold", color="#1E293B", pad=10)

legend = ax_eq.legend(ncol=6, fontsize=8.5, framealpha=0.9,
                      edgecolor="#CBD5E1", loc="upper left")

# summary bar charts
slip_arr  = SLIP_LEVELS
ret_vals  = [results[s]["ret"]  for s in slip_arr]
dd_vals   = [results[s]["dd"]   for s in slip_arr]
wr_vals   = [results[s]["wr"]*100 for s in slip_arr]
bar_cols  = [colors[s] for s in slip_arr]
x_pos     = np.arange(len(slip_arr))
labels    = [f"{s}" for s in slip_arr]

# total return
bars = ax_ret.bar(x_pos, ret_vals, color=bar_cols, edgecolor="none", width=0.7)
ax_ret.set_xticks(x_pos); ax_ret.set_xticklabels(labels, fontsize=8)
ax_ret.set_xlabel("Slippage (bps)", fontsize=10)
ax_ret.set_ylabel("Total Return (%)", fontsize=10, fontweight="bold")
ax_ret.set_title("Total Return vs Slippage", fontsize=11, fontweight="bold", color="#1E293B")
ax_ret.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))

# max drawdown
bars2 = ax_dd.bar(x_pos, dd_vals, color=bar_cols, edgecolor="none", width=0.7)
ax_dd.set_xticks(x_pos); ax_dd.set_xticklabels(labels, fontsize=8)
ax_dd.set_xlabel("Slippage (bps)", fontsize=10)
ax_dd.set_ylabel("Max Drawdown (%)", fontsize=10, fontweight="bold")
ax_dd.set_title("Max Drawdown vs Slippage", fontsize=11, fontweight="bold", color="#1E293B")
ax_dd.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

# win rate
bars3 = ax_wr.bar(x_pos, wr_vals, color=bar_cols, edgecolor="none", width=0.7)
ax_wr.axhline(50, color="#94A3B8", lw=1, ls="--", alpha=0.7)
ax_wr.set_xticks(x_pos); ax_wr.set_xticklabels(labels, fontsize=8)
ax_wr.set_xlabel("Slippage (bps)", fontsize=10)
ax_wr.set_ylabel("Win Rate (%)", fontsize=10, fontweight="bold")
ax_wr.set_title("Win Rate vs Slippage", fontsize=11, fontweight="bold", color="#1E293B")
ax_wr.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
ax_wr.set_ylim(0, 100)

fig1.savefig(OUT_OVERVIEW, dpi=160, facecolor="white", bbox_inches="tight")
print(f"Saved: {OUT_OVERVIEW}")

# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 2 — Grid: one panel per slippage level with full MC fan
# ══════════════════════════════════════════════════════════════════════════════
n_levels = len(SLIP_LEVELS)
n_cols   = 3
n_rows   = int(np.ceil(n_levels / n_cols))

fig2, axs = plt.subplots(
    n_rows, n_cols,
    figsize=(18, n_rows * 3.8),
    facecolor="white",
    gridspec_kw={"hspace": 0.55, "wspace": 0.30}
)
axs_flat = axs.flatten()

for idx, slip in enumerate(SLIP_LEVELS):
    ax  = axs_flat[idx]
    r   = results[slip]
    c   = colors[slip]

    # MC fan layers
    ax.fill_between(xs, r["p10"], r["p90"], color=c, alpha=0.12, label="p10-p90")
    ax.fill_between(xs, r["p25"], r["p75"], color=c, alpha=0.22, label="p25-p75")
    ax.plot(xs, r["p50"], color=c, lw=1.2, ls="--", alpha=0.7,  label="MC median")
    ax.plot(xs, r["det"], color=c, lw=2.0,                       label="Deterministic")
    ax.axhline(START_EQUITY, color="#94A3B8", lw=0.8, ls=":", alpha=0.6)

    # zero-profit reference line
    ax.axhline(START_EQUITY, color="#94A3B8", lw=0.8, ls="--", alpha=0.5)

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"${v/1e3:.0f}k" if v < 1e6 else f"${v/1e6:.1f}M"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(labelsize=7.5)

    final  = r["final"]
    ret    = r["ret"]
    dd     = r["dd"]
    wr     = r["wr"]
    med_r  = r["med_r"]

    ax.set_title(
        f"{slip} bps slippage\n"
        f"Final: ${final:,.0f}  |  Return: {ret:+.0f}%  |  "
        f"DD: {dd:.1f}%  |  WR: {wr:.0%}  |  Med: {med_r:+.1f}%",
        fontsize=8.5, fontweight="bold", color="#1E293B", pad=4
    )
    ax.set_xlim(xs[0], xs[-1])

    if idx % n_cols == 0:
        ax.set_ylabel("Portfolio Value", fontsize=8)

# hide any unused axes
for idx in range(n_levels, len(axs_flat)):
    axs_flat[idx].set_visible(False)

fig2.suptitle(
    f"Monte Carlo Slippage Stress Test  ({N_SIMS} paths per level, "
    f"sigma = mean x {SIGMA_FACTOR})",
    fontsize=14, fontweight="bold", color="#1E293B", y=0.995
)

fig2.savefig(OUT_GRID, dpi=160, facecolor="white", bbox_inches="tight")
print(f"Saved: {OUT_GRID}")
print("Done.")
