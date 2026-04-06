#!/usr/bin/env python3
"""
Bybit Spot 10x Synthetic Straddle — S3 Sweep
=============================================
Position per straddle unit:
  0.5 BTC spot (10x leverage) + 1 BTC notional in puts (NUM_PUTS=2 × QTY=0.5)
  = half the original 1 BTC perp + 2 BTC puts structure

Initial capital: $8,000
Session: S3 (14:00–18:00 UTC)
Sweep: 9 allocations × 2 sizing × 1 fee = 18 variants (TP disabled)
"""
import sys
import importlib.util
import csv
import itertools
import math
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "bt", str(_SCRIPT_DIR / "0dte_straddle_leveraged.py")
)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

# Patch module constants for Bybit spot setup
bt.INITIAL_CAPITAL = 8_000
bt.QTY = 0.5           # 0.5 BTC spot per straddle
bt.NUM_PUTS = 2         # NUM_PUTS * QTY = 1.0 BTC of put notional per straddle

print("=" * 70)
print("Bybit Spot Config:")
print(f"  QTY           = {bt.QTY} BTC spot per straddle")
print(f"  NUM_PUTS      = {bt.NUM_PUTS} (put notional = {bt.NUM_PUTS * bt.QTY} BTC)")
print(f"  Initial Cap   = ${bt.INITIAL_CAPITAL:,}")
print(f"  TP            = disabled (mechanic available at {bt.TP_PCT*100:.0f}% of put cost)")
print("=" * 70)

print("\nLoading data once ...", flush=True)
df = bt.load_data()
print("Data loaded.\n", flush=True)

LEVERAGE = 10
ALLOCS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
SIZING_MODES = [("flat", True), ("compound", False)]
FEE = 0

results = []
combos = list(itertools.product(ALLOCS, SIZING_MODES))
total = len(combos)

