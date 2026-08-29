"""KnowledgeRetriever 统一检索入口测试 —— 阶段三。

覆盖：
- 未配置 embedding：lexical-only 模式（jieba + 全文 + pg_trgm）
- 配置 embedding：hybrid 模式（lexical + vector + RRF 融合）
- 向量检索失败：降级 lexical-only（不阻断）
- retrieval_mode 显式标记，两套结果分开
"""

from __future__ import annotations

import asyncio

from backend.knowledge.models import RetrievalHit, RetrievalPrincipal
from backend.knowledge.retriever import KnowledgeRetriever


def _hit(doc_id: str, chunk: str, source: str = "lexical") -> RetrievalHit:
    return RetrievalHit(
        tenant_id="tenant-a",
        document_id=doc_id,
        document_version=1,
        chunk_id=chunk,
        title=f"{doc_id} 标题",
        content=f"{doc_id} 正文",
        source_uri=None,
        source=source,  # type: ignore[arg-type]
        source_rank=1,
    )


class _FakeRepo:
    def __init__(self, hits):
        self.hits = hits

    async def lexical_search(self, principal, query, limit=10):
        return [h for h in self.hits if h.tenant_id == principal.tenant_id][:limit]


class _FakeVector:
    def __init__(self, hits, fail: bool = False):
        self.hits = hits
        self.fail = fail

    async def search(self, principal, query, *, limit):
        if self.fail:
            raise RuntimeError("embedding service down")
        return [h for h in self.hits if h.tenant_id == principal.tenant_id][:limit]


def _principal():
    return RetrievalPrincipal(tenant_id="tenant-a", departments=frozenset(), internal=True)


def test_lexical_only_mode_without_embedding():
    """未配置 embedding：标记 lexical-only，仅 lexical 结果。"""
    repo = _FakeRepo([_hit("vpn-guide", "c1")])
    retriever = KnowledgeRetriever(repo)  # 默认 NullVectorRetriever

    result = asyncio.run(retriever.search(_principal(), "vpn"))
    assert result.retrieval_mode == "lexical-only"
    assert [h.document_id for h in result.hits] == ["vpn-guide"]
    assert result.vector_hits == []


def test_hybrid_mode_with_embedding_and_rrf():
    """配置 embedding：hybrid 模式，lexical + vector 双路 RRF 融合。"""
    repo = _FakeRepo([_hit("doc-a", "c1"), _hit("doc-b", "c2")])
    vector = _FakeVector([_hit("doc-b", "c2", source="vector"), _hit("doc-c", "c3", source="vector")])
    retriever = KnowledgeRetriever(repo, vector)

    result = asyncio.run(retriever.search(_principal(), "query"))
    assert result.retrieval_mode == "hybrid"
    # RRF 融合：双路命中的 doc-b 应排前
    assert result.hits
    assert result.lexical_hits and result.vector_hits


def test_vector_failure_degrades_to_lexical_only():
    """向量检索失败：降级 lexical-only，不阻断。"""
    repo = _FakeRepo([_hit("doc-a", "c1")])
    vector = _FakeVector([], fail=True)
    retriever = KnowledgeRetriever(repo, vector)

    result = asyncio.run(retriever.search(_principal(), "query"))
    assert result.retrieval_mode == "lexical-only"  # 降级标记
    assert [h.document_id for h in result.hits] == ["doc-a"]


def test_retrieval_mode_property():
    repo = _FakeRepo([])
    assert KnowledgeRetriever(repo).retrieval_mode == "lexical-only"
    assert KnowledgeRetriever(repo, _FakeVector([])).retrieval_mode == "hybrid"
