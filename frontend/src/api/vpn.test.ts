import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  diagnoseVpn,
  getVpnDiagnosis,
  resumeVpnDiagnosis,
  submitVpnActionResult,
} from './vpn'

// 用 vi.mock 拦截底层 api()，同时保留 ApiError / describeApiError 的既有行为（client.test 已覆盖）
vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>()
  return {
    ...actual,
    api: vi.fn(),
  }
})

import * as client from './client'
const apiMock = client.api as unknown as ReturnType<typeof vi.fn>

const sampleRun = {
  run_id: 'diag_abc',
  ticket_id: 't-1',
  fault: 'configuration_error',
  hypothesis: '客户端配置版本过旧',
  confidence: 0.9,
  evidence: [
    { tool_name: 'get_vpn_account_status', evidence: '账号 active', title: null, found: true },
  ],
  ruled_out: ['gateway_down'],
  next_action: 'provide_steps',
  reason_codes: ['client_version_too_old'],
  status: 'completed',
  created_at: new Date().toISOString(),
}

describe('vpn API 封装（对齐诊断 API 落地契约）', () => {
  beforeEach(() => {
    apiMock.mockReset()
  })

  it('diagnoseVpn 走 POST /tickets/{id}/vpn/diagnose，无请求体', async () => {
    apiMock.mockResolvedValue({ run: sampleRun, result: {}, dispatch: {} })
    const result = await diagnoseVpn('t-1')
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnose', {
      method: 'POST',
      signal: undefined,
    })
    expect(result.run.run_id).toBe('diag_abc')
  })

  it('getVpnDiagnosis 走 GET /tickets/{id}/vpn/diagnosis，返回快照', async () => {
    const snapshot = {
      ticket_id: 't-1',
      latest_run: sampleRun,
      runs: [sampleRun],
      actions: [],
      results: [],
      escalations: [],
    }
    apiMock.mockResolvedValue(snapshot)
    const result = await getVpnDiagnosis('t-1')
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnosis', { signal: undefined })
    expect(result.latest_run).toEqual(sampleRun)
    expect(result.runs).toHaveLength(1)
  })

  it('submitVpnActionResult 走 POST /tickets/{id}/vpn/actions/{aid}/result，evidence 为 dict、details 为字符串', async () => {
    const response = {
      action_result: {
        action_id: 'a-1',
        result: '已重启',
        evidence: { note: '连接恢复' },
        details: '客户已操作',
        submitted_at: new Date().toISOString(),
      },
      transition: true,
      re_diagnosis: null,
    }
    apiMock.mockResolvedValue(response)
    const result = await submitVpnActionResult('t-1', 'a-1', {
      result: '已重启',
      evidence: { note: '连接恢复' },
      details: '客户已操作',
    })
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/actions/a-1/result', {
      method: 'POST',
      body: JSON.stringify({ result: '已重启', evidence: { note: '连接恢复' }, details: '客户已操作' }),
      signal: undefined,
    })
    expect(result.action_result.action_id).toBe('a-1')
  })

  it('resumeVpnDiagnosis 走 POST /tickets/{id}/vpn/diagnose/resume，请求体为 {comment}', async () => {
    apiMock.mockResolvedValue({ run: sampleRun, result: {}, dispatch: {} })
    await resumeVpnDiagnosis('t-1', { comment: '继续' })
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnose/resume', {
      method: 'POST',
      body: JSON.stringify({ comment: '继续' }),
      signal: undefined,
    })
  })
})
