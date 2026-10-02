import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { ArrowLeft, FileText, Bell, Upload, Trash2, Download, Plus, ChevronDown, ChevronRight } from 'lucide-react';
import { getToken } from '../utils/auth';
import { API_BASE as API } from '../config.js';

const authHeaders = () => ({ Authorization: `Bearer ${getToken()}` });

export default function ResultUpdatesPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [reminders, setReminders] = useState([]);
  const [basketLabels, setBasketLabels] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(null); // key of the row whose detail editor is open
  const [selected, setSelected] = useState({}); // {rowKey: true} for merge selection
  const [generating, setGenerating] = useState(false);
  const [addingFor, setAddingFor] = useState(null); // reminder being turned into a tracked row

  const load = useCallback(() => {
    setLoading(true);
    axios.get(`${API}/admin/result-updates`, { headers: authHeaders() })
      .then(r => { setRows(r.data.rows || []); setReminders(r.data.reminders || []); setBasketLabels(r.data.basketLabels || {}); })
      .catch(err => setError(err.response?.data?.detail || 'Failed to load'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const rowKey = (r) => `${r.basket}|${r.nseCode}`;

  const byBasket = useMemo(() => {
    const groups = {};
    rows.forEach(r => { (groups[r.basket] ||= []).push(r); });
    Object.values(groups).forEach(list => list.sort((a, b) => a.securityName.localeCompare(b.securityName)));
    return groups;
  }, [rows]);

  const patchRow = async (basket, nseCode, patch) => {
    try {
      const resp = await axios.post(`${API}/admin/result-updates`, { basket, nseCode, ...patch }, { headers: authHeaders() });
      setRows(prev => {
        const k = `${basket}|${nseCode}`;
        const exists = prev.some(r => rowKey(r) === k);
        return exists ? prev.map(r => rowKey(r) === k ? resp.data : r) : [...prev, resp.data];
      });
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to save');
    }
  };

  const removeRow = async (r) => {
    if (!window.confirm(`Stop tracking ${r.securityName}?`)) return;
    try {
      await axios.delete(`${API}/admin/result-updates/${encodeURIComponent(r.basket)}/${encodeURIComponent(r.nseCode)}`, { headers: authHeaders() });
      setRows(prev => prev.filter(x => rowKey(x) !== rowKey(r)));
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to remove');
    }
  };

  const uploadSnapshot = async (r, file) => {
    const form = new FormData();
    form.append('file', file);
    try {
      const resp = await axios.post(
        `${API}/admin/result-updates/${encodeURIComponent(r.basket)}/${encodeURIComponent(r.nseCode)}/snapshot`,
        form, { headers: { ...authHeaders(), 'Content-Type': 'multipart/form-data' } }
      );
      setRows(prev => prev.map(x => rowKey(x) === rowKey(r) ? { ...x, snapshotImage: resp.data.snapshotImage } : x));
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to upload image');
    }
  };

  const toggleSelect = (r) => {
    const k = rowKey(r);
    setSelected(prev => ({ ...prev, [k]: !prev[k] }));
  };

  const generatePdf = async (basket) => {
    const nseCodes = (byBasket[basket] || []).filter(r => selected[rowKey(r)]).map(r => r.nseCode);
    if (!nseCodes.length) { setError('Select at least one company to merge first.'); return; }
    setGenerating(true);
    setError('');
    try {
      const resp = await axios.post(`${API}/admin/result-updates/generate-pdf`, { basket, nseCodes },
        { headers: authHeaders(), responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([resp.data], { type: 'application/pdf' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `${basketLabels[basket] || basket}_Result_Update.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError('Failed to generate PDF -- make sure every selected company has at least the financial snapshot or bullet text entered.');
    } finally {
      setGenerating(false);
    }
  };

  const startTrackingFromReminder = (rem) => {
    setAddingFor(rem);
  };

  const confirmAddFromReminder = async (rem, resultDate, concallDate) => {
    await patchRow(rem.basket, rem.nseCode, { securityName: rem.securityName, resultDate: resultDate || null, concallDate: concallDate || null });
    setAddingFor(null);
    load();
  };

  const inputStyle = { padding: '6px 10px', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.05)', color: 'var(--text-main)', fontSize: '0.82rem', outline: 'none', fontFamily: 'inherit' };

  return (
    <div className="animate-slide-up" style={{ maxWidth: 980, margin: '0 auto', padding: '0 1rem 3rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '28px' }}>
        <button className="btn btn-secondary" onClick={() => navigate('/')} style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px' }}>
          <ArrowLeft size={16} /> Back
        </button>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <FileText size={20} color="var(--primary)" />
            <h2 className="text-gradient" style={{ margin: 0, fontSize: '1.5rem' }}>Result Update</h2>
          </div>
          <p style={{ color: 'var(--text-muted)', margin: '4px 0 0', fontSize: '0.85rem' }}>
            Track result/concall dates, received/checked/sent status, and merge quarterly result updates into a single PDF -- per basket.
          </p>
        </div>
      </div>

      {error && <div style={{ marginBottom: '16px', padding: '10px 14px', borderRadius: '8px', background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171', fontSize: '0.85rem' }}>{error}</div>}

      {/* ── Reminders ── */}
      <div className="glass-panel" style={{ padding: 0, overflow: 'hidden', marginBottom: '20px', border: '1px solid rgba(251,191,36,0.25)' }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.08)', display: 'flex', alignItems: 'center', gap: '10px', background: 'rgba(251,191,36,0.06)' }}>
          <Bell size={15} color="#fbbf24" />
          <span style={{ fontWeight: 700, color: '#fbbf24', fontSize: '0.88rem' }}>Reminders</span>
          <span style={{ fontSize: '0.72rem', background: 'rgba(251,191,36,0.2)', color: '#fbbf24', borderRadius: '10px', padding: '2px 8px', fontWeight: 700 }}>{reminders.length}</span>
        </div>
        {loading ? (
          <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading…</div>
        ) : reminders.length === 0 ? (
          <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>Nothing pending.</div>
        ) : reminders.map((rem, i) => {
          const k = `${rem.basket}|${rem.nseCode}`;
          const isAdding = addingFor && `${addingFor.basket}|${addingFor.nseCode}` === k;
          return (
            <div key={k} style={{ padding: '12px 20px', borderBottom: i < reminders.length - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px' }}>
                <div style={{ fontSize: '0.84rem', color: 'var(--text-main)' }}>{rem.message}</div>
                {rem.type === 'new' && !isAdding && (
                  <button onClick={() => startTrackingFromReminder(rem)} style={{ display: 'flex', alignItems: 'center', gap: '5px', padding: '5px 12px', borderRadius: '7px', border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: 'var(--primary)', fontSize: '0.78rem', cursor: 'pointer', whiteSpace: 'nowrap' }}>
                    <Plus size={12} /> Add dates
                  </button>
                )}
              </div>
              {isAdding && (
                <ReminderDateForm rem={rem} onConfirm={confirmAddFromReminder} onCancel={() => setAddingFor(null)} inputStyle={inputStyle} />
              )}
            </div>
          );
        })}
      </div>

      {/* ── Per-basket tracking ── */}
      {Object.keys(basketLabels).sort((a, b) => (basketLabels[a] || a).localeCompare(basketLabels[b] || b)).map(basketKey => {
        const basketRows = byBasket[basketKey] || [];
        if (!basketRows.length) return null;
        const selectedCount = basketRows.filter(r => selected[rowKey(r)]).length;
        const allReceived = basketRows.every(r => r.received);
        return (
          <div key={basketKey} className="glass-panel" style={{ padding: 0, overflow: 'hidden', marginBottom: '20px' }}>
            <div style={{ padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.08)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span style={{ fontWeight: 700, color: 'var(--text-main)', fontSize: '0.92rem' }}>{basketLabels[basketKey] || basketKey}</span>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '10px', padding: '2px 8px' }}>
                  {basketRows.length} tracked
                </span>
                {allReceived && (
                  <span title="Every tracked, still-held company has its result update received -- ready for the full consolidated update" style={{ fontSize: '0.72rem', color: 'var(--positive)', background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.3)', borderRadius: '10px', padding: '2px 8px', fontWeight: 600 }}>
                    All received
                  </span>
                )}
              </div>
              <button
                onClick={() => generatePdf(basketKey)}
                disabled={generating || selectedCount === 0}
                style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '7px 16px', borderRadius: '7px', border: 'none', background: selectedCount ? 'var(--primary)' : 'rgba(255,255,255,0.08)', color: selectedCount ? '#fff' : 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600, cursor: selectedCount ? 'pointer' : 'not-allowed' }}
              >
                <Download size={13} /> {generating ? 'Generating…' : `Generate Merged PDF (${selectedCount})`}
              </button>
            </div>
            {basketRows.map((r, i) => (
              <CompanyRow
                key={rowKey(r)} r={r} i={i} total={basketRows.length}
                selected={!!selected[rowKey(r)]} onToggleSelect={() => toggleSelect(r)}
                expanded={expanded === rowKey(r)} onToggleExpand={() => setExpanded(expanded === rowKey(r) ? null : rowKey(r))}
                onPatch={(patch) => patchRow(r.basket, r.nseCode, patch)}
                onUpload={(file) => uploadSnapshot(r, file)}
                onRemove={() => removeRow(r)}
                inputStyle={inputStyle}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function ReminderDateForm({ rem, onConfirm, onCancel, inputStyle }) {
  const [resultDate, setResultDate] = useState('');
  const [concallDate, setConcallDate] = useState('');
  return (
    <div style={{ marginTop: '10px', display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
      <label style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>Result date
        <input type="date" value={resultDate} onChange={e => setResultDate(e.target.value)} style={{ ...inputStyle, marginLeft: '6px' }} />
      </label>
      <label style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>Concall date
        <input type="date" value={concallDate} onChange={e => setConcallDate(e.target.value)} style={{ ...inputStyle, marginLeft: '6px' }} />
      </label>
      <button onClick={() => onConfirm(rem, resultDate, concallDate)} className="btn btn-primary" style={{ padding: '6px 14px', fontSize: '0.78rem' }}>Save</button>
      <button onClick={onCancel} className="btn btn-secondary" style={{ padding: '6px 14px', fontSize: '0.78rem' }}>Cancel</button>
    </div>
  );
}

function BulletEditor({ label, items, onChange }) {
  const update = (idx, field, value) => {
    const next = items.map((b, i) => i === idx ? { ...b, [field]: value } : b);
    onChange(next);
  };
  const add = () => onChange([...items, { heading: '', text: '' }]);
  const remove = (idx) => onChange(items.filter((_, i) => i !== idx));
  return (
    <div style={{ marginBottom: '12px' }}>
      <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '6px' }}>{label}</div>
      {items.map((b, idx) => (
        <div key={idx} style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'flex-start' }}>
          <input placeholder="Bold lead-in (e.g. Robust Revenue Growth)" value={b.heading}
            onChange={e => update(idx, 'heading', e.target.value)}
            style={{ flex: '0 0 220px', padding: '6px 10px', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.05)', color: 'var(--text-main)', fontSize: '0.78rem', fontFamily: 'inherit' }} />
          <textarea placeholder="Description text" value={b.text} rows={2}
            onChange={e => update(idx, 'text', e.target.value)}
            style={{ flex: 1, padding: '6px 10px', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.05)', color: 'var(--text-main)', fontSize: '0.78rem', fontFamily: 'inherit', resize: 'vertical' }} />
          <button onClick={() => remove(idx)} style={{ padding: '6px 8px', borderRadius: '6px', border: '1px solid rgba(239,68,68,0.28)', background: 'rgba(239,68,68,0.08)', color: '#f87171', cursor: 'pointer' }}>
            <Trash2 size={12} />
          </button>
        </div>
      ))}
      <button onClick={add} style={{ display: 'flex', alignItems: 'center', gap: '4px', padding: '4px 10px', borderRadius: '6px', border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: 'var(--primary)', fontSize: '0.74rem', cursor: 'pointer' }}>
        <Plus size={11} /> Add point
      </button>
    </div>
  );
}

function CompanyRow({ r, i, total, selected, onToggleSelect, expanded, onToggleExpand, onPatch, onUpload, onRemove, inputStyle }) {
  const [opPerf, setOpPerf] = useState(r.operationalPerformance || []);
  const [outlook, setOutlook] = useState(r.outlook || []);
  useEffect(() => { setOpPerf(r.operationalPerformance || []); setOutlook(r.outlook || []); }, [r.operationalPerformance, r.outlook]);

  const checkbox = (field, label) => (
    <label style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.76rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
      <input type="checkbox" checked={!!r[field]} onChange={e => onPatch({ [field]: e.target.checked })} />
      {label}
    </label>
  );

  return (
    <div style={{ borderBottom: i < total - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none', opacity: r.removedFromPortfolio ? 0.55 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 20px', flexWrap: 'wrap' }}>
        <input type="checkbox" checked={selected} onChange={onToggleSelect} title="Include in next merged PDF" />
        <button onClick={onToggleExpand} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', padding: 0 }}>
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <div style={{ minWidth: '160px', flex: '1 1 160px' }}>
          <span style={{ color: 'var(--text-main)', fontSize: '0.85rem' }}>{r.securityName}</span>
          <span style={{ color: 'var(--text-muted)', fontSize: '0.74rem', marginLeft: '6px' }}>({r.nseCode})</span>
          {r.removedFromPortfolio && <span style={{ marginLeft: '8px', fontSize: '0.68rem', color: '#f87171' }}>no longer held -- not sent</span>}
        </div>
        <input type="date" value={r.resultDate || ''} onChange={e => onPatch({ resultDate: e.target.value })} title="Result date" style={inputStyle} />
        <input type="date" value={r.concallDate || ''} onChange={e => onPatch({ concallDate: e.target.value })} title="Concall date" style={inputStyle} />
        {checkbox('received', 'Received')}
        {checkbox('checked', 'Checked')}
        {checkbox('sent', 'Sent')}
        <button onClick={onRemove} style={{ padding: '5px 8px', borderRadius: '6px', border: '1px solid rgba(239,68,68,0.28)', background: 'rgba(239,68,68,0.08)', color: '#f87171', cursor: 'pointer' }}>
          <Trash2 size={12} />
        </button>
      </div>
      {expanded && (
        <div style={{ padding: '0 20px 18px 48px' }}>
          <div style={{ marginBottom: '12px' }}>
            <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '6px' }}>Financial Snapshot Image</div>
            {r.snapshotImage && (
              <img src={`${API}/admin/result-updates/snapshot/${r.snapshotImage}`} alt="snapshot"
                style={{ maxWidth: '100%', maxHeight: '220px', border: '1px solid rgba(255,255,255,0.15)', borderRadius: '4px', marginBottom: '8px', display: 'block' }} />
            )}
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '6px 12px', borderRadius: '6px', border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: 'var(--primary)', fontSize: '0.78rem', cursor: 'pointer' }}>
              <Upload size={12} /> {r.snapshotImage ? 'Replace image' : 'Upload image'}
              <input type="file" accept="image/png,image/jpeg" style={{ display: 'none' }} onChange={e => e.target.files[0] && onUpload(e.target.files[0])} />
            </label>
          </div>
          <BulletEditor label="Operational Performance" items={opPerf} onChange={(v) => { setOpPerf(v); onPatch({ operationalPerformance: v }); }} />
          <BulletEditor label="Outlook" items={outlook} onChange={(v) => { setOutlook(v); onPatch({ outlook: v }); }} />
        </div>
      )}
    </div>
  );
}
