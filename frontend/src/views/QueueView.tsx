/**
 * 工单队列视图（QueueView.tsx）—— 本应用最核心的页面。
 *
 * 职责：
 * - 左侧队列列表：按状态/类别/优先级/关键词筛选工单，支持游标分页「加载更多」（每页 LIMIT=30）。
 * - 右侧详情面板：展示工单详情、SLA、消息流、处理记录，并提供状态流转、发起回访、信息补全等操作。
 * - 路由驱动：列表页 /tickets 与详情页 /tickets/:ticketId 共用本组件；?view=mine|resolved 切换列表视角。
 *
 * 与后端 API 的对应关系（均来自 src/api 封装层）：
 * - listTickets（列表+分页+筛选）、getTicket / getTicketOverview / getPendingInterrupt（详情三件套）、
 * - transitionTicket（状态流转）、createSurvey（回访）、resumeIntake（补充信息）、
 * - createTicket 与 startIntake（经 CreateTicketDialog 弹窗）。
 *
 * 关键交互逻辑：
 * - 竞态防护：列表与详情请求都使用「请求序号 requestId + AbortController」双重保护，
 *   避免快速切换工单/筛选时旧响应覆盖新状态。
 * - 详情请求用 Promise.allSettled：overview/pending 失败不阻塞工单主体展示。
 * - 操作成功后统一刷新详情（refreshTicketDetail）；若用户已切走，则只更新列表、不覆盖当前详情。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { AlertCircle, Filter, Menu, Plus, RefreshCw, Search, X } from 'lucide-react'
import type { PendingInterrupt, Ticket, TicketOverview } from '../types'
import {
  ApiError,
  createSurvey,
  getPendingInterrupt,
  getTicket,
  getTicketOverview,
  listTickets,
  resumeIntake,
  transitionTicket,
} from '../api'
import type { ResumeIntakeInput, TicketQuery } from '../api/tickets'
import TicketList from '../components/TicketList'
import TicketDetail from '../components/TicketDetail'
import type { TransitionAction } from '../components/TicketDetail'
import CreateTicketDialog from '../components/CreateTicketDialog'
import { isV1Category, v1CategoryOptions } from '../lib/labels'
import { useDebounce } from '../lib/useDebounce'

// 列表每页请求的工单数量（对应后端 limit 查询参数）
const LIMIT = 30

// 列表筛选条件：status/category/priority 为精确过滤，q 为关键词（经防抖后传给后端）
interface Filters {
  status: string
  category: string
  priority: string
  q: string
}

// 空筛选对象：用于初始化与重置
const emptyFilters: Filters = { status: '', category: '', priority: '', q: '' }

// 根据当前视角与可选工单 id 生成跳转路径：详情页带 ticketId，列表页按 view 拼接查询参数
function ticketsPath(view: string, ticketId?: string) {
  if (ticketId) return `/tickets/${ticketId}`
  return view === 'queue' ? '/tickets' : `/tickets?view=${view}`
}

/** 合并队列错误与详情错误，详情错误优先级更高。 */
function formatError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export default function QueueView({ onOpenSidebar }: { onOpenSidebar?: () => void }) {
  const navigate = useNavigate()
  // 当前路由中的工单 id：存在则处于详情模式（右侧面板展示对应工单）
  const { ticketId } = useParams<{ ticketId?: string }>()
  const [searchParams] = useSearchParams()
  // 列表视角：queue（默认）/ mine（我的处理）/ resolved（已解决），来自 ?view= 查询参数
  const view = searchParams.get('view') ?? 'queue'

  // —— 列表相关状态 ——
  const [tickets, setTickets] = useState<Ticket[]>([])
  // —— 详情相关状态 ——
  const [selected, setSelected] = useState<Ticket | null>(null)
  const [overview, setOverview] = useState<TicketOverview | null>(null)
  // —— 筛选条件 ——
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  // 搜索词防抖 300ms：避免每次按键都向后端发起请求
  const debouncedQ = useDebounce(filters.q, 300)
  // 游标分页：非 null 表示还有更多数据可加载
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  // busy：详情面板上某个操作（流转/回访/补全）正在执行，期间禁用按钮防重复提交
  const [busy, setBusy] = useState(false)
  const [listError, setListError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  // 待补全信息：工单进入 awaiting_customer 状态时后端返回的 interrupt
  const [pendingClarification, setPendingClarification] = useState<PendingInterrupt | null>(null)
  // Copilot 采用的回复草稿：由 CopilotPanel 填充，仅作展示/复制，不自动发送
  const [adoptedReply, setAdoptedReply] = useState('')
  // 移动端搜索框是否展开（阶段七：≤540px 时搜索按钮展开内联输入框）
  const [mobileSearchOpen, setMobileSearchOpen] = useState(false)

  // —— 竞态防护：详情请求与列表请求各自独立的「序号 + AbortController」 ——
  // 序号用于忽略过期响应；AbortController 用于取消尚未完成的旧请求
  const detailRequestId = useRef(0)
  const detailAbortRef = useRef<AbortController | null>(null)
  const listRequestId = useRef(0)
  const listAbortRef = useRef<AbortController | null>(null)

  // 组装列表请求的公共查询参数（类型化 TicketQuery，避免 URLSearchParams 键名漂移）；
  // 依赖筛选条件变化时自动重建（useMemo 减少重复计算）
  const baseQuery = useMemo(() => {
    // resolved 视角下强制 status=resolved，忽略用户选择的状态筛选项
    const status = view === 'resolved' ? 'resolved' : filters.status
    return {
      status: status || undefined,
      category: filters.category || undefined,
      priority: filters.priority || undefined,
      q: debouncedQ.trim() || undefined,
      // mine 视角下只查分配给当前用户（current_user）的工单
      assignedUserId: view === 'mine' ? 'current_user' : undefined,
      limit: LIMIT,
    } satisfies TicketQuery
  }, [view, filters.status, filters.category, filters.priority, debouncedQ])

  // 加载工单列表。append=false 为首次/刷新（替换列表），true 为「加载更多」（游标分页追加）
  const loadTickets = useCallback(
    async (append = false, pageCursor: string | null = null) => {
      // 先取消上一次未完成的列表请求，避免并发请求相互覆盖
      listAbortRef.current?.abort()
      const controller = new AbortController()
      listAbortRef.current = controller

      // 递增请求序号：响应返回时若序号已过期则直接丢弃
      const requestId = ++listRequestId.current
      setLoading(true)
      setListError('')
      try {
        const result = await listTickets(
          { ...baseQuery, cursor: pageCursor ?? undefined },
          controller.signal,
        )
        // 请求已被更新的请求取代（如用户切换筛选）时，丢弃本次结果
        if (requestId !== listRequestId.current) return
        // V1 默认队列只展示 IT 服务台工单（非 V1 类别从默认筛选隐藏）
        const v1Items = result.items.filter((item) => isV1Category(item.category))
        setTickets((items) =>
          append
            ? deduplicateById([...items, ...v1Items], (item) => item.ticket_id)
            : v1Items,
        )
        setCursor(result.next_cursor ?? null)
      } catch (err) {
        // 主动取消导致的异常不视为错误，不展示错误条
        if (controller.signal.aborted) return
        if (requestId !== listRequestId.current) return
        setListError(formatError(err))
        if (!append) setTickets([])
      } finally {
        // 仅当本次请求仍是最新请求时才清除 loading
        if (requestId === listRequestId.current) {
          setLoading(false)
        }
      }
    },
    [baseQuery],
  )

  // 挂载 / 筛选条件（baseQuery）变化时自动加载第一页；卸载时取消未完成请求
  useEffect(() => {
    loadTickets(false)
    return () => {
      listAbortRef.current?.abort()
    }
  }, [loadTickets])

  /** 刷新当前工单详情（操作成功后调用）。
   *
   * 只在目标工单仍与当前路由一致时才更新状态，避免刷新期间用户切换导致旧数据覆盖。
   */
  const refreshTicketDetail = useCallback(
    async (targetId: string) => {
      const [ticketResult, overviewResult, pendingResult] = await Promise.allSettled([
        getTicket(targetId),
        getTicketOverview(targetId),
        getPendingInterrupt(targetId),
      ])

      // 操作完成后用户可能已经切走，此时只刷新列表，不覆盖详情
      if (targetId !== ticketId) {
        if (ticketResult.status === 'fulfilled') {
          setTickets((items) =>
            items.map((item) =>
              item.ticket_id === ticketResult.value.ticket_id ? ticketResult.value : item,
            ),
          )
        }
        return
      }

      const nextTicket = ticketResult.status === 'fulfilled' ? ticketResult.value : null
      const nextOverview = overviewResult.status === 'fulfilled' ? overviewResult.value : null
      const nextPending = pendingResult.status === 'fulfilled' ? pendingResult.value : null

      if (nextTicket) {
        setSelected(nextTicket)
        setTickets((items) =>
          items.map((item) => (item.ticket_id === nextTicket.ticket_id ? nextTicket : item)),
        )
      }
      if (nextOverview) setOverview(nextOverview)
      setPendingClarification(nextPending?.interrupt ?? null)

      const errs: string[] = []
      if (ticketResult.status === 'rejected') errs.push(formatError(ticketResult.reason))
      if (overviewResult.status === 'rejected') errs.push(formatError(overviewResult.reason))
      if (pendingResult.status === 'rejected') errs.push(formatError(pendingResult.reason))
      if (errs.length > 0) {
        setDetailError(errs.join('；'))
      }
    },
    [ticketId],
  )

  // 加载指定工单的详情（打开详情页/切换工单/重试时调用）。
  // 同样采用「取消旧请求 + 序号校验」的竞态防护；详情三件套并行请求，任一失败只记入错误条
  const loadDetail = useCallback((targetId: string) => {
    detailAbortRef.current?.abort()
    const controller = new AbortController()
    detailAbortRef.current = controller
    const requestId = ++detailRequestId.current

    setSelected(null)
    setOverview(null)
    setPendingClarification(null)
    setAdoptedReply('')  // 切换工单：清空上一张工单采用的 Copilot 草稿
    setDetailError('')
    setDetailLoading(true)

    // allSettled：overview/pending 失败不阻塞工单主体显示；ticket 失败才整体失败。
    Promise.allSettled([
      getTicket(targetId, controller.signal),
      getTicketOverview(targetId, controller.signal),
      getPendingInterrupt(targetId, controller.signal),
    ]).then(([ticketResult, overviewResult, pendingResult]) => {
      if (controller.signal.aborted || requestId !== detailRequestId.current) return
      const errs: string[] = []
      if (ticketResult.status === 'fulfilled') {
        setSelected(ticketResult.value)
      } else {
        errs.push(formatError(ticketResult.reason))
      }
      if (overviewResult.status === 'fulfilled') {
        setOverview(overviewResult.value)
      } else {
        errs.push(formatError(overviewResult.reason))
      }
      if (pendingResult.status === 'fulfilled') {
        setPendingClarification(pendingResult.value.interrupt ?? null)
      } else {
        errs.push(formatError(pendingResult.reason))
      }
      if (errs.length > 0) {
        setDetailError(errs.join('；'))
      }
    }).finally(() => {
      if (requestId === detailRequestId.current) {
        setDetailLoading(false)
      }
    })
  }, [])

  // 路由参数变化时驱动详情加载：无 ticketId 则清空详情面板；有则加载并返回时取消请求
  useEffect(() => {
    detailAbortRef.current?.abort()
    detailAbortRef.current = null

    if (!ticketId) {
      setSelected(null)
      setOverview(null)
      setPendingClarification(null)
      setDetailError('')
      setDetailLoading(false)
      return
    }

    loadDetail(ticketId)

    return () => {
      detailAbortRef.current?.abort()
    }
  }, [ticketId, loadDetail])

  // 操作前的守卫：若详情面板显示的工单与当前路由已不一致（用户切换过工单），
  // 拒绝执行操作并提示重新选择，防止把操作发到错误的工单上
  const guardSelected = (targetId?: string) => {
    if (!selected) return false
    const id = targetId ?? ticketId
    if (id && selected.ticket_id !== id) {
      setDetailError('工单已切换，请重新选择当前工单')
      return false
    }
    return true
  }

  // 将更新后的工单对象同步到「选中详情」与「列表项」，保证两处展示一致
  const applyTicket = (updated: Ticket) => {
    setSelected(updated)
    setTickets((items) =>
      items.map((item) => (item.ticket_id === updated.ticket_id ? updated : item)),
    )
  }

  // 状态流转（开始受理/接单/开始处理/标记解决/关闭）：
  // 携带 expected_version 做乐观并发控制，后端校验版本不匹配会拒绝并返回错误
  const transition = async (action: TransitionAction) => {
    if (!selected || !guardSelected()) return
    setBusy(true)
    setDetailError('')
    try {
      const updated = await transitionTicket(selected.ticket_id, {
        ...action,
        expected_version: selected.version,
      })
      applyTicket(updated)
      // 流转可能改变状态机与待办（如进入 awaiting_customer），刷新详情以保持一致
      await refreshTicketDetail(updated.ticket_id)
    } catch (err) {
      setDetailError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  // 对已解决工单发起回访（评分 7 分制）
  const survey = async () => {
    if (!selected || !guardSelected()) return
    setBusy(true)
    setDetailError('')
    try {
      await createSurvey(selected.ticket_id, 7)
      await refreshTicketDetail(selected.ticket_id)
    } catch (err) {
      setDetailError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  // 提交客户补充的信息：调用 resumeIntake 恢复被 interrupt 挂起的受理流程
  const resumeClarification = async (payload: ResumeIntakeInput) => {
    if (!selected || !guardSelected()) return
    setBusy(true)
    setDetailError('')
    try {
      const result = await resumeIntake(selected.ticket_id, payload)
      applyTicket(result.ticket)
      setPendingClarification(result.interrupt ?? null)
      await refreshTicketDetail(result.ticket.ticket_id)
    } catch (err) {
      setDetailError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  // 点击列表项：跳转到对应详情路由（不直接 setState，保证 URL 与视图同步、可刷新/可分享）
  const selectTicket = (id: string) => navigate(ticketsPath(view, id))
  // 关闭详情：回到当前视角的列表页
  const closeDetail = () => navigate(ticketsPath(view))

  // 统一错误展示：详情错误优先（列表错误常因筛选变化被新请求取代）
  const error = detailError || listError
  const clearError = () => {
    setDetailError('')
    setListError('')
  }

  // 布局：左侧列表（queue-pane）+ 右侧详情（TicketDetail）；存在 ticketId 时加 show-detail 类，移动端切换显示详情
  return (
    <main className={`workspace ${ticketId ? 'show-detail' : ''}`}>
      <section className="queue-pane">
        {/* 顶栏：移动端菜单按钮 + 标题（随视角变化）+ 新建工单按钮 */}
        <header className="topbar">
          <button
            className="icon-button mobile-only"
            onClick={onOpenSidebar}
            aria-label="打开导航"
          >
            <Menu />
          </button>
          <div>
            <span className="eyebrow">服务台</span>
            <h1>
              {view === 'resolved' ? '已解决工单' : view === 'mine' ? '我的处理' : '工单队列'}
            </h1>
          </div>
          {/* 打开新建工单弹窗 */}
          <button className="primary-action" onClick={() => setCreateOpen(true)}>
            <Plus size={17} />
            新建
          </button>
        </header>

        {/* 筛选区：搜索（防抖）+ 状态/类别/优先级下拉 + 手动刷新；筛选变化时清空分页游标回到第一页 */}
        <div className="filters">
          {/* 桌面搜索框（≤540px 隐藏，见 CSS .desktop-search） */}
          <div className="search-box desktop-search">
            <Search size={16} />
            <input
              placeholder="搜索工单"
              value={filters.q}
              onChange={(e) => {
                setCursor(null)
                setFilters({ ...filters, q: e.target.value })
              }}
            />
          </div>
          {/* 移动端搜索按钮：展开内联搜索框（阶段七恢复移动端搜索） */}
          <button
            className="icon-button search-toggle"
            aria-label={mobileSearchOpen ? '收起搜索' : '展开搜索'}
            onClick={() => setMobileSearchOpen((open) => !open)}
          >
            <Search size={16} />
          </button>
          {mobileSearchOpen && (
            <div className="search-box mobile-search">
              <Search size={16} />
              <input
                autoFocus
                placeholder="搜索工单"
                value={filters.q}
                onChange={(e) => {
                  setCursor(null)
                  setFilters({ ...filters, q: e.target.value })
                }}
              />
            </div>
          )}
          <label>
            <Filter size={15} />
            {/* resolved 视角下状态由后端固定，禁用下拉避免与强制 status 冲突 */}
            <select
              value={filters.status}
              onChange={(e) => {
                setCursor(null)
                setFilters({ ...filters, status: e.target.value })
              }}
              disabled={view === 'resolved'}
            >
              <option value="">全部状态</option>
              <option value="new">新建</option>
              <option value="queued">待分派</option>
              <option value="assigned">已分派</option>
              <option value="in_progress">处理中</option>
              <option value="awaiting_customer">等待客户</option>
              <option value="resolved">已解决</option>
            </select>
          </label>
          <label>
            <select
              value={filters.category}
              onChange={(e) => {
                setCursor(null)
                setFilters({ ...filters, category: e.target.value })
              }}
            >
              <option value="">全部类别（IT 服务台）</option>
              {/* V1 默认只暴露 IT 服务台类别；非 V1 类别不在默认筛选中出现 */}
              {v1CategoryOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <select
              value={filters.priority}
              onChange={(e) => {
                setCursor(null)
                setFilters({ ...filters, priority: e.target.value })
              }}
            >
              <option value="">全部优先级</option>
              <option value="urgent">紧急</option>
              <option value="high">高</option>
              <option value="normal">普通</option>
              <option value="low">低</option>
            </select>
          </label>
          {/* 手动刷新当前列表（回到第一页） */}
          <button className="icon-button" onClick={() => loadTickets(false)} aria-label="刷新">
            <RefreshCw size={17} />
          </button>
        </div>

        {/* 错误横幅：展示列表或详情错误信息，可一键清除 */}
        {error && (
          <div className="error-banner">
            <AlertCircle size={16} />
            <span>{error}</span>
            <button onClick={clearError}>
              <X size={15} />
            </button>
          </div>
        )}

        {/* 工单列表：选中项由路由 ticketId 驱动；hasMore 控制「加载更多」（游标分页） */}
        <TicketList
          tickets={tickets}
          selectedId={ticketId}
          onSelect={selectTicket}
          loading={loading}
          hasMore={Boolean(cursor)}
          onMore={() => loadTickets(true, cursor)}
        />
      </section>

      {/* 详情面板：展示选中工单的完整上下文，并处理流转/回访/补全/Copilot 等操作；加载失败时提供重试 */}
      <TicketDetail
        ticket={selected}
        overview={overview}
        busy={busy}
        detailLoading={detailLoading}
        detailError={detailError}
        pendingClarification={pendingClarification}
        onTransition={transition}
        onSurvey={survey}
        onResume={resumeClarification}
        onBack={closeDetail}
        onRetry={() => {
          if (ticketId) loadDetail(ticketId)
        }}
        onAdoptDraft={setAdoptedReply}
        adoptedReply={adoptedReply}
      />

      {/* 新建工单弹窗：创建+受理成功后把新工单插入列表头部，并跳转到新工单详情 */}
      <CreateTicketDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(ticket) => {
          setTickets((items) => [ticket, ...items])
          navigate(ticketsPath(view, ticket.ticket_id))
        }}
      />
    </main>
  )
}

// 按 id 去重：分页追加时防止游标边界上后端重复返回同一工单，导致列表出现重复项
function deduplicateById<T>(items: T[], idOf: (item: T) => string): T[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const id = idOf(item)
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
}
