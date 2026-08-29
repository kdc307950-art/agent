"""知识库的持久化与检索数据模型（Pydantic）。

职责：
    - 定义知识文档 / 分块 / 检索主体 / 检索命中 / 引用等核心数据结构
    - 用字段约束（正则、长度、枚举）在入口处拦截非法输入

关键设计：
    - 输入模型一律 extra="forbid"：拒绝未知字段，防止脏数据流入
    - RetrievalHit / Citation 等"对外"模型 frozen=True：不可变，
      检索结果可在多轮 Agentic 流程中安全共享，杜绝意外篡改
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeDocumentInput(BaseModel):
    """知识文档的输入元信息（创建 / 覆盖入库时提交）。

    字段说明：
        document_id: 业务侧文档 ID（仅字母数字与 _ . -，最长 128）
        version: 文档版本号（>=1），同一 document_id 可保留多版本
        status: 生命周期状态：draft 草稿 -> published 已发布 -> retired 已停用
                （状态机迁移约束见 repository.publish_document_version）
        visibility: 可见性分级 public / internal / restricted，
                    语义见 RetrievalPrincipal.internal 的注释
        allowed_departments: 允许访问的部门白名单（空元组 = 不限制）
        valid_from / valid_until: 有效期窗口，检索时按当前时间过滤
        metadata: 业务自定义元数据（JSONB 存储）
    """

    model_config = ConfigDict(extra="forbid")

    # 白名单正则：ID 只能含字母数字与 _ . -，杜绝路径注入 / 非法字符
    document_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    source_uri: str | None = Field(default=None, max_length=2_048)
    status: Literal["draft", "published", "retired"] = "draft"
    category: str | None = Field(default=None, max_length=128)
    visibility: Literal["public", "internal", "restricted"] = "internal"
    allowed_departments: tuple[str, ...] = ()
    created_by: str | None = Field(default=None, max_length=128)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunkInput(BaseModel):
    """知识分块的输入模型（一段可独立检索的文本单元）。

    字段说明：
        chunk_id: 分块 ID，入库侧用"序号 + 内容摘要"生成（见 ingestion.chunk_text），
                  内容不变则 ID 不变，天然幂等
        ordinal: 在文档内的序号（>=0），用于恢复文档顺序
        content: 分块正文（1..20000 字符）
        embedding_ref: 可选的外部嵌入引用（如对象存储地址）
        embedding_model: 生成该分块向量的嵌入模型名
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=20_000)
    embedding_ref: str | None = Field(default=None, max_length=512)
    embedding_model: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalPrincipal(BaseModel):
    """检索主体：一次检索请求的"身份 + 可见性"声明。

    所有检索 SQL 都以它作为强制过滤条件（租户隔离 + 部门 ACL），
    见 repository.lexical_search 与 pgvector.search_embedding。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    departments: frozenset[str] = frozenset()
    # 检索可见性语义（与 knowledge_documents.visibility 对应）：
    #   public      —— 任何检索主体可见（部门 ACL 仍生效）；
    #   internal    —— 仅 internal=True 的检索主体可见（客服/内部系统）；
    #   restricted  —— 仅 internal=True 且文档声明 allowed_departments
    #                  且包含主体部门时可见（比 internal 更严格）。
    # 客户工单建议回复场景传 internal=False；客服工作台传 internal=True。
    internal: bool = False


class RetrievalHit(BaseModel):
    """一次检索命中的不可变快照（词法 / 向量 / 混合融合的公共形态）。

    字段说明：
        source: 命中来源：lexical 词法、vector 向量、hybrid 双路融合
        source_rank: 该来源内部的排名（>=1，由 SQL 窗口函数产生）
        fused_score: RRF 融合分数（仅 hybrid 融合场景非零）
        key: (document_id, document_version, chunk_id) 三元组，
             用于跨来源去重与引用白名单匹配（见 service.reciprocal_rank_fusion）
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    document_id: str
    document_version: int
    chunk_id: str
    title: str
    content: str
    source_uri: str | None
    source: Literal["lexical", "vector", "hybrid"]
    source_rank: int = Field(ge=1)
    fused_score: float = Field(default=0.0, ge=0.0)
    # 向量相似度（1 - 余弦距离，仅 vector 来源命中非 None）：
    # 供检索后拒答阈值判定「无答案」——低于阈值的命中不应进入证据链
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def key(self) -> tuple[str, int, str]:
        """稳定的唯一键：文档 + 版本 + 分块，跨检索来源可比较。"""
        return self.document_id, self.document_version, self.chunk_id


class Citation(BaseModel):
    """对外暴露的答案引用（带标题与来源 URI，供前端展示）。

    与 GeneratedCitation 的区别：GeneratedCitation 是模型输出的
    原始三元组；Citation 是经过白名单校验、补充了展示信息的最终形态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_version: int
    chunk_id: str
    title: str
    source_uri: str | None = None


class KnowledgeEvidence(BaseModel):
    """知识工具输出的统一结构化证据（阶段一：工具输出契约）。

    真实 search_knowledge 不再返回纯展示文本，而是返回
    {"content": 展示文本, "evidence": [KnowledgeEvidence...]} 的 JSON；
    Agent 看到 content（可读），系统保留 evidence（引用白名单唯一来源），
    避免从展示文本反向解析引用。

    字段说明：
        document_id / document_version / chunk_id: 引用三元组
        title: 文档标题
        content: 命中分块正文（供门禁上下文与审计）
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=128)
    document_version: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    content: str = Field(default="", max_length=8_000)

    @property
    def citation_key(self) -> tuple[str, int, str]:
        """稳定唯一键：文档 + 版本 + 分块（引用白名单匹配用）。"""
        return (self.document_id, self.document_version, self.chunk_id)
