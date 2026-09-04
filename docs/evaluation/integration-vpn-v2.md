# VPN 专项真实 PostgreSQL/Redis 集成验证报告（vpn-v2 评测集）

> 执行角色：integration-engineer（队伍 vpn-eval-milestone） · 任务：t4
> 日期：本轮集成验证（以仓库实际运行结果为准）
> 目标：验证真实 PostgreSQL/Redis 集成闭环（报障→补全→假设与证据→客户执行→回填→再诊断/升级→审批受控操作→回访关闭）可跑通，并产出**真实库**上的引用支撑率/边界/误放行等可量化指标（vpn-v2，63 条）。

---

## 0. 结论速览

- **容器/迁移/种子：全部通过**。`infra/compose.test.yml` 的 Postgres(55436)/Redis(56379) 健康；`backend.migrations`、`backend.vector_migrations`、`backend.seed_demo` 均 exit=0，种子库（租户 `demo`）就绪。
- **真实仓储生命周期测试套件**（`pytest tests -q -m "not live_e2e"`）：**542 passed / 0 failed / 3 deselected**（3 条为 `live_e2e`，不触发真实模型，交由 e2e-engineer）。修复了 2 个最初失败（详见 §3 修复说明）。
- **VPN 评测 db 模式**（`run_vpn_eval --database-url <test> --require-db --dataset vpn-v2`）：知识模式 **db**，引用支撑率 **0.3077**（有值），失败 29 条可定位。CLI exit=1，唯一原因 = `--max-auto-misdirect=0` 门禁被 `auto_misdirect=12` 触发（评估集/边界/检索问题，见 §4 反馈）。
- **不触发真实模型**：本任务未调用任何外部 LLM；`agent` 字段为 `deterministic-keyword-classifier + classify_vpn_fault + vpn-boundary`。

---

## 1. 验证矩阵与每项结果

| # | 验证项 | 命令/入口 | 结果 | 说明 |
|---|---|---|---|---|
| 1 | 起测试栈 | `docker compose -f infra/compose.test.yml up -d --wait` | ✅ | postgres/redis 均 Healthy；端口 55436/56379 无冲突 |
| 2 | Postgres schema | `python -m backend.migrations` | ✅ exit=0 | 幂等建表/索引 |
| 3 | pgvector 迁移 | `python -m backend.vector_migrations` | ✅ exit=0 | vector/pg_trgm + HNSW |
| 4 | 演示种子 | `python -m backend.seed_demo --tenant demo` | ✅ exit=0 | SLA 4、team 1、member 1、schedule 1、routing_rule 1、asset 5、it_policy 2、knowledge 9 |
| 5 | 真实仓储生命周期测试 | `pytest tests -q -m "not live_e2e"` | ✅ 542 通过 | 详见 §2 / §3 |
| 6 | VPN 评测 db 模式 | `run_vpn_eval --database-url … --require-db --dataset vpn-v2` | ⚠️ 报告生成但门禁 exit=1 | 模式=db、引用支撑率有值、失败可定位；门禁=auto_misdirect>0 |

---

## 2. 生命周期测试套件（真实库）— 结果

运行环境变量：`DATABASE_URL`/`TEST_DATABASE_URL`=`postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55436/langgraph`，`REDIS_URL`=`redis://127.0.0.1:56379/0`，`CI=true`。

- 首轮：**540 passed / 2 failed / 3 deselected**（失败：`test_channel_identities_postgres.py::…crud_and_unique_key`、`test_ticket_lifecycle_postgres.py::…http_style`）。
- **独立复现**：两失败在隔离运行下仍复现 → 为真实缺陷，非测试污染/顺序依赖。
- **修复后**：**542 passed / 0 failed / 3 deselected**。
- **偶发/顺序依赖**：`test_inbound_worker_postgres.py::test_inbound_worker_retries_then_dead_letters_and_replays` 在完整套件中偶发失败（`assert 'failed'=='dead'`），**独立运行通过**（4.06s），且首轮完整运行通过 → 属顺序依赖/时序敏感，非代码缺陷；已记录，不影响本报告主体结论。

---

## 3. 修复说明（2 个失败）

### 3.1 `test_channel_identities_postgres.py::…crud_and_unique_key` — 代码缺陷（已修复）

