/** 演示模式短期开发令牌（AUTH_MODE=dev）。
 *
 * 仅用于本地/演示环境：用户在页面粘贴 `issue_dev_token` 签发的令牌，
 * 保存在 sessionStorage（关闭标签页即失效），每次 API 请求由 client.ts 附加到 Authorization。
 * 生产环境接 OIDC/BFF，禁止复用此方案。
 */

export const DEV_TOKEN_KEY = 'helpdesk_dev_token'

export function getDevToken(): string | null {
  try {
    return sessionStorage.getItem(DEV_TOKEN_KEY)
  } catch {
    return null
  }
}

export function setDevToken(token: string): void {
  sessionStorage.setItem(DEV_TOKEN_KEY, token.trim())
}

export function clearDevToken(): void {
  sessionStorage.removeItem(DEV_TOKEN_KEY)
}
