/**
 * VPN 诊断 API 封装（阶段二）。
 *
 * 对齐 backend/vpn/api_v2.py 落地契约（backend/vpn/closed_loop.py 服务编排）：
 *   - POST /tickets/{ticket_id}/vpn/diagnose               发起一次诊断，无请求体
 *   - GET  /tickets/{ticket_id}/vpn/diagnosis              查询处置闭环快照
 *   - POST /tickets/{ticket_id}/vpn/actions/{action_id}/result  客户回填一条排查步骤结果
 *   - POST /tickets/{ticket_id}/vpn/diagnose/resume        客户动作后再次诊断（体 {comment}）
 *
 * 请求统一经 `api()`（client.ts）携带租户 token 与错误处理；非 2xx 抛 ApiError。
 */

import { api } from './client'
import type {
  VpnCustomerActionResult,
  VpnDiagnosisRun,
  VpnDiagnosisSnapshot,
} from '../types'

/** 回填单个客户排查步骤结果的请求体（诊断 API 的 VpnActionResultRequest）。 */
export interface VpnActionResultInput {
  /** 自由文本：客户提供的执行结果。 */
  result: string
  /** 结构化证据 dict（可选，默认 {}）。 */
  evidence?: Record<string, unknown>
  /** 文本备注（可选，默认 ""）。 */
  details?: string
}

/** 再次诊断请求体（诊断 API 的 VpnResumeRequest，暂为保留结构）。 */
export interface VpnResumeInput {
  comment?: string
}

/** POST /vpn/diagnose 或 /vpn/diagnose/resume 的响应：run + result + dispatch。 */
export interface VpnDiagnoseResponse {
  run: VpnDiagnosisRun
  result: Record<string, unknown>
  dispatch: Record<string, unknown>
}

/** POST /vpn/actions/{id}/result 的响应：action_result + transition + re_diagnosis。 */
export interface VpnActionResultResponse {
  action_result: VpnCustomerActionResult
  transition: boolean
  re_diagnosis?: VpnDiagnoseResponse | null
}

/** 发起一次 VPN 诊断（无请求体）；返回 run / result / dispatch。 */
export function diagnoseVpn(
  ticketId: string,
  signal?: AbortSignal,
): Promise<VpnDiagnoseResponse> {
  return api(`/tickets/${ticketId}/vpn/diagnose`, {
    method: 'POST',
    signal,
  })
}

/** 查询当前工单的 VPN 处置闭环快照（含 latest_run / runs / actions / results / escalations）。 */
export function getVpnDiagnosis(
  ticketId: string,
  signal?: AbortSignal,
): Promise<VpnDiagnosisSnapshot> {
  return api(`/tickets/${ticketId}/vpn/diagnosis`, { signal })
}

/** 回填单个客户排查步骤的执行结果。 */
export function submitVpnActionResult(
  ticketId: string,
  actionId: string,
  input: VpnActionResultInput,
  signal?: AbortSignal,
): Promise<VpnActionResultResponse> {
  return api(`/tickets/${ticketId}/vpn/actions/${actionId}/result`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 客户回填后再次诊断（新 run）；请求体为 {comment}。 */
export function resumeVpnDiagnosis(
  ticketId: string,
  input: VpnResumeInput = {},
  signal?: AbortSignal,
): Promise<VpnDiagnoseResponse> {
  return api(`/tickets/${ticketId}/vpn/diagnose/resume`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}
