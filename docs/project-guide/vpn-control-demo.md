# VPN 控制面演示说明

> 冻结基线：`vpn-control-v1.0` · 文档更新：2026-09-07

## 一句话介绍

这是一个面向约 50 人中小企业的 VPN 配置变更安全执行与对账系统。它把 AI 限制在分类和建议边界，把租户校验、审批、状态迁移、异步任务、幂等和人工接管交给确定性服务。

## 8 分钟演示

1. 展示架构：Web/API → 状态与审批服务 → PostgreSQL → Reconciliation Worker → FMG Gateway。
2. 在仓库根目录执行 `./scripts/demo.ps1`；它会构建并启动本地 Compose 环境、等待 `/readyz`，并默认运行八步 Fake FMG 演练。打开工作台后选择固定演示身份登录。
3. 说明 `/sys/status` 是只读探针，install 前必须先 preview。
4. 展示成功任务：install 返回 task id，worker 轮询至 completed。
5. 展示超时任务：保留 task id，状态进入未知/待对账，不自动重发。
6. 展示业务拒绝：FMG 错误映射为可审计失败。
7. 展示 `confirm-submission`：人工确认 `submitted` 时只读查询旧 task，`not_submitted` 收敛为失败。
8. 展示工单升级入口 `POST /tickets/{ticket_id}/vpn/redeploy-request`：仅处理中的 `it.vpn` 工单可创建审批，仍先 preview、再审批、最后对账；跨租户工单对调用方隐藏为 `404`。
9. 最后说明 Fake FMG 只证明客户端协议和失败处置，真实 FMG、FortiGate 和生产写入仍未验证。

## 设计重点

### 为什么不用 Supervisor 多 Agent 做生产写入

写入链路的核心问题是授权、状态一致性、重复副作用和异常恢复，不是自然语言规划。Agent 可以帮助分类或生成建议，但不能直接决定 install；确定性状态机负责 preview、审批、task 和对账。

### 如何处理 exactly-once

FMG 没有本地请求级幂等键，因此不能宣称绝对 exactly-once。系统用租户级唯一约束、旧 task 只读续查和 `submission_unknown` 人工确认降低重复风险；在 task id 未确认时禁止自动重发。

### 当前未验证边界

- 未验证真实 FortiManager staging 的 ADOM、Model Device 和 Policy Package 映射。
- 未验证真实 FortiGate 最终下发效果、回滚和长期运行。
- 本地 Fake FMG、PostgreSQL 集成测试和单元测试不能替代生产证据。

## 证据入口

- 一键启动与演练：`scripts/demo.ps1`
- 仅运行演练：`scripts/drill-fmg.ps1`（容器内执行 `drill_fake_fmg.py`）
- 本地夹具与演示证书：`tools/fake-fmg/`。其中私钥是固定公开的演示材料，只能用于该离线演练。
- 产品边界：`docs/product/vpn-v1-scope.md`
- 协议事实：外部实施资料《Fortinet-FortiManager控制面写入协议》（未纳入仓库）；真实接入前仍须以目标版本的厂商文档和 staging 实测为准。
- 状态与测试：`docs/evaluation/vpn-acceptance-checklist.md`
