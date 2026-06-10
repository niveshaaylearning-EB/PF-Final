import { Suspense, lazy, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Link, useNavigate, useLocation } from 'react-router-dom';
import axios from 'axios';
import ProtectedRoute from './components/ProtectedRoute.jsx';
import { clearToken, getEmail, isAdmin, getFirstName, isLoggedIn } from './utils/auth.js';
import { API_ROOT } from './config.js';

// Auto-logout on any 401 (expired token) from the API
axios.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      clearToken();
      window.location.replace('/login');
    }
    return Promise.reject(err);
  }
);

const HomePage            = lazy(() => import('./pages/HomePage'));
const ActualPortfolio     = lazy(() => import('./pages/ActualPortfolio'));
const SimulatorPortfolio  = lazy(() => import('./pages/SimulatorPortfolio'));
const ScreenerData        = lazy(() => import('./pages/ScreenerData'));
const BasketComparison    = lazy(() => import('./pages/BasketComparison'));
const LoginPage           = lazy(() => import('./pages/LoginPage'));
const AdminBacklog        = lazy(() => import('./pages/AdminBacklog'));
const ResultCalendar      = lazy(() => import('./pages/ResultCalendar'));
const RebalanceAlertPage  = lazy(() => import('./pages/RebalanceAlertPage'));
const ApprovedEmailsPage  = lazy(() => import('./pages/ApprovedEmailsPage'));

function PageLoader() {
  return (
    <div style={{ textAlign: 'center', marginTop: '4rem' }}>
      <h3 className="text-gradient">Loading...</h3>
    </div>
  );
}

function Header() {
  const navigate = useNavigate();
  const location = useLocation();
  const loggedIn = isLoggedIn();
  const email    = getEmail();

  // Check every 60 s — redirect to login if the midnight-expiry JWT has elapsed
  useEffect(() => {
    const id = setInterval(() => {
      if (!isLoggedIn() && location.pathname !== '/login') {
        clearToken();
        navigate('/login', { replace: true });
      }
    }, 60_000);
    return () => clearInterval(id);
  }, [location.pathname, navigate]);

  async function handleLogout() {
    try {
      await axios.post(`${API_ROOT}/auth/logout`);
    } catch (_) {
      // best-effort — still clear the token even if the call fails
    }
    clearToken();
    navigate('/login', { replace: true });
  }

  if (location.pathname === '/login') return null;

  return (
    <header style={{ marginBottom: '2rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <Link to="/" style={{ textDecoration: 'none' }}>
        <h1 className="text-gradient" style={{ margin: 0, fontSize: '1.8rem' }}>NIA Performance Center</h1>
      </Link>
      <nav style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
        <Link to="/" className="btn btn-secondary">Home</Link>

        {loggedIn && (
          <Link
            to="/calendar"
            className="btn btn-secondary"
            style={{ fontSize: '0.78rem', padding: '6px 12px' }}
          >
            Calendar
          </Link>
        )}
        {loggedIn && isAdmin() && (
          <Link
            to="/admin"
            className="btn btn-secondary"
            style={{ fontSize: '0.78rem', padding: '6px 12px', color: '#f59e0b', borderColor: 'rgba(245,158,11,0.4)' }}
          >
            Backlog
          </Link>
        )}
        {loggedIn && (
          <>
            {email && (
              <span style={{
                fontSize: '0.75rem', color: 'var(--text-muted)',
                background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: '20px', padding: '4px 10px',
              }}>
                {getFirstName()}
              </span>
            )}
            <button
              onClick={handleLogout}
              className="btn btn-secondary"
              style={{ fontSize: '0.78rem', padding: '6px 12px' }}
            >
              Logout
            </button>
          </>
        )}
      </nav>
    </header>
  );
}

function App() {
  return (
    <BrowserRouter>
      <div className="container">
        <Header />
        <Suspense fallback={<PageLoader />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/rebalance-alerts" element={
              <ProtectedRoute><RebalanceAlertPage /></ProtectedRoute>
            } />
            <Route path="/approved-emails" element={
              <ProtectedRoute adminOnly><ApprovedEmailsPage /></ProtectedRoute>
            } />
            <Route path="/" element={
              <ProtectedRoute><HomePage /></ProtectedRoute>
            } />
            <Route path="/actual" element={
              <ProtectedRoute><ActualPortfolio /></ProtectedRoute>
            } />
            <Route path="/simulator" element={
              <ProtectedRoute><SimulatorPortfolio /></ProtectedRoute>
            } />
            <Route path="/screener" element={
              <ProtectedRoute><ScreenerData /></ProtectedRoute>
            } />
            <Route path="/comparison" element={
              <ProtectedRoute><BasketComparison /></ProtectedRoute>
            } />
            <Route path="/calendar" element={
              <ProtectedRoute><ResultCalendar /></ProtectedRoute>
            } />
            <Route path="/admin" element={
              <ProtectedRoute adminOnly><AdminBacklog /></ProtectedRoute>
            } />
          </Routes>
        </Suspense>
      </div>
    </BrowserRouter>
  );
}

export default App;
