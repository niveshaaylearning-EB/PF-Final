import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { TrendingUp, Activity, Search, BarChart2, AlertTriangle, Target, ShieldCheck, Calendar, Users, X } from 'lucide-react';
import axios from 'axios';
import { isAdmin, getEmail, getFirstName } from '../utils/auth';

import { API_BASE } from '../config.js';

const RESULTS_POPUP_DISMISS_KEY = 'nia_results_popup_dismissed';

function HomePage() {
  const [alerts, setAlerts] = useState([]);
  const [resultsTomorrow, setResultsTomorrow] = useState([]);
  const [showResultsPopup, setShowResultsPopup] = useState(false);

  useEffect(() => {
    axios.get(`${API_BASE}/alerts`).then(r => setAlerts(r.data || [])).catch(() => {});
  }, []);

  useEffect(() => {
    axios.get(`${API_BASE}/portfolio/results-calendar`).then(r => {
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      const tomorrowStr = tomorrow.toISOString().slice(0, 10);
      const events = (r.data || []).filter(e => e.date === tomorrowStr);
      setResultsTomorrow(events);

      if (events.length === 0) return;
      // Only pop up once per distinct set of tomorrow's results -- if the admin
      // already saw and closed this exact list, don't nag again on every visit,
      // but a new/changed stock list (or a new day) shows it again.
      const signature = `${tomorrowStr}|${events.map(e => e.stock_code).sort().join(',')}`;
      let dismissed = '';
      try { dismissed = localStorage.getItem(RESULTS_POPUP_DISMISS_KEY) || ''; } catch { /* ignore */ }
      if (dismissed !== signature) setShowResultsPopup(true);
    }).catch(() => {});
  }, []);

  const dismissResultsPopup = () => {
    setShowResultsPopup(false);
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    const tomorrowStr = tomorrow.toISOString().slice(0, 10);
    const signature = `${tomorrowStr}|${resultsTomorrow.map(e => e.stock_code).sort().join(',')}`;
    try { localStorage.setItem(RESULTS_POPUP_DISMISS_KEY, signature); } catch { /* ignore */ }
  };

  const targetHits   = alerts.filter(a => a.type === 'target_hit');
  const stoplossHits = alerts.filter(a => a.type === 'stoploss_hit');

  return (
    <div className="animate-slide-up">


      {/* ── Target / Stoploss alert banners ── */}
      {stoplossHits.length > 0 && (
        <div style={{
          margin: '12px 0', padding: '14px 18px',
          background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.4)',
          borderRadius: '12px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700, color: '#f87171', marginBottom: '10px' }}>
            <AlertTriangle size={18} />
            {stoplossHits.length} Stoploss Breach{stoplossHits.length > 1 ? 'es' : ''} Across Portfolios
          </div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            {stoplossHits.map((a, i) => (
              <Link key={i} to={`/actual`} style={{ textDecoration: 'none' }}>
                <div style={{
                  background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.3)',
                  borderRadius: '8px', padding: '6px 12px', fontSize: '0.8rem', cursor: 'pointer',
                }}>
                  <strong style={{ color: '#f87171' }}>{a.stock_code}</strong>
                  <span style={{ color: 'var(--text-muted)', marginLeft: '6px', fontSize: '0.73rem' }}>
                    {a.basket_name.replace('NIA ', '')}
                  </span>
                  <div style={{ fontSize: '0.73rem', marginTop: '2px' }}>
                    CMP <strong>₹{a.cmp?.toFixed(2)}</strong>
                    {' · '}SL <strong style={{ color: '#f87171' }}>₹{a.stoploss?.toFixed(2)}</strong>
                    {' · '}<span style={{ color: '#f87171' }}>−{a.pct?.toFixed(1)}% below</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}

      {targetHits.length > 0 && (
        <div style={{
          margin: '12px 0', padding: '14px 18px',
          background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.35)',
          borderRadius: '12px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700, color: 'var(--positive)', marginBottom: '10px' }}>
            <Target size={18} />
            {targetHits.length} Target Hit{targetHits.length > 1 ? 's' : ''} Across Portfolios
          </div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            {targetHits.map((a, i) => (
              <Link key={i} to={`/actual`} style={{ textDecoration: 'none' }}>
                <div style={{
                  background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.25)',
                  borderRadius: '8px', padding: '6px 12px', fontSize: '0.8rem', cursor: 'pointer',
                }}>
                  <strong style={{ color: 'var(--positive)' }}>{a.stock_code}</strong>
                  <span style={{ color: 'var(--text-muted)', marginLeft: '6px', fontSize: '0.73rem' }}>
                    {a.basket_name.replace('NIA ', '')}
                  </span>
                  <div style={{ fontSize: '0.73rem', marginTop: '2px' }}>
                    CMP <strong>₹{a.cmp?.toFixed(2)}</strong>
                    {' · '}Target <strong style={{ color: 'var(--positive)' }}>₹{a.target_price?.toFixed(2)}</strong>
                    {' · '}<span style={{ color: 'var(--positive)' }}>+{a.pct?.toFixed(1)}% above</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* ── Tomorrow's results banner ── */}
      {resultsTomorrow.length > 0 && (
        <div style={{
          margin: '12px 0', padding: '14px 18px',
          background: 'var(--primary-glow)', border: '1px solid var(--primary)',
          borderRadius: '12px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700, color: 'var(--primary)', marginBottom: '10px' }}>
            <Calendar size={18} />
            {resultsTomorrow.length} Stock{resultsTomorrow.length > 1 ? 's' : ''} Reporting Results Tomorrow
          </div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            {resultsTomorrow.map((e, i) => (
              <Link key={i} to="/calendar" style={{ textDecoration: 'none' }}>
                <div style={{
                  background: 'var(--primary-glow)', border: '1px solid var(--primary)',
                  borderRadius: '8px', padding: '6px 12px', fontSize: '0.8rem', cursor: 'pointer',
                }}>
                  <strong style={{ color: 'var(--primary)' }}>{e.stock_code}</strong>
                  <span style={{ color: 'var(--text-muted)', marginLeft: '6px', fontSize: '0.73rem' }}>
                    {e.baskets?.join(', ')}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* ── Tomorrow's results popup ── */}
      {showResultsPopup && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 2000,
          background: 'var(--modal-overlay-bg)', display: 'flex',
          alignItems: 'center', justifyContent: 'center', padding: '1rem',
        }}>
          <div className="glass-panel" style={{
            maxWidth: '440px', width: '100%', padding: '24px', position: 'relative',
          }}>
            <button
              onClick={dismissResultsPopup}
              style={{
                position: 'absolute', top: '14px', right: '14px', background: 'none',
                border: 'none', color: 'var(--text-muted)', cursor: 'pointer', padding: '4px',
              }}
              aria-label="Close"
            >
              <X size={18} />
            </button>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px' }}>
              <div style={{ background: 'var(--primary-glow)', padding: '10px', borderRadius: '10px', display: 'flex' }}>
                <Calendar color="var(--primary)" size={22} />
              </div>
              <h3 style={{ margin: 0, color: 'var(--text-main)' }}>
                Results Tomorrow
              </h3>
            </div>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', lineHeight: 1.6, marginBottom: '14px' }}>
              {resultsTomorrow.length === 1
                ? 'The following stock in your portfolio reports financial results tomorrow:'
                : 'The following stocks in your portfolio report financial results tomorrow:'}
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '18px' }}>
              {resultsTomorrow.map((e, i) => (
                <div key={i} style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  background: 'var(--hover-overlay)', border: '1px solid var(--panel-border)',
                  borderRadius: '8px', padding: '8px 12px',
                }}>
                  <div>
                    <strong style={{ color: 'var(--text-main)' }}>{e.stock_code}</strong>
                    <span style={{ color: 'var(--text-muted)', marginLeft: '8px', fontSize: '0.78rem' }}>
                      {e.baskets?.join(', ')}
                    </span>
                  </div>
                </div>
              ))}
            </div>
            <button
              onClick={dismissResultsPopup}
              className="btn btn-primary"
              style={{ width: '100%', padding: '10px', fontWeight: 600 }}
            >
              OK
            </button>
          </div>
        </div>
      )}

      {/* ── Floating background orbs ── */}
      <div aria-hidden="true" style={{ position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 0, overflow: 'hidden' }}>
        <div style={{
          position: 'absolute', top: '12%', left: '8%',
          width: '420px', height: '420px', borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(99,102,241,0.18) 0%, transparent 70%)',
          animation: 'orbFloat1 12s ease-in-out infinite',
          filter: 'blur(40px)',
        }} />
        <div style={{
          position: 'absolute', top: '45%', right: '6%',
          width: '360px', height: '360px', borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(139,92,246,0.15) 0%, transparent 70%)',
          animation: 'orbFloat2 15s ease-in-out infinite',
          filter: 'blur(50px)',
        }} />
        <div style={{
          position: 'absolute', bottom: '10%', left: '35%',
          width: '300px', height: '300px', borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(16,185,129,0.10) 0%, transparent 70%)',
          animation: 'orbFloat3 18s ease-in-out infinite',
          filter: 'blur(45px)',
        }} />
      </div>

      <div style={{ position: 'relative', zIndex: 1 }}>


        {/* ── Welcome greeting ── */}
        {getEmail() && (
          <div style={{
            animation: 'welcomeSlide 0.6s cubic-bezier(0.22,1,0.36,1) both',
            animationDelay: '0.05s',
            display: 'flex', justifyContent: 'center', marginBottom: '8px',
          }}>
            <span style={{
              fontSize: '0.82rem', color: 'var(--text-muted)',
              background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.2)',
              borderRadius: '20px', padding: '5px 16px', letterSpacing: '0.02em',
            }}>
              Welcome back,&nbsp;
              <strong style={{ color: 'var(--primary)' }}>
                {getFirstName()}
              </strong>
            </span>
          </div>
        )}

        {/* ── Feature cards ── */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
          gap: '24px',
          marginTop: '2.5rem',
        }}>
          <Link to="/actual" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d1">
            <div style={{ background: 'rgba(99,102,241,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(99,102,241,0.2)' }}>
              <TrendingUp color="var(--primary)" size={28} />
            </div>
            <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Actual Portfolio</h2>
            <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
              Live view of stock-level intelligence, top gainers, top losers, and rationale tracking.
            </p>
          </Link>

          <Link to="/simulator" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d2">
            <div style={{ background: 'rgba(16,185,129,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(16,185,129,0.15)' }}>
              <Activity color="var(--positive)" size={28} />
            </div>
            <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Simulator Portfolio</h2>
            <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
              Test hypothetical trades. Modify quantities, buy prices, or add new stocks to see potential outcomes.
            </p>
          </Link>

          <Link to="/screener" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d3">
            <div style={{ background: 'rgba(245,158,11,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(245,158,11,0.12)' }}>
              <Search color="#f59e0b" size={28} />
            </div>
            <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Screener Data</h2>
            <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
              Browse all portfolio stocks and jump directly to Screener.in for deep fundamental analysis on any holding.
            </p>
          </Link>

          <Link to="/comparison" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d4">
            <div style={{ background: 'rgba(99,102,241,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(99,102,241,0.2)' }}>
              <BarChart2 color="var(--primary)" size={28} />
            </div>
            <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Multi-Basket Comparison</h2>
            <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
              Side-by-side historical performance across all NIA baskets with benchmark comparison vs Nifty 50, Nifty 200, and MidSmall index.
            </p>
          </Link>

          <Link to="/calendar" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d5">
            <div style={{ background: 'rgba(16,185,129,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(16,185,129,0.15)' }}>
              <Calendar color="var(--positive)" size={28} />
            </div>
            <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Result Calendar</h2>
            <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
              Track upcoming earnings announcements, board meetings, and corporate actions chronologically for active holdings.
            </p>
          </Link>

          {isAdmin() && (
            <Link to="/admin" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d6">
              <div style={{ background: 'rgba(245,158,11,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(245,158,11,0.12)' }}>
                <ShieldCheck color="#f59e0b" size={28} />
              </div>
              <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Activity Backlog</h2>
              <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
                Admin view of all portfolio changes, user logins, and rebalance uploads — with timestamps and email attribution.
              </p>
            </Link>
          )}

          {isAdmin() && (
            <Link to="/approved-emails" style={{ textDecoration: 'none' }} className="glass-panel home-card home-card-d6">
              <div style={{ background: 'rgba(99,102,241,0.12)', padding: '16px', borderRadius: '12px', display: 'inline-block', marginBottom: '16px', boxShadow: '0 0 20px rgba(99,102,241,0.2)' }}>
                <Users color="var(--primary)" size={28} />
              </div>
              <h2 style={{ color: 'var(--text-main)', marginBottom: '10px' }}>Approved Emails</h2>
              <p style={{ color: 'var(--text-muted)', lineHeight: 1.65 }}>
                Manage which @niveshaay.com email addresses are allowed to log in to the dashboard.
              </p>
            </Link>
          )}
        </div>

      </div>
    </div>
  );
}

export default HomePage;
