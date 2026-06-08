import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Mail, LogIn, RefreshCw, UserPlus, ArrowLeft } from 'lucide-react';
import { setToken } from '../utils/auth';
import { API_ROOT as API } from '../config.js';

export default function LoginPage() {
  const navigate = useNavigate();
  const [email,   setEmail]   = useState('');
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState('');

  // Request access state
  const [mode,       setMode]       = useState('login');
  const [reqEmail,   setReqEmail]   = useState('');
  const [reqLoading, setReqLoading] = useState(false);
  const [reqMsg,     setReqMsg]     = useState('');
  const [reqError,   setReqError]   = useState('');

  async function handleLogin(e) {
    e.preventDefault();
    setError('');
    const em = email.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setError('Only @niveshaay.com email addresses are allowed.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/direct-login`, { email: em });
      setToken(res.data.token);
      navigate('/rebalance-alerts', { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || 'Login failed. Please try again.');
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
        setEmail(em); setMode('login');
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
            <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div style={{ position: 'relative' }}>
                <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                <input type="email" placeholder="Enter your NIA email" value={email}
                  onChange={e => setEmail(e.target.value)} required autoFocus style={inputStyle} />
              </div>
              <button type="submit" disabled={loading} className="btn btn-primary"
                style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                {loading
                  ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Signing in…</span></>
                  : <><LogIn size={16} /><span>Sign In</span></>}
              </button>
            </form>
            <div style={{ textAlign: 'center', marginTop: '1.25rem' }}>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.82rem' }}>Don't have access? </span>
              <button onClick={() => { setMode('request'); setReqEmail(email); setReqMsg(''); setReqError(''); }}
                style={{ background: 'none', border: 'none', color: 'var(--primary)', fontSize: '0.82rem', cursor: 'pointer', textDecoration: 'underline', fontFamily: 'inherit' }}>
                Request Access
              </button>
            </div>
          </>
        )}

        {/* ── Request Access mode ── */}
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
