"""
End-to-end demo: generate synthetic Bhavcopy data (both "null" and
"injected" scenarios), run it through validation -> normalization ->
walk-forward backtest -> attribution, and run the alert engine day-by-day
over the same data.

Run: python run_demo.py
"""
from __future__ import annotations

import sys

from pipeline.synthetic import generate_synthetic_bhavcopy
from pipeline.validate import validate_rows, flag_price_jumps
from pipeline.normalize import build_contract_series, concurrent_pairs
from backtest.engine import walk_forward_backtest
from backtest.attribution import analyze
from alerts.engine import evaluate_day, Alert, SuppressedSignal


def run_scenario(scenario: str, pair=("GOLDM", "GOLDPETAL")):
    print(f"\n{'='*70}\nSCENARIO: {scenario}\n{'='*70}")
    rows = generate_synthetic_bhavcopy(scenario=scenario)
    print(f"Generated {len(rows)} raw rows across GOLDM/GOLDTEN/GOLDGUINEA/GOLDPETAL")

    issues = validate_rows(rows)
    jump_issues = flag_price_jumps(rows)
    print(f"Validation: {len(issues)} structural issues, {len(jump_issues)} large price-jump flags")

    series = build_contract_series(rows)
    print(f"Built {len(series)} discrete (symbol, expiry) contract series")

    pairs = concurrent_pairs(series, pair[0], pair[1], max_expiry_gap_days=10)
    print(f"Found {len(pairs)} concurrent {pair[0]}/{pair[1]} contract-month pairs")

    all_trades = []
    for a, b in pairs:
        trades = walk_forward_backtest(a, b)
        all_trades.extend((t, a, b) for t in trades)

    print(f"Total trades generated across all contract-month pairs: {len(all_trades)}")

    # Aggregate attribution across all pairs' trades (report is computed per
    # pair since gold-move attribution needs the specific a/b series; here
    # we just pool the summary stats for the demo printout).
    import statistics as stats
    closed = [(t, a, b) for t, a, b in all_trades if t.exit_date is not None]
    if closed:
        net_bps = [t.net_pnl_bps for t, _, _ in closed]
        gross_bps = [t.gross_pnl_bps for t, _, _ in closed]
        cost_bps = [t.cost_bps for t, _, _ in closed]
        win_rate = sum(1 for t, _, _ in closed if t.net_pnl_bps > 0) / len(closed)
        print(f"\nClosed trades: {len(closed)}")
        print(f"Win rate: {win_rate:.1%}")
        print(f"Mean gross P&L: {stats.mean(gross_bps):.2f} bps/trade")
        print(f"Mean cost: {stats.mean(cost_bps):.2f} bps/trade")
        print(f"Mean NET P&L: {stats.mean(net_bps):.2f} bps/trade")
        print(f"Total NET P&L: {sum(net_bps):.2f} bps")

        # exit reasons
        from collections import Counter
        reasons = Counter(t.exit_reason for t, _, _ in closed)
        print(f"Exit reasons: {dict(reasons)}")

        # single-pair attribution report (first pair with enough trades) for the detailed verdict
        for a, b in pairs:
            pair_trades = [t for t, aa, bb in closed if aa is a and bb is b]
            if len(pair_trades) >= 5:
                report = analyze(pair_trades, a, b)
                print(f"\nDetailed attribution ({a.symbol} exp {a.expiry_date} vs "
                      f"{b.symbol} exp {b.expiry_date}):")
                print(f"  gold_correlation of net P&L to outright gold move: {report.gold_correlation}")
                print(f"  net bps at 0.5x assumed cost: {report.net_bps_at_half_cost:.2f}")
                print(f"  net bps at 2.0x assumed cost: {report.net_bps_at_double_cost:.2f}")
                print(f"  VERDICT: {report.verdict}")
                break
    else:
        print("No closed trades -- no cost-surviving/executable signal fired in this scenario.")

    # --- alert engine demo: run it over the last ~120 days of one pair ---
    if pairs:
        a, b = pairs[-1]
        common = sorted(set(a.bars) & set(b.bars))
        tail = common[-120:] if len(common) > 120 else common
        alerts, suppressed = [], []
        for i, d in enumerate(tail):
            hist_start_idx = max(0, len(common) - len(tail) + i - 30)
            hist_dates = common[hist_start_idx: len(common) - len(tail) + i]
            result = evaluate_day(a, b, d, hist_dates)
            if isinstance(result, Alert):
                alerts.append(result)
            elif isinstance(result, SuppressedSignal):
                suppressed.append(result)
        print(f"\nAlert engine over last {len(tail)} days of {a.symbol}/{b.symbol}:")
        print(f"  Alerts fired: {len(alerts)}")
        print(f"  Signals seen but suppressed: {len(suppressed)}")
        if suppressed:
            from collections import Counter
            print(f"  Suppression reasons: {dict(Counter(s.reason for s in suppressed))}")
        if alerts:
            ex = alerts[0]
            print(f"  Example alert: {ex.date} z={ex.z_score:.2f} dir={ex.suggested_direction}")
            print(f"    {ex.note}")


if __name__ == "__main__":
    run_scenario("null")
    run_scenario("injected")
