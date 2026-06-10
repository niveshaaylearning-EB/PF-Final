import { API_BASE } from '../api/base.js';
import { useState } from 'react';

const UPDATE_TYPE_COLORS = {
  'New Addition': '#10b981',
  'Partial Add':  '#34d399',
  'Partial Sell': '#f87171',
  'Wholly Sell':  '#ef4444',
  'No Change':    '#64748b',
};

const cellStyle = { padding: '0.35rem 0.7rem', fontSize: '0.79rem', verticalAlign: 'middle' };
const hdrStyle  = { padding: '0.3rem 0.7rem', color: '#64748b', fontWeight: 600,
                    fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.04em',
                    borderBottom: '1px solid rgba(255,255,255,0.1)' };
const inputStyle = {
  background: 'transparent', border: 'none',
  borderBottom: '1px solid rgba(99,102,241,0.3)',
  color: '#e2e8f0', fontSize: '0.79rem', width: '100%',
  outline: 'none', padding: '0.1rem 0',
};

function Badge({ type }) {
  const color = UPDATE_TYPE_COLORS[type] || '#94a3b8';
  return (
    <span style={{ fontSize: '0.68rem', fontWeight: 700, color,
                   background: color + '22', padding: '0.15rem 0.5rem',
                   borderRadius: '4px', whiteSpace: 'nowrap' }}>
      {type}
    </span>
  );
}

