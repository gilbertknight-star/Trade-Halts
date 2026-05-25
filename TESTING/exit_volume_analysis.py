"""
Exit volume vs trade return analysis.

Uses the exit_liquidity.csv already computed (3:20-3:50 PM window volume)
and plots the same analysis as prehalt_volume_analysis.py but for exit side.

Run from Trade Halts root:
    python TESTING/exit_volume_analysis.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from scipy import stats as scipy_stats

LIQUIDITY_CSV = Path("TESTING/exit_liquidity.csv")
TRADES_CSV    = Path("TESTING/halt_trades_rvol.csv")
OUT_PNG       = Path("TESTING/exit_volume_analysis.png")

# ── Load & merge ──────────────────────────────────────────────────────────────
# exit_liquidity.csv was built row-by-row from halt_trades_rvol.csv in the
# same order — join by position (reset_index) to avoid symbol+date duplicates
# from pyramid trades.
liq    = pd.read_csv(LIQUIDITY_CSV).reset_index(drop=True)
trades = pd.read_csv(TRADES_CSV).reset_index(drop=True)

# exit_liquidity.py skips rows with missing bars; mark those as NaN by index
# Both files are in the same sort order (exit_ts), so align by index directly
df = trades[["date","symbol","ret_pct","entry_ts","exit_ts","rvol"]].copy()
df["window_dolvol_30m"] = np.nan
df["last_bar_dolvol"]   = np.nan
df["window_shares"]     = np.nan

# liq rows correspond to the same rows in trades (same source, same order)
df.loc[liq.index, "window_dolvol_30m"] = liq["window_dolvol_30m"].values
df.loc[liq.index, "last_bar_dolvol"]   = liq["last_bar_dolvol"].values
df.loc[liq.index, "window_shares"]     = liq["window_shares"].values

df = df.dropna(subset=["window_dolvol_30m"])
print(f"Trades with exit volume data: {len(df)} / {len(trades)}")

vol  = df["window_dolvol_30m"].values
rets = df["ret_pct"].values

# ── Stats ─────────────────────────────────────────────────────────────────────
r, p = scipy_stats.pearsonr(np.log1p(vol), rets)
print(f"\nCorrelation (log exit vol vs ret): r={r:.3f}  p={p:.4f}")

print(f"\nEXIT WINDOW DOLLAR VOLUME (3:20-3:50 PM):")
print(f"  p10:    ${np.percentile(vol, 10):>14,.0f}")
print(f"  p25:    ${np.percentile(vol, 25):>14,.0f}")
print(f"  Median: ${np.median(vol):>14,.0f}")
print(f"  p75:    ${np.percentile(vol, 75):>14,.0f}")
print(f"  p90:    ${np.percentile(vol, 90):>14,.0f}")

df["vol_quartile"] = pd.qcut(df["window_dolvol_30m"], 4,
                              labels=["Q1 (lowest)","Q2","Q3","Q4 (highest)"])
print(f"\nRETURN BY EXIT VOLUME QUARTILE:")
print(f"  {'Quartile':<15} {'Trades':>7} {'Win%':>7} {'Median%':>9} {'Mean%':>9} {'p90%':>9}")
for q, grp in df.groupby("vol_quartile", observed=True):
    wr  = (grp["ret_pct"] > 0).mean() * 100
    med = grp["ret_pct"].median()
    avg = grp["ret_pct"].mean()
    p90 = grp["ret_pct"].quantile(0.90)
    print(f"  {str(q):<15} {len(grp):>7} {wr:>6.1f}% {med:>+8.1f}% {avg:>+8.1f}% {p90:>+8.1f}%")

# Threshold sweep — min exit dollar volume required
thresholds = [0, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000]
thr_wr, thr_med, thr_n = [], [], []
for thr in thresholds:
    sub = df[df["window_dolvol_30m"] >= thr]
    thr_wr.append((sub["ret_pct"] > 0).mean() * 100 if len(sub) else 0)
    thr_med.append(sub["ret_pct"].median() if len(sub) else 0)
    thr_n.append(len(sub))
    print(f"  Min exit vol ${thr:>10,.0f}: n={len(sub):>4}  WR={thr_wr[-1]:.1f}%  Median={thr_med[-1]:+.1f}%")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(16, 11))
fig.patch.set_facecolor("#0F172A")
gs  = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.30)
axes = [fig.add_subplot(gs[i, j]) for i in range(2) for j in range(2)]

for ax in axes:
    ax.set_facecolor("#1E293B")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.tick_params(colors="#94A3B8", labelsize=9)
    ax.yaxis.label.set_color("#94A3B8")
    ax.xaxis.label.set_color("#94A3B8")
    ax.grid(True, alpha=0.15, color="#475569")

ax1, ax2, ax3, ax4 = axes

# 1. Scatter: exit volume vs return
sc = ax1.scatter(
    df["window_dolvol_30m"].clip(upper=df["window_dolvol_30m"].quantile(0.98)),
    df["ret_pct"].clip(-60, 300),
    c=df["ret_pct"].clip(-60, 300), cmap="RdYlGn",
    alpha=0.65, s=28, vmin=-50, vmax=150, zorder=3
)
# Trend line
log_vol = np.log1p(df["window_dolvol_30m"].clip(
    upper=df["window_dolvol_30m"].quantile(0.98)))
z = np.polyfit(log_vol, df["ret_pct"].clip(-60, 300), 1)
xfit = np.linspace(log_vol.min(), log_vol.max(), 100)
ax1.plot(np.expm1(xfit), np.polyval(z, xfit),
         color="#F59E0B", lw=2, linestyle="--", label=f"Trend (r={r:.2f})")
ax1.set_xscale("log")
ax1.set_title("Exit Volume vs Trade Return", color="white", fontsize=11, fontweight="bold")
ax1.set_xlabel("30-Min Exit Window Dollar Volume (log scale)", color="#94A3B8")
ax1.set_ylabel("Trade Return %", color="#94A3B8")
ax1.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1e3:.0f}k" if x < 1e6 else f"${x/1e6:.1f}M"))
ax1.axhline(0, color="#475569", lw=0.8)
ax1.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")
plt.colorbar(sc, ax=ax1).ax.tick_params(colors="#94A3B8")

# 2. Median return & win rate by exit volume quartile
q_med = df.groupby("vol_quartile", observed=True)["ret_pct"].median()
q_wr  = df.groupby("vol_quartile", observed=True).apply(
    lambda x: (x["ret_pct"] > 0).mean() * 100, include_groups=False)
q_n   = df.groupby("vol_quartile", observed=True)["ret_pct"].count()
colors_q = ["#DC2626","#D97706","#16A34A","#2563EB"]
bars = ax2.bar(range(4), q_med.values, color=colors_q, alpha=0.85,
               edgecolor="#334155", width=0.6)
ax2_twin = ax2.twinx()
ax2_twin.plot(range(4), q_wr.values, color="#F59E0B", lw=2,
              marker="o", markersize=7, label="Win rate %")
ax2_twin.set_ylabel("Win Rate %", color="#F59E0B")
ax2_twin.tick_params(colors="#F59E0B")
ax2_twin.set_ylim(0, 100)
for bar, med, n in zip(bars, q_med.values, q_n.values):
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
             f"{med:+.0f}%\nn={n}", ha="center", va="bottom", color="white", fontsize=8.5)
ax2.set_xticks(range(4))
ax2.set_xticklabels(["Q1\n(lowest)", "Q2", "Q3", "Q4\n(highest)"], color="#94A3B8")
ax2.set_title("Median Return & Win Rate by Exit Volume Quartile",
              color="white", fontsize=11, fontweight="bold")
ax2.set_ylabel("Median Return %", color="#94A3B8")
ax2.axhline(0, color="#475569", lw=0.8)
ax2_twin.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 3. Return distribution by quartile (overlaid)
bins = np.linspace(-60, 200, 60)
q_colors = ["#DC2626","#D97706","#16A34A","#2563EB"]
for (q, grp), color in zip(df.groupby("vol_quartile", observed=True), q_colors):
    ax3.hist(grp["ret_pct"].clip(-60, 200), bins=bins, color=color,
             alpha=0.45, label=f"{q} (med {grp['ret_pct'].median():+.0f}%)", edgecolor="none")
ax3.axvline(0, color="#475569", lw=1)
ax3.set_title("Return Distribution by Exit Volume Quartile",
              color="white", fontsize=11, fontweight="bold")
ax3.set_xlabel("Trade Return % (clipped at +200%)", color="#94A3B8")
ax3.set_ylabel("Count", color="#94A3B8")
ax3.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 4. Threshold sweep — min exit volume filter effect
ax4_twin = ax4.twinx()
bars4 = ax4.bar(range(len(thresholds)), thr_med, color="#2563EB", alpha=0.75,
                edgecolor="#334155", width=0.6, label="Median return %")
ax4_twin.plot(range(len(thresholds)), thr_wr, color="#F59E0B", lw=2,
              marker="o", markersize=6, label="Win rate %")
ax4_twin.set_ylabel("Win Rate %", color="#F59E0B")
ax4_twin.tick_params(colors="#F59E0B")
for i, (med, n) in enumerate(zip(thr_med, thr_n)):
    ax4.text(i, max(med, 0) + 0.5, f"{med:+.0f}%\nn={n}",
             ha="center", va="bottom", color="white", fontsize=7.5)
ax4.set_xticks(range(len(thresholds)))
thr_labels = ["$0","$50k","$100k","$250k","$500k","$1M","$2.5M","$5M"]
ax4.set_xticklabels(thr_labels, rotation=35, ha="right", color="#94A3B8", fontsize=8.5)
ax4.set_title("Effect of Min Exit Dollar Volume Filter",
              color="white", fontsize=11, fontweight="bold")
ax4.set_ylabel("Median Return %", color="#94A3B8")
ax4.axhline(0, color="#475569", lw=0.8)
ax4_twin.legend(fontsize=9, framealpha=0.3, loc="lower right",
                facecolor="#1E293B", labelcolor="white")

fig.suptitle(
    "Exit Volume Analysis — Does 3:20-3:50 PM Dollar Volume Predict Returns?",
    color="white", fontsize=13, fontweight="bold", y=1.01,
)
plt.tight_layout()
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"\nSaved: {OUT_PNG}")
