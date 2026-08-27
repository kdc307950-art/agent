"""LLM-backed answer generator and retrieval planner with safe fallbacks."""

from __future__ import annotations

import json
import logging
import re
from typing import Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .models import RetrievalHit
from .service import GeneratedAnswer, GeneratedCitation

logger = logging.getLogger(__name__)

_SYSTEM_GENERATE = (
    "你是客服知识库回答助手。仅依据提供的上下文回答问题；"
    "输出严格 JSON：{\"text\": 答案, \"citations\": [{\"document_id\", \"document_version\", \"chunk_id\"}], \"abstained\": bool}。"
    "citations 只能引用提供的上下文；没有把握时 abstained 设为 true。"
)

_SYSTEM_PLAN = (
    "你是客服检索规划器。根据问题和已有检索结果决定是否需要补充检索。"
    "输出严格 JSON：{\"queries\": [字符串列表]}；不需要补充检索时输出 {\"queries\": []}。"
)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("模型未返回 JSON")
    return json.loads(match.group(0))


class LlmAnswerGenerator:
    def __init__(self, *, api_key: str, base_url: str, model: str, temperature: float = 0.1) -> None:
        self._client = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=1200,
        )

    async def generate(
        self,
        question: str,
        contexts: Sequence[RetrievalHit],
    ) -> GeneratedAnswer:
        context_text = "\n---\n".join(
            f"[{hit.document_id}/{hit.document_version}/{hit.chunk_id}] {hit.content}" for hit in contexts
        )
        try:
            response = await self._client.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_GENERATE),
                    HumanMessage(content=f"问题：{question}\n\n上下文：\n{context_text}"),
                ]
            )
            payload = _extract_json(str(response.content))
        except Exception as exc:
            logger.warning("答案生成失败，转为 abstained: %s", exc)
            return GeneratedAnswer(text="", citations=(), abstained=True)
        citations = []
        for item in payload.get("citations") or []:
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
        text = str(payload.get("text") or "").strip()
        abstained = bool(payload.get("abstained")) or not text
        return GeneratedAnswer(text=text, citations=tuple(citations), abstained=abstained)


class LlmRetrievalPlanner:
    def __init__(self, *, api_key: str, base_url: str, model: str, temperature: float = 0.0) -> None:
        self._client = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=400,
        )

    async def next_queries(
        self,
        question: str,
        hits: Sequence[RetrievalHit],
        round_number: int,
    ) -> Sequence[str]:
        try:
            response = await self._client.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PLAN),
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
        except Exception as exc:
            logger.warning("检索规划失败，停止补充检索: %s", exc)
            return []
        queries = [str(item).strip() for item in payload.get("queries") or [] if str(item).strip()]
        return queries
