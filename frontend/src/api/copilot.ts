/** Resolution Copilot API：生成建议 / 最新草稿 / 审批（阶段四）。 */

import { api } from './client'
import type {
  CopilotGenerateResult,
  CopilotLatestResult,
} from '../types'

/** 生成 Copilot 处理建议；operation_id 幂等，expected_version 做并发校验。 */
export interface CopilotGenerateInput {
  operation_id: string
  expected_version: number
}

export function generateCopilot(
  ticketId: string,
  input: CopilotGenerateInput,
  signal?: AbortSignal,
): Promise<CopilotGenerateResult> {
  return api(`/tickets/${ticketId}/copilot`, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 查询工单最新 Copilot 草稿（无则 draft 为 null）。 */
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
