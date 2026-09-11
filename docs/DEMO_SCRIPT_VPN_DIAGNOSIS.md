# 演示脚本：VPN 报障 → 诊断 → 人工审批受控操作 → 回访关闭审计（5–10 分钟）

> 生成角色：demo-engineer（队伍 vpn-eval-milestone） · 任务：t6
> 日期：本轮演示链路制作（以仓库实际运行 / 代码核验为准）
> 适用环境：`infra/compose.demo.yml` 一键启动（migrate → seed → agent → web，浏览器访问 http://127.0.0.1:8000）。
> 与旧版 `docs/DEMO_SCRIPT.md` 的关系：旧版只覆盖「受理→分类→追问→SLA→派单→知识→解决→回访→关闭」，
> **不含** VPN Diagnosis Agent 的「假设/证据→执行步骤→回填→再诊断→人工审批」。本脚本是对验收口径的逐段补齐。

---

## 0. 结论速览（先看这里）

最终验收口径逐段映射到「是否真实走通」：

| 验收段 | 现状 | 标注 | 证据 |
|---|---|---|---|
| ① VPN 报障 | ✅ 真实走通 | **真实** | 受理图 `normalize/classify/dispatch`（`src/my_agent/helpdesk/graph.py`）；`POST /tickets` 建单 + `POST /tickets/{id}/intake` 受理 |
| ② 信息补全（8 项字段） | ✅ 真实走通 | **真实** | seed 种入租户 `it.vpn` 策略 8 字段（`backend/seed_demo.py:54-70`）；`completeness/clarify`（`graph.py:180/200`）追问 |
| ③ 故障假设与证据（诊断 Agent） | ⚠️ 命令只校验、**不落地** | **GAP** | `graph.vpn_diagnose_node` 只 `DiagnosisCommandValidator().validate`（`graph.py:409-432`），**未调用** `executor.execute_diagnosis_command`（`backend/vpn/executor.py:120`） |
| ④ 客户执行步骤 | ⚠️ 域支持（PROPOSE_ANSWER 草稿），**不落工单** | **GAP** | `executor.py:152-160` `provide_steps` 仅返回草稿；图桥接不调用 executor |
| ⑤ 回填结果 | ⚠️ 域支持（AWAITING_CUSTOMER→INTAKING），**无再诊断编排** | **模拟** | `domain.py:109-110`；`service.py:77` 重读消息机制具备、无自动闭环时序 |
| ⑥ 再诊断 / 升级 | ⚠️ 映射存在（escalate→QUEUE→team-service-desk），**未落库** | **GAP** | `executor.py:211-214` `_with_team_queue`；图桥接不调用 executor |
| ⑦ 人工审批受控操作 | ✅ 已接入生产 API；数据对齐后可真实走通 | **真实（需数据对齐）** | `app.py:325` 挂载 `vpn_reissue_router`；`approval.py` preflight/幂等/审计 + `POST /vpn/reissue[/approve|/reject]`；**实测 start→approve→CONFIRMED** |
| ⑧ 回访、关闭、审计 | ✅ 真实走通 | **真实** | `domain.py:144` RESOLVED→CLOSED；`POST /tickets/{id}/survey`（回访）；审计贯穿各段 |

**一句话结论**：从「诊断命令 → 工单状态机 / 审批」的**执行侧未打通**（GAP①/④/⑥，根因同一处：`graph.vpn_diagnose_node` 只校验、不调用 `executor.execute_diagnosis_command`）；「人工审批受控操作」作为**独立 HTTP 链路已接入**（⑦），但在默认 seed 数据下会因 **VPN 账号域与资产域不一致**而 preflight 失败，需按「最小补齐」对齐数据后真实走通。报障 / 补全 / 回访关闭审计三段完全真实。

---

## 1. 前置与准备（约 1 分钟）

