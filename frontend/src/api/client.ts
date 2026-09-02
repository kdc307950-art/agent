/** 统一 API 客户端：JSON 请求封装 + SSE 底层读取。 */

import { getDevToken } from '../lib/devToken'

const API_PREFIX = '/api'

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 通用 JSON 请求；非 2xx 时抛出带后端 detail 的 ApiError。
 *
 * 支持 options.signal 取消请求；被取消时原样抛出 AbortError，调用方可据此判断是否静默处理。
 */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getDevToken()
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers ?? {}),
    },
  })
  if (response.status === 204) {
    return {} as T
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : ''
    throw new ApiError(detail || `请求失败 (${response.status})`, response.status)
  }
  return body as T
}

export function sseFetch(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  const token = getDevToken()
  return fetch(`${API_PREFIX}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  })
}

/** 统一 HTTP 状态码 → 用户可读文案（收敛方案阶段七）。
 *
 * 401/403/409/429/5xx 有明确语义；其余回退到服务端 detail。
 * 生产环境 401 应触发登录/刷新会话（由接入 OIDC/BFF 时接入），
 * 本前端当前为 dev-token 模式，只做清晰的错误呈现。
 */
const HTTP_STATUS_MESSAGES: Record<number, string> = {
  401: '登录已过期，请重新登录',
  403: '没有权限执行此操作',
  409: '数据已变更，请刷新后重试',
  429: '请求过于频繁，请稍后再试',
}

export function describeApiError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status >= 500) return `服务暂时不可用（${err.status}），请稍后重试`
    const known = HTTP_STATUS_MESSAGES[err.status]
    return known ?? err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}
