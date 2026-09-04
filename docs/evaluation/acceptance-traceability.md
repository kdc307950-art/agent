# 验收口径 ↔ 现有能力/缺口 Traceability（VPN 专项，vpn-v1）

> 生成角色：analyst（研究者） · 日期：本轮的验收分析
> 目的：把「最终验收口径」与现有实现做精确的可追溯映射，标 GAP、标「已存在但未打通」，
> 作为后续 eval-set / metrics / integration / e2e / demo 成员直接可用的分工依据。
>
> 系统参考基线（均以仓库实际文件为准）：
> - 受理图：`src/my_agent/helpdesk/graph.py`、`intake.py`、`domain.py`
> - VPN 诊断 Agent：`backend/vpn/{agent,models,service,executor,tools,approval,reissue_service,api,mock_adapter}.py`
> - 评测：`backend/knowledge/vpn_eval_cases.py`(60 条)、`backend/run_vpn_eval.py`、
>   `backend/run_ticket_eval.py`、`backend/run_knowledge_eval.py`
> - 装配：`backend/runtime.py`、`backend/app.py`、`backend/tool_governance.py`、`backend/seed_demo.py`

---

## 0. 结论速览（T 头）

- 主线「**报障 → 信息补全 → 假设与证据 → 客户执行步骤 → 回填结果 → 再诊断或升级 → 人工审批的受控操作 → 回访关闭审计**」中：
  - **前 4 段**：受理图 + VPN 诊断 Agent 已实现主体（分类/字段补全/边界三态/只读命令），但「回填结果→再诊断」与「命令实际执行」**未打通**。
  - **第 5 段（回填/再诊断）**：域状态机支持（`AWAITING_CUSTOMER→INTAKING` 回填、`RESOLVED→CLOSED`、`REQUEST_APPROVAL`），**无编排时序/无评测**。
  - **第 6 段（审批式受控操作）**：`backend/vpn/approval.py + reissue_service.py + api.py` 已完整实现（幂等 + 前置校验 + 审批 + 受控执行），且 `/vpn/reissue` 已挂到 `app.py`；但**与受理图主流程未串**（vpn_diagnose_node 不产出/不执行 request_approval 命令）。
  - **第 7 段（回访关闭审计）**：`domain.py` RESOLVED→CLOSED、回访事件、`test_ticket_lifecycle_postgres.py` 有支撑。
- **9 类评测场景**：5 类 VPN 故障基本覆盖；**769 错误码完全缺失**；「客户端版本过旧」「家庭网络 vs 手机热点对比」「账号锁定与 VPN 故障混淆」**无/弱**；「ACL 越权」**完全缺失**。
- **9 项指标**：VPN 分类 Top1 / 字段补全 / 边界三态 / 引用支撑率 / 高风险误放行部分已有；**故障假设命中率、客户平均排障轮次、工具调用失败率（聚合比率）、VPN 专用成本** **无**。
- **关键「存在但未打通」**：`executor.execute_diagnosis_command`（`backend/vpn/executor.py:120`）已实现完整「命令→状态机映射」，但 `graph.py` 的 `vpn_diagnose_node` **只校验不执行**（`backend/vpn/../graph.py:408-464`），导致诊断结果无法落工单/发命令。

---

## 1. 验收流程 → 模块/文件/API/字段证据映射

