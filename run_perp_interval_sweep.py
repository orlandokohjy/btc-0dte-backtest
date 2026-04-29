#!/usr/bin/env python3
"""
Perp Synthetic Straddle 0DTE — Fine-Grained Interval Sweep
===========================================================
Runs 30-min or 1-hr windows from 08:30 UTC → 07:30 UTC next day.

Config matches prior perp sweeps:
  QTY=1.0, Leverage 25x/50x, Capital 8K/10K,
  Alloc 10-90%, flat/compound, noTP, 0 fee, Deribit only.

Usage:
  python run_perp_interval_sweep.py --interval 30 --day-filter weekday
  python run_perp_interval_sweep.py --interval 60 --day-filter weekend
"""

import sys, argparse, importlib.util, itertools, csv
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
ALLOCS = [round(x * 0.01, 2) for x in range(10, 100, 10)]
SIZING_MODES = [("flat", True), ("compound", False)]

OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(exist_ok=True)


def _build_timings(interval_min):
    """Generate (tag, entry_time, close_time) tuples spanning 08:30→07:30."""
    timings = []
    start_total = 8 * 60 + 30  # 08:30 in minutes
    end_total = start_total + 23 * 60  # 07:30 next day = 08:30 + 23h
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


def run_sweep(timings, day_filter, df):
    combos = list(itertools.product(
        timings, LEVERAGES, CAPITALS, ALLOCS, SIZING_MODES,
    ))
    total = len(combos)
    print(f"Total configurations: {total}\n", flush=True)

    results = []
    for i, ((tag, entry_t, close_t), lev, cap, alloc, (sz_label, is_flat)) in enumerate(combos, 1):
        lev_label = f"{lev}x"
        cap_label = f"{cap // 1000}k"
        alloc_int = int(alloc * 100)

        print(
            f"[{i}/{total}] {tag} {lev_label} ${cap_label} {alloc_int}% {sz_label}",
            flush=True,
        )

        bt.INITIAL_CAPITAL = cap
        bt.QTY = 1.0

        log = bt.run_backtest_custom(
            df, FEE, float(lev),
            entry_time=entry_t,
            close_time=close_t,
            day_filter=day_filter,
            tp_enabled=TP_ENABLED,
            alloc_pct=alloc,
            flat_sizing=is_flat,
        )

        if log.empty:
            print(f"  => No trades\n", flush=True)
            results.append({
                "timing": tag, "leverage": lev_label,
                "capital": cap, "alloc_pct": alloc_int,
                "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
                "trades": 0, "return_pct": 0, "sharpe": 0,
                "max_dd_pct": 0, "win_rate_pct": 0,
                "liq_rate_pct": 0, "profit_factor": 0,
                "final_capital": cap, "total_fees": 0,
            })
            continue

        overall, yearly = bt.compute_metrics(log)
        results.append({
            "timing": tag, "leverage": lev_label,
            "capital": cap, "alloc_pct": alloc_int,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, required=True, choices=[30, 60],
                        help="Window length in minutes (30 or 60)")
    parser.add_argument("--day-filter", required=True, choices=["weekday", "weekend"],
                        help="Day filter")
    args = parser.parse_args()

    interval_min = args.interval
    day_filter = args.day_filter
    timings = _build_timings(interval_min)

    int_label = f"{interval_min}min"
    print(f"=== Perp Interval Sweep: {int_label}, {day_filter} ===", flush=True)
    print(f"Windows: {len(timings)}", flush=True)
    for t in timings:
        print(f"  {t[0]}  entry={t[1]}  close={t[2]}", flush=True)
    print(f"Leverages: {LEVERAGES}", flush=True)
    print(f"Capitals: {CAPITALS}", flush=True)
    print(f"Allocations: {[int(a*100) for a in ALLOCS]}%", flush=True)
    print(f"Sizing: {[s[0] for s in SIZING_MODES]}", flush=True)
    print(f"TP: {TP_ENABLED}, Fee: {FEE}bps, Day filter: {day_filter}", flush=True)
    print(f"Exchange: {EXCHANGE}\n", flush=True)

    print("Loading data once (deribit only) ...", flush=True)
    df = bt.load_data(exchange_filter=EXCHANGE)
    print("Data loaded.\n", flush=True)

    results = run_sweep(timings, day_filter, df)

    out_csv = OUT_DIR / f"perp_{int_label}_{day_filter}_results.csv"
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
