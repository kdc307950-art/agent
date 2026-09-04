# VPN Diagnosis Agent 契约锚点文档

> 本文件是后续实现的**唯一锚点**。所有接口名、枚举值、工具签名、升级判定、治理注册项均以此为准，
> 实现不得臆造或偏离。所有命名与行为来自已验证的现有代码库（见「0. 现有架构锚点」），仅新增
> VPN Diagnosis Agent 自己的领域约定（`DiagnosisCommandType` / `DiagnosisCommand` /
> `DiagnosisEvaluation` / 6 只读工具 / `MockVpnAdapter` / `VPN_DIAGNOSIS_TOOLS`）。
>
> 一句话：**本 agent 产出「结构化处置命令」，而不是像 `ResolutionCopilot` 那样产出「回复草稿」；
> 且用 `MockVpnAdapter` 隔离真实 VPN 后台，并执行比既有自动建议更严格的升级判定。**

---

## 0. 现有架构锚点（已核实，不得改名）

以下符号均已存在于代码库，本 agent 必须基于它们扩展，不得重命名或绕过：

| 现有事实 | 位置 | 锚点 |
| --- | --- | --- |
| 工具策略 | `backend/tool_governance.py` | `ToolPolicy(name, required_scopes, timeout_seconds, max_input_chars, retryable, side_effect)`；`DEFAULT_TOOL_POLICIES: dict[str, ToolPolicy]`；`RESOLUTION_COPILOT_TOOLS: frozenset[str]` |
| 治理主入口 | `backend/tool_governance.py` | `ToolGovernance.awrap_tool_call` 统一执行「scope 校验 → 租户 allowlist → 输入长度 → 超时/重试 → 审计 → 指标」 |
| 有界只读 Agent 榜样 | `backend/copilot/agent.py` | `ResolutionCopilot`（手动循环）+ `CopilotLimits(max_rounds, max_tool_calls, max_tool_calls_per_round, max_context_items, single_tool_timeout_seconds, total_timeout_seconds)` |
| 治理适配 | `backend/copilot/tool_adapter.py` | `governed_invoke(...)` → `ToolInvocationResult(ok, content, evidence, status, denied_reason, error_code)` |
| 上下文→生成→门禁 | `backend/copilot/service.py` | `CopilotService.apply_gate`（引用白名单/敏感类别/置信度/租户）；`MIN_CONFIDENCE = 0.80` |
| 结构化模型 | `backend/copilot/models.py` | `extra="forbid"`、`auto_reply: Literal[False]` |
| 确定性状态机 | `src/my_agent/helpdesk/domain.py` | `TicketStatus` / `TicketAction` / `ActorType` 枚举、`_TRANSITIONS`、`transition_ticket`、`assert_actor_authorized`、`required_scopes` |
| VPN 受理边界 | `src/my_agent/helpdesk/intake.py` | `VPN_FAULT_*`（5 类）、`BOUNDARY_AUTO_SUGGEST / BOUNDARY_MUST_ASK / BOUNDARY_MUST_ESCALATE`、`boundary_vpn(...)`、`classify_vpn_fault(...)`、`_SENSITIVE_TERMS` / `_HIGH_IMPACT_TERMS` |
| VPN 范围 | `docs/product/vpn-v1-scope.md` | 第 2/3/4/5/6 节（fixed 5 类故障、8 项固定字段、3 态边界矩阵、主链路、非目标：**不做真实 VPN 自动诊断**） |
| 租户隔离 | `backend/run_context.py` | `RunContext(tenant_id, scopes, allowed_tools, remaining_seconds, ...)`；工具实现从 `runtime.context` 取租户，不信任入参 |
| 受理配置 | `backend/ticket_intake.py` | `intake_config()` 构造 `thread_id = "helpdesk:{tenant}:{ticket}"` |
| 运行时装配 | `backend/runtime.py` | `AgentRuntime` dataclass；`runtime_context()`；Copilot 在 `if settings.deepseek_api_key:` 下用 `ChatOpenAI` 构造 `ResolutionCopilot` 并注入 `copilot` 字段 |

> **本 agent 应注册为一个 `AgentRuntime` 字段**（例如新增 `vpn_diagnosis: VpnDiagnosisService | None`），
> 装配方式与现有 `copilot` 完全对称：仅在 `settings.deepseek_api_key` 存在时构造，否则为 `None`。

---

## 1. `DiagnosisCommandType` 枚举（允许命令）

