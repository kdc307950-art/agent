# LangGraph Agent 项目地图（PROJECT_MAP）

> 用途：新人上手 / 隔段时间回顾，看这一个文件就能建立全局认识。
> 更新时间：随代码结构同步更新。产品定位以根目录 `README.md` 为准。

## 项目定位

一个从「单 Agent Demo」一路升级到「多租户生产化 API」的 Agent 工程。
产品为**多租户 IT 服务台工单系统**：员工经 Web / 企业微信 / 钉钉提交工单，系统自动分类、
按租户策略追问、加载 SLA、规则派单，Agent 检索知识库生成带引用的建议，客服在工作台完成
接单/处理/回访/关闭。

核心分三层：

- **Agent 本身（src/my_agent）**：受理图（Agent 1，生产路径）+ 早期 Demo 图（legacy）。
- **生产化包装（backend）**：FastAPI + 多租户隔离 + 审计/预算/限流 + 各类 Worker。
- **前端（frontend）**：React 客服工作台。

受理相关图形态由 `AGENT_GRAPH_MODE` 切换（`single` / `workflow`）；**生产工单链路不依赖它**，
由 `src/my_agent/helpdesk/graph.py` 定义的受理图承载。

## 目录结构

```
src/my_agent/                ★ Agent 核心包
├── agent.py                 单 Agent 图（legacy，向后兼容；SG 循环）
├── state.py                 状态定义（messages 用 add_messages 自动追加）
├── tools.py                 get_weather / calculate 等 Demo 工具
├── supervisor_agent.py      Supervisor 多 Agent + interrupt 审批（legacy）
├── workflow/                JSON 工作流编排层（legacy）
│   ├── schema.py            WorkflowSpec（Pydantic 定义 + 校验）
│   ├── nodes.py             节点工厂（supervisor/agent/tool/condition/human_approval）
│   └── compiler.py          把 JSON spec 编译成 LangGraph 图
└── helpdesk/                ★ ★ 生产 IT 服务台受理图（Agent 1）
    ├── domain.py            工单领域规则：状态机/动作权限/恢复命令校验
    ├── intake.py            确定性受理规则：分类/必填字段/派单决策
    ├── graph.py             受理 LangGraph：归一化→分类→策略→完整性→澄清/派单→拟答
    └── tools.py             受理/派单相关的工具封装

backend/                     生产化 API 层
├── app.py                   ★ FastAPI 网关 + SSE；挂载 tickets/admin/copilot/knowledge/assets 路由
├── ticket_api.py            工单 API（建单/受理/流转/回访/overview/pending-interrupt）
├── settings.py              环境变量集中读取 + 校验（生产强约束）
├── security.py              鉴权（dev token / OIDC）、限流、CORS
├── runtime.py               ★ 运行时装配：Postgres checkpointer/store + Redis + 治理 + 各仓储
├── schema.py / migrations.py  Postgres 建表与版本管理
├── channel_adapters.py      渠道 Webhook 适配器（企微/钉钉 验签解密）
├── channel_processor.py     渠道入站事件异步处理核心（建单/受理，HTTP 路由不执行）
├── inbound_worker.py        Inbound 入站事件常驻 Worker
├── outbox_worker.py         Outbox 消息投递 Worker
├── sla_worker.py / workflow_recovery.py   SLA 违约扫描 / 工作流恢复 Worker
├── run_copilot_worker.py    Resolution Copilot 异步 Worker 入口
├── ticket_intake.py         受理图与 API 的桥接（apply_intake_resume 等）
├── tool_governance.py        ★ 工具治理：白名单 → 调用 → 审计 → 重试
├── audit.py / budget.py / rate_limit.py / usage.py
├── metrics.py / telemetry.py / worker_metrics.py  可观测性
├── repositories.py / revocation.py / readiness.py
├── retention.py / data_retention.py / backup_restore.py / migrate_sqlite.py
├── seed_demo.py / issue_dev_token.py / vector_migrations.py
├── tickets/                 ★ 工单域（领域状态机之外的数据访问）
│   ├── repository.py         工单核心仓储（乐观锁流转/渠道事件/追问登记）
│   ├── operations.py         运营数据：Outbox、SLA 实例、回访、概览聚合
│   ├── policies.py           租户 IT 策略（每 (tenant, category) 一条）
│   ├── routing.py            派单路由 + 「最空闲 + 在岗」坐席选择
│   ├── sla.py                业务日历与 SLA 时限计算
│   └── models.py             Pydantic 模型
├── knowledge/               ★ Agentic RAG
│   ├── agentic.py / service.py / retriever.py / repository.py / pgvector.py
│   ├── ingestion.py / api.py / tokenizer.py / models.py / identity.py / llm.py
│   └── eval_cases.py / eval_holdout_cases.py（脱敏评测集）
├── assets/                  资产域（api / models / repository）
└── copilot/                 ★ Resolution Copilot（解决阶段只读 Agent）
    ├── agent.py              有界工具循环（最大轮次/工具数/超时硬限制）
    ├── service.py            上下文组装 → 生成 → 答案门禁
    ├── api.py / models.py / repository.py / tools.py / tool_adapter.py / worker.py

frontend/src/               React 客服工作台
├── api/                    API 封装（client/tickets/assets/knowledge/admin/chat/copilot）
├── views/                  各页面（QueueView / TicketDetail / AssistantView / KnowledgeView /
│                           AssetsView / ItPoliciesView）
├── components/             组件（Sidebar / CreateTicketDialog / CopilotPanel / ApprovalCard ...）
└── types.ts                类型定义

legacy-demo/                早期通用聊天/天气/计算 Demo（统一标记 legacy-demo）
workflows/                  legacy JSON 工作流定义（如 legacy-demo.json）
tests/                      测试（单元 + 集成 + e2e）
infra/                      Docker Compose（demo/dev/test）、k8s、gateway、可观测性栈
```

