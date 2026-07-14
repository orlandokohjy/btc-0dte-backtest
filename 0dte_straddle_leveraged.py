#!/usr/bin/env python3
"""
BTC 0DTE Proper Synthetic Straddle Backtest (Leveraged Spot + 2 Puts)
=====================================================================
Strategy: Long 1 BTC via leveraged spot/perp + Long 2 ATM Puts per straddle unit

Straddle cost = QTY * spot / LEVERAGE + NUM_PUTS * QTY * put_premium
TP target = 30% of (NUM_PUTS * QTY * put_premium * num_straddles)
TP trigger = combined PnL (spot + puts) >= TP target

Three sessions per day (Mon-Fri only):
  Session 1: 08:30-18:00 UTC  (half allocation)
  Session 2: 12:00-14:00 UTC  (half allocation)
  Session 3: 14:00-18:00 UTC  (full allocation, SOD IVC)

Usage:
  python 0dte_straddle_leveraged.py --leverage 25
  python 0dte_straddle_leveraged.py --leverage 50 --fee 1
"""

from __future__ import annotations

import argparse
import math
import os
from datetime import timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

# =============================================================================
INITIAL_CAPITAL = 10_000
IVC_PCT = 0.60
IVC_FLOOR = 6_000
RESERVE_IVC_PCT = 0.50
MIN_RESERVE_CONTRACTS = 2
QTY = 1.0              # 1 BTC per straddle unit
NUM_PUTS = 2            # 2 puts per straddle (proper synthetic straddle)
TP_PCT = 0.30           # 30% of put cost as TP target
LIQ_FEE_RATE = 0.005        # 0.5% liquidation fee on notional

# Bybit BTCUSDT perpetual tiered risk limits (pos_value_usd, mm_rate, max_leverage)
LEVERAGE_TIERS = [
    (100_000,      0.0040, 125.00),
    (1_000_000,    0.0050, 100.00),
    (2_000_000,    0.0100,  66.67),
    (3_000_000,    0.0150,  50.00),
    (4_000_000,    0.0200,  40.00),
    (5_000_000,    0.0250,  33.33),
    (6_000_000,    0.0300,  28.57),
    (7_000_000,    0.0350,  25.00),
    (8_000_000,    0.0400,  22.22),
    (9_000_000,    0.0450,  20.00),
    (10_000_000,   0.0500,  18.18),
]


def _get_tier(position_value_usd):
    """Return (mm_rate, max_leverage) for a given position notional."""
    for cap, mm, ml in LEVERAGE_TIERS:
        if position_value_usd <= cap:
            return mm, ml
    return LEVERAGE_TIERS[-1][1], LEVERAGE_TIERS[-1][2]

START_DATE = "2024-01-01"
END_DATE = "2026-05-31"
_SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BTC_DATA_DIR", str(_SCRIPT_DIR / "data")))
DATA_PATH_1 = DATA_DIR / "btc_0dte_data.parquet"
DATA_PATH_2 = DATA_DIR / "btc_0dte_data_2026.parquet"
BASE_OUT = Path(os.environ.get("BTC_OUTPUT_DIR", str(_SCRIPT_DIR / "output")))
# =============================================================================


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--leverage", type=float, required=True,
                   help="Leverage for the spot/perp leg (e.g. 25 or 50)")
    p.add_argument("--fee", type=float, default=0,
                   help="Fee in basis points per leg per side (default 0)")
    p.add_argument("--notp", action="store_true",
                   help="Disable take-profit (all positions exit at hard close)")
    p.add_argument("--session", type=str, default="all",
                   help="Session to run: 1, 2, 3, or 'all' (default: all combined)")
    return p.parse_args()


