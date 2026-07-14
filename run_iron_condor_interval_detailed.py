#!/usr/bin/env python3
"""
Iron Condor 0DTE — Detailed Interval Sweep
============================================
Strategy: Buy ATM Straddle + Sell OTM Wings
  - Buy 1 ATM Call + Buy 1 ATM Put  (long straddle at ATM)
  - Sell 1 Put at next strike below ATM  (short put wing, 1 strike down)
  - Sell 1 Call at 2nd strike above ATM  (short call wing, 2 strikes up)

Usage:
  python run_iron_condor_interval_detailed.py --interval 30 --day-filter weekday
  python run_iron_condor_interval_detailed.py --interval 60 --day-filter weekend --start 0900
"""

import sys, os, argparse, math, itertools, csv
from pathlib import Path
from datetime import timedelta

import pandas as pd
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

_SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BTC_DATA_DIR", str(_SCRIPT_DIR / "data")))
DATA_PATH_1 = DATA_DIR / "btc_0dte_data.parquet"
DATA_PATH_2 = DATA_DIR / "btc_0dte_data_2026.parquet"

EXCHANGE = "deribit"
FEE_BPS = 0
TP_ENABLED = False
DEFAULT_CAPITALS = [10_000]
DEFAULT_ALLOCS = [0.50]
SIZING_MODES = [("flat", True), ("compound", False)]

OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(exist_ok=True)


def _build_timings(interval_min, start_hh=8, start_mm=30):
    timings = []
    start_total = start_hh * 60 + start_mm
    end_total = start_total + 23 * 60
    t = start_total
    while t < end_total:
        entry_m = t % (24 * 60)
        close_m = (t + interval_min) % (24 * 60)
        eh, em = divmod(entry_m, 60)
        ch, cm = divmod(close_m, 60)
        entry_str = f"{eh:02d}:{em:02d}"
        close_str = f"{ch:02d}:{cm:02d}"
        tag = f"{eh:02d}{em:02d}-{ch:02d}{cm:02d}"
        timings.append((tag, entry_str, close_str))
        t += interval_min
    return timings


def load_data_all(exchange_filter=None):
    """Load both calls and puts from 0DTE parquet files."""
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
        print(f"  Exchange filter '{exchange_filter}': {n_pre:,} -> {len(df):,} rows", flush=True)

    df["daystogo"] = pd.to_numeric(df["daystogo"], errors="coerce")
    df = df[df["daystogo"] <= 1.0]
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["spot_price"] = pd.to_numeric(df["spot_price"], errors="coerce")
    df = df.dropna(subset=["premium_usd", "strike", "spot_price"])
    df = df[df["premium_usd"] > 0]
    df = df[df["spot_price"] > 10_000]

    df["date"] = df["datetime"].dt.date
    df["time_utc"] = df["datetime"].dt.strftime("%H:%M")
    df["expiry_str"] = df["expiry_date"].astype(str).str[:10]

    start_d = pd.Timestamp("2024-01-01").date()
    end_d = pd.Timestamp("2026-05-31").date()
    df = df[(df["date"] >= start_d) & (df["date"] <= end_d)]

    print(f"  Final dataset (calls+puts): {len(df):,} rows  ({df['date'].min()} to {df['date'].max()})", flush=True)
    return df


def _get_expiry_str(dt_date):
    return [str(dt_date + timedelta(days=1)), str(dt_date)]


