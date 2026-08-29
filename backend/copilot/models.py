"""Resolution Copilot 结构化数据契约（Agent 2 的输入输出模型）。

职责：
    - CopilotRequest：一次 Copilot 生成的入参（工单上下文快照）
    - CopilotResult：Agent 2 的结构化输出（草稿/步骤/引用/置信度/风险标记/工具轨迹）
    - CopilotDraft：持久化草稿记录（copilot_drafts 表一行）

关键设计：
    - 全部模型 extra="forbid"：禁止自由返回任意字段，杜绝把模型文本解析成业务命令
    - CopilotResult 的 citations 带 document_id/version/chunk_id 三元组，
      由答案门禁与知识库命中白名单比对，模型伪造引用直接拒绝
    - auto_reply 恒为 False：Agent 2 只生成草稿，不直接发送客户消息
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CopilotRequest(BaseModel):
    """Copilot 生成请求：工单上下文的不可变快照（由 API 层组装）。

    只携带解决阶段所需的只读上下文；租户/坐席身份由 RunContext 提供，
    不进入本模型（避免请求体伪造身份）。
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    requester_id: str = Field(min_length=1, max_length=128)
    ticket_text: str = Field(min_length=1, max_length=8_000)
    category: str | None = Field(default=None, max_length=128)
    asset_id: str | None = Field(default=None, max_length=64)
    current_status: str = Field(min_length=1, max_length=32)


class CopilotCitation(BaseModel):
    """知识引用三元组（持久化/展示形态，与知识库 Citation 对齐）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=128)
    document_version: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)

    @property
    def citation_key(self) -> tuple[str, int, str]:
        """稳定唯一键：文档 + 版本 + 分块（权威校验与白名单匹配用）。"""
        return (self.document_id, self.document_version, self.chunk_id)


class CopilotResult(BaseModel):
    """Copilot 生成结果（结构化，禁止自由字段）。

    字段说明：
        draft_answer:        客服回复草稿（None 表示放弃作答）
        troubleshooting_steps: 排查步骤列表
        citations:            通过门禁的知识引用（白名单校验后）
        confidence:           置信度 0..1
        needs_human_review:   是否需要人工复核（无引用/敏感类别/低置信度等）
        reason_codes:         门控/放弃原因编码（空则记 "gate_passed"）
        tool_trace:           工具调用轨迹（审计与展示用）
        auto_reply:           恒为 False —— 只生成草稿，绝不自动发送
    """

    model_config = ConfigDict(extra="forbid")

    draft_answer: str | None = Field(default=None, max_length=8_000)
    troubleshooting_steps: list[str] = Field(default_factory=list, max_length=20)
    citations: list[CopilotCitation] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_review: bool = True
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    auto_reply: Literal[False] = False
    # 失败/超限编码（copilot_timeout / tool_call_limit_exceeded / model_failed ...）；
    # None 表示本次生成成功（可能仍需人工复核，见 needs_human_review）
    error_code: str | None = Field(default=None, max_length=64)
    # 检索模式（阶段二）：lexical-only / hybrid；degraded 表示配置了向量但本次降级
    retrieval_mode: str | None = Field(default=None, max_length=16)
    degraded: bool = False


class CopilotDraft(BaseModel):
    """copilot_drafts 表一行的持久化模型（生成/审批状态机）。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    tenant_id: str
    ticket_id: str
    run_id: str
    draft_answer: str | None
    steps: list[str]
    citations: list[CopilotCitation]
    confidence: float
    needs_human_review: bool
    status: Literal["generated", "reviewing", "approved", "rejected", "expired"]
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None


class CopilotRunRecord(BaseModel):
    """copilot_runs 表一行的持久化模型（每次 Agent 执行）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    ticket_id: str
    agent_name: str
    status: Literal["running", "completed", "failed", "rejected"]
    operation_id: str
    started_at: datetime
    completed_at: datetime | None = None
    tool_calls: int = 0
    error_code: str | None = None
    latency_ms: int | None = None
