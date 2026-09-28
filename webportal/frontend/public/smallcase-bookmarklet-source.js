// Source for the "Fetch to Dashboard" bookmarklet.
// This file is NOT loaded directly by the app -- it exists so the readable
// version of the bookmarklet is version-controlled and reviewable.
// GET /api/admin/smallcase-bookmarklet (historical_data.py) reads this file
// at request time, fills in __INGEST_URL__/__INGEST_KEY__, URL-encodes it
// into a "javascript:" URI, and hands that to the smallcase Login modal for
// admins to drag into their bookmarks bar.
//
// Runs entirely inside an already-logged-in smallcase.com tab -- no server
// browser automation needed. Each admin's own smallcase session (with full
// subscriber access) is what smallcase's API sees, exactly as if they'd
// clicked around the page themselves.
(function () {
  var BACKEND_URL = "__INGEST_URL__";
  var INGEST_KEY  = "__INGEST_KEY__";

  var BASKETS = {
    Mid_Small_Cap:   { scid: "NIVMO_0001",  benchmark_id: ".NIFSMCP100" },
    Green_Energy:    { scid: "NIVTR_0001",  benchmark_id: ".NIFSMCP100" },
    IPO_Basket:      { scid: "NIVFMM_0001", benchmark_id: ".NIFSMCP100" },
    Trends_Triology: { scid: "NIVMO_0004",  benchmark_id: ".NIFSMCP100" },
    Techstack:       { scid: "NIVNM_0003",  benchmark_id: ".NIFSMCP100" },
    Make_in_India:   { scid: "NIVNM_0001",  benchmark_id: ".NIFSMCP100" },
    Consumer_Trends: { scid: "NIVNM_0002",  benchmark_id: ".NIFSMCP100" },
  };

  function banner(text, ok) {
    var el = document.getElementById("_niveshaay_bkmklt_banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "_niveshaay_bkmklt_banner";
      el.style.cssText =
        "position:fixed;top:12px;right:12px;z-index:2147483647;" +
        "font:600 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;" +
        "padding:12px 16px;border-radius:8px;max-width:360px;" +
        "box-shadow:0 8px 24px rgba(0,0,0,0.3);white-space:pre-line;";
      document.body.appendChild(el);
    }
    el.style.background = ok === false ? "#fee2e2" : ok === true ? "#dcfce7" : "#fef9c3";
    el.style.color = ok === false ? "#991b1b" : ok === true ? "#166534" : "#854d0e";
    el.style.border = "1px solid " + (ok === false ? "#fca5a5" : ok === true ? "#86efac" : "#fde047");
    el.textContent = text;
  }

  async function fetchBasket(key, cfg) {
    var url = "https://api.smallcase.com/sam/graph/performance?currency=INR&duration=1m" +
      "&scids[]=" + encodeURIComponent(cfg.scid) +
      "&stockBenchmarkIds[]=" + encodeURIComponent(cfg.benchmark_id);
    var resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) throw new Error(key + ": HTTP " + resp.status);
    var body = await resp.json();
    var data = (body && body.data) || {};
    return {
      port_pts: (data[cfg.scid] && data[cfg.scid].points) || [],
      bench_pts: (data[cfg.benchmark_id] && data[cfg.benchmark_id].points) || [],
    };
  }

  (async function main() {
    banner("Fetching from smallcase...", null);
    var payload = {};
    var failed = [];
    var names = Object.keys(BASKETS);
    for (var i = 0; i < names.length; i++) {
      var key = names[i];
      try {
        payload[key] = await fetchBasket(key, BASKETS[key]);
      } catch (e) {
        failed.push(key);
      }
    }

    if (Object.keys(payload).length === 0) {
      banner("Could not fetch any basket data. Are you logged in to smallcase.com?", false);
      return;
    }

    banner("Sending to dashboard...", null);
    try {
      var resp = await fetch(BACKEND_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Ingest-Key": INGEST_KEY },
        body: JSON.stringify(payload),
      });
      var result = await resp.json();
      if (!resp.ok || !result.ok) {
        banner("Dashboard rejected the data: " + (result.detail || result.error || resp.status), false);
        return;
      }
      var lines = [];
      for (var b in result.results) {
        var r = result.results[b];
        if (r.ok) lines.push(b + ": +" + r.added_dates.length + " day(s)");
        else lines.push(b + ": " + r.error);
      }
      if (failed.length) lines.push("Could not fetch: " + failed.join(", "));
      banner("Done.\n" + lines.join("\n"), true);
    } catch (e) {
      banner("Could not reach the dashboard: " + e.message, false);
    }
  })();
})();
