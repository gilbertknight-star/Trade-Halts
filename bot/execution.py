"""
Handles order placement and cancellation via ib_insync.
Entry: limit order at pre-halt price, auto-cancels if unfilled after timeout.
Exit: market order at EOD.
"""

import logging

from ib_insync import IB, LimitOrder, MarketOrder, Stock, Trade

log = logging.getLogger(__name__)

LIMIT_FILL_TIMEOUT = 60   # seconds to wait for limit order fill before cancelling


def place_entry(ib: IB, symbol: str, shares: int, limit_price: float | None = None) -> Trade | None:
    """
    Place a BUY order for the given shares.
    If limit_price provided: limit order at that price (pre-halt last trade price).
      - If stock resumes above limit_price  -> fills immediately
      - If stock resumes below limit_price  -> does not fill, cancelled after timeout
    Falls back to market order if limit_price is None.
    """
    if shares <= 0:
        log.warning("place_entry called with shares=%d for %s — skipping", shares, symbol)
        return None

    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        log.error("Could not qualify contract %s: %s", symbol, e)
        return None

    from config import IBKR_ACCOUNT
    if limit_price is not None:
        order = LimitOrder("BUY", shares, round(limit_price, 2),
                           account=IBKR_ACCOUNT or "")
        log.info("ENTRY LIMIT ORDER: %s BUY %d @ $%.4f", symbol, shares, limit_price)
    else:
        order = MarketOrder("BUY", shares, account=IBKR_ACCOUNT or "")
        log.info("ENTRY MARKET ORDER: %s BUY %d shares", symbol, shares)

    trade = ib.placeOrder(contract, order)
    return trade


def cancel_order(ib: IB, trade: Trade) -> None:
    """Cancel an open order."""
    try:
        ib.cancelOrder(trade.order)
        log.info("Order %s cancelled", trade.order.orderId)
    except Exception as e:
        log.warning("Could not cancel order %s: %s", trade.order.orderId, e)


def place_exit(ib: IB, symbol: str, shares: int) -> Trade | None:
    """Place a market SELL order to close the position."""
    if shares <= 0:
        log.warning("place_exit called with shares=%d for %s — skipping", shares, symbol)
        return None

    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        log.error("Could not qualify contract %s for exit: %s", symbol, e)
        return None

    from config import IBKR_ACCOUNT
    order = MarketOrder("SELL", shares, account=IBKR_ACCOUNT or "")
    trade = ib.placeOrder(contract, order)
    log.info("EXIT ORDER placed: %s SELL %d shares  orderId=%s", symbol, shares, trade.order.orderId)
    return trade


def wait_for_fill(ib: IB, trade: Trade, timeout_seconds: int = 30) -> float | None:
    """
    Wait up to timeout_seconds for a trade to fill.
    If it's a limit order and doesn't fill in time, cancels it automatically.
    Returns the average fill price or None.
    """
    for _ in range(timeout_seconds):
        ib.sleep(1)
        if trade.orderStatus.status == "Filled":
            fill_price = trade.orderStatus.avgFillPrice
            log.info(
                "Order %s filled: %d shares @ %.4f",
                trade.order.orderId,
                trade.orderStatus.filled,
                fill_price,
            )
            return float(fill_price)

    # Timed out — cancel if still open (limit orders only, market orders fill instantly)
    if trade.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
        log.info("Limit order %s unfilled after %ds — cancelling",
                 trade.order.orderId, timeout_seconds)
        cancel_order(ib, trade)

    return None


def get_first_minute_volume(ib: IB, symbol: str) -> float | None:
    """
    Request 1-minute historical bar immediately after resumption
    to get first-minute volume for the vol cap calculation.
    """
    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="120 S",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        if bars:
            return float(bars[-1].volume)
    except Exception as e:
        log.warning("Could not get first-minute volume for %s: %s", symbol, e)
    return None
