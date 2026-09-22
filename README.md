# MCX Gold Cross-Contract Relative Value: Pipeline, Walk-Forward Backtest, Alerts

## Status of this deliverable — read this first

`mcxindia.com` is not reachable from the sandbox this was built in (it isn't
on the environment's allowed outbound domain list). **No live MCX data was
fetched, and no result below is a claim about real MCX mispricing.** What's
here is:

1. A fetcher (`pipeline/fetch.py`) written to the documented Bhavcopy
   request/response contract, ready to point at the real endpoint.
2. A full pipeline → normalization → walk-forward backtest → cost model →
   attribution → alert system, exercised end-to-end against **synthetic**
   data (`pipeline/synthetic.py`) that mimics MCX's structure (four
   contracts, correct purities/units, correct expiry windows, GOLDTEN's
   2025 inception, realistic liquidity tiers).
3. An honest run of that pipeline, including a bug I found and fixed while
   validating it (see "What the demo run found," below) — logged rather
   than smoothed over, since that's the kind of thing a walk-forward
   backtest is supposed to catch.

**Before trusting any number from this repo**: confirm the live Bhavcopy
endpoint shape (`fetch.py` has a placeholder URL — the exact JSON endpoint
behind the public bhavcopy page needs to be confirmed against the live
site), run `fetch_bhavcopy_range` over real history, and replace the
assumed spreads in `backtest/costs.py` with real broker-quoted spreads.
Everything downstream of the fetcher is design-complete and unit-tested
against synthetic data, but synthetic data cannot tell you whether real
MCX gold contracts are actually mispriced against each other.

---

## 1. The hypothesis

**What's being tested:** GOLDM, GOLDTEN, GOLDGUINEA, and GOLDPETAL are the
same underlying commodity (gold) in different unit sizes and purities.
Converted to a common basis — Rs per gram of fine (pure) gold — their
prices should track each other closely. The hypothesis is that **transient,
mean-reverting deviations in that normalized spread, between two
contracts expiring in the same nominal delivery month, predict a reversion
over a short holding period large enough to survive realistic transaction
costs.**

**The honest null**, stated up front: four listings of the same commodity
on the same exchange, with public settlement prices that everyone can see,
is exactly the kind of thing that should NOT have a persistent,
easily-exploitable mispricing — any real inefficiency here is more likely
to be a liquidity/microstructure effect (compensation for holding a thin
GOLDPETAL/GOLDGUINEA position) than genuine informational arbitrage. The
backtest is built to try to disconfirm this null, not to find a story that
confirms an edge exists.

**Normalization** (`pipeline/normalize.py`): every settlement price is
converted with `price_per_gram_fine = (price / grams_quoted) / (purity/1000)`,
using the "quoted per" grams (not the trading-unit grams — GOLDM trades in
100g lots but is quoted per 10g; using the trading unit here is a 10x bug
I specifically guarded against).

**Contract matching**: GOLDM expires 3rd–5th of a month; the other three
expire 27th–31st. A GOLDM contract's natural delivery-month partner is
about 25 days away in expiry date, not the literal nearest contract.
`concurrent_pairs()` takes an explicit `max_expiry_gap_days` so this
assumption is visible and adjustable rather than hidden in a "nearest
expiry" heuristic that would quietly pair the wrong months.

## 2. Data pipeline (`pipeline/`)

- `fetch.py`: requests use DD/MM/YYYY, responses use MM/DD/YYYY — handled
  explicitly, not inferred. MCX silently substitutes the nearest prior
  trading day for holidays/weekends/future/malformed dates instead of
  erroring; `fetch_bhavcopy_day` detects this (`DateMismatchError`) and
  `fetch_bhavcopy_range` treats it as a missing day rather than mislabeling
  the substituted data. Symbols are stripped/upper-cased. GOLDTEN rows
  dated before 2025-01-01 are rejected as bad data, not treated as a gap.
- `validate.py`: OHLC internal consistency, non-positive prices, expiry-vs-
  trade-date sanity, zero-activity flags, and day-over-day jump detection
  per discrete contract (used later to suppress alerts on untrustworthy
  settlements).
- `normalize.py`: purity/size normalization plus `ContractSeries`, which
  tracks every contract by **`(symbol, expiry_date)`**, never as a spliced
  continuous series — rollover splicing would inject artificial jumps at
  exactly the moments this strategy is trying to measure real ones.

## 3. Walk-forward backtest (`backtest/engine.py`)

- Signal for day *t* is a z-score of the normalized spread, computed from
  a rolling window of strictly prior days `[t-lookback, t-1]`. Day *t*'s
  own close is never in its own signal. The window slides forward and is
  re-estimated every day — this is what makes it walk-forward rather than
  a single in-sample fit.
- Entry: `|z| >= 2.0` (fixed a priori, not fit on this data), both legs
  pass a liquidity floor (`is_executable`), and there's at least 5 days
  before whichever leg expires first.
- Exit: reversion to `|z| <= 0.5`, a 20-day max hold, forced exit 5 days
  before the nearer expiry, or loss of executability — whichever comes
  first. Rollover is never automatic: a position is fully closed and any
  new-contract position is a fresh, independently-signaled entry, so
  rollover cost is paid explicitly.
- Transaction costs (`backtest/costs.py`): **Bhavcopy has no bid-ask or
  depth data, so these are stated assumptions, not measured spreads** —
  8–45 bps assumed spread and 4–25 bps assumed slippage per leg, widening
  from GOLDM (most liquid) to GOLDPETAL (thinnest, retail-sized). A
  round-trip spread trade pays this on both legs. An open-interest floor
  per contract excludes days where a fill is unlikely to be realistic at
  any real size.

## 4. Alert system (`alerts/engine.py`)

