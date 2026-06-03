"""
Central configuration for the LULD halt resumption live trader.
Switch PAPER = True/False to toggle between paper and live trading.

Key parameters match the realistic_backtest.py settings exactly.
"""

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER = False   # True = paper account (port 4002), False = live account (port 4001)

# ── IBKR Gateway connection ───────────────────────────────────────────────────
# IB Gateway: paper=4002, live=4001
# TWS:        paper=7497, live=7496
IBKR_HOST      = "127.0.0.1"
IBKR_PORT      = 4002 if PAPER else 4001   # IB Gateway ports
IBKR_CLIENT_ID = 1
IBKR_ACCOUNT   = "U25292584"   # live account — empty string = auto (picks first)

# ── Position sizing (mirrors README backtest exactly) ────────────────────────
POSITION_FRACTION = 0.10          # 10% of BOD equity per trade
MAX_POS_USD       = 100_000.0     # hard cap per trade ($100k)
MIN_POS_USD       = 5.0           # skip if position would be below this
CASH_RESERVE      = 0.0           # no cash reserve (matches README backtest)

# ── Live execution cap ────────────────────────────────────────────────────────
# Set to an integer to hard-cap shares per trade regardless of sizing.
# Use during live testing to get real fills while risking minimal capital.
# Set to None to use full sizing (POSITION_FRACTION / MAX_POS_USD logic).
MAX_SHARES_OVERRIDE = 1           # 1 share per trade — real execution, minimal risk

# ── Signal filters (mirrors backtest_rvol.py exactly) ────────────────────────
UP_MIN_MOVE   = 0.02    # pre-halt close must be >= session open * 1.02 (halt-up)
MIN_RVOL      = 1.0     # surge vol rate / baseline vol rate >= 1.0
ENTRY_CUTOFF_HOUR   = 15  # no new entries at or after 3:49 PM ET (backtest cutoff)
ENTRY_CUTOFF_MINUTE = 49

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

# ── Daily validator email (Gmail SMTP) ───────────────────────────────────────
# Uses a Gmail App Password (not your main password).
# Generate at: https://myaccount.google.com/apppasswords
# Store the app password in the GMAIL_APP_PASSWORD environment variable on
# the server:  echo 'export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"' >> /root/.bashrc
# Leave GMAIL_APP_PASSWORD as None to disable email.
import os as _os
GMAIL_APP_PASSWORD = None  # SMTP blocked by DigitalOcean — using xlsx report instead
VALIDATOR_EMAIL_TO   = "gilbert.knight@gmail.com"
VALIDATOR_EMAIL_FROM = "gilbert.knight@gmail.com"
