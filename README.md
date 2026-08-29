# Algo Trader — Intraday Momentum Trading Bot (NSE)

Automated intraday trading bot for the Indian stock market. Scans all NSE equity stocks, picks the single best momentum stock, and holds for big gains using wide trailing stops. Uses **Angel One** for market data and **Dhan** for order execution. Includes **paper trading** mode for risk-free simulation.

> **Current operating recommendation:** keep the bot in paper mode. Production
> paper logs were positive before costs but approximately break-even/negative
> after modeled fees and slippage, while the broader production-equivalent
> backtest remained negative. Strategy experiments are recorded in shadow mode
> and do not change orders.

## Strategy

The bot follows a "fewer trades, bigger gains" approach:

1. **Enter** stocks already up 5–8% on the day with confirmed momentum
2. **Hold** through normal pullbacks using wide fixed-percentage trailing stops
3. **Exit** at 15% profit target, or via profit lock when stock hits 12%+ intraday gain
4. **Re-enter** only after PROFIT_LOCK exit, before 12:30 PM

## Daily Flow

```
09:15  Market opens. Bot logs in via auto-TOTP.
10:00  Scan window opens.
         • 4-factor market bullishness check (2-of-4 must pass).
         • Score=2 → contra-momentum mode (entry before 12:00 only, no re-entry).
         • Scan ~2500 NSE equity stocks for 5–8% intraday gainers.
         • Filter out ASM/GSM/T2T stocks.
         • Score, rank, and pick top 1 candidate.
         • Real-time entry validation (5 checks).
         • Enter with MARKET order, place SL-M at hard stop.
10:00  Monitor every 10 seconds.
  –      • Wide fixed-percentage trailing stops (4%/3.5%/3%/2.5%).
14:00    • Target exit at 15% profit from entry.
         • Profit lock at 12% intraday gain (14% for 8–10% entry stocks).
         • Smart re-entry after PROFIT_LOCK exits (up to 2 rounds).
15:05  Force-close remaining positions. Daily P&L summary via Telegram.
```

## Architecture

```
main.py (orchestrator)
├── broker/
│   ├── angelone_client.py     Angel One API — market data, candles, quotes
│   ├── dhan_client.py         Dhan API — order execution, TOTP auto-login
│   └── paper_client.py        Paper trading — simulated orders, live data
├── strategy/
│   └── market_scanner.py      4-factor bullishness model + stock scanning/scoring
├── execution/
│   ├── entry_validator.py     5 real-time entry checks
│   ├── tradability_filter.py  Read-only ASM/GSM/T2T filtering
│   └── order_manager.py       Broker-confirmed order state and fills
├── monitor/
│   ├── position_tracker.py    Persistent positions, stops, reconciliation
│   ├── risk_state.py          Persistent daily loss and sizing state
│   └── decision_journal.py    Immutable production decision snapshots
├── config/
│   ├── settings.py            .env loader
│   └── instruments.py         Stock symbol/token data structures
├── utils/
│   ├── atr.py                 ATR calculation from candles
│   ├── tick.py                NSE tick rounding (₹0.05)
│   ├── logger.py              File + console logging
│   └── telegram_alert.py      Telegram notifications
├── production_backtest.py     Point-in-time production workflow backtest
├── production_replay.py       Decision comparison and fill calibration
├── backtest.py                Legacy backtest retained for comparison
└── deploy/
    └── algo-trader.service    systemd service file for EC2
```

## Market Bullishness — 4-Factor Model

At least 2 of 4 factors must pass. Score=2 triggers contra-momentum mode.

| Factor | Condition |
|---|---|
| Index Direction | Nifty 50 change > 0% |
| Market Breadth | Advancers/Decliners > 1.2 among Nifty 50 |
| Intraday Strength | Nifty LTP > Open AND NIFTYBEES > VWAP |
| Volatility Filter | India VIX change < +5% |

**Contra-momentum mode** (score=2): entry only before 12:00 PM, no re-entry.

## Entry Validation — 5 Checks

| Check | Condition |
|---|---|
| Momentum | Gain still 5–10% from prev close |
| Range Position | Active production: top 15% of day's range (≥ 0.85) |
| Micro Breakout | LTP ≥ last completed 5-min candle high |
| Volume | Active production: newest one-minute value ≥ 1.2× prior average |
| Spread | Bid-ask spread ≤ 0.2% of price |

The shadow policy additionally evaluates range position ≥0.95, completed-minute
volume, and either two completed closes above the breakout level or a successful
retest. Its result is stored with each validation snapshot but cannot approve or
reject an actual order. This separation is intentional: historical coverage is
not yet sufficient to deploy the stricter entry policy.

## Risk Management

### Trailing Stops

| Profit from entry | Trail distance | After 2:30 PM |
|---|---|---|
| < 2% | 4.0% | 3.5% |
| 2–6% | 3.0% | 2.5% |
| 6–10% | 3.0% | 2.5% |
| > 10% | 2.5% | 2.0% |

