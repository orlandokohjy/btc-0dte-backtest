# Prompt: Build Bybit 0DTE BTC Synthetic Straddle Algo (0.5 BTC Notional)

## Overview

Build an automated execution engine for a **0DTE synthetic straddle** on Bybit.
The algo runs **one session per day** (Session 3: 14:00–18:00 UTC, Mon–Fri) and
constructs a delta-neutral position that profits from large intraday BTC moves
in either direction.

## Position Structure (per straddle unit)

Each straddle consists of:
- **Long 0.5 BTC spot** with 10x leverage (margin = 0.5 × BTC_price / 10)
- **Long 2 × 0.5 BTC ITM put options** expiring same day (0DTE)

Strike selection: nearest strike ABOVE current spot price (in-the-money put).

This is the original 1 BTC synthetic straddle scaled down by 50%:
- Spot exposure: 0.5 BTC
- Put notional: 2 × 0.5 = 1.0 BTC total
- Put-to-spot ratio: 2:1 (maintained)

## Sizing — COMPOUND (scales with equity)

Use **compound sizing** — allocate 60% of CURRENT EQUITY each day:
- Available capital = ALLOC_PCT × current_equity
- Number of straddles = floor(available / straddle_cost)
- **No cap on number of straddles** — if equity grows enough to afford 5 straddles, trade 5
- Straddle cost = (0.5 × spot / 10) + (2 × 0.5 × put_premium)

Default parameters:
- INITIAL_CAPITAL = $10,000
- ALLOC_PCT = 0.60 (60% of current equity)
- LEVERAGE = 10 (Bybit spot leverage)

As the strategy profits, equity grows, and the algo automatically scales up the number of straddles. This is the compounding engine — early 2024 trades 1 straddle, by 2026 it trades 2-3 per day as equity grows.

## Session Lifecycle

```
14:00 UTC — ENTRY
  1. Fetch current equity (initial capital + cumulative realized P&L)
  2. Fetch BTC spot price
  3. Fetch 0DTE option chain, filter for puts
  4. Select ITM put: nearest strike > spot, passing spread/liquidity filters
  5. Compute straddle_cost = (0.5 × spot / 10) + (2 × 0.5 × put_premium)
  6. Compute num_straddles = floor(0.60 × current_equity / straddle_cost)
     — no maximum cap, trade as many as equity allows
  7. If num_straddles == 0 → skip today, log "insufficient capital"
  8. For each straddle unit:
     a. Open long 0.5 BTC spot position (market order, 10x leverage)
     b. Buy 2 × 0.5 BTC put at selected strike (aggressive limit chase)
     c. If put fill fails → immediately close spot leg
  9. Start TP monitor loop

14:00–18:00 UTC — MONITOR
  - Poll combined P&L every 5 seconds:
      spot_pnl = 0.5 × (current_spot - entry_spot) × num_straddles
      put_pnl  = 2 × 0.5 × (current_put_mark - entry_put_price) × num_straddles
      combined_pnl = spot_pnl + put_pnl
  - TP trigger: combined_pnl >= 0.30 × total_put_cost
      where total_put_cost = 2 × 0.5 × entry_put_price × num_straddles
  - If TP hit → close all legs immediately

18:00 UTC — HARD CLOSE
  - Close all open positions:
      1. Close spot leg (market order)
      2. Sell all puts (aggressive limit chase)
  - Update equity: current_equity += net_pnl
  - Log trade results
  - Send notification
```

## Execution

### Spot Leg
- Market orders on BTCUSDT spot with 10x leverage
- Set leverage to 10x at startup via API

### Option Leg
- Bybit does not support market orders for options
- Use aggressive limit chase:
  1. Post IOC limit at ask + 0.2% (round to $5 tick)
  2. If unfilled, cancel and re-post higher
  3. Repeat up to 10 attempts, max slippage 5% above initial ask
  4. If all fail → emergency close spot leg
- Same chase logic in reverse when selling puts (walk bid down)

### Order Sequence
- Entry: spot first, then puts. If put fails → unwind spot.
- Exit: spot first (market, instant), then puts (chase limit).

