// Multi-Compare -- true N-way comparison (any mix of our baskets +
// tracked competitors, up to MULTI_MAX_ENTITIES at once). A separate full
// page (opened in a new tab from CompetitorAnalysisPage's sidebar) rather
// than a section squeezed under the 1-vs-1 view, since it carries every
// section that page has (Executive, Overlap, Sector, Market Cap,
// Performance, Stock Comparison, Rebalance History, Stock Timing Insights)
// generalized to N entities instead of 2.
import { API_BASE, getAuthToken } from '../api/base.js';
import { useState, useEffect, useMemo } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  LineChart, Line,
} from 'recharts';
import { fetchBasket } from '../api/client.js';
import { BASKET_OPTIONS } from './Header.jsx';
import {
  usePalette, _getAdminState,
  BRAND_INK, FONT_HEADING, FONT_BODY,
  OURS_COLOR, THEIRS_COLOR, TIMING_WINDOW_DAYS,
  MULTI_PALETTE, MULTI_CAP_BUCKETS, MULTI_MAX_ENTITIES,
  round1, normalizeStockName, toIsoDate, buildIndexedSeriesMulti, buildTimingInsights, maxDrawdownPct,
  Card, SectionTitle, EmptyNote, PortfolioCard, RebalanceHistoryPanel, UniqueHoldingsList, TimingInsightRow,
} from './CompetitorAnalysisPage.jsx';

