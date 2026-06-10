import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Mail, LogIn, RefreshCw, KeyRound } from 'lucide-react';
import { setToken } from '../utils/auth';
import { API_ROOT as API } from '../config.js';

export default function LoginPage() {
  const navigate = useNavigate();
  const [email,   setEmail]   = useState('');
  const [code,    setCode]    = useState('');
  const [step,    setStep]    = useState('email'); // 'email' | 'otp'
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState('');
  const [hint,    setHint]    = useState('');     // shown when email delivery fails

  async function handleRequestOtp(e) {
    e.preventDefault();
    setError('');
    setHint('');
    const em = email.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setError('Only @niveshaay.com email addresses are allowed.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/request-email-otp`, { email: em });
      // If SMTP failed the backend returns the code directly so login still works
      if (res.data.code) setHint(`Email delivery failed. Your code: ${res.data.code}`);
      setCode('');
      setStep('otp');
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to send OTP. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  async function handleVerifyOtp(e) {
    e.preventDefault();
    setError('');
    if (code.trim().length < 6) { setError('Enter the 6-digit code from your email.'); return; }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/verify-email-otp`, {
        email: email.toLowerCase().trim(),
        code:  code.trim(),
      });
      setToken(res.data.token);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid or expired code. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  const inputStyle = {
    width: '100%', padding: '12px 16px 12px 40px', fontSize: '1rem',
    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.15)',
    borderRadius: '10px', color: 'var(--text-main)', outline: 'none', boxSizing: 'border-box',
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1rem' }}>
      <div className="glass-panel animate-slide-up" style={{ width: '100%', maxWidth: '440px', padding: '2.5rem 2rem' }}>

        <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
          <div style={{
            width: '56px', height: '56px', borderRadius: '16px', margin: '0 auto 16px',
            background: 'linear-gradient(135deg, rgba(99,102,241,0.3), rgba(16,185,129,0.2))',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            border: '1px solid rgba(99,102,241,0.35)',
          }}>
            <LogIn color="var(--primary)" size={26} />
          </div>
          <h1 className="text-gradient" style={{ fontSize: '1.7rem', margin: 0 }}>NIA Performance Center</h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.88rem', marginTop: '6px' }}>
            {step === 'email' ? 'Sign in with your NIA email address' : `Code sent to ${email}`}
          </p>
        </div>

        {error && (
          <div style={{
            padding: '10px 14px', marginBottom: '16px', borderRadius: '8px',
            background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)',
            color: '#f87171', fontSize: '0.85rem'
          }}>
            {error}
          </div>
        )}

        {hint && (
          <div style={{
            padding: '10px 14px', marginBottom: '16px', borderRadius: '8px',
            background: 'rgba(251,191,36,0.1)', border: '1px solid rgba(251,191,36,0.3)',
            color: '#fbbf24', fontSize: '0.85rem', fontFamily: 'monospace', letterSpacing: '0.05em',
          }}>
            {hint}
          </div>
        )}

        {step === 'email' ? (
          <form onSubmit={handleRequestOtp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            <div style={{ position: 'relative' }}>
              <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
              <input
                type="email"
                placeholder="Enter your @niveshaay.com email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                required
                autoFocus
                style={inputStyle}
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}
            >
              {loading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Sending code…</span></> : <><Mail size={16} /><span>Send Code</span></>}
            </button>
          </form>
        ) : (
          <form onSubmit={handleVerifyOtp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            <div style={{ position: 'relative' }}>
              <KeyRound size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
              <input
                type="text"
                inputMode="numeric"
                placeholder="Enter 6-digit code"
                value={code}
                onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                required
                autoFocus
                maxLength={6}
                style={{ ...inputStyle, letterSpacing: '0.25em', fontSize: '1.15rem' }}
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}
            >
              {loading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Verifying…</span></> : <><LogIn size={16} /><span>Sign In</span></>}
            </button>
            <button
              type="button"
              onClick={() => { setStep('email'); setError(''); setHint(''); }}
              style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.85rem', cursor: 'pointer', padding: 0 }}
            >
              ← Use a different email
            </button>
          </form>
        )}
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
