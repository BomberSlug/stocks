"""
Normalization and contract lifecycle handling.

Design decision: track every series by (symbol, expiry_date), never as a
"continuous near-month" series. Continuous series splice different contracts
together at rollover and inject artificial jumps exactly at the moment
you'd otherwise measure a real spread move. Since this whole strategy IS a
spread-measurement exercise, that contamination is fatal -- so we keep
contracts as discrete objects with their own start/end and only ever compare
two contracts that are trading concurrently.

Normalization math:
  Each contract quotes a price for a certain number of grams at a certain
  purity. To compare them we convert every quote to Rs per gram of fine
  (pure, 999.9) gold:

      price_per_gram_fine = (settle_price / grams_quoted) / (purity / 1000)

  where grams_quoted is the "Price Quoted Per" column, not the trading unit
  (GOLDM trades in 100g lots but is QUOTED per 10g -- using the trading unit
  here would be a 10x error).

Contract reference (from task spec):
  GOLDM:      trading unit 100g, quoted per 10g,  purity 995, expiry 3-5
  GOLDTEN:    trading unit 10g,  quoted per 10g,  purity 999, expiry 27-31
  GOLDGUINEA: trading unit 8g,   quoted per 8g,   purity 999, expiry 27-31
  GOLDPETAL:  trading unit 1g,   quoted per 1g,   purity 999, expiry 27-31
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

CONTRACT_SPEC = {
    "GOLDM": {"grams_quoted": 10, "purity": 995, "expiry_window": (3, 5)},
    "GOLDTEN": {"grams_quoted": 10, "purity": 999, "expiry_window": (27, 31)},
    "GOLDGUINEA": {"grams_quoted": 8, "purity": 999, "expiry_window": (27, 31)},
    "GOLDPETAL": {"grams_quoted": 1, "purity": 999, "expiry_window": (27, 31)},
}


def price_per_gram_fine(symbol: str, price: float) -> float:
    spec = CONTRACT_SPEC[symbol]
    return (price / spec["grams_quoted"]) / (spec["purity"] / 1000.0)


@dataclass
class ContractSeries:
    """One specific contract instance: a symbol + a specific expiry date,
    with its daily bars from listing to expiry (or last observed day)."""

    symbol: str
    expiry_date: dt.date
    bars: dict = field(default_factory=dict)  # trade_date -> BhavRow

    def add(self, row) -> None:
        self.bars[row.trade_date] = row

    def normalized_close(self, d: dt.date) -> float | None:
        row = self.bars.get(d)
        if row is None:
            return None
        return price_per_gram_fine(self.symbol, row.close)

    @property
    def first_date(self) -> dt.date:
        return min(self.bars)

    @property
    def last_date(self) -> dt.date:
        return max(self.bars)

    def is_trading_on(self, d: dt.date) -> bool:
        return d in self.bars


def build_contract_series(rows: list) -> dict[tuple[str, dt.date], ContractSeries]:
    """Group raw Bhavcopy rows into discrete (symbol, expiry) contract objects."""
    series: dict[tuple[str, dt.date], ContractSeries] = {}
    for row in rows:
        key = (row.symbol, row.expiry_date)
        if key not in series:
            series[key] = ContractSeries(symbol=row.symbol, expiry_date=row.expiry_date)
        series[key].add(row)
    return series


def concurrent_pairs(
    series: dict[tuple[str, dt.date], ContractSeries],
    symbol_a: str,
    symbol_b: str,
    max_expiry_gap_days: int = 10,
) -> list[tuple[ContractSeries, ContractSeries]]:
    """
    Find pairs of (contract_a, contract_b) -- one per symbol -- whose expiry
    dates are close enough (within max_expiry_gap_days) that comparing them
    is a fair "same delivery month" comparison, not an accidental calendar
    spread. GOLDM expires 3rd-5th of a month while the other three expire
    27th-31st of a month -- so a GOLDM contract's "matching" partner is
    typically the one expiring in the *same nominal delivery month*, i.e.
    roughly one month apart in expiry_date, not the literally-nearest one.
    This function only pairs contracts within max_expiry_gap_days of each
    other; callers comparing GOLDM to the 27-31 group should pass a gap
    wide enough to bridge that ~25-day structural offset deliberately,
    with that assumption stated (see backtest/engine.py PAIR_CONFIG).
    """
    a_list = [s for (sym, _), s in series.items() if sym == symbol_a]
    b_list = [s for (sym, _), s in series.items() if sym == symbol_b]
    pairs = []
    for a in a_list:
        for b in b_list:
            gap = abs((a.expiry_date - b.expiry_date).days)
            if gap <= max_expiry_gap_days:
                pairs.append((a, b))
    return pairs
