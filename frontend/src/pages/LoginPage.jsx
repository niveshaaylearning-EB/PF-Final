import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Mail, LogIn, RefreshCw, KeyRound, UserPlus, Lock, Eye, EyeOff } from 'lucide-react';
import { setToken, setRefreshToken } from '../utils/auth';
import { API_ROOT as API } from '../config.js';

// ── Password strength validation (mirrors backend rules) ─────────────────────
function validatePassword(pw) {
  if (pw.length < 8)                    return 'Password must be at least 8 characters.';
  if (!/[A-Z]/.test(pw))               return 'Password must contain at least one uppercase letter.';
  if (!/[0-9]/.test(pw))               return 'Password must contain at least one number.';
  if (!/[^A-Za-z0-9]/.test(pw))        return 'Password must contain at least one special character.';
  return null;
}

// ── Shared styles ─────────────────────────────────────────────────────────────
const inputBase = {
  width: '100%', padding: '12px 16px 12px 40px', fontSize: '1rem',
  background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.15)',
  borderRadius: '10px', color: 'var(--text-main)', outline: 'none', boxSizing: 'border-box',
};
const inputNoIcon = { ...inputBase, paddingLeft: '16px' };
const iconStyle = {
  position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)',
  color: 'var(--text-muted)', pointerEvents: 'none',
};

function ErrorBox({ msg }) {
  if (!msg) return null;
  return (
    <div style={{
      padding: '10px 14px', marginBottom: '14px', borderRadius: '8px',
      background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)',
      color: '#f87171', fontSize: '0.85rem',
    }}>{msg}</div>
  );
}

function HintBox({ msg }) {
  if (!msg) return null;
  return (
    <div style={{
      padding: '10px 14px', marginBottom: '14px', borderRadius: '8px',
      background: 'rgba(251,191,36,0.1)', border: '1px solid rgba(251,191,36,0.3)',
      color: '#fbbf24', fontSize: '0.85rem', fontFamily: 'monospace', letterSpacing: '0.05em',
    }}>{msg}</div>
  );
}

function SuccessBox({ msg }) {
  if (!msg) return null;
  return (
    <div style={{
      padding: '10px 14px', marginBottom: '14px', borderRadius: '8px',
      background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.3)',
      color: '#34d399', fontSize: '0.85rem',
    }}>{msg}</div>
  );
}

function PasswordInput({ placeholder, value, onChange, name }) {
  const [show, setShow] = useState(false);
  return (
    <div style={{ position: 'relative' }}>
      <Lock size={16} style={iconStyle} />
      <input
        type={show ? 'text' : 'password'}
        placeholder={placeholder}
        value={value}
        onChange={onChange}
        name={name}
        required
        style={{ ...inputBase, paddingRight: '42px' }}
      />
      <button
        type="button"
        onClick={() => setShow(s => !s)}
        style={{
          position: 'absolute', right: '12px', top: '50%', transform: 'translateY(-50%)',
          background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)',
          padding: 0, display: 'flex',
        }}
      >
        {show ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}

function Spinner() {
  return <RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} />;
}

