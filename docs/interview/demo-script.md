# 面试演示脚本（10 分钟固定版）

> 定位：面向中小企业内部 IT 服务台的多租户工单自动处置系统骨架，重点展示
> **确定性工作流、异步可靠处理、知识检索、安全隔离、故障恢复设计**。
> 全部证据来自本地 Docker + 自动化测试 + 内部基准评测，不夸大。

## 时间分配（严格 10 分钟）

| 分钟 | 内容 | 要点 |
|---|---|---|
| 0–1 | 业务场景与系统定位 | 多租户工单自动处置：分类/追问/派单/SLA/知识建议；"骨架"定位，不宣称生产级 |
| 1–3 | Web 建单到派单 | 演示主线：VPN 工单 → 分类 it.vpn → 追问 → 派单 team-it → SLA |
| 3–5 | 企微追问 Resume | 客户回复「字段:值」关联原工单恢复受理，**绝不新建工单** |
| 5–7 | 202 ACK、幂等、Worker 重试/租约恢复 | 快速 ACK + 异步 Worker；崩溃恢复与死信重放的设计 |
| 7–8 | **Copilot 生成草稿** | assigned 工单 → 生成 AI 建议（异步 Worker）→ 发起人身份快照持久化 → 统一知识检索（lexical-only，向量缺失自动降级标记）→ 两层门禁 → 客服确认 |
| 8–9 | 中文检索 52 条基准 + /metrics | 98.1% Top1（内部基准，lexical-only）；hybrid holdout 集已冻结待评测；双轨指标、心跳门禁 |
| 9–10 | 边界与未完成项 | 企微沙箱待执行、hybrid 未评测、生产长期运行未证明（身份透传与检索接线已完成） |

## 必须展示的证据（按优先级）

1. **Docker 全量回归输出**：`341 passed, 3 deselected`（含测试命令与日期，可复现）。
2. **52 条评测报告**：lexical-only Top1 98.1%（51/52）、Recall@5 100%、MRR@5 0.990，
   `it.account` 83.3% 及成因说明；hybrid holdout 集已冻结（`eval_holdout_cases.py`），
   CLI 阈值参数已实现但**尚未接入 CI**、**未配置真实 embedding，未执行**。
3. **Worker 依赖故障注入测试**：`tests/test_worker_fault_isolation.py`
   （替身注入：指标写入失败仍 committed、心跳失败不退出、claim 故障不终止循环）。
4. **Copilot 异步 Worker 证据**：`tests/test_copilot_worker.py`（领取/完成/退避/dead/
   崩溃恢复）、POST 202 入队 + GET 状态轮询（`tests/test_copilot_api.py`）、
   `tests/test_copilot_identity_postgres.py`（发起人身份快照持久化 / 部门 ACL 隔离 /
   身份缺失闭锁）、`run_copilot_worker.py` 独立进程入口。
5. **企微自动化验收**：`tests/test_wecom_resume_postgres.py` / `test_channel_adapters.py`
   （验签/解密/幂等/Resume 的自动化用例，非真实沙箱）。
6. **关键 commit SHA**：`git log --oneline -8`（身份快照 / 检索接线 / worker 租约与死信 / 前端徽标 / 面试材料五个提交）。
7. **/metrics 与 /readyz 示例**：worker 心跳 ok、outbox 门禁、`worker_loop_errors_total`、
   `copilot_runs_total` / `copilot_tool_calls_total`。

## 演示主线速查（Web 建单 → 派单 → Resume → 关闭）

```powershell
# 0) 环境（演示前就绪）
docker compose -f infra/compose.test.yml up -d --wait
$env:TEST_DATABASE_URL="postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55436/langgraph"
$env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:REDIS_URL="redis://127.0.0.1:56379/0"
$env:KNOWLEDGE_EMBEDDING_DIMENSION="1536"
.\.venv\Scripts\python.exe -m backend.migrations
.\.venv\Scripts\python.exe -m backend.vector_migrations
.\.venv\Scripts\python.exe -m backend.seed_demo

# 1) 提交入站事件 → 立即 202 ACK（演示异步）
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/integrations/wecom/events" `
  -ContentType "application/json" `
  -Body '{"tenant_id":"demo","external_event_id":"demo-1","requester_id":"alice","title":"VPN 无法连接","content":"VPN 无法连接，错误码 809"}'

# 2) Inbound Worker 异步建单 → 追问澄清 → Outbox 投递
.\.venv\Scripts\python.exe -m backend.run_inbound_worker --poll-interval 0.5
.\.venv\Scripts\python.exe -m backend.run_outbox_worker --poll-interval 0.5

# 3) 客户回复「字段:值」→ Resume 原工单（不新建）
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/integrations/wecom/events" `
  -ContentType "application/json" `
  -Body '{"tenant_id":"demo","external_event_id":"demo-2","requester_id":"alice","content":"device: laptop-001"}'

# 4) 客服工作台：接单 → 处理 → 解决 → 确认 → 关闭（状态流水可查）
```

检查点：事件 `received → processing → committed`；工单 `awaiting_customer → classified
→ assigned → in_progress → resolved → closed`；澄清消息含工单编号与有效期。

## 故障隔离加分演示（时间允许时，替代第 8 分钟）

- 停止 PostgreSQL：Worker 日志出现 `worker_round_failed` 后**退避继续不退出**，
  恢复后自动续跑；`/readyz` 503（指标与业务同库语义）。
- 注意：这是"依赖不可用"演示，与测试里的替身注入（ExplodingMetrics）分开讲。

## 明确不展示为已完成

- ❌ 真实企微沙箱端到端（仅自动化测试 + 步骤手册就绪）。
- ❌ 真实 embedding 服务的 hybrid 评测（未配置/未执行）。
- ❌ 生产 SLA 达成或长期稳定性（未证明）。
- ❌ 独立指标系统容灾（当前与业务同库，故障即整体 503）。

## 对外统一表述（背熟）

> 已完成本地 Docker、自动化测试和内部基准验证；真实企微端到端及生产长期运行
> 能力尚未证明。检索指标来自内部 52 条基准集，不等同真实企业知识库泛化结果。
