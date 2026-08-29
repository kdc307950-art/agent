/** 工单 API。 */

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

export const getTicket = (ticketId: string, signal?: AbortSignal): Promise<Ticket> =>
  api(`/tickets/${ticketId}`, { signal })

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

export function resumeIntake(ticketId: string, input: ResumeIntakeInput): Promise<IntakeResult> {
  return api(`/tickets/${ticketId}/resume`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
