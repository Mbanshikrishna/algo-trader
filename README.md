# algo-trader

Automated **intraday trading bot** for Indian markets using the **Zerodha Kite API**, with support for running continuously on an **AWS EC2** instance.

## Project Overview

This repository is structured as a modular event-driven trading bot:

- `data/market_stream.py` – consumes live market ticks/candles.
- `strategy/momentum_strategy.py` – generates buy/sell signals.
- `execution/order_manager.py` – places and manages orders.
- `risk/risk_manager.py` – enforces position sizing and limits.
- `monitor/position_tracker.py` – tracks open positions/PnL.
- `db/trade_db.py` – persists trade and bot state.
- `utils/telegram_alert.py` – sends alerts/notifications.
- `main.py` – application entrypoint.

## Zerodha API Setup

1. Create a Kite Connect app from your Zerodha developer account.
2. Collect:
   - `KITE_API_KEY`
   - `KITE_API_SECRET`
3. Implement a secure token flow:
   - Generate `request_token` via login.
   - Exchange for `access_token`.
   - Store token securely (never commit secrets to git).
4. Configure keys/tokens through environment variables or a secure secrets manager.

## AWS EC2 Deployment (Recommended Baseline)

### 1) Launch server

- Use Ubuntu 22.04 LTS (or latest stable).
- Instance type for small setups: `t3.small` / `t3.medium`.
- Attach IAM role (if using AWS services like SSM/CloudWatch/Secrets Manager).

### 2) Install dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

### 3) Clone and configure

```bash
git clone <your-repo-url>
cd algo-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # if present
```

### 4) Configure environment

Create a `.env` (or equivalent secure config):

```env
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 5) Run bot as a service

Use `systemd` so the bot auto-starts and restarts on failure.

Example unit file `/etc/systemd/system/algo-trader.service`:

```ini
[Unit]
Description=Algo Trader Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/algo-trader
Environment="PATH=/home/ubuntu/algo-trader/.venv/bin"
ExecStart=/home/ubuntu/algo-trader/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader
sudo systemctl status algo-trader
```

## Production Safety Checklist

- Add strict stop-loss and max daily loss guardrails.
- Enforce max concurrent positions and exposure per symbol.
- Handle API/network retries with cooldown.
- Persist state so reboot does not lose positions/orders context.
- Add market-hours checks and holiday calendar handling.
- Send Telegram alerts for order failures and risk breaches.
- Log every signal, order request/response, and risk decision.

## Important Notes

- Run in **paper/sandbox mode first** until strategy behavior is stable.
- Keep AWS security groups restrictive (SSH only from trusted IP).
- Rotate API tokens and secrets regularly.
- Ensure compliance with broker/exchange rules and your local regulations.

## Disclaimer

This project is for educational purposes only and does not constitute financial advice. Live trading involves substantial risk.
