/* Shared zero-deps widgets: sparklines + heatmap treemap. */
"use strict";

function sparkline(canvas, closes, color) {
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var w = canvas.clientWidth || 90, h = canvas.clientHeight || 26;
  canvas.width = w * dpr; canvas.height = h * dpr;
  var ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  var vals = (closes || []).filter(function (v) { return v != null && isFinite(v); });
  if (vals.length < 2) return;
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (!(hi > lo)) hi = lo + 1;
  var up = vals[vals.length - 1] >= vals[0];
  ctx.strokeStyle = color || (up ? "#2bffa3" : "#ff5470");
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  vals.forEach(function (v, i) {
    var x = i / (vals.length - 1) * (w - 2) + 1;
    var y = h - 2 - ((v - lo) / (hi - lo)) * (h - 4);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.lineTo(w - 1, h); ctx.lineTo(1, h); ctx.closePath();
  ctx.fillStyle = up ? "rgba(43,255,163,0.12)" : "rgba(255,84,112,0.12)";
  ctx.fill();
}

/* Squarish treemap (sorted slice-and-dice). items: [{label,value,pct}].
 * value sizes tiles, pct colors them. Calls onTile(item) on click. */
function treemap(canvas, items, onTile) {
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var W = canvas.clientWidth || 600, H = canvas.clientHeight || 380;
  canvas.width = W * dpr; canvas.height = H * dpr;
  var ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#020507";
  ctx.fillRect(0, 0, W, H);
  var list = (items || []).filter(function (d) { return d.value > 0; })
    .sort(function (a, b) { return b.value - a.value; });
  if (!list.length) {
    ctx.fillStyle = "#7d93a1";
    ctx.font = "13px ui-monospace,monospace";
    ctx.fillText("no data", 12, 24);
    return;
  }
  var total = list.reduce(function (a, d) { return a + d.value; }, 0);
  function color(p) {
    if (p == null || !isFinite(p)) return "#16232c";
    var a = Math.min(1, Math.abs(p) / 3);
    if (p >= 0) return "rgba(43,255,163," + (0.12 + 0.65 * a).toFixed(2) + ")";
    return "rgba(255,84,112," + (0.12 + 0.65 * a).toFixed(2) + ")";
  }
  var rects = [];
  (function layout(sub, x, y, w, h, horiz) {
    var t = sub.reduce(function (a, d) { return a + d.value; }, 0) || 1;
    sub.forEach(function (d) {
      var f = d.value / t, r;
      if (horiz) { r = { x: x, y: y, w: w * f, h: h }; x += w * f; }
      else { r = { x: x, y: y, w: w, h: h * f }; y += h * f; }
      r.item = d;
      rects.push(r);
    });
  })(list, 0, 0, W, H, W >= H);
  // second pass: split longest axis per group for squarish tiles
  rects = [];
  (function split(sub, x, y, w, h) {
    if (sub.length === 1) { rects.push({ x: x, y: y, w: w, h: h, item: sub[0] }); return; }
    var t = sub.reduce(function (a, d) { return a + d.value; }, 0) || 1;
    var acc = 0, k = 0;
    for (var i = 0; i < sub.length; i++) {
      acc += sub[i].value / t;
      if (acc >= 0.5) { k = i + 1; break; }
    }
    k = Math.max(1, Math.min(sub.length - 1, k || 1));
    var a = sub.slice(0, k), b = sub.slice(k);
    var fa = a.reduce(function (s, d) { return s + d.value; }, 0) / t;
    if (w >= h) {
      split(a, x, y, w * fa, h);
      split(b, x + w * fa, y, w * (1 - fa), h);
    } else {
      split(a, x, y, w, h * fa);
      split(b, x, y + h * fa, w, h * (1 - fa));
    }
  })(list, 0, 0, W, H);
  ctx.font = "11px ui-monospace,monospace";
  rects.forEach(function (r) {
    if (r.w < 2 || r.h < 2) return;
    ctx.fillStyle = color(r.item.pct);
    ctx.fillRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
    ctx.strokeStyle = "#04070a";
    ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
    if (r.w > 44 && r.h > 30) {
      ctx.fillStyle = "#e8f2f7";
      ctx.fillText(String(r.item.label).slice(0, 10), r.x + 5, r.y + 15);
      if (r.item.pct != null && isFinite(r.item.pct) && r.h > 44) {
        var s = (r.item.pct >= 0 ? "+" : "") + r.item.pct.toFixed(2) + "%";
        ctx.fillStyle = r.item.pct >= 0 ? "#06351f" : "#4d0f1c";
        ctx.fillText(s, r.x + 5, r.y + 29);
      }
    }
  });
  if (onTile) {
    canvas.onclick = function (ev) {
      var b = canvas.getBoundingClientRect();
      var mx = ev.clientX - b.left, my = ev.clientY - b.top;
      for (var i = 0; i < rects.length; i++) {
        var r = rects[i];
        if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) {
          onTile(r.item);
          break;
        }
      }
    };
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { sparkline: sparkline, treemap: treemap };
}