### Profit Lock

| Entry gain | Lock threshold | Lock floor |
|---|---|---|
| 5–8% | 12% intraday | 10% |
| 8–10% | 14% intraday | 12% |

Once locked, trail tightens to 2% of highest price.

### Re-Entry Gates (all 4 must pass)

| Gate | Condition |
|---|---|
| 1 | R0 did NOT exit via HARD_STOP |
| 2 | R0 trade was profitable |
| 3 | R0 exited before 12:30 PM |
| 4 | R0 exited via PROFIT_LOCK |

### Limits

- **Hard stop**: 2% per stock (or 3×ATR, whichever is tighter)
- **Target exit**: 15% profit from entry
- **Position size**: 1% capital risk at the hard stop, capped by available buying power
- **Maximum realized daily loss**: 2% of capital, persisted across restarts
- **Max re-entry rounds**: 2
- **Re-entry cooldown**: 15 minutes

## Production Safety Controls

- Paper trading is the default. Live orders require `PAPER_TRADE=false`,
  `LIVE_TRADING_ENABLED=true`, and the exact confirmation phrase shown below.
- Submitted orders are not treated as fills. Position state changes only from
  broker-confirmed complete or partial fills.
- Positions and daily risk state persist across restarts and reconcile against
  the broker before new entries are allowed.
- Failed or uncertain exits remain tracked. Exchange protection is retained
  until a replacement exit is confirmed.
- Tradability checks use downloaded restriction lists and circuit data; they do
  not place one-share probe orders.
- Rate-limited requests use bounded retry/backoff behavior.
- Decision snapshots record market gates, quotes, candles, rankings, sizing,
  order states, fills, position decisions, and both shadow strategy outcomes.

The shadow staged-stop policy records floors at entry -1.25% after +1% MFE,
entry -0.35% after +2%, and entry +1% after +3%. It does not modify the live or
paper stop. Immediate break-even-plus-cost after +2% is not enabled because it
reduced historical net P&L.

## Historical Backtest Results (Legacy — Not Comparable)

### 4.5 Years (Jan 2022 – Jun 2026)

| Metric | Value |
|---|---|
| Total P&L | ₹61.87L |
| Trades | 773 |
| Win Rate | 67.8% |
| Profit Factor | 4.81 |
| Max Drawdown | ₹84K |

### 1 Year (Jun 2025 – Jun 2026)

| Metric | Value |
|---|---|
| Total P&L | ₹10.48L |
| Return on Capital | +1048% (₹1L × 5x leverage) |
| Trades | 159 |
| Win Rate | 63.5% |
| Profit Factor | 3.56 |
| Avg Win | +₹14,422 |
| Avg Loss | -₹7,047 |
| Max Drawdown | ₹84K |
| Profitable Months | 13 of 13 |

### Exit Reason Breakdown (1 year)

| Reason | Trades | P&L |
|---|---|---|
| PROFIT_LOCK | 53 | +₹10.69L |
| MARKET_CLOSE | 36 | +₹2.36L |
| TRAILING_STOP | 36 | +₹0.52L |
| HARD_STOP | 34 | -₹3.09L |

These legacy headline results came from a materially different candidate
selection and market-gate simulation. They are retained only for historical
comparison and are not evidence for the current production strategy.

## Production-Equivalent Backtest

Run the active point-in-time engine with:

```bash
python backtest.py
```

It mirrors production's hourly market-gate retries, cross-sectional composite
ranking, fallback validation order, risk sizing, conservative stop/target
ordering, 15-minute cooldown, and smart re-entry gates. Only bars completed
before each decision are visible, and fees plus adverse slippage are charged on
both sides.

A production-grade run requires dated universe snapshots through
`BACKTEST_POINT_IN_TIME_UNIVERSE`. Each JSON key is the snapshot's effective
date and remains active until the next snapshot:

```json
{
  "2025-01-01": [
    {"symbol": "SBIN-EQ", "token": "3045", "name": "STATE BANK OF INDIA"}
  ]
}
```

Set `BACKTEST_REQUIRE_POINT_IN_TIME_UNIVERSE=false` only for an exploratory run
using today's scrip master; such results have survivorship bias. Leave
`BACKTEST_STOCK_LIMIT=0` to scan the complete supplied universe.

`BACKTEST_SIZING_MODE=risk` uses the current stop-risk sizing. For direct
comparison with historical production logs that allocated 95% of leveraged
buying power, use `BACKTEST_SIZING_MODE=logged_production_notional`.

`BACKTEST_STAGED_STOPS=true` enables the observation-derived stop experiment:
after confirmed MFE of +1%, +2%, and +3%, the stop floors become entry -1.25%,
entry -0.35%, and entry +1.0%, respectively. A floor earned from a candle's
high applies only to later candles, so an ambiguous same-candle high/low is
handled adversely. The experiment is disabled by default and does not alter
production execution.

