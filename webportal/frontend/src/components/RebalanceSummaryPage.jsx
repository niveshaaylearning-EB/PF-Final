import { useEffect, useState } from 'react';
import { fetchRebalanceSummary } from '../api/client.js';

const fmtWt = (v) => v == null ? '' : (Math.round(v * 100) / 100) + '%';

function DayCard({ entry }) {
  const { date, added, removed, reweighted } = entry;
  return (
    <div style={{
      background: 'var(--card-bg, var(--panel-bg))', border: '1px solid var(--panel-border)',
      borderRadius: '10px', padding: '0.9rem 1.1rem', marginBottom: '0.75rem',
    }}>
      <div style={{ fontWeight: 700, color: 'var(--text-primary)', fontSize: '0.92rem', marginBottom: '0.6rem' }}>
        {date}
      </div>

      {added.length > 0 && (
        <div style={{ marginBottom: '0.5rem' }}>
          <span style={{ color: '#10b981', fontWeight: 600, fontSize: '0.78rem' }}>+ Added</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginTop: '0.3rem' }}>
            {added.map(s => (
              <span key={s.nseCode} title={s.securityName} style={{
                background: 'rgba(16,185,129,0.1)', border: '1px solid rgba(16,185,129,0.3)',
                color: '#10b981', borderRadius: '6px', padding: '2px 8px', fontSize: '0.76rem', fontWeight: 600,
              }}>
                {s.nseCode} {s.weight != null ? `(${fmtWt(s.weight)})` : ''}
              </span>
            ))}
          </div>
        </div>
      )}

      {removed.length > 0 && (
        <div style={{ marginBottom: '0.5rem' }}>
          <span style={{ color: '#ef4444', fontWeight: 600, fontSize: '0.78rem' }}>− Removed</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginTop: '0.3rem' }}>
            {removed.map(s => (
              <span key={s.nseCode} title={s.securityName} style={{
                background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)',
                color: '#ef4444', borderRadius: '6px', padding: '2px 8px', fontSize: '0.76rem', fontWeight: 600,
              }}>
                {s.nseCode} {s.weight != null ? `(was ${fmtWt(s.weight)})` : ''}
              </span>
            ))}
          </div>
        </div>
      )}

      {reweighted.length > 0 && (
        <div>
          <span style={{ color: '#fbbf24', fontWeight: 600, fontSize: '0.78rem' }}>↕ Reweighted</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginTop: '0.3rem' }}>
            {reweighted.map(s => (
              <span key={s.nseCode} title={s.securityName} style={{
                background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.3)',
                color: 'var(--text-secondary)', borderRadius: '6px', padding: '2px 8px', fontSize: '0.76rem',
              }}>
                <strong style={{ color: 'var(--text-primary)' }}>{s.nseCode}</strong>{' '}
                {fmtWt(s.from)} → {fmtWt(s.to)}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function RebalanceSummaryPage({ basketKey, basketLabel }) {
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    setSummary(null);
    fetchRebalanceSummary(basketKey)
      .then(setSummary)
      .catch(() => setError('Failed to load rebalance history.'))
      .finally(() => setLoading(false));
  }, [basketKey]);

  const handleExportCsv = () => {
    const header = ['Date', 'Action', 'NSE Code', 'Security Name', 'Weight', 'From', 'To'];
    const lines = [];
    for (const entry of summary || []) {
      for (const s of entry.added || [])
        lines.push([entry.date, 'Added', s.nseCode, s.securityName, s.weight, '', '']);
      for (const s of entry.removed || [])
        lines.push([entry.date, 'Removed', s.nseCode, s.securityName, s.weight, '', '']);
      for (const s of entry.reweighted || [])
        lines.push([entry.date, 'Reweighted', s.nseCode, s.securityName, '', s.from, s.to]);
    }
    const csv = [header, ...lines].map(row => row.map(v => `"${v ?? ''}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${basketLabel.replace(/\s+/g, '_')}_RebalanceSummary.csv`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ padding: '0.5rem 0' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', marginBottom: '1rem' }}>
        <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Every rebalance recorded for <strong style={{ color: 'var(--text-primary)' }}>{basketLabel}</strong>,
          most recent first — what was added, removed, or reweighted on each date.
        </div>
        {summary?.length > 0 && (
          <button onClick={handleExportCsv} style={{
            padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--accent-blue)',
            background: 'var(--accent-blue)', color: '#fff', fontSize: '0.78rem', fontWeight: 600, cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0,
          }}>
            <i className="fa-solid fa-file-export" /> Export CSV
          </button>
        )}
      </div>

      {loading && <div style={{ color: 'var(--text-secondary)', padding: '2rem 0', textAlign: 'center' }}>Loading…</div>}
      {error && <div style={{ color: '#ef4444', padding: '1rem 0' }}>{error}</div>}
      {!loading && !error && summary?.length === 0 && (
        <div style={{ color: 'var(--text-secondary)', padding: '2rem 0', textAlign: 'center' }}>
          No rebalance history recorded for this basket yet.
        </div>
      )}
      {!loading && !error && summary?.map(entry => <DayCard key={entry.date} entry={entry} />)}
    </div>
  );
}
