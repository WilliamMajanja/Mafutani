# Research: sub-agent architectures for trading, analysis, strategy testing, execution

Survey of public GitHub systems, read 2026-10-16. Star counts at time of reading.

Scope note: this is a survey of *designs*. Following the OpenBB precedent
(`data/fred.py`), the intent is to reimplement what transfers in stdlib rather
than adopt a dependency. None of the surveyed projects are importable under
`RFC-005`; the most popular has 16 direct dependencies including `langchain`
and `pandas`.

## 1. The landscape

| Stars | Project | Shape |
|---:|---|---|
| 111,471 | `TauricResearch/TradingAgents` | The canonical multi-agent design; everything else forks or adapts it |
| 35,620 | `HKUDS/Vibe-Trading` | Personal trading agent, same lineage |
| 6,314 | `The-Swarm-Corporation/AutoHedge` | "Swarm intelligence", agent-generated agents |
| 3,717 | `simonlin1212/TradingAgents-astock` | 7 analysts, bull/bear debate, A-share rules |
| 1,087 | `kyky2347/ALTA` | Research-only. Evidence-first, reviewer challenge, Shadow/Broker split |
| 379 | `0xethanq/astra-quant-agent` | Multi-LLM analysis + **deterministic** Python risk + OKX execution |
| 94 | `renee-jia/trading-bot` | 5 agents → weighted score → live Alpaca. Publishes its own negative result |

The field's centre of gravity is long-horizon swing/equity analysis. Almost
none of it is intraday microstructure or cross-venue arbitrage, which is where
mafutani sits.

## 2. The canonical topology

From `TradingAgents/graph/conditional_logic.py`. The topology is fixed, not
adaptive — a round-robin with a turn budget:

```
analysts (parallel): market · fundamentals · news · sentiment
        |
   bull <-> bear debate, N rounds each, alternating
        |
   Research Manager        <- scores the debate into a thesis
        |
   Trader                  <- sizes the position
        |
   risk debate: Aggressive -> Conservative -> Neutral, N rounds each
        |
   Portfolio Manager      <- final BUY / HOLD / SELL
        |
   execution
```

The debate is a *social* construct. `should_continue_debate` alternates by
checking whether the last message starts with "Bull" — the debate runs to a
turn count regardless of whether anyone changed their mind.

### What transfers

- **Separation of analysis from synthesis.** Many independent readers, one
  adjudicator. Mafutani already has this shape in `engines/analysis.py` and
  `engines/anomaly.py`, deterministically rather than with a model.
- **A distinct risk stage after the trader, not inside it.** Mafutani already
  has this: `engines/risk_check.py` and `core/risk.py` are separate from
  `engines/signals.py`.
- **Versioned policy constants as first-class state.** ALTA carries
  `EVALUATION_PROTOCOL_VERSION`, `SCOUT_PROMPT_VERSION`,
  `RANKING_POLICY_VERSION`, `RESEARCH_QUALITY_VERSION`,
  `UNDERWRITING_POLICY_VERSION`. A result is only interpretable against the
  exact policy version that produced it.

## 3. The single most valuable idea: point-in-time discipline

`TradingAgents/dataflows/date_window.py` is the best-engineered file in the
survey, and it has nothing to do with LLMs. Four rules:

1. **`as_of` / `as_of_window`** clamp any date the model asks for to the trade
   date, so no tool reaches a vendor with a later one.
2. **`in_window(pub_dt, start, end)`** trims dated items to a half-open window
   `[start, end + 1 day)`. An item with *no* timestamp is kept **only when the
   window reaches the present** — the comment is the argument:
   > a backtest cannot prove it is not from the future
3. **`coverage_gap(...)`** reports a window a feed could not reach as
   *unavailable*, not as *empty*. Yahoo news and the social feeds return their
   latest items regardless of the window asked for, so "none found" over a
   window they never observed would assert an absence nobody saw.
4. **`withhold_live_profile`** withholds present-day snapshots from historical
   runs.

Rule 3 is mafutani's house rule — refuse rather than default — arrived at
independently by a different route and applied to the same class of error.

## 4. Does it work? The honest answer from the one repo that measured it

`renee-jia/trading-bot` is a complete system: 5 specialist agents (technical,
trend, macro, sentiment, quantitative alpha), weighted fusion into a 0–100
conviction score, real Alpaca execution, +34.5% paper return against SPY
+16.1%. Its `docs/BACKTEST_RESULTS.md` then decomposes that number:

| Year | Strategy | Basket B&H | SPY | Strategy − SPY | **Strategy − Basket** |
|---|---:|---:|---:|---:|---:|
| 2022 | −45.4% | −41.2% | −18.6% | −26.7% | **−4.1%** |
| 2023 | +92.4% | +103.5% | +26.7% | +65.7% | **−11.1%** |
| 2024 | +60.1% | +51.5% | +25.6% | +34.5% | **+8.6%** |
| 2025 | +24.2% | +22.4% | +18.0% | +6.2% | **+1.8%** |
| 2026 YTD | +26.6% | +16.7% | +10.7% | +15.9% | **+9.9%** |

