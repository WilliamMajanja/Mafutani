/* OpenTerminal drawer + ticker tape. Zero deps: fetch + DOM only.
 * Exposes window.OpenTerminal = { init(getCsrf), toggle, open, close, run }.
 * getCsrf: function returning the current CSRF token string (wired by app.js).
 */
"use strict";
window.OpenTerminal = (function () {
  var csrfGetter = function () { return null; };
  var hist = [];
  var histIdx = -1;
  var el = {};
  var opened = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function printLine(text, cls) {
    var out = el.out;
    var div = document.createElement("div");
    div.className = "term-line" + (cls ? " " + cls : "");
    div.textContent = text;
    out.appendChild(div);
    out.scrollTop = out.scrollHeight;
    while (out.children.length > 500) out.removeChild(out.firstChild);
  }

  function send(cmd) {
    var token = null;
    try { token = csrfGetter(); } catch (e) { token = null; }
    var headers = { "Content-Type": "application/json" };
    if (token) { headers["X-CSRF"] = token; headers["X-CSRF-Token"] = token; }
    return fetch("/api/terminal/command", {
      method: "POST", headers: headers, credentials: "same-origin",
      body: JSON.stringify({ cmd: cmd })
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
        return data;
      });
    });
  }

  function run(cmd) {
    cmd = (cmd || "").trim();
    if (!cmd) return Promise.resolve();
    printLine("$ " + cmd, "term-echo");
    hist.unshift(cmd);
    histIdx = -1;
    return send(cmd).then(function (res) {
      printLine(res.output || "(ok)", res.ok ? "" : "term-err");
      try {
        if (window.dispatchEvent) {
          window.dispatchEvent(new CustomEvent("terminal:result", { detail: res }));
        }
      } catch (e) { /* ignore */ }
      return res;
    }).catch(function (e) {
      printLine("error: " + e.message, "term-err");
      throw e;
    });
  }

  function toggle(force) {
    opened = (typeof force === "boolean") ? force : !opened;
    el.panel.classList.toggle("open", opened);
    el.toggleBtn.classList.toggle("active", opened);
    el.toggleBtn.setAttribute("aria-expanded", opened ? "true" : "false");
    if (opened) { setTimeout(function () { el.input.focus(); }, 50); }
  }

  function refreshTape() {
    send("screener").then(function (res) {
      var leaders = (res.data && res.data.leaders) || [];
      if (!leaders.length) return;
      var html = leaders.map(function (l) {
        return '<span class="tape-item"><b>' + esc(l.symbol) +
          "</b> vol " + esc(Math.round(l.vol)) + "</span>";
      }).join('<span class="tape-sep">///</span>');
      el.tapeTrack.innerHTML = html + '<span class="tape-sep">///</span>' + html;
      el.tapeTrack.dataset.ready = "1";
    }).catch(function () {
      if (!el.tapeTrack.dataset.ready) {
        el.tapeTrack.textContent = "tape offline — log in to stream quotes";
      }
    });
  }

  function bind() {
    el.toggleBtn.addEventListener("click", function () { toggle(); });
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target && ev.target.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (ev.key === "`" && !typing) { ev.preventDefault(); toggle(); }
      if (ev.key === "Escape" && opened) toggle(false);
    });
    el.form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var cmd = el.input.value;
      el.input.value = "";
      run(cmd);
    });
    el.input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowUp") {
        ev.preventDefault();
        if (histIdx < hist.length - 1) { histIdx++; el.input.value = hist[histIdx] || ""; }
      } else if (ev.key === "ArrowDown") {
        ev.preventDefault();
        if (histIdx > 0) { histIdx--; el.input.value = hist[histIdx] || ""; }
        else { histIdx = -1; el.input.value = ""; }
      }
    });
    el.closeBtn.addEventListener("click", function () { toggle(false); });
  }

  function init(getCsrf) {
    if (getCsrf) csrfGetter = getCsrf;
    el.panel = document.getElementById("terminal-panel");
    el.out = document.getElementById("term-out");
    el.form = document.getElementById("term-form");
    el.input = document.getElementById("term-input");
    el.toggleBtn = document.getElementById("terminal-toggle");
    el.closeBtn = document.getElementById("term-close");
    el.tapeTrack = document.getElementById("tape-track");
    if (!el.panel || !el.out || !el.form) return false;
    bind();
    printLine("OpenTerminal v1 — type 'help'.  (` toggles, Ctrl+K = palette)", "term-dim");
    refreshTape();
    setInterval(refreshTape, 30000);
    return true;
  }

  return { init: init, toggle: toggle, open: function () { toggle(true); },
           close: function () { toggle(false); }, run: run, refresh: refreshTape };
})();
