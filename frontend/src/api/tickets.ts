/**
 * 工单 API 封装：前端所有工单相关请求的入口。
 *
 * 覆盖：列表/详情/概览、建单、启动与恢复受理图、状态流转、满意度回访、
 * 以及查询挂起中的受理 interrupt。请求统一经 `api()` 携带租户令牌与错误处理。
 */

import { api } from './client'
import type {
  PendingInterruptResponse,
  Ticket,
  TicketListResult,
  TicketOverview,
  TicketPriority,
} from '../types'

export interface TicketQuery {
  status?: string
  category?: string
  priority?: string
  q?: string
  assignedUserId?: string
  cursor?: string | null
  limit?: number
}

/** 游标分页查询工单；仅传非空筛选参数，limit 默认 30。 */
export function listTickets(
  query: TicketQuery,
  signal?: AbortSignal,
): Promise<TicketListResult> {
  const params = new URLSearchParams()
  if (query.status) params.set('status', query.status)
  if (query.category) params.set('category', query.category)
  if (query.priority) params.set('priority', query.priority)
  if (query.q?.trim()) params.set('q', query.q.trim())
  if (query.assignedUserId) params.set('assigned_user_id', query.assignedUserId)
  if (query.cursor) params.set('cursor', query.cursor)
  params.set('limit', String(query.limit ?? 30))
  return api<TicketListResult>(`/tickets?${params.toString()}`, { signal })
}

/** 查询单个工单详情。 */
export const getTicket = (ticketId: string, signal?: AbortSignal): Promise<Ticket> =>
  api(`/tickets/${ticketId}`, { signal })

/** 聚合概览：SLA、满意度、消息流、指派记录与 RAG 引用。 */
export const getTicketOverview = (
  ticketId: string,
  signal?: AbortSignal,
): Promise<TicketOverview> => api(`/tickets/${ticketId}/overview`, { signal })

export const getPendingInterrupt = (
  ticketId: string,
  signal?: AbortSignal,
): Promise<PendingInterruptResponse> =>
  api(`/tickets/${ticketId}/pending-interrupt`, { signal })

export interface CreateTicketInput {
  ticket_id?: string
  title: string
  description: string
  priority: TicketPriority
  asset_id?: string | null
}

/** 新建工单（Web 渠道），可选绑定资产。 */
export function createTicket(input: CreateTicketInput): Promise<Ticket> {
  return api('/tickets', {
    method: 'POST',
    body: JSON.stringify({ ...input, channel: 'web' }),
  })
}

export interface StartIntakeInput {
  operation_id: string
  text: string
  fields: Record<string, string>
  expected_version: number
}

/** 启动受理图；带 operation_id 与 expected_version 保证幂等与乐观锁。 */
export function startIntake(
  ticketId: string,
  input: StartIntakeInput,
): Promise<IntakeResult> {
  return api(`/tickets/${ticketId}/intake`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export interface IntakeResult {
  ticket: Ticket
  state: Record<string, unknown>
  interrupt?: PendingInterruptResponse['interrupt']
}

export interface TransitionInput {
  action: string
  actor_type: string
  expected_version: number
}

/** 工单状态流转（接单/处理/解决/关闭等）。 */
export function transitionTicket(ticketId: string, input: TransitionInput): Promise<Ticket> {
  return api(`/tickets/${ticketId}/transitions`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function createSurvey(
  ticketId: string,
  expiresInDays: number,
): Promise<{ survey_id: string; status: string }> {
  return api(`/tickets/${ticketId}/survey`, {
    method: 'POST',
    body: JSON.stringify({ expires_in_days: expiresInDays }),
  })
}

export interface ResumeIntakeInput {
  operation_id: string
  interrupt_id?: string
  ticket_id: string
  actor_type: string
  actor_id: string
  action: string
  expected_version: number
  payload: { fields: Record<string, string> }
}

/** 恢复被 interrupt 挂起的受理图（客户补充字段 / 审批人批准）。 */
export function resumeIntake(ticketId: string, input: ResumeIntakeInput): Promise<IntakeResult> {
  return api(`/tickets/${ticketId}/resume`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
