# LULD Halt Resumption Strategy

Systematic trading strategy that buys LULD (Limit Up-Limit Down) halt resumptions on NASDAQ-listed stocks and exits at 3:50 PM ET.

## Backtest Results (realistic_backtest.py)

| Metric | Value |
|--------|-------|
| Period | Apr 2021 – May 2026 |
| Trades | 550 |
| Win rate (net of commissions) | 69.6% |
| Starting equity | $900 |
| Final equity | $35,691,470 |
| CAGR | +717% |
| Max drawdown | -13.0% |
| Total commissions | $69,608 |

> **Note:** CAGR is driven by compounding from $900 to the $100k/trade cap. At full scale ($1M account, $100k/trade), realistic annual income is $400k–$900k gross.

## Strategy Logic

1. **Signal:** NASDAQ RSS feed detects LULD halt with `LUDP` reason code
2. **Filter:** Stock must be up ≥2% from session open (halt-up) and RVOL ≥1.0
3. **Entry:** Market order at halt resumption
4. **Sizing:** 10% of BOD equity, max $100k/trade, max $300k/stock/day
5. **Exit:** Market order at 3:50 PM ET regardless of P&L

## Repository Structure

```
├── live_trader/          # Production bot (runs on server)
│   ├── main.py           # Entry point
│   ├── config.py         # All tunable parameters — edit this
│   ├── halt_monitor.py   # Polls NASDAQ RSS, detects resumptions
│   ├── signal_filter.py  # Applies halt-up + RVOL filters
│   ├── execution.py      # Places / waits for IBKR orders
│   ├── position_manager.py # Sizing, state, trade CSV logging
│   ├── eod_exit.py       # 3:50 PM exit logic
│   └── requirements.txt  # pip dependencies
│
├── TESTING/              # Research & backtesting scripts
│   ├── realistic_backtest.py      # Main backtest ($900 start, 10% sizing)
│   ├── sizing_comparison.py       # 5% vs 10% vs 25% vs 50% sizing
│   ├── pyramid_vs_single.py       # All halts vs first-halt-only comparison
│   ├── exit_liquidity.py          # Measures 3:20-3:50 PM exit window volume
│   ├── exit_volume_analysis.py    # Exit volume vs returns correlation
│   └── prehalt_volume_analysis.py # Pre-halt volume vs returns (no edge found)
│
└── docs/
    ├── server_setup.md   # How to set up the DigitalOcean server
    └── deployment.md     # How to deploy updates to the server
```

## Quick Start

### Paper trading (first time)
```bash
# On the server
cd /root/Live_Trader_Halts
source venv/bin/activate
python main.py
```

See [docs/server_setup.md](docs/server_setup.md) for full server setup including IB Gateway.

## Key Config Settings (live_trader/config.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `PAPER` | `True` | Set `False` for live trading |
| `IBKR_PORT` | `4002` | IB Gateway paper port |
| `POSITION_FRACTION` | `0.10` | 10% of BOD equity per trade |
| `MAX_POS_USD` | `100_000` | Hard cap per trade |
| `MAX_POS_PER_STOCK` | `300_000` | Max exposure per stock per day |
| `MIN_RVOL` | `1.0` | Minimum relative volume |
| `UP_MIN_MOVE` | `0.02` | Min 2% up move from session open |
| `EXIT_HOUR_ET` | `15` | Exit time: 3:50 PM ET |
| `EXIT_MINUTE_ET` | `50` | |