Reuses the exact same z-score/executability/cost gates as the backtest —
no separate, looser alert threshold that was never validated. Default
behavior is silence:

- No alert if `|z|` never approaches the entry threshold (not even logged
  — that's just normal market noise).
- A near-threshold signal that fails a gate is logged as a
  `SuppressedSignal` with a reason (`not_executable`, `too_close_to_expiry`,
  `insufficient_history`, `edge_below_cost`) so the silence is auditable,
  not just an empty feed you have to trust blindly.
- An `Alert` only fires when the expected reversion, discounted 50% and net
  of the assumed round-trip cost, clears a minimum edge floor
  (`MIN_EXPECTED_EDGE_BPS`) — so alerts aren't generated for moves the
  backtest itself would call unprofitable.

## 5. Attribution and validation (`backtest/attribution.py`)

Because every trade is a normalized-neutral spread (long one leg, short
the other), P&L should be close to market-neutral with respect to the
outright gold price — this is checked, not assumed, via the correlation of
trade net P&L to the concurrent move in the common gold level. The report
also stress-tests the (assumed) cost model at 0.5x and 2x, since the real
number is unknown, and explicitly refuses to render a verdict on fewer than
15 closed trades.

## 6. What the demo run found (synthetic data — see caveat above)

Two scenarios, both over ~3.5 years of synthetic daily bars, GOLDM vs
GOLDPETAL:

- **`null`**: no injected mispricing, just one common gold price path plus
  independent per-leg noise. 202 closed trades, mean gross P&L +51.8
  bps/trade, mean assumed cost 82.0 bps/trade (GOLDM+GOLDPETAL is the
  widest-spread pair) → **mean net P&L −30.2 bps/trade**. No edge, as it
  should be on data with nothing embedded in it.
- **`injected`**: a deliberately embedded, mean-reverting AR(1) mispricing
  between GOLDM and GOLDPETAL, sized larger than the assumed round-trip
  cost. 203 closed trades, mean gross P&L +63.6 bps/trade (higher than the
  null case, and win rate ~10x higher — the engine does find the injected
  signal) but **mean net P&L still −18.4 bps/trade** after the assumed 82
  bps round-trip cost for this specific pair.

**Reading this correctly**: the gross-P&L and win-rate gap between the two
scenarios shows the walk-forward/normalization machinery is doing its job
— it detects an embedded signal and does not fabricate one where none
exists. But GOLDM/GOLDPETAL is the widest-spread pair (illiquid
GOLDPETAL leg), and even an oversized injected edge doesn't clear that
assumed cost. That's not a failure of the code; it's the single most
useful honest result this kind of exercise can produce: **if GOLDPETAL's
real spread is anywhere near the assumed 45 bps, this specific pair is
very unlikely to have a tradeable edge, and the pair worth actually testing
on live data is GOLDM vs GOLDTEN**, where assumed round-trip cost is
8+4+15+8 = 35 bps instead of 82 — roughly 2.3x tighter. That substitution
is a one-line change to `run_scenario()`'s `pair=` argument and should be
the first thing run once real data is available. Running it on the same
injected-scenario synthetic data confirms the direction of this point:
GOLDM/GOLDTEN, 87 closed trades, 89.7% win rate, mean net P&L **+14.8
bps/trade** (positive, survives the assumed cost — though it flips negative
under the 2x-cost stress test, and 87 trades is still a thin sample). This
is a demonstration that the cost assumption is the dominant variable here,
not proof of a real GOLDM/GOLDTEN edge — but it's exactly the kind of
result that should redirect where real-data validation effort goes first.

**Bug found and fixed during validation**: the first version of the entry
logic had the direction sign inverted (betting the spread would fall was
scored as if it were betting the spread would rise), which produced a
suspicious *exactly* 0% win rate across ~400 trades in both scenarios —
a "too clean" failure signature that was the tell. Fixed in
`backtest/engine.py`; flagging this here rather than silently correcting
it, since a walk-forward backtest that doesn't independently sanity-check
its own P&L sign is exactly the kind of thing that produces false
"edge found" claims in practice.

## 7. Known limitations

- No real bid-ask/depth data exists in Bhavcopy; all costs are assumptions
  (labeled as such throughout) and must be replaced before any live
  decision is based on this.
- Synthetic data cannot tell you whether real MCX contracts are mispriced
  — it only proves the code path is wired correctly and isn't leaking
  future information into its own signal.
- `fetch.py`'s exact endpoint URL is a placeholder and needs confirming
  against the live site's actual API before use.
- The z-score entry/exit thresholds (2.0 / 0.5) and lookback (30 days) are
  fixed a priori for walk-forward integrity, not fit on any data —
  reasonable starting points, but untested for sensitivity; a real
  validation pass should check robustness across a grid of these before
  treating any one setting as "the" strategy.
- GOLDTEN's history only starts 2025, so any GOLDM/GOLDTEN or
  GOLDTEN/GOLDGUINEA backtest on real data will have a much shorter,
  less statistically meaningful sample than the GOLDM/GOLDPETAL or
  GOLDM/GOLDGUINEA pairs.

## Files

```
pipeline/fetch.py        MCX Bhavcopy client (date-format & substitution handling)
pipeline/validate.py     structural + price-jump sanity checks
pipeline/normalize.py    purity/size normalization, contract lifecycle, pairing
pipeline/synthetic.py    synthetic data generator (sandbox-only substitute for live fetch)
backtest/costs.py        assumed spread/slippage/liquidity-floor model
backtest/engine.py       walk-forward signal, entry/exit, no-look-ahead backtest
backtest/attribution.py  gold-correlation check, cost-sensitivity, verdict
alerts/engine.py         live alert gating (same thresholds as the backtest)
run_demo.py              runs both scenarios end-to-end, prints results
```

Run it: `python3 run_demo.py`
