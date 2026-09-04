# legacy-demo（旧版通用 Supervisor Demo）

> **状态：LEGACY（遗留）/ 不维护，仅供参考。** 本目录是早期「通用 Supervisor Demo」的遗留实现，
> 与当前 V1 产品基线（`docs/product/vpn-v1-scope.md`，`vpn-v1`）**无关**，不属于生产 IT 服务台
> 或 VPN 受理闭环的任何能力，也**不参与**版本冻结与 CI 验收。

## 说明

- 入口：`legacy-demo/main.py`（单 Agent 命令行演示）、`main_supervisor.py`（Supervisor 编排演示）、
  `main_workflow.py`（Workflow 演示）、`workflows/legacy-demo.json`。
- 特点：不经过鉴权、审计、限流与预算，属于早期一次性演示脚本。生产入口统一为 `backend/app.py`。
- 为什么保留：**不删除**本目录，保留结构作为「旧版通用 Supervisor Demo」的代码与文档参照痕迹；
  如需彻底归档，可整体移除 `legacy-demo/` 并清理由其引用的文档。

## 与 V1 的关系

- V1 产品基线：`vpn-v1`（VPN 受理与建议闭环，仅 `it.vpn` 主线），权威范围见
  `docs/product/vpn-v1-scope.md`。
- 本目录**不**属于 `vpn-v1` / `vpn-v2` 版本口径，测试与文档一律不引用 `legacy-demo` 作为现状证明。

> 本文件为 release-engineer 在「版本冻结与质量收口」阶段新增的 legacy 标记说明；未改动任何
> `legacy-demo/` 下原有代码文件。