def run_iron_condor(df, entry_time, close_time, day_filter,
                    initial_capital, alloc_pct, flat_sizing):
    fee_rate = FEE_BPS / 10_000.0

    date_groups = dict(list(df.groupby("date")))
    all_dates = sorted(date_groups.keys())

    if day_filter == "weekday":
        trading_dates = [d for d in all_dates if pd.Timestamp(d).dayofweek < 5]
    elif day_filter == "weekend":
        trading_dates = [d for d in all_dates if pd.Timestamp(d).dayofweek >= 5]
    else:
        trading_dates = all_dates

    cross_midnight = close_time <= entry_time

    equity = float(initial_capital)
    trades = []

    for day in trading_dates:
        if equity <= 0:
            break

        expiry_candidates = _get_expiry_str(day)
        day_data = date_groups.get(day, pd.DataFrame())
        if day_data.empty:
            continue

        if cross_midnight:
            next_day = day + timedelta(days=1)
            next_data = date_groups.get(next_day, pd.DataFrame())
            next_expiry = _get_expiry_str(next_day)
            all_expiry = list(set(expiry_candidates + next_expiry))
            combined = pd.concat([day_data, next_data]) if not next_data.empty else day_data
            exp_data = combined[combined["expiry_str"].isin(all_expiry)]
        else:
            exp_data = day_data[day_data["expiry_str"].isin(expiry_candidates)]

        if exp_data.empty:
            continue

        entry_snap = exp_data[exp_data["time_utc"] == entry_time]
        if entry_snap.empty:
            continue

        spot_entry = float(entry_snap["spot_price"].iloc[0])

        calls = entry_snap[entry_snap["call_put"] == "C"][["strike", "premium_usd", "expiry_str"]].drop_duplicates("strike")
        puts = entry_snap[entry_snap["call_put"] == "P"][["strike", "premium_usd", "expiry_str"]].drop_duplicates("strike")
        both = calls.merge(puts, on="strike", suffixes=("_c", "_p"))
        both = both.dropna(subset=["premium_usd_c", "premium_usd_p"])
        if both.empty:
            continue

        # ATM strike: highest strike <= spot
        itm = both[both["strike"] <= spot_entry]
        if not itm.empty:
            atm_row = itm.loc[itm["strike"].idxmax()]
        else:
            both["_d"] = (both["strike"] - spot_entry).abs()
            atm_row = both.loc[both["_d"].idxmin()]

        atm_strike = float(atm_row["strike"])
        long_call_prem = float(atm_row["premium_usd_c"])
        long_put_prem = float(atm_row["premium_usd_p"])
        entry_expiry = atm_row["expiry_str_c"]

        # --- Find short wings ---
        all_strikes = sorted(both["strike"].unique())
        atm_idx = all_strikes.index(atm_strike)

        # Short put: 1st strike below ATM
        if atm_idx < 1:
            continue
        short_put_strike = all_strikes[atm_idx - 1]

        # Short call: 2nd strike above ATM
        if atm_idx + 2 >= len(all_strikes):
            continue
        short_call_strike = all_strikes[atm_idx + 2]

        short_put_row = both[both["strike"] == short_put_strike]
        short_call_row = both[both["strike"] == short_call_strike]
        if short_put_row.empty or short_call_row.empty:
            continue

        short_put_prem = float(short_put_row["premium_usd_p"].iloc[0])
        short_call_prem = float(short_call_row["premium_usd_c"].iloc[0])

        # Net debit = long premiums - short premiums
        net_debit = (long_call_prem + long_put_prem) - (short_call_prem + short_put_prem)
        if net_debit <= 0:
            continue

        if equity < net_debit:
            continue

        sizing_capital = float(initial_capital) if flat_sizing else equity
        allocated = alloc_pct * sizing_capital
        num_units = max(1, int(math.floor(allocated / net_debit)))

        total_cost = num_units * net_debit
        if total_cost > equity:
            num_units = max(1, int(math.floor(equity / net_debit)))
            total_cost = num_units * net_debit
        if total_cost > equity:
            continue

        # --- Find exit prices for all 4 strikes ---
        exp_data_exp = exp_data[exp_data["expiry_str"].isin([entry_expiry])]
        all_4_strikes = [atm_strike, short_put_strike, short_call_strike]

        exit_snap = exp_data_exp[
            (exp_data_exp["strike"].isin(all_4_strikes)) &
            (exp_data_exp["time_utc"] == close_time)
        ]
        if exit_snap.empty:
            if cross_midnight:
                after_midnight = exp_data_exp[
                    (exp_data_exp["strike"].isin(all_4_strikes)) &
                    (exp_data_exp["datetime"] > pd.Timestamp(f"{day} {entry_time}:00", tz="UTC")) &
                    (exp_data_exp["datetime"] <= pd.Timestamp(f"{day} {entry_time}:00", tz="UTC") + pd.Timedelta(hours=24))
                ]
                if not after_midnight.empty:
                    max_dt = after_midnight["datetime"].max()
                    exit_snap = after_midnight[after_midnight["datetime"] == max_dt]
            else:
                before = exp_data_exp[
                    (exp_data_exp["strike"].isin(all_4_strikes)) &
                    (exp_data_exp["time_utc"] <= close_time)
                ]
                if not before.empty:
                    max_time = before["time_utc"].max()
                    exit_snap = before[before["time_utc"] == max_time]

        if exit_snap.empty:
            continue

        # Extract exit premiums for all 4 legs
        exit_long_call = exit_snap[(exit_snap["strike"] == atm_strike) & (exit_snap["call_put"] == "C")]
        exit_long_put = exit_snap[(exit_snap["strike"] == atm_strike) & (exit_snap["call_put"] == "P")]
        exit_short_put = exit_snap[(exit_snap["strike"] == short_put_strike) & (exit_snap["call_put"] == "P")]
        exit_short_call = exit_snap[(exit_snap["strike"] == short_call_strike) & (exit_snap["call_put"] == "C")]

        if exit_long_call.empty or exit_long_put.empty or exit_short_put.empty or exit_short_call.empty:
            continue

        exit_long_call_prem = float(exit_long_call["premium_usd"].iloc[0])
        exit_long_put_prem = float(exit_long_put["premium_usd"].iloc[0])
        exit_short_put_prem = float(exit_short_put["premium_usd"].iloc[0])
        exit_short_call_prem = float(exit_short_call["premium_usd"].iloc[0])
        spot_exit = float(exit_long_call["spot_price"].iloc[0])
        exit_dt = str(exit_long_call["datetime"].iloc[0])

        # PnL per unit
        long_call_pnl = exit_long_call_prem - long_call_prem
        long_put_pnl = exit_long_put_prem - long_put_prem
        short_put_pnl = short_put_prem - exit_short_put_prem
        short_call_pnl = short_call_prem - exit_short_call_prem

        unit_pnl = long_call_pnl + long_put_pnl + short_put_pnl + short_call_pnl
        gross_pnl = num_units * unit_pnl

        entry_fee = num_units * 4 * fee_rate * spot_entry
        exit_fee = num_units * 4 * fee_rate * spot_exit
        total_fees = entry_fee + exit_fee

        net_pnl = gross_pnl - total_fees
        equity += net_pnl

        trades.append({
            "date": day,
            "entry_time": f"{day} {entry_time}:00+00:00",
            "exit_time": exit_dt,
            "exit_reason": "hard_close",
            "spot_entry": spot_entry,
            "spot_exit": spot_exit,
            "atm_strike": atm_strike,
            "short_put_strike": short_put_strike,
            "short_call_strike": short_call_strike,
            "long_call_prem_entry": long_call_prem,
            "long_call_prem_exit": exit_long_call_prem,
            "long_put_prem_entry": long_put_prem,
            "long_put_prem_exit": exit_long_put_prem,
            "short_put_prem_entry": short_put_prem,
            "short_put_prem_exit": exit_short_put_prem,
            "short_call_prem_entry": short_call_prem,
            "short_call_prem_exit": exit_short_call_prem,
            "net_debit_per_unit": net_debit,
            "num_units": num_units,
            "entry_cost": total_cost,
            "long_call_pnl": num_units * long_call_pnl,
            "long_put_pnl": num_units * long_put_pnl,
            "short_put_pnl": num_units * short_put_pnl,
            "short_call_pnl": num_units * short_call_pnl,
            "gross_pnl": gross_pnl,
            "fees": total_fees,
            "pnl": net_pnl,
            "pnl_pct": net_pnl / total_cost if total_cost > 0 else 0,
            "capital_after": equity,
        })

    return pd.DataFrame(trades)


