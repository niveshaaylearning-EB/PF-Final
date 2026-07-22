// Shared pure calc logic for the "what-if" holdings simulator. Kept separate
// from WhatIfModal.jsx so the table-level Add/Delete bar can reuse the exact
// same formulas without duplicating them.
import { calcPerformance, calcContribution, calcAbsoluteReturns } from './App.jsx';

export const EMPTY_SLOT = { editedBuys: {}, deletedNse: [], added: [], weightReductions: {} };

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// "DD Mon YYYY" (this app's date convention everywhere) <-> "YYYY-MM-DD"
// (what a native <input type="date"> requires) -- converted by hand rather
// than via `new Date(...)` to avoid any timezone-shift-by-a-day surprises.
export function toIsoDate(ddMonYyyy) {
  if (!ddMonYyyy) return '';
  const parts = ddMonYyyy.trim().split(/\s+/);
  if (parts.length !== 3) return '';
  const [d, mon, y] = parts;
  const mi = MONTHS.findIndex(m => m.toLowerCase() === mon.toLowerCase());
  if (mi < 0) return '';
  return `${y}-${String(mi + 1).padStart(2, '0')}-${String(parseInt(d, 10)).padStart(2, '0')}`;
}
export function fromIsoDate(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-');
  if (!y || !m || !d) return '';
  return `${parseInt(d, 10)} ${MONTHS[parseInt(m, 10) - 1]} ${y}`;
}

export function weightedBuyPriceFromEvents(events) {
  const valid = (events || []).filter(e => e.date && e.ohlc != null && e.weight != null);
  if (!valid.length) return null;
  const totalQty = valid.reduce((s, e) => s + e.weight, 0);
  if (totalQty <= 0) return null;
  const weightedSum = valid.reduce((s, e) => s + e.weight * e.ohlc, 0);
  return Math.round((weightedSum / totalQty) * 100) / 100;
}

const dateToTs = (dateStr) => {
  const d = new Date(dateStr);
  return isNaN(d) ? null : d.getTime();
};

// The current-series events list starts fresh right after a full exit (see
// currentSeriesBuyEvents in App.jsx), so its first entry IS this position's
// entry date -- used to recompute holding days when a buy-lot date is edited.
function holdingDaysFromEvents(events) {
  const firstDate = (events || []).find(e => e.date)?.date;
  const entryTs = firstDate ? dateToTs(firstDate) : null;
  if (entryTs == null) return null;
  return Math.floor((Date.now() - entryTs) / 86_400_000);
}

// Annualized return from a total gain% held over `holdingDays`. Unlike
// "Since Inception" (a separately-tracked basket NAV/index series, entirely
// independent of any stock's buy price) or "1M Returns" (pure CMP momentum),
// CAGR here is derived directly from buyPrice-vs-CMP, so it's the metric that
// legitimately DOES move when a buy price/date is simulated differently.
export function calcCagr(gainPct, holdingDays) {
  if (gainPct == null || holdingDays == null || holdingDays <= 0) return null;
  const years = holdingDays / 365;
  const growth = 1 + gainPct;
  if (growth <= 0) return null; // a >=100% loss has no real annualized root
  return Math.pow(growth, 1 / years) - 1;
}

// Attaches/refreshes the derived `cagr` field from whatever absoluteReturns/
// holdingDays the row currently has (real or simulated) -- applied uniformly
// so the portfolio-wide weighted CAGR aggregates real AND touched stocks the
// same way.
function withCagr(row) {
  return { ...row, cagr: calcCagr(row.absoluteReturns, row.holdingDays) };
}

// Applies an edited buy-lot list to a real row. Allocation is shifted by the
// DELTA between the edited lots' total weight and their original total
// weight (captured once as baseTotalQty/baseAllocation when the edit was
// first made) -- not replaced outright -- so a pure OHLC/date edit (weight
// unchanged) never nudges allocation, only a genuine weight edit does.
// Holding days is recomputed from the edited events' entry date, so a
// simulated date change also (correctly) shifts CAGR annualization.
export function applyEditToRow(row, edit) {
  if (!edit || !row) return row;
  const bp = weightedBuyPriceFromEvents(edit.events);
  const editedTotalQty = edit.events.reduce((s, e) => s + (e.weight || 0), 0);
  const baseTotalQty   = edit.baseTotalQty   ?? editedTotalQty;
  const baseAllocation = edit.baseAllocation ?? row.allocation ?? 0;
  const allocation = baseAllocation + (editedTotalQty - baseTotalQty) / 100;
  const holdingDays = holdingDaysFromEvents(edit.events) ?? row.holdingDays;
  return {
    ...row,
    allocation,
    buyPrice: bp ?? row.buyPrice,
    absoluteReturns: bp != null ? calcAbsoluteReturns(row.cmp, bp) : row.absoluteReturns,
    holdingDays,
    contribution: calcContribution(allocation, row.performance),
  };
}

// Cuts a stock's weight by a flat number of percentage points -- used when a
// user adding a hypothetical stock chooses to free up room by reducing an
// existing holding rather than requiring headroom to already exist. Additive
// with applyEditToRow (a buy-lot edit and a weight reduction on the same
// stock both just shift allocation further from baseAllocation).
function applyWeightReduction(row, reduceByPct) {
  if (!reduceByPct) return row;
  const allocation = Math.max(0, (row.allocation || 0) - reduceByPct / 100);
  return { ...row, allocation, contribution: calcContribution(allocation, row.performance) };
}

