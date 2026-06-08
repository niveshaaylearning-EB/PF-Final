import { useRef, useMemo, useState } from 'react';
import { formatPercent, formatRupee, getColorClass } from '../App.jsx';
import NseAutocomplete from './NseAutocomplete.jsx';
import ColumnFilter from './ColumnFilter.jsx';

// Formula and PE Ratio removed per user request
const TABLE_HEADERS = [
  'NSE Code', 'Allocation', 'Performance (1M)', 'Contribution (1M)',
  'Buy Price', 'CMP', 'Market Cap (Cr)', 'Open 1M', 'Close 1M',
  'High 1M', 'Low 1M', 'Absolute Returns', 'Holding Days', 'CMP Status', 'Actions',
];

const TABLE_HEADERS_IPO = [
  'NSE Code', 'Listing Date', 'Performance', 'Contribution',
  'Listing Price', 'CMP', 'Market Cap (Cr)', 'Open 1M', 'Close 1M',
  'High 1M', 'Low 1M', 'Absolute Returns', 'Holding Days', 'Actions',
];

const SORT_KEY = {
  'NSE Code':        'nseCode',
  'Allocation':      'allocation',
  'Performance (1M)': 'performance',
  'Contribution (1M)': 'contribution',
  'Buy Price':       'buyPrice',
  'Listing Price':   'buyPrice',
  'CMP':             'cmp',
  'Market Cap (Cr)': 'marketCap',
  'Open 1M':         'open1M',
  'Close 1M':        'close1M',
  'High 1M':         'high1M',
  'Low 1M':          'low1M',
  'Absolute Returns':'absoluteReturns',
  'Holding Days':    'holdingDays',
};

function getCmpStatus(cmp, targetPrice, stopLoss) {
  if (cmp == null) return null;
  if (targetPrice != null) {
    if (cmp >= targetPrice)             return { text: 'Target Achieved',  color: '#10b981' };
    if (cmp >= targetPrice * 0.95)      return { text: 'Near Target Price', color: '#eab308' };
  }
  if (stopLoss != null) {
    if (cmp <= stopLoss)                return { text: 'SL Triggered',     color: '#ef4444' };
    if (cmp <= stopLoss * 1.05)         return { text: 'Near Stop Loss',   color: '#f97316' };
  }
  return null;
}

function CellLoading() {
  return <span className="cell-loading">Loading…</span>;
}

const OHLC_LABELS = new Set(['Open 1M', 'Close 1M', 'High 1M', 'Low 1M', 'Market Cap (Cr)', 'CMP Status']);

function getColVal(field, row) {
  switch (field) {
    case 'nseCode':         return row.nseCode || '';
    case 'allocation':      return row.allocation != null ? (row.allocation * 100).toFixed(2) + '%' : '';
    case 'performance':     return row.performance != null ? (row.performance * 100).toFixed(2) + '%' : '';
    case 'contribution':    return row.contribution != null ? (row.contribution * 100).toFixed(2) + '%' : '';
    case 'buyPrice':        return row.buyPrice != null ? String(row.buyPrice) : '';
    case 'cmp':             return row.cmp != null ? String(row.cmp) : '';
    case 'marketCap':       return row.marketCap != null ? String(Math.round(row.marketCap)) : '';
    case 'open1M':          return row.open1M != null ? String(row.open1M) : '';
    case 'close1M':         return row.close1M != null ? String(row.close1M) : '';
    case 'high1M':          return row.high1M != null ? String(row.high1M) : '';
    case 'low1M':           return row.low1M != null ? String(row.low1M) : '';
    case 'absoluteReturns': return row.absoluteReturns != null ? (row.absoluteReturns * 100).toFixed(2) + '%' : '';
    case 'holdingDays':     return row.holdingDays != null ? `${row.holdingDays} days` : '';
    default:                return String(row[field] ?? '');
  }
}