```powershell
# 0) 走通⑦审批段前，先做「最小补齐」：把 docs/demo/vpn_mock_demo_data.json 挂进 agent 容器，
#    让 VPN 账号域(customer-1)与真实资产域(laptop-001 owner=customer-1)对齐。见 §4。
#    （若只演示 ①/②/⑧，可不做，直接跳到第 1 步。）

# 1) 起栈（migrate → seed → agent → web 由 compose 依赖顺序自动完成）
docker compose -f infra/compose.demo.yml up --build -d
docker compose -f infra/compose.demo.yml ps

# 2) 登录工作台（AUTH_MODE=dev；打开页面后选择对应固定演示身份）
# 员工：customer-1；IT 客服：agent-1；IT 管理员：admin-1
```

> 角色→scope（`backend/security.py:231-272`）：`customer`=`ticket:customer,asset:read`；`agent`=`ticket:agent,…`；
> `approver`=`ticket:agent,ticket:approve`（**⑦审批 approve/reject 必须用 approver 令牌**）。命令以 `docker … exec agent python -m …` 的访问方式以实际栈为准。
> 注意：web 通过 Nginx 把 `/api/*` 剥离前缀代理给 agent（`frontend/nginx.conf:9`），因此**所有 HTTP 调用都走 `http://127.0.0.1:8000/api/...`**，而 FastAPI 路由本身不带 `/api` 前缀。

---

## 2. 演示流程（约 8 分钟，逐段对应验收口径）

> 下面给出「浏览器」与「curl」两条路径，二选一即可；每段末尾标注**在哪看**（前端 / `/api` / 数据库 / 审计）。

### 段① VPN 报障（约 1 分钟）— 真实

浏览器：选择「员工」身份登录 → 新建工单 → 标题「VPN 无法连接」、描述「笔记本连不上公司 VPN，提示错误码 809」→ 关联资产 `laptop-001` → 提交。
curl（等价的建单 + 受理）：
```powershell
$TOKEN_CUSTOMER = '<customer-1 令牌>'
$TID = "demo-vpn-$(Get-Random)"
# 建单（直接落库）
curl -s -X POST "http://127.0.0.1:8000/api/tickets" -H "Authorization: Bearer $TOKEN_CUSTOMER" -H "Content-Type: application/json" `
  -d "{`"ticket_id`":`"$TID`",`"title`":`"VPN 无法连接`",`"description`":`"笔记本连不上公司 VPN，提示错误码 809`",`"asset_id`":`"laptop-001`"}"
# 启动受理工作流（分类/字段/SLA/路由）
curl -s -X POST "http://127.0.0.1:8000/api/tickets/$TID/intake" -H "Authorization: Bearer $TOKEN_CUSTOMER" -H "Content-Type: application/json" `
  -d "{`"operation_id`":`"op-demo-$(Get-Random)`",`"text`":`"VPN 无法连接，错误码 809`",`"fields`":{},`"expected_version`":0}"
```
预期：工单 `new` → 受理图自动分类为 **it.vpn**，命中 `sla-vpn`。
在哪看：前端「工单队列/详情」category=SLA；后端库 `tickets`（`tenant_id=demo`）。

### 段② 信息补全（8 项字段）（约 1.5 分钟）— 真实

由于 seed 给租户 `it.vpn` 配了必填 8 项（`device/operating_system/vpn_client/client_version/error_code/network/multi_user_impacted/recent_change`，`seed_demo.py:54-70`），受理进入「等待客户」并触发追问。
浏览器：工单详情出现补充信息表单 → 填写 8 项（device=`laptop-001`、OS=`Windows 11`、client=`公司客户端`、client_version=`3.4.2`、error_code=`809`、network=`办公网`、multi_user_impacted=`否`、recent_change=`升级客户端`）→ 提交 → `POST /tickets/{id}/resume` 恢复受理。
预期：`missing_fields` 清空，受理继续，进入派单/（若诊断节点装配）诊断。
在哪看：前端补充表单；库 `tickets.metadata` 的 8 项字段；`backend/seed_demo.py` 策略定义。

