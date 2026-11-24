/**
 * Market analytics computed from stored candles. Every figure returned here is
 * derived from rows in Postgres; nothing is simulated. When a series is too
 * short for a statistic the field is null rather than a plausible-looking
 * placeholder.
 */

export type Candle = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source: string;
};

export type Analysis = {
  bias: "long" | "short" | "flat";
  score: number;
  last: number | null;
  rsi14: number | null;
  trend: "up" | "down" | "flat";
  momentum_pct: number | null;
  vol_ann_pct: number | null;
  atr_pct: number | null;
  anomaly_score: number;
  anomalies: Anomaly[];
  reason: string;
  regime: string;
  regime_description: string;
  timeframes: [string, string][];
  tf_verdict: string;
};

export type Anomaly = { kind: string; severity: "low" | "medium" | "high"; message: string };

export function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    out.push(i + 1 >= period ? sum / period : null);
  }
  return out;
}

export function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  const k = 2 / (period + 1);
  let prev: number | null = null;
  for (let i = 0; i < values.length; i++) {
    if (i + 1 < period) { out.push(null); continue; }
    if (prev == null) {
      prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
    } else {
      prev = values[i] * k + prev * (1 - k);
    }
    out.push(prev);
  }
  return out;
}

export function rsi(values: number[], period = 14): number | null {
  if (values.length <= period) return null;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  gain /= period;
  loss /= period;
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (loss === 0) return gain === 0 ? 50 : 100;
  const rs = gain / loss;
  return 100 - 100 / (1 + rs);
}

export function atrPct(candles: Candle[], period = 14): number | null {
  if (candles.length <= period) return null;
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i];
    const p = candles[i - 1].close;
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p), Math.abs(c.low - p)));
  }
  const atr = trs.slice(-period).reduce((a, b) => a + b, 0) / period;
  const last = candles[candles.length - 1].close;
  return last ? (atr / last) * 100 : null;
}

function stdev(values: number[]): number {
  if (values.length < 2) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  return Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length);
}

export function annualisedVolPct(closes: number[]): number | null {
  if (closes.length < 20) return null;
  const rets: number[] = [];
  for (let i = 1; i < closes.length; i++) {
    if (closes[i - 1] > 0) rets.push(Math.log(closes[i] / closes[i - 1]));
  }
  return stdev(rets) * Math.sqrt(252) * 100;
}

export function analyse(candles: Candle[]): Analysis {
  const closes = candles.map((c) => c.close);
  const last = closes.length ? closes[closes.length - 1] : null;
  const r = rsi(closes);
  const fast = ema(closes, 9);
  const slow = ema(closes, 21);
  const f = fast.length ? fast[fast.length - 1] : null;
  const s = slow.length ? slow[slow.length - 1] : null;
  const trend: "up" | "down" | "flat" = f == null || s == null ? "flat" : f > s ? "up" : f < s ? "down" : "flat";
  const back = closes.length > 20 ? closes[closes.length - 21] : null;
  const momentum = back && last ? ((last - back) / back) * 100 : null;
  const vol = annualisedVolPct(closes);
  const atr = atrPct(candles);
  const anomalies = detectAnomalies(candles);
  const anomalyScore = anomalies.reduce((a, x) => a + (x.severity === "high" ? 20 : x.severity === "medium" ? 10 : 4), 0);

  let score = 0;
  if (trend === "up") score += 2; else if (trend === "down") score -= 2;
  if (r != null) {
    if (r >= 70) score -= 1;
    else if (r <= 30) score += 1;
  }
  if (momentum != null) score += Math.max(-2, Math.min(2, momentum / 5));
  const bias: "long" | "short" | "flat" = score > 1 ? "long" : score < -1 ? "short" : "flat";

  const reason = last == null
    ? "no stored candles for this instrument"
    : `${trend} EMA structure, RSI ${r == null ? "n/a" : r.toFixed(1)}, 20-bar momentum ${momentum == null ? "n/a" : momentum.toFixed(2)}%`;

  return {
    bias,
    score: Number(score.toFixed(2)),
    last,
    rsi14: r == null ? null : Number(r.toFixed(2)),
    trend,
    momentum_pct: momentum == null ? null : Number(momentum.toFixed(2)),
    vol_ann_pct: vol == null ? null : Number(vol.toFixed(2)),
    atr_pct: atr == null ? null : Number(atr.toFixed(2)),
    anomaly_score: anomalyScore,
    anomalies,
    reason,
    regime: trend,
    regime_description: `${candles.length} daily bars; trend read from EMA(9/21) and realised volatility from log returns.`,
    timeframes: [],
    tf_verdict: "daily series",
  };
}

