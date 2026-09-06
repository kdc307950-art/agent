# 可观测性

> 目标：故障可解释、证据可复现。双轨指标 + 健康检查 + 结构化日志。

## 1. 指标（双轨）

| 轨道 | 载体 | 谁写 | 谁读 |
|---|---|---|---|
| 进程内 | OpenTelemetry / Prometheus（`RuntimeMetrics`） | API 进程（http_requests_total、agent_runs_total、预算/审计错误等） | 本进程 /metrics 端点 |
| 跨进程 | `worker_metrics` 表（schema v15） | 四个 Worker（safe_incr/safe_observe/safe_beat） | API 进程聚合输出 |

- `/metrics`（`METRICS_AUTH_TOKEN` 保护）拼接两段：进程内 exporter + worker_metrics 表
  （Prometheus 文本格式，`prometheus_text`）。
- 直方图桶落库（0.1–60s），P95 由 `render_latency_quantile` 从桶分布线性估计。

Worker 指标命名（与进程内 RuntimeMetrics 对齐，避免双轨语义漂移）：

```
inbound_events_total{channel,status} / inbound_event_processing_seconds
inbound_worker_retry_total / inbound_worker_dead_total / wecom_resume_total
outbox_claimed_total / outbox_delivered_total / outbox_retried_total
outbox_dead_total / outbox_lease_recovered_total / outbox_dead_present_total
outbox_backlog_check_errors_total
sla_scan_runs_total / ticket_sla_breach_total
worker_loop_errors_total{worker}      # 单轮循环异常（DB 领取/扫描失败），退避后继续
pending_intake_expired_total          # recovery worker 翻转过期追问
```

## 2. 故障隔离（safe_* 语义）

| 写入失败 | 行为 |
|---|---|
| 业务提交失败 | 进入业务重试/死信（不受指标影响） |
| 指标写入失败 | 只记结构化日志（`metric_write_failed`），不改变业务结果 |
| 心跳写入失败 | 记日志，不终止 Worker |
| claim/扫描阶段 DB 故障 | 记录 `worker_loop_errors_total` + 指数退避 + 继续下一轮，不退出进程 |
| PostgreSQL 整体不可用 | 业务与指标一起不可用，/readyz 503（**指标与业务同库**，不做"指标降级但业务继续"） |

## 3. 健康检查

| 端点 | 语义 |
|---|---|
| /health | 进程存活（不探测依赖） |
| /livez | 存活探针 |
| /readyz | Agent、Postgres schema/version、Redis（按配置）、OIDC（按配置）；`READINESS_CHECK_WORKERS=true` 时增加：worker 心跳（每类 90s 内新鲜=ok，否则 missing/failed）、outbox backlog（pending≥100 → degraded）、outbox dead（>0 → failed） |
| /metrics | Prometheus 文本（鉴权） |

任意检查非 "ok" → 整体 503。心跳过期/缺失是 Worker 进程死亡的最早信号。

## 4. 结构化日志

- JSON 日志（`logging_config.setup_json_logging`），字段约定：
  `ts/level/logger/msg/tenant_id/channel/event_id/ticket_id/worker_id/attempt/status/duration_ms/error_code`。
- 禁止记录：access token、签名密钥、用户敏感正文、完整加密 XML。
- 关键事件：`inbound_event_processed`、`worker_round_failed`、`metric_write_failed`、
  `heartbeat_write_failed`、`outbox_delivery`、`pending_expiry_failed`。

## 5. 排障路径（示例）

- `/readyz` 503 且 `worker_inbound=missing` → 查 inbound worker 日志最后心跳。
- Outbox 死信增长 → `outbox_dead_total` + 回调端点连通性 + 幂等键实现。
- Resume 失败率升高 → `wecom_resume_total{result=unparsable_reply}` 占比。
- 指标表故障 → 结构化日志仍可用，业务不受影响（safe_*）。注：该语义由
  **依赖故障注入测试**（替身对象）验证，非真实表损坏恢复测试。
