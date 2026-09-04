import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  diagnoseVpn,
  getVpnDiagnosis,
  resumeVpnDiagnosis,
  submitVpnActionResult,
} from './vpn'
import type { VpnDiagnosisRun } from '../types'

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

const sampleRun: VpnDiagnosisRun = {
  run_id: 'run-1',
  ticket_id: 't-1',
  fault: 'configuration_error',
  hypothesis: '客户端配置版本过旧',
  confidence: 0.9,
  evidence: [
    { finding_type: '账号正常', description: '账号 active', source_tool: 'get_vpn_account_status' },
  ],
  ruled_out: [
    { finding_type: '网关异常', description: '网关 up', source_tool: 'get_vpn_gateway_status' },
  ],
  next_action: '请客户升级客户端后重试',
  reason_codes: ['client_version_too_old'],
  status: 'completed',
  created_at: new Date().toISOString(),
}

describe('vpn API 封装', () => {
  beforeEach(() => {
    apiMock.mockReset()
  })

  it('diagnoseVpn 走 POST /tickets/{id}/vpn/diagnose 并携带 body', async () => {
    apiMock.mockResolvedValue({ run: sampleRun, customer_actions: [] })
    const result = await diagnoseVpn('t-1', { operation_id: 'op-1', expected_version: 1 })
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnose', {
      method: 'POST',
      body: JSON.stringify({ operation_id: 'op-1', expected_version: 1 }),
      signal: undefined,
    })
    expect(result.run).toEqual(sampleRun)
  })

  it('getVpnDiagnosis 走 GET /tickets/{id}/vpn/diagnosis', async () => {
    apiMock.mockResolvedValue({ run: null })
    const result = await getVpnDiagnosis('t-1')
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnosis', { signal: undefined })
    expect(result).toEqual({ run: null })
  })

  it('submitVpnActionResult 走 POST /tickets/{id}/vpn/actions/{aid}/result，details 为 dict', async () => {
    apiMock.mockResolvedValue({
      action_id: 'a-1',
      result: 'success',
      evidence: '已重启',
      details: null,
      submitted_at: new Date().toISOString(),
    })
    await submitVpnActionResult('t-1', 'a-1', {
      result: 'success',
      evidence: '已重启',
      details: { note: '连接恢复' },
    })
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/actions/a-1/result', {
      method: 'POST',
      body: JSON.stringify({ result: 'success', evidence: '已重启', details: { note: '连接恢复' } }),
      signal: undefined,
    })
  })

  it('resumeVpnDiagnosis 走 POST /tickets/{id}/vpn/diagnose/resume', async () => {
    apiMock.mockResolvedValue({ run: sampleRun, customer_actions: [] })
    await resumeVpnDiagnosis('t-1', { operation_id: 'op-2', expected_version: 2 })
    expect(apiMock).toHaveBeenCalledWith('/tickets/t-1/vpn/diagnose/resume', {
      method: 'POST',
      body: JSON.stringify({ operation_id: 'op-2', expected_version: 2 }),
      signal: undefined,
    })
  })
})
