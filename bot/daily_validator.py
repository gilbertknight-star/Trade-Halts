"""
daily_validator.py — Daily regression test for the live trading bot.
=====================================================================

Runs after market close each day (add to eod-report.timer or a separate timer).

For every LULD halt that occurred today (scraped from the NASDAQ RSS feed):
  1. Fetches 1-min IBKR bars — same single call as signal_filter.py
  2. Runs the EXACT same filters as the live bot:
       • Entry cutoff  ≥ 3:49 PM ET → skip
       • Gap-up        (pre_halt_close / session_open − 1) ≥ UP_MIN_MOVE
       • RVOL          surge_rate / baseline_rate ≥ MIN_RVOL
  3. Simulates entry at the OPEN of the first 1-min bar after resumption
     (no slippage model — so slippage vs actual fill is visible in the report)
  4. Simulates exit at the close of the last bar at or before 3:50 PM
  5. Sizes position at POSITION_FRACTION × today's actual account equity

Then compares the validator's signals to trades.csv (what the live bot did):
  • MATCH      — validator and bot agree on every qualifying halt
  • MISSED     — validator flagged it, bot did NOT trade it  (logic gap)
  • UNEXPECTED — bot traded it, validator did NOT flag it    (logic gap)
  • SLIPPAGE   — both traded it; shows actual vs sim entry fill

Output:
  reports/YYYY-MM-DD_validator.txt   human-readable report
  reports/validator_log.csv          cumulative machine-readable log
"""

import csv
import logging
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from ib_insync import IB, Stock

from config import (
    IBKR_HOST, IBKR_PORT,
    HALT_RSS_URL,
    LULD_HALT_CODES,
    UP_MIN_MOVE, MIN_RVOL,
    ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MINUTE,
    EXIT_HOUR_ET, EXIT_MINUTE_ET,
    POSITION_FRACTION, MAX_POS_USD, MIN_POS_USD,
    TRADES_CSV,
    GMAIL_APP_PASSWORD, VALIDATOR_EMAIL_TO, VALIDATOR_EMAIL_FROM,
)

ET_TZ        = ZoneInfo("America/New_York")
NDAQ_NS      = "http://www.nasdaqtrader.com/"
LULD_MINUTES = 5     # LULD pauses are always exactly 5 minutes
RVOL_WINDOW  = 5     # minutes before halt_start to measure surge volume

REPORT_DIR   = Path("reports")
VALIDATOR_LOG = REPORT_DIR / "validator_log.csv"

VALIDATOR_LOG_FIELDS = [
    "date", "symbol", "resume_time",
    "rvol", "up_move",
    "filter_result", "filter_reason",
    "sim_entry_px", "sim_exit_px", "sim_pnl", "sim_shares",
    "live_bot_action",
    "actual_entry_px", "actual_exit_px", "actual_pnl",
    "entry_slip_per_sh", "entry_slip_total",
]

log = logging.getLogger(__name__)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(REPORT_DIR / "daily_validator.log"),
        ],
    )

    # Use ET date — server runs UTC but markets trade in ET.
    # After midnight UTC but before midnight ET (00:00-04:00 UTC),
    # date.today() gives tomorrow's date in UTC but today's ET date is yesterday.
    today     = datetime.now(ET_TZ).date()
    today_str = today.isoformat()
    log.info("Daily validator starting for %s", today_str)

    # ── 1. Scrape today's halts from NASDAQ RSS ───────────────────────────────
    halts = _fetch_today_halts(today)
    log.info("NASDAQ RSS: %d LULD halts with resumptions today", len(halts))

    if not halts:
        log.info("No LULD halts today — writing empty report")
        _write_report(today_str, [], [], 0.0)
        return

    # ── 2. Read today's live bot trades ──────────────────────────────────────
    live_trades = _read_today_trades(today_str)
    live_by_sym = {t["symbol"]: t for t in live_trades}
    log.info("Live bot trades today: %d", len(live_trades))

    # ── 3. Connect to IBKR ───────────────────────────────────────────────────
    ib = IB()
    results: list[dict] = []
    equity = 0.0

    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=3, timeout=15)
        log.info("Connected to IBKR for daily validator")

        equity = _get_account_equity(ib)
        log.info("Account equity: $%.2f", equity)

        for halt in halts:
            log.info("Processing halt: %s  resume=%s",
                     halt["symbol"], halt["resume_dt"].strftime("%H:%M:%S"))
            result = _run_filters(ib, halt, equity)
            result["live_trade"] = live_by_sym.get(halt["symbol"])
            results.append(result)
            ib.sleep(1.5)   # IBKR pacing

    except ConnectionRefusedError:
        log.warning("Could not connect to IBKR — is IB Gateway running?")
        results = [{"symbol": h["symbol"], "resume_dt": h["resume_dt"],
                    "passes": False, "reason": "IBKR unavailable",
                    "rvol": None, "up_move": None,
                    "sim_entry_px": None, "sim_exit_px": None,
                    "sim_pnl": None, "sim_shares": None, "sim_pos_usd": None,
                    "pre_halt_close": None,
                    "live_trade": live_by_sym.get(h["symbol"])}
                   for h in halts]
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    _write_report(today_str, halts, results, equity)
    _append_validator_log(today_str, results)
    log.info("Daily validator complete → %s/%s_validator.txt", REPORT_DIR, today_str)

    # ── Email report ──────────────────────────────────────────────────────────
    if GMAIL_APP_PASSWORD:
        try:
            _send_email(today_str, halts, results, equity)
            log.info("Validator email sent to %s", VALIDATOR_EMAIL_TO)
        except Exception as e:
            log.warning("Email send failed: %s", e)
    else:
        log.info("Email disabled — set GMAIL_APP_PASSWORD env var on server to enable")


