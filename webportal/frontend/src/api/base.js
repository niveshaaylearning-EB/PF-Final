// When served under /wp/ (cloud via nginx), API calls must use /wp/api/
// On localhost (port 8001 direct), /api/ is correct
const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
export const API_BASE = IS_LOCAL ? '/api' : '/wp/api';
