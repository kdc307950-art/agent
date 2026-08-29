import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CopilotPanel from './CopilotPanel'
import { copilotPollConfig } from './copilotPoll'
import * as copilotApi from '../api/copilot'
import type { CopilotQueuedResult } from '../api/copilot'
import { ApiError } from '../api/client'
import type { CopilotDraft } from '../types'

const draft: CopilotDraft = {
  draft_id: 'draft-1',
  ticket_id: 't-1',
  run_id: 'run-1',
  draft_answer: '请先检查网络连接',
  steps: ['检查网络'],
  citations: [],
  confidence: 0.9,
  needs_human_review: false,
  status: 'generated',
  created_at: new Date().toISOString(),
}

describe('CopilotPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('生成成功后展示草稿，采用草稿只填充回复框', async () => {
    copilotPollConfig.intervalMs = 10
    vi.spyOn(copilotApi, 'generateCopilot').mockResolvedValue({
      status: 'queued',
      run_id: 'run-1',
    })
    vi.spyOn(copilotApi, 'getCopilotRunStatus').mockResolvedValue({
      run_id: 'run-1',
      status: 'completed',
      draft,
      draft_id: 'draft-1',
      error_code: null,
      tool_calls: 2,
    })
    const onAdopt = vi.fn()
    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={onAdopt} />)

    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))
    await waitFor(() => expect(screen.getByText('请先检查网络连接')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: '采用草稿' }))
    expect(onAdopt).toHaveBeenCalledWith('请先检查网络连接')
  })

  it('503 时显示 Copilot 未配置提示', async () => {
    vi.spyOn(copilotApi, 'generateCopilot').mockRejectedValue(
      new ApiError('Copilot 服务尚未初始化', 503),
    )
    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))
    await waitFor(() =>
      expect(screen.getByText(/Copilot 服务未配置/)).toBeInTheDocument(),
    )
  })

  it('409 时提示刷新当前工单', async () => {
    vi.spyOn(copilotApi, 'generateCopilot').mockRejectedValue(
      new ApiError('工单版本已变化', 409),
    )
    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))
    await waitFor(() =>
      expect(screen.getByText(/请刷新当前工单/)).toBeInTheDocument(),
    )
  })

  it('生成期间禁止重复点击', async () => {
    let resolveGen: (value: CopilotQueuedResult) => void = () => {}
    vi.spyOn(copilotApi, 'generateCopilot').mockImplementation(
      () =>
        new Promise<CopilotQueuedResult>((resolve) => {
          resolveGen = resolve
        }),
    )
    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))
    // 生成中按钮应禁用（防重复点击）
    expect(screen.getByRole('button', { name: /生成 AI 处理建议/ })).toBeDisabled()

    resolveGen({ status: 'queued', run_id: 'run-1' })
  })

  it('202 入队时显示"正在生成"并轮询', async () => {
    copilotPollConfig.intervalMs = 10
    vi.spyOn(copilotApi, 'generateCopilot').mockResolvedValue({
      status: 'queued',
      run_id: 'run-1',
    })
    vi.spyOn(copilotApi, 'getCopilotRunStatus').mockResolvedValue({
      run_id: 'run-1',
      status: 'completed',
      draft,
      draft_id: 'draft-1',
      error_code: null,
      tool_calls: 2,
    })

    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))

    // 202：立即显示"正在生成"，轮询后展示草稿
    await waitFor(() => expect(screen.getByText(/正在生成/)).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('请先检查网络连接')).toBeInTheDocument())
  })

  it('轮询到 failed 时显示可重试提示', async () => {
    copilotPollConfig.intervalMs = 10
    vi.spyOn(copilotApi, 'generateCopilot').mockResolvedValue({
      status: 'queued',
      run_id: 'run-1',
    })
    vi.spyOn(copilotApi, 'getCopilotRunStatus').mockResolvedValue({
      run_id: 'run-1',
      status: 'failed',
      draft: null,
      draft_id: null,
      error_code: 'model_failed',
      tool_calls: 0,
    })

    render(<CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))

    await waitFor(() => expect(screen.getByText(/生成失败/)).toBeInTheDocument())
  })

  it('切换工单（ticketId 变化）时取消旧请求，旧结果不覆盖新工单', async () => {
    let resolveGen: (value: CopilotQueuedResult) => void = () => {}
    const genSpy = vi.spyOn(copilotApi, 'generateCopilot').mockImplementation(
      () =>
        new Promise<CopilotQueuedResult>((resolve) => {
          resolveGen = resolve
        }),
    )
    const { rerender } = render(
      <CopilotPanel ticketId="t-1" expectedVersion={1} enabled onAdopt={() => {}} />,
    )
    await userEvent.click(screen.getByRole('button', { name: '生成 AI 处理建议' }))
    expect(genSpy).toHaveBeenCalledTimes(1)

    // 切换到新工单：旧请求应被取消（signal.aborted），旧结果不得渲染
    rerender(<CopilotPanel ticketId="t-2" expectedVersion={1} enabled onAdopt={() => {}} />)
    const signal = genSpy.mock.calls[0][2] as AbortSignal
    expect(signal.aborted).toBe(true)

    // 先让旧请求 resolve，再断言旧结果没有渲染（ticketId 守卫丢弃）
    resolveGen({ status: 'queued', run_id: 'run-1' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByText('请先检查网络连接')).not.toBeInTheDocument()
  })
})