export default function RebalanceUploadModal({ previewData, onClose, onConfirmed }) {
  const hasSlide1 = previewData.slide1.length > 0;
  const [slide,    setSlide]    = useState(hasSlide1 ? 1 : 2);
  const [slide1,   setSlide1]   = useState(previewData.slide1);
  const [slide2,   setSlide2]   = useState(previewData.slide2);
  const [confirming, setConfirming] = useState(false);
  const [error,    setError]    = useState('');

  const totalSlides = hasSlide1 ? 2 : 1;
  const displaySlide = hasSlide1 ? slide : 2;

  // Propagate slide1 date edits → slide2 eventDate
  const updateSlide1Date = (idx, field, value) => {
    setSlide1(prev => {
      const next = [...prev];
      next[idx] = { ...next[idx], [field]: value };
      // If user edits newDate, also sync into slide2 for that stock
      if (field === 'newDate') {
        setSlide2(s2 => s2.map(row =>
          row.nseCode === next[idx].nseCode ? { ...row, eventDate: value } : row
        ));
      }
      return next;
    });
  };

  const updateSlide2 = (idx, field, value) => {
    setSlide2(prev => {
      const next = [...prev];
      next[idx] = { ...next[idx], [field]: value };
      return next;
    });
  };

  const handleConfirm = async () => {
    setError('');
    setConfirming(true);
    try {
      const token = localStorage.getItem('nia_auth_token') || '';
      const resp = await fetch(`${API_BASE}/confirm-rebalance`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          basket:           previewData.basketKey,
          latestDate:       previewData.latestDate,
          slide2,
          historicalEvents: previewData.historicalEvents,
          historyEntries:   previewData.historyEntries,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        const d = data.detail;
        throw new Error(Array.isArray(d) ? d.map(e => e.msg || JSON.stringify(e)).join('; ') : String(d || 'Confirm failed'));
      }
      onConfirmed(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setConfirming(false);
    }
  };

  const canGoNext   = hasSlide1 && slide === 1;
  const canGoBack   = hasSlide1 && slide === 2;
  const isLastSlide = displaySlide === 2;

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(0,0,0,0.7)', display: 'flex',
      alignItems: 'center', justifyContent: 'center', padding: '1rem',
    }}>
      <div style={{
        background: 'rgba(15,23,42,0.98)', border: '1px solid rgba(99,102,241,0.35)',
        borderRadius: '14px', width: '100%', maxWidth: '900px',
        maxHeight: '90vh', display: 'flex', flexDirection: 'column',
        boxShadow: '0 20px 60px rgba(0,0,0,0.7)',
      }}>
        {/* Header */}
        <div style={{ padding: '1.2rem 1.5rem 1rem', borderBottom: '1px solid rgba(255,255,255,0.07)',
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: '1rem', color: '#e2e8f0' }}>
              <i className="fa-solid fa-code-branch" style={{ color: '#818cf8', marginRight: '0.5rem' }} />
              Review Rebalance — {previewData.basket}
            </div>
            <div style={{ fontSize: '0.76rem', color: '#64748b', marginTop: '0.25rem' }}>
              {previewData.latestDate} · {previewData.slide2.length} stock{previewData.slide2.length !== 1 ? 's' : ''}
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            {totalSlides > 1 && (
              <span style={{ fontSize: '0.78rem', color: '#64748b' }}>{slide} / {totalSlides}</span>
            )}
            <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer',
              color: '#64748b', fontSize: '1.1rem', padding: '0.2rem' }}>
              <i className="fa-solid fa-xmark" />
            </button>
          </div>
        </div>

        {/* Slide indicator dots */}
        {totalSlides > 1 && (
          <div style={{ display: 'flex', gap: '0.4rem', justifyContent: 'center', padding: '0.75rem 0 0' }}>
            {[1, 2].map(n => (
              <div key={n} style={{
                width: '8px', height: '8px', borderRadius: '50%',
                background: slide === n ? '#818cf8' : 'rgba(99,102,241,0.25)',
                transition: 'background 0.2s',
              }} />
            ))}
          </div>
        )}

        {/* Slide content */}
        <div style={{ overflowY: 'auto', flex: 1, padding: '1rem 1.5rem' }}>

          {/* ── Slide 1: Date Discrepancies ── */}
          {displaySlide === 1 && (
            <>
              <div style={{ fontSize: '0.84rem', color: '#94a3b8', marginBottom: '0.9rem' }}>
                <i className="fa-solid fa-calendar-days" style={{ color: '#fbbf24', marginRight: '0.4rem' }} />
                The following stocks have existing event dates that differ slightly from the Excel rebalance date.
                Edit the <strong style={{ color: '#e2e8f0' }}>New Date</strong> column if needed before continuing.
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    {['Stock', 'Event', 'Existing Date', 'New Date (Excel)', 'Diff'].map(h => (
                      <th key={h} style={hdrStyle}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {slide1.map((row, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                      <td style={cellStyle}>
                        <div style={{ color: '#e2e8f0', fontWeight: 500 }}>{row.stockName}</div>
                        <div style={{ color: '#64748b', fontSize: '0.72rem' }}>{row.nseCode}</div>
                      </td>
                      <td style={cellStyle}>
                        <span style={{ fontSize: '0.72rem', fontWeight: 700,
                          color: row.eventType === 'Buy' ? '#34d399' : '#f87171',
                          background: (row.eventType === 'Buy' ? '#34d399' : '#f87171') + '22',
                          padding: '0.15rem 0.4rem', borderRadius: '4px' }}>
                          {row.eventType}
                        </span>
                      </td>
                      <td style={{ ...cellStyle, color: '#94a3b8' }}>{row.existingDate}</td>
                      <td style={cellStyle}>
                        <input
                          style={inputStyle}
                          value={row.newDate}
                          onChange={e => updateSlide1Date(i, 'newDate', e.target.value)}
                        />
                      </td>
                      <td style={{ ...cellStyle, color: '#fbbf24', fontWeight: 600 }}>
                        {row.diffDays > 0 ? `±${row.diffDays}d` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {/* ── Slide 2: Weight Changes ── */}
          {displaySlide === 2 && (
            <>
              <div style={{ fontSize: '0.84rem', color: '#94a3b8', marginBottom: '0.9rem' }}>
                <i className="fa-solid fa-scale-balanced" style={{ color: '#818cf8', marginRight: '0.4rem' }} />
                Review weight changes for the rebalance. All fields are editable before confirming.
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    {['Stock', 'NSE Code', 'Prev Wt%', 'New Wt%', 'Change', 'Update Type', 'Event Date'].map(h => (
                      <th key={h} style={{ ...hdrStyle, textAlign: h.includes('Wt') || h === 'Change' ? 'right' : 'left' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {slide2.map((row, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                      <td style={cellStyle}>
                        <input
                          style={inputStyle}
                          value={row.stockName}
                          onChange={e => updateSlide2(i, 'stockName', e.target.value)}
                        />
                      </td>
                      <td style={cellStyle}>
                        <input
                          style={{ ...inputStyle, color: '#818cf8', fontWeight: 700, width: '8rem' }}
                          value={row.nseCode}
                          onChange={e => updateSlide2(i, 'nseCode', e.target.value.toUpperCase())}
                        />
                      </td>
                      <td style={{ ...cellStyle, textAlign: 'right', color: '#94a3b8' }}>
                        {row.prevWeight > 0 ? `${row.prevWeight}%` : '—'}
                      </td>
                      <td style={{ ...cellStyle, textAlign: 'right',
                                   color: row.newWeight === 0 ? '#94a3b8' : '#e2e8f0', fontWeight: 600 }}>
                        {row.newWeight > 0 ? `${row.newWeight}%` : '—'}
                      </td>
                      <td style={{ ...cellStyle, textAlign: 'right',
                                   color: row.updateType === 'New Addition' || row.updateType === 'Partial Add'
                                     ? '#34d399' : row.updateType === 'No Change' ? '#64748b' : '#f87171' }}>
                        {row.change}
                      </td>
                      <td style={cellStyle}>
                        <select
                          value={row.updateType}
                          onChange={e => updateSlide2(i, 'updateType', e.target.value)}
                          style={{ background: 'rgba(15,23,42,0.9)', border: '1px solid rgba(99,102,241,0.3)',
                                   color: UPDATE_TYPE_COLORS[row.updateType] || '#94a3b8',
                                   borderRadius: '4px', padding: '0.15rem 0.3rem',
                                   fontSize: '0.72rem', fontWeight: 700, cursor: 'pointer' }}
                        >
                          {['New Addition','Partial Add','Partial Sell','Wholly Sell','No Change'].map(opt => (
                            <option key={opt} value={opt}>{opt}</option>
                          ))}
                        </select>
                      </td>
                      <td style={cellStyle}>
                        <input
                          style={{ ...inputStyle, width: '9rem' }}
                          value={row.eventDate}
                          onChange={e => updateSlide2(i, 'eventDate', e.target.value)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>

        {/* Footer */}
        <div style={{ padding: '1rem 1.5rem', borderTop: '1px solid rgba(255,255,255,0.07)',
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem' }}>
          <button
            onClick={onClose}
            style={{ background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.25)',
                     color: '#f87171', borderRadius: '8px', padding: '0.5rem 1.1rem',
                     cursor: 'pointer', fontSize: '0.84rem', fontWeight: 600 }}
          >
            Cancel
          </button>

          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            {error && (
              <span style={{ color: '#ef4444', fontSize: '0.78rem' }}>
                <i className="fa-solid fa-triangle-exclamation" style={{ marginRight: '0.3rem' }} />
                {error}
              </span>
            )}

            {canGoBack && (
              <button
                onClick={() => setSlide(1)}
                style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.25)',
                         color: '#818cf8', borderRadius: '8px', padding: '0.5rem 1.1rem',
                         cursor: 'pointer', fontSize: '0.84rem', fontWeight: 600 }}
              >
                <i className="fa-solid fa-arrow-left" style={{ marginRight: '0.4rem' }} />
                Back
              </button>
            )}

            {canGoNext && (
              <button
                onClick={() => setSlide(2)}
                style={{ background: 'rgba(99,102,241,0.18)', border: '1px solid rgba(99,102,241,0.35)',
                         color: '#818cf8', borderRadius: '8px', padding: '0.5rem 1.2rem',
                         cursor: 'pointer', fontSize: '0.84rem', fontWeight: 600 }}
              >
                Next
                <i className="fa-solid fa-arrow-right" style={{ marginLeft: '0.4rem' }} />
              </button>
            )}

            {isLastSlide && (
              <button
                onClick={handleConfirm}
                disabled={confirming}
                style={{ background: confirming ? 'rgba(16,185,129,0.08)' : 'rgba(16,185,129,0.18)',
                         border: '1px solid rgba(16,185,129,0.35)',
                         color: confirming ? '#64748b' : '#10b981',
                         borderRadius: '8px', padding: '0.5rem 1.4rem',
                         cursor: confirming ? 'default' : 'pointer',
                         fontSize: '0.84rem', fontWeight: 700 }}
              >
                <i className={`fa-solid ${confirming ? 'fa-spinner fa-spin' : 'fa-check'}`}
                   style={{ marginRight: '0.4rem' }} />
                {confirming ? 'Saving…' : 'Save & Confirm'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
