"""
MCX Bhavcopy fetcher.

Talks to https://www.mcxindia.com/market-data/bhavcopy (or the underlying
CSV/JSON endpoint it calls) for daily settlement data on GOLDM, GOLDTEN,
GOLDGUINEA, GOLDPETAL.

NOTE ON THIS ENVIRONMENT: mcxindia.com is not reachable from this sandbox
(it isn't on the allowed outbound domain list), so this module has NOT been
exercised against the live site. It is written to the documented request/
response contract and to the quirks called out in the task spec. Before
relying on it, run it once against a live day you can eyeball and confirm
column names / endpoint shape haven't drifted -- exchange data pages change
their markup without notice.

Key quirks this module defends against:
  - Requests use DD/MM/YYYY; responses use MM/DD/YYYY. Mixing these up
    silently shifts every date by up to a month for day<=12.
  - MCX returns the most recent available trading day for unrecognized,
    holiday, malformed, or future dates -- i.e. it does NOT error, it
    silently substitutes. A naive pipeline will duplicate the same day's
    data under the wrong requested date and never notice.
  - Symbol values may be space-padded ("GOLDM   ").
  - ExpiryDate is compact format (04SEP2026), not ISO.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Optional

import requests

CONTRACTS = ("GOLDM", "GOLDTEN", "GOLDGUINEA", "GOLDPETAL")

# GOLDTEN only exists from 2025 onward -- any request for GOLDTEN before
# this date is a known-bad request, not a data gap.
GOLDTEN_INCEPTION = dt.date(2025, 1, 1)

BHAVCOPY_URL = "https://www.mcxindia.com/backpage.aspx/GetBhavCopy"  # placeholder: confirm live endpoint


class MCXFetchError(Exception):
    pass


class DateMismatchError(MCXFetchError):
    """Raised when MCX silently substituted a different trading day than requested."""

    def __init__(self, requested: dt.date, returned: dt.date):
        self.requested = requested
        self.returned = returned
        super().__init__(
            f"Requested {requested.isoformat()} but MCX returned {returned.isoformat()} "
            "(likely a holiday/weekend/future/malformed date substitution)."
        )


@dataclass
class BhavRow:
    symbol: str
    trade_date: dt.date
    expiry_date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int

    @staticmethod
    def clean_symbol(raw: str) -> str:
        return raw.strip().upper()

    @staticmethod
    def parse_expiry(raw: str) -> dt.date:
        # Compact format e.g. "04SEP2026"
        return dt.datetime.strptime(raw.strip().upper(), "%d%b%Y").date()

    @staticmethod
    def parse_response_date(raw: str) -> dt.date:
        # Responses come back MM/DD/YYYY -- NOT the DD/MM/YYYY used in requests.
        return dt.datetime.strptime(raw.strip(), "%m/%d/%Y").date()


def _format_request_date(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")  # requests are DD/MM/YYYY


def fetch_bhavcopy_day(
    requested_date: dt.date,
    session: Optional[requests.Session] = None,
    max_retries: int = 3,
    backoff_seconds: float = 1.5,
    strict_date_match: bool = True,
) -> list[BhavRow]:
    """
    Fetch one day's Bhavcopy and return parsed gold-contract rows only.

    Raises DateMismatchError if strict_date_match and MCX substituted a
    different day than requested (weekend/holiday/future/malformed date).
    Callers doing a historical backfill should catch this and treat it as
    "no new data for this date" rather than accepting the substituted rows
    under the wrong date -- otherwise a Saturday and the preceding Friday
    both end up labeled as the same trading day, or worse, get merged.
    """
    sess = session or requests.Session()
    payload = {"date": _format_request_date(requested_date)}

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = sess.post(BHAVCOPY_URL, json=payload, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            break
        except Exception as e:  # network, timeout, bad JSON, 5xx
            last_exc = e
            time.sleep(backoff_seconds * (2 ** attempt))
    else:
        raise MCXFetchError(f"Failed after {max_retries} attempts: {last_exc}") from last_exc

    rows: list[BhavRow] = []
    returned_dates: set[dt.date] = set()

    for rec in raw.get("data", []):
        symbol = BhavRow.clean_symbol(rec["Symbol"])
        if symbol not in CONTRACTS:
            continue
        trade_date = BhavRow.parse_response_date(rec["Date"])
        returned_dates.add(trade_date)
        row = BhavRow(
            symbol=symbol,
            trade_date=trade_date,
            expiry_date=BhavRow.parse_expiry(rec["ExpiryDate"]),
            open=float(rec["Open"]),
            high=float(rec["High"]),
            low=float(rec["Low"]),
            close=float(rec["Close"]),
            volume=int(rec["Volume"]),
            open_interest=int(rec["OpenInterest"]),
        )
        # GOLDTEN sanity check: reject rows that predate its actual listing.
        if row.symbol == "GOLDTEN" and row.trade_date < GOLDTEN_INCEPTION:
            continue
        rows.append(row)

    if strict_date_match and returned_dates and requested_date not in returned_dates:
        # MCX substituted the nearest prior trading day -- surface this rather
        # than silently mislabeling data.
        returned = sorted(returned_dates)[-1]
        raise DateMismatchError(requested_date, returned)

    return rows


def fetch_bhavcopy_range(
    start: dt.date, end: dt.date, session: Optional[requests.Session] = None
) -> tuple[list[BhavRow], list[dt.date]]:
    """
    Fetch every calendar day in [start, end] and split into (rows, missing_days).
    A "missing day" is any date where fetch raised DateMismatchError (holiday/
    weekend/no trading) or returned zero gold-contract rows. This list is what
    the normalization/rollover layer uses to know which sessions to skip
    rather than infer.
    """
    sess = session or requests.Session()
    all_rows: list[BhavRow] = []
    missing: list[dt.date] = []
    d = start
    while d <= end:
        try:
            day_rows = fetch_bhavcopy_day(d, session=sess)
            if not day_rows:
                missing.append(d)
            else:
                all_rows.extend(day_rows)
        except DateMismatchError:
            missing.append(d)
        except MCXFetchError as e:
            missing.append(d)
            print(f"WARN: fetch failed for {d}: {e}")
        d += dt.timedelta(days=1)
    return all_rows, missing
