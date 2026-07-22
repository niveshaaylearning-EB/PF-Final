import { API_BASE } from './api/base.js';
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import './App.css';
import { fetchBasket, fetchLiveData, fetchLiveStock, saveBasket, fetchBasketStockMap, fetchPerformanceBatch } from './api/client.js';

import Header, { BASKET_OPTIONS } from './components/Header.jsx';
import KPIPanel         from './components/KPIPanel.jsx';
import PortfolioTable   from './components/PortfolioTable.jsx';
import InsightsSidebar  from './components/InsightsSidebar.jsx';
import ConfirmModal     from './components/ConfirmModal.jsx';
import WhatIfModal from './components/WhatIfModal.jsx';
import WhatIfAddStockBar from './components/WhatIfAddStockBar.jsx';
import WhatIfImpactBanner from './components/WhatIfImpactBanner.jsx';
import { EMPTY_SLOT, mergeSimForDisplay, computeWhatIf, buildTouchedDetails } from './whatIfCalc.js';
import LoadProgress     from './components/LoadProgress.jsx';
import SoldStocksTable  from './components/SoldStocksTable.jsx';
import BuyPricePage          from './components/BuyPricePage.jsx';
import CalculateReturnPage   from './components/CalculateReturnPage.jsx';
import PLStatementPage       from './components/PLStatementPage.jsx';
import CorporateActionsPage  from './components/CorporateActionsPage.jsx';
import DashboardView         from './components/DashboardView.jsx';

// ── Formatters ───────────────────────────────────────────────────────────────
export const formatPercent = (v) =>
  v == null || isNaN(v) ? '-' : (v * 100).toFixed(2) + '%';

export const formatRupee = (v) =>
  v == null || isNaN(v)
    ? '-'
    : '\u20B9' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });

export const getColorClass = (v) =>
  v == null || isNaN(v) ? 'neutral' : v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral';

// ── Helpers ───────────────────────────────────────────────────────────────────
export const calcPerformance = (open1M, close1M) => {
  if (open1M != null && close1M != null && open1M !== 0)
    return (close1M - open1M) / open1M;
  return null;
};

export const calcContribution = (allocation, performance) => {
  if (allocation != null && performance != null)
    return allocation * performance;
  return null;
};

export const calcAbsoluteReturns = (cmp, buyPrice) => {
  if (cmp != null && buyPrice != null && buyPrice !== 0)
    return (cmp - buyPrice) / buyPrice;
  return null;
};

const _parseEvDates = (s) =>
  (s || '').trim().split('\n').flatMap(l => {
    const p = l.split('*');
    if (p.length !== 2) return [];
    const d = new Date(p[0].trim()); const q = parseFloat(p[1]);
    return (!isNaN(d) && !isNaN(q)) ? [{ d, q }] : [];
  });

const calcHoldingDays = (buyEvents, sellEvents) => {
  const buys  = _parseEvDates(buyEvents).map(e  => ({ ...e, t: 'buy'  }));
  const sells = _parseEvDates(sellEvents).map(e => ({ ...e, t: 'sell' }));
  const all   = [...buys, ...sells].sort((a, b) => a.d - b.d);
  if (!all.length) return null;
  let wt = 0, lastEntry = null;
  for (const ev of all) {
    if (ev.t === 'buy') { if (wt <= 0) lastEntry = ev.d; wt += ev.q; }
    else wt = Math.max(0, wt - ev.q);
  }
  if (!lastEntry) return null;
  return Math.floor((Date.now() - lastEntry.getTime()) / 86_400_000);
};

// IPO_Recommendations stocks have no buyEvents at all (only a listingDate),
// so calcHoldingDays(buyEvents) always returned null for them -- this parses
// the mixed date formats actually present in that basket's data ("28-Oct-2024",
// "30-07-2025") and computes days-since-listing directly.
const _parseListingDate = (s) => {
  if (!s) return null;
  const str = s.trim();
  const monthMap = { jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11 };
  let m = str.match(/^(\d{1,2})[-\s]([A-Za-z]{3})[-\s](\d{4})$/);
  if (m) {
    const mi = monthMap[m[2].toLowerCase()];
    if (mi != null) return new Date(Number(m[3]), mi, Number(m[1]));
    return null;
  }
  m = str.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$/);
  if (m) return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
  m = str.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(str);
  return isNaN(d) ? null : d;
};

const calcDaysSinceListing = (listingDateStr) => {
  const d = _parseListingDate(listingDateStr);
  if (!d) return null;
  return Math.floor((Date.now() - d.getTime()) / 86_400_000);
};

