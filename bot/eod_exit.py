"""
Scheduled EOD exit — closes all open positions at 3:50 PM ET.
Logs each closed trade to TRADES_CSV via PositionManager.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ib_insync import IB

from config import EXIT_HOUR_ET, EXIT_MINUTE_ET
from execution import place_exit, wait_for_fill
from position_manager import PositionManager

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


def should_exit_now() -> bool:
    now = datetime.now(ET)
    return now.hour == EXIT_HOUR_ET and now.minute >= EXIT_MINUTE_ET


def run_eod_exit(ib: IB, pm: PositionManager) -> None:
    """Close all open positions and log each trade to CSV."""
    positions = pm.open_positions()
    if not positions:
        log.info("EOD exit: no open positions")
        return

    log.info("EOD exit: closing %d positions", len(positions))
    exit_time_utc = datetime.now(timezone.utc)

    for key, pos in list(positions.items()):
        symbol = pos["symbol"]
        shares = pos["shares"]
        log.info("EOD exit: selling %s (%s) %d shares", key, symbol, shares)

        trade = place_exit(ib, symbol, shares)
        if trade is None:
            log.error("EOD exit: failed to place order for %s", key)
            continue

        fill_price = wait_for_fill(ib, trade, timeout_seconds=90)
        if fill_price:
            pnl     = (fill_price - pos["entry_price"]) * shares
            ret_pct = (fill_price / pos["entry_price"] - 1.0) * 100
            log.info(
                "EOD filled [%s]: entry=%.4f exit=%.4f  ret=%.2f%%  pnl=$%.2f",
                key, pos["entry_price"], fill_price, ret_pct, pnl,
            )
            pm.log_closed_trade(key, fill_price, exit_time_utc)
        else:
            log.warning("EOD: %s fill not confirmed — logging at last price", key)
            # Still log with 0 exit price so we know it happened
            pm.log_closed_trade(key, 0.0, exit_time_utc)

        pm.remove_position(key)

    log.info("EOD exit complete.")