function DataRow({
  row, idx, searchTerm, nseSymbols, isIPO, showOHLC, readOnly,
  onNseChange, onAllocChange, onBuyPriceChange, onListingDateChange,
  onAddRow, onRemoveRow,
  onInfoHover, onInfoLeave,
}) {
  const buyPriceRef = useRef(null);

  const loading = row.nseCode && row.cmp === null;
  const lv = (val) => loading ? <CellLoading /> : val;

  const lowerSearch  = searchTerm.toLowerCase().trim();
  const lowerNse     = (row.nseCode || '').toLowerCase();
  const matchSearch  = !lowerSearch || lowerNse.includes(lowerSearch);
  const dimRow       = lowerSearch && !matchSearch;
  const highlightRow = lowerSearch && matchSearch;
  const trClass      = dimRow ? 'search-dim' : highlightRow ? 'search-highlight' : '';

  const handleAllocBlur = (e) => { onAllocChange(idx, e.target.value); };

  const handleBuyPriceBlur = () => {
    const val = (buyPriceRef.current?.value || '').trim();
    if (val) onBuyPriceChange(idx, val);
  };
  const handleBuyPriceKeyDown = (e) => { if (e.key === 'Enter') e.target.blur(); };

  const handleInfoEnter = (e) => {
    const rect   = e.currentTarget.getBoundingClientRect();
    const approxH = 160;
    const top  = (rect.bottom + approxH + 8 > window.innerHeight)
      ? Math.max(8, rect.top - approxH - 8)
      : rect.bottom + 6;
    const left = Math.min(rect.left, window.innerWidth - 310);
    onInfoHover(row.nseCode || '', left, top);
  };

  const allocDisplay    = row.allocation != null ? (row.allocation * 100).toFixed(2) : '';
  const buyPriceDisplay = row.buyPrice   != null ? row.buyPrice : '';

  return (
    <tr className={trClass} data-row-index={idx}>
      {/* NSE Code */}
      <td className="nse-code editable-nse pt-sticky-col" style={{ position: 'sticky', left: 0, zIndex: 1, overflow: 'visible' }}>
        <button className="stock-info-btn" onMouseEnter={handleInfoEnter} onMouseLeave={onInfoLeave} title="Portfolio history">
          <i className="fa-solid fa-circle-info" />
        </button>
        <NseAutocomplete
          initialValue={row.nseCode || ''}
          onCommit={(val) => onNseChange(idx, val)}
          symbols={nseSymbols}
        />
      </td>

      {/* Listing Date — IPO only */}
      {isIPO && (
        <td style={{ whiteSpace: 'nowrap' }}>
          <input
            className="cell-edit"
            defaultValue={row.listingDate || ''}
            placeholder="DD Mon YYYY"
            onBlur={e => onListingDateChange && onListingDateChange(idx, e.target.value.trim())}
            style={{ width: '7.5rem', fontSize: '0.82rem' }}
          />
        </td>
      )}

      {/* Allocation — hidden for IPO */}
      {!isIPO && (
        <td className="editable-alloc">
          <div className="alloc-wrapper">
            <input
              type="number" className="cell-edit alloc-edit"
              defaultValue={allocDisplay}
              step="0.01" min="0" max="100" placeholder="0.00"
              onBlur={handleAllocBlur}
            />
            <span className="alloc-suffix">%</span>
          </div>
        </td>
      )}

      {/* Performance */}
      <td className={`${getColorClass(row.performance)} perf-display`}>
        {lv(formatPercent(row.performance))}
      </td>

      {/* Contribution */}
      <td className={`${getColorClass(row.contribution)} contrib-display`}>
        {lv(formatPercent(row.contribution))}
      </td>

      {/* Buy Price / Listing Price */}
      <td className="editable-alloc">
        <div className="alloc-wrapper">
          <span className="alloc-suffix" style={{ left: '0.4rem', right: 'auto' }}>₹</span>
          <input
            ref={buyPriceRef}
            type="number"
            className="cell-edit buyprice-edit"
            defaultValue={buyPriceDisplay}
            step="0.01" min="0"
            placeholder="—"
            onBlur={handleBuyPriceBlur}
            onKeyDown={handleBuyPriceKeyDown}
            style={{ paddingLeft: '1.4rem', paddingRight: '0.4rem' }}
          />
        </div>
      </td>

      {/* CMP */}
      <td className="cmp-display">{lv(formatRupee(row.cmp))}</td>

      {/* OHLC columns */}
      {showOHLC && <td className="marketcap-display">{lv(formatRupee(row.marketCap))}</td>}
      {showOHLC && <td className="open1m-display">{lv(formatRupee(row.open1M))}</td>}
      {showOHLC && <td className="close1m-display">{lv(formatRupee(row.close1M))}</td>}
      {showOHLC && <td className="high1m-display">{lv(formatRupee(row.high1M))}</td>}
      {showOHLC && <td className="low1m-display">{lv(formatRupee(row.low1M))}</td>}

      {/* Absolute Returns */}
      <td className={`${getColorClass(row.absoluteReturns)} absret-display`}>
        {lv(formatPercent(row.absoluteReturns))}
      </td>

      {/* Holding Days */}
      <td className="holdingdays-display">
        {row.holdingDays != null ? (
          <span className="pt-holding-badge">{row.holdingDays} days</span>
        ) : '—'}
      </td>

      {/* CMP Status */}
      {!isIPO && showOHLC && (
        <td className="cmpstatus-display">
          {(() => {
            const s = getCmpStatus(row.cmp, row.targetPrice, row.stopLoss);
            return s
              ? <span style={{ color: s.color, fontWeight: 600, fontSize: '0.78rem', whiteSpace: 'nowrap' }}>{s.text}</span>
              : <span style={{ color: 'var(--text-secondary)' }}>—</span>;
          })()}
        </td>
      )}

      {/* Actions */}
      <td className="action-cell">
        {!readOnly && <button className="row-action-btn row-add-btn"    title="Add new stock" onClick={() => onAddRow(idx)}>+</button>}
        {!readOnly && <button className="row-action-btn row-remove-btn" title="Remove stock"   onClick={() => onRemoveRow(idx)}>&minus;</button>}
      </td>
    </tr>
  );
}

