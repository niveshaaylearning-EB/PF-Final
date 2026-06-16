import { Navigate } from 'react-router-dom';
import { isLoggedIn, isAdmin, clearAllTokens } from '../utils/auth';

export default function ProtectedRoute({ children, adminOnly = false }) {
  if (!isLoggedIn()) {
    clearAllTokens();
    return <Navigate to="/login" replace />;
  }
  if (adminOnly && !isAdmin()) {
    return <Navigate to="/" replace />;
  }
  return children;
}
