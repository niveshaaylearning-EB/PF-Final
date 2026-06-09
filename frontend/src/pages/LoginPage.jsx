import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Mail, LogIn, RefreshCw, UserPlus, ArrowLeft, KeyRound, QrCode, Copy, Download, ShieldCheck } from 'lucide-react';
import { setToken } from '../utils/auth';
import { API_ROOT as API } from '../config.js';

export default function LoginPage() {
  const navigate = useNavigate();
  const [email,        setEmail]       = useState('');
  const [loading,      setLoading]     = useState(false);
  const [error,        setError]       = useState('');
  const [mode,         setMode]        = useState('login'); // 'login' | 'totp_verify' | 'totp_setup' | 'backup_display' | 'request'
  
  // 2FA state
  const [tempToken,    setTempToken]   = useState('');
  const [otpCode,      setOtpCode]     = useState('');
  const [qrSvg,        setQrSvg]       = useState('');
  const [setupSecret,  setSetupSecret] = useState('');
  const [backupCodes,  setBackupCodes] = useState([]);
  const [finalToken,   setFinalToken]  = useState('');
  const [copied,       setCopied]      = useState(false);

  // Request Access state
  const [reqEmail,     setReqEmail]    = useState('');
  const [reqLoading,   setReqLoading]  = useState(false);
  const [reqMsg,       setReqMsg]      = useState('');
  const [reqError,     setReqError]    = useState('');

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
      const { status, temp_token } = res.data;
      setTempToken(temp_token);

      if (status === '2fa_required') {
        setMode('totp_verify');
        setOtpCode('');
      } else if (status === '2fa_setup_required') {
        // Fetch QR Code and Secret
        const enrollRes = await axios.post(
          `${API}/auth/totp/enroll`,
          {},
          { headers: { Authorization: `Bearer ${temp_token}` } }
        );
        setQrSvg(enrollRes.data.qr_svg);
        setSetupSecret(enrollRes.data.secret);
        setMode('totp_setup');
        setOtpCode('');
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  async function handleVerifyTotp(e) {
    e.preventDefault();
    setError('');
    if (otpCode.trim().length < 6) {
      setError('Verification code must be at least 6 characters.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(
        `${API}/auth/totp/verify`,
        { code: otpCode.trim() },
        { headers: { Authorization: `Bearer ${tempToken}` } }
      );
      setToken(res.data.token);
      navigate('/rebalance-alerts', { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid or expired code.');
    } finally {
      setLoading(false);
    }
  }

  async function handleVerifyEnroll(e) {
    e.preventDefault();
    setError('');
    if (otpCode.trim().length !== 6) {
      setError('Authenticator code must be exactly 6 digits.');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post(
        `${API}/auth/totp/verify-enroll`,
        { code: otpCode.trim() },
        { headers: { Authorization: `Bearer ${tempToken}` } }
      );
      setBackupCodes(res.data.backup_codes);
      setFinalToken(res.data.token);
      setMode('backup_display');
    } catch (err) {
      setError(err.response?.data?.detail || 'Invalid code. Scan again and enter the code.');
    } finally {
      setLoading(false);
    }
  }

  function handleConfirmBackupCodes() {
    setToken(finalToken);
    navigate('/rebalance-alerts', { replace: true });
  }

  function handleCopyBackupCodes() {
    const text = backupCodes.join('\n');
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function handleDownloadBackupCodes() {
    const text = `NIA Performance Center Backup Recovery Codes\nGenerated at: ${new Date().toLocaleString()}\nEmail: ${email}\n\n${backupCodes.join('\n')}\n\nStore these safely! Each code can only be used once.`;
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `nia_backup_codes_${email.replace('@', '_')}.txt`;
    link.click();
    URL.revokeObjectURL(url);
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
      setReqError(err.response?.data?.detail || 'Failed to submit. Please try again.');
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
      <div className="glass-panel animate-slide-up" style={{ width: '100%', maxWidth: '440px', padding: '2.5rem 2rem' }}>

        <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
          <div style={{
            width: '56px', height: '56px', borderRadius: '16px', margin: '0 auto 16px',
            background: 'linear-gradient(135deg, rgba(99,102,241,0.3), rgba(16,185,129,0.2))',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            border: '1px solid rgba(99,102,241,0.35)',
          }}>
            {mode === 'login' && <LogIn color="var(--primary)" size={26} />}
            {mode === 'totp_verify' && <KeyRound color="var(--primary)" size={26} />}
            {mode === 'totp_setup' && <QrCode color="var(--primary)" size={26} />}
            {mode === 'backup_display' && <ShieldCheck color="var(--positive)" size={26} />}
            {mode === 'request' && <UserPlus color="var(--primary)" size={26} />}
          </div>
          <h1 className="text-gradient" style={{ fontSize: '1.7rem', margin: 0 }}>NIA Performance Center</h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.88rem', marginTop: '6px' }}>
            {mode === 'login' && 'Sign in with your NIA email'}
            {mode === 'totp_verify' && 'Enter 2FA Code or Recovery Code'}
            {mode === 'totp_setup' && 'Set up Google Authenticator 2FA'}
            {mode === 'backup_display' && 'Save your backup recovery codes'}
            {mode === 'request' && 'Request access to the dashboard'}
          </p>
        </div>

        {/* ── Mode 1: Sign in with email ── */}
        {mode === 'login' && (
          <>
            {error && <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>{error}</div>}
            <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div style={{ position: 'relative' }}>
                <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                <input type="email" placeholder="Enter your NIA email" value={email}
                  onChange={e => setEmail(e.target.value)} required autoFocus style={inputStyle} />
              </div>
              <button type="submit" disabled={loading} className="btn btn-primary"
                style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                {loading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Verifying email…</span></> : <><LogIn size={16} /><span>Continue</span></>}
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

        {/* ── Mode 2: Verify TOTP 6-digit or Recovery Code ── */}
        {mode === 'totp_verify' && (
          <>
            {error && <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>{error}</div>}
            <form onSubmit={handleVerifyTotp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div style={{ position: 'relative' }}>
                <KeyRound size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                <input type="text" placeholder="6-digit code or XXXX-XXXX" value={otpCode}
                  onChange={e => setOtpCode(e.target.value.toUpperCase())} required autoFocus style={inputStyle} />
              </div>
              <button type="submit" disabled={loading || !otpCode.trim()} className="btn btn-primary"
                style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                {loading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Verifying…</span></> : <><LogIn size={16} /><span>Verify &amp; Sign In</span></>}
              </button>
              <div style={{ textAlign: 'center', marginTop: '4px' }}>
                <button type="button" onClick={() => { setMode('login'); setError(''); }}
                  style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px', fontFamily: 'inherit' }}>
                  <ArrowLeft size={13} /> Change email
                </button>
              </div>
            </form>
          </>
        )}

        {/* ── Mode 3: 2FA First-Time Setup (QR SVG + Code) ── */}
        {mode === 'totp_setup' && (
          <>
            {error && <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>{error}</div>}
            <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', marginBottom: '16px', lineHeight: 1.5 }}>
              1. Scan the QR code below inside <strong>Google Authenticator</strong> or any authenticator app.
            </div>
            
            {qrSvg && (
              <div 
                className="qr-container"
                dangerouslySetInnerHTML={{ __html: qrSvg }} 
                style={{ 
                  margin: '16px auto', 
                  maxWidth: '180px', 
                  background: '#fff', 
                  padding: '12px', 
                  borderRadius: '12px', 
                  boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center'
                }} 
              />
            )}

            <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', textAlign: 'center', marginBottom: '16px' }}>
              Secret Key: <code style={{ color: 'var(--primary)', padding: '2px 6px', background: 'rgba(255,255,255,0.06)', borderRadius: '4px', letterSpacing: '1px' }}>{setupSecret}</code>
            </div>

            <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', marginBottom: '12px' }}>
              2. Enter the 6-digit code shown in the app to confirm:
            </div>

            <form onSubmit={handleVerifyEnroll} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div style={{ position: 'relative' }}>
                <KeyRound size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                <input type="text" maxLength={6} placeholder="6-digit code" value={otpCode}
                  onChange={e => setOtpCode(e.target.value.replace(/\D/g, '').slice(0, 6))} required style={inputStyle} />
              </div>
              <button type="submit" disabled={loading || otpCode.length !== 6} className="btn btn-primary"
                style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px', opacity: otpCode.length !== 6 ? 0.6 : 1 }}>
                {loading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Activating 2FA…</span></> : <><ShieldCheck size={16} /><span>Activate 2FA</span></>}
              </button>
              <div style={{ textAlign: 'center', marginTop: '4px' }}>
                <button type="button" onClick={() => { setMode('login'); setError(''); }}
                  style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px', fontFamily: 'inherit' }}>
                  <ArrowLeft size={13} /> Cancel setup
                </button>
              </div>
            </form>
          </>
        )}

        {/* ── Mode 4: Display Backup Recovery Codes ── */}
        {mode === 'backup_display' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            <div style={{ fontSize: '0.85rem', color: 'var(--positive)', background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.2)', padding: '10px 14px', borderRadius: '8px', lineHeight: 1.5 }}>
              🛡️ <strong>2FA is now enabled successfully!</strong>
            </div>
            
            <p style={{ color: 'var(--text-muted)', fontSize: '0.82rem', margin: 0, lineHeight: 1.5 }}>
              Save these recovery codes. If you lose your phone, you can enter one of these codes to log in. <strong>Each code can only be used once.</strong>
            </p>

            <div style={{
              display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px',
              background: 'rgba(0,0,0,0.15)', padding: '16px', borderRadius: '10px',
              fontFamily: 'monospace', fontSize: '0.95rem', color: 'var(--text-main)',
              letterSpacing: '1px', border: '1px solid rgba(255,255,255,0.06)'
            }}>
              {backupCodes.map((code, idx) => (
                <div key={idx} style={{ padding: '4px 8px' }}>{code}</div>
              ))}
            </div>

            <div style={{ display: 'flex', gap: '10px' }}>
              <button onClick={handleCopyBackupCodes} className="btn"
                style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px', padding: '10px', background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', color: 'var(--text-main)' }}>
                <Copy size={14} />
                <span>{copied ? 'Copied!' : 'Copy'}</span>
              </button>
              <button onClick={handleDownloadBackupCodes} className="btn"
                style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px', padding: '10px', background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', color: 'var(--text-main)' }}>
                <Download size={14} />
                <span>Download</span>
              </button>
            </div>

            <button onClick={handleConfirmBackupCodes} className="btn btn-primary"
              style={{ width: '100%', padding: '12px', marginTop: '8px', fontWeight: 'bold' }}>
              I have saved the codes — Go to Dashboard
            </button>
          </div>
        )}

        {/* ── Mode 5: Request Access ── */}
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
                {reqError && <div style={{ padding: '10px 14px', marginBottom: '16px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.85rem' }}>{reqError}</div>}
                <form onSubmit={handleRequestAccess} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                  <div style={{ position: 'relative' }}>
                    <Mail size={16} style={{ position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
                    <input type="email" placeholder="your.name@niveshaay.com" value={reqEmail}
                      onChange={e => setReqEmail(e.target.value)} required autoFocus style={inputStyle} />
                  </div>
                  <button type="submit" disabled={reqLoading} className="btn btn-primary"
                    style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px' }}>
                    {reqLoading ? <><RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /><span>Sending…</span></> : <><UserPlus size={16} /><span>Request Access</span></>}
                  </button>
                </form>
                <div style={{ textAlign: 'center', marginTop: '1rem' }}>
                  <button onClick={() => setMode('login')} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.82rem', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px', fontFamily: 'inherit' }}>
                    <ArrowLeft size={13} /> Back to Login
                  </button>
                </div>
              </>
            )}
          </>
        )}
      </div>
      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        .qr-container svg { width: 100% !important; height: 100% !important; }
        .qr-container svg rect, .qr-container svg path { fill: #000000 !important; }
      `}</style>
    </div>
  );
}
