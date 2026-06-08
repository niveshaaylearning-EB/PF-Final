import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Mail, LogIn, RefreshCw, UserPlus, ArrowLeft, KeyRound } from 'lucide-react';
import { setToken } from '../utils/auth';
import { API_ROOT as API } from '../config.js';

export default function LoginPage() {
  const navigate = useNavigate();

  // Login & OTP state
  const [email,         setEmail]       = useState('');
  const [otpCode,       setOtpCode]     = useState('');
  const [otpMode,       setOtpMode]     = useState(false); // false = email input, true = otp input
  const [loading,       setLoading]     = useState(false);
  const [error,         setError]       = useState('');
  const [successMsg,    setSuccessMsg]  = useState('');
  const [coords,        setCoords]      = useState({ latitude: null, longitude: null });
  const [resendTimer,   setResendTimer] = useState(0);

  // Request access state
  const [mode,       setMode]       = useState('login');  // 'login' | 'request'
  const [reqEmail,   setReqEmail]   = useState('');
  const [reqLoading, setReqLoading] = useState(false);
  const [reqMsg,     setReqMsg]     = useState('');
  const [reqError,   setReqError]   = useState('');

  // Countdown timer effect
  useEffect(() => {
    let interval = null;
    if (resendTimer > 0) {
      interval = setInterval(() => {
        setResendTimer((prev) => prev - 1);
      }, 1000);
    } else {
      clearInterval(interval);
    }
    return () => clearInterval(interval);
  }, [resendTimer]);

  // Helper to fetch geolocation coordinates
  async function fetchCoords() {
    if (!navigator.geolocation) return { latitude: null, longitude: null };
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const c = { latitude: pos.coords.latitude, longitude: pos.coords.longitude };
          setCoords(c);
          resolve(c);
        },
        () => {
          resolve({ latitude: null, longitude: null });
        },
        { timeout: 4000 }
      );
    });
  }

  async function handleSendOtp(e, isResend = false) {
    if (e) e.preventDefault();
    setError('');
    setSuccessMsg('');
    const em = email.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setError('Only @niveshaay.com email addresses are allowed.');
      return;
    }
    setLoading(true);
    try {
      const activeCoords = await fetchCoords();
      await axios.post(`${API}/auth/request-otp`, {
        email: em,
        latitude: activeCoords.latitude,
        longitude: activeCoords.longitude
      });
      setOtpMode(true);
      setResendTimer(60);
      setSuccessMsg(isResend ? 'OTP code resent successfully!' : `OTP sent to ${em}`);
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to send OTP. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  async function handleVerifyOtp(e) {
    e.preventDefault();
    setError('');
    setSuccessMsg('');
    const em = email.toLowerCase().trim();
    const codeStr = otpCode.trim();
    if (!codeStr || codeStr.length < 6) {
      setError('Please enter a valid 6-digit OTP code.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/verify-otp`, {
        email: em,
        code: codeStr,
        latitude: coords.latitude,
        longitude: coords.longitude
      });
      setToken(res.data.token);
      navigate('/rebalance-alerts', { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid or expired OTP code.');
    } finally {
      setLoading(false);
    }
  }

  async function handleRequestAccess(e) {
    e.preventDefault();
    setReqError(''); setReqMsg('');
    const em = reqEmail.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setReqError('Only @niveshaay.com email addresses are allowed.');
      return;
    }
    setReqLoading(true);
    try {
      const res = await axios.post(`${API}/api/access-requests`, { email: em });
      const { status, message } = res.data;
      if (status === 'already_approved') {
        setReqMsg(''); setReqError('');
        setEmail(em);
        setMode('login');
        setError('Your email is already approved — sign in below.');
      } else {
        setReqMsg(message);
      }
    } catch (err) {
      setReqError(err.response?.data?.detail || 'Failed to submit request. Please try again.');
    } finally {
      setReqLoading(false);
    }
  }

  const inputStyle = {
    width: '100%', padding: '12px 16px 12px 40px', fontSize: '1rem',
    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.15)',
    borderRadius: '10px', color: 'var(--text-main)', outline: 'none', boxSizing: 'border-box',
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1rem' }}>
      <div className="glass-panel animate-slide-up" style={{ width: '100%', maxWidth: '400px', padding: '2.5rem 2rem' }}>

        {/* Logo + title */}
        <div style={{ textAlign: 'center', marginBottom: '2rem' }}>
          <div style={{
            width: '56px', height: '56px', borderRadius: '16px', margin: '0 auto 16px',
            background: 'linear-gradient(135deg, rgba(99,102,241,0.3), rgba(16,185,129,0.2))',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            border: '1px solid rgba(99,102,241,0.35)',
          }}>
            {mode === 'login' ? <LogIn color="var(--primary)" size={26} /> : <UserPlus color="var(--primary)" size={26} />}
          </div>
          <h1 className="text-gradient" style={{ fontSize: '1.7rem', margin: 0 }}>NIA Performance Center</h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.88rem', marginTop: '6px' }}>
            {mode === 'login' ? 'Sign in with your NIA email' : 'Request access to the dashboard'}
          </p>
        </div>

        {/* ── Login mode ── */}
        {mode === 'login' && (
          <>
            {error && (
              <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>
                {error}
              </div>
            )}
            {successMsg && (
              <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.35)', color: 'var(--positive)', fontSize: '0.85rem' }}>
                {successMsg}
              </div>
            )}

            {!otpMode ? (
              <form onSubmit={handleSendOtp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <div style={{ position: 'relative' }}>
                  <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                  <input type="email" placeholder="Enter your NIA email" value={email}
                    onChange={e => setEmail(e.target.value)} required autoFocus style={inputStyle} />
                </div>
                <button type="submit" disabled={loading} className="btn btn-primary"
                  style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                  {loading
                    ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Sending OTP…</span></>
                    : <><LogIn size={16} /><span>Send OTP</span></>}
                </button>
              </form>
            ) : (
              <form onSubmit={handleVerifyOtp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', background: 'rgba(255,255,255,0.03)', padding: '10px 12px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.06)', wordBreak: 'break-all' }}>
                  Sending OTP to: <strong style={{ color: 'var(--text-main)' }}>{email}</strong>
                </div>
                <div style={{ position: 'relative' }}>
                  <KeyRound size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                  <input type="text" pattern="[0-9]*" inputMode="numeric" maxLength={6} placeholder="Enter 6-digit OTP code" value={otpCode}
                    onChange={e => setOtpCode(e.target.value.replace(/\D/g, ''))} required autoFocus style={inputStyle} />
                </div>
                <button type="submit" disabled={loading} className="btn btn-primary"
                  style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                  {loading
                    ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Verifying…</span></>
                    : <><LogIn size={16} /><span>Verify & Sign In</span></>}
                </button>

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '4px', fontSize: '0.82rem' }}>
                  <button type="button" onClick={() => { setOtpMode(false); setOtpCode(''); setError(''); setSuccessMsg(''); }}
                    style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', textDecoration: 'underline', fontFamily: 'inherit', padding: 0 }}>
                    ← Change Email
                  </button>
                  {resendTimer > 0 ? (
                    <span style={{ color: 'var(--text-muted)' }}>Resend OTP in {resendTimer}s</span>
                  ) : (
                    <button type="button" onClick={(e) => handleSendOtp(null, true)} disabled={loading}
                      style={{ background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', textDecoration: 'underline', fontFamily: 'inherit', padding: 0, fontWeight: 500 }}>
                      Resend OTP
                    </button>
                  )}
                </div>
              </form>
            )}

            {/* Request access link */}
            <div style={{ textAlign: 'center', marginTop: '1.25rem' }}>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.82rem' }}>Don't have access? </span>
              <button onClick={() => { setMode('request'); setReqEmail(email); setReqMsg(''); setReqError(''); }}
                style={{ background: 'none', border: 'none', color: 'var(--primary)', fontSize: '0.82rem', cursor: 'pointer', textDecoration: 'underline', fontFamily: 'inherit' }}>
                Request Access
              </button>
            </div>
          </>
        )}

        {/* ── Request access mode ── */}
        {mode === 'request' && (
          <>
            {reqMsg ? (
              <div style={{ padding: '16px', borderRadius: '10px', background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.25)', textAlign: 'center' }}>
                <div style={{ fontSize: '2rem', marginBottom: '8px' }}>✅</div>
                <p style={{ color: 'var(--positive)', fontSize: '0.88rem', margin: 0, lineHeight: 1.6 }}>{reqMsg}</p>
                <button onClick={() => setMode('login')} style={{ marginTop: '14px', background: 'none', border: 'none', color: 'var(--primary)', fontSize: '0.82rem', cursor: 'pointer', textDecoration: 'underline', fontFamily: 'inherit' }}>
                  ← Back to Login
                </button>
              </div>
            ) : (
              <>
                {reqError && (
                  <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>
                    {reqError}
                  </div>
                )}
                <form onSubmit={handleRequestAccess} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                  <div style={{ position: 'relative' }}>
                    <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                    <input type="email" placeholder="your.name@niveshaay.com" value={reqEmail}
                      onChange={e => setReqEmail(e.target.value)} required autoFocus style={inputStyle} />
                  </div>
                  <button type="submit" disabled={reqLoading} className="btn btn-primary"
                    style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                    {reqLoading
                      ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Sending…</span></>
                      : <><UserPlus size={16} /><span>Request Access</span></>}
                  </button>
                </form>
                <div style={{ textAlign: 'center', marginTop: '1rem' }}>
                  <button onClick={() => setMode('login')}
                    style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px', fontFamily: 'inherit' }}>
                    <ArrowLeft size={13} /> Back to Login
                  </button>
                </div>
              </>
            )}
          </>
        )}
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
