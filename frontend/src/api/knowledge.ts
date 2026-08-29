/** 知识库 API。 */

import { api } from './client'
import type { KnowledgeDocumentListResult, UploadDocumentInput } from '../types'

export function listDocuments(limit = 50, offset = 0): Promise<KnowledgeDocumentListResult> {
  return api(`/knowledge/documents?limit=${limit}&offset=${offset}`)
}

export function uploadDocument(input: UploadDocumentInput): Promise<Record<string, unknown>> {
  return api('/knowledge/documents', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function publishDocument(documentId: string, version: number): Promise<Record<string, unknown>> {
  return api(`/knowledge/documents/${encodeURIComponent(documentId)}/publish`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export function retireDocument(documentId: string): Promise<Record<string, unknown>> {
  return api(`/knowledge/documents/${encodeURIComponent(documentId)}/retire`, {
    method: 'POST',
  })
}