| # | 验收段 | 现有实现？ | 证据（file:line） | 缺什么 / 备注 |
|---|---|---|---|---|
| R1 | VPN 报障（建单/受理） | ✅ | `graph.py:120 normalize_node`、`graph.py:127 classify_node`（`KeywordTicketClassifier`，`intake.py:227`）；`channel_processor.py:105`「字段:值」追问提示；`ticket_api.py` 建单入口 | 分类仅到 `it`+`vpn` 子类（`intake.py:194-225` `_IT_SUBCATEGORY_KEYWORDS`），`vpn_fault` 由 `classify_vpn_fault`（`intake.py:323`）另判 |
| R2 | 信息补全（8 项固定字段） | ⚠️ 依赖租户策略 | 8 项字段定义：`docs/product/vpn-v1-scope.md:39-55`；`vpn_eval_cases.py:56-65 VPN_REQUIRED_FIELDS`；**强制入口在租户 it.vpn 策略**：`seed_demo.py:54-69`（`it.vpn` -> 8 字段）经 `ItPolicyProvider`（`graph.py:138 apply_policy_node`）+ `completeness_node`（`graph.py:180`） | `IntakePolicy` 默认只要求 `IT:{affected_system,impact}`（`intake.py:103-111`），**若租户未配 it.vpn 策略，8 项不受迫**。评测器 `run_vpn_eval.py` 直接用 `VPN_REQUIRED_FIELDS` 算 missing，与策略无关 |
| R2b | 追问/澄清（interrupt） | ✅ | `graph.py:200 clarify_node`（`interrupt`）+ `validate_resume_command`（`domain.py:294`）；`PendingTicketInterrupt` 防越权（`domain.py:211`） | 追问耗尽转人工：`completeness_node` `clarification_exhausted`（`graph.py:193`）+ `dispatch_node` `clarification_exhausted` 原因码（`intake.py:429`） |
| R3 | 故障假设与证据 | ✅（只读诊断）+ ⚠️ 未落地 | `VpnDiagnosisAgent`（`agent.py:220`）：系统提示只读工具（`agent.py:83`）、有界循环、`governed_invoke`；6+1 只读工具（`tools.py:285` `VPN_TOOLS`）；`MockVpnAdapter` 只读（`mock_adapter.py:209`）；`evaluate_handoff` 四类转人工（`models.py:160`） | `agent.run` 以**模型输出的 `DiagnosisCommand`** 为产物，中间「假设」并未结构化沉淀；`tool_evidence` 仅在 `tool_trace`/`tool_evidence` 里留痕，**无「假设命中率」指标** |
| R4 | 客户执行步骤 | ⚠️ 语义存在，未打通 | `provide_steps` 命令 → `TicketAction.PROPOSE_ANSWER`（仅草稿）（`executor.py:27-33`+`36` `DRAFT_ONLY_COMMANDS`；`domain.py:111 CLASSIFIED→ANSWER_PROPOSED`） | **未接主流程**：`graph.py` `vpn_diagnose_node` 只 `model_validate`+`validate` 命令（`graph.py:409-432`），**不调用 `executor.execute_diagnosis_command`**，因此 `provide_steps` 草稿不会真正落到工单 |
| R5 | 回填结果 | ✅ 域支持 / ⚠️ 无评测 | `AWAITING_CUSTOMER + PROVIDE_INFORMATION → INTAKING`（`domain.py:109-110`）；`validate_resume_command`（`domain.py:294`）；`channel_processor.py:105` 补字段 | 无「回填后再诊断」的自动闭环；`prepare_context` 会重读工单消息（`service.py:77-82`），机制具备但**无编排时序** |
| R6 | 再诊断 / 升级 | ✅ 边界判定 + ⚠️ 无再诊断编排 | 边界三态 `boundary_vpn`（`intake.py:349`）；`evaluate_handoff`（`models.py:160`）；`escalate_incident`→`TicketAction.QUEUE`→`team-service-desk`（`executor.py:38-40`+`173-175`+`211`；`domain.py:107-108`) | 升级要么由受理图 `must_escalate` 触发，要么诊断 `must_handoff` 标记（`graph.py:455-464` 只写审计）。**诊断→执行升级命令未打通** |
| R7 | 人工审批的受控操作 | ✅ 已实现（独立 API 链路） | `approval.py`：`start/approve/reject/execute`（`approval.py:377/474/524/586`）+ `ReissueRegistry` 幂等（`302`、`586` NOT-APPROVED 门禁）+ `preflight_reissue`（`223`）；`reissue_service.py` 编排层落 `workflow_operation` 终态（`204`/`222`/`244`）；`api.py` `POST /vpn/reissue` /`approve`/`reject`（`88`/`108`/`133`）挂 `app.py:325` | **与受理图主流程未串**：`request_approval`/`reissue` 仅能走 HTTP API 或工具 `reissue_vpn_config`（`tools.py:233` 受控），`graph.py` 桥接节点不触发 |
| R8 | 回访、关闭、审计 | ✅ 域 + 生命周期测试 | `RESOLVED→CLOSED`（`domain.py:144`）、`REOPEN`（`145`）；回访/满意度 outbox（`DEMO_SCRIPT.md:43-46`）；`test_ticket_lifecycle_postgres.py`（`建单→…→回访→关闭`） | VPN 专项评测集**无「回访/关闭/审计」样本**（`vpn_eval_cases.py` 无此类 scenario） |