# ── NASDAQ halt scraper ───────────────────────────────────────────────────────

def _fetch_today_halts(today: date) -> list[dict]:
    """Fetch all LULD halts that resumed today from the NASDAQ RSS feed."""
    session = requests.Session()
    session.headers["User-Agent"] = "halt-trader/1.0"

    try:
        resp = session.get(HALT_RSS_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error("Could not fetch NASDAQ RSS: %s", e)
        return []

    content = resp.content
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.error("RSS parse error: %s", e)
        return []

    halts = []
    for item in root.findall(".//item"):
        symbol      = _rss_text(item, f"{{{NDAQ_NS}}}IssueSymbol") or _rss_text(item, "title")
        halt_code   = _rss_text(item, f"{{{NDAQ_NS}}}ReasonCode") or ""
        resume_time = _rss_text(item, f"{{{NDAQ_NS}}}ResumptionTradeTime") or ""

        if not symbol or halt_code not in LULD_HALT_CODES:
            continue
        if not resume_time.strip():
            continue  # halt not yet resumed — skip

        symbol = symbol.upper().strip()

        # Parse resumption time — RSS gives "HH:MM:SS" (ET, no date)
        try:
            t = datetime.strptime(resume_time.strip(), "%H:%M:%S")
            resume_dt = datetime(today.year, today.month, today.day,
                                 t.hour, t.minute, t.second, tzinfo=ET_TZ)
        except ValueError:
            log.warning("Unparseable resumption time for %s: %q", symbol, resume_time)
            continue

        halt_start = resume_dt - timedelta(minutes=LULD_MINUTES)

        # Only include market-hours resumptions (9:30–3:49 PM ET)
        market_open = resume_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        cutoff      = resume_dt.replace(hour=ENTRY_CUTOFF_HOUR,
                                        minute=ENTRY_CUTOFF_MINUTE,
                                        second=0, microsecond=0)
        if resume_dt < market_open or resume_dt >= cutoff:
            continue

        halts.append({
            "symbol":     symbol,
            "halt_code":  halt_code,
            "resume_dt":  resume_dt,
            "halt_start": halt_start,
        })

    # Sort chronologically; deduplicate (symbol, resume_dt) — same key as live bot
    seen = set()
    unique = []
    for h in sorted(halts, key=lambda x: x["resume_dt"]):
        key = (h["symbol"], h["resume_dt"].strftime("%H:%M:%S"))
        if key not in seen:
            seen.add(key)
            unique.append(h)

    return unique


# ── Filter engine (mirrors signal_filter.py exactly) ─────────────────────────

def _run_filters(ib: IB, halt: dict, equity: float) -> dict:
    """
    Run the live-bot filter logic with historical halt timestamps.
    Returns a result dict regardless of outcome.
    """
    symbol     = halt["symbol"]
    resume_dt  = halt["resume_dt"]
    halt_start = halt["halt_start"]

    base = {
        "symbol":         symbol,
        "resume_dt":      resume_dt,
        "passes":         False,
        "reason":         "",
        "rvol":           None,
        "up_move":        None,
        "pre_halt_close": None,
        "sim_entry_px":   None,
        "sim_exit_px":    None,
        "sim_pnl":        None,
        "sim_shares":     None,
        "sim_pos_usd":    None,
        "live_trade":     None,
    }

    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        base["reason"] = f"qualify failed: {e}"
        return base

    # ── Fetch 1-min bars (single call — same as live bot) ────────────────────
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
    except Exception as e:
        base["reason"] = f"bars fetch failed: {e}"
        return base

    if not bars:
        base["reason"] = "no 1-min bars returned"
        return base

    # ── Compute all metrics from bars ─────────────────────────────────────────
    market_open = resume_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    surge_start = halt_start - timedelta(minutes=RVOL_WINDOW)
    exit_time   = resume_dt.replace(hour=EXIT_HOUR_ET, minute=EXIT_MINUTE_ET,
                                    second=0, microsecond=0)

    baseline_min = (surge_start - market_open).total_seconds() / 60

    day_open_px    = None
    pre_halt_close = None
    surge_vol      = 0.0
    baseline_vol   = 0.0
    sim_entry_px   = None
    sim_exit_px    = None

    for bar in bars:
        bar_dt = _bar_et(bar)

        if day_open_px is None:
            day_open_px = float(bar.close)   # first bar = day open (matches backtest)

        if bar_dt < halt_start:
            pre_halt_close = float(bar.close)  # last bar before halt

        if surge_start <= bar_dt < halt_start:
            surge_vol += bar.volume

        if market_open <= bar_dt < surge_start:
            baseline_vol += bar.volume

        # Sim entry: open of first bar at or after resumption
        if sim_entry_px is None and bar_dt >= resume_dt:
            sim_entry_px = float(bar.open)

        # Sim exit: close of last bar at or before 3:50 PM
        if bar_dt <= exit_time:
            sim_exit_px = float(bar.close)

    if pre_halt_close is None or pre_halt_close <= 0:
        base["reason"] = "no pre-halt close price in bars"
        return base

    # ── Always compute RVOL (store before any filter returns) ─────────────────
    if baseline_min > 0 and baseline_vol > 0:
        baseline_rate = baseline_vol / baseline_min
        surge_rate    = surge_vol / RVOL_WINDOW if surge_vol > 0 else 0.0
        base["rvol"]  = round(surge_rate / baseline_rate, 2)

    # ── Filter 1: gap-up ─────────────────────────────────────────────────────
    if not day_open_px or day_open_px <= 0:
        base["reason"] = "no day open price"
        return base

    up_move = (pre_halt_close - day_open_px) / day_open_px
    base["up_move"]        = round(up_move, 4)
    base["pre_halt_close"] = round(pre_halt_close, 4)

    if up_move < UP_MIN_MOVE:
        base["reason"] = f"gap-up {up_move:.2%} < min {UP_MIN_MOVE:.0%}"
        return base

    # ── Filter 2: RVOL ───────────────────────────────────────────────────────
    if baseline_min <= 0:
        base["reason"] = "halt too close to open — no RVOL baseline"
        return base

    if baseline_vol <= 0:
        base["reason"] = "zero baseline volume"
        return base

    rvol         = base["rvol"] or 0.0

    if rvol < MIN_RVOL:
        base["reason"] = f"RVOL {rvol:.2f} < min {MIN_RVOL}"
        return base

    # ── Sim position sizing ───────────────────────────────────────────────────
    if equity <= 0:
        base["reason"] = "equity unavailable"
        return base

    pos_usd = min(equity * POSITION_FRACTION, MAX_POS_USD)
    if pos_usd < MIN_POS_USD:
        base["reason"] = f"position ${pos_usd:.2f} < MIN_POS_USD ${MIN_POS_USD}"
        return base

    if not sim_entry_px or sim_entry_px <= 0:
        base["reason"] = "no resumption bar found (stock may have not traded at open)"
        return base

    sim_shares = pos_usd / sim_entry_px
    sim_pnl    = round((sim_exit_px - sim_entry_px) * sim_shares, 2) \
                 if sim_exit_px else None

    base.update({
        "passes":       True,
        "reason":       "ok",
        "sim_entry_px": round(sim_entry_px, 4),
        "sim_exit_px":  round(sim_exit_px, 4) if sim_exit_px else None,
        "sim_pnl":      sim_pnl,
        "sim_shares":   round(sim_shares, 2),
        "sim_pos_usd":  round(pos_usd, 2),
    })
    return base


# ── Report writing ────────────────────────────────────────────────────────────

def _write_report(today_str: str, halts: list, results: list, equity: float) -> None:
    W   = 66
    SEP = "─" * W

    lines = [
        f"╔{'═' * W}╗",
        f"║  DAILY VALIDATOR  {today_str:<{W - 19}}║",
        f"╚{'═' * W}╝",
        "",
        f"  Account equity (live):   ${equity:,.2f}" if equity else "  Account equity: unavailable",
        f"  NASDAQ LULD halts today: {len(halts)}",
        "",
    ]

    if not results:
        lines += ["  No halts to validate today.", ""]
        _save_and_print(today_str, lines)
        return

    qualifying = [r for r in results if r["passes"]]
    lines += [
        f"  Qualifying halts (pass all filters): {len(qualifying)}",
        "",
    ]

    # ── Per-halt detail ───────────────────────────────────────────────────────
    missed      = []
    unexpected  = []
    matched     = []

    for r in results:
        sym        = r["symbol"]
        resume_str = r["resume_dt"].strftime("%H:%M:%S")
        live       = r.get("live_trade")

        status_parts = [f"  ▶  {sym}  (resume {resume_str})"]

        if not r["passes"]:
            status_parts.append(f"     Filter: SKIP — {r['reason']}")
            if live:
                unexpected.append(r)
                status_parts.append(f"     ⚠ UNEXPECTED: live bot TRADED this  "
                                    f"(entry={live.get('entry_px')})")
        else:
            rvol_str   = f"{r['rvol']:.2f}"   if r["rvol"]    is not None else "n/a"
            up_str     = f"{r['up_move']:.2%}" if r["up_move"] is not None else "n/a"
            status_parts.append(
                f"     Filter: PASS   RVOL={rvol_str}  gap-up={up_str}  "
                f"pre_halt=${r.get('pre_halt_close', 'n/a')}"
            )
            status_parts.append(
                f"     Sim:    entry=${r['sim_entry_px']:.4f}  "
                f"exit=${r['sim_exit_px']:.4f}  "
                f"PnL=${r['sim_pnl']:+.2f}  "
                f"({r['sim_shares']:.0f} sh @ ${r['sim_pos_usd']:.0f})"
                if r["sim_entry_px"] and r["sim_exit_px"] and r["sim_pnl"] is not None
                else "     Sim:    entry data unavailable"
            )

            if live:
                matched.append(r)
                actual_entry = float(live.get("entry_px") or 0)
                actual_exit  = float(live.get("exit_px")  or 0)
                actual_pnl   = float(live.get("gross_pnl") or 0)
                slip_ps      = actual_entry - r["sim_entry_px"] if r["sim_entry_px"] else None
                slip_total   = round(slip_ps * r["sim_shares"], 2) if slip_ps and r["sim_shares"] else None

                status_parts.append(
                    f"     Live:   entry=${actual_entry:.4f}  "
                    f"exit=${actual_exit:.4f}  PnL=${actual_pnl:+.2f}"
                )
                if slip_ps is not None:
                    slip_dir = "above" if slip_ps > 0 else "below"
                    status_parts.append(
                        f"     Slip:   {slip_ps:+.4f}/sh  (${slip_total:+.2f} total)  "
                        f"— paid {abs(slip_ps):.4f} {slip_dir} sim open"
                    )
                status_parts.append(f"     ✓ MATCH")
            else:
                missed.append(r)
                status_parts.append(f"     ✗ MISSED — live bot did NOT trade this")

        lines += status_parts + [""]

    # ── Summary ───────────────────────────────────────────────────────────────
    lines += [SEP, "", f"  SIGNAL MATCH SUMMARY  ({today_str})", ""]

    if not qualifying:
        lines.append("  No qualifying halts today.")
    elif not missed and not unexpected:
        lines.append(f"  ✓ PERFECT MATCH — bot traded all {len(qualifying)} qualifying halt(s)")
    else:
        lines.append(f"  Qualifying halts:  {len(qualifying)}")
        lines.append(f"  ✓ Matched:         {len(matched)}")
        if missed:
            lines.append(f"  ✗ MISSED:          {len(missed)}  "
                         f"(validator passed, bot skipped: {[r['symbol'] for r in missed]})")
        if unexpected:
            lines.append(f"  ⚠ UNEXPECTED:      {len(unexpected)}  "
                         f"(bot traded, validator skipped: {[r['symbol'] for r in unexpected]})")

    if matched:
        lines += ["", "  SLIPPAGE SUMMARY", ""]
        total_slip = 0.0
        for r in matched:
            live      = r["live_trade"]
            actual_px = float(live.get("entry_px") or 0)
            sim_px    = r["sim_entry_px"] or 0
            slip_ps   = actual_px - sim_px
            slip_tot  = round(slip_ps * (r["sim_shares"] or 0), 2)
            total_slip += slip_tot
            lines.append(
                f"  {r['symbol']:<8}  sim={sim_px:.4f}  actual={actual_px:.4f}  "
                f"slip={slip_ps:+.4f}/sh  (${slip_tot:+.2f})"
            )
        lines += ["", f"  Total entry slippage today: ${total_slip:+.2f}", ""]

    lines += [""]
    _save_and_print(today_str, lines)


def _save_and_print(today_str: str, lines: list) -> None:
    text = "\n".join(lines)
    path = REPORT_DIR / f"{today_str}_validator.txt"
    path.write_text(text)
    print(text)


# ── Cumulative log ────────────────────────────────────────────────────────────

def _append_validator_log(today_str: str, results: list) -> None:
    # Load existing keys to avoid duplicating rows from re-runs
    existing_keys: set[tuple] = set()
    if VALIDATOR_LOG.exists():
        with open(VALIDATOR_LOG, newline="") as f:
            for row in csv.DictReader(f):
                existing_keys.add((row.get("date"), row.get("symbol"),
                                   row.get("resume_time")))

    write_header = not VALIDATOR_LOG.exists()
    new_rows = []
    for r in results:
        key = (today_str, r["symbol"], r["resume_dt"].strftime("%H:%M:%S"))
        if key in existing_keys:
            continue   # already logged from an earlier run today
        existing_keys.add(key)
        live       = r.get("live_trade") or {}
        actual_px  = float(live.get("entry_px") or 0) or None
        slip_ps    = round(actual_px - r["sim_entry_px"], 4) \
                     if actual_px and r.get("sim_entry_px") else None
        slip_total = round(slip_ps * r["sim_shares"], 2) \
                     if slip_ps and r.get("sim_shares") else None
        new_rows.append({
            "date":              today_str,
            "symbol":            r["symbol"],
            "resume_time":       r["resume_dt"].strftime("%H:%M:%S"),
            "rvol":              r.get("rvol"),
            "up_move":           r.get("up_move"),
            "filter_result":     "PASS" if r["passes"] else "SKIP",
            "filter_reason":     r.get("reason", ""),
            "sim_entry_px":      r.get("sim_entry_px"),
            "sim_exit_px":       r.get("sim_exit_px"),
            "sim_pnl":           r.get("sim_pnl"),
            "sim_shares":        r.get("sim_shares"),
            "live_bot_action":   "TRADED" if live else ("SKIPPED" if r["passes"] else "n/a"),
            "actual_entry_px":   live.get("entry_px"),
            "actual_exit_px":    live.get("exit_px"),
            "actual_pnl":        live.get("gross_pnl"),
            "entry_slip_per_sh": slip_ps,
            "entry_slip_total":  slip_total,
        })

    if not new_rows:
        log.info("No new rows to append (all already logged from earlier run today)")
        return

    with open(VALIDATOR_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VALIDATOR_LOG_FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new_rows)
    log.info("Appended %d rows to validator_log.csv", len(new_rows))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_account_equity(ib: IB) -> float:
    """Fetch live account net liquidation value."""
    from config import IBKR_ACCOUNT
    try:
        # Subscribe, wait for values to populate, then unsubscribe
        ib.accountSummary()   # triggers account data fetch
        ib.sleep(2)
        for v in ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == "USD":
                if not IBKR_ACCOUNT or v.account == IBKR_ACCOUNT:
                    return float(v.value)
    except Exception as e:
        log.warning("Could not fetch account equity: %s", e)
    return 0.0


def _read_today_trades(today_str: str) -> list[dict]:
    path = Path(TRADES_CSV)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("date") == today_str]


