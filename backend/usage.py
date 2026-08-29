"""从模型返回值提取 token 用量，并按单价换算成成本。

各家 LLM 的 usage 格式各异（LangChain 会根据供应商包装），本模块做的是
标准化：提供通用的形状，消费方无需关心是用了 langchain_openai/langchain_anthropic
还是 langchain_community 的哪个集成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """模型 API 调用的 token 计数。slots=True 降低内存占用（百万量级 run 时管用）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @property
    def known(self) -> bool:
        """是否成功从模型响应中提取了 usage 数据。"""
        return self.input_tokens > 0 or self.output_tokens > 0 or self.total_tokens > 0


def _int(value: Any) -> int:
    """容错的整数转换。None/空/非数值都转成 0。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extract_model_usage(message: Any) -> ModelUsage:
    """Normalize common LangChain provider usage metadata shapes."""

    candidates: list[Mapping[str, Any]] = []
    for name in ("usage_metadata", "response_metadata"):
        value = getattr(message, name, None)
        if isinstance(value, Mapping):
            candidates.append(value)
            nested = value.get("token_usage") or value.get("usage")
            if isinstance(nested, Mapping):
                candidates.append(nested)
    input_tokens = output_tokens = total_tokens = 0
    for data in candidates:
        input_tokens = max(
            input_tokens, _int(data.get("input_tokens")), _int(data.get("prompt_tokens"))
        )
        output_tokens = max(
            output_tokens, _int(data.get("output_tokens")), _int(data.get("completion_tokens"))
        )
        total_tokens = max(total_tokens, _int(data.get("total_tokens")))
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return ModelUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens
    )


def usage_cost_usd(usage: ModelUsage, *, input_per_1k: float, output_per_1k: float) -> float:
    return round(
        (usage.input_tokens / 1000.0) * input_per_1k
        + (usage.output_tokens / 1000.0) * output_per_1k,
        8,
    )