def _segment_metrics(pnl, equity, start_eq):
    n = len(pnl)
    if n == 0:
        return {}
    total_pnl = float(np.sum(pnl))
    end_eq = float(equity[-1])
    cum_return = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0
    mu = float(np.mean(pnl))
    sd = float(np.std(pnl, ddof=1)) if n > 1 else 0.0
    sharpe = (mu / sd * math.sqrt(252)) if sd > 0 else 0.0
    peaks = np.maximum.accumulate(equity)
    dd_pct = (equity - peaks) / peaks * 100.0
    max_dd_pct = float(np.min(dd_pct))
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = float(len(wins) / n * 100.0)
    gross_win = float(np.sum(wins))
    gross_loss = float(np.sum(np.abs(losses)))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    return {
        "n_trades": n, "total_pnl_usd": total_pnl, "return_pct": cum_return * 100.0,
        "sharpe": sharpe, "max_dd_pct": max_dd_pct, "win_rate_pct": win_rate,
        "profit_factor": profit_factor, "start_equity": start_eq, "end_equity": end_eq,
    }


def compute_metrics(log, initial_capital):
    pnl = log["pnl"].values.astype(float)
    equity = log["capital_after"].values.astype(float)
    n = len(log)
    if n == 0:
        return {}, []

    total_pnl = float(np.sum(pnl))
    total_return_pct = total_pnl / initial_capital * 100.0

    mu = float(np.mean(pnl))
    sd = float(np.std(pnl, ddof=1)) if n > 1 else 0.0
    sharpe = (mu / sd * math.sqrt(252)) if sd > 0 else 0.0

    peaks = np.maximum.accumulate(equity)
    dd_usd = equity - peaks
    dd_pct = dd_usd / peaks * 100.0
    max_dd_usd = float(np.min(dd_usd))
    max_dd_pct = float(np.min(dd_pct)) if len(dd_pct) > 0 else 0.0

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = float(len(wins) / n * 100.0) if n > 0 else 0.0
    avg_win = float(np.mean(wins)) if len(wins) else 0.0
    avg_loss = float(np.mean(losses)) if len(losses) else 0.0
    gross_win = float(np.sum(wins))
    gross_loss = float(np.sum(np.abs(losses)))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_fees = float(log["fees"].sum())

    overall = {
        "total_trades": n, "win_rate_pct": win_rate,
        "total_pnl_usd": total_pnl, "avg_daily_pnl_usd": mu,
        "total_return_pct": total_return_pct,
        "sharpe": sharpe, "max_drawdown_usd": max_dd_usd,
        "max_dd_pct": max_dd_pct,
        "avg_win_usd": avg_win, "avg_loss_usd": avg_loss,
        "profit_factor": profit_factor,
        "total_fees": total_fees,
    }

    dates = pd.to_datetime(log["date"])
    years = sorted(dates.dt.year.unique())
    yearly = []
    for y in years:
        mask = dates.dt.year == y
        y_log = log[mask]
        y_pnl = y_log["pnl"].values.astype(float)
        y_eq = y_log["capital_after"].values.astype(float)
        y_start = float(y_eq[0]) - float(y_pnl[0])
        seg = _segment_metrics(y_pnl, y_eq, y_start)
        seg["year"] = y
        yearly.append(seg)

    return overall, yearly


