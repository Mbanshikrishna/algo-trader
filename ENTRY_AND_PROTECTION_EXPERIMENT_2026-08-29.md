# Entry and Protection Experiments — 2026-08-29

## Status

The requested rules are implemented in the backtest, not in live production.
The command-line backtest uses the strict entry candidate by default. Both exit
protection variants remain opt-in so they can be run separately on identical
data.

## Implemented entry candidate

- Require range position of at least 0.95.
- Use only a fully completed one-minute candle for volume confirmation.
- Require either two completed one-minute closes above the latest completed
  five-minute breakout high, or a successful retest of that level.
- Reject five-minute-only input when completed-minute confirmation is required;
  the engine does not silently use a partial or five-minute volume substitute.

The legacy entry rules remain available for a controlled baseline by setting:

```bash
BACKTEST_ENTRY_RANGE_POSITION_MIN=0.85
BACKTEST_COMPLETED_MINUTE_VOLUME=false
BACKTEST_BREAKOUT_CONFIRMATION=production
```

## Range-position validation on production trades

Of the 49 completed production-paper trades, 45 have both a recorded entry
range position and paired entry/exit fills. This is an accepted-trade
counterfactual; it cannot model which later candidate would have replaced a
rejected entry.

| Cohort | Covered | Pass >=0.95 | Rejected | Rejected gross P&L |
|---|---:|---:|---:|---:|
| Historical winners | 19 | 14 | 5 | +Rs.54,151.30 |
| Historical losers | 26 | 14 | 12 | -Rs.76,878.13 |
| Total | 45 | 28 | 17 | -Rs.22,726.83 |

On this limited counterfactual, rejecting the 17 trades would improve gross P&L
by Rs.22,726.83 and raise the retained win rate from 42.2% to 50.0%. However,
the rejected winners include SOLARA (+Rs.37,116.30), MOTILALOFS, RKFORGE,
HYUNDAI, and HEXT. The result therefore supports further testing, not automatic
live deployment.

## Completed-volume and breakout validation coverage

Only one of the 45 comparable historical trades has the required cached
one-minute entry path. That trade, SOLARA, passes both completed-minute volume
and persistence/retest confirmation. One winner and zero losers is not a valid
comparison sample, so these two entry rules remain unvalidated historically.

Production decision snapshots now capture the inputs needed to grow this
sample. The thresholds must remain unchanged while new paper trades accumulate,
otherwise the evaluation becomes repeatedly fitted to the same observations.

## +2% break-even-plus-cost test

The +2% rule raises the raw stop enough to cover the configured 10 bps fee on
both sides and 5 bps adverse exit slippage. A floor earned from a candle high
applies only to the next candle.

Replay coverage is 44 of 49 completed trades:

| Cohort | Baseline net P&L | +2% cost-floor net P&L | Change | Trades changed |
|---|---:|---:|---:|---:|
| Winners | Rs.180,204.23 | Rs.140,177.09 | -Rs.40,027.14 | 2 |
| Losers | -Rs.195,476.19 | -Rs.161,795.51 | +Rs.33,680.68 | 7 |
| Total | -Rs.15,271.96 | -Rs.21,618.41 | **-Rs.6,346.46** | 9 |

The rule saves money on seven losers but gives up more on two winners. SOLARA
accounts for approximately Rs.36,126 of the lost winner P&L, and RKFORGE for
approximately Rs.3,901. Immediate break-even-plus-cost at +2% should therefore
not be deployed.

Run this variant with:

```bash
BACKTEST_BREAK_EVEN_PLUS_COST=true BACKTEST_STAGED_STOPS=false python backtest.py
```

## Staged-protection test

The staged candidate begins at +1% MFE but keeps the floor below entry until a
confirmed +3% move:

| Confirmed MFE | Stop floor |
|---|---:|
| +1% | Entry -1.25% |
| +2% | Entry -0.35% |
| +3% | Entry +1.00% |

The existing 44-trade replay improved modeled net P&L from -Rs.15,271.96 to
Rs.17,021.23, a change of +Rs.32,293.19. Seven covered losers improved and no
covered winner changed. This remains in-sample and must be verified on later
paper trades.

Run this variant with:

```bash
BACKTEST_STAGED_STOPS=true BACKTEST_BREAK_EVEN_PLUS_COST=false python backtest.py
```

## Deployment decision

| Change | Current decision |
|---|---|
| Range position >=0.95 | Promising; validate in a full point-in-time run and paper shadow mode |
| Completed-minute volume | Correctly implemented; insufficient winner/loser coverage |
| Breakout persistence/retest | Correctly implemented; insufficient winner/loser coverage |
| Break-even-plus-cost after +2% | Reject at present; reduced net P&L |
| Staged protection from +1% | Best current candidate; keep paper/backtest-only pending out-of-sample evidence |

No live execution rule should change until the entry filters have meaningful
coverage among both winners and losers and the staged exit succeeds on an
untouched out-of-sample period with fees, slippage, gaps, and broker-confirmed
fills.