- **现象**：第二次 `upsert` 只传 `departments=["it"]`（未传 `external_user_id`/`asset_id`），随后 `get` 返回 `external_user_id=None`，而测试期望保留首次写入的 `wecom-user-1`。
- **根因**：`backend/channel_identities.py` 的 upsert 用 `ON CONFLICT … DO UPDATE SET external_user_id = EXCLUDED.external_user_id` 无条件覆盖；当本次入参未提供该字段（`None`）时，用 `NULL` 覆盖了既有可信映射。
- **修复**：改为**部分更新语义**——未提供的可选字段保留原值：
  `external_user_id = COALESCE(EXCLUDED.external_user_id, channel_identities.external_user_id)`，`asset_id` 同理。
- **影响**：避免一次未带该字段的 upsert 误清空可信身份映射；与「服务端登记的身份映射不应被静默清空」的安全语义一致。修复后测试通过，未引入回归。

### 3.2 `test_ticket_lifecycle_postgres.py::…http_style` — 测试与生产受理流脱节（已对齐）

- **现象**：最初在**第二次** `transition_many`（`operation_id="op-life-resume"`）抛 `WorkflowOperationConflict: 未登记的 operation_id`。
- **根因（并存多个契约不一致）**：
  1. `op-life-resume` 从未经 `start_workflow_operation` 登记（生产 `ticket_intake.py:316`、`channel_processor.py:253`、`ticket_api.py:527` 均先登记再 `transition_many`）；
  2. 恢复 `transition_many` 只传 `{"ticket:system"}`，而恢复命令需 `ticket:customer`（生产 `apply_intake_resume` 用 `scopes | {"ticket:system"}`）；
  3. 恢复只补 `device`，但 `IntakePolicy` 内置对 IT 还要求 `affected_system`/`impact`，completeness 仍判定缺失 → 停在待补全而非入队。
- **修复**（对齐生产范式）：
  1. 恢复前新增 `start_workflow_operation(operation_id="op-life-resume", command_type="resume", expected_version=ticket.version, …)`；
  2. 恢复 `transition_many` 的 scopes 改为 `{"ticket:system", "ticket:customer"}`；
  3. 恢复命令补全 `affected_system`/`impact`。
- **说明**：这是**测试（参考生命周期）与生产受理流未同步**导致，非生产代码缺陷；生产流程本身一致地要求上述三步。

---

## 4. VPN 评测 db 模式 — 真实指标（vpn-v2，63 条）

运行：`.venv\Scripts\python.exe -m backend.run_vpn_eval --database-url …55436… --require-db --dataset vpn-v2 --json docs/evaluation/vpn-eval-report-v2.json`

### 4.1 样本画像

| 项 | 值 |
|---|---|
| total_cases / dataset | 63 / `vpn_v2`（9 类场景 × 7 条）|
| 知识模式 | **db**（真实词法检索，租户 `demo`）|
| fault_counts | connection_failed 33、multi_user_impact 7、negative_out_of_scope 23 |
| boundary_counts | auto_suggest 13、must_ask 9、must_escalate 41 |
| scenario_counts | 9 类各 7 条 |

### 4.2 核心指标

| 指标 | 值 | 口径 |
|---|---|---|
| 分类 Top1（vpn_fault）| **1.0**（40 条真实 VPN）| `classify_vpn_fault` 准确率 |
| 分类（category）| accuracy 0.9206 / vpn_category_rate 1.0 | 类别口径 |
| 字段补全 detection_rate | **1.0**（63 条）| 缺失检测匹配率 |
| 字段补全 complete_rate | 1.0（40 条 real VPN）/ 0.775（metrics 全 63 条口径）| 8 项全齐 |
| 边界判定 accuracy | **0.5397** | 整体 |
| 边界 by_boundary | must_ask **1.0**、must_escalate **0.5122**、auto_suggest **0.3077** | |
| 引用支撑率（M6）| **0.3077**（numerator 4 / denominator 13）| db 模式、auto_suggest 样本预期文档全召回占比 |
| 高风险误放行 | **0**（16 条高影响/敏感样本）| has_sensitive/high_impact → 被判定 auto_suggest |
| 负向/越界误导向 it.vpn 自动建议 | **12** | `auto_misdirect`（account-lockout + ACL 样例）|
| 闭环可达率 | **1.0**（40/40）| real VPN 进入 it.vpn 主线 |
| 升级准确率（M4）| recall 0.5122 / precision 0.7 / acc 0.5397（tp21 fp9 fn20 tn13）| |
| 故障假设命中（M3，回退口径）| 1.0（40 条）| 无结构化假设时回退 vpn_fault |
| 工具调用失败率（M8）| null（0 次工具调用）| 确定性组件无工具调用记录 |
| 延迟 p50/p95 | 0.0 / 0.0 | 仅分类器，确定性 |
| 成本（M9）| unrated（per_ticket_usd=null）| 无模型 token；VPN 诊断 token 需在 agent.run 内埋点 |
| 失败样本 | **29** | 可定位（index/text/vpn_fault/boundary/reasons）|

