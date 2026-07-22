import { getAuthToken } from './base.js';

// When served under /wp/ (cloud), API calls must also go through /wp/api/
// On localhost the app runs directly at port 8001, so /api/ is correct
const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const BASE = IS_LOCAL ? '/api' : '/wp/api';

export const fetchBaskets   = ()    => fetch(`${BASE}/baskets`).then(r => r.json());
export const fetchBasketStockMap = () => fetch(`${BASE}/basket-stock-map`).then(r => r.json());
export const fetchBasket    = (key) => fetch(`${BASE}/basket/${key}`).then(r => r.json());
export const fetchLiveData  = ()    => fetch(`${BASE}/live`).then(r => r.json());
export const fetchPerformanceBatch = (codes) =>
  fetch(`${BASE}/performance?codes=${encodeURIComponent(codes.join(','))}`).then(r => r.json());
export const fetchLiveStock = (nse, { fast = false } = {}) =>
  fetch(`${BASE}/live/${nse}${fast ? '?fast=true' : ''}`).then(r => r.json());
export const fetchOhlcLookup = (nse, date) =>
  fetch(`${BASE}/ohlc-lookup/${encodeURIComponent(nse)}?date=${encodeURIComponent(date)}`).then(r => r.json());
export const fetchCaStatus = (basket, code) =>
  fetch(`${BASE}/corporate-actions/status/${encodeURIComponent(basket)}/${encodeURIComponent(code)}`).then(r => r.json());

export const saveBasket = (key, stocks, soldStocks, buyPriceDetails) => {
  const token = getAuthToken();
  return fetch(`${BASE}/basket/${key}`, {
    method:  'PUT',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body:    JSON.stringify({ stocks, soldStocks, buyPriceDetails }),
  }).then(r => r.json());
};
