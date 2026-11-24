/* Verbose pure-canvas market chart. Zero deps.
 * drawCandles(canvas, candles, ema9, ema21, vwap, opts?)
 * candles: [{o,h,l,c,v?,ts?}] (or {open,high,low,close,volume,ts}).
 * overlays: number[]|null. opts: {markers:[{i,side:'buy'|'sell'}],
 *   showVolume:true, title:''}. Backward compatible with 5-arg calls.
 * Features: price grid + right axis, time axis, volume pane, OHLC legend,
 * series legend, last-price tag, trade markers, hover crosshair readout.
 */
"use strict";

function drawCandles(canvas, candles, ema9, ema21, vwap, opts) {
  opts = opts || {};
  var showVol = opts.showVolume !== false;
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var W = canvas.clientWidth || 600, H = canvas.clientHeight || 340;
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  var ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  var C = { bg: "#020507", grid: "#16232c", txt: "#7d93a1", up: "#2bffa3",
            down: "#ff5470", ema9: "#ffeb3b", ema21: "#ff9800", vwap: "#41c7ff" };
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);
  if (!candles || candles.length === 0) {
    ctx.fillStyle = C.txt;
    ctx.font = "13px ui-monospace,monospace";
    ctx.fillText("no data — ingest candles or run seed", 12, 24);
    return;
  }
  function o(c) { return c.o !== undefined ? c.o : c.open; }
  function h(c) { return c.h !== undefined ? c.h : c.high; }
  function l(c) { return c.l !== undefined ? c.l : c.low; }
  function cl(c) { return c.c !== undefined ? c.c : c.close; }
  function v(c) { var x = c.v !== undefined ? c.v : c.volume; return x == null ? 0 : x; }
  function t(c) { return c.ts !== undefined ? c.ts : c.time; }

  var n = candles.length, i;
  var lo = Infinity, hi = -Infinity, vmax = 0;
  for (i = 0; i < n; i++) {
    lo = Math.min(lo, l(candles[i])); hi = Math.max(hi, h(candles[i]));
    vmax = Math.max(vmax, v(candles[i]) || 0);
  }
  [ema9, ema21, vwap].forEach(function (s) {
    if (!s) return;
    for (var j = 0; j < s.length; j++) {
      if (s[j] != null && isFinite(s[j])) { lo = Math.min(lo, s[j]); hi = Math.max(hi, s[j]); }
    }
  });
  if (!(hi > lo)) hi = lo + 1;
  var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;

  var AXW = 58, TAX = 18;                       // right axis / time axis
  var VP = (showVol && vmax > 0) ? Math.floor((H - TAX) * 0.22) : 0;
  var PH = H - TAX - VP - (VP ? 6 : 0);         // price pane height
  function y(p) { return PH - 6 - ((p - lo) / (hi - lo)) * (PH - 12); }
  
  var step = (W - AXW) / n, bw = Math.max(1, step * 0.62);
  function x(i) { return i * step + step / 2; }
  function fmt(p) {
    if (p == null || !isFinite(p)) return "—";
    var a = Math.abs(p);
    if (a >= 1000) return p.toFixed(1);
    if (a >= 100) return p.toFixed(2);
    if (a >= 1) return p.toFixed(3);
    return p.toFixed(5);
  }
  function fmtt(ts) {
    if (ts == null) return "";
    var d = new Date(ts * 1000);
    function p2(z) { return (z < 10 ? "0" : "") + z; }
    return p2(d.getHours()) + ":" + p2(d.getMinutes());
  }

  // grid + right axis
  ctx.font = "10px ui-monospace,monospace";
  ctx.lineWidth = 1;
  for (var g = 0; g <= 5; g++) {
    var gp = lo + (hi - lo) * g / 5, gy = y(gp);
    ctx.strokeStyle = C.grid;
    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W - AXW, gy); ctx.stroke();
    ctx.fillStyle = C.txt;
    ctx.fillText(fmt(gp), W - AXW + 5, gy + 3);
  }
  // time axis
  var every = Math.max(1, Math.floor(n / 5));
  ctx.fillStyle = C.txt;
  for (var ti = 0; ti < n; ti += every) {
    var ts = t(candles[ti]);
    if (ts != null) ctx.fillText(fmtt(ts), Math.min(x(ti), W - AXW - 34), H - 5);
  }

  // volume bars
  if (VP) {
    for (i = 0; i < n; i++) {
      var vv = v(candles[i]) || 0;
      if (vv <= 0) continue;
      ctx.fillStyle = cl(candles[i]) >= o(candles[i]) ?
        "rgba(43,255,163,0.35)" : "rgba(255,84,112,0.35)";
      var bh = Math.max(1, (vv / vmax) * VP);
      ctx.fillRect(x(i) - bw / 2, H - TAX - bh, bw, bh);
    }
    ctx.fillStyle = C.txt;
    ctx.fillText("VOL " + fmt(vmax), 4, H - TAX - VP + 10);
  }

  // candles
  for (i = 0; i < n; i++) {
    var c = candles[i], xx = x(i);
    var up = cl(c) >= o(c);
    ctx.strokeStyle = up ? C.up : C.down;
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(xx, y(h(c))); ctx.lineTo(xx, y(l(c))); ctx.stroke();
    var yO = y(o(c)), yC = y(cl(c));
    ctx.fillRect(xx - bw / 2, Math.min(yO, yC), bw, Math.max(1, Math.abs(yC - yO)));
  }
  function line(series, color) {
    if (!series) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.beginPath();
    var started = false;
    for (var k = 0; k < series.length && k < n; k++) {
      var sv = series[k];
      if (sv == null || !isFinite(sv)) { started = false; continue; }
      if (!started) { ctx.moveTo(x(k), y(sv)); started = true; }
      else ctx.lineTo(x(k), y(sv));
    }
    ctx.stroke(); ctx.lineWidth = 1;
  }
  line(ema9, C.ema9); line(ema21, C.ema21); line(vwap, C.vwap);
  (opts.bands || []).forEach(function (b) {
    if (b && b.data) line(b.data, b.color || "#8b949e");
  });

  // trade markers
  (opts.markers || []).forEach(function (m) {
    if (!m || m.i == null || m.i < 0 || m.i >= n) return;
    var buy = String(m.side || "").toLowerCase() === "buy";
    var mx = x(m.i), my = buy ? y(l(candles[m.i])) + 9 : y(h(candles[m.i])) - 9;
    ctx.fillStyle = buy ? C.up : C.down;
    ctx.beginPath();
    if (buy) { ctx.moveTo(mx, my - 6); ctx.lineTo(mx - 5, my + 3); ctx.lineTo(mx + 5, my + 3); }
    else { ctx.moveTo(mx, my + 6); ctx.lineTo(mx - 5, my - 3); ctx.lineTo(mx + 5, my - 3); }
    ctx.closePath(); ctx.fill();
  });

  // -- position overlay -------------------------------------------------
  //
  // An entry line at the average price, shaded above it green and below it
  // red: a long is in profit above the line, a short below. The colour is
  // the *direction of the position*, not of the move, so a profitable short
  // reads as the position it is rather than as a falling market.
  //
  // The shade is scaled to the visible range, not to the P&L, so it stays
  // legible whether the position is 0.1% or 40% from entry.
  var pos = opts.position;
  if (pos && pos.avg_price > 0 && lo < pos.avg_price && pos.avg_price < hi) {
    var py = y(pos.avg_price);
    var long = pos.qty >= 0;
    ctx.save();
    ctx.fillStyle = long ? "rgba(43,255,163,0.07)"
                         : "rgba(255,84,112,0.07)";
    if (long) ctx.fillRect(0, 6, W - AXW, py - 6);
    else ctx.fillRect(0, py, W - AXW, PH - py);
    ctx.restore();

    ctx.save();
    ctx.setLineDash([5, 3]);
    ctx.strokeStyle = long ? "rgba(43,255,163,0.75)"
                           : "rgba(255,84,112,0.75)";
    ctx.beginPath();
    ctx.moveTo(0, py);
    ctx.lineTo(W - AXW, py);
    ctx.stroke();
    ctx.restore();

    var lbl = (long ? "LONG " : "SHORT ") + fmt(pos.avg_price);
    ctx.font = "11px ui-monospace,monospace";
    var tw = ctx.measureText(lbl).width + 6;
    ctx.fillStyle = "#0a1116";
    ctx.fillRect(W - AXW + 1, py - 8, Math.min(tw, AXW - 2), 15);
    ctx.strokeStyle = long ? C.up : C.down;
    ctx.strokeRect(W - AXW + 1, py - 8, Math.min(tw, AXW - 2), 15);
    ctx.fillStyle = long ? C.up : C.down;
    ctx.fillText(lbl, W - AXW + 4, py + 3);
  }

  // -- volatility regime strip ------------------------------------------
  //
  // A band along the bottom of the price pane: red where the EWMA/stdev
  // ratio is in this instrument's top 5% (expansion), blue where it is in
  // the bottom 5% (compression), empty elsewhere. The cut is *estimated per
  // instrument*, so it is not comparable to a fixed number -- the caption
  // reports which basis produced it.
  //
  // save/restore, because this sets globalAlpha and must not leak it into
  // anything drawn afterwards.
  var reg = opts.regime;
  if (reg && reg.series && reg.series.length) {
    var STRIP = 9, sy = PH + 2;
    ctx.save();
    ctx.globalAlpha = 0.5;
    for (var rb = 0; rb < n; rb++) {
      var st = (reg.series[rb] || {}).state;
      if (!st) continue;
      ctx.fillStyle = st === "expansion" ? "#ff5470" : "#41c7ff";
      ctx.fillRect(x(rb) - step / 2, sy, Math.max(1, step), STRIP);
    }
    ctx.restore();
  }

  // -- anomaly flags -----------------------------------------------------
  //
  // A tick above the bar that tripped the detector, so the operator sees
  // *where* rather than only being told something fired. Colour is
  // severity, not direction: a spike up and a spike down are both events.
  if (reg && reg.series) {
    for (var an = 0; an < n; an++) {
      var pt = reg.series[an] || {};
      if (!pt.anomaly) continue;
      ctx.fillStyle = pt.severity === "high" ? "#ff5470" : "#ffeb3b";
      ctx.fillRect(x(an) - 1, y(h(candles[an])) - 13, 2, 7);
    }
  }

  // last-price tag
  var last = cl(candles[n - 1]), ly = y(last);
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = "rgba(125,147,161,0.6)";
  ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(W - AXW, ly); ctx.stroke();
  ctx.setLineDash([]);
  var tag = fmt(last);
  ctx.fillStyle = "#0a1116";
  ctx.fillRect(W - AXW + 1, ly - 8, AXW - 2, 15);
  ctx.strokeStyle = C.txt; ctx.strokeRect(W - AXW + 1, ly - 8, AXW - 2, 15);
  ctx.fillStyle = last >= o(candles[n - 1]) ? C.up : C.down;
  ctx.fillText(tag, W - AXW + 5, ly + 3);

  // OHLC + series legend
  var lc = candles[n - 1];
  var leg = "O " + fmt(o(lc)) + "  H " + fmt(h(lc)) + "  L " + fmt(l(lc)) +
    "  C " + fmt(last) + "  n=" + n;
  if (opts.title) leg = opts.title + "   " + leg;
  ctx.fillStyle = C.txt;
  ctx.fillText(leg, 6, 12);
  var lx = 6, ly2 = 26;
  [["EMA9", C.ema9, ema9], ["EMA21", C.ema21, ema21], ["VWAP", C.vwap, vwap]].forEach(function (s) {
    if (!s[2]) return;
    ctx.fillStyle = s[1];
    ctx.fillRect(lx, ly2 - 7, 14, 3);
    ctx.fillStyle = C.txt;
    ctx.fillText(s[0], lx + 17, ly2);
    lx += 17 + ctx.measureText(s[0]).width + 12;
  });

  // hover crosshair readout (attached once per canvas)
  canvas._render = { candles: candles, x: x, y: y, n: n, AXW: AXW, o: o, h: h, l: l, cl: cl, t: t, fmtt: fmtt, fmt: fmt };
  if (!canvas._xhair && typeof window !== "undefined") {
    canvas._xhair = true;
    canvas.addEventListener("mousemove", function (ev) {
      var R = canvas._render;
      if (!R) return;
      var r = canvas.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      drawCandles(canvas, R.candles, ema9, ema21, vwap, opts);
      var c2 = canvas.getContext("2d");
      var step2 = (canvas.clientWidth - R.AXW) / R.n;
      var idx = Math.max(0, Math.min(R.n - 1, Math.floor(mx / step2)));
      var bx = idx * step2 + step2 / 2;
      c2.strokeStyle = "rgba(65,199,255,0.5)";
      c2.setLineDash([3, 3]);
      c2.beginPath(); c2.moveTo(bx, 0); c2.lineTo(bx, canvas.clientHeight); c2.stroke();
      c2.beginPath(); c2.moveTo(0, my); c2.lineTo(canvas.clientWidth, my); c2.stroke();
      c2.setLineDash([]);
      var b = R.candles[idx];
      var label = "i=" + idx + " O=" + R.fmt(R.o(b)) + " H=" + R.fmt(R.h(b)) +
        " L=" + R.fmt(R.l(b)) + " C=" + R.fmt(R.cl(b)) +
        (R.t(b) != null ? " " + R.fmtt(R.t(b)) : "");
      c2.font = "11px ui-monospace,monospace";
      var tw = c2.measureText(label).width + 12;
      var tx = Math.min(bx + 10, canvas.clientWidth - tw - 4);
      c2.fillStyle = "rgba(10,17,22,0.92)";
      c2.fillRect(tx, 30, tw, 18);
      c2.strokeStyle = "#2c4252"; c2.strokeRect(tx, 30, tw, 18);
      c2.fillStyle = "#d7e3ea";
      c2.fillText(label, tx + 6, 43);
    });
    canvas.addEventListener("mouseleave", function () {
      var R = canvas._render;
      if (R) drawCandles(canvas, R.candles, ema9, ema21, vwap, opts);
    });
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = { drawCandles: drawCandles, drawRSI: drawRSI };

/* RSI(14) sub-pane. Zero deps. drawRSI(canvas, candles) — same candle shape. */
function drawRSI(canvas, candles) {
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var W = canvas.clientWidth || 600, H = canvas.clientHeight || 110;
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  var ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#020507";
  ctx.fillRect(0, 0, W, H);
  ctx.font = "10px ui-monospace,monospace";
  function cl(c) { return c.c !== undefined ? c.c : c.close; }
  if (!candles || candles.length < 15) {
    ctx.fillStyle = "#7d93a1";
    ctx.fillText("RSI needs 15+ candles", 12, 20);
    return;
  }
  var closes = candles.map(cl), P = 14, rsis = [];
  var g = 0, l = 0, i;
  for (i = 1; i <= P; i++) {
    var d = closes[i] - closes[i - 1];
    if (d > 0) g += d; else l -= d;
  }
  g /= P; l /= P;
  for (i = 0; i < closes.length; i++) rsis.push(null);
  rsis[P] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
  for (i = P + 1; i < closes.length; i++) {
    var dd = closes[i] - closes[i - 1];
    g = (g * (P - 1) + Math.max(dd, 0)) / P;
    l = (l * (P - 1) + Math.max(-dd, 0)) / P;
    rsis[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
  }
  function y(v) { return H - 6 - (v / 100) * (H - 12); }
  [[70, "#ff5470"], [50, "#16232c"], [30, "#2bffa3"]].forEach(function (ln) {
    ctx.strokeStyle = ln[1];
    ctx.setLineDash(ln[0] === 50 ? [2, 3] : []);
    ctx.beginPath(); ctx.moveTo(0, y(ln[0])); ctx.lineTo(W, y(ln[0])); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#7d93a1";
    ctx.fillText(String(ln[0]), 4, y(ln[0]) - 3);
  });
  ctx.strokeStyle = "#41c7ff"; ctx.lineWidth = 1.3; ctx.beginPath();
  var started = false, n = closes.length, step = W / n;
  for (i = 0; i < n; i++) {
    if (rsis[i] == null) continue;
    var xx = i * step + step / 2, yy = y(rsis[i]);
    if (!started) { ctx.moveTo(xx, yy); started = true; }
    else ctx.lineTo(xx, yy);
  }
  ctx.stroke(); ctx.lineWidth = 1;
  var last = rsis[n - 1];
  ctx.fillStyle = "#d7e3ea";
  ctx.fillText("RSI(14) " + (last == null ? "—" : last.toFixed(1)), 60, 12);
}

/* ---- auxiliary panels: volume, MACD, ATR, drawdown ---- */
function _panelSetup(canvas, h) {
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var W = canvas.clientWidth || 600;
  var H = canvas.clientHeight || h || 90;
  canvas.width = W * dpr; canvas.height = H * dpr;
  var ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#020507"; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "#1d2b36"; ctx.strokeRect(0.5, 0.5, W - 1, H - 1);
  return { ctx: ctx, W: W, H: H };
}

function drawVolume(canvas, candles) {
  var s = _panelSetup(canvas, 90), ctx = s.ctx, W = s.W, H = s.H;
  var max = 0;
  candles.forEach(function (c) { var v = c.v != null ? c.v : (c.volume || 0); if (v > max) max = v; });
  if (!max) { ctx.fillStyle = "#7d93a1"; ctx.fillText("no volume", 8, 16); return; }
  var n = candles.length, step = W / n;
  candles.forEach(function (c, i) {
    var v = c.v != null ? c.v : (c.volume || 0);
    var up = (c.c != null ? c.c : c.close) >= (c.o != null ? c.o : c.open);
    var bh = (v / max) * (H - 18);
    ctx.fillStyle = up ? "rgba(34,197,94,0.55)" : "rgba(239,68,68,0.55)";
    ctx.fillRect(i * step + 0.5, H - bh - 2, Math.max(1, step - 1), bh);
  });
  ctx.fillStyle = "#7d93a1"; ctx.font = "10px ui-monospace,monospace";
  ctx.fillText("VOLUME · max " + Math.round(max).toLocaleString(), 6, 12);
}

function drawMACD(canvas, closes) {
  var s = _panelSetup(canvas, 100), ctx = s.ctx, W = s.W, H = s.H;
  function ema(vals, p) {
    var k = 2 / (p + 1), out = [], prev = null;
    vals.forEach(function (v, i) {
      prev = prev == null ? v : v * k + prev * (1 - k);
      out.push(i + 1 >= p ? prev : null);
    });
    return out;
  }
  var e12 = ema(closes, 12), e26 = ema(closes, 26);
  var macd = closes.map(function (_, i) { return e12[i] == null || e26[i] == null ? null : e12[i] - e26[i]; });
  var valid = macd.filter(function (v) { return v != null; });
  var sig = ema(valid, 9);
  var off = macd.length - valid.length;
  var signal = macd.map(function (_, i) { return i < off ? null : sig[i - off]; });
  var hist = macd.map(function (v, i) { return v == null || signal[i] == null ? null : v - signal[i]; });
  var all = macd.concat(signal).concat(hist).filter(function (v) { return v != null; });
  var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  if (!isFinite(lo) || lo === hi) { ctx.fillStyle = "#7d93a1"; ctx.fillText("MACD warming up", 8, 16); return; }
  var pad = (hi - lo) * 0.1 || 1;
  lo -= pad; hi += pad;
  function y(v) { return H - 14 - ((v - lo) / (hi - lo)) * (H - 24); }
  var n = closes.length, step = W / n;
  ctx.strokeStyle = "#2c4252"; ctx.beginPath(); ctx.moveTo(0, y(0)); ctx.lineTo(W, y(0)); ctx.stroke();
  hist.forEach(function (v, i) {
    if (v == null) return;
    ctx.fillStyle = v >= 0 ? "rgba(34,197,94,0.45)" : "rgba(239,68,68,0.45)";
    var y0 = y(0), y1 = y(v);
    ctx.fillRect(i * step + 0.5, Math.min(y0, y1), Math.max(1, step - 1), Math.abs(y1 - y0));
  });
  function line(data, color) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    var started = false;
    data.forEach(function (v, i) {
      if (v == null) return;
      var x = i * step + step / 2;
      if (!started) { ctx.moveTo(x, y(v)); started = true; } else ctx.lineTo(x, y(v));
    });
    ctx.stroke(); ctx.lineWidth = 1;
  }
  line(macd, "#41c7ff"); line(signal, "#ffb224");
  ctx.fillStyle = "#7d93a1"; ctx.font = "10px ui-monospace,monospace";
  ctx.fillText("MACD(12,26,9) · cyan MACD · amber signal", 6, 12);
}

function drawATR(canvas, candles, period) {
  period = period || 14;
  var s = _panelSetup(canvas, 90), ctx = s.ctx, W = s.W, H = s.H;
  if (candles.length < period + 1) { ctx.fillStyle = "#7d93a1"; ctx.fillText("ATR warming up", 8, 16); return; }
  var trs = [];
  for (var i = 1; i < candles.length; i++) {
    var c = candles[i], h = c.h != null ? c.h : c.high, l = c.l != null ? c.l : c.low;
    var pc = candles[i - 1].c != null ? candles[i - 1].c : candles[i - 1].close;
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
  }
  var atr = [], sum = 0;
  for (i = 0; i < trs.length; i++) {
    sum += trs[i];
    if (i >= period) sum -= trs[i - period];
    atr.push(i + 1 >= period ? sum / period : null);
  }
  var vals = atr.filter(function (v) { return v != null; });
  var hi = Math.max.apply(null, vals) || 1, lo = Math.min.apply(null, vals) || 0;
  var pad = (hi - lo) * 0.15 || 1; hi += pad; lo = Math.max(0, lo - pad);
  function y(v) { return H - 14 - ((v - lo) / (hi - lo || 1)) * (H - 26); }
  var n = atr.length, step = W / n;
  ctx.strokeStyle = "#ffb224"; ctx.lineWidth = 1.2; ctx.beginPath();
  var started = false;
  atr.forEach(function (v, i) {
    if (v == null) return;
    var x = i * step + step / 2;
    if (!started) { ctx.moveTo(x, y(v)); started = true; } else ctx.lineTo(x, y(v));
  });
  ctx.stroke(); ctx.lineWidth = 1;
  var last = atr[atr.length - 1];
  ctx.fillStyle = "#7d93a1"; ctx.font = "10px ui-monospace,monospace";
  ctx.fillText("ATR(" + period + ") " + (last == null ? "—" : last.toFixed(4)), 6, 12);
}

function drawDrawdown(canvas, closes) {
  var s = _panelSetup(canvas, 90), ctx = s.ctx, W = s.W, H = s.H;
  var peak = -Infinity, dd = [];
  closes.forEach(function (c) {
    if (c > peak) peak = c;
    dd.push(peak > 0 ? ((c - peak) / peak) * 100 : 0);
  });
  var min = Math.min.apply(null, dd);
  if (!isFinite(min) || min === 0) { ctx.fillStyle = "#7d93a1"; ctx.fillText("no drawdown", 8, 16); return; }
  var n = dd.length, step = W / n;
  function y(v) { return 14 + (v / min) * (H - 28); }
  ctx.beginPath(); ctx.moveTo(0, 14);
  dd.forEach(function (v, i) { ctx.lineTo(i * step + step / 2, y(v)); });
  ctx.lineTo(W, 14); ctx.closePath();
  ctx.fillStyle = "rgba(239,68,68,0.22)"; ctx.fill();
  ctx.strokeStyle = "#ff5c7a"; ctx.lineWidth = 1.2; ctx.beginPath();
  dd.forEach(function (v, i) { var x = i * step + step / 2; if (i === 0) ctx.moveTo(x, y(v)); else ctx.lineTo(x, y(v)); });
  ctx.stroke(); ctx.lineWidth = 1;
  ctx.fillStyle = "#7d93a1"; ctx.font = "10px ui-monospace,monospace";
  ctx.fillText("DRAWDOWN · max " + min.toFixed(2) + "%", 6, 12);
}
