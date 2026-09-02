/** 领域类型：与后端 ticket_api / assets / knowledge / admin 返回结构对齐。 */

export type TicketStatus =
  | 'new'
  | 'intaking'
  | 'awaiting_customer'
  | 'classified'
  | 'answer_proposed'
  | 'awaiting_customer_confirmation'
  | 'queued'
  | 'assigned'
  | 'in_progress'
  | 'awaiting_approval'
  | 'resolved'
  | 'closed'
  | 'cancelled'

export type TicketPriority = 'low' | 'normal' | 'high' | 'urgent'

export interface Ticket {
  tenant_id?: string
  ticket_id: string
  requester_id: string
  channel: string
  external_ticket_id?: string | null
  title: string
  description: string
  status: TicketStatus
  priority: TicketPriority
  category?: string | null
  asset_id?: string | null
  assigned_team_id?: string | null
  assigned_user_id?: string | null
  version: number
  created_at: string
  updated_at: string
  resolved_at?: string | null
  closed_at?: string | null
}

export interface TicketListResult {
  items: Ticket[]
  next_cursor?: string | null
}

export interface SlaInfo {
  first_response_due_at?: string | null
  resolution_due_at?: string | null
  paused_at?: string | null
  first_responded_at?: string | null
}

export interface Citation {
  document_id: string
  document_version?: number
  chunk_id?: string
  title?: string | null
}

export interface SurveyInfo {
  status: string
  score?: number | null
  feedback?: string | null
}

export interface TicketMessage {
  message_id: string
  actor_id: string
  content: string
}

export interface IntakeInfo {
  category?: string | null
  subcategory?: string | null
  missing_fields?: string[]
  dispatch_reason_codes?: string[]
  answer_status?: string | null
  answer_reason_codes?: string[]
  auto_reply?: boolean | null
  identity_missing?: boolean
  risk_level?: string | null
}

export interface TicketOverview {
  sla?: SlaInfo | null
  citations?: Citation[]
  survey?: SurveyInfo | null
  messages?: TicketMessage[]
  intake?: IntakeInfo | null
  handoff_reasons?: string[]
}

/** 工单受理图中断时前端需要展示的信息（resume 时原样回传 interrupt_id）。 */
export interface PendingInterrupt {
  interrupt_id: string
  question?: string | null
  missing_fields?: string[]
}

export interface PendingInterruptResponse {
  interrupt?: PendingInterrupt | null
}

export interface Asset {
  asset_id: string
  asset_no: string
  asset_type: string
  name?: string | null
  hostname?: string | null
  department?: string | null
  owner_user_id?: string | null
}

export interface AssetListResult {
  items: Asset[]
}

export interface ItPolicy {
  category: string
  policy_id: string
  required_fields?: string[]
  default_priority?: TicketPriority
  approval_required?: boolean
  auto_answer_enabled?: boolean
  active?: boolean
  updated_at?: string | null
  created_at?: string | null
}

export interface ItPolicyListResult {
  items: ItPolicy[]
}

export interface KnowledgeChunk {
  chunk_id: string
  ordinal: number
  content: string
}

export interface KnowledgeDocument {
  document_id: string
  version: number
  title: string
  source_uri?: string | null
  status: string
  category?: string | null
  visibility: string
  allowed_departments?: string[] | null
  created_by?: string | null
  valid_from?: string | null
  valid_until?: string | null
  updated_at?: string | null
  chunk_count?: number | null
  embedding_status?: string | null
}

export interface KnowledgeDocumentListResult {
  items: KnowledgeDocument[]
}

export interface UploadDocumentInput {
  document: {
    document_id: string
    version: number
    title: string
    category?: string | null
    visibility: string
    allowed_departments?: string[]
    status: string
  }
  chunks: KnowledgeChunk[]
}

/** 智能助手 SSE 事件（协议见 backend/app.py 的 _execute_run）。 */
export type ChatEvent =
  | { type: 'text'; content: string }
  | { type: 'tool'; status: 'calling' | 'done' }
  | {
      type: 'interrupt'
      run_id: string
      thread_id: string
      interrupt_id: string
      question: string
    }
  | { type: 'end'; run_id: string }
  | { type: 'error'; code: string; run_id?: string; content: string }

/** Resolution Copilot：知识引用（与后端 CopilotCitation 对齐）。 */
export interface CopilotCitation {
  document_id: string
  document_version: number
  chunk_id: string
  title?: string | null
}

/** Copilot 草稿（copilot_drafts 表一行的前端形态）。 */
export interface CopilotDraft {
  draft_id: string
  ticket_id: string
  run_id: string
  draft_answer: string | null
  steps: string[]
  citations: CopilotCitation[]
  confidence: number
  needs_human_review: boolean
  status: string
  created_at: string
  approved_by?: string | null
  approved_at?: string | null
  /** 检索模式（阶段二）：lexical-only / hybrid；degraded 表示向量降级 */
  retrieval_mode?: string | null
  degraded?: boolean
}

export interface CopilotGenerateResult {
  run_id: string
  draft: CopilotDraft | null
  idempotent_replay: boolean
}

export interface CopilotLatestResult {
  draft: CopilotDraft | null
}
