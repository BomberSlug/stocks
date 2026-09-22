"""
Transaction cost model.

IMPORTANT HONESTY NOTE: Bhavcopy gives settlement (close) prices and
end-of-day volume/OI -- it does NOT give bid-ask quotes or intraday depth.
We do not have real spread data. The numbers below are conservative
assumptions grounded in typical MCX gold-derivative liquidity tiers, stated
explicitly so they can be swapped for real broker-quoted spreads before
anyone trades on this. Treat every backtest result as "returns net of an
assumed cost model," not "returns net of costs we measured."

Liquidity tiers (by average daily volume in lots, our assumption):
  GOLDM:      typically the most liquid of the four -> tightest spread
  GOLDTEN:    moderate
  GOLDGUINEA: thin
  GOLDPETAL:  thinnest, retail-sized -> widest spread, most slippage risk

Spread assumption: expressed in basis points of price, charged as a
half-spread on entry and a half-spread on exit (i.e. full round-trip spread
cost = ASSUMED_SPREAD_BPS[symbol] / 10000 * price, split across the two legs
of the trade). Slippage is added on top as an extra assumed bps, meant to
capture the gap between "settlement price" and "price you'd actually get"
when crossing the spread against a thin book.
"""
from __future__ import annotations

from dataclasses import dataclass

ASSUMED_SPREAD_BPS = {
    "GOLDM": 8,
    "GOLDTEN": 15,
    "GOLDGUINEA": 30,
    "GOLDPETAL": 45,
}

ASSUMED_SLIPPAGE_BPS = {
    "GOLDM": 4,
    "GOLDTEN": 8,
    "GOLDGUINEA": 18,
    "GOLDPETAL": 25,
}

# Below this OI, treat the settlement price as not reliably executable at
# any reasonable size and exclude the day from tradeable signal generation.
# This is a floor, not a precise threshold -- again, an assumption in the
# absence of real depth data.
MIN_OPEN_INTEREST = {
    "GOLDM": 200,
    "GOLDTEN": 100,
    "GOLDGUINEA": 50,
    "GOLDPETAL": 50,
}


@dataclass
class CostEstimate:
    symbol: str
    round_trip_bps: float
    round_trip_cost_rs: float


def round_trip_cost(symbol: str, price: float) -> CostEstimate:
    spread_bps = ASSUMED_SPREAD_BPS[symbol]
    slip_bps = ASSUMED_SLIPPAGE_BPS[symbol]
    total_bps = spread_bps + slip_bps  # full round trip (both legs, entry+exit already embedded in bps convention)
    cost_rs = price * total_bps / 10000.0
    return CostEstimate(symbol=symbol, round_trip_bps=total_bps, round_trip_cost_rs=cost_rs)


def is_executable(symbol: str, open_interest: int, volume: int) -> bool:
    if open_interest < MIN_OPEN_INTEREST[symbol]:
        return False
    if volume == 0:
        return False
    return True


def pair_round_trip_cost_bps(symbol_a: str, symbol_b: str) -> float:
    """A spread trade pays the round-trip cost on BOTH legs."""
    return ASSUMED_SPREAD_BPS[symbol_a] + ASSUMED_SLIPPAGE_BPS[symbol_a] + \
           ASSUMED_SPREAD_BPS[symbol_b] + ASSUMED_SLIPPAGE_BPS[symbol_b]