```python
class DiagnosisCommandType(StrEnum):
    ASK_CUSTOMER        = "ask_customer"        # 向客户追问缺失信息
    PROVIDE_STEPS       = "provide_steps"       # 提供排查步骤（仅草稿，不直接发消息）
    ASSIGN_AGENT        = "assign_agent"        # 派单给坐席
    ESCALATE_INCIDENT   = "escalate_incident"   # 升级为事件/转人工队列（不回复）
    REQUEST_APPROVAL    = "request_approval"    # 请求审批
```

### 1.1 禁止命令 `FORBIDDEN_COMMANDS`（永远不能由 agent 产出）

```python
FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
    "reset_password",          # 重置密码
    "unlock_account",          # 解锁账号
    "grant_vpn_permission",    # 授予 VPN 权限
    "modify_vpn_config",       # 修改 VPN 配置
    "restart_gateway",         # 重启网关
    "close_ticket",            # 关闭工单
    "send_customer_message",   # 直接给客户发消息
})
```

> 这些命令即使被模型产出，`DiagnosisCommand` 在 `model_validator` / executor 层也必须拒绝
> （见 §2 与 §8），且**永远不产生任何副作用**。语义上它们对应「账号/权限修改」「网关/配置变更」
> 「关闭工单」「代发消息」——均属于必须人工操作的高风险动作，本 agent 只做只读诊断。

---

## 2. `DiagnosisCommand` Pydantic 模型

```python
class DiagnosisCommand(BaseModel):
    """单条结构化处置命令；extra="forbid" 禁止自由字段，杜绝模型文本直出业务命令。"""
    model_config = ConfigDict(extra="forbid")

    command: DiagnosisCommandType
    content: str                       # 面向坐席/客户的命令文本（解释性，非业务字段）
    payload: dict[str, Any]            # 命令参数（如 assign_agent 的目标 team_id）
    reason_codes: list[str]            # 决策依据编码（审计用）
    confidence: float = Field(ge=0.0, le=1.0)  # 0..1

    @model_validator(mode="after")
    def _reject_forbidden(self) -> DiagnosisCommand:
        # 若 command 落在 FORBIDDEN_COMMANDS / 非允许集合内 -> 抛 ValueError（executor 兜底拒绝）
        ...
```

**约束要点：**
- `extra="forbid"`：与 `backend/copilot/models.py` 的 `CopilotRequest/CopilotResult` 一致，字段之外的模型输出一律拒绝。
- `confidence` 必须落在 `[0.0, 1.0]`。
- `command` 只能是 `DiagnosisCommandType` 的 5 个允许值；属禁止命令的值在模型校验层即拒绝。

---

## 3. 升级判定（`DiagnosisEvaluation` / `evaluate_handoff`）

四种**必须转人工**场景，返回 `must_handoff` + `handoff_reasons`：

```python
class HandoffReason(StrEnum):
    NO_EVIDENCE         = "no_evidence"         # 证据不足：工具未返回可支撑结论的依据
    LOW_CONFIDENCE      = "low_confidence"      # 置信度低于阈值（复用/对齐 MIN_CONFIDENCE=0.80）
    MULTI_USER_IMPACT   = "multi_user_impact"   # 群体/多用户影响（对应 vpn_fault=multi_user_impact）
    IDENTITY_MISSING    = "identity_missing"    # 身份缺失：无法确认用户/资产/账号归属


class DiagnosisEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    must_handoff: bool
    handoff_reasons: list[HandoffReason]


def evaluate_handoff(*, evidence: ..., fault: str, confidence: float, identity_ok: bool) -> DiagnosisEvaluation:
    ...
```

**判定条件（确定性，显式、可单测，顺序即优先级）：**

| 场景 | 判定条件 | 结果 |
| --- | --- | --- |
| `multi_user_impact` | `fault == "multi_user_impact"` | `must_handoff=True`，追加 `multi_user_impact` |
| `identity_missing` | 用户/资产/账号归属无法确认（`identity_ok=False`） | `must_handoff=True`，追加 `identity_missing` |
| `no_evidence` | 只读工具未返回任何可支撑结论的依据 | `must_handoff=True`，追加 `no_evidence` |
| `low_confidence` | `confidence < 0.80`（对齐 `copilot.service.MIN_CONFIDENCE`） | `must_handoff=True`，追加 `low_confidence` |
| 其余 | 以上均不满足且证据充分、置信度≥0.80、身份明确 | `must_handoff=False`，`handoff_reasons=[]` |

> **关系锚点**：本判定比 `boundary_vpn(...)` 更严格——`boundary_vpn` 中 `must_escalate` 覆盖
> 「字段不全/认证/群体/敏感高影响/无依据」，而本 agent 额外把「身份缺失」与「低置信度」显式
> 升级为独立的人工条件。二者一致处：`multi_user_impact` 与 `no_evidence`（`boundary_vpn` 的
> `must_escalate`）必然转人工。

