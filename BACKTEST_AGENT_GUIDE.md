# BTC 0DTE Backtest — Agent Reference Guide

This document captures all decisions, nuances, bugs fixed, and conventions used across the BTC 0DTE backtesting project. Refer to this before running any new backtests to ensure consistency.

---

## 1. Data Source

### Files
| File | Path | Contents |
|------|------|----------|
| `btc_0dte_data.parquet` | `data/` | Historical data, Deribit only. ~21.9M rows. |
| `btc_0dte_data_2026.parquet` | `data/` | 2026 data. Multi-exchange: Deribit (2.2M), Bybit (2.8M), OKX (2.2M), Binance (2.0M). |

### Key Data Fields
`datetime`, `expiry_date`, `strike`, `call_put`, `spot_price`, `premium` (or `mark_price`), `daystogo`, `exchange`

### Premium Handling (CRITICAL)
- **File 1**: Column is `mark_price`, denominated in **BTC**. Converted to USD via: `premium_usd = mark_price * spot_price`.
- **File 2**: Column is `premium`. For **Bybit/Binance**, premium is already in USD. For **Deribit/OKX**, premium is in BTC and must be converted: `premium_usd = premium * spot_price`.

### Overlap Removal
File 2 contains data overlapping with File 1. All rows in File 2 with `datetime <= 2026-01-22 UTC` are dropped before concatenation.

### Exchange Filter
All recent backtests use **Deribit only**: `df = df[df["exchange"] == "deribit"]`. This drops ~7M non-Deribit rows. The load function prints: `Exchange filter 'deribit': 31,098,042 → 24,139,062 rows`.

### Data Cleaning
- `daystogo <= 1.0` (0DTE only)
- `premium_usd > 0`
- `spot_price > $10,000` (drops corrupt rows)
- NaN values in `premium_usd`, `strike`, `spot_price` are dropped

### Date Range
`2024-01-01` to `2026-03-25` (hardcoded in `0dte_straddle_leveraged.py` as `START_DATE` / `END_DATE`).

### Deribit Expiry Time
Deribit options expire at **08:00 UTC** daily. This affects the `_get_expiry_str` function and explains why timing windows starting at 08:00 may have limited data.

---

## 2. Strategies

### 2a. Synthetic Straddle (Perp or Spot)
**Logic**: Long BTC (via perp/spot with leverage) + Long 2 ITM Puts.

| Variant | Leverage | QTY per unit | Script (engine) |
|---------|----------|-------------|-----------------|
| Perp 25x/50x | 25x or 50x | 1.0 BTC | `0dte_straddle_leveraged.py` |
| Spot 10x | 10x | 0.5 BTC | `0dte_straddle_leveraged.py` (same engine, QTY=0.5) |

**Cost per straddle**: `spot_margin + 2 * put_premium_usd`
- `spot_margin = QTY * spot_price / leverage`
- `put_cost = NUM_PUTS * QTY * put_premium_usd`

### 2b. Pure Straddle (Options Only)
**Logic**: Long 1 ATM Call + Long 1 ATM Put (same strike, same expiry). No spot/perp leg.

**Cost per straddle**: `call_premium_usd + put_premium_usd`

Script: `run_pure_straddle_interval_detailed.py` (self-contained engine, does not import `0dte_straddle_leveraged.py`).

---

## 3. Strike Selection (IMPORTANT)

### Synthetic Straddle — `_find_itm_put(snap, spot)`
Selects the **lowest strike ABOVE spot** (nearest ITM put).

```python
itm = snap[snap["strike"] > spot]
min_strike = itm["strike"].min()
```

Example: If spot = $95,000, and available strikes are $94,000, $95,000, $96,000 → selects **$96,000**.

### Pure Straddle — inline logic
Selects the **highest strike AT OR BELOW spot** (nearest ATM from below). Same strike for both call and put.

```python
itm = both[both["strike"] <= spot_entry]
row = itm.loc[itm["strike"].idxmax()]
```

Fallback: if no strike ≤ spot exists, picks the absolute nearest strike.

Example: If spot = $95,500, and strikes are $95,000, $96,000 → selects **$95,000**. This means:
- **Call is ITM** (strike $95K < spot $95.5K)
- **Put is OTM** (strike $95K < spot $95.5K)

> **Note**: This is NOT a true ATM straddle. It selects the nearest in-the-money call / out-of-the-money put. Be aware of this when comparing with theoretical straddle pricing.

---

## 4. Expiry Handling

### `_get_expiry_str(dt_date)`
Returns two candidate expiry strings: `[str(dt_date + 1 day), str(dt_date)]`.

Deribit labels its 0DTE option expiring at 08:00 UTC on Jan 15 as expiry `2025-01-15`. But before ~08:00 UTC, `daystogo` may show this as having >0 days. The function returns both same-day and next-day candidates to handle edge cases.

### Entry Expiry Lock (BUG FIX)
The entry option's expiry string is captured at entry time and used to filter all subsequent exit lookups:

```python
entry_expiry = entry_snap[entry_snap["strike"] == strike].iloc[0]["expiry_str"]
put_data_exp = put_data[put_data["expiry_str"] == entry_expiry]
```

**Why this matters**: Without this, the 06:00-08:00 UTC window would pick up next-day 1DTE options at exit (08:00 = new day's options appear), causing phantom profits. This was a critical bug that was identified and fixed.

---

## 5. Cross-Midnight Sessions

For timing windows that span midnight (e.g., 22:30-00:30, 23:00-01:00):

1. `close_dt <= entry_dt` is detected as cross-midnight.
2. `close_dt += pd.Timedelta(days=1)` to set the correct exit time.
3. Data from both the entry day and next day is concatenated for option price lookups.
4. Next-day expiry candidates are also included.

---

## 6. Sizing Modes

### Flat Sizing
Position size is calculated off `INITIAL_CAPITAL` every trade. Equity changes don't affect sizing.
- `sizing_capital = INITIAL_CAPITAL` (always)

### Compound Sizing
Position size is calculated off current equity. Grows with wins, shrinks with losses.
- `sizing_capital = current_equity`

### Allocation Percentage
`num_straddles = floor(alloc_pct * sizing_capital / straddle_cost)`

Capital constraint: if `num_straddles * straddle_cost > equity`, the trade is skipped. This causes **reduced participation** when BTC price rises and equity drops simultaneously (observed in the Jul-Oct 2025 drawdown with 10x/60% allocation).

---

## 7. Timing Intervals

### Anchor-Based Generation
The `_build_timings(interval_min, start_hh, start_mm)` function generates windows:
- Spans 23 hours from the start time
- E.g., start=08:30 → 08:30-09:00, 09:00-09:30, ..., 07:00-07:30 (for 30-min)
- E.g., start=09:00 → 09:00-10:00, 10:00-11:00, ..., 07:00-08:00 (for 60-min)

### Interval Sets Run

| Label | Anchor | Interval | Windows | CLI |
|-------|--------|----------|---------|-----|
| `30min` | 08:30 | 30 min | 46 | `--interval 30` |
| `60min` | 08:30 | 60 min | 23 | `--interval 60` |
| `120min` | 08:30 | 120 min | 11-12 | `--interval 120` |
| `60mins0900` | 09:00 | 60 min | 23 | `--interval 60 --start 0900` |

The `--start` parameter controls the anchor. Default is `0830`. When set to `0900`, folder/CSV labels include `s0900` (e.g., `perp_60mins0900_weekday_...`).

---

## 8. Sharpe Ratio Calculation

```python
daily_pnl = log.groupby("date")["pnl"].sum()
mu = mean(daily_pnl)
sd = std(daily_pnl, ddof=1)
sharpe = mu / sd * sqrt(252)
```

- Annualized using `sqrt(252)` (trading days per year).
- Computed on **daily aggregated PnL**, not per-trade PnL.
- `ddof=1` (sample standard deviation).

**Note**: The pure straddle script (`run_pure_straddle_interval_detailed.py`) has its own `compute_metrics` that computes Sharpe on **per-trade PnL** (not daily-aggregated), since it runs one trade per day. This is functionally equivalent when there's exactly 1 trade per day.

---

## 9. Fee and TP Settings (Recent Runs)

All recent interval sweep backtests use:
- **Fee**: 0 bps (no transaction costs)
- **Take-Profit**: Disabled (`tp_enabled=False`)
- **Exchange**: Deribit only

---

## 10. Day Filters

| Filter | Days Included |
|--------|---------------|
| `weekday` | Monday through Friday |
| `weekend` | Saturday and Sunday |
| `all` | All days |

---

## 11. Output Structure

Each configuration generates an individual folder containing:

| File | Description |
|------|-------------|
| `trade_log.csv` | Every trade with entry/exit prices, PnL, capital after |
| `backtest_summary.md` (synth) or `metrics.txt` (pure) | Overall and yearly metrics |
| `yearly_metrics.csv` | Year-by-year performance breakdown |
| `backtest_equity_curve.png` / `equity_curve.png` | Equity curve plot |
| `backtest_drawdown.png` / `drawdown.png` | Drawdown plot |
| `backtest_monthly_heatmap.png` / `monthly_heatmap.png` | Monthly returns heatmap |
| `daily_pnl.png` | Daily PnL bar chart (pure straddle only) |
| `pnl_distribution.png` | PnL histogram (pure straddle only) |

A master summary CSV is also produced per sweep (e.g., `perp_60min_weekday_detailed_results.csv`).

### Folder Naming Convention
```
{strategy}_{interval}_{dayfilter}_{timing}_{leverage}_{capital}_{alloc}pct_{sizing}_notp
```
Examples:
- `perp_60min_weekday_1330-1430_25x_8k_60pct_flat_notp`
- `spot10x_30min_weekend_2230-2300_8k_80pct_compound_notp`
- `pure_straddle_60mins0900_weekday_1400-1500_8k_60pct_flat_notp`

---

## 12. Key Scripts

### Engine
| Script | Role |
|--------|------|
| `0dte_straddle_leveraged.py` | Core backtest engine for synthetic straddle (perp and spot). Contains `load_data`, `run_backtest_custom`, `compute_metrics`, `save_summary`, `generate_plots`. |

### Sweep Runners (Detailed Output)
| Script | Strategy | Notes |
|--------|----------|-------|
| `run_perp_interval_detailed.py` | Perp 25x/50x synth straddle | Supports `--interval`, `--day-filter`, `--start` |
| `run_spot10x_interval_detailed.py` | Spot 10x synth straddle (0.5 BTC) | Same CLI interface |
| `run_pure_straddle_interval_detailed.py` | Pure straddle (1 call + 1 put) | Self-contained engine (loads calls + puts) |

### Config Subsets Used in Recent Runs
| Parameter | Perp | Spot 10x | Pure Straddle |
|-----------|------|----------|---------------|
| Leverage | 25x, 50x | 10x | N/A |
| Capital | $8,000 | $8,000 | $8,000 |
| Allocation | 60%, 80% | 60%, 80% | 60%, 80% |
| Sizing | flat, compound | flat, compound | flat, compound |
| Fee | 0 bps | 0 bps | 0 bps |
| TP | Disabled | Disabled | Disabled |
| Exchange | Deribit | Deribit | Deribit |

---

## 13. Bugs Found and Fixed

### Bug 1: 06:00-08:00 UTC Phantom Profits (CRITICAL)
**Symptom**: Astronomically high Sharpe ratios and returns for the 06:00-08:00 window.
**Root Cause**: At exit time (08:00 UTC = Deribit expiry), the engine picked up the next day's 1DTE option instead of the expiring 0DTE, showing inflated exit premiums.
**Fix**: Capture `entry_expiry` from the selected put at entry, then filter `put_data` to only that expiry for all exit lookups (`put_data_exp = put_data[put_data["expiry_str"] == entry_expiry]`).

### Bug 2: File Path Errors in Pure Straddle Script
**Symptom**: `FileNotFoundError` when running `run_pure_straddle_sweep.py`.
**Fix**: Changed `DATA_PATH_1` and `DATA_PATH_2` to use `DATA_DIR` relative to the script directory, not hardcoded paths.

### Bug 3: Premium Column Name Mismatch
**Symptom**: `KeyError: 'mark_price'` in audit scripts.
**Root Cause**: File 1 uses `mark_price`, File 2 uses `premium`. The engine renames File 1's column to `premium` after loading, but audit scripts accessing raw data must be aware of this.

---

## 14. Known Nuances and Gotchas

### Participation Rate
Not every day produces a trade. Common reasons:
1. **No option data at the entry timestamp** on Deribit for that day.
2. **Capital insufficient** — `alloc_pct * equity < straddle_cost` (especially with compounding sizing during drawdowns when BTC price is high).
3. **No matching expiry** — the 0DTE option may not exist or have pricing data at the exact entry time.

### The Jul-Oct 2025 Squeeze (10x Spot / 60% Alloc)
During this period, BTC rose to $110K-$119K while equity drew down to ~$10,400. With 60% allocation of ~$10,400 = $6,240, and spot margin alone costing $5,500-$5,900, there was almost no headroom for put premiums. Monthly trade counts dropped from 18-20 to 1-7, creating a self-reinforcing drawdown where the strategy couldn't trade its way out.

### Time Tolerance
`_find_nearest_time(sub, target_dt, tol_min=5)` allows up to 5 minutes tolerance when matching entry/exit timestamps to available data. If no data exists within ±5 minutes of the target time, the trade is skipped.

### Memory Pressure
Each data load consumes significant RAM (~24M rows). Running more than 4-6 parallel processes loading data simultaneously can crash the system (observed when 12 parallel processes caused a laptop crash). Batch runs in groups of 3-4.

### Bybit Tiered Leverage
The engine contains Bybit's tiered leverage table (`LEVERAGE_TIERS`), which caps effective leverage based on position size. For example, positions >$100K are capped at 100x even if 125x is requested. This affects the perp strategy's actual leverage.

---

## 15. Verification Checklist

Before sharing any backtest results, verify:

1. **Trade count**: Summary CSV matches trade log row count.
2. **Capital chain**: `capital_after[i] = capital_after[i-1] + pnl[i]` for all trades.
3. **Final capital**: Summary CSV `final_capital` matches last row of trade log.
4. **Sharpe recalculation**: Independently recalculate from PnL series.
5. **Day filter**: No weekend trades in weekday filter (and vice versa).
6. **Entry time**: All trades enter at the declared time.
7. **Fees**: Should be $0 if fee_bps=0.
8. **No anomalous results**: Sharpe > 5 or returns > 1000% for short windows should be investigated.

---

## 16. GitHub Repository

**URL**: `https://github.com/orlandokohjy/btc-0dte-backtest`

Only key scripts are committed (no CSV outputs or data files). The `.gitignore` should exclude `data/`, `output/`, and any large result files.