// Build history from buyPriceDetails client-side (fallback if server omits it)
export const parseEventLines = (str) => {
  if (!str) return [];
  return str.trim().split('\n').flatMap(line => {
    const parts = line.split('*');
    if (parts.length !== 2) return [];
    const date = parts[0].trim();
    const qty  = parseFloat(parts[1].trim());
    return isNaN(qty) ? [] : [{ date, qty }];
  });
};

// Mirrors the backend's _current_series_buy_events (buy_price_gains.py) exactly:
// returns {date, weight} for every buy lot still open in the CURRENT series,
// FIFO-consuming the oldest lot(s) first on each sell so a later PARTIAL sell
// correctly reduces an earlier lot's remaining weight (not just tracked via a
// net-total reset-on-full-exit check, which silently left every buy lot at
// its full original weight even after part of it had been sold). A full exit
// naturally "resets" the series since the next buy appends to an empty list.
export const currentSeriesBuyEvents = (buyEventsStr, sellEventsStr) => {
  const combined = [
    ...parseEventLines(buyEventsStr).map(e => ({ ...e, type: 'buy' })),
    ...parseEventLines(sellEventsStr).map(e => ({ ...e, type: 'sell' })),
  ].sort((a, b) => dateToTs(a.date) - dateToTs(b.date));

  let lots = [];
  for (const ev of combined) {
    if (ev.type === 'buy') {
      lots.push({ date: ev.date, remaining: Math.round(ev.qty * 1e6) / 1e6 });
    } else {
      let toConsume = ev.qty;
      for (const lot of lots) {
        if (toConsume <= 1e-6) break;
        const take = Math.min(lot.remaining, toConsume);
        lot.remaining = Math.round((lot.remaining - take) * 1e6) / 1e6;
        toConsume = Math.round((toConsume - take) * 1e6) / 1e6;
      }
      lots = lots.filter(l => l.remaining > 1e-6);
    }
  }
  return lots.map(l => ({ date: l.date, weight: l.remaining }));
};

const dateToTs = (dateStr) => {
  const d = new Date(dateStr);
  return isNaN(d) ? 0 : d.getTime();
};

const buildHistoryFromDetails = (buyPriceDetails) => {
  const history = {};
  for (const [nse, det] of Object.entries(buyPriceDetails || {})) {
    const buys  = parseEventLines(det?.buyEvents);
    const sells = parseEventLines(det?.sellEvents);
    if (!buys.length && !sells.length) continue;
    const combined = [
      ...buys.map(e  => ({ date: e.date, note: `Buy ${e.qty}%`,  _ts: dateToTs(e.date) })),
      ...sells.map(e => ({ date: e.date, note: `Sell ${e.qty}%`, _ts: dateToTs(e.date) })),
    ].sort((a, b) => a._ts - b._ts);
    const buyDates = combined.filter(e => e.note.startsWith('Buy'));
    const added    = buyDates.length ? buyDates.reduce((a, b) => a._ts <= b._ts ? a : b).date : null;
    history[nse] = { added, rebalances: combined.map(({ date, note }) => ({ date, note })) };
  }
  return history;
};

const mergeRowLive = (row, live) => {
  if (!live) return row;
  const cmp     = live.cmp    ?? live.close1M ?? null;
  const open1M  = live.open1M ?? null;
  const close1M = live.close1M ?? cmp;
  const performance    = calcPerformance(open1M, close1M);
  const contribution   = calcContribution(row.allocation, performance);
  const absoluteReturns = calcAbsoluteReturns(cmp, row.buyPrice);
  return {
    ...row,
    cmp,
    open1M,
    close1M,
    high1M:        live.high1M    ?? null,
    low1M:         live.low1M     ?? null,
    marketCap:     live.marketCapCr ?? null,
    peRatio:       live.peRatio   ?? null,
    performance,
    contribution,
    absoluteReturns,
  };
};

const MAX_HISTORY = 15;

// ─────────────────────────────────────────────────────────────────────────────
// Read edit permission from URL param set by the main app
const _qp = new URLSearchParams(window.location.search);
const READ_ONLY = _qp.get('edit') === '0';

