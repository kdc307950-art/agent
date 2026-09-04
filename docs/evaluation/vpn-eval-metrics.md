# VPN 诊断链路 9 项关键指标 —— 计算口径与报告扩展说明

> 生成角色：metrics-engineer · 日期：本任务（t3）
> 对应模块：`backend/vpn_eval_metrics.py`；被 `backend/run_vpn_eval.py` 调用。
> 对齐：`docs/evaluation/acceptance-traceability.md` §3（9 项指标核对）与 `docs/product/vpn-v1-scope.md`。

---

## 0. 目的

把 VPN 诊断链路的 **9 项关键指标** 统一计算进评测报告，输出到报告顶层新增的 **`metrics`** 小节。
该小节与既有小节（`classify_vpn_fault` / `field_completion` / `boundary` / `auto_misdirect` /
`knowledge` / `latency_ms` 等）**并存、互补**，不覆盖旧字段，保证向后兼容。

> 兼容性说明：既有 `run_vpn_eval` 静态报告测试（`tests/test_vpn_eval.py`）依赖旧字段，本模块不删除、
> 不改变旧字段语义，只在报告上**新增** `metrics` 小节。

---

## 1. 设计原则

- **纯函数、无 IO、可单测**：每个指标是一个入参 -> 返回值的纯函数。
- **输入输出类型化**：输入为 `VpnEvalRecord`（Pydantic，每条归一化样本）；输出为 `TypedDict`（每项一小节）。
- **静态 / db 两模式复用**：同一套函数在 `static`（确定性 keyword classifier）与 `db`（真实词法检索）
  两种模式下运行，各自返回“该模式可计算的值”。
- **缺数据不抛异常**：字段缺失时返回 `None` / `0`，并在 `basis` / `bias_note` 字段注明「不适用 / 取值依据」。

---

## 2. 报告 `metrics` 小节结构

```jsonc
{
  "protection": {                     // D4：报告级，仅结构校验，不代表真实防护
    "structural_only": true, "real_guards_invoked": false,
    "note": "本报告只做结构校验..."
  },
  "metrics": {
    "vpn_fault_classification": {"accuracy": 0.97, "sample_count": 50},          // M1
    "field_completion": {"detection_rate": 0.98, "complete_rate": 0.9, "sample_count": 60}, // M2
    "fault_hypothesis_hit": {"hit_rate": 0.0, "sample_count": 50, "basis": "..."}, // M3
    "escalation_accuracy": {"recall": 0.95, "precision": 1.0, "accuracy": 0.9,
                            "tp": 40, "fp": 0, "fn": 2, "tn": 18, "sample_count": 60}, // M4
    "customer_steps": {"avg_steps": 3.2, "sample_count": 0, "bias_note": "..."}, // M5
    "reference_support": {"mode": "static", "support_rate": null, "denominator": null}, // M6
    "high_risk_misdirect": {"misdirect_rate": 0.0, "misdirect_count": 0,
                            "negative_sample_count": 23, "samples": []}, // M7（D2 改 misdirect 口径）
    "acl_rejection": {"rejection_rate": 1.0, "rejected_count": 7,
                      "acl_sample_count": 7, "samples": []}, // D3 新增
    "tool_failure": {"failure_rate": null, "failed_count": 0, "total_count": 0}, // M8
    "latency": {"p50": 1.2, "p95": 3.5, "sample_count": 60},                     // M9a
    "cost": {"per_ticket_usd": null, "total_usd": 0.0, "ticket_count": 60,
             "input_per_1k_usd": 0.0, "output_per_1k_usd": 0.0,
             "rated": false}                                                     // M9b
  }
}
```

---

## 3. 9 项指标逐一口径

### M1 VPN 分类 Top1 —— `vpn_fault_classification`
- **口径**：真实 VPN 样本（`is_negative=False`）中 `predicted_fault == expected_fault` 的比例。
  仅统计真实 VPN 样本，负向/越界样本排除。