def save_metrics(overall, yearly, out_dir, label, initial_capital):
    lines = [
        "=" * 65,
        f"  Iron Condor 0DTE — {label}",
        "=" * 65,
        f"  Total trades:            {overall['total_trades']}",
        f"  Win rate:                {overall['win_rate_pct']:.2f}%",
        f"  Total PnL:               ${overall['total_pnl_usd']:,.2f}",
        f"  Total fees:              ${overall['total_fees']:,.2f}",
        f"  Avg daily PnL:           ${overall['avg_daily_pnl_usd']:,.2f}",
        f"  Cumulative return:       {overall['total_return_pct']:.2f}%",
        f"  Sharpe ratio:            {overall['sharpe']:.3f}",
        f"  Max drawdown (USD):      ${overall['max_drawdown_usd']:,.2f}",
        f"  Max drawdown (%):        {overall['max_dd_pct']:.2f}%",
        f"  Avg win:                 ${overall['avg_win_usd']:,.2f}",
        f"  Avg loss:                ${overall['avg_loss_usd']:,.2f}",
        f"  Profit factor:           {overall['profit_factor']:.3f}",
        "=" * 65, "",
        "  YEARLY BREAKDOWN",
        "-" * 65,
        f"  {'Year':>6}  {'Trades':>6}  {'Return%':>10}  {'Sharpe':>8}  {'MaxDD%':>8}  {'WinRate%':>9}  {'PF':>6}",
        "-" * 65,
    ]
    for y in yearly:
        lines.append(
            f"  {y['year']:>6}  {y['n_trades']:>6}  {y['return_pct']:>9.2f}%  "
            f"{y['sharpe']:>8.3f}  {y['max_dd_pct']:>7.2f}%  "
            f"{y['win_rate_pct']:>8.2f}%  {y['profit_factor']:>6.3f}"
        )
    lines.append("-" * 65)
    text = "\n".join(lines)
    print(text)
    (out_dir / "metrics.txt").write_text(text + "\n")
    pd.DataFrame(yearly).to_csv(out_dir / "yearly_metrics.csv", index=False)


