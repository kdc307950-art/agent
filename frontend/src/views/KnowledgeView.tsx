/**
 * 知识库视图（KnowledgeView.tsx）。
 *
 * 职责：
 * - 展示知识文档列表（标题/ID/版本/状态/可见性/分类/更新时间），支持本地关键词搜索。
 * - 上传新文档（以「每行一个分块」的文本形式提交，后端会做分块/索引）、发布草稿、废弃已发布文档。
 *
 * 与后端 API 的对应关系（src/api）：
 * - listDocuments：拉取全部知识文档。
 * - uploadDocument：上传文档（document 元数据 + chunks 分块列表，status 初始为 draft）。
 * - publishDocument / retireDocument：草稿 → 已发布 / 已发布 → 废弃。
 *
 * 关键交互逻辑：
 * - 文档状态机驱动按钮：draft 显示「发布」，published 显示「废弃」，其他状态无操作按钮。
 * - 上传时把 textarea 的每行文本构造成分块（chunk_id 用序号 c0/c1/…，ordinal 保持顺序）。
 * - busyDocId / uploadBusy 用于禁用进行中操作，防止重复提交。
 */
import { useEffect, useState } from 'react'
import { AlertCircle, BookOpen, FilePlus, RefreshCw, Search, Trash2, Upload } from 'lucide-react'
import type { KnowledgeDocument } from '../types'
import { ApiError, listDocuments, publishDocument, retireDocument, uploadDocument } from '../api'

// 上传表单字段（与后端 Document 元数据对应；allowed_departments/chunks 为逗号/换行分隔的文本）
interface UploadForm {
  document_id: string
  version: number
  title: string
  category: string
  visibility: string
  allowed_departments: string
  chunks: string
}

// 空表单初始值：默认 internal 可见、版本 1
const emptyUpload: UploadForm = {
  document_id: '',
  version: 1,
  title: '',
  category: '',
  visibility: 'internal',
  allowed_departments: '',
  chunks: '',
}

