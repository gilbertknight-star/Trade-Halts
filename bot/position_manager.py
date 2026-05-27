"""
Tracks SOD equity, open positions, position sizing, and trade logging.
Persists open positions to disk so state survives restarts.
Logs every closed trade to TRADES_CSV for comparison with backtest.
"""

import csv
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from config import (
    POSITION_FRACTION,
    MAX_POS_USD,
    MIN_POS_USD,
    MAX_POS_PER_STOCK,
    MAX_SHARES_OVERRIDE,
    IBKR_ACCOUNT,
    STATE_FILE,
    TRADES_CSV,
)

log = logging.getLogger(__name__)

_CSV_FIELDS = [
    "date", "symbol",
    "entry_ts", "exit_ts",
    "signal_px",            # pre-halt filter price (pre_halt_close from signal_filter)
    "entry_px", "exit_px",
    "shares", "invested",
    "gross_pnl", "ret_pct",
    "rvol", "up_move",
    "BOD_equity",
]


class PositionManager:
    def __init__(self, ib):
        self.ib          = ib
        self._state_path = Path(STATE_FILE)
        self._csv_path   = Path(TRADES_CSV)
        self._open_positions: dict[str, dict] = {}   # symbol -> position dict
        self._sod_equity: float | None = None
        self._today: str = ""
        self._load_state()
        self._ensure_csv_header()

    # ── Equity ────────────────────────────────────────────────────────────────

    def snapshot_sod_equity(self) -> float:
        equity    = self._fetch_net_liquidation()
        today_str = date.today().isoformat()
        if self._today != today_str:
            self._sod_equity = equity
            self._today      = today_str
            log.info("SOD equity snapshot: $%.2f on %s", equity, today_str)
            self._save_state()
        return equity

    def sod_equity(self) -> float:
        if self._sod_equity is None:
            return self.snapshot_sod_equity()
        return self._sod_equity

    def current_equity(self) -> float:
        return self._fetch_net_liquidation()

    def _fetch_net_liquidation(self) -> float:
        for av in self.ib.accountValues():
            if av.tag != "NetLiquidation" or av.currency != "USD":
                continue
            # If a specific account is configured, only use that one
            if IBKR_ACCOUNT and av.account != IBKR_ACCOUNT:
                continue
            return float(av.value)
        raise RuntimeError(
            f"Could not fetch NetLiquidation from IBKR "
            f"(account={IBKR_ACCOUNT or 'any'})"
        )

    # ── Position sizing ───────────────────────────────────────────────────────

    def compute_shares(self, symbol: str, price: float,
                       vol_1m: float | None = None) -> int:
        """
        Returns whole shares to buy.
        Applies: 10% of BOD equity, $100k hard cap,
                 per-stock daily cap, minimum position size.
        If MAX_SHARES_OVERRIDE is set, caps at that many shares regardless
        of sizing (used for live execution testing with minimal risk).
        """
        # Hard share cap — for live testing mode (e.g. 1 share per trade)
        if MAX_SHARES_OVERRIDE is not None:
            log.info(
                "Sizing %s: MAX_SHARES_OVERRIDE=%d  price=%.4f  risk=$%.2f",
                symbol, MAX_SHARES_OVERRIDE, price, MAX_SHARES_OVERRIDE * price,
            )
            return MAX_SHARES_OVERRIDE

        sod_eq = self.sod_equity()

        # 10% of BOD equity, capped at MAX_POS_USD
        budget = min(sod_eq * POSITION_FRACTION, MAX_POS_USD)

        # Per-stock daily cap: don't exceed MAX_POS_PER_STOCK across pyramid entries
        already_in = self._stock_notional(symbol)
        room = MAX_POS_PER_STOCK - already_in
        if room <= 0:
            log.info("SKIP %s — hit per-stock cap ($%.0f)", symbol, MAX_POS_PER_STOCK)
            return 0
        budget = min(budget, room)

        if budget < MIN_POS_USD:
            return 0

        shares = int(budget / price)
        log.info(
            "Sizing %s: sod_eq=$%.0f budget=$%.0f price=%.4f -> %d shares",
            symbol, sod_eq, budget, price, shares,
        )
        return shares

    def _open_notional(self) -> float:
        return sum(p["invested"] for p in self._open_positions.values())

    def _stock_notional(self, symbol: str) -> float:
        """Total invested in all open positions for this symbol today."""
        return sum(
            p["invested"]
            for key, p in self._open_positions.items()
            if p["symbol"] == symbol
        )

    # ── Position tracking ─────────────────────────────────────────────────────

    def add_position(self, symbol: str, shares: int, entry_price: float,
                     order_id: int, halt_dt_et=None, signal_price: float | None = None,
                     metrics: dict | None = None) -> None:
        metrics = metrics or {}
        # Use a unique key for pyramid entries (symbol_N)
        key = self._unique_key(symbol)
        self._open_positions[key] = {
            "symbol":       symbol,
            "shares":       shares,
            "entry_price":  entry_price,
            "invested":     shares * entry_price,
            "order_id":     order_id,
            "entry_time":   datetime.now(timezone.utc).isoformat(),
            "halt_time":    halt_dt_et.isoformat() if halt_dt_et else None,
            "signal_price": signal_price,           # pre_halt_close from signal_filter
            "rvol":         metrics.get("rvol"),
            "up_move":      metrics.get("up_move"),
            "BOD_equity":   self._sod_equity,
        }
        log.info("Position added [%s]: %d shares @ %.4f  invested=$%.0f",
                 key, shares, entry_price, shares * entry_price)
        self._save_state()

    def remove_position(self, key: str) -> None:
        if key in self._open_positions:
            del self._open_positions[key]
            log.info("Position removed: %s", key)
            self._save_state()

    def open_positions(self) -> dict[str, dict]:
        return dict(self._open_positions)

    def has_position(self, symbol: str) -> bool:
        """True if any open position (including pyramids) exists for symbol."""
        return any(p["symbol"] == symbol for p in self._open_positions.values())

    def _unique_key(self, symbol: str) -> str:
        """Generate unique key for pyramid entries: SYMBOL, SYMBOL_2, SYMBOL_3 ..."""
        if symbol not in self._open_positions:
            return symbol
        i = 2
        while f"{symbol}_{i}" in self._open_positions:
            i += 1
        return f"{symbol}_{i}"

    # ── Trade logging ─────────────────────────────────────────────────────────

    def log_closed_trade(self, key: str, exit_price: float,
                         exit_time_utc: datetime) -> None:
        """Write a completed trade row to TRADES_CSV."""
        pos = self._open_positions.get(key)
        if pos is None:
            return
        shares     = pos["shares"]
        entry_px   = pos["entry_price"]
        gross_pnl  = (exit_price - entry_px) * shares
        ret_pct    = (exit_price / entry_px - 1.0) * 100 if entry_px > 0 else 0.0

        row = {
            "date":       date.today().isoformat(),
            "symbol":     pos["symbol"],
            "entry_ts":   pos.get("entry_time", ""),
            "exit_ts":    exit_time_utc.isoformat(),
            "signal_px":  round(pos["signal_price"], 4) if pos.get("signal_price") else "",
            "entry_px":   round(entry_px, 4),
            "exit_px":    round(exit_price, 4),
            "shares":     shares,
            "invested":   round(pos["invested"], 2),
            "gross_pnl":  round(gross_pnl, 2),
            "ret_pct":    round(ret_pct, 4),
            "rvol":       pos.get("rvol", ""),
            "up_move":    pos.get("up_move", ""),
            "BOD_equity": pos.get("BOD_equity", ""),
        }
        with open(self._csv_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS).writerow(row)
        log.info("Trade logged: %s ret=%.2f%%  pnl=$%.2f", key, ret_pct, gross_pnl)

    def _ensure_csv_header(self) -> None:
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_CSV_FIELDS).writeheader()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        state = {
            "sod_equity":      self._sod_equity,
            "today":           self._today,
            "open_positions":  self._open_positions,
        }
        self._state_path.write_text(json.dumps(state, indent=2))

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text())
            if state.get("today") == date.today().isoformat():
                self._sod_equity     = state.get("sod_equity")
                self._today          = state.get("today", "")
                self._open_positions = state.get("open_positions", {})
                log.info("Restored state: sod_equity=$%.2f open=%d",
                         self._sod_equity or 0, len(self._open_positions))
            else:
                log.info("State from %s — starting fresh", state.get("today"))
        except Exception as e:
            log.warning("Could not load state: %s", e)
