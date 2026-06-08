import { Navigate } from 'react-router-dom';
import { isLoggedIn, clearToken } from '../utils/auth';

export default function ProtectedRoute({ children }) {
  if (isLoggedIn()) return children;
  clearToken();
  return <Navigate to="/login" replace />;
}
