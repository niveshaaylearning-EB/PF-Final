import { Navigate } from 'react-router-dom';
import { isLoggedIn, isAdmin, clearToken } from '../utils/auth';

export default function ProtectedRoute({ children, adminOnly = false }) {
  if (!isLoggedIn()) {
    clearToken();
    return <Navigate to="/login" replace />;
  }
  if (adminOnly && !isAdmin()) {
    return <Navigate to="/" replace />;
  }
  return children;
}
