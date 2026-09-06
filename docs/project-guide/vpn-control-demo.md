# VPN 控制面演示说明

> 冻结基线：`vpn-control-v1.0` · 2026-09-05

## 一句话介绍

这是一个面向约 50 人中小企业的 VPN 配置变更安全执行与对账系统。它把 AI 限制在分类和建议边界，把租户校验、审批、状态迁移、异步任务、幂等和人工接管交给确定性服务。

## 8 分钟演示

1. 展示架构：Web/API → 状态与审批服务 → PostgreSQL → Reconciliation Worker → FMG Gateway。
2. 启动 `D:\fmg-vm\fake_fmg_server.py`，运行仓库根目录 `drill_fake_fmg.py`。
3. 说明 `/sys/status` 是只读探针，install 前必须先 preview。
4. 展示成功任务：install 返回 task id，worker 轮询至 completed。
5. 展示超时任务：保留 task id，状态进入未知/待对账，不自动重发。
6. 展示业务拒绝：FMG 错误映射为可审计失败。
7. 展示 `confirm-submission`：人工确认 `submitted` 时只读查询旧 task，`not_submitted` 收敛为失败。
8. 展示跨租户请求返回 `403`，最后说明 Fake FMG 只证明客户端协议和失败处置，真实 FMG、FortiGate 和生产写入仍未验证。

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

- 演练脚本：`drill_fake_fmg.py`
- 产品边界：`docs/product/vpn-v1-scope.md`
- 协议事实：桌面文件 `Fortinet-FortiManager控制面写入协议.md`
- 状态与测试：`docs/evaluation/vpn-acceptance-checklist.md`
