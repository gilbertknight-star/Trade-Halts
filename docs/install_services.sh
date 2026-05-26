#!/bin/bash
# install_services.sh
# Sets up all systemd services for the LULD halt trader stack.
# Run once on a fresh server: bash /root/Live_Trader_Halts/docs/install_services.sh

set -e
echo "=== Installing LULD Halt Trader service stack ==="

# ── 1. xvfb.service — virtual display ────────────────────────────────────────
cat > /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=Virtual Display (Xvfb :1)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :1 -screen 0 1280x800x24
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "  xvfb.service created"

# ── 2. display-stack.service — openbox + x11vnc on top of Xvfb ───────────────
cat > /etc/systemd/system/display-stack.service << 'EOF'
[Unit]
Description=Display Stack (openbox + x11vnc on :1)
After=xvfb.service
Requires=xvfb.service

[Service]
Type=forking
Environment=DISPLAY=:1
ExecStartPre=/bin/sleep 2
ExecStartPre=/usr/bin/openbox --startup /bin/true
ExecStart=/usr/bin/x11vnc -display :1 -rfbauth /root/.vnc/passwd \
          -rfbport 5900 -forever -bg -quiet -o /var/log/x11vnc.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "  display-stack.service created"

# ── 3. ibgateway.service — IB Gateway via IBC (auto-login) ───────────────────
cat > /etc/systemd/system/ibgateway.service << 'EOF'
[Unit]
Description=IB Gateway (auto-login via IBC)
After=display-stack.service
Requires=display-stack.service

[Service]
Type=simple
Environment=DISPLAY=:1
# Wait for display stack to be fully ready
ExecStartPre=/bin/sleep 5
ExecStart=/opt/ibc/scripts/ibcstart.sh \
    "" \
    "/opt/ibgateway" \
    "" \
    "IBC" \
    "/opt/ibc" \
    "/opt/ibc/config_live.ini" \
    "paper"
Restart=on-failure
RestartSec=60
# Give IB Gateway 3 minutes to log in before considering it failed
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOF
echo "  ibgateway.service created"

# ── 4. halt-trader.service — the trading bot ─────────────────────────────────
cat > /etc/systemd/system/halt-trader.service << 'EOF'
[Unit]
Description=LULD Halt Trader
After=ibgateway.service
# Note: we don't Requires= ibgateway because the bot handles reconnection
# internally — it will retry IBKR connection every 60s

[Service]
User=root
WorkingDirectory=/root/Live_Trader_Halts/bot
ExecStart=/root/Live_Trader_Halts/venv/bin/python main.py
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
echo "  halt-trader.service created"

# ── 5. Enable all services ────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable xvfb display-stack ibgateway halt-trader
echo ""
echo "=== Services enabled. Starting now... ==="

systemctl start xvfb
sleep 3
systemctl start display-stack
sleep 3

echo ""
echo "=== Status ==="
systemctl status xvfb --no-pager -l | tail -5
systemctl status display-stack --no-pager -l | tail -5

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "  1. Make sure /opt/ibc/config_live.ini has your IBKR credentials"
echo "  2. Start IB Gateway:  systemctl start ibgateway"
echo "  3. Start the bot:     systemctl start halt-trader"
echo ""
echo "VNC is now available at 157.230.2.149:5900"
echo "It will auto-start on every server reboot."
echo ""
echo "Monitor everything:"
echo "  systemctl status xvfb display-stack ibgateway halt-trader"
echo "  journalctl -u halt-trader -f"
