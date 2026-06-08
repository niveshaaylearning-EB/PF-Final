import { useState, useEffect, useMemo } from 'react';
import axios from 'axios';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Download, Plus, Edit2, RotateCcw, X, Target, Filter, ArrowLeft } from 'lucide-react';
import AutoCompleteInput from '../components/AutoCompleteInput';
import HistoricComparison from '../components/HistoricComparison';

import { API_BASE } from '../config.js';

const PERIOD_OPTIONS = ['1W', '1M', '3M', '6M', '1Y'];

function SimulatorPortfolio() {
  const [baskets, setBaskets] = useState({});
  const [selectedId, setSelectedId] = useState('all');
  const [loading, setLoading] = useState(true);
  const [mods, setMods] = useState([]);
  const [sips, setSips] = useState([]);
  const [simReturnData, setSimReturnData] = useState(null);
  const [calculatingReturn, setCalculatingReturn] = useState(false);
  const [sipModalOpen, setSipModalOpen] = useState(false);
  const [newSip, setNewSip] = useState({ sip_date: '', amount: 0 });
  const [sipExpandedIdx, setSipExpandedIdx] = useState(null);

  // Sorting & Filtering
  const [filters, setFilters] = useState({ code: '', theme: '', performance: '' });
  const [sortConfig, setSortConfig] = useState({ key: 'performance', direction: 'desc' });
  const [showFilters, setShowFilters] = useState(false);

  // Modal State
  const [modalOpen, setModalOpen] = useState(false);
  const [editMod, setEditMod] = useState(null);
  const [modalDate, setModalDate] = useState('');
  const [allocationError, setAllocationError] = useState('');
  const [histRefreshKey, setHistRefreshKey] = useState(0);
  const [fetchingPrice, setFetchingPrice] = useState(false);
  const [exportModalOpen, setExportModalOpen] = useState(false);


  // Universal confirmation dialog
  const [confirmDialog, setConfirmDialog] = useState({
    open: false, title: '', message: '', confirmText: 'Confirm',
    confirmStyle: {}, onConfirm: null, working: false,
  });

  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const period = searchParams.get('period') || '1M';
  const setPeriod = (p) => setSearchParams(prev => { prev.set('period', p); return prev; }, { replace: true });

  // Basket-level period return from webportal index-history
  const [allBasketPeriodReturns, setAllBasketPeriodReturns] = useState({});
  const [fetchingPeriod, setFetchingPeriod] = useState(false);

  useEffect(() => {
    fetchBaskets();
  }, []);

  useEffect(() => {
    if (selectedId !== 'all') {
      fetchMods(selectedId);
    }
  }, [selectedId]);

  // Fetch basket-level period returns from webportal index-history whenever period changes
  useEffect(() => {
    setFetchingPeriod(true);
    axios.get(`${API_BASE}/basket-period-returns?period=${period}`)
      .then(r => setAllBasketPeriodReturns(r.data || {}))
      .catch(() => {})
      .finally(() => setFetchingPeriod(false));
  }, [period]);

  const fetchBaskets = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/baskets`);
      const data = res.data || {};
      setBaskets(data);
      if (Object.keys(data).length > 0) setSelectedId(Object.keys(data)[0]);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  const fetchMods = async (basketId) => {
    try {
      const [modsRes, sipsRes] = await Promise.all([
        axios.get(`${API_BASE}/simulator/${basketId}`),
        axios.get(`${API_BASE}/simulator/${basketId}/sips`),
      ]);
      setMods(modsRes.data || []);
      setSips(sipsRes.data || []);
    } catch (e) {
      console.error(e);
    }
  };

  const handleExportExcel = async () => {
    if (!selectedId || !baskets[selectedId]) return;
    setExportModalOpen(false);
    try {
      let historicData = {};
      try {
        const hRes = await axios.get(`${API_BASE}/simulator/${selectedId}/historic`);
        historicData = hRes.data || {};
      } catch (_) {}

      const _simRetVal = simReturnData ? simReturnData.absolute_return : simulatedPortfolio.basket_return;
      const payload = {
        basket_name:   baskets[selectedId].name,
        actual_return: baskets[selectedId].stats.basket_return,
        sim_return:    _simRetVal,
        alpha:         _simRetVal - baskets[selectedId].stats.basket_return,
        holdings:      simulatedPortfolio.holdings,
        historic:      historicData,
      };
      const res = await axios.post(`${API_BASE}/download/simulator-full`, payload, { responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `${baskets[selectedId].name}_Simulated.xlsx`);
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (e) {
      console.error('Download failed', e);
    }
  };

  const handleExportPDF = () => {
    setExportModalOpen(false);
    setTimeout(() => window.print(), 150);
  };

  const saveMod = async () => {
    try {
      await axios.post(`${API_BASE}/simulator/${selectedId}`, editMod);
      setModalOpen(false);
      fetchMods(selectedId);
      setHistRefreshKey(k => k + 1);
    } catch(e) {
      console.error(e);
    }
  };

  const removeMod = async (stockCode) => {
    try {
      await axios.post(`${API_BASE}/simulator/${selectedId}`, { stock_code: stockCode, override_type: 'remove' });
      fetchMods(selectedId);
    } catch (e) {
      console.error(e);
    }
  };

  const resetMods = async () => {
    try {
      await axios.post(`${API_BASE}/simulator/${selectedId}/reset`);
      fetchMods(selectedId);
      setHistRefreshKey(k => k + 1);
    } catch (e) {
      console.error(e);
    }
  };

  const saveSip = async () => {
    if (!newSip.sip_date || newSip.amount <= 0) return;
    try {
      await axios.post(`${API_BASE}/simulator/${selectedId}/sips`, newSip);
      setSipModalOpen(false);
      setNewSip({ sip_date: '', amount: 0 });
      fetchMods(selectedId);
    } catch (e) { console.error(e); }
  };

  const removeSip = async (sipId) => {
    try {
      await axios.delete(`${API_BASE}/simulator/${selectedId}/sips/${sipId}`);
      fetchMods(selectedId);
    } catch (e) { console.error(e); }
  };

  const deleteModStock = async (stockCode) => {
    try {
      await axios.post(`${API_BASE}/simulator/${selectedId}`, { stock_code: stockCode, override_type: 'delete' });
      fetchMods(selectedId);
    } catch (e) { console.error(e); }
  };

  const showConfirm = (title, message, confirmText, confirmStyle, onConfirm) => {
    setConfirmDialog({ open: true, title, message, confirmText, confirmStyle, onConfirm, working: false });
  };

  const runConfirmed = async () => {
    setConfirmDialog(d => ({ ...d, working: true }));
    try {
      await confirmDialog.onConfirm();
    } finally {
      setConfirmDialog(d => ({ ...d, open: false, working: false }));
    }
  };

  const confirmSaveMod = () => {
    const isAdd = editMod?.override_type === 'add';
    
    // Calculate new total allocation
    const code = editMod?.stock_code?.toUpperCase()?.trim();
    const newAlloc = parseFloat(editMod?.allocation || 0);
    
    // Find if the stock already exists in the current simulated holdings
    const existingHolding = simulatedPortfolio?.holdings?.find(h => h.code === code);
    const oldAlloc = existingHolding ? parseFloat(existingHolding.allocation || 0) : 0;
    
    // Calculate what the new total allocation would be
    const currentTotal = simulatedPortfolio?.total_allocation || 0;
    const projectedTotal = currentTotal - oldAlloc + newAlloc;
    
    if (projectedTotal > 100) {
      setAllocationError(`Overall allocation cannot exceed 100%. Current total would be ${projectedTotal.toFixed(1)}%. Please reduce the allocation.`);
      return;
    }

    setModalOpen(false);

    showConfirm(
      isAdd ? 'Add Stock to Simulator' : `Modify ${editMod?.stock_code}`,
      isAdd
        ? `Add ${editMod?.stock_code || 'stock'} to the simulated portfolio?`
        : `Apply changes to ${editMod?.stock_code} in the simulated portfolio?`,
      isAdd ? 'Yes, Add' : 'Yes, Modify',
      { background: 'rgba(99,102,241,0.2)', border: '1px solid rgba(99,102,241,0.4)', color: 'var(--primary)', fontWeight: 700 },
      saveMod
    );
  };

  const confirmRemoveMod = (stockCode) => {
    showConfirm(
      'Revert Override',
      `Revert ${stockCode} back to its actual portfolio values?`,
      'Yes, Revert',
      { background: 'rgba(239,68,68,0.2)', border: '1px solid rgba(239,68,68,0.4)', color: '#f87171', fontWeight: 700 },
      () => removeMod(stockCode)
    );
  };

  const confirmDeleteStock = (stockCode) => {
    showConfirm(
      'Delete Stock',
      `Remove ${stockCode} entirely from the simulated portfolio?`,
      'Yes, Delete',
      { background: 'rgba(239,68,68,0.2)', border: '1px solid rgba(239,68,68,0.4)', color: '#f87171', fontWeight: 700 },
      () => deleteModStock(stockCode)
    );
  };

  const confirmResetAll = () => {
    showConfirm(
      'Reset All Overrides',
      'Remove all modifications, added stocks, weight changes, and SIPs — and restore the simulator to match the actual portfolio?',
      'Yes, Reset All',
      { background: 'rgba(239,68,68,0.2)', border: '1px solid rgba(239,68,68,0.4)', color: '#f87171', fontWeight: 700 },
      resetMods
    );
  };

  const simulatedPortfolio = useMemo(() => {
    if (!selectedId || !baskets[selectedId]) return null;
    const base = baskets[selectedId];

    let simHoldings = base.holdings.map(h => ({ ...h, isModded: false }));

    mods.forEach(mod => {
      if (mod.override_type === 'modify') {
        const idx = simHoldings.findIndex(h => h.code === mod.stock_code);
        if (idx !== -1) {
          if (mod.allocation !== null) simHoldings[idx].allocation = mod.allocation;
          if (mod.buy_price !== null) simHoldings[idx].buy_price = mod.buy_price;
          if (mod.cmp !== null) simHoldings[idx].cmp = mod.cmp;
          const perf = ((simHoldings[idx].cmp - simHoldings[idx].buy_price) / simHoldings[idx].buy_price) * 100;
          simHoldings[idx].performance = isNaN(perf) ? 0 : perf;
          simHoldings[idx].isModded = true;
        }
      } else if (mod.override_type === 'add') {
        const bp = mod.buy_price || 0;
        const cmp = mod.cmp || 0;
        const perf = bp > 0 ? ((cmp - bp)/bp)*100 : 0;
        simHoldings.push({
          code: mod.stock_code,
          formula: 'Custom',
          theme: baskets[selectedId]?.name?.replace(/^NIA\s*/,'').trim() || 'Custom',
          allocation: mod.allocation || 0,
          buy_price: bp,
          cmp: cmp,
          performance: perf,
          contribution: (perf * (mod.allocation || 0))/100,
          isModded: true,
          isNew: true
        });
      } else if (mod.override_type === 'delete') {
        simHoldings = simHoldings.filter(h => h.code !== mod.stock_code);
      }
    });

    let totalAlloc = simHoldings.reduce((sum, h) => sum + h.allocation, 0);
    let basketReturn = 0;
    if (totalAlloc > 0) {
      basketReturn = simHoldings.reduce((sum, h) => sum + (h.performance * h.allocation), 0) / totalAlloc;
    } else if (simHoldings.length > 0) {
      basketReturn = simHoldings.reduce((sum, h) => sum + h.performance, 0) / simHoldings.length;
    }

    return { holdings: simHoldings, basket_return: basketReturn, total_allocation: totalAlloc };
  }, [baskets, selectedId, mods]);

  useEffect(() => {
    if (!simulatedPortfolio || !simulatedPortfolio.holdings.length) {
       setSimReturnData(null);
       return;
    }
    let cancelled = false;
    const compute = async () => {
       setCalculatingReturn(true);
       try {
         const res = await axios.post(`${API_BASE}/simulator/calculate-return`, {
           holdings: simulatedPortfolio.holdings,
           sips: sips
         });
         if (!cancelled) {
           setSimReturnData(res.data);
         }
       } catch (err) { console.error(err); }
       if (!cancelled) setCalculatingReturn(false);
    };
    compute();
    return () => { cancelled = true; };
  }, [simulatedPortfolio, sips]);

  // Auto-fetch Buy Price + CMP whenever stock code or date changes in the Add modal.
  // Uses a 600ms debounce and cancels stale requests to avoid race conditions
  // (AutoCompleteInput calls onChange on every keystroke, so without this the "T"
  //  and "TC" responses could overwrite the correct "TCS" response).
  const modalCode = editMod?.override_type === 'add' ? (editMod?.stock_code || '') : '';
  useEffect(() => {
    if (!modalCode || modalCode.length < 2) return;

    let cancelled = false;
    setFetchingPrice(true);

    const timer = setTimeout(async () => {
      const today = new Date().toISOString().split('T')[0];
      try {
        const cmpRes = await axios.get(`${API_BASE}/stocks/history?code=${modalCode}&date=${today}`);
        if (!cancelled && cmpRes.data.price > 0) {
          setEditMod(prev => ({ ...prev, cmp: cmpRes.data.price }));
        }
      } catch (err) { console.error('CMP fetch error', err); }

      if (modalDate) {
        try {
          const bpRes = await axios.get(`${API_BASE}/stocks/history?code=${modalCode}&date=${modalDate}`);
          if (!cancelled && bpRes.data.price > 0) {
            setEditMod(prev => ({ ...prev, buy_price: bpRes.data.price }));
          }
        } catch (err) { console.error('Buy price fetch error', err); }
      }

      if (!cancelled) setFetchingPrice(false);
    }, 600);

    return () => { cancelled = true; clearTimeout(timer); };
  }, [modalCode, modalDate]);

  if (loading) return <div style={{ textAlign: 'center', marginTop: '4rem' }}><h3 className="text-gradient">Loading Simulator...</h3></div>;
  if (!simulatedPortfolio) return null;

  // Match selected PF basket to webportal basket by name similarity
  const pfBasketShortName = (baskets[selectedId]?.name || '').replace(/^NIA\s*/i, '').trim().toLowerCase();
  const basketPeriodReturn = Object.values(allBasketPeriodReturns).find(wb => {
    const wn = (wb.name || '').toLowerCase();
    return wn === pfBasketShortName || wn.includes(pfBasketShortName) || pfBasketShortName.includes(wn) ||
      pfBasketShortName.split(/\s+/).filter(w => w.length > 2).every(w => wn.includes(w));
  }) || null;

  const actualReturn = baskets[selectedId].stats.basket_return;
  const simReturn = simulatedPortfolio.basket_return;
  // Use the same value shown on the Simulated Return card for the alpha diff.
  // simReturnData.absolute_return is the displayed simulated return (10L + SIPs model);
  // fall back to the useMemo weighted return while that API call is still in flight.
  const displayedSimReturn = simReturnData ? simReturnData.absolute_return : simReturn;
  const diff = displayedSimReturn - actualReturn;

  const handleEditClick = (h) => {
    setEditMod({
      basket_id: selectedId,
      stock_code: h.code,
      override_type: h.isNew ? 'add' : 'modify',
      allocation: h.allocation,
      buy_price: h.buy_price,
      cmp: h.cmp
    });
    setModalDate('');
    setAllocationError('');
    setModalOpen(true);
  };

  const handleAddNew = () => {
    setEditMod({
      basket_id: selectedId,
      stock_code: '',
      override_type: 'add',
      allocation: 0,
      buy_price: 0,
      cmp: 0
    });
    setModalDate('');
    setAllocationError('');
    setModalOpen(true);
  };

  const handleStockCodeChange = (val) => {
    setEditMod(prev => ({ ...prev, stock_code: val }));
  };

  const handleDateChange = (dateVal) => {
    setModalDate(dateVal);
  };

  const handleSort = (key) => {
    setSortConfig(prev => ({
      key,
      direction: prev.key === key && prev.direction === 'desc' ? 'asc' : 'desc',
    }));
  };

  const SortIcon = ({ colKey }) => {
    if (sortConfig.key !== colKey) return <span style={{ opacity: 0.3, marginLeft: '4px' }}>⇅</span>;
    return <span style={{ marginLeft: '4px', color: 'var(--primary)' }}>{sortConfig.direction === 'asc' ? '↑' : '↓'}</span>;
  };

  const getFilteredSorted = (holdings) => {
    let result = [...holdings];

    if (filters.code.trim()) {
      const q = filters.code.trim().toLowerCase();
      result = result.filter(h => h.code && h.code.toLowerCase().includes(q));
    }
    if (filters.theme.trim()) {
      const q = filters.theme.trim().toLowerCase();
      result = result.filter(h => h.theme && h.theme.toLowerCase().includes(q));
    }
    if (filters.performance.trim()) {
      const threshold = parseFloat(filters.performance);
      if (!isNaN(threshold)) {
        result = result.filter(h => Number(h.performance) >= threshold);
      }
    }

    result.sort((a, b) => {
      let aVal = a[sortConfig.key];
      let bVal = b[sortConfig.key];
      if (typeof aVal === 'string') aVal = aVal.toLowerCase();
      if (typeof bVal === 'string') bVal = bVal.toLowerCase();
      if (aVal < bVal) return sortConfig.direction === 'asc' ? -1 : 1;
      if (aVal > bVal) return sortConfig.direction === 'asc' ? 1 : -1;
      return 0;
    });
    return result;
  };

  const activeFilterCount = Object.values(filters).filter(v => v.trim()).length;

  return (
    <div className="animate-slide-up">
      <div style={{ marginBottom: '16px' }}>
        <button className="btn btn-secondary" onClick={() => navigate(-1)} style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
          <ArrowLeft size={16} /> Back
        </button>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div>
          <h2 style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Target color="var(--secondary)" /> Simulator
          </h2>
          {/* Period selector — above basket dropdown */}
          <div style={{ display: 'flex', gap: '2px', background: 'rgba(255,255,255,0.05)', borderRadius: '8px', padding: '3px', marginBottom: '8px', width: 'fit-content' }}>
            {PERIOD_OPTIONS.map(p => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                style={{
                  padding: '3px 10px', fontSize: '0.75rem', fontWeight: period === p ? 700 : 400,
                  background: period === p ? 'rgba(99,102,241,0.35)' : 'transparent',
                  color: period === p ? '#a5b4fc' : 'var(--text-muted)',
                  border: 'none', borderRadius: '6px', cursor: 'pointer', transition: 'all 0.15s',
                }}
              >
                {p}
              </button>
            ))}
          </div>
          <select
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            style={{ minWidth: '250px' }}
          >
            {Object.values(baskets).map(b => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '20px', marginBottom: '24px' }}>
        <div className="glass-panel" style={{ textAlign: 'center' }}>
          <p className="text-muted" style={{ marginBottom: '8px' }}>Actual Return</p>
          <h1 className={actualReturn >= 0 ? "text-gradient" : ""} style={{ color: actualReturn < 0 ? 'var(--negative)' : 'inherit', margin: 0 }}>
            {actualReturn > 0 ? '+' : ''}{actualReturn.toFixed(2)}%
          </h1>
        </div>

        <div className="glass-panel" style={{ textAlign: 'center', border: '1px solid var(--primary)', background: 'var(--primary-glow)' }}>
          <p className="text-muted" style={{ marginBottom: '8px', color: 'var(--text-main)' }}>Simulated Return</p>
          <h1 style={{ color: 'var(--text-main)', margin: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            {calculatingReturn ? <RotateCcw size={24} style={{ animation: 'spin 1s linear infinite' }}/> : <>{simReturnData ? (simReturnData.absolute_return > 0 ? '+' : '') + simReturnData.absolute_return.toFixed(2) + '%' : '--'}</>}
          </h1>
          {simReturnData && !calculatingReturn && (
            <div style={{ fontSize: '0.75rem', marginTop: '8px', color: 'rgba(255,255,255,0.7)' }}>
               Invested: ₹{(simReturnData.total_invested).toLocaleString('en-IN')} &nbsp;|&nbsp;
               Current: ₹{(simReturnData.current_value).toLocaleString('en-IN')}
            </div>
          )}
          {simReturnData?.sip_details?.length > 0 && !calculatingReturn && (
            <button
              onClick={() => setSipModalOpen(true)}
              style={{ marginTop: '8px', fontSize: '0.7rem', background: 'rgba(99,102,241,0.15)', border: '1px solid rgba(99,102,241,0.3)', color: '#a5b4fc', borderRadius: '6px', padding: '3px 10px', cursor: 'pointer' }}
            >
              View SIP Breakdown →
            </button>
          )}
        </div>

        <div className="glass-panel" style={{ textAlign: 'center' }}>
          <p className="text-muted" style={{ marginBottom: '8px' }}>Alpha Difference</p>
          <h1 style={{ color: diff >= 0 ? 'var(--positive)' : 'var(--negative)', margin: 0 }}>
            {calculatingReturn
              ? <RotateCcw size={24} style={{ animation: 'spin 1s linear infinite' }} />
              : <>{diff > 0 ? '+' : ''}{diff.toFixed(2)}%</>}
          </h1>
        </div>

        <div className="glass-panel" style={{ textAlign: 'center', border: '1px solid rgba(99,102,241,0.3)' }}>
          <p className="text-muted" style={{ marginBottom: '8px' }}>{period} Return</p>
          <h1 style={{ margin: 0, color: basketPeriodReturn ? (basketPeriodReturn.net >= 0 ? 'var(--positive)' : 'var(--negative)') : 'var(--text-muted)' }}>
            {fetchingPeriod ? <RotateCcw size={24} style={{ animation: 'spin 1s linear infinite' }} />
              : basketPeriodReturn
                ? <>{basketPeriodReturn.net > 0 ? '+' : ''}{basketPeriodReturn.net.toFixed(2)}%</>
                : '—'}
          </h1>
          {basketPeriodReturn && (
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '6px' }}>
              {basketPeriodReturn.base_date} → {basketPeriodReturn.latest_date}
            </div>
          )}
        </div>
      </div>

      <HistoricComparison basketId={selectedId} refreshKey={histRefreshKey} />

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <h3 style={{ margin: 0 }}>Simulated Data Engine</h3>
          <button className="btn btn-primary" onClick={handleAddNew} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Plus size={16}/> Add Stock
          </button>
          <button className="btn btn-secondary" onClick={() => setSipModalOpen(true)} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Target size={16}/> Manage SIPs ({sips.length})
          </button>
          <button className="btn btn-secondary no-print" onClick={() => setExportModalOpen(true)} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Download size={16}/> Export
          </button>
          <button className="btn btn-secondary" onClick={confirmResetAll} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <RotateCcw size={16}/> Reset All
          </button>
        </div>
        <button
          className={`btn ${activeFilterCount > 0 ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setShowFilters(f => !f)}
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <Filter size={16} />
          {showFilters ? 'Hide Filters' : 'Show Filters'}
          {activeFilterCount > 0 && (
            <span style={{
              background: 'var(--positive)', color: '#fff',
              borderRadius: '50%', width: '18px', height: '18px',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.7rem', fontWeight: 700
            }}>{activeFilterCount}</span>
          )}
        </button>
      </div>

      <div className="table-wrapper" style={{ fontSize: '0.78rem' }}>
        <table style={{ borderCollapse: 'collapse', tableLayout: 'auto' }}>
          <thead>
            {/* Sort Headers */}
            <tr>
              <th style={{ whiteSpace: 'nowrap', padding: '6px 8px', color: 'var(--text-muted)', fontWeight: 500 }}>#</th>
              <th style={{ whiteSpace: 'nowrap', padding: '6px 8px' }}>Status</th>
              <th
                onClick={() => handleSort('code')}
                style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '6px 8px', position: 'sticky', left: 0, background: 'var(--surface)', zIndex: 2 }}
              >
                Stock<SortIcon colKey="code" />
              </th>

              <th
                onClick={() => handleSort('allocation')}
                style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '6px 8px' }}
              >
                Alloc%<SortIcon colKey="allocation" />
              </th>
              <th
                onClick={() => handleSort('buy_price')}
                style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '6px 8px', minWidth: '80px' }}
              >
                BuyPx<SortIcon colKey="buy_price" />
              </th>
              <th
                onClick={() => handleSort('cmp')}
                style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '6px 8px' }}
              >
                CMP<SortIcon colKey="cmp" />
              </th>
              <th
                onClick={() => handleSort('performance')}
                style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap', padding: '6px 8px' }}
              >
                SimRet%<SortIcon colKey="performance" />
              </th>
              <th style={{ padding: '6px 8px' }}>Act.</th>
            </tr>

            {/* Filter Row */}
            {showFilters && (
              <tr style={{ background: 'rgba(99,102,241,0.08)' }}>
                <th />
                <th />
                <th style={{ padding: '6px 8px' }}>
                  <input
                    type="text"
                    placeholder="Filter stock…"
                    value={filters.code}
                    onChange={e => setFilters(f => ({ ...f, code: e.target.value }))}
                    style={{ width: '100%', padding: '4px 8px', fontSize: '0.8rem' }}
                  />
                </th>

                <th />
                <th />
                <th />
                <th style={{ padding: '6px 8px' }}>
                  <input
                    type="number"
                    placeholder="Min % return"
                    value={filters.performance}
                    onChange={e => setFilters(f => ({ ...f, performance: e.target.value }))}
                    style={{ width: '100%', padding: '4px 8px', fontSize: '0.8rem' }}
                  />
                </th>
                <th style={{ padding: '6px 8px' }}>
                  <button
                    className="btn"
                    style={{ padding: '4px 8px', fontSize: '0.75rem', width: '100%' }}
                    onClick={() => setFilters({ code: '', theme: '', performance: '' })}
                  >
                    Clear
                  </button>
                </th>
              </tr>
            )}
          </thead>
          <tbody>
            {getFilteredSorted(simulatedPortfolio.holdings).map((h, idx) => (
              <tr key={h.code} style={{ background: h.isModded ? (h.isNew ? 'rgba(99, 102, 241, 0.15)' : 'rgba(255, 255, 255, 0.08)') : 'transparent' }}>
                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', color: 'var(--text-muted)', fontSize: '0.72rem', textAlign: 'right' }}>
                  {idx + 1}
                </td>
                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>
                  {h.isNew && <span className="badge positive" style={{ fontSize: '0.7rem', padding: '1px 6px' }}>Added</span>}
                  {h.isModded && !h.isNew && <span className="badge" style={{ background: 'var(--primary)', color: '#fff', fontSize: '0.7rem', padding: '1px 6px' }}>Modified</span>}
                  {!h.isModded && <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>—</span>}
                </td>
                <td style={{ padding: '5px 8px', position: 'sticky', left: 0, background: h.isModded ? (h.isNew ? 'rgba(30,30,80,0.98)' : 'rgba(30,30,50,0.98)') : 'var(--surface)', zIndex: 1, whiteSpace: 'nowrap' }}>
                  <strong style={{ fontSize: '0.8rem' }}>{h.code}</strong>
                </td>

                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{h.allocation.toFixed(1)}%</td>
                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', minWidth: '80px' }}>{h.buy_price > 0 ? h.buy_price.toFixed(2) : '—'}</td>
                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', fontWeight: 600 }}>{h.cmp.toFixed(2)}</td>
                <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', color: h.performance >= 0 ? 'var(--positive)' : 'var(--negative)', fontWeight: 600 }}>
                  {h.performance > 0 ? '+' : ''}{h.performance.toFixed(2)}%
                </td>
                <td style={{ padding: '5px 8px', display: 'flex', gap: '4px' }}>
                  <button className="btn btn-secondary" style={{ padding: '3px 5px' }} onClick={() => handleEditClick(h)} title="Edit">
                    <Edit2 size={13} />
                  </button>
                  {h.isModded && (
                    <button className="btn" style={{ padding: '3px 5px', color: 'var(--negative)', borderColor: 'var(--negative)' }} onClick={() => confirmRemoveMod(h.code)} title="Revert">
                      <RotateCcw size={13} />
                    </button>
                  )}
                  <button className="btn" style={{ padding: '3px 5px', color: 'var(--negative)', borderColor: 'var(--negative)' }} onClick={() => confirmDeleteStock(h.code)} title="Delete from Simulator">
                    <X size={13} />
                  </button>
                </td>
              </tr>
            ))}
            {(() => {
              const cashPct = 100 - (simulatedPortfolio.total_allocation || 0);
              if (cashPct <= 0) return null;
              return (
                <tr style={{ borderTop: '2px solid rgba(255,255,255,0.08)', background: 'rgba(234,179,8,0.06)' }}>
                  <td style={{ padding: '5px 8px', color: 'var(--text-muted)', fontSize: '0.72rem', textAlign: 'right' }}>—</td>
                  <td style={{ padding: '5px 8px' }} />
                  <td style={{ padding: '5px 8px', position: 'sticky', left: 0, background: 'rgba(20,18,10,0.98)', zIndex: 1, whiteSpace: 'nowrap' }}>
                    <strong style={{ fontSize: '0.8rem', color: '#fbbf24' }}>CASH</strong>
                  </td>
                  <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', color: '#fbbf24', fontWeight: 600 }}>{cashPct.toFixed(1)}%</td>
                  <td colSpan={4} style={{ padding: '5px 8px', color: 'var(--text-muted)', fontSize: '0.78rem' }}>Uninvested allocation</td>
                </tr>
              );
            })()}
          </tbody>
        </table>
      </div>

      {/* SIP Management Modal */}
      {sipModalOpen && (
        <div className="modal-overlay no-print" onClick={() => setSipModalOpen(false)}>
          <div className="modal-content glass-panel" onClick={e => e.stopPropagation()} style={{ maxWidth: '560px', width: '95%' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <h3 style={{ margin: 0 }}>Manage SIPs</h3>
              <button onClick={() => setSipModalOpen(false)} style={{ background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>
                <X size={22} />
              </button>
            </div>
            <p style={{ color: 'var(--text-muted)', marginBottom: '4px', fontSize: '0.82rem' }}>
              Base Investment: <strong style={{ color: 'var(--text-main)' }}>₹10,00,000</strong>
              &nbsp;·&nbsp; SIP amount is split by each stock's current allocation weight.
              &nbsp;·&nbsp; If date falls on weekend/holiday, next trading session is used.
            </p>

            {/* SIP list */}
            <div style={{ maxHeight: '260px', overflowY: 'auto', margin: '12px 0', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.08)' }}>
              {sips.length === 0 ? (
                <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '20px 0', fontSize: '0.85rem' }}>No SIPs added yet.</div>
              ) : sips.map((s, i) => {
                const detail = simReturnData?.sip_details?.find(d => d.input_date === s.sip_date);
                const isExpanded = sipExpandedIdx === i;
                const dateAdjusted = detail && detail.actual_date !== detail.input_date;
                return (
                  <div key={s.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                    {/* Header row */}
                    <div style={{ display: 'flex', alignItems: 'center', padding: '8px 12px', gap: '8px' }}>
                      <span style={{ flex: 1, fontSize: '0.84rem' }}>
                        {s.sip_date}
                        {dateAdjusted && (
                          <span style={{ marginLeft: '6px', fontSize: '0.7rem', color: '#fbbf24', background: 'rgba(251,191,36,0.1)', borderRadius: '4px', padding: '1px 6px' }}>
                            → {detail.actual_date}
                          </span>
                        )}
                      </span>
                      <span style={{ fontSize: '0.84rem', fontWeight: 600, color: 'var(--text-main)' }}>
                        ₹{s.amount.toLocaleString('en-IN')}
                      </span>
                      {detail?.stocks?.length > 0 && (
                        <button
                          onClick={() => setSipExpandedIdx(isExpanded ? null : i)}
                          style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.25)', color: '#a5b4fc', borderRadius: '4px', fontSize: '0.7rem', padding: '2px 8px', cursor: 'pointer' }}
                        >
                          {isExpanded ? 'Hide' : 'Details'}
                        </button>
                      )}
                      <button onClick={() => removeSip(s.id)} style={{ background: 'transparent', border: 'none', color: 'var(--negative)', cursor: 'pointer', padding: '0 2px' }}>
                        <X size={14} />
                      </button>
                    </div>
                    {/* Expanded breakdown */}
                    {isExpanded && detail?.stocks?.length > 0 && (
                      <div style={{ padding: '4px 12px 10px', background: 'rgba(99,102,241,0.04)' }}>
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.76rem' }}>
                          <thead>
                            <tr style={{ color: 'var(--text-muted)', borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
                              <th style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600 }}>Stock</th>
                              <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600 }}>Alloc%</th>
                              <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600 }}>Buy Price</th>
                              <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600 }}>Invested</th>
                              <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600 }}>Units</th>
                            </tr>
                          </thead>
                          <tbody>
                            {detail.stocks.map(st => (
                              <tr key={st.code} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                                <td style={{ padding: '3px 6px', fontWeight: 700, color: 'var(--text-main)' }}>{st.code}</td>
                                <td style={{ padding: '3px 6px', textAlign: 'right', color: 'var(--text-muted)' }}>{st.allocation.toFixed(1)}%</td>
                                <td style={{ padding: '3px 6px', textAlign: 'right' }}>₹{st.price.toLocaleString('en-IN')}</td>
                                <td style={{ padding: '3px 6px', textAlign: 'right', color: '#a5b4fc' }}>₹{st.amount_invested.toLocaleString('en-IN')}</td>
                                <td style={{ padding: '3px 6px', textAlign: 'right', color: 'var(--text-muted)' }}>{st.shares.toFixed(3)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Add new SIP */}
            <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-end', paddingTop: '8px', borderTop: '1px solid rgba(255,255,255,0.07)' }}>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: '0.8rem', display: 'block', marginBottom: '4px', color: 'var(--text-muted)' }}>Date</label>
                <input type="date" value={newSip.sip_date} max={new Date().toISOString().split("T")[0]} onChange={e => setNewSip(prev => ({ ...prev, sip_date: e.target.value }))} style={{ width: '100%', padding: '8px', borderRadius: '4px', border: '1px solid rgba(255,255,255,0.2)', background: 'rgba(0,0,0,0.2)', color: 'white', colorScheme: 'dark' }} />
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: '0.8rem', display: 'block', marginBottom: '4px', color: 'var(--text-muted)' }}>Amount (₹)</label>
                <input type="number" value={newSip.amount || ''} onChange={e => setNewSip(prev => ({ ...prev, amount: parseFloat(e.target.value) }))} style={{ width: '100%', padding: '8px', borderRadius: '4px', border: '1px solid rgba(255,255,255,0.2)', background: 'rgba(0,0,0,0.2)', color: 'white' }} />
              </div>
              <button className="btn btn-primary" onClick={saveSip} style={{ padding: '8px 16px', height: '38px' }}>Add</button>
            </div>
          </div>
        </div>
      )}

      {/* Export Format Picker Modal */}
      {exportModalOpen && (
        <div className="modal-overlay no-print" onClick={() => setExportModalOpen(false)}>
          <div className="modal-content glass-panel" onClick={e => e.stopPropagation()} style={{ maxWidth: '420px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
              <h3 style={{ margin: 0 }}>Export Simulator</h3>
              <button onClick={() => setExportModalOpen(false)} style={{ background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>
                <X size={22} />
              </button>
            </div>
            <p style={{ color: 'var(--text-muted)', marginBottom: '24px', fontSize: '0.9rem' }}>
              Choose the format to export <strong style={{ color: 'var(--text-main)' }}>{baskets[selectedId]?.name}</strong>:
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
              <button
                className="btn btn-primary"
                onClick={handleExportExcel}
                style={{ padding: '20px 16px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px', height: 'auto' }}
              >
                <Download size={28} />
                <div>
                  <div style={{ fontWeight: 700, fontSize: '1rem' }}>Excel</div>
                  <div style={{ fontSize: '0.75rem', opacity: 0.8, marginTop: '4px' }}>Summary + Holdings + Historical Comparison</div>
                </div>
              </button>
              <button
                className="btn btn-secondary"
                onClick={handleExportPDF}
                style={{ padding: '20px 16px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px', height: 'auto' }}
              >
                <span style={{ fontSize: '1.8rem', lineHeight: 1 }}>📄</span>
                <div>
                  <div style={{ fontWeight: 700, fontSize: '1rem' }}>PDF</div>
                  <div style={{ fontSize: '0.75rem', opacity: 0.8, marginTop: '4px' }}>Full page snapshot via browser print</div>
                </div>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Universal Confirmation Dialog */}
      {confirmDialog.open && (
        <div className="modal-overlay no-print" onClick={() => { if (!confirmDialog.working) setConfirmDialog(d => ({ ...d, open: false })); }}>
          <div className="modal-content glass-panel" onClick={e => e.stopPropagation()} style={{ maxWidth: '400px', textAlign: 'center' }}>
            <h3 style={{ margin: '0 0 16px' }}>{confirmDialog.title}</h3>
            <p style={{ color: 'var(--text-muted)', marginBottom: '24px', whiteSpace: 'pre-line', lineHeight: 1.6 }}>
              {confirmDialog.message}
            </p>
            <div style={{ display: 'flex', gap: '12px', justifyContent: 'center' }}>
              <button className="btn btn-secondary" onClick={() => setConfirmDialog(d => ({ ...d, open: false }))} disabled={confirmDialog.working}>
                Cancel
              </button>
              <button className="btn" onClick={runConfirmed} disabled={confirmDialog.working} style={confirmDialog.confirmStyle}>
                {confirmDialog.working ? 'Working...' : confirmDialog.confirmText}
              </button>
            </div>
          </div>
        </div>
      )}

      {modalOpen && editMod && (
        <div className="modal-overlay" onClick={() => setModalOpen(false)}>
          <div className="modal-content glass-panel" onClick={e => e.stopPropagation()}>
            <h3 style={{ marginBottom: '24px' }}>
              {editMod.override_type === 'add' ? 'Add New Stock to Simulator' : `Modify ${editMod.stock_code}`}
            </h3>

            {editMod.override_type === 'add' && (
              <>
                <div className="input-group">
                  <label>NSE Stock Code</label>
                  <AutoCompleteInput
                    value={editMod.stock_code}
                    onChange={handleStockCodeChange}
                    placeholder="e.g. RELIANCE"
                  />
                </div>
                <div className="input-group">
                  <label>Historical Purchase Date (up to 5 years back)</label>
                  <input
                    type="date"
                    value={modalDate}
                    max={new Date().toISOString().split("T")[0]}
                    min={new Date(Date.now() - 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split("T")[0]}
                    onChange={(e) => handleDateChange(e.target.value)}
                  />
                  <small style={{color: 'var(--text-muted)', display: 'block', marginTop: '4px'}}>
                    {fetchingPrice
                      ? '⏳ Fetching prices from NSE...'
                      : 'Auto-fetches historic EOD buy price and current CMP. Both can be edited manually.'}
                  </small>
                </div>
              </>
            )}

            <div className="input-group">
              <label>Allocation (%)</label>
              <input type="number" step="0.1" value={editMod.allocation || ''} onChange={e => setEditMod({...editMod, allocation: parseFloat(e.target.value)})} />
            </div>

            <div className="input-group">
              <label>Buy Price {fetchingPrice && <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>fetching…</span>}</label>
              <input
                type="number"
                step="0.1"
                value={editMod.buy_price || ''}
                placeholder={fetchingPrice ? 'Fetching…' : ''}
                onChange={e => setEditMod({...editMod, buy_price: parseFloat(e.target.value)})}
              />
            </div>

            <div className="input-group">
              <label>Current Market Price (CMP) {fetchingPrice && <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>fetching…</span>}</label>
              <input
                type="number"
                step="0.1"
                value={editMod.cmp || ''}
                placeholder={fetchingPrice ? 'Fetching…' : ''}
                onChange={e => setEditMod({...editMod, cmp: parseFloat(e.target.value)})}
              />
            </div>

            {allocationError && (
              <div style={{ padding: '10px 14px', marginTop: '12px', borderRadius: '8px', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', color: '#f87171', fontSize: '0.83rem' }}>
                {allocationError}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', marginTop: '16px' }}>
              <button className="btn btn-secondary" onClick={() => setModalOpen(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={confirmSaveMod}>Save Changes</button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

export default SimulatorPortfolio;
