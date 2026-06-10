export const TOKEN_KEY = 'nia_auth_token';

export const getToken   = () => localStorage.getItem(TOKEN_KEY);
export const setToken   = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

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
  const email = getEmail();
  if (!email) return null;
  const part = email.split('@')[0].split(/[._-]/)[0];
  return part.charAt(0).toUpperCase() + part.slice(1);
};

export const isAdmin = () => {
  const email = getEmail();
  if (!email) return false;
  const lowerEmail = email.toLowerCase().trim();
  return lowerEmail === 'jay.chaudhari@niveshaay.com' || lowerEmail === 'nukul.madaan@niveshaay.com';
};
