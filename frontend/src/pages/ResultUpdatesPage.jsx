import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { ArrowLeft, FileText, Bell, Upload, Trash2, Download, Plus, ChevronDown, ChevronRight, Search, X } from 'lucide-react';
import { getToken } from '../utils/auth';
import { API_BASE as API } from '../config.js';

const authHeaders = () => ({ Authorization: `Bearer ${getToken()}` });

// Tracking is PER COMPANY (by nseCode), not per basket -- a company held in
// several baskets at once (e.g. ACUTAAS in both Mid & Small Cap and Trends
// Triology) shares one result date / received-checked-sent-sentResults
// state / bullets / financial table, since it's the same real-world result
// regardless of which basket holds it. Each row's `baskets` map lists every
// basket it's tracked under, each with its own `currentlyHeld` flag, so it
// still shows (and is still sendable) under any basket that still holds it
// even after being removed from another.
//
// 2026-10-23 refinement: concall date, the separate snapshot-image upload,
// and manual Operational Performance/Outlook typing are all gone -- each
// company now gets ONE "upload or drag" document (parsed automatically into
// bullets + an optional financial table, see backend's
// result_update_doc_parser.py), and `sentResults` is a new, separate
// checkbox from received/checked/sent that actually selects a company into
// the merged PDF (replacing the old per-row "selected" checkbox).
export default function ResultUpdatesPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [reminders, setReminders] = useState([]);
  const [basketLabels, setBasketLabels] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(null); // composite "basket|nseCode" of the open detail editor
  const [generating, setGenerating] = useState(null); // basket key currently generating, or null
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedBasket, setSelectedBasket] = useState(''); // '' = All Baskets
  // Two separate tabs per the 2026-10-27 refinement: "Result Status" (track
  // dates/documents/checkboxes per basket) and "Generate Result" (its own
  // basket dropdown, just for producing the merged/consolidated PDF) -- kept
  // apart since generating was previously buried inside each basket's
  // tracking section, mixed in with everything else.
  const [activeTab, setActiveTab] = useState('status'); // 'status' | 'generate'
  const [generateBasket, setGenerateBasket] = useState('');

  const load = useCallback(() => {
    setLoading(true);
    axios.get(`${API}/admin/result-updates`, { headers: authHeaders() })
      .then(r => { setRows(r.data.rows || []); setReminders(r.data.reminders || []); setBasketLabels(r.data.basketLabels || {}); })
      .catch(err => setError(err.response?.data?.detail || 'Failed to load'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!generateBasket && Object.keys(basketLabels).length > 0) {
      setGenerateBasket(Object.keys(basketLabels).sort((a, b) => (basketLabels[a] || a).localeCompare(basketLabels[b] || b))[0]);
    }
  }, [basketLabels, generateBasket]);

  const matchesSearch = useCallback((name, code) => {
    const term = searchTerm.trim().toLowerCase();
    if (!term) return true;
    return (name || '').toLowerCase().includes(term) || (code || '').toLowerCase().includes(term);
  }, [searchTerm]);

  const filteredReminders = useMemo(() => {
    const basketLabel = selectedBasket ? basketLabels[selectedBasket] : null;
    return reminders.filter(rem =>
      matchesSearch(rem.securityName, rem.nseCode) &&
      (!basketLabel || rem.baskets.includes(basketLabel))
    );
  }, [reminders, matchesSearch, selectedBasket, basketLabels]);

  const byBasket = useMemo(() => {
    const groups = {};
    rows.forEach(r => {
      Object.keys(r.baskets || {}).forEach(basketKey => {
        (groups[basketKey] ||= []).push(r);
      });
    });
    Object.values(groups).forEach(list => list.sort((a, b) => a.securityName.localeCompare(b.securityName)));
    return groups;
  }, [rows]);

  const patchRow = async (nseCode, patch) => {
    try {
      const resp = await axios.post(`${API}/admin/result-updates`, { nseCode, ...patch }, { headers: authHeaders() });
      setRows(prev => {
        const exists = prev.some(r => r.nseCode === nseCode);
        return exists ? prev.map(r => r.nseCode === nseCode ? resp.data : r) : [...prev, resp.data];
      });
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to save');
    }
  };

  const removeRow = async (r) => {
    const heldIn = Object.values(r.baskets || {}).filter(b => b.currentlyHeld).map(b => b.label);
    const warning = heldIn.length > 1 ? ` This stops tracking it for ALL of: ${heldIn.join(', ')}.` : '';
    if (!window.confirm(`Stop tracking ${r.securityName} entirely?${warning}`)) return;
    try {
      await axios.delete(`${API}/admin/result-updates/${encodeURIComponent(r.nseCode)}`, { headers: authHeaders() });
      setRows(prev => prev.filter(x => x.nseCode !== r.nseCode));
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to remove');
    }
  };

  const [uploading, setUploading] = useState(null); // nseCode currently uploading, or null
  const uploadResultUpdateDocument = async (r, file) => {
    const form = new FormData();
    form.append('file', file);
    setUploading(r.nseCode);
    setError('');
    try {
      const resp = await axios.post(
        `${API}/admin/result-updates/${encodeURIComponent(r.nseCode)}/parse-document`,
        form, { headers: { ...authHeaders(), 'Content-Type': 'multipart/form-data' } }
      );
      setRows(prev => prev.map(x => x.nseCode === r.nseCode ? resp.data : x));
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to upload/parse document');
    } finally {
      setUploading(null);
    }
  };

  const selKey = (basketKey, nseCode) => `${basketKey}|${nseCode}`;

  const downloadPdf = (blobData, filenameBase) => {
    const url = window.URL.createObjectURL(new Blob([blobData], { type: 'application/pdf' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `${filenameBase}.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  };

  // Both modes now derive their company list server-side from sentResults
  // (+ received/checked for consolidated) within the basket -- no selection
  // payload to send from here anymore, see backend's generate_pdf.
  const generatePdf = async (basketKey) => {
    setGenerating(basketKey);
    setError('');
    try {
      const resp = await axios.post(`${API}/admin/result-updates/generate-pdf`, { basket: basketKey },
        { headers: authHeaders(), responseType: 'blob' });
      downloadPdf(resp.data, `${basketLabels[basketKey] || basketKey}_Result_Update`);
    } catch (err) {
      setError(await blobErrorMessage(err) || 'Failed to generate PDF.');
    } finally {
      setGenerating(null);
    }
  };

  const generateConsolidated = async (basketKey) => {
    setGenerating(basketKey);
    setError('');
    try {
      const resp = await axios.post(`${API}/admin/result-updates/generate-pdf`, { basket: basketKey, consolidated: true },
        { headers: authHeaders(), responseType: 'blob' });
      downloadPdf(resp.data, `${basketLabels[basketKey] || basketKey}_Consolidated_Result_Update`);
    } catch (err) {
      setError(await blobErrorMessage(err) || 'Failed to generate the consolidated PDF.');
    } finally {
      setGenerating(null);
    }
  };

  const inputStyle = { padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--panel-border)', background: 'var(--input-bg)', color: 'var(--text-main)', fontSize: '0.82rem', outline: 'none', fontFamily: 'inherit' };

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
            Result dates auto-fill from Result Calendar. Track each company and upload its result-update document under "Result Status", then check "Sent Results" and head to "Generate Result" to produce the merged/consolidated PDF for a basket.
          </p>
        </div>
      </div>

      {error && <div style={{ marginBottom: '16px', padding: '10px 14px', borderRadius: '8px', background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171', fontSize: '0.85rem' }}>{error}</div>}

      {/* ── Tabs ── */}
      <div style={{ display: 'flex', gap: '4px', marginBottom: '20px', borderBottom: '1px solid var(--panel-border)' }}>
        {[['status', 'Result Status'], ['generate', 'Generate Result']].map(([key, label]) => (
          <button key={key} onClick={() => setActiveTab(key)} style={{
            padding: '10px 18px', border: 'none', borderBottom: `2px solid ${activeTab === key ? 'var(--primary)' : 'transparent'}`,
            background: 'none', color: activeTab === key ? 'var(--primary)' : 'var(--text-muted)',
            fontSize: '0.88rem', fontWeight: 600, cursor: 'pointer',
          }}>
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'generate' ? (
        <GenerateResultTab
          basketLabels={basketLabels} rows={rows} loading={loading}
          generateBasket={generateBasket} setGenerateBasket={setGenerateBasket}
          generating={generating} onGenerate={generatePdf} onGenerateConsolidated={generateConsolidated}
        />
      ) : (
      <>
      {/* ── Basket picker + Search ── */}
      <div style={{ display: 'flex', gap: '10px', marginBottom: '20px', flexWrap: 'wrap' }}>
        <select
          value={selectedBasket} onChange={e => setSelectedBasket(e.target.value)}
          style={{ padding: '10px 14px', borderRadius: '8px', border: '1px solid var(--panel-border)', background: 'var(--input-bg)', color: 'var(--text-main)', fontSize: '0.85rem', outline: 'none', fontFamily: 'inherit', cursor: 'pointer', flex: '0 0 220px' }}
        >
          <option value="">All Baskets</option>
          {Object.keys(basketLabels).sort((a, b) => (basketLabels[a] || a).localeCompare(basketLabels[b] || b)).map(key => (
            <option key={key} value={key}>{basketLabels[key] || key}</option>
          ))}
        </select>
        <div style={{ position: 'relative', flex: '1 1 260px' }}>
        <Search size={15} color="var(--text-muted)" style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)' }} />
        <input
          type="text" value={searchTerm} onChange={e => setSearchTerm(e.target.value)}
          placeholder="Search by company name or NSE code…"
          style={{ width: '100%', padding: '10px 36px', borderRadius: '8px', border: '1px solid var(--panel-border)', background: 'var(--input-bg)', color: 'var(--text-main)', fontSize: '0.85rem', outline: 'none', fontFamily: 'inherit', boxSizing: 'border-box' }}
        />
        {searchTerm && (
          <button onClick={() => setSearchTerm('')} style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', padding: 0 }}>
            <X size={15} />
          </button>
        )}
        </div>
      </div>

      {/* ── Reminders ── */}
      <div className="glass-panel" style={{ padding: 0, overflow: 'hidden', marginBottom: '20px', border: '1px solid rgba(251,191,36,0.25)' }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--panel-border)', display: 'flex', alignItems: 'center', gap: '10px', background: 'rgba(251,191,36,0.06)' }}>
          <Bell size={15} color="#fbbf24" />
          <span style={{ fontWeight: 700, color: '#fbbf24', fontSize: '0.88rem' }}>Reminders</span>
          <span style={{ fontSize: '0.72rem', background: 'rgba(251,191,36,0.2)', color: '#fbbf24', borderRadius: '10px', padding: '2px 8px', fontWeight: 700 }}>
            {(searchTerm || selectedBasket) ? `${filteredReminders.length} of ${reminders.length}` : reminders.length}
          </span>
        </div>
        {!loading && filteredReminders.length > 0 && (
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(160px, 1.4fr) minmax(140px, 1fr) minmax(160px, 1.6fr)', gap: '10px', padding: '8px 20px', fontSize: '0.68rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.03em', borderBottom: '1px solid var(--panel-border)' }}>
            <span>Company</span><span>Basket(s)</span><span>Status</span>
          </div>
        )}
        {loading ? (
          <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading…</div>
        ) : filteredReminders.length === 0 ? (
          <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>{searchTerm ? 'No reminders match your search.' : 'Nothing pending.'}</div>
        ) : filteredReminders.map((rem, i) => {
          // "new" (no result date yet -- Result Calendar has nothing for it,
          // fix it directly in the per-basket table below) vs "overdue_result"
          // (date's passed, still not Received). No "Add dates" flow anymore
          // -- the row already exists as a real tracked row the moment a
          // basket starts holding it, see backend's _sync_and_compute_reminders.
          const status = rem.type === 'new'
            ? { text: 'No result date yet -- Result Calendar has none; enter one in the table below', tone: 'var(--text-muted)' }
            : { text: `Results announced ${rem.resultDate} — not yet Received`, tone: '#fbbf24' };
          return (
            <div key={rem.nseCode} style={{ borderBottom: i < filteredReminders.length - 1 ? '1px solid var(--panel-border)' : 'none' }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(160px, 1.4fr) minmax(140px, 1fr) minmax(160px, 1.6fr)', gap: '10px', alignItems: 'center', padding: '10px 20px' }}>
                <div>
                  <div style={{ fontSize: '0.84rem', color: 'var(--text-main)', fontWeight: 600 }}>{rem.securityName}</div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{rem.nseCode}</div>
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                  {/* A specific basket is picked -- show only that one, not
                      every basket this company happens to also be held in. */}
                  {(selectedBasket ? [basketLabels[selectedBasket]] : rem.baskets).map(b => <BasketChip key={b} label={b} />)}
                </div>
                <div style={{ fontSize: '0.78rem', color: status.tone }}>{status.text}</div>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Per-basket tracking -- restricted to the picked basket, if any ── */}
      {Object.keys(basketLabels).sort((a, b) => (basketLabels[a] || a).localeCompare(basketLabels[b] || b))
        .filter(basketKey => !selectedBasket || basketKey === selectedBasket)
        .map(basketKey => {
        const basketRows = byBasket[basketKey] || [];
        // When no specific basket is picked, skip empty ones entirely so the
        // "All Baskets" view isn't cluttered with sections that have nothing
        // tracked yet. Once the admin explicitly picks a basket from the
        // dropdown though, show it regardless -- an empty table is still a
        // meaningful answer to "what does this basket currently look like".
        if (!basketRows.length && !selectedBasket) return null;
        // A company removed from THIS basket shouldn't be fetched/shown in
        // its list at all -- per the user's own spec, "if an existing stock
        // has been completely removed... we won't be sending the result
        // update for the same." It still exists globally (other baskets
        // still holding it, or its own history) -- just not surfaced here.
        const heldRows = basketRows.filter(r => r.baskets[basketKey]?.currentlyHeld);
        const visibleRows = heldRows.filter(r => matchesSearch(r.securityName, r.nseCode));
        if (searchTerm && heldRows.length > 0 && visibleRows.length === 0) return null;
        // sentResults is what actually selects a company into a PDF now (not
        // a separate "selected" checkbox) -- the buttons themselves only
        // appear once at least one held company has it checked, per the
        // 2026-10-23 refinement ("the button... shall get popped up only
        // when I select the companies 'sent results' box only").
        return (
          <div key={basketKey} className="glass-panel" style={{ padding: 0, overflow: 'hidden', marginBottom: '20px' }}>
            <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--panel-border)', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              <span style={{ fontWeight: 700, color: 'var(--text-main)', fontSize: '0.92rem' }}>{basketLabels[basketKey] || basketKey}</span>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', background: 'var(--input-bg)', border: '1px solid var(--panel-border)', borderRadius: '10px', padding: '2px 8px' }}>
                {heldRows.length} tracked
              </span>
            </div>
            {visibleRows.length > 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: '78px minmax(160px, 1fr) 128px 86px 86px 70px 90px 32px', gap: '8px', alignItems: 'center', padding: '8px 20px', fontSize: '0.68rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.03em', borderBottom: '1px solid var(--panel-border)' }}>
                <span>Details</span><span>Company</span><span>Result Date</span><span>Received</span><span>Checked</span><span>Sent</span><span>Sent Results</span><span></span>
              </div>
            )}
            {visibleRows.length === 0 ? (
              <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                {searchTerm ? 'No companies in this basket match your search.' : 'No companies tracked yet for this basket -- check the Reminders above.'}
              </div>
            ) : visibleRows.map((r, i) => (
              <CompanyRow
                key={r.nseCode} r={r} i={i} total={visibleRows.length} basketKey={basketKey}
                hideOtherBaskets={!!selectedBasket}
                expanded={expanded === selKey(basketKey, r.nseCode)} onToggleExpand={() => setExpanded(expanded === selKey(basketKey, r.nseCode) ? null : selKey(basketKey, r.nseCode))}
                onPatch={(patch) => patchRow(r.nseCode, patch)}
                onUploadDocument={(file) => uploadResultUpdateDocument(r, file)}
                uploading={uploading === r.nseCode}
                onRemove={() => removeRow(r)}
                inputStyle={inputStyle}
              />
            ))}
          </div>
        );
      })}
      </>
      )}
    </div>
  );
}

function GenerateResultTab({ basketLabels, rows, loading, generateBasket, setGenerateBasket, generating, onGenerate, onGenerateConsolidated }) {
  const heldRows = rows.filter(r => r.baskets[generateBasket]?.currentlyHeld);
  const sentResultsCount = heldRows.filter(r => r.sentResults).length;
  const consolidatedCount = heldRows.filter(r => r.sentResults && r.received && r.checked).length;
  const isGenerating = generating === generateBasket;

  return (
    <div className="glass-panel" style={{ padding: '24px' }}>
      <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '8px' }}>Pick a basket</div>
      <select
        value={generateBasket} onChange={e => setGenerateBasket(e.target.value)}
        style={{ padding: '10px 14px', borderRadius: '8px', border: '1px solid var(--panel-border)', background: 'var(--input-bg)', color: 'var(--text-main)', fontSize: '0.88rem', outline: 'none', fontFamily: 'inherit', cursor: 'pointer', width: '280px', marginBottom: '20px' }}
      >
        {Object.keys(basketLabels).sort((a, b) => (basketLabels[a] || a).localeCompare(basketLabels[b] || b)).map(key => (
          <option key={key} value={key}>{basketLabels[key] || key}</option>
        ))}
      </select>

      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading…</div>
      ) : !generateBasket ? (
        <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>No baskets found.</div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' }}>
            <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
              <b style={{ color: 'var(--text-main)' }}>{heldRows.length}</b> tracked &middot; <b style={{ color: 'var(--text-main)' }}>{sentResultsCount}</b> sent results &middot; <b style={{ color: 'var(--text-main)' }}>{consolidatedCount}</b> ready for consolidated
            </div>
          </div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            {sentResultsCount > 0 ? (
              <button
                onClick={() => onGenerate(generateBasket)}
                disabled={isGenerating}
                style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '10px 20px', borderRadius: '8px', border: 'none', background: 'var(--primary)', color: '#fff', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}
              >
                <Download size={15} /> {isGenerating ? 'Generating…' : `Generate Merged PDF (${sentResultsCount})`}
              </button>
            ) : (
              <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>No company in this basket has "Sent Results" checked yet -- go to Result Status to mark some.</div>
            )}
            {consolidatedCount > 0 && (
              <button
                onClick={() => onGenerateConsolidated(generateBasket)}
                disabled={isGenerating}
                title="Generate the consolidated update for every held company that's received, checked, and marked sent results"
                style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '10px 20px', borderRadius: '8px', border: '1px solid var(--positive)', background: 'rgba(16,185,129,0.12)', color: 'var(--positive)', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}
              >
                <Download size={15} /> {isGenerating ? 'Generating…' : `Generate Consolidated PDF (${consolidatedCount})`}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

async function blobErrorMessage(err) {
  // axios with responseType: 'blob' gives the error body as a Blob too --
  // has to be read back out as text before the backend's real `detail`
  // message (e.g. which specific companies are still unreceived) can be shown.
  const data = err.response?.data;
  if (data instanceof Blob) {
    try {
      const text = await data.text();
      const parsed = JSON.parse(text);
      return parsed.detail;
    } catch { return null; }
  }
  return err.response?.data?.detail || null;
}

// Redesigned 2026-10-27 as a single bordered box with one compact row per
// point (heading + text side by side) instead of stacked heading-input +
// full-width-textarea cards -- the old layout "extend[ed] the list
// unnecessarily" per direct feedback; this is meant to read like the table
// view the final PDF itself now renders (see backend's
// _bullets_box_flowable), not a sprawling form.
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
      {items.length > 0 && (
        <div style={{ border: '1px solid var(--panel-border)', borderRadius: '8px', overflow: 'hidden', marginBottom: '8px' }}>
          {items.map((b, idx) => (
            <div key={idx} style={{
              display: 'grid', gridTemplateColumns: '200px 1fr 28px', gap: '8px', alignItems: 'start',
              padding: '8px 10px', background: idx % 2 === 1 ? 'var(--input-bg)' : 'transparent',
              borderTop: idx > 0 ? '1px solid var(--panel-border)' : 'none',
            }}>
              <input placeholder="Bold lead-in" value={b.heading}
                onChange={e => update(idx, 'heading', e.target.value)}
                style={{ padding: '5px 8px', borderRadius: '5px', border: '1px solid var(--panel-border)', background: 'var(--card-bg, transparent)', color: 'var(--text-main)', fontSize: '0.76rem', fontWeight: 600, fontFamily: 'inherit' }} />
              <textarea placeholder="Description text" value={b.text} rows={1}
                onChange={e => update(idx, 'text', e.target.value)}
                style={{ padding: '5px 8px', borderRadius: '5px', border: '1px solid var(--panel-border)', background: 'var(--card-bg, transparent)', color: 'var(--text-main)', fontSize: '0.76rem', fontFamily: 'inherit', resize: 'vertical', minHeight: '28px' }} />
              <button onClick={() => remove(idx)} style={{ padding: '5px 6px', borderRadius: '5px', border: '1px solid rgba(239,68,68,0.28)', background: 'rgba(239,68,68,0.08)', color: '#f87171', cursor: 'pointer' }}>
                <Trash2 size={11} />
              </button>
            </div>
          ))}
        </div>
      )}
      <button onClick={add} style={{ display: 'flex', alignItems: 'center', gap: '4px', padding: '4px 10px', borderRadius: '6px', border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: 'var(--primary)', fontSize: '0.74rem', cursor: 'pointer' }}>
        <Plus size={11} /> Add point
      </button>
    </div>
  );
}

function BasketChip({ label, muted }) {
  return (
    <span style={{
      fontSize: '0.68rem', fontWeight: 600, padding: '2px 8px', borderRadius: '999px', whiteSpace: 'nowrap',
      color: muted ? 'var(--text-muted)' : 'var(--primary)',
      background: muted ? 'var(--input-bg)' : 'rgba(99,102,241,0.12)',
      border: `1px solid ${muted ? 'var(--panel-border)' : 'rgba(99,102,241,0.3)'}`,
    }}>
      {label}
    </span>
  );
}

// Every /api/* route requires a Bearer token on the request itself -- a
// plain <a href> or <img src> sends no such header and just gets a 401 JSON
// body instead of the real file. Fetches the file as a blob with the auth
// header attached, then hands the caller an object URL to use normally.
function useAuthedBlobUrl(url) {
  const [blobUrl, setBlobUrl] = useState(null);
  useEffect(() => {
    if (!url) { setBlobUrl(null); return; }
    let revoke = null;
    axios.get(url, { headers: authHeaders(), responseType: 'blob' })
      .then(resp => {
        const obj = window.URL.createObjectURL(resp.data);
        revoke = obj;
        setBlobUrl(obj);
      })
      .catch(() => setBlobUrl(null));
    return () => { if (revoke) window.URL.revokeObjectURL(revoke); };
  }, [url]);
  return blobUrl;
}

function AuthedDocumentLink({ url, children }) {
  const blobUrl = useAuthedBlobUrl(url);
  if (!blobUrl) return <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>{children}</span>;
  return <a href={blobUrl} target="_blank" rel="noreferrer" style={{ display: 'block', fontSize: '0.78rem', color: 'var(--primary)', marginBottom: '8px' }}>{children}</a>;
}

function AuthedImage({ url, alt }) {
  const blobUrl = useAuthedBlobUrl(url);
  if (!blobUrl) return null;
  return <img src={blobUrl} alt={alt} style={{ maxWidth: '100%', borderRadius: '6px', border: '1px solid var(--panel-border)', marginBottom: '8px', display: 'block' }} />;
}

function CompanyRow({ r, i, total, basketKey, hideOtherBaskets, expanded, onToggleExpand, onPatch, onUploadDocument, uploading, onRemove, inputStyle }) {
  const [opPerf, setOpPerf] = useState(r.operationalPerformance || []);
  const [outlook, setOutlook] = useState(r.outlook || []);
  const [dragOver, setDragOver] = useState(false);
  useEffect(() => { setOpPerf(r.operationalPerformance || []); setOutlook(r.outlook || []); }, [r.operationalPerformance, r.outlook]);

  const thisBasketHeld = r.baskets[basketKey]?.currentlyHeld;
  // A specific basket is picked from the dropdown -- don't surface other
  // baskets this company happens to also be held in, even when common.
  const otherBaskets = hideOtherBaskets ? [] : Object.entries(r.baskets || {}).filter(([key]) => key !== basketKey);

  const checkbox = (field) => (
    <input type="checkbox" checked={!!r[field]} onChange={e => onPatch({ [field]: e.target.checked })} style={{ cursor: 'pointer' }} />
  );

  const handleFile = (file) => {
    if (!file) return;
    const ext = file.name.toLowerCase().split('.').pop();
    if (ext !== 'doc' && ext !== 'docx') return;
    onUploadDocument(file);
  };

  return (
    <div style={{ borderBottom: i < total - 1 ? '1px solid var(--panel-border)' : 'none', opacity: thisBasketHeld ? 1 : 0.55 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '78px minmax(160px, 1fr) 128px 86px 86px 70px 90px 32px', gap: '8px', alignItems: 'center', padding: '10px 20px' }}>
        <button onClick={onToggleExpand} title="Show the uploaded document, parsed bullets, and financial table"
          style={{ display: 'flex', alignItems: 'center', gap: '3px', padding: '4px 8px', borderRadius: '6px', border: '1px solid rgba(99,102,241,0.3)', background: 'rgba(99,102,241,0.1)', color: 'var(--primary)', cursor: 'pointer', fontSize: '0.7rem', fontWeight: 600, whiteSpace: 'nowrap' }}>
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Details
        </button>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px', flexWrap: 'wrap' }}>
            <span style={{ color: 'var(--text-main)', fontSize: '0.85rem', fontWeight: 600 }}>{r.securityName}</span>
            <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>({r.nseCode})</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '4px' }}>
            {!thisBasketHeld && <BasketChip label="no longer held here" muted />}
            {otherBaskets.map(([key, b]) => <BasketChip key={key} label={`also in: ${b.label}`} muted />)}
          </div>
        </div>
        <input type="date" value={r.resultDate || ''} onChange={e => onPatch({ resultDate: e.target.value })} title="Result date (shared across every basket holding this company) -- auto-filled from Result Calendar when available" style={inputStyle} />
        <div style={{ textAlign: 'center' }}>{checkbox('received')}</div>
        <div style={{ textAlign: 'center' }}>{checkbox('checked')}</div>
        <div style={{ textAlign: 'center' }}>{checkbox('sent')}</div>
        <div style={{ textAlign: 'center' }} title="Selects this company into the next Generate Merged/Consolidated PDF for this basket">{checkbox('sentResults')}</div>
        <button onClick={onRemove} title="Stop tracking this company entirely (all baskets)" style={{ padding: '5px 8px', borderRadius: '6px', border: '1px solid rgba(239,68,68,0.28)', background: 'rgba(239,68,68,0.08)', color: '#f87171', cursor: 'pointer' }}>
          <Trash2 size={12} />
        </button>
      </div>
      {expanded && (
        <div style={{ padding: '0 20px 18px 48px' }}>
          <div style={{ marginBottom: '14px' }}>
            <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '6px' }}>Result Update Document</div>
            {r.resultUpdateDocument && (
              <AuthedDocumentLink url={`${API}/admin/result-updates/document/${r.resultUpdateDocument}`}>
                {r.resultUpdateDocument}
              </AuthedDocumentLink>
            )}
            <label
              onDragOver={e => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]); }}
              style={{
                display: 'flex', alignItems: 'center', gap: '8px', padding: '14px 18px', borderRadius: '8px',
                border: `2px dashed ${dragOver ? 'var(--primary)' : 'var(--panel-border)'}`,
                background: dragOver ? 'rgba(99,102,241,0.08)' : 'var(--input-bg)',
                color: 'var(--text-muted)', fontSize: '0.78rem', cursor: 'pointer', maxWidth: '420px',
              }}
            >
              <Upload size={14} />
              {uploading ? 'Uploading & parsing…' : (r.resultUpdateDocument ? 'Replace -- drag or click (.doc/.docx)' : 'Upload or drag the result update (.doc/.docx)')}
              <input type="file" accept=".doc,.docx" style={{ display: 'none' }} disabled={uploading}
                onChange={e => handleFile(e.target.files[0])} />
            </label>
          </div>

          {r.financialTable?.headers?.length > 0 && (
            <div style={{ marginBottom: '14px', overflowX: 'auto' }}>
              <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '6px' }}>Financial Snapshot (parsed from the uploaded document)</div>
              <table style={{ borderCollapse: 'collapse', fontSize: '0.75rem', minWidth: '480px' }}>
                <thead>
                  <tr>
                    {r.financialTable.headers.map((h, idx) => (
                      <th key={idx} style={{ textAlign: 'left', padding: '5px 10px', background: 'var(--primary)', color: '#fff', whiteSpace: 'nowrap' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.financialTable.rows.map((row, ridx) => (
                    <tr key={ridx} style={{ background: row.bold ? 'rgba(16,185,129,0.18)' : 'var(--input-bg)', fontWeight: row.bold ? 700 : 400 }}>
                      {row.cells.map((c, cidx) => (
                        <td key={cidx} style={{ padding: '5px 10px', borderBottom: '1px solid var(--panel-border)', whiteSpace: 'nowrap' }}>{c}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {r.financialImages?.length > 0 && (
            <div style={{ marginBottom: '14px' }}>
              <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-main)', marginBottom: '6px' }}>Financial Snapshot (image embedded in the uploaded document)</div>
              {r.financialImages.map((img, idx) => (
                <AuthedImage key={idx} url={`${API}/admin/result-updates/image/${img}`} alt="Financial snapshot" />
              ))}
            </div>
          )}

          <BulletEditor label="Operational Performance (auto-parsed -- correct here if needed)" items={opPerf} onChange={(v) => { setOpPerf(v); onPatch({ operationalPerformance: v }); }} />
          <BulletEditor label="Outlook (auto-parsed -- correct here if needed)" items={outlook} onChange={(v) => { setOutlook(v); onPatch({ outlook: v }); }} />
        </div>
      )}
    </div>
  );
}
