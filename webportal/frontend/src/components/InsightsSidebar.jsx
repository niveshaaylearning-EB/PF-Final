import { formatPercent, getColorClass } from '../App.jsx';

function InsightCard({ title, subtitle, items, valueKey, accent }) {
  if (!items.length) return null;
  const [top, ...rest] = items;
  const topVal = top[valueKey];
  const topCls = getColorClass(topVal);

  return (
    <div className={`icard icard--${accent}`}>
      {/* Header row */}
      <div className="icard-header">
        <div className="icard-header-left">
          <span className={`icard-dot icard-dot--${accent}`} />
          <span className="icard-title">{title}</span>
        </div>
        <span className="icard-badge">{subtitle}</span>
      </div>

      {/* Featured #1 — shown large like image 2 */}
      <div className="icard-featured">
        <span className={`icard-featured-val ${topCls}`}>{formatPercent(topVal)}</span>
        <span className="icard-featured-stock">{top.nseCode}</span>
      </div>

      {/* Remaining 4 items */}
      <div className="icard-rest">
        {rest.map((item, i) => {
          const val = item[valueKey];
          return (
            <div key={item.nseCode + i} className="icard-rest-row">
              <span className="icard-rest-rank">{i + 2}</span>
              <span className="icard-rest-name">{item.nseCode}</span>
              <span className={`icard-rest-val ${getColorClass(val)}`}>{formatPercent(val)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function InsightsSidebar({ rows, isIPO }) {
  const validPerf = rows.filter(r => r.performance  != null && isFinite(r.performance));
  const validCont = rows.filter(r => r.contribution != null && isFinite(r.contribution));

  const topGainers  = [...validPerf].sort((a, b) => b.performance  - a.performance).slice(0, 5);
  const topLosers   = [...validPerf].sort((a, b) => a.performance  - b.performance).slice(0, 5);
  const topContribs = [...validCont].sort((a, b) => b.contribution - a.contribution).slice(0, 5);
  const topDraggers = [...validCont].sort((a, b) => a.contribution - b.contribution).slice(0, 5);

  return (
    <div className="insights-section">
      {!isIPO && (
        <div className="insights-grid">
          <InsightCard title="Top Gainers"      subtitle="1M Perf"     items={topGainers}  valueKey="performance"  accent="green" />
          <InsightCard title="Top Losers"       subtitle="1M Perf"     items={topLosers}   valueKey="performance"  accent="red"   />
          <InsightCard title="Top Contributors" subtitle="Contribution" items={topContribs} valueKey="contribution" accent="green" />
          <InsightCard title="Top Draggers"     subtitle="Contribution" items={topDraggers} valueKey="contribution" accent="red"   />
        </div>
      )}
    </div>
  );
}
