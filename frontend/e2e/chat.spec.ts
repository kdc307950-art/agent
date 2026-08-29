import { test, expect } from '@playwright/test'

function buildSseBody(events: object[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('')
}

function routeChatStream(page: import('@playwright/test').Page, events: object[]) {
  return page.route((url) => url.toString().includes('/api/chat/stream'), async (route, request) => {
    if (request.method() !== 'POST') return route.continue()
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: buildSseBody(events),
    })
  })
}

function routeChatResume(page: import('@playwright/test').Page, events: object[]) {
  return page.route((url) => url.toString().includes('/api/chat/resume'), async (route, request) => {
    if (request.method() !== 'POST') return route.continue()
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: buildSseBody(events),
    })
  })
}

test.describe('智能助手对话', () => {
  test('流式输出文本并渲染', async ({ page }) => {
    await routeChatStream(page, [
      { type: 'text', content: '你好' },
      { type: 'text', content: '，世界' },
      { type: 'end', run_id: 'run-e2e-1' },
    ])

    await page.goto('/assistant')
    await page.getByPlaceholder('输入问题').fill('Hello')
    await page.getByRole('button', { name: '发送' }).click()

    await expect(page.getByText('Hello')).toBeVisible()
    await expect(page.getByText('你好，世界')).toBeVisible()
  })

  test('收到 interrupt 时显示审批卡片，确认后继续', async ({ page }) => {
    await routeChatStream(page, [
      { type: 'tool', status: 'calling' },
      {
        type: 'interrupt',
        run_id: 'run-e2e-1',
        thread_id: 't-e2e',
        interrupt_id: 'int-e2e-1',
        question: '是否需要我帮你创建一张工单？',
      },
    ])

    await routeChatResume(page, [
      { type: 'text', content: '已为你创建工单' },
      { type: 'end', run_id: 'run-e2e-2' },
    ])

    await page.goto('/assistant')
    await page.getByPlaceholder('输入问题').fill('帮我建单')
    await page.getByRole('button', { name: '发送' }).click()

    await expect(page.getByText('是否需要我帮你创建一张工单？')).toBeVisible()
    await page.getByRole('button', { name: '同意' }).click()

    await expect(page.getByText('已为你创建工单')).toBeVisible()
  })
})
