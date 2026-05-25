# Server Setup Guide

## Server Details
- **Provider:** DigitalOcean
- **IP:** 157.230.2.149
- **User:** root
- **Location:** /root/Live_Trader_Halts/

## First-Time Setup

### 1. SSH into the server
```bash
ssh root@157.230.2.149
```

### 2. Clone the repository
```bash
cd /root
git clone https://github.com/YOUR_USERNAME/luld-halt-trader.git Live_Trader_Halts
cd Live_Trader_Halts
```

### 3. Create Python virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r live_trader/requirements.txt
```

### 4. Set up IB Gateway

IB Gateway must be running for the bot to connect.

**Install IB Gateway:**
```bash
# Download IB Gateway installer
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh
```

**Set up virtual display (headless):**
```bash
apt-get install -y xvfb x11vnc openbox
Xvfb :1 -screen 0 1024x768x24 &
DISPLAY=:1 openbox &
```

**Start IB Gateway:**
```bash
DISPLAY=:1 /opt/ibgateway/ibgateway &
```

**Connect via VNC to log in (first time only):**
```bash
# On server — start VNC
x11vnc -display :1 -storepasswd   # set a password
x11vnc -display :1 -rfbauth ~/.vnc/passwd -rfbport 5900 -bg -quiet

# On your local machine — connect with TigerVNC to 157.230.2.149:5900
# Log into IB Gateway with your credentials
# Enable API: Configure -> Settings -> API -> Enable ActiveX and Socket Clients
#             Socket port: 4002, uncheck Read-Only API
```

**Auto-start IB Gateway (systemd):**
Create `/etc/systemd/system/ibgateway.service`:
```ini
[Unit]
Description=IB Gateway
After=network.target

[Service]
User=root
Environment=DISPLAY=:1
ExecStartPre=/usr/bin/Xvfb :1 -screen 0 1024x768x24
ExecStart=/opt/ibgateway/ibgateway
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### 5. Set up the bot as a systemd service

Create `/etc/systemd/system/halt-trader.service`:
```ini
[Unit]
Description=LULD Halt Trader
After=network.target

[Service]
User=root
WorkingDirectory=/root/Live_Trader_Halts/live_trader
ExecStart=/root/Live_Trader_Halts/venv/bin/python main.py
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
systemctl daemon-reload
systemctl enable halt-trader
systemctl start halt-trader
```

### 6. Monitor the bot
```bash
# Live logs
journalctl -u halt-trader -f

# Or tail the log file directly
tail -f /root/Live_Trader_Halts/live_trader/trader.log

# Check status
systemctl status halt-trader

# View today's trades
cat /root/Live_Trader_Halts/live_trader/trades.csv
```

## Paper vs Live Trading

In `live_trader/config.py`:
```python
PAPER = True   # paper trading on port 4002
PAPER = False  # live trading on port 4001
```

**Never set PAPER = False until you have:**
- [ ] 3+ months of paper trading results matching backtest expectations
- [ ] Win rate above 55% on live fills
- [ ] Confirmed fills are within 2% of expected prices
- [ ] IB Gateway connected to your LIVE account (not paper)
