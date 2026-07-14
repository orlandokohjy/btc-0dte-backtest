#!/usr/bin/env python3
"""
BTC Pure Straddle 1DTE — Detailed Interval Sweep
==================================================
Strategy: Long 1 ATM Call + Long 1 ATM Put (options only, no spot/perp)
Uses 1DTE options (daystogo 1.0-2.0) instead of 0DTE.

Usage:
  python run_btc_1dte_pure_straddle_interval_detailed.py --interval 30 --day-filter weekday
  python run_btc_1dte_pure_straddle_interval_detailed.py --interval 60 --day-filter weekend --start 0900
  python run_btc_1dte_pure_straddle_interval_detailed.py --interval 120 --day-filter weekday --capital 10000 --alloc 50
"""

import sys, os, argparse, math, itertools, csv, glob
from pathlib import Path
from datetime import timedelta

import pandas as pd
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

_SCRIPT_DIR = Path(__file__).resolve().parent
BTC_1DTE_DATA_DIR = Path(os.environ.get("BTC_1DTE_DATA_DIR", "/Users/jiayikoh/Downloads/BTC_data_1DTE"))

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


def load_btc_1dte_data(exchange_filter=None):
    """Load all monthly BTC 1DTE parquet files."""
    files = sorted(glob.glob(str(BTC_1DTE_DATA_DIR / "btc_1dte_*.parquet")))
    if not files:
        raise FileNotFoundError(f"No BTC 1DTE parquet files found in {BTC_1DTE_DATA_DIR}")

    print(f"Loading {len(files)} BTC 1DTE parquet files ...", flush=True)
    dfs = []
    for f in files:
        chunk = pd.read_parquet(
            f,
            columns=["datetime", "expiry_date", "strike", "call_put",
                     "spot_price", "premium", "daystogo", "exchange"],
        )
        dfs.append(chunk)
        print(f"  {Path(f).name}: {len(chunk):,} rows", flush=True)

    df = pd.concat(dfs, ignore_index=True)
    del dfs
    print(f"  Combined: {len(df):,} rows", flush=True)

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df["spot_price"] = pd.to_numeric(df["spot_price"], errors="coerce")
    df["premium"] = pd.to_numeric(df["premium"], errors="coerce")

    if exchange_filter:
        n_pre = len(df)
        df = df[df["exchange"] == exchange_filter]
        print(f"  Exchange filter '{exchange_filter}': {n_pre:,} → {len(df):,} rows", flush=True)

    # Premium conversion: Deribit premium is in BTC, needs * spot_price for USD
    usd_mask = df["exchange"].isin(["bybit", "binance"])
    btc_mask = ~usd_mask
    df["premium_usd"] = 0.0
    df.loc[usd_mask, "premium_usd"] = df.loc[usd_mask, "premium"]
    df.loc[btc_mask, "premium_usd"] = (
        df.loc[btc_mask, "premium"] * df.loc[btc_mask, "spot_price"]
    )

    df["daystogo"] = pd.to_numeric(df["daystogo"], errors="coerce")
    df = df[(df["daystogo"] >= 1.0) & (df["daystogo"] <= 2.1)]
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["premium_usd", "strike", "spot_price"])
    df = df[df["premium_usd"] > 0]
    df = df[df["spot_price"] > 10_000]

    df["date"] = df["datetime"].dt.date
    df["time_utc"] = df["datetime"].dt.strftime("%H:%M")
    df["expiry_str"] = df["expiry_date"].astype(str).str[:10]

    print(f"  Final dataset (calls+puts): {len(df):,} rows  ({df['date'].min()} to {df['date'].max()})", flush=True)
    return df


def _get_expiry_str(dt_date):
    # 1DTE: options expire tomorrow (before 08:00 UTC) or day-after-tomorrow (after 08:00 UTC)
    return [str(dt_date + timedelta(days=2)), str(dt_date + timedelta(days=1))]


