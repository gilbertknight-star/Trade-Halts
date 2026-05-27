"""
eod_report.py — End-of-day performance report.

Runs after market close each day (via systemd timer at 4:10 PM ET).
For each trade in today's trades.csv:
  1. Fetches 1-minute IBKR bars for the full holding period → simulated P&L.
  2. Fetches 1-second bars for the first 5 minutes after entry → entry slippage detail.
  3. Compares actual fills to simulated (backtest-style) fills.

Output:
  reports/YYYY-MM-DD_eod.txt  — human-readable daily summary (printed + saved)
  reports/running_log.csv     — machine-readable cumulative log across all days

Usage:
  python eod_report.py
"""

import csv
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock

from config import (
    IBKR_HOST, IBKR_PORT,
    TRADES_CSV,
    EXIT_HOUR_ET, EXIT_MINUTE_ET,
)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

REPORT_DIR  = Path("reports")
RUNNING_LOG = REPORT_DIR / "running_log.csv"

RUNNING_LOG_FIELDS = [
    "date", "symbol",
    "entry_ts", "exit_ts",
    "signal_px",
    "actual_entry_px", "actual_exit_px",
    "sim_entry_px", "sim_exit_px",
    "shares",
    "actual_pnl", "sim_pnl",
    "entry_slip_per_sh", "entry_slip_total",
    "exit_slip_per_sh",  "exit_slip_total",
    "rvol", "up_move",
    "BOD_equity",
]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(REPORT_DIR / "eod_report.log"),
        ],
    )

    today  = date.today().isoformat()
    trades = _read_today_trades(today)

    if not trades:
        log.info("No trades on %s — writing empty report", today)
        _write_report(today, [], [])
        return

    log.info("Processing %d trade(s) for %s", len(trades), today)

    # Connect with clientId=2 so we don't clash with the main bot (clientId=1)
    ib = IB()
    results: list[dict] = []
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=2, timeout=15)
        log.info("Connected to IBKR for EOD report")
        for trade in trades:
            result = _simulate_trade(ib, trade)
            results.append(result)
            ib.sleep(1.5)   # IBKR pacing: avoid hitting rate limits
    except ConnectionRefusedError:
        log.warning(
            "Could not connect to IBKR — writing report without simulation data. "
            "Is IB Gateway running and logged in?"
        )
        results = [_empty_sim() for _ in trades]
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
        results = [_empty_sim() for _ in trades]
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    _write_report(today, trades, results)
    _append_running_log(trades, results)
    log.info("EOD report complete → %s/%s_eod.txt", REPORT_DIR, today)


# ── Simulation ────────────────────────────────────────────────────────────────

