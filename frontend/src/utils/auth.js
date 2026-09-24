export const TOKEN_KEY   = 'nia_auth_token';
export const REFRESH_KEY = 'nia_refresh_token';

export const getToken        = () => localStorage.getItem(TOKEN_KEY);
export const setToken        = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken      = () => localStorage.removeItem(TOKEN_KEY);
export const getRefreshToken = () => localStorage.getItem(REFRESH_KEY);
export const setRefreshToken = (t) => localStorage.setItem(REFRESH_KEY, t);
export const clearRefreshToken = () => localStorage.removeItem(REFRESH_KEY);

export const clearAllTokens = () => {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
};

const _decodePayload = (t) => {
  try { return JSON.parse(atob(t.split('.')[1])); } catch { return null; }
};

export const isLoggedIn = () => {
  const t = getToken();
  if (!t) return false;
  const payload = _decodePayload(t);
  if (!payload?.exp) return false;
  return Date.now() < payload.exp * 1000;
};

export const getEmail = () => {
  const t = getToken();
  if (!t) return null;
  return _decodePayload(t)?.sub ?? null;
};

export const getFirstName = () => {
  const t = getToken();
  if (!t) return null;
  const payload = _decodePayload(t);
  if (payload?.fn) return payload.fn;
  // Fall back to deriving name from email
  const email = payload?.sub;
  if (!email) return null;
  const part = email.split('@')[0].split(/[._-]/)[0];
  return part.charAt(0).toUpperCase() + part.slice(1);
};

// The JWT now carries an "admin" claim computed server-side (backend/auth.py,
// via common/admin.py's is_admin_email -- the hardcoded ADMIN_EMAILS founders
// PLUS anyone granted admin from the Approved Emails page). Reading it here
// instead of a hardcoded list means a newly-granted admin sees admin-only
// pages/buttons immediately on their next login, with no separate frontend
// deploy needed -- this used to be a duplicate hardcoded list that could only
// ever reflect the 3 founders.
export const isAdmin = () => {
  const t = getToken();
  if (!t) return false;
  return _decodePayload(t)?.admin === true;
};
