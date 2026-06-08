import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { API_BASE as API } from '../config.js';

const clr = (pct) => {
  if (pct == null) return '#94a3b8';
  return pct >= 0 ? '#10b981' : '#ef4444';
};

const fmt = (n) => n == null ? '—' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 });
const fmtPct = (n) => n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(2) + '%';

function Section({ title, color, dot, children }) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, boxShadow: `0 0 6px ${color}`, flexShrink: 0 }} />
        <span style={{ fontSize: '0.75rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color }}>{title}</span>
      </div>
      {children}
    </div>
  );
}

function Table({ headers, rows }) {
  if (!rows.length) return null;
  return (
    <div style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid rgba(255,255,255,0.07)', marginBottom: '0.25rem' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
        <thead>
          <tr style={{ background: 'rgba(255,255,255,0.04)' }}>
            {headers.map(h => (
              <th key={h} style={{ padding: '0.45rem 0.85rem', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em', whiteSpace: 'nowrap', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: i < rows.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none' }}>
              {row.map((cell, j) => (
                <td key={j} style={{ padding: '0.5rem 0.85rem', whiteSpace: 'nowrap', ...cell.style }}>{cell.node || cell.text}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AlertCard({ alert, idx, total }) {
  const { basketLabel, rebalanceDate, fullExits, partialSells, newAdditions, weightIncreased } = alert;

  const exitRows = fullExits.map(s => [
    { text: s.nseCode, style: { fontWeight: 700, color: '#e2e8f0' } },
    { text: s.securityName, style: { color: '#94a3b8' } },
    { text: fmt(s.buyPrice), style: { color: '#94a3b8' } },
    { text: fmt(s.sellPrice), style: { color: clr(s.returnPct) } },
    { text: fmtPct(s.returnPct), style: { color: clr(s.returnPct), fontWeight: 700 } },
    { text: s.weight + '%', style: { color: '#94a3b8' } },
  ]);

  const partialRows = partialSells.map(s => [
    { text: s.nseCode, style: { fontWeight: 700, color: '#e2e8f0' } },
    { text: s.securityName, style: { color: '#94a3b8' } },
    { text: s.weight + '%', style: { color: '#f59e0b' } },
    { text: fmt(s.buyPrice), style: { color: '#94a3b8' } },
    { text: fmt(s.sellPrice), style: { color: clr(s.returnPct) } },
    { text: fmtPct(s.returnPct), style: { color: clr(s.returnPct), fontWeight: 700 } },
  ]);

  const addRows = newAdditions.map(s => [
    { text: s.nseCode, style: { fontWeight: 700, color: '#e2e8f0' } },
    { text: s.securityName, style: { color: '#94a3b8' } },
    { text: (s.weight || '—') + (s.weight ? '%' : ''), style: { color: '#10b981' } },
  ]);

  const increaseRows = weightIncreased.map(s => [
    { text: s.nseCode, style: { fontWeight: 700, color: '#e2e8f0' } },
    { text: s.securityName, style: { color: '#94a3b8' } },
    { node: <span style={{ color: '#f59e0b' }}>{s.oldWeight}% <span style={{ color: '#475569' }}>→</span> {s.newWeight}%</span> },
  ]);

  return (
    <div style={{
      background: 'rgba(15,23,42,0.9)',
      border: '1px solid rgba(99,102,241,0.2)',
      borderRadius: 14,
      padding: '1.5rem 1.75rem',
      marginBottom: '1rem',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.2rem' }}>
        <div>
          <h3 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: '#f1f5f9' }}>{basketLabel}</h3>
          <span style={{ fontSize: '0.82rem', color: '#64748b' }}>Rebalanced on {rebalanceDate}</span>
        </div>
        <span style={{ fontSize: '0.72rem', color: '#475569', background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6, padding: '0.2rem 0.6rem' }}>
          {idx + 1} / {total}
        </span>
      </div>

      {fullExits.length > 0 && (
        <Section title="Fully Exited" color="#ef4444">
          <Table
            headers={['Stock', 'Name', 'Buy Price', 'Sell Price', 'Return', 'Weight Sold']}
            rows={exitRows}
          />
        </Section>
      )}

      {partialSells.length > 0 && (
        <Section title="Weight Reduced (partial sell)" color="#f59e0b">
          <Table
            headers={['Stock', 'Name', 'Weight Sold', 'Buy Price', 'Sell Price', 'Return']}
            rows={partialRows}
          />
        </Section>
      )}

      {weightIncreased.length > 0 && (
        <Section title="Weight Increased (partial top-up)" color="#38bdf8">
          <Table
            headers={['Stock', 'Name', 'Weight Change']}
            rows={increaseRows}
          />
        </Section>
      )}

      {newAdditions.length > 0 && (
        <Section title="New Additions" color="#10b981">
          <Table
            headers={['Stock', 'Name', 'Weight']}
            rows={addRows}
          />
        </Section>
      )}
    </div>
  );
}

export default function RebalanceAlertPage() {
  const navigate = useNavigate();
  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    axios.get(`${API}/rebalance-alerts`)
      .then(r => { setAlerts(r.data || []); setLoading(false); })
      .catch(() => { setAlerts([]); setLoading(false); });
  }, []);

  const handleContinue = async () => {
    if (alerts.length > 0) {
      const items = alerts.map(a => ({ basketId: a.basketId, rebalanceDate: a.rebalanceDate }));
      await axios.post(`${API}/rebalance-alerts/ack`, { items }).catch(() => {});
    }
    navigate('/');
  };

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '60vh', color: '#64748b', fontSize: '0.9rem' }}>
      Checking for rebalance updates…
    </div>
  );

  if (!alerts.length) {
    navigate('/');
    return null;
  }

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: '2rem 1rem 4rem' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '1.75rem' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', marginBottom: '0.3rem' }}>
            <span style={{ fontSize: '1.5rem' }}>🔔</span>
            <h2 style={{ margin: 0, fontSize: '1.45rem', fontWeight: 700, color: '#f1f5f9' }}>
              Portfolio Rebalance Update
            </h2>
          </div>
          <p style={{ margin: 0, color: '#64748b', fontSize: '0.88rem' }}>
            {alerts.length} basket{alerts.length > 1 ? 's were' : ' was'} rebalanced since your last visit.
          </p>
        </div>
      </div>

      {/* Alert cards */}
      {alerts.map((a, i) => (
        <AlertCard key={`${a.basketId}-${a.rebalanceDate}`} alert={a} idx={i} total={alerts.length} />
      ))}

      {/* Footer note + Continue */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '1.5rem', padding: '1rem 1.25rem', background: 'rgba(255,255,255,0.02)', borderRadius: 10, border: '1px solid rgba(255,255,255,0.06)' }}>
        <span style={{ fontSize: '0.8rem', color: '#475569' }}>
          ℹ️ This alert shows once per rebalance event — you won't see it again after clicking Continue.
        </span>
        <div style={{ display: 'flex', gap: '0.75rem', flexShrink: 0 }}>
          <button
            onClick={() => navigate('/actual')}
            style={{ padding: '0.55rem 1.2rem', borderRadius: 8, border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: '#a5b4fc', fontSize: '0.88rem', fontWeight: 600, cursor: 'pointer' }}
          >
            View P&amp;L Statement
          </button>
          <button
            onClick={handleContinue}
            style={{ padding: '0.55rem 1.4rem', borderRadius: 8, border: 'none', background: '#6366f1', color: '#fff', fontSize: '0.88rem', fontWeight: 700, cursor: 'pointer' }}
          >
            Continue →
          </button>
        </div>
      </div>
    </div>
  );
}
