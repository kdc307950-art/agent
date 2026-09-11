import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { createDemoSession, getAuthConfig, validateStoredSession } from './api'
import { finishOidcLogin } from './oidc'
import { clearAuthSession, getAuthSession } from './session'
import type { AuthConfig, AuthSession } from './types'

type AuthStatus = 'loading' | 'anonymous' | 'authenticated' | 'error'

interface AuthContextValue {
  status: AuthStatus
  config: AuthConfig | null
  session: AuthSession | null
  error: string | null
  signInDemo: (persona: 'customer' | 'agent' | 'admin') => Promise<void>
  completeOidcLogin: () => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [session, setSession] = useState<AuthSession | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const initialize = async () => {
      try {
        const nextConfig = await getAuthConfig()
        if (!active) return
        setConfig(nextConfig)
        const stored = getAuthSession()
        if (!stored) {
          setStatus('anonymous')
          return
        }
        const nextSession = await validateStoredSession(stored.accessToken)
        if (!active) return
        setSession(nextSession)
        setStatus('authenticated')
      } catch (cause) {
        if (!active) return
        clearAuthSession()
        setSession(null)
        setError(cause instanceof Error ? cause.message : String(cause))
        setStatus('error')
      }
    }
    void initialize()
    const expire = () => {
      clearAuthSession()
      setSession(null)
      setStatus('anonymous')
    }
    window.addEventListener('helpdesk-auth-expired', expire)
    return () => {
      active = false
      window.removeEventListener('helpdesk-auth-expired', expire)
    }
  }, [])

  const signInDemo = useCallback(async (persona: 'customer' | 'agent' | 'admin') => {
    const nextSession = await createDemoSession(persona)
    setSession(nextSession)
    setError(null)
    setStatus('authenticated')
  }, [])

  const completeOidcLogin = useCallback(async () => {
    if (!config) throw new Error('登录配置尚未加载')
    const nextSession = await finishOidcLogin(config)
    setSession(nextSession)
    setError(null)
    setStatus('authenticated')
  }, [config])

  const signOut = useCallback(() => {
    clearAuthSession()
    setSession(null)
    setStatus('anonymous')
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ status, config, session, error, signInDemo, completeOidcLogin, signOut }),
    [completeOidcLogin, config, error, session, signInDemo, signOut, status],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return context
}
