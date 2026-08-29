/**
 * Resolution Copilot 演示流程（Playwright，mock API）。
 *
 * 流程（PRD 第十二节演示）：
 * 打开已派单工单（assigned）→ 点击"生成 AI 处理建议"→ 查看处理步骤与知识引用
 * → 采用草稿 → 复制到剪贴板（人工确认发送）→ 工单不被自动改变状态
 */
import { test, expect } from '@playwright/test'
import {
  baseTicket,
  mockCopilotApprove,
  mockCopilotGenerate,
  mockCopilotLatest,
  mockPendingInterrupt,
  mockTicketDetail,
  mockTicketList,
  mockTicketOverview,
  type MockCopilotDraft,
} from './fixtures'

test.describe('Resolution Copilot 演示流程', () => {
  test('生成处理建议、采用草稿、标记已核对', async ({ page }) => {
    const ticket = { ...baseTicket, status: 'assigned' as const, version: 3 }
    const draft: MockCopilotDraft = {
      draft_id: 'draft-copilot-1',
      ticket_id: ticket.ticket_id,
      run_id: 'run-copilot-1',
      draft_answer: '建议先确认客户端时间和网络类型，然后重新导入 VPN 配置。',
      steps: ['确认设备和操作系统', '确认当前网络是否为公司内网', '重新导入 VPN 配置'],
      citations: [
        {
          document_id: 'it-vpn-guide',
          document_version: 2,
          chunk_id: 'vpn-03',
          title: 'VPN 配置指南',
        },
      ],
      confidence: 0.91,
      needs_human_review: false,
      status: 'generated',
      created_at: new Date().toISOString(),
    }

    await mockTicketList(page, [ticket])
    await mockTicketDetail(page, ticket)
    await mockTicketOverview(page, ticket.ticket_id)
    await mockPendingInterrupt(page, ticket.ticket_id)
    await mockCopilotGenerate(page, ticket.ticket_id, draft)
    await mockCopilotLatest(page, ticket.ticket_id, draft)
    await mockCopilotApprove(page, ticket.ticket_id)

    await page.goto('/tickets')

    // 打开已派单工单详情
    await page.getByRole('button', { name: 'E2E 测试工单' }).click()
    await expect(page.locator('.detail h2')).toHaveText('E2E 测试工单')

    // Copilot 面板：assigned 状态可见"生成 AI 处理建议"
    const copilotSection = page.locator('.copilot-panel')
    await expect(copilotSection).toBeVisible()

    // 点击生成：显示处理步骤与引用
    await page.getByRole('button', { name: '生成 AI 处理建议' }).click()
    await expect(copilotSection.getByText('处理步骤')).toBeVisible()
    // 步骤列表中的条目（exact 匹配避免与草稿正文重复命中）
    await expect(copilotSection.locator('ol li').getByText('重新导入 VPN 配置', { exact: true })).toBeVisible()
    await expect(copilotSection.getByText('VPN 配置指南')).toBeVisible()
    await expect(copilotSection.getByText('AI 草稿，发送前必须由客服确认')).toBeVisible()

    // 采用草稿：填充到"已采用草稿"区（仅填充，不自动发送）
    await page.getByRole('button', { name: '采用草稿' }).click()
    await expect(page.locator('.adopted-reply')).toBeVisible()
    await expect(
      page.locator('.adopted-reply').getByText('建议先确认客户端时间和网络类型'),
    ).toBeVisible()

    // 标记已核对：草稿状态 approved（不发送消息、不改变工单状态）
    await page.getByRole('button', { name: '标记已核对' }).click()
    await expect(page.getByText('待客服发送')).toBeVisible()

    // 工单状态仍是 assigned（Copilot 不改变状态机）—— 限定在详情面板内断言
    await expect(page.locator('.detail .status-assigned')).toHaveText('已分派')
  })
})
