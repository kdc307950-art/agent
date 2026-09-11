export interface AuthPrincipal {
  tenant_id: string
  user_id: string
  scopes: string[]
  departments: string[]
  internal: boolean
}

export interface OidcWebConfig {
  client_id: string
  redirect_uri: string
  authorization_endpoint: string
  token_endpoint: string
  scopes: string[]
}

export interface AuthConfig {
  auth_mode: 'dev' | 'oidc'
  demo_login_enabled: boolean
  oidc: OidcWebConfig | null
}

export interface AuthSession {
  accessToken: string
  principal: AuthPrincipal
}