**「存在但未打通」汇总（重点标注）**：
1. **`executor.execute_diagnosis_command` 未接入受理图主流程**。`backend/vpn/executor.py:120-201` 已实现「集合/禁止校验→TicketCommand→状态机 transition→escalate 进队列」，但 `graph.py:408-464` 的 `vpn_diagnose_node` 仅在非 must_handoff 时 `DiagnosisCommandValidator.validate`（只校验不执行），`vpn_diagnosis_command` 只作为状态字段回写，**没有任何代码调用 `execute_diagnosis_command` 把命令落地**。
2. **VPN 诊断 Agent 仅在 `settings.deepseek_api_key` 存在时装配**，否则 `vpn_diagnosis`/`vpn_adapter`/`vpn_reissue` 均为 `None`（`runtime.py:238`，API 返回 503 `api.py:52-54`）。
3. **8 项 VPN 字段的强制依赖租户 `it.vpn` 策略被 seed**（`seed_demo.py:54-69`）；未 seed 时图退回 `IntakePolicy` 默认（不受迫 8 字段）。
4. **VPN 诊断模型的 token/成本未纳入 app.py 的 usage 统计**（见 §3.9）。

---

## 2. 9 类评测场景覆盖核对

> 场景基线来自 task 描述（单用户连接失败 / 多用户同时失败 / 769/809 / 客户端版本过旧 /
> 家庭网络与热点对比 / 账号锁定与 VPN 混淆 / 无知识答案 / 高风险请求 / ACL 越权）。

| # | 场景 | 现有覆盖 | 证据 | 需新增样本形态 | 需造的基础 |
|---|---|---|---|---|---|
| S1 | 单用户连接失败 | **有** | `vpn_eval_cases.py:160-174` `_connection_failed_cases()` 12 条（809/800/转圈/超时/无法建立连接）| — | — |
| S2 | 多用户同时失败 | **有** | `_multi_user_impact_cases()` `:222-232` 8 条（全公司/整个部门/多人/大家），全部 `must_escalate` | 补「多用户 + 已确认事件/升级单」联动样本（调 `get_incident_status`/`get_vpn_gateway_status` 的升级路径） | `mock_adapter.py:64-72` 有 `INC-9` 事件 + `gw-ne-001 degraded`，可支撑升级联动 |
| S3 | 769/809 错误码 | **809 有 / 769 无** | 809：`_connection_failed_cases` `:162`、`:169`；`intake.py:315-319` `connection_failed` 含 "809"/"800"；**769：无任何样本、无 `vpn_fault` 映射**；scope `:49` 列了 769 | 769 样本（`"VPN 连接失败，错误码 769"`）；明确 769 归 `connection_failed` 还是 `auth_failed`（769 常见是 Windows 宽带/凭据相关，建议与 691 区分：769→connection_failed，691→auth_failed）| 在 `intake.py:315-319` / `vpn_eval_cases.py` 增加 769 token；如需区分 691/769 的凭据语义，需在 `classify_vpn_fault` 增映射 |
| S4 | 客户端版本过旧 | **有字段 / 无场景** | 字段 `client_version`（`vpn_eval_cases.py:60`，固定 "3.4.2"）；`mock_adapter.py:118-131` 有 `client_configs` 版本 | 「客户端版本过旧→建议升级/触发 reissue」样本；在 `recent_change`/`client_version` 上做版本差判定 | `get_client_config_version`（`mock_adapter.py:365`）+ `reissue` 前置校验（`approval.py:223` config_version 检查）已具备；缺「版本阈值/对比规则」 |
| S5 | 家庭网络 vs 手机热点对比 | **无** | 字段 `network` 仅固定 "家庭宽带"/"办公网"（`vpn_eval_cases.py:79`）；**无 4G5G/手机热点样本、无「家庭 vs 公司网络」对故障影响的诊断样本** | 新增 `network="手机热点/4G5G"`、`network="酒店Wi-Fi"` 对比样本（`scope.md:50` 网络环境含 4G5G/酒店 Wi-Fi）；构造「同症状、异网络环境 → 不同处置」的诊断样本 | `classify_vpn_fault`/`boundary_vpn` 目前不读 network 差异；需明确 network 对故障假设/处置的影响规则 |
| S6 | 账号锁定与 VPN 故障混淆 | **部分（有文本，无歧义分诊）** | `_auth_failed_cases` `:198` "VPN 登录失败，账号被锁定"（判 `auth_failed`→must_escalate）；`intake.py:300-304` `auth_failed` 含 "密码错误"；`it.account` 关键词（`intake.py:198` "锁定"/"重置密码"）| 「账号被锁定 但错误码是 809/无法连接」的**歧义样本**：应判定 VPN 认证失败还是账号域问题；构造「诊断 agent 用 `get_vpn_account_status` 佐证账号状态 vs 网关状态」样本 | 现有 `mock_adapter.py` 账号只有 `user-042 active`（`27-35`），需造 `locked`/`expired` 账号样例；`get_vpn_account_status`+`get_vpn_gateway_status` 交叉判定 |
| S7 | 无知识答案 | **部分** | `_negative_cases` `:305-314` "VPN 连不上，怎么排查" `documents=()` `must_escalate`；`run_vpn_eval.py:123-141` `has_evidence = expected_document_ids 非空` | 补「知识检索 0 命中 → 无引用 → 转人工」的正向样例（现仅 1 条）；区分「无知识答案」与「无证据」两类，产出引用支撑率分母 | `run_vpn_eval` db 模式走真实 `lexical_search`（`run_vpn_eval.py:133`）；static 模式 `reference_supported` 为 None（`140-142`），需 db 才能量化 |
| S8 | 高风险请求 | **部分** | `_negative_cases` `:277-294` "VPN 删除数据"/"VPN 生产环境数据泄露"（敏感/高影响→`must_escalate`）；`intake.py:337-346` `contains_sensitive/high_impact` | 补「高风险请求应**触发人工审批（request_approval→reissue）**」样本（现只到 must_escalate，未进 approval 链路）；补「认证类（auth_failed）被误放行为 auto_suggest」的反向误放行样本 | `approval.py` 链路已具备；缺「从受理/诊断决策 → 发起审批」的说明/编排 |
| S9 | ACL 越权测试 | **无（VPN 专项）** | `vpn_eval_cases.py` 全量 60 条**无 ACL 用例**；ACL 越权只在 `run_knowledge_eval.py:98-105`（acl kind）与 `run_ticket_eval.py`（`acl_leaks`）里做知识/检索隔离；`tool_governance.py:299-345` 有 `tool_call_denied_total` 记录 | 新增 VPN 专用 ACL 样本：跨租户工具越权、越权调用 `get_*` 工具、`reissue_vpn_config` 未审批、跨租户查 `/vpn/reissue`、`scope=agent` 调需 `approve` 的动作 | `tool_governance` `denied_scope/denied_tenant`（`agent.py:427-429`）、`mock_adapter` 数据源租户隔离；需在 `run_vpn_eval` 加「越权拒绝率」统计 |

