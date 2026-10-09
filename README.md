# Mafutani Trading Consolidation and Consortium (zero-deps, stdlib only)

> **Disclaimer: not financial advice. Paper trading is the default. Live trading
> is disabled unless `LIVE_TRADING_ENABLED=1` + eligible tier + explicit confirm.**

## Runbook

```bash
python3 scripts/seed.py     # init DB + demo user + 30d synthetic 1m candles
python3 app.py              # serve on :8000 (PORT env overrides)
```

Demo creds: `demo@local` / `demo1234` (tier Pro).

CSV ingest:

```bash
python3 scripts/ingest_csv.py data.csv --symbol BTCUSD
# CSV header: ts,o,h,l,c,v (ts = epoch or ISO-8601)
```

Docker:

```bash
docker build -t mafutani-tcc .
docker run -p 8000:8000 mafutani-tcc
```

## API (JSON, cookie session + `X-CSRF` on POST/DELETE)

- `POST /api/auth/signup|login|logout`, `GET /api/me/entitlements`
- `GET /api/symbols`, `GET /api/candles?symbol&limit`, `GET /api/screener?min_vol&max_spread_bps`
- `GET/POST /api/watchlists`, `DELETE /api/watchlists/:id`
- `GET /api/strategies`, `POST /api/signals/preview`
- `POST /api/backtests`, `GET /api/backtests`, `GET /api/backtests/:id`
- `POST /api/orders` (paper default; live needs env + tier + `{confirm:true}`
  + configured venue credentials),
  `GET /api/orders`, `DELETE /api/orders/:id`,
  `POST /api/positions/flatten`, `GET /api/portfolio`
- `GET /api/brokers`, `POST /api/brokers/command` (venue layer: `paper`,
  `binance`, and the REST `*_style` profiles)
- `GET /api/arbitrage?asset_class=crypto|fx|commodity|all` (Pro+),
  `GET /api/arbitrage/venues` (public: venue registry + fee schedule)
- `GET/POST /api/risk`, `GET/POST /api/journal`
- `GET /api/admin/overview`, `GET /api/admin/audit` (Admin tier only)
- `GET /api/news?symbol&limit` (free public RSS, cached 10 min)
- `GET /api/cot?report&q`, `GET /api/cot/contract?contract&weeks`,
  `GET /api/cot/history?contract&category`, `POST /api/cot/sync`
  (CFTC Commitments of Traders, Pro+ — weekly positioning, not prices)
- `GET /api/status` (public service health: uptime, table counts, feed freshness)
- `POST /api/terminal/command` `{cmd}` — OpenTerminal (same risk/fill path as REST)
- `GET /stream/quotes` (SSE)

## OpenTerminal

Press `` ` `` or `Ctrl+K` (or the `_ Terminal` button) to open the
Bloomberg-style command drawer + scrolling quotes tape. 100% zero-deps
(vanilla JS + `core/terminal.py`, no xterm, no npm).

```
help | quote AAPL | buy AAPL 1 | sell BTCUSD 0.5 limit 70000
flatten [SYM] | positions | orders [N] | signal ema_trend AAPL
backtest ema_trend AAPL 2000 | screener | risk | tier
arb [crypto|fx|commodity|all] | venues BTCUSD
```

Terminal trades are paper-only and risk-checked exactly like REST orders.

## Proxy (SOCKS5 + HTTP CONNECT)

Venue calls leave from this machine's IP by default. For a *signed live
order* that is a real exposure: the exchange sees where the money moves
from. `data/proxy.py` routes all outbound traffic through a proxy you
control, stdlib only.

```bash
export MAFUTANI_PROXY="socks5://127.0.0.1:1080"     # or http://host:8080
export MAFUTANI_PROXY_NO_PROXY="internal.example"    # optional bypass list
python3 app.py
```

| Variable | Purpose |
|---|---|
| `MAFUTANI_PROXY` | `socks5://`, `http://`, `https://`. Empty string **disables** proxying, overriding any stray `HTTP_PROXY` |
| `MAFUTANI_PROXY_NO_PROXY` | Comma-separated hosts to reach directly; `*` disables all |

