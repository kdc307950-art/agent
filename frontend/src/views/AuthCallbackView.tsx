import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'

export function AuthCallbackView() {
  const { status, config, completeOidcLogin } = useAuth()
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (status !== 'anonymous' || !config) return
    void completeOidcLogin().catch((cause) => {
      setError(cause instanceof Error ? cause.message : String(cause))
    })
  }, [completeOidcLogin, config, status])
  if (status === 'authenticated') return <Navigate to="/tickets" replace />
  return <main className="auth-page"><section className="auth-panel"><h1>正在完成登录</h1><p>{error ?? '正在验证企业身份与权限...'}</p></section></main>
}
