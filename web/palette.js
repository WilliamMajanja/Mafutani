/* Command palette (Ctrl+K / Cmd+K). Zero deps.
 * Symbol search + page nav + terminal passthrough (prefix with $).
 * window.CommandPalette = { init(getCsrf) }.
 */
"use strict";
window.CommandPalette = (function () {
  var csrfGetter = function () { return null; };
  var el = {}, opened = false, items = [], sel = 0, symbols = [];

  var PAGES = [
    ["Dashboard", "#/dashboard"], ["Screener", "#/screener"],
    ["Scalp Lab", "#/scalp-lab"], ["Scalp Live", "#/scalp-live"],
    ["Analysis", "#/analysis"], ["Anomalies", "#/anomalies"],
    ["Backtests", "#/backtests"], ["Portfolio", "#/portfolio"],
    ["Journal", "#/journal"], ["News", "#/news"], ["Heatmap", "#/heatmap"],
    ["Admin", "#/admin"]
  ];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function loadSymbols() {
    return fetch("/api/symbols", { credentials: "same-origin" }).then(function (r) {
      return r.json();
    }).then(function (d) {
      var list = d.symbols || d.rows || d || [];
      symbols = list.map(function (s) {
        return typeof s === "string" ? s : (s.symbol || s.name);
      }).filter(Boolean);
    }).catch(function () { symbols = []; });
  }

  function build(q) {
    q = (q || "").trim();
    items = [];
    if (q.charAt(0) === "$") {
      items.push({ kind: "term", label: "Run terminal: " + q.slice(1).trim(), cmd: q.slice(1).trim() });
      return;
    }
    var ql = q.toLowerCase();
    PAGES.forEach(function (p) {
      if (!ql || p[0].toLowerCase().indexOf(ql) >= 0) {
        items.push({ kind: "page", label: "Go: " + p[0], hash: p[1] });
      }
    });
    symbols.forEach(function (s) {
      if (!ql || String(s).toLowerCase().indexOf(ql) >= 0) {
        items.push({ kind: "chart", label: "Chart " + s, hash: "#/chart/" + encodeURIComponent(s) });
        items.push({ kind: "analysis", label: "Analyse " + s, hash: "#/analysis/" + encodeURIComponent(s) });
      }
    });
    items = items.slice(0, 12);
  }

  function render() {
    el.list.innerHTML = items.map(function (it, i) {
      return "<div class='pal-item" + (i === sel ? " sel" : "") + "' data-i='" + i + "'>" +
        "<span class='pal-kind'>" + esc(it.kind) + "</span>" + esc(it.label) + "</div>";
    }).join("") || "<div class='pal-empty'>no match — try $help for terminal</div>";
    el.list.querySelectorAll(".pal-item").forEach(function (d) {
      d.onclick = function () { choose(parseInt(d.getAttribute("data-i"), 10)); };
    });
  }

  function runTerminal(cmd) {
    var token = null;
    try { token = csrfGetter(); } catch (e) { token = null; }
    var headers = { "Content-Type": "application/json" };
    if (token) { headers["X-CSRF"] = token; headers["X-CSRF-Token"] = token; }
    return fetch("/api/terminal/command", {
      method: "POST", headers: headers, credentials: "same-origin",
      body: JSON.stringify({ cmd: cmd })
    }).then(function (r) { return r.json(); });
  }

  function choose(i) {
    var it = items[i == null ? sel : i];
    if (!it) return;
    if (it.kind === "term") {
      runTerminal(it.cmd).then(function (res) {
        el.foot.textContent = (res.ok ? "" : "ERR ") + (res.output || "ok").split("\n")[0];
      }).catch(function (e) { el.foot.textContent = "ERR " + e.message; });
      return;
    }
    toggle(false);
    location.hash = it.hash;
  }

  function toggle(force) {
    opened = (typeof force === "boolean") ? force : !opened;
    el.overlay.classList.toggle("open", opened);
    if (opened) {
      el.input.value = "";
      el.foot.textContent = "pages · symbols · $cmd for terminal";
      sel = 0;
      build("");
      render();
      setTimeout(function () { el.input.focus(); }, 30);
    }
  }

  function init(getCsrf) {
    if (getCsrf) csrfGetter = getCsrf;
    el.overlay = document.getElementById("palette");
    el.input = document.getElementById("pal-input");
    el.list = document.getElementById("pal-list");
    el.foot = document.getElementById("pal-foot");
    if (!el.overlay || !el.input) return false;
    el.input.addEventListener("input", function () { sel = 0; build(el.input.value); render(); });
    el.input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); sel = Math.min(items.length - 1, sel + 1); render(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); sel = Math.max(0, sel - 1); render(); }
      else if (ev.key === "Enter") { ev.preventDefault(); choose(sel); }
      else if (ev.key === "Escape") { toggle(false); }
    });
    el.overlay.addEventListener("click", function (ev) {
      if (ev.target === el.overlay) toggle(false);
    });
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target && ev.target.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        if (!opened) loadSymbols().then(function () { toggle(true); });
        else toggle(false);
      } else if (ev.key === "Escape" && opened) { toggle(false); }
      else if (!typing && ev.altKey && /^[1-9]$/.test(ev.key)) {
        var idx = parseInt(ev.key, 10) - 1;
        if (idx < PAGES.length) { ev.preventDefault(); location.hash = PAGES[idx][1]; }
      }
    });
    loadSymbols();
    return true;
  }

  return { init: init, toggle: toggle };
})();
