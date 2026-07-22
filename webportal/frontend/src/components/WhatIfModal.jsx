import { useEffect, useMemo, useRef, useState } from 'react';
import { currentSeriesBuyEvents, formatPercent, formatRupee, getColorClass } from '../App.jsx';
import { EMPTY_SLOT, applyEditToRow, computeWhatIf, toIsoDate, fromIsoDate } from '../whatIfCalc.js';
import { fetchOhlcLookup, fetchCaStatus } from '../api/client.js';

const CA_TYPE_LABEL = { split: 'stock split', bonus: 'bonus issue', demerger: 'demerger' };

export default function WhatIfModal({ nse, basketKey, basketMeta, rows, simOverlay, setSimOverlay, onClose }) {
  const overlayRef = useRef(null);
  const slot = simOverlay[basketKey] || EMPTY_SLOT;

  const det = basketMeta.buyPriceDetails?.[nse] || {};
  const historyEntry = basketMeta.history?.[nse] || null;

  const realSeries = useMemo(
    () => currentSeriesBuyEvents(det.buyEvents, det.sellEvents)
      .map(e => ({ date: e.date, weight: e.weight, ohlc: det.buyOHLC?.[e.date] ?? null })),
    [det.buyEvents, det.sellEvents, det.buyOHLC]
  );

  const events = slot.editedBuys[nse]?.events || realSeries;

  const [ohlcWarn, setOhlcWarn] = useState({});
  const [weightWarn, setWeightWarn] = useState({});
  const [caRecords, setCaRecords] = useState([]);
  const latestRequestRef = useRef({});
  const dateDebounceRef = useRef({});
  const eventsRef = useRef(events);
  eventsRef.current = events;

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    return () => { Object.values(dateDebounceRef.current).forEach(clearTimeout); };
  }, []);

  // Surface any pending/approved corporate action for this stock -- explains
  // upfront why buy-lot prices might look inconsistent (e.g. a split whose
  // ex-date falls between two lots), rather than leaving the user to wonder.
  useEffect(() => {
    let cancelled = false;
    fetchCaStatus(basketKey, nse).then(res => {
      if (!cancelled) setCaRecords(res?.records || []);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [basketKey, nse]);

  const patchSlot = (patchFn) => {
    setSimOverlay(prev => {
      const prevSlot = prev[basketKey] || EMPTY_SLOT;
      return { ...prev, [basketKey]: patchFn(prevSlot) };
    });
  };

  // Pure -- builds what editedBuys[nse] would become after `patch` is applied
  // to event `idx`, without committing it. Shared by updateEvent (which always
  // commits) and handleWeightChange (which previews the result first to
  // enforce the 100% cap before committing).
  const computeEditedEntry = (fromSlot, idx, patch) => {
    const existing = fromSlot.editedBuys[nse];
    const baseEvents = existing?.events || realSeries;
    const nextEvents = baseEvents.map((e, i) => (i === idx ? { ...e, ...patch } : e));
    const baseTotalQty   = existing?.baseTotalQty   ?? realSeries.reduce((s, e) => s + (e.weight || 0), 0);
    const baseAllocation = existing?.baseAllocation ?? (rows.find(r => r.nseCode === nse)?.allocation ?? 0);
    return { events: nextEvents, baseTotalQty, baseAllocation };
  };

  const updateEvent = (idx, patch) => {
    patchSlot(prevSlot => ({
      ...prevSlot,
      editedBuys: { ...prevSlot.editedBuys, [nse]: computeEditedEntry(prevSlot, idx, patch) },
    }));
  };

  // Weight edits change this stock's simulated allocation, which can push the
  // portfolio's total over 100% -- unlike date/OHLC edits, which never touch
  // allocation. Preview the resulting total before committing and reject the
  // edit (rather than silently letting the total exceed 100%) if it would.
  const handleWeightChange = (idx, rawValue) => {
    const newWeight = rawValue === '' ? 0 : parseFloat(rawValue);
    if (isNaN(newWeight) || newWeight < 0) return;

    const prospectiveEntry = computeEditedEntry(slot, idx, { weight: newWeight });
    const prospectiveSlot  = { ...slot, editedBuys: { ...slot.editedBuys, [nse]: prospectiveEntry } };
    const prospectiveTotalPct = computeWhatIf(rows, prospectiveSlot).totalAllocation * 100;

    if (prospectiveTotalPct > 100.0001) {
      setWeightWarn(w => ({ ...w, [idx]: `Would push total allocation to ${prospectiveTotalPct.toFixed(2)}% — capped at 100%. Reduce the weight.` }));
      return;
    }
    setWeightWarn(w => ({ ...w, [idx]: null }));
    updateEvent(idx, { weight: newWeight });
  };

  // A native date input fires onChange per segment (day/month/year) as it's
  // filled -- e.g. editing just the day of an existing full date produces a
  // complete, valid, but WRONG intermediate date on every keystroke before
  // landing on the final one. Each of those intermediate dates used to fire
  // its own OHLC lookup immediately; `latestRequestRef` was meant to let only
  // the last one win, but in practice the final lookup could still lose a
  // response-ordering race against an earlier intermediate one, leaving the
  // OLD price on screen even though the date field shows the new date.
  // Debouncing so only the value the user actually settles on ever fires a
  // request removes that race entirely, rather than trying to out-guess it.
  const fetchAndApplyOhlc = async (idx, newDate) => {
    const requestKey = newDate;
    latestRequestRef.current[idx] = requestKey;
    try {
      const res = await fetchOhlcLookup(nse, newDate);
      if (latestRequestRef.current[idx] !== requestKey) return;
      if (res && res.price != null) {
        updateEvent(idx, { date: newDate, ohlc: res.price });
        // Sanity-check against this stock's OTHER lots: a live re-fetch can
        // land on a split/bonus-adjusted price (data providers apply splits
        // retroactively to their whole history) while sibling lots still hold
        // whatever was stored before the adjustment -- exactly the "why is
        // one lot 10x the others" confusion a partial re-fetch can create.
        // Reads eventsRef (kept fresh every render) rather than the `events`
        // closure captured when this async call started, in case other lots
        // changed in the meantime.
        const siblingPrices = eventsRef.current.filter((e, j) => j !== idx && e.ohlc != null).map(e => e.ohlc);
        if (siblingPrices.length > 0) {
          const siblingAvg = siblingPrices.reduce((s, v) => s + v, 0) / siblingPrices.length;
          const ratio = siblingAvg > 0 ? res.price / siblingAvg : 1;
          if (ratio > 3 || ratio < 1 / 3) {
            setOhlcWarn(w => ({ ...w, [idx]: `This is ${ratio >= 1 ? ratio.toFixed(1) + '×' : '1/' + (1 / ratio).toFixed(1)} this stock's other buy lots (avg ${'₹'}${siblingAvg.toFixed(2)}) — likely an unrecorded stock split/bonus. Check Corporate Actions before trusting this comparison.` }));
            return;
          }
        }
        setOhlcWarn(w => ({ ...w, [idx]: null }));
      } else {
        setOhlcWarn(w => ({ ...w, [idx]: 'No price data for this date — enter the price manually.' }));
      }
    } catch {
      if (latestRequestRef.current[idx] !== requestKey) return;
      setOhlcWarn(w => ({ ...w, [idx]: 'Lookup failed — enter the price manually.' }));
    }
  };

  const handleDateChange = (idx, isoValue) => {
    const newDate = fromIsoDate(isoValue);
    const prevDate = events[idx]?.date;
    updateEvent(idx, { date: newDate });
    if (!newDate || newDate === prevDate) return; // no real change -- skip the network round-trip entirely

    if (dateDebounceRef.current[idx]) clearTimeout(dateDebounceRef.current[idx]);
    dateDebounceRef.current[idx] = setTimeout(() => {
      fetchAndApplyOhlc(idx, newDate);
    }, 500);
  };

  const isDeleted = slot.deletedNse.includes(nse);
  const toggleDelete = () => patchSlot(prevSlot => ({
    ...prevSlot,
    deletedNse: isDeleted ? prevSlot.deletedNse.filter(n => n !== nse) : [...prevSlot.deletedNse, nse],
  }));

  const resetThisStock = () => patchSlot(prevSlot => {
    const editedBuys = { ...prevSlot.editedBuys };
    delete editedBuys[nse];
    return { ...prevSlot, editedBuys, deletedNse: prevSlot.deletedNse.filter(n => n !== nse) };
  });

  const resetAll = () => setSimOverlay(prev => ({ ...prev, [basketKey]: { editedBuys: {}, deletedNse: [], added: [] } }));

  const before = useMemo(() => computeWhatIf(rows, EMPTY_SLOT), [rows]);
  const after  = useMemo(() => computeWhatIf(rows, slot), [rows, slot]);

  const realRow    = rows.find(r => r.nseCode === nse) || null;
  const currentEdit = slot.editedBuys[nse];
  const revisedRow  = isDeleted ? null : applyEditToRow(realRow, currentEdit);

  return (
    <div className="whatif-overlay" ref={overlayRef} onClick={e => { if (e.target === overlayRef.current) onClose(); }}>
      <div className="whatif-modal">
        <div className="whatif-header">
          <span className="sit-symbol" style={{ background: 'transparent', border: 'none', padding: 0 }}>
            NSE: {nse}
          </span>
          <button className="whatif-close" onClick={onClose}>&times;</button>
        </div>

        <div className="whatif-body">
          {caRecords.length > 0 && (
            <div className="whatif-ca-warning">
              {caRecords.map((r, i) => (
                <div key={i}>
                  ⚠ {r.status === 'approved' ? 'Approved' : 'Pending'} {CA_TYPE_LABEL[r.type] || r.type}
                  {r.ratio && r.type !== 'demerger' ? ` (${r.ratio.old ?? r.ratio.existing}:${r.ratio.new ?? r.ratio.bonus})` : ''}
                  {' '}with ex-date {r.exDate}. Buy lots before this date may show inconsistent prices
                  {r.status === 'pending_review' ? ' until this is reviewed and approved' : ''} — see Corporate Actions.
                </div>
              ))}
            </div>
          )}

          <div className="sit-row">
            <span className="sit-label">Added to Portfolio</span>
            <span className="sit-date">{historyEntry?.added || '—'}</span>
          </div>

          <div className="whatif-section-title">Rebalancing History</div>
          {(!historyEntry || historyEntry.rebalances.length === 0) ? (
            <div className="sit-no-data">No rebalancing records yet.</div>
          ) : (
            historyEntry.rebalances.map((r, i) => (
              <div key={i} className="sit-rebal-row">
                <span className="sit-rebal-date">{r.date}</span>
                <span>{r.note}</span>
              </div>
            ))
          )}

          <div className="whatif-section-title">What-If: Current Buy Lots</div>
          {isDeleted ? (
            <div className="sit-no-data">This stock is simulated as never bought. Click "Undo Delete" below to restore it.</div>
          ) : events.length === 0 ? (
            <div className="sit-no-data">No open buy lots to simulate.</div>
          ) : (
            events.map((e, i) => (
              <div key={i} className="whatif-buyrow">
                <input
                  type="date" value={toIsoDate(e.date)}
                  onChange={ev => handleDateChange(i, ev.target.value)}
                  style={{ width: '9.5rem' }}
                />
                <input
                  type="number" step="0.01" value={e.weight ?? ''} placeholder="Weight"
                  onChange={ev => handleWeightChange(i, ev.target.value)}
                  style={{ width: '4.6rem' }}
                />
                <span className="sit-label">% wt</span>
                <input
                  type="number" step="0.01" value={e.ohlc ?? ''} placeholder="OHLC"
                  onChange={ev => updateEvent(i, { ohlc: ev.target.value === '' ? null : parseFloat(ev.target.value) })}
                  style={{ width: '6rem' }}
                />
                {ohlcWarn[i] && <span className="whatif-warn">{ohlcWarn[i]}</span>}
                {weightWarn[i] && <span className="whatif-warn">{weightWarn[i]}</span>}
              </div>
            ))
          )}

          {!isDeleted && realRow && (
            <div className="whatif-impact">
              <div className="whatif-impact-row">
                <span className="sit-label">Weight</span>
                <span>{formatPercent(realRow.allocation)} &rarr; <strong>{formatPercent(revisedRow?.allocation)}</strong></span>
              </div>
              <div className="whatif-impact-row">
                <span className="sit-label">Buy Price</span>
                <span>{formatRupee(realRow.buyPrice)} &rarr; <strong>{formatRupee(revisedRow?.buyPrice)}</strong></span>
              </div>
              <div className="whatif-impact-row">
                <span className="sit-label">Gain / Loss %</span>
                <span>
                  <span className={getColorClass(realRow.absoluteReturns)}>{formatPercent(realRow.absoluteReturns)}</span>
                  {' → '}
                  <strong className={getColorClass(revisedRow?.absoluteReturns)}>{formatPercent(revisedRow?.absoluteReturns)}</strong>
                </span>
              </div>
            </div>
          )}

          <div className="whatif-section-title">Portfolio Impact (simulated)</div>
          <div className="whatif-impact">
            <div className="whatif-impact-row">
              <span className="sit-label">Total Allocation</span>
              <span>{formatPercent(before.totalAllocation)} &rarr; <strong>{formatPercent(after.totalAllocation)}</strong></span>
            </div>
            <div className="whatif-impact-row">
              <span className="sit-label">Weighted Avg Gain %</span>
              <span>
                <span className={getColorClass(before.weightedGainPct)}>{formatPercent(before.weightedGainPct)}</span>
                {' → '}
                <strong className={getColorClass(after.weightedGainPct)}>{formatPercent(after.weightedGainPct)}</strong>
              </span>
            </div>
            <div className="whatif-impact-row">
              <span className="sit-label">1M Weighted Contribution</span>
              <span>{formatPercent(before.totalContribution)} &rarr; {formatPercent(after.totalContribution)}</span>
            </div>
            <div className="sit-no-data" style={{ marginTop: '0.4rem' }}>
              Buy-date/price edits move Gain %; weight edits also move Total Allocation/Contribution. 1M Returns and Since Inception track live market momentum, not buy price, so they're otherwise unaffected.
            </div>
          </div>

          <div className="whatif-section-title">Delete Stock (simulation only)</div>
          <button className="btn btn-secondary" onClick={toggleDelete} style={{ fontSize: '0.78rem' }}>
            {isDeleted ? 'Undo Delete' : `Simulate not having bought ${nse}`}
          </button>
          <div className="sit-no-data" style={{ marginTop: '0.5rem' }}>
            To add a hypothetical new stock, use the "Add Hypothetical Stock" bar at the end of the holdings table.
          </div>
        </div>

        <div className="whatif-footer">
          <span className="sit-no-data" style={{ marginRight: 'auto', alignSelf: 'center' }}>Temporary — resets on refresh, never saved.</span>
          <button className="btn btn-secondary" onClick={resetThisStock} style={{ fontSize: '0.78rem' }}>Reset This Stock</button>
          <button className="btn btn-secondary" onClick={resetAll} style={{ fontSize: '0.78rem' }}>Reset All</button>
          <button className="btn btn-secondary" onClick={onClose} style={{ fontSize: '0.78rem' }}>Close</button>
        </div>
      </div>
    </div>
  );
}
