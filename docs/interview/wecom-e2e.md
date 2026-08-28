# 企微闭环（WeCom E2E）

> 企业微信自建应用：加密 XML 回调 + SHA-1 签名；文本建单、追问、客户回复 Resume
> 原工单（绝不新建）、SLA/派单恢复。钉钉适配器与测试就绪，作为后续扩展。

## 闭环场景（A/B/C）

**A. 文本建单 → 追问 → 客户补充 → 恢复受理**

```
员工发文本 → 回调验签/解密 → 202 ACK → InboundWorker 建单 → 受理图分类
  → it.vpn 策略缺必填字段 → 登记 pending（唯一索引）+ 澄清消息进 Outbox
  → 客户回复 "device: laptop-001" → 匹配 (tenant, channel, external_user_id) awaiting
  → Resume 原工单（apply_intake_resume）→ 分类/SLA/派单 → 状态流水
    customer_reply_received → intake_resumed → classified → assigned
```

**B. 同 MsgId 重复回调** → 幂等：不重复建单，返回既有事件状态。

**C. 非文本事件（enter_agent/location）** → 验签通过返回 200 忽略，不登记不建单。

## 追问消息（本次收敛固化）

- 内容携带：工单编号、需补充字段、有效期（本地时区，`YYYY-MM-DD HH:MM`）与
  「字段:值」回复格式示例。
- 产品边界：同一租户/渠道/外部用户**只保留一个 awaiting 追问**，新追问自动取消
  旧追问（partial unique index + upsert）；过期由 recovery worker 翻转 expired。
- 暂不实现：多工单选择、模糊匹配、短链接系统（无真实用户需求前不加）。

## 客户回复解析

- `_FIELD_RE`：`字段[:：=]值` 逐行解析；无法解析 → 不 Resume，
  按普通新消息处理并提示格式（`unparsable_reply` 计数）。
- 已关闭/已取消/非等待补充的工单：不得 Resume，按新消息处理。

## 验证状态（诚实口径）

- **自动化测试已覆盖**（PostgreSQL 集成测试）：
  - `test_channel_adapters.py`：验签/解密/幂等登记/事件归一化。
  - `test_ticket_api.py`：`test_wecom_webhook_*` / `test_dingtalk_webhook_*`。
  - `test_wecom_resume_postgres.py`：Resume 闭环、唯一追问、过期翻转。
- **真实企微沙箱端到端**：步骤手册就绪（`docs/CHANNEL_SANDBOX.md`，
  注册→自建应用→回调配置→闭环验收矩阵），**尚未执行**。
  对外表述不得宣称"已完成企微沙箱验证"。

## 安全

- 回调验签（SHA-1 排序拼接）+ AES 解密（EncodingAESKey），失败返回 4xx 不登记。
- 日志禁止记录加密 XML、Token、密钥、用户正文。
- 事件查询与重放均需权限（`ticket:channel`）。
