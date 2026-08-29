import { test, expect } from '@playwright/test'
import {
  baseTicket,
  mockPendingInterrupt,
  mockTicketDetail,
  mockTicketList,
  mockTicketOverview,
  mockTransition,
} from './fixtures'

test.describe('工单详情处理流程', () => {
  test('打开工单详情并执行状态流转', async ({ page }) => {
    const ticket = { ...baseTicket, status: 'queued' as const }
    await mockTicketList(page, [ticket])
    await mockTicketDetail(page, ticket)
    await mockTicketOverview(page, ticket.ticket_id)
    await mockPendingInterrupt(page, ticket.ticket_id)
    await mockTransition(page, ticket, 'assigned')

    await page.goto('/tickets')

    await page.getByRole('button', { name: 'E2E 测试工单' }).click()
    await expect(page.locator('.detail h2')).toHaveText('E2E 测试工单')
    await expect(page.locator('.detail .status-queued')).toHaveText('待分派')

    await page.getByRole('button', { name: '接单' }).click()
    await expect(page.locator('.detail .status-assigned')).toHaveText('已分派')
  })
})