for i, (alloc, (sizing_label, is_flat)) in enumerate(combos, 1):
    alloc_label = f"{int(alloc*100)}pct"

    folder = f"0dte_bybit_s3_10x_{alloc_label}_{sizing_label}_output"
    out_dir = _SCRIPT_DIR / "output" / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{i}/{total}] 10x {alloc_label} {sizing_label} ...", flush=True)

    log = bt.run_backtest(
        df, FEE, float(LEVERAGE),
        tp_enabled=False,
        session_filter="3",
        alloc_pct=alloc,
        flat_sizing=is_flat,
    )

    if log.empty:
        print(f"  => No trades\n", flush=True)
        results.append({
            "leverage": "10x", "alloc_pct": int(alloc * 100),
            "sizing": sizing_label,
            "trades": 0, "return_pct": 0, "sharpe": 0,
            "max_dd_pct": 0, "win_rate_pct": 0,
            "liq_rate_pct": 0, "profit_factor": 0, "final_capital": bt.INITIAL_CAPITAL,
            "total_option_contracts": 0, "total_spot_btc_volume": 0,
        })
        continue

    # --- Monthly volume calculations ---
    log_copy = log.copy()
    dates_s = pd.to_datetime(log_copy["date"])
    log_copy["year"] = dates_s.dt.year
    log_copy["month"] = dates_s.dt.month

    # Each straddle = 2 puts of QTY BTC each + QTY BTC spot
    # Option contracts per straddle: buy 2 puts (entry) + sell 2 puts (exit) = 4
    # Each put is QTY = 0.5 BTC notional on Bybit
    contracts_per_straddle = bt.NUM_PUTS * 2  # 2 puts × (buy + sell) = 4
    log_copy["option_contracts_traded"] = contracts_per_straddle * log_copy["num_straddles"]
    log_copy["option_btc_notional"] = bt.NUM_PUTS * bt.QTY * 2 * log_copy["num_straddles"]

    # Spot volume: buy QTY BTC (entry) + sell QTY BTC (exit) = 2 × QTY per straddle
    log_copy["spot_btc_traded"] = 2 * bt.QTY * log_copy["num_straddles"]

    monthly_vol = log_copy.groupby(["year", "month"]).agg(
        trades=("num_straddles", "size"),
        total_straddles=("num_straddles", "sum"),
        option_contracts=("option_contracts_traded", "sum"),
        option_btc_notional=("option_btc_notional", "sum"),
        spot_btc_volume=("spot_btc_traded", "sum"),
    ).reset_index()

    monthly_vol.to_csv(out_dir / "monthly_volumes.csv", index=False)

    vol_lines = [
        f"Monthly Volume Report — 10x / {int(alloc*100)}% / {sizing_label}",
        f"Position per straddle: {bt.QTY} BTC spot + {bt.NUM_PUTS} puts × {bt.QTY} BTC each",
        f"  Option contracts per straddle: {bt.NUM_PUTS} buy + {bt.NUM_PUTS} sell = {bt.NUM_PUTS*2} contracts (each {bt.QTY} BTC)",
        f"  Spot per straddle: buy {bt.QTY} BTC + sell {bt.QTY} BTC = {bt.QTY*2} BTC",
        "",
        f"{'Month':<10} {'Trades':>7} {'Straddles':>10} {'Opt Contracts':>15} {'Opt BTC':>10} {'Spot BTC':>10}",
        "-" * 70,
    ]
    for _, row in monthly_vol.iterrows():
        m_str = f"{int(row['year'])}-{int(row['month']):02d}"
        vol_lines.append(
            f"{m_str:<10} {int(row['trades']):>7} {int(row['total_straddles']):>10} "
            f"{int(row['option_contracts']):>15} {row['option_btc_notional']:>10.1f} {row['spot_btc_volume']:>10.1f}"
        )
    vol_lines.append("-" * 70)
    vol_lines.append(
        f"{'TOTAL':<10} {monthly_vol['trades'].sum():>7} "
        f"{monthly_vol['total_straddles'].sum():>10} "
        f"{int(monthly_vol['option_contracts'].sum()):>15} "
        f"{monthly_vol['option_btc_notional'].sum():>10.1f} "
        f"{monthly_vol['spot_btc_volume'].sum():>10.1f}"
    )
    vol_text = "\n".join(vol_lines)
    (out_dir / "monthly_volumes.txt").write_text(vol_text + "\n")

    log.to_csv(out_dir / "trade_log.csv", index=False)
    overall, yearly = bt.compute_metrics(log)
    bt.save_summary(overall, yearly, out_dir, FEE, float(LEVERAGE))
    bt.generate_plots(log, out_dir, float(LEVERAGE))

    results.append({
        "leverage": "10x", "alloc_pct": int(alloc * 100),
        "sizing": sizing_label,
        "trades": overall["total_trades"],
        "return_pct": round(overall["total_return_pct"], 2),
        "sharpe": round(overall["sharpe"], 3),
        "max_dd_pct": round(overall["max_dd_pct"], 2),
        "win_rate_pct": round(overall["win_rate_pct"], 2),
        "liq_rate_pct": round(overall.get("liq_rate_pct", 0), 2),
        "profit_factor": round(overall["profit_factor"], 3),
        "final_capital": round(overall["total_pnl"] + bt.INITIAL_CAPITAL, 2),
        "total_option_contracts": round(monthly_vol["option_contracts"].sum(), 1),
        "total_spot_btc_volume": round(monthly_vol["spot_btc_volume"].sum(), 1),
    })

    print(f"  => {overall['total_trades']} trades, "
          f"return={overall['total_return_pct']:.1f}%, "
          f"sharpe={overall['sharpe']:.3f}, "
          f"final=${overall['total_pnl'] + bt.INITIAL_CAPITAL:,.0f}\n",
          flush=True)

# Save summary
out_csv = _SCRIPT_DIR / "output" / "bybit_spot_sweep_results.csv"
keys = results[0].keys()
with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=keys)
    w.writeheader()
    w.writerows(results)

print("\n" + "=" * 110)
print("BYBIT SPOT 10x SWEEP — S3 SESSION — 0.5 BTC + 2 PUTS — $8K START")
print("=" * 110)
hdr = (f"{'Alloc%':>6} {'Sizing':<10} {'Trades':>6} {'Return%':>9} {'Sharpe':>7} "
       f"{'MaxDD%':>8} {'WinR%':>6} {'PF':>6} {'Final$':>11} {'OptContr':>9} {'SpotBTC':>9}")
print(hdr)
print("-" * 110)
for r in results:
    opt_c = r.get("total_option_contracts", 0)
    spot_v = r.get("total_spot_btc_volume", 0)
    print(f"{r['alloc_pct']:>5}% {r['sizing']:<10} {r['trades']:>6} "
          f"{r['return_pct']:>8.1f}% {r['sharpe']:>7.3f} "
          f"{r['max_dd_pct']:>7.1f}% {r['win_rate_pct']:>5.1f}% {r['profit_factor']:>6.3f} "
          f"${r['final_capital']:>9,.0f} {opt_c:>9.0f} {spot_v:>9.1f}")
print("=" * 110)
print(f"\nResults saved to {out_csv}")
print("DONE.")
