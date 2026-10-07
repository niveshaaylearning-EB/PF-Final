import { useState, useEffect } from 'react';
import axios from 'axios';
import { Mail, MessageCircle, Pencil } from 'lucide-react';
import { getToken } from '../utils/auth.js';
import { API_ROOT } from '../config.js';

// Email is shown read-only, deliberately -- it's the account identity and is
// used as the raw key across many other tables (audit log, login history,
// simulator holdings, access requests, etc. all reference the email string
// directly, not a stable user ID), so changing it needs a careful cross-table
// migration, not a simple field edit. WhatsApp number is fully self-service:
// the same request/verify-OTP flow as the first-login opt-in modal
// (WhatsAppOptInModal.jsx), reused here for both adding AND changing it.
export default function ProfilePage() {
  const [me, setMe] = useState(null);
  const [editing, setEditing] = useState(false);
  const [step, setStep] = useState('phone'); // 'phone' | 'otp'
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  const authHeaders = { headers: { Authorization: `Bearer ${getToken()}` } };

  const loadMe = () => {
    axios.get(`${API_ROOT}/auth/me`, authHeaders)
      .then(res => setMe(res.data))
      .catch(() => setError('Could not load profile.'));
  };

  useEffect(() => { loadMe(); }, []);

  const startEdit = () => {
    setEditing(true);
    setStep('phone');
    setPhone(me?.whatsappPhone || '+91');
    setCode('');
    setError(''); setInfo('');
  };

  const cancelEdit = () => {
    setEditing(false);
    setError(''); setInfo('');
  };

  const sendOtp = async () => {
    setError(''); setInfo('');
    if (!phone.startsWith('+') || phone.length < 9) {
      setError('Enter a valid number in international format, e.g. +919537407484.');
      return;
    }
    setLoading(true);
    try {
      await axios.post(`${API_ROOT}/auth/profile/request-phone-otp`, { phone }, authHeaders);
      setStep('otp');
      setInfo(`Code sent via WhatsApp to ${phone}.`);
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to send code.');
    } finally {
      setLoading(false);
    }
  };

  const verifyOtp = async () => {
    setError('');
    setLoading(true);
    try {
      await axios.post(`${API_ROOT}/auth/profile/verify-phone-otp`, { phone, code }, authHeaders);
      setEditing(false);
      loadMe();
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid or expired code.');
    } finally {
      setLoading(false);
    }
  };

  if (!me) return <div style={{ textAlign: 'center', marginTop: '3rem' }}>Loading…</div>;

  return (
    <div style={{ maxWidth: '480px', margin: '2rem auto', padding: '0 16px' }}>
      <h2 className="text-gradient" style={{ marginBottom: '1.5rem' }}>Your Profile</h2>

      <div style={{
        background: 'var(--select-bg)', border: '1px solid var(--panel-border)',
        borderRadius: '14px', padding: '20px',
      }}>
        {/* Email -- read-only */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px' }}>
          <Mail size={18} color="var(--text-muted)" />
          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>Email (login identity -- not editable)</div>
            <div style={{ fontSize: '0.92rem', color: 'var(--text-main)' }}>{me.email}</div>
          </div>
        </div>

        {/* WhatsApp number -- editable, OTP-verified */}
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <MessageCircle size={18} color="var(--text-muted)" />
            <div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>WhatsApp number (for login OTP backup)</div>
              <div style={{ fontSize: '0.92rem', color: 'var(--text-main)' }}>{me.whatsappPhone || 'Not added yet'}</div>
            </div>
          </div>
          {!editing && (
            <button className="btn btn-secondary" onClick={startEdit} style={{ fontSize: '0.78rem', padding: '6px 10px', display: 'flex', alignItems: 'center', gap: '5px' }}>
              <Pencil size={13} /> {me.whatsappPhone ? 'Change' : 'Add'}
            </button>
          )}
        </div>

        {editing && (
          <div style={{ marginTop: '16px', paddingTop: '16px', borderTop: '1px solid var(--panel-border)' }}>
            {step === 'phone' ? (
              <>
                <input
                  type="tel" value={phone} onChange={e => setPhone(e.target.value)}
                  placeholder="+919537407484" disabled={loading}
                  style={{ width: '100%', marginBottom: '10px' }}
                />
                {error && <div style={{ color: '#f87171', fontSize: '0.78rem', marginBottom: '10px' }}>{error}</div>}
                <div style={{ display: 'flex', gap: '10px' }}>
                  <button className="btn btn-secondary" onClick={cancelEdit} disabled={loading} style={{ flex: 1 }}>Cancel</button>
                  <button className="btn btn-primary" onClick={sendOtp} disabled={loading} style={{ flex: 1 }}>
                    {loading ? 'Sending…' : 'Send Code'}
                  </button>
                </div>
              </>
            ) : (
              <>
                {info && <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem', marginBottom: '10px' }}>{info}</div>}
                <input
                  type="text" inputMode="numeric" value={code} onChange={e => setCode(e.target.value)}
                  placeholder="6-digit code" disabled={loading}
                  style={{ width: '100%', marginBottom: '10px' }}
                />
                {error && <div style={{ color: '#f87171', fontSize: '0.78rem', marginBottom: '10px' }}>{error}</div>}
                <div style={{ display: 'flex', gap: '10px' }}>
                  <button className="btn btn-secondary" onClick={() => setStep('phone')} disabled={loading} style={{ flex: 1 }}>Back</button>
                  <button className="btn btn-primary" onClick={verifyOtp} disabled={loading || !code.trim()} style={{ flex: 1 }}>
                    {loading ? 'Verifying…' : 'Verify & Save'}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