**场景层净缺口优先级**：S3(769) ≈ S9(ACL) > S5(网络对比) > S4(版本过旧) ≈ S6(账号锁定混淆) > S2(升级联动) > S7/S8(补强)。

---

## 3. 9 项指标核对（现状 + 口径建议 + 数据来源）

| # | 指标 | 现状 | 证据 | 计算口径建议 | 数据来源 |
|---|---|---|---|---|---|
| M1 | VPN 分类 Top1 | **有** | `run_ticket_eval.py:234-237`（混合口径）；VPN 口径 `run_vpn_eval.py:261-276`（`classify_vpn_fault.accuracy` + `category.accuracy/vpn_category_rate`）；`agent` 字段 `:367` | Top1 = 分类到 `it.vpn` 且 `vpn_fault` 正确的比例（仅 real VPN 样本） | `KeywordTicketClassifier`（`intake.py:227`）+ `classify_vpn_fault`（`intake.py:323`） |
| M2 | 字段补全成功率 | **有** | `run_vpn_eval.py:277-284` `field_completion.detection_rate`(+`complete_rate`)；`run_ticket_eval.py` `field_completion.detection_rate` | detection_rate = 缺失检测匹配率（`_actual_missing == _expected_missing`）；complete_rate = real VPN 8 项全齐比例 | `VPN_REQUIRED_FIELDS`（`vpn_eval_cases.py:56`） |
| M3 | 故障假设命中率 | **无** | `agent.py:248-506` `run` 内部只产出 `command`/`tool_evidence`/`evaluation`，无「fault hypothesis」归一结构；无命中统计 | 每样本定义「应命中假设」(如 `vpn_fault` / 根因假设)；诊断 agent 产出的假设词表与预期假设比对 → 命中/样本 | 需在 `DiagnosisCommand.reason_codes`(建议关键根因) 或新增结构化假设字段；`tool_evidence` + `models.py:evaluate_handoff` 关联 |
| M4 | 人工升级准确率 | **部分** | 三态边界正确率 `run_vpn_eval.py:285-293`（`boundary.accuracy` + by_boundary，含 must_escalate）；`evaluate_handoff` `must_handoff`（`models.py:160`）；`vpn_diagnosis_handoff_reasons`（`graph.py:457`） | 定义为「正确需人工升级（must_escalate/must_handoff）实际判为人工升级」比例；应区分「受理边界三态」与「诊断 must_handoff」两个口径 | 受理层：`boundary_vpn`；诊断层：`raw.must_handoff`+`evaluation.handoff_reasons`（`agent.py:499-506`） |
| M5 | 客户平均排障轮次 | **无** | `agent.py:279` 内部 `rounds` 变量、`:305-311` `tool_call_count`；均**未写入返回结构** | 平均 = Σ(每工单诊断轮次或工具调用数)/工单数；或由工单消息往返数（`AWAITING_CUSTOMER` 触发次数）统计 | 需在 `agent.run` 返回值暴露 `rounds`/`tool_calls`；或从 `clarify` 触发次数 / `service.prepare_context` 消息长度统计 |
| M6 | 引用支撑率 | **有（仅 db）** | `run_vpn_eval.py:306-316` `knowledge.reference_support_rate`（static 为 None）；`run_ticket_eval.py:277-283` `knowledge.reference_support_rate` | auto_suggest 样本引用支撑比例（预期文档子集是否全召回） | db 模式 `lexical_search`（`run_vpn_eval.py:133`）；`mock_adapter.search_knowledge`（`mock_adapter.py:341`） |
| M7 | 高风险误放行率 | **部分** | `run_vpn_eval.py:294-305` `auto_misdirect.count`（负向/越界被误导向 it.vpn 自动建议=0）；`run_ticket_eval.py:271` `acl_leaks` | 误放行 = ①负向/越界→auto_suggest（已有 `auto_misdirect`）；②auth_failed/敏感词→auto_suggest（**需新增**）；用「误放行/仅处置」而非「/总样本」 | `boundary_vpn` + `has_sensitive/high_impact`（`run_vpn_eval.py:119-121`） |
| M8 | 工具调用失败率 | **有（instrument，无比率报告）** | 治理层 `tool_governance.py:365-451` `tool_call_failed/timeout/denied/completed/retries`；VPN 专用 `agent.py:412` `vpn_tool_calls_total`、`:429` `vpn_acl_rejected_total` | 失败率 = (timeout + error + denied) / 总调用；或 denials 单列（越权） | `ToolGovernance.awrap_tool_call` 审计事件 `tool_call_failed`/`tool_call_denied_total`；`vpn_tool_calls_total` 需聚合 |
| M9 | P95 与单工单成本 | **部分** | `run_vpn_eval.py:321-328` `latency_ms.p50/p95`（**仅分类器延迟**，static=0）；成本：`app.py:480-528` `extract_model_usage`+`usage_cost_usd`（`usage.py:64`）+`budget`；`settings.py:127-128` `MODEL_INPUT/OUTPUT_COST` | P95 应覆盖**完整工单链路**（分类+字段+边界+诊断+引用），现仅分类；成本 = Σ token×单价，仅覆盖 app.py astream 图主流程 | 成本来源 `usage.py:37`+`app.py:480`；**VPN 诊断模型调用在 `agent.py`（VpnDiagnosisAgent）内部，未经 app.py 的 astream 循环，token/成本未计**（需在 `agent.run` 内埋 usage 或把 VPN agent 纳入计量）。P95 需重定义采集点 |