export function detectAnomalies(candles: Candle[]): Anomaly[] {
  const out: Anomaly[] = [];
  if (candles.length < 30) return out;
  const closes = candles.map((c) => c.close);
  const volumes = candles.map((c) => c.volume);
  const window = closes.slice(-30);
  const mean = window.reduce((a, b) => a + b, 0) / window.length;
  const sd = stdev(window) || 1;
  const z = (closes[closes.length - 1] - mean) / sd;
  if (Math.abs(z) >= 2) {
    out.push({
      kind: "price_z",
      severity: Math.abs(z) >= 3 ? "high" : "medium",
      message: `last close is ${z.toFixed(2)}σ from the 30-bar mean`,
    });
  }
  const vMean = volumes.slice(-30).reduce((a, b) => a + b, 0) / 30;
  const vSd = stdev(volumes.slice(-30)) || 1;
  const vz = (volumes[volumes.length - 1] - vMean) / vSd;
  if (vz >= 2) {
    out.push({ kind: "volume_z", severity: vz >= 3 ? "high" : "medium", message: `volume ${vz.toFixed(2)}σ above its 30-bar mean` });
  }
  const last = candles[candles.length - 1];
  const prev = candles[candles.length - 2];
  if (prev && last.high < prev.low) {
    out.push({ kind: "gap_down", severity: "medium", message: "bar gapped below the prior low" });
  }
  if (prev && last.low > prev.high) {
    out.push({ kind: "gap_up", severity: "medium", message: "bar gapped above the prior high" });
  }
  return out;
}

export function scoreSeries(candles: Candle[]): { closes: number[]; last: number | null; change_pct: number | null; volume: number } {
  const closes = candles.map((c) => c.close);
  const last = closes.length ? closes[closes.length - 1] : null;
  const first = closes.length ? closes[0] : null;
  return {
    closes: closes.slice(-30),
    last,
    change_pct: first && last ? Number((((last - first) / first) * 100).toFixed(2)) : null,
    volume: candles.reduce((a, c) => a + (c.volume || 0), 0),
  };
}

export type Signal = {
  signal: "buy" | "sell" | "flat";
  reason: string;
  last: number | null;
  bars: number;
  source: string;
  stub?: boolean;
};

