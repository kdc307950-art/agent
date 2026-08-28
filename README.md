# 多租户 IT 服务台工单系统（Helpdesk）

[![CI](https://github.com/kdc307950-art/agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kdc307950-art/agent/actions/workflows/ci.yml)

面向中小企业客服、IT、行政支持部门的多租户工单自动处置系统骨架，首个落地形态是**内部 IT 服务台**：员工通过 Web / 企业微信提交工单，系统自动分类（`it.vpn` / `it.account` / `it.network` 等子分类）、按租户 IT 策略追问必填字段、加载对应 SLA、规则派单，Agent 检索知识库生成带引用的建议，客服在响应式工作台上完成接单、处理、回访与关闭。

技术底座：确定性工单状态机 + LangGraph 受理/补全/分类/派单图 + Agentic RAG 引用门禁，运行在多租户隔离、PostgreSQL Checkpoint、审计、Redis 限流、预算和 Outbox 之上。

> 仓库根目录的 `main.py` / `main_supervisor.py` / `main_workflow.py` 与 `workflows/legacy-demo.json` 是早期通用聊天/天气/计算 Demo，统一标记为 **legacy-demo**，仅供试跑图结构，与生产工单链路无关；产品定位与入口见下文。

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

没填 `DEEPSEEK_API_KEY` 也能起来——服务健康、接口可看，只是自动分类和知识建议会失败。想完整演示就在 shell 里 `export DEEPSEEK_API_KEY=sk-xxx` 再 `up`。

用完 `docker compose -f infra/compose.demo.yml down -v` 清干净。这份 compose 里的密码是写死的弱口令且不暴露数据库端口，**仅供本地演示，不要用于任何联网环境**。

## 10 分钟演示：跑通 IT 服务台完整闭环

一条命令生成幂等演示数据（租户 `demo`：SLA、`it.vpn` 策略、客服团队/成员/排班/路由、8 篇知识文档、5 台资产）：

```powershell
uv run python -m backend.seed_demo
```

演示账号（签发开发令牌，`AUTH_MODE=dev`）：

```powershell
uv run python -m backend.issue_dev_token demo customer-1 --role helpdesk-customer   # 员工
uv run python -m backend.issue_dev_token demo agent-1    --role helpdesk-agent       # IT 客服
uv run python -m backend.issue_dev_token demo admin-1    --role helpdesk-it-admin    # IT 管理员
```

把令牌分别粘贴到工作台，演示脚本与验收检查点见 [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)：客户提交「VPN 无法连接」→ 自动分类 `it.vpn` → 追问缺失字段 → 命中 `sla-vpn` → 派单给 team-it → 知识建议带引用 → 客服接单处理 → 回访关闭。

## 仓库里的入口

**生产路径只有一条：`backend/app.py`。** 本文档其余部分讲的都是它——多租户、审计、预算、限流、工具治理、跨请求人工审批都在这条路径上。

根目录还有三个 CLI 脚本与一个 JSON 工作流，是早期通用聊天/天气/计算 Demo（**legacy-demo**），保留用于快速试跑图结构，**不是**产品入口：

| 入口 | 用途 | 与生产路径的关系 |
| --- | --- | --- |
| `backend/app.py` | **生产服务**，FastAPI + SSE（IT 服务台） | 唯一受支持的部署形态 |
| `main.py` | legacy-demo：单 Agent 命令行对话，带消息摘要 | 不经过鉴权、审计、限流和预算 |
| `main_supervisor.py` | legacy-demo：硬编码 supervisor 图 + 命令行审批 | 审批是进程内 `input()` 阻塞，**不是**生产的跨请求审批 |
| `main_workflow.py` | legacy-demo：从 JSON 加载图 + 命令行审批 | 用本地 `checkpoints.db`，加载 `workflows/legacy-demo.json` |

三个 CLI 的价值是改完 `workflows/*.json` 后不起容器就能验证图跑不跑得通。**但人工审批的真实实现不在它们里面**——CLI 里审批是同一个进程同一次调用中的阻塞输入，生产里是两次 HTTP 请求、两条 SSE 流、状态落在 PostgreSQL checkpoint 上，见 [人工审批为什么是两次请求](#人工审批为什么是两次请求)。

`SUPERVISOR_AGENT.md` 是同一时期写的说明书（legacy-demo），其中的 HITL 部分已被生产链路取代，只作为图结构和路由逻辑的补充阅读。

## 工单业务链路

正式工单链路与旧 `/chat/*` 对话接口独立：

```text
创建工单 → 受理 → 缺字段则 awaiting_customer → 客户类型化恢复
       → 分类 → 规则派单 queued → assigned → in_progress
       → resolved → 满意度回访 → closed
```

工单快照与 `ticket_status_events` 使用连续乐观锁版本；批量状态转换在同一个 PostgreSQL 事务中写快照和事件。LangGraph Checkpoint 负责受理节点的暂停/恢复，工单表是业务状态的事实来源。

主要 API：

| API | Scope | 用途 |
|---|---|---|
| `POST /tickets` | `ticket:customer` | 创建工单 |
| `GET /tickets` | `ticket:customer` / `ticket:agent` | 游标分页、状态/类别/团队筛选 |
| `GET /tickets/{id}` | 同上 | 查询工单；客户只能看自己的 |
| `POST /tickets/{id}/intake` | `ticket:customer` | 启动受理图 |
| `POST /tickets/{id}/resume` | `ticket:customer` | 按真实 interrupt ID 补充信息 |
| `POST /tickets/{id}/transitions` | 与 actor 对应的 `ticket:*` | 接单、处理、解决、关闭等 |
| `POST /tickets/{id}/survey` | `ticket:agent` | 发起回访并写 Outbox |
| `POST /tickets/{id}/survey/{survey_id}/response` | `ticket:customer` | 提交 1–5 分满意度 |

渠道入站采用**快速 ACK + 异步 Worker**：企微/钉钉 Webhook 与内部通道 `POST /integrations/{channel}/events` 只做验签解密并登记事件（`inbound_events`，状态 `received`），立即返回 `202 {"accepted": true, "event_id": ...}`；`run_inbound_worker` 领取后异步建单、受理、分类、派单与澄清 Outbox，状态经 `received → processing → committed/failed/dead`，临时错误指数退避、超限进 dead 可重放。调用方通过 `GET /integrations/events/{event_id}`（`ticket:channel`）轮询状态并获取 `ticket_id`；同 `event_id` 重复登记幂等，不重复建单。企微事件消息（enter_agent / location 等）验签通过后返回 200 忽略，不登记、不建单。

企业微信 `/integrations/wecom/webhook` 使用 SHA-1 验签、AES-CBC 解密、CorpID 与重放窗口校验；钉钉 `/integrations/dingtalk/webhook` 使用时间戳和 HMAC-SHA256。两个厂商端点以服务端配置绑定租户，不信任请求体 tenant。内部适配器也可使用带 `ticket:channel` scope 的 `/integrations/{channel}/events`。

知识库采用 Agentic RAG：Agent 可在有界轮次内根据检索结果生成补充查询，所有查询都重复执行 tenant、发布状态、有效期和部门 ACL；全文与向量候选使用 RRF 融合。运行时已装配 `AgenticRAGService`、`KnowledgeAnswerService` 和可选 `PgVectorRetriever`，受理图在派单后调用回答门禁生成建议回复。默认策略是建议回复，搜索耗尽后不会自动发送。没有双路证据、缺少有效引用、高风险或财务类问题都禁止自动回复并转人工。pgvector 为可选真实向量后端，未安装扩展或未配置 embedding 端点时只使用全文并明确记录降级原因。

## 架构

```mermaid
flowchart LR
    U[React 客服工作台] --> API[FastAPI]
    CH[企业微信 / 钉钉] --> API
    API --> AUTH[OIDC / 租户令牌 / Webhook 验签]
    AUTH --> TICKET[工单领域状态机]
    TICKET --> INTAKE[受理 / 补全 / 分类 / 派单图]
    INTAKE --> RAG[ACL 检索 / 引用门禁]
    API --> CHAT[legacy-demo<br/>旧 Chat / JSON 多 Agent 编排]
    TICKET --> PG[(PostgreSQL<br/>工单 / 事件 / Checkpoint / Outbox)]
    API --> REDIS[(Redis<br/>限流 / 预算 / 撤销)]
    PG --> WORKER[Outbox / SLA / 回访任务]
    INTAKE --> LLM[DeepSeek / 可替换分类器]
    API -. 指标 / 链路 .-> OTEL[OTel / Prometheus / Jaeger]
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

## 编排模式与人工审批（legacy-demo）

> 以下 `workflow` 图形态与 `/chat/*` 审批链路是早期通用聊天 Demo 的遗留能力（**legacy-demo**），与生产 IT 服务台工单链路无关。工单受理图由 `src/my_agent/helpdesk/graph.py` 定义，不依赖 `AGENT_GRAPH_MODE`。

后端仍保留两种图形态，由 `AGENT_GRAPH_MODE` 切换，**默认 `single`**：

```dotenv
AGENT_GRAPH_MODE=workflow
AGENT_WORKFLOW_PATH=workflows/legacy-demo.json
```

`workflow` 模式由 JSON 定义编译出图，支持 supervisor 路由、子 Agent 和 `human_approval` 审批节点。配置缺失或 spec 不合法时服务在启动阶段直接失败，不会带病启动。示例图 `workflows/legacy-demo.json` 是天气/计算路由，仅用于试跑 JSON 编译层。

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
- `WECOM_TENANT_ID/TOKEN/ENCODING_AES_KEY/CORP_ID`：企业微信 Webhook，四项必须成组配置。
- `DINGTALK_TENANT_ID/APP_SECRET`：钉钉 Webhook，两项必须成组配置。
- `WEBHOOK_REPLAY_WINDOW_SECONDS`：Webhook 时间戳容差，默认 300 秒。

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
uv run python -m backend.issue_dev_token tenant-a agent-1 --role helpdesk-agent
```

可选角色：`chat`、`helpdesk-agent`、`helpdesk-customer`、`helpdesk-channel`、`helpdesk-approver`。Vite 开发代理把 `/api` 转发到后端，并从项目根 `.env` 读取 `DEV_TENANT_TOKEN` 注入 Bearer 头；该令牌不会打进 React bundle。客服工作台需要 `ticket:agent`，客户建单/补充/回访需要 `ticket:customer`，渠道内部适配器需要 `ticket:channel`。开发时可按角色签发不同令牌；公网部署必须改为 OIDC/JWT、撤销机制和共享限流。

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

Outbox 使用独立常驻进程发送渠道消息、回访和 SLA 升级事件。Worker 通过 `FOR UPDATE SKIP LOCKED` 支持多副本，暂时性网络错误指数退避，超过最大次数进入 `dead`：

```powershell
$env:OUTBOX_SHARED_SECRET="replace-with-service-to-service-secret"
$env:OUTBOX_TICKET_MESSAGE_ENDPOINT="https://internal.example/messages"
$env:OUTBOX_SURVEY_ENDPOINT="https://internal.example/surveys"
$env:OUTBOX_SLA_ENDPOINT="https://internal.example/sla"
uv run python -m backend.run_outbox_worker --poll-interval 1 --batch-size 20
uv run python -m backend.run_sla_worker --interval 30 --batch-size 100
uv run python -m backend.run_workflow_recovery --interval 30 --grace 30
```

每个 Endpoint 都必须实现 `X-Idempotency-Key` 幂等语义；Worker 本身只保证数据库领取和状态转移，不替渠道服务解决重复请求。

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

构建时 uv 二进制默认从 `ghcr.io/astral-sh/uv:0.11` 获取；ghcr 网络受限时用镜像站覆盖：

```bash
docker build --build-arg UV_IMAGE=ghcr.nju.edu.cn/astral-sh/uv:0.11 -t langgraph-agent:local .
```

CI（GitHub Actions）镜像加速采用显式配置，不硬编码公共第三方站：

- **GitHub-hosted runner**：运行在海外，`docker.io` / `ghcr.io` 默认可达，无需额外配置。
- **自建 runner（国内网络）**：
  1. 在 runner 主机配置 `~/.docker/daemon.json` 的 `registry-mirrors`，写入受控可信的 Docker Hub 镜像站后重启 Docker，供 service containers 和构建基础镜像拉取使用。
  2. 在仓库 Settings → Variables 配置 `UV_IMAGE` 指向可信 ghcr 镜像站（例如 `ghcr.nju.edu.cn/astral-sh/uv:0.11`），CI 构建步骤会自动作为 `--build-arg` 传入；未配置时默认使用官方 `ghcr.io/astral-sh/uv:0.11`。

公网部署参考 `infra/k8s/agent-deployment.yaml` 与 `infra/gateway/nginx.conf`：TLS/WAF/JWT 粗校验应放在云 API Gateway 或 WAF，应用仍必须做完整 OIDC、scope、tenant 和撤销校验。Kubernetes Secret 只是接口示例，生产应由云 secrets manager 或 External Secrets 控制器注入，不要把真实密钥写入 YAML。YAML 里的 `image: ghcr.io/your-org/langgraph-agent:REPLACE` 需要替换成实际镜像仓库地址。

### Readiness 与恢复演练

`/livez` 只表示进程存活；`/readyz` 会检查 Agent、PostgreSQL schema/version、Redis，以及 OIDC 模式下的 JWKS。生产负载均衡器应只把流量转发给 `readyz=200` 的实例。容器的 `HEALTHCHECK` 探的是 `/livez` 而不是 `/readyz`：依赖不可用时应该摘流量，而不是重启进程。

```powershell
uv run python -m backend.backup_restore backup --output .\artifacts\langgraph.dump
uv run python -m backend.backup_restore restore --backup .\artifacts\langgraph.dump --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore
uv run python -m backend.backup_restore verify --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore --tenant-id tenant-a --user-id user-1 --thread-id recovery-1
```

`verify` 只验证恢复后的 checkpoint 和长期记忆记录；必须再运行一次受保护的真实模型 E2E，验证同一 `tenant:user:thread` 能继续对话。备份文件和恢复数据库不得使用生产明文密钥或暴露公网端口。

## 检索评测（量化结果）

内置 42 条脱敏 IT 检索评测集（`backend/knowledge/eval_cases.py`，覆盖 8 个 IT 子分类 + 跨文档用例）。在已导入知识库的数据库上运行评测：

```powershell
# 准备：迁移 + 导入脱敏 IT 知识库（幂等）
uv run python -m backend.seed_demo
# 全文检索基线（未配置 embedding 时自动降级为 lexical-only）
uv run python -m backend.run_knowledge_eval
# 配置真实 embedding 服务后跑 hybrid（KNOWLEDGE_EMBEDDING_ENDPOINT）
uv run python -m backend.run_knowledge_eval --embed
```

输出量化报告：**Top1 命中率 / Recall@k / MRR@k**，按分类分项；并统计「无检索命中」用例数——这些用例在受理链路中触发门禁转人工，不会自动发送。`--topk 5 --seed --limit N` 可调参。配置 embedding 服务：`KNOWLEDGE_EMBEDDING_MODEL` / `KNOWLEDGE_EMBEDDING_DIMENSION` / `KNOWLEDGE_EMBEDDING_ENDPOINT`（POST `{"texts": [...]}` 返回 `{"embeddings": [...]}`），并先执行 `uv run python -m backend.vector_migrations` 与 embedding 导入流水线。

### 中文分词全文检索（lexical-only 基线）

全文检索使用 **jieba + 自定义 IT 词典**（`backend/knowledge/tokenizer.py`）在入库与查询两侧做同一分词，`search_text` 存分词文本，`search_vector` 基于分词结果生成（schema v12），pg_trgm 提供错别字/短词兜底。查询侧为三路召回：`plainto_tsquery`（AND 精确）∪ `to_tsquery`（OR 覆盖率）∪ `trigram` 相似度，排序按命中 token 数（子串匹配，对中文分词上下文不一致鲁棒）→ ts_rank → 相似度。所有召回均强制 tenant / published / 有效期 / visibility / 部门 ACL 过滤。

当前内置 42 条评测集（8 个 IT 子分类 + 跨文档用例）在演示数据上的 **lexical-only 基线：Top1 100%、Recall@5 100%、MRR@5 1.000、无命中 0 条**。配置 embedding 后 `--embed` 出 hybrid（RRF 融合 + 分类加权）对比。

## 渠道沙箱验证（企业微信）

验签/解密/幂等/追问/门禁的自动化测试覆盖见 `tests/test_channel_adapters.py` 与 `tests/test_ticket_api.py`（`test_wecom_webhook_*`、`test_dingtalk_webhook_*`）。真实沙箱端到端验证步骤、验证矩阵与验收清单见 [docs/CHANNEL_SANDBOX.md](docs/CHANNEL_SANDBOX.md)：企业微信自建应用回调 URL 校验（echostr）、文本建单、同 `MsgId` 幂等、缺字段追问进 Outbox、`run_outbox_worker` 回调投递、无引用/高风险转人工。

## 测试

```powershell
uv run pytest tests -q -m "not live_e2e"
```

需要 PostgreSQL 和 Redis 的集成测试通过 `TEST_DATABASE_URL` / `REDIS_URL` 开关。没配这两个变量时它们会被 skip；配了但服务连不上会 fail（每个用例要等满连接超时，整套会从 9 秒变成 6 分钟）。

本地使用轻量集成测试栈跑全套（使用非标准端口，避开已有服务）：

```powershell
docker compose -f infra/compose.test.yml up -d --wait
$env:TEST_DATABASE_URL="postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55432/langgraph"
$env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:REDIS_URL="redis://127.0.0.1:56379/0"
uv run python -m backend.migrations
$env:KNOWLEDGE_EMBEDDING_DIMENSION="1536"
uv run python -m backend.vector_migrations
uv run pytest tests -q -m "not live_e2e"
```

跑完使用 `docker compose -f infra/compose.test.yml down -v` 清理。该栈只有 PostgreSQL 和 Redis，数据放在临时文件系统中，仅用于本地集成测试。

命令行的环境变量优先于 `.env`（`conftest.py` 的 `load_dotenv()` 不覆盖已存在的变量），所以不必改本地配置。

CI 使用 pgvector PostgreSQL 17 / Redis 7 service containers；当 `CI=true` 时缺少这两个变量会直接失败，不会静默跳过。本地还在 PostgreSQL 14 + pgvector 0.8.1 / Redis 6 上执行过兼容验证：schema v9 和 HNSW 迁移成功，全部非 live 测试 `192 passed`。真实 HTTP 工单 E2E 验证了创建、缺字段中断、补充恢复、分类派单、处理、解决、回访和关闭，最终事件版本连续到 v9。

真实 DeepSeek E2E 默认不运行，以免普通 CI 产生费用。手动 workflow `Live Agent E2E` 需要受保护环境中的 `DEEPSEEK_API_KEY`、`LIVE_AGENT_TOKEN` 和 `TENANT_TOKEN_SECRET`，覆盖文本 SSE、工具调用和同线程续聊。

## 当前边界

明确没做的，不是遗漏而是取舍：

| 项 | 说明 |
|---|---|
| 工作流存库 + 热编译 | 改 JSON 工作流仍需重启服务，所有租户共用一份定义 |
| 挂起任务 TTL | 工单信息补全和旧审批尚未实现自动过期/取消策略 |
| Embedding 供应商 | pgvector、HNSW、入库流水线和 Agentic RAG 已实现；生产仍需选择 embedding 模型、固定维度并完成离线评测，默认建议回复而不自动发送 |
| 附件安全链路 | 尚未接对象存储、病毒扫描、临时授权下载和内容解析隔离 |
| PostgreSQL RLS | Repository 全部强制 tenant 条件，但数据库行级安全尚未启用 |
| 工作台认证 | 本地 Vite 代理使用单个开发令牌；生产需接 IdP 并按客户/客服/审批人分配 scope |
| 企微追问闭环 | 企业微信文本消息建单并追问后，客户按「字段:值」回复（如 `device: laptop-001`）会**关联原工单恢复受理**（`ticket_customer_pending_intake` 唯一待补全索引），不新建工单；无待补全记录时按普通新消息建单。恢复后自动分类/SLA/派单，状态流水记录 `customer_reply_received → intake_resumed → classified → assigned`；`GET /tickets/{id}/intake-status` 可查待补全与 resume 次数 |
| 成本统计 | 单价配置默认为 0，接入真实供应商价格前，成本与预算功能不产生实际数值 |
