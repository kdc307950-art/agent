# LangGraph 内部 IT 服务台 V1 —— 验证报告（真实数据库口径）

> 生成日期：2026-09-03（命令输出：date +%F） · 版本标识：`2026-09-12-v1`（目标发布日期，计划）
> 本报告只记录 **PostgreSQL 真实检索评测** 结果；static 模式的引用/ACL 指标一律 N/A，不进入报告。

## 当前状态

- **本地环境未配置 `TEST_DATABASE_URL`，真实数据库评测未运行**。
- 以下指标待 PostgreSQL 环境下执行（与 CI 步骤相同）后填写；
  在未执行前，本报告不把任何 static 数字当作“已验证”。

## 指标（仅真实数据库评测）

| 指标 | V1 门槛 | 实测 | 状态 |
| --- | --- | --- | --- |
| 分类 Top1（总体 / VPN / 账号 / 网络） | ≥ 90% | N/A | 待 db 模式执行 |
| 字段补全成功率 | ≥ 95% | N/A | 待 db 模式执行 |
| 自动草稿引用支撑率 | 100% | N/A | 只在 db 模式计算 |
| ACL 越权 | 0 | N/A | 只在 db 模式计算 |
| 端到端成功率（真实仓储生命周期） | ≥ 90% | N/A | `test_ticket_lifecycle_postgres.py` 待 CI/本地 DB 执行 |
| P95 | 记录基线 | N/A | 未建立服务端基线 |
| 失败重试率 | — | N/A | 无生产失败数据 |
| 成本 | 接入真实单价后才量化 | N/A | 未配置真实模型价格 |

## 如何生成可入报告的真实 JSON

```powershell
# 1) PostgreSQL + Redis 就绪（infra/compose.test.yml 或 CI service containers）
docker compose -f infra/compose.test.yml up -d --wait
$env:TEST_DATABASE_URL="postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55436/langgraph"
$env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:REDIS_URL="redis://127.0.0.1:56379/0"

# 2) 迁移 + 种子（幂等）
uv run python -m backend.migrations
uv run python -m backend.seed_demo

# 3) 90 条评测（引用/ACL 为真实检索；不达标则非零退出）
uv run python -m backend.run_ticket_eval --database-url $env:TEST_DATABASE_URL --require-db --fail-under-classification 0.9 --fail-under-field-rate 0.95 --fail-under-reference 1.0 --max-acl-leaks 0

# 4) 真实仓储生命周期测试
uv run pytest tests/test_ticket_lifecycle_postgres.py -q
```

`docs/evaluation/ticket-eval-report.json` 只由上述 db 模式命令生成；
检测到 `knowledge.mode == "static"` 时视为未达标（`--require-db` 会直接拒绝）。

## 已通过的非数据库验证（辅助，不代替数据库评测）

- 单元/路由回归：越界分类不自动处置、渠道身份伪造无效、AI 三状态门禁、Fake runtime HTTP 生命周期。
- `test_ticket_api.py::test_full_lifecycle_http_regression_vpn` 为 **HTTP 路由回归（Fake runtime）**，
  不再称呼为“真实端到端”；真实端到端以 `test_ticket_lifecycle_postgres.py` 为准。
- 前端 30 单测 + 20 Playwright（Mock）+ 可选的 real-api 冒烟（需 `E2E_WEB_BASE` / `E2E_API_TOKEN`）。

## 未验证（明确不写“已完成”）

- 真实企业微信自建应用回调与消息收发（自动化验签/解密/幂等已测，真实沙箱待执行）。
- 真实模型生成建议与 P95/成本（无真实模型单价与生产流量基线）。
- 演示视频。
