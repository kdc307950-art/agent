import { expect, test } from '@playwright/test'
import {
  baseTicket,
  mockAssets,
  mockTicketList,
  mockTicketOverview,
} from './fixtures'

/**
 * Web 闭环 Mock 冒烟：创建 → 缺字段补问 → 补全恢复 → 接单 → 处理 → 解决 → 关闭。
 * 真实 API 版见 real-api.spec.ts（需 E2E_WEB_BASE/E2E_API_TOKEN）。
 */
test.describe('Web 闭环（Mock）', () => {
  test('创建、缺字段补问、恢复、接单、处理、解决、关闭完整链路', async ({ page }) => {
    const ticketId = 'ticket-web-loop'
    const ticket = {
      ...baseTicket,
      ticket_id: ticketId,
      title: 'VPN 无法连接',
      status: 'new',
      version: 0,
      category: 'it.vpn',
    }
    const awaiting = { ...ticket, status: 'awaiting_customer', version: 2 }
    const queued = { ...awaiting, status: 'queued', version: 5 }

    await mockAssets(page)
    await mockTicketList(page, [ticket])
    let currentTicket = awaiting
    await page.route(
      url => url.pathname === `/api/tickets/${ticketId}`,
      async route => {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(currentTicket) })
      },
    )
    await mockTicketOverview(page, ticketId)
    await page.route(
      url => url.pathname === `/api/tickets/${ticketId}/pending-interrupt`,
      async route => {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            interrupt: {
              interrupt_id: 'int-web-1',
              question: '请补充以下信息：device、operating_system、error_message、network',
              missing_fields: ['device', 'operating_system', 'error_message', 'network'],
            },
          }),
        })
      },
    )

    await page.route(
      url => url.pathname === '/api/tickets' && url.searchParams.size === 0,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(ticket) })
      },
    )
    await page.route(
      url => url.pathname === `/api/tickets/${ticketId}/intake`,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ticket: awaiting, state: { missing_fields: ['device'] }, interrupt: null }),
        })
      },
    )
    await page.route(
      url => url.pathname === `/api/tickets/${ticketId}/resume`,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        currentTicket = queued
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ticket: queued, state: {}, interrupt: null }) })
      },
    )
    await page.route(
      url => url.pathname === `/api/tickets/${ticketId}/transitions`,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        const body = request.postDataJSON()
        const next = {
          assign: 'assigned',
          start_work: 'in_progress',
          resolve: 'resolved',
          close: 'closed',
        }[body?.action] ?? body?.action ?? 'queued'
        currentTicket = { ...queued, status: next, version: 6 }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(currentTicket) })
      },
    )

    await page.goto('/tickets')
    await page.getByRole('button', { name: '新建', exact: true }).click()
    await page.getByPlaceholder('标题').fill('VPN 无法连接')
    await page.getByPlaceholder('问题描述').fill('错误码 809')
    await page.getByRole('button', { name: '提交工单' }).click()

    // 缺字段补问面板出现
    await expect(page.locator('.clarification-panel')).toBeVisible()
    const inputs = page.locator('.clarification-panel input')
    await expect(inputs).toHaveCount(4)
    await inputs.nth(0).fill('laptop-001')
    await inputs.nth(1).fill('Windows 11')
    await inputs.nth(2).fill('809')
    await inputs.nth(3).fill('办公网')
    await page.getByRole('button', { name: '提交补充' }).click()

    // 补充后恢复受理进入 queued
    await expect(page.locator('.detail .status-queued')).toHaveText('待分派')
    await page.getByRole('button', { name: '接单' }).click()
    await expect(page.locator('.detail .status-assigned')).toHaveText('已分派')
    await page.getByRole('button', { name: '开始处理' }).click()
    await expect(page.locator('.detail .status-in_progress')).toHaveText('处理中')
    await page.getByRole('button', { name: '标记解决' }).click()
    await expect(page.locator('.detail .status-resolved')).toHaveText('已解决')
    await page.getByRole('button', { name: '关闭工单' }).click()
    await expect(page.locator('.detail .status-closed')).toHaveText('已关闭')
  })
})
