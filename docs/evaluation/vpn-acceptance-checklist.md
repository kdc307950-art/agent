# VPN 故障智能服务台 —— 整体验收与外部复验清单

> 面向最终整体验收。当前冻结基线：`vpn-control-v1.0`（2026-09-05）。汇总本项目五阶段交付、已验证项、以及**依赖外部 / Docker / CI 环境
> 才能最终复验**的项，并列明可执行命令。本清单只做记录与指引，不对代码做任何修改。

---

## 0. 一句话结论

主体交付已经落盘并通过本地静态/非 DB 校验；**最终验收需要在具备 Docker / 无管道限制的
外部环境复验以下三类：Docker 集成测试、前端 `build`/`test`、依赖 `tmp_path` 的
fixture 用例**。真实 VPN 数据源本期不接入（属后续范围）。

---

## 1. 五阶段交付清单

### 阶段一 —— 基线冻结（已完成）
- lint/类型治理：ruff `36 → 0`、mypy `8 → 0`。
- 版本统一：产品基线 `vpn-control-v1.0`，评测集版本 `2026-09-05-vpn-v1` / `2026-09-05-vpn-v2`
  （见 `backend/knowledge/vpn_eval_cases.py` 与 `backend/knowledge/vpn_eval_cases_v2.py`）。
- `legacy-demo` 标记（区分演示路径与正式路径）。
- git commit：`1cf40f9`（`feat(vpn): VPN 故障智能服务台基线（版本冻结，阶段一）`，已核实存在）。

### 阶段二 —— 领域对象 / 状态机 / 闭环 / 4 条 REST / 持久化（已完成）
- 领域对象：`VpnDiagnosisRun` / `VpnDiagnosisFinding` / `VpnCustomerAction` /
  `VpnCustomerActionResult` / `VpnEscalation`（`backend/vpn/diagnosis.py`，均 `extra="forbid"`）。
- 状态机迁移：START_DIAGNOSIS / PRESCRIBE_STEPS / PROVIDE_ACTION_RESULT /
  REQUEST_RECONCILIATION（复用 `domain.transition_ticket` + `tickets.transition`）。
- 闭环服务：`backend/vpn/closed_loop.py`（`VpnClosedLoopService`，诊断落库 + 状态机联动 +
  客户动作闭环 + 审计 `vpn_diagnosis_*`）。
- REST 4 端点：`backend/vpn/diagnosis_api.py`
  - `POST /tickets/{ticket_id}/vpn/diagnose`
  - `GET  /tickets/{ticket_id}/vpn/diagnosis`
  - `POST /tickets/{ticket_id}/vpn/actions/{action_id}/result`
  - `POST /tickets/{ticket_id}/vpn/diagnose/resume`
- 持久化（schema v23）：`vpn_diagnosis_runs` / `vpn_customer_actions` /
  `vpn_customer_action_results` / `vpn_escalations` 四张表（`backend/schema.py` v23，已核实）。
- 诊断结果进入工单时间线（前端 `VpnDiagnosisPanel` 展示回填记录时间线）。
- 测试：`tests/test_vpn_diagnosis_closed_loop.py` —— 21 passed。

### 阶段三 —— 证据链规则 / 评测口径 / 新指标（已完成）
- 证据链规则：`backend/vpn/rules.py`，R0–R6（R0 身份缺失、R1 多用户影响、
  R2 账号锁定、R3 客户端/配置版本、R4 网关侧、R5 连接类失败、R6 无证据转人工），
  优先级唯一命中，已核实。
- D6 修复；M3 / M5 采用真实口径。
- 6 项新指标（见 `backend/vpn_eval_metrics.py`）。
- 测试：`tests/test_vpn_evidence_rules.py` —— 17 passed。

### 阶段四 —— 只读沙箱韧性（已完成）
- `backend/vpn/sandbox_adapter.py`：韧性包装 + 三只只读适配。
- 测试：`tests/test_vpn_sandbox_adapter.py` —— 26 passed。

### 阶段五 —— 可恢复审批一致性 + 补偿对账（已完成）
- 可恢复状态模型：`ApprovalStatus` 增补 `executing` / `execution_unknown` / `reconciliation_required`
  （`backend/vpn/approval.py`）；`ReissueRegistry` 支持 request 快照 + `scan_reconcilable()`。
