# VPN 质量基线快照

> 更新时间：2026-09-05。当前产品冻结基线：`vpn-control-v1.0`；维护清理版本：
> `vpn-control-v1.0.1`。本文只记录当前已验证结果，不把模拟环境或静态检查包装成真实厂商生产证据。

## 1. 已验证结果

| 检查 | 结果 | 口径 |
| --- | --- | --- |
| Ruff | 通过 | `backend src tests` 无错误 |
| Mypy | 通过 | 126 个源文件，0 errors |
| 后端 pytest | 797 passed / 1 skipped / 3 deselected | `not live_e2e` 全量回归 |
| 前端 TypeScript | 通过 | `tsc -b` |
| 前端 Oxlint | 通过 | 0 warning / 0 error |
| 前端 Vitest | 38 passed | 单元测试 |
| Playwright Mock E2E | 22 passed | 固定 Mock 环境 |
| Fake FMG 演练 | ALL PASS | 8 步，含成功、拒绝、停滞和人工确认 |

## 2. 当前产品边界

- 已验证：确定性状态机、审批、preview/diff hash 门禁、异步 task 轮询、失败分类、人工确认、
  补偿对账、跨租户隔离和 Fake FMG 协议往返。
- 未验证：真实 FortiManager staging、真实 FortiGate 最终下发、生产长期运行、生产回滚、
  真实企业微信/钉钉闭环、真实模型线上指标和成本。
- 生产写入边界：不恢复 Supervisor 多 Agent 作为写入链路；生产动作仍由确定性流程、审批、
  幂等约束和人工对账控制。

## 3. Git 基线

- `cd2b9a1`：冻结 `vpn-control-v1.0`。
- `afed74d`：工作区清理与审计记录维护，标记 `vpn-control-v1.0.1`。
- 当前文档修正完成后，将追加一个文档维护提交；代码冻结基线不变。

## 4. 维护规则

- V1 维护期只接受 Bug 修复、安全修复、依赖升级和证据/文档同步。
- 业务范围、状态机语义、外部写入协议或生产放量策略变更，应进入新版本评审。
- 对外说明只使用“已验证”栏目中的事实；真实厂商链路必须在完成 staging 证据后单独声明。
