import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createDemoSession, getAuthConfig } from './api'

describe('auth API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    sessionStorage.clear()
  })

  it('loads public config and creates a fixed demo session', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ auth_mode: 'dev', demo_login_enabled: true, oidc: null }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              access_token: 'token-1',
              principal: {
                tenant_id: 'demo',
                user_id: 'agent-1',
                scopes: ['ticket:agent'],
                departments: ['it'],
                internal: true,
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
    )

    await expect(getAuthConfig()).resolves.toMatchObject({ demo_login_enabled: true })
    const session = await createDemoSession('agent')
    expect(session.accessToken).toBe('token-1')
    expect(JSON.parse(sessionStorage.getItem('helpdesk_auth_session') ?? '{}')).toEqual(session)
    expect(fetch).toHaveBeenCalledWith('/api/auth/dev/session', expect.objectContaining({ method: 'POST' }))
  })
})