def load_data(exchange_filter=None) -> pd.DataFrame:
    """Load and merge parquet data files.

    exchange_filter : str or None — if set (e.g. "deribit"), keep only that exchange.
    """
    print("Loading File 1 ...", flush=True)
    df1 = pd.read_parquet(
        DATA_PATH_1,
        columns=["datetime", "expiry_date", "strike", "call_put",
                 "spot_price", "mark_price", "daystogo", "exchange"],
    )
    df1["datetime"] = pd.to_datetime(df1["datetime"], utc=True)
    df1.rename(columns={"mark_price": "premium"}, inplace=True)
    df1["premium_usd"] = (
        pd.to_numeric(df1["premium"], errors="coerce")
        * pd.to_numeric(df1["spot_price"], errors="coerce")
    )
    print(f"  File 1: {len(df1):,} rows", flush=True)

    print("Loading File 2 ...", flush=True)
    df2 = pd.read_parquet(
        DATA_PATH_2,
        columns=["datetime", "expiry_date", "strike", "call_put",
                 "spot_price", "premium", "daystogo", "exchange"],
    )
    df2["datetime"] = pd.to_datetime(df2["datetime"], utc=True)
    df2["spot_price"] = pd.to_numeric(df2["spot_price"], errors="coerce")
    df2["premium"] = pd.to_numeric(df2["premium"], errors="coerce")

    usd_mask = df2["exchange"].isin(["bybit", "binance"])
    btc_mask = ~usd_mask
    df2["premium_usd"] = 0.0
    df2.loc[usd_mask, "premium_usd"] = df2.loc[usd_mask, "premium"]
    df2.loc[btc_mask, "premium_usd"] = (
        df2.loc[btc_mask, "premium"] * df2.loc[btc_mask, "spot_price"]
    )

    overlap_ts = pd.Timestamp("2026-01-22", tz="UTC")
    df2 = df2[df2["datetime"] > overlap_ts]
    print(f"  File 2 after overlap removal: {len(df2):,} rows", flush=True)

    df = pd.concat([df1, df2], ignore_index=True)
    del df1, df2

    if exchange_filter:
        n_pre = len(df)
        df = df[df["exchange"] == exchange_filter]
        print(f"  Exchange filter '{exchange_filter}': {n_pre:,} → {len(df):,} rows", flush=True)

    df["daystogo"] = pd.to_numeric(df["daystogo"], errors="coerce")
    df = df[df["daystogo"] <= 1.0]
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["spot_price"] = pd.to_numeric(df["spot_price"], errors="coerce")
    df = df.dropna(subset=["premium_usd", "strike", "spot_price"])
    df = df[df["premium_usd"] > 0]
    n_before = len(df)
    df = df[df["spot_price"] > 10_000]
    n_dropped = n_before - len(df)
    if n_dropped > 0:
        print(f"  Dropped {n_dropped} rows with corrupt spot_price (<$10K)", flush=True)
    df = df[df["call_put"] == "P"].copy()

    df["date"] = df["datetime"].dt.date
    df["time_utc"] = df["datetime"].dt.strftime("%H:%M")
    df["expiry_str"] = df["expiry_date"].astype(str).str[:10]

    start_d = pd.Timestamp(START_DATE).date()
    end_d = (pd.Timestamp(END_DATE) + timedelta(days=1)).date()
    df = df[(df["date"] >= start_d) & (df["date"] <= end_d)]
    df = df.sort_values("datetime").reset_index(drop=True)
    print(
        f"  Final put dataset: {len(df):,} rows  "
        f"({df['date'].min()} to {df['date'].max()})",
        flush=True,
    )
    return df


def _get_expiry_str(dt_date):
    """Return list of candidate expiry strings — next-day (old) and same-day (new)."""
    return [str(dt_date + timedelta(days=1)), str(dt_date)]


def _find_nearest_time(sub, target_dt, tol_min=5):
    if sub.empty:
        return pd.DataFrame()
    diffs = (sub["datetime"] - target_dt).abs()
    mask = diffs <= pd.Timedelta(minutes=tol_min)
    if not mask.any():
        return pd.DataFrame()
    filtered = sub[mask].copy()
    filtered["_tdiff"] = diffs[mask]
    return filtered.sort_values("_tdiff").drop(columns=["_tdiff"])


def _find_itm_put(snap, spot):
    """Nearest ITM put: closest strike ABOVE spot."""
    itm = snap[snap["strike"] > spot]
    if itm.empty:
        return None
    min_strike = itm["strike"].min()
    row = itm[itm["strike"] == min_strike].iloc[0]
    return float(min_strike), float(row["premium_usd"]), float(row["spot_price"])


def _compute_sizing(capital, spot, put_prem, ivc, half_alloc, target_leverage,
                    alloc_pct=None, sizing_capital=None, fixed_num=None):
    """Iteratively size the position, capping leverage per Bybit tier table.

    If fixed_num is set, always use that many straddles (no capital check).
    If alloc_pct and sizing_capital are provided, use simplified allocation:
      available = alloc_pct * sizing_capital
    Otherwise fall back to the original IVC/reserve logic.
    """
    put_cost = NUM_PUTS * QTY * put_prem
    eff_lev = target_leverage

    if fixed_num is not None:
        spot_margin = QTY * spot / eff_lev
        straddle_cost = spot_margin + put_cost
        if straddle_cost <= 0:
            return 0, 0.0, eff_lev, 0.0
        pos_value = fixed_num * QTY * spot
        mm_rate, tier_max_lev = _get_tier(pos_value)
        eff_lev = min(target_leverage, tier_max_lev)
        spot_margin = QTY * spot / eff_lev
        straddle_cost = spot_margin + put_cost
        return fixed_num, straddle_cost, eff_lev, mm_rate

    for _ in range(5):
        spot_margin = QTY * spot / eff_lev
        straddle_cost = spot_margin + put_cost
        if straddle_cost <= 0:
            return 0, 0.0, eff_lev, 0.0

        if alloc_pct is not None and sizing_capital is not None:
            available = alloc_pct * sizing_capital
        else:
            reserve = max(MIN_RESERVE_CONTRACTS * straddle_cost, RESERVE_IVC_PCT * ivc)
            available = (capital - reserve) / 2.0 if half_alloc else capital - reserve

        if available <= 0:
            return 0, 0.0, eff_lev, 0.0
        num = max(int(math.floor(available / straddle_cost)), 0)
        if num <= 0:
            return 0, 0.0, eff_lev, 0.0
        pos_value = num * QTY * spot
        mm_rate, tier_max_lev = _get_tier(pos_value)
        new_lev = min(target_leverage, tier_max_lev)
        if new_lev == eff_lev:
            return num, straddle_cost, eff_lev, mm_rate
        eff_lev = new_lev

    return num, straddle_cost, eff_lev, mm_rate


