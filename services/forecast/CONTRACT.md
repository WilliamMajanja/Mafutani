# Mafutani forecast service contract — `mafutani.forecast/1`

A forecast model (Kronos, TimesFM, anything) runs as a **separate process**
with its own heavy dependencies. This repository stays stdlib-only and talks
to it over HTTP.

The split exists because the constraint is real: Kronos is a PyTorch model.
Pulling torch into a project whose defining property is "stdlib only" would
be trading the thing that makes this codebase tractable for a marginal
convenience. So the model lives outside, and only its wire format lives here.

## Why the client sends the candles

The service is **stateless**. Mafutani reads `candles_1m` and POSTs the
series it wants modelled; the service runs inference and returns a forecast.

The obvious alternative — service owns a database, client queries it — was
rejected because it makes two things true that should not be true. It
duplicates 1.5M rows of history into a second store that can silently drift
from the first, and it makes the forecast's provenance ambiguous. Here, the
exact series that produced a number is the series in the request, so a
forecast can always be reproduced from the row it was computed from.

## Endpoints

### `GET /health`

```json
{
  "contract": "mafutani.forecast/1",
  "ok": true,
  "model": "rw-null",
  "family": "baseline",
  "is_real_model": false,
  "lookback": 400,
  "horizons": [1, 4, 8],
  "uptime_s": 41.2
}
```

`family` is `"baseline"` or `"foundation"`. `is_real_model` is the one field
that matters and the client **rejects the response if it is absent** — see
"Isolation guarantee" below.

### `POST /predict`

Request:

```json
{
  "contract": "mafutani.forecast/1",
  "symbol": "BTCUSD",
  "freq": "1m",
  "bars": [[1791543000, 82560.5], [1791543060, 82561.2]]
}
```

`bars` is `[[ts, close], ...]`, **oldest first**, most recent last. Only
`close` is required today; OHLC may be sent by a future contract revision.

Response:

```json
{
  "contract": "mafutani.forecast/1",
  "model": "rw-null",
  "family": "baseline",
  "is_real_model": false,
  "symbol": "BTCUSD",
  "freq": "1m",
  "asof": 1791544075,
  "n_bars": 400,
  "last_close": 82560.52,
  "predicted": [
    {"horizon": 1, "mean": 82560.52, "sigma": 12.10,
     "p10": 82444.99, "p50": 82560.52, "p90": 82676.05}
  ],
  "elapsed_ms": 2.7
}
```

`horizon` is in **bars**, not seconds. `p10`/`p50`/`p90` are quantiles of the
predictive distribution, not confidence intervals on a point estimate — a
90%-interval implies a well-calibrated model, and the client refuses to let
one be implied without `sigma` being present to check it against.

### Errors

Any non-2xx carries a JSON body with an `error` string. The client surfaces
the message verbatim. It never substitutes a fallback number — see below.

## The isolation guarantee

**The client returns an error rather than a forecast whenever the service is
unreachable, slow, malformed, or lying about what it is.**

This is the whole design constraint. A terminal that shows a plausible number
when the model failed is worse than one that shows nothing, because the
number is indistinguishable from a real forecast at a glance — and a trader
who cannot tell a real model from a fallback will eventually act on the
fallback. Every failure path in `data/forecast.py` raises
`ForecastUnavailable`; none of them return a number.

Three specific rejections, all in the client's response validation:

1. **`contract` mismatch** — a service speaking a different revision is not
   silently reinterpreted.
2. **Missing `is_real_model`** — a service that cannot say whether it is a
   trained model is not trusted with a number. A reference implementation
   that omitted this field would pass silently otherwise, which is precisely
   the failure this contract exists to prevent.
3. **`sigma` absent while quantiles are present** — quantile claims without a
   dispersion to check them against are decoration.

## Display only

Forecasts are **observations, never instructions**. No forecast endpoint
places, modifies, or arms an order, and `forecast` is deliberately absent
from `methods_allowed` in every tier, so no backtest or signal path can
reach it. `engines/forecast.py` exports `NOT_A_TRADE` for code that wants to
assert that.

The reasoning is not squeamishness. A time-series foundation model has no
demonstrated edge on liquid crypto — Kronos' own reported results are
in-sample and dominated by a market that has since moved — and gating money
movement on a number that cannot beat a random walk is how a paper portfolio
becomes a real loss.

## The null model is the point, not a placeholder

`services/forecast/reference_server.py` is a **random walk with drift**, and
it is not scaffolding to be deleted. It is the benchmark a real model has to
beat, and it is computable in pure Python:

> The best forecast available without an edge is that the next bar is the
> last bar, and the band it should be judged against widens as √horizon.

A model that cannot beat this is adding nothing. So the reference service is
useful on its own terms, and swapping in Kronos is a matter of running a
different process against the same contract — no change to this repository.