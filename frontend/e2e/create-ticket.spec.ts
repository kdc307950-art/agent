import { test, expect } from '@playwright/test'
import {
  baseTicket,
  mockAssets,
  mockPendingInterrupt,
  mockTicketDetail,
  mockTicketList,
  mockTicketOverview,
} from './fixtures'

test.describe('新建工单流程', () => {
  test('填写表单提交后，列表中出现新工单', async ({ page }) => {
    await mockAssets(page)
    await mockTicketList(page, [baseTicket])
    await mockTicketDetail(page, { ...baseTicket, status: 'intaking' })
    await mockTicketOverview(page, baseTicket.ticket_id)
    await mockPendingInterrupt(page, baseTicket.ticket_id)

    await page.route(
      (url) => url.pathname === '/api/tickets',
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        return route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify(baseTicket),
        })
      },
    )
    await page.route(
      (url) => url.pathname === `/api/tickets/${baseTicket.ticket_id}/intake`,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ticket: { ...baseTicket, status: 'intaking' }, state: {} }),
        })
      },
    )

    await page.goto('/tickets')

    await page.getByRole('button', { name: '新建', exact: true }).click()
    await expect(page.getByRole('heading', { name: '提交服务请求' })).toBeVisible()

    await page.getByPlaceholder('标题').fill('E2E 测试工单')
    await page.getByPlaceholder('问题描述').fill('自动化测试创建')
    await page.getByRole('button', { name: '提交工单' }).click()

    await expect(page.locator('.detail .status-intaking')).toHaveText('受理中')
    await expect(page.locator('.detail h2')).toHaveText('E2E 测试工单')
  })

  test('建单成功但受理失败时，可重试受理而不重复建单', async ({ page }) => {
    await mockAssets(page)
    await mockTicketList(page, [baseTicket])
    await mockTicketDetail(page, { ...baseTicket, status: 'intaking' })
    await mockTicketOverview(page, baseTicket.ticket_id)
    await mockPendingInterrupt(page, baseTicket.ticket_id)

    let createCount = 0
    await page.route(
      (url) => url.pathname === '/api/tickets',
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        createCount += 1
        return route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify(baseTicket),
        })
      },
    )

    let intakeCount = 0
    await page.route(
      (url) => url.pathname === `/api/tickets/${baseTicket.ticket_id}/intake`,
      async (route, request) => {
        if (request.method() !== 'POST') return route.continue()
        intakeCount += 1
        if (intakeCount === 1) {
          return route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({ detail: '受理服务不可用' }),
          })
        }
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ticket: { ...baseTicket, status: 'intaking' }, state: {} }),
        })
      },
    )

    await page.goto('/tickets')

    await page.getByRole('button', { name: '新建', exact: true }).click()
    await page.getByPlaceholder('标题').fill('E2E 测试工单')
    await page.getByPlaceholder('问题描述').fill('自动化测试创建')
    await page.getByRole('button', { name: '提交工单' }).click()

    await expect(page.locator('.form-error')).toHaveText('受理失败：受理服务不可用')
    await page.getByRole('button', { name: '重试受理' }).click()

    await expect(page.locator('.detail .status-intaking')).toHaveText('受理中')
    expect(createCount).toBe(1)
    expect(intakeCount).toBe(2)
  })
})
