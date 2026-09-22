"""
Attribution and validation of backtest results.

Because every trade here is a spread (long one leg, short the other, sized
to be gram-fine-neutral at entry), the strategy's P&L should already be
close to market-neutral with respect to the outright gold price. This
module checks that assumption rather than assuming it, and reports the
statistics needed to judge whether any apparent edge is real:

  1. Correlation of trade net P&L against the concurrent move in the common
     gold price level (base fine-gold price proxied by the average of the
     two legs' normalized prices over the trade's life). Should be ~0 for a
     genuinely market-neutral spread strategy; a large correlation means
     the "edge" is partly just directional gold exposure in disguise.
  2. Win rate, average net bps per trade, total net bps, trade count --
     with the explicit caveat that these come from ONE dataset and are not
     a probability of live success.
  3. Sensitivity to the cost assumption: what does net P&L look like at
     0.5x and 2x the assumed cost model, since the costs in backtest/costs.py
     are estimates, not measured spreads.
"""
from __future__ import annotations

import statistics as stats
from dataclasses import dataclass

from backtest.engine import Trade
from pipeline.normalize import ContractSeries


@dataclass
class AttributionReport:
    n_trades: int
    win_rate: float | None
    mean_net_bps: float | None
    total_net_bps: float | None
    mean_gross_bps: float | None
    total_cost_bps: float | None
    gold_correlation: float | None
    net_bps_at_half_cost: float | None
    net_bps_at_double_cost: float | None
    verdict: str


def _gold_move_for_trade(t: Trade, a: ContractSeries, b: ContractSeries) -> float | None:
    pa0, pb0 = a.normalized_close(t.entry_date), b.normalized_close(t.entry_date)
    pa1, pb1 = (a.normalized_close(t.exit_date) if t.exit_date else None,
                b.normalized_close(t.exit_date) if t.exit_date else None)
    if None in (pa0, pb0, pa1, pb1):
        return None
    level0 = (pa0 + pb0) / 2
    level1 = (pa1 + pb1) / 2
    return (level1 - level0) / level0 * 10000  # bps move in the common gold level


def analyze(trades: list[Trade], a: ContractSeries, b: ContractSeries) -> AttributionReport:
    closed = [t for t in trades if t.exit_date is not None]
    if not closed:
        return AttributionReport(0, None, None, None, None, None, None, None, None,
                                  "No closed trades -- signal never fired, or never both "
                                  "entered and exited within the data window. This is a "
                                  "valid 'no edge found' outcome, not a bug by itself.")

    net = [t.net_pnl_bps for t in closed]
    gross = [t.gross_pnl_bps for t in closed]
    cost = [t.cost_bps for t in closed]
    wins = [1 for t in closed if t.net_pnl_bps > 0]
    win_rate = len(wins) / len(closed)

    gold_moves = [_gold_move_for_trade(t, a, b) for t in closed]
    paired = [(n, g) for n, g in zip(net, gold_moves) if g is not None]
    gold_corr = None
    if len(paired) >= 3:
        ns, gs = zip(*paired)
        try:
            gold_corr = stats.correlation(ns, gs)
        except stats.StatisticsError:
            gold_corr = None

    mean_cost = stats.mean(cost)
    net_half = stats.mean([g - c / 2 for g, c in zip(gross, cost)])
    net_double = stats.mean([g - c * 2 for g, c in zip(gross, cost)])

    mean_net = stats.mean(net)
    total_net = sum(net)

    if len(closed) < 15:
        verdict = (f"Only {len(closed)} closed trades -- not enough to draw any statistical "
                   "conclusion either way. Treat this as a code-path check, not a result.")
    elif mean_net <= 0:
        verdict = "No cost-surviving edge found: mean net P&L per trade is not positive."
    elif net_double <= 0:
        verdict = ("Mean net P&L is positive at the assumed cost level but flips negative "
                  "if round-trip costs are ~2x the assumption -- the 'edge' is not robust "
                  "to plausible cost uncertainty and should not be treated as real until "
                  "validated against actual quoted spreads.")
    else:
        verdict = ("Positive mean net P&L that survives a doubling of the assumed cost "
                  "model. Still only meaningful if the underlying data is real MCX data, "
                  "the sample is large enough, and it holds out-of-sample beyond this "
                  "backtest window -- none of which this run alone establishes.")

    return AttributionReport(
        n_trades=len(closed), win_rate=win_rate,
        mean_net_bps=mean_net, total_net_bps=total_net,
        mean_gross_bps=stats.mean(gross), total_cost_bps=sum(cost),
        gold_correlation=gold_corr,
        net_bps_at_half_cost=net_half, net_bps_at_double_cost=net_double,
        verdict=verdict,
    )
