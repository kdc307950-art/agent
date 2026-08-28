# 渠道沙箱验证：企业微信（WeCom）真实闭环

> 阶段四验收项：渠道端可完成一次真实闭环。本机/CI 的加解密、验签、幂等、追问
> 均有自动化测试（`tests/test_channel_adapters.py`、`tests/test_ticket_api.py` 的
> `test_wecom_webhook_*` / `test_dingtalk_webhook_*`）；本文档描述在**真实企业微信
> 沙箱**（自建应用）上的端到端验证步骤。

## 选型

先接**企业微信**：回调为「加密 XML + SHA-1 签名」，与本仓库 `WeComWebhookAdapter`
的验签/解密实现一一对应；钉钉随后再接（适配器与测试已就绪）。

## 前置条件

1. 企业微信管理后台 → 应用管理 → 创建**自建应用**，记录：
   - CorpID（企业 ID）
   - AgentId + Secret（回调验签不需要 AgentId，但发消息需要）
2. 应用 → 接收消息 → 设置回调 URL（需公网可达，可用内网穿透工具）：
   - URL：`https://your-domain/integrations/wecom/webhook`
   - Token：任意随机串（对应 `WECOM_TOKEN`）
   - EncodingAESKey：管理后台生成（43 字符，对应 `WECOM_ENCODING_AES_KEY`）
3. 配置环境变量（`AUTH_MODE=dev` 即可，webhook 不依赖租户令牌）：

   ```powershell
   $env:WECOM_TENANT_ID="demo"                       # 回调消息归属的租户
   $env:WECOM_TOKEN="<回调 Token>"
   $env:WECOM_ENCODING_AES_KEY="<EncodingAESKey>"
   $env:WECOM_CORP_ID="<CorpID>"
   ```

## 验证矩阵

| # | 场景 | 操作 | 预期 |
|---|---|---|---|
| 1 | 回调 URL 验证（GET echostr） | 管理后台「保存」回调设置，微信发 GET（msg_signature/timestamp/nonce/echostr） | 返回 200 + 明文 echostr（`text/plain`），后台提示配置成功 |
| 2 | 验签失败 | 篡改 `msg_signature` 或过期 `timestamp` 重放 | 响应 401 `WebhookVerificationError`；不入库、不建单 |
| 3 | 文本建单 | 员工在企业微信向应用发「VPN 无法连接」 | 返回 200；`tickets` 新增工单（channel=wecom，requester=员工外部 ID） |
| 4 | 幂等建单 | 同一消息由微信重试推送/手动重放相同 `MsgId` | 第二次返回 `created: false`，不产生新工单（`inbound_events` 幂等键） |
| 5 | 自动分类 | 查看工单 category | 识别为 `it.vpn`（关键词分类；真实准确率依赖模型，关键词基线为确定性兜底） |
| 6 | 必填字段追问 | 工单进入受理后缺少 device / error_message | 工单 `awaiting_customer`；`outbox_events` 出现澄清消息，worker 投递到回调端点 |
| 7 | Outbox 回调 | 运行 `run_outbox_worker` 观察投递 | 回调端点收到 `X-Idempotency-Key`；重复投递被渠道侧去重 |
| 8 | 门禁转人工 | 内容无检索证据 / 含高风险词 | 不自动发送建议；工单进人工队列（`reason_codes` 含 `missing_citations` / `sensitive_or_high_risk`） |

> **GET 验证**：`GET /integrations/wecom/webhook` 只验签 + 解密 echostr 并原样回显（`text/plain`），不建单、不访问业务表。失败统一 401，未配置返回 503。若后台提示「URL 未通过安全校验」，优先检查 Token / EncodingAESKey / CorpID 是否与服务端一致、服务器时间偏差是否超过 `WEBHOOK_REPLAY_WINDOW_SECONDS`（默认 300 秒）。

## 观察命令

```powershell
# 启动依赖与应用
docker compose -f infra/compose.demo.yml up -d
uv run python -m backend.seed_demo

# 观察建单与幂等
uv run python -c "import asyncio,os; from backend.tickets import TicketRepository; \
r=asyncio.run(TicketRepository.connect(os.environ['DATABASE_URL'])); \
print(asyncio.run(r.list_tickets('demo', requester_id='ext-user-1')))"

# 观察澄清消息与 Outbox
uv run python -m backend.run_outbox_worker --poll-interval 2 --batch-size 20
# 回调端点侧应收到 ticket_message.send 事件
```

## 验收检查点

- [ ] 回调 URL 校验通过（echostr 回显）
- [ ] 坏签名/过期时间戳被拒（401）
- [ ] 员工消息可建单，channel=wecom
- [ ] 同一 `MsgId` 重放不重复建单
- [ ] 缺字段工单进入 `awaiting_customer` 并产生澄清 Outbox 事件
- [ ] Outbox worker 成功投递回调且幂等键生效
- [ ] 无引用/高风险内容不自动发送，转人工
