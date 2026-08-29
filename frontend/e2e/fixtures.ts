import type { Page } from '@playwright/test'
import type { Ticket } from '../src/types'

export const baseTicket: Ticket = {
  ticket_id: 'ticket-e2e-1',
  requester_id: 'user-e2e',
  channel: 'web',
  title: 'E2E 测试工单',
  description: '自动化测试创建',
  status: 'new',
  priority: 'normal',
  version: 1,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

export function mockTicketList(page: Page, items: Ticket[]) {
  return page.route(
    (url) => {
      const path = url.pathname
      return path === '/api/tickets'
    },
    async (route, request) => {
      const url = new URL(request.url())
      const q = url.searchParams.get('q') ?? ''
      const filtered = items.filter(
        (t) =>
          t.title.includes(q) ||
          t.description.includes(q) ||
          t.ticket_id.includes(q),
      )
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: filtered, next_cursor: null }),
      })
    },
  )
}

export function mockTicketDetail(page: Page, ticket: Ticket) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticket.ticket_id}`,
    async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ticket),
      })
    },
  )
}

export function mockTicketOverview(page: Page, ticketId: string) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticketId}/overview`,
    async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({}),
      })
    },
  )
}

export function mockPendingInterrupt(page: Page, ticketId: string) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticketId}/pending-interrupt`,
    async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ interrupt: null }),
      })
    },
  )
}

export function mockAssets(page: Page) {
  return page.route(
    (url) => url.pathname === '/api/assets',
    async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [] }),
      })
    },
  )
}

export function mockCreateTicket(page: Page, ticket: Ticket) {
  return page.route(
    (url) => url.pathname === '/api/tickets',
    async (route, request) => {
      if (request.method() !== 'POST') return route.continue()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(ticket),
      })
    },
  )
}

export function mockStartIntake(page: Page, ticket: Ticket) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticket.ticket_id}/intake`,
    async (route, request) => {
      if (request.method() !== 'POST') return route.continue()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ticket: { ...ticket, status: 'intaking' }, state: {} }),
      })
    },
  )
}

export function mockTransition(page: Page, ticket: Ticket, nextStatus: string) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticket.ticket_id}/transitions`,
    async (route, request) => {
      if (request.method() !== 'POST') return route.continue()
      const updated = { ...ticket, status: nextStatus, version: ticket.version + 1 }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(updated),
      })
    },
  )
}

/** Copilot 生成的草稿 fixture（结构对齐后端 copilot_drafts 返回）。 */
export interface MockCopilotDraft {
  draft_id: string
  ticket_id: string
  run_id: string
  draft_answer: string | null
  steps: string[]
  citations: { document_id: string; document_version: number; chunk_id: string; title?: string | null }[]
  confidence: number
  needs_human_review: boolean
  status: string
  created_at: string
}

export function mockCopilotGenerate(
  page: Page,
  ticketId: string,
  draft: MockCopilotDraft,
) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticketId}/copilot`,
    async (route, request) => {
      if (request.method() !== 'POST') return route.continue()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          run_id: draft.run_id,
          draft,
          idempotent_replay: false,
        }),
      })
    },
  )
}

export function mockCopilotLatest(page: Page, ticketId: string, draft: MockCopilotDraft | null) {
  return page.route(
    (url) => url.pathname === `/api/tickets/${ticketId}/copilot/latest`,
    async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ draft }),
      })
    },
  )
}

export function mockCopilotApprove(page: Page, ticketId: string) {
  return page.route(
    (url) =>
      url.pathname.startsWith(`/api/tickets/${ticketId}/copilot/`) &&
      url.pathname.endsWith('/approve'),
    async (route, request) => {
      if (request.method() !== 'POST') return route.continue()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ draft_id: 'draft-1', status: 'approved' }),
      })
    },
  )
}
