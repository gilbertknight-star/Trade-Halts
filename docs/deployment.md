# Deployment Guide

How to push code changes from your local machine to the server.

## Workflow Overview

```
Local machine (Windows)          Server (DigitalOcean)
C:\Trade Halts\                  /root/Live_Trader_Halts/
      |                                    |
      |-- git commit                       |
      |-- git push --> GitHub --> git pull-|
```

## Making and Deploying a Change

### 1. Edit files locally
Make your changes in `C:\Trade Halts\live_trader\` on your Windows machine.

### 2. Commit locally
Open PowerShell or Git Bash in `C:\Trade Halts\`:
```bash
git add live_trader/config.py          # add specific changed files
git add live_trader/signal_filter.py   # never use git add -A blindly
git commit -m "Increase MIN_RVOL to 1.5 based on paper trading results"
git push origin main
```

### 3. Deploy to server
```bash
# SSH into server
ssh root@157.230.2.149

# Pull latest changes
cd /root/Live_Trader_Halts
git pull origin main

# Restart the bot to pick up changes
systemctl restart halt-trader

# Confirm it started cleanly
journalctl -u halt-trader -n 30
```

## Common Config Changes

### Change position sizing
Edit `live_trader/config.py`:
```python
POSITION_FRACTION = 0.10   # 10% -> change this
MAX_POS_USD       = 100_000 # hard cap -> change this
```

### Tighten RVOL filter
```python
MIN_RVOL = 1.5   # was 1.0 — only take higher-conviction halts
```

### Switch to live trading
```python
PAPER = False    # WARNING: uses real money
```
Then restart the bot AND make sure IB Gateway is connected to your live account.

### Change exit time
```python
EXIT_HOUR_ET   = 15
EXIT_MINUTE_ET = 45   # exit 5 minutes earlier
```

## Checking Results

### View today's trades
```bash
ssh root@157.230.2.149
cat /root/Live_Trader_Halts/live_trader/trades.csv
```

### Download trades CSV to local machine
From your local machine:
```bash
scp root@157.230.2.149:/root/Live_Trader_Halts/live_trader/trades.csv "C:\Trade Halts\live_results\trades_live.csv"
```

### Compare live vs backtest
Once you have `trades_live.csv`, run the comparison notebook/script (TBD).

## Emergency: Stop the Bot Immediately
```bash
ssh root@157.230.2.149
systemctl stop halt-trader
```

Or if you can't SSH, reboot the server from the DigitalOcean dashboard.

## Check Bot is Running During Market Hours
```bash
ssh root@157.230.2.149
systemctl status halt-trader
journalctl -u halt-trader --since "today" | grep -E "PASS|SKIP|filled|ERROR"
```

## Updating Python Dependencies
If `requirements.txt` changes:
```bash
ssh root@157.230.2.149
cd /root/Live_Trader_Halts
git pull
source venv/bin/activate
pip install -r live_trader/requirements.txt
systemctl restart halt-trader
```
