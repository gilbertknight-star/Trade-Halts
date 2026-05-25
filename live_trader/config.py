"""
Central configuration for the LULD halt resumption live trader.
Switch PAPER = True/False to toggle between paper and live trading.

Key parameters match the realistic_backtest.py settings exactly.
"""

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER = True   # Set False only when ready for real money

# ── IBKR Gateway connection ───────────────────────────────────────────────────
# IB Gateway: paper=4002, live=4001
# TWS:        paper=7497, live=7496
IBKR_HOST      = "127.0.0.1"
IBKR_PORT      = 4002 if PAPER else 4001   # IB Gateway ports
IBKR_CLIENT_ID = 1

# ── Position sizing (mirrors realistic_backtest.py) ───────────────────────────
POSITION_FRACTION = 0.10          # 10% of BOD equity per trade
MAX_POS_USD       = 100_000.0     # hard cap per trade
MIN_POS_USD       = 5.0           # skip if position would be below this
MAX_POS_PER_STOCK = 300_000.0     # max total exposure per stock per day

# ── Signal filters ────────────────────────────────────────────────────────────
MIN_PRICE     = 1.00    # ignore sub-$1 stocks
MAX_PRICE     = 500.0   # ignore stocks above $500
UP_MIN_MOVE   = 0.02    # pre-halt close must be >= session open * 1.02 (halt-up)
MIN_RVOL      = 1.0     # surge vol rate / baseline vol rate >= 1.0

# ── EOD exit ──────────────────────────────────────────────────────────────────
EXIT_HOUR_ET   = 15   # 3:50 PM ET — matches backtest exit time
EXIT_MINUTE_ET = 50

# ── NASDAQ halt RSS feed ──────────────────────────────────────────────────────
HALT_RSS_URL      = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
HALT_POLL_SECONDS = 3

# ── LULD halt codes to act on ─────────────────────────────────────────────────
LULD_HALT_CODES = {"LUDP"}

# ── Files ─────────────────────────────────────────────────────────────────────
STATE_FILE  = "state.json"   # open positions, persists across restarts
TRADES_CSV  = "trades.csv"   # closed trade log for comparison with backtest
LOG_FILE    = "trader.log"
