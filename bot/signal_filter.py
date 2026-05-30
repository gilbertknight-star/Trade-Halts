"""
Applies strategy filters to a halted symbol before placing an order.

Filters (matching backtest_rvol.py exactly):
  1. Entry cutoff: no new trades at or after 3:49 PM ET
  2. Halt-up: pre_halt_close >= session_open * (1 + UP_MIN_MOVE)
  3. RVOL: surge_vol_rate / baseline_vol_rate >= MIN_RVOL

Returns (passes: bool, reason: str, metrics: dict)
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock

from config import UP_MIN_MOVE, MIN_RVOL, ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MINUTE

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

    Single data call: reqHistoricalData 1D 1-min bars only.
    No reqMktData snapshot needed — works on paper accounts with delayed quotes.

    pre_halt_close (last completed bar before halt) is used for:
      - price range filter
      - gap-up filter   (matches backtest exactly)
      - position sizing (passed back in metrics as 'price')
    """
    # ── Entry cutoff: no new trades at or after 3:49 PM ET (matches backtest) ───
    now_et = datetime.now(ET)
    if now_et.hour > ENTRY_CUTOFF_HOUR or (
        now_et.hour == ENTRY_CUTOFF_HOUR and now_et.minute >= ENTRY_CUTOFF_MINUTE
    ):
        return False, f"past entry cutoff {ENTRY_CUTOFF_HOUR}:{ENTRY_CUTOFF_MINUTE:02d} ET", {}

    contract = Stock(symbol, "SMART", "USD")

    with _IB_LOCK:
        try:
            ib.qualifyContracts(contract)
        except Exception as e:
            log.warning("Could not qualify contract %s: %s", symbol, e)
            return False, f"qualify failed: {e}", {}

    # ── Single historical data call — all filters computed from these bars ─────
    day_open, rvol, pre_halt_close = _get_open_and_rvol(ib, contract)

    if pre_halt_close is None or pre_halt_close <= 0:
        return False, "no valid pre-halt price from bars", {}

    price = pre_halt_close   # used for sizing

    # ── Filter 1: Halt-up (pre_halt_close vs day_open — matches backtest) ─────
    up_move = None
    if day_open and day_open > 0:
        up_move = (price - day_open) / day_open
        if up_move < UP_MIN_MOVE:
            return False, f"up_move {up_move:.4f} < UP_MIN_MOVE {UP_MIN_MOVE}", {}

    # ── Filter 2: RVOL ────────────────────────────────────────────────────────
    if rvol is None:
        return False, "rvol unavailable (insufficient baseline — halt too early in session)", {}
    if rvol < MIN_RVOL:
        return False, f"rvol {rvol:.2f} < MIN_RVOL {MIN_RVOL}", {}

    metrics = {
        "price":          round(price, 4),
        "day_open":       round(day_open, 4)   if day_open  else None,
        "up_move":        round(up_move, 6)    if up_move is not None else None,
        "rvol":           round(rvol, 4)       if rvol    is not None else None,
    }
    log.info("PASS %s — pre_halt=%.4f up_move=%s rvol=%s",
             symbol, price,
             f"{up_move:.2%}" if up_move is not None else "n/a",
             f"{rvol:.2f}"    if rvol    is not None else "n/a")
    return True, "ok", metrics


def _get_open_and_rvol(ib: IB, contract) -> tuple[float | None, float | None, float | None]:
    """
    Single reqHistoricalData call that returns:
      - day_open:       open price of the first 1-min bar today
      - rvol:           surge/baseline volume ratio (matches backtest formula)
      - pre_halt_close: last bar's close BEFORE the halt started
                        (used for gap-up filter — matches backtest exactly)
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
            return None, None, None

        # day open = close of first bar (matches backtest: bars.iloc[0]["close"])
        day_open = float(bars[0].close) if bars else None

        # ── Timing windows ────────────────────────────────────────────────────
        now_et       = datetime.now(ET)
        market_open  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        halt_started = now_et - timedelta(minutes=LULD_PAUSE_MINUTES)
        surge_start  = halt_started - timedelta(minutes=RVOL_SURGE_MINUTES)

        baseline_minutes = (surge_start - market_open).total_seconds() / 60
        if baseline_minutes <= 0:
            log.debug("RVOL: surge window starts before open for %s — skipping",
                      contract.symbol)
            return day_open, None, None

        surge_vol      = 0.0
        baseline_vol   = 0.0
        pre_halt_close = None   # last bar before halt started

        surge_bars_count    = 0
        baseline_bars_count = 0

        for bar in bars:
            bar_dt = bar.date
            if hasattr(bar_dt, 'tzinfo') and bar_dt.tzinfo is not None:
                # Already timezone-aware — convert to ET
                bar_dt = bar_dt.astimezone(ET)
            else:
                # Naive datetime: IBKR returns these as UTC, not local time.
                # Must attach UTC first, then convert — NOT replace(tzinfo=ET)
                # which would wrongly treat a UTC time as if it were already ET.
                bar_dt = bar_dt.replace(tzinfo=timezone.utc).astimezone(ET)

            # Pre-halt close: last bar that completed before the halt started
            if bar_dt < halt_started:
                pre_halt_close = float(bar.close)

            if surge_start <= bar_dt < halt_started:
                surge_vol += bar.volume
                surge_bars_count += 1
            elif market_open <= bar_dt < surge_start:
                baseline_vol += bar.volume
                baseline_bars_count += 1

        log.info(
            "RVOL windows %s: market_open=%s  surge=%s→%s  halt_started=%s  "
            "baseline_bars=%d vol=%.0f  surge_bars=%d vol=%.0f  baseline_min=%.1f",
            contract.symbol,
            market_open.strftime("%H:%M"),
            surge_start.strftime("%H:%M:%S"),
            halt_started.strftime("%H:%M:%S"),
            halt_started.strftime("%H:%M:%S"),
            baseline_bars_count, baseline_vol,
            surge_bars_count, surge_vol,
            baseline_minutes,
        )

        if baseline_vol <= 0:
            log.info("RVOL %s: no baseline volume (halt too close to open) — skip",
                     contract.symbol)
            return day_open, None, pre_halt_close

        if surge_vol <= 0:
            log.info("RVOL %s: surge_vol=0 — no trades in the 5 min before halt "
                     "(stock may have been halted earlier or extremely illiquid)",
                     contract.symbol)
            return day_open, 0.0, pre_halt_close

        baseline_rate = baseline_vol / baseline_minutes
        surge_rate    = surge_vol    / RVOL_SURGE_MINUTES
        rvol          = surge_rate / baseline_rate

        log.info("RVOL %s: surge=%.0f/min  baseline=%.0f/min  rvol=%.2f  pre_halt=%.4f",
                 contract.symbol, surge_rate, baseline_rate, rvol,
                 pre_halt_close or 0)
        return day_open, rvol, pre_halt_close

    except Exception as e:
        log.warning("Could not compute open/RVOL for %s: %s", contract.symbol, e,
                    exc_info=True)
    return None, None, None


