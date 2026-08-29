import { useEffect, useState } from 'react'
import { AlertCircle, BookOpen, FilePlus, RefreshCw, Search, Trash2, Upload } from 'lucide-react'
import type { KnowledgeDocument } from '../types'
import { ApiError, listDocuments, publishDocument, retireDocument, uploadDocument } from '../api'

interface UploadForm {
  document_id: string
  version: number
  title: string
  category: string
  visibility: string
  allowed_departments: string
  chunks: string
}

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
  const [search, setSearch] = useState('')
  const [showUpload, setShowUpload] = useState(false)
  const [form, setForm] = useState<UploadForm>(emptyUpload)
  const [busyDocId, setBusyDocId] = useState<string | null>(null)
  const [uploadBusy, setUploadBusy] = useState(false)

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

  useEffect(() => {
    void load()
  }, [])

  const filtered = documents.filter((doc) => {
    const q = search.trim().toLowerCase()
    if (!q) return true
    return (
      (doc.title ?? '').toLowerCase().includes(q) ||
      (doc.document_id ?? '').toLowerCase().includes(q) ||
      (doc.category ?? '').toLowerCase().includes(q)
    )
  })

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
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          <button className="primary-action" onClick={() => setShowUpload(true)}>
            <FilePlus size={17} />
            上传
          </button>
        </div>
      </header>

      <div className="assistant-thread">
        <div className="search-box">
          <Search size={16} />
          <input
            placeholder="搜索文档标题、ID、分类"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {!loading && filtered.length === 0 && (
          <div className="assistant-empty">
            <BookOpen size={34} />
            <h2>暂无知识文档</h2>
          </div>
        )}

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
            <label>
              分块内容（每行一段）
              <textarea required rows={4} value={form.chunks} onChange={(e) => setForm({ ...form, chunks: e.target.value })} />
            </label>
            <footer>
              <button type="button" className="secondary-action" onClick={() => setShowUpload(false)}>
                取消
              </button>
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