export default function PortfolioTable({
  rows, searchTerm, nseSymbols, isIPO, readOnly = false,
  onNseChange, onAllocChange, onBuyPriceChange, onListingDateChange,
  onAddRow, onRemoveRow,
  onInfoHover, onInfoLeave,
  totalContribution, avgMarketCap, medianPE,
}) {
  const [sortKey,   setSortKey]   = useState(null);
  const [sortDir,   setSortDir]   = useState('asc');
  const [showOHLC,  setShowOHLC]  = useState(false);
  const [colFilters,  setColFilters]  = useState({});
  const [openFilter,  setOpenFilter]  = useState(null);
  const [filterPos,   setFilterPos]   = useState({ top: 0, left: 0 });

  const handleSort = (key) => {
    if (sortKey === key) {
      if (sortDir === 'asc') setSortDir('desc');
      else { setSortKey(null); setSortDir('asc'); }
    } else {
      setSortKey(key); setSortDir('asc');
    }
  };

  const handleFilterOpen = (col, e) => {
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    setFilterPos({ top: rect.bottom, left: rect.left });
    setOpenFilter(prev => prev === col ? null : col);
  };
  const handleFilterSort = (col, dir) => { setSortKey(col); setSortDir(dir); };
  const handleFilterVal  = (col, vals) => {
    setColFilters(prev => {
      const next = { ...prev };
      if (vals === null) delete next[col]; else next[col] = vals;
      return next;
    });
  };

  const sortedEntries = useMemo(() => {
    const indexed = rows.map((r, i) => [r, i]);
    const filtered = Object.keys(colFilters).length === 0 ? indexed : indexed.filter(([r]) => {
      for (const [field, values] of Object.entries(colFilters)) {
        if (!values) continue;
        if (values.size === 0) return false;
        if (!values.has(getColVal(field, r))) return false;
      }
      return true;
    });
    if (!sortKey) return filtered;
    return [...filtered].sort(([a], [b]) => {
      const d = sortDir === 'asc' ? 1 : -1;
      if (sortKey === 'nseCode') return d * (a.nseCode || '').localeCompare(b.nseCode || '');
      const va = a[sortKey] ?? -Infinity;
      const vb = b[sortKey] ?? -Infinity;
      return d * (va - vb);
    });
  }, [rows, sortKey, sortDir, colFilters]);

  const totalAllocation = rows.reduce((s, r) => s + (r.allocation || 0), 0);
  const headers = (isIPO ? TABLE_HEADERS_IPO : TABLE_HEADERS).filter(h => showOHLC || !OHLC_LABELS.has(h));
  const colCount = headers.length;

  if (rows.length === 0) {
    return (
      <div className="table-container">
        <table className="portfolio-table">
          <thead><tr>{headers.map(h => <th key={h}>{h}</th>)}</tr></thead>
          <tbody>
            <tr>
              <td colSpan={colCount} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-secondary)' }}>
                Loading portfolio data…
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <>
    <div className="ohlc-toggle-bar">
      <button className="ohlc-toggle-btn" onClick={() => setShowOHLC(v => !v)}>
        {showOHLC ? '▲ Hide Details' : '▼ Show Details'}
      </button>
    </div>
    <div className="table-container">
      <table className="portfolio-table">
        <thead>
          <tr>
            {headers.map(h => {
              const col = SORT_KEY[h];
              const isFiltered = col && colFilters[col] != null;
              const isSorted   = col && sortKey === col;
              if (col) {
                return (
                  <th key={h} style={{ whiteSpace: 'nowrap', userSelect: 'none' }}>
                    <div className="cf-th-inner">
                      <span onClick={() => handleSort(col)} style={{ cursor: 'pointer' }}>{h}</span>
                      <span style={{ fontSize: '0.6em', color: isSorted ? '#a5b4fc' : '#3a4f6a', marginLeft: '0.1em' }}>
                        {isSorted ? (sortDir === 'asc' ? '▲' : '▼') : '⇅'}
                      </span>
                      <button className={`cf-trigger${isFiltered ? ' on' : ''}`}
                        onClick={e => handleFilterOpen(col, e)} title="Filter">▾</button>
                    </div>
                  </th>
                );
              }
              return <th key={h}>{h}</th>;
            })}
          </tr>
        </thead>
        <tbody>
          {sortedEntries.map(([row, origIdx]) => (
            <DataRow
              key={origIdx}
              row={row} idx={origIdx} searchTerm={searchTerm} nseSymbols={nseSymbols} isIPO={isIPO} showOHLC={showOHLC} readOnly={readOnly}
              onNseChange={onNseChange}
              onAllocChange={onAllocChange}
              onBuyPriceChange={onBuyPriceChange}
              onListingDateChange={onListingDateChange}
              onAddRow={onAddRow}
              onRemoveRow={onRemoveRow}
              onInfoHover={onInfoHover}
              onInfoLeave={onInfoLeave}
            />
          ))}

          {/* Summary row */}
          {isIPO ? (
            <tr className="summary-row pt-summary-row">
              <td style={{ fontWeight: 700, color: 'var(--text-secondary)', paddingRight: '1.5rem', textAlign: 'right' }}>Average</td>
              <td />{/* Listing Date */}
              <td />{/* Performance */}
              <td />{/* Contribution */}
              <td colSpan={2} />{/* Listing Price + CMP */}
              {showOHLC && <td style={{ fontWeight: 700, color: 'var(--text-primary)' }}>{formatRupee(Math.round(avgMarketCap))}</td>}
              {showOHLC && <td colSpan={4} />}
              <td />{/* Abs Returns */}
              <td />{/* Holding Days */}
              <td />{/* Actions */}
            </tr>
          ) : (
            <tr className="summary-row pt-summary-row">
              <td style={{ fontWeight: 700, color: 'var(--text-secondary)', textAlign: 'right' }}>Total / Avg</td>
              <td style={{ fontWeight: 700, color: 'var(--accent-blue)' }}>{formatPercent(totalAllocation)}</td>
              <td />{/* Performance */}
              <td style={{ fontWeight: 700 }} className={getColorClass(totalContribution)}>{formatPercent(totalContribution)}</td>
              <td colSpan={2} />{/* Buy Price + CMP */}
              {showOHLC && <td style={{ fontWeight: 700, color: 'var(--text-primary)' }}>{formatRupee(Math.round(avgMarketCap))}</td>}
              {showOHLC && <td colSpan={4} />}
              <td />{/* Abs Returns */}
              <td />{/* Holding Days */}
              {showOHLC && <td />}{/* CMP Status */}
              <td />{/* Actions */}
            </tr>
          )}
        </tbody>
      </table>
    </div>
    {openFilter && (
      <ColumnFilter
        rows={rows}
        getValue={r => getColVal(openFilter, r)}
        activeValues={colFilters[openFilter] ?? null}
        isSorted={sortKey === openFilter}
        sortDir={sortDir}
        onSort={dir => handleFilterSort(openFilter, dir)}
        onFilter={vals => handleFilterVal(openFilter, vals)}
        onClose={() => setOpenFilter(null)}
        top={filterPos.top}
        left={filterPos.left}
      />
    )}
    </>
  );
}
