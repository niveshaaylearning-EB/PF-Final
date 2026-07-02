import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useEffect, useState } from 'react';
import { getEmail, ADMIN_EMAILS } from '../utils/auth.js';

const EDIT_ALLOWED = ADMIN_EMAILS;
const BAR_H = 44;

const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const WP_BASE  = IS_LOCAL
  ? `http://${window.location.hostname}:8001`
  : `${window.location.origin}/wp/`;

export default function ActualPortfolio() {
  const navigate = useNavigate();
  const [headerBottom, setHeaderBottom] = useState(120);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const header = document.querySelector('header');
    if (header) setHeaderBottom(Math.ceil(header.getBoundingClientRect().bottom));
  }, []);

  const iframeTop = headerBottom + BAR_H;
  const email     = getEmail() || '';
  const canEdit   = EDIT_ALLOWED.has(email);
  const WEBPORTAL_URL = `${WP_BASE}?u=${encodeURIComponent(email)}&edit=${canEdit ? '1' : '0'}`;

  return (
    <>
      {/* Placeholder so the container doesn't collapse */}
      <div style={{ height: `calc(100vh - ${headerBottom}px)` }} />

      {/* Thin sub-bar: sits between app header and iframe, no overlap */}
      <div style={{
        position: 'fixed',
        top: headerBottom,
        left: 0,
        width: '100vw',
        height: BAR_H,
        background: '#0b0f19',
        borderBottom: '1px solid rgba(255,255,255,0.08)',
        display: 'flex',
        alignItems: 'center',
        padding: '0 16px',
        gap: '14px',
        zIndex: 100,
        boxSizing: 'border-box',
      }}>
        <button
          className="btn btn-secondary"
          onClick={() => navigate('/')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '6px 12px', fontSize: '0.82rem' }}
        >
          <ArrowLeft size={14} /> Back
        </button>
        <h3 className="text-gradient" style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600 }}>
          Actual Portfolio
        </h3>
      </div>

      {/* Loading overlay — visible until iframe fires onLoad */}
      {!loaded && (
        <div style={{
          position: 'fixed',
          top: iframeTop,
          left: 0,
          width: '100vw',
          height: `calc(100vh - ${iframeTop}px)`,
          background: '#0b0f19',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '16px',
          zIndex: 60,
        }}>
          <div style={{
            width: 40,
            height: 40,
            border: '3px solid rgba(255,255,255,0.1)',
            borderTop: '3px solid #6366f1',
            borderRadius: '50%',
            animation: 'spin 0.8s linear infinite',
          }} />
          <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', margin: 0 }}>
            Loading portfolio data...
          </p>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      )}

      {/* Full-viewport iframe — starts below the sub-bar */}
      <iframe
        src={WEBPORTAL_URL}
        title="Actual Portfolio Dashboard"
        onLoad={() => setLoaded(true)}
        style={{
          position: 'fixed',
          top: iframeTop,
          left: 0,
          width: '100vw',
          height: `calc(100vh - ${iframeTop}px)`,
          border: 'none',
          zIndex: 50,
          display: 'block',
        }}
        allowFullScreen
      />
    </>
  );
}
