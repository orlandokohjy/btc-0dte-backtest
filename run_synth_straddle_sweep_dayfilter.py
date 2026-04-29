#!/usr/bin/env python3
"""
Synthetic Straddle 0DTE Sweep — Deribit Only — Weekday/Weekend Split
=====================================================================
Strategy: Long 1 BTC Perp (leveraged) + Long 2 ITM Puts (full premium)
Data: Deribit only
Fee: 0 (no fee)
TP: disabled (no take-profit)
Leverage: 25x, 50x (on perp leg)
Capital: 8K, 10K
Allocation: 10% to 90%
Sizing: flat, compound
Day filter: weekday or weekend (via --day-filter)

Usage:
  python run_synth_straddle_sweep_dayfilter.py --day-filter weekday
  python run_synth_straddle_sweep_dayfilter.py --day-filter weekend
  python run_synth_straddle_sweep_dayfilter.py --day-filter weekday --timing 1400-1800
"""

import sys
import argparse
import importlib.util
import itertools
import csv
import math
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "bt",
    str(Path(__file__).resolve().parent / "0dte_straddle_leveraged.py"),
)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

EXCHANGE = "deribit"
FEE = 0
TP_ENABLED = False
LEVERAGES = [25, 50]
CAPITALS = [8_000, 10_000]
ALLOCS = [round(x * 0.01, 2) for x in range(10, 100, 10)]  # 0.10 to 0.90
SIZING_MODES = [("flat", True), ("compound", False)]
DAY_FILTER = None  # set via --day-filter arg

ALL_TIMINGS = [
    ("1000-1400", "10:00", "14:00"),
    ("1000-1200", "10:00", "12:00"),
    ("1200-1400", "12:00", "14:00"),
    ("1400-1600", "14:00", "16:00"),
    ("1600-1800", "16:00", "18:00"),
    ("0830-1000", "08:30", "10:00"),
    ("0900-1000", "09:00", "10:00"),
    ("1400-1800", "14:00", "18:00"),
    ("0830-1300", "08:30", "13:00"),
    ("0830-1400", "08:30", "14:00"),
    ("1800-2000", "18:00", "20:00"),
    ("2000-2200", "20:00", "22:00"),
    ("2200-2400", "22:00", "23:59"),
    ("0000-0200", "00:00", "02:00"),
    ("0200-0400", "02:00", "04:00"),
    ("0400-0600", "04:00", "06:00"),
    ("0600-0800", "06:00", "08:00"),
]

OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(exist_ok=True)


def run_sweep(timings_to_run, df):
    combos = list(itertools.product(
        timings_to_run, LEVERAGES, CAPITALS, ALLOCS, SIZING_MODES,
    ))
    total = len(combos)
    print(f"Total configurations: {total}\n", flush=True)

    results = []

    for i, ((tag, entry_t, close_t), lev, cap, alloc, (sz_label, is_flat)) in enumerate(combos, 1):
        lev_label = f"{lev}x"
        cap_label = f"{cap // 1000}k"
        alloc_pct_int = int(alloc * 100)

        folder = f"0dte_synth_{DAY_FILTER}_{tag}_{lev_label}_{cap_label}_{alloc_pct_int}pct_{sz_label}_notp_output"
        out_path = OUT_DIR / folder
        out_path.mkdir(parents=True, exist_ok=True)

        print(
            f"[{i}/{total}] {tag} {lev_label} ${cap_label} {alloc_pct_int}% {sz_label}",
            flush=True,
        )

        bt.INITIAL_CAPITAL = cap

        log = bt.run_backtest_custom(
            df, FEE, float(lev),
            entry_time=entry_t,
            close_time=close_t,
            day_filter=DAY_FILTER,
            tp_enabled=TP_ENABLED,
            alloc_pct=alloc,
            flat_sizing=is_flat,
        )

        if log.empty:
            print(f"  => No trades\n", flush=True)
            results.append({
                "timing": tag, "leverage": lev_label,
                "capital": cap, "alloc_pct": alloc_pct_int,
                "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
                "trades": 0, "return_pct": 0, "sharpe": 0,
                "max_dd_pct": 0, "win_rate_pct": 0,
                "liq_rate_pct": 0, "profit_factor": 0,
                "final_capital": cap, "total_fees": 0,
            })
            continue

        log.to_csv(out_path / "trade_log.csv", index=False)
        overall, yearly = bt.compute_metrics(log)
        bt.save_summary(overall, yearly, out_path, FEE, float(lev))

        results.append({
            "timing": tag, "leverage": lev_label,
            "capital": cap, "alloc_pct": alloc_pct_int,
            "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
            "trades": overall["total_trades"],
            "return_pct": round(overall["total_return_pct"], 2),
            "sharpe": round(overall["sharpe"], 3),
            "max_dd_pct": round(overall["max_dd_pct"], 2),
            "win_rate_pct": round(overall["win_rate_pct"], 2),
            "liq_rate_pct": round(overall.get("liq_rate_pct", 0), 2),
            "profit_factor": round(overall["profit_factor"], 3),
            "final_capital": round(overall["total_pnl"] + cap, 2),
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
    global DAY_FILTER
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timing", action="append", default=None,
        help="Timing tag to run (e.g. 1400-1800). Can specify multiple. Default: all.",
    )
    parser.add_argument(
        "--day-filter", required=True, choices=["weekday", "weekend"],
        help="Day filter: weekday (Mon-Fri) or weekend (Sat-Sun)",
    )
    args = parser.parse_args()
    DAY_FILTER = args.day_filter

    if args.timing:
        tag_map = {t[0]: t for t in ALL_TIMINGS}
        timings_to_run = []
        for t in args.timing:
            if t in tag_map:
                timings_to_run.append(tag_map[t])
            else:
                print(f"WARNING: Unknown timing '{t}', skipping.", flush=True)
        if not timings_to_run:
            print("No valid timings specified. Exiting.")
            return
    else:
        timings_to_run = ALL_TIMINGS

    timing_tags = [t[0] for t in timings_to_run]
    print(f"Timings to run: {timing_tags}", flush=True)
    print(f"Leverages: {LEVERAGES}", flush=True)
    print(f"Capitals: {CAPITALS}", flush=True)
    print(f"Allocations: {[int(a*100) for a in ALLOCS]}%", flush=True)
    print(f"Sizing: {[s[0] for s in SIZING_MODES]}", flush=True)
    print(f"TP: {TP_ENABLED}, Fee: {FEE}bps, Day filter: {DAY_FILTER}", flush=True)
    print(f"Exchange: {EXCHANGE}\n", flush=True)

    print(f"Loading data once ({EXCHANGE} only) ...", flush=True)
    df = bt.load_data(exchange_filter=EXCHANGE)
    print("Data loaded.\n", flush=True)

    results = run_sweep(timings_to_run, df)

    tag_suffix = "_".join(timing_tags) if len(timing_tags) <= 3 else f"{len(timing_tags)}timings"
    out_csv = OUT_DIR / f"synth_straddle_{DAY_FILTER}_{tag_suffix}_results.csv"
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
