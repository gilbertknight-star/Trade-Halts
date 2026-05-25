# LULD Halt Resumption Live Trader

## Setup on DigitalOcean

```bash
# 1. Update server
sudo apt update && sudo apt upgrade -y

# 2. Install Python and dependencies
sudo apt install -y python3-pip python3-venv

# 3. Clone/upload this folder to the server
# e.g. scp -r live_trader/ root@YOUR_IP:~/live_trader

# 4. Create virtual environment
cd ~/live_trader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Download and install IBKR Gateway (Linux)
# Download from: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
# Run the installer: sh ibgateway-stable-standalone-linux-x64.sh

# 6. Configure Gateway
# - Enable API: File → Global Config → API → Settings → Enable ActiveX and Socket Clients
# - Socket port: 7497 (paper) or 7496 (live)
# - Uncheck "Read-Only API"
# - Check "Allow connections from localhost only"

# 7. Run Gateway headlessly using Xvfb
sudo apt install -y xvfb
Xvfb :1 -screen 0 1024x768x24 &
export DISPLAY=:1
~/Jts/ibgateway/1019/ibgateway &

# 8. Run the trader
python main.py
```

## Running persistently with screen

```bash
# Start a screen session so it keeps running after you disconnect
screen -S trader
source venv/bin/activate
python main.py

# Detach: Ctrl+A then D
# Reattach later: screen -r trader
```

## Config

All parameters are in `config.py`:
- `PAPER = True` for paper trading, `False` for live
- `POSITION_FRACTION = 0.05` — 5% of SOD equity per trade
- `VOLUME_CAP_FRAC = 0.10` — max 10% of first-minute volume
- `EXIT_HOUR_ET / EXIT_MINUTE_ET` — EOD exit time (default 3:58pm)

## Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, trading day loop |
| `halt_monitor.py` | Polls NASDAQ RSS, detects LULD resumptions |
| `signal_filter.py` | Price/direction filters |
| `execution.py` | IBKR order placement |
| `position_manager.py` | SOD equity, sizing, position tracking |
| `eod_exit.py` | 3:58pm market close of all positions |
| `config.py` | All parameters |
| `state.json` | Auto-generated, persists open positions |
| `trader.log` | Auto-generated, full trade log |