---

## 4. 6 个只读工具（契约签名）

**全部满足**：`side_effect=False`、scope=`frozenset({"ticket:agent"})`、租户从 `RunContext` 取
（`runtime.context.tenant_id`），不信任入参。统一返回 **JSON 字符串**，形如
`{"content": 展示文本, ...}`（`content` 给模型/坐席看，与 `copilot.tools.search_knowledge` 的契约对齐）。

| 工具名 | 签名 | 参数 | 返回 JSON | 数据源 |
| --- | --- | --- | --- | --- |
| `search_vpn_knowledge` | `(query: str, limit: int = 5)` | `query` 必填、≤1024 字符；`limit` ∈ [1,20] | `{"content": ...}`（可选扩展 `evidence` 供引用） | 知识库（复用 `runtime.knowledge_retriever` / `KnowledgeRetriever`） |
| `get_asset` | `(asset_id: str \| None = None, query: str = "", limit: int = 10)` | `asset_id` 或 `query` 二者提供其一 | `{"content": 资产文本, ...}` | `runtime.assets`（对齐 `copilot.tools.search_assets`） |
| `get_vpn_account_status` | `(user_id: str)` | `user_id` 必填、≤128 字符 | `{"content": 账号状态文本, ...}` | `MockVpnAdapter.get_account_status` |
| `get_vpn_gateway_status` | `(gateway_id: str \| None = None, region: str \| None = None)` | `gateway_id` 或 `region` 其一 | `{"content": 网关状态文本, ...}` | `MockVpnAdapter.get_gateway_status` |
| `get_recent_similar_tickets` | `(user_id: str, fault: str \| None = None, limit: int = 5)` | `user_id` 必填；`fault` 可选（对齐 `VPN_FAULT_*`）；`limit` ∈ [1,20] | `{"content": 历史相似工单文本, ...}` | `runtime.tickets`（对齐 `copilot.tools.get_ticket_history`） |
| `get_incident_status` | `(incident_id: str)` | `incident_id` 必填 | `{"content": 事件状态文本, ...}` | `MockVpnAdapter.get_incident_status` |

**逐条示例（仅示意 `content` 形态，实现以对应数据源为准）：**

```
search_vpn_knowledge -> {"content": "知识库命中：…", "evidence": [...]}
get_asset           -> {"content": "资产：asset-001 laptop-001 (laptop) 状态=active …"}
get_vpn_account_status -> {"content": "账号 user-042 状态=active, 过期=2026-12-31"}
get_vpn_gateway_status -> {"content": "网关 gw-cn-north region=north status=up"}
get_recent_similar_tickets -> {"content": "历史工单： #T-102 [resolved] it.vpn | VPN 频繁掉线"}
get_incident_status -> {"content": "事件 INC-9 status=monitoring severity=major"}
```

---

## 5. `MockVpnAdapter` 接口

可配置的读适配器，初期模拟 VPN 网关/账号目录/资产。**全部只读**；数据可从配置加载
（Python dict 或 JSON 文件路径）。

```python
class MockVpnAdapter:
    """只读 Mock 适配器：隔离真实 VPN 后台，供 VPN Diagnosis Agent 查询。

    初始化：MockVpnAdapter(config: dict | str, ...)  —— config 为 dict 或 JSON 文件路径。
    数据来源（示例键）：accounts / gateways / assets / incidents / knowledge。
    """

    async def get_account_status(self, user_id: str) -> dict[str, Any]: ...
    async def get_gateway_status(self, gateway_id: str | None = None, region: str | None = None) -> dict[str, Any]: ...
    async def get_asset(self, asset_id: str | None = None, query: str = "") -> dict[str, Any]: ...
    async def get_incident_status(self, incident_id: str) -> dict[str, Any]: ...
    async def get_similar_tickets(self, user_id: str, fault: str | None = None) -> dict[str, Any]: ...
    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]: ...
```

> 关键设计：这 6 个方法把「真实后台调用」收口到 `MockVpnAdapter`，与 `docs/product/vpn-v1-scope.md`
> 第 6 节「**不做真实 VPN 自动诊断**」一致——mock 只提供只读状态，不模拟排障/修复/后台操作。
> 工具实现应通过 `runtime` 持有的 `vpn_adapter` 访问它，而非直接耦合真实网关 SDK。

---

## 6. 有界 Agent 限制

对齐 `backend/copilot/agent.py` 的 `CopilotLimits` 模式，新增本 agent 的常数/不可变限制：

