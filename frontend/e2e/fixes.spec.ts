/**
 * 阶段五/六/七 E2E：我的处理筛选、桌面侧栏键盘可达、移动端搜索。
 *
 * 覆盖审查修复：
 * - view=mine 发送 assigned_user_id=current_user（筛选生效）
 * - 桌面端侧栏不设 inert，可键盘 Tab 访问
 * - 移动端搜索按钮展开输入框并过滤列表
 */
import { test, expect } from '@playwright/test'
import { baseTicket, mockTicketList } from './fixtures'

test.describe('筛选与无障碍修复', () => {
  test('我的处理视图发送 assigned_user_id=current_user', async ({ page }) => {
    const ticket = { ...baseTicket, status: 'assigned' as const }
    let captured: URL | null = null
    await page.route(
      (url) => url.pathname === '/api/tickets',
      async (route) => {
        captured = new URL(route.request().url())
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [ticket], next_cursor: null }),
        })
      },
    )

    await page.goto('/tickets?view=mine')
    await expect(page.locator('.ticket-row')).toHaveCount(1)
    expect(captured).not.toBeNull()
    expect(captured!.searchParams.get('assigned_user_id')).toBe('current_user')
  })

  test('桌面端侧栏可键盘聚焦（不设 inert）', async ({ page }) => {
    // 强制桌面视口（>900px），确保不受移动端抽屉逻辑影响
    await page.setViewportSize({ width: 1280, height: 800 })
    const ticket = { ...baseTicket, status: 'new' as const }
    await mockTicketList(page, [ticket])
    await page.goto('/tickets')
    await expect(page.locator('.sidebar')).toBeVisible()

    // 桌面视口：侧栏不应有 inert / aria-hidden
    const sidebar = page.locator('.sidebar')
    expect(await sidebar.getAttribute('inert')).toBeNull()
    expect(await sidebar.getAttribute('aria-hidden')).toBeNull()

    // Tab 应能聚焦到侧栏内的导航按钮（工单队列）
    await page.keyboard.press('Tab')
    await expect(page.locator('.sidebar button').first()).toBeFocused()
  })

  test('移动端侧栏关闭时不可聚焦（inert）', async ({ page }) => {
    const ticket = { ...baseTicket, status: 'new' as const }
    await mockTicketList(page, [ticket])
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/tickets')

    // 移动端且未展开：侧栏应 inert
    const sidebar = page.locator('.sidebar')
    await expect(sidebar).toHaveAttribute('inert', '')
    await expect(sidebar).toHaveAttribute('aria-hidden', 'true')

    // Tab 不能聚焦到侧栏内元素（inert 阻止）
    await page.keyboard.press('Tab')
    await page.keyboard.press('Tab')
    await expect(sidebar.locator('button').first()).not.toBeFocused()
  })

  test('移动端搜索按钮展开输入框并按关键词过滤', async ({ page }) => {
    const ticketA = { ...baseTicket, ticket_id: 't-vpn', title: 'VPN 无法连接', status: 'new' as const }
    const ticketB = { ...baseTicket, ticket_id: 't-mail', title: '邮箱配置问题', status: 'new' as const }
    await mockTicketList(page, [ticketA, ticketB])
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/tickets')

    // 移动端（≤540px 是 390px）：桌面搜索框隐藏，出现搜索切换按钮
    await expect(page.locator('.desktop-search')).toBeHidden()
    const toggle = page.getByRole('button', { name: '展开搜索' })
    await expect(toggle).toBeVisible()

    // 点击展开搜索框并输入 VPN
    await toggle.click()
    const mobileSearch = page.locator('.mobile-search input')
    await expect(mobileSearch).toBeVisible()
    await mobileSearch.fill('VPN')

    // 列表按关键词过滤（fixtures 的 mock 按 title 匹配）
    await expect(page.locator('.ticket-row')).toHaveCount(1)
    await expect(page.locator('.ticket-row').getByText('VPN 无法连接')).toBeVisible()
  })
})
