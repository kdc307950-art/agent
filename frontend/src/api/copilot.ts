/** Resolution Copilot API：提交任务 / 状态轮询 / 草稿审批（阶段二异步 Worker）。 */

import { api } from './client'
import type { CopilotDraft, CopilotLatestResult } from '../types'

/** 生成 Copilot 处理建议；operation_id 幂等，expected_version 做并发校验。
 *
 * 阶段二：POST 只入队，立即返回 202 {"status":"queued|processing","run_id"}；
 * 模型执行由 CopilotWorker 异步完成，前端轮询 getCopilotRunStatus。
 * 503 = Copilot 未配置；409 = 工单已变更/失败运行。
 */
export interface CopilotGenerateInput {
  operation_id: string
  expected_version: number
}

/** POST 的 202 中间响应（任务已入队/处理中，前端轮询 run 状态）。 */
export interface CopilotQueuedResult {
  status: 'queued' | 'processing'
  run_id: string
}

/** GET /copilot/{run_id} 的完整运行状态（前端轮询目标）。 */
export interface CopilotRunStatus {
  run_id: string
  status: 'queued' | 'processing' | 'completed' | 'failed' | 'dead' | 'expired'
  draft: CopilotDraft | null
  draft_id: string | null
  error_code: string | null
  tool_calls: number
}

export function generateCopilot(
  ticketId: string,
  input: CopilotGenerateInput,
  signal?: AbortSignal,
): Promise<CopilotQueuedResult> {
  return api(`/tickets/${ticketId}/copilot`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 查询运行状态（前端轮询：直到 completed/failed/dead）。 */
export const getCopilotRunStatus = (
  ticketId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<CopilotRunStatus> =>
  api(`/tickets/${ticketId}/copilot/${runId}`, { signal })

/** 查询工单最新 Copilot 草稿（无则 draft 为 null；仅展示，不用于幂等）。 */
export const getCopilotLatest = (
  ticketId: string,
  signal?: AbortSignal,
): Promise<CopilotLatestResult> =>
  api(`/tickets/${ticketId}/copilot/latest`, { signal })

/** 审批草稿（generated -> approved）；只做状态迁移，不发送消息。 */
export function approveCopilotDraft(
  ticketId: string,
  draftId: string,
  note?: string,
): Promise<{ draft_id: string; status: string }> {
  return api(`/tickets/${ticketId}/copilot/${draftId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ note: note ?? null }),
  })
}
