import { API_BASE, getAuthToken } from '../api/base.js';
import { useState, useRef, useEffect } from 'react';

async function postJson(path, body) {
  const token = getAuthToken();
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error(data?.detail || `Request failed (${resp.status})`);
  return data;
}

async function getJson(path) {
  const token = getAuthToken();
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error(data?.detail || `Request failed (${resp.status})`);
  return data;
}

export default function SmallcaseLoginModal({ onClose }) {
  // 'checking' | 'phone' | 'otp' | 'done' -- starts on 'checking' so we don't
  // blindly try the phone/OTP flow (and fail looking for a Login button that
  // won't be on the page) when a session from a previous login is still valid.
  const [step, setStep] = useState('checking');
  const [phone, setPhone] = useState('');
  const [otp, setOtp]     = useState('');
  const [busy, setBusy]   = useState(false);
  const [error, setError] = useState('');
  const [msg, setMsg]     = useState('');
  const [fetchResult, setFetchResult] = useState(null);
  const overlayRef = useRef(null);

  useEffect(() => {
    getJson('/admin/smallcase-login/status')
      .then(r => setStep(r.logged_in ? 'done' : 'phone'))
      .catch(() => setStep('phone'));
  }, []);

  const handleSendOtp = async () => {
    setError(''); setBusy(true);
    try {
      const r = await postJson('/admin/smallcase-login/start', { phone: phone.trim() });
      if (!r.ok) throw new Error(r.error || 'Failed to request OTP.');
      setStep('otp');
      setMsg('OTP sent — check your phone.');
    } catch (e) {
      setError(e.message);
    } finally { setBusy(false); }
  };

  const handleVerify = async () => {
    setError(''); setBusy(true);
    try {
      const r = await postJson('/admin/smallcase-login/verify', { otp: otp.trim() });
      if (!r.ok) throw new Error(r.error || 'Verification failed.');
      setStep('done');
      setMsg('Logged in to smallcase.');
    } catch (e) {
      setError(e.message);
    } finally { setBusy(false); }
  };

  const handleFetchNow = async () => {
    setError(''); setBusy(true); setFetchResult(null);
    try {
      const r = await postJson('/admin/smallcase-fetch-daily', {});
      setFetchResult(r.results || {});
    } catch (e) {
      setError(e.message);
    } finally { setBusy(false); }
  };

  return (
    <div
      ref={overlayRef}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'var(--modal-overlay-bg)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={e => { if (e.target === overlayRef.current) onClose(); }}
    >
      <div style={{
        background: 'var(--modal-bg)', border: '1px solid rgba(139,92,246,0.2)',
        borderRadius: '16px', padding: '2rem 2.25rem', width: 'min(420px, 92vw)',
        boxShadow: '0 40px 100px rgba(0,0,0,0.8), 0 0 0 1px rgba(139,92,246,0.08)',
      }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '1.25rem' }}>
          <h2 style={{ margin: 0, fontSize: '1.2rem', fontWeight: 700, color: 'var(--text-primary)' }}>
            smallcase Login
          </h2>
          <button
            onClick={onClose}
            style={{ background: 'var(--hover-bg)', border: '1px solid var(--border-color)', borderRadius: '8px', color: 'var(--text-secondary)', cursor: 'pointer', fontSize: '1.15rem', padding: '0.3rem 0.65rem', lineHeight: 1 }}
          >&times;</button>
        </div>

        {step === 'checking' && (
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: 0 }}>
            Checking smallcase login status…
          </p>
        )}

        {step === 'phone' && (
          <>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: 0 }}>
              Enter your smallcase-registered mobile number. We'll request an OTP the same way smallcase's own site does.
            </p>
            <input
              type="tel" placeholder="10-digit mobile number" value={phone}
              onChange={e => setPhone(e.target.value)}
              style={{ width: '100%', padding: '0.65rem 0.9rem', borderRadius: '8px', border: '1.5px solid var(--border-color)', background: 'var(--input-bg)', color: 'var(--text-primary)', fontSize: '1rem', outline: 'none', boxSizing: 'border-box', marginBottom: '1rem' }}
            />
            <button
              onClick={handleSendOtp}
              disabled={busy || !phone.trim()}
              style={{ width: '100%', padding: '0.68rem', borderRadius: '9px', fontSize: '0.95rem', fontWeight: 700, background: busy ? 'rgba(139,92,246,0.35)' : 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 50%, #a78bfa 100%)', border: 'none', color: '#fff', cursor: busy ? 'default' : 'pointer' }}
            >
              {busy ? 'Requesting…' : 'Get OTP'}
            </button>
          </>
        )}

        {step === 'otp' && (
          <>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: 0 }}>
              Enter the OTP sent to {phone}.
            </p>
            <input
              type="text" inputMode="numeric" placeholder="OTP" value={otp}
              onChange={e => setOtp(e.target.value)}
              style={{ width: '100%', padding: '0.65rem 0.9rem', borderRadius: '8px', border: '1.5px solid var(--border-color)', background: 'var(--input-bg)', color: 'var(--text-primary)', fontSize: '1rem', outline: 'none', boxSizing: 'border-box', marginBottom: '1rem', letterSpacing: '0.2em' }}
            />
            <button
              onClick={handleVerify}
              disabled={busy || !otp.trim()}
              style={{ width: '100%', padding: '0.68rem', borderRadius: '9px', fontSize: '0.95rem', fontWeight: 700, background: busy ? 'rgba(139,92,246,0.35)' : 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 50%, #a78bfa 100%)', border: 'none', color: '#fff', cursor: busy ? 'default' : 'pointer' }}
            >
              {busy ? 'Verifying…' : 'Verify'}
            </button>
          </>
        )}

        {step === 'done' && (
          <>
            <p style={{ fontSize: '0.9rem', color: '#6ee7b7', marginTop: 0 }}>✓ Logged in to smallcase.</p>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              You can now pull the latest daily values directly.
            </p>
            <button
              onClick={handleFetchNow}
              disabled={busy}
              style={{ width: '100%', padding: '0.68rem', borderRadius: '9px', fontSize: '0.95rem', fontWeight: 700, background: busy ? 'rgba(139,92,246,0.35)' : 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 50%, #a78bfa 100%)', border: 'none', color: '#fff', cursor: busy ? 'default' : 'pointer', marginBottom: '0.75rem' }}
            >
              {busy ? 'Fetching…' : 'Fetch Latest Daily Values'}
            </button>
            {fetchResult && (
              <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                {Object.entries(fetchResult).map(([basket, r]) => (
                  <div key={basket}>
                    {basket}: {r.ok ? `${(r.added_dates || []).length} new date(s)` : r.error}
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {error && (
          <div style={{ marginTop: '1rem', padding: '0.6rem 0.85rem', borderRadius: '8px', background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)', color: '#fca5a5', fontSize: '0.82rem' }}>
            ⚠ {error}
          </div>
        )}
        {msg && !error && (
          <div style={{ marginTop: '1rem', padding: '0.6rem 0.85rem', borderRadius: '8px', background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.2)', color: '#6ee7b7', fontSize: '0.82rem' }}>
            ✓ {msg}
          </div>
        )}
      </div>
    </div>
  );
}