**指标净缺口优先级**：M3(假设命中) ≈ M5(排障轮次) ≈ M9(VPN 成本/P95 定义) > M8(聚合失败率) > M7(误放行) > M4(升级准确率) > M6(引用支撑需 db)。

---

## 4. 真实 PG/Redis 集成验证 & 真实模型受控 E2E 评审

### 4.1 PG/Redis 集成验证该跑哪些

| 验证项 | 现状 | 是否必需 | 说明 |
|---|---|---|---|
| `tests/test_ticket_lifecycle_postgres.py` | 已存在（`build→追问→补充→分类→SLA→接单→处理→解决→回访→关闭`） | **跑**（真实仓储生命周期） | 覆盖 R1/R2/R8 主链路的真实 DB 存取、版本冲突、重复建单；`TEST_DATABASE_URL` 未配则 skip（`test_ticket_lifecycle_postgres.py:39-40`） |
| `tests/test_vpn_*.py`（agent/executor/tools/models/mock_adapter/reissue_service/api） | 已存在，多为**内存/fake runtime 单测** | **跑**（单元层回归），并评估是否升级为 DB 集成 | 目前不落 PG；审批链路 reissue 用 `ReissueRegistry`（内存 `approval.py:302-333`）+ `workflow_operation`（`reissue_service.py:204/222/244`），建议补一条「reissue 落 PG workflow_operation 终态」集成 |
| `run_vpn_eval --database-url` | 支持 db 词法检索模式（`run_vpn_eval.py:345-358`）| **加 `--require-db` 跑**（产生真实引用支撑率与 ACL） | 现 report 为 static（`vpn-eval-report.json:52-55` `mode=static` `reference_support_rate=null`）；需真实库才有 M6/M9 的可信值 |
| `run_ticket_eval --require-db` | 现有混合 90 条 db 评测（`v1-report.md:39`） | 保留（与 VPN 口径**分账**） | VPN 不混算（`scope.md:103-113`） |
| `run_knowledge_eval --database-url` | 知识召回 Top1/Recall/MRR + ACL 隔离（`run_knowledge_eval.py:229-315`） | **跑**（ACL/召回真实指标） | VPN 诊断依赖知识库 `vpn-001` 召回，需真实 embedding/词法 |
| `infra/compose.test.yml` 的 Postgres/Redis | 提供 `TEST_DATABASE_URL`/`REDIS_URL` | 启动 | `v1-report.md:27-32` 给出完整命令；migrate+seed 幂等 |

