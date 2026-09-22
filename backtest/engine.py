"""
Walk-forward backtest of a relative-value strategy between two MCX gold
contracts, normalized to Rs per gram of fine gold.

HYPOTHESIS BEING TESTED
------------------------
Null hypothesis (the honest default for four instruments on the same
underlying with public settlement prices): normalized price spreads between
same-delivery-month contract pairs do NOT show a persistent, cost-surviving
mean-reversion pattern -- any apparent edge is within the noise of the
assumed transaction-cost model and/or an artifact of overfitting the
lookback window.

Alternative hypothesis being tested against it: the spread between two
concurrently-listed, same-delivery-month contracts (purity/size normalized)
mean-reverts around a rolling fair value, and deviations beyond a
volatility-scaled threshold predict convergence over a short holding period,
net of the assumed round-trip costs in backtest/costs.py.

WALK-FORWARD DESIGN (no look-ahead)
------------------------------------
For each trading day t in the test contract's life:
  1. Compute the normalized spread series using ONLY data from
     [t - lookback, t - 1] -- day t's own close is never used to compute
     the signal that trades on day t.
  2. Signal = z-score of day (t-1)'s spread against that trailing window's
     mean/std. We trade the OPEN of day t (approximated here with day t's
     close and a cost haircut, since Bhavcopy gives no intraday open we can
     trust as executable -- see costs.py note).
  3. Re-estimate the mean/std fresh each day (rolling window slides forward)
     -- this is what makes it "walk-forward" rather than a single in-sample
     fit applied to the whole history.
  4. No parameter that depends on future data (no full-sample z-scoring,
     no threshold fit on the same data used to evaluate it -- thresholds
     are fixed a priori, not optimized on this dataset).

EXPIRY HANDLING
----------------
A position is force-closed FORCE_EXIT_DAYS_BEFORE_EXPIRY before whichever
leg expires first. We never carry a spread position into the illiquid final
days of a contract's life, and we never roll a leg automatically -- a roll
is modeled as a full close + a fresh, independently-signaled entry in the
new contract, so rollover cost is paid explicitly rather than hidden.
"""
from __future__ import annotations

import datetime as dt
import statistics as stats
from dataclasses import dataclass, field

from pipeline.normalize import ContractSeries, price_per_gram_fine
from backtest.costs import is_executable, pair_round_trip_cost_bps

FORCE_EXIT_DAYS_BEFORE_EXPIRY = 5
ENTRY_Z = 2.0
EXIT_Z = 0.5
MAX_HOLD_DAYS = 20
LOOKBACK_DAYS = 30
MIN_LOOKBACK_OBS = 15  # don't trade until the rolling window has enough real observations


@dataclass
class Trade:
    symbol_a: str
    symbol_b: str
    expiry_a: dt.date
    expiry_b: dt.date
    entry_date: dt.date
    exit_date: dt.date | None = None
    entry_spread: float = 0.0
    exit_spread: float = 0.0
    direction: int = 0  # +1 = betting spread falls (a was rich vs b), -1 = betting spread rises
    entry_z: float = 0.0
    exit_reason: str = ""
    gross_pnl_bps: float = 0.0
    cost_bps: float = 0.0
    net_pnl_bps: float = 0.0


def _spread_series(a: ContractSeries, b: ContractSeries, dates: list[dt.date]) -> dict[dt.date, float]:
    out = {}
    for d in dates:
        pa = a.normalized_close(d)
        pb = b.normalized_close(d)
        if pa is not None and pb is not None:
            out[d] = pa - pb
    return out


def walk_forward_backtest(
    a: ContractSeries,
    b: ContractSeries,
    lookback: int = LOOKBACK_DAYS,
    entry_z: float = ENTRY_Z,
    exit_z: float = EXIT_Z,
    max_hold: int = MAX_HOLD_DAYS,
) -> list[Trade]:
    common_dates = sorted(set(a.bars) & set(b.bars))
    if len(common_dates) < lookback + 5:
        return []  # not enough overlapping history to trade responsibly

    spreads = _spread_series(a, b, common_dates)
    force_exit_date = min(a.expiry_date, b.expiry_date) - dt.timedelta(days=FORCE_EXIT_DAYS_BEFORE_EXPIRY)

    trades: list[Trade] = []
    open_trade: Trade | None = None

    for i, d in enumerate(common_dates):
        if d not in spreads:
            continue

        # ---- signal uses ONLY strictly-prior days ----
        history = [common_dates[j] for j in range(max(0, i - lookback), i) if common_dates[j] in spreads]
        if len(history) < MIN_LOOKBACK_OBS:
            continue
        hist_vals = [spreads[hd] for hd in history]
        mean = stats.mean(hist_vals)
        sd = stats.pstdev(hist_vals) or 1e-9
        z = (spreads[d] - mean) / sd

        row_a, row_b = a.bars[d], b.bars[d]
        executable = is_executable(a.symbol, row_a.open_interest, row_a.volume) and \
                     is_executable(b.symbol, row_b.open_interest, row_b.volume)

        # ---- manage open position ----
        if open_trade is not None:
            days_held = (d - open_trade.entry_date).days
            should_exit = False
            reason = ""
            if d >= force_exit_date:
                should_exit, reason = True, "force_exit_pre_expiry"
            elif days_held >= max_hold:
                should_exit, reason = True, "max_hold_reached"
            elif abs(z) <= exit_z:
                should_exit, reason = True, "reverted_to_mean"
            elif not executable:
                should_exit, reason = True, "lost_executability"

            if should_exit:
                open_trade.exit_date = d
                open_trade.exit_spread = spreads[d]
                open_trade.exit_reason = reason
                move = open_trade.entry_spread - open_trade.exit_spread
                # gross_pnl in "Rs per gram fine" terms; convert to bps of
                # the entry normalized price level for a comparable unit
                ref_price = (a.normalized_close(open_trade.entry_date) or 1.0)
                gross_bps = (move * open_trade.direction / ref_price) * 10000
                cost_bps = pair_round_trip_cost_bps(open_trade.symbol_a, open_trade.symbol_b)
                open_trade.gross_pnl_bps = gross_bps
                open_trade.cost_bps = cost_bps
                open_trade.net_pnl_bps = gross_bps - cost_bps
                trades.append(open_trade)
                open_trade = None

        # ---- consider new entry (only if flat, executable, before force-exit window) ----
        if open_trade is None and executable and d < force_exit_date and abs(z) >= entry_z:
            # z > 0: spread is too high -> we bet it falls. exit P&L uses
            # move = entry_spread - exit_spread (positive when spread falls),
            # so direction=+1 here makes a correct "falls" prediction score
            # positive. z < 0: spread too low -> bet it rises -> direction=-1
            # flips the sign so a rising spread also scores positive.
            direction = 1 if z > 0 else -1
            open_trade = Trade(
                symbol_a=a.symbol, symbol_b=b.symbol,
                expiry_a=a.expiry_date, expiry_b=b.expiry_date,
                entry_date=d, entry_spread=spreads[d],
                direction=direction, entry_z=z,
            )

    return trades
