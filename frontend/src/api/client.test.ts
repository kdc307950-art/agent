import { describe, it, expect } from 'vitest'
import { ApiError, describeApiError } from './client'

describe('describeApiError（统一 HTTP 状态码文案）', () => {
  it.each([
    [401, '登录已过期，请重新登录'],
    [403, '没有权限执行此操作'],
    [409, '数据已变更，请刷新后重试'],
    [429, '请求过于频繁，请稍后再试'],
    [500, '服务暂时不可用（500），请稍后重试'],
    [503, '服务暂时不可用（503），请稍后重试'],
  ])('%i 映射为明确文案', (status, expected) => {
    expect(describeApiError(new ApiError('detail', status))).toBe(expected)
  })

  it('未知状态码回退到服务端 detail', () => {
    expect(describeApiError(new ApiError('自定义错误', 422))).toBe('自定义错误')
  })

  it('非 ApiError 回退到 Error.message / String', () => {
    expect(describeApiError(new Error('boom'))).toBe('boom')
    expect(describeApiError('raw')).toBe('raw')
  })
})
