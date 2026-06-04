"""
replay_backtest.py — Replay a NASDAQ halt CSV through the validated filter using
IBKR data (proven equivalent to Massive). Builds a current-regime backtest with
the exact live-bot logic: gap-up + 1-second RVOL + daily capital constraint +
compounding equity.

Resumable: checkpoints per-day so a long run survives interruption.

Usage (run on the server, in the background):
    python bot/replay_backtest.py --csv halts.csv --equity 902.79 \
        --start 2026-03-03 --end 2026-06-03

Outputs (under reports/):
    replay_trades.csv     one row per FUNDED trade
    replay_daily.csv      one row per trading day (signals, funded, pnl, equity)
    replay_done.txt       completed dates (for resume)
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock
from config import (IBKR_HOST, IBKR_PORT, UP_MIN_MOVE, MIN_RVOL,
                    ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MINUTE,
                    EXIT_HOUR_ET, EXIT_MINUTE_ET,
                    POSITION_FRACTION, MAX_POS_USD, MIN_POS_USD, CASH_RESERVE)

ET = ZoneInfo("America/New_York")
RVOL_WINDOW = 5
PAUSE = 5
REPORT = Path(__file__).parent.parent / "reports"
REPORT.mkdir(exist_ok=True)
TRADES = REPORT / "replay_trades.csv"
DAILY  = REPORT / "replay_daily.csv"
DONE   = REPORT / "replay_done.txt"

_barcache = {}   # (symbol, date) -> list of 1-min bars


def bar_utc(b):
    d = b.date
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, tz=timezone.utc)
    if getattr(d, "tzinfo", None):
        return d.astimezone(timezone.utc)
    return d.replace(tzinfo=timezone.utc)


def get_1min(ib, symbol, date_et):
    key = (symbol, date_et.isoformat())
    if key in _barcache:
        return _barcache[key]
    c = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(c)
    except Exception:
        _barcache[key] = (None, None); return (None, None)
    end = datetime(date_et.year, date_et.month, date_et.day, 20, 0, tzinfo=timezone.utc)
    try:
        bars = ib.reqHistoricalData(c, endDateTime=end.strftime("%Y%m%d %H:%M:%S UTC"),
            durationStr="1 D", barSizeSetting="1 min", whatToShow="TRADES",
            useRTH=True, formatDate=2)
    except Exception:
        bars = None
    _barcache[key] = (c, bars)
    return (c, bars)


def rvol_1sec(ib, contract, halt_start, surge_start, open_utc, bars_1m):
    end = halt_start.astimezone(timezone.utc).strftime("%Y%m%d %H:%M:%S UTC")
    try:
        s = ib.reqHistoricalData(contract, endDateTime=end, durationStr="600 S",
            barSizeSetting="1 secs", whatToShow="TRADES", useRTH=False, formatDate=2)
    except Exception:
        s = []
    if not s:
        return None
    floor = surge_start.replace(second=0, microsecond=0)
    surge = partial = 0.0
    for b in s:
        t = bar_utc(b)
        if surge_start <= t < halt_start: surge += b.volume
        elif floor <= t < surge_start: partial += b.volume
    full = 0.0
    for b in bars_1m:
        t = bar_utc(b)
        if open_utc <= t < floor: full += b.volume
    return (full + partial, surge)


def evaluate(ib, symbol, resume_dt):
    """Return dict with verdict + sim prices for one halt, or None if no data."""
    halt_start  = resume_dt - timedelta(minutes=PAUSE)
    surge_start = halt_start - timedelta(minutes=RVOL_WINDOW)
    open_et     = resume_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    exit_et     = resume_dt.replace(hour=EXIT_HOUR_ET, minute=EXIT_MINUTE_ET, second=0, microsecond=0)
    open_utc    = open_et.astimezone(timezone.utc)
    halt_utc    = halt_start.astimezone(timezone.utc)
    surge_utc   = surge_start.astimezone(timezone.utc)
    resume_utc  = resume_dt.astimezone(timezone.utc)
    exit_utc    = exit_et.astimezone(timezone.utc)

    contract, bars = get_1min(ib, symbol, resume_dt.date())
    if not bars:
        return {"verdict": "SKIP", "reason": "no bars"}

    day_open = pre_halt = sim_entry = sim_exit = None
    for b in bars:
        t = bar_utc(b)
        if day_open is None: day_open = float(b.close)
        if t < halt_utc: pre_halt = float(b.close)
        if sim_entry is None and t >= resume_utc: sim_entry = float(b.open)
        if t <= exit_utc: sim_exit = float(b.close)
    if not pre_halt or pre_halt <= 0 or not day_open or day_open <= 0:
        return {"verdict": "SKIP", "reason": "no px"}

    gap = (pre_halt - day_open) / day_open
    if gap < UP_MIN_MOVE:
        return {"verdict": "SKIP", "reason": "gap"}

    bmin = (surge_utc - open_utc).total_seconds() / 60
    if bmin <= 0:
        return {"verdict": "SKIP", "reason": "too early"}
    pr = rvol_1sec(ib, contract, halt_utc, surge_utc, open_utc, bars)
    if pr is None:
        return {"verdict": "SKIP", "reason": "no 1sec"}
    base_vol, surge_vol = pr
    if base_vol <= 0 or surge_vol <= 0:
        return {"verdict": "SKIP", "reason": "zero vol"}
    rvol = (surge_vol / RVOL_WINDOW) / (base_vol / bmin)
    if rvol < MIN_RVOL:
        return {"verdict": "SKIP", "reason": "rvol", "rvol": round(rvol, 2)}
    if not sim_entry or sim_entry <= 0 or not sim_exit or sim_exit <= 0:
        return {"verdict": "SKIP", "reason": "no entry/exit"}
    return {"verdict": "PASS", "rvol": round(rvol, 2), "gap": round(gap, 4),
            "sim_entry": sim_entry, "sim_exit": sim_exit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--equity", type=float, default=902.79)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    # ── Load + filter halts ───────────────────────────────────────────────────
    rows = list(csv.DictReader(open(args.csv)))
    halts = []
    for r in rows:
        if "LULD" not in (r.get("Reason") or "").upper():
            continue
        rt = (r.get("NYSE Resume Time") or "").strip()
        rd = (r.get("Resume Date") or "").strip()
        hd = (r.get("Halt Date") or "").strip()
        if not rt or not rd:
            continue
        try:
            d = datetime.strptime(rd, "%Y-%m-%d").date()
            t = datetime.strptime(rt, "%H:%M:%S").time()
        except ValueError:
            continue
        resume_dt = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=ET)
        # market-hours entry window
        mo = resume_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        co = resume_dt.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MINUTE, second=0, microsecond=0)
        if resume_dt < mo or resume_dt >= co:
            continue
        if args.start and hd < args.start: continue
        if args.end and hd > args.end: continue
        halts.append({"symbol": r["Symbol"].strip().upper(), "resume_dt": resume_dt,
                      "date": resume_dt.date().isoformat()})

    # dedup (symbol, resume_dt)
    seen = set(); uniq = []
    for h in sorted(halts, key=lambda x: x["resume_dt"]):
        k = (h["symbol"], h["resume_dt"].isoformat())
        if k not in seen: seen.add(k); uniq.append(h)
    halts = uniq
    by_day = {}
    for h in halts:
        by_day.setdefault(h["date"], []).append(h)
    days = sorted(by_day)
    print(f"Loaded {len(halts)} LULD halts across {len(days)} days "
          f"({days[0]} to {days[-1]})")

    # ── Resume support ────────────────────────────────────────────────────────
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    equity = args.equity
    if done:  # recompute equity from existing trades
        if TRADES.exists():
            for t in csv.DictReader(open(TRADES)):
                equity += float(t["pnl"])
        print(f"Resuming — {len(done)} days done, equity=${equity:,.2f}")

    ib = IB(); ib.connect(IBKR_HOST, IBKR_PORT, clientId=12, timeout=15)

    new_trades = not TRADES.exists()
    tf = open(TRADES, "a", newline=""); tw = csv.writer(tf)
    if new_trades:
        tw.writerow(["date","symbol","resume","rvol","gap","entry","exit","shares","pos_usd","pnl","equity_after"])
    new_daily = not DAILY.exists()
    df = open(DAILY, "a", newline=""); dw = csv.writer(df)
    if new_daily:
        dw.writerow(["date","halts","signals","funded","wins","day_pnl","equity_eod"])

    for d in days:
        if d in done:
            continue
        day_halts = sorted(by_day[d], key=lambda x: x["resume_dt"])
        available = equity * (1.0 - CASH_RESERVE)
        signals = funded = wins = 0
        day_pnl = 0.0
        for h in day_halts:
            res = evaluate(ib, h["symbol"], h["resume_dt"])
            ib.sleep(0.8)
            if not res or res["verdict"] != "PASS":
                continue
            signals += 1
            pos = min(equity * POSITION_FRACTION, MAX_POS_USD, available)
            if pos < MIN_POS_USD:
                continue   # qualified but no capital
            shares = pos / res["sim_entry"]
            pnl = (res["sim_exit"] - res["sim_entry"]) * shares
            available -= pos
            funded += 1
            if pnl > 0: wins += 1
            day_pnl += pnl
            tw.writerow([d, h["symbol"], h["resume_dt"].strftime("%H:%M:%S"),
                         res["rvol"], res["gap"], round(res["sim_entry"],4),
                         round(res["sim_exit"],4), round(shares,2), round(pos,2),
                         round(pnl,2), round(equity+day_pnl,2)])
        equity += day_pnl
        dw.writerow([d, len(day_halts), signals, funded, wins,
                     round(day_pnl,2), round(equity,2)])
        tf.flush(); df.flush()
        with open(DONE, "a") as f: f.write(d + "\n")
        print(f"{d}: {len(day_halts):>3} halts  {signals:>2} sig  {funded:>2} funded  "
              f"{wins}W  pnl=${day_pnl:+,.2f}  equity=${equity:,.2f}")

    ib.disconnect()
    print(f"\nDONE. Final equity: ${equity:,.2f}  "
          f"(start ${args.equity:,.2f}, {(equity/args.equity-1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
