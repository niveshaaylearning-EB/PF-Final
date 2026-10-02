import { useNavigate, useLocation } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { getEmail, getToken, isAdmin } from '../utils/auth.js';
import { getTheme, setTheme, THEME_CHANGE_EVENT } from '../utils/theme.js';

const BAR_H = 44;

const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const WP_BASE  = IS_LOCAL
  ? `http://${window.location.hostname}:8001`
  : `${window.location.origin}/wp/`;
const WP_ORIGIN = IS_LOCAL ? `http://${window.location.hostname}:8001` : window.location.origin;
const THEME_SYNC_TYPE = 'nia-theme-sync';

export default function ActualPortfolio() {
  const navigate = useNavigate();
  // Optional ?wp=/some-page lets a Link deep-link straight into one of the
  // webportal's own sub-pages (e.g. Competitor Analysis) instead of always
  // landing on its root dashboard -- read once at mount, same as the other
  // auth query params below, since the iframe's src never changes after.
  const wpPath = new URLSearchParams(useLocation().search).get('wp') || '';
  const [headerBottom, setHeaderBottom] = useState(120);
  const [loaded, setLoaded] = useState(false);
  const iframeRef = useRef(null);

  // The sub-bar and iframe below are both `position: fixed`, pinned at
  // `headerBottom` px from the viewport top -- so they only stay correctly
  // aligned under the real <header> if that measurement stays current. A
  // one-time mount measurement goes stale the moment the header's height
  // changes afterward (e.g. its content reflows once web fonts finish
  // loading, or an admin-only nav button appears after an async isAdmin()
  // check resolves), which is exactly when the overlap was reported. A
  // ResizeObserver keeps this in sync for as long as the header exists,
  // not just at the instant this component first mounted.
  useEffect(() => {
    const header = document.querySelector('header');
    if (!header) return;
    const measure = () => setHeaderBottom(Math.ceil(header.getBoundingClientRect().bottom));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(header);
    window.addEventListener('resize', measure);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, []);

  // Everything in this page (sub-bar, overlay, iframe) is `position: fixed`
  // and sized to exactly fill the viewport below the header, so the OUTER
  // document itself should never need to scroll. But without this, it still
  // CAN (nothing stops it), and once you scroll the iframe's own content to
  // its end and keep scrolling, the browser hands remaining wheel input to
  // the next scrollable ancestor -- the outer page -- dragging the real
  // header out of view while this component's fixed elements stay put,
  // which is exactly the misalignment that was reported. Locking outer
  // scroll here (and restoring it on unmount) keeps all scroll input inside
  // the iframe where it belongs.
  useEffect(() => {
    const prevHtml = document.documentElement.style.overflow;
    const prevBody = document.body.style.overflow;
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    return () => {
      document.documentElement.style.overflow = prevHtml;
      document.body.style.overflow = prevBody;
    };
  }, []);

  // Keep the embedded webportal iframe's theme live-synced with the outer
  // app: push our theme into it whenever it changes (rather than only at
  // iframe-load time, since the iframe's `src` never changes after mount),
  // and accept theme changes made via the toggle inside the iframe itself.
  useEffect(() => {
    const pushTheme = (theme) => {
      iframeRef.current?.contentWindow?.postMessage({ type: THEME_SYNC_TYPE, theme }, WP_ORIGIN);
    };
    const onOuterThemeChange = (e) => pushTheme(e.detail);
    const onMessage = (e) => {
      if (e.origin !== WP_ORIGIN) return;
      if (e.data?.type === THEME_SYNC_TYPE && (e.data.theme === 'light' || e.data.theme === 'dark')) {
        setTheme(e.data.theme);
      }
    };
    window.addEventListener(THEME_CHANGE_EVENT, onOuterThemeChange);
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener(THEME_CHANGE_EVENT, onOuterThemeChange);
      window.removeEventListener('message', onMessage);
    };
  }, []);

  const iframeTop = headerBottom + BAR_H;
  const email     = getEmail() || '';
  const canEdit   = isAdmin();
  // Pass the auth token through the URL too, not just email/edit flags: in
  // local dev the iframe loads from a different origin (port 8001), so it
  // can't read the main app's localStorage token at all. Without this, admin
  // detection AND every authenticated upload inside the iframe silently fail
  // (empty Authorization header -> 403) even when the edit flag says "yes".
  const token = getToken() || '';
  const WEBPORTAL_URL = `${WP_BASE.replace(/\/$/, '')}${wpPath}?u=${encodeURIComponent(email)}&edit=${canEdit ? '1' : '0'}&t=${encodeURIComponent(token)}&theme=${getTheme()}`;

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
        background: 'var(--bg-color)',
        borderBottom: '1px solid var(--panel-border)',
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
          background: 'var(--bg-color)',
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
            border: '3px solid var(--panel-border)',
            borderTop: '3px solid var(--primary)',
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
        ref={iframeRef}
        src={WEBPORTAL_URL}
        title="Actual Portfolio Dashboard"
        onLoad={() => {
          setLoaded(true);
          // Covers the case where the outer theme changed while the iframe
          // was still loading (its src param would already be stale by then).
          iframeRef.current?.contentWindow?.postMessage({ type: THEME_SYNC_TYPE, theme: getTheme() }, WP_ORIGIN);
        }}
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
