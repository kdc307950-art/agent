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
import type { ResumeIntakeInput } from '../api/tickets'
import TicketList from '../components/TicketList'
import TicketDetail from '../components/TicketDetail'
import type { TransitionAction } from '../components/TicketDetail'
import CreateTicketDialog from '../components/CreateTicketDialog'
import { categoryLabel } from '../lib/labels'
import { useDebounce } from '../lib/useDebounce'

const LIMIT = 30

interface Filters {
  status: string
  category: string
  priority: string
  q: string
}

const emptyFilters: Filters = { status: '', category: '', priority: '', q: '' }

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
  const { ticketId } = useParams<{ ticketId?: string }>()
  const [searchParams] = useSearchParams()
  const view = searchParams.get('view') ?? 'queue'

  const [tickets, setTickets] = useState<Ticket[]>([])
  const [selected, setSelected] = useState<Ticket | null>(null)
  const [overview, setOverview] = useState<TicketOverview | null>(null)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const debouncedQ = useDebounce(filters.q, 300)
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [busy, setBusy] = useState(false)
  const [listError, setListError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [pendingClarification, setPendingClarification] = useState<PendingInterrupt | null>(null)

  const detailRequestId = useRef(0)
  const detailAbortRef = useRef<AbortController | null>(null)
  const listRequestId = useRef(0)
  const listAbortRef = useRef<AbortController | null>(null)

  const baseQuery = useMemo(() => {
    const params = new URLSearchParams({ limit: String(LIMIT) })
    const status = view === 'resolved' ? 'resolved' : filters.status
    if (status) params.set('status', status)
    if (filters.category) params.set('category', filters.category)
    if (filters.priority) params.set('priority', filters.priority)
    if (debouncedQ.trim()) params.set('q', debouncedQ.trim())
    if (view === 'mine') params.set('assigned_user_id', 'current_user')
    return params
  }, [view, filters.status, filters.category, filters.priority, debouncedQ])

  const loadTickets = useCallback(
    async (append = false, pageCursor: string | null = null) => {
      listAbortRef.current?.abort()
      const controller = new AbortController()
      listAbortRef.current = controller

      const requestId = ++listRequestId.current
      setLoading(true)
      setListError('')
      try {
        const params = new URLSearchParams(baseQuery)
        if (pageCursor) params.set('cursor', pageCursor)
        const result = await listTickets(
          { ...Object.fromEntries(params), limit: LIMIT },
          controller.signal,
        )
        if (requestId !== listRequestId.current) return
        setTickets((items) =>
          append
            ? deduplicateById([...items, ...result.items], (item) => item.ticket_id)
            : result.items,
        )
        setCursor(result.next_cursor ?? null)
      } catch (err) {
        if (controller.signal.aborted) return
        if (requestId !== listRequestId.current) return
        setListError(formatError(err))
        if (!append) setTickets([])
      } finally {
        if (requestId === listRequestId.current) {
          setLoading(false)
        }
      }
    },
    [baseQuery],
  )

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

    setSelected(null)
    setOverview(null)
    setPendingClarification(null)
    setDetailError('')
    setDetailLoading(true)

    const controller = new AbortController()
    detailAbortRef.current = controller
    const requestId = ++detailRequestId.current

    Promise.all([
      getTicket(ticketId, controller.signal),
      getTicketOverview(ticketId, controller.signal),
      getPendingInterrupt(ticketId, controller.signal),
    ])
      .then(([ticket, detail, pending]) => {
        if (controller.signal.aborted || requestId !== detailRequestId.current) return
        setSelected(ticket)
        setOverview(detail)
        setPendingClarification(pending.interrupt ?? null)
      })
      .catch((err) => {
        if (controller.signal.aborted || requestId !== detailRequestId.current) return
        setDetailError(formatError(err))
      })
      .finally(() => {
        if (requestId === detailRequestId.current) {
          setDetailLoading(false)
        }
      })

    return () => {
      controller.abort()
    }
  }, [ticketId])

  const guardSelected = (targetId?: string) => {
    if (!selected) return false
    const id = targetId ?? ticketId
    if (id && selected.ticket_id !== id) {
      setDetailError('工单已切换，请重新选择当前工单')
      return false
    }
    return true
  }

  const applyTicket = (updated: Ticket) => {
    setSelected(updated)
    setTickets((items) =>
      items.map((item) => (item.ticket_id === updated.ticket_id ? updated : item)),
    )
  }

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
      await refreshTicketDetail(updated.ticket_id)
    } catch (err) {
      setDetailError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

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

  const selectTicket = (id: string) => navigate(ticketsPath(view, id))
  const closeDetail = () => navigate(ticketsPath(view))

  const error = detailError || listError
  const clearError = () => {
    setDetailError('')
    setListError('')
  }

  return (
    <main className={`workspace ${ticketId ? 'show-detail' : ''}`}>
      <section className="queue-pane">
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
          <button className="primary-action" onClick={() => setCreateOpen(true)}>
            <Plus size={17} />
            新建
          </button>
        </header>

        <div className="filters">
          <div className="search-box">
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
          <label>
            <Filter size={15} />
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
              <option value="">全部类别</option>
              {Object.entries(categoryLabel).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
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
          <button className="icon-button" onClick={() => loadTickets(false)} aria-label="刷新">
            <RefreshCw size={17} />
          </button>
        </div>

        {error && (
          <div className="error-banner">
            <AlertCircle size={16} />
            <span>{error}</span>
            <button onClick={clearError}>
              <X size={15} />
            </button>
          </div>
        )}

        <TicketList
          tickets={tickets}
          selectedId={ticketId}
          onSelect={selectTicket}
          loading={loading}
          hasMore={Boolean(cursor)}
          onMore={() => loadTickets(true, cursor)}
        />
      </section>

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
          if (ticketId) {
            setDetailError('')
            detailRequestId.current += 1
            // 通过微调依赖触发 useEffect 重新执行
            navigate(ticketsPath(view, ticketId), { replace: true })
          }
        }}
      />

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

function deduplicateById<T>(items: T[], idOf: (item: T) => string): T[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const id = idOf(item)
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
}