```python
# VPN Diagnosis Agent 有界限制（PRD 初始值）
DIAGNOSIS_MAX_ROUNDS = 3            # 最大轮次
DIAGNOSIS_MAX_TOOL_CALLS = 8        # 总工具调用上限
DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND = 2
DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS = 3.0
DIAGNOSIS_TOTAL_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class DiagnosisLimits:
    max_rounds: int = DIAGNOSIS_MAX_ROUNDS
    max_tool_calls: int = DIAGNOSIS_MAX_TOOL_CALLS
    max_tool_calls_per_round: int = DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND
    single_tool_timeout_seconds: float = DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS
    total_timeout_seconds: float = DIAGNOSIS_TOTAL_TIMEOUT_SECONDS
    # __post_init__ 校验正数/有限值，与 CopilotLimits 一致
```

> **硬限制语义**：达到任一上限即终止本轮并标记错误，**不产生任何命令**（交给上层按 `must_handoff`
> 转人工）。单工具超时/总超时不拖垮工单主流程；所有工具调用经 `governed_invoke` 走治理层。

---

## 7. 治理注册

6 个只读工具的 `ToolPolicy` 加入 `DEFAULT_TOOL_POLICIES`（或独立策略表），并新增
`VPN_DIAGNOSIS_TOOLS` 只读 profile：

```python
# backend/tool_governance.py（新增注册项）
DEFAULT_TOOL_POLICIES["search_vpn_knowledge"] = ToolPolicy(
    name="search_vpn_knowledge",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=5.0, max_input_chars=1_024,
    retryable=True, side_effect=False,
)
DEFAULT_TOOL_POLICIES["get_asset"] = ToolPolicy(
    name="get_asset",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=3.0, max_input_chars=512,
    retryable=True, side_effect=False,
)
DEFAULT_TOOL_POLICIES["get_vpn_account_status"] = ToolPolicy(
    name="get_vpn_account_status",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=3.0, max_input_chars=128,
    retryable=True, side_effect=False,
)
DEFAULT_TOOL_POLICIES["get_vpn_gateway_status"] = ToolPolicy(
    name="get_vpn_gateway_status",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=3.0, max_input_chars=256,
    retryable=True, side_effect=False,
)
DEFAULT_TOOL_POLICIES["get_recent_similar_tickets"] = ToolPolicy(
    name="get_recent_similar_tickets",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=3.0, max_input_chars=512,
    retryable=True, side_effect=False,
)
DEFAULT_TOOL_POLICIES["get_incident_status"] = ToolPolicy(
    name="get_incident_status",
    required_scopes=frozenset({"ticket:agent"}),
    timeout_seconds=3.0, max_input_chars=128,
    retryable=True, side_effect=False,
)

# 独立 profile：VPN 只读工具白名单（与 RESOLUTION_COPILOT_TOOLS 用法一致）
VPN_DIAGNOSIS_TOOLS: frozenset[str] = frozenset({
    "search_vpn_knowledge",
    "get_asset",
    "get_vpn_account_status",
    "get_vpn_gateway_status",
    "get_recent_similar_tickets",
    "get_incident_status",
})
```

**治理校验链（经 `ToolGovernance.awrap_tool_call`，逐项强制执行）**：scope 校验 → 租户 allowlist →
输入长度 → 超时/重试 → 审计 → 指标。运行期通过 `RunContext.allowed_tools` 注入
`VPN_DIAGNOSIS_TOOLS`（或租户 allowlist），模型无法绕过 profile 调用未授权工具（对齐
`copilot/tool_adapter.COPILOT_ALLOWED_TOOLS` 的防线模式）。

> **不可变性**：模型伪造 `reset_password`/`send_message` 等未注册/禁止工具时，治理层直接拒绝并
> 记 `denied`（`denied_scope`/`denied_unregistered` 等结构化 error_code）。

---

## 8. 业务层校验/执行（executor）

`executor` 对 `DiagnosisCommand` 做两层校验，并映射到 `domain.TicketAction`。

### 8.1 校验

1. **集合校验**：`command.command` 必须在允许集合内（`DiagnosisCommandType` 的 5 个值）。
2. **禁止命令拒绝**：落在 `FORBIDDEN_COMMANDS`（或 `DiagnosisCommand` 模型校验已拒绝）→ 拒绝并**留审计**，
   **不产生任何副作用、不发任何消息、不改工单状态、不调用业务动作**。

### 8.2 命令 → 状态机映射表