def _scan_exit(put_data, strike, entry_dt, close_dt,
               entry_spot, entry_put_prem, num, leverage, mm_rate, tp_enabled):
    """
    Scan chronologically for the FIRST of:
      1) Liquidation — combined loss (perp+puts) exceeds capital deployed
         (portfolio margin: puts hedge the perp, so check NET position)
      2) Take-profit — combined PnL >= 30% of put cost
      3) Hard close  — session end time
    Returns (exit_reason, exit_spot, exit_put_prem, exit_dt).
    """
    sub = put_data[
        (put_data["strike"] == strike)
        & (put_data["datetime"] > entry_dt)
        & (put_data["datetime"] <= close_dt)
    ].sort_values("datetime")
    if sub.empty:
        return "hard_close", None, None, None

    spots = sub["spot_price"].values
    prems = sub["premium_usd"].values

    spot_pnl = num * QTY * (spots - entry_spot)
    put_pnl = num * NUM_PUTS * QTY * (prems - entry_put_prem)
    combined_pnl = spot_pnl + put_pnl

    capital_deployed = num * (QTY * entry_spot / leverage + NUM_PUTS * QTY * entry_put_prem)
    liq_threshold = -capital_deployed * (1.0 - mm_rate)
    liq_hits = np.where(combined_pnl <= liq_threshold)[0]
    liq_idx = int(liq_hits[0]) if len(liq_hits) > 0 else None

    tp_idx = None
    if tp_enabled:
        total_put_cost = num * NUM_PUTS * QTY * entry_put_prem
        tp_target = TP_PCT * total_put_cost
        tp_hits = np.where(combined_pnl >= tp_target)[0]
        if len(tp_hits) > 0:
            tp_idx = int(tp_hits[0])

    first_liq = liq_idx if liq_idx is not None else float("inf")
    first_tp = tp_idx if tp_idx is not None else float("inf")

    if first_liq <= first_tp and liq_idx is not None:
        row = sub.iloc[liq_idx]
        return "liquidation", float(row["spot_price"]), float(row["premium_usd"]), row["datetime"]

    if first_tp < first_liq and tp_idx is not None:
        row = sub.iloc[tp_idx]
        return "TP", float(row["spot_price"]), float(row["premium_usd"]), row["datetime"]

    return "hard_close", None, None, None


def _find_close_data(put_data, strike, close_dt):
    sub = put_data[put_data["strike"] == strike]
    if sub.empty:
        return None
    near = _find_nearest_time(sub, close_dt, tol_min=5)
    if not near.empty:
        row = near.iloc[0]
        return float(row["spot_price"]), float(row["premium_usd"]), row["datetime"]
    before = sub[sub["datetime"] <= close_dt]
    if before.empty:
        return None
    row = before.iloc[-1]
    return float(row["spot_price"]), float(row["premium_usd"]), row["datetime"]


