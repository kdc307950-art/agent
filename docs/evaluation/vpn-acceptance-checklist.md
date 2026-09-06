# VPN 故障智能服务台 —— 整体验收与外部复验清单

> 面向最终整体验收。当前冻结基线：`vpn-control-v1.0`（2026-09-05）。汇总本项目五阶段交付、已验证项、
> 尚未验证的真实外部系统边界，以及可选复验命令。本清单只做记录与指引，不对代码做任何修改。

---

## 0. 一句话结论

主体交付已经落盘，并已完成本地后端、前端、Mock E2E、静态检查和 Fake FMG
八步演练；Fake FMG、演示证书与 Windows 一键演示脚本均已纳入仓库。**真实 FortiManager staging、真实 FortiGate 最终下发、生产长期运行与回滚仍未验证**。
真实 VPN 数据源本期不接入（属后续范围）。

---

## 1. 五阶段交付清单

### 阶段一 —— 基线冻结（已完成）
- lint/类型治理：ruff `36 → 0`、mypy `8 → 0`。
- 版本统一：产品基线 `vpn-control-v1.0`，评测集版本 `2026-09-05-vpn-v1` / `2026-09-05-vpn-v2`
  （见 `backend/knowledge/vpn_eval_cases.py` 与 `backend/knowledge/vpn_eval_cases_v2.py`）。
- `legacy-demo` 标记（区分演示路径与正式路径）。
- git commit：`cd2b9a1`（`chore(release): freeze vpn control v1.0`，冻结提交）。

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
| mypy 全量 | 0 错误 | 类型检查通过（`src backend`，126 个源文件） |
| 全量 pytest（非 live） | 797 passed / 1 skipped / 3 deselected | 本次冻结前已实测；skip 为环境条件分支 |
| ruff | 通过 | `backend src tests` 检查无错误 |
| 前端 typecheck（tsc -b） | 通过，exit 0 | `frontend/src` 全量 |
| 前端 lint（oxlint） | 0 warning / 0 error | 通过 |
| 前端 Vitest | 38 passed | 单元测试全量通过 |
| Playwright Mock E2E | 22 passed | 固定 Mock 环境回归通过 |
| Fake FMG 八步演练 | ALL PASS | 覆盖成功、REJECT、STALL、人工确认等分支 |
| 工单升级重推入口 | 本地 API 测试通过 | 仅 `it.vpn` + `in_progress` + `ticket:agent` 可创建审批；跨租户返回 `404`，不触发 install |

---

## 3. 尚未验证的外部项

### (a) 真实 staging 接入
- 说明：尚未连接真实 FortiManager staging。Fake FMG 只证明协议适配、异步任务轮询、
  拒绝/停滞和人工确认路径，不等价于厂商控制面或设备下发验证。
- 复验前置：准备隔离 ADOM、Model Device、Policy Package、只读/最小写权限账号，
  配置 CA bundle，并先执行 preview、审批、canary 和对账。

### (b) 真实 FortiGate 最终下发
- 说明：尚未验证 FortiManager 到真实 FortiGate 的最终安装效果、策略生效和回滚。
- 复验要求：以设备侧状态、策略版本、连通性和人工回归结果形成可追溯证据。

### (c) 生产长期运行与回滚
- 说明：尚未验证生产流量下的容量、告警、任务停滞、外部超时、数据库故障恢复和回滚。
- 复验要求：至少完成小流量 canary、失败注入、人工对账、重复提交和恢复演练。

### (d) Docker 集成测试（环境相关）
- 说明：`infra/compose.test.yml` 依赖 `pgvector/pgvector:pg17` 与 `redis:7-alpine`；
  当前冻结回归已通过，Docker 仅作为可重复环境复验方式，不再作为本地结果的替代口径。
- 复验命令：
  ```bash
  cd <repo_root>
  docker compose -f infra/compose.test.yml up -d
  pytest -m "not live_e2e"
  docker compose -f infra/compose.test.yml down
  ```
  （`-m "not live_e2e"` 排除需要真实外部渠道/真实 VPN 数据源的 live e2e。）

### (e) 真实渠道与真实模型
- 说明：企业微信/钉钉真实闭环、真实模型生成建议、生产成本和 P95 尚未验证，
  不纳入 V1 演示或生产能力声明。

### (f) 可选外部复验命令
在具备 Docker / 无管道限制的环境中，可执行：
  ```bash
  cd frontend
  npm ci
  npm run typecheck   # tsc -b
  npm run lint        # oxlint
  npm run build       # tsc -b && vite build
  npm run test        # vitest run
  ```

### (g) `tmp_path` fixture 用例
- 说明：冻结前全量回归已通过；不再保留早期沙箱限制导致的旧错误数字。
- 复验命令：
  ```bash
  pytest tests/test_vpn_mock_adapter.py -v
  ```

### (h) 真实 VPN 数据源
- 说明：本期**不接入**真实 VPN 后台，使用 `MockVpnAdapter`（只读 mock），
  与 `docs/product/vpn-v1-scope.md` 第 6 节「不做真实 VPN 自动诊断」一致。
- 复验项：无（明确不在本期范围，作为后续边界说明）。

### (i) 本地 Fake FMG 与 Windows 演示
- 入口：在仓库根目录执行 `./scripts/demo.ps1`。它会启动 Compose、等待服务就绪、生成开发令牌并运行 Fake FMG 八步演练；`./scripts/drill-fmg.ps1` 仅运行演练，`./scripts/demo.ps1 -Down` 停止环境。
- 范围：`tools/fake-fmg/` 内的服务和固定自签名证书只用于协议级本地复现。它们不构成真实 FMG、staging、FortiGate 或生产写入证据，私钥不能用于任何真实环境。
- 环境条件：首次构建必须能访问 Docker 镜像和 npm 依赖仓库；已完成构建的机器可使用 `-SkipBuild` 复用本地镜像。

---

## 4. 汇总命令速查

```bash
# 1) 后端静态 + 非 DB 单测
cd <repo_root>
ruff check .            # 预期通过
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