### 4.3 失败归因（29 条）

| 归因组合 | 数量 | 性质 |
|---|---|---|
| boundary + reference_miss | 9 | 边界 + 检索 |
| boundary | 8 | 边界 |
| boundary + misdirect_to_auto_suggest | 7 | 边界 + 越权/负向误放行 |
| category_mismatch + boundary + misdirect_to_auto_suggest | 5 | 分类 + 边界 + 误放行（账号锁定类）|

**全部 29 条均含 boundary 不一致**；9 条命中 `reference_miss`（auto_suggest 样本预期文档未被全召回）；12 条 `misdirect_to_auto_suggest`（负向/越权样本被导向 it.vpn 自动建议）；5 条 `category_mismatch`（账号锁定→it.account 被分类成 it.vpn）。

### 4.4 CLI 退出码说明

`run_vpn_eval --require-db` 退出码 = 1，**唯一**原因是默认门禁 `--max-auto-misdirect=0`（实际 12 > 0）。报告 JSON 已正常写出（`docs/evaluation/vpn-eval-report-v2.json`），非执行异常。

---

## 5. 反馈给 eval-set-engineer / metrics-engineer 的关键缺口

1. **账号锁定与 VPN 混淆（S6）**：5 条 expected_category=`it.account` 被分类为 `it.vpn` 且判定 `auto_suggest` → 需澄清分诊规则（`it.account` vs `it.vpn`，并基于 `get_vpn_account_status` 佐证账号状态 vs 网关状态）。
2. **ACL 越权（S9）**：7 条 ACL 样例被判为 `it.vpn` 自动建议而非 `must_escalate` → `boundary_vpn` / `classify_vpn_fault` 需识别越权/跨租户信号；建议在评测项加「越权拒绝率」并断言 =0。
3. **引用支撑率低（M6，0.3077）**：仅 4/13 auto_suggest 样本的预期文档被全召回 → 词法检索召回质量/文档分块需优化，或 `expected_document_ids` 与实际可见文档不一致（种子仅 9 篇知识，`vpn-001` 等需确认覆盖）。
4. **边界判定整体偏低（0.5397）**：must_escalate 0.5122、auto_suggest 0.3077 → `boundary_vpn` 决策与评测集 `expected_boundary` 存在系统偏差，建议 metrics-engineer 复核对齐。
5. **M3 假设命中 / M5 排障轮次 / M9 成本**：确定性组件的 M3 现为「回退 vpn_fault」口径；M5 无结构化步骤、M9 无模型 token → 需在 `agent.run` 埋点后再量化（依赖真实模型侧，向 e2e-engineer/metrics-engineer 交接）。

---

## 6. 遗留观察

- `test_inbound_worker_postgres.py::test_inbound_worker_retries_then_dead_letters_and_replays`：完整套件中偶发失败、独立运行通过 → 顺序依赖/时序敏感，建议后续隔离（如清理共享队列/加时序容忍）。
- 真实模型 E2E（`live_e2e` 3 条）+ 诊断 agent token/成本（M9）交由 e2e-engineer / metrics-engineer 处理，本任务未触发。

---

## 7. 复现命令

```powershell
# 1) 起测试栈
& 'C:\Users\孔德草\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe' compose -f infra/compose.test.yml up -d --wait
$env:DATABASE_URL='postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55436/langgraph'
$env:TEST_DATABASE_URL=$env:DATABASE_URL
$env:REDIS_URL='redis://127.0.0.1:56379/0'
# 2) 迁移 + 种子
.\.venv\Scripts\python.exe -m backend.migrations
.\.venv\Scripts\python.exe -m backend.vector_migrations
.\.venv\Scripts\python.exe -m backend.seed_demo --tenant demo
# 3) 真实仓储生命周期测试
$env:CI='true'; .\.venv\Scripts\python.exe -m pytest tests -q -m 'not live_e2e'
# 4) VPN 评测 db 模式（vpn-v2）
.\.venv\Scripts\python.exe -m backend.run_vpn_eval --database-url $env:TEST_DATABASE_URL --require-db --dataset vpn-v2 --json docs/evaluation/vpn-eval-report-v2.json
```