def _process_session(sess_id, entry_time, close_time, half_alloc,
                     capital, ivc, put_data, entry_date, fee_rate, target_leverage,
                     tp_enabled=True, alloc_pct=None, sizing_capital=None,
                     fixed_num=None):
    entry_dt = pd.Timestamp(f"{entry_date} {entry_time}:00", tz="UTC")
    close_dt = pd.Timestamp(f"{entry_date} {close_time}:00", tz="UTC")
    if close_dt <= entry_dt:
        close_dt += pd.Timedelta(days=1)

    entry_snap = _find_nearest_time(put_data, entry_dt, tol_min=5)
    if entry_snap.empty:
        return None
    spot = float(entry_snap["spot_price"].iloc[0])
    result = _find_itm_put(entry_snap, spot)
    if result is None:
        return None
    strike, put_prem, _ = result
    if pd.isna(put_prem) or put_prem <= 0:
        return None

    entry_expiry = entry_snap[entry_snap["strike"] == strike].iloc[0]["expiry_str"]
    put_data_exp = put_data[put_data["expiry_str"] == entry_expiry]

    num, straddle_cost, eff_lev, mm_rate = _compute_sizing(
        capital, spot, put_prem, ivc, half_alloc, target_leverage,
        alloc_pct=alloc_pct, sizing_capital=sizing_capital,
        fixed_num=fixed_num,
    )
    if num <= 0:
        return None

    spot_margin = num * QTY * spot / eff_lev
    put_cost_total = num * NUM_PUTS * QTY * put_prem
    entry_cost = spot_margin + put_cost_total

    exit_reason, ex_spot, ex_prem, ex_dt = _scan_exit(
        put_data_exp, strike, entry_dt, close_dt,
        spot, put_prem, num, eff_lev, mm_rate, tp_enabled,
    )

    if exit_reason in ("TP", "liquidation"):
        exit_spot, exit_prem, exit_dt_actual = ex_spot, ex_prem, ex_dt
    else:
        close_result = _find_close_data(put_data_exp, strike, close_dt)
        if close_result is None:
            return None
        exit_spot, exit_prem, exit_dt_actual = close_result

    if exit_reason == "liquidation":
        spot_pnl = num * QTY * (exit_spot - spot)
        put_pnl = num * NUM_PUTS * QTY * (exit_prem - put_prem)
        liq_fee = LIQ_FEE_RATE * num * QTY * exit_spot
        gross_pnl = spot_pnl + put_pnl
        entry_fee = (1 + NUM_PUTS) * num * fee_rate * QTY * spot
        total_fee = entry_fee + liq_fee
        pnl = gross_pnl - total_fee
    else:
        spot_pnl = num * QTY * (exit_spot - spot)
        put_pnl = num * NUM_PUTS * QTY * (exit_prem - put_prem)
        gross_pnl = spot_pnl + put_pnl
        entry_fee = (1 + NUM_PUTS) * num * fee_rate * QTY * spot
        exit_fee = (1 + NUM_PUTS) * num * fee_rate * QTY * exit_spot
        total_fee = entry_fee + exit_fee
        pnl = gross_pnl - total_fee

    return {
        "date": entry_date,
        "session": sess_id,
        "entry_time": str(entry_dt),
        "exit_time": str(exit_dt_actual),
        "exit_reason": exit_reason,
        "spot_entry": spot,
        "spot_exit": exit_spot,
        "put_instrument": f"P-{int(strike)}",
        "put_strike": strike,
        "put_premium_entry": put_prem,
        "put_premium_exit": exit_prem,
        "num_straddles": num,
        "effective_leverage": eff_lev,
        "capital_required": straddle_cost * num,
        "straddle_cost": straddle_cost,
        "entry_cost": entry_cost,
        "spot_margin": spot_margin,
        "put_cost_total": put_cost_total,
        "spot_pnl": spot_pnl,
        "put_pnl": put_pnl,
        "gross_pnl": gross_pnl,
        "fees": total_fee,
        "pnl": pnl,
        "pnl_pct": pnl / entry_cost if entry_cost > 0 else 0,
        "exit_dt": exit_dt_actual,
    }


