// Competitor Analysis / "Niveshaay Competitive Intelligence" -- compares one
// of our own baskets against one tracked competitor smallcase: overlap,
// sector exposure, market-cap mix, concentration, returns and rebalance
// activity. Data sources, all real (never fabricated -- a metric with no
// data shows "N/A"/"—", not a guess):
//   - Our own basket: fetchBasket (current holdings), /admin/basket-profile
//     (sector/cap-mix aggregation, last rebalance +/new/increased/decreased/
//     removed counts), /index-history (daily index series for returns).
//   - Competitor: /admin/competitor-list, populated by competitor_login.py's
//     server-side Playwright fetch (real portfolio-report PDF -- exact
//     per-stock market-cap segment, precise CAGR, exact latest-rebalance
//     stock moves -- falling back to page-scraped data if the PDF's layout
//     breaks the parser, see competitor_login.py for the full story) and
//     its real indexed performance series (smallcase's own graph API).
//   - Market Cap (Cr) / P/E for the stock-comparison table: /admin/stock-
//     metrics, reusing price_engine's existing Screener/Google/NSE cascade.
// The main view is scoped to ONE basket vs ONE competitor at a time; a
// separate "Multi-Compare" section (MultiComparePanel, opened on demand via
// the sidebar) supports comparing any mix of our baskets + competitors
// simultaneously. No stock detail drawer, no PDF/Excel export, no ROE/ROCE/
// Debt-Equity/liquidity columns (not available anywhere in this codebase)
// -- those would need substantially more data plumbing than this covers.
import { API_BASE, getAuthToken } from '../api/base.js';
import { useState, useEffect, useMemo } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  LineChart, Line,
} from 'recharts';
import { fetchBasket } from '../api/client.js';
import { BASKET_OPTIONS } from './Header.jsx';
import CompetitorLoginModal from './CompetitorLoginModal.jsx';
import { computeTenureReturn } from '../utils/tenureReturn.js';
import { getTheme, THEME_SYNC_TYPE } from '../utils/theme.js';

// Niveshaay brand palette (brandbook.pdf) -- applied to this page only, per
// explicit request. Real brand fonts LT Serif/OV Soge aren't available as
// web fonts; Noto Serif substitutes for headings (loaded in index.html
// alongside the real Poppins body font). The exact traced corner-burst
// motif is deliberately not reproduced here (it needs the precise angles
// from base.pptx's own shape geometry to not look approximated) -- the
// color/stroke/type system is what's carried over.
export const BRAND_GREEN = '#456232';   // headings, stat values
export const BRAND_STROKE = '#6A8557';  // frame rules / card borders (brand's only stroke weight, 2pt)
export const BRAND_CREAM = '#F4E9C9';   // card / band fill
export const BRAND_INK = '#2E3A26';     // body text
export const BRAND_GOLD = '#E9BE5F';    // title highlight bar
export const BRAND_GOLD_TEXT = '#8a6c1f'; // readable-on-cream variant of gold, for text/labels
export const FONT_HEADING = "'Noto Serif', serif";
export const FONT_BODY = "'Poppins', sans-serif";

export const OURS_COLOR = BRAND_GREEN;
export const THEIRS_COLOR = BRAND_GOLD_TEXT;
export const POS = '#22c55e';
export const NEG = '#ef4444';
// Stock Timing Insights: a shared addition more than this many days apart
// isn't treated as a meaningful "we caught it earlier/later" signal.
export const TIMING_WINDOW_DAYS = 30;

// Light/dark variants of the brand palette -- the page previously used
// fixed light-mode hex values everywhere, so the app's existing dark-mode
// toggle (Header.jsx etc.) had no visible effect on it at all. Both
// variants keep the same green/gold/cream identity, just inverted in
// lightness so text stays readable against the opposite background.
const PALETTES = {
  light: {
    pageBg: '#FBF7EC', cardBg: BRAND_CREAM, border: BRAND_STROKE, borderSoft: '#c9bd8f',
    heading: BRAND_GREEN, text: BRAND_INK, muted: '#6b7a5e', mutedLight: '#9a9573',
    gold: BRAND_GOLD_TEXT, rowA: '#FBF7EC', rowB: BRAND_CREAM, headerRow: '#efe3bd',
    rowDivider: '#ddd0a0', inputBg: '#fff', inputText: BRAND_INK,
  },
  dark: {
    pageBg: '#1b2117', cardBg: '#242b1e', border: BRAND_STROKE, borderSoft: '#3c4631',
    heading: '#9bc17a', text: '#e9e2c6', muted: '#b2ae8e', mutedLight: '#847f61',
    gold: BRAND_GOLD, rowA: '#1f251a', rowB: '#242b1e', headerRow: '#2d3524',
    rowDivider: '#3c4631', inputBg: '#2a3123', inputText: '#e9e2c6',
  },
};

