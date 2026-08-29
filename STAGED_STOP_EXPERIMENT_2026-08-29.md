# Staged-Stop Replay Experiment — 2026-08-29

## Decision

The observation-derived stop fixture is implemented in the production-equivalent
backtest, disabled by default. It has not been enabled in production. The first
offline replay is encouraging, but it is in-sample and is not sufficient for a
live rollout.

## Fixture

| Confirmed MFE | Stop floor |
|---|---:|
| Below +1% | Existing stop logic |
| At least +1% | Entry -1.25% |
| At least +2% | Entry -0.35% |
| At least +3% | Entry +1.00% |

Only the high of a completed prior candle can earn a new floor. If one candle
touches both a new stage and the old active stop, the stop is assumed to occur
first. This deliberately avoids favourable look-ahead inside an OHLC candle.

Enable the fixture for a backtest with:

```bash
BACKTEST_STAGED_STOPS=true python backtest.py
```

## Historical path comparison

The comparison overlays the fixture on the cached intraday paths for completed
production-paper trades. It uses the actual logged entry and exit fills,
completed candles, adverse intrabar ordering, and 5 bps of adverse slippage for
a replacement staged-stop exit.

| Metric | Logged-path baseline | Staged fixture | Change |
|---|---:|---:|---:|
| Covered completed trades | 44 of 49 | 44 of 49 | — |
| Gross P&L | Rs.24,917.83 | Rs.57,243.35 | +Rs.32,325.52 |
| Net P&L at 10 bps/side fees | -Rs.15,271.96 | Rs.17,021.23 | +Rs.32,293.19 |
| Covered winners changed | — | 0 | — |
| Covered losers changed | — | 7 | — |

Trades changed by the fixture:

| Date | Symbol | Logged gross P&L | Staged gross P&L | Change |
|---|---|---:|---:|---:|
| 2026-06-16 | LALPATHLAB | -Rs.8,424.00 | -Rs.1,896.92 | +Rs.6,527.08 |
| 2026-06-18 | CDSL | -Rs.6,835.90 | -Rs.1,898.53 | +Rs.4,937.37 |
| 2026-06-24 | HUBTOWN | -Rs.9,272.16 | -Rs.6,174.63 | +Rs.3,097.53 |
| 2026-07-07 | SAKSOFT | -Rs.3,187.14 | Rs.4,501.22 | +Rs.7,688.36 |
| 2026-07-10 | APOLLOPIPE | -Rs.4,116.25 | -Rs.1,892.09 | +Rs.2,224.16 |
| 2026-07-29 | DEVYANI | -Rs.715.86 | Rs.4,531.55 | +Rs.5,247.41 |
| 2026-08-07 | COSMOFIRST | -Rs.8,772.00 | -Rs.6,168.39 | +Rs.2,603.61 |

## Limitations

- Five completed trades lack a usable cached path and are excluded.
- Most paths are five-minute candles; this cannot resolve tick order within a
  candle or model stop latency precisely.
- This is an overlay on actual logged trade paths, not an independent recreation
  of the selection process and every production decision.
- The rule was derived from this same trade sample, so the result is in-sample
  and vulnerable to overfitting.
- The attempted full-period run was stopped after the broker historical-data
  endpoint returned repeated HTTP 403/rate-limit responses. Running against
  today's universe would also introduce survivorship bias.

## Rollout criteria

1. Run the complete engine with dated point-in-time universes and locally cached
   one-minute data; compare the baseline and staged configurations over the same
   immutable dataset.
2. Replay newly captured production decisions and confirmed fills without
   changing the thresholds. Treat these later trades as out-of-sample evidence.
3. Compare net expectancy, drawdown, profit factor, winner retention, stop gaps,
   partial fills, and failed/replaced exit orders.
4. Keep the setting in paper mode until it improves out-of-sample results and
   restart/reconciliation tests confirm that protection is never removed before
   its replacement is broker-confirmed.
5. Change the production tracker only through a separate reviewed rollout; this
   experiment must not silently become a live-trading rule.