def run_backtest(df, fee_bps, leverage, tp_enabled=False, session_filter="all",
                 alloc_pct=None, flat_sizing=False, fixed_num=None):
    """Run the backtest.

    alloc_pct : float or None — fraction of capital to allocate (e.g. 0.30).
                If None, use original IVC/reserve logic.
    flat_sizing : bool — if True, size positions off INITIAL_CAPITAL (flat);
                  if False, size off current equity (compounding).
    fixed_num : int or None — if set, always trade this many straddles (no sizing).
    """
    tp_tag = "TP" if tp_enabled else "NoTP"
    sess_tag = f"S{session_filter}" if session_filter != "all" else "S1+S2+S3"
    if fixed_num is not None:
        sizing_tag = f"fixed_{fixed_num}"
        alloc_tag = "N/A"
    else:
        sizing_tag = "flat" if flat_sizing else "compound"
        alloc_tag = f"{int(alloc_pct*100)}%" if alloc_pct is not None else "IVC"
    print(
        f"\nRunning backtest ({sess_tag}, leverage={leverage}x, fee={fee_bps}bp, "
        f"alloc={alloc_tag}, sizing={sizing_tag}, "
        f"QTY={QTY} BTC, {NUM_PUTS} puts/straddle, {tp_tag}) ...",
        flush=True,
    )
    fee_rate = fee_bps / 10_000.0
    run_s1 = session_filter in ("all", "1")
    run_s2 = session_filter in ("all", "2")
    run_s3 = session_filter in ("all", "3")

    date_groups = dict(list(df.groupby("date")))
    all_dates = sorted(date_groups.keys())
    weekdays = [d for d in all_dates if pd.Timestamp(d).dayofweek < 5]
    print(f"  Weekday trading dates: {len(weekdays)}", flush=True)

    capital = float(INITIAL_CAPITAL)
    trades = []

    for di, day in enumerate(weekdays):
        if di > 0 and di % 100 == 0:
            print(
                f"  [{di}/{len(weekdays)}] capital=${capital:,.2f}  trades={len(trades)}",
                flush=True,
            )

        sod_capital = capital
        sod_ivc = max(IVC_FLOOR, IVC_PCT * capital)

        expiry_candidates = _get_expiry_str(day)
        day_data = date_groups.get(day, pd.DataFrame())
        if day_data.empty:
            continue
        exp_data = day_data[day_data["expiry_str"].isin(expiry_candidates)]
        if exp_data.empty:
            continue

        live_ivc = max(IVC_FLOOR, IVC_PCT * capital)

        if alloc_pct is not None:
            sizing_cap = float(INITIAL_CAPITAL) if flat_sizing else capital
        else:
            sizing_cap = None

        s1 = None
        if run_s1:
            s1 = _process_session(
                1, "08:30", "18:00", True, capital, live_ivc, exp_data, day, fee_rate, leverage,
                tp_enabled=tp_enabled,
                alloc_pct=alloc_pct, sizing_capital=sizing_cap,
                fixed_num=fixed_num,
            )
        s1_exit_dt = pd.Timestamp(s1["exit_dt"]) if s1 else None
        s1_closed = False

        noon_dt = pd.Timestamp(f"{day} 12:00:00", tz="UTC")
        if s1 and s1_exit_dt <= noon_dt:
            capital += s1["pnl"]
            s1["capital_after"] = capital
            trades.append(s1)
            s1_closed = True

        live_ivc = max(IVC_FLOOR, IVC_PCT * capital)
        if alloc_pct is not None:
            sizing_cap = float(INITIAL_CAPITAL) if flat_sizing else capital

        s2 = None
        if run_s2:
            s2 = _process_session(
                2, "12:00", "14:00", True, capital, live_ivc, exp_data, day, fee_rate, leverage,
                tp_enabled=tp_enabled,
                alloc_pct=alloc_pct, sizing_capital=sizing_cap,
                fixed_num=fixed_num,
            )

        s2_closed = False
        t_1400 = pd.Timestamp(f"{day} 14:00:00", tz="UTC")

        if s2:
            s2_exit_dt = pd.Timestamp(s2["exit_dt"])
            if s2_exit_dt <= t_1400:
                capital += s2["pnl"]
                s2["capital_after"] = capital
                trades.append(s2)
                s2_closed = True

        if s1 and not s1_closed and s1_exit_dt <= t_1400:
            capital += s1["pnl"]
            s1["capital_after"] = capital
            trades.append(s1)
            s1_closed = True

        if alloc_pct is not None:
            sizing_cap = float(INITIAL_CAPITAL) if flat_sizing else capital

        s3 = None
        if run_s3:
            s3 = _process_session(
                3, "14:00", "18:00", False, capital, sod_ivc, exp_data, day, fee_rate, leverage,
                tp_enabled=tp_enabled,
                alloc_pct=alloc_pct, sizing_capital=sizing_cap,
                fixed_num=fixed_num,
            )

        if s1 and not s1_closed:
            capital += s1["pnl"]
            s1["capital_after"] = capital
            trades.append(s1)

        if s3:
            capital += s3["pnl"]
            s3["capital_after"] = capital
            trades.append(s3)

    print(
        f"  Completed: {len(trades)} trades, final capital=${capital:,.2f}",
        flush=True,
    )
    return pd.DataFrame(trades)


