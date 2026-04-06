# BTC 0DTE Synthetic Straddle Backtest

Backtesting engine for 0DTE BTC options strategies on Bybit, specifically the **Synthetic Straddle**: Long BTC Spot/Perp + Long ITM Puts.

## Strategy

Each straddle unit consists of:
- **Long 0.5 BTC Spot** (10x leverage) — directional upside
- **Long 2 × 0.5 BTC ITM Put Options** — downside protection / volatility capture

Session: **S3 (14:00–18:00 UTC)**, weekdays only.

Take-profit: 30% of total put cost, triggered by combined PnL (spot + puts).

## Files

| File | Description |
|------|-------------|
| `0dte_straddle_leveraged.py` | Core backtest engine — data loading, sizing, trade execution, PnL, metrics, plots |
| `run_bybit_spot_sweep.py` | Runner script — sweeps allocations (10–60%), flat/compound sizing, TP/NoTP for 10x spot with $8K start |
| `bybit_0dte_algo_prompt.md` | Prompt for building the live trading algorithm on Bybit |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Add data files

Place these two parquet files in a `data/` subdirectory:

```
btc-0dte-backtest/
├── data/
│   ├── btc_0dte_data.parquet        # 2024-01-01 to 2026-01-22
│   └── btc_0dte_data_2026.parquet   # 2026-01-22 onwards
```

Or set the `BTC_DATA_DIR` environment variable to point to wherever they live:

```bash
export BTC_DATA_DIR=/path/to/your/data
```

### 3. Run the sweep

```bash
python run_bybit_spot_sweep.py
```

Results go into `output/` (summary CSV + per-config folders with trade logs, plots, and monthly volumes).

### Customising

To change initial capital (e.g. $10K), edit the patch line in the runner:

```python
bt.INITIAL_CAPITAL = 10_000
```

To run with fees (e.g. net 0.5bps per side across all 3 legs):

```python
FEE = 0.5 / 3  # = 0.1667 bps per leg per side
```

To run the engine standalone (perp mode, original 1 BTC sizing):

```bash
python 0dte_straddle_leveraged.py --leverage 25 --session 3
python 0dte_straddle_leveraged.py --leverage 50 --fee 0.5 --notp --session 3
```

## Data Schema

The parquet files must contain these columns:

| Column | Type | Description |
|--------|------|-------------|
| `datetime` | timestamp | Quote timestamp (UTC) |
| `expiry_date` | string/date | Option expiry date |
| `strike` | numeric | Strike price (USD) |
| `call_put` | string | `"P"` for puts (only puts are used) |
| `spot_price` | numeric | BTC spot price (USD) |
| `mark_price` or `premium` | numeric | Option premium (mark price in BTC for file 1, USD for file 2) |
| `daystogo` | numeric | Days to expiry (filtered to ≤ 1.0 for 0DTE) |
| `exchange` | string | Exchange name (e.g. `"bybit"`, `"deribit"`) |

## Key Backtest Results (10x Spot, $10K, S3)

| Config | Trades | Return | Sharpe | MaxDD | Final |
|--------|--------|--------|--------|-------|-------|
| 60% Compound TP | 530 | 195% | 1.714 | -14.9% | $29,518 |
| 60% Compound NoTP | 530 | 213% | 1.509 | -20.1% | $31,277 |
| 50% Flat TP | 498 | 67% | 1.318 | -10.8% | $16,715 |