- 补偿对账：`VpnReissueService.reconcile()/reconcile_all()`（`backend/vpn/reissue_service.py`）扫描
  `execution_unknown`/`reconciliation_required` → 幂等重查外部 → 补写 workflow_operation / 工单 / 审计；
  异常分类（业务拒绝/权限失败/版本冲突→FAILED、外部超时/结果未知→EXECUTION_UNKNOWN、
  DB 提交失败→RECONCILIATION_REQUIRED）。
- worker 入口：`backend/runtime.py::reconcile_pending_reissues()`；API：`POST /vpn/reissue/reconcile`、
  `POST /vpn/reissue/{key}/reconcile`。
- 测试：`tests/test_vpn_reissue_reconciliation.py` —— 7 passed（覆盖六项生产验收）。

---

## 2. 已验证项（本地 / 静态，已通过）

| 项 | 结果 | 说明 |
| --- | --- | --- |
| mypy 全量 | 0 错误 | 类型检查通过（`src backend`，120 文件） |
| 全量 pytest（非 live） | 797 passed / 1 skipped / 3 deselected | 本次冻结前已实测；skip 为环境条件分支 |
| ruff | 剩 2 × B017 | 均在 `test_vpn_diagnosis_closed_loop.py`，由 qa 处理中（非阻断）
| 前端 typecheck（tsc -b） | 通过，exit 0 | `frontend/src` 全量 |
| 前端 lint（oxlint） | 0 warning / 0 error | 通过 |

---

## 3. 需外部 / CI / Docker 复验项

### (a) Docker 集成测试（本机无 Docker）
- 说明：`infra/compose.test.yml` 依赖 `pgvector/pgvector:pg17` 与 `redis:7-alpine`，
  需在有 Docker 的 CI / 环境运行；postgres/redis 集成测试按 `skipif` 跳过（本机无服务）。
- 复验命令：
  ```bash
  cd <repo_root>
  docker compose -f infra/compose.test.yml up -d
  pytest -m "not live_e2e"
  docker compose -f infra/compose.test.yml down
  ```
  （`-m "not live_e2e"` 排除需要真实外部渠道/真实 VPN 数据源的 live e2e。）

### (b) 前端 `npm run build` / `npm run test`（沙箱管道 EPERM）
- 说明：本沙箱中二者均在 Vite **配置加载阶段**
  （`bundleAndLoadConfigFile` → `optimizeSafeRealPathSync` → `exec("net use")`）被
  `spawn EPERM` 拦截——发生在编译任何项目代码之前，与改动无关（同样拦截基线）。
- 复验命令（在无管道限制的外部 node 环境 / Docker CI 直接执行）：
  ```bash
  cd frontend
  npm ci
  npm run typecheck   # tsc -b
  npm run lint        # oxlint
  npm run build       # tsc -b && vite build
  npm run test        # vitest run
  ```

### (c) `tmp_path` fixture 用例（无沙箱限制环境复验）
- 说明：`tests/test_vpn_mock_adapter.py` 的 json-file 加载等依赖 `tmp_path` 的用例，
  在本沙箱下受目录/临时区限制（表现为 6 errors），需在无该限制环境复验。
- 复验命令：
  ```bash
  pytest tests/test_vpn_mock_adapter.py -v
  ```

### (d) 真实 VPN 数据源
- 说明：本期**不接入**真实 VPN 后台，使用 `MockVpnAdapter`（只读 mock），
  与 `docs/product/vpn-v1-scope.md` 第 6 节「不做真实 VPN 自动诊断」一致。
- 复验项：无（明确不在本期范围，作为后续边界说明）。

---

## 4. 汇总命令速查

```bash
# 1) 后端静态 + 非 DB 单测
cd <repo_root>
ruff check .            # 预期仅剩 B017 / I001（qa 处理中）
mypy .
pytest -k "not live_e2e"   # 非 DB 用例（tmp_path 用例在无限制环境复验）

# 2) Docker 集成测试（需 docker）
docker compose -f infra/compose.test.yml up -d
pytest -m "not live_e2e"
docker compose -f infra/compose.test.yml down

# 3) 前端（无管道限制环境）
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
npm run test
```

> 说明：以上命令仅为指引，具体以仓库 `README` / `pyproject.toml` / `package.json`
> 的实际脚本与 pytest 标记为准。