## 建议熟悉顺序

1. `README.md` —— 产品定位与跑通全链路。
2. `backend/app.py` → `backend/runtime.py` → `backend/settings.py` —— 服务如何装配。
3. `src/my_agent/helpdesk/graph.py` —— 受理图（Agent 1）的核心节点流。
4. `src/my_agent/helpdesk/domain.py` → `intake.py` —— 状态机与确定性规则。
5. `backend/tickets/repository.py` → `operations.py` —— 工单域数据访问与运营数据。
6. `backend/tickets/policies.py` → `routing.py` → `sla.py` —— 策略/派单/SLA。
7. `backend/channel_processor.py` → `inbound_worker.py` → `outbox_worker.py` —— 渠道闭环。
8. `backend/knowledge/` —— Agentic RAG。
9. `backend/copilot/` —— Resolution Copilot（解决阶段 Agent）。
10. `frontend/src/views/` —— 工作台各页面。

## 运行命令

```bash
# 依赖栈（Postgres/Redis/OTel）并初始化 schema
docker compose -f infra/compose.dev.yml up -d
uv run python -m backend.migrations

# 后端（Windows 加 --loop；Linux 不需要）
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --loop backend.uvicorn_loop:selector_event_loop_factory

# 前端
cd frontend && npm run dev

# 演示数据与开发令牌
uv run python -m backend.seed_demo
uv run python -m backend.issue_dev_token demo agent-1 --role helpdesk-agent

# 各类常驻 Worker
uv run python -m backend.run_outbox_worker
uv run python -m backend.run_inbound_worker
uv run python -m backend.run_sla_worker
uv run python -m backend.run_copilot_worker

# legacy 试跑（仅供试跑图结构，非产品入口）
uv run python legacy-demo/main.py
uv run python legacy-demo/main_workflow.py legacy-demo/workflows/legacy-demo.json
```

## 关键设计决策

- **Human-in-the-loop**：`interrupt()` 暂停图执行 → 调用方 `Command(resume=...)` 恢复；
  必须有 checkpointer 才能恢复。生产走 PostgreSQL Checkpoint（跨请求）。
- **工具治理**：`ToolGovernance.awrap_tool_call` 通过 `BuildContext.tool_call_wrapper`
  注入每个 ToolNode，编排图/子 Agent（含 Copilot）的所有工具入口都过治理。
- **租户隔离**：`tenant_thread_id()` 给业务 thread_id 加租户前缀；Repository 层所有查询
  强制带 `tenant_id` 条件，防跨租户串会话。
- **限流/预算放 Redis 而非进程内**：多副本部署时进程内各自算账会失效（额度/预算翻倍），
  Lua 脚本保证原子性。
- **生产强约束**：`APP_ENV=production` 强制 OIDC 鉴权、Redis 限流/撤销、关闭 auto_setup、显式 CORS。
- **Outbox 事务性发件箱**：业务变更与出站事件同事务提交；`FOR UPDATE SKIP LOCKED` + 租约
  支持多副本，失败指数退避、超限进 dead 可重放。
- **独立答案门禁**（知识回答与 Copilot 草稿）：引用必须落在工具返回的检索证据里，
  敏感类别强转人工，模型自述的置信度/引用不可信。
- **Windows 兼容**：psycopg 异步连接要求 Selector 事件循环（`uvicorn_loop.py` +
  `backend/__init__.py` 兜底）。
- **Agent / Worker / 确定性流程边界**：生产不是多智能体编排——两个独立专用 Agent
  （受理图 + Resolution Copilot）、大量异步 Worker、受理/派单为确定性流程。详细边界表见
  `README.md`「Agent / Worker / 确定性流程」边界。