Resolution order is `MAFUTANI_PROXY`, then `SOCKS_PROXY` / `HTTPS_PROXY` /
`HTTP_PROXY`. **Unset changes nothing** — the no-proxy path delegates to
`urllib` exactly as before.

Why SOCKS5 is hand-written here: `requests[socks]`, `PySocks` and
`httpx` all support it and all are dependencies. RFC 1928 is a greeting,
an optional username/password exchange, and a CONNECT — about a hundred
lines of `socket`, with no cryptography, which is what keeps it inside the
stdlib budget.

**A dead proxy fails loudly.** It never falls back to a direct
connection, because that would leak the IP the operator was trying to
hide while leaving them believing they were protected.

Honest limits: verified against SOCKS5 and HTTP `CONNECT` servers written
for the tests, and against real TLS through a tunnel with certificate
verification on. Not verified against a commercial proxy (a corporate
appliance, or a WireGuard-based VPN, which has its own framing).

## Live trading (Binance spot)

Paper is the default. Live requires **all** of:

1. `LIVE_TRADING_ENABLED=1` in the environment
2. a tier with `live_enabled` (Pro+)
3. `{confirm: true}` on the request
4. `BROKER_BINANCE_API_KEY` + `BROKER_BINANCE_SECRET`

```bash
export LIVE_TRADING_ENABLED=1
export BROKER_BINANCE_API_KEY=...
export BROKER_BINANCE_SECRET=...
python3 app.py
```

Add `BROKER_BINANCE_TESTNET=1` to target `testnet.binance.vision` — a real
exchange with real signing and no real money. **Verify there before risking
funds.**

### Three states that are deliberately not conflated

| Venue says | Order status | Local ledger |
|---|---|---|
| `FILLED` | `filled` | position + cash written |
| rejected | `rejected` | nothing written |
| **timeout / no answer** | **`unknown`** | **nothing written** |

`unknown` is a real, findable state. A timeout *after* the exchange
accepted the order is exactly when an operator most needs to be told "go
look at the exchange" rather than shown a position they may or may not own.

This exists because the previous live path called a stub returning
`{"status": "routed_stub"}`, and the handler treated any non-rejected
status as a fill — so a live BTC buy reported `filled`, opened a position,
charged a fee and moved P&L for an asset that was never bought.

### Credentials

`BROKER_BINANCE_API_KEY` / `_SECRET` (uppercase, per-venue). Keys are never
logged, never stored, never returned by an endpoint — `/api/arbitrage/venues`
reports only a truncated SHA-256 fingerprint so an operator can confirm
*which* key is wired up.

Symbols are mapped explicitly (`BTCUSD → BTCUSDT`, `XAUUSD → XAUTUSDT`) and
an unmapped symbol is **refused**, never guessed. A suffix rule produced
`XAUUSDT` for gold; the venue's gold pair is `XAUTUSDT`.

## Backtests report a verdict, not just a Sharpe

Every backtest — API, UI, or terminal — returns a `significance` block next
to its metrics:

```json
"significance": {
  "verdict": "no edge after costs",
  "dsr": 0.0007, "psr": 0.999,
  "net_sharpe": -8.81, "expectancy_bps": -6.51,
  "trades": 169, "n_trials": 10,
  "realised_net_bps": -6.51, "cost_drag_bps": 8.0,
  "reasons": ["net expectancy -6.51 bps/trade is not positive"]
}
```

The verdict is one of `edge likely real`, `edge plausible, unproven`,
`no demonstrated edge`, `no edge after costs`, `not evaluable`, or
`insufficient data`. There is no path from a backtest to a confident
positive on thin evidence.