### 段③ 故障假设与证据（诊断 Agent）（约 1.5 分钟）— GAP

诊断 Agent（`VpnDiagnosisAgent`，`backend/vpn/agent.py`）在有 `DEEPSEEK_API_KEY` 时装配（`runtime.py:238`），图里 `vpn_diagnose_node` 在 `it.vpn` + 字段齐时触发，用真实模型调用 6 个只读工具（`tools.py:285`）收集证据并产出 `DiagnosisCommand`。
浏览器：工单详情出现「诊断」卡片/证据轨迹（工具调用 `tool_trace`、`tool_evidence`）；前端可看到模型给出的故障假设与网关/账号/历史工单证据。
预期（**注意 GAP**）：诊断 Agent 能产出结构化命令与证据，但 `graph.vpn_diagnose_node` **只做 `DiagnosisCommandValidator().validate`（只校验）**，命令作为 `vpn_diagnosis_command` 状态回写，**不触发**任何工单状态迁移。
在哪看：`/api` 的 `vpn_diagnosis_run`/`vpn_diagnosis_command`/`vpn_diagnosis_must_handoff` 状态字段；审计事件 `vpn_diagnosis_handoff`（仅转人工时）。
> 演示话术：诊断 Agent 已能给出「假设 + 证据」并把结论给坐席，但**把它做成的处置命令真正落到工单状态机（执行步骤/派单/升级/审批）这条链路尚未打通**——见 §4 最小补齐。

### 段④ 客户执行步骤（provide_steps）（约 0.5 分钟）— GAP

诊断 Agent 若产出 `provide_steps`，映射为 `TicketAction.PROPOSE_ANSWER`（`executor.py:27-33`），语义是**仅生成排查步骤草稿、不直接发消息**（`executor.py:152-160` `DRAFT_ONLY_COMMANDS`）。
预期：只产生草稿文本；因图桥接未接 executor，草稿**不会真正落工单**。演示时通过前端说明「草稿已生成（仅坐席可见，未发送）」。
在哪看：`DiagnosisCommand.content`（诊断返回）；`domain` 的 `ANSWER_PROPOSED` 状态被**未触发**（GAP）。

### 段⑤ 回填结果（约 0.5 分钟）— 模拟

域支持：客户补充信息（`AWAITING_CUSTOMER + PROVIDE_INFORMATION → INTAKING`，`domain.py:109-110`）；`service.prepare_context` 会重读工单消息（`service.py:77-82`）。
预期：客户回填后工单返回受理态，上下文更新。**无自动「回填后再诊断」闭环时序**（演示为手动再触发诊断）。
在哪看：`POST /tickets/{id}/resume`；库 `tickets` 状态；`service.py` 重读逻辑。

### 段⑥ 再诊断 / 升级（escalate_incident）（约 0.5 分钟）— GAP

`escalate_incident` 映射为 `TicketAction.QUEUE` → 进 `team-service-desk` 人工队列且**禁止自动回复**（`executor.py:38-40/211-214`、`domain.py:107-108`）。
预期：因图桥接未接 executor，`escalate` 命令**不落库**；升级要么由受理图 `must_escalate` 触发（真实），要么只能看到诊断侧 `must_handoff` 标记（`graph.py:455-464`）。
在哪看：`vpn_diagnosis_handoff_reasons`（诊断层标记）；「转人工/升级」事件（受理层 `must_escalate` 真实，诊断层命令落地为 GAP）。

### 段⑦ 人工审批的受控操作（reissue_vpn_config）（约 2 分钟）— 真实（需数据对齐）

审批链已完整接入生产 API（`approval.py` + `reissue_service.py` + `api.py` + `app.py:325`）。它**不依赖真实模型**（用 `MockVpnAdapter` 确定性前置校验 + `reissue_config`），因此可稳定演示。触发 → 审批通过 → 下发并确认。

