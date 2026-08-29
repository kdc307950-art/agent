/**
 * 新建工单弹窗（CreateTicketDialog.tsx）。
 *
 * 职责：
 * - 填写标题/描述/优先级，可选关联资产，提交「建单 + 受理」两步流程。
 * - 打开弹窗时并行加载资产列表，供「关联资产」下拉选择（含加载中/失败重试提示）。
 *
 * 与后端 API 的对应关系（src/api）：
 * - createTicket：创建工单（携带前端生成的 ticket_id，天然幂等）。
 * - startIntake：对已建工单启动受理流程（携带 operation_id 幂等标识与 expected_version 并发控制）。
 * - listAssets：加载可选资产列表。
 *
 * 关键交互逻辑（幂等重试）：
 * - ticket_id 与 operation_id 在弹窗打开时生成一次，整个流程保持不变，失败重试不会产生重复数据。
 * - 建单成功但受理失败后进入 intake_retrying 阶段：禁止再次建单，只允许重试受理或稍后再说。
 * - 受理成功返回的最新工单对象（可能含状态变化）通过 onCreated 回传父组件。
 */
import { useEffect, useRef, useState } from 'react'
import { AlertCircle, LoaderCircle, RefreshCw, X } from 'lucide-react'
import type { Ticket, TicketPriority } from '../types'
import type { AssetListResult } from '../types'
import { ApiError, createTicket, listAssets, startIntake } from '../api'

// 弹窗 props：open 控制显隐；onClose 关闭回调；onCreated 建单+受理成功后回传最新工单
interface CreateTicketDialogProps {
  open: boolean
  onClose: () => void
  onCreated: (ticket: Ticket) => void
}

// 表单字段：标题 / 描述 / 优先级 / 可选关联资产
interface FormState {
  title: string
  description: string
  priority: TicketPriority
  asset_id: string
}

// 空表单初始值：优先级默认 normal、不关联资产
const emptyForm: FormState = {
  title: '',
  description: '',
  priority: 'normal',
  asset_id: '',
}

// 提交流程阶段机：
// idle 初始 / creating 建单中 / starting_intake 受理中 / intake_retrying 受理失败（可重试受理）
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
  // 可选资产列表（打开弹窗时加载一次，失败可单独重试）
  const [assets, setAssets] = useState<AssetListResult['items']>([])
  const [assetsLoading, setAssetsLoading] = useState(false)
  const [assetsError, setAssetsError] = useState('')
  // 提交流程阶段（见 SubmitPhase 注释）
  const [phase, setPhase] = useState<SubmitPhase>('idle')
  const [error, setError] = useState('')
  // 建单成功后暂存的工单：受理失败重试时复用，避免二次建单
  const [createdTicket, setCreatedTicket] = useState<Ticket | null>(null)

  // —— 幂等标识：弹窗每次打开时生成一次，整个建单/受理流程保持不变 ——
  const ticketIdRef = useRef<string | null>(null)
  const operationIdRef = useRef<string | null>(null)
  // 「取消/稍后再说」按钮引用（当前仅用于占位，未做焦点管理扩展）
  const newButtonRef = useRef<HTMLButtonElement | null>(null)

  // 弹窗打开时加载资产列表：成功填充下拉，失败记录错误并清空列表
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

  // 弹窗开关驱动的状态重置/初始化：
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

  // 第一步：创建工单。成功则暂存到 createdTicket；失败回到 idle 允许修改表单重试
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

  // 第二步：启动受理。首次为 starting_intake，失败后重试为 intake_retrying（复用同一 operation_id）
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

  // 主提交：已建单则跳过建单，直接受理；两步都成功才回调 onCreated 并关闭
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

  // 受理失败后的重试入口：仅重试受理，绝不重复建单
  const retryIntake = async () => {
    if (!createdTicket || isBusy) return
    const finalTicket = await doStartIntake(createdTicket)
    if (!finalTicket) return
    setForm(emptyForm)
    onCreated(finalTicket)
    onClose()
  }

  // 关闭守卫：建单/受理进行中禁止关闭，防止流程被打断丢失幂等上下文
  const handleClose = () => {
    if (isBusy) return
    onClose()
  }

  // 弹窗标题文案随阶段变化
  const title =
    phase === 'creating'
      ? '正在创建工单…'
      : phase === 'starting_intake'
        ? '正在启动受理…'
        : phase === 'intake_retrying'
          ? '受理失败，可重试'
          : '提交服务请求'

  // 主按钮文案随阶段变化
  const primaryLabel =
    phase === 'intake_retrying'
      ? '重试受理'
      : phase === 'creating' || phase === 'starting_intake'
        ? '提交中…'
        : '提交工单'

  // 关联资产下拉：加载中/失败提示 + 失败重试按钮；表单提交期间禁用
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
          {/* 单独重试加载资产（不影响主表单） */}
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
    // 遮罩层：点击遮罩空白处（而非弹窗内部）时触发关闭（提交中则忽略）
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
          {/* 关闭按钮：提交中禁用 */}
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

        {/* 正常/提交阶段展示表单字段；受理失败重试阶段隐藏表单，改为展示状态说明 */}
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

            {/* 关联资产下拉（独立组件，含加载/失败重试状态） */}
            {assetSelect}
          </>
        )}

        {/* 受理失败重试阶段：说明工单已创建成功、仅受理未完成，引导用户重试或稍后处理 */}
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

        {/* 错误提示条 */}
        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        <footer>
          {/* 取消/稍后再说：提交中禁用；重试阶段文案变为「稍后再说」 */}
          <button
            type="button"
            ref={newButtonRef}
            className="secondary-action"
            onClick={handleClose}
            disabled={isBusy}
          >
            {phase === 'intake_retrying' ? '稍后再说' : '取消'}
          </button>
          {/* 主按钮：重试阶段为「重试受理」（走 retryIntake），其余阶段为表单提交按钮 */}
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