def run_backtest_custom(df, fee_bps, leverage, entry_time, close_time,
                        day_filter="weekday", tp_enabled=False,
                        alloc_pct=None, flat_sizing=False, fixed_num=None):
    """Run a single-session backtest with custom entry/close times and day filter.

    Parameters
    ----------
    entry_time : str   e.g. "10:00"
    close_time : str   e.g. "14:00"
    day_filter : str   "weekday" (Mon-Fri), "weekend" (Sat-Sun), or "all"
    """
    tp_tag = "TP" if tp_enabled else "NoTP"
    if fixed_num is not None:
        sizing_tag = f"fixed_{fixed_num}"
        alloc_tag = "N/A"
    else:
        sizing_tag = "flat" if flat_sizing else "compound"
        alloc_tag = f"{int(alloc_pct*100)}%" if alloc_pct is not None else "IVC"
    print(
        f"\nRunning backtest (custom {entry_time}-{close_time} UTC, {day_filter}, "
        f"leverage={leverage}x, fee={fee_bps}bp, "
        f"alloc={alloc_tag}, sizing={sizing_tag}, "
        f"QTY={QTY} BTC, {NUM_PUTS} puts/straddle, {tp_tag}) ...",
        flush=True,
    )
    fee_rate = fee_bps / 10_000.0

    date_groups = dict(list(df.groupby("date")))
    all_dates = sorted(date_groups.keys())

    if day_filter == "weekday":
        trading_dates = [d for d in all_dates if pd.Timestamp(d).dayofweek < 5]
    elif day_filter == "weekend":
        trading_dates = [d for d in all_dates if pd.Timestamp(d).dayofweek >= 5]
    else:
        trading_dates = all_dates
    print(f"  {day_filter} trading dates: {len(trading_dates)}", flush=True)

    cross_midnight = close_time <= entry_time

    capital = float(INITIAL_CAPITAL)
    trades = []

    for di, day in enumerate(trading_dates):
        if di > 0 and di % 100 == 0:
            print(
                f"  [{di}/{len(trading_dates)}] capital=${capital:,.2f}  trades={len(trades)}",
                flush=True,
            )

        expiry_candidates = _get_expiry_str(day)
        day_data = date_groups.get(day, pd.DataFrame())
        if day_data.empty:
            continue

        if cross_midnight:
            next_day = day + timedelta(days=1) if hasattr(day, 'day') else (pd.Timestamp(day) + pd.Timedelta(days=1)).date()
            next_data = date_groups.get(next_day, pd.DataFrame())
            next_expiry = _get_expiry_str(next_day)
            all_expiry = list(set(expiry_candidates + next_expiry))
            combined = pd.concat([day_data, next_data]) if not next_data.empty else day_data
            exp_data = combined[combined["expiry_str"].isin(all_expiry)]
        else:
            exp_data = day_data[day_data["expiry_str"].isin(expiry_candidates)]

        if exp_data.empty:
            continue

        if alloc_pct is not None:
            sizing_cap = float(INITIAL_CAPITAL) if flat_sizing else capital
        else:
            sizing_cap = None

        ivc = max(IVC_FLOOR, IVC_PCT * capital)

        result = _process_session(
            1, entry_time, close_time, False, capital, ivc,
            exp_data, day, fee_rate, leverage,
            tp_enabled=tp_enabled,
            alloc_pct=alloc_pct, sizing_capital=sizing_cap,
            fixed_num=fixed_num,
        )

        if result:
            capital += result["pnl"]
            result["capital_after"] = capital
            trades.append(result)

    print(
        f"  Completed: {len(trades)} trades, final capital=${capital:,.2f}",
        flush=True,
    )
    return pd.DataFrame(trades)


# ========================= METRICS =========================

