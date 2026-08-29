"""有界 Agentic RAG 编排：让规划器迭代改进检索，但用确定性安全门约束。

职责：
    - 在 KnowledgeAnswerService 之上叠加多轮"检索 -> 规划 -> 再检索"循环
    - 通过 AgenticRAGPolicy 限制轮次、每轮查询数、上下文总量，防止失控发散
    - 最终只返回经由 KnowledgeAnswerService 门控过的 AnswerDecision

关键设计：
    - 规划器只接触"清洗后的检索叶子"（RetrievalHit），永远接触不到租户级内部对象
    - 无论多轮检索结果如何，auto_reply 只有在策略显式允许时才放行，
      保证 Agentic 增强不会绕过既有的自动化应答安全门
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import RetrievalHit, RetrievalPrincipal
from .service import AnswerDecision, KnowledgeAnswerService


class RetrievalPlanner(Protocol):
    """检索规划器协议：根据问题与已有命中，决定下一轮补充查询。

    设计意图：把"再查什么"的决策与"如何执行检索"解耦，
    便于替换为不同的规划策略（LLM 规划、规则规划等）。
    """

    async def next_queries(
        self,
        question: str,
        hits: Sequence[RetrievalHit],
        round_number: int,
    ) -> Sequence[str]:
        """返回下一轮要检索的查询词列表。

        参数：
            question: 用户的原始问题
            hits: 截至当前轮累计收集到的检索命中（尚未去重）
            round_number: 当前轮次（从 0 开始）
        返回：下一轮查询词序列；空序列表示无需继续检索。
        """
        ...


@dataclass(frozen=True, slots=True)
class AgenticRAGPolicy:
    """Agentic 检索的策略参数（不可变，构造后可安全共享）。

    这些阈值共同构成"有界性"：即使规划器行为异常，
    也不会无限检索、也不会把过多上下文塞给答案生成器。
    """

    max_rounds: int = 3
    max_queries_per_round: int = 2
    max_contexts: int = 12
    allow_auto_reply: bool = False


class AgenticRAGService:
    """让规划器迭代改进检索，但绝不绕过 KnowledgeAnswerService 的安全门。

    每个查询仍走完整的"检索 -> 融合 -> 生成 -> 门控"链路，
    Agentic 只负责决定"下一轮再问什么"，不直接生成或放行答案。
    """

    def __init__(
        self,
        answer_service: KnowledgeAnswerService,
        planner: RetrievalPlanner,
        *,
        policy: AgenticRAGPolicy | None = None,
    ) -> None:
        """构造 Agentic RAG 服务。

        参数：
            answer_service: 底层问答服务（负责检索、生成与门控，见 service.py）
            planner: 检索规划器，决定每轮之后补充哪些查询
            policy: 迭代策略；为 None 时使用默认策略
        异常：
            ValueError: 策略轮次或每轮查询数为非正数时抛出
        """
        self.answer_service = answer_service
        self.planner = planner
        self.policy = policy or AgenticRAGPolicy()
        if self.policy.max_rounds < 1 or self.policy.max_queries_per_round < 1:
            raise ValueError("Agentic RAG policy 必须为正数")

    async def answer(
        self,
        principal: RetrievalPrincipal,
        question: str,
        *,
        category: str,
        risk_level: str,
        limit: int = 8,
    ) -> AnswerDecision:
        """执行多轮 Agentic 检索并返回门控后的答案决策。

        参数：
            principal: 检索主体（租户 + 部门 + 可见性），用于 ACL 过滤
            question: 用户原始问题（也是第一轮的初始查询）
            category / risk_level: 透传给底层问答服务的门控上下文
            limit: 每轮每个查询的检索结果条数上限
        返回：
            AnswerDecision：若任一查询得到答案，返回最后一个非空决策的
            副本并强制 auto_reply=False（Agentic 场景不允许自动回复客户）；
            否则返回无答案的兜底决策。
        """
        queries = [question]
        best: AnswerDecision | None = None
        all_hits: list[RetrievalHit] = []
        for round_number in range(self.policy.max_rounds):
            # 每轮最多执行 max_queries_per_round 个查询，控制 LLM 调用成本
            for query in queries[: self.policy.max_queries_per_round]:
                decision = await self.answer_service.answer(
                    principal, query, category=category, risk_level=risk_level, limit=limit
                )
                # 记录最后一个有答案的决策作为兜底结果（多轮取新弃旧）
                if decision.answer is not None:
                    best = decision
                # 仅在策略显式允许时提前返回自动应答，否则继续多轮检索
                if decision.auto_reply and self.policy.allow_auto_reply:
                    return decision
            # Planner receives only sanitized retrieval leaves, never tenant-wide objects.
            # 给规划器的仅是清洗后的检索叶子，绝不暴露租户级内部对象
            lexical = await self.answer_service.repository.lexical_search(
                principal, queries[0], limit=limit
            )
            all_hits.extend(lexical)
            # 上下文总量达到上限即停止扩检，防止答案上下文失控膨胀
            if len(all_hits) >= self.policy.max_contexts:
                break
            next_queries = await self.planner.next_queries(question, all_hits, round_number)
            # 清洗规划器输出：去空白、过滤空串；空列表表示规划器认为无需继续
            queries = [str(item).strip() for item in next_queries if str(item).strip()]
            if not queries:
                break
        if best is not None:
            # 用 dict.fromkeys 去重并保留顺序，再追加"检索已穷尽"标记
            reasons = tuple(dict.fromkeys((*best.reason_codes, "agentic_search_exhausted")))
            # 强制关闭 auto_reply：Agentic 增强的结果必须先经人工确认
            return best.model_copy(update={"auto_reply": False, "reason_codes": reasons})
        # 全程无答案：返回显式表示"检索穷尽且无命中"的兜底决策
        return AnswerDecision(
            answer=None,
            citations=(),
            auto_reply=False,
            reason_codes=("agentic_search_exhausted", "no_retrieval_hits"),
        )