// Turns one hypothetical "added" entry into a row shaped like a real one.
function buildAddedRow(a) {
  const performance = calcPerformance(a.open1M, a.close1M);
  const allocation  = (a.weight || 0) / 100;
  return {
    nseCode: a.nseCode, allocation, buyPrice: a.ohlc, cmp: a.cmp,
    open1M: a.open1M, close1M: a.close1M, performance,
    contribution: calcContribution(allocation, performance),
    absoluteReturns: calcAbsoluteReturns(a.cmp, a.ohlc),
    holdingDays: holdingDaysFromEvents([{ date: a.buyDate }]),
    _simAdded: true, _simId: a.id,
  };
}

// Mirrors the backend's weighted_sum/total_qty formula (buy_price_gains.py
// calc_buy_price) but fed the simulated overlay's edited/added/deleted stocks
// instead of persisted data -- nothing here is ever written back to the server.
// Used for AGGREGATE numbers (KPIs, top gainer/loser, pie chart) where it's
// correct for a deleted stock to disappear from the list entirely.
export function computeWhatIf(rows, slot) {
  let overlaid = rows
    .filter(r => !slot.deletedNse.includes(r.nseCode))
    .map(r => withCagr(applyEditToRow(r, slot.editedBuys[r.nseCode])))
    .map(r => applyWeightReduction(r, slot.weightReductions?.[r.nseCode]));

  overlaid = [...overlaid, ...slot.added.map(a => withCagr(buildAddedRow(a)))];

  const totalAllocation   = overlaid.reduce((s, r) => s + (r.allocation || 0), 0);
  const totalContribution = overlaid.reduce((s, r) => s + (r.contribution || 0), 0);
  const gainRows      = overlaid.filter(r => r.absoluteReturns != null && r.allocation != null);
  const gainWeightSum = gainRows.reduce((s, r) => s + r.allocation, 0);
  const weightedGainPct = gainWeightSum > 0
    ? gainRows.reduce((s, r) => s + r.allocation * r.absoluteReturns, 0) / gainWeightSum
    : null;
  const cagrRows      = overlaid.filter(r => r.cagr != null && r.allocation != null);
  const cagrWeightSum = cagrRows.reduce((s, r) => s + r.allocation, 0);
  const weightedCagr = cagrWeightSum > 0
    ? cagrRows.reduce((s, r) => s + r.allocation * r.cagr, 0) / cagrWeightSum
    : null;

  return { overlaid, totalAllocation, totalContribution, weightedGainPct, weightedCagr };
}

// Per-stock before/after detail for every stock the simulation actually
// touched (edited, added, or deleted) -- the "what exactly happened to this
// stock" breakdown, as opposed to computeWhatIf's portfolio-wide aggregates.
export function buildTouchedDetails(rows, slot) {
  const details = [];
  for (const r of rows) {
    const isDeleted = slot.deletedNse.includes(r.nseCode);
    const edit = slot.editedBuys[r.nseCode];
    const reduceBy = slot.weightReductions?.[r.nseCode];
    if (!isDeleted && !edit && !reduceBy) continue;
    const before = withCagr(r);
    let after;
    if (isDeleted) {
      after = { ...before, allocation: 0, contribution: 0 };
    } else {
      after = edit ? withCagr(applyEditToRow(r, edit)) : before;
      if (reduceBy) after = applyWeightReduction(after, reduceBy);
    }
    const status = isDeleted ? 'deleted' : (reduceBy ? 'reduced' : 'edited');
    details.push({ nseCode: r.nseCode, status, before, after });
  }
  for (const a of slot.added) {
    details.push({ nseCode: a.nseCode, status: 'added', before: null, after: withCagr(buildAddedRow(a)) });
  }
  return details;
}

// Used for the TABLE DISPLAY specifically: unlike computeWhatIf's `overlaid`,
// this NEVER removes or reorders a real row -- deleting a stock in the
// simulation only zeroes its allocation/contribution and flags it, it never
// shrinks the array. That's essential because PortfolioTable's real edit
// handlers (onAllocChange, onBuyPriceChange, ...) are index-based against the
// real `rows` array; removing/reordering rows here would silently misdirect
// an admin's real edit to the wrong stock. Added hypothetical stocks are
// appended at the end instead, past the real rows' index range, where the
// table renders them read-only.
export function mergeSimForDisplay(rows, slot) {
  const displayed = rows.map(r => {
    if (slot.deletedNse.includes(r.nseCode)) {
      return { ...r, allocation: 0, contribution: 0, _simDeleted: true };
    }
    const edit = slot.editedBuys[r.nseCode];
    const reduceBy = slot.weightReductions?.[r.nseCode];
    let out = edit ? { ...applyEditToRow(r, edit), _simEdited: true } : r;
    if (reduceBy) out = { ...applyWeightReduction(out, reduceBy), _simReduced: true };
    return out;
  });
  return [...displayed, ...slot.added.map(buildAddedRow)];
}