**是否需要新增**：
- **是**：①一条「reissue 审批式执行落 PG `workflow_operation` 终态 + 工单状态联动」集成测试（目前只到内存 registry）；②VPN 诊断 Agent 的 token/成本纳入真实模型 E2E 计量（M9）；③ACL 越权集成（跨租户工具拒绝）。
- **否（沿用）**：`test_ticket_lifecycle_postgres.py` 已覆盖主链生命周期。

### 4.2 真实模型受控 E2E 入口 & 环境变量

- **入口**：
  - **程序化入口**：`backend/vpn/service.py` `VpnDiagnosisService.diagnose/run_with_context`（`service.py:95/141`）→ 底层 `agent.py` `VpnDiagnosisAgent.run`（`agent.py:248`）。这是真正跑真实模型（`ChatOpenAI`，`runtime.py:251-256`）的路径。
  - **HTTP/图入口**：`vpn_diagnose_node`（`graph.py:323`）在受理图执行时被调用；受理经 `app.py` `_execute_run`（`app.py:338`）经 `graph.astream` 触发。需 `settings.deepseek_api_key` 且分类为 `it.vpn` + 字段齐（`graph.py:344-348`）。
  - **审批受控操作 HTTP 入口**：`backend/vpn/api.py` `/vpn/reissue[/{idempotency_key}/{approve,reject}]`（`api.py:88-159`），需 `vpn_reissue` 服务（`runtime.py:301`）。
- **触发条件/关键**：`runtime.py:238` `if settings.deepseek_api_key:` 才装配 `vpn_diagnosis`/`vpn_adapter`/`vpn_reissue`；否则 `None`。
- **所需环境变量**：
  - 模型：`DEEPSEEK_API_KEY`（必，35 字符；进程内未设需 `load_dotenv`）、`LLM_BASE_URL`（默认 `https://api.deepseek.com`）、`LLM_MODEL`（默认 `deepseek-chat`）、`VPN_MOCK_DATA_PATH`（可选 Mock 数据 JSON）。
  - 平台：`DATABASE_URL`、`TEST_DATABASE_URL`（评测/集成）、`REDIS_URL`、`TENANT_TOKEN_SECRET`（`AUTH_MODE=dev` 必填 `settings.py:159-160`）。
  - 成本：`MODEL_INPUT_COST_PER_1K_USD`、`MODEL_OUTPUT_COST_PER_1K_USD`（`settings.py:209-210`；Prod 预算需配）、`TENANT_DAILY_BUDGET_USD`（`settings.py:206`）。
  - 限流/矩阵：`METRICS_ENABLED`、`RATE_LIMIT_*` 等（非 VPN 特有）。
