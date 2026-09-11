import type { AuthSession } from './types'

const AUTH_SESSION_KEY = 'helpdesk_auth_session'

export function getAuthSession(): AuthSession | null {
  try {
    const raw = sessionStorage.getItem(AUTH_SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<AuthSession>
    if (
      typeof parsed.accessToken !== 'string' ||
      !parsed.accessToken ||
      !parsed.principal ||
      typeof parsed.principal.tenant_id !== 'string' ||
      typeof parsed.principal.user_id !== 'string'
    ) {
      clearAuthSession()
      return null
    }
    return parsed as AuthSession
  } catch {
    return null
  }
}

export function setAuthSession(session: AuthSession): void {
  sessionStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session))
}

export function clearAuthSession(): void {
  try {
    sessionStorage.removeItem(AUTH_SESSION_KEY)
  } catch {
    // Storage is unavailable in some hardened browser contexts.
  }
}

export function getAccessToken(): string | null {
  return getAuthSession()?.accessToken ?? null
}
