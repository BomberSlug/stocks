"""
Sanity checks run on every batch of parsed Bhavcopy rows before they're
allowed into the normalization/backtest layer. These catch malformed data
that passes basic type parsing but is still wrong.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class ValidationIssue:
    trade_date: dt.date
    symbol: str
    kind: str
    detail: str


def validate_rows(rows: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for row in rows:
        # OHLC internal consistency
        if not (row.low <= row.open <= row.high and row.low <= row.close <= row.high):
            issues.append(ValidationIssue(row.trade_date, row.symbol, "ohlc_inconsistent",
                                           f"O={row.open} H={row.high} L={row.low} C={row.close}"))
        # Non-positive prices
        if min(row.open, row.high, row.low, row.close) <= 0:
            issues.append(ValidationIssue(row.trade_date, row.symbol, "nonpositive_price",
                                           f"O={row.open} H={row.high} L={row.low} C={row.close}"))
        # Expiry must be in the future relative to (or equal to) trade date
        if row.expiry_date < row.trade_date:
            issues.append(ValidationIssue(row.trade_date, row.symbol, "expired_contract_traded",
                                           f"expiry={row.expiry_date} trade_date={row.trade_date}"))
        # Zero volume + zero OI on a day other than listing/expiry is suspicious
        # (flagged, not dropped -- thin days are expected for GOLDGUINEA/GOLDPETAL,
        # this is informational for the liquidity filter downstream)
        if row.volume == 0 and row.open_interest == 0:
            issues.append(ValidationIssue(row.trade_date, row.symbol, "zero_activity",
                                           "volume and OI both zero"))
    return issues


def flag_price_jumps(rows: list, threshold_pct: float = 0.08) -> list[ValidationIssue]:
    """
    Flag day-over-day close jumps bigger than threshold_pct within the same
    (symbol, expiry) contract. A single flag isn't necessarily bad data --
    gold does move -- but a jump on a day with abnormally low volume is a
    strong "don't trust this settlement" signal and should suppress alerts
    (see alerts/engine.py silence_reasons).
    """
    from collections import defaultdict

    by_contract = defaultdict(list)
    for row in rows:
        by_contract[(row.symbol, row.expiry_date)].append(row)

    issues: list[ValidationIssue] = []
    for key, contract_rows in by_contract.items():
        contract_rows.sort(key=lambda r: r.trade_date)
        for prev, cur in zip(contract_rows, contract_rows[1:]):
            if prev.close <= 0:
                continue
            pct = abs(cur.close - prev.close) / prev.close
            if pct > threshold_pct:
                issues.append(ValidationIssue(
                    cur.trade_date, cur.symbol, "large_price_jump",
                    f"{pct:.1%} move from {prev.close} to {cur.close} (expiry {key[1]})",
                ))
    return issues


def missing_day_report(expected_days: list[dt.date], missing_days: list[dt.date]) -> dict:
    return {
        "expected_trading_days": len(expected_days),
        "missing_days": len(missing_days),
        "missing_pct": (len(missing_days) / len(expected_days)) if expected_days else 0.0,
        "missing_dates": sorted(missing_days),
    }
