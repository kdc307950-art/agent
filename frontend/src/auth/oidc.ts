import { getCurrentPrincipal } from './api'
import { setAuthSession } from './session'
import type { AuthConfig, AuthSession, OidcWebConfig } from './types'

const OIDC_TRANSACTION_KEY = 'helpdesk_oidc_transaction'

interface OidcTransaction {
  state: string
  verifier: string
}

function base64Url(bytes: Uint8Array): string {
  let value = ''
  for (const byte of bytes) value += String.fromCharCode(byte)
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '')
}

function randomValue(length = 32): string {
  const bytes = new Uint8Array(length)
  crypto.getRandomValues(bytes)
  return base64Url(bytes)
}

async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return base64Url(new Uint8Array(digest))
}

function requireOidc(config: AuthConfig): OidcWebConfig {
  if (config.auth_mode !== 'oidc' || !config.oidc) {
    throw new Error('企业 SSO 尚未完成配置')
  }
  return config.oidc
}

export async function beginOidcLogin(config: AuthConfig): Promise<void> {
  const oidc = requireOidc(config)
  const state = randomValue()
  const verifier = randomValue(64)
  sessionStorage.setItem(OIDC_TRANSACTION_KEY, JSON.stringify({ state, verifier } satisfies OidcTransaction))
  const url = new URL(oidc.authorization_endpoint)
  url.search = new URLSearchParams({
    response_type: 'code',
    client_id: oidc.client_id,
    redirect_uri: oidc.redirect_uri,
    scope: oidc.scopes.join(' '),
    state,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: 'S256',
  }).toString()
  window.location.assign(url.toString())
}

export async function finishOidcLogin(config: AuthConfig): Promise<AuthSession> {
  const oidc = requireOidc(config)
  const params = new URLSearchParams(window.location.search)
  const authorizationError = params.get('error')
  if (authorizationError) {
    throw new Error(params.get('error_description') || authorizationError)
  }
  const code = params.get('code')
  const state = params.get('state')
  const rawTransaction = sessionStorage.getItem(OIDC_TRANSACTION_KEY)
  sessionStorage.removeItem(OIDC_TRANSACTION_KEY)
  if (!code || !state || !rawTransaction) throw new Error('登录回调缺少必要参数')
  const transaction = JSON.parse(rawTransaction) as Partial<OidcTransaction>
  if (!transaction.state || !transaction.verifier || transaction.state !== state) {
    throw new Error('登录状态校验失败，请重新登录')
  }
  const response = await fetch(oidc.token_endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      redirect_uri: oidc.redirect_uri,
      client_id: oidc.client_id,
      code_verifier: transaction.verifier,
    }),
  })
  if (!response.ok) throw new Error('企业 SSO 未能完成令牌交换')
  const body = (await response.json()) as { access_token?: unknown }
  if (typeof body.access_token !== 'string' || !body.access_token) {
    throw new Error('企业 SSO 未返回访问令牌')
  }
  const principal = await getCurrentPrincipal(body.access_token)
  const session = { accessToken: body.access_token, principal }
  setAuthSession(session)
  return session
}