def compute_metrics(log):
    pnl = log["pnl"].values.astype(float)
    equity = log["capital_after"].values.astype(float)
    n = len(log)

    total_pnl = float(np.sum(pnl))
    cum_ret = equity[-1] / INITIAL_CAPITAL - 1.0

    daily = log.groupby("date")["pnl"].sum()
    daily_arr = daily.values.astype(float)
    n_days = len(daily_arr)
    mu_d = float(np.mean(daily_arr))
    sd_d = float(np.std(daily_arr, ddof=1)) if n_days > 1 else 0
    sharpe = (mu_d / sd_d * math.sqrt(252)) if sd_d > 0 else float("nan")

    ann_ret = (
        ((1 + cum_ret) ** (252.0 / n_days) - 1.0)
        if n_days > 0 and cum_ret > -1
        else float("nan")
    )

    peaks = np.maximum.accumulate(equity)
    dd_pct = (equity - peaks) / peaks * 100
    max_dd_pct = float(np.min(dd_pct))
    max_dd_usd = float(np.min(equity - peaks))

    peak_idx = np.maximum.accumulate(np.arange(n) * (equity >= peaks))
    dd_durations = np.arange(n) - peak_idx
    max_dd_dur = int(np.max(dd_durations)) if n > 0 else 0

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    wr = len(wins) / n * 100 if n else 0
    aw = float(np.mean(wins)) if len(wins) else 0
    al = float(np.mean(losses)) if len(losses) else 0
    gw = float(np.sum(wins))
    gl = float(np.sum(np.abs(losses)))
    pf = gw / gl if gl > 0 else float("inf")

    total_fees = float(log["fees"].sum())
    tp_rate = (log["exit_reason"] == "TP").sum() / n * 100 if n else 0
    liq_rate = (log["exit_reason"] == "liquidation").sum() / n * 100 if n else 0

    overall = {
        "total_trades": n,
        "total_pnl": total_pnl,
        "total_fees": total_fees,
        "total_return_pct": cum_ret * 100,
        "ann_return_pct": ann_ret * 100 if np.isfinite(ann_ret) else float("nan"),
        "sharpe": sharpe,
        "max_dd_pct": max_dd_pct,
        "max_dd_usd": max_dd_usd,
        "max_dd_duration_trades": max_dd_dur,
        "win_rate_pct": wr,
        "avg_win": aw,
        "avg_loss": al,
        "profit_factor": pf,
        "tp_hit_rate_pct": tp_rate,
        "liq_rate_pct": liq_rate,
    }

    dates = pd.to_datetime(log["date"])
    years = sorted(dates.dt.year.unique())
    yearly = []
    for y in years:
        mask = dates.dt.year == y
        ylog = log[mask]
        ypnl = ylog["pnl"].values.astype(float)
        yeq = ylog["capital_after"].values.astype(float)
        ystart = float(yeq[0]) - float(ypnl[0])
        yret = (yeq[-1] / ystart - 1.0) * 100 if ystart > 0 else 0

        ydaily = ylog.groupby("date")["pnl"].sum().values.astype(float)
        ymu = float(np.mean(ydaily))
        ysd = float(np.std(ydaily, ddof=1)) if len(ydaily) > 1 else 0
        ysh = (ymu / ysd * math.sqrt(252)) if ysd > 0 else float("nan")

        ypeaks = np.maximum.accumulate(yeq)
        ydd = float(np.min((yeq - ypeaks) / ypeaks * 100))

        ywins = ypnl[ypnl > 0]
        ylosses = ypnl[ypnl < 0]
        ygw = float(np.sum(ywins))
        ygl = float(np.sum(np.abs(ylosses)))

        yearly.append({
            "year": y,
            "n_trades": len(ylog),
            "return_pct": yret,
            "sharpe": ysh,
            "max_dd_pct": ydd,
            "win_rate_pct": len(ywins) / len(ylog) * 100 if len(ylog) else 0,
            "profit_factor": ygw / ygl if ygl > 0 else float("inf"),
            "start_equity": ystart,
            "end_equity": float(yeq[-1]),
            "total_pnl": float(np.sum(ypnl)),
        })

    return overall, yearly


def save_summary(m, yearly, out_dir, fee_bps, leverage):
    lines = [
        f"# BTC 0DTE Synthetic Straddle ({leverage}x Lev + 2 Puts) — Backtest Summary",
        "",
        f"**Leverage:** {leverage}x on spot/perp leg",
        f"**Fee:** {fee_bps} bp per leg per side",
        f"**Sizing:** {QTY} BTC per unit, {NUM_PUTS} puts per straddle, compounding",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Trades | {m['total_trades']} |",
        f"| Total P&L | ${m['total_pnl']:,.2f} |",
        f"| Total Fees | ${m['total_fees']:,.2f} |",
        f"| Total Return | {m['total_return_pct']:.2f}% |",
        f"| Annualized Return | {m['ann_return_pct']:.2f}% |",
        f"| Sharpe Ratio | {m['sharpe']:.3f} |",
        f"| Max Drawdown (%) | {m['max_dd_pct']:.2f}% |",
        f"| Max Drawdown (USD) | ${m['max_dd_usd']:,.2f} |",
        f"| Max DD Duration (trades) | {m['max_dd_duration_trades']} |",
        f"| Win Rate | {m['win_rate_pct']:.2f}% |",
        f"| TP Hit Rate | {m['tp_hit_rate_pct']:.2f}% |",
        f"| Liquidation Rate | {m['liq_rate_pct']:.2f}% |",
        f"| Average Win | ${m['avg_win']:,.2f} |",
        f"| Average Loss | ${m['avg_loss']:,.2f} |",
        f"| Profit Factor | {m['profit_factor']:.3f} |",
        "",
        "## Yearly Breakdown",
        "",
        "| Year | Trades | Return% | Sharpe | MaxDD% | WinRate% | PF | Start Eq | End Eq |",
        "|------|--------|---------|--------|--------|----------|----|----------|--------|",
    ]
    for y in yearly:
        lines.append(
            f"| {y['year']} | {y['n_trades']} | {y['return_pct']:.2f}% | "
            f"{y['sharpe']:.3f} | {y['max_dd_pct']:.2f}% | {y['win_rate_pct']:.1f}% | "
            f"{y['profit_factor']:.3f} | ${y['start_equity']:,.0f} | "
            f"${y['end_equity']:,.0f} |"
        )

    text = "\n".join(lines)
    print(text)
    (out_dir / "backtest_summary.md").write_text(text + "\n")
    pd.DataFrame(yearly).to_csv(out_dir / "yearly_metrics.csv", index=False)


