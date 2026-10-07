import { useState, useEffect } from 'react';
import axios from 'axios';
import { X, MessageCircle } from 'lucide-react';
import { getToken, isLoggedIn } from '../utils/auth.js';
import { API_ROOT } from '../config.js';

// Shown once per browser session, right after login, to any user who has no
// WhatsApp number on file yet -- lets them opt in to receiving their login
// OTP via WhatsApp as a backup to email, same code on both channels
// (backend/auth.py's send_whatsapp_otp alongside the existing send_email_otp).
// Per the user's own spec (2026-10-07): "once they have entered their number
// then the pop up shouldn't show up" -- the condition is literally "do we
// have a number on file", re-checked via /auth/me on every fresh load, so it
// naturally stops once saved. Skipping only suppresses it for this browser
// session (sessionStorage), not permanently, so it isn't lost track of if the
// user genuinely forgot rather than deliberately declined forever.
const DISMISS_KEY = 'nia_wa_optin_dismissed';

export default function WhatsAppOptInModal() {
  const [visible, setVisible] = useState(false);
  const [step, setStep] = useState('phone'); // 'phone' | 'otp'
  const [phone, setPhone] = useState('+91');
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  useEffect(() => {
    if (!isLoggedIn()) return;
    if (sessionStorage.getItem(DISMISS_KEY)) return;
    axios.get(`${API_ROOT}/auth/me`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then(res => {
        if (!res.data.whatsappPhone) setVisible(true);
      })
      .catch(() => {});
  }, []);

  if (!visible) return null;

  const dismiss = () => {
    sessionStorage.setItem(DISMISS_KEY, '1');
    setVisible(false);
  };

  const sendOtp = async () => {
    setError(''); setInfo('');
    if (!phone.startsWith('+') || phone.length < 9) {
      setError('Enter a valid number in international format, e.g. +919537407484.');
      return;
    }
    setLoading(true);
    try {
      await axios.post(`${API_ROOT}/auth/profile/request-phone-otp`, { phone },
        { headers: { Authorization: `Bearer ${getToken()}` } });
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
      await axios.post(`${API_ROOT}/auth/profile/verify-phone-otp`, { phone, code },
        { headers: { Authorization: `Bearer ${getToken()}` } });
      setVisible(false);
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid or expired code.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 999998,
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '16px',
    }}>
      <div style={{
        width: '380px', maxWidth: '100%', background: 'var(--select-bg)',
        border: '1px solid var(--panel-border)', borderRadius: '14px',
        padding: '22px', position: 'relative',
      }}>
        <button onClick={dismiss} style={{ position: 'absolute', top: '14px', right: '14px', background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex' }}>
          <X size={18} />
        </button>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
          <MessageCircle size={20} color="var(--primary)" />
          <h3 style={{ margin: 0, fontSize: '1.05rem', color: 'var(--text-main)' }}>Add your WhatsApp number</h3>
        </div>
        <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', lineHeight: 1.5, marginTop: 0 }}>
          Get your login code on WhatsApp too, as a backup in case email is ever delayed or missed. Optional -- you can skip this.
        </p>

        {step === 'phone' ? (
          <>
            <input
              type="tel" value={phone} onChange={e => setPhone(e.target.value)}
              placeholder="+919537407484" disabled={loading}
              style={{ width: '100%', marginBottom: '10px' }}
            />
            {error && <div style={{ color: '#f87171', fontSize: '0.78rem', marginBottom: '10px' }}>{error}</div>}
            <div style={{ display: 'flex', gap: '10px' }}>
              <button className="btn btn-secondary" onClick={dismiss} disabled={loading} style={{ flex: 1 }}>Skip for now</button>
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
    </div>
  );
}