/** Strategy preview on stored candles. Unimplemented methods say so. */
export function signalFor(strategy: string, candles: Candle[], params: Record<string, unknown> = {}): Signal {
  const closes = candles.map((c) => c.close);
  const last = closes.length ? closes[closes.length - 1] : null;
  const base = { last, bars: candles.length, source: "engine" };
  if (!closes.length) return { ...base, signal: "flat", reason: "no stored candles" };

  if (strategy === "ema_trend" || strategy === "dual_ema" || strategy === "ema_trend_cot") {
    const fastPeriod = Number(params.fast ?? (strategy === "dual_ema" ? 12 : 9));
    const slowPeriod = Number(params.slow ?? (strategy === "dual_ema" ? 26 : 21));
    if (closes.length < slowPeriod + 1) return { ...base, signal: "flat", reason: `need ${slowPeriod + 1} bars` };
    const f = ema(closes, fastPeriod);
    const s = ema(closes, slowPeriod);
    const fv = f[f.length - 1]!;
    const sv = s[s.length - 1]!;
    return {
      ...base,
      signal: fv > sv ? "buy" : fv < sv ? "sell" : "flat",
      reason: `EMA${fastPeriod} ${fv.toFixed(2)} ${fv >= sv ? "above" : "below"} EMA${slowPeriod} ${sv.toFixed(2)}` + (strategy === "ema_trend_cot" ? "; COT gate unavailable in this deployment" : ""),
    };
  }
  if (strategy === "rsi_fade") {
    const period = Number(params.period ?? 14);
    const overbought = Number(params.overbought ?? 70);
    const oversold = Number(params.oversold ?? 30);
    const r = rsi(closes, period);
    if (r == null) return { ...base, signal: "flat", reason: `need more than ${period} bars` };
    return {
      ...base,
      signal: r <= oversold ? "buy" : r >= overbought ? "sell" : "flat",
      reason: `RSI(${period}) ${r.toFixed(1)}; bands ${oversold}/${overbought}`,
    };
  }
  if (strategy === "mean_reversion") {
    const period = Number(params.period ?? 20);
    const entryZ = Number(params.entry_z ?? 2);
    if (closes.length < period) return { ...base, signal: "flat", reason: `need ${period} bars` };
    const w = closes.slice(-period);
    const mean = w.reduce((a, b) => a + b, 0) / w.length;
    const sd = stdev(w) || 1;
    const z = (last! - mean) / sd;
    return {
      ...base,
      signal: z <= -entryZ ? "buy" : z >= entryZ ? "sell" : "flat",
      reason: `z-score ${z.toFixed(2)} vs ${period}-bar mean ${mean.toFixed(2)}`,
    };
  }
  if (strategy === "vwap_pullback") {
    const lookback = Number(params.lookback ?? 20);
    const devBps = Number(params.dev_bps ?? 10);
    if (candles.length < lookback) return { ...base, signal: "flat", reason: `need ${lookback} bars` };
    const w = candles.slice(-lookback);
    let pv = 0, vv = 0;
    for (const c of w) {
      const typical = (c.high + c.low + c.close) / 3;
      pv += typical * (c.volume || 0);
      vv += c.volume || 0;
    }
    const vwap = vv > 0 ? pv / vv : closes[closes.length - 1];
    const dev = vwap * (devBps / 10_000);
    const px = closes[closes.length - 1];
    return {
      ...base,
      signal: px <= vwap - dev ? "buy" : px >= vwap + dev ? "sell" : "flat",
      reason: `last ${px.toFixed(2)} vs VWAP ${vwap.toFixed(2)} (±${devBps}bp band)`,
    };
  }
  if (strategy === "vwap_bands") {
    const lookback = Number(params.lookback ?? 20);
    const entrySigma = Number(params.entry_sigma ?? 2);
    if (candles.length < lookback) return { ...base, signal: "flat", reason: `need ${lookback} bars` };
    const w = candles.slice(-lookback);
    let pv = 0, vv = 0;
    for (const c of w) {
      const typical = (c.high + c.low + c.close) / 3;
      pv += typical * (c.volume || 0);
      vv += c.volume || 0;
    }
    const vwap = vv > 0 ? pv / vv : closes[closes.length - 1];
    const sd = stdev(w.map((c) => c.close)) || 1;
    const z = (closes[closes.length - 1] - vwap) / sd;
    return {
      ...base,
      signal: z <= -entrySigma ? "buy" : z >= entrySigma ? "sell" : "flat",
      reason: `VWAP band z ${z.toFixed(2)} (entry ±${entrySigma}σ)`,
    };
  }
  if (strategy === "orb_breakout") {
    const rangeMin = Number(params.range_min ?? 15);
    const look = Math.max(2, Math.min(20, Math.round(rangeMin / 5)));
    if (candles.length < look + 1) return { ...base, signal: "flat", reason: `need ${look + 1} bars` };
    const prior = candles.slice(-look - 1, -1);
    const hi = Math.max(...prior.map((c) => c.high));
    const lo = Math.min(...prior.map((c) => c.low));
    const px = closes[closes.length - 1];
    return {
      ...base,
      signal: px > hi ? "buy" : px < lo ? "sell" : "flat",
      reason: `last ${px.toFixed(2)} vs ${look}-bar range ${lo.toFixed(2)}–${hi.toFixed(2)}`,
    };
  }
  if (strategy === "manual") {
    return { ...base, signal: "flat", reason: "manual strategy: no automatic signal; place orders directly" };
  }
  return { ...base, signal: "flat", reason: "no live logic for this method yet", stub: true };
}
