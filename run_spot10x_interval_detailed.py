#!/usr/bin/env python3
"""
Spot 10x Synthetic Straddle 0DTE — Detailed Interval Sweep
============================================================
Strategy: Long 0.5 BTC Spot (10x leverage) + Long 2 ITM Puts (0.5 BTC each)
Saves individual folders with trade log, metrics, yearly CSV, and plots.

Config: 10x leverage, 8K capital, 60%/80% alloc, flat/compound, noTP, 0 fee, Deribit.

Usage:
  python run_spot10x_interval_detailed.py --interval 30 --day-filter weekday
  python run_spot10x_interval_detailed.py --interval 60 --day-filter weekend
  python run_spot10x_interval_detailed.py --interval 120 --day-filter weekday
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
LEVERAGE = 10
SPOT_QTY = 0.5
CAPITALS = [8_000]
ALLOCS = [0.60, 0.80]
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


def run_sweep(timings, day_filter, df, interval_label):
    combos = list(itertools.product(
        timings, CAPITALS, ALLOCS, SIZING_MODES,
    ))
    total = len(combos)
    print(f"Total configurations: {total}\n", flush=True)

    results = []
    for i, ((tag, entry_t, close_t), cap, alloc, (sz_label, is_flat)) in enumerate(combos, 1):
        cap_label = f"{cap // 1000}k"
        alloc_int = int(alloc * 100)

        folder_name = f"spot10x_{interval_label}_{day_filter}_{tag}_{cap_label}_{alloc_int}pct_{sz_label}_notp"
        config_dir = OUT_DIR / folder_name
        config_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"[{i}/{total}] {tag} 10x ${cap_label} {alloc_int}% {sz_label}",
            flush=True,
        )

        bt.INITIAL_CAPITAL = cap
        bt.QTY = SPOT_QTY

        log = bt.run_backtest_custom(
            df, FEE, float(LEVERAGE),
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
                "timing": tag, "leverage": "10x",
                "capital": cap, "alloc_pct": alloc_int,
                "sizing": sz_label, "tp": "NoTP", "fee_bps": 0,
                "trades": 0, "return_pct": 0, "sharpe": 0,
                "max_dd_pct": 0, "win_rate_pct": 0,
                "liq_rate_pct": 0, "profit_factor": 0,
                "final_capital": cap, "total_fees": 0,
            })
            continue

        log.to_csv(config_dir / "trade_log.csv", index=False)
        overall, yearly = bt.compute_metrics(log)
        bt.save_summary(overall, yearly, config_dir, FEE, float(LEVERAGE))
        bt.generate_plots(log, config_dir, float(LEVERAGE))

        results.append({
            "timing": tag, "leverage": "10x",
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
    parser.add_argument("--interval", type=int, required=True, choices=[30, 60, 120])
    parser.add_argument("--day-filter", required=True, choices=["weekday", "weekend"])
    parser.add_argument("--start", type=str, default="0830",
                        help="Start time HHMM (default 0830)")
    args = parser.parse_args()

    interval_min = args.interval
    day_filter = args.day_filter
    start_hh = int(args.start[:2])
    start_mm = int(args.start[2:])
    timings = _build_timings(interval_min, start_hh, start_mm)
    start_tag = f"s{args.start}" if args.start != "0830" else ""
    int_label = f"{interval_min}min{start_tag}"

    print(f"=== Spot 10x Detailed Sweep: {int_label}, {day_filter} ===", flush=True)
    print(f"Windows: {len(timings)}", flush=True)
    print(f"Leverage: {LEVERAGE}x (spot)", flush=True)
    print(f"QTY: {SPOT_QTY} BTC per straddle", flush=True)
    print(f"Capitals: {CAPITALS}", flush=True)
    print(f"Allocations: {[int(a*100) for a in ALLOCS]}%", flush=True)
    print(f"Sizing: {[s[0] for s in SIZING_MODES]}", flush=True)
    print(f"TP: {TP_ENABLED}, Fee: {FEE}bps, Day filter: {day_filter}", flush=True)
    print(f"Exchange: {EXCHANGE}\n", flush=True)

    print("Loading data once (deribit only) ...", flush=True)
    df = bt.load_data(exchange_filter=EXCHANGE)
    print("Data loaded.\n", flush=True)

    results = run_sweep(timings, day_filter, df, int_label)

    out_csv = OUT_DIR / f"spot10x_{int_label}_{day_filter}_detailed_results.csv"
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
