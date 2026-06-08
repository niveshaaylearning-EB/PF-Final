export default function StockInfoTooltip({ nse, x, y, basketMeta }) {
  const basketHistory = basketMeta?.history || {};
  const entry         = basketHistory[nse] || null;
  const addedDate     = entry?.added     || null;
  const rebalances    = entry?.rebalances || [];

  return (
    <div
      className="stock-info-tooltip"
      style={{ left: x, top: y }}
    >
      <div className="sit-symbol">NSE: {nse || 'New Stock'}</div>
      <div className="sit-body">
        <div className="sit-row">
          <span className="sit-label">Added to Portfolio</span>
          <span className="sit-date">{addedDate || '\u2014'}</span>
        </div>
        <hr className="sit-divider" />
        <div className="sit-rebal-header">Rebalancing History</div>
        {rebalances.length === 0 ? (
          <div className="sit-no-data">No rebalancing records yet.</div>
        ) : (
          rebalances.map((r, i) => (
            <div key={i} className="sit-rebal-row">
              <span className="sit-rebal-date">{r.date}</span>
              <span>{r.note}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