function formatError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export default function KnowledgeView() {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 搜索关键词（本地过滤标题/ID/分类）
  const [search, setSearch] = useState('')
  // 上传弹窗开关
  const [showUpload, setShowUpload] = useState(false)
  // 上传表单值
  const [form, setForm] = useState<UploadForm>(emptyUpload)
  // 正在操作（发布/废弃）的文档 id，用于禁用该行按钮
  const [busyDocId, setBusyDocId] = useState<string | null>(null)
  // 上传请求进行中
  const [uploadBusy, setUploadBusy] = useState(false)

  // 从后端重新拉取全部知识文档
  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listDocuments()
      setDocuments(data.items ?? [])
    } catch (err) {
      setError(formatError(err))
      setDocuments([])
    } finally {
      setLoading(false)
    }
  }

  // 挂载时加载一次
  useEffect(() => {
    void load()
  }, [])

  // 本地搜索：关键词匹配标题/文档 ID/分类
  const filtered = documents.filter((doc) => {
    const q = search.trim().toLowerCase()
    if (!q) return true
    return (
      (doc.title ?? '').toLowerCase().includes(q) ||
      (doc.document_id ?? '').toLowerCase().includes(q) ||
      (doc.category ?? '').toLowerCase().includes(q)
    )
  })

  // 提交上传：把每行文本构造成分块（trim 后过滤空行，chunk_id 用序号），
  // 组装 document 元数据（draft 状态）后调用 uploadDocument，成功后关闭弹窗并刷新列表
  const upload = async (event: React.FormEvent) => {
    event.preventDefault()
    setUploadBusy(true)
    setError('')
    try {
      const chunks = form.chunks
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((content, index) => ({ chunk_id: `c${index}`, ordinal: index, content }))
      // 至少需要一个分块，否则后端无内容可索引
      if (chunks.length === 0) throw new Error('至少输入一个知识分块（每行一段）')
      await uploadDocument({
        document: {
          document_id: form.document_id,
          version: Number(form.version),
          title: form.title,
          category: form.category || null,
          visibility: form.visibility,
          allowed_departments: form.allowed_departments
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean),
          status: 'draft',
        },
        chunks,
      })
      setForm(emptyUpload)
      setShowUpload(false)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setUploadBusy(false)
    }
  }

  // 发布草稿文档：发布后成为可检索版本
  const publish = async (documentId: string, version: number) => {
    setBusyDocId(documentId)
    setError('')
    try {
      await publishDocument(documentId, version)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusyDocId(null)
    }
  }

  // 废弃已发布文档：确认后废弃（已发布版本将不可检索）
  const retire = async (documentId: string) => {
    if (!window.confirm(`确认废弃文档 ${documentId} 吗？已发布版本将不可检索。`)) return
    setBusyDocId(documentId)
    setError('')
    try {
      await retireDocument(documentId)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusyDocId(null)
    }
  }

  return (
    <main className="assistant-view">
      <header>
        <div>
          <span className="eyebrow">知识管理</span>
          <h1>知识文档</h1>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {/* 手动刷新文档列表 */}
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          {/* 打开上传弹窗 */}
          <button className="primary-action" onClick={() => setShowUpload(true)}>
            <FilePlus size={17} />
            上传
          </button>
        </div>
      </header>

      <div className="assistant-thread">
        {/* 搜索框：本地过滤 */}
        <div className="search-box">
          <Search size={16} />
          <input
            placeholder="搜索文档标题、ID、分类"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* 错误提示条 */}
        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {/* 空状态：无匹配文档时提示 */}
        {!loading && filtered.length === 0 && (
          <div className="assistant-empty">
            <BookOpen size={34} />
            <h2>暂无知识文档</h2>
          </div>
        )}

        {/* 文档行：draft 显示「发布」，published 显示「废弃」；操作中禁用按钮 */}
        {filtered.map((doc) => (
          <div key={`${doc.document_id}-${doc.version}`} className="requester" style={{ alignItems: 'flex-start' }}>
            <BookOpen />
            <div style={{ flex: 1 }}>
              <strong>{doc.title || doc.document_id}</strong>
              <span>
                {doc.document_id} · v{doc.version} · {doc.status} · {doc.visibility}
                {doc.category ? ` · ${doc.category}` : ''}
                {doc.updated_at ? ` · 更新 ${new Date(doc.updated_at).toLocaleString('zh-CN')}` : ''}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              {/* 草稿 → 发布 */}
              {doc.status === 'draft' && (
                <button
                  className="primary-action"
                  onClick={() => publish(doc.document_id, doc.version)}
                  disabled={busyDocId === doc.document_id}
                >
                  <Upload size={14} />
                  发布
                </button>
              )}
              {/* 已发布 → 废弃 */}
              {doc.status === 'published' && (
                <button
                  className="secondary-action"
                  onClick={() => retire(doc.document_id)}
                  disabled={busyDocId === doc.document_id}
                >
                  <Trash2 size={14} />
                  废弃
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* 上传弹窗：点击遮罩空白处关闭；提交后进入上传中状态 */}
      {showUpload && (
        <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && setShowUpload(false)}>
          <form className="modal" onSubmit={upload}>
            <header>
              <div>
                <span className="eyebrow">新建文档</span>
                <h2>上传知识文档</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setShowUpload(false)} aria-label="关闭">
                ×
              </button>
            </header>
            <label>
              文档 ID
              <input required value={form.document_id} onChange={(e) => setForm({ ...form, document_id: e.target.value })} />
            </label>
            <label>
              版本
              <input required type="number" min={1} value={form.version} onChange={(e) => setForm({ ...form, version: Number(e.target.value) })} />
            </label>
            <label>
              标题
              <input required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            </label>
            <label>
              分类（如 it.vpn）
              <input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
            </label>
            <label>
              可见性
              <select value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
                <option value="public">public</option>
                <option value="internal">internal</option>
                <option value="restricted">restricted</option>
              </select>
            </label>
            <label>
              允许部门（逗号分隔）
              <input value={form.allowed_departments} onChange={(e) => setForm({ ...form, allowed_departments: e.target.value })} />
            </label>
            {/* 分块内容：每行文本将被拆成一个知识分块 */}
            <label>
              分块内容（每行一段）
              <textarea required rows={4} value={form.chunks} onChange={(e) => setForm({ ...form, chunks: e.target.value })} />
            </label>
            <footer>
              <button type="button" className="secondary-action" onClick={() => setShowUpload(false)}>
                取消
              </button>
              {/* 上传中禁用并显示进度文案 */}
              <button className="primary-action" disabled={uploadBusy}>
                {uploadBusy ? '上传中…' : '上传'}
              </button>
            </footer>
          </form>
        </div>
      )}
    </main>
  )
}
