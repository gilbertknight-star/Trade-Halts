"""
Pre-halt volume vs trade return analysis.

For each trade, finds the 1-minute bar immediately before the halt started
and measures its volume. Plots volume vs return to see if pre-halt volume
is a useful filter.

Flags:
  - "no_prehalt_bar" : halted within the first bar of the session (e.g. 9:30 open halt)
    → still plotted but marked separately (these are gap-up-at-open halts)

Run from Trade Halts root:
    python TESTING/prehalt_volume_analysis.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from zoneinfo import ZoneInfo
from scipy import stats as scipy_stats

TRADES_CSV  = Path("TESTING/halt_trades_rvol.csv")
EVENTS_PATH = Path("TESTING/halt_events.parquet")
DATA_ROOT   = Path("data_massive/Halt_model")
OUT_CSV     = Path("TESTING/prehalt_volume_analysis.csv")
OUT_PNG     = Path("TESTING/prehalt_volume_analysis.png")

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

# ── Load data ─────────────────────────────────────────────────────────────────
trades = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
trades["date"] = pd.to_datetime(trades["date"]).dt.date

events = pd.read_parquet(EVENTS_PATH)
events["anchor_ts_utc"] = pd.to_datetime(events["anchor_ts_utc"], utc=True)
events["event_date"]    = pd.to_datetime(events["event_date"]).dt.date

# Build lookup: (symbol, date) → (halt_time_utc, bars_file)
# Multiple halts on same symbol+date → keep all in a list
ev_lookup = {}
for _, row in events.iterrows():
    key = (row["symbol_resolved"], row["event_date"])
    if key not in ev_lookup:
        ev_lookup[key] = []
    ev_lookup[key].append({
        "halt_ts":  row["anchor_ts_utc"],
        "bars_file": row["bars_file"],
    })

print(f"Processing {len(trades)} trades...")

# ── Per-trade: find pre-halt 1-min bar volume ─────────────────────────────────
rows = []
missing = 0

for _, t in trades.iterrows():
    sym   = t["symbol"]
    tdate = t["date"]
    ret   = t["ret_pct"]
    entry = t["entry_ts"]  # resumption time

    ev_list = ev_lookup.get((sym, tdate), [])
    if not ev_list:
        missing += 1
        continue

    # Match this trade to the closest halt event before this resumption
    # (entry_ts is resumption, halt is ~5 min before)
    entry_utc = pd.Timestamp(entry).tz_localize("UTC") if t["entry_ts"].tzinfo is None else pd.Timestamp(entry).tz_convert("UTC")
    best_ev   = min(ev_list, key=lambda e: abs((e["halt_ts"] - entry_utc).total_seconds()))
    halt_ts   = best_ev["halt_ts"]
    bars_file = best_ev["bars_file"]

    b = load_bars(bars_file)
    if b is None or b.empty:
        missing += 1
        continue

    # Market open for this date (9:30 AM ET)
    mkt_open_utc = pd.Timestamp(
        year=tdate.year, month=tdate.month, day=tdate.day,
        hour=9, minute=30, tz=NY_TZ
    ).tz_convert("UTC")

    # Last 1-min bar BEFORE the halt
    # Bar at time T covers [T, T+1min), so we want bars where bar_start < halt_ts
    pre_halt_bars = b[b.index < halt_ts]
    rth_pre       = pre_halt_bars[pre_halt_bars.index >= mkt_open_utc]

    if rth_pre.empty:
        # Halted before any RTH bar printed → gap-up-at-open / immediate halt
        flag = "no_prehalt_bar"
        prehalt_vol      = np.nan
        prehalt_dolvol   = np.nan
        prehalt_bar_time = np.nan
        mins_into_session = 0.0
    else:
        last_bar  = rth_pre.iloc[-1]
        flag      = "ok"
        prehalt_vol      = float(last_bar["volume"])
        prehalt_dolvol   = float(last_bar["close"] * last_bar["volume"])
        prehalt_bar_time = rth_pre.index[-1]
        mins_into_session = (halt_ts - mkt_open_utc).total_seconds() / 60

    rows.append({
        "date":               str(tdate),
        "symbol":             sym,
        "ret_pct":            round(ret, 4),
        "entry_ts":           t["entry_ts"],
        "halt_ts":            halt_ts,
        "mins_into_session":  round(mins_into_session, 1),
        "flag":               flag,
        "prehalt_vol":        prehalt_vol,
        "prehalt_dolvol":     prehalt_dolvol,
        "rvol":               t["rvol"],
    })

df = pd.DataFrame(rows)
df.to_csv(OUT_CSV, index=False)

ok    = df[df["flag"] == "ok"]
flagg = df[df["flag"] == "no_prehalt_bar"]

print(f"\nMissing bar data:     {missing}")
print(f"Has pre-halt bar:     {len(ok)}  ({len(ok)/len(df)*100:.1f}%)")
print(f"No pre-halt bar:      {len(flagg)}  ({len(flagg)/len(df)*100:.1f}%)  (halted at/near open)")

print(f"\nPRE-HALT BAR VOLUME (last 1-min before halt):")
print(f"  Median vol:         {ok['prehalt_vol'].median():>12,.0f} shares")
print(f"  Median dolvol:      ${ok['prehalt_dolvol'].median():>12,.0f}")
print(f"  p10:                {ok['prehalt_vol'].quantile(0.10):>12,.0f} shares")
print(f"  p25:                {ok['prehalt_vol'].quantile(0.25):>12,.0f} shares")
print(f"  p75:                {ok['prehalt_vol'].quantile(0.75):>12,.0f} shares")
print(f"  p90:                {ok['prehalt_vol'].quantile(0.90):>12,.0f} shares")

# Correlation
r, p = scipy_stats.pearsonr(np.log1p(ok["prehalt_vol"]), ok["ret_pct"])
print(f"\nCorrelation (log vol vs ret): r={r:.3f}  p={p:.3f}")

# Return stats split by volume quartile
ok["vol_quartile"] = pd.qcut(ok["prehalt_vol"], 4, labels=["Q1 (lowest)","Q2","Q3","Q4 (highest)"])
print(f"\nRETURN BY PRE-HALT VOLUME QUARTILE:")
print(f"  {'Quartile':<15} {'Trades':>7} {'Win%':>7} {'Median%':>9} {'Mean%':>9} {'p90%':>9}")
for q, grp in ok.groupby("vol_quartile", observed=True):
    wr  = (grp["ret_pct"] > 0).mean() * 100
    med = grp["ret_pct"].median()
    avg = grp["ret_pct"].mean()
    p90 = grp["ret_pct"].quantile(0.90)
    print(f"  {str(q):<15} {len(grp):>7} {wr:>6.1f}% {med:>+8.1f}% {avg:>+8.1f}% {p90:>+8.1f}%")

no_bar_stats = flagg["ret_pct"]
print(f"\nNO PRE-HALT BAR TRADES (open halts):")
print(f"  Count:   {len(flagg)}")
print(f"  Win%:    {(no_bar_stats > 0).mean()*100:.1f}%")
print(f"  Median:  {no_bar_stats.median():+.2f}%")
print(f"  Mean:    {no_bar_stats.mean():+.2f}%")

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

# 1. Scatter: pre-halt volume vs return
ret_clip = 300
ax1.scatter(ok["prehalt_vol"].clip(upper=ok["prehalt_vol"].quantile(0.99)),
            ok["ret_pct"].clip(-60, ret_clip),
            c=ok["ret_pct"].clip(-60, ret_clip), cmap="RdYlGn",
            alpha=0.65, s=28, vmin=-50, vmax=150, label="Has pre-halt bar", zorder=3)

# Flagged trades on x=0 with jitter
jitter = np.random.uniform(-2000, 2000, size=len(flagg))
ax1.scatter(np.zeros(len(flagg)) + jitter,
            flagg["ret_pct"].clip(-60, ret_clip),
            c="#A855F7", alpha=0.5, s=22, marker="^",
            label=f"No pre-halt bar (n={len(flagg)}, open halts)", zorder=2)

# Trend line on log volume
log_vol = np.log1p(ok["prehalt_vol"].clip(upper=ok["prehalt_vol"].quantile(0.99)))
z = np.polyfit(log_vol, ok["ret_pct"].clip(-60, ret_clip), 1)
xfit = np.linspace(log_vol.min(), log_vol.max(), 100)
ax1.plot(np.expm1(xfit), np.polyval(z, xfit),
         color="#F59E0B", lw=2, linestyle="--", label=f"Trend (r={r:.2f})", zorder=4)

ax1.set_xscale("symlog", linthresh=1000)
ax1.set_title("Pre-Halt Volume vs Trade Return", color="white", fontsize=11, fontweight="bold")
ax1.set_xlabel("Last 1-Min Volume Before Halt (log scale)", color="#94A3B8")
ax1.set_ylabel("Trade Return %", color="#94A3B8")
ax1.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
ax1.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")
ax1.axhline(0, color="#475569", lw=0.8)

# 2. Median return by volume quartile (bar chart)
q_data   = ok.groupby("vol_quartile", observed=True)["ret_pct"]
q_med    = q_data.median()
q_wr     = ok.groupby("vol_quartile", observed=True).apply(lambda x: (x["ret_pct"] > 0).mean() * 100)
colors_q = ["#DC2626","#D97706","#16A34A","#2563EB"]
bars = ax2.bar(range(4), q_med.values, color=colors_q, alpha=0.85, edgecolor="#334155", width=0.6)
ax2_twin = ax2.twinx()
ax2_twin.plot(range(4), q_wr.values, color="#F59E0B", lw=2, marker="o", markersize=7, label="Win rate %")
ax2_twin.set_ylabel("Win Rate %", color="#F59E0B")
ax2_twin.tick_params(colors="#F59E0B")
ax2_twin.set_ylim(0, 100)
for bar, med in zip(bars, q_med.values):
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
             f"{med:+.0f}%", ha="center", va="bottom", color="white", fontsize=9)
ax2.set_xticks(range(4))
ax2.set_xticklabels(["Q1\n(lowest vol)", "Q2", "Q3", "Q4\n(highest vol)"], color="#94A3B8")
ax2.set_title("Median Return & Win Rate by Volume Quartile",
              color="white", fontsize=11, fontweight="bold")
ax2.set_ylabel("Median Return %", color="#94A3B8")
ax2.axhline(0, color="#475569", lw=0.8)
ax2_twin.legend(fontsize=9, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 3. Return distribution: flagged vs non-flagged
bins = np.linspace(-60, 200, 70)
ax3.hist(ok["ret_pct"].clip(-60, 200),    bins=bins, color="#2563EB", alpha=0.65,
         label=f"Has pre-halt bar (n={len(ok)})", edgecolor="none")
ax3.hist(flagg["ret_pct"].clip(-60, 200), bins=bins, color="#A855F7", alpha=0.65,
         label=f"No pre-halt bar (n={len(flagg)})", edgecolor="none")
ax3.axvline(ok["ret_pct"].median(),    color="#2563EB", lw=2, linestyle="--",
            label=f"Median {ok['ret_pct'].median():+.0f}%")
ax3.axvline(flagg["ret_pct"].median(), color="#A855F7", lw=2, linestyle="--",
            label=f"Median {flagg['ret_pct'].median():+.0f}%")
ax3.axvline(0, color="#475569", lw=1)
ax3.set_title("Return Distribution: Pre-halt Bar vs Open Halt",
              color="white", fontsize=11, fontweight="bold")
ax3.set_xlabel("Trade Return % (clipped at +200%)", color="#94A3B8")
ax3.set_ylabel("Count", color="#94A3B8")
ax3.legend(fontsize=8.5, framealpha=0.3, facecolor="#1E293B", labelcolor="white")

# 4. Pre-halt dollar volume threshold sweep — win rate & median return
thresholds  = [0, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000]
thr_wr, thr_med, thr_n = [], [], []
for thr in thresholds:
    sub = ok[ok["prehalt_dolvol"] >= thr]
    thr_wr.append((sub["ret_pct"] > 0).mean() * 100 if len(sub) else 0)
    thr_med.append(sub["ret_pct"].median() if len(sub) else 0)
    thr_n.append(len(sub))

ax4_twin = ax4.twinx()
ax4.bar(range(len(thresholds)), thr_med, color="#2563EB", alpha=0.7,
        edgecolor="#334155", width=0.6, label="Median return %")
ax4_twin.plot(range(len(thresholds)), thr_wr, color="#F59E0B", lw=2,
              marker="o", markersize=6, label="Win rate %")
ax4_twin.set_ylabel("Win Rate %", color="#F59E0B")
ax4_twin.tick_params(colors="#F59E0B")
for i, (med, n) in enumerate(zip(thr_med, thr_n)):
    ax4.text(i, med + 0.5, f"{med:+.0f}%\nn={n}", ha="center", va="bottom",
             color="white", fontsize=7.5)
ax4.set_xticks(range(len(thresholds)))
ax4.set_xticklabels([f"${t:,.0f}" for t in thresholds],
                    rotation=35, ha="right", color="#94A3B8", fontsize=8)
ax4.set_title("Effect of Min Pre-Halt Dollar Volume Filter",
              color="white", fontsize=11, fontweight="bold")
ax4.set_ylabel("Median Return %", color="#94A3B8")
ax4.axhline(0, color="#475569", lw=0.8)
ax4_twin.legend(fontsize=9, loc="lower right", framealpha=0.3,
                facecolor="#1E293B", labelcolor="white")

fig.suptitle(
    "Pre-Halt Volume Analysis — Does Last-Minute Volume Predict Returns?",
    color="white", fontsize=13, fontweight="bold", y=1.01,
)
plt.tight_layout()
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"\nSaved: {OUT_PNG}")
print(f"CSV:   {OUT_CSV}")
