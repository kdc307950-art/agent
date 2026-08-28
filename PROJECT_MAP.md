# LangGraph Agent 项目地图（PROJECT_MAP）

> 用途：新人上手 / 隔段时间回顾，看这一个文件就能建立全局认识。
> 生成时间：2026-08-23，基于当时代码结构。

## 项目定位

一个从「单 Agent Demo」一路升级到「多租户生产化 API」的 Agent 工程。
核心是 **`src/my_agent`（Agent 本身）**，外围是 **`backend`（生产化包装）**。

三种图形态（由 `AGENT_GRAPH_MODE` 切换）：
- `single`：单 Agent（默认，向后兼容）
- `workflow`：JSON 工作流编排图（配置驱动，最进阶）

## 目录结构

```
main.py              单 Agent 交互入口（含智能摘要，最简 Demo）
main_supervisor.py   Supervisor 多 Agent 交互入口（HITL 审批 Demo）
main_workflow.py     JSON 工作流交互入口（同一能力、配置驱动版）
workflows/           JSON 工作流定义文件（如 legacy-demo.json）

src/my_agent/        ★ 核心包
├── agent.py         单 Agent 图：model + tools → StateGraph（agent⇄tools 循环）
├── state.py         状态定义（messages 用 add_messages 自动追加）
├── tools.py         工具集：get_weather（Open-Meteo）/ calculate（AST 安全计算）
├── supervisor_agent.py  Supervisor 编排：LLM 路由 → interrupt 人工审批 → 子 Agent
└── workflow/        JSON 工作流编排层（画布式配置驱动，最进阶）
    ├── schema.py      WorkflowSpec：state/nodes/edges 的 Pydantic 定义+校验
    ├── nodes.py       节点工厂：supervisor/agent/tool/condition/human_approval/rag(占位)
    └── compiler.py    把 JSON spec 编译成 LangGraph 图

backend/             生产化 API 层（22 模块，大部分是基础设施）
├── app.py           ★ FastAPI 网关：/api/chat/stream、resume、鉴权、审计、健康检查
├── settings.py      环境变量集中读取+校验（生产环境强约束）
├── runtime.py       运行时装配：Postgres checkpointer + Redis + 治理
├── security.py      鉴权（dev token / OIDC）、限流、CORS
├── rate_limit.py    限流两种实现（内存滑动窗口 / Redis 令牌桶）
├── tool_governance.py  ★ 工具治理：租户白名单 → 调用 → 审计 → 重试
├── audit.py         审计落库（脱敏）
├── budget.py        租户日预算（Redis Lua 原子记账）
├── usage.py         token 用量提取 + 成本换算
├── metrics.py       运行时指标 / telemetry.py OpenTelemetry 追踪
├── repositories.py  租户隔离 thread_id + 长期记忆
├── revocation.py    OIDC token 吊销（Redis）
├── readiness.py     就绪检查（/livez /readyz）
├── migrations.py / schema.py  Postgres 建表与版本管理
├── retention.py / data_retention.py  数据保留清理
├── backup_restore.py 备份/恢复工具
├── migrate_sqlite.py SQLite→Postgres 迁移
├── workflow_loader.py 加载 JSON 工作流配置
├── issue_dev_token.py 生成开发 token 工具
└── uvicorn_loop.py  Windows 下 Selector 事件循环

tests/               测试（单元 + 集成 + e2e）
```

## 建议熟悉顺序

1. `main.py` —— 看 Agent 怎么跑起来
2. `src/my_agent/agent.py` —— 单 Agent 图的核心
3. `src/my_agent/tools.py` —— 工具如何定义/绑定
4. `src/my_agent/supervisor_agent.py` —— 多 Agent + HITL 审批
5. `main_supervisor.py` —— Supervisor 交互流程
6. `src/my_agent/workflow/` 三件套 —— 配置驱动编排
7. `backend/` —— 按需深入（先 app.py → settings.py → runtime.py）

## 运行命令

```bash
# 单 Agent（需要 DEEPSEEK_API_KEY）
uv run python main.py

# Supervisor 多 Agent（HITL 审批）
uv run python main_supervisor.py

# JSON 工作流
uv run python main_workflow.py workflows/legacy-demo.json

# API 服务（生产化形态，需要 Postgres/Redis/.env 齐全）
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1
```

## 关键设计决策

- **Human-in-the-loop**：`interrupt()` 暂停图执行 → 调用方 `Command(resume={"approved": ...})` 恢复；必须有 checkpointer 才能恢复
- **工具治理**：`ToolGovernance.awrap_tool_call` 通过 `BuildContext.tool_call_wrapper` 注入每个 ToolNode，编排图/子 Agent 的所有工具入口都过治理
- **租户隔离**：`tenant_thread_id()` 给业务 thread_id 加租户前缀，防跨租户串会话
- **限流/预算放 Redis 而非进程内**：多副本部署时进程内各自算账会失效（限流额度翻倍、预算翻倍），Lua 脚本保证原子性
- **生产强约束**：`APP_ENV=production` 强制 OIDC 鉴权、Redis 限流/撤销、关闭 auto_setup、显式 CORS
- **Windows 兼容**：psycopg 异步连接要求 Selector 事件循环（`uvicorn_loop.py` + `backend/__init__.py` 兜底）
