"""【legacy-demo】单 Agent 命令行演示入口 —— P1(基础 Agent) + P2(智能摘要) + P5(状态追踪)。

遗留演示：不经过鉴权、审计、限流和预算。生产 IT 服务台入口是 `backend/app.py`。

运行：uv run python main.py
功能：
    - 交互式对话，输入 exit/quit 退出
    - 消息超过 MAX_MESSAGES_BEFORE_SUMMARY 条时，用 LLM 摘要压缩早期历史
    - 每次对话后把最新状态写回，重启后可从断点继续（thread_id 固定为 user_demo_001）
"""

import asyncio
import os
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from src.my_agent.agent import build_agent

# ====== 配置 ======
MAX_MESSAGES_BEFORE_SUMMARY = 15  # 超过 15 条消息触发摘要


def smart_summarize(messages, model):
    """调用 LLM 对早期消息生成摘要，保留关键信息"""
    if len(messages) <= MAX_MESSAGES_BEFORE_SUMMARY:
        return messages

    # 1. 保留最近 5 条消息（确保对话连续性）
    recent = messages[-5:]
    # 2. 将之前的消息转为文本用于摘要
    history_text = "\n".join([
        f"{'用户' if msg.type == 'human' else '助手'}: {msg.content}"
        for msg in messages[:-5]
        if hasattr(msg, 'content') and msg.content
    ])

    # 3. 生成摘要
    summary_prompt = f"""请用中文总结以下对话的核心内容，提取所有关键事实（如名字、地点、数字、偏好），摘要不超过 200 字：

{history_text}"""

    try:
        summary_response = model.invoke([HumanMessage(content=summary_prompt)])
        summary = summary_response.content
        # 4. 用摘要替换早期消息，保留最近 5 条
        return [SystemMessage(content=f"【历史对话摘要】{summary}")] + recent
    except Exception as e:
        print(f"⚠️ 摘要生成失败，使用硬裁剪: {e}")
        return messages[-MAX_MESSAGES_BEFORE_SUMMARY:]


async def main():
    print("🚀 LangGraph Agent (P1 + P2 智能摘要 + P5 追踪)")
    print(f"📌 超过 {MAX_MESSAGES_BEFORE_SUMMARY} 条消息时自动生成摘要")
    print("💡 输入 'exit' 退出，重启自动恢复")
    print("-" * 40)

    agent = build_agent()

    # 获取一个独立的模型实例用于摘要（避免和 Agent 冲突）
    summarizer_model = ChatOpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0,
    )

    thread_id = "user_demo_001"
    config = {"configurable": {"thread_id": thread_id}}
    print(f"📌 当前会话 ID: {thread_id}\n")

    state = {"messages": []}

    while True:
        user_input = input("🧑 你: ")
        if user_input.lower() in ("exit", "quit"):
            print("👋 再见！")
            break

        state["messages"].append(HumanMessage(content=user_input))

        # 🆕 智能摘要（自动压缩上下文）
        state["messages"] = smart_summarize(state["messages"], summarizer_model)

        print("🤖 Agent: ", end="", flush=True)

        final_content = ""
        async for event in agent.astream(state, config=config, stream_mode="updates"):
            for node_name, update in event.items():
                if node_name == "agent":
                    if "messages" in update:
                        msg = update["messages"][0]
                        if hasattr(msg, "content") and msg.content:
                            print(msg.content, end="", flush=True)
                            final_content += msg.content
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            print(" [🔧 调用工具] ", end="", flush=True)
                elif node_name == "tools":
                    print(" [✅ 工具完成] ", end="", flush=True)

        print("\n")

        snapshot = await agent.aget_state(config)
        state = dict(snapshot.values)


if __name__ == "__main__":
    asyncio.run(main())
