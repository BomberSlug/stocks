"""
Synthetic Bhavcopy generator.

This exists ONLY because mcxindia.com is unreachable from this sandbox. It
is NOT a substitute for real data and no conclusion about actual MCX
mispricing should be drawn from it -- its only job is to prove the pipeline
/ normalization / walk-forward / cost / alert code is wired correctly and
behaves sensibly, in two scenarios:

  scenario="null"    : four contracts track one common gold price with pure
                        idiosyncratic noise (no persistent mispricing). A
                        correct backtest should find no cost-surviving edge
                        here -- if it finds a big profitable edge on this
                        data, the code has a look-ahead bug.

  scenario="injected" : same as above, plus a deliberately injected,
                        mean-reverting spread between GOLDM and GOLDPETAL
                        (magnitude tuned to be larger than assumed round-trip
                        costs). A correct backtest should find and monetize
                        this -- if it doesn't, the signal/entry logic is
                        broken or too conservative to ever fire.

Real MCX data may look like neither of these. Running against it is a
prerequisite before trusting any number from this repo.
"""
from __future__ import annotations

import datetime as dt
import math
import random

from pipeline.fetch import BhavRow
from pipeline.normalize import CONTRACT_SPEC, price_per_gram_fine

TRADING_WEEKDAYS = {0, 1, 2, 3, 4}


def _trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() in TRADING_WEEKDAYS:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def _month_expiry(year: int, month: int, day_range: tuple[int, int]) -> dt.date:
    day = day_range[0]
    while True:
        try:
            return dt.date(year, month, day)
        except ValueError:
            day -= 1


def generate_synthetic_bhavcopy(
    start: dt.date = dt.date(2023, 1, 1),
    end: dt.date = dt.date(2026, 9, 1),
    scenario: str = "null",
    seed: int = 42,
) -> list[BhavRow]:
    rng = random.Random(seed)
    days = _trading_days(start, end)

    # One common fine-gold price path (Rs per gram fine), a mild random walk
    # with drift, shared by all four contracts as their common factor.
    base = {}
    price = 5800.0  # rough starting Rs/g-fine level
    for d in days:
        price *= math.exp(rng.gauss(0.0002, 0.008))
        base[d] = price

    # Injected mean-reverting mispricing on GOLDM vs GOLDPETAL only.
    mispricing = {}
    if scenario == "injected":
        state = 0.0
        for d in days:
            state = 0.85 * state + rng.gauss(0, 14.0)  # AR(1), mean-reverting, Rs/g-fine
            mispricing[d] = state
    else:
        mispricing = {d: 0.0 for d in days}

    rows: list[BhavRow] = []
    goldten_inception = dt.date(2025, 1, 1)

    # Build a rolling set of monthly contracts per symbol, each with its
    # own expiry window and a listing window ~5 months before expiry.
    for symbol, spec in CONTRACT_SPEC.items():
        d_start, d_end = spec["expiry_window"]
        cur = dt.date(start.year, start.month, 1)
        while cur <= end:
            if symbol == "GOLDTEN" and cur < goldten_inception:
                cur = _add_month(cur)
                continue
            expiry = _month_expiry(cur.year, cur.month, (d_start, d_end))
            listing_start = expiry - dt.timedelta(days=150)
            contract_days = [d for d in days if listing_start <= d <= min(expiry, end)]
            for d in contract_days:
                fine_price = base[d]
                if symbol == "GOLDM":
                    fine_price += mispricing[d] / 2
                elif symbol == "GOLDPETAL":
                    fine_price -= mispricing[d] / 2
                # small idiosyncratic noise per contract, per day
                fine_price *= math.exp(rng.gauss(0, 0.0015))

                quoted_price = fine_price * (spec["purity"] / 1000.0) * spec["grams_quoted"]
                # liquidity: GOLDM most liquid, GOLDPETAL thinnest; decays near listing/expiry edges
                base_oi = {"GOLDM": 4000, "GOLDTEN": 1500, "GOLDGUINEA": 600, "GOLDPETAL": 300}[symbol]
                days_to_expiry = (expiry - d).days
                ramp = min(1.0, (150 - days_to_expiry) / 30.0) if days_to_expiry > 0 else 0.3
                oi = max(0, int(base_oi * max(0.15, ramp) * math.exp(rng.gauss(0, 0.2))))
                vol = max(0, int(oi * rng.uniform(0.05, 0.25)))

                daily_noise = fine_price * 0.004
                o = quoted_price + rng.gauss(0, daily_noise * 0.3)
                c = quoted_price + rng.gauss(0, daily_noise * 0.3)
                h = max(o, c) + abs(rng.gauss(0, daily_noise * 0.2))
                l = min(o, c) - abs(rng.gauss(0, daily_noise * 0.2))

                rows.append(BhavRow(
                    symbol=symbol, trade_date=d, expiry_date=expiry,
                    open=round(o, 2), high=round(h, 2), low=round(l, 2), close=round(c, 2),
                    volume=vol, open_interest=oi,
                ))
            cur = _add_month(cur)

    return rows


def _add_month(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year + 1, 1, 1)
    return dt.date(d.year, d.month + 1, 1)
