/** 统一 API 客户端：JSON 请求封装 + SSE 底层读取。 */

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
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
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
  return fetch(`${API_PREFIX}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}
