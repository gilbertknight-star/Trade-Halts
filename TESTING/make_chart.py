"""
Professional equity curve chart — run from Trade Halts root directory.
    python TESTING/make_chart.py
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

NY_TZ        = ZoneInfo("America/New_York")
DATA_ROOT    = Path("data_massive/Halt_model")
EVENTS_PATH  = Path("TESTING/halt_events.parquet")
OUT_PNG      = Path("TESTING/halt_equity_curve_professional.png")
START_EQUITY = 30_000.0
PER_TRADE    = 100.0
SLIP_BPS     = 10
MIN_PRICE    = 1.00

# ── colours ───────────────────────────────────────────────────────────────────
C_EQUITY  = "#2563EB"   # rich blue
C_FILL    = "#DBEAFE"   # pale blue fill
C_DD      = "#DC2626"   # red
C_DD_FILL = "#FEE2E2"   # pale red fill
C_DIST    = "#1D4ED8"   # distribution bars
C_ZERO    = "#94A3B8"   # zero lines
C_GRID    = "#E2E8F0"   # grid

# ── bar loader ────────────────────────────────────────────────────────────────
_cache: dict = {}

def load_bars(bars_file):
    key = str(bars_file or "").strip()
    if key in _cache:
        return _cache[key]
    if not key:
        _cache[key] = None; return None
    norm = key.replace("\\", "/")
    candidates = [Path(key)]
    for prefix in ["data_massive/Halt_model/", "data_massive\\Halt_model\\"]:
        pnorm = prefix.replace("\\", "/")
        if norm.startswith(pnorm):
            candidates.append(DATA_ROOT / norm[len(pnorm):])
        elif norm.startswith("data_massive/"):
            candidates.append(DATA_ROOT / norm[len("data_massive/"):])
    for c in candidates:
        if not c.exists(): continue
        try:
            b = pd.read_parquet(c)
            if "timestamp_utc" in b.columns:
                b = b.set_index("timestamp_utc")
            b.index = pd.to_datetime(b.index, utc=True)
            b = b.sort_index()
            _cache[key] = b; return b
        except Exception:
            pass
    _cache[key] = None; return None

# ── backtest ──────────────────────────────────────────────────────────────────
ev = pd.read_parquet(EVENTS_PATH)
ev["anchor_ts_utc"] = pd.to_datetime(ev["anchor_ts_utc"], utc=True)
slip_entry = 1 + SLIP_BPS / 10_000
slip_exit  = 1 - SLIP_BPS / 10_000

trades = []
for _, row in ev.iterrows():
    ts = row["anchor_ts_utc"]
    b  = load_bars(row.get("bars_file"))
    if b is None or b.empty or "close" not in b.columns: continue
    p0 = b.index.searchsorted(ts, side="left")
    if p0 >= len(b): continue
    entry_px = float(pd.to_numeric(b.iloc[p0]["close"], errors="coerce"))
    if not np.isfinite(entry_px) or entry_px <= 0: continue
    entry_exec = entry_px * slip_entry
    if entry_exec < MIN_PRICE: continue
    d = pd.Timestamp(str(row["event_date"]))
    exit_utc = pd.Timestamp(year=d.year, month=d.month, day=d.day,
                            hour=16, minute=0, tz=NY_TZ).tz_convert("UTC")
    pend = b.index.searchsorted(exit_utc, side="right") - 1
    if pend < p0: continue
    exit_px = float(pd.to_numeric(b.iloc[pend]["close"], errors="coerce"))
    if not np.isfinite(exit_px) or exit_px <= 0: continue
    exit_exec = exit_px * slip_exit
    ret = exit_exec / entry_exec - 1.0
    trades.append({"exit_ts": b.index[pend], "ret": ret, "pnl": PER_TRADE * ret})

tr = pd.DataFrame(trades).sort_values("exit_ts").reset_index(drop=True)
tr["exit_ts"] = pd.to_datetime(tr["exit_ts"], utc=True).dt.tz_localize(None)

# ── derived series ────────────────────────────────────────────────────────────
equity   = START_EQUITY + tr["pnl"].cumsum().values
xs       = tr["exit_ts"].values
roll_max = np.maximum.accumulate(equity)
drawdown = equity - roll_max
dd_pct   = drawdown / roll_max * 100

wr       = (tr["ret"] > 0).mean()
avg_r    = tr["ret"].mean() * 100
med_r    = tr["ret"].median() * 100
total_r  = (equity[-1] / START_EQUITY - 1) * 100
max_dd   = dd_pct.min()
n        = len(tr)

rets_pct = (tr["ret"] * 100).clip(-40, 100).values

# ── figure layout ─────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family":      "sans-serif",
    "font.size":        11,
    "axes.spines.top":  False,
    "axes.spines.right":False,
    "axes.grid":        True,
    "grid.color":       C_GRID,
    "grid.linewidth":   0.8,
    "axes.labelcolor":  "#334155",
    "xtick.color":      "#64748B",
    "ytick.color":      "#64748B",
    "text.color":       "#1E293B",
})

fig = plt.figure(figsize=(16, 10), facecolor="white")
gs  = GridSpec(2, 2, figure=fig,
               height_ratios=[1.8, 1],
               hspace=0.42, wspace=0.30,
               left=0.07, right=0.97, top=0.88, bottom=0.08)

ax_eq = fig.add_subplot(gs[0, :])   # full-width top
ax_dd = fig.add_subplot(gs[1, 0])
ax_di = fig.add_subplot(gs[1, 1])

# ── panel 1: equity curve ─────────────────────────────────────────────────────
ax_eq.fill_between(xs, START_EQUITY, equity, color=C_FILL, alpha=0.7)
ax_eq.plot(xs, equity, color=C_EQUITY, lw=2, zorder=3)
ax_eq.axhline(START_EQUITY, color=C_ZERO, lw=1, ls="--", alpha=0.6)

ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(
    lambda v, _: f"${v/1e3:.0f}k" if v < 1e6 else f"${v/1e6:.1f}M"))
ax_eq.xaxis.set_major_locator(mdates.YearLocator())
ax_eq.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax_eq.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 7, 10]))
ax_eq.set_ylabel("Portfolio Value", fontsize=12, fontweight="bold")
ax_eq.set_xlim(xs[0], xs[-1])
ax_eq.set_ylim(bottom=min(equity) * 0.92)

# stat box
stats_text = (
    f"Total Return  {total_r:+.0f}%     "
    f"Win Rate  {wr:.0%}     "
    f"Avg Trade  {avg_r:+.1f}%     "
    f"Median Trade  {med_r:+.1f}%     "
    f"Max Drawdown  {max_dd:.1f}%     "
    f"Trades  {n:,}"
)
ax_eq.text(0.5, 1.06, stats_text,
           transform=ax_eq.transAxes, ha="center", va="bottom",
           fontsize=10.5, color="#475569",
           bbox=dict(boxstyle="round,pad=0.3", facecolor="#F8FAFC",
                     edgecolor="#CBD5E1", linewidth=0.8))

ax_eq.set_title("LULD Halt Resumption — Equity Curve  ($100/trade · 10 bps slippage · 4 PM exit)",
                fontsize=13, fontweight="bold", pad=28, color="#1E293B")

# ── panel 2: drawdown ─────────────────────────────────────────────────────────
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

# ── panel 3: return distribution ──────────────────────────────────────────────
ax_di.hist(rets_pct, bins=70, color=C_DIST, alpha=0.25,
           edgecolor="none", density=True)

# KDE overlay
kde = gaussian_kde(rets_pct, bw_method=0.35)
kx  = np.linspace(rets_pct.min() - 2, rets_pct.max() + 2, 400)
ax_di.plot(kx, kde(kx), color=C_EQUITY, lw=2.5)
ax_di.axvline(0,    color=C_ZERO, lw=1.2, ls="--", alpha=0.7)
ax_di.axvline(med_r, color="#10B981", lw=1.5, ls="--", alpha=0.9,
              label=f"Median  {med_r:+.1f}%")
ax_di.axvline(avg_r, color="#F59E0B", lw=1.5, ls="--", alpha=0.9,
              label=f"Mean  {avg_r:+.1f}%")

ax_di.set_xlabel("Return per Trade (%)", fontsize=11, fontweight="bold")
ax_di.set_ylabel("Density", fontsize=11, fontweight="bold")
ax_di.set_title("Trade Return Distribution", fontsize=12, fontweight="bold", color="#1E293B")
ax_di.legend(fontsize=9.5, framealpha=0.8, edgecolor="#CBD5E1")
ax_di.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))

# ── save ──────────────────────────────────────────────────────────────────────
plt.savefig(OUT_PNG, dpi=180, facecolor="white", bbox_inches="tight")
print(f"Saved: {OUT_PNG}")
