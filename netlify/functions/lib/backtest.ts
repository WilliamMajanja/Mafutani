import type { Candle } from "./analytics";

const COST_BPS = 5;

function emaSeries(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const out: number[] = [];
  let prev = values[0];
  for (let i = 0; i < values.length; i++) {
    prev = i === 0 ? values[0] : values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

export type BacktestResult = {
  metrics: Record<string, unknown>;
  equity: number[];
  params: Record<string, unknown>;
};

/** Long/flat EMA-cross backtest with explicit cost, on stored bars only. */
export function runBacktest(
  symbol: string,
  strategy: string,
  candles: Candle[],
  params: Record<string, unknown> = {},
): BacktestResult {
  const fast = Number(params.fast ?? (strategy === "dual_ema" ? 12 : 9));
  const slow = Number(params.slow ?? (strategy === "dual_ema" ? 26 : 21));
  if (candles.length < slow + 2) {
    throw Object.assign(new Error(`need at least ${slow + 2} bars for this strategy`), { status: 400 });
  }
  const closes = candles.map((c) => c.close);
  const f = emaSeries(closes, fast);
  const s = emaSeries(closes, slow);

  let equity = 100;
  let peak = 100;
  let maxDd = 0;
  let position = 0;
  let entry = 0;
  let trades = 0;
  let wins = 0;
  const curve: number[] = [];
  const tradeReturns: number[] = [];

  for (let i = 1; i < candles.length; i++) {
    const signal = f[i] > s[i] ? 1 : 0;
    const px = closes[i];
    if (signal !== position) {
      if (position === 1) {
        const ret = (px - entry) / entry - COST_BPS / 10_000;
        equity *= 1 + ret;
        tradeReturns.push(ret);
        trades += 1;
        if (ret > 0) wins += 1;
      } else {
        entry = px * (1 + COST_BPS / 10_000);
      }
      position = signal;
    } else if (position === 1) {
      equity *= 1 + (px - closes[i - 1]) / closes[i - 1];
    }
    peak = Math.max(peak, equity);
    maxDd = Math.min(maxDd, (equity - peak) / peak);
    curve.push(Number(equity.toFixed(4)));
  }

  // Close an open position at the final bar for a complete round trip.
  if (position === 1) {
    const ret = (closes[closes.length - 1] - entry) / entry - COST_BPS / 10_000;
    equity *= 1 + ret;
    tradeReturns.push(ret);
    trades += 1;
    if (ret > 0) wins += 1;
    curve[curve.length - 1] = Number(equity.toFixed(4));
  }

  const rets = tradeReturns;
  const mean = rets.length ? rets.reduce((a, b) => a + b, 0) / rets.length : 0;
  const sd = rets.length > 1
    ? Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1))
    : 0;
  const barsPerYear = 252;
  const years = Math.max(1 / barsPerYear, candles.length / barsPerYear);
  const totalReturn = equity / 100 - 1;
  const cagr = (1 + totalReturn) ** (1 / years) - 1;
  const sharpe = sd > 0 ? (mean / sd) * Math.sqrt(rets.length / years) : 0;

  const metrics: Record<string, unknown> = {
    strategy,
    symbol,
    candles: candles.length,
    total_return_pct: Number((totalReturn * 100).toFixed(2)),
    cagr_pct: Number((cagr * 100).toFixed(2)),
    max_drawdown_pct: Number((maxDd * 100).toFixed(2)),
    sharpe: Number(sharpe.toFixed(2)),
    trades,
    win_rate_pct: trades ? Number(((wins / trades) * 100).toFixed(2)) : null,
    avg_trade_pct: trades ? Number((mean * 100).toFixed(2)) : null,
    cost_bps_per_trade: COST_BPS,
    significance: {
      verdict: trades >= 20 && sharpe >= 1 ? "edge plausible, unproven" : "no edge after costs",
      net_sharpe: Number(sharpe.toFixed(2)),
      trades,
      n_trials: 1,
      cost_drag_bps: COST_BPS,
      reasons: [
        "single-symbol, single-strategy run; not a multiple-testing correction",
        "daily bars only; intraday execution effects are not modelled",
      ],
    },
    coverage: {
      days: Math.round((candles[candles.length - 1].ts - candles[0].ts) / 86400),
      bars: candles.length,
      requested_days: 3650,
      short_of_window: candles.length < 3650,
      truncated_by_cap: false,
      note: "daily bars from the stored Yahoo series",
    },
  };
  return { metrics, equity: curve, params: { fast, slow } };
}
