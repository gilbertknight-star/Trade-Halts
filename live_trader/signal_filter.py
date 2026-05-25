"""
Applies strategy filters to a halted symbol before placing an order.

Filters (matching backtest):
  1. Price range: MIN_PRICE <= price <= MAX_PRICE
  2. Halt-up: pre_halt_close >= session_open * (1 + UP_MIN_MOVE)
  3. RVOL: surge_vol_rate / baseline_vol_rate >= MIN_RVOL

Returns (passes: bool, reason: str, metrics: dict)
"""

import logging
import threading

from ib_insync import IB, Stock, Ticker

from config import MIN_PRICE, MAX_PRICE, UP_MIN_MOVE, MIN_RVOL

log = logging.getLogger(__name__)

# ib_insync is not thread-safe — all API calls must be serialised
_IB_LOCK = threading.Lock()


def passes_filters(ib: IB, symbol: str, halt_dt_et=None) -> tuple[bool, str, dict]:
    """
    Request a snapshot quote and bar data for symbol, apply all filters.
    Returns (passes, reason, metrics).
    metrics keys: price, prev_close, day_open, up_move, rvol
    """
    contract = Stock(symbol, "SMART", "USD")

    with _IB_LOCK:
        try:
            ib.qualifyContracts(contract)
        except Exception as e:
            log.warning("Could not qualify contract %s: %s", symbol, e)
            return False, f"qualify failed: {e}", {}

        ticker: Ticker = ib.reqMktData(contract, "", True, False)
        ib.sleep(1.5)
        price     = ticker.last or ticker.close or ticker.bid or None
        prev_close = ticker.close or None
        ib.cancelMktData(contract)

    if price is None or price <= 0:
        return False, "no valid price", {}

    # ── Filter 1: Price range ─────────────────────────────────────────────────
    if price < MIN_PRICE:
        return False, f"price {price:.4f} below MIN_PRICE {MIN_PRICE}", {}
    if price > MAX_PRICE:
        return False, f"price {price:.4f} above MAX_PRICE {MAX_PRICE}", {}

    # ── Filter 2: Halt-up (price above session open by UP_MIN_MOVE) ──────────
    day_open = _get_day_open(ib, contract)
    up_move  = None
    if day_open and day_open > 0:
        up_move = (price - day_open) / day_open
        if up_move < UP_MIN_MOVE:
            return False, f"up_move {up_move:.4f} < UP_MIN_MOVE {UP_MIN_MOVE}", {}

    # ── Filter 3: RVOL ────────────────────────────────────────────────────────
    rvol = _get_rvol(ib, contract)
    if rvol is not None and rvol < MIN_RVOL:
        return False, f"rvol {rvol:.2f} < MIN_RVOL {MIN_RVOL}", {}

    metrics = {
        "price":      round(price, 4),
        "prev_close": round(prev_close, 4) if prev_close else None,
        "day_open":   round(day_open, 4)   if day_open  else None,
        "up_move":    round(up_move, 6)    if up_move is not None else None,
        "rvol":       round(rvol, 4)       if rvol is not None else None,
    }
    log.info("PASS %s — price=%.4f up_move=%s rvol=%s",
             symbol, price,
             f"{up_move:.2%}" if up_move is not None else "n/a",
             f"{rvol:.2f}"    if rvol    is not None else "n/a")
    return True, "ok", metrics


def _get_day_open(ib: IB, contract) -> float | None:
    """Fetch today's session open price via 1-day 1-min bars."""
    try:
        with _IB_LOCK:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        if bars:
            return bars[0].open  # first bar of day = open
    except Exception as e:
        log.debug("Could not fetch day open for %s: %s", contract.symbol, e)
    return None


def _get_rvol(ib: IB, contract) -> float | None:
    """
    Compute RVOL: average volume of today's bars so far vs
    average volume of same bars over last 10 trading days.
    """
    try:
        with _IB_LOCK:
            # Today's 1-min bars
            today_bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        if not today_bars:
            return None

        today_avg = sum(b.volume for b in today_bars) / len(today_bars)

        with _IB_LOCK:
            hist_bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="10 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        if not hist_bars:
            return None

        hist_avg = sum(b.volume for b in hist_bars) / len(hist_bars)
        if hist_avg <= 0:
            return None

        return today_avg / hist_avg

    except Exception as e:
        log.debug("Could not compute RVOL for %s: %s", contract.symbol, e)
    return None
