import { describe, expect, it } from 'vitest'
import { clearAuthSession, getAccessToken, getAuthSession, setAuthSession } from './session'
import type { AuthSession } from './types'

const session: AuthSession = {
  accessToken: 'token-1',
  principal: {
    tenant_id: 'demo',
    user_id: 'agent-1',
    scopes: ['ticket:agent'],
    departments: ['it'],
    internal: true,
  },
}

describe('auth session storage', () => {
  it('stores and restores the current session without exposing a custom identity', () => {
    setAuthSession(session)
    expect(getAuthSession()).toEqual(session)
    expect(getAccessToken()).toBe('token-1')
  })

  it('clears malformed or signed-out sessions', () => {
    sessionStorage.setItem('helpdesk_auth_session', '{bad json')
    expect(getAuthSession()).toBeNull()
    setAuthSession(session)
    clearAuthSession()
    expect(getAuthSession()).toBeNull()
  })
})
