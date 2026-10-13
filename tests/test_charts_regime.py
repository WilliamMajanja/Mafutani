"""web/charts.js regime strip + anomaly flags. Node, no browser.

The overlay code touches canvas state -- `globalAlpha`, transforms,
clipping -- so the failure mode is not an exception but a *quietly wrong*
chart: an alpha left at 0.5 makes everything after it translucent, and a
missing save/restore corrupts the next redraw. Neither shows up in a
syntax check.

So this drives the real `drawCandles` against a recording stub that
implements enough of the 2D context to catch state leaks, and asserts on
what was actually drawn rather than on whether it returned.
"""
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVER = r"""
// Records canvas calls so the test can assert on drawing, and tracks
// save/restore and globalAlpha so a leak is detectable rather than visual.
const calls = [];
let depth = 0;
let alpha = 1;
const stack = [];

function mkctx() {
  const c = {
    canvas: {
      width: 900, height: 400, clientWidth: 900, clientHeight: 400,
      // drawCandles fetches its own context; the stub must supply one.
      getContext(kind) { if (kind !== '2d') return null; return c; },
      // drawCandles attaches hover/crosshair listeners to the canvas.
      addEventListener() {}, removeEventListener() {},
      setAttribute() {}, getBoundingClientRect() {
        return { left: 0, top: 0, width: 900, height: 400 };
      },
      style: {},
    },
    save() { depth++; stack.push(alpha); calls.push({ op: 'save' }); },
    restore() {
      depth--;
      alpha = stack.pop();
      calls.push({ op: 'restore', alpha: alpha });
    },
    scale() { calls.push({ op: 'scale' }); },
    clearRect() {}, fillRect(...a) {
      calls.push({ op: 'fillRect', a, alpha, fill: this._f });
    },
    strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    closePath() {}, fill() {}, stroke() {}, setLineDash() {},
    fillText() {}, strokeText() {},
    measureText: () => ({ width: 10 }),
    set fillStyle(v) { this._f = v; }, get fillStyle() { return this._f; },
    set strokeStyle(v) { this._s = v; }, get strokeStyle() { return this._s; },
    set lineWidth(v) { this._w = v; }, get lineWidth() { return this._w; },
    set font(v) { this._ft = v; }, get font() { return this._ft; },
    set globalAlpha(v) { alpha = v; }, get globalAlpha() { return alpha; },
  };
  return c;
}

const ctx = mkctx();
global.window = { devicePixelRatio: 1 };
global.document = { getElementById: () => ctx.canvas };
// drawCandles calls canvas.getContext('2d') itself.

const mod = require(process.argv[2]);
const drawCandles = mod.drawCandles;

function candles(n, opts) {
  const out = [];
  let p = 100;
  for (let i = 0; i < n; i++) {
    const bump = (opts && opts.bump && opts.bump.includes(i)) ? 6 : 0;
    p = p * (1 + (i % 7 === 0 ? 0.01 : -0.001)) + bump;
    out.push({ o: p * 0.999, h: p * 1.01, l: p * 0.99, c: p, v: 1000 });
  }
  return out;
}

// ---- case 1: regime + anomalies supplied -------------------------------
const reg = {
  basis: 'estimated', kurtosis: 5.2, cut_z: 3.1, n_threshold: 300,
  regime: { expansion: 1.37, compression: 0.71, basis: 'estimated' },
  series: candles(200, { bump: [40, 41, 90] }).map((c, i) => ({
    ts: i, ret: 0.001, z: 0.1, ratio: 1.0,
    state: i % 9 === 0 ? 'expansion' : (i % 9 === 4 ? 'compression' : null),
    anomaly: [40, 41, 90].includes(i) ? 'price_spike' : null,
    severity: [41].includes(i) ? 'high' : 'medium',
  })),
};
drawCandles(ctx.canvas, candles(200, { bump: [40, 41, 90] }), null, null, null,
            { title: 'X', regime: reg });

const fills = calls.filter(c => c.op === 'fillRect');
const red = fills.filter(c => c.fill === '#ff5470');
const cyan = fills.filter(c => c.fill === '#41c7ff');
const tickRed = red.filter(c => c.a[2] === 2 && c.a[3] === 7);
const yellow = fills.filter(c => c.fill === '#ffeb3b');

// alpha must be back to 1 everywhere after drawCandles returns
const lastRestore = calls.filter(c => c.op === 'restore').pop();

console.log(JSON.stringify({
  depth: depth,
  alphaAfter: alpha,
  regimeRed: red.length,
  regimeBlue: cyan.length,
  tickRed: tickRed.length,
  tickYellow: yellow.length,
  stripYs: [...new Set(red.concat(cyan).map(c => Math.round(c.a[1])))],
}));
"""


def run_driver(case_args=""):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(DRIVER)
        path = f.name
    js = os.path.join(ROOT, "web", "charts.js")
    try:
        p = subprocess.run(["node", path, js], capture_output=True, text=True,
                           timeout=60, cwd=ROOT,
                           env=dict(os.environ, ARGS0=js))
        return p
    finally:
        os.unlink(path)


