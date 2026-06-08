// When served under /wp/ (cloud), API calls must also go through /wp/api/
// On localhost the app runs directly at port 8001, so /api/ is correct
const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const BASE = IS_LOCAL ? '/api' : '/wp/api';

export const fetchBaskets   = ()    => fetch(`${BASE}/baskets`).then(r => r.json());
export const fetchBasket    = (key) => fetch(`${BASE}/basket/${key}`).then(r => r.json());
export const fetchLiveData  = ()    => fetch(`${BASE}/live`).then(r => r.json());
export const fetchLiveStock = (nse) => fetch(`${BASE}/live/${nse}`).then(r => r.json());

export const saveBasket = (key, stocks, soldStocks, buyPriceDetails) =>
  fetch(`${BASE}/basket/${key}`, {
    method:  'PUT',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ stocks, soldStocks, buyPriceDetails }),
  }).then(r => r.json());
