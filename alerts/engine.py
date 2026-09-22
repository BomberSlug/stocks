"""
Alert system: the live-running counterpart of the entry logic in
backtest/engine.py, using the SAME rolling z-score / executability / cost
gates so the alert stream matches what the backtest actually validated
(no separate, looser "alert" threshold that was never backtested).

Design principle: default to silence. An alert fires only when every gate
passes; every gate that fails is recorded as a "suppressed" reason so you
can audit why the system is quiet, rather than just trusting an empty feed.
"""
from __future__ import annotations

import datetime as dt
import statistics as stats
from dataclasses import dataclass

from pipeline.normalize import ContractSeries
from backtest.costs import is_executable, pair_round_trip_cost_bps
from backtest.engine import ENTRY_Z, LOOKBACK_DAYS, MIN_LOOKBACK_OBS, FORCE_EXIT_DAYS_BEFORE_EXPIRY

# An alert must clear the entry threshold by a margin big enough that,
# after paying round-trip costs, there's still a plausible edge left --
# otherwise we'd be alerting on moves that are backtested losers by
# construction. This is a sanity floor, not a guarantee of profit.
MIN_EXPECTED_EDGE_BPS = 5.0


@dataclass
class Alert:
    symbol_a: str
    symbol_b: str
    expiry_a: dt.date
    expiry_b: dt.date
    date: dt.date
    z_score: float
    spread: float
    suggested_direction: str  # "long_a_short_b" or "short_a_long_b"
    estimated_cost_bps: float
    note: str


@dataclass
class SuppressedSignal:
    symbol_a: str
    symbol_b: str
    date: dt.date
    reason: str
    detail: str


def evaluate_day(
    a: ContractSeries,
    b: ContractSeries,
    d: dt.date,
    history_dates: list[dt.date],
) -> Alert | SuppressedSignal | None:
    """
    Evaluate a single day for a single pair. Returns:
      - Alert if every gate passes,
      - SuppressedSignal if the raw z-score would have triggered but a
        gate blocked it (useful for the trader-facing "why nothing fired"
        log),
      - None if the z-score never got close (routine, not logged loudly).
    """
    if d not in a.bars or d not in b.bars:
        return None

    hist = [hd for hd in history_dates if hd in a.bars and hd in b.bars]
    if len(hist) < MIN_LOOKBACK_OBS:
        return SuppressedSignal(a.symbol, b.symbol, d, "insufficient_history",
                                 f"only {len(hist)} overlapping obs, need {MIN_LOOKBACK_OBS}")

    hist_spreads = [(a.normalized_close(hd) - b.normalized_close(hd)) for hd in hist]
    mean = stats.mean(hist_spreads)
    sd = stats.pstdev(hist_spreads) or 1e-9
    cur_spread = a.normalized_close(d) - b.normalized_close(d)
    z = (cur_spread - mean) / sd

    if abs(z) < ENTRY_Z:
        return None  # nothing near threshold -- stay quiet, don't log noise

    row_a, row_b = a.bars[d], b.bars[d]
    if not (is_executable(a.symbol, row_a.open_interest, row_a.volume) and
            is_executable(b.symbol, row_b.open_interest, row_b.volume)):
        return SuppressedSignal(a.symbol, b.symbol, d, "not_executable",
                                 f"z={z:.2f} but OI/volume below liquidity floor on one or both legs")

    force_exit_date = min(a.expiry_date, b.expiry_date) - dt.timedelta(days=FORCE_EXIT_DAYS_BEFORE_EXPIRY)
    if d >= force_exit_date:
        return SuppressedSignal(a.symbol, b.symbol, d, "too_close_to_expiry",
                                 f"z={z:.2f} but within {FORCE_EXIT_DAYS_BEFORE_EXPIRY}d of nearer expiry")

    cost_bps = pair_round_trip_cost_bps(a.symbol, b.symbol)
    # Rough expected-move proxy: distance back to mean, in bps of price level,
    # discounted by 50% since z-score extremity doesn't guarantee full reversion.
    ref_price = a.normalized_close(d) or 1.0
    expected_move_bps = abs(cur_spread - mean) / ref_price * 10000 * 0.5
    expected_edge_bps = expected_move_bps - cost_bps

    if expected_edge_bps < MIN_EXPECTED_EDGE_BPS:
        return SuppressedSignal(a.symbol, b.symbol, d, "edge_below_cost",
                                 f"z={z:.2f}, expected_move={expected_move_bps:.1f}bps, "
                                 f"cost={cost_bps:.1f}bps, edge={expected_edge_bps:.1f}bps")

    direction = "short_a_long_b" if z > 0 else "long_a_short_b"
    return Alert(
        symbol_a=a.symbol, symbol_b=b.symbol,
        expiry_a=a.expiry_date, expiry_b=b.expiry_date,
        date=d, z_score=z, spread=cur_spread,
        suggested_direction=direction,
        estimated_cost_bps=cost_bps,
        note=(f"Normalized spread {cur_spread:.2f} Rs/g-fine is {z:.2f} sigma from its "
              f"{LOOKBACK_DAYS}d rolling mean ({mean:.2f}). Estimated edge after costs "
              f"~{expected_edge_bps:.1f} bps. Both legs pass the liquidity floor; "
              f"{FORCE_EXIT_DAYS_BEFORE_EXPIRY}+ days remain before the nearer expiry."),
    )
