import { expect, test } from '@playwright/test'
import { randomUUID } from 'node:crypto'

/**
 * 真实 /api/tickets 冒烟链路：创建 → 读取。
 *
 * 需要 E2E_WEB_BASE（如 http://127.0.0.1:8000）与 E2E_API_TOKEN（customer 令牌）。
 * 未配置时 skip —— 常规 CI 的 Playwright 仍然只依赖 Mock，不误报失败。
 */
const base = process.env.E2E_WEB_BASE
const token = process.env.E2E_API_TOKEN

test.describe('@real-api 真实 API 冒烟（需 E2E_WEB_BASE / E2E_API_TOKEN）', () => {
  test('创建并读取 /api/tickets', async ({ request }) => {
    test.skip(!base || !token, 'E2E_WEB_BASE / E2E_API_TOKEN 未配置，跳过真实 API 测试')
    const headers = { Authorization: `Bearer ${token}` }
    const ticketId = `e2e-${randomUUID()}`
    const create = await request.post(`${base}/api/tickets`, {
      headers,
      data: {
        ticket_id: ticketId,
        title: 'Real API ticket',
        description: 'created by real api spec',
      },
    })
    expect(create.status()).toBe(201)
    const created = await create.json()
    expect(created.ticket_id).toBe(ticketId)

    const read = await request.get(`${base}/api/tickets/${ticketId}`, { headers })
    expect(read.status()).toBe(200)
    const got = await read.json()
    expect(got.ticket_id).toBe(ticketId)
  })
})
