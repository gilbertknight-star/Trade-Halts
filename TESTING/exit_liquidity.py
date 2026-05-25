"""
Exit liquidity analysis for the RVOL-filtered 550-trade backtest.

For each trade, loads the 1-min bar data and measures dollar volume
in the final 30 minutes of trading (3:20-3:50 PM ET) — the window
where we'd be exiting. This tells us the realistic position cap.

Outputs:
  - TESTING/exit_liquidity.csv   — per-trade liquidity stats
  - TESTING/exit_liquidity.png   — distribution charts

Run from Trade Halts root:
    python TESTING/exit_liquidity.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from zoneinfo import ZoneInfo

TRADES_CSV  = Path("TESTING/halt_trades_rvol.csv")
EVENTS_PATH = Path("TESTING/halt_events.parquet")
DATA_ROOT   = Path("data_massive/Halt_model")
OUT_CSV     = Path("TESTING/exit_liquidity.csv")
OUT_PNG     = Path("TESTING/exit_liquidity.png")

NY_TZ = ZoneInfo("America/New_York")

# ── Bar loader ────────────────────────────────────────────────────────────────
_cache = {}

def load_bars(bars_file) -> pd.DataFrame | None:
    key = str(bars_file or "").strip()
    if key in _cache:
        return _cache[key]
    if not key:
        _cache[key] = None
        return None
    norm = key.replace("\\", "/")
    candidates = [Path(key)]
    for prefix in ["data_massive/Halt_model/", "data_massive/"]:
        if norm.startswith(prefix):
            candidates.append(DATA_ROOT / norm[len(prefix):])
    candidates.append(DATA_ROOT / Path(key).name)
    for c in candidates:
        if not c.exists():
            continue
        try:
            b = pd.read_parquet(c)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            _cache[key] = b.sort_index()
            return _cache[key]
        except Exception:
            pass
    _cache[key] = None
    return None

# ── Load trades + events (need bars_file path) ────────────────────────────────
trades = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
trades["date"] = pd.to_datetime(trades["date"]).dt.date

events = pd.read_parquet(EVENTS_PATH)
events["anchor_ts_utc"] = pd.to_datetime(events["anchor_ts_utc"], utc=True)
events["event_date"]    = pd.to_datetime(events["event_date"]).dt.date

# Match trades to events by symbol + date to get bars_file
ev_lookup = {}
for _, row in events.iterrows():
    key = (row["symbol_resolved"], row["event_date"])
    if key not in ev_lookup:
        ev_lookup[key] = row["bars_file"]

print(f"Analysing exit liquidity for {len(trades)} trades...")
print("(Loading bar files — may take 30-60s)\n")

# ── Per-trade liquidity measurement ──────────────────────────────────────────
rows = []
missing = 0

for _, t in trades.iterrows():
    sym      = t["symbol"]
    tdate    = t["date"]
    exit_px  = t["exit_px"]
    ret_pct  = t["ret_pct"]

    bars_file = ev_lookup.get((sym, tdate))
    b = load_bars(bars_file)
    if b is None or b.empty:
        missing += 1
        continue

    # Build ET timestamps for the exit window
    exit_window_start = pd.Timestamp(
        year=tdate.year, month=tdate.month, day=tdate.day,
        hour=15, minute=20, tz=NY_TZ
    ).tz_convert("UTC")
    exit_window_end = pd.Timestamp(
        year=tdate.year, month=tdate.month, day=tdate.day,
        hour=15, minute=50, tz=NY_TZ
    ).tz_convert("UTC")
    last_bar_end = pd.Timestamp(
        year=tdate.year, month=tdate.month, day=tdate.day,
        hour=16, minute=0, tz=NY_TZ
    ).tz_convert("UTC")

    # 30-min exit window bars (3:20-3:50 PM)
    window = b[(b.index >= exit_window_start) & (b.index < exit_window_end)]

    # Last single bar at exit (3:49-3:50 PM)
    last_bar = b[(b.index >= exit_window_end - pd.Timedelta(minutes=1)) &
                 (b.index < exit_window_end)]

    if window.empty:
        missing += 1
        continue

    # Dollar volume in each period
    window_dolvol  = float((window["close"] * window["volume"]).sum())
    last_bar_dolvol = float((last_bar["close"] * last_bar["volume"]).sum()) if not last_bar.empty else 0

    # Shares in the window
    window_shares  = float(window["volume"].sum())

    rows.append({
        "date":              str(tdate),
        "symbol":            sym,
        "ret_pct":           round(ret_pct, 2),
        "exit_px":           round(exit_px, 4),
        "window_dolvol_30m": round(window_dolvol, 0),    # $ volume 3:20-3:50
        "last_bar_dolvol":   round(last_bar_dolvol, 0),  # $ volume in last bar
        "window_shares":     round(window_shares, 0),
        # What % of 30-min window would each cap represent?
        "pct_vol_10k":       round(10_000  / window_dolvol * 100, 2) if window_dolvol > 0 else 999,
        "pct_vol_25k":       round(25_000  / window_dolvol * 100, 2) if window_dolvol > 0 else 999,
        "pct_vol_50k":       round(50_000  / window_dolvol * 100, 2) if window_dolvol > 0 else 999,
        "pct_vol_100k":      round(100_000 / window_dolvol * 100, 2) if window_dolvol > 0 else 999,
        "pct_vol_250k":      round(250_000 / window_dolvol * 100, 2) if window_dolvol > 0 else 999,
    })

liq = pd.DataFrame(rows)
liq.to_csv(OUT_CSV, index=False)
print(f"Missing bar data: {missing} trades")
print(f"Analysed:         {len(liq)} trades\n")

# ── Print liquidity summary ───────────────────────────────────────────────────
print("30-MIN EXIT WINDOW DOLLAR VOLUME (3:20-3:50 PM ET):")
print(f"  Median:     ${liq['window_dolvol_30m'].median():>12,.0f}")
print(f"  Mean:       ${liq['window_dolvol_30m'].mean():>12,.0f}")
print(f"  p10:        ${liq['window_dolvol_30m'].quantile(0.10):>12,.0f}")
print(f"  p25:        ${liq['window_dolvol_30m'].quantile(0.25):>12,.0f}")
print(f"  p75:        ${liq['window_dolvol_30m'].quantile(0.75):>12,.0f}")
print(f"  p90:        ${liq['window_dolvol_30m'].quantile(0.90):>12,.0f}")
print()

# At each cap, what % of 30-min volume are we?
print("% OF 30-MIN WINDOW VOLUME CONSUMED AT EACH CAP:")
print(f"  {'Cap':>8}   {'p10 (worst)':>12}  {'p25':>8}  {'Median':>8}  {'p75':>8}")
for cap, col in [(10_000,"pct_vol_10k"),(25_000,"pct_vol_25k"),
                 (50_000,"pct_vol_50k"),(100_000,"pct_vol_100k"),(250_000,"pct_vol_250k")]:
    p10 = liq[col].quantile(0.10)
    p25 = liq[col].quantile(0.25)
    med = liq[col].median()
    p75 = liq[col].quantile(0.75)
    print(f"  ${cap:>7,.0f}   {p10:>11.1f}%  {p25:>7.1f}%  {med:>7.1f}%  {p75:>7.1f}%")

print()
print("TRADES WHERE $50k > 10% OF EXIT WINDOW VOLUME (hard to fill):")
illiquid = liq[liq["pct_vol_50k"] > 10].sort_values("pct_vol_50k", ascending=False)
print(illiquid[["date","symbol","ret_pct","exit_px","window_dolvol_30m","pct_vol_50k"]].head(20).to_string())

print()
print("TRADES WHERE $25k > 10% OF EXIT WINDOW VOLUME:")
illiquid25 = liq[liq["pct_vol_25k"] > 10].sort_values("pct_vol_25k", ascending=False)
print(f"  Count: {len(illiquid25)} trades ({len(illiquid25)/len(liq)*100:.1f}% of total)")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig, axes = plt.subplots(2, 2, figsize=(15, 10))
fig.patch.set_facecolor("#0F172A")

for ax in axes.flat:
    ax.set_facecolor("#1E293B")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.tick_params(colors="#94A3B8", labelsize=9)
    ax.yaxis.label.set_color("#94A3B8")
    ax.xaxis.label.set_color("#94A3B8")
    ax.grid(True, alpha=0.15, color="#475569")

ax1, ax2, ax3, ax4 = axes.flat

# 1. Distribution of 30-min exit dollar volume (log scale)
dolvols = liq["window_dolvol_30m"].clip(upper=liq["window_dolvol_30m"].quantile(0.99))
ax1.hist(np.log10(liq["window_dolvol_30m"].clip(lower=1)), bins=50,
         color="#2563EB", alpha=0.8, edgecolor="none")
med_log = np.log10(liq["window_dolvol_30m"].median())
ax1.axvline(med_log, color="#F59E0B", lw=2, linestyle="--",
            label=f"Median ${liq['window_dolvol_30m'].median():,.0f}")
for cap, color, label in [(50_000,"#16A34A","$50k cap"),
                           (100_000,"#D97706","$100k cap"),
                           (250_000,"#DC2626","$250k cap")]:
    ax1.axvline(np.log10(cap), color=color, lw=1.5, linestyle=":",
                label=f"{label}")
ax1.set_title("30-Min Exit Window Dollar Volume", color="white", fontsize=11, fontweight="bold")
ax1.set_xlabel("Dollar Volume (log10 scale)", color="#94A3B8")
ax1.set_ylabel("# Trades", color="#94A3B8")
ax1.xaxis.set_major_formatter(mticker.FuncFormatter(
    lambda x, _: f"${10**x:,.0f}"))
ax1.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 2. % of exit window volume consumed at $50k vs $25k
ax2.hist(liq["pct_vol_25k"].clip(0, 50), bins=50,
         color="#16A34A", alpha=0.7, label="$25k cap", edgecolor="none")
ax2.hist(liq["pct_vol_50k"].clip(0, 50), bins=50,
         color="#D97706", alpha=0.7, label="$50k cap", edgecolor="none")
ax2.axvline(10, color="#DC2626", lw=2, linestyle="--", label="10% threshold")
ax2.set_title("Position as % of 30-Min Exit Volume", color="white", fontsize=11, fontweight="bold")
ax2.set_xlabel("% of Exit Window Volume", color="#94A3B8")
ax2.set_ylabel("# Trades", color="#94A3B8")
ax2.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 3. Scatter: return vs exit liquidity
sc = ax3.scatter(
    liq["window_dolvol_30m"].clip(upper=5e6),
    liq["ret_pct"].clip(-50, 300),
    c=liq["ret_pct"].clip(-50, 300), cmap="RdYlGn",
    alpha=0.6, s=25, vmin=-50, vmax=200,
)
ax3.axvline(50_000,  color="#16A34A", lw=1.5, linestyle="--", label="$50k cap")
ax3.axvline(100_000, color="#D97706", lw=1.5, linestyle="--", label="$100k cap")
ax3.set_title("Return vs Exit Liquidity", color="white", fontsize=11, fontweight="bold")
ax3.set_xlabel("30-Min Exit Volume ($, clipped at $5M)", color="#94A3B8")
ax3.set_ylabel("Trade Return % (clipped at +300%)", color="#94A3B8")
ax3.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1e3:.0f}k"))
ax3.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")
plt.colorbar(sc, ax=ax3).ax.tick_params(colors="#94A3B8")

# 4. Cumulative % of trades executable at each cap (< 10% of volume)
caps     = [5_000, 10_000, 25_000, 50_000, 75_000, 100_000, 150_000, 200_000, 250_000]
pct_ok   = []
for cap in caps:
    pct_vol = cap / liq["window_dolvol_30m"].replace(0, np.nan) * 100
    pct_ok.append((pct_vol <= 10).mean() * 100)

ax4.plot([c/1000 for c in caps], pct_ok, color="#2563EB", lw=2.5, marker="o", markersize=6)
ax4.fill_between([c/1000 for c in caps], pct_ok, alpha=0.15, color="#2563EB")
ax4.axhline(90, color="#16A34A", lw=1.5, linestyle="--", label="90% of trades")
ax4.axhline(80, color="#D97706", lw=1.5, linestyle="--", label="80% of trades")
ax4.set_title("% of Trades Fillable (<10% of Exit Volume)", color="white", fontsize=11, fontweight="bold")
ax4.set_xlabel("Position Cap ($k)", color="#94A3B8")
ax4.set_ylabel("% of Trades Executable", color="#94A3B8")
ax4.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
ax4.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")
for cap, pct in zip(caps, pct_ok):
    ax4.annotate(f"{pct:.0f}%", (cap/1000, pct), textcoords="offset points",
                 xytext=(0, 7), ha="center", fontsize=8, color="#94A3B8")

fig.suptitle(
    "Exit Liquidity Analysis — RVOL Filtered Halts  (3:20-3:50 PM ET window)",
    color="white", fontsize=13, fontweight="bold", y=1.01,
)
plt.tight_layout()
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"\nSaved: {OUT_PNG}")
