"""【legacy-demo】通用工作流 CLI —— 从 JSON 加载工作流、编译并交互运行（含 HITL 审批）。

遗留演示：天气/计算图 + 本地 SQLite checkpoints，不经过生产链路。
生产 IT 服务台入口是 `backend/app.py`；默认加载 `workflows/legacy-demo.json`。

用法：
    uv run python main_workflow.py workflows/legacy-demo.json
    uv run python main_workflow.py --help

与 main_supervisor.py 等价，但图结构来自 JSON 配置而非硬编码代码。
"""

import argparse
import asyncio
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.my_agent.workflow import build_workflow_from_json

load_dotenv()


async def _handle_interrupts(agent, config) -> None:
    """循环处理所有挂起的 interrupt（人工审批）。"""
    while True:
        snapshot = await agent.aget_state(config)
        pending = []
        for task in snapshot.tasks:
            for it in task.interrupts:
                pending.append(it.value)

        if not pending:
            return

        for payload in pending:
            question = payload.get("question", "是否批准？")
            ans = input(f"⏸  {question} (y/n): ").strip().lower()
            approved = ans in ("y", "yes", "是", "1")
            await agent.ainvoke(Command(resume={"approved": approved}), config=config)


async def _run(workflow_path: str, thread_id: str) -> None:
    agent = build_workflow_from_json(workflow_path, checkpointer=SqliteSaver(_sqlite_conn()))
    config = {"configurable": {"thread_id": thread_id}}

    print(f"🚀 工作流: {Path(workflow_path).name} (thread={thread_id})")
    print("💡 输入 exit 退出")
    print("-" * 40)

    while True:
        user_input = input("🧑 你: ")
        if user_input.lower() in ("exit", "quit"):
            break

        await agent.ainvoke({"messages": [HumanMessage(content=user_input)]}, config=config)
        await _handle_interrupts(agent, config)

        snapshot = await agent.aget_state(config)
        msgs = snapshot.values.get("messages", [])
        if msgs:
            content = getattr(msgs[-1], "content", "")
            if content:
                print(f"🤖 {content}")
        print()


def _sqlite_conn():
    return sqlite3.connect("checkpoints.db", check_same_thread=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="从 JSON 工作流编译并运行 LangGraph 图")
    parser.add_argument(
        "workflow", nargs="?", default="workflows/legacy-demo.json", help="工作流 JSON 路径"
    )
    parser.add_argument("--thread", default="workflow_demo_001", help="thread_id")
    args = parser.parse_args()
    asyncio.run(_run(args.workflow, args.thread))


if __name__ == "__main__":
    main()