export default function CompetitorMultiComparePage() {
  const pal = usePalette();
  const { isAdmin: userIsAdmin } = _getAdminState();
  const [competitors, setCompetitors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [entities, setEntities] = useState([]);
  const [dataCache, setDataCache] = useState({});
  const [addBasket, setAddBasket] = useState(BASKET_OPTIONS[0]?.key || '');
  const [addCompetitor, setAddCompetitor] = useState('');

  useEffect(() => {
    const token = getAuthToken();
    fetch(`${API_BASE}/admin/competitor-list`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(r => r.json())
      .then(d => setCompetitors(d.competitors || []))
      .finally(() => setLoading(false));
  }, []);

  const loadOursEntity = async (key) => {
    const id = `ours:${key}`;
    const token = getAuthToken();
    const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};

    // Stocks, index series, and rebalance history are all instant local
    // reads -- resolve and render them as soon as they're ready. Only
    // sector/cap mix depends on basket-profile's live market-cap cascade
    // (Screener -> Google -> NSE, 15-60s on a cold cache), confirmed via
    // direct timing tests -- fetching it separately (not Promise.all'd
    // with the fast calls) means the rest of the page no longer sits on
    // "Loading..." for data that was already available instantly.
    try {
      const [basketData, seriesResp, historyResp] = await Promise.all([
        fetchBasket(key),
        fetch(`${API_BASE}/index-history`, { headers: authHeaders }).then(r => r.json()),
        fetch(`${API_BASE}/admin/basket-rebalance-history/${key}`, { headers: authHeaders }).then(r => r.json()),
      ]);
      setDataCache(prev => ({ ...prev, [id]: {
        ...prev[id],
        loading: false,
        stocks: (basketData.stocks || []).map(s => ({ name: s.name || s.securityName, nseCode: s.nseCode, weight: (s.allocation || 0) * 100 })),
        series: seriesResp[key]?.data || [],
        rebalanceHistory: historyResp.history || [],
        sectorMix: prev[id]?.sectorMix || {},
        capMix: prev[id]?.capMix || {},
        profileLoading: true,
      } }));
    } catch {
      setDataCache(prev => ({ ...prev, [id]: { loading: false, error: true } }));
      return;
    }

    try {
      const profile = await fetch(`${API_BASE}/admin/basket-profile/${key}`, { headers: authHeaders }).then(r => r.json());
      setDataCache(prev => ({ ...prev, [id]: {
        ...prev[id],
        sectorMix: profile.sectorMix || {},
        capMix: profile.capMix || {},
        profileLoading: false,
      } }));
    } catch {
      setDataCache(prev => ({ ...prev, [id]: { ...prev[id], profileLoading: false } }));
    }
  };

  const addEntity = (type, key) => {
    if (!key || entities.length >= MULTI_MAX_ENTITIES) return;
    const id = `${type}:${key}`;
    if (entities.some(e => e.id === id)) return;
    if (type === 'ours') {
      const label = BASKET_OPTIONS.find(o => o.key === key)?.label || key;
      setEntities(prev => [...prev, { id, type, key, label }]);
      setDataCache(prev => ({ ...prev, [id]: { loading: true } }));
      loadOursEntity(key);
    } else {
      const comp = competitors.find(c => c.key === key);
      if (!comp) return;
      setEntities(prev => [...prev, { id, type, key, label: comp.label }]);
      const sectorMix = {};
      (comp.stocks || []).forEach(s => { if (s.sector) sectorMix[s.sector] = round1((sectorMix[s.sector] || 0) + (s.weight || 0)); });
      setDataCache(prev => ({ ...prev, [id]: {
        loading: false,
        stocks: (comp.stocks || []).map(s => ({ name: s.name, nseCode: s.nseCode, weight: s.weight })),
        series: comp.performanceSeries || [],
        sectorMix,
        capMix: comp.marketCapMix || {},
        rebalanceHistory: comp.fullRebalanceHistory || [],
      } }));
    }
  };
  const removeEntity = (id) => setEntities(prev => prev.filter(e => e.id !== id));
  const colorFor = (i) => MULTI_PALETTE[i % MULTI_PALETTE.length];

  const sectorChartData = useMemo(() => {
    const allSectors = new Set();
    entities.forEach(e => Object.keys(dataCache[e.id]?.sectorMix || {}).forEach(s => allSectors.add(s)));
    const rows = [...allSectors].map(sector => {
      const row = { sector };
      entities.forEach(e => { row[e.label] = round1(dataCache[e.id]?.sectorMix?.[sector] || 0); });
      return row;
    });
    rows.sort((a, b) => Math.max(...entities.map(e => b[e.label] || 0)) - Math.max(...entities.map(e => a[e.label] || 0)));
    return rows.slice(0, 10);
  }, [entities, dataCache]);

  const capMixChartData = useMemo(() => MULTI_CAP_BUCKETS
    .map(bucket => {
      const row = { bucket };
      entities.forEach(e => { row[e.label] = round1(dataCache[e.id]?.capMix?.[bucket] || 0); });
      return row;
    })
    .filter(row => entities.some(e => row[e.label] > 0)), [entities, dataCache]);

  const perfChartData = useMemo(() => {
    const seriesList = entities.filter(e => dataCache[e.id]?.series?.length).map(e => ({ key: e.label, data: dataCache[e.id].series }));
    if (seriesList.length < 1) return [];
    const allDates = seriesList.flatMap(s => s.data.map(d => d.date)).sort();
    return buildIndexedSeriesMulti(seriesList, allDates[0] || '2000-01-01');
  }, [entities, dataCache]);

  const drawdownByEntity = useMemo(() => entities.map(e => ({
    entity: e, drawdown: maxDrawdownPct(dataCache[e.id]?.series),
  })), [entities, dataCache]);

  const stockTableRows = useMemo(() => {
    const byKey = {};
    entities.forEach(e => {
      (dataCache[e.id]?.stocks || []).forEach(s => {
        const k = s.nseCode || s.name;
        (byKey[k] ||= { name: s.name, weights: {} }).weights[e.label] = s.weight;
      });
    });
    return Object.values(byKey)
      .sort((a, b) => Math.max(...Object.values(b.weights), 0) - Math.max(...Object.values(a.weights), 0));
  }, [entities, dataCache]);

  // Overlap: common to every selected entity, plus what's unique to each.
  const overlapData = useMemo(() => {
    if (entities.length < 2) return null;
    const keySets = entities.map(e => new Set((dataCache[e.id]?.stocks || []).map(s => s.nseCode || s.name)));
    const anyMissing = entities.some(e => !dataCache[e.id] || dataCache[e.id].loading);
    if (anyMissing) return null;
    const allKeys = new Set(keySets.flatMap(s => [...s]));
    const common = [...allKeys].filter(k => keySets.every(s => s.has(k)));
    const uniquePerEntity = entities.map((e, i) => ({
      entity: e,
      items: (dataCache[e.id]?.stocks || []).filter(s => {
        const k = s.nseCode || s.name;
        return keySets.filter(set => set.has(k)).length === 1;
      }),
    }));
    return { commonCount: common.length, uniquePerEntity };
  }, [entities, dataCache]);

  // Stock Timing Insights: first entity added is the "base" -- every other
  // entity is compared against it, same pairwise logic as the 2-way page,
  // just run once per other entity instead of once overall.
  const timingByEntity = useMemo(() => {
    if (entities.length < 2) return [];
    const base = entities[0];
    const baseHistory = dataCache[base.id]?.rebalanceHistory;
    if (!baseHistory?.length) return [];
    return entities.slice(1).map(e => {
      const otherHistory = dataCache[e.id]?.rebalanceHistory;
      if (!otherHistory?.length) return { entity: e, buys: [], sells: [] };
      const otherStocks = dataCache[e.id]?.stocks;
      return {
        entity: e,
        buys: buildTimingInsights(baseHistory, otherHistory, otherStocks, 'new'),
        sells: buildTimingInsights(baseHistory, otherHistory, otherStocks, 'removed'),
      };
    });
  }, [entities, dataCache]);

  const [ohlcData, setOhlcData] = useState({});
  useEffect(() => {
    const pairs = [];
    const seen = new Set();
    timingByEntity.forEach(({ buys, sells }) => {
      [...buys, ...sells].forEach(t => {
        if (!t.code) return;
        [t.ourDate, t.theirDate].forEach(date => {
          const iso = toIsoDate(date);
          const k = `${t.code}|${iso}`;
          if (iso && !seen.has(k)) { seen.add(k); pairs.push({ nseCode: t.code, date: iso }); }
        });
      });
    });
    if (!pairs.length) { setOhlcData({}); return; }
    const token = getAuthToken();
    fetch(`${API_BASE}/admin/stock-ohlc-on-date`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ pairs }),
    }).then(r => r.json()).then(d => setOhlcData(d.ohlc || {})).catch(() => setOhlcData({}));
  }, [timingByEntity]);

  const navItems = [
    { id: 'picker', label: 'Entities', icon: 'fa-layer-group' },
    { id: 'executive', label: 'Overview', icon: 'fa-gauge-high' },
    { id: 'overlap', label: 'Overlap', icon: 'fa-circle-nodes' },
    { id: 'sector', label: 'Sector Allocation', icon: 'fa-chart-bar' },
    { id: 'marketcap', label: 'Market Cap', icon: 'fa-building-columns' },
    { id: 'performance', label: 'Performance', icon: 'fa-chart-line' },
    { id: 'stocks', label: 'Stock Comparison', icon: 'fa-table-list' },
    { id: 'rebalance', label: 'Rebalances', icon: 'fa-arrows-rotate' },
    { id: 'timing', label: 'Timing Insights', icon: 'fa-stopwatch' },
  ];
  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const selectStyle = { padding: '0.4rem 0.6rem', borderRadius: '6px', border: `1px solid ${pal.borderSoft}`, fontSize: '0.78rem', background: pal.inputBg, color: pal.inputText };
  const btnStyle = { padding: '0.4rem 0.8rem', borderRadius: '6px', border: 'none', background: '#456232', color: '#fff', fontSize: '0.78rem', fontWeight: 600, cursor: 'pointer' };
  // Opened in its own tab (see CompetitorAnalysisPage's navClick), so there's
  // no "Back" to the outer app's own nav the way the iframe-embedded pages
  // have -- "Home" goes to the main dashboard's root instead. Local dev runs
  // the two apps on separate ports (:8000 main, :8001 this one); production
  // mounts this one at /wp under the main app's own origin, so home is just
  // that origin's root.
  const homeUrl = window.location.port === '8001' ? `${window.location.protocol}//${window.location.hostname}:8000/` : '/';

  return (
    <div style={{ minHeight: '100vh', background: pal.pageBg, fontFamily: FONT_BODY, color: pal.text }}>
      <div style={{ background: '#2E3A26', color: '#fff', padding: '0.9rem 1.5rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: '1.15rem', fontWeight: 800, letterSpacing: '0.04em', fontFamily: FONT_HEADING, color: '#E9BE5F' }}>NIVESHAAY</div>
          <div style={{ fontSize: '0.72rem', color: '#cdbf8a' }}>Multi-Compare — N-Way Smallcase Comparison</div>
        </div>
        <a href={homeUrl} style={{ padding: '0.45rem 0.9rem', borderRadius: '6px', border: '1px solid #6A8557', background: '#3a4a30', color: '#fff', fontSize: '0.8rem', fontWeight: 600, textDecoration: 'none', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          <i className="fa-solid fa-house" /> Home
        </a>
      </div>

      {loading ? (
        <p style={{ padding: '2rem', color: pal.muted }}>Loading…</p>
      ) : (
        <div style={{ display: 'flex', alignItems: 'flex-start' }}>
          <div style={{ width: '180px', flexShrink: 0, background: '#2E3A26', minHeight: 'calc(100vh - 68px)', padding: '0.8rem 0', position: 'sticky', top: 0 }}>
            {navItems.map(n => (
              <button key={n.id} onClick={() => scrollTo(n.id)}
                style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', width: '100%', padding: '0.55rem 1rem', background: 'transparent', border: 'none', color: '#e8dfc0', fontSize: '0.78rem', textAlign: 'left', cursor: 'pointer' }}
                onMouseEnter={e => e.currentTarget.style.background = '#3a4a30'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <i className={`fa-solid ${n.icon}`} style={{ width: '14px', opacity: 0.8 }} />
                {n.label}
              </button>
            ))}
          </div>

          <div style={{ flex: 1, padding: '1.2rem', minWidth: 0 }}>
            <Card id="picker" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
              <SectionTitle>Select Entities to Compare</SectionTitle>
              <div style={{ padding: '0.9rem' }}>
                <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center', marginBottom: '0.7rem' }}>
                  <select value={addBasket} onChange={e => setAddBasket(e.target.value)} style={selectStyle}>
                    {BASKET_OPTIONS.map(o => <option key={o.key} value={o.key} style={{ color: BRAND_INK, background: '#fff' }}>{o.label}</option>)}
                  </select>
                  <button onClick={() => addEntity('ours', addBasket)} style={btnStyle}>+ Add basket</button>
                  <select value={addCompetitor} onChange={e => setAddCompetitor(e.target.value)} style={selectStyle}>
                    <option value="" style={{ color: BRAND_INK, background: '#fff' }}>Select competitor…</option>
                    {competitors.map(c => <option key={c.key} value={c.key} style={{ color: BRAND_INK, background: '#fff' }}>{c.label}{c.manager ? ` — ${c.manager}` : ''}</option>)}
                  </select>
                  <button onClick={() => addEntity('competitor', addCompetitor)} style={btnStyle}>+ Add competitor</button>
                  <span style={{ fontSize: '0.7rem', color: pal.mutedLight }}>up to {MULTI_MAX_ENTITIES}</span>
                </div>
                <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                  {entities.map((e, i) => (
                    <span key={e.id} style={{ background: colorFor(i), color: '#fff', padding: '0.25rem 0.3rem 0.25rem 0.7rem', borderRadius: '999px', fontSize: '0.74rem', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                      {e.label}{dataCache[e.id]?.loading ? '…' : ''}
                      <span onClick={() => removeEntity(e.id)} style={{ cursor: 'pointer', width: '16px', height: '16px', borderRadius: '50%', background: 'rgba(255,255,255,0.3)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.7rem' }}>×</span>
                    </span>
                  ))}
                  {entities.length === 0 && <EmptyNote text="Add 2 or more of our baskets or competitors to compare." />}
                </div>
                {entities.some(e => dataCache[e.id]?.loading) && (
                  <div style={{ marginTop: '0.6rem', fontSize: '0.72rem', color: pal.mutedLight }}>
                    <i className="fa-solid fa-spinner fa-spin" style={{ marginRight: '0.4rem' }} />
                    Still loading {entities.filter(e => dataCache[e.id]?.loading).map(e => e.label).join(', ')} — charts below will look incomplete for it until this finishes.
                  </div>
                )}
                {entities.some(e => !dataCache[e.id]?.loading && dataCache[e.id]?.profileLoading) && (
                  <div style={{ marginTop: '0.4rem', fontSize: '0.72rem', color: pal.mutedLight }}>
                    <i className="fa-solid fa-spinner fa-spin" style={{ marginRight: '0.4rem' }} />
                    Fetching live sector/market-cap mix for {entities.filter(e => !dataCache[e.id]?.loading && dataCache[e.id]?.profileLoading).map(e => e.label).join(', ')} (can take up to a minute on a cold cache) — Sector Allocation and Market Cap charts will fill in once this finishes.
                  </div>
                )}
              </div>
            </Card>

            {entities.length < 2 ? (
              <EmptyNote text="Add at least 2 entities above to see the comparison." />
            ) : (
              <>
                <div id="executive" style={{ display: 'flex', gap: '0.9rem', flexWrap: 'wrap', marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  {entities.map((e, i) => (
                    <div key={e.id} style={{ flex: '1 1 260px', minWidth: '240px' }}>
                      <PortfolioCard label={e.label} sub={e.type === 'ours' ? 'Our basket' : 'Competitor'} color={colorFor(i)}
                        stockCount={dataCache[e.id]?.stocks?.length}
                        top5={null} top10={null}
                        avgMcap={null} capMix={dataCache[e.id]?.capMix}
                        sectorCount={dataCache[e.id]?.sectorMix ? Object.keys(dataCache[e.id].sectorMix).length : null}
                        series={dataCache[e.id]?.series} />
                    </div>
                  ))}
                </div>

                <Card id="overlap" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Overlap</SectionTitle>
                  <div style={{ padding: '0.9rem' }}>
                    {!overlapData ? <EmptyNote text="Loading…" /> : (
                      <>
                        <div style={{ fontSize: '0.8rem', color: pal.text, marginBottom: '0.8rem' }}>
                          <strong>{overlapData.commonCount}</strong> stock{overlapData.commonCount === 1 ? '' : 's'} common to all {entities.length} selected entities.
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(entities.length, 3)}, 1fr)`, gap: '1rem' }}>
                          {overlapData.uniquePerEntity.map(({ entity, items }, i) => (
                            <UniqueHoldingsList key={entity.id} title={`${entity.label} Only`} color={colorFor(i)}
                              items={[...items].sort((a, b) => b.weight - a.weight).slice(0, 6).map(s => ({ ...s, key: s.nseCode || s.name }))}
                              weightKey="weight" />
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                </Card>

                <Card id="sector" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Sector Allocation</SectionTitle>
                  <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                    {sectorChartData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={320}>
                        <BarChart data={sectorChartData} layout="vertical" margin={{ left: 10, right: 10 }}>
                          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                          <XAxis type="number" unit="%" tick={{ fontSize: 10 }} />
                          <YAxis type="category" dataKey="sector" width={120} tick={{ fontSize: 10 }} />
                          <Tooltip formatter={v => `${v}%`} />
                          <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                          {entities.map((e, i) => <Bar key={e.id} dataKey={e.label} fill={colorFor(i)} barSize={12} />)}
                        </BarChart>
                      </ResponsiveContainer>
                    ) : <EmptyNote text="Sector data not available yet." />}
                  </div>
                </Card>

                <Card id="marketcap" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Market Cap Mix</SectionTitle>
                  <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                    {capMixChartData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <BarChart data={capMixChartData}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="bucket" tick={{ fontSize: 10 }} />
                          <YAxis unit="%" tick={{ fontSize: 10 }} />
                          <Tooltip formatter={v => `${v}%`} />
                          <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                          {entities.map((e, i) => <Bar key={e.id} dataKey={e.label} fill={colorFor(i)} barSize={16} />)}
                        </BarChart>
                      </ResponsiveContainer>
                    ) : <EmptyNote text="Market-cap mix not available yet." />}
                  </div>
                </Card>

                <Card id="performance" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Performance (indexed to 100)</SectionTitle>
                  <div style={{ padding: '0.6rem 0.9rem 0', fontSize: '0.78rem', fontWeight: 600, display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
                    <span style={{ color: pal.text }}>Max Drawdown (since inception):</span>
                    {drawdownByEntity.map(({ entity, drawdown }, i) => (
                      <span key={entity.id} style={{ color: colorFor(i) }}>{entity.label} {drawdown != null ? `${(drawdown * 100).toFixed(1)}%` : '—'}</span>
                    ))}
                  </div>
                  <div style={{ padding: '0.6rem 0.9rem 0.9rem' }}>
                    {perfChartData.length > 1 ? (
                      <ResponsiveContainer width="100%" height={320}>
                        <LineChart data={perfChartData}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="date" tick={{ fontSize: 9 }} minTickGap={40} />
                          <YAxis tick={{ fontSize: 10 }} domain={['auto', 'auto']} />
                          <Tooltip />
                          <Legend wrapperStyle={{ fontSize: '0.72rem' }} />
                          {entities.map((e, i) => <Line key={e.id} type="monotone" dataKey={e.label} stroke={colorFor(i)} dot={false} strokeWidth={2} connectNulls />)}
                        </LineChart>
                      </ResponsiveContainer>
                    ) : <EmptyNote text="Not enough overlapping price history yet." />}
                  </div>
                </Card>

                <Card id="stocks" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Stock Comparison</SectionTitle>
                  <div style={{ display: 'grid', gridTemplateColumns: `2fr repeat(${entities.length}, 90px)`, gap: '0.4rem', padding: '0.5rem 0.9rem', fontSize: '0.68rem', fontWeight: 700, color: pal.muted, textTransform: 'uppercase', background: pal.headerRow, borderBottom: `1px solid ${pal.border}` }}>
                    <span>Stock</span>
                    {entities.map(e => <span key={e.id} style={{ textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.label}</span>)}
                  </div>
                  <div style={{ maxHeight: '460px', overflowY: 'auto' }}>
                    {stockTableRows.map((r, i) => (
                      <div key={r.name} style={{ display: 'grid', gridTemplateColumns: `2fr repeat(${entities.length}, 90px)`, gap: '0.4rem', padding: '0.35rem 0.9rem', fontSize: '0.76rem', background: i % 2 ? pal.rowA : pal.rowB, borderTop: `1px solid ${pal.rowDivider}` }}>
                        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: pal.text }}>{r.name}</span>
                        {entities.map(e => <span key={e.id} style={{ textAlign: 'right', color: pal.muted }}>{r.weights[e.label] != null ? `${r.weights[e.label].toFixed(2)}%` : '—'}</span>)}
                      </div>
                    ))}
                  </div>
                </Card>

                <Card id="rebalance" style={{ marginBottom: '1rem', scrollMarginTop: '12px' }}>
                  <SectionTitle>Rebalance History</SectionTitle>
                  <div style={{ padding: '0.9rem', display: 'grid', gridTemplateColumns: `repeat(${Math.min(entities.length, 3)}, 1fr)`, gap: '1.2rem' }}>
                    {entities.map((e, i) => (
                      <RebalanceHistoryPanel key={e.id} label={e.label} color={colorFor(i)} history={dataCache[e.id]?.rebalanceHistory}
                        limitedNote="smallcase only exposes exact stock-level detail for the most recent rebalance -- older dates show added/removed counts only." />
                    ))}
                  </div>
                </Card>

                <Card id="timing" style={{ scrollMarginTop: '12px' }}>
                  <SectionTitle right={<span style={{ fontWeight: 400, textTransform: 'none', color: pal.mutedLight, fontSize: '0.74rem' }}>{entities[0]?.label} vs each other, within {TIMING_WINDOW_DAYS} days</span>}>
                    Stock Timing Insights
                  </SectionTitle>
                  <div style={{ padding: '0.9rem' }}>
                    {timingByEntity.length === 0 ? (
                      <EmptyNote text="Add at least 2 entities to see timing insights (compared against the first one added)." />
                    ) : timingByEntity.map(({ entity, buys, sells }) => (
                      <div key={entity.id} style={{ marginBottom: '1.2rem' }}>
                        <div style={{ fontSize: '0.78rem', fontWeight: 700, color: pal.heading, marginBottom: '0.4rem' }}>{entities[0]?.label} vs {entity.label}</div>
                        <div style={{ fontSize: '0.7rem', fontWeight: 700, color: pal.muted, marginBottom: '0.2rem' }}>Buys</div>
                        {buys.length === 0 ? <EmptyNote text="No matching buys within the window." /> : buys.map(t => (
                          <TimingInsightRow key={`buy-${t.name}-${t.theirDate}`} t={t} action="added" ourBasketLabel={entities[0]?.label} selected={entity} ohlcData={ohlcData} />
                        ))}
                        <div style={{ fontSize: '0.7rem', fontWeight: 700, color: pal.muted, margin: '0.5rem 0 0.2rem' }}>Sells</div>
                        {sells.length === 0 ? <EmptyNote text="No matching sells within the window." /> : sells.map(t => (
                          <TimingInsightRow key={`sell-${t.name}-${t.theirDate}`} t={t} action="removed" ourBasketLabel={entities[0]?.label} selected={entity} ohlcData={ohlcData} />
                        ))}
                      </div>
                    ))}
                  </div>
                </Card>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
