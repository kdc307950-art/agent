# 阶段二前端验收：VPN 诊断结果展示 + 客户排查步骤回填 UI

> 面向整体验收。本文档记录 t6 前端在阶段二 VPN 客户处置闭环上的改动、后端契约核对
> （与 `backend/vpn/api_v2.py` / `diagnosis.py` / `closed_loop.py` 对齐）、
> 以及 build/test 的沙箱限制与外部复验路径。

---

## 1. 阶段二前端改动清单

全部改动位于 `frontend/src`，未触碰任何后端代码。

### 1.1 API 封装 —— `frontend/src/api/vpn.ts`（新增）

对齐 `backend/vpn/diagnosis_api.py` 落地的 4 条受控接口，统一经 `api()`（`client.ts`，
携带租户 token + `ApiError` 错误处理）：

| 前端函数 | 后端路由 | 请求体 | 响应 |
| --- | --- | --- | --- |
| `diagnoseVpn(ticketId, signal?)` | `POST /tickets/{id}/vpn/diagnose` | 无 | `{ run, result, dispatch }` |
| `getVpnDiagnosis(ticketId, signal?)` | `GET /tickets/{id}/vpn/diagnosis` | 无 | `VpnDiagnosisSnapshot` |
| `submitVpnActionResult(ticketId, actionId, {result, evidence?, details?})` | `POST /tickets/{id}/vpn/actions/{action_id}/result` | `{ result, evidence, details }` | `{ action_result, transition, re_diagnosis }` |
| `resumeVpnDiagnosis(ticketId, {comment})` | `POST /tickets/{id}/vpn/diagnose/resume` | `{ comment }` | `{ run, result, dispatch }` |

- `diagnose` / `resume` 实际后端端点**不接收** `operation_id` / `expected_version`，
  故前端不发送这些字段（与 Copilot/intake 的幂等字段不同，勿混淆）。
- `submitVpnActionResult` 的 `evidence` 为 `Record<string, unknown>`（dict）、
  `details` 为字符串。`result` 为自由文本。

统一出口：`frontend/src/api/index.ts` 已加 `export * from './vpn'`，自动汇入。

### 1.2 类型 —— `frontend/src/types.ts`（新增，对齐诊断领域契约）

- `VpnDiagnosisRun` —— `{ run_id, ticket_id, tenant_id?, fault, hypothesis, confidence,
  evidence[], ruled_out?: string[], next_action: string|null, reason_codes[], status,
  created_at, updated_at? }`。
- `VpnDiagnosisFinding` —— `{ tool_name, evidence, document_id?, document_version?,
  chunk_id?, title?, found? }`（证据项，来自只读工具返回的依据）。
- `VpnCustomerAction` —— `{ action_id, ticket_id?, tenant_id?, run_id?, title, instruction,
  expected_result, risk_level('low'|'medium'|'high'), requires_agent, status?, order?,
  created_at? }`。
- `VpnCustomerActionResult` —— `{ action_id, ticket_id?, tenant_id?, run_id?, result(自由文本),
  evidence(dict)?, details(string)?, submitted_by?, submitted_at }`。
- `VpnEscalation`、`VpnDiagnosisSnapshot`（`get_snapshot`：`{ ticket_id, latest_run, runs,
  actions, results, escalations }`）。
- 辅助类型：`VpnDiagnosisRunStatus`（diagnosing/completed/handed_off/failed/cancelled）、
  `VpnCustomerActionStatus`、`VpnEscalationStatus`、`VpnFault`、`VpnActionResult(=string)`。

### 1.3 UI 组件 —— `frontend/src/components/VpnDiagnosisPanel.tsx`（新增）

自包含诊断面板，消费 `GET /vpn/diagnosis` 的闭环保真（`latest_run` / `actions` / `results`）：

- **诊断结果展示**：故障类型 + 故障假设、支撑证据（`evidence[]`）、已排除项
  （`ruled_out` 字符串列表）、置信度、下一步动作、原因码。
- **客户排查步骤逐条回填**：对每条 `action` 可填「结果（自由文本，必填）」「证据（→ evidence dict）」
  「备注（→ details string）」，点击「提交结果」调用 `submitVpnActionResult`，成功后重新拉取快照。
- **回填记录时间线**：读 `results`，进入工单时间线。
- **操作**：无诊断时「发起 VPN 诊断」（`diagnoseVpn`）；`completed` 且有步骤时「再次诊断」
  （`resumeVpnDiagnosis`）。
