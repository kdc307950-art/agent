# Resolution Copilot 架构（双 Agent 之二）

## 定位

Resolution Copilot 是解决阶段的客服分析 Agent：检索知识、资产、历史工单，
生成带结构化引用的客服处理草稿。它**只生成草稿**，不发送消息、不改工单状态，
所有副作用操作由确定性服务和客服确认控制。

## 双 Agent 协作方式

两个 Agent 不通过自然语言互相传递指令，而通过结构化数据协作：

```text
Agent 1（Intake/Triage）→ 工单记录 + ticket_workflow_runs（账本）
        ↓ 状态机落库
tickets.status = assigned / in_progress
        ↓ POST /tickets/{id}/copilot（operation_id + expected_version）
Agent 2（Resolution Copilot）→ CopilotRequest（后端只读组装）
        ↓ 治理工具循环 + 两层引用门禁
CopilotResult → copilot_drafts（草稿 + 引用 + 置信度）
        ↓ 客服查看、修改、审批
既有消息 Outbox 发送
```

结构化 Handoff 契约：
- `IntakeHandoff`（backend/tickets/models.py）：Agent 1 受理完成后的字段快照
- `CopilotRequest` / `CopilotResult`（backend/copilot/models.py）：全部
  `extra="forbid"`，禁止模型自由返回任意字段
- 租户/部门/角色身份从服务端 `RunContext` 注入，不读取模型请求体

## 异步 Worker 化（阶段二）

模型执行不在 HTTP 请求内，由独立 `CopilotWorker` 进程异步完成：

```text
POST /tickets/{id}/copilot
  ├─ 校验 ticket:agent scope / 工单状态 / expected_version / operation_id
  └─ 创建 copilot_runs(status=queued)，立即返回 202 + run_id

CopilotWorker（常驻进程）
  ├─ FOR UPDATE SKIP LOCKED 领取 queued/failed 运行
  ├─ 写入 worker_id + 租约（lease_expires_at）
  ├─ 上下文准备 → 工具循环（治理）→ 两层引用门禁 → 保存草稿
  ├─ 成功 → completed；瞬时错误 → 指数退避重试；超限 → dead
  └─ 定期 recover 超租约 processing 僵尸运行（崩溃恢复）

GET /tickets/{id}/copilot/{run_id}  ← 前端轮询状态
```

运行状态机：`queued → processing → completed | failed → (重试) | dead | expired`

关键保证：
- Web 进程不执行模型调用（POST 在 500ms 内返回）
- 同一 operation_id 不重复消耗模型（幂等）
- Worker 崩溃后任务随租约过期被 recover 回队（任务可恢复）
- 失败不修改工单、不停止 SLA、不创建客户消息、不产生 Outbox

## 工具治理（阶段一/四）

Agent 2 只绑定只读工具，所有调用统一经过 `ToolGovernance`：

```text
search_knowledge / search_assets / get_ticket_history / get_ticket_messages
```

治理链：工具名称校验 → 租户 allowlist → scope 校验 → 输入长度 → 超时 →
执行 → 结构化结果（content + evidence）→ 审计 + 指标。
模型伪造 `send_message` 等副作用工具会被治理层拒绝（结构化 error_code：
denied_scope / denied_tenant / denied_unregistered / denied_input / timeout）。

## 两层引用门禁（阶段二）

1. **第一层**：模型输出的引用必须存在于本轮实际工具证据
   （`search_knowledge` 返回的 `{content, evidence}` 中的三元组）。
2. **第二层**：权威数据校验（`verify_citations`）：租户 / published / 有效期 /
   部门 ACL / chunk 存在 / 版本一致，在草稿保存前再次确认。

业务门禁（不得自动发送）：无引用、引用校验失败、confidence < 0.80、
finance 类别、模型/工具异常。`auto_reply` 恒为 False。

## 统一知识检索（阶段三，Copilot 已接线）

Copilot `search_knowledge` 经 `runtime.knowledge_retriever.search()` 执行：

```text
未配置 embedding → lexical-only（jieba + PostgreSQL 全文 + pg_trgm）
配置 embedding   → hybrid（lexical + vector + RRF 融合 + 分类权重）
向量请求失败     → lexical-only + degraded=true（禁止标成 hybrid）
```

`retrieval_mode` 显式标记并进入 tool_trace / copilot_runs / copilot_drafts /
指标 / 最终结果；两套评测结果分开记录。文档嵌入走批量 texts 请求
（单批 ≤32 条，复用 HTTP Client，任一向量异常直接失败）。

## 部门身份透传（阶段一，已完成）

`RunContext` 增加 role / departments / internal；`retrieval_principal(context)`
统一构造检索主体。**发起人身份快照**：POST 入队时从认证主体（服务端查询
坐席部门，`support_members JOIN support_teams`）持久化到 `copilot_runs`
（requester_user_id / role / departments / internal），Worker 从运行记录
恢复快照构造 RunContext——任务执行期间权限变化不影响本任务；
身份缺失闭锁（不默认全权限）、查询失败闭锁（503）。

## 关键文件

```text
backend/copilot/models.py      结构化输入输出契约
backend/copilot/agent.py       有界工具循环（3 轮/6 调用/12 上下文/超时）
backend/copilot/tool_adapter.py 治理适配器（governed_invoke + error_code）
backend/copilot/service.py     编排 + 两层引用门禁
backend/copilot/worker.py      异步执行 Worker（租约/退避/dead/恢复）
backend/copilot/repository.py  copilot_runs / copilot_drafts 持久化
backend/copilot/api.py         POST(202)/GET 状态/审批端点
backend/knowledge/retriever.py 统一检索入口（lexical/hybrid）
backend/knowledge/identity.py  统一检索主体构造
frontend/src/components/CopilotPanel.tsx  前端面板（轮询/状态/取消）
```

## 诚实边界

不宣称：Copilot 已完全 hybrid 化、已完成真正的多部门身份透传、
已验证高并发与长期生产稳定性、已上线生产。
