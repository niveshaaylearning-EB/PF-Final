import { useEffect, useState, useRef } from 'react';
import RollbackButtons from './RollbackButtons.jsx';

const BASKET_OPTIONS = [
  { key: 'Mid_Small_Cap',        label: 'Mid & Small Cap'      },
  { key: 'Green_Energy',         label: 'Green Energy'         },
  { key: 'IPO_Basket',           label: 'IPO Basket'           },
  { key: 'Trends_Triology',      label: 'Trends Triology'      },
  { key: 'Techstack',            label: 'Techstack'            },
  { key: 'Make_in_India',        label: 'Make in India'        },
  { key: 'Consumer_Trends',      label: 'Consumer Trends'      },
  { key: 'IPO_Recommendations',  label: 'IPO Recommendations'  },
];

export default function Header({
  basketKey, onBasketChange,
  searchTerm, onSearchChange, onSearchClear,
  canUndo, onUndo,
  onBuyPrice, onCalculateReturn, onPLStatement, onCorporateActions,
  readOnly = false,
}) {
  const [dateStr,      setDateStr]      = useState('');
  const [actionsOpen,  setActionsOpen]  = useState(false);
  const actionsRef = useRef(null);

  useEffect(() => {
    setDateStr(new Date().toLocaleDateString('en-IN', { day: 'numeric', month: 'long', year: 'numeric' }));
    const close = e => { if (actionsRef.current && !actionsRef.current.contains(e.target)) setActionsOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  return (
    <header className="db-header">
      {/* Left title */}
      <div className="db-header-title">
        <div className="db-title-row">
          <h1 className="db-title">Actual Portfolio</h1>
          <span className="db-perf-badge">
            <i className="fa-solid fa-chart-line" /> Past 1 Month Trailing Returns
          </span>
        </div>
        <p className="db-subtitle">As on {dateStr}</p>
      </div>

      {/* Right controls */}
      <div className="db-header-controls">
        {/* Search */}
        <div className="search-wrapper">
          <i className="fa-solid fa-magnifying-glass search-icon" />
          <input type="text" className="search-input" placeholder="Find stock…"
            value={searchTerm} onChange={e => onSearchChange(e.target.value)} />
          {searchTerm && (
            <button className="search-clear" onClick={onSearchClear}>
              <i className="fa-solid fa-xmark" />
            </button>
          )}
        </div>

        {/* Undo — hidden for read-only users */}
        {!readOnly && (
          <button className="undo-btn" onClick={onUndo} disabled={!canUndo} title="Undo last change">
            <i className="fa-solid fa-rotate-left" /> Undo
          </button>
        )}

        {/* Basket selector */}
        <select className="db-basket-select" value={basketKey} onChange={e => onBasketChange(e.target.value)}>
          {BASKET_OPTIONS.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>

        {/* Date chip */}
        <div className="date-display">
          <i className="fa-regular fa-calendar" />
          <span>{dateStr}</span>
        </div>

        {/* Rollback */}
        <RollbackButtons btnStyle="header" />

        {/* Portfolio Actions dropdown — edit-only users */}
        {!readOnly && <div className="db-actions-wrap" ref={actionsRef}>
          <button className="db-actions-btn" onClick={() => setActionsOpen(v => !v)}>
            <i className="fa-solid fa-sliders" />
            Portfolio Actions
            <i className={`fa-solid fa-chevron-${actionsOpen ? 'up' : 'down'}`} style={{ fontSize: '0.65rem' }} />
          </button>
          {actionsOpen && (
            <div className="db-actions-menu">
              <button className="db-action-item" onClick={() => { setActionsOpen(false); onBuyPrice(); }}>
                <i className="fa-solid fa-receipt" /> Buy Price Data
              </button>
              <button className="db-action-item" onClick={() => { setActionsOpen(false); onCalculateReturn(); }}>
                <i className="fa-solid fa-chart-line" /> Calculate Return
              </button>
              <button className="db-action-item" onClick={() => { setActionsOpen(false); onPLStatement(); }}>
                <i className="fa-solid fa-file-invoice-dollar" /> P&amp;L Statement
              </button>
              <button className="db-action-item" onClick={() => { setActionsOpen(false); onCorporateActions(); }}>
                <i className="fa-solid fa-code-branch" /> Corporate Actions
              </button>
            </div>
          )}
        </div>}
      </div>
    </header>
  );
}
