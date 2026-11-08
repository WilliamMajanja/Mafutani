/* Mafutani global locale integration (zero-dep). */
(function () {
  var KEY = "mafutani_locale";
  function get() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function setLocale(loc) {
    try { localStorage.setItem(KEY, loc); } catch (e) {}
    apply(loc);
  }
  function negotiate(loc) {
    var supported = ["en","sw","fr","es","ar","pt","de","zh","hi"];
    loc = (loc || get() || navigator.language || "en").replace("_","-");
    if (supported.indexOf(loc) >= 0) return loc;
    var base = loc.split("-")[0];
    return supported.indexOf(base) >= 0 ? base : "en";
  }
  function apply(loc) {
    loc = negotiate(loc);
    var rtl = {"ar":1,"he":1,"fa":1,"ur":1};
    document.documentElement.lang = loc;
    document.documentElement.dir = rtl[loc] ? "rtl" : "ltr";
    return loc;
  }
  function fetchMessages(loc, cb) {
    fetch("/api/i18n/messages?locale=" + encodeURIComponent(negotiate(loc)))
      .then(function (r) { return r.json(); })
      .then(function (d) { if (cb) cb(d.messages || {}, d.locale); })
      .catch(function () { if (cb) cb({}, negotiate(loc)); });
  }
  window.MafutaniI18n = { get: get, set: setLocale, negotiate: negotiate, apply: apply, fetch: fetchMessages };
  apply(get());
})();

(function () {
  function init() {
    var sel = document.getElementById("locale-select");
    if (!sel || !window.MafutaniI18n) return;
    var loc = window.MafutaniI18n.negotiate();
    for (var i=0;i<sel.options.length;i++) if (sel.options[i].value===loc) sel.selectedIndex=i;
    sel.onchange = function () { window.MafutaniI18n.set(sel.value); };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
