# VPN 客户处置闭环 —— 持久化与迁移说明（阶段二）

> 本文档是阶段二「VPN 客户处置闭环领域对象与状态机扩展」的**存储结构 / 迁移说明 / 方案确认**锚点。
> 代码实现见 `backend/vpn/diagnosis.py`（领域对象 + 内存登记表）、`backend/vpn/repository.py`
> （PostgreSQL 仓储）、`backend/vpn/closed_loop.py`（服务编排）、`backend/schema.py`（v23 迁移）。

## 1. 采用的持久化方案：方案 A（新建专门表）

本阶段采用 **方案 A**：为诊断运行 / 排查步骤 / 客户结果 / 升级记录**各建一张专门表**，而不是
把全部塞进 `ticket_workflow_runs` 或现有 JSONB 列。理由：

- 排查步骤需**逐条**持久化并被客户**逐条回填**（`VpnCustomerAction` / `VpnCustomerActionResult`
  —— 一张宽表无法干净表达「一条步骤 + 多条结果」的一对多关系）；
- 诊断运行 / 升级记录有各自的独立生命周期状态，独立建表便于查询与审计；
- 与已落库的 `copilot_runs` / `copilot_drafts` 先例一致（见 schema.py v16）。

### 存储结构（4 张专用表，均租户隔离 + FK 指向 tickets）

| 表 | 主键 | 关键列 | 对应领域对象 |
| --- | --- | --- | --- |
| `vpn_diagnosis_runs` | `run_id` | tenant_id, ticket_id, fault, hypothesis, confidence, evidence(JSONB), ruled_out(TEXT[]), next_action, reason_codes(TEXT[]), status, created_at, updated_at | `VpnDiagnosisRun` |
| `vpn_customer_actions` | `action_id` | tenant_id, ticket_id, run_id, title, instruction, expected_result, risk_level, requires_agent, status, ordinal, created_at | `VpnCustomerAction` |
| `vpn_customer_action_results` | `result_id` | tenant_id, ticket_id, action_id(FK), run_id, result, evidence(JSONB), details, submitted_by, submitted_at | `VpnCustomerActionResult` |
| `vpn_escalations` | `escalation_id` | tenant_id, ticket_id, run_id, reason, reason_codes(TEXT[]), target_queue, status, created_at | `VpnEscalation` |

### 领域对象（`backend/vpn/diagnosis.py`，全部 `extra="forbid"`）

- `VpnDiagnosisRun`: run_id/ticket_id/tenant_id/fault/hypothesis/confidence[0..1]/evidence[]/ruled_out[]/next_action/reason_codes[]/status/created_at/updated_at
- `VpnDiagnosisFinding`: tool_name/evidence/document_id/document_version/chunk_id/title/found
- `VpnCustomerAction`: **必填** action_id/title/instruction/expected_result/risk_level/requires_agent，另有 ticket_id/tenant_id/run_id/status/order/created_at
- `VpnCustomerActionResult`: **必填** action_id/result，另有 result_id/ticket_id/tenant_id/run_id/evidence/details/submitted_by/submitted_at
- `VpnEscalation`: escalation_id/ticket_id/tenant_id/run_id/reason/reason_codes[]/target_queue/status/created_at

## 2. 持久化双通道（生产落库 / 单测内存）

- **生产**：`runtime.vpn_closed_loop`（`VpnClosedLoopService`）持有
  `VpnDiagnosisRepository(audit.pool)`（`runtime.vpn_diagnosis_repo`），把诊断运行、排查步骤、
  客户结果、升级记录**镜像写入**上述 4 张表；`get_snapshot` 优先读库。
- **单测**（fake runtime / 未配置模型 key 时 repository 为 None）：走内存 `DiagnosisRegistry`
  （与 reissue 的 `ReissueRegistry` 同款，可单测）。

## 3. 闭环关键路径（落库 + 状态机 + 时间线）

1. **`POST /tickets/{id}/vpn/diagnose`** → 运行诊断 → 落 `vpn_diagnosis_runs` → 工单迁至
   `diagnosing`（`START_DIAGNOSIS`）→ 按命令分派：
   - `provide_steps`：草稿经 `parse_steps_to_actions` 解析为 **`VpnCustomerAction[]`** 逐条落
     `vpn_customer_actions`，工单迁至 `awaiting_customer_action`（`PRESCRIBE_STEPS`）；
   - 其它命令复用 `executor.execute_diagnosis_command`（详见 executor）。
2. **`POST /tickets/{id}/vpn/actions/{action_id}/result`**（客户回填）→ 落
   `vpn_customer_action_results` + 把该 `VpnCustomerAction.status` 置 `executed` → 工单迁回
   `diagnosing`（`PROVIDE_ACTION_RESULT`）→ **再次诊断**。
3. **`POST /tickets/{id}/vpn/diagnose/resume`** → 再开一轮新 `VpnDiagnosisRun`（新 run_id）继续闭环。
4. 诊断无命令 / 必须人工时记录 `vpn_escalations` 并联手工单至 `reconciliation_required`
   （`REQUEST_RECONCILIATION`）；对账后 `RECONCILE` 回 `in_progress`。

每次关键动作（诊断开始/派步骤/客户结果/升级）除写专用表外，还经
`runtime.tickets.append_status_event` 追加一条工单状态流水（`vpn_diagnosis` /
`vpn_prescribed_steps` / `vpn_customer_action` / `vpn_escalated`），使**诊断进入工单时间线**。

## 4. 迁移（增量，勿动已应用迁移）

- Schema 版本：`APP_SCHEMA_VERSION = 22 → 23`（`backend/schema.py`）。
- 迁移在 `ensure_schema_version` 内新增 **`if current < 23:`** 增量块（add-only，不修改已应用迁移）：
  1. 重建 `tickets` 的 `tickets_status_check` 约束，纳入 3 个新状态
     `diagnosing` / `awaiting_customer_action` / `reconciliation_required`；
  2. `CREATE TABLE IF NOT EXISTS` 4 张 `vpn_*` 表 + 索引。
- 迁移入口：`backend/migrations.py::setup_postgres()` → `ensure_schema_version(lock_connection)`
  （增量迁移在此执行），与 `AUTO_SETUP` 幂等一致。
- `REQUIRED_RELATIONS` 已加入 4 张 `vpn_*` 表，`check_schema_ready` 在版本不匹配或缺表时拒绝就绪；
  `tests/test_ticket_repository_unit.py` 的版本断言已同步为 23。
- 备注：`tests/test_vpn_mock_adapter.py::test_*_json_file` 与 `test_runtime_build_graph.py` 的
  `tmp_path` 用例在当前沙箱 temp 目录因 `PermissionError` 报错，属环境问题，与本次改动无关。

## 5. 命令

```bash
# 初始化/增量迁移（幂等，AUTO_SETUP 已含；亦可手动执行）
python -m backend.migrations
```