- **数据来源**：`classify_vpn_fault`（`intake.py`）+ `KeywordTicketClassifier`；`vpn_eval_cases` 的 `vpn_fault`。
- **模式**：static 与 db 一致。

### M2 字段补全成功率 —— `field_completion`
- **detection_rate**：缺失检测匹配率 `set(actual_missing) == set(expected_missing)` 的比例（全样本）。
- **complete_rate**：真实 VPN 样本 8 项必填全齐（`actual_missing` 为空）的比例。
- **数据来源**：`VPN_REQUIRED_FIELDS`（`vpn_eval_cases.py`）。

### M3 故障假设命中率 —— `fault_hypothesis_hit`
- **口径**：`expected_hypothesis` 与 `predicted_hypothesis` 相等即命中；仅统计真实 VPN 样本。
- **取值依据（回退）**：评测期诊断 agent 尚未沉淀结构化 `fault hypothesis` 归一结构，因此当样本无
  `predicted_hypothesis` 时 **回退 `vpn_fault` 分类**，命中率 = `fault_ok`（M1 口径）。
  生产端待 `DiagnosisCommand.reason_codes` / 新增结构化假设字段接入后，覆盖 M3 真实口径。
- **模式**：static 与 db 一致（此阶段均为回退口径）。

### M4 人工升级准确率 —— `escalation_accuracy`
- **needs_escalation（真值）**：`expected_boundary == "must_escalate"`。
- **escalated（预测）**：`predicted_boundary == "must_escalate"`，**或** 诊断产出 `escalate_incident` /
  `request_approval` 命令，**或** 无命令且诊断强制转人工 `must_handoff=True`。
- **指标**：`recall = TP/(TP+FN)`（需升级且正确升级的比例）、`precision = TP/(TP+FP)`、`accuracy = (TP+TN)/总样本`；
  并额外报 **误升级 FP**。
- **数据来源**：受理层 `boundary_vpn`；诊断层 `raw.must_handoff` + `evaluation.handoff_reasons`（`agent.py`）。

### M5 客户平均排障轮次 —— `customer_steps`
- **口径**：优先取 executor 对单工单产出的 **客户执行步骤数**（`provide_steps` 命令的步骤数）均值；
  无该数据时用 **诊断工具轮数**（`diagnostic_rounds`）作为 proxy。
- **为何用 proxy**：仅 `provide_steps` 命令才产出可计量的客户执行步骤；`ask_customer` /
  `escalate_incident` / `request_approval` 等命令不产生可计量的客户排障步骤。
  相关真实字段（`agent.run` 的 `rounds` / `tool_call_count`）**尚未写入返回结构**，待暴露后覆盖 M5 真实口径。
- **模式**：static（确定性分类器不诊断）`sample_count=0`，`avg_steps=None`。

### M6 引用支撑率 —— `reference_support`
- **口径**：仅 db 模式、且预期为 `auto_suggest` 的样本参与分母；`reference_supported` 为
  「预期文档子集是否被实际召回」。
- **static 模式**：不做真实词法检索，`support_rate` / `denominator` 返回 `None`（不适用）。
- **数据来源**：db 模式 `KnowledgeRepository.lexical_search`（`run_vpn_eval.py`）。

### M7 语义高风险误放行率 —— `high_risk_misdirect`
- **口径（D2 修复）**：`分子 = is_negative 且 predicted_boundary == auto_suggest 的样本数`，
  `分母 = is_negative 样本总数`。理想值 **0**，`>0` 需人工复核。
- **为何改口径**：旧口径只统计 `has_sensitive or has_high_impact` 关键词命中的样本，漏掉 ACL 越权 /
  账号锁定 / 无知识答案等「语义高风险但无敏感词」样本，导致 `misdirect_rate=0.0` 与实际误放行脱节。