```powershell
$TOKEN_AGENT    = '<agent-1 令牌>'    # ticket:agent，可发起审批
$TOKEN_APPROVER = '<admin-1 令牌>'    # ticket:approve，可批准/拒绝

# 1) 触发审批（不执行副作用；前置校验通过→登记 PENDING 并审计 vpn_reissue_started）
$USER='customer-1'; $ASSET='laptop-001'; $TICKET=$TID; $VER='v2.4.1'
$RESP = curl -s -X POST "http://127.0.0.1:8000/api/vpn/reissue" `
  -H "Authorization: Bearer $TOKEN_AGENT" -H "Content-Type: application/json" `
  -d "{`"action`":`"reissue_vpn_config`",`"user_id`":`"$USER`",`"asset_id`":`"$ASSET`",`"ticket_id`":`"$TICKET`",`"client_version`":`"$VER`",`"target_version`":`"$VER`",`"reason_codes`":[`"client_version_outdated`"]}"
$RESP   # 预期 status=pending, approval_required=true, preflight.ok=true
$IDEMPOTENCY = ($RESP | ConvertFrom-Json).idempotency_key

# 2) 审批通过 → 受控执行 → 落终态（status=confirmed, delivered/confirmed=true）
curl -s -X POST "http://127.0.0.1:8000/api/vpn/reissue/$IDEMPOTENCY/approve" `
  -H "Authorization: Bearer $TOKEN_APPROVER" -H "Content-Type: application/json" `
  -d "{`"action`":`"reissue_vpn_config`",`"user_id`":`"$USER`",`"asset_id`":`"$ASSET`",`"ticket_id`":`"$TICKET`",`"client_version`":`"$VER`"}"

# 3) 查询审批/执行结果（可选）
curl -s "http://127.0.0.1:8000/api/vpn/reissue/$IDEMPOTENCY" -H "Authorization: Bearer $TOKEN_AGENT"
```
演示「**拒绝**」路径（人工接管）：
```powershell
curl -s -X POST "http://127.0.0.1:8000/api/vpn/reissue/$IDEMPOTENCY/reject" `
  -H "Authorization: Bearer $TOKEN_APPROVER" -H "Content-Type: application/json" `
  -d "{`"action`":`"reissue_vpn_config`",`"user_id`":`"$USER`",`"asset_id`":`"$ASSET`",`"ticket_id`":`"$TICKET`",`"client_version`":`"$VER`"}"
