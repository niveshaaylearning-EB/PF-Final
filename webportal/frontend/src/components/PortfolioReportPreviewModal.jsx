// Shows exactly what a fetched smallcase portfolio report WOULD change,
// before anything is written -- the admin must explicitly click Confirm.
// Mirrors the same preview-then-confirm pattern RebalanceUploadModal already
// uses for the Excel-upload path.
function Row({ label, weight, delta, tone }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.3rem 0', fontSize: '0.85rem' }}>
      <span>{label}</span>
      <span style={{ fontWeight: 700, color: tone }}>
        {weight != null ? `${weight}%` : ''}{delta != null ? ` (${tone === '#ef4444' ? '−' : '+'}${delta}%)` : ''}
      </span>
    </div>
  );
}

function Section({ title, tone, items, render }) {
  if (!items || items.length === 0) return null;
  return (
    <div style={{ marginBottom: '0.9rem' }}>
      <div style={{ fontSize: '0.72rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: tone, marginBottom: '0.2rem' }}>
        {title} ({items.length})
      </div>
      {items.map((item, i) => <div key={i}>{render(item)}</div>)}
    </div>
  );
}

export default function PortfolioReportPreviewModal({ preview, onConfirm, onCancel, confirming }) {
  const handleOverlayClick = (e) => { if (e.target === e.currentTarget) onCancel(); };

  return (
    <div className="modal-overlay" onClick={handleOverlayClick}>
      <div className="modal-box" style={{ maxWidth: '520px', textAlign: 'left' }}>
        <div className="modal-title" style={{ marginBottom: '0.2rem' }}>
          {preview.basketLabel} &mdash; smallcase report, {preview.date}
        </div>
        <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginBottom: '0.9rem' }}>
          Nothing has been changed yet. Review below, then Confirm to apply.
        </div>

        <Section title="New Additions" tone="#34d399" items={preview.added}
          render={it => <Row label={`${it.companyName} (${it.nseCode})`} weight={it.newWeight} tone="#34d399" />} />

        <Section title="Fully Removed" tone="#ef4444" items={preview.removed}
          render={it => <Row label={`${it.companyName} (${it.nseCode})`} weight={0} tone="#ef4444" />} />

        <Section title="Weight Increased" tone="#34d399" items={preview.increased}
          render={it => <Row label={`${it.companyName} (${it.nseCode})`} weight={it.newWeight} delta={it.delta} tone="#34d399" />} />

        <Section title="Weight Decreased" tone="#f87171" items={preview.decreased}
          render={it => <Row label={`${it.companyName} (${it.nseCode})`} weight={it.newWeight} delta={it.delta} tone="#ef4444" />} />

        {preview.unmatched && preview.unmatched.length > 0 && (
          <div style={{
            marginTop: '0.5rem', padding: '0.6rem 0.85rem', borderRadius: '8px',
            background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.3)', fontSize: '0.8rem',
          }}>
            <strong style={{ color: '#fbbf24' }}>Could not match to a ticker</strong> (won't be applied &mdash; add manually afterward):
            <div style={{ marginTop: '0.2rem' }}>{preview.unmatched.join(', ')}</div>
          </div>
        )}

        {(preview.added.length + preview.removed.length + preview.increased.length + preview.decreased.length) === 0 && (
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>No changes could be matched to a ticker.</div>
        )}

        <div className="modal-actions" style={{ marginTop: '1rem' }}>
          <button className="btn-confirm-yes" onClick={onConfirm} disabled={confirming}>
            {confirming ? 'Applying…' : 'Confirm & Apply'}
          </button>
          <button className="btn-confirm-no" onClick={onCancel} disabled={confirming}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
