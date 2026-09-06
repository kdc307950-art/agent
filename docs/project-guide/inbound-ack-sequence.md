# 入站快速 ACK 与时序

> 目标：渠道回调在毫秒级返回，建单等重活异步执行；崩溃不丢单、不重复建单。

## 时序（企微文本消息为例）

```
客户 ──▶ 企微 ──▶ POST /integrations/wecom/webhook（加密 XML + SHA-1 签名）
  │
  ▼
WebhookAdapter：验签 → 解密 → 归一化为 NormalizedChannelEvent
  │  （校验 URL 验证 echostr；enter_agent/location 等非文本事件验签后 200 忽略）
  ▼
POST /integrations/{channel}/events：幂等登记 inbound_events（status=received）
  │  （同 external_event_id 重复登记不重复插入）
  ▼
立即返回 202 {"accepted": true, "event_id": ...}        ◀── 快速 ACK
  │
  ▼（独立进程）
inbound_worker：claim（FOR UPDATE SKIP LOCKED, attempts+1, status=processing）
  ▼
process_inbound_event：
  1. 企微回复优先匹配 (tenant, channel, external_user_id) 唯一 awaiting 追问 → Resume 原工单
  2. 否则幂等建单（事件已关联工单则复用，恢复受理）
  3. 受理图（LangGraph）：分类 → 缺失字段追问 → 运营路由 → 派单 → SLA → 澄清 Outbox
  4. transition_many 原子应用命令（乐观锁 expected_version）
  ▼
complete_inbound_event（status=committed）
```

调用方通过 `GET /integrations/events/{event_id}`（权限 `ticket:channel`）
轮询 `received → processing → committed` 并取得 `ticket_id`。

## 崩溃恢复点（why 不丢单）

| 崩溃时刻 | 恢复机制 |
|---|---|
| 登记后、建单前 | 事件保持 received，下轮被领取重试 |
| 建单后、受理前 | 事件已关联 ticket_id，恢复时复用该工单继续受理（幂等） |
| 检查点落库后、业务命令提交前 | `workflow_operations` 记录 intent，recovery worker 重放命令 |
| 受理完成、complete 前 | 事件 processing 租约过期后重新领取，attempts+1 重试（幂等建单保证不重复） |
| 失败超限 | 进入 dead，人工/脚本 replay 后重试 |

## 关键不变式

1. **幂等**：`(tenant, channel, external_event_id)` 唯一 + 工单关联，任意重试不重复建单。
2. **租约**：领取即写租约，崩溃后租约过期才可被其他副本领取（`lease_recovered` 计数）。
3. **业务提交唯一性**：检查点（LangGraph）与业务命令（tickets）分裂时，
   recovery worker 以 `operation_id` 幂等重放。
4. **异步只改"谁在什么时候调用"**：HTTP 路由与 Worker 共用同一份业务逻辑
   （`backend/channel_processor.py`），避免双份实现漂移。
