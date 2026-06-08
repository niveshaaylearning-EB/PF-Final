import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';

import { API_BASE } from '../config.js';

function colorFor(v) {
  return v >= 0 ? 'var(--positive)' : 'var(--negative)';
}

export default function HistoricComparison({ basketId, refreshKey }) {
  const [actual, setActual] = useState(null);
  const [sim, setSim] = useState(null);
  const [loading, setLoading] = useState(false);

  const key = useMemo(() => refreshKey ?? '', [refreshKey]);

  useEffect(() => {
    if (!basketId || basketId === 'all') return;
    const run = async () => {
      setLoading(true);
      try {
        const res = await axios.get(`${API_BASE}/simulator/${basketId}/historic`);
        setActual(res.data?.actual || {});
        setSim(res.data?.simulated || {});
      } catch (e) {
        console.error(e);
        setActual(null);
        setSim(null);
      }
      setLoading(false);
    };
    run();
  }, [basketId, key]);

  if (loading) {
    return (
      <div style={{ color: 'var(--text-muted)', marginBottom: '24px' }}>
        Calculating historical comparison... (this may take a few seconds)
      </div>
    );
  }

  if (!actual || !sim) return null;
  if (Object.keys(actual).length === 0 && Object.keys(sim).length === 0) return null;

  const periods = ['1M', '6M', '1Y', '3Y', '5Y'];
  const cagrPeriods = ['1Y', '3Y', '5Y'];
  const hasAnyCagr = cagrPeriods.some(p => actual[p]?.cagr != null || sim[p]?.cagr != null);

  return (
    <div className="glass-panel" style={{ marginBottom: '24px', padding: '24px 28px' }}>
      <h3 style={{ 
        marginBottom: '20px', 
        color: 'var(--text-main)', 
        fontSize: '1.25rem', 
        fontWeight: 600,
        borderBottom: '1px solid rgba(255,255,255,0.08)',
        paddingBottom: '12px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between'
      }}>
        <span>Historical Performance Comparison</span>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 400 }}>
          Actual vs Simulated Portfolios
        </span>
      </h3>
      
      {/* Section 1: Cumulative Net Returns */}
      <div style={{ marginBottom: '24px' }}>
        <h4 style={{ 
          color: 'var(--text-muted)', 
          fontSize: '0.82rem', 
          fontWeight: 600, 
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          marginBottom: '14px' 
        }}>
          Cumulative Net Returns
        </h4>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '16px' }}>
          {periods.map((p) => {
            const a = actual[p];
            const s = sim[p];
            if (!a && !s) return null;

            const aNet = a?.net ?? null;
            const sNet = s?.net ?? null;
            const dNet = aNet != null && sNet != null ? (sNet - aNet) : null;

            return (
              <div 
                key={p} 
                className="hist-card" 
                style={{ 
                  background: 'rgba(255,255,255,0.02)', 
                  border: '1px solid rgba(255,255,255,0.05)', 
                  padding: '16px 20px', 
                  borderRadius: '12px', 
                  transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)' 
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
                  <span style={{ fontSize: '1.15rem', fontWeight: 700, color: '#a5b4fc' }}>{p}</span>
                  <span style={{ 
                    fontSize: '0.65rem', 
                    color: 'var(--primary)', 
                    fontWeight: 600,
                    textTransform: 'uppercase', 
                    background: 'rgba(99,102,241,0.12)', 
                    padding: '2px 8px', 
                    borderRadius: '20px',
                    border: '1px solid rgba(99,102,241,0.2)'
                  }}>
                    Simple
                  </span>
                </div>
                
                <div style={{ display: 'grid', gap: '10px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>Actual</span>
                    <strong style={{ color: aNet == null ? 'var(--text-muted)' : colorFor(aNet), fontSize: '0.85rem' }}>
                      {aNet == null ? '—' : `${aNet >= 0 ? '+' : ''}${aNet.toFixed(2)}%`}
                    </strong>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>Simulated</span>
                    <strong style={{ color: sNet == null ? 'var(--text-muted)' : colorFor(sNet), fontSize: '0.85rem' }}>
                      {sNet == null ? '—' : `${sNet >= 0 ? '+' : ''}${sNet.toFixed(2)}%`}
                    </strong>
                  </div>
                  <div style={{ 
                    display: 'flex', 
                    justifyContent: 'space-between', 
                    alignItems: 'center',
                    borderTop: '1px solid rgba(255,255,255,0.08)', 
                    paddingTop: '10px',
                    marginTop: '2px'
                  }}>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem', fontWeight: 500 }}>Difference</span>
                    <strong style={{ color: dNet == null ? 'var(--text-muted)' : colorFor(dNet), fontSize: '0.88rem', fontWeight: 700 }}>
                      {dNet == null ? '—' : `${dNet >= 0 ? '+' : ''}${dNet.toFixed(2)}%`}
                    </strong>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Section 2: Annualized CAGR */}
      {hasAnyCagr && (
        <div>
          <h4 style={{ 
            color: 'var(--text-muted)', 
            fontSize: '0.82rem', 
            fontWeight: 600, 
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            marginBottom: '14px',
            marginTop: '28px'
          }}>
            Annualized Returns (CAGR)
          </h4>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '16px' }}>
            {cagrPeriods.map((p) => {
              const a = actual[p];
              const s = sim[p];
              if (!a && !s) return null;

              const aCagr = a?.cagr ?? null;
              const sCagr = s?.cagr ?? null;
              const dCagr = aCagr != null && sCagr != null ? (sCagr - aCagr) : null;
              
              if (aCagr == null && sCagr == null) return null;

              return (
                <div 
                  key={p} 
                  className="hist-card" 
                  style={{ 
                    background: 'rgba(255,255,255,0.02)', 
                    border: '1px solid rgba(255,255,255,0.05)', 
                    padding: '16px 20px', 
                    borderRadius: '12px', 
                    transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)' 
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
                    <span style={{ fontSize: '1.15rem', fontWeight: 700, color: '#c4b5fd' }}>{p}</span>
                    <span style={{ 
                      fontSize: '0.65rem', 
                      color: 'var(--secondary)', 
                      fontWeight: 600,
                      textTransform: 'uppercase', 
                      background: 'rgba(139,92,246,0.12)', 
                      padding: '2px 8px', 
                      borderRadius: '20px',
                      border: '1px solid rgba(139,92,246,0.2)'
                    }}>
                      Annualized
                    </span>
                  </div>
                  
                  <div style={{ display: 'grid', gap: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>Actual</span>
                      <strong style={{ color: aCagr == null ? 'var(--text-muted)' : colorFor(aCagr), fontSize: '0.85rem' }}>
                        {aCagr == null ? '—' : `${aCagr >= 0 ? '+' : ''}${aCagr.toFixed(2)}%`}
                      </strong>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>Simulated</span>
                      <strong style={{ color: sCagr == null ? 'var(--text-muted)' : colorFor(sCagr), fontSize: '0.85rem' }}>
                        {sCagr == null ? '—' : `${sCagr >= 0 ? '+' : ''}${sCagr.toFixed(2)}%`}
                      </strong>
                    </div>
                    <div style={{ 
                      display: 'flex', 
                      justifyContent: 'space-between', 
                      alignItems: 'center',
                      borderTop: '1px solid rgba(255,255,255,0.08)', 
                      paddingTop: '10px',
                      marginTop: '2px'
                    }}>
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem', fontWeight: 500 }}>Difference</span>
                      <strong style={{ color: dCagr == null ? 'var(--text-muted)' : colorFor(dCagr), fontSize: '0.88rem', fontWeight: 700 }}>
                        {dCagr == null ? '—' : `${dCagr >= 0 ? '+' : ''}${dCagr.toFixed(2)}%`}
                      </strong>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <style>{`
        .hist-card:hover {
          transform: translateY(-3px);
          border-color: rgba(99, 102, 241, 0.22) !important;
          background: rgba(255, 255, 255, 0.04) !important;
          box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25);
        }
      `}</style>
    </div>
  );
}