def _simulate_trade(ib: IB, trade: dict) -> dict:
    """
    Replay a trade using IBKR historical data.

    sim_entry_px  — open of the first 1-min bar at or after entry_ts.
                    This is the price the backtest would have assumed.
    sim_exit_px   — price at EXIT_HOUR_ET:EXIT_MINUTE_ET from 1-min bars.
    entry slip    — actual_entry_px minus sim_entry_px (positive = paid more).
    exit slip     — actual_exit_px minus sim_exit_px.

    Also fetches 1-second bars for the first 5 minutes after entry so the
    report can show tick-by-tick price action at resumption.
    """
    symbol       = trade["symbol"]
    shares       = int(trade["shares"])
    actual_entry = float(trade["entry_px"])
    actual_exit  = float(trade["exit_px"]) if trade.get("exit_px") else None

    entry_ts_utc = _parse_ts(trade["entry_ts"])
    entry_ts_et  = entry_ts_utc.astimezone(ET)
    today_d      = entry_ts_et.date()
    exit_et      = datetime(today_d.year, today_d.month, today_d.day,
                            EXIT_HOUR_ET, EXIT_MINUTE_ET, 0, tzinfo=ET)

    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        log.warning("qualify failed for %s: %s", symbol, e)
        return _empty_sim()

    # ── 1-minute bars: full session ───────────────────────────────────────────
    try:
        bars_1m = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
    except Exception as e:
        log.warning("1-min bars failed for %s: %s", symbol, e)
        return _empty_sim()

    if not bars_1m:
        log.warning("No 1-min bars for %s", symbol)
        return _empty_sim()

    sim_entry_px  = None
    sim_exit_px   = None
    price_path_1m = []   # (time_et_str, close) for bars from entry → exit

    for bar in bars_1m:
        bar_dt = _bar_et(bar)

        # First bar at/after entry → sim entry (matches backtest open-of-bar fill)
        if sim_entry_px is None and bar_dt >= entry_ts_et - timedelta(seconds=30):
            sim_entry_px = float(bar.open)

        # Track price path from entry onwards
        if sim_entry_px is not None and bar_dt <= exit_et + timedelta(minutes=1):
            price_path_1m.append((bar_dt.strftime("%H:%M"), float(bar.close)))

        # Sim exit: accumulate until we reach the exit bar
        if bar_dt <= exit_et:
            sim_exit_px = float(bar.close)
        elif sim_exit_px is None:
            sim_exit_px = float(bar.open)   # fallback: open of first post-exit bar

    if sim_entry_px is None or sim_exit_px is None:
        log.warning("Could not compute sim prices for %s", symbol)
        return _empty_sim()

    # ── 1-second bars: first 5 minutes after entry ────────────────────────────
    entry_seconds: list[tuple] = []
    try:
        end_str = (entry_ts_et + timedelta(minutes=5)).strftime("%Y%m%d %H:%M:%S ET")
        bars_1s = ib.reqHistoricalData(
            contract,
            endDateTime=end_str,
            durationStr="300 S",
            barSizeSetting="1 secs",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        for bar in bars_1s:
            bar_dt = _bar_et(bar)
            if bar_dt >= entry_ts_et - timedelta(seconds=5):
                entry_seconds.append((
                    bar_dt.strftime("%H:%M:%S"),
                    float(bar.open),
                    float(bar.close),
                    int(bar.volume),
                ))
    except Exception as e:
        log.debug("1-sec bars unavailable for %s (normal if > 7 days old): %s", symbol, e)

    # ── Slippage ──────────────────────────────────────────────────────────────
    sim_pnl          = round((sim_exit_px - sim_entry_px) * shares, 2)
    entry_slip_ps    = round(actual_entry - sim_entry_px, 4)
    entry_slip_total = round(entry_slip_ps * shares, 2)
    exit_slip_ps     = round(actual_exit - sim_exit_px, 4) if actual_exit else None
    exit_slip_total  = round(exit_slip_ps * shares, 2)     if actual_exit else None

    log.info(
        "%-6s  actual_entry=%7.4f  sim_entry=%7.4f  entry_slip=%+.4f/sh ($%+.2f)  "
        "actual_pnl=%s  sim_pnl=$%+.2f",
        symbol,
        actual_entry, sim_entry_px, entry_slip_ps, entry_slip_total,
        trade.get("gross_pnl", "?"), sim_pnl,
    )

    return {
        "sim_entry_px":     round(sim_entry_px, 4),
        "sim_exit_px":      round(sim_exit_px, 4),
        "sim_pnl":          sim_pnl,
        "entry_slip_ps":    entry_slip_ps,
        "entry_slip_total": entry_slip_total,
        "exit_slip_ps":     exit_slip_ps,
        "exit_slip_total":  exit_slip_total,
        "price_path_1m":    price_path_1m,
        "entry_seconds":    entry_seconds,
    }


def _empty_sim() -> dict:
    return {
        "sim_entry_px":     None,
        "sim_exit_px":      None,
        "sim_pnl":          None,
        "entry_slip_ps":    None,
        "entry_slip_total": None,
        "exit_slip_ps":     None,
        "exit_slip_total":  None,
        "price_path_1m":    [],
        "entry_seconds":    [],
    }


# ── Report writing ────────────────────────────────────────────────────────────

def _write_report(today: str, trades: list[dict], results: list[dict]) -> None:
    W   = 62
    SEP = "─" * W

    lines = [
        f"╔{'═' * W}╗",
        f"║  EOD REPORT  {today:<(W - 14)}║".format(),
        f"╚{'═' * W}╝",
        "",
    ]

    # Reformat the header line properly
    header = f"  EOD REPORT  {today}"
    lines[1] = f"║{header:<{W}}║"

    if not trades:
        lines += ["  No trades today.", ""]
    else:
        total_actual_pnl = 0.0
        total_sim_pnl    = 0.0
        total_entry_slip = 0.0
        winners          = 0

        for trade, res in zip(trades, results):
            sym        = trade["symbol"]
            shares     = int(trade["shares"])
            entry_px   = float(trade["entry_px"])
            exit_px    = float(trade.get("exit_px") or 0)
            actual_pnl = float(trade.get("gross_pnl") or 0)
            ret_pct    = float(trade.get("ret_pct") or 0)
            signal_px  = trade.get("signal_px") or "n/a"
            rvol       = trade.get("rvol") or "n/a"
            up_move    = trade.get("up_move") or "n/a"

            total_actual_pnl += actual_pnl
            if actual_pnl > 0:
                winners += 1

            sim_entry = res.get("sim_entry_px")
            sim_exit  = res.get("sim_exit_px")
            sim_pnl   = res.get("sim_pnl")
            e_slip_ps = res.get("entry_slip_ps")
            e_slip    = res.get("entry_slip_total")
            x_slip_ps = res.get("exit_slip_ps")
            x_slip    = res.get("exit_slip_total")

            if sim_pnl    is not None: total_sim_pnl    += sim_pnl
            if e_slip     is not None: total_entry_slip += e_slip

            lines += [
                f"  ▶  {sym}",
                f"     Shares:         {shares}",
                f"     Signal price:   {signal_px}   (pre-halt filter price)",
                f"     Actual fill:    entry={entry_px:.4f}  exit={exit_px:.4f}"
                f"  →  PnL=${actual_pnl:+.2f} ({ret_pct:+.2f}%)",
            ]

            if sim_entry is not None:
                lines += [
                    f"     Backtest sim:   entry={sim_entry:.4f}  exit={sim_exit:.4f}"
                    f"  →  PnL=${sim_pnl:+.2f}",
                    f"     Entry slippage: {e_slip_ps:+.4f}/sh  (${e_slip:+.2f} total)",
                    f"     Exit  slippage: {x_slip_ps:+.4f}/sh  (${x_slip:+.2f} total)"
                    if x_slip_ps is not None else "     Exit  slippage: n/a",
                ]
            else:
                lines.append("     Backtest sim:   unavailable (IBKR not connected)")

            lines.append(f"     RVOL: {rvol}   Up move: {up_move}")

            # 1-second price action at entry
            secs = res.get("entry_seconds", [])
            if secs:
                lines.append(f"     ── First {min(len(secs), 20)} seconds after entry ──")
                for ts_str, o, c, vol in secs[:20]:
                    marker = " ◀ entry" if ts_str == secs[0][0] else ""
                    lines.append(f"       {ts_str}  open={o:.4f}  close={c:.4f}  vol={vol}{marker}")

            # 1-minute price path
            path = res.get("price_path_1m", [])
            if path:
                lines.append(f"     ── 1-min close prices (entry → exit) ──")
                for ts_str, close in path[:20]:
                    lines.append(f"       {ts_str}  ${close:.4f}")
                if len(path) > 20:
                    lines.append(f"       ... ({len(path) - 20} more bars)")

            lines += ["", SEP, ""]

        n = len(trades)
        lines += [
            f"  DAILY SUMMARY ({today})",
            f"  Trades:              {n}",
            f"  Win rate:            {winners}/{n} ({100 * winners // n}%)",
            f"  Actual total PnL:    ${total_actual_pnl:+.2f}",
            f"  Simulated total PnL: ${total_sim_pnl:+.2f}",
            f"  Total entry slip:    ${total_entry_slip:+.2f}",
            f"  BOD equity:          ${float(trades[0].get('BOD_equity') or 0):.2f}",
            "",
        ]

    report_text = "\n".join(lines)
    path = REPORT_DIR / f"{today}_eod.txt"
    path.write_text(report_text)
    print(report_text)


def _append_running_log(trades: list[dict], results: list[dict]) -> None:
    """Append today's trades to the running multi-day CSV log."""
    write_header = not RUNNING_LOG.exists()
    with open(RUNNING_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUNNING_LOG_FIELDS)
        if write_header:
            w.writeheader()
        for trade, res in zip(trades, results):
            w.writerow({
                "date":             trade.get("date"),
                "symbol":           trade.get("symbol"),
                "entry_ts":         trade.get("entry_ts"),
                "exit_ts":          trade.get("exit_ts"),
                "signal_px":        trade.get("signal_px"),
                "actual_entry_px":  trade.get("entry_px"),
                "actual_exit_px":   trade.get("exit_px"),
                "sim_entry_px":     res.get("sim_entry_px"),
                "sim_exit_px":      res.get("sim_exit_px"),
                "shares":           trade.get("shares"),
                "actual_pnl":       trade.get("gross_pnl"),
                "sim_pnl":          res.get("sim_pnl"),
                "entry_slip_per_sh":  res.get("entry_slip_ps"),
                "entry_slip_total":   res.get("entry_slip_total"),
                "exit_slip_per_sh":   res.get("exit_slip_ps"),
                "exit_slip_total":    res.get("exit_slip_total"),
                "rvol":             trade.get("rvol"),
                "up_move":          trade.get("up_move"),
                "BOD_equity":       trade.get("BOD_equity"),
            })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_today_trades(today: str) -> list[dict]:
    path = Path(TRADES_CSV)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("date") == today]


def _parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bar_et(bar) -> datetime:
    bar_dt = bar.date
    if hasattr(bar_dt, "tzinfo") and bar_dt.tzinfo:
        return bar_dt.astimezone(ET)
    return bar_dt.replace(tzinfo=ET)


if __name__ == "__main__":
    main()
