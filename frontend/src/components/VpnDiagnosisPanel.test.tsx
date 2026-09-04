import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VpnDiagnosisPanel from './VpnDiagnosisPanel'
import * as vpnApi from '../api/vpn'
import { ApiError } from '../api/client'
import type { VpnDiagnosisRun } from '../types'

const completedRun: VpnDiagnosisRun = {
  run_id: 'diag_abc',
  ticket_id: 't-1',
  fault: 'configuration_error',
  hypothesis: '客户端配置版本过旧',
  confidence: 0.9,
  evidence: [
    { tool_name: 'get_vpn_account_status', evidence: '账号 active', title: '账号正常', found: true },
  ],
  ruled_out: ['gateway_down'],
  next_action: 'provide_steps',
  reason_codes: ['client_version_too_old'],
  status: 'completed',
  created_at: new Date().toISOString(),
}

const actions = [
  {
    action_id: 'a-1',
    title: '重启客户端',
    instruction: '退出并重新登录客户端',
    expected_result: '恢复连接',
    risk_level: 'low' as const,
    requires_agent: false,
  },
]

function snapshot() {
  return {
    ticket_id: 't-1',
    latest_run: completedRun,
    runs: [completedRun],
    actions,
    results: [],
    escalations: [],
  }
}

// getVpnDiagnosis 返回完整快照；diagnose 返回 run/result/dispatch
const diagnoseResp = {
  run: completedRun,
  result: { must_handoff: false },
  dispatch: { ok: true, command: 'provide_steps', status: 'awaiting_customer_action', actions },
}

describe('VpnDiagnosisPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('拉取到快照后展示故障假设/证据/排除项/置信度/步骤', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockResolvedValue(snapshot())
    render(<VpnDiagnosisPanel ticketId="t-1" enabled />)

    await waitFor(() => expect(screen.getByText('VPN 诊断')).toBeInTheDocument())
    expect(screen.getByText('客户端配置版本过旧')).toBeInTheDocument()
    expect(screen.getByText('账号 active')).toBeInTheDocument()
    expect(screen.getByText('gateway_down')).toBeInTheDocument()
    expect(screen.getByText(/置信度 90%/)).toBeInTheDocument()
    expect(screen.getByText('重启客户端')).toBeInTheDocument()
  })

  it('无诊断时显示"发起 VPN 诊断"并触发 diagnoseVpn', async () => {
    // 首次快照：尚无诊断 -> 展示"发起"；发起后再次拉取 -> 有诊断
    vi.spyOn(vpnApi, 'getVpnDiagnosis')
      .mockResolvedValueOnce({
        ticket_id: 't-1',
        latest_run: null,
        runs: [],
        actions: [],
        results: [],
        escalations: [],
      })
      .mockResolvedValue(snapshot())
    const diagnoseSpy = vi.spyOn(vpnApi, 'diagnoseVpn').mockResolvedValue(diagnoseResp)
    render(<VpnDiagnosisPanel ticketId="t-1" enabled />)

    await waitFor(() => expect(screen.getByText(/尚未发起 VPN 诊断/)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: '发起 VPN 诊断' }))
    expect(diagnoseSpy).toHaveBeenCalledWith('t-1', expect.any(AbortSignal))
    await waitFor(() => expect(screen.getByText('重启客户端')).toBeInTheDocument())
  })

  it('回填步骤结果后调用 submitVpnActionResult 并刷新快照', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockResolvedValue(snapshot())
    const submitSpy = vi.spyOn(vpnApi, 'submitVpnActionResult').mockResolvedValue({
      action_result: { action_id: 'a-1', result: '已重启', evidence: {}, details: '', submitted_at: new Date().toISOString() },
      transition: true,
      re_diagnosis: null,
    })
    render(<VpnDiagnosisPanel ticketId="t-1" enabled />)

    await waitFor(() => expect(screen.getByText('重启客户端')).toBeInTheDocument())
    // 填写结果并提交（result 为自由文本，必填）
    await userEvent.type(screen.getByLabelText('结果'), '已重启')
    await userEvent.click(screen.getByRole('button', { name: '提交结果' }))

    await waitFor(() =>
      expect(submitSpy).toHaveBeenCalledWith('t-1', 'a-1', {
        result: '已重启',
        evidence: {},
        details: '',
      }),
    )
  })

  it('后端未就绪（getVpnDiagnosis 报 503）时展示"未配置"提示', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockRejectedValue(
      new ApiError('VPN 处置闭环服务尚未初始化', 503),
    )
    render(<VpnDiagnosisPanel ticketId="t-1" enabled />)

    await waitFor(() =>
      expect(screen.getByText(/VPN 诊断服务未配置/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/尚未发起 VPN 诊断/)).toBeInTheDocument()
  })
})