def run_pure_straddle(df, entry_time, close_time, day_filter,
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

        # ATM strike: highest strike <= spot (ITM call, OTM put)
        itm = both[both["strike"] <= spot_entry]
        if not itm.empty:
            row = itm.loc[itm["strike"].idxmax()]
        else:
            both["_d"] = (both["strike"] - spot_entry).abs()
            row = both.loc[both["_d"].idxmin()]

        strike = float(row["strike"])
        call_prem = float(row["premium_usd_c"])
        put_prem = float(row["premium_usd_p"])
        entry_expiry = row["expiry_str_c"]
        straddle_cost_usd = call_prem + put_prem

        if straddle_cost_usd <= 0 or equity < straddle_cost_usd:
            continue

        sizing_capital = float(initial_capital) if flat_sizing else equity
        allocated = alloc_pct * sizing_capital
        num_straddles = max(1, int(math.floor(allocated / straddle_cost_usd)))

        total_cost = num_straddles * straddle_cost_usd
        if total_cost > equity:
            num_straddles = max(1, int(math.floor(equity / straddle_cost_usd)))
            total_cost = num_straddles * straddle_cost_usd
        if total_cost > equity:
            continue

        exp_data_exp = exp_data[exp_data["expiry_str"].isin([entry_expiry])]

        exit_snap = exp_data_exp[
            (exp_data_exp["strike"] == strike) & (exp_data_exp["time_utc"] == close_time)
        ]
        if exit_snap.empty:
            before = exp_data_exp[
                (exp_data_exp["strike"] == strike) & (exp_data_exp["time_utc"] <= close_time)
            ]
            if cross_midnight:
                after_midnight = exp_data_exp[
                    (exp_data_exp["strike"] == strike) &
                    (exp_data_exp["datetime"] > pd.Timestamp(f"{day} {entry_time}:00", tz="UTC")) &
                    (exp_data_exp["datetime"] <= pd.Timestamp(f"{day} {entry_time}:00", tz="UTC") + pd.Timedelta(hours=24))
                ]
                if not after_midnight.empty:
                    exit_snap = after_midnight[after_midnight["datetime"] == after_midnight["datetime"].max()]
            elif not before.empty:
                exit_snap = before[before["time_utc"] == before["time_utc"].max()]

        if exit_snap.empty:
            continue

        ec = exit_snap[exit_snap["call_put"] == "C"]
        ep = exit_snap[exit_snap["call_put"] == "P"]
        if ec.empty or ep.empty:
            continue

        exit_call_prem = float(ec["premium_usd"].iloc[0])
        exit_put_prem = float(ep["premium_usd"].iloc[0])
        spot_exit = float(ec["spot_price"].iloc[0])
        exit_dt = str(ec["datetime"].iloc[0])

        call_pnl = num_straddles * (exit_call_prem - call_prem)
        put_pnl = num_straddles * (exit_put_prem - put_prem)
        gross_pnl = call_pnl + put_pnl

        entry_fee = num_straddles * 2 * fee_rate * spot_entry
        exit_fee = num_straddles * 2 * fee_rate * spot_exit
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
            "strike": strike,
            "call_prem_entry": call_prem,
            "call_prem_exit": exit_call_prem,
            "put_prem_entry": put_prem,
            "put_prem_exit": exit_put_prem,
            "straddle_cost_usd": straddle_cost_usd,
            "num_straddles": num_straddles,
            "entry_cost": total_cost,
            "call_pnl": call_pnl,
            "put_pnl": put_pnl,
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
    ann_return = ((1 + total_pnl / initial_capital) ** (252.0 / n) - 1.0) if n > 0 and total_pnl / initial_capital > -1 else float("nan")

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
        f"  BTC Pure Straddle 1DTE — {label}",
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
    ax.set_title("BTC 1DTE Equity Curve (USD)"); ax.set_xlabel("Date"); ax.set_ylabel("Equity ($)"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "equity_curve.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(14, 5))
    colors = ["#22c55e" if p > 0 else "#ef4444" for p in pnl]
    ax.bar(dates, pnl, color=colors, width=1.0, edgecolor="none")
    ax.axhline(0, color="grey", linewidth=0.5)
    ax.set_title("BTC 1DTE Daily PnL (USD)"); ax.set_xlabel("Date"); ax.set_ylabel("PnL ($)"); ax.grid(True, alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "daily_pnl.png", dpi=150); plt.close(fig)

    peaks = np.maximum.accumulate(equity)
    dd_pct = (equity - peaks) / peaks * 100.0
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, dd_pct, 0, color="#ef4444", alpha=0.5)
    ax.set_title("BTC 1DTE Drawdown (%)"); ax.set_xlabel("Date"); ax.set_ylabel("DD (%)"); ax.grid(True, alpha=0.3)
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
    ax.set_title("BTC 1DTE Monthly Returns (%)")
    fig.colorbar(im, ax=ax, fraction=0.02, pad=0.04)
    fig.tight_layout(); fig.savefig(out_dir / "monthly_heatmap.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(pnl, bins=60, color="#6366f1", edgecolor="white", alpha=0.8)
    ax.axvline(np.mean(pnl), color="#ef4444", linestyle="--", label=f"Mean: ${np.mean(pnl):,.0f}")
    ax.axvline(np.median(pnl), color="#22c55e", linestyle="--", label=f"Median: ${np.median(pnl):,.0f}")
    ax.legend(); ax.set_title("BTC 1DTE Daily PnL Distribution")
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

        folder_name = f"btc_1dte_pure_straddle_{interval_label}_{day_filter}_{tag}_{cap_label}_{alloc_int}pct_{sz_label}_notp"
        config_dir = OUT_DIR / folder_name
        config_dir.mkdir(parents=True, exist_ok=True)

        label = f"{day_filter} {tag} ${cap_label} {alloc_int}% {sz_label}"
        print(f"[{i}/{total}] {label}", flush=True)

        log = run_pure_straddle(
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

    print(f"=== BTC 1DTE Pure Straddle Detailed Sweep: {int_label}, {day_filter} ===", flush=True)
    print(f"Windows: {len(timings)}", flush=True)
    print(f"Capitals: {capitals}", flush=True)
    print(f"Allocations: {[int(a*100) for a in allocs]}%", flush=True)
    print(f"Sizing: {[s[0] for s in SIZING_MODES]}", flush=True)
    print(f"TP: {TP_ENABLED}, Fee: {FEE_BPS}bps, Day filter: {day_filter}", flush=True)
    print(f"Exchange: {EXCHANGE}\n", flush=True)

    print("Loading BTC 1DTE data (deribit only, calls+puts) ...", flush=True)
    df = load_btc_1dte_data(exchange_filter=EXCHANGE)
    print("Data loaded.\n", flush=True)

    results = run_sweep(timings, day_filter, df, int_label, capitals=capitals, allocs=allocs)

    out_csv = OUT_DIR / f"btc_1dte_pure_straddle_{int_label}_{day_filter}_{cap_tag}_{alloc_tag}_detailed_results.csv"
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