# 预期 status=rejected；工单回到可接管仲裁态（AWAITING_APPROVAL→REJECT）
```
预期结果（**实测**）：`start`→`pending/approval_required=true`；`approve`→`ok=true, status=confirmed, delivered=true, confirmed=true`。
在哪看：`/api/vpn/reissue/*`；审计事件 `vpn_reissue_started/approved/rejected/executed`；内置 `ReissueRegistry` 状态；`workflow_operation`（若工单仓储存在则落 started/committed 终态）；工单状态 `AWAITING_APPROVAL`→`IN_PROGRESS`（审批通过 `api.py:183` domain 迁移）。
> **关键坑**：默认 seed 数据下（真实资产 owner=`customer-1`、内置 Mock 账号=`user-042`），preflight 会返回 `asset_owner_mismatch`（user-042+laptop-001）或 `account_not_active`（customer-1 无 VPN 账号）。**必须先做 §4 数据对齐**，否则 `start` 返回 `status=failed` + `fail_reasons`。

### 段⑧ 回访、关闭、审计（约 1 分钟）— 真实

工单解决后：客服发起回访（满意度调查），员工提交评分，客服关闭工单。
```powershell
$TOKEN_AGENT='<agent-1 令牌>'
# 状态流转：queued -> assigned -> in_progress -> resolved -> closed（浏览器「接单/开始处理/标记解决/关闭工单」）
# 回访：客服发起满意度调查
curl -s -X POST "http://127.0.0.1:8000/api/tickets/$TID/survey" -H "Authorization: Bearer $TOKEN_AGENT" -H "Content-Type: application/json" -d '{}'
```
预期：工单 `resolved`→`closed`；满意度回访生成（`satisfaction_surveys` + Outbox 事件）；全程审计。
在哪看：库 `tickets`/`satisfaction_surveys`/`outbox`；审计事件（`audit`）。
> 注：回访/关闭走**受理图主生命周期**（`domain.py:144`），与 VPN 诊断/审批独立，旧 `DEMO_SCRIPT.md` 已覆盖。

---

## 3. 无模型降级演示路径（若 `DEEPSEEK_API_KEY` 无效 / 无真实模型）

不依赖真实模型也能演示大半条链路，因为报障/补全/派单/审批/回访关闭都是**确定性**组件：

1. **① 报障 + ② 8 项字段补全**：完全确定性，真实走通。
2. **③⑥ 诊断 Agent**：无有效 key 时 `runtime.py:238` 不装配 `vpn_diagnosis` → 图退化为 `dispatch → compose_answer`（`graph.py:488-490`），诊断节点不做；**改用「受理层 boundary 三态」确定性入口**：构造 `must_escalate` 工单（如群体故障/敏感词/无依据）→ 受理图直接转人工（可演示「升级/无自动回复」）。
3. **⑦ 人工审批**：审批链不依赖模型（`MockVpnAdapter` 确定性），**在做了 §4 数据对齐后仍可真实走通**——这是无模型演示里最有价值的一段。
4. **⑧ 回访关闭**：确定性，真实走通。

降级演示脚本：`起栈（置 DEEPSEEK_API_KEY=占位，不装配 VPN 诊断）→ 段①②⑧ → 段⑦审批（需数据对齐）→ 用受理层 must_escalate 演示升级 → 说明段③④⑥ 的诊断命令落地为已知 GAP`。

---

## 4. 最小补齐方案（当前为 GAP，本任务未改业务代码）

**GAP-A（代码，主因）—— 诊断命令不落地**：`graph.vpn_diagnose_node`（`graph.py:408-432`）只校验、不执行。
最小改动：在 `graph.py` 非 `must_handoff` 且命令通过校验后，**调用 `executor.execute_diagnosis_command(command, runtime=runtime, run_context=run_context, ticket_id=...)`** 把 `provide_steps/assign_agent/escalate_incident/request_approval` 真正落到工单状态机；`escalate_incident` 走 `team-service-desk` 且禁止自动回复，`request_approval` 经 `approval.dispatch_approved_action` 触发审批。改动后需跑 `tests/test_vpn_*.py`（executor/models/api/reissue_service）+ 相关生命周期测试确认无回归。**本任务未改，标注为 GAP。**

**GAP-B（数据）—— 账号域与资产域不一致**：默认内置 `MockVpnAdapter` 账号=`user-042`，而真实 `it_assets` 的 owner=`customer-1`，导致审批 preflight 恒失败。最小补齐 = 让两域对齐，**本任务已提供** `docs/demo/vpn_mock_demo_data.json`（customer-1 active VPN 账号 + client_config + 资产/网关/事件/知识）。用法：把该文件挂进 agent 容器并设 `VPN_MOCK_DATA_PATH`：
```yaml
# infra/compose.demo.yml 的 agent 服务追加：
#   environment:
#     VPN_MOCK_DATA_PATH: /app/data/vpn_mock_demo_data.json
#   volumes:
#     - ../docs/demo/vpn_mock_demo_data.json:/app/data/vpn_mock_demo_data.json:ro
```
> 已用该数据在真实 test 栈 PostgreSQL(55436) + `MockVpnAdapter(data=…)` 实测：`start→pending(approval_required=true, preflight.ok=true)`、`approve→confirmed(delivered/confirmed=true)`。证明对齐数据后 ⑦ 真实走通。

（可选）**GAP-C（编排）**：把「回填后再诊断」做成自动时序（客户补充后自动再跑一次 `vpn_diagnose_node`），使 ⑤ 从「模拟」升级为「真实闭环」。

---

## 5. 验收检查点清单（逐条可勾选）

- [ ] **①报障**：`POST /tickets` 建单 + `POST /tickets/{id}/intake` 受理，工单 `new`；前端可见。
- [ ] **②补全**：it.vpn 命中 8 项必填字段，缺失触发追问；补齐后 `missing_fields` 清空继续。
- [ ] **③假设与证据**：诊断 Agent 命中 it.vpn+字段齐时触发，产出 `DiagnosisCommand` + `tool_trace/tool_evidence`（前端/状态可见）。
- [ ] **④执行步骤**：`provide_steps` 生成草稿、未发送（仅坐席可见）；命令落地标注为 GAP（未落工单）。
- [ ] **⑤回填**：客户补充后工单回 `INTAKING`，上下文更新（无自动再诊断，标注模拟）。
- [ ] **⑥再诊断/升级**：`escalate_incident`→`QUEUE`→`team-service-desk` 映射存在但**未落库**（GAP）；受理层 `must_escalate` 转人工真实可演示。
- [ ] **⑦人工审批**：`POST /api/vpn/reissue`→`pending`，`/approve`→`confirmed`；`/reject`→`rejected`；幂等键防重复执行；`AWAITING_APPROVAL`→`IN_PROGRESS`。**需先做 §4 数据对齐（GAP-B），否则 preflight 失败。**
- [ ] **⑧回访关闭审计**：`resolved→closed`；满意度回访生成；全程审计事件可查。
- [ ] **审计**：各关键点有 `audit.record_event`（`vpn_reissue_*`、`vpn_diagnosis_handoff`、`model_usage`…）。
- [ ] **账户/权限**：customer 只见自己工单；approver 才能批准（`ticket:approve`）。
- [ ] **时长**：整条脚本 ≈ 8 分钟（含准备），落在 5–10 分钟窗口。

---

## 6. 复现 / 实测记录（本任务的实际核验）

| 验证项 | 结果 |
|---|---|
| 代码核验：`app.py:325` 挂载 `vpn_reissue_router`；`approval.py` preflight/幂等/审计；`reissue_service.py` 编排 | ✅ 审批链结构完整、已接入生产 API |
| 代码核验：`graph.vpn_diagnose_node` 只 `DiagnosisCommandValidator().validate`，**未调用** `execute_diagnosis_command` | ✅ 确认诊断命令不落地（GAP-A） |
| 实测（test 栈 PG 55436 + `MockVpnAdapter`）：默认 seed 数据审批 preflight | ❌ 全不通过：`asset_owner_mismatch` / `account_not_active`（账号域 user-042 vs 资产域 customer-1）|
| 实测（test 栈 PG 55436 + 注入对齐数据 `customer-1`）：`VpnReissueService.start→approve` | ✅ `start=pending(approval_required=true)`，`approve=confirmed(delivered/confirmed=true)` |
| 结构核验：诊断 Agent 依赖真实模型（`runtime.py:251` `ChatOpenAI`）；`settings.deepseek_api_key` 非空才装配 | ✅ 无有效 key 时诊断/审批均为 `None`，API 503 |

---

## 7. 清理

```powershell
docker compose -f infra/compose.demo.yml down -v
```

> 与旧 `docs/DEMO_SCRIPT.md` 的关系：本脚本是**验收口径的 VPN 诊断 + 审批闭环补充**；旧脚本的「受理→…→回访→关闭」仍是 ①/②/⑧ 的可靠基线。建议后续把本脚本合入统一 DEMO，并保留「诊断命令落地」与「账号/资产域对齐」两处 GAP 提示。
