"""src.my_agent —— Agent 核心包。

模块划分：
    agent.py             单 Agent 图（模型 + 工具 + 重试）
    state.py             LangGraph 状态定义
    tools.py             工具集（天气 / 计算器）
    supervisor_agent.py  Supervisor 多 Agent 编排（HITL 审批）
    workflow/            JSON 工作流编排层（schema / nodes / compiler）
"""

# 使 src/my_agent 成为一个包