- **注意**：真实模型 E2E 的**成本（M9）不被 app.py 的 astream 计量覆盖**（见 §3.9），需在 `agent.run` 内补 usage 埋点或改走统一计量。

---

## 5. 推荐分工方案与优先级（供后续成员直接采用）

> 建议 7 个角色按「先补评测与指标、再打通链路、最后集成/演示」的顺序推进；每个任务可并行，但相互依赖的串行。

### 优先级 P0（先把可量化口径立住）
- **eval-set-engineer**：S3(769 错误码) + S5(家庭/热点网络对比) + S6(账号锁定与 VPN 混淆歧义) 的样本扩充；给 `q` 明确 `vpn_fault` 映射 & 期望边界；同步改 `intake.py:295-320` 关键词与（如需要）`IntakePolicy`。
- **metrics-engineer**：补齐 M3（故障假设命中率，需在 `DiagnosisCommand.reason_codes` 或新增结构化字段 + 打分）、M5（客户平均排障轮次，在 `agent.run` 返回 `rounds`/`tool_calls`）、M9（VPN 诊断成本埋点 + P95 采集点重定义）。
- **e2e-engineer / integration-engineer**：把 `run_vpn_eval --database-url --require-db` 与 `run_knowledge_eval --database-url` 配 `compose.test.yml` 跑通，出**真实**引用支撑率/ACL 指标。

### 优先级 P1（打通「存在但未打通」）
- **integration-engineer**：把 `executor.execute_diagnosis_command`（`executor.py:120`）接入 `graph.vpn_diagnose_node`（`graph.py:408-464`），让非 must_handoff 的 `provide_steps/assign_agent/ask_customer` 真正落地工单状态机；同时保证 escalation 仅走 `team-service-desk` 且零自动回复。
- **e2e-engineer**：连接诊断→审批链，让 `request_approval`（reissue）在真实工单状态机上触发 `AWAITING_APPROVAL`（现已由 `app.py`+`api.py`+`reissue_service.py` 支撑，需与图串起）。

### 优先级 P2（ACL 与越权）
- **eval-set-engineer + integration-engineer**：S9 ACL 越权样本（跨租户工具越权、未审批 reissue、scope=agent 调 approve 动作），在 `run_vpn_eval` 加「越权拒绝率」并断言 =0。

### 优先级 P3（演示与验收收口）
- **demo-engineer**：更新 `docs/DEMO_SCRIPT.md` 走「VPN（含 769 排查/网络对比/版本过旧升级/审批式 reissue→回访关闭）」主线；对齐 `docs/product/vpn-v1-scope.md` 第 7 节口径。
- **reviewer**：最终核对所有「×」（未测/未打通）项，确保 `v1-report.md` 不把未验证项写为「已完成」（当前 `v1-report.md:72-75` 已明确未验证项）。

### 依赖关系（一句话）
`P0 评测口径 → P1 打通 executor/审批链 → P2 ACL 越权 → P3 演示/验收`。M3/M5/M9 需要 `P0` 的样本与埋点同时就位才能出数；M6/M9 需要 `P0` 的 db 模式先跑通。

---

## 6. 附：关键锚点速查（供后续引用）

- 8 项 VPN 字段定义：`docs/product/vpn-v1-scope.md:39-55`、`backend/knowledge/vpn_eval_cases.py:56-65`、`backend/seed_demo.py:54-69`
- VPN 故障 5 类：`backend/src/my_agent/helpdesk/intake.py:275-320`、`docs/product/vpn-v1-scope.md:21-37`
- 边界三态决策：`intake.py:349-372`、`run_vpn_eval.py:144-151`
- VPN 诊断命令与升级判定：`backend/vpn/models.py`、`backend/vpn/service.py:106 apply_handoff`
- 命令→状态机映射：`backend/vpn/executor.py:27-40`
- 审批式受控操作：`backend/vpn/approval.py`、`backend/vpn/reissue_service.py`、`backend/vpn/api.py`
- 只读 Mock 数据源：`backend/vpn/mock_adapter.py`（账户/网关/资产/事件/相似工单/知识/客户端版本）
- 治理注册：`backend/tool_governance.py:197-211`（`VPN_DIAGNOSIS_TOOLS`/`VPN_REISSUE_TOOLS`）
- 装配开关：`backend/runtime.py:238-262`（需 `DEEPSEEK_API_KEY`）
