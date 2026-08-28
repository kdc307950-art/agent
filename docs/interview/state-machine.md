# 状态机

## 1. 工单状态机（tickets）

定义于 `src/my_agent/helpdesk/domain.py`（`TicketStatus` / `TicketAction` / 转移表），
全部转移通过 `transition_many` 以命令列表原子应用，失败回滚并抛 `TicketVersionConflict`
（乐观锁：`expected_version`）。

| 状态 | 动作 | 目标状态 |
|---|---|---|
| new | cancel | cancelled |
| intaking | queue / cancel | queued / cancelled |
| awaiting_customer | cancel | cancelled |
| classified | queue / cancel | queued / cancelled |
| answer_proposed | queue | queued |
| awaiting_customer_confirmation | confirm_resolved / report_unresolved / cancel | resolved / queued / cancelled |
| queued | assign / cancel | assigned / cancelled |
| assigned | start_work / queue / cancel | in_progress / queued / cancelled |
| in_progress | resolve / queue / cancel | resolved / queued / cancelled |
| awaiting_approval | cancel | cancelled |
| resolved | close / reopen | closed / in_progress |

动作按 Actor 类型受限（如 `confirm_resolved` 仅 CUSTOMER 可触发），
权限模型 `ActorType × 动作 → 允许集合` 独立校验。

## 2. 渠道入站事件状态机（inbound_events）

```
received ──claim──▶ processing ──complete──▶ committed
    │                    │
    │                    ├──fail(重试)──▶ failed ──claim──▶ processing
    │                    │
    │                    └──fail(超限)──▶ dead ──replay──▶ received
    └──────────────── processing（租约过期后可被再次 claim，attempts+1）
```

- claim：`FOR UPDATE SKIP LOCKED`，`received/failed` 且 `next_attempt_at <= now()`，
  或 `processing` 且租约过期；领取时 `attempts = attempts + 1`。
- 幂等：`(tenant_id, channel, external_event_id)` 唯一，重复登记不重复建单。
- 重放：dead 可 `replay_inbound_event` 重置为 received（attempts=0）。

## 3. Outbox 事件状态机（outbox_events）

```
pending ──claim──▶ (投递中, 租约续期) ──complete──▶ delivered
   │                        │
   │                        ├──临时失败──▶ pending（指数退避, retry_at）
   │                        └──永久失败/超限──▶ dead ──replay──▶ pending
```

- 投递超时（10s）与 5xx/408/409/425/429/503/504 视为临时失败；
  其余 HTTP 错误与本地异常视为永久失败进 dead。
- 幂等：投递头 `X-Idempotency-Key: {tenant}:{idempotency_key}` + HMAC 签名时间戳，
  回调端点负责去重。
- 租约：投递期间按 `lease_seconds/3` 续约，续约失败（lease_lost）转临时失败重试，
  防多副本重复投递。

## 4. 客户待补全追问状态机（ticket_customer_pending_intake）

```
awaiting ──客户回复并解析成功──▶ resumed（resume_count+1）
awaiting ──超时（recovery worker 扫描）──▶ expired
awaiting ──新追问登记（同客户不同工单）──▶ cancelled
```

- 部分唯一索引 `(tenant_id, channel, external_user_id) WHERE status='awaiting'`：
  同一客户同时只允许一个待补全追问，新追问自动取消旧追问。
- 过期由 `workflow_recovery` 周期扫描执行（`expire_pending_intakes`），
  计数指标 `pending_intake_expired_total`。
