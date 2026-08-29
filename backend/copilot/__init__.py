"""Resolution Copilot 包：客服解决阶段的分析与拟答 Agent。

子模块分工：
    - models.py ：结构化输入输出契约（CopilotRequest / CopilotResult / 草稿与运行记录）
    - agent.py  ：有界只读工具循环执行器（轮次/超时/上下文限制）
    - service.py：上下文组装 + Agent 编排 + 独立答案门禁
    - repository.py：copilot_runs / copilot_drafts 持久化（阶段 3）

安全边界：
    - Agent 2 只绑定只读工具（RESOLUTION_COPILOT_TOOLS），无副作用工具
    - 门禁独立于模型自述：无引用/敏感类别/低置信度一律转人工
    - auto_reply 恒为 False，只生成草稿
"""

from .agent import (
    MAX_CONTEXT_ITEMS,
    MAX_ROUNDS,
    MAX_TOOL_CALLS,
    CopilotLimits,
    ResolutionCopilot,
)
from .models import (
    CopilotCitation,
    CopilotDraft,
    CopilotRequest,
    CopilotResult,
    CopilotRunRecord,
)
from .service import MIN_CONFIDENCE, SENSITIVE_CATEGORIES, CopilotService

__all__ = [
    "CopilotCitation",
    "CopilotDraft",
    "CopilotLimits",
    "CopilotRequest",
    "CopilotResult",
    "CopilotRunRecord",
    "CopilotService",
    "MAX_CONTEXT_ITEMS",
    "MAX_ROUNDS",
    "MAX_TOOL_CALLS",
    "MIN_CONFIDENCE",
    "ResolutionCopilot",
    "SENSITIVE_CATEGORIES",
]
