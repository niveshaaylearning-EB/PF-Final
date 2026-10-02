// ONE bookmarklet, two independent jobs in one click:
//   1. Our own 7 baskets' daily index/benchmark values (unchanged, works
//      from ANY smallcase.com page via /sam/graph/performance -- this
//      endpoint has never needed anything beyond cookies).
//   2. IF the current page is one of the 8 tracked competitor smallcases'
//      own page, scrapes its real rendered content (CAGR, rebalance
//      timeline, full stocks & weights table -- all on the SAME
//      /constituents page, confirmed live 2026-10-01, no tab-click needed).
//
// Two different "fetch all 8 from anywhere, zero page visits" approaches
// were tried and BOTH hit hard walls smallcase itself puts up, confirmed
// live (2026-10-01):
//   - Replaying smallcase's own internal JSON APIs (/sam/subscriptions/v2
//     etc.) 401s with "Invalid csrf token provided" even after replicating
//     every header the real page sends -- an invisible anti-bot check only
//     the real page's own JS can satisfy (same class of protection
//     smallcase_login.py's OTP flow already has to work around
//     server-side).
//   - Loading each competitor's page in a hidden same-origin iframe (to let
//     THAT real page authenticate itself normally) is blocked outright by
//     smallcase's own CSP: "frame-ancestors *.juspay.in" -- it only allows
//     being framed by its own payment-widget domain, not even by itself.
// Reading what the CURRENT page already rendered (this file) is what's
// left, at the cost of one click per competitor page instead of zero.
//
// GET /api/admin/dashboard-bookmarklet (historical_data.py) reads this file
// at request time, fills in __INGEST_URL__/__INGEST_KEY__, URL-encodes it
// into a "javascript:" URI.
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

  // scid -> our own internal competitor key (competitor_login.py's
  // COMPETITOR_SMALLCASE_MAP) -- kept in sync manually, same as BASKETS above.
  var COMPETITOR_SCIDS = {
    "SCMO_0029":   "gem_q_model",
    "QURETR_0009": "consumer_durables_stars",
    "OMNNM_0014":  "omni_ai_tech",
    "GRINMX_0003": "ai_data_center",
    "WRTNM_0004":  "wright_innovation",
    "NVNTNM_0001": "nirivantes_techwave",
    "CAPIMO_0001": "caprize_earnings_momentum",
    "CAPINM_0001": "caprize_midcap_smallcap",
  };

  function banner(text, ok) {
    var el = document.getElementById("_niveshaay_bkmklt_banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "_niveshaay_bkmklt_banner";
      el.style.cssText =
        "position:fixed;top:12px;right:12px;z-index:2147483647;" +
        "font:600 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;" +
        "padding:12px 16px;border-radius:8px;max-width:380px;" +
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

  function extractScid() {
    var m = location.pathname.match(/([A-Z]+_\d+)/);
    return m ? m[1] : null;
  }

  // Real layout confirmed live (2026-10-01, subscribed account): the
  // /constituents page renders CAGR, "Rebalance timeline", and the full
  // "Stocks & Weights" table all on ONE page (no tab-click needed). Stock
  // rows are grouped under sector sub-headings ("Biotechnology", "14.28",
  // "Sai Life Sciences Limited", "7.14", "Acutaas Chemicals Ltd", "7.14",
  // ...) -- sector name + sector total weight, then each real company +
  // its own weight. Real NSE company names reliably end in "Limited"/"Ltd"
  // (confirmed across all 14 of GEM-Q Model's holdings); sector names never
  // do, which is what distinguishes a sector-aggregate row from an actual
  // stock row without needing to track "how many stocks in this sector".
  function parseStocksAndWeights(lines) {
    var stocks = [];
    for (var i = 0; i < lines.length - 1; i++) {
      if (/\b(Limited|Ltd\.?)$/i.test(lines[i]) && /^[\d.]+$/.test(lines[i + 1])) {
        stocks.push({ name: lines[i], weight: parseFloat(lines[i + 1]) });
        i++; // skip the weight line we just consumed
      }
    }
    return stocks;
  }

  var _DATE_RE = /^\d{1,2} [A-Za-z]{3}, \d{4}$/;

  function parseRebalanceTimeline(lines, startIdx, endIdx) {
    var events = [];
    for (var i = startIdx; i < endIdx - 1; i++) {
      if (_DATE_RE.test(lines[i]) && lines[i + 1] === "Constituents updated") {
        var added = null, removed = null;
        if (lines[i + 2] && /^\+\d+$/.test(lines[i + 2])) added = parseInt(lines[i + 2].slice(1), 10);
        if (lines[i + 3] && /^-\d+$/.test(lines[i + 3])) removed = parseInt(lines[i + 3].slice(1), 10);
        events.push({ date: lines[i], assetsAdded: added, assetsRemoved: removed });
      }
    }
    return events;
  }

  function scrapeCompetitorPage() {
    var scid = extractScid();
    if (!scid || !COMPETITOR_SCIDS[scid]) return null;

    var bodyText = document.body.innerText || "";
    var lines = bodyText.split("\n").map(function (l) { return l.trim(); }).filter(function (l) { return l.length > 0; });

    var result = { scid: scid };

    var cagrIdx = lines.indexOf("CAGR");
    if (cagrIdx !== -1 && lines[cagrIdx + 1]) {
      var m = lines[cagrIdx + 1].match(/([\d.]+)%/);
      if (m) result.cagr = parseFloat(m[1]);
    }

    var timelineStart = lines.indexOf("Rebalance timeline");
    var timelineEnd = lines.indexOf("Portfolio Report", timelineStart);
    if (timelineEnd === -1) timelineEnd = lines.indexOf("Get portfolio report", timelineStart);
    if (timelineStart !== -1) {
      result.rebalanceTimeline = parseRebalanceTimeline(lines, timelineStart, timelineEnd === -1 ? lines.length : timelineEnd);
    }

    var stocksHeaderIdx = lines.indexOf("Weightage (%)");
    var stocksEndIdx = lines.findIndex(function (l, idx) { return idx > stocksHeaderIdx && l.indexOf("You can also edit constituents") === 0; });
    if (stocksHeaderIdx !== -1) {
      result.stocks = parseStocksAndWeights(lines.slice(stocksHeaderIdx + 1, stocksEndIdx === -1 ? lines.length : stocksEndIdx));
    }

    if (result.cagr == null && !result.rebalanceTimeline && !result.stocks) return null;
    return result;
  }

  (async function main() {
    banner("Fetching from smallcase...", null);

    var payload = {};

    var basketResults = {};
    var basketFailed = [];
    var basketNames = Object.keys(BASKETS);
    for (var i = 0; i < basketNames.length; i++) {
      var key = basketNames[i];
      try {
        basketResults[key] = await fetchBasket(key, BASKETS[key]);
      } catch (e) {
        basketFailed.push(key + ": " + e.message);
      }
    }
    if (Object.keys(basketResults).length > 0) payload.baskets = basketResults;

    var competitor = scrapeCompetitorPage();
    if (competitor) payload.competitor = competitor;

    if (!payload.baskets && !payload.competitor) {
      // Surface the ACTUAL failure reason per basket instead of a blanket
      // "are you logged in?" guess -- a 401/403 really does mean the
      // smallcase session cookie isn't valid here, but other causes (CORS
      // block, network error, a changed API shape) look identical to the
      // user otherwise and this message used to hide which one it was.
      var reasons = basketFailed.length ? "\n" + basketFailed.join("\n") : "";
      banner("Nothing to send -- are you logged in to smallcase.com?" + reasons, false);
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
      if (result.baskets) {
        for (var b in result.baskets) {
          var r = result.baskets[b];
          if (r.ok) lines.push(b + ": +" + r.added_dates.length + " day(s)");
          else lines.push(b + ": " + r.error);
        }
        if (basketFailed.length) lines.push("Could not fetch: " + basketFailed.join(", "));
      }
      if (result.competitor) {
        lines.push(result.competitor.label + ": " + result.competitor.stockCount + " stock(s)");
      } else if (competitor) {
        lines.push("(on a competitor page, but nothing recognized there -- see console)");
      }
      banner("Done.\n" + lines.join("\n"), true);
    } catch (e) {
      banner("Could not reach the dashboard: " + e.message, false);
    }
  })();
})();
