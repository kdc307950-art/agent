# LangGraph Agent

## 安全配置

`.env` 只用于本地环境，禁止提交。工作区 `.env` 中的真实 DeepSeek 和 LangSmith 密钥应在对应平台撤销并重新生成。

复制 `.env.example` 为 `.env`，填写：

- `DEEPSEEK_API_KEY`：后端调用模型所需。
- `TENANT_TOKEN_SECRET`：签发和验证租户令牌的服务端密钥；生产建议替换为 OIDC/JWT 验证。
- `LANGCHAIN_API_KEY`：可选，仅用于 LangSmith 追踪。

启动后，`POST /chat/stream` 必须带 Bearer 令牌。开发环境使用 `AUTH_MODE=dev` 的内部签名令牌；公网环境必须切换为 `AUTH_MODE=oidc`，校验 issuer、audience、过期时间、scope 和撤销状态。服务端会将客户端会话转换为 `tenant_id:user_id:client_thread_id`。`GET /health` 保持公开，便于健康检查。限流默认使用 Redis，按租户和用户共享计数；Redis 不可用时默认 fail-closed。

## 启动

本地开发先启动 PostgreSQL 和 Redis，再初始化 LangGraph 的 checkpoint/store 表：

```powershell
docker compose -f infra/compose.dev.yml up -d
uv run python -m backend.migrations
```

应用启动时默认不会自动建表；只有本地临时环境才建议设置 `LANGGRAPH_AUTO_SETUP=true`。

后端：

```powershell
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --loop backend.uvicorn_loop:selector_event_loop_factory
```

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

CI 会使用 PostgreSQL 和 Redis service containers；当 `CI=true` 时，缺少 `TEST_DATABASE_URL` 或 `REDIS_URL` 会直接失败，不会静默跳过集成测试。

每次 Agent run 都会在 PostgreSQL 的 `agent_runs` / `agent_events` 中记录运行状态和脱敏事件。部署或 CI 必须先执行 `uv run python -m backend.migrations`；应用启动只检查 schema 是否存在，不在多 Worker 中自动迁移。运行状态可通过带 `chat:read` scope 的令牌查询：`GET /audit/runs/{run_id}`。跨租户查询返回 404，不泄露其他租户记录。

审计记录由独立 retention 任务按 `AUDIT_RETENTION_DAYS` 清理，默认关闭以避免误删。启用前先用 dry-run 评估范围，再由单独的 Cron/容器任务执行：

```powershell
$env:AUDIT_RETENTION_ENABLED="true"
uv run python -m backend.retention --dry-run
uv run python -m backend.retention
```

任务使用 PostgreSQL advisory lock，多个实例同时运行时只有一个会执行；每批最多删除 `AUDIT_RETENTION_BATCH_SIZE` 个已结束 run，并受 `AUDIT_RETENTION_MAX_RUNTIME_SECONDS` 限制。`status=running` 或没有 `finished_at` 的记录不会被删除。清理失败不会回退到内存，也不会影响 Agent 请求；调度器应对非零退出码告警。

运行指标通过 OpenTelemetry SDK 生成，并提供 Prometheus scrape endpoint：`GET /metrics`。生产环境必须设置 `METRICS_AUTH_TOKEN`，OTLP 导出可通过 `OTEL_EXPORTER_OTLP_ENDPOINT` 开启。指标标签只包含 route、status、outcome、tool 等低基数字段，不包含 tenant、user、run_id、prompt 或 token。
本地观测栈可通过 `infra/compose.dev.yml` 启动 OTel Collector、Prometheus、Jaeger 和 Grafana。生产环境应将 Collector、Prometheus、Trace backend 和 Grafana 分开部署并配置认证；不要把开发 compose 直接暴露到公网。

`MODEL_INPUT_COST_PER_1K_USD`、`MODEL_OUTPUT_COST_PER_1K_USD` 用于将供应商 usage metadata 统一换算为成本。设置 `TENANT_DAILY_BUDGET_USD` 后，Redis 会按 UTC 自然日对租户做共享预算计数，超过预算的运行返回 `budget_exceeded`；价格变更时必须同步配置并记录版本。

OIDC 生产模式要求 issuer、audience、JWKS、Redis 撤销、`jti` 和 token 年龄校验。具备 `security:admin` scope 的内部管理令牌可调用 `POST /admin/oidc/revoke`，提交 `jti` 和 `expires_at`；普通用户令牌不能调用该接口。公网上线前必须将 `AUTH_MODE=oidc`，禁止使用内部 dev token。

真实 DeepSeek E2E 默认不运行，以免普通 CI 产生费用。手动 workflow `Live Agent E2E` 需要受保护环境中的 `DEEPSEEK_API_KEY`、`LIVE_AGENT_TOKEN` 和 `TENANT_TOKEN_SECRET`，覆盖文本 SSE、工具调用和同线程续聊。

工具调用经过统一治理层：按服务端租户身份检查 scope 和工具白名单，限制输入长度，执行单工具超时，临时错误仅对无副作用工具重试。工具审计只保存工具名、状态、耗时和错误类型等摘要，不保存完整 prompt、Authorization、API key 或原始工具结果。

需要限制租户工具集合时设置 `TOOL_TENANT_ALLOWLIST=tenant-a=calculate,get_weather;tenant-b=calculate`；启用该配置后，未列出的租户默认不能调用任何工具。

`checkpoints.db` 仅作为本地 SQLite 后备。生产运行时使用 `DATABASE_URL` 指向 PostgreSQL；迁移完成并验证重启恢复前，不要删除旧的 SQLite 文件。

迁移现有 SQLite checkpoint：

```powershell
uv run python -m backend.migrate_sqlite
```

迁移旧 SQLite 前必须设置 `MIGRATION_TENANT_ID` 和 `MIGRATION_USER_ID`，旧线程会被导入为该租户用户的线程，避免迁移后出现不可见历史。

## Readiness 与恢复演练

`/livez` 只表示进程存活；`/readyz` 会检查 Agent、PostgreSQL schema/version、Redis，以及 OIDC 模式下的 JWKS。生产负载均衡器应只把流量转发给 `readyz=200` 的实例。

迁移命令使用 PostgreSQL advisory lock，应用进程不会在生产环境自动建表。备份和恢复演练示例：

```powershell
uv run python -m backend.backup_restore backup --output .\artifacts\langgraph.dump
uv run python -m backend.backup_restore restore --backup .\artifacts\langgraph.dump --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore
uv run python -m backend.backup_restore verify --database-url postgresql://langgraph:password@127.0.0.1:5433/langgraph_restore --tenant-id tenant-a --user-id user-1 --thread-id recovery-1
```

`verify` 只验证恢复后的 checkpoint 和长期记忆记录；必须再运行一次受保护的真实模型 E2E，验证同一 `tenant:user:thread` 能继续对话。备份文件和恢复数据库不得使用生产明文密钥或暴露公网端口。

公网部署参考 `infra/k8s/agent-deployment.yaml` 与 `infra/gateway/nginx.conf`：TLS/WAF/JWT 粗校验应放在云 API Gateway 或 WAF，应用仍必须做完整 OIDC、scope、tenant 和撤销校验。Kubernetes Secret 只是接口示例，生产应由云 secrets manager 或 External Secrets 控制器注入，不要把真实密钥写入 YAML。