class TestRegimeOverlay(unittest.TestCase):
    def _drive(self):
        p = run_driver()
        self.assertEqual(p.returncode, 0,
                         "drawCandles threw:\n%s" % (p.stderr[-1500:]))
        line = [l for l in p.stdout.strip().splitlines() if l.startswith("{")]
        self.assertTrue(line, "no JSON from driver:\n%s" % p.stdout)
        return json.loads(line[-1])

    def test_regime_bars_are_drawn(self):
        d = self._drive()
        self.assertGreater(d["regimeRed"], 0, "no expansion bars drawn")
        self.assertGreater(d["regimeBlue"], 0, "no compression bars drawn")

    def test_anomaly_ticks_are_drawn(self):
        """Three flagged bars: two medium (yellow), one high (red)."""
        d = self._drive()
        self.assertEqual(d["tickYellow"], 2,
                         "expected 2 medium-severity ticks")
        self.assertEqual(d["tickRed"], 1,
                         "expected 1 high-severity tick")

    def test_regime_strip_sits_below_the_price_pane(self):
        """It must not be drawn over the candles."""
        d = self._drive()
        self.assertTrue(d["stripYs"], "no strip drawn")
        self.assertGreater(min(d["stripYs"]), 120,
                           "strip is drawn over the price area (y=%s)"
                           % d["stripYs"])

    def test_no_canvas_state_leak(self):
        """globalAlpha and the save/restore stack must be balanced.

        An alpha left at 0.5 makes every subsequent draw translucent and
        looks like a styling bug rather than a state leak, which is why it
        has to be asserted rather than eyeballed.
        """
        d = self._drive()
        self.assertEqual(d["depth"], 0,
                         "unbalanced save/restore: depth %d" % d["depth"])
        self.assertEqual(d["alphaAfter"], 1,
                         "globalAlpha leaked at %s" % d["alphaAfter"])


class TestOverlayIsOptional(unittest.TestCase):
    def test_draws_without_a_regime(self):
        """Backwards compatibility: the older call shapes must still work.

        The overlay is optional. If adding it broke the 5-argument call that
        every other chart in the app still uses, this is where it shows.
        """
        drv = r"""
        const ctx = {
          canvas: { width: 600, height: 300, clientWidth: 600, clientHeight: 300 },
          save() {}, restore() {}, scale() {},
          clearRect() {}, fillRect() {}, strokeRect() {},
          beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
          fill() {}, stroke() {}, setLineDash() {}, fillText() {},
          measureText: () => ({ width: 10 }),
        };
        // Attached after the literal exists. Declaring getContext inside the
        // object literal referenced `ctx` before initialisation, which is a
        // syntax error rather than a runtime one -- so it took three tries
        // to see, and the first two "fixes" looked like working code.
        ctx.canvas.getContext = (k) => (k === '2d' ? ctx : null);
        ctx.canvas.addEventListener = () => {};
        ctx.canvas.removeEventListener = () => {};
        ctx.canvas.setAttribute = () => {};
        ctx.canvas.style = {};
        ctx.canvas.getBoundingClientRect =
          () => ({ left: 0, top: 0, width: 600, height: 300 });

        global.window = { devicePixelRatio: 1 };
        global.document = { getElementById: () => ctx.canvas };

        const mod = require(require('path').resolve(process.argv[2]));
        const drawCandles = mod.drawCandles;

        const c = [];
        for (let i = 0; i < 50; i++) c.push({ o: 100, h: 101, l: 99, c: 100, v: 1 });
        drawCandles(ctx.canvas, c, null, null, null);
        drawCandles(ctx.canvas, c, null, null, null, { title: 'X' });
        drawCandles(ctx.canvas, [], null, null, null, { regime: { series: [] } });
        drawCandles(ctx.canvas, c, null, null, null,
                    { markers: [{ i: 3, side: 'buy' }, { i: 9, side: 'sell' }] });
        console.log(JSON.stringify({ ok: true }));
        """
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(drv)
            path = f.name
        try:
            p = subprocess.run(["node", path, os.path.join(ROOT, "web", "charts.js")],
                               capture_output=True, text=True, cwd=ROOT, timeout=60)
            self.assertEqual(p.returncode, 0,
                             "legacy call shapes now throw:\n%s" % p.stderr[-1200:])
            self.assertIn('{"ok":true}', p.stdout)
        finally:
            os.unlink(path)



class TestSourceContract(unittest.TestCase):
    def test_export_includes_nothing_new_but_stays_valid(self):
        """charts.js exports its functions for the existing tests."""
        with open(os.path.join(ROOT, "web", "charts.js"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("module.exports", src)

    def test_app_js_passes_regime_through(self):
        """The overlay only exists if the page actually sends it."""
        with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("regime: reg", src, "regime never reaches drawCandles")
        self.assertIn("/api/chart_context", src, "context is never fetched")


if __name__ == "__main__":
    unittest.main(verbosity=2)
