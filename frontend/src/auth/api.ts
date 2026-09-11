import { clearAuthSession, setAuthSession } from './session'
import type { AuthConfig, AuthPrincipal, AuthSession } from './types'

const API_PREFIX = '/api'

async function readError(response: Response): Promise<string> {
  const body = await response.json().catch(() => ({}))
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    return String((body as { detail: unknown }).detail)
  }
  return `请求失败 (${response.status})`
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const response = await fetch(`${API_PREFIX}/auth/config`)
  if (!response.ok) throw new Error(await readError(response))
  return response.json() as Promise<AuthConfig>
}

export async function getCurrentPrincipal(accessToken: string): Promise<AuthPrincipal> {
  const response = await fetch(`${API_PREFIX}/auth/me`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if (!response.ok) throw new Error(await readError(response))
  return response.json() as Promise<AuthPrincipal>
}

export async function createDemoSession(
  persona: 'customer' | 'agent' | 'admin',
): Promise<AuthSession> {
  const response = await fetch(`${API_PREFIX}/auth/dev/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ persona }),
  })
  if (!response.ok) throw new Error(await readError(response))
  const result = (await response.json()) as { access_token: string; principal: AuthPrincipal }
  const session = { accessToken: result.access_token, principal: result.principal }
  setAuthSession(session)
  return session
}

export async function validateStoredSession(accessToken: string): Promise<AuthSession> {
  try {
    const principal = await getCurrentPrincipal(accessToken)
    const session = { accessToken, principal }
    setAuthSession(session)
    return session
  } catch (error) {
    clearAuthSession()
    throw error
  }
}
