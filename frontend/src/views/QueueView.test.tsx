import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import QueueView from './QueueView'
import * as api from '../api'
import type { Ticket } from '../types'

const ticketA: Ticket = {
  ticket_id: 'ticket-a',
  requester_id: 'user-1',
  channel: 'web',
  title: '工单 A',
  description: '描述 A',
  status: 'new',
  priority: 'normal',
  version: 1,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

const ticketB: Ticket = {
  ticket_id: 'ticket-b',
  requester_id: 'user-1',
  channel: 'web',
  title: '工单 B',
  description: '描述 B',
  status: 'queued',
  priority: 'high',
  version: 2,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

function Wrapper({ initialEntries }: { initialEntries: string[] }) {
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/tickets" element={<QueueView />} />
        <Route path="/tickets/:ticketId" element={<QueueView />} />
      </Routes>
    </MemoryRouter>
  )
}

describe('QueueView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.spyOn(api, 'listTickets').mockResolvedValue({ items: [ticketA, ticketB] })
  })

  it('快速切换工单时，详情只显示最后选中的工单', async () => {
    let resolveA: (value: Ticket) => void = () => {}
    let resolveB: (value: Ticket) => void = () => {}

    vi.spyOn(api, 'getTicket').mockImplementation((id) => {
      if (id === 'ticket-a') {
        return new Promise((resolve) => {
          resolveA = resolve
        })
      }
      return new Promise((resolve) => {
        resolveB = resolve
      })
    })
    vi.spyOn(api, 'getTicketOverview').mockResolvedValue({})
    vi.spyOn(api, 'getPendingInterrupt').mockResolvedValue({ interrupt: null })

    render(<Wrapper initialEntries={['/tickets']} />)

    await waitFor(() => {
      expect(screen.getByText('工单 A')).toBeInTheDocument()
      expect(screen.getByText('工单 B')).toBeInTheDocument()
    })

    // 先点 A，再快速点 B
    await userEvent.click(screen.getByText('工单 A'))
    await userEvent.click(screen.getByText('工单 B'))

    // B 先返回，A 后返回
    resolveB(ticketB)
    await waitFor(() => {
      // queued 状态对应的详情操作按钮是“接单”
      expect(screen.getByRole('button', { name: '接单' })).toBeInTheDocument()
    })

    resolveA(ticketA)
    await new Promise((resolve) => setTimeout(resolve, 50))

    // 最终详情仍应展示 B，而不是被 A 覆盖
    const detailStatus = document.querySelector('.detail .status')
    expect(detailStatus).toHaveClass('status-queued')
    expect(detailStatus).toHaveTextContent('待分派')
    expect(document.querySelector('.detail .status-new')).not.toBeInTheDocument()
    expect(document.querySelector('.detail h2')).toHaveTextContent('工单 B')
  })

  it('切换工单后，状态转换只作用于当前选中的工单', async () => {
    vi.spyOn(api, 'getTicket').mockImplementation((id) => Promise.resolve(id === 'ticket-a' ? ticketA : ticketB))
    vi.spyOn(api, 'getTicketOverview').mockResolvedValue({})
    vi.spyOn(api, 'getPendingInterrupt').mockResolvedValue({ interrupt: null })
    const transitionSpy = vi
      .spyOn(api, 'transitionTicket')
      .mockResolvedValue({ ...ticketB, status: 'assigned', version: 3 })

    render(<Wrapper initialEntries={['/tickets']} />)

    await waitFor(() => {
      expect(screen.getByText('工单 A')).toBeInTheDocument()
      expect(screen.getByText('工单 B')).toBeInTheDocument()
    })

    // 先选中 A，再切到 B
    await userEvent.click(screen.getByText('工单 A'))
    await userEvent.click(screen.getByText('工单 B'))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '接单' })).toBeInTheDocument()
    })

    await userEvent.click(screen.getByRole('button', { name: '接单' }))
    await waitFor(() => {
      expect(transitionSpy).toHaveBeenCalledTimes(1)
    })
    // 只对 ticket-b 做转换，绝不作用到 ticket-a
    expect(transitionSpy.mock.calls[0][0]).toBe('ticket-b')
    expect(transitionSpy.mock.calls[0][1].expected_version).toBe(2)
  })

  it('切换工单时取消旧的详情请求', async () => {
    let signalA: AbortSignal | null = null
    vi.spyOn(api, 'getTicket').mockImplementation((id, signal) => {
      if (id === 'ticket-a') {
        signalA = (signal ?? null) as AbortSignal | null
        return new Promise<Ticket>(() => {}) // A 永不返回
      }
      return Promise.resolve(ticketB)
    })
    vi.spyOn(api, 'getTicketOverview').mockResolvedValue({})
    vi.spyOn(api, 'getPendingInterrupt').mockResolvedValue({ interrupt: null })

    render(<Wrapper initialEntries={['/tickets']} />)
    await waitFor(() => {
      expect(screen.getByText('工单 A')).toBeInTheDocument()
      expect(screen.getByText('工单 B')).toBeInTheDocument()
    })

    await userEvent.click(screen.getByText('工单 A'))
    await userEvent.click(screen.getByText('工单 B'))

    await waitFor(() => {
      expect(signalA?.aborted).toBe(true) // A 的请求被取消
    })
    // B 正常显示
    await waitFor(() => {
      expect(document.querySelector('.detail h2')).toHaveTextContent('工单 B')
    })
  })

  it('详情请求失败后可以重试', async () => {
    vi.spyOn(api, 'getTicket')
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue(ticketA)
    vi.spyOn(api, 'getTicketOverview').mockResolvedValue({})
    vi.spyOn(api, 'getPendingInterrupt').mockResolvedValue({ interrupt: null })

    render(<Wrapper initialEntries={['/tickets']} />)
    await waitFor(() => {
      expect(screen.getByText('工单 A')).toBeInTheDocument()
      expect(screen.getByText('工单 B')).toBeInTheDocument()
    })

    await userEvent.click(screen.getByText('工单 A'))
    await waitFor(() => {
      expect(screen.getByText(/工单加载失败/)).toBeInTheDocument()
    })

    await userEvent.click(screen.getByRole('button', { name: '重新加载' }))
    await waitFor(() => {
      expect(document.querySelector('.detail h2')).toHaveTextContent('工单 A')
    })
  })

  it('快速连续输入搜索词时，只显示最终查询的结果', async () => {
    const printerTicket: Ticket = { ...ticketA, ticket_id: 'ticket-c', title: '打印机工单' }
    vi.spyOn(api, 'listTickets').mockImplementation(async (params) => {
      const q = String((params as { q?: string }).q ?? '')
      if (q.includes('打印机')) return { items: [printerTicket] }
      if (q.includes('邮箱')) return { items: [ticketB] }
      return { items: [ticketA] }
    })
    vi.spyOn(api, 'getTicket').mockResolvedValue(printerTicket)
    vi.spyOn(api, 'getTicketOverview').mockResolvedValue({})
    vi.spyOn(api, 'getPendingInterrupt').mockResolvedValue({ interrupt: null })

    render(<Wrapper initialEntries={['/tickets']} />)
    const search = screen.getByPlaceholderText('搜索工单')

    await userEvent.clear(search)
    await userEvent.type(search, 'VPN')
    await waitFor(() => expect(screen.getByText('工单 A')).toBeInTheDocument())

    await userEvent.clear(search)
    await userEvent.type(search, '邮箱')
    await waitFor(() => expect(screen.getByText('工单 B')).toBeInTheDocument())

    await userEvent.clear(search)
    await userEvent.type(search, '打印机')
    await waitFor(() => expect(screen.getByText('打印机工单')).toBeInTheDocument())

    // 最终只显示打印机查询结果，旧查询结果被丢弃
    expect(screen.queryByText('工单 B')).not.toBeInTheDocument()
    expect(screen.queryByText('工单 A')).not.toBeInTheDocument()
  })
})
