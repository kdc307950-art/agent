# 可观测性：指标、健康检查与排障

> 覆盖：Worker 心跳（inbound/outbox/sla/recovery）、跨进程指标表、`/metrics` 聚合、
> `/readyz` 心跳与死信门禁、结构化 JSON 日志。

## 1. 指标（Prometheus 文本格式，`GET /metrics`）

Worker 是独立进程，内存指标 API 读不到；关键计数/时延写入 `worker_metrics` 表
（schema v15），API 进程 `/metrics` 聚合输出。每行格式：
`metric{label="value",...} value`。

| 指标 | 类型 | 说明 |
|---|---|---|
| `inbound_events_total{channel,status}` | counter | 渠道入站事件处理结果（committed/failed/dead） |
| `inbound_event_processing_seconds_bucket/count/sum` | histogram | 入站处理时延（秒），桶 0.1–60 |
| `inbound_worker_retry_total{channel}` | counter | 入站临时失败重试次数 |
| `inbound_worker_dead_total{channel}` | counter | 入站死信次数 |
| `outbox_delivery_total{status}` | counter | Outbox 投递结果（delivered/retried/dead） |
| `wecom_resume_total{result}` | counter | 企微回复恢复受理结果（resumed/unparsable_reply/…） |
| `ticket_sla_breach_total` | counter | SLA 超时事件（SLA worker 扫描产出） |
| `sla_scan_runs_total` | counter | SLA 扫描轮次 |

### 初始告警阈值

| 阈值 | 含义 |
|---|---|
| `inbound_worker_dead_total` > 0 且持续增长 | 入站处理持续失败，检查业务异常 |
| `inbound_worker_retry_total` / `inbound_events_total` > 5%（5 分钟窗口） | 处理失败率异常升高 |
| `wecom_resume_total{result="unparsable_reply"}` 占比 > 3% | 客户回复格式问题增多 |
| `outbox_delivery_total{status="dead"}` > 0 | 死信：检查回调端点与签名 |
| `inbound_event_processing_seconds` P95 > 30s | 受理/LLM 过慢，触发企微重试 |
| `ticket_sla_breach_total` 持续增长 | SLA 未在时限内响应，需告警升级 |

### P95 查询

直方图桶已落库，按 `{metric}_count` 与 `{metric}_bucket{le=...}` 估算
（`backend/worker_metrics.py::render_latency_quantile`）。

## 2. 健康检查

| 端点 | 语义 |
|---|---|
| `GET /health` | 进程存活（不探测依赖） |
| `GET /livez` | 存活探针 |
| `GET /readyz` | 就绪探针：PostgreSQL schema、Redis（按配置）、OIDC JWKS（按配置）；`READINESS_CHECK_WORKERS=true` 时增加 worker 心跳与 Outbox 积压/死信 |
| `GET /metrics` | Prometheus 文本（`METRICS_AUTH_TOKEN` 保护） |

### Worker 心跳

- 表 `worker_heartbeats(worker_type, worker_id, last_beat_at)`，主键 `(worker_type, worker_id)`。
- 四类进程：`inbound` / `outbox` / `sla` / `recovery`，各自常驻循环每轮刷新心跳。
- `/readyz` 判定：每类至少一个心跳在 `WORKER_HEARTBEAT_TTL_SECONDS`（默认 90s）内为 `ok`，
  否则 `missing` → 503。
- 默认 `READINESS_CHECK_WORKERS=false`（单机 demo 无 worker 时不误报）；生产/CI 置 `true`。

### Outbox 门禁

- `outbox_backlog`：`pending 且 available_at <= now()` 数量 ≥100 → `degraded`。
- `outbox_dead`：`dead` 数量 >0 → `failed`。

## 3. 结构化日志

Worker 处理事件输出 JSON 日志，字段约定：
`ts / level / logger / msg / tenant_id / channel / event_id / ticket_id /
worker_id / attempt / status / duration_ms / error_code`。

禁止记录：access token、签名密钥、用户敏感正文、完整加密 XML。
日志经 `backend/logging_config.py::setup_json_logging()` 启用（各 `run_*_worker` 启动时调用）。

示例：

```json
{"ts": "2026-08-28T08:58:33.290Z", "level": "INFO", "logger": "backend.inbound_worker",
 "msg": "inbound_event_processed", "tenant_id": "demo", "channel": "wecom",
 "event_id": "a-1787907513", "ticket_id": "665895…", "worker_id": "inbound-abc",
 "attempt": 1, "status": "committed", "duration_ms": 137.2}
```

## 4. 排障步骤

| 现象 | 检查 |
|---|---|
| `/readyz` 503 且 `worker_inbound=missing` | 确认 `run_inbound_worker` 进程存活；看其日志最后心跳时间 |
| 企微回调一直重试 | `inbound_event_processing_seconds` P95 是否 >5s；看 inbound worker 日志 `status=failed` 与 `error_code` |
| Outbox 死信 >0 | `GET /integrations/outbox/dead` 列出死信；检查回调端点连通与 `OUTBOX_SHARED_SECRET`；`POST /integrations/outbox/replay` 重放 |
| Resume 失败率升高 | `wecom_resume_total` 分 result；`unparsable_reply` 多为客户格式问题，提示「字段:值」格式 |
| SLA 超时无告警 | `sla_scan_runs_total` 是否增长；`run_sla_worker` 心跳是否新鲜；`ticket_sla_breach_total` 计数 |

## 5. 快速验证

```powershell
# 观察指标（含 worker 指标）
Invoke-WebRequest http://127.0.0.1:8000/metrics -UseBasicParsing | Select-Object -ExpandProperty Content

# 心跳门禁（CI/生产）
$env:READINESS_CHECK_WORKERS="true"
Invoke-WebRequest http://127.0.0.1:8000/readyz -UseBasicParsing
```