| `DiagnosisCommand.command` | 映射 `domain.TicketAction` | 语义 / 系统行为 |
| --- | --- | --- |
| `ask_customer` | `TicketAction.REQUEST_INFORMATION` | 向客户追问；工单进入 `AWAITING_CUSTOMER` |
| `provide_steps` | `TicketAction.PROPOSE_ANSWER` | 生成排查步骤草稿 → 工单进入 `ANSWER_PROPOSED`，**仅草稿，不直接发消息** |
| `assign_agent` | `TicketAction.ASSIGN` | 派单给坐席 → 工单进入 `ASSIGNED`（`payload.team_id`/`user_id`） |
| `escalate_incident` | `TicketAction.QUEUE` | 进人工队列 `team-service-desk` 人工接管，**不回复**（复用 `_TRANSITIONS`: `(...QUEUE) -> QUEUED`） |
| `request_approval` | `TicketAction.REQUEST_APPROVAL` | 请求审批 → 工单进入 `AWAITING_APPROVAL` |

> 映射说明：
> - `ask_customer`/`assign_agent`/`request_approval` 直接对应 `domain._TRANSITIONS` 中合法的
>   `REQUEST_INFORMATION`/`ASSIGN`/`REQUEST_APPROVAL` 动作。
> - `provide_steps` 采用 `PROPOSE_ANSWER`（`CLASSIFIED → ANSWER_PROPOSED`），语义就是「给出建议草稿
>   但不直接发送」，与本 agent「只诊断、不代发消息」的边界一致。
> - `escalate_incident` 映射为 `QUEUE` 到服务台人工队列，且禁止任何自动回复；如需额外标注事件，
>   可在 `payload` 中携带（如 `event_id`/`severity`），但**不产生独立的变更/授权副作用**。

### 8.3 executor 伪代码

```python
async def execute_diagnosis_command(command: DiagnosisCommand, *, runtime, run_context) -> ExecResult:
    # 1) 集合与禁止校验
    if command.command not in ALLOWED_COMMANDS or str(command.command) in FORBIDDEN_COMMANDS:
        await audit.record_event(run_context, "vpn_command_rejected",
                                 payload={"command": str(command.command)})
        return ExecResult(ok=False, reason="forbidden_command")

    # 2) 组装 TicketCommand（actor_type=AGENT，作用域 ticket:agent），交给状态机
    ticket_action = COMMAND_TO_ACTION[command.command]
    ticket_cmd = TicketCommand(
        ticket_id=...,
        action=ticket_action,
        actor_type=ActorType.AGENT,
        actor_id=...,
        expected_version=...,
        payload=command.payload,
    )
    # 3) 经 transition_ticket / 仓储 transition_many 执行（乐观锁+scope）
    ...

    # 4) 仅当 command == escalate_incident 时进人工队列（team-service-desk），且禁止自动发消息
```

> 一致性保证：executor 与 `domain.transition_ticket`/`assert_actor_authorized` 走同一张状态机与
> 权限表，任何入口对同一命令的判定一致；禁止命令一律 `ok=False` 且零副作用。

---

## 9. 与 `ResolutionCopilot` 的差异点（≤150 字简评）

与 ResolutionCopilot 的差异：① 产出物不同——Copilot 只为坐席生成回复草稿（draft_answer，auto_reply 恒 False），本 agent 输出结构化处置命令 DiagnosisCommand；② 数据源不同——本 agent 用只读 MockVpnAdapter 隔离真实 VPN 后台，不做真实排障；③ 升级更严——evaluate_handoff 显式把 multi_user_impact、identity_missing、no_evidence、low_confidence 统一转人工，并用 FORBIDDEN_COMMANDS 封死账号/权限/网关/关单/代发消息等所有副作用。

---

## 10. 验收锚点（对齐现有口径）

- 允许命令集合与 `FORBIDDEN_COMMANDS` 不可被模型伪造（`extra="forbid"` + executor 兜底 + 治理层拒绝未注册工具）。
- 6 工具全部 `side_effect=False`、scope `ticket:agent`、租户取自 `RunContext`。
- 4 种升级场景判定确定性、可单测；`multi_user_impact` 与 `no_evidence` 与 `boundary_vpn` 的 `must_escalate` 语义一致但更严格。
- `MockVpnAdapter` 只读，6 方法数据源可配置（dict / JSON 路径），与 `vpn-v1-scope.md` 第 6 节「不做真实 VPN 自动诊断」一致。
- 有界限制（轮次/总工具数/单工具超时/总超时）不因模型失控而拖垮主流程。
- 治理注册项（6 条 `ToolPolicy` + `VPN_DIAGNOSIS_TOOLS` profile）进入 `tool_governance.py`。