- **健壮性**：ticketId 守卫 + `AbortController` 竞态防护；缺省态（`latest_run` 为 null）展示
  「发起诊断」；503 展示「VPN 诊断服务未配置」；409/其它错误经 `describeApiError` 统一文案。

### 1.4 挂载与样式

- `frontend/src/components/TicketDetail.tsx`：对 `it.vpn` 类别、`assigned` / `in_progress`
  状态的工单挂载 `<VpnDiagnosisPanel ticketId enabled />`（与既有 `CopilotPanel` 并列）。
- `frontend/src/App.css`：新增 `.vpn-panel` / `.vpn-step` / `.vpn-step-*` / `.vpn-risk-*` 等
  样式，复用既有 `copilot-block` / `requester` / `timeline` 风格。

### 1.5 前端单测

- `frontend/src/api/vpn.test.ts` —— 校验 4 条封装的路由 / 方法 / 请求体 / 响应结构。
- `frontend/src/components/VpnDiagnosisPanel.test.tsx` —— 校验快照渲染、发起诊断、
  回填提交、503 未配置态的展示。
- 对 `client` 的 `api()` 用 `vi.mock` 拦截，保留 `ApiError` / `describeApiError` 既有行为。

---

## 2. 后端契约核对备注

> 结论：前端与后端实际落地**一致**。清单如下，验收时可直接对照。

| 前端约定 | 后端依据 | 核对 |
| --- | --- | --- |
| `result` 为自由文本（非枚举 success/failed/skipped/blocked） | `VpnActionResultRequest.result: str`；`VpnCustomerActionResult.result: str` | ✅ |
| `evidence` 为 dict | `evidence: dict[str, Any]` | ✅ |
| `details` 为字符串 | `details: str` | ✅ |
| `ruled_out` 为 `string[]` | `ruled_out: list[str]`（reason_codes） | ✅ |
| 快照 `{ticket_id, latest_run, runs, actions, results, escalations}` | `closed_loop.get_snapshot` | ✅ |
| `risk_level` 用 low/medium/high | `RiskLevel`（StrEnum：low/medium/high） | ✅ |
| `diagnose`/`resume` 无 `operation_id`/`expected_version` 请求字段 | `diagnose_ticket` / `resume_diagnosis` 不接收该体 | ✅ |
| `VpnDiagnosisFinding` 字段 | `tool_name/evidence/document_id?/document_version?/chunk_id?/title?/found` | ✅ |
| `VpnCustomerAction` 必填字段 | `action_id/title/instruction/expected_result/risk_level/requires_agent` | ✅ |

说明：`VpnDiagnosisRun.tenant_id`、`VpnCustomerAction.status/order/created_at`、
`VpnCustomerActionResult.submitted_by` 等为后端返回的附加字段，前端类型标为可选，
不影响请求构造与渲染主路径。

---

## 3. 本地校验（tsc / lint）与 build/test 沙箱限制

### 3.1 已在本地通过

```bash
cd frontend
npm run typecheck   # tsc -b —— 通过，0 错误
npm run lint        # oxlint —— 通过，0 warning 0 error
```

### 3.2 build/test 被沙箱拦截（非代码问题）

`npm run build` 与 `npm run test` 在本沙箱**无法执行**，二者都在 Vite **配置加载阶段**
（`bundleAndLoadConfigFile` -> `optimizeSafeRealPathSync`）触发：

```
exec("net use", { windowsHide: true }, ...)
```

该子进程使用默认 piped stdio，被 pwsh 沙箱以 `spawn EPERM` 拦截。失败发生在**编译任何
项目代码之前**（未触及 `src/`），属环境限制，与本次改动的正确性无关，会同样拦截基线
`build`/`test`。

**证据（复现命令）**：

```bash
cd frontend
npm run build       # -> Startup Error / Error: spawn EPERM（Vite config 加载）
npm run test        # -> Startup Error / Error: spawn EPERM（Vite config 加载）
```

关键栈帧：`optimizeSafeRealPathSync` -> `exec("net use")` -> `spawn EPERM`。

### 3.3 外部复验路径（无 piped-stdio 限制）

请在 **Docker CI / 本地 node 直接执行**（不经过本沙箱）复验：

```bash
cd frontend
npm ci                    # 或 npm install
npm run typecheck         # tsc -b
npm run lint              # oxlint
npm run build             # tsc -b && vite build
npm run test              # vitest run
```

预期：4 条命令全部通过；`npm run test` 应包含 `vpn.test.ts` 与
`VpnDiagnosisPanel.test.tsx` 的用例。