## Fee Budget
- Target: net 0.5bps per side across all 3 legs combined
- At BTC $80K with 1 straddle: entry fee ≈ $4, round-trip ≈ $8
- This is ~0.27% of a typical $3,000 straddle cost

## Risk Management

| Control | Default |
|---------|---------|
| Max daily loss | 10% of current equity |
| Bid-ask spread filter | Skip puts with spread > 10% of mid |
| Option slippage cap | 5% above initial ask |
| API circuit breaker | Halt after 5 consecutive errors, 5min cooldown |
| **No straddle cap** | Trade as many as equity allows |

## Notifications (Telegram)
Send alerts on:
- Session entry (num_straddles, current equity, straddle cost, strike)
- TP hit (PnL, exit prices, time to TP)
- Hard close (PnL, exit prices)
- Skipped day (insufficient capital)
- Errors / circuit breaker triggers
- Daily summary (equity, cumulative return)

## State Persistence
- Track current_equity in `state/equity.json` (updated after every trade)
- Write open positions to `state/positions.json` on every state change
- On restart, load existing equity + positions and resume monitoring
- Log all trades to `state/trade_log.csv` with columns:
  date, entry_time, exit_time, exit_reason, spot_entry, spot_exit,
  put_strike, put_premium_entry, put_premium_exit, num_straddles,
  straddle_cost, capital_before, spot_pnl, put_pnl, gross_pnl,
  fees, net_pnl, capital_after

## Monthly Volume Tracking
Track and log to `state/monthly_volumes.csv`:
- option_contracts: 4 per straddle (buy 2 puts + sell 2 puts, each 0.5 BTC)
- option_btc_notional: 2.0 BTC per straddle (2 × 0.5 BTC × 2 sides)
- spot_btc_volume: 1.0 BTC per straddle (0.5 BTC × 2 sides)

## Architecture

```
main.py
 └── Algo (orchestrator)
      ├── BybitExchange       ← REST + WebSocket API wrapper (V5)
      │    ├── Spot orders (market, 10x leverage)
      │    └── Option orders (aggressive limit chase)
      ├── MarketData          ← Real-time BTC price feed
      ├── OptionChain         ← 0DTE chain: fetch, filter, select ITM put
      ├── PositionSizer       ← Compound: floor(60% × current_equity / cost)
      ├── Portfolio           ← Equity tracking, P&L, state persistence
      ├── RiskManager         ← Pre-trade checks, daily loss limit, circuit breaker
      ├── ExitManager         ← TP monitor (5s poll) + hard close at 18:00
      ├── Scheduler           ← APScheduler: entry at 14:00, close at 18:00
      ├── VolumeTracker       ← Monthly contract and BTC volume logging
      └── Notifier            ← Telegram alerts
```

## Config Defaults

```python
LEVERAGE = 10
QTY_PER_LEG = 0.5           # BTC per leg per straddle
NUM_PUTS = 2                 # puts per straddle
INITIAL_CAPITAL = 10_000
ALLOC_PCT = 0.60             # 60% of current equity (compound)
SIZING_MODE = "compound"     # use current equity, not initial capital
TAKE_PROFIT_PCT = 0.30       # 30% of put cost
SESSION_ENTRY = "14:00 UTC"
SESSION_CLOSE = "18:00 UTC"
WEEKDAYS_ONLY = True
MAX_STRADDLES = None         # no cap — trade as many as equity allows
```

## Backtest Results (this exact config, 0bp fee)

- Period: Jan 2024 – Mar 2026 (577 weekday sessions)
- Trades: 530 | Coverage: 92% | Sharpe: 1.714
- Return: 195% ($10K → $29,500) | Max DD: -34.5%
- Win rate: 42.5% | Profit factor: 1.335
- 0% liquidation rate (hedged position)
- Yearly: 2024 +36.6% | 2025 +46.7% | 2026 +47.2%

## Tech Stack
- Python 3.12+
- pybit (Bybit V5 SDK)
- APScheduler
- python-telegram-bot (optional)
- Runs on any VPS with stable internet (recommend Singapore for Bybit latency)
