"""
Main entry point for the LULD halt resumption live trader.

Flow:
  1. Connect to IBKR Gateway
  2. Snapshot BOD equity at market open
  3. Start halt monitor in background thread
  4. On each halt resumption: filter -> size -> enter
  5. At 3:50 PM ET: close all positions and log trades
  6. Disconnect, sleep until next market open

Run:
    cd /root/Live_Trader_Halts && source venv/bin/activate && python main.py
"""

import logging
import queue
import threading
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

from ib_insync import IB

from config import IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID, PAPER, LOG_FILE
from halt_monitor import HaltMonitor
from signal_filter import passes_filters
from execution import place_entry, wait_for_fill
from position_manager import PositionManager
from eod_exit import should_exit_now, run_eod_exit

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR  = 16


def is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
    close_t = now.replace(hour=MARKET_CLOSE_HOUR, minute=0,                  second=0, microsecond=0)
    return open_t <= now <= close_t


def seconds_until_market_open() -> float:
    from datetime import timedelta
    now = datetime.now(ET)
    open_today = now.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )
    if now < open_today and now.weekday() < 5:
        return (open_today - now).total_seconds()
    days_ahead = 1
    while (now.weekday() + days_ahead) % 7 >= 5:
        days_ahead += 1
    next_open = (now + timedelta(days=days_ahead)).replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )
    return (next_open - now).total_seconds()


def run_trading_day(ib: IB) -> None:
    pm = PositionManager(ib)
    pm.snapshot_sod_equity()

    eod_done = False

    def on_resumption(symbol: str) -> None:
        # Called from the main thread — IB API calls are safe here.
        log.info("Processing resumption: %s", symbol)

        # Apply signal filters — returns (passes, reason, metrics)
        passes, reason, metrics = passes_filters(ib, symbol)
        if not passes:
            log.info("SKIP %s — %s", symbol, reason)
            return

        price = metrics.get("price")
        if not price:
            return

        # Compute position size
        shares = pm.compute_shares(symbol, price)
        if shares < 1:
            log.info("SKIP %s — position size too small", symbol)
            return

        # Market order at resumption — matches backtest fill assumption
        trade = place_entry(ib, symbol, shares, limit_price=None)
        if trade is None:
            return

        fill_price = wait_for_fill(ib, trade, timeout_seconds=60)
        if fill_price is None:
            log.warning("Entry not confirmed for %s — not tracking position", symbol)
            return

        pm.add_position(
            symbol, shares, fill_price, trade.order.orderId,
            signal_price=metrics.get("price"),   # pre_halt_close — for slippage analysis
            metrics=metrics,
        )

    # Start halt monitor poll thread (RSS only — no IB calls inside)
    monitor = HaltMonitor()
    thread  = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    log.info("Trading day started. Mode: %s  BOD_equity=$%.2f",
             "PAPER" if PAPER else "LIVE", pm.sod_equity())

    # Main loop — drain halt queue on main thread so IB calls work correctly
    while is_market_hours():
        # Process all queued resumptions (IB calls must run on this thread)
        while True:
            try:
                symbol = monitor._queue.get_nowait()
                log.info("Resumption dequeued: %s  (queue depth after: %d)",
                         symbol, monitor._queue.qsize())
                if eod_done:
                    log.info("Ignoring %s — EOD exit already run", symbol)
                else:
                    try:
                        on_resumption(symbol)
                    except Exception as e:
                        log.error("on_resumption error for %s: %s", symbol, e, exc_info=True)
            except queue.Empty:
                break

        if should_exit_now() and not eod_done:
            eod_done = True
            run_eod_exit(ib, pm)
        time.sleep(1)

    log.info("Market closed. Trading day complete.")


def main() -> None:
    log.info("=== LULD Halt Trader starting. PAPER=%s PORT=%d ===", PAPER, IBKR_PORT)

    while True:
        if not is_market_hours():
            secs = seconds_until_market_open()
            log.info("Market closed — sleeping %.0fs until next open", secs)
            time.sleep(max(secs - 60, 60))
            continue

        ib = IB()
        try:
            ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
            log.info("Connected to IBKR at %s:%d", IBKR_HOST, IBKR_PORT)
            run_trading_day(ib)
        except Exception as e:
            log.error("Fatal error: %s", e, exc_info=True)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
            log.info("Disconnected from IBKR")

        time.sleep(60)


if __name__ == "__main__":
    main()
