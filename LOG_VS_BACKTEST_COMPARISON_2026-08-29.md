# Production Log vs Backtest Comparison — 2026-08-29

## Scope

The latest remote production log contains 49 completed trades through
2026-08-28. The common reproducible comparison period is 2026-06-02 through
2026-08-20 because the local historical cache ends on August 20.

The offline backtest used:

- 57 cached market sessions and an average of 145.2 cached stock paths per
  session.
- The notional sizing observed in the production logs: 95% of leveraged buying
  power.
- 10 bps fees per side and 5 bps adverse slippage per side.
- Five-minute candles, next-candle entry, and adverse same-candle ordering.
- The current security universe, because dated point-in-time universe snapshots
  are not available for this period.

No broker requests were needed for this run.

## Headline comparison

| Dataset / configuration | Trades | Wins | Losses | Win rate | Net P&L | Profit factor | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|
| Production paper log, reported gross | 45 | 19 | 26 | 42.2% | +Rs.58,961.51 | 1.344 | Rs.46,846.51 |
| Production log after modeled 10 bps fees + 5 bps slippage per side | 45 | 17 | 28 | 37.8% | **-Rs.2,798.87** | 0.987 | Rs.75,011.22 |
| Backtest baseline: range 0.85 | 47 | 15 | 32 | 31.9% | **-Rs.106,603.89** | 0.591 | Rs.153,465.70 |
| Backtest: range 0.95 only | 42 | 15 | 27 | 35.7% | **-Rs.41,352.53** | 0.829 | Rs.105,032.09 |
| Backtest: staged stops only | 47 | 14 | 33 | 29.8% | **-Rs.103,384.92** | 0.593 | Rs.150,246.73 |
| Backtest: range 0.95 + staged stops | 42 | 14 | 28 | 33.3% | **-Rs.44,648.94** | 0.812 | Rs.108,328.50 |

The stricter range filter improves the backtest by Rs.65,251.36 relative to the
baseline and reduces drawdown by Rs.48,433.61, but the resulting strategy is
still loss-making after costs.

Staged protection alone improves the baseline by only Rs.3,218.97. When added
to the range filter, it makes net P&L Rs.3,296.41 worse than range 0.95 alone.
This differs from the favourable logged-path overlay and demonstrates that the
earlier stop result does not generalize when candidate selection changes.

## Production-match quality

The baseline selected the same symbol on the same date as production for only
10 of 45 logged trades. The range-plus-staged version matched only 5 of 45.
Daily P&L correlation with the log was 0.383 for the baseline and 0.397 for the
combined candidate.

Matched baseline trades:

| Date | Symbol | Production gross P&L | Baseline backtest net P&L |
|---|---|---:|---:|
| 2026-06-09 | THOMASCOOK | -Rs.9,817.20 | -Rs.10,688.64 |
| 2026-06-17 | KIRLPNU | +Rs.26,792.00 | +Rs.26,697.72 |
| 2026-06-18 | CDSL | -Rs.6,835.90 | -Rs.10,784.66 |
| 2026-06-24 | HUBTOWN | -Rs.9,272.16 | -Rs.10,630.05 |
| 2026-07-13 | ROUTE | +Rs.4,726.50 | +Rs.3,313.81 |
| 2026-07-14 | STYL | +Rs.3,310.20 | +Rs.3,652.16 |
| 2026-07-15 | LTTS | -Rs.856.80 | -Rs.2,107.52 |
| 2026-08-03 | GODFRYPHLP | -Rs.483.00 | -Rs.791.85 |
| 2026-08-05 | AGARWALEYE | +Rs.13,877.60 | +Rs.9,646.55 |
| 2026-08-20 | SOLARA | +Rs.37,116.30 | +Rs.30,773.21 |

The close KIRLPNU result shows that execution simulation can agree when the
same trade is selected. The low overall symbol/date overlap shows that candidate
selection remains the dominant fidelity problem.

## Rules that could not be fully tested

Completed-minute volume and breakout persistence/retest require one-minute
paths at the original decision time. Only one of the 45 comparable logged
trades has that cached input; SOLARA passed both checks. The complete cached
period therefore cannot provide a winner-versus-loser validation for these
rules. They must remain paper/backtest-only until decision snapshots provide a
meaningful sample.

The strict engine deliberately rejects five-minute data for these checks. This
prevents the backtest from presenting a fabricated high-fidelity result.

## Conclusion

The production paper result is not yet profitable after modeled trading costs,
and the backtest is not yet a faithful reproduction of production selection.
Range position 0.95 is the most promising change in the broad cached run, but
it is not ready for live deployment. The staged stop is also not robust across
the two evaluation methods, and immediate break-even-plus-cost after +2% was
already shown to reduce net P&L.

Before deployment, rerun the same frozen configurations using dated universes,
complete one-minute candidate paths, captured partial-minute decisions, and
broker-confirmed fills. Do not tune thresholds again on these same 45 trades.
