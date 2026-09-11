import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './AuthProvider'
import type { ReactNode } from 'react'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, config, error } = useAuth()
  const location = useLocation()
  if (status === 'loading') return <div className="auth-loading">正在恢复会话...</div>
  if (status === 'error') return <div className="auth-loading">认证服务不可用：{error}</div>
  if (status === 'authenticated') return <>{children}</>
  const target = config?.auth_mode === 'dev' && config.demo_login_enabled ? '/demo-login' : '/login'
  return <Navigate to={target} replace state={{ from: location.pathname + location.search }} />
}
