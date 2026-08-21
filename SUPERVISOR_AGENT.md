# Supervisor 多 Agent + Human-in-the-loop 使用说明书

## 一、这是什么

在原有单 Agent 基础上，新增的一套 **Supervisor（主管）多智能体协作** 架构，并内置
**Human-in-the-loop（人工审批）** 能力。用于演示和练习 LangGraph 的多 Agent 编排、状态路由、
以及高风险操作人工确认。

> 与原有 `agent.py` 的 `build_agent()` **互不影响**，可并行使用。

---

## 二、架构图

```
                     ┌─────────────┐
       用户输入 ────▶ │ supervisor  │  主管：判断问题类型，决定路由
                     └──────┬──────┘
                            │ next = weather / calc / finish
                     ┌──────▼──────┐
                     │  approval   │  人工审批节点：interrupt() 暂停
                     └──┬───────┬──┘
            approved=True │       │ approved=False
                          │       └──────▶ END（拒绝，终止）
              ┌───────────┴───────────┐
              ▼                       ▼
      ┌───────────────┐       ┌───────────────┐
      │ weather_agent │       │  calc_agent   │   两个专业子 Agent
      └───────┬───────┘       └───────┬───────┘
              │                       │
              └───────────┬───────────┘
                          ▼
                   回到 supervisor（多轮路由，直到 finish）
```

**路由规则**：
- `weather`：用户想查天气 → 交给天气子 Agent
- `calc`：用户想算数学 → 交给计算子 Agent
- `finish`：问题已解决 → 结束

---

## 三、文件说明

| 文件 | 作用 |
|------|------|
| `src/my_agent/supervisor_agent.py` | 核心：`build_supervisor_agent()` 构建图 |
| `main_supervisor.py` | 命令行入口，演示审批交互 |
| `tests/test_supervisor_agent.py` | 图结构测试（2 个，不调 LLM） |

---

## 四、快速开始

**环境要求**：项目已有 `.venv`（Python 3.12），`.env` 里已配置 `DEEPSEEK_API_KEY`。

在 Windows 终端（项目根目录）：

```powershell
.venv\Scripts\python.exe main_supervisor.py
```

运行后交互示例：

```
你: 北京天气怎么样？
[审批] 是否批准将问题交给 weather_agent 处理？ (y/n): y
Agent: 为您查询到北京当前的天气情况如下...
```

输入 `exit` 退出。

---

## 五、Human-in-the-loop 原理（重点）

审批节点 `approval` 的核心是 `interrupt()`：

```python
def _approval_node(state):
    # 1. 暂停图执行，把 {"question": ...} 抛给调用方
    decision = interrupt({"question": f"是否批准将问题交给 {target} 处理？"})
    # 2. 调用方用 Command(resume={"approved": True/False}) 恢复
    #    resume 的值就是这里 decision 收到的值
    if not decision.get("approved", False):
        return {"next": "finish", "messages": [AIMessage("已拒绝")]}  # 拒绝 → 终止
    return {}  # 批准 → 继续路由
```

**完整流程**：
1. 图跑到 `approval` 节点，`interrupt()` 触发，图暂停（state 进入 pending）
2. 调用方通过 `agent.aget_state(config)` 检测到 `snapshot.tasks` 里有 interrupt
3. 调用方用 `Command(resume={"approved": ...})` 恢复，`resume` 的值传回给 `decision`
4. 批准则继续路由到子 Agent，拒绝则终止

**与 `interrupt_before` 的区别**：
- `interrupt_before=["tools"]`：只能「暂停后继续」，不能拒绝
- `interrupt()` 函数：能真正「批准 / 拒绝」，返回值可分支（本项目用这种）

---

## 六、关键代码位置

| 功能 | 位置 |
|------|------|
| 共享状态定义 | `SupervisorState`（messages + next） |
| 主管路由判断 | `_make_supervisor_node()` |
| 人工审批节点 | `_approval_node()` |
| 子 Agent 创建 | `_make_weather_agent()` / `_make_calc_agent()` |
| 审批 resume 处理 | `main_supervisor.py` 的 `_handle_interrupts()` |

---

## 七、持久化说明（可选）

当前默认用 `MemorySaver`（内存 checkpointer），重启后对话历史丢失，但 **interrupt 恢复在单次
会话内正常工作**。

如需**长任务断点续跑**（持久化到 SQLite），调用方在 async 上下文创建异步 checkpointer 传入：

```python
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async def main():
    async with aiosqlite.connect("checkpoints.db") as conn:
        checkpointer = AsyncSqliteSaver(conn)
        agent = build_supervisor_agent(checkpointer=checkpointer)
        # ... 后续逻辑
```

> 注意：`build_supervisor_agent()` 是同步函数，不能自己 `await aiosqlite.connect()`，
> 所以持久化 checkpointer 由调用方传入。

---

## 八、已修复的 Bug（2026-08-21）

| Bug | 现象 | 修复 |
|-----|------|------|
| SqliteSaver 不支持异步 | `NotImplementedError: SqliteSaver does not support async methods` | 默认改用 `MemorySaver`，持久化改用 `AsyncSqliteSaver`（由调用方传入） |
| GBK 编码崩溃 | Windows 控制台 print 中文/emoji 报 `UnicodeEncodeError` | `main_supervisor.py` 开头 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` + 去除装饰性 emoji |

> 注：原有 `agent.py` 也存在「SqliteSaver + astream」的同类异步 bug（codex 生成后未实测），
> 本次未改动，如需修复请告知。

---

## 九、简历可写内容

升级后，Agent 项目经历可写：

- 基于 LangGraph 构建 Supervisor 多智能体调度体系，实现任务路由、状态管理、工具调用
- 引入 Human-in-the-loop 人工审批（interrupt 机制），高风险操作先人工确认，支持批准/拒绝
- 基于 Checkpoint 机制支持长任务断点续跑与状态恢复
