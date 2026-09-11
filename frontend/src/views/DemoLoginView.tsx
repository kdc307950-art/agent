import { useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { BriefcaseBusiness, ShieldCheck, UserRound } from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'

const personas = [
  { id: 'customer' as const, title: '员工', description: '提交和查看自己的 VPN 工单', Icon: UserRound },
  { id: 'agent' as const, title: 'IT 客服', description: '处理队列、查看资产和知识建议', Icon: BriefcaseBusiness },
  { id: 'admin' as const, title: 'IT 管理员', description: '维护策略、知识库和资产权限', Icon: ShieldCheck },
]

export function DemoLoginView() {
  const { status, config, signInDemo } = useAuth()
  const [submitting, setSubmitting] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/tickets'
  if (status === 'authenticated') return <Navigate to={from} replace />
  if (config?.auth_mode !== 'dev' || !config.demo_login_enabled) return <Navigate to="/login" replace />
  const login = async (persona: 'customer' | 'agent' | 'admin') => {
    try {
      setSubmitting(persona)
      setError(null)
      await signInDemo(persona)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setSubmitting(null)
    }
  }
  return (
    <main className="auth-page">
      <section className="auth-panel demo-login-panel" aria-labelledby="demo-login-title">
        <div className="auth-brand"><ShieldCheck size={28} aria-hidden="true" /><span>Helpdesk</span></div>
        <div>
          <p className="eyebrow">本地演示环境</p>
          <h1 id="demo-login-title">选择演示身份</h1>
          <p>身份和权限固定为演示租户，不接受自定义用户、角色或租户。</p>
        </div>
        <div className="demo-personas">
          {personas.map(({ id, title, description, Icon }) => (
            <button key={id} className="demo-persona" onClick={() => void login(id)} disabled={submitting !== null}>
              <Icon size={22} aria-hidden="true" />
              <span><strong>{title}</strong><small>{description}</small></span>
              <span className="demo-persona-action">{submitting === id ? '登录中...' : '进入'}</span>
            </button>
          ))}
        </div>
        {error && <p className="auth-error" role="alert">{error}</p>}
      </section>
    </main>
  )
}