- **输出**：`misdirect_rate / misdirect_count / negative_sample_count / samples`。
- **与既有 `auto_misdirect` 的区分**：`auto_misdirect` 在 run_vpn_eval 内按
  `is_negative & actual_category==it.vpn & predicted_boundary==auto_suggest` 判；本指标按
  `is_negative & predicted_boundary==auto_suggest` 判，覆盖所有越界/负向，两者互补。

### D3 ACL 越权拒绝率 —— `acl_rejection`
- **口径**：`scenario == acl_out_of_scope` 的越权样本被判 `must_escalate` 的比例，理想 **1.0**。
- **输出**：`rejection_rate / rejected_count / acl_sample_count / samples`（未拒绝的越权样本列入 samples）。
- **数据来源**：v2 越权场景识别 + `boundary_vpn`；配合 repository 的 tenant+visibility+department ACL 过滤
  与 tool_governance 越权拒绝（真实防护见集成/E2E 断言）。

### M8 工具调用失败率 —— `tool_failure`
- **口径**：`失败次数 / 总调用次数`。失败状态 = `denied`（越权/拒绝）、`timeout`、`failed`、`error`、
  `cancelled`；`completed` 视为成功。
- **数据来源**：`tool_trace` / 治理层 `ToolGovernance` 审计与指标（`tool_call_failed`/
  `tool_call_denied_total`/`tool_call_timeout_total` 等）；`vpn_tool_calls_total` 需聚合。
- **模式**：static（确定性分类器不调用工具）`total_count=0`，`failure_rate=None`。

### M9a P95 延迟 —— `latency`
- **口径**：p50 / p95 保留既有取法（nearest-rank）。当前采集点为 **分类器延迟**（static 下即
  keyword-classify 耗时）；完整工单链路（分类+字段+边界+诊断+引用）的 P95 需在诊断 E2E 重新定义采集点。
- **模式**：static 与 db 均可提供（若 latency 有值）。

### M9b 单工单成本 —— `cost`
- **口径**：`单工单成本 = Σ(model input/output tokens × 单价) / 工单数`。
  单价来自 `MODEL_INPUT_COST_PER_1K_USD` / `MODEL_OUTPUT_COST_PER_1K_USD`。
- **默认 0 标 unrated**：单价为 0 时 `rated=false`，`per_ticket_usd=None`，**不抛异常**。
- **数据来源**：`usage.py`（`extract_model_usage` + `usage_cost_usd`）。注意：VPN 诊断模型在
  `VpnDiagnosisAgent` 内部调用，**未纳入 app.py 的 astream 计量**，需在 `agent.run` 内埋 usage 才真实计算；
  static 评估不跑模型，token 均为 0 → `rated=false`。

---

## 4. 静态 vs db 模式适用性小结

| 指标 | static | db | 说明 |
|---|---|---|---|
| M1 分类 | ✅ | ✅ | |
| M2 字段补全 | ✅ | ✅ | |
| M3 假设命中 | ⚠️ 回退 fault_ok | ⚠️ 回退 fault_ok | 需诊断结构化假设 |
| M4 升级准确率 | ⚠️ 边界代理 | ⚠️ 边界代理 | 需诊断 must_handoff/命令 |
| M5 排障轮次 | ⚠️ 无数据 | ⚠️ 无数据 | 需 executor/agent 埋点 |
| M6 引用支撑 | ❌ None | ✅ | 需 db 词法检索 |
| M7 语义高风险误放行 | ✅ | ✅ | misdirect 口径（is_negative） |
| D3 ACL 越权拒绝率 | ✅ | ✅ | scenario==acl_out_of_scope |
| M8 工具失败率 | ⚠️ 无调用 | ⚠️ 无调用 | 需 tool_trace/治理审计 |
| M9a P95 | ✅（分类器） | ✅（分类器） | 完整链路需 E2E 重定义 |
| M9b 成本 | ⚠️ unrated | ⚠️ unrated | 需 agent.run 埋 usage |

`✅` 可量化；`❌` 不适用（返回 None）；`⚠️` 需诊断/埋点数据才真实量化，当前回退或为空。

