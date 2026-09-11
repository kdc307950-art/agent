import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Building2, LogIn, ShieldCheck } from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import { beginOidcLogin } from '../auth/oidc'

export function LoginView() {
  const { status, config, error } = useAuth()
  const [submitting, setSubmitting] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const location = useLocation()
  const navigate = useNavigate()
  const from = (location.state as { from?: string } | null)?.from ?? '/tickets'

  if (status === 'authenticated') return <Navigate to={from} replace />
  if (config?.auth_mode === 'dev' && config.demo_login_enabled) {
    return <Navigate to="/demo-login" replace state={{ from }} />
  }
  const login = async () => {
    try {
      setSubmitting(true)
      setLocalError(null)
      if (!config) throw new Error('登录配置尚未加载')
      await beginOidcLogin(config)
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : String(cause))
      setSubmitting(false)
    }
  }
  return (
    <main className="auth-page">
      <section className="auth-panel" aria-labelledby="login-title">
        <div className="auth-brand"><ShieldCheck size={28} aria-hidden="true" /><span>Helpdesk</span></div>
        <div>
          <p className="eyebrow">企业 IT 服务台</p>
          <h1 id="login-title">登录工作台</h1>
          <p>使用企业单点登录进入与您权限匹配的工作区。</p>
        </div>
        {config?.oidc ? (
          <button className="primary-action auth-login-button" onClick={() => void login()} disabled={submitting}>
            <LogIn size={18} aria-hidden="true" />
            {submitting ? '正在跳转...' : '企业 SSO 登录'}
          </button>
        ) : (
          <div className="auth-config-warning"><Building2 size={18} aria-hidden="true" />企业 SSO 尚未完成配置</div>
        )}
        {(error || localError) && <p className="auth-error" role="alert">{localError ?? error}</p>}
        {config?.auth_mode === 'dev' && (
          <button className="auth-back" onClick={() => navigate('/demo-login')}>返回演示登录</button>
        )}
      </section>
    </main>
  )
}