function useAppTheme() {
  const [theme, setTheme] = useState(getTheme());
  useEffect(() => {
    const onMessage = (e) => {
      if (e.data?.type === THEME_SYNC_TYPE && (e.data.theme === 'light' || e.data.theme === 'dark')) {
        setTheme(e.data.theme);
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);
  return theme;
}
export function usePalette() {
  const theme = useAppTheme();
  return PALETTES[theme === 'light' ? 'light' : 'dark'];
}

function computeSinceInception(data) {
  if (!data || data.length < 2) return null;
  const first = data[0], last = data[data.length - 1];
  if (!first.value || first.date >= last.date) return null;
  return { pct: (last.value - first.value) / first.value, baseDate: first.date, latestDate: last.date };
}

// Handles both "05 Nov 2019" (our rebalance_history.json) and "17 Mar, 2026"
// (competitor page-scrape) date strings -- the comma trips up some engines'
// "DD Mon YYYY" parsing, so it's stripped before handing off to Date().
export function parseLooseDate(s) {
  if (!s) return 0;
  const t = Date.parse(s.replace(',', ''));
  return Number.isNaN(t) ? 0 : t;
}

export function toIsoDate(s) {
  const t = parseLooseDate(s);
  if (!t) return null;
  // Date.parse("30 Jun 2026") (a non-ISO string) parses as LOCAL midnight,
  // per the JS spec -- going through .toISOString() converts that to UTC,
  // which for any positive UTC-offset timezone (e.g. IST, UTC+5:30) rolls
  // it back to the PREVIOUS calendar day ("2026-06-29"), confirmed live:
  // the OHLC lookup silently fetched the wrong day's price. Reading the
  // LOCAL year/month/day back out instead keeps the same calendar day the
  // string actually named, with no UTC round-trip to shift it.
  const d = new Date(t);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// The backend stores "lastFetched" as a server-formatted UTC string (e.g.
// "01 Oct 2026 11:15 UTC") -- shown to users as-is before, which reads as
// the wrong clock time for anyone in India. Converts to IST for display
// regardless of the viewer's own machine timezone, since this is shown to
// every user of the page, not just whoever's browser happens to be IST.
export function formatIst(s) {
  const t = parseLooseDate(s);
  if (!t) return s;
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }).formatToParts(new Date(t));
  const get = (type) => parts.find(p => p.type === type)?.value || '';
  return `${get('day')} ${get('month')} ${get('year')}, ${get('hour')}:${get('minute')} ${get('dayPeriod').toUpperCase()} IST`;
}

export function normalizeStockName(s) {
  return (s || '').toLowerCase().replace(/\b(ltd|limited)\.?\b/g, '').replace(/[^a-z0-9]/g, '').trim();
}

// Shared by the buy ('new') and sell ('removed') Stock Timing Insights --
// finds, for every matching stock event on their side, the closest same-
// status event on our side, within TIMING_WINDOW_DAYS.
export function buildTimingInsights(ourHistory, theirHistory, theirStocksList, status) {
  if (!ourHistory?.length || !theirHistory?.length) return [];

  const theirCodeByName = {};
  (theirStocksList || []).forEach(s => { if (s.nseCode) theirCodeByName[normalizeStockName(s.name)] = s.nseCode; });

  const ourEventsByCode = {};
  const ourEventsByName = {};
  ourHistory.forEach(h => {
    (h.changes || []).forEach(c => {
      if (c.status !== status) return;
      const entry = { date: h.date, name: c.name, code: c.nseCode, weight: c.newWeight };
      if (c.nseCode) (ourEventsByCode[c.nseCode] ||= []).push(entry);
      (ourEventsByName[normalizeStockName(c.name)] ||= []).push(entry);
    });
  });

  const insights = [];
  theirHistory.forEach(h => {
    (h.changes || []).filter(c => c.status === status).forEach(c => {
      const normName = normalizeStockName(c.name);
      const code = c.nseCode || theirCodeByName[normName];
      const ourEvents = (code && ourEventsByCode[code]) || ourEventsByName[normName];
      if (!ourEvents?.length) return;
      const theirTs = parseLooseDate(h.date);
      let best = null;
      ourEvents.forEach(ev => {
        const diffDays = Math.round((theirTs - parseLooseDate(ev.date)) / 86400000);
        if (best === null || Math.abs(diffDays) < Math.abs(best.diffDays)) best = { diffDays, ourDate: ev.date, code: ev.code || code, ourWeight: ev.weight };
      });
      if (best && Math.abs(best.diffDays) <= TIMING_WINDOW_DAYS) {
        insights.push({ name: c.name, code: best.code, ourDate: best.ourDate, theirDate: h.date, diffDays: best.diffDays, ourWeight: best.ourWeight, theirWeight: c.newWeight });
      }
    });
  });
  return insights.sort((a, b) => Math.abs(a.diffDays) - Math.abs(b.diffDays)).slice(0, 15);
}

function formatReturnDate(iso) {
  if (!iso) return '?';
  const [y, m, d] = iso.split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${+d} ${months[+m - 1]} ${y}`;
}

export const _FOUNDER_FALLBACK = new Set(['jay.chaudhari@niveshaay.com', 'nukul.madaan@niveshaay.com', 'nakshatra.rathi@niveshaay.com']);
export const _getAdminState = () => {
  try {
    const t = getAuthToken();
    if (!t) return { isAdmin: false };
    const payload = JSON.parse(atob(t.split('.')[1]));
    if (payload.exp && Date.now() > payload.exp * 1000) return { isAdmin: false };
    const email = (payload.sub || '').toLowerCase().trim();
    return { isAdmin: (payload.admin === true || _FOUNDER_FALLBACK.has(email)) };
  } catch { return { isAdmin: false }; }
};

function topNWeight(stocks, n) {
  return [...stocks].sort((a, b) => (b.weight || 0) - (a.weight || 0)).slice(0, n).reduce((sum, s) => sum + (s.weight || 0), 0);
}

const RANKS = [1, 5, 10, 20, 30, 40, 50];
function concentrationCurve(weights) {
  const sorted = [...weights].sort((a, b) => b - a);
  return RANKS.map(n => ({
    rank: `Top ${n}`,
    ours: round1(sorted.slice(0, n).reduce((s, w) => s + w, 0)),
  }));
}
export function round1(v) { return Math.round(v * 10) / 10; }

// Largest peak-to-trough decline over the FULL series (since inception),
// not scoped to whatever return-tenure window is currently selected --
// drawdown is about "how bad did it ever get", so it should reflect the
// worst point in the whole history, not just a recent slice of it.
// Returns a negative fraction (e.g. -0.234 for a 23.4% drawdown), or null
// if there's not enough data.
export function maxDrawdownPct(series) {
  if (!series || series.length < 2) return null;
  let peak = series[0].value;
  let worst = 0;
  for (const pt of series) {
    if (pt.value == null) continue;
    if (pt.value > peak) peak = pt.value;
    if (peak > 0) {
      const dd = (pt.value - peak) / peak;
      if (dd < worst) worst = dd;
    }
  }
  return worst;
}

// N-way version of buildIndexedSeries below, for the Multi-Compare panel --
// `series` is [{key, data: [{date,value}]}], one per selected entity.
export function buildIndexedSeriesMulti(series, fromDate) {
  const trimmed = series.map(s => ({ key: s.key, data: (s.data || []).filter(d => d.date >= fromDate) }));
  if (trimmed.every(s => s.data.length < 2)) return [];
  const dates = [...new Set(trimmed.flatMap(s => s.data.map(d => d.date)))].sort();
  const bases = {};
  trimmed.forEach(s => { bases[s.key] = s.data[0]?.value; });
  const cursors = {};
  const last = {};
  trimmed.forEach(s => { cursors[s.key] = 0; last[s.key] = null; });
  return dates.map(date => {
    const row = { date };
    trimmed.forEach(s => {
      while (cursors[s.key] < s.data.length && s.data[cursors[s.key]].date <= date) {
        last[s.key] = s.data[cursors[s.key]].value;
        cursors[s.key]++;
      }
      row[s.key] = last[s.key] != null && bases[s.key] ? round1((last[s.key] / bases[s.key]) * 100) : null;
    });
    return row;
  });
}

// Merges two independent {date,value} series into one chart-ready array,
// normalized to 100 at the window's start, forward-filling each series onto
// the union of both sets of dates so two differently-sampled sources (our
// daily index vs smallcase's own series) still plot as continuous lines.
function buildIndexedSeries(oursRaw, theirsRaw, fromDate) {
  const ours = (oursRaw || []).filter(d => d.date >= fromDate);
  const theirs = (theirsRaw || []).filter(d => d.date >= fromDate);
  if (ours.length < 2 && theirs.length < 2) return [];
  const dates = [...new Set([...ours.map(d => d.date), ...theirs.map(d => d.date)])].sort();
  const ourBase = ours[0]?.value;
  const theirBase = theirs[0]?.value;
  let oi = 0, ti = 0, lastOur = null, lastTheir = null;
  return dates.map(date => {
    while (oi < ours.length && ours[oi].date <= date) { lastOur = ours[oi].value; oi++; }
    while (ti < theirs.length && theirs[ti].date <= date) { lastTheir = theirs[ti].value; ti++; }
    return {
      date,
      ours: lastOur != null && ourBase ? round1((lastOur / ourBase) * 100) : null,
      theirs: lastTheir != null && theirBase ? round1((lastTheir / theirBase) * 100) : null,
    };
  });
}

export function Card({ children, style, id }) {
  const pal = usePalette();
  return (
    <div id={id} style={{ background: pal.cardBg, border: `1.5px solid ${pal.border}`, borderRadius: '8px', color: pal.text, ...style }}>
      {children}
    </div>
  );
}

export function SectionTitle({ children, right }) {
  const pal = usePalette();
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0.7rem 0.9rem', borderBottom: `1px solid ${pal.borderSoft}` }}>
      <span style={{ fontSize: '0.78rem', fontWeight: 700, color: pal.heading, textTransform: 'uppercase', letterSpacing: '0.02em', fontFamily: FONT_HEADING }}>{children}</span>
      {right}
    </div>
  );
}

export default function CompetitorAnalysisPage() {
  const pal = usePalette();
  const { isAdmin: userIsAdmin } = _getAdminState();
  const [competitors, setCompetitors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedKey, setSelectedKey] = useState(null);
  const [showFetchInfo, setShowFetchInfo] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [fetchResult, setFetchResult] = useState(null);
  const [compareBasket, setCompareBasket] = useState('Mid_Small_Cap');
  const [ourStocks, setOurStocks] = useState(null);
  const [ourSeries, setOurSeries] = useState(null);
  const [ourProfile, setOurProfile] = useState(null);
  const [ourProfileLoading, setOurProfileLoading] = useState(false);
  const [ourRebalanceHistory, setOurRebalanceHistory] = useState(null);
  const [returnTenure, setReturnTenure] = useState('1Y');
  const [sortMode, setSortMode] = useState('diff');
  const [statusFilter, setStatusFilter] = useState('all'); // all | common | ours | theirs
  const [search, setSearch] = useState('');
  const [stockMetrics, setStockMetrics] = useState({});

  const refresh = () => {
    const token = getAuthToken();
    setLoading(true);
    fetch(`${API_BASE}/admin/competitor-list`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(r => r.json())
      .then(d => {
        setCompetitors(d.competitors || []);
        setSelectedKey(prev => prev || (d.competitors || [])[0]?.key || null);
      })
      .finally(() => setLoading(false));
  };
  // Read-only data is open to every logged-in user (per the webportal's
  // "non-admins view everything, only admins mutate" permission model) --
  // only handleFetchAll (triggering a new scrape) below stays admin-gated.
  useEffect(() => { refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleFetchAll = async () => {
    setFetching(true);
    setFetchResult(null);
    try {
      const token = getAuthToken();
      const resp = await fetch(`${API_BASE}/admin/competitor-fetch-all`, { method: 'POST', headers: token ? { Authorization: `Bearer ${token}` } : {} });
      const data = await resp.json();
      if (!resp.ok) {
        if (resp.status === 502 && /not logged in/i.test(data.detail || '')) setShowFetchInfo(true);
        throw new Error(data.detail || 'Fetch failed');
      }
      setFetchResult(data.results || []);
      refresh();
    } catch (err) {
      setFetchResult([{ ok: false, label: 'Error', error: err.message }]);
    } finally {
      setFetching(false);
    }
  };

  useEffect(() => {
    if (!compareBasket) { setOurStocks(null); setOurSeries(null); setOurProfile(null); setOurRebalanceHistory(null); setOurProfileLoading(false); return; }
    const token = getAuthToken();
    const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};
    fetchBasket(compareBasket).then(d => setOurStocks(d.stocks || [])).catch(() => setOurStocks(null));
    fetch(`${API_BASE}/index-history`, { headers: authHeaders })
      .then(r => r.json())
      .then(d => setOurSeries(d[compareBasket]?.data || null))
      .catch(() => setOurSeries(null));
    // basket-profile's market-cap mix falls back to a LIVE lookup
    // (Screener/Google/NSE cascade) for any stock without a cached cap
    // label -- can take 30-60s cold for a basket with many uncached
    // stocks. ourProfileLoading drives an explicit "still loading market
    // data" message instead of the panels just silently sitting blank,
    // which looked indistinguishable from stuck/broken.
    setOurProfileLoading(true);
    fetch(`${API_BASE}/admin/basket-profile/${compareBasket}`, { headers: authHeaders })
      .then(r => r.json())
      .then(setOurProfile)
      .catch(() => setOurProfile(null))
      .finally(() => setOurProfileLoading(false));
    fetch(`${API_BASE}/admin/basket-rebalance-history/${compareBasket}`, { headers: authHeaders })
      .then(r => r.json())
      .then(d => setOurRebalanceHistory(d.history || []))
      .catch(() => setOurRebalanceHistory(null));
  }, [compareBasket]);

  const selected = competitors.find(c => c.key === selectedKey) || null;

  // `fullRebalanceHistory` (from the "Download rebalance timeline" .xlsx --
  // see competitor_login.diff_historical_constituents) has exact per-stock
  // detail for EVERY historical rebalance since inception, not just the
  // latest -- same shape as ourRebalanceHistory, so it's used as-is when
  // present. Only competitors not yet re-fetched since this was added fall
  // back to the old reconstruction below (exact detail for the latest
  // rebalance only, coarse counts + a collapsed summary for the rest).
  const theirRebalanceHistory = useMemo(() => {
    if (selected?.fullRebalanceHistory?.length) return selected.fullRebalanceHistory;
    if (!selected?.rebalanceTimeline?.length) return [];
    const timeline = [...selected.rebalanceTimeline].sort((a, b) => parseLooseDate(a.date) - parseLooseDate(b.date));
    const rebalanceEvents = timeline.filter(ev => (ev.type || 'rebalance') === 'rebalance');
    const latestDate = rebalanceEvents[rebalanceEvents.length - 1]?.date;
    return timeline.slice().reverse().map(ev => {
      const type = ev.type || 'rebalance';
      if (type === 'launch') {
        return { date: ev.date, type, label: 'smallcase went Live (launch date)' };
      }
      if (type === 'summary') {
        return { date: ev.date, type, rangeStart: ev.rangeStart, rangeEnd: ev.rangeEnd, rebalanceCount: ev.rebalanceCount };
      }
      if (ev.date === latestDate && selected.latestRebalanceDetail?.length) {
        const changes = selected.latestRebalanceDetail.map(e => ({
          name: e.companyName, oldWeight: e.section === 'addition' ? 0 : (e.newWeight + (e.section === 'decrease' ? e.delta : e.section === 'increase' ? -e.delta : 0)),
          newWeight: e.newWeight,
          status: e.section === 'addition' ? 'new' : e.section === 'removal' ? 'removed' : e.section === 'increase' ? 'increased' : 'decreased',
        }));
        const counts = { new: 0, increased: 0, decreased: 0, removed: 0 };
        changes.forEach(c => { counts[c.status] = (counts[c.status] || 0) + 1; });
        return { date: ev.date, type, counts, changes };
      }
      return { date: ev.date, type, counts: { new: ev.assetsAdded || 0, removed: ev.assetsRemoved || 0 }, changes: null };
    });
  }, [selected]);

  // "Did we catch this stock earlier or later than them" (and the same for
  // exits) -- matches EVERY addition/removal event in theirRebalanceHistory
  // (full since-inception history when available, see above) against the
  // full history of when WE ever added/removed that same stock. Matches by
  // NSE code where resolvable, falling back to a normalized name match for
  // rows that predate nseCode resolution. Only kept within
  // TIMING_WINDOW_DAYS of each other -- a stock we and they both bought, but
  // a year apart, isn't a timing insight, it's just the same stock existing
  // in both portfolios at different points.
  const timingInsights = useMemo(
    () => buildTimingInsights(ourRebalanceHistory, theirRebalanceHistory, selected?.stocks, 'new'),
    [ourRebalanceHistory, theirRebalanceHistory, selected]
  );
  const sellTimingInsights = useMemo(
    () => buildTimingInsights(ourRebalanceHistory, theirRebalanceHistory, selected?.stocks, 'removed'),
    [ourRebalanceHistory, theirRebalanceHistory, selected]
  );

  // Batch-fetch real OHLC for every (code, date) pair the timing-insight
  // rows need -- one call covers both buy and sell insights, deduped, so a
  // stock appearing in both lists (or matched against multiple of our own
  // dates) only costs one network fetch per unique pair.
  const [ohlcData, setOhlcData] = useState({});
  useEffect(() => {
    const pairs = [];
    const seen = new Set();
    [...timingInsights, ...sellTimingInsights].forEach(t => {
      if (!t.code) return;
      // Dates come in from two different sources in two different formats
      // (our side: "DD Mon YYYY" from rebalance_history.json; their side:
      // "YYYY-MM-DD" from the xlsx, or "DD Mon, YYYY" from the old
      // fallback) -- normalized to ISO here since that's all the OHLC
      // endpoint (and ohlcData's cache keys below) accepts.
      [t.ourDate, t.theirDate].forEach(date => {
        const iso = toIsoDate(date);
        const k = `${t.code}|${iso}`;
        if (iso && !seen.has(k)) { seen.add(k); pairs.push({ nseCode: t.code, date: iso }); }
      });
    });
    if (!pairs.length) { setOhlcData({}); return; }
    const token = getAuthToken();
    fetch(`${API_BASE}/admin/stock-ohlc-on-date`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ pairs }),
    }).then(r => r.json()).then(d => setOhlcData(d.ohlc || {})).catch(() => setOhlcData({}));
  }, [timingInsights, sellTimingInsights]);

  const returnCompare = useMemo(() => {
    if (!ourSeries || !selected?.performanceSeries?.length) return null;
    const calc = (data) => returnTenure === 'MAX' ? computeSinceInception(data) : computeTenureReturn(data, returnTenure);
    const ours = calc(ourSeries);
    const theirs = calc(selected.performanceSeries);
    if (!ours && !theirs) return null;
    return { ours, theirs };
  }, [ourSeries, selected, returnTenure]);

  const comparisonRows = useMemo(() => {
    if (!selected || !ourStocks || !selected.stocks) return null;
    const rows = new Map();
    (selected.stocks || []).forEach(s => {
      const code = (s.nseCode || `~${s.name}`).toUpperCase();
      rows.set(code, { key: code, name: s.name, nseCode: s.nseCode, sector: s.sector, theirWeight: s.weight || 0, ourWeight: 0 });
    });
    ourStocks.forEach(s => {
      const code = (s.nseCode || `~${s.securityName || ''}`).toUpperCase();
      const ourWeight = (s.allocation || 0) * 100;
      if (rows.has(code)) {
        rows.get(code).ourWeight = ourWeight;
      } else {
        rows.set(code, { key: code, name: s.securityName || s.nseCode, nseCode: s.nseCode, sector: null, theirWeight: 0, ourWeight });
      }
    });
    const list = [...rows.values()].map(r => ({ ...r, diff: r.ourWeight - r.theirWeight }));
    const common = list.filter(r => r.theirWeight > 0 && r.ourWeight > 0);
    const onlyCompetitor = list.filter(r => r.theirWeight > 0 && r.ourWeight === 0);
    const onlyOurs = list.filter(r => r.theirWeight === 0 && r.ourWeight > 0);
    const overlapWeight = common.reduce((sum, r) => sum + r.theirWeight, 0);
    return {
      all: list, common, onlyCompetitor, onlyOurs, overlapWeight,
      theirTop5: topNWeight(selected.stocks, 5), theirTop10: topNWeight(selected.stocks, 10),
      ourTop5: topNWeight(ourStocks.map(s => ({ weight: (s.allocation || 0) * 100 })), 5),
      ourTop10: topNWeight(ourStocks.map(s => ({ weight: (s.allocation || 0) * 100 })), 10),
    };
  }, [selected, ourStocks]);

  const sortedFilteredRows = useMemo(() => {
    if (!comparisonRows) return [];
    const sorters = {
      diff: (a, b) => Math.abs(b.diff) - Math.abs(a.diff),
      ourWeight: (a, b) => b.ourWeight - a.ourWeight,
      theirWeight: (a, b) => b.theirWeight - a.theirWeight,
      name: (a, b) => a.name.localeCompare(b.name),
    };
    let rows = [...comparisonRows.all];
    if (statusFilter === 'common') rows = rows.filter(r => r.ourWeight > 0 && r.theirWeight > 0);
    else if (statusFilter === 'ours') rows = rows.filter(r => r.ourWeight > 0 && r.theirWeight === 0);
    else if (statusFilter === 'theirs') rows = rows.filter(r => r.theirWeight > 0 && r.ourWeight === 0);
    if (search.trim()) {
      const q = search.trim().toUpperCase();
      rows = rows.filter(r => r.name.toUpperCase().includes(q) || (r.nseCode || '').toUpperCase().includes(q));
    }
    return rows.sort(sorters[sortMode] || sorters.diff);
  }, [comparisonRows, statusFilter, search, sortMode]);

  const [metricsLoading, setMetricsLoading] = useState(false);
  useEffect(() => {
    if (!comparisonRows) return;
    const codes = [...new Set(comparisonRows.all.map(r => r.nseCode).filter(Boolean))];
    if (codes.length === 0) return;
    const token = getAuthToken();
    setMetricsLoading(true);
    // Cold cache can genuinely take 1-2 minutes for ~40 stocks (Screener →
    // Google Finance → NSE cascade per stock, 4 at a time) -- the table/
    // avg-market-cap stat show a "Loading…" state rather than a bare "N/A"
    // while this is in flight, so it doesn't read as "data unavailable".
    fetch(`${API_BASE}/admin/stock-metrics`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ codes }),
    }).then(r => r.json()).then(d => setStockMetrics(d.metrics || {})).catch(() => {}).finally(() => setMetricsLoading(false));
  }, [comparisonRows]);

  const theirSectorMix = useMemo(() => {
    if (!selected?.stocks) return null;
    const mix = {};
    selected.stocks.forEach(s => { if (s.sector) mix[s.sector] = (mix[s.sector] || 0) + (s.weight || 0); });
    return mix;
  }, [selected]);

  const sectorChartData = useMemo(() => {
    if (!ourProfile?.sectorMix || !theirSectorMix) return [];
    const keys = [...new Set([...Object.keys(ourProfile.sectorMix), ...Object.keys(theirSectorMix)])]
      .sort((a, b) => (theirSectorMix[b] || ourProfile.sectorMix[b] || 0) - (theirSectorMix[a] || ourProfile.sectorMix[a] || 0))
      .slice(0, 8);
    return keys.map(k => ({ sector: k, Ours: round1(ourProfile.sectorMix[k] || 0), [selected?.label || 'Theirs']: round1(theirSectorMix[k] || 0) }));
  }, [ourProfile, theirSectorMix, selected]);

  const capMixChartData = useMemo(() => {
    if (!ourProfile?.capMix && !selected?.marketCapMix) return [];
    const cm = ourProfile?.capMix || {};
    const tm = selected?.marketCapMix || {};
    return [
      { name: 'Ours', Largecap: round1(cm.Largecap || 0), Midcap: round1(cm.Midcap || 0), Smallcap: round1(cm.Smallcap || 0), Unclassified: round1(cm.Unclassified || 0) },
      { name: selected?.label || 'Theirs', Largecap: round1(tm.Largecap || 0), Midcap: round1(tm.Midcap || 0), Smallcap: round1(tm.Smallcap || 0), Unclassified: round1(tm.Unclassified || 0) },
    ];
  }, [ourProfile, selected]);

  const concentrationData = useMemo(() => {
    if (!comparisonRows || !ourStocks || !selected?.stocks) return [];
    const ourWeights = ourStocks.map(s => (s.allocation || 0) * 100);
    const theirWeights = selected.stocks.map(s => s.weight || 0);
    const ours = concentrationCurve(ourWeights);
    const theirs = concentrationCurve(theirWeights);
    return RANKS.map((n, i) => ({ rank: `Top ${n}`, Ours: ours[i].ours, [selected.label]: theirs[i].ours }));
  }, [comparisonRows, ourStocks, selected]);

  const perfChartData = useMemo(() => {
    if (!returnCompare || !ourSeries || !selected?.performanceSeries) return [];
    const fromDate = returnCompare.ours?.baseDate || returnCompare.theirs?.baseDate;
    if (!fromDate) return [];
    return buildIndexedSeries(ourSeries, selected.performanceSeries, fromDate);
  }, [returnCompare, ourSeries, selected]);

  const drawdownCompare = useMemo(() => ({
    ours: maxDrawdownPct(ourSeries),
    theirs: maxDrawdownPct(selected?.performanceSeries),
  }), [ourSeries, selected]);

  const avgMarketCap = useMemo(() => {
    if (!comparisonRows) return null;
    const weighted = (rows, side) => {
      let num = 0, den = 0;
      rows.forEach(r => {
        const mc = r.nseCode && stockMetrics[r.nseCode.toUpperCase()]?.marketCapCr;
        const w = side === 'our' ? r.ourWeight : r.theirWeight;
        if (mc && w) { num += mc * w; den += w; }
      });
      return den > 0 ? num / den : null;
    };
    const ourRows = comparisonRows.all.filter(r => r.ourWeight > 0);
    const theirRows = comparisonRows.all.filter(r => r.theirWeight > 0);
    return { ours: weighted(ourRows, 'our'), theirs: weighted(theirRows, 'their') };
  }, [comparisonRows, stockMetrics]);

  const differentiation = useMemo(() => {
    if (!comparisonRows || !ourProfile) return null;
    const lines = [];
    lines.push({ icon: 'fa-chart-pie', text: `${comparisonRows.onlyOurs.length} stocks unique to our basket`, tone: null });
    lines.push({ icon: 'fa-link', text: `${comparisonRows.common.length} common holdings (${comparisonRows.overlapWeight.toFixed(1)}% of ${selected.label}'s weight)`, tone: null });
    const concDiff = comparisonRows.ourTop5 - comparisonRows.theirTop5;
    lines.push({ icon: 'fa-layer-group', text: `${Math.abs(concDiff).toFixed(1)}pp ${concDiff >= 0 ? 'higher' : 'lower'} Top-5 concentration`, tone: concDiff >= 0 ? NEG : POS });
    if (ourProfile.capMix && selected.marketCapMix) {
      const smallDiff = (ourProfile.capMix.Smallcap || 0) - (selected.marketCapMix.Smallcap || 0);
      lines.push({ icon: 'fa-seedling', text: `${Math.abs(smallDiff).toFixed(1)}pp ${smallDiff >= 0 ? 'higher' : 'lower'} smallcap exposure`, tone: null });
    }
    if (theirSectorMix && ourProfile.sectorMix) {
      const ourSectors = new Set(Object.keys(ourProfile.sectorMix));
      const theirSectors = new Set(Object.keys(theirSectorMix));
      const sectorsOnlyOurs = [...ourSectors].filter(s => !theirSectors.has(s)).length;
      lines.push({ icon: 'fa-industry', text: `${sectorsOnlyOurs} sectors represented in ours that ${selected.label} has no exposure to`, tone: null });
    }
    if (avgMarketCap?.ours && avgMarketCap?.theirs) {
      const diff = avgMarketCap.ours - avgMarketCap.theirs;
      lines.push({ icon: 'fa-building', text: `₹${Math.abs(diff).toFixed(0)} Cr ${diff >= 0 ? 'higher' : 'lower'} average market cap`, tone: null });
    }
    return lines;
  }, [comparisonRows, ourProfile, selected, theirSectorMix, avgMarketCap]);

  // Condensed, already-computed summary handed to the AI for Rebalance
  // Insights -- every number in here is real (nothing fabricated for the
  // prompt), capped to recent/top entries so the request stays small.
  const insightsSummary = useMemo(() => {
    // Wait for comparisonRows too (not just selected/ourProfile) -- without
    // this, the summary was computing (and firing a whole LLM request) the
    // instant selected+ourProfile landed, then AGAIN moments later once
    // comparisonRows caught up, confirmed live: two sequential ~3-5s LLM
    // calls back to back on every basket/competitor switch, roughly
    // doubling how long the section sat on "Analyzing..." before showing
    // anything. Gating on comparisonRows too means only one request fires.
    if (!selected || !ourProfile || !comparisonRows) return null;
    const fmtChanges = (h) => (h || []).filter(e => (e.type || 'rebalance') === 'rebalance' || !e.type).slice(0, 4).map(e => ({
      date: e.date,
      added: (e.changes || []).filter(c => c.status === 'new').map(c => c.name),
      removed: (e.changes || []).filter(c => c.status === 'removed').map(c => c.name),
      increased: (e.changes || []).filter(c => c.status === 'increased').map(c => `${c.name} ${c.oldWeight?.toFixed(1)}%->${c.newWeight?.toFixed(1)}%`),
      decreased: (e.changes || []).filter(c => c.status === 'decreased').map(c => `${c.name} ${c.oldWeight?.toFixed(1)}%->${c.newWeight?.toFixed(1)}%`),
    }));
    const round2 = (v) => (v == null ? null : Math.round(v * 100) / 100);
    const withPrice = (t) => ({
      stock: t.name, diffDays: t.diffDays,
      ourDate: t.ourDate, ourWeight: round1(t.ourWeight), ourPrice: round2(ohlcAverage(t.code ? ohlcData[`${t.code}|${toIsoDate(t.ourDate)}`] : null)),
      theirDate: t.theirDate, theirWeight: round1(t.theirWeight), theirPrice: round2(ohlcAverage(t.code ? ohlcData[`${t.code}|${toIsoDate(t.theirDate)}`] : null)),
    });
    return {
      ourBasket: BASKET_OPTIONS.find(o => o.key === compareBasket)?.label || compareBasket,
      competitor: `${selected.label} (${selected.manager || 'unknown manager'})`,
      ourRecentChanges: fmtChanges(ourRebalanceHistory),
      competitorRecentChanges: fmtChanges(theirRebalanceHistory),
      timingInsightsBuys: timingInsights.map(withPrice),
      timingInsightsSells: sellTimingInsights.map(withPrice),
      // Returns are sent as already-formatted percentages (not raw 0-1
      // fractions) with the exact date range they cover -- the LLM was
      // echoing the raw unrounded fraction (e.g. "0.4897103658536586")
      // verbatim and never mentioning the period, since the system prompt
      // forbids inventing numbers and it had no clean ones or dates to use.
      returns: returnCompare ? {
        oursPct: round1((returnCompare.ours?.pct || 0) * 100),
        theirsPct: round1((returnCompare.theirs?.pct || 0) * 100),
        periodLabel: returnTenure === 'MAX' ? 'since inception' : returnTenure,
        fromDate: formatReturnDate(returnCompare.ours?.baseDate || returnCompare.theirs?.baseDate),
        toDate: formatReturnDate(returnCompare.ours?.latestDate || returnCompare.theirs?.latestDate),
      } : null,
      sectorMixOurs: ourProfile.sectorMix, sectorMixTheirs: theirSectorMix,
      capMixOurs: ourProfile.capMix, capMixTheirs: selected.marketCapMix,
      stocksOnlyOurs: comparisonRows?.onlyOurs?.slice(0, 8).map(s => ({ name: s.name, weight: round1(s.ourWeight) })),
      stocksOnlyTheirs: comparisonRows?.onlyCompetitor?.slice(0, 8).map(s => ({ name: s.name, weight: round1(s.theirWeight) })),
    };
  }, [selected, ourProfile, compareBasket, ourRebalanceHistory, theirRebalanceHistory, timingInsights, sellTimingInsights, ohlcData, returnCompare, returnTenure, theirSectorMix, comparisonRows]);

  const [insightsState, setInsightsState] = useState({ loading: false, good: null, bad: null, error: null });
  useEffect(() => {
    if (!selectedKey) { setInsightsState({ loading: false, good: null, bad: null, error: null }); return; }
    // A competitor IS selected but insightsSummary hasn't computed yet
    // (waiting on comparisonRows) -- show the loading spinner, not the
    // terminal "not enough data" message, otherwise the page briefly shows
    // a false "no insights" result while other sections are still loading.
    if (!insightsSummary) { setInsightsState(s => s.loading ? s : { loading: true, good: null, bad: null, error: null }); return; }
    let cancelled = false;
    setInsightsState({ loading: true, good: null, bad: null, error: null });
    const token = getAuthToken();
    fetch(`${API_BASE}/admin/rebalance-insights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ basket: compareBasket, competitorKey: selectedKey, summary: insightsSummary }),
    }).then(r => r.json()).then(d => {
      if (cancelled) return;
      if (d.good || d.bad) setInsightsState({ loading: false, good: d.good || [], bad: d.bad || [], error: null });
      else setInsightsState({ loading: false, good: null, bad: null, error: d.error || 'No insights returned.' });
    }).catch(err => { if (!cancelled) setInsightsState({ loading: false, good: null, bad: null, error: String(err) }); });
    return () => { cancelled = true; };
  }, [insightsSummary, compareBasket, selectedKey]);

  const ourBasketLabel = BASKET_OPTIONS.find(o => o.key === compareBasket)?.label || compareBasket;

  const navItems = [
    { id: 'executive', label: 'Overview', icon: 'fa-gauge-high' },
    { id: 'overlap', label: 'Overlap Analysis', icon: 'fa-circle-nodes' },
    { id: 'sector', label: 'Sector Allocation', icon: 'fa-chart-bar' },
    { id: 'marketcap', label: 'Market Cap Analysis', icon: 'fa-building-columns' },
    { id: 'performance', label: 'Performance', icon: 'fa-chart-line' },
    { id: 'stocks', label: 'Stock Comparison', icon: 'fa-table-list' },
    { id: 'rebalance', label: 'Rebalances', icon: 'fa-arrows-rotate' },
    { id: 'rebalance-insights', label: 'Rebalance Insights', icon: 'fa-wand-magic-sparkles' },
    { id: 'timing', label: 'Timing Insights', icon: 'fa-stopwatch' },
    { id: 'multicompare', label: 'Multi-Compare', icon: 'fa-layer-group' },
  ];
  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  // Multi-Compare is a full separate page (CompetitorMultiComparePage.jsx),
  // opened in a new tab rather than an inline section -- it has its own
  // entity picker, data-fetching and every section this page has (plus
  // Rebalance History + Stock Timing Insights, N-way), so it needs real
  // page-width room rather than being squeezed under this 2-way view.
  // "Last synced" -- the most recent of all competitors' lastFetched
  // timestamps (each already a real server-side "01 Oct 2026 11:15 UTC"
  // string set at save time in historical_data.py/main.py), shown to every
  // user so they know how fresh the data is even though only admins can
  // trigger a new fetch.
  const lastSynced = useMemo(() => {
    const stamps = competitors.map(c => c.lastFetched).filter(Boolean);
    if (!stamps.length) return null;
    const latest = stamps.sort((a, b) => parseLooseDate(b) - parseLooseDate(a))[0];
    return formatIst(latest);
  }, [competitors]);

  const navClick = (id) => {
    if (id === 'multicompare') {
      const url = window.location.pathname.replace(/\/competitor-analysis$/, '/competitor-multi-compare') + window.location.search;
      window.open(url, '_blank');
    } else {
      scrollTo(id);
    }
  };

  return (
    <div style={{ minHeight: '100vh', background: pal.pageBg, fontFamily: FONT_BODY, color: pal.text }}>
      {/* ── Dark header ───────────────────────────────────────────────── */}
      <div style={{ background: '#2E3A26', color: '#fff', padding: '0.9rem 1.5rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.8rem' }}>
        <div>
          <div style={{ fontSize: '1.15rem', fontWeight: 800, letterSpacing: '0.04em', fontFamily: FONT_HEADING, color: BRAND_GOLD }}>NIVESHAAY</div>
          <div style={{ fontSize: '0.72rem', color: '#cdbf8a' }}>Competitive Intelligence — Smallcase Portfolio Comparison</div>
          <div style={{ fontSize: '0.68rem', color: '#9aa88a', marginTop: '0.2rem' }}>
            <i className="fa-regular fa-clock" style={{ marginRight: '0.3rem' }} />
            {lastSynced ? `Last synced ${lastSynced}` : 'Not synced yet'}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
          <select value={compareBasket} onChange={e => setCompareBasket(e.target.value)}
            style={{ padding: '0.45rem 0.7rem', borderRadius: '6px', border: '1px solid #6A8557', background: '#3a4a30', color: '#fff', fontSize: '0.82rem' }}>
            {BASKET_OPTIONS.map(o => <option key={o.key} value={o.key} style={{ color: BRAND_INK, background: '#fff' }}>{o.label}</option>)}
          </select>
          <span style={{ color: '#cdbf8a', fontSize: '0.8rem' }}>vs</span>
          <select value={selectedKey || ''} onChange={e => setSelectedKey(e.target.value)}
            style={{ padding: '0.45rem 0.7rem', borderRadius: '6px', border: '1px solid #6A8557', background: '#3a4a30', color: '#fff', fontSize: '0.82rem' }}>
            {competitors.map(c => <option key={c.key} value={c.key} style={{ color: BRAND_INK, background: '#fff' }}>{c.label}{c.manager ? ` — ${c.manager}` : ''}</option>)}
          </select>
          {userIsAdmin && (
            <button onClick={handleFetchAll} disabled={fetching}
              style={{ padding: '0.45rem 0.9rem', borderRadius: '6px', border: 'none', background: '#456232', color: '#fff', fontSize: '0.8rem', fontWeight: 600, cursor: 'pointer', opacity: fetching ? 0.6 : 1 }}>
              <i className={`fa-solid ${fetching ? 'fa-spinner fa-spin' : 'fa-cloud-arrow-down'}`} style={{ marginRight: '0.35rem' }} />
              {fetching ? 'Fetching…' : 'Fetch Competitor Data'}
            </button>
          )}
        </div>
      </div>

      {fetchResult && (
        <div style={{ padding: '0.6rem 1.5rem', background: pal.cardBg, borderBottom: `1px solid ${pal.border}`, fontSize: '0.78rem', color: pal.text }}>
          {fetchResult.map((r, i) => <span key={i} style={{ marginRight: '1rem' }}>{r.ok ? `✓ ${r.label}: ${r.stockCount} stocks` : `✗ ${r.label}: ${r.error}`}</span>)}
        </div>
      )}
      {userIsAdmin && showFetchInfo && <CompetitorLoginModal onClose={() => { setShowFetchInfo(false); refresh(); }} />}

      {loading ? (
        <p style={{ padding: '2rem', color: pal.muted }}>Loading…</p>
      ) : !selected ? (
        <p style={{ padding: '2rem', color: pal.muted }}>No competitor data yet — click "Fetch Competitor Data" above.</p>
      ) : (
        <div style={{ display: 'flex', alignItems: 'flex-start' }}>
          {/* ── Dark sidebar ──────────────────────────────────────────── */}
          <div style={{ width: '180px', flexShrink: 0, background: '#2E3A26', minHeight: 'calc(100vh - 68px)', padding: '0.8rem 0', position: 'sticky', top: 0 }}>
            {navItems.map(n => (
              <button key={n.id} onClick={() => navClick(n.id)}
                style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', width: '100%', padding: '0.55rem 1rem', background: 'transparent', border: 'none', color: '#e8dfc0', fontSize: '0.78rem', textAlign: 'left', cursor: 'pointer' }}
                onMouseEnter={e => e.currentTarget.style.background = '#3a4a30'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <i className={`fa-solid ${n.icon}`} style={{ width: '14px', opacity: 0.8 }} />
                {n.label}
              </button>
            ))}
          </div>

          {/* ── Main content ──────────────────────────────────────────── */}
          <div style={{ flex: 1, padding: '1.2rem', minWidth: 0 }}>

            {/* Executive comparison */}
            <div id="executive" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 320px', gap: '0.9rem', marginBottom: '1rem', scrollMarginTop: '12px' }}>
              <PortfolioCard label={ourBasketLabel} sub="Our basket" color={OURS_COLOR}
                stockCount={ourStocks?.length} top5={comparisonRows?.ourTop5} top10={comparisonRows?.ourTop10}
                avgMcap={avgMarketCap?.ours} capMix={ourProfile?.capMix} sectorCount={ourProfile ? Object.keys(ourProfile.sectorMix || {}).length : null}
                series={ourSeries} />
              <PortfolioCard label={selected.label} sub={selected.manager} color={THEIRS_COLOR}
                stockCount={selected.stocks?.length} top5={comparisonRows?.theirTop5} top10={comparisonRows?.theirTop10}
                avgMcap={avgMarketCap?.theirs} capMix={selected.marketCapMix} sectorCount={theirSectorMix ? Object.keys(theirSectorMix).length : null}
                series={selected.performanceSeries} />
              <Card>
                <SectionTitle>What makes us different</SectionTitle>
                <div style={{ padding: '0.8rem 0.9rem' }}>
                  {differentiation ? differentiation.map((l, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem', padding: '0.35rem 0', fontSize: '0.78rem', color: pal.text }}>
                      <i className={`fa-solid ${l.icon}`} style={{ color: l.tone || pal.heading, marginTop: '0.15rem', width: '14px' }} />
                      <span>{l.text}</span>
                    </div>
                  )) : (
                    <span style={{ fontSize: '0.78rem', color: pal.mutedLight }}>
                      {ourProfileLoading
                        ? 'Loading market data… (a new basket\'s market-cap lookup can take up to a minute the first time)'
                        : 'Loading comparison…'}
                    </span>
                  )}
                </div>
              </Card>
            </div>

            {/* Returns tenure strip */}
            {selected.performanceSeries?.length > 1 && (
              <Card style={{ marginBottom: '1rem', padding: '0.6rem 0.9rem', display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.72rem', fontWeight: 700, color: pal.text, marginRight: '0.3rem' }}>RETURNS</span>
                {['1M', '3M', '6M', '1Y', '3Y', 'MAX'].map(t => (
                  <button key={t} onClick={() => setReturnTenure(t)}
                    style={{ padding: '0.2rem 0.6rem', borderRadius: '5px', fontSize: '0.74rem', cursor: 'pointer', border: `1px solid ${returnTenure === t ? pal.heading : pal.borderSoft}`, background: returnTenure === t ? pal.headerRow : pal.inputBg, color: returnTenure === t ? pal.heading : pal.muted }}>
                    {t === 'MAX' ? 'Since Inception' : t}
                  </button>
                ))}
                {returnCompare?.ours && (
                  <span style={{ fontSize: '0.8rem', marginLeft: '0.6rem', color: returnCompare.ours.pct >= 0 ? POS : NEG, fontWeight: 700 }}>
                    Ours {(returnCompare.ours.pct * 100).toFixed(2)}%
                  </span>
                )}
                {returnCompare?.theirs && (
                  <span style={{ fontSize: '0.8rem', color: returnCompare.theirs.pct >= 0 ? POS : NEG, fontWeight: 700 }}>
                    {selected.label} {(returnCompare.theirs.pct * 100).toFixed(2)}%
                  </span>
                )}
                {returnCompare?.ours && (
                  <span style={{ fontSize: '0.72rem', color: pal.mutedLight }}>
                    ({formatReturnDate(returnCompare.ours.baseDate)} → {formatReturnDate(returnCompare.ours.latestDate)})
                  </span>
                )}
              </Card>
            )}

            {/* Overlap / Sector / Market Cap row */}
            <div id="overlap" style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr 1.4fr', gap: '0.9rem', marginBottom: '1rem', scrollMarginTop: '12px' }}>
              <Card>
                <SectionTitle>Portfolio Overlap</SectionTitle>
                <div style={{ padding: '0.9rem', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                  {comparisonRows && (
                    <>
                      <OverlapVenn ourOnly={comparisonRows.onlyOurs.length} common={comparisonRows.common.length} theirOnly={comparisonRows.onlyCompetitor.length} onClick={setStatusFilter} />
                      <div style={{ width: '100%', marginTop: '0.6rem', fontSize: '0.78rem' }}>
                        <LegendRow color={OURS_COLOR} label={`${comparisonRows.onlyOurs.length} ours only`} onClick={() => setStatusFilter('ours')} />
                        <LegendRow color="#a78bfa" label={`${comparisonRows.common.length} common`} onClick={() => setStatusFilter('common')} />
                        <LegendRow color={THEIRS_COLOR} label={`${comparisonRows.onlyCompetitor.length} theirs only`} onClick={() => setStatusFilter('theirs')} />
                      </div>
                    </>
                  )}
                </div>
              </Card>

              <Card id="sector" style={{ scrollMarginTop: '12px' }}>
                <SectionTitle>Sector Allocation</SectionTitle>
                <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                  {sectorChartData.length > 0 ? (
                    <ResponsiveContainer width="100%" height={260}>
                      <BarChart data={sectorChartData} layout="vertical" margin={{ left: 10, right: 10 }}>
                        <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                        <XAxis type="number" tick={{ fontSize: 10 }} unit="%" />
                        <YAxis type="category" dataKey="sector" tick={{ fontSize: 10 }} width={100} />
                        <Tooltip formatter={v => `${v}%`} />
                        <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                        <Bar dataKey="Ours" fill={OURS_COLOR} barSize={8} />
                        <Bar dataKey={selected.label} fill={THEIRS_COLOR} barSize={8} />
                      </BarChart>
                    </ResponsiveContainer>
                  ) : <EmptyNote text="Sector data unavailable for one or both sides." />}
                </div>
              </Card>

              <Card id="marketcap" style={{ scrollMarginTop: '12px' }}>
                <SectionTitle>Market Cap Allocation</SectionTitle>
                <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                  {capMixChartData.length > 0 ? (
                    <ResponsiveContainer width="100%" height={180}>
                      <BarChart data={capMixChartData} layout="vertical" margin={{ left: 10, right: 10 }}>
                        <XAxis type="number" tick={{ fontSize: 10 }} unit="%" domain={[0, 100]} />
                        <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={90} />
                        <Tooltip formatter={v => `${v}%`} />
                        <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                        <Bar dataKey="Largecap" stackId="a" fill="#2f4323" barSize={22} />
                        <Bar dataKey="Midcap" stackId="a" fill="#6A8557" barSize={22} />
                        <Bar dataKey="Smallcap" stackId="a" fill="#c9d6bb" barSize={22} />
                        <Bar dataKey="Unclassified" stackId="a" fill="#d1d5db" barSize={22} />
                      </BarChart>
                    </ResponsiveContainer>
                  ) : <EmptyNote text="Market-cap mix unavailable for one or both sides." />}
                  <div style={{ marginTop: '0.6rem', fontSize: '0.72rem', color: pal.muted }}>
                    Avg market cap — Ours: {metricsLoading ? 'Loading…' : avgMarketCap?.ours ? `₹${avgMarketCap.ours.toFixed(0)} Cr` : 'N/A'} · {selected.label}: {metricsLoading ? 'Loading…' : avgMarketCap?.theirs ? `₹${avgMarketCap.theirs.toFixed(0)} Cr` : 'N/A'}
                  </div>
                </div>
              </Card>
            </div>

            {/* Performance / Concentration row */}
            <div id="performance" style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: '0.9rem', marginBottom: '1rem', scrollMarginTop: '12px' }}>
              <Card>
                <SectionTitle>Performance Comparison <span style={{ fontWeight: 400, textTransform: 'none', color: pal.mutedLight }}>(indexed to 100)</span></SectionTitle>
                <div style={{ padding: '0.6rem 0.9rem 0', fontSize: '0.78rem', fontWeight: 600 }}>
                  <span style={{ color: pal.text }}>Max Drawdown (since inception):</span>{' '}
                  <span style={{ color: OURS_COLOR }}>Ours {drawdownCompare.ours != null ? `${(drawdownCompare.ours * 100).toFixed(1)}%` : '—'}</span>
                  {' · '}
                  <span style={{ color: THEIRS_COLOR }}>{selected.label} {drawdownCompare.theirs != null ? `${(drawdownCompare.theirs * 100).toFixed(1)}%` : '—'}</span>
                </div>
                <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                  {perfChartData.length > 1 ? (
                    <ResponsiveContainer width="100%" height={260}>
                      <LineChart data={perfChartData}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="date" tick={{ fontSize: 9 }} minTickGap={40} />
                        <YAxis tick={{ fontSize: 10 }} domain={['auto', 'auto']} />
                        <Tooltip />
                        <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                        <Line type="monotone" dataKey="ours" name="Ours" stroke={OURS_COLOR} dot={false} strokeWidth={2} connectNulls />
                        <Line type="monotone" dataKey="theirs" name={selected.label} stroke={THEIRS_COLOR} dot={false} strokeWidth={2} connectNulls />
                      </LineChart>
                    </ResponsiveContainer>
                  ) : <EmptyNote text="Not enough overlapping price history yet." />}
                </div>
              </Card>
              <Card>
                <SectionTitle>Portfolio Concentration</SectionTitle>
                <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                  {concentrationData.length > 0 ? (
                    <ResponsiveContainer width="100%" height={260}>
                      <LineChart data={concentrationData}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="rank" tick={{ fontSize: 9 }} />
                        <YAxis tick={{ fontSize: 10 }} unit="%" domain={[0, 100]} />
                        <Tooltip formatter={v => `${v}%`} />
                        <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                        <Line type="monotone" dataKey="Ours" stroke={OURS_COLOR} strokeWidth={2} />
                        <Line type="monotone" dataKey={selected.label} stroke={THEIRS_COLOR} strokeWidth={2} />
                      </LineChart>
                    </ResponsiveContainer>
                  ) : <EmptyNote text="Not enough holdings data yet." />}
                </div>
              </Card>
            </div>

            {/* Stock-by-stock comparison */}
            <Card id="stocks" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
              <SectionTitle right={<span style={{ fontWeight: 400, textTransform: 'none', color: pal.mutedLight, fontSize: '0.74rem' }}>{sortedFilteredRows.length} of {comparisonRows?.all.length || 0}</span>}>
                Stock-by-Stock Comparison
              </SectionTitle>
              <div style={{ display: 'flex', gap: '0.5rem', padding: '0.6rem 0.9rem', borderBottom: `1px solid ${pal.borderSoft}`, flexWrap: 'wrap' }}>
                <input placeholder="Search stock or ticker…" value={search} onChange={e => setSearch(e.target.value)}
                  style={{ flex: 1, minWidth: '180px', padding: '0.35rem 0.6rem', borderRadius: '6px', border: `1px solid ${pal.borderSoft}`, fontSize: '0.78rem', background: pal.inputBg, color: pal.inputText }} />
                <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
                  style={{ padding: '0.35rem 0.6rem', borderRadius: '6px', border: `1px solid ${pal.borderSoft}`, fontSize: '0.78rem', background: pal.inputBg, color: pal.inputText }}>
                  <option value="all" style={{ color: BRAND_INK, background: '#fff' }}>All Stocks</option>
                  <option value="common" style={{ color: BRAND_INK, background: '#fff' }}>Common</option>
                  <option value="ours" style={{ color: BRAND_INK, background: '#fff' }}>Ours Only</option>
                  <option value="theirs" style={{ color: BRAND_INK, background: '#fff' }}>{selected.label} Only</option>
                </select>
                <select value={sortMode} onChange={e => setSortMode(e.target.value)}
                  style={{ padding: '0.35rem 0.6rem', borderRadius: '6px', border: `1px solid ${pal.borderSoft}`, fontSize: '0.78rem', background: pal.inputBg, color: pal.inputText }}>
                  <option value="diff" style={{ color: BRAND_INK, background: '#fff' }}>Sort: Weight Difference</option>
                  <option value="theirWeight" style={{ color: BRAND_INK, background: '#fff' }}>Sort: Their Weight</option>
                  <option value="ourWeight" style={{ color: BRAND_INK, background: '#fff' }}>Sort: Our Weight</option>
                  <option value="name" style={{ color: BRAND_INK, background: '#fff' }}>Sort: Name</option>
                </select>
              </div>
              <div style={{ maxHeight: '460px', overflowY: 'auto' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr 85px 85px 85px 75px 90px 100px', gap: '0.5rem', padding: '0.5rem 0.9rem', background: pal.headerRow, fontSize: '0.66rem', fontWeight: 700, color: pal.muted, textTransform: 'uppercase', position: 'sticky', top: 0, borderBottom: `1px solid ${pal.border}` }}>
                  <span>Stock</span><span>Sector</span><span style={{ textAlign: 'right' }}>Mkt Cap</span><span style={{ textAlign: 'right' }}>P/E</span><span style={{ textAlign: 'right' }}>Ours</span><span style={{ textAlign: 'right' }}>Theirs</span><span style={{ textAlign: 'right' }}>Diff</span><span style={{ textAlign: 'center' }}>Status</span>
                </div>
                {sortedFilteredRows.map((r, i) => {
                  const metrics = r.nseCode ? stockMetrics[r.nseCode.toUpperCase()] : null;
                  const status = r.ourWeight > 0 && r.theirWeight > 0
                    ? { label: 'Common', bg: pal.headerRow, fg: pal.heading }
                    : r.ourWeight > 0 ? { label: 'Ours only', bg: pal.headerRow, fg: OURS_COLOR }
                    : { label: 'Theirs only', bg: pal.headerRow, fg: THEIRS_COLOR };
                  return (
                    <div key={r.key} style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr 85px 85px 85px 75px 90px 100px', gap: '0.5rem', padding: '0.4rem 0.9rem', fontSize: '0.76rem', alignItems: 'center', background: i % 2 ? pal.rowA : pal.rowB, borderTop: `1px solid ${pal.rowDivider}` }}>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: pal.text }}>{r.name} {r.nseCode && <span style={{ opacity: 0.5 }}>({r.nseCode})</span>}</span>
                      <span style={{ color: pal.muted, fontSize: '0.72rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.sector || '—'}</span>
                      <span style={{ textAlign: 'right', color: pal.muted }}>{metrics?.marketCapCr ? `₹${Math.round(metrics.marketCapCr)}Cr` : metricsLoading ? '…' : '—'}</span>
                      <span style={{ textAlign: 'right', color: pal.muted }}>{metrics?.peRatio ? metrics.peRatio.toFixed(1) : metricsLoading ? '…' : '—'}</span>
                      <span style={{ textAlign: 'right', color: pal.muted }}>{r.ourWeight ? `${r.ourWeight.toFixed(2)}%` : '0%'}</span>
                      <span style={{ textAlign: 'right', color: pal.muted }}>{r.theirWeight ? `${r.theirWeight.toFixed(2)}%` : '0%'}</span>
                      <span style={{ textAlign: 'right', fontWeight: 600, color: r.diff > 0 ? POS : r.diff < 0 ? NEG : pal.muted }}>{r.diff > 0 ? '+' : ''}{r.diff.toFixed(2)}</span>
                      <span style={{ textAlign: 'center' }}><span style={{ padding: '0.12rem 0.5rem', borderRadius: '999px', fontSize: '0.64rem', fontWeight: 700, background: status.bg, color: status.fg }}>{status.label}</span></span>
                    </div>
                  );
                })}
              </div>
            </Card>

            {/* Rebalance history -- side by side, each row using a fixed-width
                value column (not flex space-between) so the "Increased
                4.0% -> 5.0%" text never wraps onto its own line even at
                half-card width; the stock name truncates with an ellipsis
                instead if it's the one that doesn't fit. */}
            <Card id="rebalance" style={{ scrollMarginTop: '12px' }}>
              <SectionTitle>Rebalance History</SectionTitle>
              <div style={{ padding: '0.9rem', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.2rem' }}>
                <RebalanceHistoryPanel label={ourBasketLabel} color={OURS_COLOR} history={ourRebalanceHistory} />
                <RebalanceHistoryPanel label={selected.label} color={THEIRS_COLOR} history={theirRebalanceHistory}
                  limitedNote="smallcase only exposes exact stock-level detail for the most recent rebalance -- older dates show added/removed counts only." />
              </div>
            </Card>

            <Card id="rebalance-insights" style={{ scrollMarginTop: '12px' }}>
              <SectionTitle right={<span style={{ fontWeight: 400, textTransform: 'none', color: pal.mutedLight, fontSize: '0.74rem' }}>AI-generated, grounded only in the data on this page</span>}>
                Rebalance Insights
              </SectionTitle>
              <div style={{ padding: '0.9rem' }}>
                {insightsState.loading ? (
                  <span style={{ fontSize: '0.78rem', color: pal.mutedLight }}>
                    <i className="fa-solid fa-spinner fa-spin" style={{ marginRight: '0.4rem' }} />
                    Analyzing rebalance history… (can take up to 15s the first time for a new basket/competitor pairing; instant afterwards)
                  </span>
                ) : insightsState.error ? (
                  <span style={{ fontSize: '0.78rem', color: pal.mutedLight }}>Could not generate insights: {insightsState.error}</span>
                ) : (insightsState.good || insightsState.bad) ? (
                  (insightsState.good?.length || insightsState.bad?.length) ? (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                      <InsightsTable title="What We Did Better" tone={POS} icon="fa-circle-check" items={insightsState.good} emptyText="Nothing stood out here." />
                      <InsightsTable title="Where The Competitor Did Better" tone={NEG} icon="fa-circle-exclamation" items={insightsState.bad} emptyText="Nothing stood out here." />
                    </div>
                  ) : (
                    <EmptyNote text="Not enough rebalance history yet to generate insights." />
                  )
                ) : (
                  <EmptyNote text="Not enough rebalance history yet to generate insights." />
                )}
              </div>
            </Card>

            <Card id="timing" style={{ scrollMarginTop: '12px' }}>
              <SectionTitle right={<span style={{ fontWeight: 400, textTransform: 'none', color: pal.mutedLight, fontSize: '0.74rem' }}>within {TIMING_WINDOW_DAYS} days of each other, since inception</span>}>
                Stock Timing Insights
              </SectionTitle>
              <div style={{ padding: '0.9rem' }}>
                <div style={{ fontSize: '0.72rem', fontWeight: 700, color: pal.heading, marginBottom: '0.3rem' }}>Buys</div>
                {timingInsights.length === 0 ? (
                  <EmptyNote text={`No stock was added by both ${ourBasketLabel} and ${selected.label} within ${TIMING_WINDOW_DAYS} days of each other.`} />
                ) : timingInsights.map(t => (
                  <TimingInsightRow key={`buy-${t.name}-${t.theirDate}`} t={t} action="added" ourBasketLabel={ourBasketLabel} selected={selected} ohlcData={ohlcData} />
                ))}
                <div style={{ fontSize: '0.72rem', fontWeight: 700, color: pal.heading, margin: '0.8rem 0 0.3rem' }}>Sells</div>
                {sellTimingInsights.length === 0 ? (
                  <EmptyNote text={`No stock was removed by both ${ourBasketLabel} and ${selected.label} within ${TIMING_WINDOW_DAYS} days of each other.`} />
                ) : sellTimingInsights.map(t => (
                  <TimingInsightRow key={`sell-${t.name}-${t.theirDate}`} t={t} action="removed" ourBasketLabel={ourBasketLabel} selected={selected} ohlcData={ohlcData} />
                ))}
              </div>
            </Card>

            <Card id="discovery" style={{ scrollMarginTop: '12px' }}>
              <SectionTitle>Top Unique Holdings</SectionTitle>
              <div style={{ padding: '0.9rem', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                <UniqueHoldingsList title="Ours Only" color={OURS_COLOR} items={[...(comparisonRows?.onlyOurs || [])].sort((a, b) => b.ourWeight - a.ourWeight).slice(0, 6)} weightKey="ourWeight" />
                <UniqueHoldingsList title={`${selected.label} Only`} color={THEIRS_COLOR} items={[...(comparisonRows?.onlyCompetitor || [])].sort((a, b) => b.theirWeight - a.theirWeight).slice(0, 6)} weightKey="theirWeight" />
              </div>
            </Card>

          </div>
        </div>
      )}
    </div>
  );
}

export function PortfolioCard({ label, sub, color, stockCount, top5, top10, avgMcap, capMix, sectorCount, series }) {
  const pal = usePalette();
  const periods = ['1M', '3M', '6M', '1Y'];
  const returns = periods.map(t => ({ t, r: computeTenureReturn(series, t) }));
  return (
    <Card>
      <div style={{ padding: '0.8rem 0.9rem', borderBottom: `1px solid ${pal.borderSoft}`, display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <i className="fa-solid fa-chart-line" style={{ color }} />
        <div>
          <div style={{ fontSize: '0.88rem', fontWeight: 700, color: pal.heading, fontFamily: FONT_HEADING }}>{label}</div>
          <div style={{ fontSize: '0.7rem', color: pal.mutedLight }}>{sub}</div>
        </div>
      </div>
      <div style={{ padding: '0.8rem 0.9rem' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem', marginBottom: '0.7rem' }}>
          <Stat label="Stocks" value={stockCount ?? '—'} />
          <Stat label="Top 5" value={top5 != null ? `${top5.toFixed(1)}%` : '—'} />
          <Stat label="Top 10" value={top10 != null ? `${top10.toFixed(1)}%` : '—'} />
          <Stat label="Avg Mkt Cap" value={avgMcap ? `₹${avgMcap.toFixed(0)}Cr` : '—'} />
        </div>
        {capMix && (
          <div style={{ display: 'flex', gap: '0.7rem', fontSize: '0.7rem', color: pal.muted, marginBottom: '0.6rem', flexWrap: 'wrap' }}>
            <span>{(capMix.Smallcap || 0).toFixed(0)}% Small</span>
            <span>{(capMix.Midcap || 0).toFixed(0)}% Mid</span>
            <span>{(capMix.Largecap || 0).toFixed(0)}% Large</span>
            {capMix.Unclassified > 0 && <span title="No reliable market-cap label on record for these stocks and live lookup also failed" style={{ color: pal.gold }}>{capMix.Unclassified.toFixed(0)}% Unclassified</span>}
            {sectorCount != null && <span>{sectorCount} Sectors</span>}
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'space-between', borderTop: `1px solid ${pal.borderSoft}`, paddingTop: '0.5rem' }}>
          {returns.map(({ t, r }) => (
            <div key={t} style={{ textAlign: 'center' }}>
              <div style={{ fontSize: '0.66rem', color: pal.mutedLight }}>{t}</div>
              <div style={{ fontSize: '0.78rem', fontWeight: 700, color: r ? (r.pct >= 0 ? POS : NEG) : pal.mutedLight }}>{r ? `${(r.pct * 100).toFixed(1)}%` : '—'}</div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

function Stat({ label, value }) {
  const pal = usePalette();
  return (
    <div>
      <div style={{ fontSize: '0.68rem', color: pal.mutedLight }}>{label}</div>
      <div style={{ fontSize: '0.84rem', fontWeight: 700, color: pal.text }}>{value}</div>
    </div>
  );
}

export function EmptyNote({ text }) {
  const pal = usePalette();
  return <div style={{ fontSize: '0.78rem', color: pal.mutedLight, padding: '1rem 0', textAlign: 'center' }}>{text}</div>;
}

function LegendRow({ color, label, onClick }) {
  const pal = usePalette();
  return (
    <div onClick={onClick} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', padding: '0.15rem 0', cursor: 'pointer' }}>
      <span style={{ width: '9px', height: '9px', borderRadius: '2px', background: color, flexShrink: 0 }} />
      <span style={{ color: pal.text }}>{label}</span>
    </div>
  );
}

// Two overlapping circles sized/positioned to roughly reflect the overlap
// ratio -- not area-exact, just a clean visual approximation (clicking each
// region filters the stock table below via statusFilter).
function OverlapVenn({ ourOnly, common, theirOnly, onClick }) {
  const pal = usePalette();
  const total = ourOnly + common + theirOnly || 1;
  const overlapFrac = common / total;
  const offset = 90 - overlapFrac * 70;
  return (
    <svg width="220" height="140" viewBox="0 0 220 140">
      <circle cx={110 - offset / 2} cy="70" r="55" fill={OURS_COLOR} opacity="0.55" onClick={() => onClick('ours')} style={{ cursor: 'pointer' }} />
      <circle cx={110 + offset / 2} cy="70" r="55" fill={THEIRS_COLOR} opacity="0.55" onClick={() => onClick('theirs')} style={{ cursor: 'pointer' }} />
      <text x={110 - offset} y="75" textAnchor="middle" fontSize="16" fontWeight="700" fill={pal.text}>{ourOnly}</text>
      <text x={110} y="75" textAnchor="middle" fontSize="16" fontWeight="700" fill={pal.text} onClick={() => onClick('common')} style={{ cursor: 'pointer' }}>{common}</text>
      <text x={110 + offset} y="75" textAnchor="middle" fontSize="16" fontWeight="700" fill={pal.text}>{theirOnly}</text>
    </svg>
  );
}

const _STATUS_TONE = { new: POS, increased: POS, decreased: NEG, removed: NEG };
const _STATUS_LABEL = { new: 'New', increased: 'Increased', decreased: 'Reduced', removed: 'Removed' };

// Full clickable rebalance date list -- expanding a date shows which stocks
// were added/increased/decreased/removed on it, when that detail is known.
// Our own baskets have full history (rebalance_history.json records every
// date); competitors only ever expose exact detail for their most recent
// rebalance (see limitedNote), older dates show counts only.
export function RebalanceHistoryPanel({ label, color, history, limitedNote }) {
  const pal = usePalette();
  const [expanded, setExpanded] = useState(null);
  return (
    <div>
      <div style={{ fontSize: '0.8rem', fontWeight: 700, color, marginBottom: '0.5rem' }}>{label}</div>
      {!history ? (
        <span style={{ fontSize: '0.76rem', color: pal.mutedLight }}>Loading…</span>
      ) : history.length === 0 ? (
        <span style={{ fontSize: '0.76rem', color: pal.mutedLight }}>No rebalance recorded</span>
      ) : (
        <div style={{ maxHeight: '340px', overflowY: 'auto' }}>
          {history.map(h => {
            const type = h.type || 'rebalance';
            if (type === 'launch') {
              return (
                <div key={h.date} style={{ display: 'flex', justifyContent: 'space-between', padding: '0.4rem 0.1rem', fontSize: '0.74rem', borderBottom: `1px solid ${pal.borderSoft}` }}>
                  <span style={{ color: pal.mutedLight, fontStyle: 'italic' }}><i className="fa-solid fa-flag" style={{ marginRight: '0.4rem' }} />{h.label}</span>
                  <span style={{ color: pal.mutedLight }}>{h.date}</span>
                </div>
              );
            }
            if (type === 'summary') {
              return (
                <div key={h.date} style={{ padding: '0.4rem 0.1rem', fontSize: '0.72rem', color: pal.mutedLight, fontStyle: 'italic', borderBottom: `1px solid ${pal.borderSoft}` }}
                  title="smallcase's own timeline widget collapses this range into a count and doesn't expose individual historical dates here.">
                  <i className="fa-solid fa-circle-info" style={{ marginRight: '0.4rem' }} />
                  {h.rebalanceCount} rebalance{h.rebalanceCount === 1 ? '' : 's'} between {h.rangeStart} and {h.rangeEnd} (date-level detail not exposed by smallcase)
                </div>
              );
            }
            const isOpen = expanded === h.date;
            return (
              <div key={h.date} style={{ borderBottom: `1px solid ${pal.borderSoft}` }}>
                <div onClick={() => setExpanded(isOpen ? null : h.date)}
                  style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.4rem 0.1rem', cursor: 'pointer' }}>
                  <span style={{ fontSize: '0.76rem', color: pal.text, fontWeight: isOpen ? 700 : 400 }}>
                    <i className={`fa-solid fa-caret-${isOpen ? 'down' : 'right'}`} style={{ marginRight: '0.4rem', fontSize: '0.68rem', color: pal.mutedLight }} />
                    {h.date}
                  </span>
                  <span style={{ display: 'flex', gap: '0.5rem', fontSize: '0.68rem' }}>
                    {!!h.counts.new && <span style={{ color: POS }}>+{h.counts.new} new</span>}
                    {!!h.counts.increased && <span style={{ color: POS }}>{h.counts.increased} up</span>}
                    {!!h.counts.decreased && <span style={{ color: NEG }}>{h.counts.decreased} down</span>}
                    {!!h.counts.removed && <span style={{ color: NEG }}>-{h.counts.removed} out</span>}
                  </span>
                </div>
                {isOpen && (
                  <div style={{ padding: '0.3rem 0.1rem 0.6rem 1.2rem' }}>
                    {h.changes ? h.changes.map(c => (
                      <div key={c.nseCode || c.name} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 150px', gap: '0.5rem', fontSize: '0.7rem', padding: '0.12rem 0' }}>
                        <span style={{ color: pal.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.name}</span>
                        <span style={{ color: _STATUS_TONE[c.status], fontWeight: 600, textAlign: 'right', whiteSpace: 'nowrap' }}>
                          {_STATUS_LABEL[c.status]}{c.oldWeight != null && c.newWeight != null ? ` ${c.oldWeight.toFixed(1)}% → ${c.newWeight.toFixed(1)}%` : ''}
                        </span>
                      </div>
                    )) : (
                      <span style={{ fontSize: '0.7rem', color: pal.mutedLight, fontStyle: 'italic' }}>{limitedNote || 'Stock-level detail not available for this date.'}</span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function InsightsTable({ title, tone, icon, items, emptyText }) {
  const pal = usePalette();
  return (
    <div style={{ border: `1px solid ${pal.borderSoft}`, borderRadius: '10px', overflow: 'hidden' }}>
      <div style={{ padding: '0.5rem 0.8rem', background: `${tone}1a`, borderBottom: `1px solid ${pal.borderSoft}`, display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
        <i className={`fa-solid ${icon}`} style={{ color: tone, fontSize: '0.8rem' }} />
        <span style={{ fontSize: '0.74rem', fontWeight: 700, color: tone, textTransform: 'uppercase', letterSpacing: '0.02em' }}>{title}</span>
      </div>
      <div style={{ padding: '0.7rem 0.9rem' }}>
        {(!items || items.length === 0) ? (
          <span style={{ fontSize: '0.76rem', color: pal.mutedLight }}>{emptyText}</span>
        ) : (
          <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '0.55rem' }}>
            {items.map((s, i) => (
              <li key={i} style={{ fontSize: '0.8rem', color: pal.text, lineHeight: 1.55, display: 'flex', gap: '0.5rem' }}>
                <i className={`fa-solid ${icon}`} style={{ color: tone, fontSize: '0.7rem', marginTop: '0.3rem', flexShrink: 0 }} />
                <span>{s}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function UniqueHoldingsList({ title, color, items, weightKey }) {
  const pal = usePalette();
  const max = Math.max(1, ...items.map(i => i[weightKey]));
  return (
    <div>
      <div style={{ fontSize: '0.72rem', fontWeight: 700, color, marginBottom: '0.5rem' }}>{title}</div>
      {items.length === 0 ? <span style={{ fontSize: '0.75rem', color: pal.mutedLight }}>None</span> : items.map(it => (
        <div key={it.key} style={{ marginBottom: '0.4rem' }}>
          <div style={{ fontSize: '0.74rem', color: pal.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.name}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
            <div style={{ flex: 1, height: '6px', background: pal.pageBg, borderRadius: '3px', overflow: 'hidden' }}>
              <div style={{ width: `${(it[weightKey] / max) * 100}%`, height: '100%', background: color }} />
            </div>
            <span style={{ fontSize: '0.7rem', fontWeight: 700, color: pal.text, width: '36px', textAlign: 'right' }}>{it[weightKey].toFixed(1)}%</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// A single representative price per day -- the mean of Open/High/Low/Close
// -- rather than 4 separate columns, per request: "I want average of ohlc
// ... so that i get an average price for that day".
function ohlcAverage(ohlc) {
  if (!ohlc || ohlc.open == null || ohlc.high == null || ohlc.low == null || ohlc.close == null) return null;
  return (ohlc.open + ohlc.high + ohlc.low + ohlc.close) / 4;
}

function OhlcLine({ label, color, ohlc, requestedDate, weight }) {
  const pal = usePalette();
  const weightText = weight != null ? ` · ${weight.toFixed(2)}% weight` : '';
  if (!ohlc) return (
    <div style={{ fontSize: '0.68rem', color: pal.mutedLight }}>{label}: price unavailable{weightText}</div>
  );
  const pricedLater = ohlc.date && ohlc.date !== requestedDate;
  const avg = ohlcAverage(ohlc);
  return (
    <div style={{ fontSize: '0.68rem', color: pal.muted }}>
      <strong style={{ color }}>{label}</strong> ₹{avg != null ? avg.toFixed(2) : '—'}{weightText}
      {pricedLater && <span title="Requested date had no trading session; priced at the next trading day"> (as of {ohlc.date})</span>}
    </div>
  );
}

export const MULTI_PALETTE = [OURS_COLOR, THEIRS_COLOR, '#2563eb', '#dc2626', '#7c3aed', '#0891b2'];
export const MULTI_CAP_BUCKETS = ['Largecap', 'Midcap', 'Smallcap', 'Multicap', 'Unclassified'];
export const MULTI_MAX_ENTITIES = 6;

export function TimingInsightRow({ t, action, ourBasketLabel, selected, ohlcData }) {
  const pal = usePalette();
  const ourOhlc = t.code ? ohlcData[`${t.code}|${toIsoDate(t.ourDate)}`] : null;
  const theirOhlc = t.code ? ohlcData[`${t.code}|${toIsoDate(t.theirDate)}`] : null;
  return (
    <div style={{ padding: '0.5rem 0', borderBottom: `1px solid ${pal.borderSoft}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', fontSize: '0.78rem' }}>
        <i className={`fa-solid ${t.diffDays > 0 ? 'fa-forward' : t.diffDays < 0 ? 'fa-backward' : 'fa-equals'}`}
          style={{ color: t.diffDays >= 0 ? OURS_COLOR : THEIRS_COLOR, width: '16px' }} />
        <span style={{ color: pal.text }}>
          <strong>{t.name}</strong> — we {action} it {action === 'added' ? 'to' : 'from'} {ourBasketLabel} on {t.ourDate}, {
            t.diffDays === 0 ? `the same day ${selected.label} ${action} it (${t.theirDate})`
            : t.diffDays > 0 ? `${t.diffDays} day${t.diffDays === 1 ? '' : 's'} before ${selected.label} did (${t.theirDate})`
            : `${Math.abs(t.diffDays)} day${Math.abs(t.diffDays) === 1 ? '' : 's'} after ${selected.label} did (${t.theirDate})`
          }.
        </span>
      </div>
      <div style={{ marginLeft: '22px' }}>
        <OhlcLine label="Ours" color={OURS_COLOR} ohlc={ourOhlc} requestedDate={toIsoDate(t.ourDate)} weight={t.ourWeight} />
        <OhlcLine label={selected.label} color={THEIRS_COLOR} ohlc={theirOhlc} requestedDate={toIsoDate(t.theirDate)} weight={t.theirWeight} />
      </div>
    </div>
  );
}