> **D4 重要声明**：本运行器（static / db 词法检索）只做**结构校验**，不触发 tool_governance 的
> 租户白名单/scope/allowed_tools 校验、repository 的 tenant+visibility+department ACL 过滤、
> models.FORBIDDEN_COMMANDS 二次拒绝、approval.APPROVED 审批门禁等**真实防护**。因此 static/db 的
> boundary / auto_misdirect / misdirect 数字只能证明结构层面的边界三态，**不能作为**
> 「越权被拒 / 未审批被拦」的安全结论——这些需在集成/E2E 层用真实调用断言。报告顶层 `protection`
> 字段已标注 `structural_only=true`、`real_guards_invoked=false`。

---

## 5. 运行与验证

```bash
# 静态模式：产出报告并在 metrics 小节给出指标
.\\.venv\\Scripts\\python.exe -m backend.run_vpn_eval \
    --json docs/evaluation/vpn-eval-report.json

# 真实库评测（vpn-v2，需 TEST_DATABASE_URL）：M6/M9 可信值 + ACL 越权拒绝率
.\\.venv\\Scripts\\python.exe -m backend.run_vpn_eval \
    --database-url %TEST_DATABASE_URL% --require-db --dataset vpn-v2 \
    --json docs/evaluation/vpn-eval-report-v2.json

# 单元测试（metrics + eval + v2）
.\\.venv\\Scripts\\python.exe -m pytest tests/test_vpn_eval_metrics.py \
    tests/test_vpn_eval.py tests/test_vpn_eval_v2.py -q
```

---

## 6. 已知缺口与后续（对应 acceptance-traceability §3.9）

1. **M3 真实口径**：需在 `DiagnosisCommand.reason_codes` 或新增结构化假设字段，并做假设词表比对打分。
2. **M5 真实口径**：需在 `agent.run` 返回结构暴露 `rounds` / `tool_call_count`，或按工单消息往返统计。
3. **M9 真实成本/P95**：VPN 诊断 token/成本未纳入 app.py 计量，需在 `agent.run` 内埋 usage 埋点；
   P95 采集点需重定义为完整工单链路。

> **D1 / D3 / D5 修复说明**：
> - **D1**：`run_vpn_eval` 的 `has_evidence` 已解耦「无依据应升级」与「引用支撑」——空 `expected_document_ids`
>   （无知识/ACL/账号锁定等语义高风险样本）不再恒 True，改为按真实检索是否有内容判定；`is_negative` 样本一律
>   视为无合法自动建议依据（`has_evidence=False`）→ `boundary_vpn` 推向 `must_escalate`，修复越权/账号锁定/无知识
>   被误放行为 auto_suggest 的缺陷。
> - **D2**：M7 改为 `misdirect` 口径（`is_negative` 且 `predicted_boundary==auto_suggest`），覆盖 ACL/账号锁定/无知识等
>   「语义高风险但无敏感词」样本，不再报与漏洞脱节的 `0.0`。
> - **D3**：v2 新增字段（`error_code/client_version/network_type/fault_hypothesis/acls/risk_level/
>   escalation_expected/departments/internal/resource`）已进入 `VpnEvalRecord` 与 `to_record` 归一化；新增
>   **ACL 越权拒绝率**（`acl_rejection`，`scenario==acl_out_of_scope` 被判 `must_escalate` 比例，理想 1.0）。
> - **D5**：复核表明引用支撑率非评测集/文档映射缺陷（v2 auto_suggest 样本 `expected_document_ids=("vpn-001",)`
>   与 seed 知识库 `vpn-001` 一致且词法检索确实命中）；此前 0.3077 为数据库索引/数据状态不一致的伪象，
>   重新 `migrations + seed_demo` 后 db 评测 `reference_support_rate=1.0`。若日后再次偏低，请先重跑迁移/种子定位
>   索引状态，而非样本映射。
