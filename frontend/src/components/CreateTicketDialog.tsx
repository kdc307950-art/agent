import { useEffect, useRef, useState } from 'react'
import { AlertCircle, LoaderCircle, RefreshCw, X } from 'lucide-react'
import type { Ticket, TicketPriority } from '../types'
import type { AssetListResult } from '../types'
import { ApiError, createTicket, listAssets, startIntake } from '../api'

interface CreateTicketDialogProps {
  open: boolean
  onClose: () => void
  onCreated: (ticket: Ticket) => void
}

interface FormState {
  title: string
  description: string
  priority: TicketPriority
  asset_id: string
}

const emptyForm: FormState = {
  title: '',
  description: '',
  priority: 'normal',
  asset_id: '',
}

type SubmitPhase = 'idle' | 'creating' | 'starting_intake' | 'intake_retrying'

function formatError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

/** 新建工单弹窗。
 *
 * 关键约束：
 * - ticket_id 与 operation_id 在首次提交时生成，整个流程中保持不变。
 * - 建单成功但受理失败后，禁止再次建单，仅允许重试受理。
 * - 受理成功返回的最新工单对象（可能含状态变化）通过 onCreated 回传。
 */
export default function CreateTicketDialog({
  open,
  onClose,
  onCreated,
}: CreateTicketDialogProps) {
  const [form, setForm] = useState<FormState>(emptyForm)
  const [assets, setAssets] = useState<AssetListResult['items']>([])
  const [assetsLoading, setAssetsLoading] = useState(false)
  const [assetsError, setAssetsError] = useState('')
  const [phase, setPhase] = useState<SubmitPhase>('idle')
  const [error, setError] = useState('')
  const [createdTicket, setCreatedTicket] = useState<Ticket | null>(null)

  const ticketIdRef = useRef<string | null>(null)
  const operationIdRef = useRef<string | null>(null)
  const newButtonRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return
    setAssetsLoading(true)
    setAssetsError('')
    listAssets()
      .then((data) => {
        setAssets(data.items ?? [])
      })
      .catch((err) => {
        setAssetsError(formatError(err))
        setAssets([])
      })
      .finally(() => setAssetsLoading(false))
  }, [open])

  useEffect(() => {
    if (!open) {
      // 关闭后重置所有 transient 状态
      setForm(emptyForm)
      setPhase('idle')
      setError('')
      setCreatedTicket(null)
      ticketIdRef.current = null
      operationIdRef.current = null
      return
    }
    // 打开时生成幂等标识
    ticketIdRef.current = crypto.randomUUID()
    operationIdRef.current = crypto.randomUUID()
  }, [open])

  if (!open) return null

  // “受理失败可重试”阶段不算 busy，必须允许用户点击重试
  const isBusy = phase === 'creating' || phase === 'starting_intake'

  const doCreate = async (): Promise<Ticket | null> => {
    setPhase('creating')
    setError('')
    try {
      const ticket = await createTicket({
        ticket_id: ticketIdRef.current ?? undefined,
        title: form.title,
        description: form.description,
        priority: form.priority,
        asset_id: form.asset_id || null,
      })
      setCreatedTicket(ticket)
      return ticket
    } catch (err) {
      setError(`建单失败：${formatError(err)}`)
      setPhase('idle')
      return null
    }
  }

  const doStartIntake = async (ticket: Ticket): Promise<Ticket | null> => {
    setPhase(
      createdTicket ? 'intake_retrying' : 'starting_intake',
    )
    setError('')
    try {
      const result = await startIntake(ticket.ticket_id, {
        operation_id: operationIdRef.current ?? crypto.randomUUID(),
        text: `${form.title}\n${form.description}`,
        fields: { title: form.title, description: form.description },
        expected_version: ticket.version,
      })
      return result.ticket
    } catch (err) {
      setError(`受理失败：${formatError(err)}`)
      setCreatedTicket(ticket)
      setPhase('intake_retrying')
      return null
    }
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (isBusy) return

    let ticket = createdTicket
    if (!ticket) {
      ticket = await doCreate()
      if (!ticket) return
    }

    const finalTicket = await doStartIntake(ticket)
    if (!finalTicket) return

    setForm(emptyForm)
    onCreated(finalTicket)
    onClose()
  }

  const retryIntake = async () => {
    if (!createdTicket || isBusy) return
    const finalTicket = await doStartIntake(createdTicket)
    if (!finalTicket) return
    setForm(emptyForm)
    onCreated(finalTicket)
    onClose()
  }

  const handleClose = () => {
    if (isBusy) return
    onClose()
  }

  const title =
    phase === 'creating'
      ? '正在创建工单…'
      : phase === 'starting_intake'
        ? '正在启动受理…'
        : phase === 'intake_retrying'
          ? '受理失败，可重试'
          : '提交服务请求'

  const primaryLabel =
    phase === 'intake_retrying'
      ? '重试受理'
      : phase === 'creating' || phase === 'starting_intake'
        ? '提交中…'
        : '提交工单'

  const assetSelect = (
    <label>
      关联资产
      <select
        value={form.asset_id}
        onChange={(e) => setForm({ ...form, asset_id: e.target.value })}
        disabled={isBusy}
      >
        <option value="">不关联</option>
        {assets.map((asset) => (
          <option key={asset.asset_id} value={asset.asset_id}>
            {asset.name || asset.asset_id}（{asset.asset_no}）
          </option>
        ))}
      </select>
      {assetsLoading && (
        <span style={{ fontSize: 11, color: '#69747d' }}>
          <LoaderCircle className="spin" size={12} /> 正在加载资产…
        </span>
      )}
      {assetsError && !assetsLoading && (
        <span style={{ fontSize: 11, color: '#8f3438' }}>
          资产加载失败，
          <button
            type="button"
            onClick={() => {
              setAssetsLoading(true)
              setAssetsError('')
              listAssets()
                .then((data) => setAssets(data.items ?? []))
                .catch((err) => setAssetsError(formatError(err)))
                .finally(() => setAssetsLoading(false))
            }}
            style={{ textDecoration: 'underline', cursor: 'pointer', background: 'none', border: 0, padding: 0 }}
          >
            重试
          </button>
        </span>
      )}
    </label>
  )

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && handleClose()}
    >
      <form
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-ticket-title"
        onSubmit={submit}
      >
        <header>
          <div>
            <span className="eyebrow">新建工单</span>
            <h2 id="create-ticket-title">{title}</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={handleClose}
            aria-label="关闭"
            disabled={isBusy}
          >
            <X />
          </button>
        </header>

        {phase !== 'intake_retrying' && (
          <>
            <label>
              标题
              <input
                autoFocus
                required
                maxLength={512}
                placeholder="标题"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                disabled={isBusy}
              />
            </label>

            <label>
              问题描述
              <textarea
                required
                rows={5}
                maxLength={8000}
                placeholder="问题描述"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                disabled={isBusy}
              />
            </label>

            <label>
              优先级
              <select
                value={form.priority}
                onChange={(e) =>
                  setForm({ ...form, priority: e.target.value as TicketPriority })
                }
                disabled={isBusy}
              >
                <option value="low">低</option>
                <option value="normal">普通</option>
                <option value="high">高</option>
                <option value="urgent">紧急</option>
              </select>
            </label>

            {assetSelect}
          </>
        )}

        {phase === 'intake_retrying' && createdTicket && (
          <div style={{ padding: '10px 0' }}>
            <p>
              工单 <strong>#{createdTicket.ticket_id.slice(0, 8)}</strong> 已创建，但受理尚未完成。
            </p>
            <p style={{ fontSize: 12, color: '#69747d' }}>
              点击「重试受理」继续；直接关闭则稍后从列表打开该工单处理。
            </p>
          </div>
        )}

        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        <footer>
          <button
            type="button"
            ref={newButtonRef}
            className="secondary-action"
            onClick={handleClose}
            disabled={isBusy}
          >
            {phase === 'intake_retrying' ? '稍后再说' : '取消'}
          </button>
          {phase === 'intake_retrying' ? (
            <button
              type="button"
              className="primary-action"
              onClick={retryIntake}
              disabled={isBusy}
            >
              {isBusy ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
              {primaryLabel}
            </button>
          ) : (
            <button className="primary-action" disabled={isBusy}>
              {isBusy ? <LoaderCircle className="spin" size={16} /> : null}
              {primaryLabel}
            </button>
          )}
        </footer>
      </form>
    </div>
  )
}