def _bar_et(bar) -> datetime:
    bar_dt = bar.date
    if hasattr(bar_dt, "tzinfo") and bar_dt.tzinfo:
        return bar_dt.astimezone(ET_TZ)
    return bar_dt.replace(tzinfo=timezone.utc).astimezone(ET_TZ)


def _rss_text(elem: ET.Element, tag: str) -> str:
    child = elem.find(tag)
    return child.text.strip() if child is not None and child.text else ""


# ── Gmail API email ───────────────────────────────────────────────────────────

def _send_email(today_str: str, halts: list, results: list, equity: float) -> None:
    """Build and send the daily validator email via Gmail SMTP."""
    qualifying = [r for r in results if r["passes"]]
    missed     = [r for r in qualifying if not r.get("live_trade")]
    unexpected = [r for r in results if not r["passes"] and r.get("live_trade")]
    matched    = [r for r in qualifying if r.get("live_trade")]

    subject = _email_subject(today_str, qualifying, missed, unexpected)
    html    = _email_html(today_str, halts, results, qualifying,
                          matched, missed, unexpected, equity)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = VALIDATOR_EMAIL_FROM
    msg["To"]      = VALIDATOR_EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(VALIDATOR_EMAIL_FROM, GMAIL_APP_PASSWORD)
        server.sendmail(VALIDATOR_EMAIL_FROM, VALIDATOR_EMAIL_TO, msg.as_string())


