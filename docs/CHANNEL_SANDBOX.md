# 渠道沙箱验证：企业微信（WeCom）真实闭环

> 阶段四验收项：渠道端可完成一次真实闭环。本机/CI 的加解密、验签、幂等、追问
> 均有自动化测试（`tests/test_channel_adapters.py`、`tests/test_ticket_api.py` 的
> `test_wecom_webhook_*` / `test_dingtalk_webhook_*`）；本文档描述在**真实企业微信
> 沙箱**（自建应用）上的端到端验证步骤。

## 选型

先接**企业微信**：回调为「加密 XML + SHA-1 签名」，与本仓库 `WeComWebhookAdapter`
的验签/解密实现一一对应；钉钉随后再接（适配器与测试已就绪）。

## 从零开始：完整准备步骤（约 30 分钟）

### Step 1：注册企业微信

1. 打开 https://work.weixin.qq.com/ →「企业注册」。
2. 用**个人微信扫码**，按提示填企业名称（个人开发者可自拟，如「XX 工作室」）、行业、规模，注册免费版即可，无需付费。
3. 注册完成后进入管理后台：https://work.weixin.qq.com/wework_admin/frame

### Step 2：创建自建应用

1. 管理后台 → **应用管理** →「自建」→「创建应用」。
2. 填应用名称（如 `IT 服务台沙箱`）、Logo，**可见范围**选择「全部成员」（否则发消息的人不在可见范围，消息不会进回调）。
3. 创建后进入应用详情，记下两个值：
   - **AgentId**（应用 ID）
   - **Secret**（应用密钥）——点「查看」并复制
4. 管理后台 →「我的企业」→「企业信息」，记下 **CorpID**（企业 ID）。

### Step 3：准备回调凭据（Token / EncodingAESKey）

1. 在应用详情页 →「接收消息」→「设置 API 接收」。
2. **先不要点保存**（保存会立即触发 GET 验证，需后端已就绪，见 Step 7）。
3. 准备好三个值：
   - **URL**：`https://<你的公网域名>/integrations/wecom/webhook`（公网域名见 Step 4）
   - **Token**：随机串，例如 PowerShell 生成：
     ```powershell
     -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 24 | ForEach-Object {[char]$_})
     ```
   - **EncodingAESKey**：页面上的「随机生成」按钮（43 个字符）
4. 把这三个值 + CorpID 填进后端环境变量（Step 6），**Token 和 EncodingAESKey 只在两端各存一份**。

### Step 4：开通公网回调地址（内网穿透）

后端默认监听 `127.0.0.1:8000`，企业微信需要公网 HTTPS。推荐 **Cloudflare Tunnel 快速版**（免费、无需注册）：

```powershell
# 下载 cloudflared（Windows）后执行：
cloudflared tunnel --url http://127.0.0.1:8000
```

输出会给出一个 `https://<随机名>.trycloudflare.com`，把回调 URL 填为：

```
https://<随机名>.trycloudflare.com/integrations/wecom/webhook
```

备选：**ngrok**（需注册）`ngrok http 8000` → `https://xxx.ngrok-free.app`；或自备服务器用 frp。

> ⚠️ 临时隧道域名每次重启会变化：**保存回调 URL 成功后不要再重启穿透进程**。
> 若必须固定域名，用 Cloudflare 命名隧道 / ngrok 固定子域名。

### Step 5：启动后端并注入环境变量

```powershell
# 方式 A：Docker demo 栈（含 postgres/redis/migrate/agent，暴露 127.0.0.1:8000）
docker compose -f infra/compose.demo.yml up --build -d

# 方式 B：本地后端（需自己起 Postgres/Redis）
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --loop backend.uvicorn_loop:selector_event_loop_factory

# 注入回调凭据（webhook 不依赖租户令牌，AUTH_MODE=dev 即可）
$env:WECOM_TENANT_ID="demo"
$env:WECOM_TOKEN="<Step 3 的 Token>"
$env:WECOM_ENCODING_AES_KEY="<Step 3 的 EncodingAESKey>"
$env:WECOM_CORP_ID="<Step 2 的 CorpID>"
$env:WEBHOOK_REPLAY_WINDOW_SECONDS="300"
```

> 环境变量必须在启动进程前设置（或写入 `.env` 后重启服务）。确认四个值**同时存在**，
> 只配一部分时回调端点返回 503。

### Step 6：导入演示数据（可选，便于验证追问/派单）

```powershell
uv run python -m backend.seed_demo --tenant demo
```

### Step 7：保存回调 URL（触发 GET echostr 验证）

1. 回到 Step 3 的「设置 API 接收」页面，填入 URL / Token / EncodingAESKey，点「保存」。
2. 企业微信向 `GET /integrations/wecom/webhook?msg_signature=&timestamp=&nonce=&echostr=` 发起验证。
3. 预期：页面提示**「保存成功」**；后端日志无异常。若失败见 [排查清单](#排查清单)。

### Step 8：真实消息闭环

1. 手机企业微信 App →「工作台」→ 找到刚建的应用 → 进入聊天框。
2. 发送 `VPN 无法连接，错误码 809`：
   - 后端日志出现 `POST /integrations/wecom/webhook`；
   - `tickets` 表新增工单（`channel=wecom`，`requester_id=你的企业微信外部 ID`，`category=it.vpn`）；
   - 前端客服工作台（`agent-1` 令牌）可见该工单与 SLA。
3. 立刻**原样重发同一条消息**（或后台消息重试）：第二次不产生新工单（幂等）。
4. 发送 `VPN 连不上`（缺 device/error_message）：工单进入 `awaiting_customer`，`outbox_events` 出现澄清消息。
5. 启动 Outbox worker 观察投递：
   ```powershell
   uv run python -m backend.run_outbox_worker --poll-interval 2 --batch-size 20
   ```
6. 客服完成接单 → 处理 → 解决 → 回访 → 关闭，闭环完成。

### 排查清单

| 现象 | 原因 | 处理 |
|---|---|---|
| 保存 URL 提示失败 | Token / EncodingAESKey / CorpID 不一致，或服务器时间偏差 > 300s | 核对三项配置与两端一致性；检查 `GET` 返回 401 与日志 |
| 后端返回 503 | 四个 `WECOM_*` 变量未配齐 | 补全后重启服务 |
| 返回 401 且日志「签名无效」 | Token/时间戳/密文不匹配 | 核对 Token；确认服务器时间与北京时间偏差 |
| 保存成功但发消息无回调 | 应用可见范围不含发消息者；回调 URL 已过期（临时域名重启） | 调整可见范围；重新保存 URL |
| 消息进来了但没建单 | 内容为空/事件类型不支持 | 看日志事件类型，`Content`/`Event` 兜底为空时被拒 |

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
