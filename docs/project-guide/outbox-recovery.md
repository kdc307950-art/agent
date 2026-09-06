# Outbox 可靠投递与恢复

> 所有外发消息（澄清追问、回访、SLA 升级事件）先写 `outbox_events`，由常驻
> `outbox_worker` 投递；业务提交与投递解耦，支持失败退避、死信与重放。

## 事件类型与发送方

| event_type | 写入方 | 回调端点（环境变量） |
|---|---|---|
| `ticket_message.send` | 受理链澄清/建议消息 | `OUTBOX_TICKET_MESSAGE_ENDPOINT` |
| `survey.send` | 回访流程 | `OUTBOX_SURVEY_ENDPOINT` |
| `sla.breached` | sla_worker 扫描产出 | `OUTBOX_SLA_ENDPOINT` |

## 投递协议（HttpOutboxSender）

```
POST {endpoint}
Headers:
  X-Idempotency-Key: {tenant_id}:{idempotency_key}   # 回调端去重
  X-Outbox-Timestamp: {unix}
  X-Outbox-Signature: sha256=HMAC(secret, ts + "." + body)
Body: 规范化 JSON（sort_keys + 紧凑分隔符）
```

- 每个 endpoint 必须实现幂等语义；Worker 只保证 DB 领取与状态转移，
  不替渠道服务解决重复请求。

## 失败分类与重试

| 失败 | 处理 |
|---|---|
| 超时 / 网络错误 / 408,409,425,429,500,502,503,504 | 临时失败：指数退避（封顶 300s），`attempts < max_attempts` 时重试 |
| 其他 HTTP 状态（如 400/401） | 永久失败 → dead |
| 未注册的 event_type | 直接 dead（`unsupported_event_type`） |
| 投递中租约丢失（lease_lost） | 转临时失败，防止多副本重复投递 |

## 并发安全

- `claim_outbox`：`FOR UPDATE SKIP LOCKED`，多副本不重复领取。
- 投递期间后台任务按 `lease_seconds/3` 续约；投递结束检查续约是否丢失。
- 领取行记录 `lease_recovered`，指标 `outbox_lease_recovered_total` 可观测。

## 死信与重放

- dead 事件可通过管理接口列出并重放（`POST /integrations/outbox/replay`）。
- 循环检测 `check_outbox_backlog`：`pending`（可投递）与 `dead` 计数，
  查询失败时记录 `outbox_backlog_check_errors_total`，**不假设 dead=0**
  （避免故障期间指标假 0 掩盖真实死信）。

## 可观测

- 指标：`outbox_claimed/delivered/retried/dead/lease_recovered_total`，
  `outbox_dead_present_total`（写 worker_metrics 表，API /metrics 聚合）。
- 心跳：`worker_heartbeats(outbox)` 每轮刷新，/readyz 门禁。
- 结构化日志：`outbox_round_failed` / `outbox_delivery` 等，不含正文与密钥。
