# Server Setup Guide

## Server Details
- **Provider:** DigitalOcean
- **IP:** 157.230.2.149
- **User:** root
- **Bot location:** /root/Live_Trader_Halts/

## Architecture

```
systemd boot
  └── xvfb.service          (virtual display :1)
       └── display-stack.service  (openbox + x11vnc on top of display)
            └── ibgateway.service      (IB Gateway, auto-login via IBC)
                 └── halt-trader.service    (trading bot)
```

IB Gateway logs in automatically via IBC — VNC is only needed for
occasional maintenance, not daily use.

---

## First-Time Full Setup

### 1. Clone repo and install bot dependencies
```bash
cd /root
git clone https://github.com/gilbertknight-star/Trade-Halts.git Live_Trader_Halts
cd Live_Trader_Halts
python3 -m venv venv
source venv/bin/activate
pip install -r bot/requirements.txt
```

### 2. Install system packages
```bash
apt-get update
apt-get install -y xvfb x11vnc openbox xdotool wget unzip
```

### 3. Set VNC password (one time only)
```bash
mkdir -p ~/.vnc
x11vnc -storepasswd ~/.vnc/passwd
```

### 4. Install IBC (automates IB Gateway login)
```bash
cd /root
wget -q https://github.com/IbcAlpha/IBC/releases/download/3.18.0/IBCLinux-3.18.0.zip
unzip -q IBCLinux-3.18.0.zip -d /opt/ibc
chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh
```

### 5. Configure IBC with your credentials
```bash
cp /opt/ibc/config.ini /opt/ibc/config_live.ini
nano /opt/ibc/config_live.ini
```

Key settings to change:
```ini
IbLoginId=YOUR_IBKR_USERNAME
IbPassword=YOUR_IBKR_PASSWORD
TradingMode=paper          # change to 'live' when ready
FIX=no
```

### 6. Create all systemd services
```bash
bash /root/Live_Trader_Halts/docs/install_services.sh
```

---

## Reconnecting After Restarting Your PC

VNC and IB Gateway stay running on the server — your PC restarting
doesn't affect them. Just open TigerVNC and connect to:
```
157.230.2.149:5900
```
Password: whatever you set in step 3.

---

## Daily Monitoring

```bash
# Check everything is running
systemctl status xvfb display-stack ibgateway halt-trader

# Live bot log
journalctl -u halt-trader -f

# Today's trades
cat /root/Live_Trader_Halts/bot/trades.csv
```

## Deploying Code Updates

```bash
cd /root/Live_Trader_Halts
git pull
systemctl restart halt-trader
journalctl -u halt-trader -n 20
```