def generate_plots(log, out_dir, initial_capital):
    dates = pd.to_datetime(log["date"]).values
    equity = log["capital_after"].values.astype(float)
    pnl = log["pnl"].values.astype(float)

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(dates, equity, linewidth=1.2, color="#2563eb")
    ax.axhline(initial_capital, color="grey", linestyle="--", alpha=0.5, linewidth=0.8)
    ax.set_title("Iron Condor Equity Curve (USD)"); ax.set_xlabel("Date"); ax.set_ylabel("Equity ($)"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "equity_curve.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(14, 5))
    colors = ["#22c55e" if p > 0 else "#ef4444" for p in pnl]
    ax.bar(dates, pnl, color=colors, width=1.0, edgecolor="none")
    ax.axhline(0, color="grey", linewidth=0.5)
    ax.set_title("Iron Condor Daily PnL (USD)"); ax.set_xlabel("Date"); ax.set_ylabel("PnL ($)"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "daily_pnl.png", dpi=150); plt.close(fig)

    peaks = np.maximum.accumulate(equity)
    dd_pct = (equity - peaks) / peaks * 100.0
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, dd_pct, 0, color="#ef4444", alpha=0.5)
    ax.set_title("Iron Condor Drawdown (%)"); ax.set_xlabel("Date"); ax.set_ylabel("DD (%)"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "drawdown.png", dpi=150); plt.close(fig)

    log_dt = log.copy()
    dates_s = pd.to_datetime(log["date"])
    log_dt["year"] = dates_s.dt.year; log_dt["month"] = dates_s.dt.month
    monthly_pnl = log_dt.groupby(["year", "month"])["pnl"].sum()
    monthly_start_eq = log_dt.groupby(["year", "month"]).apply(
        lambda g: g["capital_after"].iloc[0] - g["pnl"].iloc[0]
    )
    monthly_ret = (monthly_pnl / monthly_start_eq * 100.0).fillna(0)
    years = sorted(log_dt["year"].unique())
    data = np.full((len(years), 12), np.nan)
    for yi, y in enumerate(years):
        for mi in range(12):
            if (y, mi + 1) in monthly_ret.index:
                data[yi, mi] = monthly_ret.loc[(y, mi + 1)]
    fig, ax = plt.subplots(figsize=(12, max(3, len(years) * 1.2)))
    fin = data[np.isfinite(data)]
    vabs = max(abs(fin.min()), abs(fin.max())) if len(fin) > 0 else 1
    if vabs == 0: vabs = 1
    norm = mcolors.TwoSlopeNorm(vmin=-vabs, vcenter=0, vmax=vabs)
    im = ax.imshow(data, aspect="auto", cmap="RdYlGn", norm=norm)
    ax.set_xticks(range(12))
    ax.set_xticklabels(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])
    ax.set_yticks(range(len(years))); ax.set_yticklabels([str(y) for y in years])
    for yi in range(len(years)):
        for mi in range(12):
            v = data[yi, mi]
            if np.isfinite(v):
                ax.text(mi, yi, f"{v:.1f}%", ha="center", va="center", fontsize=8)
    ax.set_title("Iron Condor Monthly Returns (%)")
    fig.colorbar(im, ax=ax, fraction=0.02, pad=0.04)
    fig.tight_layout(); fig.savefig(out_dir / "monthly_heatmap.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(pnl, bins=60, color="#6366f1", edgecolor="white", alpha=0.8)
    ax.axvline(np.mean(pnl), color="#ef4444", linestyle="--", label=f"Mean: ${np.mean(pnl):,.0f}")
    ax.axvline(np.median(pnl), color="#22c55e", linestyle="--", label=f"Median: ${np.median(pnl):,.0f}")
    ax.legend(); ax.set_title("Iron Condor Daily PnL Distribution")
    ax.set_xlabel("PnL ($)"); ax.set_ylabel("Frequency"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "pnl_distribution.png", dpi=150); plt.close(fig)


def run_sweep(timings, day_filter, df, interval_label, capitals=None, allocs=None):
    caps = capitals if capitals is not None else DEFAULT_CAPITALS
    alcs = allocs if allocs is not None else DEFAULT_ALLOCS
    combos = list(itertools.product(
        timings, caps, alcs, SIZING_MODES,
    ))
    total = len(combos)
    print(f"Total configurations: {total}\n", flush=True)

    results = []
    for i, ((tag, entry_t, close_t), cap, alloc, (sz_label, is_flat)) in enumerate(combos, 1):
        cap_label = f"{cap // 1000}k"
        alloc_int = int(alloc * 100)

        folder_name = f"iron_condor_{interval_label}_{day_filter}_{tag}_{cap_label}_{alloc_int}pct_{sz_label}_notp"
        config_dir = OUT_DIR / folder_name
        config_dir.mkdir(parents=True, exist_ok=True)

        label = f"{day_filter} {tag} ${cap_label} {alloc_int}% {sz_label}"
        print(f"[{i}/{total}] {label}", flush=True)

        log = run_iron_condor(
            df, entry_t, close_t, day_filter,
            initial_capital=cap, alloc_pct=alloc, flat_sizing=is_flat,
        )

        if log.empty:
            print(f"  => No trades\n", flush=True)
            results.append({
                "timing": tag, "capital": cap, "alloc_pct": alloc_int,
                "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
                "trades": 0, "return_pct": 0, "sharpe": 0,
                "max_dd_pct": 0, "win_rate_pct": 0,
                "profit_factor": 0, "final_capital": cap, "total_fees": 0,
            })
            continue

        log.to_csv(config_dir / "trade_log.csv", index=False)
        overall, yearly = compute_metrics(log, cap)
        save_metrics(overall, yearly, config_dir, label, cap)
        generate_plots(log, config_dir, cap)

        results.append({
            "timing": tag, "capital": cap, "alloc_pct": alloc_int,
            "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
            "trades": overall["total_trades"],
            "return_pct": round(overall["total_return_pct"], 2),
            "sharpe": round(overall["sharpe"], 3),
            "max_dd_pct": round(overall["max_dd_pct"], 2),
            "win_rate_pct": round(overall["win_rate_pct"], 2),
            "profit_factor": round(overall["profit_factor"], 3),
            "final_capital": round(overall["total_pnl_usd"] + cap, 2),
            "total_fees": round(overall["total_fees"], 2),
        })
        print(
            f"  => {overall['total_trades']} trades, "
            f"return={overall['total_return_pct']:.2f}%, "
            f"sharpe={overall['sharpe']:.3f}\n",
            flush=True,
        )

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, required=True, choices=[30, 60, 120])
    parser.add_argument("--day-filter", required=True, choices=["weekday", "weekend"])
    parser.add_argument("--start", type=str, default="0830",
                        help="Start time HHMM (default 0830)")
    parser.add_argument("--capital", type=int, nargs="+", default=None,
                        help="Starting capital(s), e.g. --capital 10000")
    parser.add_argument("--alloc", type=int, nargs="+", default=None,
                        help="Allocation percent(s), e.g. --alloc 50")
    args = parser.parse_args()

    interval_min = args.interval
    day_filter = args.day_filter
    start_hh = int(args.start[:2])
    start_mm = int(args.start[2:])
    timings = _build_timings(interval_min, start_hh, start_mm)
    start_tag = f"s{args.start}" if args.start != "0830" else ""
    int_label = f"{interval_min}min{start_tag}"

    capitals = args.capital if args.capital else list(DEFAULT_CAPITALS)
    allocs = [a / 100.0 for a in args.alloc] if args.alloc else list(DEFAULT_ALLOCS)

    cap_tag = "_".join(f"{c // 1000}k" for c in capitals)
    alloc_tag = "_".join(f"{int(a * 100)}pct" for a in allocs)

    print(f"=== Iron Condor 0DTE Detailed Sweep: {int_label}, {day_filter} ===", flush=True)
    print(f"Wings: short put 1 strike below ATM, short call 2 strikes above ATM", flush=True)
    print(f"Windows: {len(timings)}", flush=True)
    print(f"Capitals: {capitals}", flush=True)
    print(f"Allocations: {[int(a*100) for a in allocs]}%", flush=True)
    print(f"Sizing: {[s[0] for s in SIZING_MODES]}", flush=True)
    print(f"TP: {TP_ENABLED}, Fee: {FEE_BPS}bps, Day filter: {day_filter}", flush=True)
    print(f"Exchange: {EXCHANGE}\n", flush=True)

    print("Loading data (deribit only, calls+puts) ...", flush=True)
    df = load_data_all(exchange_filter=EXCHANGE)
    print("Data loaded.\n", flush=True)

    results = run_sweep(timings, day_filter, df, int_label, capitals=capitals, allocs=allocs)

    out_csv = OUT_DIR / f"iron_condor_{int_label}_{day_filter}_{cap_tag}_{alloc_tag}_detailed_results.csv"
    if results:
        keys = results[0].keys()
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results)
        print(f"\nSummary saved to {out_csv}")

    print("ALL DONE.")


if __name__ == "__main__":
    main()
