"""确定性的知识入库流水线：文本清洗、切片、嵌入、落库。

职责：
    - clean_text：统一清洗原始文本（NUL 字符、多余空白/空行）
    - chunk_text：按"字符窗口 + 重叠滑窗"切成带内容寻址 ID 的分块
    - ingest：串起 切片 -> 批量嵌入 -> 文档/分块落库 -> 向量写入 的完整流程

关键设计：
    - 全程确定性：同一文本 + 同一策略必然产出相同分块与 chunk_id，
      重复入库是幂等覆盖而不是重复追加
    - ingest 先把文档以 draft 状态入库，全部向量写成功后才按需发布，
      避免嵌入失败留下"已发布但向量缺失"的半成品
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import KnowledgeChunkInput, KnowledgeDocumentInput, RetrievalPrincipal
from .pgvector import PgVectorRetriever
from .repository import KnowledgeRepository


class DocumentEmbedder(Protocol):
    """批量文档嵌入协议：一次嵌入多个文本，返回等长的向量序列。"""

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class IngestionPolicy:
    """切片策略（不可变，可安全共享）。

    chunk_chars: 每个分块的最大字符数
    overlap_chars: 相邻分块的重叠字符数——保留上下文衔接，
                   避免恰好落在切片边界的关键信息被截断
    """

    chunk_chars: int = 1200
    overlap_chars: int = 160


class KnowledgeIngestionService:
    """知识入库服务：把原始文本转成可独立检索的知识单元。

    依赖三个协作者：
        repository —— 文档 / 分块的持久化（PostgreSQL）
        vector —— 向量检索器（pgvector，负责写入 embedding）
        embedder —— 嵌入提供方（把文本映射为向量）
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        vector: PgVectorRetriever,
        embedder: DocumentEmbedder,
        *,
        embedding_model: str,
        policy: IngestionPolicy | None = None,
    ) -> None:
        """构造知识入库服务。

        参数：
            repository: 知识仓库（文档 / 分块持久化）
            vector: pgvector 检索器（写入 embedding 列）
            embedder: 批量嵌入提供方
            embedding_model: 本次入库使用的嵌入模型名，记录在每个分块上，
                             便于将来按模型隔离或重新嵌入
            policy: 切片策略；为 None 时使用默认值
        异常：
            ValueError: 切片窗口参数不合法（窗口过小或重叠越界）时抛出
        """
        self.repository = repository
        self.vector = vector
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.policy = policy or IngestionPolicy()
        # 窗口过小导致分块碎片化；重叠必须非负且严格小于窗口
        if (
            self.policy.chunk_chars < 200
            or not 0 <= self.policy.overlap_chars < self.policy.chunk_chars
        ):
            raise ValueError("知识切片参数无效")

    def clean_text(self, text: str) -> str:
        """清洗原始文本：去 NUL 字符、压缩空白与连续空行，去首尾空白。

        参数：text: 原始文本
        返回：清洗后的文本
        设计：清洗在切片之前做，保证后续分块的确定性；
              也让分词与向量检索得到更稳定的输入。
        """
        # NUL 字符在 JSON / 数据库传输中易引发问题，先替换为普通空格
        text = text.replace("\x00", " ")
        # 制表符与连续空格统一为单个空格（\n 不在 [ \t] 中，不会被误压）
        text = re.sub(r"[ \t]+", " ", text)
        # 三个及以上连续换行压缩为两个，保留段落结构但不留大片空白
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def chunk_text(self, text: str) -> list[KnowledgeChunkInput]:
        """按策略把清洗后的文本切成带重叠的分块序列。

        参数：text: 原始文本（内部先清洗）
        返回：KnowledgeChunkInput 列表，chunk_id 为"序号 + 内容摘要前 16 位"
        设计：
            - chunk_id 由内容摘要决定：内容不变则 ID 不变，
              重复入库天然幂等，便于增量更新与去重
            - 滑窗步长 = 窗口 - 重叠，保证相邻分块共享重叠区
        """
        cleaned = self.clean_text(text)
        if not cleaned:
            raise ValueError("文档正文为空")
        # 步长小于窗口：相邻窗口重叠 overlap_chars 个字符
        step = self.policy.chunk_chars - self.policy.overlap_chars
        chunks = []
        for ordinal, start in enumerate(range(0, len(cleaned), step)):
            content = cleaned[start : start + self.policy.chunk_chars]
            if not content:
                break
            # 内容寻址：同内容 -> 同摘要 -> 同 chunk_id（16 位十六进制碰撞风险低）
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            chunks.append(
                KnowledgeChunkInput(
                    chunk_id=f"c{ordinal:05d}-{digest}",
                    ordinal=ordinal,
                    content=content,
                    embedding_model=self.embedding_model,
                )
            )
            # 当前窗口已覆盖文本末尾则停止，避免产生空分块
            if start + self.policy.chunk_chars >= len(cleaned):
                break
        return chunks

    async def ingest(
        self,
        tenant_id: str,
        document: KnowledgeDocumentInput,
        text: str,
    ) -> int:
        """执行完整入库流程：切片 -> 嵌入 -> 落库 -> 写向量 -> 按需发布。

        参数：
            tenant_id: 租户 ID（数据隔离边界）
            document: 文档元信息（含目标状态，可被本方法临时改写）
            text: 文档原始正文
        返回：成功入库的分块数量
        设计：
            - 嵌入结果与分块一一对应（strict zip），任何错位都直接失败，
              杜绝向量与内容错配这一最隐蔽的检索事故
            - 文档先以 draft 落库，向量全部写成功后才发布：
              中途失败最多留下 draft，不会出现"已发布但向量缺失"的半成品
        """
        chunks = self.chunk_text(text)
        embeddings = await self.embedder.embed_documents([chunk.content for chunk in chunks])
        if len(embeddings) != len(chunks):
            raise RuntimeError("embedding 数量与切片数量不匹配")
        # 记住调用方期望的最终状态；入库阶段一律强制 draft，保证失败可重试
        target_status = document.status
        await self.repository.put_document(
            tenant_id,
            document.model_copy(update={"status": "draft"}),
            chunks,
        )
        # 检索主体按文档声明的部门白名单构造，供写入向量时携带部门上下文
        principal = RetrievalPrincipal(
            tenant_id=tenant_id, departments=frozenset(document.allowed_departments)
        )
        # strict=True：嵌入与分块必须严格等长配对，长度不齐立即抛错
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            await self.vector.put_embedding(
                principal,
                document_id=document.document_id,
                document_version=document.version,
                chunk_id=chunk.chunk_id,
                embedding=embedding,
                embedding_model=self.embedding_model,
            )
        # 向量全部写入成功后才执行发布（状态机约束见 repository.publish_document_version）
        if target_status == "published":
            await self.repository.publish_document_version(
                tenant_id, document.document_id, document.version
            )
        return len(chunks)