def _email_subject(today_str: str, qualifying: list,
                   missed: list, unexpected: list) -> str:
    if not qualifying:
        return f"Daily Validator {today_str} — No qualifying halts today"
    if missed or unexpected:
        issues = []
        if missed:      issues.append(f"{len(missed)} MISSED")
        if unexpected:  issues.append(f"{len(unexpected)} UNEXPECTED")
        return f"⚠ Daily Validator {today_str} — {', '.join(issues)}"
    return f"✓ Daily Validator {today_str} — Perfect match ({len(qualifying)} halt{'s' if len(qualifying) != 1 else ''})"


def _email_html(today_str, halts, results, qualifying,
                matched, missed, unexpected, equity) -> str:
    """Build HTML email body."""

    # ── colour palette ────────────────────────────────────────────────────────
    GREEN  = "#16a34a"
    RED    = "#dc2626"
    ORANGE = "#ea580c"
    BLUE   = "#2563eb"
    GRAY   = "#64748b"
    BG     = "#f8fafc"
    BORDER = "#e2e8f0"

    def badge(text, color):
        return (f'<span style="background:{color};color:white;padding:2px 8px;'
                f'border-radius:4px;font-size:12px;font-weight:600">{text}</span>')

    def section(title, color=BLUE):
        return (f'<h3 style="color:{color};border-bottom:2px solid {color};'
                f'padding-bottom:4px;margin-top:24px">{title}</h3>')

    def row(label, value, value_color=None):
        vc = f'color:{value_color};font-weight:600' if value_color else 'font-weight:600'
        return (f'<tr><td style="padding:4px 12px 4px 0;color:{GRAY}">{label}</td>'
                f'<td style="padding:4px 0;{vc}">{value}</td></tr>')

    # ── header ────────────────────────────────────────────────────────────────
    if not qualifying:
        status_badge = badge("NO QUALIFYING HALTS", GRAY)
    elif missed or unexpected:
        status_badge = badge("⚠ DISCREPANCY", ORANGE)
    else:
        status_badge = badge("✓ PERFECT MATCH", GREEN)

    equity_str = f"${equity:,.2f}" if equity else "unavailable"

    html = f"""
    <html><body style="font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;
                       background:{BG};padding:24px">
    <div style="background:white;border:1px solid {BORDER};border-radius:8px;padding:24px">

    <h2 style="margin:0 0 4px 0;color:#0f172a">Daily Bot Validator</h2>
    <p style="color:{GRAY};margin:0 0 16px 0">{today_str} &nbsp;|&nbsp; {status_badge}</p>

    <table style="border-collapse:collapse">
      {row("Account equity", equity_str)}
      {row("NASDAQ LULD halts today", str(len(halts)))}
      {row("Qualifying halts (pass filters)", str(len(qualifying)))}
      {row("✓ Matched (bot traded)", str(len(matched)), GREEN if matched else None)}
      {row("✗ Missed (bot skipped)", str(len(missed)), RED if missed else GRAY)}
      {row("⚠ Unexpected (bot traded, validator skipped)", str(len(unexpected)), ORANGE if unexpected else GRAY)}
    </table>
    """

    # ── no qualifying halts ───────────────────────────────────────────────────
    if not qualifying:
        html += f"""
        {section("All Halts Today", GRAY)}
        <p style="color:{GRAY}">No halts passed the gap-up ≥ {UP_MIN_MOVE:.0%} and
        RVOL ≥ {MIN_RVOL} filters today. Bot correctly idle.</p>
        """
        html += _halt_table(results, BORDER, GRAY, GREEN, RED)

    else:
        # ── qualifying halts detail ───────────────────────────────────────────
        if matched:
            html += section("✓ Matched Trades", GREEN)
            html += _matched_table(matched, BORDER, GREEN, GRAY)

        if missed:
            html += section("✗ Missed — Validator Passed, Bot Skipped", RED)
            html += f'<p style="color:{RED}">These halts passed all filters but the live bot did NOT trade them. Investigate bot logs for this time period.</p>'
            html += _qualifying_table(missed, BORDER, RED, GRAY)

        if unexpected:
            html += section("⚠ Unexpected — Bot Traded, Validator Skipped", ORANGE)
            html += f'<p style="color:{ORANGE}">The live bot traded these but they should NOT have passed filters. Check for signal logic drift.</p>'
            html += _unexpected_table(unexpected, BORDER, ORANGE, GRAY)

        # ── slippage summary ──────────────────────────────────────────────────
        if matched:
            html += section("Entry Slippage (Actual vs Sim Open)", BLUE)
            html += _slippage_table(matched, BORDER, BLUE, RED, GREEN, GRAY)

        # ── all halts today ───────────────────────────────────────────────────
        html += section("All Halts Today", GRAY)
        html += _halt_table(results, BORDER, GRAY, GREEN, RED)

    html += f"""
    <hr style="border:none;border-top:1px solid {BORDER};margin:24px 0">
    <p style="color:{GRAY};font-size:12px;margin:0">
      Halt Trader — Daily Validator &nbsp;|&nbsp; {today_str}<br>
      Report saved to <code>reports/{today_str}_validator.txt</code>
    </p>
    </div></body></html>
    """
    return html


