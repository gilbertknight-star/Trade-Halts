"""
crosscheck_may8.py — run the live/validator filter on May 8 2026 halts via IBKR
and compare verdict-by-verdict to the Massive/backtest verdicts.

This is the definitive test of whether the live pipeline generates the SAME
signals as the backtest on the same day. Run on the server (IBKR connected):

    python bot/crosscheck_may8.py

Reads ../may8_massive_verdicts.csv (committed alongside) and prints a per-halt
PASS/SKIP comparison plus an agreement summary.
"""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ib_insync import IB, Stock
from config import IBKR_HOST, IBKR_PORT, UP_MIN_MOVE, MIN_RVOL

RVOL_WINDOW = 5
PAUSE = 5
CSV = Path(__file__).parent.parent / "may8_massive_verdicts.csv"


def bar_utc(b):
    d = b.date
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, tz=timezone.utc)
    if getattr(d, "tzinfo", None):
        return d.astimezone(timezone.utc)
    return d.replace(tzinfo=timezone.utc)


def ibkr_verdict(ib, symbol, resume_utc):
    halt_start  = resume_utc - timedelta(minutes=PAUSE)
    surge_start = halt_start - timedelta(minutes=RVOL_WINDOW)
    open_utc    = resume_utc.replace(hour=13, minute=30, second=0, microsecond=0)  # 9:30 ET = 13:30 UTC (EDT)
    bmin = (surge_start - open_utc).total_seconds() / 60

    c = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(c)
    except Exception as e:
        return ("ERR", None, None, f"qualify {e}")

    end_day = resume_utc.replace(hour=20, minute=0, second=0).strftime("%Y%m%d %H:%M:%S UTC")
    try:
        m = ib.reqHistoricalData(c, endDateTime=end_day, durationStr="1 D",
                                 barSizeSetting="1 min", whatToShow="TRADES",
                                 useRTH=True, formatDate=2)
    except Exception as e:
        return ("ERR", None, None, f"1min {e}")
    if not m:
        return ("SKIP", None, None, "no bars")

    day_open = None; pre_halt = None
    for b in m:
        t = bar_utc(b)
        if day_open is None:
            day_open = float(b.close)
        if t < halt_start:
            pre_halt = float(b.close)
    if not pre_halt or pre_halt <= 0 or not day_open or day_open <= 0:
        return ("SKIP", None, None, "no px")

    gap = (pre_halt - day_open) / day_open
    if gap < UP_MIN_MOVE:
        return ("SKIP", round(gap, 4), None, "gap %.2f%%" % (gap * 100))

    # 1-second RVOL (surge exact + baseline whole-minutes + partial boundary minute)
    end_1s = halt_start.strftime("%Y%m%d %H:%M:%S UTC")
    try:
        s = ib.reqHistoricalData(c, endDateTime=end_1s, durationStr="600 S",
                                 barSizeSetting="1 secs", whatToShow="TRADES",
                                 useRTH=False, formatDate=2)
    except Exception:
        s = []
    surge_floor = surge_start.replace(second=0, microsecond=0)
    surge_vol = base_partial = 0.0
    for b in s:
        t = bar_utc(b)
        if surge_start <= t < halt_start: surge_vol += b.volume
        elif surge_floor <= t < surge_start: base_partial += b.volume
    base_full = 0.0
    for b in m:
        t = bar_utc(b)
        if open_utc <= t < surge_floor: base_full += b.volume
    base_vol = base_full + base_partial
    if base_vol <= 0 or surge_vol <= 0 or bmin <= 0:
        return ("SKIP", round(gap, 4), 0.0, "zero vol")
    rvol = (surge_vol / RVOL_WINDOW) / (base_vol / bmin)
    if rvol < MIN_RVOL:
        return ("SKIP", round(gap, 4), round(rvol, 2), "rvol %.2f" % rvol)
    return ("PASS", round(gap, 4), round(rvol, 2), "ok")


def main():
    halts = list(csv.DictReader(open(CSV)))
    ib = IB(); ib.connect(IBKR_HOST, IBKR_PORT, clientId=11, timeout=15)
    print(f"Cross-checking {len(halts)} May 8 halts (Massive vs IBKR)\n")
    print(f"{'SYM':<7}{'resume':<9}{'M':<5}{'I':<5}{'Mrvol':>7}{'Irvol':>7}  match")
    print("-" * 52)

    agree = mass_pass = ibkr_pass = both_pass = 0
    for h in halts:
        sym = h["symbol"]
        rt = datetime.strptime(h["resume_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        mv = h["verdict_massive"]
        verdict, gap, rvol, reason = ibkr_verdict(ib, sym, rt)
        ib.sleep(1.2)
        same = "OK" if verdict == mv else "DIFFER"
        if verdict == mv: agree += 1
        if mv == "PASS": mass_pass += 1
        if verdict == "PASS": ibkr_pass += 1
        if mv == "PASS" and verdict == "PASS": both_pass += 1
        mrv = h["rvol_massive"] or "-"
        irv = f"{rvol:.2f}" if rvol is not None else "-"
        print(f"{sym:<7}{rt.strftime('%H:%M:%S'):<9}{mv:<5}{verdict:<5}{str(mrv):>7}{irv:>7}  {same}")

    print("-" * 52)
    print(f"Halts: {len(halts)}   Agreement: {agree}/{len(halts)} ({100*agree//len(halts)}%)")
    print(f"Massive PASS: {mass_pass}   IBKR PASS: {ibkr_pass}   Both PASS: {both_pass}")
    ib.disconnect()


if __name__ == "__main__":
    main()
