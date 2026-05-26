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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, Ticker

from config import MIN_PRICE, MAX_PRICE, UP_MIN_MOVE, MIN_RVOL

ET = ZoneInfo("America/New_York")
LULD_PAUSE_MINUTES = 5    # LULD pauses are always exactly 5 minutes
RVOL_SURGE_MINUTES = 5    # measure surge in this window before halt started

log = logging.getLogger(__name__)

# ib_insync is not thread-safe — all API calls must be serialised
_IB_LOCK = threading.Lock()


def passes_filters(ib: IB, symbol: str, halt_dt_et=None) -> tuple[bool, str, dict]:
    """
    Request a snapshot quote and bar data for symbol, apply all filters.
    Returns (passes, reason, metrics).
    metrics keys: price, prev_close, day_open, up_move, rvol

    Data calls (in order):
      1. reqMktData snapshot  — current price
      2. reqHistoricalData 1D 1-min bars  — used for BOTH day_open and RVOL
         (single request, split into windows inside _get_open_and_rvol)
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
        price      = ticker.last or ticker.close or ticker.bid or None
        prev_close = ticker.close or None
        ib.cancelMktData(contract)

    if price is None or price <= 0:
        return False, "no valid price", {}

    # ── Filter 1: Price range ─────────────────────────────────────────────────
    if price < MIN_PRICE:
        return False, f"price {price:.4f} below MIN_PRICE {MIN_PRICE}", {}
    if price > MAX_PRICE:
        return False, f"price {price:.4f} above MAX_PRICE {MAX_PRICE}", {}

    # ── Fetch 1-min bars once — reused for both day_open and RVOL ────────────
    day_open, rvol = _get_open_and_rvol(ib, contract)

    # ── Filter 2: Halt-up ─────────────────────────────────────────────────────
    up_move = None
    if day_open and day_open > 0:
        up_move = (price - day_open) / day_open
        if up_move < UP_MIN_MOVE:
            return False, f"up_move {up_move:.4f} < UP_MIN_MOVE {UP_MIN_MOVE}", {}

    # ── Filter 3: RVOL ────────────────────────────────────────────────────────
    # None means baseline window was empty (halt too early in session to measure).
    # Matches backtest: skip any event where baseline_bars was empty.
    if rvol is None:
        return False, "rvol unavailable (insufficient baseline — halt too early in session)", {}
    if rvol < MIN_RVOL:
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


def _get_open_and_rvol(ib: IB, contract) -> tuple[float | None, float | None]:
    """
    Single reqHistoricalData call that returns both:
      - day_open: open price of the first 1-min bar today
      - rvol:     surge/baseline volume ratio (matches backtest formula)

    One call instead of two cuts latency in half at the most time-sensitive
    moment — right after halt resumption.
    """
    try:
        with _IB_LOCK:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,   # returns datetime objects
            )
        if not bars:
            return None, None

        # day open = first bar's open price
        day_open = float(bars[0].open) if bars else None

        # ── RVOL windows ──────────────────────────────────────────────────────
        now_et      = datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        halt_started = now_et - timedelta(minutes=LULD_PAUSE_MINUTES)
        surge_start  = halt_started - timedelta(minutes=RVOL_SURGE_MINUTES)

        baseline_minutes = (surge_start - market_open).total_seconds() / 60
        if baseline_minutes <= 0:
            log.debug("RVOL: surge window starts before open for %s — skipping",
                      contract.symbol)
            return day_open, None

        surge_vol    = 0.0
        baseline_vol = 0.0

        for bar in bars:
            bar_dt = bar.date
            if hasattr(bar_dt, 'tzinfo') and bar_dt.tzinfo is not None:
                bar_dt = bar_dt.astimezone(ET)
            else:
                bar_dt = bar_dt.replace(tzinfo=ET)

            if surge_start <= bar_dt < halt_started:
                surge_vol += bar.volume
            elif market_open <= bar_dt < surge_start:
                baseline_vol += bar.volume

        if baseline_vol <= 0:
            return day_open, None

        baseline_rate = baseline_vol / baseline_minutes
        surge_rate    = surge_vol    / RVOL_SURGE_MINUTES
        rvol          = surge_rate / baseline_rate

        log.debug("RVOL %s: surge=%.0f/min  baseline=%.0f/min  rvol=%.2f",
                  contract.symbol, surge_rate, baseline_rate, rvol)
        return day_open, rvol

    except Exception as e:
        log.debug("Could not compute open/RVOL for %s: %s", contract.symbol, e)
    return None, None


