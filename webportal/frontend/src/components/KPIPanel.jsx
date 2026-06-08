import { formatPercent, getColorClass } from '../App.jsx';

function KPICard({ label, value, valueCls, sub, sub2 }) {
  return (
    <div className="kpi-card-new">
      <span className="kpi-card-label">{label}</span>
      <span className={`kpi-card-value ${valueCls || ''}`}>{value}</span>
      {sub  && <span className="kpi-card-sub">{sub}</span>}
      {sub2 && <span className="kpi-card-sub kpi-card-sub2">{sub2}</span>}
    </div>
  );
}

export default function KPIPanel({ totalContribution, totalAbsReturn, avgMarketCap, medianPE, activeStocks, totalAllocation, rows }) {
  const mcFormatted = avgMarketCap > 0
    ? (avgMarketCap >= 100000
        ? '₹' + (avgMarketCap / 100000).toFixed(2) + ' L Cr'
        : '₹' + Math.round(avgMarketCap).toLocaleString('en-IN') + ' Cr')
    : '#N/A';

  const totalRows = rows?.length || 0;

  return (
    <div className="kpi-strip kpi-strip--4">
      <KPICard label="1M Returns"       value={formatPercent(totalContribution)}  valueCls={getColorClass(totalContribution)}  sub="Weighted contribution" />
      <KPICard label="Since Inception"  value={totalAbsReturn != null ? formatPercent(totalAbsReturn) : '#N/A'} valueCls={getColorClass(totalAbsReturn)} sub="Basket index absolute return" />
      <KPICard label="Active Stocks"    value={activeStocks}      sub={`of ${totalRows} total`} valueCls="neutral" />
      <KPICard label="Total Allocation" value={totalAllocation > 0 ? (totalAllocation * 100).toFixed(1) + '%' : '#N/A'} valueCls={Math.abs(totalAllocation - 1) < 0.005 ? 'positive' : 'neutral'} sub="Sum of weights" />
    </div>
  );
}