export default function App() {
  const _path = window.location.pathname.replace(/^\/wp/, '') || '/';
  if (_path === '/buy-price')       return <BuyPricePage />;
  if (_path === '/calculate-return') return <CalculateReturnPage />;
  if (_path === '/pl-statement')    return <PLStatementPage />;
  if (_path === '/corporate-actions') return <CorporateActionsPage />;

  const [basketKey,    setBasketKey]    = useState('Mid_Small_Cap');
  const [rows,         setRows]         = useState([]);
  const [liveData,     setLiveData]     = useState({});
  const [liveLoading,  setLiveLoading]  = useState(false); // used to prevent duplicate fetches
  const [historyStack, setHistoryStack] = useState([]);
  const [searchTerm,   setSearchTerm]   = useState('');
  const [confirm,      setConfirm]      = useState(null);
  const [whatIfNse,    setWhatIfNse]    = useState(null);
  const [simOverlay,   setSimOverlay]   = useState({}); // { [basketKey]: { editedBuys, deletedNse, added } } -- temporary, in-memory only, never persisted
  const [basketMeta,   setBasketMeta]   = useState({ history: {}, buyPriceDetails: {} });
  const [loadProgress, setLoadProgress] = useState(null);
  const [hasChanges,   setHasChanges]   = useState(false);
  const _saveTimer = useRef(null);
  const [nseSymbols,   setNseSymbols]   = useState([]);
  const [soldRows,          setSoldRows]          = useState([]);
  const [activeTab,         setActiveTab]         = useState('holdings');
  const [ohlcFallbacks,     setOhlcFallbacks]     = useState({});
  const [fallbackDismissed, setFallbackDismissed] = useState(false);
  const [indexHistory,      setIndexHistory]      = useState(null);
  const [basketStockMap,    setBasketStockMap]    = useState(null);
  const [perfByTenure,      setPerfByTenure]      = useState({}); // {nseCode: {"1M":pct, "3M":pct, ...}}
  const [selectedTenure,    setSelectedTenure]    = useState('1M');

  const loadGenRef = useRef(0);

  // Load NSE symbol list once on mount for autocomplete
  useEffect(() => {
    fetch(`${API_BASE}/nse-symbols`)
      .then(r => r.json())
      .then(setNseSymbols)
      .catch(() => {});
  }, []);

  // Load {basketKey: [nseCode,...]} once for cross-basket search + overlap %
  useEffect(() => {
    fetchBasketStockMap().then(setBasketStockMap).catch(() => {});
  }, []);

  // Load index history once for since-inception calculation
  useEffect(() => {
    fetch(`${API_BASE}/index-history`)
      .then(r => r.json())
      .then(setIndexHistory)
      .catch(() => {});
  }, []);

  // ── Confirm modal helper ────────────────────────────────────────────────────
  const showConfirm = useCallback((title, msg) =>
    new Promise(resolve => setConfirm({ title, msg, resolve }))
  , []);

  // ── Undo ────────────────────────────────────────────────────────────────────
  const saveToHistory = useCallback((currentRows) => {
    setHistoryStack(prev => {
      const next = [...prev, JSON.parse(JSON.stringify(currentRows))];
      return next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
    });
  }, []);

  const handleUndo = useCallback(() => {
    setHistoryStack(prev => {
      if (prev.length === 0) return prev;
      const snapshot = prev[prev.length - 1];
      setRows(snapshot);
      setHasChanges(true);
      return prev.slice(0, -1);
    });
  }, []);

  // ── Auto-save (debounced 1.5 s after any change) ────────────────────────────
  const handleSave = useCallback(async () => {
    try {
      const stocks = rows.map(r => ({
        nseCode:    r.nseCode,
        allocation: r.allocation,
        buyPrice:   r.buyPrice,
      }));
      const sold = soldRows.map(r => ({
        nseCode:      r.nseCode,
        securityName: r.securityName,
        date:         r.date,
        action:       r.action,
        weightSold:   r.weightSold,
        buyPrice:     r.buyPrice,
        sellPrice:    r.sellPrice,
      }));
      if (basketKey === 'IPO_Recommendations') {
        const existing = basketMeta?.buyPriceDetails || {};
        const merged = { ...existing };
        rows.forEach(r => {
          if (!r.nseCode) return;
          merged[r.nseCode] = { ...(existing[r.nseCode] || {}), listingDate: r.listingDate || '' };
        });
        await saveBasket(basketKey, stocks, sold, merged);
      } else {
        await saveBasket(basketKey, stocks, sold);
      }
      setHasChanges(false);
    } catch (_) {}
  }, [basketKey, rows, soldRows, basketMeta]);

  useEffect(() => {
    if (!hasChanges) return;
    if (_saveTimer.current) clearTimeout(_saveTimer.current);
    _saveTimer.current = setTimeout(() => { _saveTimer.current = null; handleSave(); }, 1500);
    return () => { if (_saveTimer.current) clearTimeout(_saveTimer.current); };
  }, [hasChanges]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Basket change — save any pending changes immediately then switch ─────────
  const handleBasketChange = useCallback(async (newKey) => {
    if (newKey === basketKey) return;
    if (_saveTimer.current) { clearTimeout(_saveTimer.current); _saveTimer.current = null; }
    if (hasChanges) await handleSave();
    setHasChanges(false);
    setBasketKey(newKey);
  }, [basketKey, hasChanges, handleSave]);

  // ── Fetch OHLC fallback info on basket change ────────────────────────────────
  useEffect(() => {
    setOhlcFallbacks({});
    setFallbackDismissed(false);
    fetch(`${API_BASE}/ohlc-fallbacks/${basketKey}`)
      .then(r => r.json())
      .then(setOhlcFallbacks)
      .catch(() => {});
  }, [basketKey]);

  // ── Load basket on key change ────────────────────────────────────────────────
  useEffect(() => {
    const gen = ++loadGenRef.current;
    setRows([]);
    setSoldRows([]);
    setHistoryStack([]);
    setSearchTerm('');
    setLoadProgress(null);
    setHasChanges(false);
    setActiveTab('holdings');
    setPerfByTenure({});

    let liveSnapshot = null;

    const run = async () => {
      const data = await fetchBasket(basketKey);
      if (gen !== loadGenRef.current) return;

      const composition = data.stocks || [];
      setSoldRows(data.soldStocks || []);
      const bpDetails = data.buyPriceDetails || {};
      setBasketMeta({ history: buildHistoryFromDetails(bpDetails), buyPriceDetails: bpDetails });

      if (composition.length === 0) { setRows([]); return; }

      const buyPriceDetails = data.buyPriceDetails || {};
      const initialRows = composition.map(item => ({
        nseCode:         item.nseCode,
        formula:         `NSE: ${item.nseCode}`,
        allocation:      item.allocation,
        buyPrice:        item.buyPrice ?? null,
        targetPrice:     buyPriceDetails[item.nseCode]?.targetPrice  ?? null,
        stopLoss:        buyPriceDetails[item.nseCode]?.stopLoss    ?? null,
        listingDate:     buyPriceDetails[item.nseCode]?.listingDate || '',
        holdingDays:     basketKey === 'IPO_Recommendations'
          ? calcDaysSinceListing(buyPriceDetails[item.nseCode]?.listingDate)
          : calcHoldingDays(buyPriceDetails[item.nseCode]?.buyEvents),
        cmp:             null,
        open1M:          null,
        close1M:         null,
        high1M:          null,
        low1M:           null,
        marketCap:       null,
        peRatio:         null,
        performance:     null,
        contribution:    null,
        absoluteReturns: null,
      }));
      setRows(initialRows);
      setLoadProgress({ loaded: 0, total: composition.length });

      // Multi-tenure performance (1M/3M/6M/1Y/2Y/3Y/5Y) — fetched in the
      // background, independent of the live-price load below; the table
      // just shows "—" for any stock/tenure not in yet.
      fetchPerformanceBatch(initialRows.map(r => r.nseCode))
        .then(data => { if (gen === loadGenRef.current) setPerfByTenure(data); })
        .catch(() => {});

      if (!liveSnapshot || Object.keys(liveSnapshot).length === 0) {
        setLiveLoading(true);
        try {
          const live = await fetchLiveData();
          if (gen !== loadGenRef.current) return;
          liveSnapshot = live;
          setLiveData(live);
        } catch (_) {
          liveSnapshot = {};
        } finally {
          if (gen === loadGenRef.current) setLiveLoading(false);
        }
      } else {
        liveSnapshot = liveData;
      }

      if (gen !== loadGenRef.current) return;

      let loadedCount = 0;
      const mergedRows = [...initialRows];

      const tasks = initialRows.map((row, idx) => async () => {
        if (gen !== loadGenRef.current) return;
        const live = liveSnapshot[row.nseCode.toUpperCase()];
        let merged;
        if (live && live.cmp != null) {
          merged = mergeRowLive(row, live);
        } else {
          try {
            const singleLive = await fetchLiveStock(row.nseCode);
            if (gen !== loadGenRef.current) return;
            merged = mergeRowLive(row, singleLive?.cmp != null ? singleLive : null);
          } catch (_) {
            merged = { ...row };
          }
        }
        mergedRows[idx] = merged;
        loadedCount++;
        if (gen === loadGenRef.current) {
          setRows([...mergedRows]);
          setLoadProgress(loadedCount >= initialRows.length ? null : { loaded: loadedCount, total: initialRows.length });
        }
      });

      const runWithConcurrency = async (tasks, limit) => {
        let i = 0;
        const next = async () => { if (i >= tasks.length) return; const idx = i++; await tasks[idx](); return next(); };
        await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, next));
      };

      await runWithConcurrency(tasks, 8);
      if (gen === loadGenRef.current) setLoadProgress(null);
    };

    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basketKey]);

  // ── Auto-refresh sold stocks when user returns to this tab ──────────────────
  const soldLastFetch = useRef(0);
  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - soldLastFetch.current < 30000) return;
      soldLastFetch.current = Date.now();
      fetch(`${API_BASE}/basket/${basketKey}`)
        .then(r => r.json())
        .then(d => { if (d.soldStocks) setSoldRows(d.soldStocks); })
        .catch(() => {});
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [basketKey]);

  // ── Refresh basket data after a rebalance is confirmed in BuyPricePage ───────
  useEffect(() => {
    let bc;
    try {
      bc = new BroadcastChannel('nia_rebalance');
      bc.onmessage = () => {
        // Refresh at 22s: after OHLC fetch + backfill complete; update buy prices + sold stocks
        setTimeout(() => {
          fetch(`${API_BASE}/basket/${basketKey}`)
            .then(r => r.json())
            .then(d => {
              if (d.soldStocks) setSoldRows(d.soldStocks);
              if (d.stocks) {
                const bpDetails = d.buyPriceDetails || {};
                setRows(prev => prev.map(row => {
                  const updated = (d.stocks || []).find(s => s.nseCode === row.nseCode);
                  return updated ? { ...row, buyPrice: updated.buyPrice ?? row.buyPrice,
                    allocation: updated.allocation ?? row.allocation,
                    holdingDays: basketKey === 'IPO_Recommendations'
                      ? calcDaysSinceListing(bpDetails[row.nseCode]?.listingDate)
                      : calcHoldingDays(bpDetails[row.nseCode]?.buyEvents) } : row;
                }));
              }
            })
            .catch(() => {});
        }, 22000);
      };
    } catch {}
    return () => { try { bc?.close(); } catch {} };
  }, [basketKey]);

  // ── Equal-weight contribution for IPO basket (no allocations) ──────────────
  const isIPO = basketKey === 'IPO_Recommendations';

  const displayRows = useMemo(() => {
    if (!isIPO) return rows;
    const activeCount = rows.filter(r => r.nseCode).length;
    if (!activeCount) return rows;
    const weight = 1 / activeCount;
    return rows.map(r => ({
      ...r,
      contribution: r.nseCode && r.performance != null ? r.performance * weight : null,
    }));
  }, [rows, isIPO]);

  // ── What-if simulation overlay ───────────────────────────────────────────────
  // Applied on top of displayRows for every aggregate/summary consumer below,
  // so an active simulation is genuinely reflected across the whole portfolio
  // (KPIs, pie chart, top gainers/losers) -- not just inside its own modal.
  // `simRows` (index-safe, same order/length as displayRows + appended adds)
  // feeds the editable table; `simAgg.overlaid` (deleted stocks removed,
  // correct for aggregation) feeds every read-only summary view.
  const simSlot = simOverlay[basketKey] || EMPTY_SLOT;
  const simRows = useMemo(() => mergeSimForDisplay(displayRows, simSlot), [displayRows, simSlot]);
  const simAgg  = useMemo(() => computeWhatIf(displayRows, simSlot), [displayRows, simSlot]);
  const simBefore = useMemo(() => computeWhatIf(displayRows, EMPTY_SLOT), [displayRows]);
  const simDetails = useMemo(() => buildTouchedDetails(displayRows, simSlot), [displayRows, simSlot]);
  const hasSimulation = simSlot.added.length > 0 || simSlot.deletedNse.length > 0 ||
    Object.keys(simSlot.editedBuys).length > 0 || Object.keys(simSlot.weightReductions || {}).length > 0;

  // ── Cross-basket search: which OTHER baskets hold a stock matching the search ──
  const crossBasketMatches = useMemo(() => {
    const term = searchTerm.trim().toUpperCase();
    if (!term || !basketStockMap) return [];
    const matchedCodes = new Set();
    for (const codes of Object.values(basketStockMap)) {
      for (const code of codes) {
        if (code.includes(term)) matchedCodes.add(code);
      }
    }
    const results = [];
    for (const code of matchedCodes) {
      const inBaskets = Object.entries(basketStockMap)
        .filter(([key, codes]) => key !== basketKey && codes.includes(code))
        .map(([key]) => key);
      if (inBaskets.length > 0) results.push({ code, baskets: inBaskets });
    }
    return results.slice(0, 10);
  }, [searchTerm, basketStockMap, basketKey]);

  // ── Basket overlap %: for the CURRENT basket, what % of its stocks also
  // appear in each other basket ──────────────────────────────────────────────
  const basketOverlap = useMemo(() => {
    if (!basketStockMap || !basketStockMap[basketKey]) return [];
    const current = basketStockMap[basketKey];
    if (!current.length) return [];
    const currentSet = new Set(current);
    return Object.entries(basketStockMap)
      .filter(([key]) => key !== basketKey)
      .map(([key, codes]) => {
        const commonCodes = codes.filter(c => currentSet.has(c)).sort();
        return { key, pct: Math.round((commonCodes.length / current.length) * 1000) / 10, common: commonCodes.length, total: current.length, commonCodes };
      })
      .filter(o => o.common > 0)
      .sort((a, b) => b.pct - a.pct);
  }, [basketStockMap, basketKey]);
  const resetSimulation = useCallback(() => {
    setSimOverlay(prev => ({ ...prev, [basketKey]: { editedBuys: {}, deletedNse: [], added: [], weightReductions: {} } }));
  }, [basketKey]);

  // ── Derived KPI metrics ──────────────────────────────────────────────────────
  const totalContribution = simAgg.totalContribution;

  const avgMarketCap = (() => {
    const mc = simAgg.overlaid.filter(r => r.marketCap > 0).map(r => r.marketCap);
    return mc.length ? mc.reduce((a, b) => a + b, 0) / mc.length : 0;
  })();

  const medianPE = (() => {
    const pes = simAgg.overlaid.map(r => r.peRatio).filter(p => p != null && !isNaN(p) && isFinite(p)).sort((a, b) => a - b);
    if (!pes.length) return 0;
    const mid = Math.floor(pes.length / 2);
    return pes.length % 2 !== 0 ? pes[mid] : (pes[mid - 1] + pes[mid]) / 2;
  })();

  const activeStocks = simAgg.overlaid.filter(r => r.cmp != null).length;

  const totalAllocation = simAgg.totalAllocation;

  const totalAbsReturn = (() => {
    const hist = indexHistory?.[basketKey]?.data;
    if (!hist || hist.length < 2) return null;
    const inception = hist[0].value;
    const latest    = hist[hist.length - 1].value;
    if (!inception) return null;
    return (latest - inception) / inception;
  })();

  // ── Row editing handlers ─────────────────────────────────────────────────────
  const handleNseChange = useCallback(async (idx, newCode) => {
    if (!newCode) return;
    const code = newCode.trim().toUpperCase();
    setHasChanges(true);
    setRows(prev => {
      const next = [...prev];
      next[idx] = { ...next[idx], nseCode: code, formula: `NSE: ${code}`,
                    cmp: null, open1M: null, close1M: null, high1M: null,
                    low1M: null, marketCap: null, peRatio: null,
                    performance: null, contribution: null, absoluteReturns: null };
      return next;
    });
    try {
      const live = await fetchLiveStock(code);
      setRows(prev => {
        if (prev[idx]?.nseCode !== code) return prev;
        const next = [...prev];
        next[idx] = mergeRowLive(next[idx], live?.cmp != null ? live : null);
        return next;
      });
    } catch (_) {}
  }, []);

  const handleAllocChange = useCallback((idx, pct) => {
    const allocDecimal = parseFloat(pct) / 100;
    if (isNaN(allocDecimal)) return;
    setHasChanges(true);
    setRows(prev => {
      const next = [...prev];
      const row  = { ...next[idx], allocation: allocDecimal };
      row.contribution = calcContribution(allocDecimal, row.performance);
      next[idx] = row;
      return next;
    });
  }, []);

  const handleListingDateChange = useCallback(async (idx, val) => {
    setRows(prev => {
      const next = [...prev];
      next[idx] = { ...next[idx], listingDate: val, holdingDays: calcDaysSinceListing(val) };
      return next;
    });
    setHasChanges(true);

    const code = rows[idx]?.nseCode;
    if (!val || !code) return;
    try {
      const resp = await fetch(`${API_BASE}/listing-price/${encodeURIComponent(code)}?date=${encodeURIComponent(val)}`);
      const data = await resp.json();
      if (data.price != null) {
        setRows(prev => {
          if (prev[idx]?.nseCode !== code) return prev;
          const next = [...prev];
          const row = { ...next[idx], buyPrice: data.price };
          row.absoluteReturns = calcAbsoluteReturns(row.cmp, data.price);
          next[idx] = row;
          return next;
        });
        setHasChanges(true);
      }
    } catch (_) {}
  }, [rows]);

  const handleBuyPriceChange = useCallback((idx, price) => {
    const bp = parseFloat(price);
    if (isNaN(bp) || bp <= 0) return;
    setHasChanges(true);
    setRows(prev => {
      const next = [...prev];
      const row  = { ...next[idx], buyPrice: bp };
      row.absoluteReturns = calcAbsoluteReturns(row.cmp, bp);
      next[idx] = row;
      return next;
    });
  }, []);

  const handleAddRow = useCallback(async (afterIdx) => {
    const confirmed = await showConfirm('Add Stock', 'Add a new stock to the basket?\nEnter the NSE ticker symbol in the new row.');
    if (!confirmed) return;
    saveToHistory(rows);
    setHasChanges(true);
    setRows(prev => {
      const next = [...prev];
      next.splice(afterIdx + 1, 0, {
        nseCode: '', formula: '', allocation: null, buyPrice: null, holdingDays: null,
        cmp: null, open1M: null, close1M: null, high1M: null, low1M: null,
        marketCap: null, peRatio: null, performance: null, contribution: null, absoluteReturns: null,
      });
      return next;
    });
  }, [rows, saveToHistory, showConfirm]);

  const handleRemoveRow = useCallback(async (idx) => {
    const stock = rows[idx]?.nseCode || 'this stock';
    const alloc = rows[idx]?.allocation != null ? ` (${(rows[idx].allocation * 100).toFixed(2)}% allocation)` : '';
    const confirmed = await showConfirm('Remove Stock', `Remove <strong>${stock}</strong>${alloc} from the basket?\nThis action can be undone using the Undo button.`);
    if (!confirmed) return;
    saveToHistory(rows);
    setHasChanges(true);
    setRows(prev => prev.filter((_, i) => i !== idx));
  }, [rows, saveToHistory, showConfirm]);

  // ── What-If simulator ────────────────────────────────────────────────────────
  const openWhatIf  = useCallback((nse) => setWhatIfNse(nse), []);
  const closeWhatIf = useCallback(() => setWhatIfNse(null), []);
  const handleRemoveSimAdded = useCallback((simId) => {
    setSimOverlay(prev => {
      const prevSlot = prev[basketKey] || EMPTY_SLOT;
      return { ...prev, [basketKey]: { ...prevSlot, added: prevSlot.added.filter(a => a.id !== simId) } };
    });
  }, [basketKey]);

  // ── Confirm modal ─────────────────────────────────────────────────────────────
  const handleConfirmYes = () => { if (confirm?.resolve) confirm.resolve(true);  setConfirm(null); };
  const handleConfirmNo  = () => { if (confirm?.resolve) confirm.resolve(false); setConfirm(null); };

  const [dashView, setDashView] = useState('overview');

  return (
    <>
      <div className="dashboard-container">
        <Header
          basketKey={basketKey}
          onBasketChange={handleBasketChange}
          searchTerm={searchTerm}
          onSearchChange={setSearchTerm}
          onSearchClear={() => setSearchTerm('')}
          canUndo={historyStack.length > 0}
          onUndo={handleUndo}
          onBuyPrice={() => { window.location.href = '/wp/buy-price' + window.location.search; }}
          onCalculateReturn={() => { window.location.href = '/wp/calculate-return' + window.location.search; }}
          onPLStatement={() => { window.location.href = '/wp/pl-statement' + window.location.search; }}
          onCorporateActions={() => { window.location.href = '/wp/corporate-actions' + window.location.search; }}
          readOnly={READ_ONLY}
        />

        {crossBasketMatches.length > 0 && (
          <div className="cross-basket-panel">
            {crossBasketMatches.map(m => (
              <div key={m.code} className="cross-basket-row">
                <span className="cross-basket-code">{m.code}</span>
                <span className="cross-basket-label">also in:</span>
                {m.baskets.map(key => (
                  <button
                    key={key}
                    className="btn btn-secondary cross-basket-go"
                    onClick={() => handleBasketChange(key)}
                  >
                    {BASKET_OPTIONS.find(b => b.key === key)?.label || key} →
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}

        <KPIPanel
          totalContribution={totalContribution}
          totalAbsReturn={totalAbsReturn}
          avgMarketCap={avgMarketCap}
          medianPE={medianPE}
          activeStocks={activeStocks}
          totalAllocation={totalAllocation}
          rows={simAgg.overlaid}
        />

        {hasSimulation && (
          <WhatIfImpactBanner before={simBefore} after={simAgg} details={simDetails} onReset={resetSimulation} />
        )}

        {/* Tab switcher */}
        <div className="dv-tabs">
          <button className={`dv-tab${dashView === 'overview' ? ' active' : ''}`} onClick={() => setDashView('overview')}>
            <i className="fa-solid fa-chart-pie" /> Overview
          </button>
          <button className={`dv-tab${dashView === 'holdings' ? ' active' : ''}`} onClick={() => setDashView('holdings')}>
            <i className="fa-solid fa-table" /> Holdings
            {loadProgress && <span className="dv-tab-badge">{loadProgress.loaded}/{loadProgress.total}</span>}
          </button>
        </div>

        {dashView === 'overview' ? (
          <DashboardView
            rows={simAgg.overlaid}
            avgMarketCap={avgMarketCap}
            medianPE={medianPE}
            isIPO={isIPO}
            onViewHoldings={() => setDashView('holdings')}
            basketOverlap={basketOverlap}
            onGoToBasket={handleBasketChange}
          />
        ) : (
          <div className="holdings-view">
            {/* Insight cards — full-width row above table */}
            <InsightsSidebar rows={simAgg.overlaid} isIPO={isIPO} />

            {/* OHLC Fallback Banner */}
            {!fallbackDismissed && Object.keys(ohlcFallbacks).length > 0 && (
              <div style={{ margin:'0.5rem 0 0.25rem', padding:'0.65rem 1rem', borderRadius:'8px', background:'rgba(251,191,36,0.08)', border:'1px solid rgba(251,191,36,0.3)', display:'flex', alignItems:'flex-start', gap:'0.6rem', fontSize:'0.80rem' }}>
                <span style={{ color:'#fbbf24', flexShrink:0 }}>⚠</span>
                <div style={{ flex:1, color:'#94a3b8', lineHeight:1.5 }}>
                  <strong style={{ color:'#fbbf24' }}>Next-Trading-Day Prices Used — </strong>
                  {Object.entries(ohlcFallbacks).map(([nse, info]) => {
                    const parts = [...Object.entries(info.buyFallbacks||{}).map(([r,a])=>`${nse} Buy ${r}→${a}`),...Object.entries(info.sellFallbacks||{}).map(([r,a])=>`${nse} Sell ${r}→${a}`)];
                    return parts.join(', ');
                  }).filter(Boolean).join(' | ')}
                </div>
                <button onClick={() => setFallbackDismissed(true)} style={{ background:'none', border:'none', color:'#64748b', cursor:'pointer', fontSize:'1rem', padding:0, flexShrink:0 }}>&times;</button>
              </div>
            )}

            <div className="table-section">
              <PortfolioTable
                rows={simRows}
                searchTerm={searchTerm}
                nseSymbols={nseSymbols}
                isIPO={isIPO}
                readOnly={READ_ONLY}
                onNseChange={handleNseChange}
                onAllocChange={handleAllocChange}
                onBuyPriceChange={handleBuyPriceChange}
                onListingDateChange={handleListingDateChange}
                onAddRow={handleAddRow}
                onRemoveRow={handleRemoveRow}
                onInfoClick={openWhatIf}
                onRemoveSimAdded={handleRemoveSimAdded}
                totalContribution={totalContribution}
                avgMarketCap={avgMarketCap}
                medianPE={medianPE}
                tenure={selectedTenure}
                onTenureChange={setSelectedTenure}
                perfByTenure={perfByTenure}
              />
              {!isIPO && (
                <WhatIfAddStockBar
                  basketKey={basketKey}
                  rows={displayRows}
                  nseSymbols={nseSymbols}
                  simOverlay={simOverlay}
                  setSimOverlay={setSimOverlay}
                />
              )}
            </div>
          </div>
        )}
      </div>

      {confirm && <ConfirmModal title={confirm.title} message={confirm.msg} onConfirm={handleConfirmYes} onCancel={handleConfirmNo} />}
      {whatIfNse && (
        <WhatIfModal
          nse={whatIfNse}
          basketKey={basketKey}
          basketMeta={basketMeta}
          rows={displayRows}
          simOverlay={simOverlay}
          setSimOverlay={setSimOverlay}
          onClose={closeWhatIf}
        />
      )}
    </>
  );
}
