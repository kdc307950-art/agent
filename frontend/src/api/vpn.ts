/**
 * VPN 诊断 API 封装（阶段二）。
 *
 * 覆盖后端新增的四条受控接口：
 *   - POST /tickets/{ticket_id}/vpn/diagnose               发起/重新发起一次诊断
 *   - GET  /tickets/{ticket_id}/vpn/diagnosis              查询当前工单最新诊断
 *   - POST /tickets/{ticket_id}/vpn/actions/{action_id}/result  回填单个客户排障步骤结果
 *   - POST /tickets/{ticket_id}/vpn/diagnose/resume        在客户回填后恢复诊断
 *
 * 请求统一经 `api()`（client.ts）携带租户 token 与错误处理；非 2xx 抛 ApiError。
 * 领域契约以后端（backend-engineer t2）最终落地为准；本层为骨架，字段与之对齐。
 */

import { api } from './client'
import type {
  VpnActionResult,
  VpnCustomerActionResult,
  VpnDiagnosisResult,
} from '../types'

/** 发起/恢复诊断的入参：operation_id 幂等，expected_version 做并发校验。 */
export interface VpnDiagnoseInput {
  operation_id: string
  expected_version: number
}

/** 回填单个客户排障步骤结果（details 为结构化 dict，可选）。 */
export interface VpnActionResultInput {
  result: VpnActionResult
  evidence?: string
  details?: Record<string, unknown>
}

/** 发起 VPN 诊断并返回完整诊断上下文（run + 客步骤 + 已回填结果）。 */
export function diagnoseVpn(
  ticketId: string,
  input: VpnDiagnoseInput,
  signal?: AbortSignal,
): Promise<VpnDiagnosisResult> {
  return api(`/tickets/${ticketId}/vpn/diagnose`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 查询当前工单最新诊断结果；无诊断时 run 为 null。 */
export function getVpnDiagnosis(
  ticketId: string,
  signal?: AbortSignal,
): Promise<VpnDiagnosisResult> {
  return api(`/tickets/${ticketId}/vpn/diagnosis`, { signal })
}

/** 回填单个客户排障步骤的执行结果。 */
export function submitVpnActionResult(
  ticketId: string,
  actionId: string,
  input: VpnActionResultInput,
  signal?: AbortSignal,
): Promise<VpnCustomerActionResult> {
  return api(`/tickets/${ticketId}/vpn/actions/${actionId}/result`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 客户回填后恢复诊断；返回新的完整诊断上下文。 */
export function resumeVpnDiagnosis(
  ticketId: string,
  input: VpnDiagnoseInput,
  signal?: AbortSignal,
): Promise<VpnDiagnosisResult> {
  return api(`/tickets/${ticketId}/vpn/diagnose/resume`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}