// ── Main component ────────────────────────────────────────────────────────────
export default function LoginPage() {
  const navigate = useNavigate();

  // 'login' | 'register' | 'register-otp' | 'forgot' | 'reset' | 'pending'
  const [view,    setView]    = useState('login');
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState('');
  const [hint,    setHint]    = useState('');
  const [success, setSuccess] = useState('');

  // login
  const [loginEmail, setLoginEmail] = useState('');
  const [loginPw,    setLoginPw]    = useState('');

  // register
  const [regFirst, setRegFirst] = useState('');
  const [regLast,  setRegLast]  = useState('');
  const [regEmail, setRegEmail] = useState('');
  const [regPw,    setRegPw]    = useState('');
  const [regPw2,   setRegPw2]   = useState('');
  const [regOtp,   setRegOtp]   = useState('');

  // forgot / reset
  const [fpEmail,  setFpEmail]  = useState('');
  const [fpOtp,    setFpOtp]    = useState('');
  const [fpNewPw,  setFpNewPw]  = useState('');
  const [fpNewPw2, setFpNewPw2] = useState('');

  function resetState() {
    setError(''); setHint(''); setSuccess('');
    setLoading(false);
  }

  function go(v) { resetState(); setView(v); }

  // ── Login ──────────────────────────────────────────────────────────────────
  async function handleLogin(e) {
    e.preventDefault();
    setError(''); setHint(''); setSuccess('');
    const em = loginEmail.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) {
      setError('Only @niveshaay.com email addresses are allowed.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/login`, { email: em, password: loginPw });
      // 428 = first-time login, no password set — backend already sent OTP
      if (res.status === 428 || res.data?.status === 'password_setup_required') {
        setFpEmail(em);
        setFpOtp(''); setFpNewPw(''); setFpNewPw2('');
        if (res.data?.code) setHint(`Email delivery failed. Your setup code: ${res.data.code}`);
        go('reset');
        setSuccess(`First-time setup: a password code was sent to ${em}. Enter it below to set your password.`);
        return;
      }
      setToken(res.data.token);
      if (res.data.refresh_token) setRefreshToken(res.data.refresh_token);
      navigate('/', { replace: true });
    } catch (err) {
      // axios throws on non-2xx — check if it's the 428 password setup response
      if (err.response?.status === 428) {
        const d = err.response.data;
        setFpEmail(d?.email || em);
        setFpOtp(''); setFpNewPw(''); setFpNewPw2('');
        if (d?.code) setHint(`Email delivery failed. Your setup code: ${d.code}`);
        go('reset');
        setSuccess(`First-time setup: a password code was sent to ${d?.email || em}. Enter it below to set your password.`);
        return;
      }
      setError(err.response?.data?.detail || 'Login failed. Please check your credentials.');
    } finally {
      setLoading(false);
    }
  }

  // ── Register step 1: send OTP ──────────────────────────────────────────────
  async function handleRegisterSendOtp(e) {
    e.preventDefault();
    setError(''); setHint(''); setSuccess('');
    const em = regEmail.toLowerCase().trim();
    if (!regFirst.trim() || !regLast.trim()) { setError('First name and last name are required.'); return; }
    if (!em.endsWith('@niveshaay.com')) { setError('Only @niveshaay.com email addresses are allowed.'); return; }
    const pwErr = validatePassword(regPw);
    if (pwErr) { setError(pwErr); return; }
    if (regPw !== regPw2) { setError('Passwords do not match.'); return; }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/register`, {
        first_name: regFirst.trim(), last_name: regLast.trim(), email: em, password: regPw,
      });
      if (res.data.code) setHint(`Email delivery failed. Your code: ${res.data.code}`);
      setRegOtp('');
      go('register-otp');
    } catch (err) {
      setError(err.response?.data?.detail || 'Registration failed. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  // ── Register step 2: verify OTP + complete ─────────────────────────────────
  async function handleRegisterComplete(e) {
    e.preventDefault();
    setError(''); setHint('');
    if (regOtp.trim().length < 6) { setError('Enter the 6-digit code from your email.'); return; }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/register/complete`, {
        first_name: regFirst.trim(), last_name: regLast.trim(),
        email: regEmail.toLowerCase().trim(), password: regPw, code: regOtp.trim(),
      });
      if (res.data.status === 'pending_approval') {
        go('pending');
      } else {
        setToken(res.data.token);
        if (res.data.refresh_token) setRefreshToken(res.data.refresh_token);
        navigate('/', { replace: true });
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'Verification failed. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  // ── Forgot password: send OTP ──────────────────────────────────────────────
  async function handleForgotSendOtp(e) {
    e.preventDefault();
    setError(''); setHint(''); setSuccess('');
    const em = fpEmail.toLowerCase().trim();
    if (!em.endsWith('@niveshaay.com')) { setError('Only @niveshaay.com email addresses are allowed.'); return; }
    setLoading(true);
    try {
      const res = await axios.post(`${API}/auth/forgot-password`, { email: em });
      if (res.data.code) setHint(`Email delivery failed. Your code: ${res.data.code}`);
      setFpOtp(''); setFpNewPw(''); setFpNewPw2('');
      go('reset');
      if (!res.data.code) setSuccess(`Password reset code sent to ${em}.`);
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to send code. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  // ── Reset password ─────────────────────────────────────────────────────────
  async function handleReset(e) {
    e.preventDefault();
    setError(''); setHint('');
    if (fpOtp.trim().length < 6) { setError('Enter the 6-digit code from your email.'); return; }
    const pwErr = validatePassword(fpNewPw);
    if (pwErr) { setError(pwErr); return; }
    if (fpNewPw !== fpNewPw2) { setError('Passwords do not match.'); return; }
    setLoading(true);
    try {
      await axios.post(`${API}/auth/reset-password`, {
        email: fpEmail.toLowerCase().trim(), code: fpOtp.trim(), new_password: fpNewPw,
      });
      setSuccess('Password updated! You can now log in.');
      setTimeout(() => go('login'), 1500);
    } catch (err) {
      setError(err.response?.data?.detail || 'Reset failed. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  // ── Layout wrapper ─────────────────────────────────────────────────────────
  const subtitle = {
    login:           'Sign in to your NIA account',
    register:        'Create your NIA account',
    'register-otp':  `Verify your email — ${regEmail}`,
    forgot:          'Reset your password',
    reset:           `Enter the code sent to ${fpEmail}`,
    pending:         'Registration submitted',
  }[view] || '';

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1rem' }}>
      <div className="glass-panel animate-slide-up" style={{ width: '100%', maxWidth: '440px', padding: '2.5rem 2rem' }}>

        {/* Header */}
        <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
          <div style={{
            width: '56px', height: '56px', borderRadius: '16px', margin: '0 auto 16px',
            background: 'linear-gradient(135deg, rgba(99,102,241,0.3), rgba(16,185,129,0.2))',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            border: '1px solid rgba(99,102,241,0.35)',
          }}>
            {view === 'register' || view === 'register-otp'
              ? <UserPlus color="var(--primary)" size={26} />
              : <LogIn color="var(--primary)" size={26} />}
          </div>
          <h1 className="text-gradient" style={{ fontSize: '1.7rem', margin: 0 }}>NIA Performance Center</h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.88rem', marginTop: '6px' }}>{subtitle}</p>
        </div>

        <ErrorBox msg={error} />
        <HintBox  msg={hint} />
        <SuccessBox msg={success} />

        {/* ── LOGIN ── */}
        {view === 'login' && (
          <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ position: 'relative' }}>
              <Mail size={16} style={iconStyle} />
              <input type="email" placeholder="your@niveshaay.com" value={loginEmail}
                onChange={e => setLoginEmail(e.target.value)} required autoFocus style={inputBase} />
            </div>
            <PasswordInput placeholder="Password" value={loginPw} onChange={e => setLoginPw(e.target.value)} />
            <button type="submit" disabled={loading} className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
              {loading ? <><Spinner /><span>Signing in…</span></> : <><LogIn size={16} /><span>Sign In</span></>}
            </button>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '4px' }}>
              <button type="button" onClick={() => go('forgot')}
                style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
                Forgot password?
              </button>
              <button type="button" onClick={() => go('register')}
                style={{ background: 'none', border: 'none', color: 'var(--primary)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
                Create account →
              </button>
            </div>
          </form>
        )}

        {/* ── REGISTER step 1 ── */}
        {view === 'register' && (
          <form onSubmit={handleRegisterSendOtp} style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ display: 'flex', gap: '10px' }}>
              <input type="text" placeholder="First name" value={regFirst}
                onChange={e => setRegFirst(e.target.value)} required style={{ ...inputNoIcon, flex: 1 }} />
              <input type="text" placeholder="Last name" value={regLast}
                onChange={e => setRegLast(e.target.value)} required style={{ ...inputNoIcon, flex: 1 }} />
            </div>
            <div style={{ position: 'relative' }}>
              <Mail size={16} style={iconStyle} />
              <input type="email" placeholder="your@niveshaay.com" value={regEmail}
                onChange={e => setRegEmail(e.target.value)} required autoFocus style={inputBase} />
            </div>
            <PasswordInput placeholder="Password (min 8 chars)" value={regPw} onChange={e => setRegPw(e.target.value)} />
            <PasswordInput placeholder="Confirm password" value={regPw2} onChange={e => setRegPw2(e.target.value)} />
            <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', lineHeight: 1.5 }}>
              Must include: uppercase letter · number · special character
            </div>
            <button type="submit" disabled={loading} className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
              {loading ? <><Spinner /><span>Sending code…</span></> : <><UserPlus size={16} /><span>Send Verification Code</span></>}
            </button>
            <button type="button" onClick={() => go('login')}
              style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
              ← Back to sign in
            </button>
          </form>
        )}

        {/* ── REGISTER step 2: OTP ── */}
        {view === 'register-otp' && (
          <form onSubmit={handleRegisterComplete} style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ position: 'relative' }}>
              <KeyRound size={16} style={iconStyle} />
              <input type="text" inputMode="numeric" placeholder="Enter 6-digit code" value={regOtp}
                onChange={e => setRegOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
                required autoFocus maxLength={6}
                style={{ ...inputBase, letterSpacing: '0.25em', fontSize: '1.15rem' }} />
            </div>
            <button type="submit" disabled={loading} className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
              {loading ? <><Spinner /><span>Creating account…</span></> : <><UserPlus size={16} /><span>Create Account</span></>}
            </button>
            <button type="button" onClick={() => go('register')}
              style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
              ← Change details
            </button>
          </form>
        )}

        {/* ── FORGOT PASSWORD ── */}
        {view === 'forgot' && (
          <form onSubmit={handleForgotSendOtp} style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ position: 'relative' }}>
              <Mail size={16} style={iconStyle} />
              <input type="email" placeholder="your@niveshaay.com" value={fpEmail}
                onChange={e => setFpEmail(e.target.value)} required autoFocus style={inputBase} />
            </div>
            <button type="submit" disabled={loading} className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
              {loading ? <><Spinner /><span>Sending code…</span></> : <><Mail size={16} /><span>Send Reset Code</span></>}
            </button>
            <button type="button" onClick={() => go('login')}
              style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
              ← Back to sign in
            </button>
          </form>
        )}

        {/* ── PENDING APPROVAL ── */}
        {view === 'pending' && (
          <div style={{ textAlign: 'center', padding: '8px 0' }}>
            <div style={{
              width: '60px', height: '60px', borderRadius: '50%', margin: '0 auto 18px',
              background: 'rgba(251,191,36,0.12)', border: '1px solid rgba(251,191,36,0.35)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
              </svg>
            </div>
            <p style={{ color: 'var(--text-main)', fontSize: '0.95rem', fontWeight: 600, marginBottom: '8px' }}>
              Pending Admin Approval
            </p>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', lineHeight: 1.6, marginBottom: '20px' }}>
              Your account has been created and is waiting for approval.
              You'll receive an email at <strong style={{ color: 'var(--text-main)' }}>{regEmail}</strong> once an admin approves your account.
            </p>
            <button type="button" onClick={() => go('login')}
              className="btn btn-primary"
              style={{ width: '100%', padding: '11px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}>
              <LogIn size={16} /> Back to Sign In
            </button>
          </div>
        )}

        {/* ── RESET PASSWORD ── */}
        {view === 'reset' && (
          <form onSubmit={handleReset} style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ position: 'relative' }}>
              <KeyRound size={16} style={iconStyle} />
              <input type="text" inputMode="numeric" placeholder="Enter 6-digit code" value={fpOtp}
                onChange={e => setFpOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
                required autoFocus maxLength={6}
                style={{ ...inputBase, letterSpacing: '0.25em', fontSize: '1.15rem' }} />
            </div>
            <PasswordInput placeholder="New password (min 8 chars)" value={fpNewPw} onChange={e => setFpNewPw(e.target.value)} />
            <PasswordInput placeholder="Confirm new password" value={fpNewPw2} onChange={e => setFpNewPw2(e.target.value)} />
            <button type="submit" disabled={loading} className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
              {loading ? <><Spinner /><span>Updating password…</span></> : <><Lock size={16} /><span>Set New Password</span></>}
            </button>
            <button type="button" onClick={() => go('forgot')}
              style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', padding: 0 }}>
              ← Use a different email
            </button>
          </form>
        )}

      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
