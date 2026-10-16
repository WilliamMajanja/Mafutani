/* Mafutani CTA SPA. Zero deps: fetch + EventSource + hash router. */
"use strict";
(function () {
  var csrfToken = null;
  var state = { me: null, quotes: {}, sse: null };

  // Fetch wrapper: JSON in/out, CSRF header on mutating requests.
  function api(method, path, body) {
    var headers = { "Content-Type": "application/json" };
    if (csrfToken) { headers["X-CSRF"] = csrfToken; headers["X-CSRF-Token"] = csrfToken; }
    var opts = { method: method, headers: headers, credentials: "same-origin" };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      var ct = r.headers.get("Content-Type") || "";
      var parse = ct.indexOf("application/json") >= 0 ? r.json() : r.text();
      return parse.then(function (data) {
        if (!r.ok) {
          if (r.status === 401 && path.indexOf("/api/auth/") !== 0 &&
              path !== "/api/me/entitlements") {
            // session expired/revoked mid-use: drop state, route to login once
            state.me = null; csrfToken = null;
            try {
              if ((location.hash || "") !== "#/login") {
                location.hash = "#/login";
                if (typeof router === "function") router();
              }
            } catch (e) { /* never break error path */ }
          }
          var msg = (data && data.error) || r.statusText || ("HTTP " + r.status);
          if (r.status === 401 && path.indexOf("/api/auth/") !== 0) {
            msg += " — please log in again";
          }
          var e = new Error(msg); e.status = r.status; e.data = data;
          throw e;
        }
        return data;
      });
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // Format a number for display.
  //
  // With a flat 2-decimal default, a 0.001 BTC position rendered as
  // "0.00" and the whole portfolio read as empty -- 39 real positions
  // were on screen and every one looked like zero. Quantities span
  // many orders of magnitude (0.0005 ETH to 40,000,000 BTC), so the
  // decimal count has to follow the magnitude rather than be fixed.
  // An explicit `d` still wins, for prices where 2dp is the contract.
  function num(x, d) {
    if (x == null || x === "" || !isFinite(x)) return "—";
    var n = Number(x);
    if (d != null) return n.toFixed(d);
    var a = Math.abs(n);
    if (a === 0) return "0.00";
    if (a < 0.00001) return n.toFixed(8);
    if (a < 0.01) return n.toFixed(6);
    if (a < 1) return n.toFixed(4);
    if (a < 1000) return n.toFixed(2);
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function view() { return document.getElementById("view"); }
  function showErr(e) {
    return '<p class="error">' + esc((e && e.message) || "request failed") + "</p>";
  }

  function refreshMe() {
    return api("GET", "/api/me/entitlements").then(function (me) {
      state.me = me;
      var badge = document.getElementById("tier-badge");
      if (badge) badge.textContent = me && me.tier ? me.tier : "";
      var sbTier = document.getElementById("sb-tier");
      if (sbTier) sbTier.textContent = (me && me.tier ? me.tier : "—").toUpperCase();
      if (me && (me.csrf_token || me.csrf)) csrfToken = me.csrf_token || me.csrf;
      if (me) {
        try {
          if (window.OpenTerminal && window.OpenTerminal.refresh) window.OpenTerminal.refresh();
        } catch (e) { /* tape is decorative */ }
      }
      return me;
    }).catch(function () { state.me = null; return null; });
  }

  // ---- SSE ticker -------------------------------------------------------
  function sseDot(alive) {
    var d = document.getElementById("sb-dot");
    if (d) d.classList.toggle("dead", !alive);
  }
  // flash helper: compare with previous tick, wrap in flash span
  function flashSym(sym, last) {
    var prev = state.quotes[sym];
    var pl = prev ? (prev.last != null ? prev.last : prev.ask) : null;
    if (pl == null || last == null || pl === last) return "";
    return last > pl ? "flash-up" : "flash-down";
  }
  function tickBadge(q) {
    var last = q.last != null ? q.last : q.ask;
    var cls = flashSym(q.symbol, last);
    return "<span class='badge " + cls + "'>" + esc(q.symbol) + " " + num(last) + "</span>";
  }
  function startSSE(onTick) {
    stopSSE();
    try {
      var es = new EventSource("/stream/quotes");
      state.sse = es;
      es.onmessage = function (ev) {
        try {
          var msg = JSON.parse(ev.data);
          if (!msg) return;
          // The server emits a BATCH: {"quotes": [ ...one row per symbol ],
          // "ts": n}. This handler used to guard on `msg.symbol`, which is
          // never present on a batch, so onTick never fired, state.quotes
          // stayed empty, and every live page rendered "no quote" while the
          // stream was in fact delivering ~49 quotes every 5 seconds. The
          // top tape looked fine because it is fed by the terminal's
          // `screener` command, not by this stream -- which is exactly why
          // the failure was invisible.
          //
          // Both shapes are accepted so a single-quote producer and a batch
          // producer cannot break each other.
          var batch = Array.isArray(msg.quotes) ? msg.quotes
                   : (msg.symbol ? [msg] : []);
          if (!batch.length) return;
          for (var i = 0; i < batch.length; i++) {
            var q = batch[i];
            if (!q || !q.symbol) continue;
            var prev = state.quotes[q.symbol] || null;
            state.quotes[q.symbol] = q;
            onTick && onTick(q, tickBadge(q), prev);
          }
          sseDot(true);
          renderTape();
        } catch (e) { /* ignore malformed tick */ }
      };
      es.onerror = function () {
        sseDot(false);
        // Toggle a single marker rather than appending one. Appending on
        // every error grew the ticker without bound while the stream was
        // down: a reconnect fires error again, and a page left open
        // overnight ended up with hundreds of "stale" spans.
        var el = document.getElementById("ticker");
        if (!el) return;
        var mark = el.querySelector(".stale");
        if (mark) mark.remove();
        else {
          var s = document.createElement("span");
          s.className = "stale";
          s.textContent = "stale";
          el.appendChild(s);
        }
      };
    } catch (e) { /* EventSource unsupported */ }
  }
  function stopSSE() {
    if (state.sse) { try { state.sse.close(); } catch (e) {} state.sse = null; }
  }

  // ---- tape -------------------------------------------------------------
  // The top tape is populated from the same quote stream as every other
  // ticker. Without this it sat on "connecting tape…" forever: the CSS
  // animation was running, but on a single static line.
  function renderTape() {
    var track = document.getElementById("tape-track");
    if (!track) return;
    var keys = Object.keys(state.quotes);
    if (!keys.length) return; // keep the connecting message until first tick
    var items = keys.map(function (s) {
      var q = state.quotes[s];
      var last = q.last != null ? q.last : q.ask;
      return "<span class='tape-item'><b>" + esc(s) + "</b> " + num(last) +
        "</span><span class='tape-sep'>///</span>";
    }).join("");
    // The animation translates to -50%, so the track must contain the
    // items twice for the loop to be seamless rather than jumping.
    track.innerHTML = items + items;
  }

  // ---- pages -------------------------------------------------------------
  function pageLogin() {
    // A native (no-JS) form submit redirects back here with the reason in
    // the fragment, so surface it instead of silently re-rendering.
    try {
      var h = (location.hash || "");
      var m = h.match(/[?&]e=([^&]+)/);
      if (m) {
        var el = document.getElementById("login-error");
        if (el) el.textContent = decodeURIComponent(m[1].replace(/\+/g, " "));
        history.replaceState(null, "", "#/login");
      }
    } catch (e) { /* never break the login page */ }
    document.getElementById("login-section").style.display = "";
    view().innerHTML = "<p>Please log in (or sign up) to continue. New users default to Free tier.</p>";
  }
  function hideLogin() {
    document.getElementById("login-section").style.display = "none";
  }

  function pageDashboard() {
    hideLogin();
    view().innerHTML = "<h2>Dashboard</h2><div id='ticker'></div><div id='dash'></div>";
    Promise.all([
      api("GET", "/api/portfolio"),
      api("GET", "/api/orders?limit=5").catch(function () { return { orders: [] }; }),
      api("GET", "/api/risk").catch(function () { return {}; })
    ]).then(function (res) {
      var p = res[0] || {}, poss = p.positions || [], sum = p.summary || {};
      var tot = sum.total_pnl, totCls = tot > 0 ? "up" : (tot < 0 ? "down" : "");
      var cash = p.cash != null ? +p.cash : 0;
      var html = "<div class='cards'>" +
        "<div class='card kpi'><h3>Cash Balance</h3><div class='kpi-v'>" + num(cash) +
        "</div><div class='kpi-l'>" + esc(p.tier || "paper") + " account</div></div>" +
        "<div class='card kpi'><h3>Total P&amp;L</h3><div class='kpi-v " + totCls + "'>" +
        (tot > 0 ? "+" : "") + num(tot) + "</div><div class='kpi-l'>real " + num(sum.realized_pnl) +
        " / unreal " + num(sum.unrealized_pnl) + "</div></div>" +
        "<div class='card kpi'><h3>Realized</h3><div class='kpi-v'>" + num(sum.realized_pnl) +
        "</div><div class='kpi-l'>closed trade P&amp;L</div></div>" +
        "<div class='card kpi'><h3>Open positions</h3><div class='kpi-v'>" + poss.length +
        "</div><div class='kpi-l'>across " + poss.filter(function (x) { return +x.qty !== 0; }).length +
        " live</div></div></div>";
      html += "<h3>Positions</h3>" + positionRows(poss, {
        showRealized: true,
        mode: p.live_trading_enabled ? "LIVE" : "paper",
        empty: "no open positions — the book is flat"
      });
      // Every enabled instrument, not just the ones held. With a flat
      // book the positions table is empty and there was no way to reach
      // the other 38 symbols from here at all.
      html += "<h3>All assets</h3><div id='dash-assets'></div>";
      var ords = res[1].orders || res[1].rows || [];
      html += "<h3>Recent orders</h3><table><tr><th>ID</th><th>Side</th><th>Qty</th><th>Symbol</th><th>Status</th><th>Mode</th></tr>" +
        ords.map(function (o) {
          return "<tr><td>" + esc(o.id) + "</td><td>" + esc(o.side) + "</td><td>" + num(o.qty) +
            "</td><td>" + esc(o.symbol) + "</td><td>" + esc(o.status) + "</td><td>" + esc(o.mode) + "</td></tr>";
        }).join("") + "</table>";
      var lim = res[2] || {};
      html += "<p class='kpi-l'>risk: maxQty " + esc(lim.max_position_qty) + " · maxNotional " +
        esc(lim.max_notional) + " · maxDailyLoss " + esc(lim.max_daily_loss) +
        (lim.kill_switch ? " · <span class='neg'>KILL-SWITCH ON</span>" : "") + "</p>";
      document.getElementById("dash").innerHTML = html;
      mountAssets("dash-assets");
    }).catch(function (e) { document.getElementById("dash").innerHTML = showErr(e); });
    startSSE(function () {
      renderTape();
    });
  }

  // ---- shared render helpers -------------------------------------------
  function fmtTs(ts) {
    if (ts == null || !ts) return "—";
    try { return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " "); }
    catch (e) { return String(ts); }
  }
  // signed qty -> LONG/SHORT/FLAT chip (shorting is fully supported)
  function sideChip(q) {
    var v = +(q || 0);
    if (v > 0) return "<span class='chip ch-long'>LONG</span>";
    if (v < 0) return "<span class='chip ch-short'>SHORT</span>";
    return "<span class='chip ch-flat'>FLAT</span>";
  }
  function renderNews(el, nd) {
    if (!el) return;
    var items = (nd && nd.items) || [];
    if (!items.length) {
      el.innerHTML = "<span class='kpi-l'>no headlines" +
        (nd && nd.stale ? " (feed unreachable — will retry)" : "") + "</span>";
      return;
    }
    el.innerHTML = items.map(function (a) {
      return "<div class='news-item'><a href='" + esc(a.link) + "' target='_blank' rel='noopener'>" +
        esc(a.title) + "</a><div class='news-meta'>" + esc(a.source || "") + " · " +
        esc(fmtTs(a.ts)) + "</div></div>";
    }).join("") + (nd && nd.stale ? "<p class='kpi-l'>feed stale — showing cache</p>" : "");
  }

  function pageMarkets() {
    hideLogin();
    view().innerHTML = "<h2>Global Markets</h2>" +
      "<div class='kpi-l' id='mk-sub'>loading market config…</div>" +
      "<div id='mk-regions'></div><div id='mk-venues'></div>";
    api("GET", "/api/markets").then(function (d) {
      var regions = d.regions || [];
      var venues = d.venues || [];
      document.getElementById("mk-sub").innerHTML =
        "Config: <code>" + esc(d.config_path || "core/markets.py") + "</code> · " +
        "<span class='dim'>" + regions.length + " regions · " + venues.length + " venues tracked · sessions in UTC</span>";
      document.getElementById("mk-regions").innerHTML = regions.map(function (r) {
        var s = r.open_utc, e = r.close_utc;
        function hh(v) {
          var h = Math.floor(v), m = Math.round((v - h) * 60);
          return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
        }
        return "<div class='card'><h4>" + esc(r.name) +
          " <span class='tag'>" + esc(r.code) + "</span> " +
          "<span class='tag " + (r.open ? "pos" : "dim") + "'>" +
          (r.open ? "session open" : "session closed") + "</span></h4>" +
          "<p class='kpi-l'>" + esc(r.tz) + " · " + hh(s) + "–" + hh(e) + " UTC</p>" +
          "<p class='kpi-l'>" + (r.venues_tracked || 0) + " venues tracked · " +
          (r.asset_classes || []).map(esc).join(" · ") + "</p></div>";
      }).join("");
      var byRegion = {};
      venues.forEach(function (v) {
        var k = v.region || "unassigned";
        (byRegion[k] = byRegion[k] || []).push(v);
      });
      document.getElementById("mk-venues").innerHTML = Object.keys(byRegion).sort().map(function (k) {
        return "<h3>" + esc(k) + "</h3><table><tr><th>Venue</th><th>Name</th>" +
          "<th>Assets</th><th>Ccy</th><th>Session</th></tr>" +
          byRegion[k].map(function (v) {
            return "<tr><td><b>" + esc(v.code) + "</b></td><td>" + esc(v.name) + "</td>" +
              "<td>" + (v.asset_classes || []).map(esc).join(", ") + "</td>" +
              "<td>" + esc(v.currency) + "</td><td>" + esc(v.tz) + "</td></tr>";
          }).join("") + "</table>";
      }).join("");
    }).catch(function (e) {
      document.getElementById("mk-sub").textContent = "market config failed: " + e.message;
    });
  }

  // ---- arbitrage --------------------------------------------------------
  //
  // One page, three asset classes, four models. The classes behave
  // differently enough that a single flat table would misrepresent at
  // least one of them:
  //
  //  - cross-venue (crypto only) has two real books, so its edge is
  //    directly executable in principle.
  //  - fx_cross prices a *consistent* identity, so a non-zero result is
  //    usually an artefact rather than money.
  //  - spread (commodity) is a ratio against history, not a locked
  //    relationship -- a wide spread is a regime, not a free lunch.
  //
  // So each row states its own basis, and unpriceable rows render as an
  // explicit "no data" rather than a 0.00 edge, which would read as
  // "we checked and there is nothing".
  function edgeCell(r) {
    var e = r.edge_bps;
    if (e == null) return "<td class='dim'>—</td>";
    var cls = e > 0 ? "pos" : (e < 0 ? "neg" : "dim");
    return "<td class='" + cls + "'>" + (e > 0 ? "+" : "") + num(e, 2) + "</td>";
  }
  function arbLabel(r) {
    if (r.model === "cross_venue") {
      if (!r.sell_venue) {
        return esc(r.symbol) + " <span class='dim'>(no second venue)</span>";
      }
      return esc(r.symbol) + " <span class='dim'>" + esc(r.buy_venue) +
        " → " + esc(r.sell_venue) + "</span>";
    }
    if (r.model === "triangular") {
      return "<span class='dim'>cycle</span> " +
        esc((r.symbols || []).join(" → "));
    }
    if (r.model === "fx_cross") {
      return esc(r.direct_symbol) + " <span class='dim'>vs</span> " +
        esc(r.cross_symbol);
    }
    return esc(r.label || (r.a_symbol + " / " + r.b_symbol));
  }
  function arbBasis(r) {
    var bits = [];
    if (r.gross_bps != null) bits.push("gross " + num(r.gross_bps, 1));
    if (r.cost_bps != null) bits.push("fees " + num(r.cost_bps, 1));
    if (r.z_score != null) bits.push("z " + num(r.z_score, 2) + "σ");
    if (r.profit_factor != null) bits.push("factor " + r.profit_factor);
    if (r.max_size != null) bits.push("size " + num(r.max_size, 4));
    else if (r.notional != null) bits.push("notional " + num(r.notional, 2));
    if (r.direction) bits.push(esc(r.direction));
    return bits.join(" · ");
  }
  function pageArbitrage() {
    hideLogin();
    view().innerHTML = "<h2>Arbitrage</h2>" +
      "<div class='kpi-l' id='arb-sub'>Cross-venue, triangular, FX cross and " +
      "commodity spread — every edge net of two legs of taker fees.</div>" +
      "<div class='row' id='arb-controls'>" +
      "<label>Asset class <select id='arb-class'>" +
      "<option value='all'>All</option><option value='crypto'>Crypto</option>" +
      "<option value='fx'>Currency (FX)</option>" +
      "<option value='commodity'>Commodity</option></select></label>" +
      "<label>Min edge bps <input id='arb-min' type='number' step='0.5' " +
      "placeholder='0' style='width:8em'></label>" +
      "<label>Size cap <input id='arb-size' type='number' step='0.01' " +
      "placeholder='optional' style='width:9em'></label>" +
      "<button id='arb-go' type='button'>Scan</button>" +
      "<button id='arb-auto' type='button'>Auto</button></div>" +
      "<div id='arb-out'></div>";
    var auto = null;
    function load() {
      var cls = document.getElementById("arb-class").value;
      var parts = ["asset_class=" + encodeURIComponent(cls)];
      var minv = document.getElementById("arb-min").value.trim();
      if (minv) parts.push("min_edge_bps=" + encodeURIComponent(minv));
      var sz = document.getElementById("arb-size").value.trim();
      if (sz) parts.push("size=" + encodeURIComponent(sz));
      var out = document.getElementById("arb-out");
      out.innerHTML = "<span class='kpi-l'>scanning venue books…</span>";
      api("GET", "/api/arbitrage?" + parts.join("&"))
        .then(function (d) { render(d); })
        .catch(function (e) {
          if (e && e.status === 403) {
            out.innerHTML = "<div class='card'><h4>Arbitrage is a Pro feature</h4>" +
              "<p class='kpi-l'>The scanner reveals which venues this deployment " +
              "watches and what each is quoting at the same moment. Upgrade to Pro " +
              "to use it.</p></div>";
            return;
          }
          out.innerHTML = showErr(e);
        });
    }
    function render(d) {
      var rows = d.opportunities || [];
      var priced = d.profitable_count || 0;
      var sub = document.getElementById("arb-sub");
      var counts = d.models || {};
      sub.innerHTML = "Scanned " + (d.scanned || 0) + " observations · " +
        (d.priced_count || 0) + " priceable · <b>" + priced +
        "</b> net-positive after fees · " +
        Object.keys(counts).map(function (k) {
          return esc(k) + " " + counts[k];
        }).join(" · ");
      if (!rows.length) {
        document.getElementById("arb-out").innerHTML =
          "<div class='card'><h4>No venues reachable</h4>" +
          "<p class='kpi-l'>The scanner polls the venues listed below directly " +
          "on each scan (cached for 15s), so this means every one of them failed " +
          "or returned nothing — not that no opportunity exists.</p></div>";
        return;
      }
      var order = { cross_venue: 0, triangular: 1, fx_cross: 2, spread: 3 };
      var sorted = rows.slice().sort(function (a, b) {
        var ao = order[a.model], bo = order[b.model];
        if (ao !== bo) return ao - bo;
        return (b.edge_bps || -1e9) - (a.edge_bps || -1e9);
      });
      var body = sorted.map(function (r) {
        return "<tr class='" + (r.edge_bps > 0 ? "" : "clean-row") + "'>" +
          "<td>" + arbLabel(r) + "</td>" +
          "<td><span class='tag'>" + esc(r.model) + "</span></td>" +
          edgeCell(r) + "<td class='dim'>" + arbBasis(r) + "</td>" +
          "<td class='kpi-l'>" + esc(r.reason || "") + "</td></tr>";
      }).join("");
      var attr = d.attribution || {};
      // Shown above the venue table because it qualifies the headline count.
      // A "2 net-positive" that is really one venue quoting oddly is the same
      // bug as the count, with a summary on top -- see README on
      // Flowerday et al. (2026).
      var attrBlock = "";
      if (attr.priced) {
        attrBlock = "<div class='card" +
          (attr.fragile ? " danger-card" : "") + "'><h4>Venue attribution</h4>" +
          "<p class='kpi-l'>Each gap recomputed with one venue removed. " +
          "<b>" + attr.fragile + " fragile</b> of " + attr.priced +
          " priced — collapse when a single venue is dropped, so that is one " +
          "venue's book rather than a market dislocation." +
          (attr.untested ? " " + attr.untested + " untested (fewer than 3 venues)." : "") +
          "</p></div>";
      }
      var venues = (d.venues || []).map(function (v) {
        return "<tr><td><b>" + esc(v.venue) + "</b></td><td>" +
          num(v.fee_bps, 1) + "</td><td>" + esc(v.asset_class || "") + "</td>" +
          "<td class='kpi-l'>" + ((v.symbols || []).map(esc).join(" ") ||
            (v.synthetic ? "generated only" : "—")) + "</td></tr>";
      }).join("");
      document.getElementById("arb-out").innerHTML =
        "<table><tr><th>Instrument</th><th>Model</th><th>Edge bps</th>" +
        "<th>Basis</th><th>Why</th></tr>" + body + "</table>" +
        "<div class='card'><h4>Venue cost model</h4>" +
        "<p class='kpi-l'>Every edge above is gross minus the taker fees of " +
        "both legs. A leg priced off a generated book is never reported as an " +
        "executable edge — only as a ratio with its own history.</p>" +
        "<table><tr><th>Venue</th><th>Taker bps</th><th>Class</th><th>Instruments</th></tr>" +
        (venues || "<tr><td colspan='4' class='dim'>none</td></tr>") + "</table>" +
        "</div>" + attrBlock + "<p class='kpi-l'>" + esc(d.disclaimer || "") + "</p>";
    }
    document.getElementById("arb-go").onclick = load;
    // Auto-refresh only while this page is mounted. An interval that
    // survives navigation keeps polling a hidden endpoint, and on a
    // Pro tier that is a real request rate for a page nobody is reading.
    document.getElementById("arb-auto").onclick = function () {
      if (auto) {
        clearInterval(auto); auto = null;
        this.textContent = "Auto"; return;
      }
      this.textContent = "Stop";
      load();
      auto = setInterval(load, 30000);
    };
    window.addEventListener("hashchange", function stop() {
      if (auto) { clearInterval(auto); auto = null; }
      window.removeEventListener("hashchange", stop);
    });
    load();
  }

  function pageSecurity() {
    hideLogin();
    view().innerHTML = "<h2>Security</h2>" +
      "<div id='sec-body' class='kpi-l'>loading…</div>";
    function render(st) {
      document.getElementById("sec-body").innerHTML =
        "<div class='card'><h4>Two-factor authentication</h4>" +
        "<p class='kpi-l'>Status: " +
        (st.enabled ? "<b class='pos'>enabled</b>" : "<b>not enabled</b>") +
        " · " + (st.recovery_left || 0) + " recovery codes remaining</p>" +
        "<div class='row' id='sec-actions'></div></div>" +
        "<div class='card'><h4>Login protection</h4>" +
        "<p class='kpi-l'>Failed logins are throttled per email and per IP with exponential " +
        "lockout; every attempt is written to the audit log. Sessions expire after 24h.</p></div>";
      var act = document.getElementById("sec-actions");
      if (st.enabled) {
        act.innerHTML =
          "<label>Code <input id='sec-code' inputmode='numeric' maxlength='9' style='width:12em'></label>" +
          "<button type='button' id='sec-disable'>Disable 2FA</button>";
        document.getElementById("sec-disable").onclick = function () {
          var c = document.getElementById("sec-code").value.trim();
          api("POST", "/api/auth/2fa/disable", { code: c })
            .then(function () { load(); })
            .catch(function (e) { alert(e.message); });
        };
      } else {
        act.innerHTML = "<button type='button' id='sec-setup'>Set up 2FA</button>";
        document.getElementById("sec-setup").onclick = function () {
          api("POST", "/api/auth/2fa/setup", {}).then(function (d) {
            act.innerHTML =
              "<p class='kpi-l'>Add this secret to your authenticator app, then confirm:</p>" +
              "<p><code id='sec-secret'>" + esc(d.secret) + "</code></p>" +
              "<label>Code <input id='sec-code' inputmode='numeric' maxlength='6' style='width:10em'></label>" +
              "<button type='button' id='sec-enable'>Enable</button>";
            document.getElementById("sec-enable").onclick = function () {
              var c = document.getElementById("sec-code").value.trim();
              api("POST", "/api/auth/2fa/enable", { code: c }).then(function (r) {
                var codes = r.recovery_codes || [];
                act.innerHTML = "<p class='pos'>2FA enabled. Save your recovery codes:</p>" +
                  "<p><code>" + codes.map(esc).join("  ") + "</code></p>";
              }).catch(function (e) { alert(e.message); });
            };
          }).catch(function (e) { alert(e.message); });
        };
      }
    }
    function load() {
      api("GET", "/api/auth/2fa").then(render).catch(function (e) {
        document.getElementById("sec-body").textContent = "failed: " + e.message;
      });
    }
    load();
  }

  function pageNews() {
    hideLogin();
    view().innerHTML = "<h2>News</h2><div class='row'>" +
      "<label>Symbol (blank = market) <input id='n-sym' placeholder='AAPL'></label>" +
      "<button id='n-go' type='button'>Load</button></div><div id='n-out'></div>";
    function load() {
      var s = document.getElementById("n-sym").value.trim();
      var out = document.getElementById("n-out");
      out.innerHTML = "<span class='kpi-l'>loading…</span>";
      var qs = s ? "?symbol=" + encodeURIComponent(s) + "&limit=25" : "?limit=25";
      api("GET", "/api/news" + qs)
        .then(function (nd) { renderNews(out, nd); })
        .catch(function (e) { out.innerHTML = showErr(e); });
    }
    document.getElementById("n-go").onclick = load;
    load();
  }

  // ---- CFTC Commitments of Traders -------------------------------------
  //
  // Positioning, not price. Every screen here leads with the report date
  // and its age: the CFTC publishes on the Friday after the Tuesday it
  // measures, so a number read today describes a market as it stood nine
  // days ago. Rendering a COT figure next to a live quote with no date on
  // it is how a stale weekly snapshot gets read as a live signal.

  // The Analysis-page overlay. It states plainly that this is not a
  // decision input, because it currently is not one: the score, bias and
  // trend on the same page are computed without reading it, and there are
  // tests pinning that. Saying so in the UI keeps the panel honest about
  // what it is rather than implying a weight it does not carry.
  function renderCotOverlay(c) {
    if (!c || typeof c !== "object") return "";
    var head = "<h4>COT positioning" +
      "<span class='cot-tag'>context only</span></h4>";
    if (!c.available) {
      return "<div class='card'>" + head +
        "<p class='kpi-l'>" + esc(c.reason || "not available") + "</p></div>";
    }
    var band = c.band || "—";
    var pct = c.percentile;
    var rows = (c.categories || []).map(function (x) {
      return "<tr><td>" + esc(x.category) + "</td><td>" + esc(x.net) + "</td>" +
        "<td>" + (x.percentile == null ? "—" : num(x.percentile, 1)) + "</td>" +
        "<td>" + (x.cot_index == null ? "—" : num(x.cot_index, 1)) + "</td>" +
        "<td><span class='badge " + cotTone(x.crowding) + "'>" +
        esc(x.crowding || "—") + "</span></td></tr>";
    }).join("");
    return "<div class='card'>" + head +
      "<div class='cot-stale'><b>" + esc(c.age_days) + " days old</b> · " +
      "positions as of " + esc(c.report_date) + " · published " +
      esc(c.report) + "</div>" +
      "<p><span class='badge " + cotTone(band) + "'>" + esc(band) + "</span>" +
      (pct == null ? "" : " " + cotGauge(pct)) + "</p>" +
      "<p class='kpi-l'>net " + esc(c.net) + " · wk chg " +
      esc(c.weekly_change) + " · index " +
      (c.cot_index == null ? "—" : num(c.cot_index, 1)) +
      " · " + esc(c.window_years) + "y window</p>" +
      (rows ? "<table><tr><th>Category</th><th>Net</th><th>Pctile</th>" +
        "<th>Index</th><th>Read</th></tr>" + rows + "</table>" : "") +
      "<p class='kpi-l'>" + esc(c.market || "") +
      " · not a decision input — weekly and lagged, and the score above " +
      "is computed without it</p></div>";
  }

  var COT_REPORTS = [
    ["disagg_comb", "Disaggregated · futures+options"],
    ["disagg_fut",  "Disaggregated · futures only"],
    ["tff_comb",    "Financial (TFF) · futures+options"],
    ["tff_fut",     "Financial (TFF) · futures only"],
    ["legacy_fut",  "Legacy · futures only"],
    ["legacy_comb", "Legacy · futures+options"],
    ["cit",         "Commodity Index Trader supplement"]
  ];
  var cotState = { report: "disagg_comb", contract: null, weeks: 156 };

  function cotReportOptions() {
    return COT_REPORTS.map(function (r) {
      return "<option value='" + esc(r[0]) + "'" +
        (r[0] === cotState.report ? " selected" : "") + ">" +
        esc(r[1]) + "</option>";
    }).join("");
  }
  // Band colouring. Neutral is the *absence* of an extreme, so it renders
  // in the plain text colour rather than as a positive signal -- only the
  // two crowded bands get emphasis.
  function cotTone(band) {
    if (band === "crowded-long") return "tone-long";
    if (band === "crowded-short") return "tone-short";
    if (band === "extended-long") return "tone-long-soft";
    if (band === "extended-short") return "tone-short-soft";
    return "tone-flat";
  }
  function cotNum(x, dp) {
    if (x === null || x === undefined) return "—";
    var n = Number(x);
    return isFinite(n) ? esc(n.toFixed(dp === undefined ? 2 : dp)) : "—";
  }
  // A gauge from 0-100 percentile. Rendered as a bar with the extremes
  // marked, so "where in the range" is legible without reading the number.
  function cotGauge(p) {
    if (p === null || p === undefined) {
      return "<div class='cot-gauge cot-none'><span>no history</span></div>";
    }
    var w = Math.max(0, Math.min(100, Number(p)));
    return "<div class='cot-gauge'><div class='cot-bar'>" +
      "<div class='cot-fill' style='width:" + w + "%'></div></div>" +
      "<span class='cot-pct'>" + cotNum(w, 1) + "</span></div>";
  }
  function cotStaleNote(st) {
    if (!st) return "";
    return "<div class='cot-stale'><b>" + esc(st.age_days) +
      " days old</b> · " + esc(st.note || "") + "</div>";
  }
  function renderCotList(out, d) {
    var rows = d.markets || [];
    if (!rows.length) {
      out.innerHTML = "<p class='dim'>No contracts stored for this report. " +
        "Run <code>python3 scripts/ingest_cot.py weekly</code> first.</p>";
      return;
    }
    var stored = (d.stored || []).map(function (s) {
      return esc(s.report) + ": " + (s.rows || 0).toLocaleString();
    }).join(" · ");
    out.innerHTML = "<p class='kpi-l'>" + rows.length + " contracts · newest " +
      esc(d.newest || "—") + "</p>" +
      (stored ? "<p class='kpi-l'>stored: " + stored + "</p>" : "") +
      "<input id='cot-q' placeholder='filter e.g. gold, WTI, corn…' " +
      "style='max-width:340px;margin:8px 0'>" +
      "<div style='max-height:460px;overflow:auto'><table><tr><th>Market</th>" +
      "<th>Code</th></tr>" + rows.map(function (m) {
        return "<tr class='clickable' data-code='" + esc(m.contract_code) +
          "'><td>" + esc(m.market) + "</td><td>" + esc(m.contract_code) +
          "</td></tr>";
      }).join("") + "</table></div><div id='cot-detail'></div>";
    var q = document.getElementById("cot-q");
    q.oninput = function () {
      var t = q.value.trim().toUpperCase();
      out.querySelectorAll("tr[data-code]").forEach(function (tr) {
        tr.style.display = (!t || tr.textContent.toUpperCase().indexOf(t) >= 0)
          ? "" : "none";
      });
    };
    out.querySelectorAll("tr[data-code]").forEach(function (tr) {
      tr.onclick = function () { loadCotContract(tr.getAttribute("data-code")); };
    });
    if (cotState.contract) loadCotContract(cotState.contract);
  }
  function renderCotDetail(el, a) {
    var cats = a.categories || [];
    var cons = a.consensus;
    var body = cats.map(function (c) {
      return "<tr><td>" + esc(c.category) + "</td>" +
        "<td>" + esc(c.net) + "</td>" +
        "<td>" + cotNum(c.percentile, 1) + "</td>" +
        "<td>" + cotNum(c.cot_index, 1) + "</td>" +
        "<td>" + cotNum(c.zscore, 2) + "</td>" +
        "<td>" + esc(c.weekly_change) + "</td>" +
        "<td>" + cotNum(c.pct_of_open_interest, 2) + "%</td>" +
        "<td><span class='badge " + cotTone(c.crowding) + "'>" +
        esc(c.crowding || "—") + "</span></td>" +
        "<td>" + cotGauge(c.percentile) + "</td></tr>";
    }).join("");
    var header = cons
      ? "<div class='sig sig-verdict " + cotTone(cons.band) + "'>" +
        "<div class='sig-label'>" + esc(cons.band) + "</div>" +
        "<div class='sig-body'><div class='metrics'>" +
        "<div class='kv'><span>consensus percentile</span><b>" +
        cotNum(cons.percentile, 1) + "</b></div>" +
        "<div class='kv'><span>cot index</span><b>" +
        cotNum(cons.cot_index, 1) + "</b></div>" +
        "<div class='kv'><span>z-score</span><b>" +
        cotNum(cons.zscore, 2) + "</b></div>" +
        "<div class='kv'><span>observations</span><b>" +
        esc(cons.observations) + "</b></div>" +
        "</div></div></div>"
      : "<p class='dim'>Not enough history in this report to rank positioning. " +
        "Backfill with <code>scripts/ingest_cot.py history</code>.</p>";
    el.innerHTML =
      "<h3>" + esc(a.market) + "</h3>" +
      "<p class='kpi-l'>" + esc(a.contract_code) + " · " + esc(a.family) +
      " · " + esc(a.basis) + " · " + esc(a.window_years) + "-year window</p>" +
      cotStaleNote(a.staleness) + header +
      (cats.length
        ? "<table><tr><th>Category</th><th>Net</th><th>Pctile</th><th>Index</th>" +
          "<th>Z</th><th>Wk chg</th><th>% OI</th><th>Read</th><th></th></tr>" +
          body + "</table>"
        : "") +
      "<p class='kpi-l'>net = long − short. Percentile and index are against " +
      "this contract's own trailing " + esc(a.window_years) + " years; they are " +
      "not comparable between contracts.</p>" +
      "<div id='cot-hist'></div>";
    // The chart category has to come from the report itself: 'm_money'
    // exists only in the disaggregated family, and hardcoding it drew an
    // empty chart on legacy, TFF and CIT with no error anywhere.
    var chartCat = (cats[0] && cats[0].category) || "";
    if (chartCat) loadCotHistory(a.contract_code, chartCat);
  }
  function loadCotHistory(code, category) {
    var el = document.getElementById("cot-hist");
    if (!el || !category) return;
    api("GET", "/api/cot/history?contract=" + encodeURIComponent(code) +
        "&report=" + encodeURIComponent(cotState.report) +
        "&category=" + encodeURIComponent(category) + "&limit=260")
      .then(function (h) {
      var s = h.series || [];
      if (s.length < 2) { el.innerHTML = ""; return; }
      el.innerHTML = "<h4>" + esc(category) + " net · " + s.length +
        " weeks</h4>" +
        "<canvas class='chart' id='cot-cv'></canvas>";
      try {
        drawCotNet(document.getElementById("cot-cv"), s);
      } catch (e) { /* chart optional */ }
    }).catch(function () { el.innerHTML = ""; });
  }
  // Net position over time. Zero is drawn explicitly because "which side
  // of flat" is the entire content of this chart.
  function drawCotNet(canvas, series) {
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 600, h = 180;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    var vals = series.map(function (p) { return p.net; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (lo === hi) { hi = lo + 1; }
    var pad = 6;
    var padL = pad, padR = pad, padT = pad, padB = pad;
    function X(i) { return padL + (w - padL - padR) * i / (series.length - 1); }
    function Y(v) { return padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo)); }
    if (lo < 0 && hi > 0) {
      ctx.strokeStyle = "#5a6478"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, Y(0)); ctx.lineTo(w - padR, Y(0));
      ctx.stroke();
    }
    ctx.strokeStyle = "#41c7ff"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    series.forEach(function (p, i) {
      if (i === 0) ctx.moveTo(X(i), Y(p.net)); else ctx.lineTo(X(i), Y(p.net));
    });
    ctx.stroke();
    ctx.fillStyle = "#8a93a6"; ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(series[0].date, padL, h - 1);
    var last = series[series.length - 1].date;
    ctx.fillText(last, w - padR - ctx.measureText(last).width, h - 1);
  }
  function loadCotContract(code) {
    cotState.contract = code;
    var el = document.getElementById("cot-detail");
    if (!el) return;
    el.innerHTML = "<p class='dim'>loading " + esc(code) + "…</p>";
    api("GET", "/api/cot/contract?contract=" + encodeURIComponent(code) +
        "&report=" + encodeURIComponent(cotState.report) +
        "&weeks=" + encodeURIComponent(cotState.weeks))
      .then(function (a) { renderCotDetail(el, a); })
      .catch(function (e) { el.innerHTML = showErr(e); });
  }
  function pageCot() {
    hideLogin();
    view().innerHTML = "<h2>Commitments of Traders</h2>" +
      "<p class='kpi-l'>CFTC weekly positioning. Positions held as of each " +
      "Tuesday, published the following Friday — this is not a price feed " +
      "and does not update daily.</p>" +
      "<div class='row'><label>Report <select id='cot-rep'>" +
      cotReportOptions() + "</select></label>" +
      "<label>Window <select id='cot-wk'>" +
      "<option value='52'>1 year</option>" +
      "<option value='104'>2 years</option>" +
      "<option value='156' selected>3 years</option>" +
      "<option value='260'>5 years</option></select></label>" +
      "<button id='cot-go' type='button'>Load</button></div>" +
      "<div id='cot-out'></div>";
    document.getElementById("cot-rep").onchange = function () {
      cotState.report = this.value; cotState.contract = null;
      load();
    };
    document.getElementById("cot-wk").onchange = function () {
      cotState.weeks = parseInt(this.value, 10) || 156;
      if (cotState.contract) loadCotContract(cotState.contract);
    };
    document.getElementById("cot-go").onclick = load;
    load();
    function load() {
      var out = document.getElementById("cot-out");
      out.innerHTML = "<span class='kpi-l'>loading…</span>";
      api("GET", "/api/cot?limit=400&report=" +
          encodeURIComponent(cotState.report))
        .then(function (d) { renderCotList(out, d); })
        .catch(function (e) { out.innerHTML = showErr(e); });
    }
  }

  function pageHeatmap() {
    hideLogin();
    view().innerHTML = "<h2>Heatmap</h2><div class='kpi-l' id='hm-sub'>loading…</div>" +
      "<canvas class='heat' id='hm'></canvas><div id='herr'></div>";
    api("GET", "/api/screener").then(function (d) {
      var list = d.results || d.rows || [];
      var items = list.map(function (r) {
        var c = r.closes || [], pct = null;
        if (c.length > 1 && c[0]) pct = (c[c.length - 1] - c[0]) / c[0] * 100;
        return { label: r.symbol, value: r.volume || 0, pct: pct };
      }).filter(function (x) { return x.value > 0; });
      document.getElementById("hm-sub").textContent = items.length +
        " symbols · tile size = volume · color = session change · click tile for chart";
      try {
        treemap(document.getElementById("hm"), items, function (it) {
          location.hash = "#/chart/" + encodeURIComponent(it.label);
        });
      } catch (e) { document.getElementById("herr").innerHTML = showErr(e); }
    }).catch(function (e) { document.getElementById("herr").innerHTML = showErr(e); });
  }

  function pageScreener() {
    hideLogin();
    view().innerHTML = "<h2>Screener</h2><div class='row'>" +
      "<label>Min vol <input id='f-vol' type='number' value='0'></label>" +
      "<label>Max spread bps <input id='f-spread' type='number' value='100'></label>" +
      "<label>Exchange <select id='f-ex'><option value=''>all</option></select></label>" +
      "<label>Asset <select id='f-asset'><option value=''>all</option>" +
      "<option>crypto</option><option>equity</option><option>fx</option><option>commodity</option></select></label>" +
      "<label>Region <select id='f-region'><option value=''>all</option>" +
      "<option value='AMER'>Americas</option><option value='EMEA'>Europe / Africa</option>" +
      "<option value='APAC'>Asia-Pacific</option></select></label>" +
      "<button id='f-go' type='button'>Run</button></div>" +
      "<div class='kpi-l'>click a column to sort · sparklines = last 30 closes · ◉ = anomaly flagged</div><div id='out'></div>";
    var cache = [], sortKey = "score", sortDir = -1;
    function val(r, k) {
      if (k === "chg") {
        var c = r.closes || [];
        if (c.length > 1 && c[0]) return (c[c.length - 1] - c[0]) / c[0] * 100;
        return null;
      }
      if (k === "spread") return r.spread_bps != null ? r.spread_bps : r.spread;
      if (k === "rsi") return r.rsi14;
      if (k === "trend") return { up: 1, down: -1, flat: 0 }[r.trend] != null ? { up: 1, down: -1, flat: 0 }[r.trend] : 0;
      if (k === "anom") return r.anomaly_score != null ? r.anomaly_score : 0;
      return r[k];
    }
    function draw() {
      var list = cache.slice().sort(function (a, b) {
        var x = val(a, sortKey), y = val(b, sortKey);
        x = (x == null ? -Infinity : x); y = (y == null ? -Infinity : y);
        return (x < y ? -1 : (x > y ? 1 : 0)) * sortDir;
      });
      function th(k, label) {
        var arrow = sortKey === k ? (sortDir === 1 ? " ▲" : " ▼") : "";
        return "<th class='sortable' data-sort='" + k + "'>" + label + arrow + "</th>";
      }
      document.getElementById("out").innerHTML = "<table><tr>" + th("symbol", "Symbol") +
        th("asset_type", "Asset") + th("last", "Last") + th("chg", "Chg%") +
        th("rsi", "RSI") + th("trend", "Trend") + th("score", "Score") +
        th("volume", "Vol") + th("spread", "Spread") + th("anom", "Anom") +
        "<th>Trend</th></tr>" +
        list.map(function (r, i) {
          var c = val(r, "chg");
          var t = { up: "▲", down: "▼", flat: "—" }[r.trend] || "—";
          var sc = r.score;
          var an = r.anomaly_score || 0;
          var why = esc(r.summary || r.reason || "");
          var tip = "'title='" + why + "'";
          return "<tr><td><a href='#/chart/" + esc(r.symbol) + "' " + tip + ">" + esc(r.symbol) +
            "</a> <a class='alnk' href='#/analysis/" + esc(r.symbol) + "' title='analysis'>⟁</a></td>" +
            "<td class='dim'>" + esc(r.asset_type) + "</td><td>" + num(r.last) + "</td><td class='" +
            (c > 0 ? "pos" : (c < 0 ? "neg" : "")) + "'>" +
            (c == null ? "—" : (c >= 0 ? "+" : "") + c.toFixed(2) + "%") + "</td><td>" +
            (r.rsi14 == null ? "—" : r.rsi14) + "</td><td class='" +
            (r.trend === "up" ? "pos" : (r.trend === "down" ? "neg" : "dim")) + "'>" + t + "</td><td>" +
            "<b class='" + (sc >= 60 ? "pos" : (sc < 40 ? "neg" : "")) + "'>" + num(sc, 1) + "</b></td><td>" +
            num(r.volume, 0) + "</td><td>" + num(val(r, "spread"), 1) + "</td><td>" +
            (an > 0 ? "<span class='badge-anom' title='" + esc((r.anomalies || []).map(function (a) { return a.message; }).join(" · ")) + "'>◉" + (an >= 10 ? " " + an : "") + "</span>" : "—") +
            "</td><td><canvas class='spark' data-spark='" + i + "'></canvas></td></tr>";
        }).join("") + "</table><p class='kpi-l'>" + list.length + " symbol(s) match</p>";
      view().querySelectorAll("th.sortable").forEach(function (h) {
        h.onclick = function () {
          var k = h.getAttribute("data-sort");
          if (sortKey === k) sortDir *= -1;
          else { sortKey = k; sortDir = (k === "symbol" || k === "asset_type") ? 1 : -1; }
          draw();
        };
      });
      try {
        view().querySelectorAll("canvas[data-spark]").forEach(function (cv) {
          var r = list[parseInt(cv.getAttribute("data-spark"), 10)];
          if (r && typeof sparkline === "function") sparkline(cv, r.closes || []);
        });
      } catch (e) { /* sparklines optional */ }
    }
    function run() {
      var q = "/api/screener?min_vol=" + encodeURIComponent(document.getElementById("f-vol").value || 0) +
        "&max_spread_bps=" + encodeURIComponent(document.getElementById("f-spread").value || 100) +
        "&exchange=" + encodeURIComponent(document.getElementById("f-ex").value || "") +
        "&asset_type=" + encodeURIComponent(document.getElementById("f-asset").value || "") +
        "&region=" + encodeURIComponent(document.getElementById("f-region").value || "");
      document.getElementById("out").innerHTML = "<span class='kpi-l'>scanning…</span>";
      api("GET", q).then(function (rows) {
        cache = rows.rows || rows.results || rows.symbols || rows || [];
        draw();
      }).catch(function (e) { document.getElementById("out").innerHTML = showErr(e); });
    }
    api("GET", "/api/exchanges").then(function (d) {
      var sel = document.getElementById("f-ex");
      (d.exchanges || []).forEach(function (x) {
        var o = document.createElement("option");
        o.value = x.code; o.textContent = x.code + (x.name ? " — " + x.name : "");
        sel.appendChild(o);
      });
    }).catch(function () { /* filter optional */ });
    document.getElementById("f-go").onclick = run;
    run();
  }

  // ---- Analysis: sweep the whole universe -------------------------------
  //
  // Analysis could analyse any one symbol but had no way to answer "how
  // does everything look right now", which is the question a screener
  // is really for. The per-symbol endpoint is one request per symbol;
  // /api/screener returns the same analysis fields for the whole
  // universe in one request, so the sweep costs one round trip instead
  // of 39.
  function renderAnalysisAll(d) {
    var rows = (d && (d.results || d.rows)) || [];
    if (!rows.length) {
      document.getElementById("an-out").innerHTML =
        "<p class='muted'>nothing to analyse</p>";
      return;
    }
    function th(k, label) {
      return "<th class='sortable as-th' data-an-sort='" + k + "'>" +
        esc(label) + "</th>";
    }
    function cell(r, k) {
      var v = r[k];
      if (v == null || v === "") return "<td class='dim'>—</td>";
      if (k === "bias") {
        return "<td class='" + (v === "long" ? "pos" : (v === "short" ? "neg" : "dim")) +
          "'>" + esc(v) + "</td>";
      }
      if (k === "anomaly_score") {
        return "<td><b class='" + (Number(v) >= 8 ? "neg" : "") + "'>" +
          num(v, 1) + "</b></td>";
      }
      if (typeof v === "number") {
        return "<td>" + num(v, Math.abs(v) < 10 ? 3 : 2) + "</td>";
      }
      return "<td>" + esc(v) + "</td>";
    }
    var html = "<div class='kpi-l'>" + rows.length +
      " instruments analysed · sorted by score · click a row for the full read</div>" +
      "<table class='an-all'><tr>" +
      th("symbol", "Symbol") + th("score", "Score") + th("bias", "Bias") +
      th("regime", "Regime") + th("rsi14", "RSI") + th("trend", "Trend") +
      th("trend_strength", "Strength") + th("momentum_pct", "Mom%") +
      th("vol_ann_pct", "Vol%") + th("spread_bps", "Spread") +
      th("anomaly_score", "Anom") + th("tf_verdict", "TF") + "</tr>" +
      rows.map(function (r) {
        return "<tr><td><a href='#/analysis/" + encodeURIComponent(r.symbol) +
          "'>" + esc(r.symbol) + "</a> <span class='dim'>" +
          esc(r.name || "") + "</span></td>" +
          cell(r, "score") + cell(r, "bias") + cell(r, "regime") +
          cell(r, "rsi14") + cell(r, "trend") + cell(r, "trend_strength") +
          cell(r, "momentum_pct") + cell(r, "vol_ann_pct") +
          cell(r, "spread_bps") + cell(r, "anomaly_score") +
          cell(r, "tf_verdict") + "</tr>";
      }).join("") + "</table>";
    var out = document.getElementById("an-out");
    out.innerHTML = html;
    var st = { sort: "score", asc: false };
    function resort() {
      var k = st.sort;
      rows.sort(function (a, b) {
        var x = a[k], y = b[k];
        if (k === "symbol") {
          return String(x || "").localeCompare(String(y || "")) * (st.asc ? 1 : -1);
        }
        x = (x == null || !isFinite(x)) ? -Infinity : Number(x);
        y = (y == null || !isFinite(y)) ? -Infinity : Number(y);
        return (x - y) * (st.asc ? 1 : -1);
      });
      renderRows();
    }
    function renderRows() {
      out.innerHTML = "<div class='kpi-l'>" + rows.length +
        " instruments analysed · sorted by " + esc(st.sort) + "</div>" +
        "<table class='an-all'><tr>" +
        th("symbol", "Symbol") + th("score", "Score") + th("bias", "Bias") +
        th("regime", "Regime") + th("rsi14", "RSI") + th("trend", "Trend") +
        th("trend_strength", "Strength") + th("momentum_pct", "Mom%") +
        th("vol_ann_pct", "Vol%") + th("spread_bps", "Spread") +
        th("anomaly_score", "Anom") + th("tf_verdict", "TF") + "</tr>" +
        rows.map(function (r) {
          return "<tr><td><a href='#/analysis/" + encodeURIComponent(r.symbol) +
            "'>" + esc(r.symbol) + "</a> <span class='dim'>" +
            esc(r.name || "") + "</span></td>" +
            cell(r, "score") + cell(r, "bias") + cell(r, "regime") +
            cell(r, "rsi14") + cell(r, "trend") + cell(r, "trend_strength") +
            cell(r, "momentum_pct") + cell(r, "vol_ann_pct") +
            cell(r, "spread_bps") + cell(r, "anomaly_score") +
            cell(r, "tf_verdict") + "</tr>";
        }).join("") + "</table>";
      Array.prototype.forEach.call(out.querySelectorAll("[data-an-sort]"),
        function (el) {
          el.onclick = function () {
            var k = el.getAttribute("data-an-sort");
            if (st.sort === k) st.asc = !st.asc; else { st.sort = k; st.asc = false; }
            resort();
          };
        });
    }
    renderRows();
  }

  // ---- Analysis: chart the symbol under review --------------------------
  //
  // Analysis produced a wall of numbers with no price context, so a
  // reading of "RSI 71, trend strong" had nothing to be checked against.
  // The chart goes above the analysis output for that reason.
  function mountAnalysisChart(sym) {
    var host = document.getElementById("an-chart");
    if (!host || !sym) return;
    host.innerHTML = "<div class='kpi-l'>loading chart…</div>" +
      "<canvas class='chart' id='an-cv'></canvas>" +
      "<canvas class='rsi' id='an-rsi' style='margin-top:8px'></canvas>" +
      "<div id='an-cerr'></div>";
    api("GET", "/api/candles?symbol=" + encodeURIComponent(sym) +
        "&tf=1m&limit=240").then(function (d) {
      var rows = d.candles || d.rows || [];
      if (!rows.length) {
        host.innerHTML = "<p class='muted'>no stored candles for " +
          esc(sym) + "</p>";
        return;
      }
      var closes = rows.map(clOf);
      try {
        drawCandles(document.getElementById("an-cv"), rows,
                    emaArr(closes, 9), emaArr(closes, 21), vwapArr(rows),
                    { title: sym });
      } catch (e) {
        var ce = document.getElementById("an-cerr");
        if (ce) ce.innerHTML = showErr(e);
      }
      try { drawRSI(document.getElementById("an-rsi"), rows); }
      catch (e) { /* rsi optional */ }
      var first = closes[0], last = closes[closes.length - 1];
      var hi = -Infinity, lo = Infinity;
      rows.forEach(function (c) {
        var h = c.h != null ? c.h : c.high;
        var l = c.l != null ? c.l : c.low;
        if (h != null && h > hi) hi = h;
        if (l != null && l < lo) lo = l;
      });
      var st = document.createElement("div");
      st.className = "kpi-l";
      st.textContent = "n=" + rows.length + " · last=" + num(last) +
        " · chg=" + (first ? ((last - first) / first * 100).toFixed(2) : "—") +
        "% · range " + num(lo) + "–" + num(hi);
      host.insertBefore(st, host.firstChild);
    }).catch(function (e) {
      host.innerHTML = showErr(e);
    });
  }

  function pageAnalysis(sym) {
    hideLogin();
    if (!sym) { sym = ""; }
    view().innerHTML = "<h2>Analysis</h2><div class='row'>" +
      "<label>Symbol <select id='an-sym'><option value=''>— pick —</option></select></label>" +
      "<button id='an-go' type='button'>Analyse</button>" +
      "<button id='an-all' type='button'>Analyse all</button>" +
      "<input id='an-q' placeholder='filter' autocomplete='off' " +
      "spellcheck='false' size='10'>" +
      "</div><div id='an-chart'></div><div id='an-out'></div>";
    // Sweep the universe: one request instead of one per symbol.
    document.getElementById("an-all").onclick = function () {
      var out = document.getElementById("an-out");
      out.innerHTML = "<p class='muted'>analysing universe…</p>";
      api("GET", "/api/screener?limit=200").then(renderAnalysisAll)
        .catch(function (e) { out.innerHTML = showErr(e); });
    };
    var anq = document.getElementById("an-q");
    if (anq) {
      anq.oninput = function () {
        var v = anq.value.trim().toUpperCase();
        var rows = document.querySelectorAll("#an-out tbody tr, #an-out table tr");
        Array.prototype.forEach.call(rows, function (tr) {
          if (tr.querySelector("th")) return;
          tr.style.display = (!v || (tr.textContent || "").toUpperCase().indexOf(v) >= 0)
            ? "" : "none";
        });
      };
    }
    api("GET", "/api/symbols").then(function (d) {
      var sel = document.getElementById("an-sym");
      (d.symbols || []).forEach(function (s) {
        var o = document.createElement("option");
        o.value = s.symbol;
        o.textContent = s.symbol + (s.name ? " — " + s.name : "");
        if (sym && s.symbol === sym) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function () {});
    document.getElementById("an-go").onclick = function () {
      var v = document.getElementById("an-sym").value;
      if (v) location.hash = "#/analysis/" + encodeURIComponent(v);
    };
    if (sym) {
      mountAnalysisChart(sym);
      api("GET", "/api/analysis?symbol=" + encodeURIComponent(sym)).then(function (d) {
        renderAnalysis(d);
      }).catch(function (e) { document.getElementById("an-out").innerHTML = showErr(e); });
    } else {
      document.getElementById("an-out").innerHTML = "<p class='kpi-l'>pick a symbol to open the intelligence report</p>";
    }
  }

  function anBadge(a) {
    var sev = (a && a.severity) || "low";
    var cls = sev === "high" ? "an-high" : (sev === "medium" ? "an-med" : "an-low");
    return "<span class='" + cls + "'>" + esc((a.kind || "flag").toUpperCase()) + "</span>" +
      " <span class='dim'>" + esc(a.message || "") + "</span>";
  }

  function renderAnalysis(d) {
    var a = d.analysis || {}, s = d.symbol || {}, ex = d.exchange || {}, rc = d.research;
    var biasCls = a.bias === "long" ? "pos" : (a.bias === "short" ? "neg" : "dim");
    var kpi = function (k, v, cls) {
      return "<div class='kpi'><span class='kpi-v " + (cls || "") + "'>" + v + "</span><span class='kpi-k'>" + k + "</span></div>";
    };
    var tfl = (a.timeframes || []).map(function (t) {
      return esc(t[0]) + ":<b class='" + (t[1] === "up" ? "pos" : (t[1] === "down" ? "neg" : "dim")) + "'>" + esc(t[1]) + "</b>";
    }).join(" · ") || "—";
    document.getElementById("an-out").innerHTML =
      "<div class='card'><div class='row spb'>" +
      "<h3>" + esc(s.symbol) + " <span class='dim'>" + esc(s.name || "") + "</span></h3>" +
      "<div class='chip-row'>" +
        ["asset " + esc(s.asset_type), esc(s.exchange), esc(s.currency), esc(s.sector || s.asset_type)]
          .filter(Boolean).map(function (c) { return "<span class='chip'>" + c + "</span>"; }).join("") +
      "</div></div>" +
      "<div class='kpi-l'>" + kpi("Bias", esc(a.bias || "flat").toUpperCase(), biasCls) +
        kpi("Score", num(a.score, 1)) + kpi("Last", num(a.last)) +
        kpi("RSI(14)", a.rsi14 == null ? "—" : a.rsi14) +
        kpi("Trend", esc(a.trend || "—"), a.trend === "up" ? "pos" : (a.trend === "down" ? "neg" : "")) +
        kpi("Momentum", a.momentum_pct == null ? "—" : (a.momentum_pct >= 0 ? "+" : "") + num(a.momentum_pct, 2) + "%",
            a.momentum_pct > 0 ? "pos" : (a.momentum_pct < 0 ? "neg" : "")) +
        kpi("Vol (ann)", a.vol_ann_pct == null ? "—" : num(a.vol_ann_pct, 1) + "%") +
        kpi("ATR", a.atr_pct == null ? "—" : num(a.atr_pct, 2) + "%") +
        kpi("Spread", a.spread_bps == null ? "—" : num(a.spread_bps, 1) + "bp") +
        kpi("Anomaly", a.anomaly_score == null ? "—" : num(a.anomaly_score, 1),
            a.anomaly_score >= 15 ? "neg" : "") +
        kpi("Anomalies", (a.anomalies || []).length) + "</div>" +
      "<p class='why'>" + esc(a.reason || a.summary || "") + "</p></div>" +
      "<div class='grid2'>" +
      "<div class='card'><h4>Regime intelligence</h4>" +
        "<p class='dim'>" + esc(a.regime_description || "") + "</p>" +
        "<p><b>Multi-timeframe</b> <span class='dim'>" + esc(a.tf_verdict || "") + "</span></p>" +
        "<p class='kpi-l'>" + tfl + "</p>" +
        (a.leverage ? "<p><b>Leverage note</b> <span class='dim'>" + esc(a.leverage.contract_note || "") + "</span></p>" +
          "<p class='kpi-l'>notional/lot " + num(a.leverage.notional_per_lot) + " · " +
          "tick value/lot " + num(a.leverage.tick_value_per_lot, 4) + "</p>" : "") +
        "</div>" +
      "<div class='card'><h4>Anomaly scan</h4>" +
        ((a.anomalies || []).length ? a.anomalies.map(anBadge).join("<br>") :
          "<p class='kpi-l'>no anomalies — quiet tape</p>") +
        "</div>" +
      renderCotOverlay(a.cot) +
      (rc ? "<div class='card'><h4>Commodity research · " + esc(rc.class || "") + "</h4>" +
        (rc.drivers || []).map(function (dr) { return "<p>◆ <span class='dim'>" + esc(dr) + "</span></p>"; }).join("") +
        "</div>" : "") +
      (ex.code ? "<div class='card'><h4>Venue · " + esc(ex.name || ex.code) + "</h4>" +
        "<p class='kpi-l'>" + esc(ex.code) + " · " + esc(ex.type) + " · " + esc(ex.region || "") + " · " + esc(ex.tz || "") + "</p>" +
        "<p class='dim'>" + esc(ex.notes || "") + "</p>" +
        "</div>" : "") +
      "</div>" +
      "<p class='kpi-l'><a href='#/chart/" + esc(s.symbol) + "'>chart</a> · " +
        "<a href='#/screener'>screener</a> · trade live in <a href='#/scalp-live'>scalp live</a></p>";
  }

  function pageAnomalies() {
    hideLogin();
    view().innerHTML = "<h2>Anomaly Radar</h2>" +
      "<div class='kpi-l' id='anm-sub'>scanning…</div>" +
      "<div id='anm-kinds' class='kpi-l'></div>" +
      "<label style='display:inline-block;margin:8px 0'><input type='checkbox' id='anm-all' style='width:auto'> show clean tape (all instruments)</label>" +
      "<div id='anm-out'></div>";
    function load() {
      var showAll = document.getElementById("anm-all") && document.getElementById("anm-all").checked;
      document.getElementById("anm-out").innerHTML = "<p class='kpi-l'>scanning…</p>";
      api("GET", "/api/anomalies" + (showAll ? "?all=1" : "")).then(function (d) {
        var list = d.results || [];
        var bySev = d.by_severity || {};
        var kinds = d.kinds || {};
        document.getElementById("anm-sub").innerHTML =
          "<span class='kpi'><b>" + (d.scanned == null ? "—" : d.scanned) + "</b> instruments scanned</span>" +
          "<span class='kpi'><b class='" + (d.flagged ? "neg" : "") + "'>" + (d.flagged == null ? "—" : d.flagged) + "</b> flagged</span>" +
          "<span class='kpi'><b class='pos'>" + (d.clean == null ? "—" : d.clean) + "</b> clean</span>" +
          "<span class='kpi'><b>" + (bySev.high || 0) + "</b> high</span>" +
          "<span class='kpi'><b>" + (bySev.medium || 0) + "</b> medium</span>" +
          "<span class='kpi'><b>" + (bySev.low || 0) + "</b> low</span>" +
          "<span class='dim'>statistical scans: price z-score · range vs ATR · volume z · EWMA vol regime · spread stress · level breaks</span>";
        var kindKeys = Object.keys(kinds).sort(function (a, b) { return kinds[b] - kinds[a]; });
        document.getElementById("anm-kinds").innerHTML = kindKeys.length ?
          kindKeys.map(function (k) { return "<span class='tag'>" + esc(k) + " ×" + kinds[k] + "</span>"; }).join("") : "";
        document.getElementById("anm-out").innerHTML = list.length ? "<table><tr>" +
          "<th>Symbol</th><th>Asset</th><th>Last</th><th>Score</th><th>Bias</th><th>Regime</th><th>Anom</th><th>Events</th></tr>" +
          list.map(function (r) {
            var flag = (r.anomaly_score || 0) >= 1;
            var al = (r.anomalies || []).slice(0, 3);
            return "<tr class='" + (flag ? "" : "clean-row") + "'><td><a href='#/analysis/" + esc(r.symbol) + "'>" + esc(r.symbol) + "</a>" +
              " <span class='dim'>" + esc(r.name || "") + "</span></td>" +
              "<td class='dim'>" + esc(r.asset_type) + "</td><td>" + num(r.last) + "</td><td>" + num(r.score, 1) +
              "</td><td class='" + (r.bias === "long" ? "pos" : (r.bias === "short" ? "neg" : "dim")) + "'>" + esc(r.bias || "flat") +
              "</td><td class='dim'>" + esc(r.regime || "—") + "</td><td><b class='" + ((r.anomaly_score || 0) >= 20 ? "neg" : "") + "'>" +
              num(r.anomaly_score, 1) + "</b></td><td>" + (al.length ? al.map(anBadge).join("<br>") : "<span class='dim'>—</span>") + "</td></tr>";
          }).join("") + "</table>" :
          "<p class='kpi-l'>no anomalies — quiet tape across the universe</p>";
      }).catch(function (e) { document.getElementById("anm-out").innerHTML = showErr(e); });
    }
    var box = document.getElementById("anm-all");
    if (box) box.addEventListener("change", load);
    load();
  }

  function emaArr(vals, period) {
    var k = 2 / (period + 1), out = [], prev = null;
    vals.forEach(function (v, i) {
      if (v == null || !isFinite(v)) { out.push(null); return; }
      prev = (prev == null) ? v : v * k + prev * (1 - k);
      out.push(i + 1 >= period ? prev : null);
    });
    return out;
  }
  function vwapArr(candles) {
    var cumPV = 0, cumV = 0;
    return candles.map(function (c) {
      var h = c.h != null ? c.h : c.high, l = c.l != null ? c.l : c.low,
          cl = c.c != null ? c.c : c.close, v = c.v != null ? c.v : (c.volume || 0);
      cumPV += ((h + l + cl) / 3) * v; cumV += v;
      return cumV > 0 ? cumPV / cumV : null;
    });
  }
  function clOf(c) { return c.c != null ? c.c : c.close; }
  function volOf(c) { return c.v != null ? c.v : (c.volume || 0); }

  function pageChart(sym) {
    hideLogin();
    sym = sym || "BTCUSD";
    view().innerHTML = "<h2>Chart " + esc(sym) + "</h2><div id='ticker'></div>" +
      "<div id='qp' class='quote-grid'></div>" +
      "<div id='cstats' class='kpi-l'>loading…</div>" +
      "<div id='c-regime' class='kpi-l'></div>" +
      "<div id='c-pos' class='kpi-l'></div>" +
      "<div class='row'><label><input type='checkbox' id='ov-e9' checked style='width:auto'> EMA9</label>" +
      "<label><input type='checkbox' id='ov-e21' checked style='width:auto'> EMA21</label>" +
      "<label><input type='checkbox' id='ov-vw' checked style='width:auto'> VWAP</label>" +
      "<label><input type='checkbox' id='ov-bb' style='width:auto'> Bollinger(20)</label>" +
      "<label><input type='checkbox' id='ov-rsi' checked style='width:auto'> RSI(14)</label>" +
      "<label>TF <select id='c-tf'><option value='1m'>1m</option>" +
      "<option value='1d'>1d</option></select></label>" +
      "<label>Limit <input id='c-lim' type='number' value='200' style='width:90px'></label>" +
      "<button id='c-go' type='button'>Reload</button></div>" +
      "<canvas class='chart' id='cv'></canvas><canvas class='rsi' id='rsi' style='margin-top:8px'></canvas>" +
      "<div id='cerr'></div><h3>News — " + esc(sym) + "</h3><div id='cnews' class='kpi-l'>loading…</div>";
    var closes = [], vols = [], raw = [], reg = null, posCtx = null;

    // Position + cost line under the chart.
    //
    // The cost figure is the point of the panel. A signal that looks good
    // on a chart is not a trade until it clears the round trip, and a
    // dislocation of a few bps against a ~70 bps round trip is not a small
    // edge -- it is a reason not to trade. Showing gross without the cost
    // beside it invites exactly the mistake the significance engine exists
    // to prevent.
    function posCaption(c) {
      if (!c) return "";
      var bits = [];
      var p = c.position;
      if (p && p.qty) {
        var side = p.qty >= 0 ? "LONG" : "SHORT";
        var pnl = c.unrealized_pnl;
        bits.push(side + " " + num(Math.abs(p.qty), 4) + " @ " +
          num(p.avg_price));
        bits.push("unrealised " +
          (pnl == null ? "—" : ((pnl >= 0 ? "+" : "") + num(pnl) + " " +
            (c.currency || ""))).trim());
      } else {
        bits.push("flat");
      }
      if (c.round_trip_cost_bps != null) {
        bits.push("round-trip cost " + num(c.round_trip_cost_bps, 1) + " bps");
      }
      return "<span class='dim'>" + esc(bits.join(" · ")) + "</span>";
    }
    // Caption under the regime strip. It reports the *basis*, because a
    // threshold drawn from a fallback constant and one estimated from this
    // instrument's own history mean very different things, and a chart that
    // looked identical either way would hide which one it was showing.
    function regCaption(c) {
      if (!c || !c.series || !c.series.length) return "";
      var r = c.regime || {};
      var rs = c.resolution || {};
      var flagged = c.series.filter(function (x) { return x.anomaly; }).length;
      var state = r.basis === "estimated"
        ? "cuts p95/p05 = " + (r.expansion || 0).toFixed(2) + " / " +
          (r.compression || 0).toFixed(2) + " (estimated)"
        : "cuts " + (r.expansion || 0).toFixed(2) + " / " +
          (r.compression || 0).toFixed(2) + " (FALLBACK constants)";
      // State the interval the bar-counted windows were counted in. On a
      // daily chart a "60-bar window" is a quarter, not an hour, and the
      // reader cannot work that out from the picture.
      var win = "";
      if (rs.regime_window_bars != null) {
        win = " · " + (rs.label || c.tf || "") + " bars, " +
          rs.regime_window_bars + "-bar window";
        if (rs.ewma_half_life_hours != null) {
          var hl = rs.ewma_half_life_hours;
          win += " · vol half-life " +
            (hl >= 48 ? (hl / 24).toFixed(0) + "d" : hl.toFixed(1) + "h");
        }
      }
      return "<span class='dim'>regime strip: red = expansion, blue = " +
        "compression · " + esc(state) + esc(win) + " · spike cut " +
        (c.cut_z || 0).toFixed(2) + " sigma · " + flagged +
        " flagged bar(s)" +
        (c.kurtosis != null ? " · kurtosis " + (c.kurtosis).toFixed(2) : "") +
        "</span>";
    }
    function bbArr(vals, period) {
      var up = [], dn = [], i;
      for (i = 0; i < vals.length; i++) {
        if (i + 1 < period) { up.push(null); dn.push(null); continue; }
        var w = vals.slice(i + 1 - period, i + 1);
        var m = w.reduce(function (a, b) { return a + b; }, 0) / period;
        var sd = Math.sqrt(w.reduce(function (a, b) { return a + (b - m) * (b - m); }, 0) / period);
        up.push(m + 2 * sd); dn.push(m - 2 * sd);
      }
      return { up: up, dn: dn };
    }
    function render() {
      var e9 = document.getElementById("ov-e9").checked ? emaArr(closes, 9) : null;
      var e21 = document.getElementById("ov-e21").checked ? emaArr(closes, 21) : null;
      var vw = document.getElementById("ov-vw").checked ? vwapArr(raw) : null;
      var bands = [];
      if (document.getElementById("ov-bb").checked) {
        var bb = bbArr(closes, 20);
        bands = [{ data: bb.up, color: "#8b949e" }, { data: bb.dn, color: "#8b949e" }];
      }
      try {
        drawCandles(document.getElementById("cv"), raw, e9, e21, vw,
          { title: sym, bands: bands, regime: reg, position: posCtx });
        if (document.getElementById("ov-rsi").checked) {
          document.getElementById("rsi").style.display = "";
          try { drawRSI(document.getElementById("rsi"), raw); }
          catch (e) { /* rsi optional */ }
        } else {
          document.getElementById("rsi").style.display = "none";
        }
      } catch (e) { document.getElementById("cerr").innerHTML = showErr(e); }
    }
    function load() {
      var lim = document.getElementById("c-lim").value || 200;
      var tf = (document.getElementById("c-tf") || {}).value || "1m";
      document.getElementById("cstats").textContent = "loading…";
      api("GET", "/api/candles?symbol=" + encodeURIComponent(sym) +
          "&tf=" + encodeURIComponent(tf) + "&limit=" + encodeURIComponent(lim))
        .then(function (data) {
          raw = data.candles || data.rows || [];
          closes = raw.map(clOf); vols = raw.map(volOf);
          if (!raw.length) {
            document.getElementById("cstats").textContent =
              "no " + tf + " candles for " + sym +
              (tf === "1d" ? " (daily history is only loaded for symbols " +
                "covered by scripts/fetch_yahoo.py)" : "");
            render(); return;
          }
          // Echo the resolution the API actually used. If it differs from
          // what was asked for, the chart is showing the wrong timeframe
          // and the operator should know that rather than infer it.
          var used = data.tf || tf;
          if (used !== tf) {
            document.getElementById("cerr").innerHTML =
              "requested " + esc(tf) + ", API served " + esc(used);
          }
          var first = closes[0], last = closes[closes.length - 1];
          var hi = Math.max.apply(null, raw.map(function (c) { return c.h != null ? c.h : c.high; }));
          var lo = Math.min.apply(null, raw.map(function (c) { return c.l != null ? c.l : c.low; }));
          var chg = last - first, pct = first ? chg / first * 100 : 0;
          var avgV = vols.reduce(function (a, b) { return a + b; }, 0) / vols.length;
          document.getElementById("cstats").innerHTML = "tf=" + esc(used) +
            " · n=" + raw.length +
            " · last=" + num(last) + " · chg=" + (chg >= 0 ? "+" : "") + num(chg) +
            " (" + (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%) · range " + num(lo) + "–" + num(hi) +
            " · avgVol=" + num(avgV, 0);
          // Position + round-trip cost. Independent of resolution: the entry
      // price and the cost figure are properties of the position, not of
      // the bar interval, so this runs for 1m and 1d alike.
      api("GET", "/api/chart_position?symbol=" + encodeURIComponent(sym))
        .then(function (c) {
          if (used !== tf) return;      // a slower response lost the race
          posCtx = (c && c.position) ? c : null;
          var host = document.getElementById("c-pos");
          if (host) host.innerHTML = posCaption(c);
          render();
        })
        .catch(function () {
          posCtx = null;
          var h = document.getElementById("c-pos");
          if (h) h.innerHTML = "";
        });

      // Regime + anomaly context, at whatever resolution is on screen.
      //
      // This previously refused tf=1d outright, on the reasoning that a
      // 60-bar regime window meant "one hour" and would become "three
      // months". That ambiguity was real but the refusal was the wrong
      // response: the window is a bar count and means 60 bars of whatever
      // is loaded, which is a legitimate question at either resolution.
      //
      // The engine now reports the interval it counted in -- the windows,
      // and the EWMA half-life in both bars and wall-clock -- so the
      // caption states the basis instead of the panel deciding for the
      // reader which question is being asked.
      api("GET", "/api/chart_context?symbol=" + encodeURIComponent(sym) +
          "&tf=" + encodeURIComponent(used) +
          "&limit=" + encodeURIComponent(lim))
        .then(function (c) {
          if (used !== tf) return;      // a slower response lost the race
          reg = (c && c.series && c.series.length) ? c : null;
          var host = document.getElementById("c-regime");
          if (host) host.innerHTML = reg ? regCaption(reg) : "";
          render();
        })
        .catch(function () {
          reg = null;
          var h2 = document.getElementById("c-regime");
          if (h2) h2.innerHTML = "";
        });

      var q0 = raw[0];
      var o0 = q0.o != null ? q0.o : q0.open;
      var totV = vols.reduce(function (a, b) { return a + b; }, 0);
      document.getElementById("qp").innerHTML =
        "<div class='quote-cell'><div class='k'>Last</div><div class='v' id='qp-last'>" + num(last) + "</div></div>" +
        "<div class='quote-cell'><div class='k'>Chg%</div><div class='v " + (pct >= 0 ? "up" : "down") + "'>" +
        (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%</div></div>" +
        "<div class='quote-cell'><div class='k'>Open</div><div class='v'>" + num(o0) + "</div></div>" +
        "<div class='quote-cell'><div class='k'>High</div><div class='v'>" + num(hi) + "</div></div>" +
            "<div class='quote-cell'><div class='k'>Low</div><div class='v'>" + num(lo) + "</div></div>" +
            "<div class='quote-cell'><div class='k'>Volume</div><div class='v'>" + num(totV, 0) + "</div></div>" +
            "<div class='quote-cell'><div class='k'>Candles</div><div class='v'>" + raw.length + "</div></div>";
          render();
          api("GET", "/api/news?symbol=" + encodeURIComponent(sym) + "&limit=8")
            .then(function (nd) { renderNews(document.getElementById("cnews"), nd); })
            .catch(function () {
              document.getElementById("cnews").textContent = "news unavailable";
            });
        }).catch(function (e) { document.getElementById("cerr").innerHTML = showErr(e); });
    }
    ["ov-e9", "ov-e21", "ov-vw", "ov-bb", "ov-rsi"].forEach(function (id) {
      document.getElementById(id).onchange = render;
    });
    document.getElementById("c-go").onclick = load;
    load();
    startSSE(function (q, html, prev) {
      if (q.symbol !== sym) return;
      var t = document.getElementById("ticker");
      if (t) t.innerHTML = "<span class='badge'>" + esc(sym) + " bid " + num(q.bid) +
        " ask " + num(q.ask) + " last " + num(q.last != null ? q.last : q.ask) + "</span>";
      var qp = document.getElementById("qp-last");
      if (qp) {
        var last = q.last != null ? q.last : q.ask;
        var pl = prev ? (prev.last != null ? prev.last : prev.ask) : null;
        qp.textContent = num(last);
        qp.className = "v" + ((pl != null && last != null && last !== pl) ? (last > pl ? " up" : " down") : "");
      }
    });
  }

  // The method list is NOT duplicated here. It is rendered entirely from
  // GET /api/strategies, which merges the engine's canonical metadata
  // (real param defaults, mode, description) with the DB row. A local
  // copy is what previously drifted: it advertised hand-tuned defaults
  // under wrong names (window/zscore instead of period/entry_z) and
  // labelled stub methods as wired.

  function pageScalpLab() {
    hideLogin();
    var lastSymbol = "BTCUSD";

    // The preview and backtest endpoints both key off a symbol. Without
    // one in the request every button returned "strategy and symbol
    // required", so the symbol is chosen once here and shared by all
    // cards rather than retyped per strategy.
    view().innerHTML = "<h2>Scalp Lab</h2>" +
      "<p class='page-purpose'><b>Research.</b> Ask each method what it would signal on stored candles. Nothing here places an order &mdash; use Scalp Live to trade.</p>" +
      "<div class='card lab-bar'>" +
      "<label>Symbol " +
      "<input id='lab-sym' list='lab-sym-list' value='" + esc(lastSymbol) + "' " +
      "autocomplete='off' spellcheck='false' size='10'>" +
      "<datalist id='lab-sym-list'></datalist></label>" +
      "<span id='lab-quote' class='lab-quote' aria-live='polite'>—</span>" +
      "<button type='button' id='lab-refresh'>Refresh quote</button>" +
      "</div>" +
      "<p class='muted'>Every card previews the same symbol against stored " +
      "1-minute candles and the latest quote, using the same engine the " +
      "backtester and live board use — so a preview cannot disagree with a fill.</p>" +
      "<div id='lab-cards'><p class='muted'>loading methods…</p></div>";

    var symInput = document.getElementById("lab-sym");
    var quoteOut = document.getElementById("lab-quote");
    var cardsOut = document.getElementById("lab-cards");

    function symbol() {
      return (symInput && symInput.value ? symInput.value : "").trim().toUpperCase();
    }

    // Parse params, and *say so* when the JSON is bad. The old code
    // swallowed a parse error and silently sent {}, so a typo looked
    // like a strategy that ignored its settings.
    function paramsFor(name) {
      var el = view().querySelector("input[data-params-for='" + name + "']");
      var hint = view().querySelector("p[data-hint-for='" + name + "']");
      var raw = el && el.value ? el.value.trim() : "";
      if (!raw) { if (hint) hint.textContent = ""; return {}; }
      try {
        var v = JSON.parse(raw);
        if (v === null || typeof v !== "object" || Array.isArray(v)) {
          throw new Error("params must be a JSON object");
        }
        if (hint) hint.textContent = "";
        return v;
      } catch (e) {
        if (hint) {
          hint.innerHTML = "<span class='error'>bad params JSON: " +
            esc(e.message) + "</span>";
        }
        return null;
      }
    }

    function setOut(name, html) {
      var out = view().querySelector("div[data-out='" + name + "']");
      if (out) out.innerHTML = html;
    }

    // Render a signal as a decision, not a JSON blob.
    function renderSignal(r) {
      if (r.stub) {
        return "<p class='muted'>This method has no live logic yet — the " +
          "tier badge marks the upgrade path, not a working strategy.</p>";
      }
      var sig = String(r.signal || "flat").toLowerCase();
      var cls = sig === "buy" ? "sig-buy" : (sig === "sell" ? "sig-sell" : "sig-flat");
      var label = sig === "buy" ? "BUY" : (sig === "sell" ? "SELL" : "FLAT");
      var rows = "";
      function kv(k, v) {
        if (v == null || v === "") return "";
        return "<div class='kv'><span>" + esc(k) + "</span><b>" + esc(v) + "</b></div>";
      }
      rows += kv("reason", r.reason);
      if (r.qty != null) rows += kv("qty", r.qty);
      if (r.last != null) rows += kv("last", num(r.last, 2));
      if (r.bid != null && r.ask != null) {
        rows += kv("bid / ask", num(r.bid, 2) + " / " + num(r.ask, 2));
      }
      if (r.spread_bps != null) rows += kv("spread", r.spread_bps + " bps");
      rows += kv("bars", r.bars);
      if (r.source && r.source !== "engine") rows += kv("source", r.source);
      return "<div class='sig " + cls + "'><span class='sig-label'>" + label +
        "</span><div class='sig-body'>" +
        (rows || "<span class='muted'>no detail</span>") + "</div></div>";
    }

    // Metric keys come from engines/backtest.py: trades, wins, win_rate,
    // pf, maxDD, sharpe, cagr, avg_hold. Guessed names here rendered an
    // almost-empty panel, so the labels are pinned to that exact set.
    function renderMetrics(m, equity) {
      m = m || {};
      var parts = [];
      function add(k, v, d) {
        if (v == null || (typeof v === "number" && !isFinite(v))) return;
        parts.push("<div class='kv'><span>" + esc(k) + "</span><b>" +
          esc(num(v, d)) + "</b></div>");
      }
      add("trades", m.trades, 0);
      add("wins", m.wins, 0);
      add("win rate", m.win_rate == null ? null : (m.win_rate * 100), 1);
      add("profit factor", m.pf, 2);
      add("sharpe", m.sharpe, 2);
      add("max drawdown", m.maxDD == null ? null : (m.maxDD * 100), 2);
      add("cagr", m.cagr == null ? null : (m.cagr * 100), 2);
      add("avg hold", m.avg_hold, 0);
      // Net change comes off the equity curve, which is the only place
      // the realised P&L actually lands.
      if (Array.isArray(equity) && equity.length > 1) {
        var first = Number(equity[0]), last = Number(equity[equity.length - 1]);
        if (isFinite(first) && isFinite(last) && first !== 0) {
          add("net return", ((last - first) / Math.abs(first)) * 100, 2);
        }
      }
      if (!parts.length) {
        return "<p class='muted'>Backtest ran but returned no metrics.</p>";
      }
      return "<div class='metrics'>" + parts.join("") + "</div>";
    }

    function updateQuote() {
      var s = symbol();
      if (!quoteOut) return;
      if (!s) { quoteOut.textContent = "—"; return; }
      quoteOut.textContent = "loading…";
      api("GET", "/api/quote?symbol=" + encodeURIComponent(s))
        .then(function (q) {
          if (!quoteOut) return;
          if (!q || q.last == null) {
            quoteOut.innerHTML = "<span class='error'>no quote for " + esc(s) + "</span>";
            return;
          }
          var txt = num(q.last, 2);
          if (q.bid != null && q.ask != null) {
            txt += "  bid " + num(q.bid, 2) + " / ask " + num(q.ask, 2);
          }
          if (q.spread_bps != null) txt += "  (" + q.spread_bps + " bps)";
          quoteOut.textContent = txt;
        })
        .catch(function (e) {
          if (quoteOut) quoteOut.innerHTML = showErr(e);
        });
    }

    function wireCard(name) {
      view().querySelectorAll("button[data-preview='" + name + "']").forEach(function (b) {
        b.onclick = function () {
          var sym = symbol();
          if (!sym) { setOut(name, "<p class='error'>enter a symbol</p>"); return; }
          var params = paramsFor(name);
          if (params === null) return;             // hint already explains
          b.disabled = true;
          setOut(name, "<p class='muted'>computing…</p>");
          api("POST", "/api/signals/preview",
              { strategy: name, symbol: sym, params: params })
            .then(function (r) { setOut(name, renderSignal(r)); })
            .catch(function (e) { setOut(name, showErr(e)); })
            .then(function () { b.disabled = false; });
        };
      });
      view().querySelectorAll("button[data-run='" + name + "']").forEach(function (b) {
        b.onclick = function () {
          var sym = symbol();
          if (!sym) { setOut(name, "<p class='error'>enter a symbol</p>"); return; }
          var params = paramsFor(name);
          if (params === null) return;
          b.disabled = true;
          setOut(name, "<p class='muted'>backtesting stored candles…</p>");
          api("POST", "/api/backtests",
              { strategy: name, symbol: sym, params: params })
            .then(function (r) {
              var id = r.id != null ? r.id : (r.backtest && r.backtest.id);
              setOut(name, renderMetrics(r.metrics, r.equity) +
                "<p class='muted'>saved — <a href='#/backtests'>Backtests" +
                (id ? " #" + esc(id) : "") + "</a></p>");
            })
            .catch(function (e) { setOut(name, showErr(e)); })
            .then(function () { b.disabled = false; });
        };
      });
    }

    api("GET", "/api/strategies")
      .then(function (r) {
        var items = (r && r.strategies) || [];
        if (!items.length) {
          cardsOut.innerHTML = "<p class='error'>no strategies configured</p>";
          return;
        }
        cardsOut.innerHTML = "<div class='cards'>" + items.map(function (m) {
          var stub = String(m.mode || "") === "stub";
          var ok = m.allowed !== false;
          var name = String(m.name || "");
          return "<div class='card lab-card" + (ok ? "" : " locked") + "'>" +
            "<h3>" + esc(m.display_name || name) + "</h3>" +
            "<span class='badge " + (stub ? "stub" : "wired") + "'>" +
            (stub ? "no logic yet" : "wired") + "</span> " +
            "<span class='badge'>" + esc(m.tier_required || "Free") + "+</span>" +
            (m.description ? "<p class='muted'>" + esc(m.description) + "</p>" : "") +
            (ok ? "" : "<p class='error'>Upgrade required for your tier.</p>") +
            "<label class='lab-params'>params " +
            "<input data-params-for='" + esc(name) + "' " +
            "value='" + esc(JSON.stringify(m.params || {})) + "' " +
            "spellcheck='false' autocomplete='off'></label>" +
            "<p class='lab-hint' data-hint-for='" + esc(name) + "'></p>" +
            "<div class='row'>" +
            "<button type='button' class='primary' data-preview='" + esc(name) + "'" +
            (ok ? "" : " disabled") + ">Preview signal</button>" +
            "<button type='button' data-run='" + esc(name) + "'" +
            (ok ? "" : " disabled") + ">Run backtest</button></div>" +
            "<div data-out='" + esc(name) + "' aria-live='polite'></div></div>";
        }).join("") + "</div>";
        items.forEach(function (m) { wireCard(String(m.name || "")); });
      })
      .catch(function (e) {
        cardsOut.innerHTML = showErr(e);
      });

    if (symInput) {
      symInput.oninput = updateQuote;
      symInput.onkeydown = function (e) { if (e.key === "Enter") updateQuote(); };
    }
    var refreshBtn = document.getElementById("lab-refresh");
    if (refreshBtn) refreshBtn.onclick = updateQuote;
    updateQuote();

    api("GET", "/api/symbols")
      .then(function (r) {
        var dl = document.getElementById("lab-sym-list");
        var items = (r && (r.symbols || r.items)) || [];
        if (!dl) return;
        dl.innerHTML = items.map(function (s) {
          var sym = typeof s === "string" ? s : (s.symbol || s.name);
          return sym ? "<option value='" + esc(sym) + "'></option>" : "";
        }).join("");
      })
      .catch(function () { /* autocomplete is optional */ });
  }

  // The instrument Scalp Live opens on when no deep link names one.
  var DEFAULT_TICKET_SYMBOL = "BTCUSD";

  // pageScalpLive(symbol, side)
  //
  // `symbol` and `side` arrive from a deep link, e.g. the Buy link on a
  // position row (#/scalp-live/BTCUSD/buy). Arriving with a side armed
  // does NOT place an order: it pre-fills the ticket and highlights the
  // matching button, because a navigation link that fires a trade on
  // arrival is how an operator ends up buying something they only meant
  // to look at.
  function pageScalpLive(presetSymbol, presetSide, presetQty) {
    hideLogin();
    var armedSide = (presetSide === "buy" || presetSide === "sell")
      ? presetSide : null;
    var preset = (presetSymbol || "").trim().toUpperCase();
    // Scalp Live opens on BTCUSD unless a deep link named an instrument.
    // The ticket is useless on an empty symbol: the chart has nothing to
    // draw and the notional estimate cannot be formed.
    if (!preset) preset = DEFAULT_TICKET_SYMBOL;
    // A trading ticket needs the market and the book in front of the
    // operator: what it costs right now, what they already hold, and
    // what a click will actually do. The previous ticket sent the
    // symbol, qty and an auto-set confirm:true with no price, no
    // position context and no second look at a live order.
    view().innerHTML = "<h2>Scalp Live</h2>" +
      "<p class='page-purpose'><b>Execution.</b> Live price, live chart and the order ticket for the symbol below. Paper unless you switch to live and confirm.</p>" +
      "<div id='ticker'></div>" +
      "<div class='lab-bar card'>" +
      "<label>Symbol " +
      "<input id='t-sym' list='t-sym-list' value='" + esc(preset || "BTCUSD") + "'" +
      " autocomplete='off' spellcheck='false' size='10'>" +
      "<datalist id='t-sym-list'></datalist></label>" +
      "<span id='t-price' class='lab-quote' aria-live='polite'>—</span>" +
      "<span id='t-cash' class='badge' title='cash balance'>cash —</span>" +
      "<span id='t-live' class='badge'>—</span>" +
      "<button type='button' id='t-refresh-mkt'>Reload chart</button>" +
      "</div>" +
      "<div class='card live-chart-card'>" +
      "<div id='t-mkt' class='quote-grid'></div>" +
      "<div class='row live-chart-ctl'>" +
      "<label>Range " +
      "<select id='t-bars'><option value='90'>90</option>" +
      "<option value='180' selected>180</option>" +
      "<option value='360'>360</option></select></label>" +
      "<label><input type='checkbox' id='t-ov-e9' checked style='width:auto'> EMA9</label>" +
      "<label><input type='checkbox' id='t-ov-e21' checked style='width:auto'> EMA21</label>" +
      "<label><input type='checkbox' id='t-ov-vw' checked style='width:auto'> VWAP</label>" +
      "<span id='t-cstats' class='kpi-l'>loading chart…</span>" +
      "</div>" +
      "<canvas class='chart' id='t-cv'></canvas>" +
      "<div id='t-cerr'></div>" +
      "</div>" +
      "<div class='grid2'>" +
      "<div class='card'><h3>Ticket</h3>" +
      "<div class='kv'><span>position</span><b id='t-pos'>no position</b></div>" +
      "<label>Qty <input id='t-qty' type='number' value='1' min='0' step='any'></label>" +
      "<label>Order type <select id='t-type'>" +
      "<option value='market'>market</option>" +
      "<option value='limit'>limit</option>" +
      "<option value='stop'>stop</option></select></label>" +
      "<label id='t-pricewrap' hidden>Limit price " +
      "<input id='t-price-in' type='number' step='any'></label>" +
      "<label>Mode <select id='t-mode'>" +
      "<option value='paper'>paper</option>" +
      "<option value='live'>live</option></select></label>" +
      "<p id='t-est' class='lab-hint'></p>" +
      "<div class='row'>" +
      "<button class='buy' id='t-buy' type='button'>Buy</button>" +
      "<button class='sell' id='t-sell' type='button'>Sell</button>" +
      "<button class='danger' id='t-flat' type='button'>Flatten all</button>" +
      "</div>" +
      "<div id='t-out' aria-live='polite'></div></div>" +

      "<div class='card'><h3>Position</h3><div id='t-hold'>—</div>" +
      "<div class='row'><button id='t-refresh' type='button'>Refresh</button></div>" +
      "<h3>Positions</h3><div id='t-positions' class='muted'>loading…</div>" +
      "<h3>Recent fills</h3><div id='t-fills' class='muted'>—</div>" +
      "</div></div>" +
      "<div id='t-confirm-host'></div>";

    var symEl = document.getElementById("t-sym");
    var qtyEl = document.getElementById("t-qty");
    var typeEl = document.getElementById("t-type");
    var priceWrap = document.getElementById("t-pricewrap");
    var priceIn = document.getElementById("t-price-in");
    var modeEl = document.getElementById("t-mode");
    var priceOut = document.getElementById("t-price");
    var cashOut = document.getElementById("t-cash");
    var liveOut = document.getElementById("t-live");
    var posOut = document.getElementById("t-pos");
    var estOut = document.getElementById("t-est");
    var out = document.getElementById("t-out");
    var posHost = document.getElementById("t-positions");
    var fillHost = document.getElementById("t-fills");
    var holdOut = document.getElementById("t-hold");
    var confirmHost = document.getElementById("t-confirm-host");

    var lastBook = null;      // latest quote for the ticket symbol
    var portfolio = null;     // last /api/portfolio payload
    var posEnteredAt = null;  // when the current position was opened

    function symbol() {
      return (symEl && symEl.value ? symEl.value : "").trim().toUpperCase();
    }
    function qty() {
      var v = parseFloat(qtyEl && qtyEl.value);
      return isFinite(v) ? v : NaN;
    }
    function pos() {
      if (!portfolio || !symbol()) return null;
      var s = symbol();
      var list = portfolio.positions || [];
      for (var i = 0; i < list.length; i++) {
        if (String(list[i].symbol).toUpperCase() === s &&
            parseFloat(list[i].qty) !== 0) return list[i];
      }
      return null;
    }
    function mid() {
      if (lastBook) {
        if (lastBook.bid != null && lastBook.ask != null) {
          var m = (Number(lastBook.bid) + Number(lastBook.ask)) / 2;
          if (isFinite(m) && m > 0) return m;
        }
        if (lastBook.last != null && Number(lastBook.last) > 0) return Number(lastBook.last);
      }
      var p = pos();
      if (p && p.last) return Number(p.last);
      return null;
    }

    function renderPosition() {
      var p = pos();
      if (!p) {
        posOut.textContent = "no position";
        posOut.className = "";
        holdOut.innerHTML = "<span class='muted'>flat in " + esc(symbol() || "—") + "</span>";
        return;
      }
      var q = Number(p.qty) || 0;
      var avg = Number(p.avg_price) || 0;
      var last = Number(p.last != null ? p.last : mid()) || avg;
      var unreal = (last - avg) * q;
      var pct = avg ? ((last - avg) / Math.abs(avg)) * 100 : 0;
      posOut.innerHTML = (q > 0 ? "LONG " : "SHORT ") + num(Math.abs(q), 4) +
        " @ " + num(avg, 2) + " <span class='" + (unreal >= 0 ? "pos" : "neg") +
        "'>" + (unreal >= 0 ? "+" : "") + num(unreal, 2) + " (" +
        (pct >= 0 ? "+" : "") + num(pct, 2) + "%)</span>";
      var held = posEnteredAt
        ? Math.floor((Date.now() - posEnteredAt) / 1000) : null;
      holdOut.innerHTML = "<div class='kv'><span>qty</span><b>" +
        num(Math.abs(q), 4) + "</b></div>" +
        "<div class='kv'><span>avg</span><b>" + num(avg, 2) + "</b></div>" +
        "<div class='kv'><span>last</span><b>" + num(last, 2) + "</b></div>" +
        "<div class='kv'><span>unrealized</span><b class='" +
        (unreal >= 0 ? "pos' " : "neg' ") + ">" +
        (unreal >= 0 ? "+" : "") + num(unreal, 2) + "</b></div>" +
        (held != null ? "<div class='kv'><span>held</span><b>" + held + "s</b></div>" : "") +
        (p.realized_pnl ? "<div class='kv'><span>realized</span><b>" +
          num(p.realized_pnl, 2) + "</b></div>" : "");
    }

    function renderEstimate() {
      var s = symbol();
      var q = qty();
      if (!s) { estOut.textContent = ""; return; }
      if (!isFinite(q) || q <= 0) {
        estOut.innerHTML = "<span class='error'>qty must be greater than 0</span>";
        return;
      }
      var m = mid();
      if (m == null) { estOut.textContent = ""; return; }
      var notional = m * q;
      var bits = ["≈ " + num(notional, 2) + " notional"];
      if (typeEl.value === "limit" && priceIn.value) {
        bits.push("limit " + num(Number(priceIn.value), 2));
      }
      if (modeEl.value === "paper") {
        if (portfolio && portfolio.cash != null) {
          var cash = Number(portfolio.cash);
          if (notional > cash) {
            estOut.innerHTML = "<span class='error'>exceeds cash " +
              num(cash, 2) + "</span>";
            return;
          }
          bits.push("cash after ≈ " + num(cash - notional, 2));
        }
      } else {
        bits.push("LIVE — real order");
      }
      var p = pos();
      if (p && Number(p.qty) < 0 && !bits.length) bits.push("would open long");
      estOut.textContent = bits.join(" · ");
    }

    function renderPositions() {
      if (!portfolio) return;
      var list = portfolio.positions || [];
      if (!list.length) {
        posHost.innerHTML = "<span class='muted'>no open positions</span>";
        return;
      }
      posHost.innerHTML = "<table><tr><th>symbol</th><th>qty</th><th>avg</th>" +
        "<th>last</th><th>unreal</th><th></th></tr>" + list.map(function (p) {
          var q = Number(p.qty) || 0;
          var u = Number(p.unrealized_pnl) || 0;
          return "<tr><td>" + esc(p.symbol) + "</td><td class='" +
            (q >= 0 ? "pos' " : "neg' ") + ">" + (q >= 0 ? "+" : "") +
            num(q, 4) + "</td><td>" + num(p.avg_price, 2) + "</td><td>" +
            num(p.last, 2) + "</td><td class='" + (u >= 0 ? "pos" : "neg") +
            "'>" + (u >= 0 ? "+" : "") + num(u, 2) + "</td>" +
            "<td><button type='button' data-close='" + esc(p.symbol) + "'>Close</button></td></tr>";
        }).join("") + "</table>";
      Array.prototype.forEach.call(
        posHost.querySelectorAll("button[data-close]"), function (b) {
          b.onclick = function () {
            var sym = b.getAttribute("data-close");
            var p = (portfolio.positions || []).filter(function (x) {
              return x.symbol === sym;
            })[0];
            if (!p) return;
            closePosition(p);
          };
        });
    }

    function renderFills() {
      if (!portfolio) return;
      var f = portfolio.fills || [];
      if (!f.length) { fillHost.innerHTML = "<span class='muted'>no fills yet</span>"; return; }
      fillHost.innerHTML = "<table><tr><th>symbol</th><th>side</th><th>qty</th>" +
        "<th>price</th></tr>" + f.slice(0, 12).map(function (x) {
          return "<tr><td>" + esc(x.symbol) + "</td><td class='" +
            (x.side === "buy" ? "pos" : "neg") + "'>" + esc(x.side) +
            "</td><td>" + num(x.qty, 4) + "</td><td>" + num(x.price, 2) + "</td></tr>";
        }).join("") + "</table>";
    }

    function loadPortfolio() {
      return api("GET", "/api/portfolio").then(function (p) {
        var hadPos = !!(portfolio && (portfolio.positions || []).length);
        portfolio = p;
        cashOut.textContent = "cash " + num(p.cash, 2);
        var canLive = !!p.live_trading_enabled;
        liveOut.textContent = canLive ? "live enabled" : "live disabled";
        liveOut.className = "badge " + (canLive ? "wired" : "locked");
        if (!canLive) {
          // Keep the option visible but say why it is closed, instead of
          // offering a mode that will 403 on submit.
          var opt = modeEl.querySelector("option[value='live']");
          if (opt) {
            opt.disabled = true;
            opt.textContent = "live (unavailable)";
          }
          if (modeEl.value === "live") modeEl.value = "paper";
        }
        var nowPos = (portfolio.positions || []).length;
        if (!hadPos && nowPos) posEnteredAt = Date.now();
        if (!nowPos) posEnteredAt = null;
        renderPosition();
        renderPositions();
        renderFills();
        renderEstimate();
        return p;
      }).catch(function (e) {
        posHost.innerHTML = showErr(e);
        throw e;
      });
    }

    // Live orders get a real second look. The old code set
    // confirm:true itself, which made the server's live gate a no-op:
    // one click fired a real order.
    function confirmOrder(side, payload) {
      var m = mid();
      confirmHost.innerHTML = "<div class='card danger-card'>" +
        "<h3>Confirm LIVE " + esc(side.toUpperCase()) + "</h3>" +
        "<div class='kv'><span>symbol</span><b>" + esc(payload.symbol) + "</b></div>" +
        "<div class='kv'><span>qty</span><b>" + num(payload.qty, 4) + "</b></div>" +
        "<div class='kv'><span>type</span><b>" + esc(payload.type) + "</b></div>" +
        (m != null ? "<div class='kv'><span>ref price</span><b>" + num(m, 2) +
          "</b></div>" : "") +
        "<p class='error'>This sends a real order. It cannot be undone here.</p>" +
        "<div class='row'><button type='button' class='danger' id='t-go'>" +
        "Yes, send it</button>" +
        "<button type='button' id='t-cancel'>Cancel</button></div></div>";
      document.getElementById("t-go").onclick = function () {
        confirmHost.innerHTML = "";
        // The API requires {confirm: true} on every live order, and this
        // button *is* the user's consent. Without setting it here the
        // request went out exactly as the user built it -- no confirm --
        // and the server refused it with 403, so clicking "Yes, send it"
        // on a real order did nothing whatsoever.
        payload.confirm = true;
        send(payload);
      };
      document.getElementById("t-cancel").onclick = function () {
        confirmHost.innerHTML = "";
      };
      var card = confirmHost.querySelector(".danger-card");
      if (card) card.scrollIntoView({ block: "nearest" });
    }

    function renderOrderResult(r) {
      // The API echoes the order both nested and flat; read the nested
      // form first so this keeps working if the flat echo is dropped.
      var o = r.order || r;
      var f = r.fill || {};
      var side = o.side || r.side;
      var bits = ["<div class='sig sig-" +
        (side === "buy" ? "buy" : "sell") + "'><span class='sig-label'>" +
        (side === "buy" ? "BUY" : "SELL") + "</span><div class='sig-body'>" +
        "<div class='kv'><span>order</span><b>#" + esc(o.id != null ? o.id : "?") +
        "</b></div>" +
        "<div class='kv'><span>status</span><b>" + esc(o.status || "—") + "</b></div>" +
        "<div class='kv'><span>qty</span><b>" + num(o.qty, 4) + "</b></div>" +
        (f.price != null ? "<div class='kv'><span>fill</span><b>" +
          num(f.price, 2) + "</b></div>" : "") +
        (f.fee != null ? "<div class='kv'><span>fee</span><b>" +
          num(f.fee, 4) + "</b></div>" : "") +
        (f.cash_after != null ? "<div class='kv'><span>cash after</span><b>" +
          num(f.cash_after, 2) + "</b></div>" : "") +
        "</div></div>"];
      if (o.error) bits.push("<p class='error'>" + esc(o.error) + "</p>");
      return bits.join("");
    }

    function send(payload) {
      out("<p class='muted'>placing…</p>");
      api("POST", "/api/orders", payload)
        .then(function (r) {
          out(renderOrderResult(r));
          return loadPortfolio();
        })
        .catch(function (e) { out(showErr(e)); });
    }

    function order(side) {
      var s = symbol();
      var q = qty();
      if (!s) { out("<p class='error'>symbol required</p>"); return; }
      if (!isFinite(q) || q <= 0) {
        out("<p class='error'>qty must be greater than 0</p>");
        return;
      }
      var payload = { symbol: s, side: side, type: typeEl.value, qty: q,
                      mode: modeEl.value };
      if (typeEl.value === "limit") {
        var lp = parseFloat(priceIn.value);
        if (!isFinite(lp) || lp <= 0) {
          out("<p class='error'>limit orders need a price</p>");
          return;
        }
        payload.price = lp;
      }
      // Warn before a sell that opens a short rather than closing a long.
      var p = pos();
      if (side === "sell" && !p) {
        out("<p class='muted'>no " + esc(s) + " position — this opens a SHORT</p>");
      }
      if (modeEl.value === "live") { confirmOrder(side, payload); return; }
      send(payload);
    }

    function closePosition(p) {
      var q = Number(p.qty) || 0;
      var side = q > 0 ? "sell" : "buy";
      var payload = { symbol: p.symbol, side: side, qty: Math.abs(q),
                      type: "market", mode: modeEl.value };
      if (modeEl.value === "live") { confirmOrder(side, payload); return; }
      out("<p class='muted'>closing " + esc(p.symbol) + "…</p>");
      api("POST", "/api/orders", payload)
        .then(function (r) {
          out(renderOrderResult(r));
          return loadPortfolio();
        })
        .catch(function (e) { out(showErr(e)); });
    }

    function flattenAll() {
      var list = (portfolio && portfolio.positions) || [];
      if (!list.length) { out("<p class='muted'>already flat</p>"); return; }
      var names = list.map(function (p) { return p.symbol; }).join(", ");
      out("<p class='muted'>flattening " + esc(names) + "…</p>");
      api("POST", "/api/positions/flatten", { mode: modeEl.value })
        .then(function (r) {
          var bits = ["<div class='sig sig-flat'><span class='sig-label'>FLAT</span>" +
            "<div class='sig-body'><div class='kv'><span>closed</span><b>" +
            esc(r.count != null ? r.count : (r.closed || []).length) + "</b></div>" +
            "</div></div>"];
          // A partial flatten must be loud: anything still open is exposure.
          (r.rejected || []).forEach(function (x) {
            bits.push("<p class='error'>still open: " + esc(x.symbol) + " × " +
              num(x.qty, 4) + " — " + esc(x.reason) + " (" + esc(x.stage) + ")</p>");
          });
          out(bits.join(""));
          return loadPortfolio();
        })
        .catch(function (e) {
          // 409 = nothing closed, everything was refused. Show why.
          if (e.status === 409 && e.data && e.data.rejected) {
            out("<p class='error'>nothing closed — every position was refused:</p>" +
              e.data.rejected.map(function (x) {
                return "<p class='error'>" + esc(x.symbol) + " × " + num(x.qty, 4) +
                  " — " + esc(x.reason) + "</p>";
              }).join(""));
          } else {
            out(showErr(e));
          }
          loadPortfolio();
        });
    }

    document.getElementById("t-buy").onclick = function () { order("buy"); };
    document.getElementById("t-sell").onclick = function () { order("sell"); };
    document.getElementById("t-flat").onclick = flattenAll;
    document.getElementById("t-refresh").onclick = function () { loadPortfolio(); };

    function syncType() {
      priceWrap.hidden = typeEl.value !== "limit";
    }
    typeEl.onchange = function () { syncType(); renderEstimate(); };
    modeEl.onchange = renderEstimate;
    priceIn.oninput = renderEstimate;
    qtyEl.oninput = renderEstimate;
    symEl.oninput = function () {
      lastBook = state.quotes[symbol()] || null;
      renderPrice();
      renderPosition();
      renderEstimate();
      loadChart();          // the chart follows the ticket's symbol
    };
    symEl.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); } };
    syncType();

    function renderPrice() {
      var q = lastBook || state.quotes[symbol()];
      if (!q) {
        priceOut.innerHTML = "<span class='stale'>no quote</span>";
        return;
      }
      var last = q.last != null ? q.last : q.ask;
      var txt = num(last, 2);
      if (q.bid != null && q.ask != null) {
        txt += "  " + num(q.bid, 2) + " × " + num(q.ask, 2);
      }
      if (q.chg_pct != null) {
        var c = Number(q.chg_pct);
        txt += "  " + (c >= 0 ? "+" : "") + num(c, 2) + "%";
      }
      priceOut.textContent = txt;
    }

    // -- live chart for the ticket symbol --------------------------------
    //
    // A ticket without a chart makes the operator trade on a number with
    // no context. This loads the same stored 1m tape the analysis page
    // uses and then extends the *last* candle from the SSE tick, so the
    // right-hand edge moves while you watch instead of the chart being
    // frozen until a manual reload.
    var raw = [], closes = [];
    var cvEl = document.getElementById("t-cv");
    var mktEl = document.getElementById("t-mkt");
    var cstatsEl = document.getElementById("t-cstats");
    var cerrEl = document.getElementById("t-cerr");
    var barsEl = document.getElementById("t-bars");
    var chartSym = null;

    function ovOn(id) {
      var el = document.getElementById(id);
      return !!(el && el.checked);
    }

    function renderChart() {
      if (!cvEl || !raw.length) return;
      try {
        drawCandles(cvEl, raw,
          ovOn("t-ov-e9") ? emaArr(closes, 9) : null,
          ovOn("t-ov-e21") ? emaArr(closes, 21) : null,
          ovOn("t-ov-vw") ? vwapArr(raw) : null,
          { title: chartSym || symbol() });
      } catch (e) {
        if (cerrEl) cerrEl.innerHTML = showErr(e);
      }
    }

    function renderMarketInfo() {
      if (!mktEl || !raw.length) return;
      var first = closes[0], last = closes[closes.length - 1];
      if (!(first > 0) || !(last > 0)) return;
      var hi = -Infinity, lo = Infinity, totV = 0;
      for (var i = 0; i < raw.length; i++) {
        var h = raw[i].h != null ? raw[i].h : raw[i].high;
        var l = raw[i].l != null ? raw[i].l : raw[i].low;
        var v = raw[i].v != null ? raw[i].v : raw[i].volume;
        if (h != null && h > hi) hi = h;
        if (l != null && l < lo) lo = l;
        if (v != null) totV += v;
      }
      var chg = last - first, pct = first ? chg / first * 100 : 0;
      var q0 = raw[0] || {};
      var o0 = q0.o != null ? q0.o : q0.open;
      var b = lastBook || {};
      var spread = b.spread_bps;
      function cell(k, v, cls) {
        return "<div class='quote-cell'><div class='k'>" + esc(k) +
          "</div><div class='v " + (cls || "") + "'>" + v + "</div></div>";
      }
      mktEl.innerHTML =
        cell("Last", num(last)) +
        cell("Chg", (pct >= 0 ? "+" : "") + num(pct) + "%",
             pct >= 0 ? "up" : "down") +
        cell("Bid", b.bid != null ? num(b.bid) : "—") +
        cell("Ask", b.ask != null ? num(b.ask) : "—") +
        cell("Spread", spread != null ? num(spread) + " bps" : "—") +
        cell("Open", o0 != null ? num(o0) : "—") +
        cell("High", hi > -Infinity ? num(hi) : "—") +
        cell("Low", lo < Infinity ? num(lo) : "—") +
        cell("Volume", num(totV, 0));
      if (cstatsEl) {
        cstatsEl.textContent = "n=" + raw.length + " · range " + num(lo) +
          "–" + num(hi) + " · 1m bars";
      }
    }

    function loadChart() {
      var s = symbol();
      if (!s) return;
      chartSym = s;
      if (cerrEl) cerrEl.innerHTML = "";
      if (cstatsEl) cstatsEl.textContent = "loading chart…";
      var lim = (barsEl && barsEl.value) || 180;
      api("GET", "/api/candles?symbol=" + encodeURIComponent(s) +
          "&tf=1m&limit=" + encodeURIComponent(lim))
        .then(function (d) {
          var rows = d.candles || d.rows || [];
          if (chartSym !== symbol()) return;   // symbol changed mid-flight
          if (!rows.length) {
            raw = []; closes = [];
            if (cstatsEl) {
              cstatsEl.innerHTML = "<span class='error'>no stored candles for " +
                esc(s) + "</span>";
            }
            if (mktEl) mktEl.innerHTML = "";
            return;
          }
          raw = rows;
          closes = raw.map(clOf);
          renderChart();
          renderMarketInfo();
        })
        .catch(function (e) {
          if (cerrEl) cerrEl.innerHTML = showErr(e);
          if (cstatsEl) cstatsEl.textContent = "chart unavailable";
        });
    }

    // Fold an SSE tick into the last candle so the chart's right edge
    // tracks the market. Only the final bar is touched -- rewriting
    // history from a tick would be a lie about what was stored.
    function foldTick(q) {
      if (!raw.length || !q) return false;
      var px = q.last != null ? Number(q.last)
        : (q.ask != null ? Number(q.ask) : NaN);
      if (!isFinite(px) || px <= 0) return false;
      var last = raw[raw.length - 1];
      last.c = px;
      if (q.bid != null) last.o = Number(q.bid);
      else if (last.o == null) last.o = px;
      last.h = Math.max(last.h != null ? Number(last.h) : px, px,
                        q.bid != null ? Number(q.bid) : px,
                        q.ask != null ? Number(q.ask) : px);
      last.l = Math.min(last.l != null ? Number(last.l) : px, px,
                        q.bid != null ? Number(q.bid) : px,
                        q.ask != null ? Number(q.ask) : px);
      closes[closes.length - 1] = px;
      return true;
    }

    if (barsEl) barsEl.onchange = loadChart;
    ["t-ov-e9", "t-ov-e21", "t-ov-vw"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.onchange = renderChart;
    });
    var reloadBtn = document.getElementById("t-refresh-mkt");
    if (reloadBtn) reloadBtn.onclick = loadChart;
    loadChart();

    // SSE drives the live price and the unrealized P&L so the ticket
    // reflects the market without a refresh.
    startSSE(function (q) {
      if (String(q.symbol || "").toUpperCase() === symbol()) {
        lastBook = q;
        renderPrice();
        // Advance the chart's last bar and its market panel from the
        // same tick that moves the price, so the whole view stays live.
        if (foldTick(q)) {
          renderChart();
          renderMarketInfo();
        }
        if (portfolio) {
          // Re-price the local position view from the tick.
          var p = pos();
          if (p) {
            var last = Number(q.last != null ? q.last : q.ask);
            var avg = Number(p.avg_price) || 0;
            p.last = last;
            p.unrealized_pnl = (last - avg) * (Number(p.qty) || 0);
          }
          renderPosition();
          renderEstimate();
        }
      }
    });

    renderPrice();
    loadPortfolio().catch(function () { /* rendered in positions panel */ });
    setInterval(function () {
      if (posEnteredAt) renderPosition();   // advance the held timer
    }, 1000);

    // Pre-fill the size. An explicit ?qty= from the link wins; otherwise
    // use the size of the position being acted on, so "sell" means
    // "close this" rather than "sell 1 unit of a 0.003 position".
    if (presetQty != null && presetQty !== "" && isFinite(Number(presetQty))
        && Number(presetQty) > 0) {
      qtyEl.value = String(Number(presetQty));
    } else if (preset) {
      loadPortfolio().then(function () {
        var p = pos();
        if (p && Math.abs(Number(p.qty)) > 0) {
          qtyEl.value = String(Math.abs(Number(p.qty)));
          renderEstimate();
        }
      }).catch(function () { /* size stays 1 */ });
    }
    if (armedSide) {
      var btn = document.getElementById(armedSide === "buy" ? "t-buy" : "t-sell");
      if (btn) {
        btn.classList.add("armed");
        btn.setAttribute("aria-pressed", "true");
        out("<p class='muted'>" + esc(symbol()) + " " +
          esc(armedSide) + " armed — review the size, then click " +
          esc(armedSide === "buy" ? "Buy" : "Sell") + " to send it. " +
          "Nothing is sent until you do.</p>");
        try { btn.focus({ preventScroll: true }); btn.scrollIntoView({ block: "nearest" }); }
        catch (e) { btn.focus(); }
      }
    }

    api("GET", "/api/symbols")
      .then(function (r) {
        var dl = document.getElementById("t-sym-list");
        var items = (r && (r.symbols || r.items)) || [];
        if (!dl) return;
        dl.innerHTML = items.map(function (s) {
          var sym = typeof s === "string" ? s : (s.symbol || s.name);
          return sym ? "<option value='" + esc(sym) + "'></option>" : "";
        }).join("");
      })
      .catch(function () { /* autocomplete is optional */ });
  }

  function pageBacktests() {
    hideLogin();
    view().innerHTML = "<h2>Backtests</h2><div class='card'><h3>Run</h3><div class='row'>" +
      "<select id='b-strat'>" +
      "<option value='ema_trend'>ema_trend</option>" +
      "<option value='ema_trend_cot'>EMA Trend + COT crowding gate</option>" +
      "<option value='vwap_pullback'>VWAP Pullback</option>" +
      "<option value='vwap_bands'>VWAP Band (session, sigma)</option>" +
      "<option value='manual'>Manual</option>" +
      "<option value='orb_breakout'>Opening Range Breakout</option>" +
      "<option value='rsi_fade'>RSI Fade</option>" +
      "<option value='dual_ema'>Dual EMA Cross</option>" +
      "<option value='mean_reversion'>Mean Reversion</option>" +
      "</select>" +
      "<label>Symbol <input id='b-sym' value='BTCUSD'></label>" +
      "<button id='b-go' type='button'>Run backtest</button></div><div id='b-out'></div></div>" +
      "<h3>Results</h3><div id='b-list'></div><canvas class='chart' id='b-cv'></canvas>";
    document.getElementById("b-go").onclick = function () {
      var out = document.getElementById("b-out");
      out.textContent = "running…";
      api("POST", "/api/backtests", {
        strategy: document.getElementById("b-strat").value,
        symbol: document.getElementById("b-sym").value
      }).then(function (r) {
        var id = r.id != null ? r.id : (r.backtest && r.backtest.id);
        out.textContent = "created #" + id;
        if (id) loadOne(id);
      }).catch(function (e) { out.innerHTML = showErr(e); });
    };
    // The verdict classes line up with the strings
    // engines/significance.py emits. An unrecognised verdict is never
    // styled green: the whole point of the block is that "no edge after
    // costs" and "not evaluable" cannot be mistaken for approval.
    function verdictTone(v) {
      if (v === "edge likely real") return "edge";
      if (v === "edge plausible, unproven") return "maybe";
      return "none";
    }
    function renderVerdict(sig) {
      if (!sig || typeof sig !== "object") return "";
      var tone = verdictTone(sig.verdict);
      var num = function (x, dp) {
        return (x === null || x === undefined) ? "—" :
          esc(Number(x).toFixed(dp === undefined ? 3 : dp));
      };
      var kvs = [
        ["DSR (deflated)", num(sig.dsr)],
        ["PSR", num(sig.psr)],
        ["net Sharpe", num(sig.net_sharpe, 2)],
        ["expectancy", num(sig.expectancy_bps, 2) + " bps/trade"],
        ["trades", esc(sig.trades)],
        ["trials deflated for", esc(sig.n_trials)],
        ["modelled cost drag", num(sig.cost_drag_bps, 1) + " bps/trade"]
      ].map(function (kv) {
        return "<div class='kv'><span>" + esc(kv[0]) + "</span><b>" + kv[1] + "</b></div>";
      }).join("");
      var why = (sig.reasons || []).map(function (x) {
        return "<li>" + esc(x) + "</li>";
      }).join("");
      return "<div class='sig sig-verdict tone-" + tone + "'>" +
        "<div class='sig-label'>" + esc(String(sig.verdict || "unknown")) + "</div>" +
        "<div class='sig-body'><div class='metrics'>" + kvs + "</div>" +
        (why ? "<ul class='verdict-why'>" + why + "</ul>" : "") +
        "</div></div>";
    }
    // The window is a permission, not a promise: a symbol with 30 days of
    // stored history answers a 180-day request with 30 days. Saying so is
    // the difference between a short backtest and a backtest that looks
    // like a long one.
    function renderCoverage(cov) {
      if (!cov || typeof cov !== "object") return "";
      var cls = cov.short_of_window ? "cov-short" :
                (cov.truncated_by_cap ? "cov-capped" : "");
      var bits = [];
      bits.push("<div class='kv'><span>tape used</span><b>" +
        esc(cov.days) + " days (" + esc(cov.bars) + " bars)</b></div>");
      bits.push("<div class='kv'><span>window</span><b>" +
        esc(cov.requested_days) + " days</b></div>");
      if (cov.note) bits.push("<div class='kv'><span>note</span><b>" +
        esc(cov.note) + "</b></div>");
      return "<div class='coverage " + cls + "'><div class='metrics'>" +
        bits.join("") + "</div></div>";
    }
    function loadOne(id) {
      api("GET", "/api/backtests/" + encodeURIComponent(id)).then(function (r) {
        var m = r.metrics || r;
        var eq = r.equity || r.equity_curve || [];
        var title = "Backtest #" + esc(id) + " — " + esc(m.strategy || r.strategy || "?") +
          " on " + esc(m.symbol || r.symbol || "?") + " (" + esc(m.candles || "?") + " candles)";
        var sig = m.significance || null;
        var cov = m.coverage || null;
        // The verdict is a labelled panel, not a row in the metrics
        // table: it is the answer to "should I trade this?", and it has
        // to be the first thing read, not the 14th row of numbers.
        var skip = { significance: 1, coverage: 1 };
        var rows = Object.keys(m).filter(function (k) { return !skip[k]; })
          .map(function (k) {
          var v = m[k];
          var s = (v != null && typeof v === "object") ? JSON.stringify(v) : String(v);
          var cls = "";
          if (/^(win_rate|total_return|cagr)$/.test(k) && parseFloat(v) > 0) cls = " class='pos'";
          if (/^(max_drawdown|maxDD)$/.test(k) && parseFloat(v) < 0) cls = " class='neg'";
          return "<tr><td>" + esc(k) + "</td><td" + cls + ">" + esc(s) + "</td></tr>";
        }).join("");
        document.getElementById("b-list").innerHTML = "<h3>" + title + "</h3>" +
          renderVerdict(sig) +
          renderCoverage(cov) +
          (r.equity_downsampled
            ? "<p class='kpi-l'>equity curve strided to " + eq.length +
              " points; full run had more</p>"
            : "") +
          "<table><tr><th>Metric</th><th>Value</th></tr>" + rows + "</table>" +
          "<p class='kpi-l'>params: " + esc(JSON.stringify(r.params || m.params || {})) + "</p>";
        try {
          var cv = document.getElementById("b-cv");
          var pts = eq.map(function (p) { return { o: p, h: p, l: p, c: p }; });
          drawCandles(cv, pts.length ? pts : [{ o: 0, h: 1, l: 0, c: 1 }], null, null, null);
        } catch (e) { /* chart optional */ }
      }).catch(function (e) { document.getElementById("b-list").innerHTML = showErr(e); });
    }
  }

  // ---- all-assets browser ----------------------------------------------
  //
  // The Dashboard and Portfolio both showed positions and nothing else,
  // so with a flat book the operator had no way to reach the other 38
  // instruments from either page -- no price, no chart, no way to act.
  // "Positions" answers "what do I hold"; this answers "what can I
  // trade", and the two are different questions.
  //
  // Renders the whole enabled universe with a live price, the change
  // over the visible window, and the same chart/buy/sell actions the
  // position rows carry, so acting on an asset you do not hold is the
  // same two clicks as acting on one you do.
  var assetState = { rows: [], filter: "", type: "all", sort: "symbol",
                     asc: true, limit: 0 };

  function assetTypeOptions() {
    var seen = {};
    assetState.rows.forEach(function (r) {
      if (r.asset_type) seen[r.asset_type] = 1;
    });
    return Object.keys(seen).sort();
  }

  function assetFilterRows() {
    var f = (assetState.filter || "").trim().toUpperCase();
    var t = assetState.type || "all";
    var rows = assetState.rows.filter(function (r) {
      if (t !== "all" && r.asset_type !== t) return false;
      if (!f) return true;
      return String(r.symbol).toUpperCase().indexOf(f) >= 0 ||
             String(r.name || "").toUpperCase().indexOf(f) >= 0;
    });
    var dir = assetState.asc ? 1 : -1;
    var key = assetState.sort;
    rows.sort(function (a, b) {
      var x = a[key], y = b[key];
      if (key === "symbol" || key === "asset_type") {
        return String(x || "").localeCompare(String(y || "")) * dir;
      }
      x = (x == null || !isFinite(x)) ? -Infinity : Number(x);
      y = (y == null || !isFinite(y)) ? -Infinity : Number(y);
      return (x - y) * dir;
    });
    return rows;
  }

  function renderAssets() {
    var host = document.getElementById("as-out");
    if (!host) return;
    var rows = assetFilterRows();
    var shown = assetState.limit > 0 ? rows.slice(0, assetState.limit) : rows;
    var types = assetTypeOptions();
    var head = "<div class='row as-ctl'>" +
      "<input id='as-q' placeholder='filter symbol or name' value='" +
      esc(assetState.filter) + "' autocomplete='off' spellcheck='false'>" +
      "<select id='as-type'><option value='all'>all assets</option>" +
      types.map(function (t) {
        return "<option value='" + esc(t) + "'" +
          (assetState.type === t ? " selected" : "") + ">" + esc(t) +
          "</option>";
      }).join("") + "</select>" +
      "<span class='dim'>" + rows.length + " of " + assetState.rows.length +
      "</span>" +
      (assetState.limit && rows.length > assetState.limit
        ? "<button type='button' id='as-more'>show all</button>"
        : (assetState.limit
            ? "<button type='button' id='as-less'>show less</button>" : "")) +
      "</div>";

    if (!shown.length) {
      host.innerHTML = head +
        "<p class='muted'>no asset matches “" + esc(assetState.filter) +
        "”</p>";
      wireAssets();
      return;
    }
    function th(key, label) {
      var cls = assetState.sort === key ? " class='sorted" +
        (assetState.asc ? " asc" : " desc") + "'" : "";
      return "<th class='sortable as-th" + cls +
        "' data-as-sort='" + esc(key) + "'>" + esc(label) + "</th>";
    }
    host.innerHTML = head +
      "<table class='as-table'><tr>" +
      th("symbol", "Symbol") + th("asset_type", "Class") +
      th("last", "Last") + th("change_pct", "Chg%") +
      th("spread_bps", "Spread") + th("volume", "Volume") +
      "<th>Action</th></tr>" +
      shown.map(function (r) {
        var chg = r.change_pct;
        return "<tr>" +
          "<td><a href='#/chart/" + encodeURIComponent(r.symbol) + "'>" +
          esc(r.symbol) + "</a>" +
          (r.name ? " <span class='dim'>" + esc(r.name) + "</span>" : "") + "</td>" +
          "<td class='dim'>" + esc(r.asset_type || "") + "</td>" +
          "<td>" + num(r.last) + "</td>" +
          "<td class='" + (chg > 0 ? "pos" : (chg < 0 ? "neg" : "")) + "'>" +
          (chg == null ? "—" : (chg >= 0 ? "+" : "") + num(chg) + "%") + "</td>" +
          "<td>" + (r.spread_bps == null ? "—" : num(r.spread_bps)) + "</td>" +
          "<td>" + (r.volume == null ? "—" : num(r.volume, 0)) + "</td>" +
          "<td>" + positionActions(r.symbol, {}) + "</td>" +
          "</tr>";
      }).join("") + "</table>";
    wireAssets();
  }

  function wireAssets() {
    var q = document.getElementById("as-q");
    if (q) {
      q.oninput = function () {
        assetState.filter = q.value;
        assetState.limit = 25;
        renderAssets();
      };
    }
    var ty = document.getElementById("as-type");
    if (ty) {
      ty.onchange = function () {
        assetState.type = ty.value;
        assetState.limit = 25;
        renderAssets();
      };
    }
    Array.prototype.forEach.call(
      (view() || document).querySelectorAll("[data-as-sort]"), function (th) {
        th.onclick = function () {
          var k = th.getAttribute("data-as-sort");
          if (assetState.sort === k) assetState.asc = !assetState.asc;
          else { assetState.sort = k; assetState.asc = true; }
          renderAssets();
        };
      });
    var more = document.getElementById("as-more");
    if (more) more.onclick = function () { assetState.limit = 0; renderAssets(); };
    var less = document.getElementById("as-less");
    if (less) less.onclick = function () { assetState.limit = 25; renderAssets(); };
  }

  // Load the whole universe. One request for all symbols, not one per
  // symbol. /api/quotes carries only the fields this table shows;
  // /api/screener returned a 200-element closes array per symbol (208KB,
  // ~2.7s for 39 symbols) and the browser used none of it.
  function mountAssets(sectionId, opts) {
    opts = opts || {};
    var host = document.getElementById(sectionId);
    if (!host) return;
    host.innerHTML = "<div id='as-out'><p class='muted'>loading universe…</p></div>";
    assetState.limit = 25;
    api("GET", "/api/quotes").then(function (d) {
      var rows = d.quotes || d.rows || d.results || [];
      assetState.rows = rows.map(function (r) {
        return {
          symbol: r.symbol, name: r.name, asset_type: r.asset_type,
          exchange: r.exchange, last: r.last,
          // The screener exposes momentum_pct / session_pct, not
          // change_pct, and momentum_pct is the populated one. Reading
          // change_pct rendered an empty Chg% column on every row.
          change_pct: r.change_pct,
          prev_close: r.prev_close,
          spread_bps: r.spread_bps, volume: r.volume,
          anomaly_score: r.anomaly_score
        };
      }).filter(function (r) { return r.symbol; });
      renderAssets();
    }).catch(function (e) {
      var out = document.getElementById("as-out");
      if (out) out.innerHTML = showErr(e);
    });
  }

  // Position action links.
  //
  // Every position row carries three ways out of it: open the chart for
  // that instrument, or go straight to the ticket pre-loaded to buy or
  // sell it. On a screen with 30 open positions and a tape scrolling
  // underneath, retyping a symbol into a ticket is the slowest possible
  // path from "I want to act on this row" to "I am acting on it".
  //
  // The buy/sell links only *arm* the ticket -- see pageScalpLive. A
  // link that placed an order on navigation would be a trap.
  function positionActions(symbol, opts) {
    opts = opts || {};
    var s = esc(symbol);
    var sym = encodeURIComponent(symbol);
    var mode = opts.mode || "";
    var extra = opts.qty != null ? "?qty=" + encodeURIComponent(opts.qty) : "";
    // No "chart" chip here: the symbol in the row is already the chart
    // link, and a second identical link on the same row is just noise.
    return "<span class='pos-actions'>" +
      "<a class='pa buy' href='#/scalp-live/" + sym + "/buy" + extra +
      "' title='Buy " + s + "'>buy</a>" +
      "<a class='pa sell' href='#/scalp-live/" + sym + "/sell" + extra +
      "' title='Sell " + s + "'>sell</a>" +
      (mode ? "<span class='pa-mode'>" + esc(mode) + "</span>" : "") +
      "</span>";
  }

  // Standard position table used by Dashboard and Portfolio so the two
  // cannot drift apart again.
  function positionRows(list, opts) {
    opts = opts || {};
    list = list || [];
    if (!list.length) {
      return "<p class='muted'>" + esc(opts.empty || "no open positions") + "</p>";
    }
    return "<table class='pos-table'><tr><th>Symbol</th><th>Side</th><th>Qty</th>" +
      "<th>Avg</th><th>Last</th><th>Unreal</th>" +
      (opts.showRealized ? "<th>Realized</th>" : "") +
      "<th>Action</th></tr>" +
      list.map(function (p) {
        var q = Number(p.qty) || 0;
        var u = Number(p.unrealized_pnl) || 0;
        var last = p.last != null ? p.last : p.avg_price;
        return "<tr>" +
          "<td><a href='#/chart/" + encodeURIComponent(p.symbol) + "'>" +
          esc(p.symbol) + "</a>" +
          (p.name ? " <span class='dim'>" + esc(p.name) + "</span>" : "") + "</td>" +
          "<td>" + sideChip(q) + "</td>" +
          "<td>" + num(q) + "</td>" +
          "<td>" + num(p.avg_price) + "</td>" +
          "<td>" + num(last) + "</td>" +
          "<td class='" + (u > 0 ? "pos" : (u < 0 ? "neg" : "")) + "'>" +
          (u > 0 ? "+" : "") + num(u) + "</td>" +
          (opts.showRealized
            ? "<td>" + num(p.realized_pnl) + "</td>" : "") +
          "<td>" + positionActions(p.symbol, {
            qty: Math.abs(q) || null,
            mode: opts.mode
          }) + "</td>" +
          "</tr>";
      }).join("") + "</table>";
  }

  function pagePortfolio() {
    hideLogin();
    view().innerHTML = "<h2>Portfolio</h2><div id='out'>loading…</div>";
    Promise.all([
      api("GET", "/api/portfolio"),
      api("GET", "/api/orders?limit=10").catch(function () { return { orders: [] }; })
    ]).then(function (res) {
      var p = res[0] || {}, sum = p.summary || {};
      var tot = sum.total_pnl, totCls = tot > 0 ? "up" : (tot < 0 ? "down" : "");
      var cash = p.cash != null ? +p.cash : 0;
      var html = "<div class='cards'>" +
        "<div class='card kpi'><h3>Cash Balance</h3><div class='kpi-v'>" + num(cash) +
        "</div><div class='kpi-l'>" + esc(p.tier || "paper") + " account</div></div>" +
        "<div class='card kpi'><h3>Total P&amp;L</h3><div class='kpi-v " + totCls + "'>" +
        (tot > 0 ? "+" : "") + num(tot) + "</div><div class='kpi-l'>paper account</div></div>" +
        "<div class='card kpi'><h3>Realized</h3><div class='kpi-v'>" + num(sum.realized_pnl) +
        "</div><div class='kpi-l'>closed trades</div></div>" +
        "<div class='card kpi'><h3>Unrealized</h3><div class='kpi-v'>" + num(sum.unrealized_pnl) +
        "</div><div class='kpi-l'>open positions</div></div></div>";
      html += "<h3>Positions</h3>" + positionRows(p.positions, {
        showRealized: true,
        mode: p.live_trading_enabled ? "LIVE" : "paper",
        empty: "no open positions — the book is flat"
      });
      html += "<h3>All assets</h3><div id='port-assets'></div>";
      html += "<h3>Fills (latest 50)</h3><table><tr><th>ID</th><th>Order</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Fee</th><th>Time</th></tr>" +
        ((p.fills || []).map(function (f) {
          var qf = +f.qty || 0;
          return "<tr><td>" + esc(f.id) + "</td><td>" + esc(f.order_id) + "</td><td>" + esc(f.symbol) +
            "</td><td>" + (qf >= 0 ? "buy" : "sell") + "</td><td>" + num(Math.abs(qf)) + "</td><td>" +
            num(f.price) + "</td><td>" + num(f.fee) + "</td><td>" + esc(fmtTs(f.ts)) + "</td></tr>";
        }).join("")) + "</table>";
      var ords = res[1].orders || res[1].rows || [];
      html += "<h3>Recent orders</h3><table><tr><th>ID</th><th>Side</th><th>Qty</th><th>Symbol</th><th>Type</th><th>Status</th><th>Mode</th></tr>" +
        ords.map(function (o) {
          return "<tr><td>" + esc(o.id) + "</td><td>" + esc(o.side) + "</td><td>" + num(o.qty) +
            "</td><td>" + esc(o.symbol) + "</td><td>" + esc(o.type || o.order_type) + "</td><td>" +
            esc(o.status) + "</td><td>" + esc(o.mode) + "</td></tr>";
        }).join("") + "</table>";
      document.getElementById("out").innerHTML = html;
      mountAssets("port-assets");
    }).catch(function (e) { document.getElementById("out").innerHTML = showErr(e); });
  }

  function pageJournal() {
    hideLogin();
    // populate symbol options from known universe
    var knownSymbols = ["AAPL","MSFT","NVDA","BTCUSD","ETHUSD","EURUSD","XAUUSD","XAGUSD","WTI","BRENT","NGAS","COPPER","WHEAT","CORN","COFFEE","COCOA",
        "ALUM","NICKEL","ZINC","TIN","LEAD","GASOLINE","HEATOIL","SOYBEANS","SOYMEAL","SOYOIL","OATS","RRICE","SUGAR","COTTON","OJ","LCATTLE","LHOGS","FCATTLE"];
    var knownStrategies = ["ema_trend","ema_trend_cot","vwap_pullback","vwap_bands","manual","orb_breakout","rsi_fade","dual_ema","mean_reversion"];

    var symOpts = knownSymbols.map(function(s) { return "<option value='"+esc(s)+"'>"+esc(s)+"</option>"; }).join("");
    var stratOpts = knownStrategies.map(function(s) { return "<option value='"+esc(s)+"'>"+esc(s)+"</option>"; }).join("");

    view().innerHTML = "<h2>Journal</h2><div class='card'><h3>New entry</h3>" +
      "<select id='j-sym'>" + symOpts + "</select>" +
      "<select id='j-strat'>" + stratOpts + "</select>" +
      "<label>Note <textarea id='j-note'></textarea></label>" +
      "<button id='j-save' type='button'>Save</button><div id='j-out'></div></div>" +
      "<h3>Entries</h3><div id='j-list'>loading…</div>";
    function load() {
      api("GET", "/api/journal").then(function (r) {
        var list = r.entries || r.journal || r || [];
        document.getElementById("j-list").innerHTML = "<table><tr><th>TS</th><th>Symbol</th><th>Strategy</th><th>Note</th></tr>" +
          list.map(function (e) {
            return "<tr><td>" + esc(e.ts) + "</td><td>" + esc(e.symbol) + "</td><td>" +
              esc(e.strategy) + "</td><td>" + esc(e.note) + "</td></tr>";
          }).join("") + "</table>";
      }).catch(function (e) {
        // The old handler replaced every failure with a literal
        // "Failed to fetch", discarding the server's reason. A rate
        // limit, a 401 and a 500 all looked identical to the operator.
        document.getElementById("j-list").innerHTML = showErr(e);
      });
    }
    document.getElementById("j-save").onclick = function () {
      api("POST", "/api/journal", {
        symbol: document.getElementById("j-sym").value,
        strategy: document.getElementById("j-strat").value,
        note: document.getElementById("j-note").value
      }).then(function () { document.getElementById("j-out").textContent = "saved"; load(); })
        .catch(function (e) { document.getElementById("j-out").innerHTML = showErr(e); });
    };
    load();
  }

  function pageAdmin() {
    hideLogin();
    view().innerHTML = "<h2>Admin</h2><div id='out'>loading…</div>";
    Promise.all([
      api("GET", "/api/admin/overview").catch(function (e) { return { error: e.message }; }),
      api("GET", "/api/admin/users").catch(function (e) { return { error: e.message }; }),
      api("GET", "/api/admin/audit").catch(function (e) { return { error: e.message }; })
    ]).then(function (res) {
      var ov = res[0] || {};
      var users = res[1].users || res[1].rows || [];
      var audit = res[2].entries || res[2].audit || [];
      var err = res[0].error || res[1].error || res[2].error;
      var html = "";
      if (err) html = showErr(new Error(err));
      var kpis = [["Users", ov.users], ["Sessions", ov.sessions], ["Symbols", ov.symbols],
                  ["Candles", ov.candles], ["Quotes", ov.quotes], ["Orders", ov.orders],
                  ["Fills", ov.fills], ["Backtests", ov.backtests], ["Risk events", ov.risk_events],
                  ["Audit log", ov.audit]];
      html += "<div class='kpi-l'>" + kpis.map(function (k) {
        return "<span class='kpi'><b>" + (k[1] == null ? "—" : k[1]) + "</b> " + esc(k[0]) + "</span>";
      }).join("") + "</div>";
      html += "<h3>Users / tiers</h3><table><tr><th>ID</th><th>Email</th><th>Tier</th><th>Disabled</th></tr>" +
        users.map(function (u) {
          return "<tr><td>" + esc(u.id) + "</td><td>" + esc(u.email) + "</td><td>" +
            esc(u.tier) + "</td><td class='" + (u.disabled ? "neg" : "") + "'>" +
            (u.disabled ? "yes" : "no") + "</td></tr>";
        }).join("") + "</table>";
      html += "<h3>Audit</h3><table><tr><th>TS</th><th>Action</th><th>Actor</th></tr>" +
        audit.map(function (a) {
          return "<tr><td>" + esc(a.ts) + "</td><td>" + esc(a.action) + "</td><td>" + esc(a.actor_user_id) + "</td></tr>";
        }).join("") + "</table>";
      document.getElementById("out").innerHTML = html;
    });
  }

  function pageWallet() {
    hideLogin();
    view().innerHTML = "<h2>Wallet</h2>" +
      "<p class='kpi-l dim'>Read-only. Connects to an injected browser wallet " +
      "to read balances. This app never requests a seed phrase or private key, " +
      "and cannot sign or send anything.</p>" +
      "<div class='row' style='margin:.6em 0'>" +
      "  <button id='wallet-connect' type='button'>Connect MetaMask</button>" +
      "  <button id='wallet-disconnect' type='button' style='display:none'>Disconnect</button>" +
      "</div>" +
      "<div id='wallet-status' class='wallet-status'></div>" +
      "<div id='wallet-panel' style='display:none'>" +
      "  <div class='card'>" +
      "    <h4>Connected account</h4>" +
      "    <table class='kv'>" +
      "      <tr><td>Address</td><td><span id='wallet-address'>-</span> " +
      "        <a id='wallet-explorer' href='#' target='_blank' rel='noopener noreferrer' " +
      "           style='display:none'>view on explorer</a></td></tr>" +
      "      <tr><td>Network</td><td><span id='wallet-chain'>-</span></td></tr>" +
      "      <tr><td>USDT balance</td><td id='wallet-usdt'>-</td></tr>" +
      "      <tr><td>Native balance</td><td id='wallet-native'>-</td></tr>" +
      "    </table>" +
      "    <div class='row' style='margin-top:.8em'>" +
      "      <label class='dim'>Network</label>" +
      "      <select id='wallet-chain-select'></select>" +
      "      <button id='wallet-switch-chain' type='button'>Switch</button>" +
      "      <button id='wallet-add-chain' type='button' style='display:none'>Add to MetaMask</button>" +
      "    </div>" +
      "    <p class='kpi-l dim' style='margin-top:.8em'>" +
      "      Balances are read with <code>eth_call</code> through your own wallet. " +
      "      Decimals are read from the contract, not assumed — they are 6 on " +
      "      Ethereum but 18 on BNB Chain. If the contract does not identify " +
      "      itself as USDT, no balance is shown." +
      "    </p>" +
      "  </div>" +
      "</div>";
    if (!window.MafutaniWallet) {
      view().innerHTML += "<p class='wallet-status wallet-status-warn'>" +
        "Wallet module failed to load (web/wallet.js). Everything else works.</p>";
      return;
    }
    document.getElementById("wallet-connect").onclick = window.MafutaniWallet.onConnect;
    document.getElementById("wallet-disconnect").onclick = window.MafutaniWallet.onDisconnect;
    document.getElementById("wallet-switch-chain").onclick = window.MafutaniWallet.onSwitchChain;
    document.getElementById("wallet-add-chain").onclick = window.MafutaniWallet.onAddChain;
    window.MafutaniWallet.render();
  }

  // ---- Router ---------------------------------------------------------------
  var routes = {
    "/login": pageLogin, "/dashboard": pageDashboard, "/screener": pageScreener,
    "/scalp-lab": pageScalpLab, "/scalp-live": pageScalpLive, "/backtests": pageBacktests,
    "/portfolio": pagePortfolio, "/journal": pageJournal, "/news": pageNews,
    "/arbitrage": pageArbitrage, "/cot": pageCot,
    
    "/heatmap": pageHeatmap, "/analysis": pageAnalysis, "/anomalies": pageAnomalies,
    "/markets": pageMarkets, "/security": pageSecurity, "/wallet": pageWallet,
    "/admin": pageAdmin
  };
  function router() {
    stopSSE();
    document.querySelectorAll("#nav a").forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("href") === location.hash);
    });
    var h = location.hash.replace(/^#/, "") || "/dashboard";
    if (h.indexOf("/chart/") === 0) { pageChart(decodeURIComponent(h.slice(7))); return; }
    if (h.indexOf("/analysis/") === 0) { pageAnalysis(decodeURIComponent(h.slice(10))); return; }
    // Deep link into the ticket: #/scalp-live/BTCUSD/buy?qty=0.003
    if (h.indexOf("/scalp-live/") === 0) {
      var rest = h.slice("/scalp-live/".length);
      var qIdx = rest.indexOf("?");
      var qty = null;
      if (qIdx >= 0) {
        var qs = rest.slice(qIdx + 1);
        rest = rest.slice(0, qIdx);
        var m = /[?&]qty=([^&]*)/.exec(qs);
        if (m) qty = decodeURIComponent(m[1]);
      }
      var parts = rest.split("/");
      pageScalpLive(decodeURIComponent(parts[0] || ""),
                    (parts[1] || "").toLowerCase(), qty);
      return;
    }
    if (h === "/login") { pageLogin(); return; }
    // Require session for app pages: probe entitlements lazily.
    (state.me ? Promise.resolve(state.me) : refreshMe()).then(function (me) {
      if (!me) { location.hash = "#/login"; pageLogin(); return; }
      (routes[h] || pageDashboard)();
    });
  }

  function bindAuth() {
    var form = document.getElementById("login-form");
    var codeRow = document.getElementById("login-2fa-row");
    var codeInput = document.getElementById("login-code");
    function show2fa(on) {
      if (!codeRow) return;
      codeRow.hidden = !on;
      if (on && codeInput) { codeInput.focus(); }
    }
    function hide2faError() {
      var err = document.getElementById("login-error");
      if (err && /two-factor/i.test(err.textContent || "")) err.textContent = "";
    }
    function is2faChallenge(e) {
      return !!(e && e.data && e.data.twofa_required) ||
             (e && /two-factor/i.test(e.message || ""));
    }
    if (codeInput) codeInput.addEventListener("input", hide2faError);
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var email = document.getElementById("login-email").value;
      var password = document.getElementById("login-password").value;
      var code = codeInput ? codeInput.value.trim() : "";
      var err = document.getElementById("login-error");
      err.textContent = "";
      var body = { email: email, password: password };
      if (code) body.code = code;
      api("POST", "/api/auth/login", body)
        .then(function (r) {
          if (r.csrf_token || r.csrf) csrfToken = r.csrf_token || r.csrf;
          show2fa(false);
          if (codeInput) codeInput.value = "";
          return refreshMe();
        })
        .then(function () { location.hash = "#/dashboard"; router(); })
        .catch(function (e) {
          err.textContent = e.message;
          // password was right, second factor missing -> reveal the field
          if (is2faChallenge(e)) show2fa(true);
        });
    });
    document.getElementById("signup-btn").onclick = function () {
      var email = document.getElementById("login-email").value;
      var password = document.getElementById("login-password").value;
      var err = document.getElementById("login-error");
      err.textContent = "";
      api("POST", "/api/auth/signup", { email: email, password: password })
        .then(function (r) {
          if (r.csrf_token || r.csrf) csrfToken = r.csrf_token || r.csrf;
          return refreshMe();
        })
        .then(function () { location.hash = "#/dashboard"; router(); })
        .catch(function (e) { err.textContent = e.message; });
    };
    document.getElementById("logout-btn").onclick = function () {
      api("POST", "/api/auth/logout", {}).catch(function () {})
        .then(function () { state.me = null; csrfToken = null; location.hash = "#/login"; router(); });
    };
  }

  function tickClock() {
    var el = document.getElementById("sb-clock");
    if (el) el.textContent = new Date().toISOString().slice(11, 19) + "Z";
  }
  function fetchStatus() {
    api("GET", "/api/status").then(function (s) {
      var r = document.querySelector("#statusbar .sb-right");
      if (r) r.innerHTML = "v" + esc(s.version || "?") + " · up " + esc(s.uptime_s || 0) +
        "s · <kbd>`</kbd> terminal · <kbd>Ctrl+K</kbd> palette · <kbd>Alt+1..9</kbd> nav";
    }).catch(function () { /* statusbar stays static offline */ });
    // Reflect the account's live trading status in the statusbar.
    // PAPER is the default and always safe; LIVE means the tier and
    // env both allow it. The mode never claims more than the API says.
    api("GET", "/api/portfolio").then(function (p) {
      var mode = document.getElementById("sb-mode");
      var tier = document.getElementById("sb-tier");
      if (mode) mode.textContent = p.live_trading_enabled ? "LIVE" : "PAPER";
      if (tier) tier.textContent = p.tier ? ("TIER " + p.tier.toUpperCase()) : "";
      // Separate element: this used to share the id "sb-tier" with the
      // span above, and getElementById only ever returned the first one,
      // so the footer read "TIER —" no matter what the account was.
      var tierLabel = document.getElementById("sb-tier-label");
      if (tierLabel) tierLabel.textContent = p.tier ? p.tier.toUpperCase() : "—";
    }).catch(function () { /* stays PAPER if unauthorized */ });
  }

  window.addEventListener("hashchange", router);
  bindAuth();
  if (!location.hash) location.hash = "#/dashboard";
  try {
    if (window.OpenTerminal) {
      window.OpenTerminal.init(function () { return csrfToken; });
    }
    if (window.CommandPalette) {
      window.CommandPalette.init(function () { return csrfToken; });
    }
  } catch (e) { /* terminal/palette are decorative; never break boot */ }
  tickClock();
  setInterval(tickClock, 1000);
  fetchStatus();
  setInterval(fetchStatus, 60000);
  refreshMe().then(router);
})();
