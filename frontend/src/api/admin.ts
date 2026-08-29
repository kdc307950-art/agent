/** 管理后台（IT 策略）API。 */

import { api } from './client'
import type { ItPolicy, ItPolicyListResult, TicketPriority } from '../types'

export function listItPolicies(): Promise<ItPolicyListResult> {
  return api('/admin/it/policies')
}

export function getItPolicy(category: string): Promise<ItPolicy> {
  return api(`/admin/it/policies/${encodeURIComponent(category)}`)
}

export function deleteItPolicy(category: string): Promise<{ category: string; deleted: boolean }> {
  return api(`/admin/it/policies/${encodeURIComponent(category)}`, { method: 'DELETE' })
}

export interface UpsertItPolicyInput {
  category: string
  policy_id: string
  required_fields: string[]
  default_priority: TicketPriority
  approval_required: boolean
  auto_answer_enabled: boolean
}

export function upsertItPolicy(category: string, input: UpsertItPolicyInput): Promise<ItPolicy> {
  return api(`/admin/it/policies/${encodeURIComponent(category)}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}
