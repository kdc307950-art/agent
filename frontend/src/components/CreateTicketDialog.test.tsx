import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CreateTicketDialog from './CreateTicketDialog'
import * as api from '../api'
import type { Ticket } from '../types'

const mockTicket: Ticket = {
  ticket_id: 'ticket-1',
  requester_id: 'user-1',
  channel: 'web',
  title: 'VPN 故障',
  description: '无法连接',
  status: 'new',
  priority: 'normal',
  version: 1,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

describe('CreateTicketDialog', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listAssets').mockResolvedValue({ items: [] })
  })

  it('建单成功但受理失败时，重试不会再次建单', async () => {
    const createSpy = vi.spyOn(api, 'createTicket').mockResolvedValue(mockTicket)
    const intakeSpy = vi
      .spyOn(api, 'startIntake')
      .mockRejectedValueOnce(new Error('受理服务不可用'))
      .mockResolvedValueOnce({ ticket: { ...mockTicket, status: 'intaking' }, state: {} })

    const onCreated = vi.fn()
    render(<CreateTicketDialog open onClose={vi.fn()} onCreated={onCreated} />)

    // 等待资产加载完成，避免异步状态更新触发 act 警告
    await waitFor(() => {
      expect(screen.queryByText(/正在加载资产/)).not.toBeInTheDocument()
    })

    await userEvent.type(screen.getByPlaceholderText('标题'), 'VPN 故障')
    await userEvent.type(screen.getByPlaceholderText('问题描述'), '无法连接')
    await userEvent.click(screen.getByRole('button', { name: /提交工单/ }))

    await waitFor(() => {
      expect(screen.getByText('受理失败：受理服务不可用')).toBeInTheDocument()
    })

    // createTicket 只应被调用一次
    expect(createSpy).toHaveBeenCalledTimes(1)
    expect(createSpy).toHaveBeenCalledWith(
      expect.objectContaining({ ticket_id: expect.any(String), title: 'VPN 故障' }),
    )

    // 点击重试受理
    await userEvent.click(screen.getByRole('button', { name: /重试受理/ }))

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ status: 'intaking' }))
    })

    // 重试受理时不能再建单
    expect(createSpy).toHaveBeenCalledTimes(1)
    expect(intakeSpy).toHaveBeenCalledTimes(2)
  })
})
