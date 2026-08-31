"""LLM 支撑的答案生成器与检索规划器（带安全兜底）。

职责：
    - LlmAnswerGenerator：依据检索上下文生成客服答案与引用，输出严格 JSON
    - LlmRetrievalPlanner：根据问题与已有命中，决定是否/如何补充检索

关键设计：
    - 任何 LLM 调用失败都不向上抛：生成失败转为 abstained（拒绝作答），
      规划失败返回空查询（停止扩检），保证"模型不可用也不破坏主流程"
    - 提示词强制输出严格 JSON，解析失败同样走兜底路径
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .models import RetrievalHit
from .service import GeneratedAnswer, GeneratedCitation

logger = logging.getLogger(__name__)

# 答案生成系统提示词：约束模型只依据给定上下文作答、输出严格 JSON、
# 引用只能来自上下文、无把握时必须 abstained（这些约束是安全门的数据来源）
_SYSTEM_GENERATE = (
    "你是客服知识库回答助手。仅依据提供的上下文回答问题；"
    '输出严格 JSON：{"text": 答案, "citations": [{"document_id", "document_version", "chunk_id"}], "abstained": bool}。'
    "citations 只能引用提供的上下文；没有把握时 abstained 设为 true。"
)

# 检索规划系统提示词：模型只能输出 {"queries": [...]}，空数组 = 无需继续检索
_SYSTEM_PLAN = (
    "你是客服检索规划器。根据问题和已有检索结果决定是否需要补充检索。"
    '输出严格 JSON：{"queries": [字符串列表]}；不需要补充检索时输出 {"queries": []}。'
)


def _extract_json(text: str) -> dict:
    """从模型输出中提取第一个最外层 JSON 对象并解析。

    参数：text: 模型原始输出（可能夹杂解释性文字）
    返回：解析后的 dict
    异常：找不到 {..} 或 JSON 非法时抛 ValueError（由调用方统一兜底）
    """
    # DOTALL 让 . 匹配换行；{.*} 贪心取第一个 { 到最后一个 }，
    # 容忍模型输出前后的多余文本
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("模型未返回 JSON")
    # 非法 JSON 由调用方统一捕获并走兜底路径
    return json.loads(match.group(0))


class LlmAnswerGenerator:
    """基于 ChatOpenAI 的答案生成器。

    低温度（默认 0.1）换取更稳定的输出；生成结果结构化为
    GeneratedAnswer（text / citations / abstained），供服务层门控。
    """

    def __init__(
        self, *, api_key: str, base_url: str, model: str, temperature: float = 0.1
    ) -> None:
        """构造生成器。

        参数：
            api_key / base_url / model: OpenAI 兼容端点配置
            temperature: 采样温度，越低越稳定
        """
        self._client = ChatOpenAI(
            api_key=SecretStr(api_key),
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=1200,  # type: ignore[call-arg]  # langchain-openai 1.5.x 透传 kwargs，运行时生效
        )

    async def generate(
        self,
        question: str,
        contexts: Sequence[RetrievalHit],
    ) -> GeneratedAnswer:
        """基于上下文生成答案，任何异常都降级为 abstained 而非抛错。

        参数：
            question: 用户问题
            contexts: 供参考的检索命中（按融合排名传入）
        返回：
            GeneratedAnswer；模型失败 / 输出非法时返回
            text="" 且 abstained=True 的兜底结果
        """
        # 把命中拼成带 [文档/版本/分块] 前缀的上下文块，便于模型逐条回引
        context_text = "\n---\n".join(
            f"[{hit.document_id}/{hit.document_version}/{hit.chunk_id}] {hit.content}"
            for hit in contexts
        )
        try:
            response = await self._client.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_GENERATE),
                    HumanMessage(content=f"问题：{question}\n\n上下文：\n{context_text}"),
                ]
            )
            payload = _extract_json(str(response.content))
            if not isinstance(payload, dict):
                raise ValueError("模型 JSON 顶层必须是对象")
        except Exception as exc:
            # 兜底：生成失败绝不能把错误暴露给客服链路，转 abstained 拒绝作答
            logger.warning("答案生成失败，转为 abstained: %s", exc)
            return GeneratedAnswer(text="", citations=(), abstained=True)
        citations = []
        # 逐条解析引用，字段缺失 / 类型错误的分支直接跳过：
        # 宁可少一条引用，也不让一条坏引用污染整个答案
        raw_citations = payload.get("citations")
        if not isinstance(raw_citations, list):
            raw_citations = []
        for item in raw_citations:
            try:
                citations.append(
                    GeneratedCitation(
                        document_id=str(item["document_id"]),
                        document_version=int(item["document_version"]),
                        chunk_id=str(item["chunk_id"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        raw_text = payload.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        # 模型明确 abstained 或文本为空，都视为放弃作答
        abstained = payload.get("abstained") is True or not text
        return GeneratedAnswer(text=text, citations=tuple(citations), abstained=abstained)


class LlmRetrievalPlanner:
    """基于 LLM 的检索规划器：决定下一轮补充哪些查询。

    零温度（默认 0.0）追求确定性决策；失败时返回空查询，
    让上层 AgenticRAGService 安全终止扩检。
    """

    def __init__(
        self, *, api_key: str, base_url: str, model: str, temperature: float = 0.0
    ) -> None:
        """构造规划器。

        参数：
            api_key / base_url / model: OpenAI 兼容端点配置
            temperature: 采样温度，规划决策用 0 保证可复现
        """
        self._client = ChatOpenAI(
            api_key=SecretStr(api_key),
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=400,  # type: ignore[call-arg]  # langchain-openai 1.5.x 透传 kwargs，运行时生效
        )

    async def next_queries(
        self,
        question: str,
        hits: Sequence[RetrievalHit],
        round_number: int,
    ) -> Sequence[str]:
        """根据问题与已有命中，返回下一轮补充查询；失败返回空列表。

        参数：
            question: 用户原始问题
            hits: 截至当前轮已收集的检索命中
            round_number: 当前轮次（从 0 开始）
        返回：清洗后的查询词列表；空列表表示"无需 / 无法继续扩检"
        """
        try:
            response = await self._client.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PLAN),
                    # 只喂标题而非全文：控制 token 成本，也避免泄露内容细节
                    HumanMessage(
                        content=(
                            f"问题：{question}\n"
                            f"轮次：{round_number}\n"
                            f"已有结果标题：{[hit.title for hit in hits]}"
                        )
                    ),
                ]
            )
            payload = _extract_json(str(response.content))
            if not isinstance(payload, dict):
                raise ValueError("模型 JSON 顶层必须是对象")
        except Exception as exc:
            # 兜底：规划失败就停止补充检索，不把异常上抛给主流程
            logger.warning("检索规划失败，停止补充检索: %s", exc)
            return []
        # 过滤空串与纯空白查询，避免空查询浪费一轮检索
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list):
            return []
        queries = [item.strip() for item in raw_queries if isinstance(item, str) and item.strip()]
        return queries
