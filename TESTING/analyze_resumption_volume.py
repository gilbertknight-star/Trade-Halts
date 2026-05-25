"""
Analyze resumption-bar volume for RVOL>1 trades.
For each trade we already took, look at:
  - Shares in the first resumption bar
  - Dollar volume in the first resumption bar
  - Dollar volume in the first 5 minutes post-resumption
  - Our position size vs available dollar volume (can we actually get filled?)

Run from Trade Halts root:
    python TESTING/analyze_resumption_volume.py
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from zoneinfo import ZoneInfo

NY_TZ      = ZoneInfo("America/New_York")
DATA_ROOT  = Path("data_massive/Halt_model")
TRADES_CSV = Path("TESTING/halt_trades_rvol.csv")
EVENTS_PQ  = Path("TESTING/halt_events.parquet")
OUT_PNG    = Path("TESTING/resumption_volume_analysis.png")

# ── bar loader ────────────────────────────────────────────────────────────────
_cache = {}
def load_bars(bars_file):
    key = str(bars_file or "").strip()
    if key in _cache: return _cache[key]
    norm = key.replace("\\", "/")
    candidates = [Path(norm)]
    for sep in ["data_massive/Halt_model/", "data_massive/"]:
        if sep in norm:
            candidates.append(DATA_ROOT / norm.split(sep)[-1])
    parts = norm.split("/")
    for i in range(1, len(parts)):
        candidates.append(DATA_ROOT / "/".join(parts[i:]))
    for c in candidates:
        if not c.exists(): continue
        try:
            b = pd.read_parquet(c)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            b = b.sort_index()
            _cache[key] = b; return b
        except Exception: pass
    _cache[key] = None; return None

# ── load trades + join bars_file from events ─────────────────────────────────
print("Loading trades ...")
tr = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
tr["entry_ts_utc"] = pd.to_datetime(tr["entry_ts"], utc=False).dt.tz_localize("UTC")

ev = pd.read_parquet(EVENTS_PQ)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
ev["symbol_resolved"] = ev["symbol_resolved"].astype(str)
ev["event_date_str"]  = ev["event_date"].astype(str)

tr["event_date_str"] = tr["date"].astype(str)
tr["symbol"]         = tr["symbol"].astype(str)

# match each trade back to its event to get bars_file
merged = tr.merge(
    ev[["symbol_resolved", "event_date_str", "bars_file", "anchor_ts_utc"]],
    left_on=["symbol", "event_date_str"],
    right_on=["symbol_resolved", "event_date_str"],
    how="left"
)
print(f"  {len(merged):,} trades, {merged['bars_file'].notna().sum():,} with bars_file matched")

# ── extract resumption volume per trade ───────────────────────────────────────
print("Extracting resumption volume ...")
rows = []
for _, row in merged.iterrows():
    bars = load_bars(row.get("bars_file"))
    if bars is None or bars.empty: continue

    anchor = row["anchor_ts_utc"]
    resume = bars[bars.index >= anchor]
    if resume.empty: continue

    # first bar at resumption
    first = resume.iloc[0]
    first_vol_shares = float(first["volume"])
    first_vol_dollar = float(first["volume"] * first["close"])

    # first 5 minutes post-resumption
    five_min_bars = resume[resume.index < anchor + pd.Timedelta(minutes=5)]
    five_min_vol_shares = float(five_min_bars["volume"].sum())
    five_min_vol_dollar = float((five_min_bars["volume"] * five_min_bars["close"]).sum())

    # first 1 minute post-resumption
    one_min_bars = resume[resume.index < anchor + pd.Timedelta(minutes=1)]
    one_min_vol_dollar = float((one_min_bars["volume"] * one_min_bars["close"]).sum())

    rows.append({
        "symbol":               row["symbol"],
        "date":                 row["date"],
        "entry_px":             row["entry_px"],
        "position_usd":         row["position_usd"],
        "pnl":                  row["pnl"],
        "ret_pct":              row["ret_pct"],
        "rvol":                 row["rvol"],
        "first_bar_vol_shares": first_vol_shares,
        "first_bar_vol_dollar": first_vol_dollar,
        "one_min_vol_dollar":   one_min_vol_dollar,
        "five_min_vol_dollar":  five_min_vol_dollar,
        "pos_vs_1min_pct":      row["position_usd"] / max(one_min_vol_dollar, 1) * 100,
        "pos_vs_5min_pct":      row["position_usd"] / max(five_min_vol_dollar, 1) * 100,
    })

df = pd.DataFrame(rows)
print(f"  {len(df):,} trades with resumption volume data\n")

# ── print summary stats ───────────────────────────────────────────────────────
for col, label in [
    ("first_bar_vol_shares", "First resumption bar  — shares"),
    ("first_bar_vol_dollar", "First resumption bar  — dollar vol ($)"),
    ("one_min_vol_dollar",   "First 1 min post-resume — dollar vol ($)"),
    ("five_min_vol_dollar",  "First 5 min post-resume — dollar vol ($)"),
    ("pos_vs_1min_pct",      "Our position as % of 1-min dollar vol"),
    ("pos_vs_5min_pct",      "Our position as % of 5-min dollar vol"),
]:
    s = df[col]
    print(f"{label}")
    print(f"  p5     : {s.quantile(0.05):>12,.1f}")
    print(f"  p25    : {s.quantile(0.25):>12,.1f}")
    print(f"  median : {s.median():>12,.1f}")
    print(f"  p75    : {s.quantile(0.75):>12,.1f}")
    print(f"  p95    : {s.quantile(0.95):>12,.1f}")
    print()

# liquidity buckets
print("Liquidity buckets (first resumption bar dollar volume):")
buckets = [0, 500, 1_000, 5_000, 10_000, 50_000, 100_000, float("inf")]
labels  = ["<$500","$500-1k","$1k-5k","$5k-10k","$10k-50k","$50k-100k",">$100k"]
for i, (lo, hi) in enumerate(zip(buckets, buckets[1:])):
    cnt = ((df["first_bar_vol_dollar"] >= lo) & (df["first_bar_vol_dollar"] < hi)).sum()
    wr  = df.loc[(df["first_bar_vol_dollar"] >= lo) & (df["first_bar_vol_dollar"] < hi), "ret_pct"]
    wr_str = f"  WR={( wr > 0).mean():.0%}  med={wr.median():+.1f}%" if len(wr) > 0 else ""
    print(f"  {labels[i]:<12}: {cnt:>4} trades ({cnt/len(df):.1%}){wr_str}")

thin = (df["first_bar_vol_dollar"] < 1_000).sum()
print(f"\nTrades where first bar dollar vol < $1,000 (very thin): {thin} ({thin/len(df):.1%})")
thin5 = (df["first_bar_vol_dollar"] < 5_000).sum()
print(f"Trades where first bar dollar vol < $5,000 (thin)      : {thin5} ({thin5/len(df):.1%})")

# ── chart ─────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 10,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": "#E2E8F0", "grid.linewidth": 0.7,
})

fig, axes = plt.subplots(2, 3, figsize=(17, 10), facecolor="white",
                          gridspec_kw={"hspace": 0.48, "wspace": 0.32})
fig.suptitle("Resumption Volume Analysis — RVOL > 1 Trades",
             fontsize=14, fontweight="bold", color="#1E293B", y=0.99)

C = "#2563EB"

# 1. First bar dollar volume distribution (log scale)
ax = axes[0, 0]
vals = df["first_bar_vol_dollar"].clip(upper=df["first_bar_vol_dollar"].quantile(0.98))
ax.hist(np.log10(vals.clip(lower=1)), bins=40, color=C, alpha=0.7, edgecolor="none")
ax.axvline(np.log10(1_000),  color="#F59E0B", lw=1.5, ls="--", label="$1k")
ax.axvline(np.log10(5_000),  color="#EF4444", lw=1.5, ls="--", label="$5k")
ax.axvline(np.log10(10_000), color="#10B981", lw=1.5, ls="--", label="$10k")
ax.set_xlabel("First Bar Dollar Volume (log10 $)", fontsize=10)
ax.set_ylabel("Count", fontsize=10)
ax.set_title("First Resumption Bar\nDollar Volume Distribution", fontsize=11, fontweight="bold")
ax.legend(fontsize=8)
ax.xaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${10**v:,.0f}" if v < 6 else f"${10**v/1e6:.0f}M"))

# 2. 5-min dollar volume distribution
ax = axes[0, 1]
vals5 = df["five_min_vol_dollar"].clip(upper=df["five_min_vol_dollar"].quantile(0.98))
ax.hist(np.log10(vals5.clip(lower=1)), bins=40, color="#7C3AED", alpha=0.7, edgecolor="none")
med5 = df["five_min_vol_dollar"].median()
ax.axvline(np.log10(med5), color="#F59E0B", lw=2, ls="--", label=f"Median ${med5:,.0f}")
ax.set_xlabel("5-Min Dollar Volume (log10 $)", fontsize=10)
ax.set_ylabel("Count", fontsize=10)
ax.set_title("First 5 Min Post-Resume\nDollar Volume Distribution", fontsize=11, fontweight="bold")
ax.legend(fontsize=8)
ax.xaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${10**v:,.0f}" if v < 6 else f"${10**v/1e6:.0f}M"))

# 3. Our position size as % of 1-min volume
ax = axes[0, 2]
pct_vals = df["pos_vs_1min_pct"].clip(upper=100)
ax.hist(pct_vals, bins=40, color="#DC2626", alpha=0.7, edgecolor="none")
ax.axvline(5,  color="#10B981", lw=1.5, ls="--", label="5% of vol")
ax.axvline(20, color="#F59E0B", lw=1.5, ls="--", label="20% of vol")
ax.set_xlabel("Position Size as % of 1-Min Volume", fontsize=10)
ax.set_ylabel("Count", fontsize=10)
ax.set_title("Market Impact:\nOur Size vs 1-Min Dollar Volume", fontsize=11, fontweight="bold")
ax.legend(fontsize=8)
ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

# 4. Scatter: first bar volume vs return
ax = axes[1, 0]
ax.scatter(np.log10(df["first_bar_vol_dollar"].clip(lower=1)),
           df["ret_pct"].clip(-40, 100),
           alpha=0.4, s=20, color=C, edgecolors="none")
ax.axhline(0, color="#94A3B8", lw=1, ls="--")
ax.set_xlabel("First Bar Dollar Volume (log10 $)", fontsize=10)
ax.set_ylabel("Trade Return (%)", fontsize=10)
ax.set_title("Volume at Resumption vs Trade Return", fontsize=11, fontweight="bold")
ax.xaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${10**v:,.0f}" if v < 6 else f"${10**v/1e6:.0f}M"))
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

# 5. Win rate by liquidity bucket (bar chart)
ax = axes[1, 1]
bucket_labels, bucket_wrs, bucket_meds, bucket_ns = [], [], [], []
for lo, hi, lbl in zip(buckets, buckets[1:], labels):
    sub = df[(df["first_bar_vol_dollar"] >= lo) & (df["first_bar_vol_dollar"] < hi)]
    if len(sub) < 3: continue
    bucket_labels.append(lbl)
    bucket_wrs.append((sub["ret_pct"] > 0).mean() * 100)
    bucket_meds.append(sub["ret_pct"].median())
    bucket_ns.append(len(sub))

x = np.arange(len(bucket_labels))
bars = ax.bar(x, bucket_wrs, color=C, alpha=0.75, edgecolor="none", width=0.6)
ax.axhline(50, color="#94A3B8", lw=1, ls="--", alpha=0.7)
for i, (wr, n) in enumerate(zip(bucket_wrs, bucket_ns)):
    ax.text(i, wr + 0.5, f"n={n}", ha="center", va="bottom", fontsize=7.5, color="#475569")
ax.set_xticks(x)
ax.set_xticklabels(bucket_labels, rotation=30, ha="right", fontsize=8)
ax.set_ylabel("Win Rate (%)", fontsize=10)
ax.set_title("Win Rate by First-Bar\nDollar Volume Bucket", fontsize=11, fontweight="bold")
ax.set_ylim(0, 100)

# 6. Median return by liquidity bucket
ax = axes[1, 2]
colors_med = ["#10B981" if m > 0 else "#EF4444" for m in bucket_meds]
ax.bar(x, bucket_meds, color=colors_med, alpha=0.75, edgecolor="none", width=0.6)
ax.axhline(0, color="#94A3B8", lw=1, ls="--")
for i, (m, n) in enumerate(zip(bucket_meds, bucket_ns)):
    ax.text(i, m + (1 if m >= 0 else -2), f"n={n}",
            ha="center", va="bottom" if m >= 0 else "top", fontsize=7.5, color="#475569")
ax.set_xticks(x)
ax.set_xticklabels(bucket_labels, rotation=30, ha="right", fontsize=8)
ax.set_ylabel("Median Return (%)", fontsize=10)
ax.set_title("Median Trade Return by First-Bar\nDollar Volume Bucket", fontsize=11, fontweight="bold")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))

plt.savefig(OUT_PNG, dpi=160, facecolor="white", bbox_inches="tight")
print(f"\nSaved: {OUT_PNG}")
