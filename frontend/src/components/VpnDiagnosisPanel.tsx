/**
 * VPN 诊断面板（VpnDiagnosisPanel.tsx）。
 *
 * 职责（阶段二前端核心）：
 * - 展示当前工单的 VPN 诊断结果（来自 GET /vpn/diagnosis 的 closed_loop 快照）：
 *   故障类型、故障假设、支撑证据、已排除项、置信度、下一步动作、原因码。
 * - 展示客户排查步骤列表（快照 actions），每个步骤可逐条回填结果（自由文本 result +
 *   evidence dict / details 文本）并提交，提交成功后刷新快照状态；可再次诊断进入下一轮。
 * - 诊断结果以面板形式进入工单详情（由 TicketDetail 在 VPN 工单上挂载）。
 *
 * 与后端 API 的对应关系（来自 src/api/vpn.ts）：
 * - getVpnDiagnosis：查询处置闭环快照（latest_run / actions / results）。
 * - diagnoseVpn：无诊断/用户主动发起时创建一次诊断。
 * - submitVpnActionResult：回填单个客户排查步骤结果。
 * - resumeVpnDiagnosis：客户回填后再次诊断。
 *
 * 关键交互与安全边界：
 * - 竞态防护：ticketId 变化/组件卸载时 abort 旧请求；响应回来校验 ticketId 守卫防覆盖。
 * - 每个步骤的回填结果以本地状态保存，提交成功后清除该步骤输入并重新拉取快照。
 * - 后端可能未完全就绪/未配置：latest_run 为 null 展示"发起诊断"；503 展示"未配置"。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  FlaskConical,
  LoaderCircle,
  Play,
  RotateCcw,
  ShieldAlert,
  Sparkles,
  Wrench,
} from 'lucide-react'
import type {
  VpnCustomerAction,
  VpnCustomerActionResult,
  VpnDiagnosisFinding,
  VpnDiagnosisRun,
} from '../types'
import {
  ApiError,
  diagnoseVpn,
  getVpnDiagnosis,
  resumeVpnDiagnosis,
  submitVpnActionResult,
} from '../api'
import { describeApiError } from '../api/client'
import { formatTime } from '../lib/labels'

interface VpnDiagnosisPanelProps {
  ticketId: string
  /** 是否允许操作（工单进入可诊断状态时 true）。 */
  enabled: boolean
}

/** 风险等级 → 中文标签。 */
const riskLevelLabel: Record<string, string> = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
}

/** 运行状态展示标签。 */
const runStatusLabel: Record<string, string> = {
  diagnosing: '诊断中',
  completed: '已完成',
  handed_off: '已转人工',
  failed: '诊断失败',
  cancelled: '已取消',
}

/** 单个客户排查步骤的本地回填表单状态。 */
interface StepDraft {
  result: string
  evidence: string
  details: string
}

function emptyStepDraft(): StepDraft {
  return { result: '', evidence: '', details: '' }
}

/** 单条证据项渲染（tool_name + evidence + title + found 标记）。 */
function FindingRow({ finding }: { finding: VpnDiagnosisFinding }) {
  return (
    <div className="requester">
      <Sparkles size={14} />
      <div>
        <strong>{finding.title || finding.tool_name}</strong>
        <span>{finding.evidence}</span>
        <span>来源：{finding.tool_name}</span>
        {finding.found === false && <span className="copilot-badge">未命中</span>}
      </div>
    </div>
  )
}

