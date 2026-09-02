# LangGraph 内部 IT 服务台 V1 —— 验证报告

> 生成日期：2026-09-12 · 评测版本：`ticket_v1` / `2026-09-12-v1`
> 一句话结论：**确定性受理链路（分类→补字段→SLA→派单→门禁建议）在固定 90 条评测集上全部通过；企业微信真实闭环与生产级引用检索标记为“待验证”。**

## 指标总览

| 指标 | V1 门槛 | 实测 | 口径 / 证据 |
| --- | --- | --- | --- |
| 分类 Top1 | ≥ 90% | **100%**（90/90） | 固定评测集 `backend/knowledge/ticket_eval_cases.py`；分类器为确定性关键词基线，LLM 自动分类未评测 |
| 分类 Top1 · VPN | ≥ 90% | 100%（30/30） | 同上 |
| 分类 Top1 · 账号/权限 | ≥ 90% | 100%（20/20） | 同上 |
| 分类 Top1 · 网络 | ≥ 90% | 100%（20/20） | 同上 |
| 字段补全成功率 | ≥ 95% | **100%** | 字段识别匹配率 100%（90/90）；初始提交完整率 90%（81/90），剩余 10 条进入字段追问，图中断/恢复单测通过，补全后全部可继续 |
| 自动草稿引用支撑率 | 100% | **100%**（静态口径） | `run_ticket_eval.py` static 模式按“预期知识文档存在 + 门禁规则”计 100%；真实词法/向量检索未执行，见限制 8b |
| ACL 越权 | 0 | **0** | 评测集 ACL 泄露 0/5；部门/租户/过期/未发布引用拒绝测试通过 |
| 端到端成功率 | ≥ 90% | **100%**（20/20） | `tests/test_ticket_api.py::test_full_lifecycle_http_regression_vpn` 本机连续 20 次全部通过 |
| P95 | 记录基线 | **N/A（未建立服务基线）** | 确定性分类器本地耗时 <1ms，不代表 HTTP/服务端 P95；后续版本须先补真实基线再比较劣化 ≤20% |
| 失败重试率 | — | N/A | 无生产失败数据；集成测试覆盖 SLA 失败幂等重试、工作流意图恢复、Outbox 死信重放 |
| 成本 | 接入真实单价后才量化 | **N/A** | 未配置真实模型价格，不报告虚假单工单成本 |

## 评测集构成（90 条）

| 场景 | 条数 | 说明 |
| --- | --- | --- |
| VPN | 30 | 报错 769/809、转圈、掉线、证书、认证、IP、远程桌面等 |
| 账号/权限 | 20 | SSO、密码、锁定、MFA、权限申请、离职回收等 |
| 网络 | 20 | 断网、Wi-Fi、DNS/IP、网关、延迟、内网/外网等 |
| 字段缺失 | 10 | 故意缺少必填字段，验证追问与补全 |
| 无知识依据 | 5 | 只转人工、不生成无依据答复 |
| ACL 越权 | 5 | 只允许 finance 部门访问的文档，it 部门不得命中 |

报告 JSON：`docs/evaluation/ticket-eval-report.json`（`uv run python -m backend.run_ticket_eval --json docs/evaluation/ticket-eval-report.json`，可重复生成）。

## 已通过的关键验证

1. 越界分类不自动处置：finance/admin/product/other → `team-service-desk` + `out_of_scope_manual_review`（`test_helpdesk_intake_graph.py`）。
2. 身份上下文注入：`tenant_id / user_id / departments / asset_id` 从认证主体/渠道事件进入受理图；缺失身份 → 空部门 + internal=False + `identity_missing` 转人工（`test_helpdesk_intake_graph.py` / `test_ticket_api.py`）。
3. 三种建议状态：`draft_ready / handoff_no_evidence / handoff_high_risk` 由门禁原因派生，前端详情页展示状态、原因与引用（`test_knowledge_service.py` + TicketDetail）。
4. 引用权威校验：空引用、草稿/停用、过期、跨租户、跨部门、chunk/版本不符均拒绝（`test_knowledge_citations_postgres.py`）。
5. 生命周期回归：创建 → 受理追问 → 补充恢复 → 分类 → SLA → 派单 → 接单 → 处理 → 解决 → 回访 → 关闭，连续 20 次通过。

## 明确未验证（不写“已完成”）

- **企业微信沙箱真实端到端**：自动测试覆盖验签/解密/幂等/追问/Resume；真实自建应用回调、真实消息收发、真实截图证据待执行（见 `docs/CHANNEL_SANDBOX.md`、`docs/interview/known-limitations.md` 第 7 条）。
- **真实引用检索**：`reference_support_rate=100%` 为静态门禁口径；`TEST_DATABASE_URL` 接入后 `run_ticket_eval.py --database-url` 的 db 模式尚未在本次环境执行。
- **演示视频**：VPN 主线 3–5 分钟与账号/网络 30 秒片段待录制（脚本见 `docs/DEMO_SCRIPT.md`）。
- **生产长期运行 / P95 / 成本**：未在真实流量或真实模型单价下测量。

## 口径一致性

本报告与 `docs/product/v1-scope.md`、`docs/DEMO_SCRIPT.md`、`docs/interview/known-limitations.md` 保持一致；任何“已完成”表述均指上述已验证范围，未验证项一律标“待验证/N/A”。
