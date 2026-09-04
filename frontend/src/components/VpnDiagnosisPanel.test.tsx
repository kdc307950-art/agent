import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VpnDiagnosisPanel from './VpnDiagnosisPanel'
import * as vpnApi from '../api/vpn'
import { ApiError } from '../api/client'
import type { VpnDiagnosisRun } from '../types'

const completedRun: VpnDiagnosisRun = {
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

const customerActions = [
  {
    action_id: 'a-1',
    title: '重启客户端',
    instruction: '退出并重新登录客户端',
    expected_result: '恢复连接',
    risk_level: 'low' as const,
    requires_agent: false,
  },
]

describe('VpnDiagnosisPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('拉取到诊断后展示故障假设/证据/排除项/置信度/步骤', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockResolvedValue({
      run: completedRun,
      customer_actions: customerActions,
      action_results: [],
    })
    render(<VpnDiagnosisPanel ticketId="t-1" expectedVersion={1} enabled />)

    await waitFor(() => expect(screen.getByText('VPN 诊断')).toBeInTheDocument())
    expect(screen.getByText('客户端配置版本过旧')).toBeInTheDocument()
    expect(screen.getByText('账号正常')).toBeInTheDocument()
    expect(screen.getByText('网关异常')).toBeInTheDocument()
    expect(screen.getByText(/置信度 90%/)).toBeInTheDocument()
    expect(screen.getByText('重启客户端')).toBeInTheDocument()
  })

  it('无诊断时显示"发起 VPN 诊断"并触发 diagnoseVpn', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockResolvedValue({ run: null })
    const diagnoseSpy = vi.spyOn(vpnApi, 'diagnoseVpn').mockResolvedValue({
      run: completedRun,
      customer_actions: customerActions,
      action_results: [],
    })
    render(<VpnDiagnosisPanel ticketId="t-1" expectedVersion={1} enabled />)

    await waitFor(() => expect(screen.getByText(/尚未发起 VPN 诊断/)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: '发起 VPN 诊断' }))
    expect(diagnoseSpy).toHaveBeenCalledWith(
      't-1',
      { operation_id: '00000000-0000-0000-0000-000000000000', expected_version: 1 },
      expect.any(AbortSignal),
    )
    await waitFor(() => expect(screen.getByText('重启客户端')).toBeInTheDocument())
  })

  it('回填步骤结果后调用 submitVpnActionResult 并刷新诊断', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockResolvedValue({
      run: completedRun,
      customer_actions: customerActions,
      action_results: [],
    })
    const submitSpy = vi.spyOn(vpnApi, 'submitVpnActionResult').mockResolvedValue({
      action_id: 'a-1',
      result: 'success',
      submitted_at: new Date().toISOString(),
    })
    render(<VpnDiagnosisPanel ticketId="t-1" expectedVersion={1} enabled />)

    await waitFor(() => expect(screen.getByText('重启客户端')).toBeInTheDocument())
    // 选择"成功"并填写证据
    await userEvent.selectOptions(screen.getByLabelText('结果'), 'success')
    await userEvent.type(screen.getByLabelText('证据'), '已重启')
    await userEvent.click(screen.getByRole('button', { name: '提交结果' }))

    await waitFor(() =>
      expect(submitSpy).toHaveBeenCalledWith(
        't-1',
        'a-1',
        expect.objectContaining({ result: 'success', evidence: '已重启' }),
        undefined,
      ),
    )
  })

  it('后端未就绪（getVpnDiagnosis 报 503）时展示"未配置"提示', async () => {
    vi.spyOn(vpnApi, 'getVpnDiagnosis').mockRejectedValue(
      new ApiError('VPN 诊断服务尚未初始化', 503),
    )
    render(<VpnDiagnosisPanel ticketId="t-1" expectedVersion={1} enabled />)

    await waitFor(() =>
      expect(screen.getByText(/VPN 诊断服务未配置/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/尚未发起 VPN 诊断/)).toBeInTheDocument()
  })
})