def _halt_table(results, BORDER, GRAY, GREEN, RED) -> str:
    rows = ""
    for r in results:
        sym    = r["symbol"]
        resume = r["resume_dt"].strftime("%H:%M:%S")
        rvol   = f"{r['rvol']:.2f}"   if r.get("rvol")    is not None else "—"
        up     = f"{r['up_move']:.2%}" if r.get("up_move") is not None else "—"
        color  = GREEN if r["passes"] else GRAY
        status = "PASS" if r["passes"] else "SKIP"
        reason = "" if r["passes"] else r.get("reason", "")
        rows += (f'<tr style="border-bottom:1px solid {BORDER}">'
                 f'<td style="padding:6px 8px;font-weight:600">{sym}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY}">{resume}</td>'
                 f'<td style="padding:6px 8px">{rvol}</td>'
                 f'<td style="padding:6px 8px">{up}</td>'
                 f'<td style="padding:6px 8px;color:{color};font-weight:600">{status}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY};font-size:12px">{reason}</td>'
                 f'</tr>')
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead style="background:#f1f5f9">
        <tr>
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Resume</th>
          <th style="padding:8px;text-align:left">RVOL</th>
          <th style="padding:8px;text-align:left">Gap-up</th>
          <th style="padding:8px;text-align:left">Filter</th>
          <th style="padding:8px;text-align:left">Reason</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _matched_table(matched, BORDER, GREEN, GRAY) -> str:
    rows = ""
    for r in matched:
        live      = r["live_trade"]
        sym       = r["symbol"]
        resume    = r["resume_dt"].strftime("%H:%M:%S")
        sim_e     = f"${r['sim_entry_px']:.4f}"   if r.get("sim_entry_px") else "—"
        sim_x     = f"${r['sim_exit_px']:.4f}"    if r.get("sim_exit_px")  else "—"
        sim_pnl   = f"${r['sim_pnl']:+.2f}"       if r.get("sim_pnl") is not None else "—"
        act_e     = f"${float(live.get('entry_px') or 0):.4f}"
        act_x     = f"${float(live.get('exit_px') or 0):.4f}"
        act_pnl   = f"${float(live.get('gross_pnl') or 0):+.2f}"
        pnl_color = GREEN if (r.get("sim_pnl") or 0) >= 0 else "#dc2626"
        rows += (f'<tr style="border-bottom:1px solid {BORDER}">'
                 f'<td style="padding:6px 8px;font-weight:600">{sym}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY}">{resume}</td>'
                 f'<td style="padding:6px 8px">{sim_e}</td>'
                 f'<td style="padding:6px 8px">{sim_x}</td>'
                 f'<td style="padding:6px 8px;color:{pnl_color};font-weight:600">{sim_pnl}</td>'
                 f'<td style="padding:6px 8px">{act_e}</td>'
                 f'<td style="padding:6px 8px">{act_x}</td>'
                 f'<td style="padding:6px 8px;font-weight:600">{act_pnl}</td>'
                 f'</tr>')
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead style="background:#f0fdf4">
        <tr>
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Resume</th>
          <th colspan="3" style="padding:8px;text-align:center;border-right:2px solid #bbf7d0">Sim (no slippage)</th>
          <th colspan="3" style="padding:8px;text-align:center">Live Bot (actual)</th>
        </tr>
        <tr style="font-size:11px;color:{GRAY}">
          <th></th><th></th>
          <th style="padding:4px 8px;text-align:left">Entry</th>
          <th style="padding:4px 8px;text-align:left">Exit</th>
          <th style="padding:4px 8px;text-align:left;border-right:2px solid #bbf7d0">P&L</th>
          <th style="padding:4px 8px;text-align:left">Entry</th>
          <th style="padding:4px 8px;text-align:left">Exit</th>
          <th style="padding:4px 8px;text-align:left">P&L</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _qualifying_table(items, BORDER, color, GRAY) -> str:
    rows = ""
    for r in items:
        up_str    = f"{r['up_move']:.2%}"    if r.get("up_move")    is not None else "—"
        entry_str = f"${r['sim_entry_px']:.4f}" if r.get("sim_entry_px") else "—"
        pnl_str   = f"${r['sim_pnl']:+.2f}"  if r.get("sim_pnl")   is not None else "—"
        rows += (f'<tr style="border-bottom:1px solid {BORDER}">'
                 f'<td style="padding:6px 8px;font-weight:600">{r["symbol"]}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY}">{r["resume_dt"].strftime("%H:%M:%S")}</td>'
                 f'<td style="padding:6px 8px">{r.get("rvol", "—")}</td>'
                 f'<td style="padding:6px 8px">{up_str}</td>'
                 f'<td style="padding:6px 8px">{entry_str}</td>'
                 f'<td style="padding:6px 8px;color:{color};font-weight:600">{pnl_str}</td>'
                 f'</tr>')
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead style="background:#fef2f2">
        <tr>
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Resume</th>
          <th style="padding:8px;text-align:left">RVOL</th>
          <th style="padding:8px;text-align:left">Gap-up</th>
          <th style="padding:8px;text-align:left">Sim Entry</th>
          <th style="padding:8px;text-align:left">Sim P&L</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _unexpected_table(items, BORDER, ORANGE, GRAY) -> str:
    rows = ""
    for r in items:
        live = r.get("live_trade") or {}
        rows += (f'<tr style="border-bottom:1px solid {BORDER}">'
                 f'<td style="padding:6px 8px;font-weight:600">{r["symbol"]}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY}">{r["resume_dt"].strftime("%H:%M:%S")}</td>'
                 f'<td style="padding:6px 8px;color:{ORANGE}">{r.get("reason","")}</td>'
                 f'<td style="padding:6px 8px">{live.get("entry_px","—")}</td>'
                 f'<td style="padding:6px 8px">{live.get("gross_pnl","—")}</td>'
                 f'</tr>')
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead style="background:#fff7ed">
        <tr>
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Resume</th>
          <th style="padding:8px;text-align:left">Why Validator Skipped</th>
          <th style="padding:8px;text-align:left">Actual Entry</th>
          <th style="padding:8px;text-align:left">Actual P&L</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _slippage_table(matched, BORDER, BLUE, RED, GREEN, GRAY) -> str:
    rows = ""
    total_slip = 0.0
    for r in matched:
        live      = r["live_trade"]
        actual_px = float(live.get("entry_px") or 0)
        sim_px    = r.get("sim_entry_px") or 0
        shares    = r.get("sim_shares") or 0
        slip_ps   = actual_px - sim_px
        slip_tot  = round(slip_ps * shares, 2)
        total_slip += slip_tot
        color = RED if slip_ps > 0 else GREEN
        rows += (f'<tr style="border-bottom:1px solid {BORDER}">'
                 f'<td style="padding:6px 8px;font-weight:600">{r["symbol"]}</td>'
                 f'<td style="padding:6px 8px">${sim_px:.4f}</td>'
                 f'<td style="padding:6px 8px">${actual_px:.4f}</td>'
                 f'<td style="padding:6px 8px;color:{color};font-weight:600">{slip_ps:+.4f}/sh</td>'
                 f'<td style="padding:6px 8px;color:{color};font-weight:600">${slip_tot:+.2f}</td>'
                 f'<td style="padding:6px 8px;color:{GRAY}">{shares:.0f} sh</td>'
                 f'</tr>')
    slip_color = RED if total_slip > 0 else GREEN
    rows += (f'<tr style="background:#f1f5f9;font-weight:600">'
             f'<td style="padding:6px 8px" colspan="4">Total entry slippage today</td>'
             f'<td style="padding:6px 8px;color:{slip_color}">${total_slip:+.2f}</td>'
             f'<td></td></tr>')
    return f"""
    <p style="color:{GRAY};font-size:13px">
      Slippage = actual fill − sim open price (no slippage model).
      Positive = you paid more than the resumption open. Negative = you got a better price.
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead style="background:#eff6ff">
        <tr>
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Sim Open</th>
          <th style="padding:8px;text-align:left">Actual Fill</th>
          <th style="padding:8px;text-align:left">Slip/Share</th>
          <th style="padding:8px;text-align:left">Slip Total</th>
          <th style="padding:8px;text-align:left">Shares</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


if __name__ == "__main__":
    main()
