# Bot — LULD Halt Resumption Live Trader

Automated live trading bot that executes the LULD halt resumption strategy
via Interactive Brokers (IBKR) API. Designed to run on a Linux server 24/7.

## Files

| File | Purpose |
|------|---------|
| `config.py` | **Start here** — all tunable parameters |
| `main.py` | Entry point, market hours loop |
| `halt_monitor.py` | Polls NASDAQ RSS feed, detects halt resumptions |
| `signal_filter.py` | Applies halt-up + RVOL filters |
| `execution.py` | Places and monitors IBKR orders |
| `position_manager.py` | Position sizing, state persistence, trade logging |
| `eod_exit.py` | 3:50 PM ET exit logic |
| `requirements.txt` | Python dependencies |

## Key Config Parameters

Edit `bot/config.py` to change strategy behaviour:

```python
PAPER             = True        # False = real money (careful!)
IBKR_PORT         = 4002        # IB Gateway paper port
POSITION_FRACTION = 0.10        # 10% of BOD equity per trade
MAX_POS_USD       = 100_000     # Hard cap per trade
MAX_POS_PER_STOCK = 300_000     # Max total per stock per day (pyramid cap)
MIN_RVOL          = 1.0         # Minimum relative volume filter
UP_MIN_MOVE       = 0.02        # Must be >=2% above session open
EXIT_HOUR_ET      = 15          # Exit at 3:50 PM ET
EXIT_MINUTE_ET    = 50
```

## Running the Bot

See [../docs/server_setup.md](../docs/server_setup.md) for full server setup.

```bash
# Start (server)
cd /root/Live_Trader_Halts/bot
source ../venv/bin/activate
python main.py

# Or via systemd (recommended — auto-restarts, survives reboots)
systemctl start halt-trader
systemctl status halt-trader
journalctl -u halt-trader -f
```

## Output Files (not in git — machine-specific)

| File | Contents |
|------|---------|
| `trader.log` | Full activity log with every decision |
| `state.json` | Open positions — survives restarts |
| `trades.csv` | Closed trade log — compare against backtest |