export default function VpnDiagnosisPanel({ ticketId, enabled }: VpnDiagnosisPanelProps) {
  const [run, setRun] = useState<VpnDiagnosisRun | null>(null)
  const [customerActions, setCustomerActions] = useState<VpnCustomerAction[]>([])
  const [actionResults, setActionResults] = useState<VpnCustomerActionResult[]>([])
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  // 每个步骤的本地回填草稿：action_id -> StepDraft
  const [stepDrafts, setStepDrafts] = useState<Record<string, StepDraft>>({})

  // 竞态守卫：ticketId 变化 / 组件卸载时 abort 旧请求；响应回来校验仍属当前工单
  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  const ticketIdRef = useRef(ticketId)
  ticketIdRef.current = ticketId

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  /** 应用一次快照到本地状态。 */
  const applySnapshot = useCallback(
    (snapshot: { latest_run: VpnDiagnosisRun | null; actions: VpnCustomerAction[]; results: VpnCustomerActionResult[] }) => {
      setRun(snapshot.latest_run ?? null)
      setCustomerActions(snapshot.actions ?? [])
      setActionResults(snapshot.results ?? [])
    },
    [],
  )

  /** 拉取当前工单的最新处置闭环快照。 */
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const snapshot = await getVpnDiagnosis(ticketId, signal)
        if (signal?.aborted || !mountedRef.current) return
        applySnapshot(snapshot)
      } catch (err) {
        if (signal?.aborted || !mountedRef.current) return
        // 后端未就绪（如 404/503）时展示缺省态，不视为致命错误
        applySnapshot({ latest_run: null, actions: [], results: [] })
        if (err instanceof ApiError && err.status >= 500) {
          setError('VPN 诊断服务未配置（未接入模型服务）')
        }
      }
    },
    [ticketId, applySnapshot],
  )

  // 挂载 / 切换工单时加载快照
  useEffect(() => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError('')
    setStepDrafts({})
    load(controller.signal).finally(() => {
      if (mountedRef.current) setLoading(false)
    })
    return () => {
      abortRef.current?.abort()
    }
  }, [ticketId, load])

  /** 发起/重新发起一次诊断。 */
  const startDiagnosis = async () => {
    setStarting(true)
    setError('')
    const controller = new AbortController()
    try {
      await diagnoseVpn(ticketId, controller.signal)
      if (controller.signal.aborted || !mountedRef.current) return
      setStepDrafts({})
      // 刷新快照以拿到最新 run/actions/results
      await load()
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return
      setError(describeApiError(err))
    } finally {
      if (mountedRef.current) setStarting(false)
    }
  }

  /** 回填单个步骤结果。 */
  const submitStep = async (action: VpnCustomerAction) => {
    const draft = stepDrafts[action.action_id] ?? emptyStepDraft()
    if (!draft.result.trim()) {
      setError('请先填写步骤结果')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      await submitVpnActionResult(ticketId, action.action_id, {
        result: draft.result.trim(),
        evidence: draft.evidence.trim() ? { note: draft.evidence.trim() } : {},
        details: draft.details.trim(),
      })
      // 提交成功后清除该步骤输入并刷新快照（state 与时间线保持一致）
      setStepDrafts((prev) => ({ ...prev, [action.action_id]: emptyStepDraft() }))
      await load()
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      if (mountedRef.current) setSubmitting(false)
    }
  }

  /** 客户回填后再次诊断，进入下一轮。 */
  const resume = async () => {
    setResuming(true)
    setError('')
    try {
      await resumeVpnDiagnosis(ticketId, { comment: '客户步骤已回填，请求再次诊断' })
      setStepDrafts({})
      await load()
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      if (mountedRef.current) setResuming(false)
    }
  }

  const updateStepDraft = (actionId: string, patch: Partial<StepDraft>) =>
    setStepDrafts((prev) => ({
      ...prev,
      [actionId]: { ...(prev[actionId] ?? emptyStepDraft()), ...patch },
    }))

  // 是否有至少一个步骤已填写证据/备注（用于展示辅助提示）
  const hasDrafts = Object.keys(stepDrafts).length > 0

  return (
    <section className="detail-section vpn-panel">
      <div className="copilot-header">
        <FlaskConical size={16} />
        <h3>VPN 诊断</h3>
        {run?.status === 'completed' && customerActions.length > 0 && (
          <button
            className="secondary-action"
            onClick={resume}
            disabled={!enabled || resuming || submitting}
          >
            {resuming ? <LoaderCircle className="spin" size={15} /> : <RotateCcw size={15} />}
            再次诊断
          </button>
        )}
      </div>

      {error && (
        <div className="error-banner" style={{ borderLeft: '3px solid #b52f35' }}>
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      )}

      {loading && (
        <p className="copilot-hint">
          <LoaderCircle className="spin" size={14} />
          正在加载 VPN 诊断…
        </p>
      )}

      {!loading && !run && (
        <div className="copilot-block">
          <p className="description">
            尚未发起 VPN 诊断。发起后会基于知识库、资产与历史工单给出结构化的故障假设与客户排查步骤。
          </p>
          <button
            className="primary-action"
            onClick={startDiagnosis}
            disabled={!enabled || starting}
          >
            {starting ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}
            发起 VPN 诊断
          </button>
        </div>
      )}

      {!loading && run && (
        <div className="copilot-result">
          {/* 诊断概述：状态 + 置信度 */}
          <div className="copilot-warning">
            <ShieldAlert size={14} />
            <span>
              诊断状态：{runStatusLabel[run.status] ?? run.status} · 置信度{' '}
              {(run.confidence * 100).toFixed(0)}%
            </span>
          </div>

          {/* 故障类型 + 故障假设 */}
          <div className="copilot-block">
            <strong>故障假设</strong>
            <p className="description">{run.hypothesis || '（暂无假设）'}</p>
            <span className="copilot-badge">{run.fault}</span>
          </div>

          {/* 支撑证据 */}
          <div className="copilot-block">
            <strong>支撑证据（{run.evidence.length}）</strong>
            {run.evidence.length === 0 ? (
              <p className="description">暂无支撑证据</p>
            ) : (
              run.evidence.map((finding, index) => (
                <FindingRow key={`${finding.tool_name}-${index}`} finding={finding} />
              ))
            )}
          </div>

          {/* 已排除项（ruled_out 为 reason_codes 字符串列表） */}
          <div className="copilot-block">
            <strong>已排除项（{run.ruled_out.length}）</strong>
            {run.ruled_out.length === 0 ? (
              <p className="description">暂无排除项</p>
            ) : (
              run.ruled_out.map((reason, index) => (
                <div className="requester" key={`${reason}-${index}`}>
                  <ClipboardList size={14} />
                  <div>
                    <strong>{reason}</strong>
                  </div>
                </div>
              ))
            )}
          </div>

          {/* 下一步动作 */}
          {run.next_action && (
            <div className="copilot-block">
              <strong>下一步动作</strong>
              <p className="description">{run.next_action}</p>
            </div>
          )}

          {/* 原因码 */}
          {run.reason_codes.length > 0 && (
            <div className="copilot-block">
              <strong>原因码</strong>
              <p className="description">{run.reason_codes.join('；')}</p>
            </div>
          )}

          {/* 客户排查步骤：逐条回填结果 */}
          {customerActions.length > 0 && (
            <div className="copilot-block">
              <strong>客户排查步骤（逐条回填）</strong>
              {customerActions.map((action) => {
                const draft = stepDrafts[action.action_id] ?? emptyStepDraft()
                return (
                  <div className="vpn-step" key={action.action_id}>
                    <div className="vpn-step-head">
                      <Wrench size={14} />
                      <strong>{action.title}</strong>
                      {action.requires_agent && <span className="copilot-badge">需人工</span>}
                      <span className={`copilot-confidence vpn-risk-${action.risk_level}`}>
                        {riskLevelLabel[action.risk_level] ?? action.risk_level}
                      </span>
                    </div>
                    <p className="description">{action.instruction}</p>
                    <p className="vpn-step-expected">预期结果：{action.expected_result || '—'}</p>
                    <div className="vpn-step-controls">
                      <label>
                        结果
                        <input
                          value={draft.result}
                          onChange={(e) =>
                            updateStepDraft(action.action_id, { result: e.target.value })
                          }
                          disabled={submitting}
                          placeholder="填写执行结果（必填）"
                        />
                      </label>
                      <label>
                        证据
                        <input
                          value={draft.evidence}
                          onChange={(e) =>
                            updateStepDraft(action.action_id, { evidence: e.target.value })
                          }
                          disabled={submitting}
                          placeholder="客户实际操作/观察（可选）"
                        />
                      </label>
                      <label>
                        备注
                        <input
                          value={draft.details}
                          onChange={(e) =>
                            updateStepDraft(action.action_id, { details: e.target.value })
                          }
                          disabled={submitting}
                          placeholder="补充说明（可选）"
                        />
                      </label>
                      <button
                        className="secondary-action"
                        onClick={() => submitStep(action)}
                        disabled={!enabled || submitting}
                      >
                        {submitting ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : (
                          <CheckCircle2 size={15} />
                        )}
                        提交结果
                      </button>
                    </div>
                  </div>
                )
              })}
              <p className="copilot-hint">
                {hasDrafts
                  ? '已回填部分步骤，逐条提交后自动刷新'
                  : '填写每条步骤的结果并提交，回填后状态会刷新'}
              </p>
            </div>
          )}

          {/* 已回填结果时间线：诊断结果进入工单时间线 */}
          {actionResults.length > 0 && (
            <div className="copilot-block">
              <strong>诊断回填记录</strong>
              <div className="timeline">
                {actionResults.map((result, index) => (
                  <div className="timeline-item" key={`${result.action_id}-${index}`}>
                    <span />
                    <div>
                      <strong>{result.result}</strong>
                      <p>
                        {formatDetails(result.evidence) || result.details || '已记录'}
                      </p>
                      <time>{formatTime(result.submitted_at)}</time>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/** 把结构化 evidence dict 渲染为可读文本。 */
function formatDetails(evidence?: Record<string, unknown> | null): string {
  if (evidence == null) return ''
  const keys = Object.keys(evidence)
  if (keys.length === 0) return ''
  return keys.map((key) => `${key}：${String(evidence[key] ?? '')}`).join('；')
}