Over a single clean window: strategy Sharpe **2.05** vs its own equal-weight
basket **2.13**. The strategy's risk-adjusted edge over simply holding the same
names is *negative*.

Its own conclusions:

> Most of the headline "beat SPY by 30–65%" is the survivorship-biased basket,
> not the algorithm.
>
> Strategy V6 behaves as a **beta amplifier** on a hand-picked winner basket —
> additive in up years, worse in down years — without a stable risk-adjusted
> edge over simply equal-weighting the same names.

It also records that its *earlier* version decided and executed on the same
close, "inflating returns" — the look-ahead bug, caught after the fact.

This is the trap `engines/significance.py` already exists to catch, and the
decomposition methodology is the part worth stealing: **split the return into
market beta, selection, and timing**, and quote only the last.

## 5. Where mafutani is already stronger

| Survey norm | mafutani |
|---|---|
| Multi-agent debate, model-adjudicated | `brokers/gods.py`: "No LLM. The dissenter is deterministic." |
| Agents propose and adopt rules | "No auto-promotion... Adoption is an explicit operator action." |
| Agent writes strategy code | "No codegen. A mutated rule is a new condition tree... never a string that gets exec'd." |
| Backtest as validation | `engines/walkforward.py` IS/OOS folds; `engines/significance.py` deflated Sharpe for trial count |
| Implicit live/backtest parity | `engines/backtest.py` calls the same `preview_signal` the live board uses |
| Silent feed failures | `data/feeds.py` `feed_health`, RFC-002 |

The GODS layer already implements "deterministic dissenter + veto gate +
explicit operator adoption" — the substance these projects sell as multi-agent
debate, without the model in the loop.

## 6. The real gaps

1. **No point-in-time layer.** Nothing clamps a request to the trade date.
   `candles_1m` and friends are read as-is. This is the highest-value gap and
   it is exactly the defect the surveyed repo found in itself.
2. **No edge lifecycle.** `strategies.mode` is `wired`/`stub` — a code-quality
   label, not an evidence label. Nothing records "passed significance at trial
   count N, OOS ratio R, M forward cycles" and nothing gates on it.
3. **No forward-shadow stage.** `brokers/paper.py` exists. `brokers/interactive.py`
   has a `shadow` command, but that means "infer a profile from this account's
   *filled* trades" — an unrelated sense. There is no stage between "paper
   fills" and "live" that requires N forward cycles of evidence.
4. **No broker verification binding.** ALTA's `Verification` is worth copying:
   the record is bound to a config `revision`, so changing credentials
   invalidates it, and critically — *"An old success must not outlive a later
   failed authentication probe"* — a later failure overwrites an earlier
   success. Snapshot freshness is bounded (30s).
5. **No systematic-exposure accounting.** ALTA's `AlphaIsolation` is
   deliberately a *conservative sizing input*: "A high score can preserve a
   risk budget; it cannot manufacture one." Mafutani's `fx_cross` books carry
   beta and venue risk that is measured per-trade but never classified.

## 7. Recommended, in order

**A. Point-in-time data layer (stdlib, ~1 module).** `core/asof.py`.
`as_of(conn, t)`, `in_window(pub, start, end)` with the undated-item rule, and
`coverage_gap()` returning *unavailable* rather than *empty*. Then apply it to
`data/cot.py`, `data/news.py`, `data/fred.py` — all three have publication
dates and all three can currently leak the future into a backtest. This is the
one item that fixes a real correctness bug rather than adding a feature.

**B. Edge ledger + promotion gate (stdlib, ~2 modules).** `engines/edgeledger.py`.
Append-only records: strategy, code version, policy version, trial count,
deflated Sharpe, OOS ratio, forward cycles observed. A strategy is eligible for
live only when the ledger says so, and the gate **reports why not** when it
refuses. Extends `brokers/gods.py`'s existing no-auto-promotion stance into
something enforceable rather than documented.

**C. Forward-shadow stage.** Paper fills recorded per strategy with the
evidence that preceded them. Promotion requires N cycles. Deterministic;
no model involved.

**D. Systematic-exposure classification.** Tag each candidate with its
exposures (market beta, venue, carry) so sizing can subtract known beta
instead of discovering it in drawdown.

**What not to do:** add a bull/bear debate. `gods.py` already argues why a
deterministic dissenter beats a model-based one — "a dissenter that needs a
model is a dissenter that can be talked out of its objection" — and §4 is the
empirical case that the elaborate versions have not earned their complexity.
The transferable content of this survey is §3 and §4, not §2.
