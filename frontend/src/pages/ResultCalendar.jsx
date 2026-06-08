import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Calendar, Search, ArrowLeft, AlertCircle, Filter, Loader2 } from 'lucide-react';
import { API_BASE } from '../config.js';

export default function ResultCalendar() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [events, setEvents] = useState([]);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedBasket, setSelectedBasket] = useState('All');

  useEffect(() => {
    axios.get(`${API_BASE}/portfolio/results-calendar`)
      .then(res => {
        setEvents(res.data || []);
        setLoading(false);
      })
      .catch(err => {
        console.error('Error fetching results calendar:', err);
        setError('Failed to load upcoming results. Please try again later.');
        setLoading(false);
      });
  }, []);

  // Get unique basket names for the filter dropdown
  const allBaskets = ['All', ...Array.from(new Set(events.flatMap(e => e.baskets || []))).sort()];

  // Filter events based on search term and selected basket
  const filteredEvents = events.filter(e => {
    const matchesSearch = 
      e.stock_code.toLowerCase().includes(searchTerm.toLowerCase()) || 
      e.stock_name.toLowerCase().includes(searchTerm.toLowerCase());
    
    const matchesBasket = 
      selectedBasket === 'All' || 
      e.baskets.includes(selectedBasket);

    return matchesSearch && matchesBasket;
  });

  const getDaysRemaining = (dateStr) => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const targetDate = new Date(dateStr);
    targetDate.setHours(0, 0, 0, 0);
    const diffTime = targetDate - today;
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
    return diffDays;
  };

  const formatEventDate = (dateStr) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-IN', { 
      weekday: 'long', 
      year: 'numeric', 
      month: 'long', 
      day: 'numeric' 
    });
  };

  return (
    <div className="animate-slide-up" style={{ minHeight: '80vh' }}>
      
      {/* Top Header Navigation */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '24px' }}>
        <button
          className="btn btn-secondary"
          onClick={() => navigate('/')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px' }}
        >
          <ArrowLeft size={18} /> Back
        </button>
        <div>
          <h2 className="text-gradient" style={{ margin: 0, fontSize: '1.6rem' }}>Result Calendar</h2>
        </div>
      </div>

      {/* Main Section */}
      <div className="glass-panel" style={{ padding: '24px', marginBottom: '24px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          
          {/* Section description & Search/Filters bar */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '16px' }}>
            <div>
              <p style={{ color: 'var(--text-muted)', fontSize: '0.92rem', margin: 0 }}>
                Earnings and board meetings scheduled for holdings present in your baskets.
              </p>
            </div>

            {/* Filter controls */}
            <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', width: '100%', maxWidth: '600px', justifyContent: 'flex-end' }}>
              
              {/* Search input */}
              <div style={{ position: 'relative', flex: 1, minWidth: '200px' }}>
                <Search size={16} style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }} />
                <input
                  type="text"
                  placeholder="Search stock code or name..."
                  value={searchTerm}
                  onChange={e => setSearchTerm(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px 8px 36px',
                    fontSize: '0.88rem',
                    boxSizing: 'border-box'
                  }}
                />
              </div>

              {/* Basket filter */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Filter size={16} style={{ color: 'var(--text-muted)' }} />
                <select
                  value={selectedBasket}
                  onChange={e => setSelectedBasket(e.target.value)}
                  style={{
                    padding: '8px 32px 8px 12px',
                    fontSize: '0.88rem',
                  }}
                >
                  {allBaskets.map(b => (
                    <option key={b} value={b}>{b}</option>
                  ))}
                </select>
              </div>

            </div>
          </div>

          {/* Table / List representation */}
          {loading ? (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '60px 0', gap: '12px' }}>
              <Loader2 className="animate-spin" size={32} style={{ color: 'var(--primary)' }} />
              <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>Analyzing portfolio and fetching earnings dates...</span>
            </div>
          ) : error ? (
            <div className="error-alert" style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '16px', background: 'rgba(239, 68, 68, 0.1)', border: '1px solid rgba(239, 68, 68, 0.2)', borderRadius: '8px', color: '#ff8080' }}>
              <AlertCircle size={20} />
              <span>{error}</span>
            </div>
          ) : filteredEvents.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '48px 0', color: 'var(--text-muted)' }}>
              No upcoming results found for the selected filters.
            </div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Stock Name</th>
                    <th>Code</th>
                    <th>Baskets</th>
                    <th>Result Date</th>
                    <th style={{ textAlign: 'right' }}>Remaining Days</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredEvents.map((event, idx) => {
                    const daysLeft = getDaysRemaining(event.date);
                    let badgeColor = 'var(--text-muted)';
                    let badgeBg = 'rgba(255,255,255,0.06)';
                    let badgeBorder = 'rgba(255,255,255,0.12)';

                    if (daysLeft === 0) {
                      badgeColor = 'var(--positive)';
                      badgeBg = 'rgba(16, 185, 129, 0.12)';
                      badgeBorder = 'rgba(16, 185, 129, 0.25)';
                    } else if (daysLeft <= 7) {
                      badgeColor = '#f59e0b';
                      badgeBg = 'rgba(245, 158, 11, 0.12)';
                      badgeBorder = 'rgba(245, 158, 11, 0.25)';
                    } else if (daysLeft <= 30) {
                      badgeColor = '#a5b4fc';
                      badgeBg = 'rgba(99, 102, 241, 0.12)';
                      badgeBorder = 'rgba(99, 102, 241, 0.25)';
                    }

                    return (
                      <tr key={idx} className="hover-row">
                        <td style={{ fontWeight: 500, color: 'var(--text-main)', whiteSpace: 'normal' }}>
                          {event.stock_name}
                        </td>
                        <td style={{ fontFamily: 'monospace', fontWeight: 600 }}>{event.stock_code}</td>
                        <td>
                          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                            {event.baskets.map(b => (
                              <span key={b} style={{
                                fontSize: '0.72rem',
                                padding: '2px 8px',
                                background: 'rgba(255, 255, 255, 0.05)',
                                border: '1px solid rgba(255, 255, 255, 0.08)',
                                borderRadius: '4px',
                                color: 'var(--text-muted)'
                              }}>
                                {b}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td style={{ color: 'var(--text-main)' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <Calendar size={14} style={{ color: 'var(--primary)', opacity: 0.8 }} />
                            <span>{formatEventDate(event.date)}</span>
                          </div>
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          <span style={{
                            display: 'inline-block',
                            fontSize: '0.75rem',
                            fontWeight: 600,
                            padding: '4px 10px',
                            background: badgeBg,
                            border: `1px solid ${badgeBorder}`,
                            borderRadius: '12px',
                            color: badgeColor
                          }}>
                            {daysLeft === 0 ? 'Today!' : daysLeft === 1 ? 'Tomorrow' : `In ${daysLeft} days`}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

        </div>
      </div>

    </div>
  );
}
