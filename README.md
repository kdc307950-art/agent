# LangGraph Agent

[![CI](https://github.com/kdc307950-art/agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kdc307950-art/agent/actions/workflows/ci.yml)

一个跑在生产形态基础设施上的 LangGraph Agent 服务：**JSON 定义的多 Agent 编排 + 跨请求人工审批**，运行在多租户隔离、审计留痕、租户预算、限流和工具治理之上。

不是 demo 脚本——审批可以隔几小时由另一个人在另一台设备上完成，状态落在 PostgreSQL checkpoint 里，不依赖任何进程内存。

## 5 分钟跑起来

需要 Docker。**不需要**本机装 Python、Postgres 或 Redis。

```bash
docker compose -f infra/compose.demo.yml up --build
```

起 4 个容器（postgres / redis / migrate / agent），只暴露 `127.0.0.1:8000`。就绪后：

```bash
curl http://127.0.0.1:8000/readyz
```

预期 `{"status": "ready", "checks": {"agent": "ok", "postgres": "ok", "redis": "ok"}}`。
接口文档在 http://127.0.0.1:8000/docs ，指标在 `/metrics`。

没填 `DEEPSEEK_API_KEY` 也能起来——服务健康、接口可看，只是真发消息会失败。想真对话就在 shell 里 `export DEEPSEEK_API_KEY=sk-xxx` 再 `up`。想看 supervisor 路由和人工审批，把 `infra/compose.demo.yml` 里 `AGENT_GRAPH_MODE` 那两行的注释去掉。

用完 `docker compose -f infra/compose.demo.yml down -v` 清干净。这份 compose 里的密码是写死的弱口令且不暴露数据库端口，**仅供本地演示，不要用于任何联网环境**。

## 架构

```mermaid
flowchart LR
    U[React 前端] -->|SSE| API[FastAPI]
    API --> AUTH[认证<br/>OIDC / 租户令牌<br/>scope 校验]
    AUTH --> RL[限流<br/>Redis 共享计数]
    RL --> BUD[租户日预算]
    BUD --> MODE{AGENT_GRAPH_MODE}
    MODE -->|single| SA[单 Agent]
    MODE -->|workflow| WF[JSON 编排图<br/>supervisor 路由<br/>human_approval]
    SA --> TG[工具治理<br/>租户白名单 / scope<br/>超时 / 重试 / 审计]
    WF --> TG
    TG --> LLM[DeepSeek]
    SA -. checkpoint / store .-> PG[(PostgreSQL)]
    WF -. checkpoint / store .-> PG
    API -. 运行与事件审计 .-> PG
    API -. 指标 / 链路 .-> OTEL[OTel Collector<br/>→ Prometheus / Jaeger]
```

两种图形态共用同一套 checkpointer、store 和工具治理钩子，因此多租户隔离、审计、预算、限流对上层完全一致，差异只在图结构本身。

### 人工审批为什么是两次请求

SSE 是服务端单向推送，而审批本质是双向的。一次审批被拆成两次 HTTP 请求、两条 SSE 流、两个 `run_id`，绑在同一个 thread 上：

```mermaid
sequenceDiagram
    participant F as 前端
    participant B as 后端
    participant P as PostgreSQL
    F->>B: POST /chat/stream
    B-->>F: data: {"type":"text", ...}
    B->>P: checkpoint 落盘 + 审计 interrupt_raised
    B-->>F: data: {"type":"interrupt", "interrupt_id": "..."}
    Note over B,F: 流在此结束，刻意不发 end<br/>本轮审计状态 awaiting_approval
    Note over F: 输入框锁定，展示审批卡片
    F->>B: POST /chat/resume {approved, interrupt_id}
    B->>P: 读状态校验挂起 → Command(resume=...)
    B-->>F: data: {"type":"end", "run_id": "..."}
```

前端靠 **`end` 事件的缺席**区分「答完了」和「等你批」，状态机因此少一个变量。挂起的那一轮审计状态是 `awaiting_approval` 而不是 `completed`——一次挂起的运行没有完成，记成完成会让运行成功率指标失真。

## 编排模式与人工审批

后端支持两种图形态，由 `AGENT_GRAPH_MODE` 切换，**默认 `single`**（单 Agent，与历史行为一致）：

```dotenv
AGENT_GRAPH_MODE=workflow
AGENT_WORKFLOW_PATH=workflows/helpdesk_supervisor.json
```

`workflow` 模式由 JSON 定义编译出图，支持 supervisor 路由、子 Agent 和 `human_approval` 审批节点。配置缺失或 spec 不合法时服务在启动阶段直接失败，不会带病启动。

图停在审批节点时，`POST /chat/stream` 的 SSE 流下发一条 interrupt 事件并结束，**不发 `end`**：

```json
{"type": "interrupt", "run_id": "...", "thread_id": "...", "interrupt_id": "...", "question": "是否批准将问题交给 weather 处理？"}
```

审批人调用 `POST /chat/resume` 恢复执行，请求体为 `{"thread_id": "...", "approved": true, "interrupt_id": "...", "resumed_from": "..."}`。该端点要求 `chat:approve` scope（与 `chat:write` 分离——能发消息不等于能替租户批准操作），并同样受限流和租户预算约束，因为恢复执行会真实触发模型调用。服务端先读取会话状态确认存在挂起审批，`interrupt_id` 不匹配或没有挂起审批时返回 409（不是 404，避免通过状态码探测他人 `thread_id` 是否存在），以此防重复审批和跨会话串批。恢复轮开新的 `run_id`，审计 metadata 记录 `resumed_from`、`approved`、`interrupt_id` 和 `approver_user_id`。

回传给前端的始终是逻辑 `thread_id`，不是内部的 `tenant:user:thread` 物理命名空间。

## 安全配置

`.env` 只用于本地环境，禁止提交。工作区 `.env` 中的真实 DeepSeek 和 LangSmith 密钥应在对应平台撤销并重新生成。

复制 `.env.example` 为 `.env`，填写：

- `DEEPSEEK_API_KEY`：后端调用模型所需。
- `TENANT_TOKEN_SECRET`：签发和验证租户令牌的服务端密钥；生产建议替换为 OIDC/JWT 验证。
- `LANGCHAIN_API_KEY`：可选，仅用于 LangSmith 追踪。

启动后，`POST /chat/stream` 必须带 Bearer 令牌。开发环境使用 `AUTH_MODE=dev` 的内部签名令牌；公网环境必须切换为 `AUTH_MODE=oidc`，校验 issuer、audience、过期时间、scope 和撤销状态。服务端会将客户端会话转换为 `tenant_id:user_id:client_thread_id`。`GET /health` 保持公开，便于健康检查。限流默认使用 Redis，按租户和用户共享计数；Redis 不可用时默认 fail-closed。

OIDC 生产模式要求 issuer、audience、JWKS、Redis 撤销、`jti` 和 token 年龄校验。具备 `security:admin` scope 的内部管理令牌可调用 `POST /admin/oidc/revoke`，提交 `jti` 和 `expires_at`；普通用户令牌不能调用该接口。公网上线前必须将 `AUTH_MODE=oidc`，禁止使用内部 dev token。

工具调用经过统一治理层：按服务端租户身份检查 scope 和工具白名单，限制输入长度，执行单工具超时，临时错误仅对无副作用工具重试。工具审计只保存工具名、状态、耗时和错误类型等摘要，不保存完整 prompt、Authorization、API key 或原始工具结果。

需要限制租户工具集合时设置 `TOOL_TENANT_ALLOWLIST=tenant-a=calculate,get_weather;tenant-b=calculate`；启用该配置后，未列出的租户默认不能调用任何工具。

## 本地开发（不走容器）

先启动 PostgreSQL 和 Redis，再初始化 checkpoint/store/审计表：

```powershell
docker compose -f infra/compose.dev.yml up -d
uv run python -m backend.migrations
```

`infra/compose.dev.yml` 是开发依赖栈，除 Postgres/Redis 外还含 OTel Collector、Prometheus、Jaeger、Grafana，应用本身跑在宿主机上；与之相对，`infra/compose.demo.yml` 把应用也放进容器，用于一键演示。

应用启动时默认不会自动建表；只有本地临时环境才建议设置 `LANGGRAPH_AUTO_SETUP=true`。

后端：

```powershell
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --loop backend.uvicorn_loop:selector_event_loop_factory
```

`--loop` 只在 Windows 上需要：psycopg 的异步实现要求 `SelectorEventLoop`。Linux 容器里用默认循环即可，所以 `Dockerfile` 的 `CMD` 没有这个参数。

前端：

```powershell
cd frontend
npm run dev
```

先签发本地开发令牌，再写入项目根 `.env` 的 `DEV_TENANT_TOKEN`：

```powershell
uv run python -m backend.issue_dev_token tenant-a user-1
```

Vite 开发代理把 `/api` 转发到后端，并从项目根 `.env` 读取 `DEV_TENANT_TOKEN` 注入 Bearer 头；该令牌不会打进 React bundle。公网部署应改为 OIDC/JWT、撤销机制和共享限流。

## 迁移与数据

部署或 CI 必须先执行 `uv run python -m backend.migrations`；应用启动只检查 schema 是否存在，不在多 Worker 中自动迁移。迁移命令使用 PostgreSQL advisory lock，多实例并发执行时只有一个会真正建表。

> 持锁连接必须是 autocommit 的。psycopg3 默认在第一条语句上隐式开启事务，那条连接会停在 `idle in transaction` 并持有锁；而 LangGraph 的 `checkpointer.setup()` 里有 `CREATE INDEX CONCURRENTLY`，它必须等待所有并发事务结束——等的正是持锁那条连接，迁移直接死锁。这个死锁**只在全新空库上出现**（已有索引时 `IF NOT EXISTS` 立刻返回），也就是只在首次部署和干净 CI 环境里触发。见 `backend/migrations.py` 的注释。

每次 Agent run 都会在 `agent_runs` / `agent_events` 中记录运行状态和脱敏事件。运行状态可通过带 `chat:read` scope 的令牌查询：`GET /audit/runs/{run_id}`。跨租户查询返回 404，不泄露其他租户记录。

审计记录由独立 retention 任务按 `AUDIT_RETENTION_DAYS` 清理，默认关闭以避免误删。启用前先用 dry-run 评估范围，再由单独的 Cron/容器任务执行：

```powershell
$env:AUDIT_RETENTION_ENABLED="true"
uv run python -m backend.retention --dry-run
uv run python -m backend.retention
```

任务使用 advisory lock，多个实例同时运行时只有一个会执行；每批最多删除 `AUDIT_RETENTION_BATCH_SIZE` 个已结束 run，并受 `AUDIT_RETENTION_MAX_RUNTIME_SECONDS` 限制。`status=running` 或没有 `finished_at` 的记录不会被删除。清理失败不会回退到内存，也不会影响 Agent 请求；调度器应对非零退出码告警。

项目根的 `checkpoints.db` 是早期 SQLite 实现留下的历史数据，**不是运行时的降级后备**——`backend/runtime.py` 只支持 PostgreSQL。迁移旧数据：

```powershell
uv run python -m backend.migrate_sqlite
```

迁移前必须设置 `MIGRATION_TENANT_ID` 和 `MIGRATION_USER_ID`，旧线程会被导入为该租户用户的线程，避免迁移后出现不可见历史。验证重启恢复正常前，不要删除旧的 SQLite 文件。

## 可观测性

运行指标通过 OpenTelemetry SDK 生成，并提供 Prometheus scrape endpoint：`GET /metrics`。生产环境必须设置 `METRICS_AUTH_TOKEN`，OTLP 导出可通过 `OTEL_EXPORTER_OTLP_ENDPOINT` 开启。指标标签只包含 route、status、outcome、tool 等低基数字段，不包含 tenant、user、run_id、prompt 或 token。

本地观测栈可通过 `infra/compose.dev.yml` 启动 OTel Collector、Prometheus、Jaeger 和 Grafana。生产环境应将它们分开部署并配置认证；不要把开发 compose 直接暴露到公网。

`MODEL_INPUT_COST_PER_1K_USD`、`MODEL_OUTPUT_COST_PER_1K_USD` 用于将供应商 usage metadata 统一换算为成本。设置 `TENANT_DAILY_BUDGET_USD` 后，Redis 会按 UTC 自然日对租户做共享预算计数，超过预算的运行返回 `budget_exceeded`；价格变更时必须同步配置并记录版本。**这三个值默认为 0，成本统计因此恒为 0**，接入真实供应商价格后才有意义。

## 部署

`Dockerfile` 是两阶段构建：builder 用 uv 按 `uv.lock` 装依赖，runtime 只带虚拟环境和源码，以 uid 10001 的非 root 用户运行。入口不用 `uv run`——uv 每次启动会校验并可能改写 `.venv`，而生产容器应跑在只读文件系统上。

```bash
docker build -t langgraph-agent:local .
```

公网部署参考 `infra/k8s/agent-deployment.yaml` 与 `infra/gateway/nginx.conf`：TLS/WAF/JWT 粗校验应放在云 API Gateway 或 WAF，应用仍必须做完整 OIDC、scope、tenant 和撤销校验。Kubernetes Secret 只是接口示例，生产应由云 secrets manager 或 External Secrets 控制器注入，不要把真实密钥写入 YAML。YAML 里的 `image: ghcr.io/your-org/langgraph-agent:REPLACE` 需要替换成实际镜像仓库地址。

### Readiness 与恢复演练

`/livez` 只表示进程存活；`/readyz` 会检查 Agent、PostgreSQL schema/version、Redis，以及 OIDC 模式下的 JWKS。生产负载均衡器应只把流量转发给 `readyz=200` 的实例。容器的 `HEALTHCHECK` 探的是 `/livez` 而不是 `/readyz`：依赖不可用时应该摘流量，而不是重启进程。

```powershell
uv run python -m backend.backup_restore backup --output .\artifacts\langgraph.dump
uv run python -m backend.backup_restore restore --backup .\artifacts\langgraph.dump --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore
uv run python -m backend.backup_restore verify --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore --tenant-id tenant-a --user-id user-1 --thread-id recovery-1
```

`verify` 只验证恢复后的 checkpoint 和长期记忆记录；必须再运行一次受保护的真实模型 E2E，验证同一 `tenant:user:thread` 能继续对话。备份文件和恢复数据库不得使用生产明文密钥或暴露公网端口。

## 测试

```powershell
uv run pytest tests -q -m "not live_e2e"
```

需要 PostgreSQL 和 Redis 的集成测试通过 `TEST_DATABASE_URL` / `REDIS_URL` 开关。没配这两个变量时它们会被 skip；配了但服务连不上会 fail（每个用例要等满连接超时，整套会从 9 秒变成 6 分钟）。

本地起一对临时实例跑全套（用非标准端口，避开本机已有的服务）：

```bash
docker run -d --name itpg -e POSTGRES_DB=langgraph -e POSTGRES_USER=langgraph -e POSTGRES_PASSWORD=itpass -p 127.0.0.1:55432:5432 postgres:17-alpine
docker run -d --name itredis -p 127.0.0.1:56379:6379 redis:7-alpine
export TEST_DATABASE_URL="postgresql://langgraph:itpass@127.0.0.1:55432/langgraph"
export DATABASE_URL="$TEST_DATABASE_URL"
export REDIS_URL="redis://127.0.0.1:56379/0"
uv run python -m backend.migrations
uv run pytest tests -q -m "not live_e2e"
```

跑完清掉：`docker rm -f itpg itredis`（失败时先留着容器查库更方便，所以没串在上面）。

命令行的环境变量优先于 `.env`（`conftest.py` 的 `load_dotenv()` 不覆盖已存在的变量），所以不必改本地配置。

CI 使用 service containers，当 `CI=true` 时缺少这两个变量会直接失败，不会静默跳过。

真实 DeepSeek E2E 默认不运行，以免普通 CI 产生费用。手动 workflow `Live Agent E2E` 需要受保护环境中的 `DEEPSEEK_API_KEY`、`LIVE_AGENT_TOKEN` 和 `TENANT_TOKEN_SECRET`，覆盖文本 SSE、工具调用和同线程续聊。

## 当前边界

明确没做的，不是遗漏而是取舍：

| 项 | 说明 |
|---|---|
| 工作流存库 + 热编译 | 改工作流需重启服务，所有租户共用一份定义。表结构、缓存失效、灰度回滚的设计已完成，代码未写；接缝留在 `backend/workflow_loader.py` 的单一函数入口 |
| 审批超时 / 过期 | 挂起状态当前永久有效，没有 TTL |
| 工作流可视化画布 | 节点 metadata 里预留了 `x/y/label`，画布未做 |
| RAG 工具节点 | `src/my_agent/workflow/nodes.py` 的 `rag_factory` 仍是 `NotImplementedError`，尚未与检索项目对接 |
| 成本统计 | 单价配置默认为 0，接入真实供应商价格前，成本与预算功能不产生实际数值 |