The command-line backtest now evaluates the stricter observation entry policy:
`BACKTEST_ENTRY_RANGE_POSITION_MIN=0.95`, completed one-minute volume, and
`BACKTEST_BREAKOUT_CONFIRMATION=persistence_or_retest`. Persistence means two
completed one-minute closes at or above the most recently completed five-minute
high. A successful retest means a prior close broke that level and a later
completed candle touched within `BACKTEST_BREAKOUT_RETEST_TOLERANCE_PCT` before
closing back above it. Five-minute-only runs fail these checks rather than
silently substituting partial or lower-resolution volume.

For an explicit old-validator baseline, set the range threshold to `0.85`, set
`BACKTEST_COMPLETED_MINUTE_VOLUME=false`, and use breakout mode `production`.
Test `BACKTEST_BREAK_EVEN_PLUS_COST=true` separately from staged protection; it
raises the stop after +2% confirmed MFE to the price required to cover modeled
fees and adverse exit slippage. Neither protection experiment changes live
execution.

Historical OHLCV does not contain live order-book buy pressure or bid/ask
spread. The report discloses a neutral buy-pressure input and the configured
assumed spread. The default uses `BACKTEST_INTRADAY_INTERVAL=ONE_MINUTE` and
`BACKTEST_SCAN_STEP_MINUTES=2`; results still cannot reproduce ticks inside
each minute. Use `FIVE_MINUTE` and a 5-minute step only as a faster approximation.
Historical candles also cannot reconstruct partial current-minute volume or the
exact quote observed while a live scan was running. The replay does not inject
transient production API failures, because doing that would fit operational
accidents rather than the strategy.

The historical engine uses completed five-minute candles for entry ATR, just as
production does, and fills at the first bar on or after
`BACKTEST_ENTRY_DELAY_MINUTES` (default one minute) so pre-fill price movement
cannot trigger an impossible exit. Set `BACKTEST_SLIPPAGE_BPS=auto` to consume
the newest confirmed-fill calibration generated by `production_replay.py`.
Every run also writes `run_manifest.json` beside the CSV reports with the full
configuration, date range, universe hash, slippage source, fidelity limitations,
and a deterministic run fingerprint.

New universe snapshots include the contemporaneous ASM/GSM and F&O membership.
The backtest applies those dated restrictions before validation. Older snapshots
without those fields remain usable but are explicitly marked incomplete in the
run manifest.

### Production decision snapshots and replay

Production writes an append-only SQLite decision journal by default. It records
the market gate, complete scan quote batches, candidate rankings, live
validation quotes and candles, order-book depth, risk sizing, broker order
states, confirmed fills, position decisions, and reconciliation outcomes.
Credential-shaped fields are redacted before persistence.

Dated universe snapshots are also written automatically. At the end of each
trading day the bot compares replayable decisions with the recorded outcomes,
exports the universe format consumed by the backtester, and calculates
confirmed-fill slippage statistics.

Run the comparison manually at any time with:

```bash
python production_replay.py \
  --db data/decision_journal.sqlite3 \
  --output-dir data/replay_reports/manual
```

The generated `summary.json` contains the decision match rate. The slippage
report recommends `BACKTEST_SLIPPAGE_BPS` from the 75th percentile of adverse
broker-confirmed fills; it never treats submitted or timed-out orders as fills.
Set `BACKTEST_POINT_IN_TIME_UNIVERSE=data/universe_snapshots.json` for subsequent
production-equivalent backtests.

## Configuration

Copy `.env.example` to `.env` and fill in credentials:

```env
BROKER=dhan                    # "angelone" or "dhan"
PAPER_TRADE=true               # Simulated orders with live data

# Angel One (always required — market data)
ANGELONE_API_KEY=
ANGELONE_CLIENT_ID=
ANGELONE_PIN=
ANGELONE_TOTP_SECRET=

# Dhan (required when BROKER=dhan)
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
DHAN_PIN=
DHAN_TOTP_SECRET=

# Capital
CAPITAL=100000
INTRADAY_LEVERAGE=5.0

# Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## Deployment (AWS EC2)

Recommended: **t3.micro** in **ap-south-1** (Mumbai), Ubuntu 24.04 LTS.

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
git clone <repo-url> && cd algo-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
sudo cp deploy/algo-trader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable algo-trader
sudo systemctl start algo-trader
```

```bash
sudo systemctl status algo-trader       # Status
sudo journalctl -u algo-trader -f       # Live logs
sudo systemctl restart algo-trader      # Restart
```

## Dependencies

```
smartapi-python    # Angel One SmartAPI
dhanhq             # Dhan broker + TOTP login
requests           # HTTP client
pyotp              # TOTP generation
numpy              # EMA calculation
```

## Configuration

Copy `.env.example` to the ignored local `.env` file and insert newly rotated credentials there. Never commit credentials, PINs, TOTP seeds, access tokens, or Telegram tokens.

Live orders require all three settings:

```dotenv
PAPER_TRADE=false
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS
```
