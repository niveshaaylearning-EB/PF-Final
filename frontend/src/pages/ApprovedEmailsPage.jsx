import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { ArrowLeft, Users, UserPlus, Trash2, User } from 'lucide-react';
import { API_BASE as API } from '../config.js';

export default function ApprovedEmailsPage() {
  const navigate = useNavigate();
  const [emails,   setEmails]   = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [newEmail, setNewEmail] = useState('');
  const [adding,   setAdding]   = useState(false);
  const [removing, setRemoving] = useState('');
  const [error,    setError]    = useState('');
  const [success,  setSuccess]  = useState('');

  const load = useCallback(() => {
    setLoading(true);
    axios.get(`${API}/allowed-emails`)
      .then(r => setEmails(r.data || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleAdd = async (e) => {
    e.preventDefault();
    setError(''); setSuccess('');
    const em = newEmail.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setError('Only @niveshaay.com emails allowed');
      return;
    }
    setAdding(true);
    try {
      await axios.post(`${API}/allowed-emails`, { email: em });
      setSuccess(`${em} has been approved`);
      setNewEmail('');
      load();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to add');
    } finally { setAdding(false); }
  };

  const handleRemove = async (email) => {
    setError(''); setSuccess('');
    setRemoving(email);
    try {
      await axios.delete(`${API}/allowed-emails/${encodeURIComponent(email)}`);
      setSuccess(`${email} removed`);
      load();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to remove');
    } finally { setRemoving(''); }
  };

  return (
    <div className="animate-slide-up" style={{ maxWidth: 640, margin: '0 auto', padding: '0 1rem 3rem' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '28px' }}>
        <button
          className="btn btn-secondary"
          onClick={() => navigate('/')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px' }}
        >
          <ArrowLeft size={16} /> Back
        </button>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <Users size={20} color="var(--primary)" />
            <h2 className="text-gradient" style={{ margin: 0, fontSize: '1.5rem' }}>Approved Login Emails</h2>
          </div>
          <p style={{ color: 'var(--text-muted)', margin: '4px 0 0', fontSize: '0.85rem' }}>
            Only @niveshaay.com addresses listed here can log in to the dashboard.
          </p>
        </div>
      </div>

      {/* Add new email */}
      <div className="glass-panel" style={{ padding: '20px 24px', marginBottom: '20px' }}>
        <h4 style={{ margin: '0 0 14px', color: 'var(--text-main)', fontSize: '0.9rem', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '7px' }}>
          <UserPlus size={15} color="var(--primary)" /> Add new user
        </h4>
        <form onSubmit={handleAdd} style={{ display: 'flex', gap: '10px' }}>
          <input
            type="email"
            value={newEmail}
            onChange={e => setNewEmail(e.target.value)}
            placeholder="name@niveshaay.com"
            style={{
              flex: 1, padding: '10px 14px',
              borderRadius: '8px', border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(255,255,255,0.05)', color: 'var(--text-main)',
              fontSize: '0.88rem', outline: 'none', fontFamily: 'inherit',
            }}
          />
          <button
            type="submit"
            disabled={adding || !newEmail.trim()}
            className="btn btn-primary"
            style={{ padding: '10px 20px', whiteSpace: 'nowrap' }}
          >
            {adding ? 'Adding…' : 'Approve'}
          </button>
        </form>

        {error   && <div style={{ marginTop: '10px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171', fontSize: '0.83rem' }}>{error}</div>}
        {success && <div style={{ marginTop: '10px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(16,185,129,0.1)', border: '1px solid rgba(16,185,129,0.3)', color: 'var(--positive)', fontSize: '0.83rem' }}>{success}</div>}
      </div>

      {/* Email list */}
      <div className="glass-panel" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.08)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontWeight: 600, color: 'var(--text-main)', fontSize: '0.88rem' }}>Approved users</span>
          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '10px', padding: '2px 8px' }}>
            {emails.length} total
          </span>
        </div>

        {loading ? (
          <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.88rem' }}>Loading…</div>
        ) : emails.length === 0 ? (
          <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.88rem' }}>No approved emails yet.</div>
        ) : (
          emails.map((em, i) => {
            const emailStr = em?.email || em;
            return (
              <div key={emailStr} style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '12px 20px',
                background: i % 2 === 0 ? 'rgba(255,255,255,0.02)' : 'transparent',
                borderBottom: i < emails.length - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <div style={{ width: 30, height: 30, borderRadius: '50%', background: 'rgba(99,102,241,0.15)', border: '1px solid rgba(99,102,241,0.25)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                    <User size={14} color="var(--primary)" />
                  </div>
                  <div>
                    <span style={{ color: 'var(--text-main)', fontSize: '0.88rem' }}>{emailStr}</span>
                    {em?.added_at && <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '1px' }}>Added {em.added_at.slice(0, 10)}</div>}
                  </div>
                </div>
                <button
                  onClick={() => handleRemove(emailStr)}
                  disabled={removing === emailStr}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '5px',
                    padding: '5px 12px', borderRadius: '7px',
                    border: '1px solid rgba(239,68,68,0.28)',
                    background: 'rgba(239,68,68,0.08)',
                    color: '#f87171', fontSize: '0.78rem', cursor: removing === emailStr ? 'not-allowed' : 'pointer',
                    opacity: removing === emailStr ? 0.5 : 1,
                  }}
                >
                  <Trash2 size={12} /> {removing === emailStr ? 'Removing…' : 'Remove'}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
