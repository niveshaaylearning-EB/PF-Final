import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { TrendingUp, Activity, Search, BarChart2, AlertTriangle, Target, ShieldCheck, Calendar, Users } from 'lucide-react';
import axios from 'axios';
import { isAdmin, getEmail, getFirstName } from '../utils/auth';

import { API_BASE } from '../config.js';


function HomePage() {
  const [alerts, setAlerts] = useState([]);

  useEffect(() => {
    axios.get(`${API_BASE}/alerts`).then(r => setAlerts(r.data || [])).catch(() => {});
  }, []);

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
              <strong style={{ color: '#a5b4fc' }}>
                {getFirstName()}
              </strong>
            </span>
          </div>
        )}

        {/* ── Hero title ── */}
        <div style={{ textAlign: 'center', margin: '2.5rem 0 3.5rem' }}>
          <h1 style={{
            fontSize: 'clamp(2.4rem, 5vw, 3.8rem)',
            fontWeight: 700,
            background: 'linear-gradient(135deg, #a5b4fc 0%, #c4b5fd 35%, #6ee7b7 70%, #a5b4fc 100%)',
            backgroundSize: '250% 250%',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
            backgroundClip: 'text',
            animation: 'titleReveal 0.8s cubic-bezier(0.22,1,0.36,1) 0.1s both, gradientDrift 6s ease-in-out infinite 1s',
            marginBottom: '1rem',
          }}>
            Portfolio Intelligence
          </h1>
          <p style={{
            color: 'var(--text-muted)', fontSize: '1.1rem',
            maxWidth: '560px', margin: '0 auto', lineHeight: 1.7,
            animation: 'subtitleFade 0.7s ease 0.45s both',
          }}>
            Select a module below to analyze performance, track intelligence, or simulate scenario models.
          </p>
        </div>

        {/* ── Feature cards ── */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
          gap: '24px',
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
