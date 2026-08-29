"""【legacy-demo】Supervisor 多 Agent 命令行演示入口 —— 含 Human-in-the-loop 人工审批。

遗留演示：天气/计算路由 + 进程内阻塞审批，不经过鉴权、审计、限流。
生产 IT 服务台入口是 `backend/app.py`；JSON 等价图见 `workflows/legacy-demo.json`。

运行：uv run python main_supervisor.py
功能：
    - 交互式对话，Supervisor 决定路由到天气 Agent 还是计算 Agent
    - 每次路由前暂停等待人工审批（y/n），用 Command(resume=...) 恢复
    - thread_id 固定为 supervisor_demo_001
"""

# ruff: noqa: E402  # legacy 脚本：先修正 sys.path 才能导入项目包
import asyncio
import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend.config import load_environment
from src.my_agent.supervisor_agent import build_supervisor_agent

load_environment()

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出，避免中文/emoji 报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


async def _handle_interrupts(agent, config):
    """循环处理所有挂起的 interrupt（人工审批）。

    审批节点用 interrupt() 暂停后，这里读取 interrupt 的 payload，
    询问用户，再用 Command(resume={"approved": ...}) 恢复执行。
    """
    while True:
        snapshot = await agent.aget_state(config)
        pending = []
        for task in snapshot.tasks:
            for it in task.interrupts:
                pending.append(it.value)

        if not pending:
            return  # 没有挂起的审批，本轮结束

        for payload in pending:
            question = payload.get("question", "是否批准？")
            ans = input(f"[审批] {question} (y/n): ").strip().lower()
            approved = ans in ("y", "yes", "是", "1")
            await agent.ainvoke(Command(resume={"approved": approved}), config=config)


async def main():
    agent = build_supervisor_agent()
    config = {"configurable": {"thread_id": "supervisor_demo_001"}}

    print("Supervisor 多 Agent（含 Human-in-the-loop 人工审批）")
    print("输入 exit 退出")
    print("-" * 40)

    while True:
        user_input = input("你: ")
        if user_input.lower() in ("exit", "quit"):
            break

        # 送入用户消息，图会跑到审批节点（interrupt 暂停）或直接结束
        await agent.ainvoke({"messages": [HumanMessage(content=user_input)]}, config=config)

        # 处理人工审批中断
        await _handle_interrupts(agent, config)

        # 打印最终回答
        snapshot = await agent.aget_state(config)
        msgs = snapshot.values.get("messages", [])
        if msgs:
            last = msgs[-1]
            content = getattr(last, "content", "")
            if content:
                print(f"Agent: {content}")
        print()

    print("再见！")


if __name__ == "__main__":
    asyncio.run(main())