def generate_plots(log, out_dir, leverage):
    dates = pd.to_datetime(log["date"]).values
    equity = log["capital_after"].values.astype(float)

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(dates, equity, linewidth=1.0, color="#2563eb")
    ax.axhline(INITIAL_CAPITAL, color="grey", linestyle="--", alpha=0.5, linewidth=0.8)
    ax.set_title(f"Equity Curve — Synthetic Straddle ({leverage}x + 2 Puts)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "backtest_equity_curve.png", dpi=150)
    plt.close(fig)

    peaks = np.maximum.accumulate(equity)
    dd_pct = (equity - peaks) / peaks * 100
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, dd_pct, 0, color="#ef4444", alpha=0.5)
    ax.set_title("Drawdown (%)")
    ax.set_xlabel("Date")
    ax.set_ylabel("DD (%)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "backtest_drawdown.png", dpi=150)
    plt.close(fig)

    log2 = log.copy()
    dates_s = pd.to_datetime(log["date"])
    log2["year"] = dates_s.dt.year
    log2["month"] = dates_s.dt.month
    mpnl = log2.groupby(["year", "month"])["pnl"].sum()
    mcount = log2.groupby(["year", "month"]).size()
    mstart = log2.groupby(["year", "month"]).apply(
        lambda g: g["capital_after"].iloc[0] - g["pnl"].iloc[0]
    )
    mret = (mpnl / mstart * 100).fillna(0)
    years = sorted(log2["year"].unique())
    ret_data = np.full((len(years), 12), np.nan)
    vol_data = np.full((len(years), 12), np.nan)
    for yi, y in enumerate(years):
        for mi in range(12):
            if (y, mi + 1) in mret.index:
                ret_data[yi, mi] = mret.loc[(y, mi + 1)]
            if (y, mi + 1) in mcount.index:
                vol_data[yi, mi] = mcount.loc[(y, mi + 1)]
    fig, ax = plt.subplots(figsize=(14, max(3, len(years) * 1.4)))
    fin = ret_data[np.isfinite(ret_data)]
    vabs = max(abs(fin.min()), abs(fin.max())) if len(fin) else 1
    if vabs == 0:
        vabs = 1
    norm = mcolors.TwoSlopeNorm(vmin=-vabs, vcenter=0, vmax=vabs)
    im = ax.imshow(ret_data, aspect="auto", cmap="RdYlGn", norm=norm)
    ax.set_xticks(range(12))
    ax.set_xticklabels(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels([str(y) for y in years])
    for yi in range(len(years)):
        for mi in range(12):
            v = ret_data[yi, mi]
            n = vol_data[yi, mi]
            if np.isfinite(v):
                n_int = int(n) if np.isfinite(n) else 0
                ax.text(mi, yi, f"{v:.1f}%\n({n_int})", ha="center", va="center",
                        fontsize=7, fontweight="bold", linespacing=1.4)
    ax.set_title("Monthly Returns (%)  —  (trades)")
    fig.colorbar(im, ax=ax, fraction=0.02, pad=0.04)
    fig.tight_layout()
    fig.savefig(out_dir / "backtest_monthly_heatmap.png", dpi=150)
    plt.close(fig)

    print(f"  Plots saved to {out_dir}/")


def main():
    args = parse_args()
    leverage = args.leverage
    fee_bps = args.fee
    tp_enabled = not args.notp
    session_filter = args.session
    fee_label = "nofee" if fee_bps == 0 else (
        f"{int(fee_bps)}bp" if fee_bps == int(fee_bps) else f"{fee_bps}bp"
    )
    lev_label = f"{int(leverage)}x" if leverage == int(leverage) else f"{leverage}x"
    tp_tag = "" if tp_enabled else "_notp"
    sess_tag = f"_S{session_filter}" if session_filter != "all" else ""
    out_dir = BASE_OUT / f"0dte_synth_{lev_label}_{fee_label}{tp_tag}{sess_tag}_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_data()
    log = run_backtest(df, fee_bps, leverage, tp_enabled=tp_enabled, session_filter=session_filter)

    if log.empty:
        print("No trades executed.")
        return

    log.to_csv(out_dir / "trade_log.csv", index=False)
    print(f"\n  Trade log saved to {out_dir / 'trade_log.csv'}")

    overall, yearly = compute_metrics(log)
    save_summary(overall, yearly, out_dir, fee_bps, leverage)
    generate_plots(log, out_dir, leverage)
    print("\nDone.")


if __name__ == "__main__":
    main()