**Why this is not decoration.** The app has measured 0.2–4.6 bps of real
cross-venue dislocation against ~70 bps of round-trip taker cost, and runs
nine strategies with tunable parameters plus a walk-forward sweep. Every
parameter combination against one tape is another trial, so the best of
them is the maximum of many draws from the same noise, not one
observation. The Deflated Sharpe Ratio (Bailey & Lopez de Prado,
[SSRN 2460551](https://ssrn.com/abstract=2460551)) asks the only question
that matters: given how many things were tried, how surprising is this
best result?

`n_trials` defaults to the size of the strategy registry
(`significance.count_trials`) and can be overridden per request with
`params.n_trials`. Under-counting it is the single most common way to make
a DSR meaningless, so the number is always reported alongside the verdict.

Two failures this wiring exists to prevent:

- The tests are fed a **per-period** Sharpe, never the annualised one.
  Annualising the observed Sharpe while leaving the null threshold
  per-period compares two units and reported 300 samples of pure noise as
  DSR 1.00, "edge likely real".
- Costs are **not** charged twice. The event loop already deducts
  `fee_bps` on both sides and slips entry and exit, so the trade list is
  net; the modelled drag is reported as `cost_drag_bps` instead of being
  subtracted again inside the test.

`metrics.sharpe` is unchanged — it is the annualised equity-curve ratio and
is deliberately *not* the number the tests run on.

## Backtest window: 180 days, one number

`engines.backtest.BACKTEST_WINDOW_DAYS = 180`, derived to
`BACKTEST_MAX_BARS = 259,200` one-minute bars. It applies to every tier.

It replaces a per-tier `backtest_days` (30 / 365 / 1825 / 3650) that was
declared in `core/tiers.py`, seeded into the `tiers` table, and **read by
nothing**. `_bt_create` gated on strategy entitlement and passed
`from_ts`/`to_ts` straight through; the engine capped every tier at the
same hardcoded 20,000 bars. An Admin was entitled to 3,650 days and
received 13.9. The column remains in the schema marked deprecated so
existing databases need no migration, but `get_entitlements` now reports
the single real window as `backtest_window_days`.

### The window is a permission, not a promise

Raising the cap to six months does not manufacture six months of history —
the tape is whatever is stored. So every response carries a `coverage`
block saying what the run actually saw:

```json
"coverage": {"requested_days": 180, "bars": 43200, "days": 30.02,
             "short_of_window": true,
             "note": "only 30.0 days of history stored for this symbol, not the full 180 requested"}
```

`truncated_by_cap` and `short_of_window` are distinguished on purpose:
the first means a long tape was cut at the cap, the second means the tape
never existed. Both are "fewer bars than requested" and they mean opposite
things.

### Equity is strided, not truncated

A six-month run yields ~259,000 equity points. Storing `equity[:5000]`
kept the first 11 days and silently dropped the rest, so the chart drew a
three-week curve under a six-month heading. `downsample_equity` strides to
5,000 points, keeps the first and last, and the response sets
`equity_downsampled: true`. 5,000 points is far more than a 1,000px canvas
resolves.

Cost: a 259,200-bar run takes ~21s synchronously (~1.6s at the old 20,000
cap). No caching yet — repeat runs recompute.

## Strategies

| Name | Tier | What it does |
|---|---|---|
| `ema_trend` | Free | EMA 9/21 cross with ATR and spread gates |
| `dual_ema` | Free | 12/26 cross confirmed by price vs the slow EMA |
| `ema_trend_cot` | Pro | EMA 9/21 with entries vetoed when the CFTC shows the crowd fully positioned (3-day lag enforced) |
| `vwap_pullback` | Pro | Buys dips below a rising VWAP, sells bounces above a falling one |
| `vwap_bands` | Pro | Session-VWAP sigma bands + 200-EMA regime gate + 50-EMA stop ([SSRN 6650958](https://ssrn.com/abstract=6650958)) |
| `mean_reversion` | Pro | Z-score fade of extremes |
| `manual` | Pro | One-click passthrough, still spread- and risk-checked |
| `orb_breakout`, `rsi_fade`, `book_imbalance`, `news_momentum` | Pro / Institutional | Metadata only — no logic yet |

`vwap_bands` uses the session-anchored VWAP in `engines/vwap.py` rather than
`indicators.vwap_series`. The latter accumulates from the first candle it is
handed and never resets, so on 1m bars its "session" VWAP is really a
multi-day average that drifts further from the current session every day.
The parity tests pin `vwap_series`, so it was left alone and the
session-aware family added beside it. Bands are the *volume-weighted*
standard deviation of the cumulative price series, not the variance of the
closes, so "2 sigma" means the same thing here as on any other VWAP band
chart.

## COT — CFTC Commitments of Traders

Weekly **positioning**, straight from the CFTC. No key, no account. Reads
the published plain-text current-week files and the annual zipped archives.

```bash
python3 scripts/ingest_cot.py weekly                     # all 7 reports
python3 scripts/ingest_cot.py weekly --report disagg_comb
python3 scripts/ingest_cot.py history --report disagg_comb --from 2019
python3 scripts/ingest_cot.py summary
```

| Module | Role |
|---|---|
| `data/cot.py` | fetch + parse. Pure, no DB |
| `data/cotstore.py` | SQLite persistence + queries |
| `engines/cot.py` | analytics. Pure, no I/O |
| `scripts/ingest_cot.py` | CLI |

**~1.4M rows across 2015–2026** for legacy + disaggregated + TFF. Weekly
pull is ~470KB/report; `history` is idempotent (upsert on
`(report, contract_code, report_date, category)`).

### This is not a price feed

The single most important thing about this data: it is a count of
contracts **held**, as of each Tuesday, published the following Friday at
15:30 ET. It cannot be joined to `candles_1m` as if it were a tape and
must not be backtested as one. Every API response and every UI panel
carries the report date and its age — a COT figure read on Monday
describes a market as it stood nine days earlier.

### The four families, and why they never merge

| Report | Splits open interest into | History from |
|---|---|---|
| `disagg` | Producer/Merchant, Swap Dealers, Managed Money, Other Reportables | 2010 |
| `tff` | Dealer/Intermediary, Asset Manager, Leveraged Funds, Other Reportables | 2010 |
| `legacy` | Non-Commercial, Commercial | 1986 |
| `cit` | Non-Comm/Comm *excluding* index traders, plus the index traders | 2006 |

The classifications are **not comparable across families**, so `report` is
in the primary key. Legacy's "commercial" for an equity index means the
market makers, which tells you nothing about positioning — that is what
TFF exists to fix. `other_rept` *is* shared, because both reports use it
for the same thing.

### Layouts are pinned by index, and validated

The weekly files have **no header row**; the annual archives do, and their
column names differ between families (`Swap__Positions_Short_All` with a
double underscore, `Change_Comm_Short_All NoCIT` with a trailing space).
Reading the annual header and applying it to the weekly file would be a
silent misalignment the moment CFTC renamed a column.

So each family carries an explicit index map and every row is validated —
field 2 must be a date, field 3 an alphanumeric code. A layout that has
shifted **raises** rather than writing the swap dealer's short into the
managed money column. Two CFTC accounting identities are pinned as tests
and hold exactly on all 282 live contracts:

```
total reportable = Σ(categories) + Σ(spread)      # on BOTH sides
open interest    = total reportable + non-reportable
```

The first is the trap: spreading has to be added to longs *and* shorts.
Omit it and the totals do not tie — but the numbers still look plausible.

### What you actually get

A raw net position is not comparable across contracts — 180k is trivial in
crude and enormous in lean hogs. `engines/cot.py` normalises against each
contract's own trailing history and returns four views, because they
disagree at turning points and that disagreement is the information:

- `cot_index` — Williams COT Index, where the net sits in its window (0–100)
- `percentile` — rank within the window
- `zscore` — standard deviations from the window mean
- `pct_of_open_interest` — size-independent

Plus `crowding` (4 blunt bands), `divergence` (positioning vs price — the
"shorts building into a rally" quadrant), and `consensus` across
categories.

**Thin data refuses rather than guesses.** One observation has no
percentile, index or z-score; all return `None`, never `0.0`. A zero would
read as "the bottom of the range" — a confident claim about a market where
exactly one thing is known. A flat window has no dispersion, so the
z-score is `None` and the index is 50 (genuinely mid-range). The window
length is echoed with every result, because a 2-year and a 5-year
percentile are different claims.

### API

Pro+ (a COT pull reveals where large money sits across ~470 contracts):

- `GET /api/cot?report=&q=&limit=` — contract list
- `GET /api/cot/contract?contract=&report=&weeks=` — full analysis
- `GET /api/cot/history?contract=&category=&limit=` — series for charting
- `POST /api/cot/sync {report}` — one report, CSRF-gated

UI: `#/cot`. Sync is one report per call with no cache age — the release
is weekly, so hammering the CFTC buys nothing and is rude.

### Where COT is *not*: no decision reads it

COT is rendered beside the technicals on the Analysis page and appears as
an overlay in `analyze_symbol`'s payload — and **nothing in the trading
path reads it.** `engines/signals.py` has no COT-aware strategy,
`engines/backtest.py` never loads it, and no parameter accepts it.

That is deliberate, and `COT_INFLUENCES_SCORE = False` says so in code.
The overlay is attached *after* `score`/`bias`/`reason`/`trend` are
settled, so it cannot reach them even by accident, and
`tests/test_cot_overlay.py` runs the same tape with and without a COT
block — including a maximally extreme one — and asserts those fields are
identical.

Stage two: **`ema_trend_cot`** (Pro+) is EMA 9/21 with entries vetoed when
the CFTC shows the speculative crowd already fully positioned. It is
registered in `STRATEGIES`, `core/tiers.py` and `data/schema.sql`.

**The publication lag is the whole ballgame.** A report dated Tuesday is
published Friday at 15:30 ET. `engines/cotgate.py` keys every print by
its *publication* instant, and `visible_at(ts)` returns only what had
actually been published by that bar. Joined naively onto candles, a
Tuesday report would look available on Tuesday — and that single join is
where every spectacular-but-fake Sharpe this strategy could produce comes
from. It reads like a discovery, not a bug.

`build_timeline` computes each print's percentile and index from
**point-in-time only** — entries 0..i, never a later print. An earlier
draft had a cheaper whole-series variant; it was the same look-ahead
hidden behind a function name that didn't advertise the difference, and
it was removed rather than documented.

**Absence is not permission.** Before the first publication — or with one
observation, and therefore no percentile — the strategy returns flat with
`cot-no-data` or `cot-no-gate`. It never trades ungated while configured
with a veto. `cot_require: false` waives *missing data*; it does not
ignore a reading. To switch the gate off deliberately, pass an
unreachable threshold (`cot_max_percentile: 101`).

**It is a veto, not a signal.** It never opens a position the EMA would
not have taken and never flips a held position — a test sweeps five tapes
× three gate states and asserts every emitted side is one the EMA also
emits. That's the shape a slow weekly measurement honestly supports: it
changes which trades are taken, not when.

**The sample size is reported, not implied.** `cot_gate.observations`
counts publications, not bars. On real data (106 prints, 1400 hourly
bars): gate on → 33 trades, 584 bars vetoed; gate off → 62 trades. The
effective sample is 106 observations, and no Sharpe over those trades can
be more significant than that.

`bars_vetoed` is deliberately *not* called `blocked_entries`. The gate
runs before the EMA, so it declines on every bar where the crowd is
crowded, including the overwhelming majority where the EMA had no cross
and was never going to trade. It's an upper bound, and counting exactly
would mean evaluating the EMA twice per bar.

**Trial counting.** Adding `ema_trend_cot` to `STRATEGIES` makes it
another trial automatically (11 now, was 10). Sweeping gate thresholds
adds more: a 4-value `cot_max_percentile` sweep reports `n_trials: 14`.
Under-counting that is exactly the error deflation exists to correct.

### Symbol mapping is exact-match and verified

CFTC names don't match app symbols. `COT_CONTRACT_MAP` in `api/routes.py`
holds **exact** CFTC market names — no substring fallback, because a
partial match on "GOLD" also catches "MICRO GOLD" and "GOLD -1 TROY
OUNCE", and a screener row silently showing the wrong contract's
positioning is worse than showing nothing.

Every string was verified against live 2026-09-29 reports. The first
draft was written from memory and **11 of 21 were wrong** — `NATURAL GAS -
NYMEX` does not exist, it's `NAT GAS NYME - NEW YORK MERCANTILE
EXCHANGE`. Wrong names fail by never matching, which is invisible in
review. A test now re-verifies the map against stored rows.

BRENT is deliberately unmapped: the reports carry `BRENT LAST DAY` and
Brent/WTI spreads, neither of which is the front contract the app quotes.
BTCUSD, ETHUSD and AAPL have no CFTC contract at all. All report as
unmatched rather than guessed — and "no positioning data for AAPL" is
kept distinct from "AAPL positioning is neutral".

## Arbitrage (crypto / currency / commodity)

Four models, because arbitrage is not one thing. Every edge is **net of
two legs of taker fees** — gross spread is not profit.

| Model | Class | Needs |
|---|---|---|
| `cross_venue` | crypto | two real books for one symbol (Binance + Coinbase) |
| `triangular` | crypto | a venue-tagged three-leg cycle, `A → B → C → A` |
| `fx_cross` | currency | direct rate vs a synthetic cross (`EURUSD` vs `EURGBP × GBPUSD`) |
| `spread` | commodity | correlated contracts (Brent–WTI crack, gold/silver) |

### The one structural change this needed

`quotes_l1` is keyed by symbol alone — one price row set per instrument.
That is correct for a tape ("what is BTCUSD trading at?") and it is why
cross-venue arbitrage was not expressible: `cryptomarkets.ROUTES` is 1:1,
so BTCUSD pointed at Binance and Coinbase had nowhere to go.

`venue_quotes` is the additive second dimension, keyed `(symbol, venue)`.
`quotes_l1` keeps answering "what is this worth"; `venue_quotes` answers
"where can I buy it and where can I sell it". `ROUTES` was deliberately
*not* widened to a list — that would have forced every existing caller to
decide which element to take, and the tape could have started averaging
two venues into one invented price.

### Three refusals the scanner makes on purpose

- **No edge from a synthetic leg.** Any model touching a generated tick
  returns `edge_bps=None` and a reason. Same reasoning that put `source`
  on `quotes_l1`.
- **A persistent gap is not an executable edge.** Books older than the
  freshness window are kept and reported (`persistent_gap`), not dropped:
  the exchange-default literature finds dislocations *persist* on venues
  traders distrust. But they are never priced as tradeable, because a gap
  measured on old quotes cannot be executed.
- **Unpriceable is not zero.** A model that cannot price an edge says so.
  Collapsing "cannot price now" into "nothing here" is how a scanner
  reports an empty table when the honest answer is "never looked".

**Which venues are real.** Crypto gets two tradeable books each (Binance,
Coinbase). FX uses **Kraken**, which publishes a real two-sided book —
not a mid price, so the cross models can be costed honestly. Gold is the
one commodity with two free keyless venues (Binance + Kraken both publish
XAUTUSD). Everything else has no free source and falls to the synthetic
generator, labelled as such.

FX pairs live in `data/fxmarkets.py`, not the seeder: a cross needs three
pairs, and the pairs arbitrage needs must not depend on a dev seeder
having been re-run. Installed on boot, so an existing DB picks them up
without a re-seed.

Set `MAFUTANI_ARB_PERSIST=0` (the default) to keep venue books in memory
only. The scanner fetches on demand behind a 15s cache, so arbitrage
works from a cold start with nothing stored; persisting buys a record of
past books the scanner does not read, at the cost of a second
market-data table. Set `MAFUTANI_ARB_PERSIST=1` if you want that history.

Set `MAFUTANI_ARB_FEED=0` to stop the feed loop polling venues (it is up to two HTTP
calls per symbol per tick, against third-party rate limits).

## Tests

```bash
python3 -m unittest discover -s tests   # 1150 tests, stdlib only
```

## Workspace standard

Terminal-grade UX inspired by [ErTasselli/OpenTerminal](https://github.com/ErTasselli/OpenTerminal)
(MIT): ⌘K/Ctrl+K command palette, quote panel, sortable screener with
sparklines, sector-style heatmap, news feed, flash-on-tick quotes — all
reimplemented here from scratch with zero dependencies (no Next.js/React).
