# V1 产品范围说明：VPN 受理与建议闭环（vpn-v1）

> 版本标识 `vpn-control-v1.0`（冻结日 2026-09-05；历史评测集版本号仍按其生成记录保留）。本文件是 **canonical 锚点**：
> VPN 的故障分类、固定字段、边界矩阵均以本文件命名为准，Demo Script、README、
> VPN 专项评测集与项目口径一律引用这里，避免四处漂移。
> 一句话：**V1 演示只突出 `it.vpn` 一条主线，完成「VPN 受理与建议闭环」。**

## 1. V1 演示主线：只突出 it.vpn

- **主链路唯一主产品 = `it.vpn`**：员工从 Web 提交 VPN 故障 → 系统自动分类到
  `it.vpn` → 按 VPN 固定字段追问 → 命中 `sla-vpn` → 派单 `team-it` → 知识检索
  `vpn-001` 生成带引用的建议 → 客服接单/处理/回访/人工关闭。
- **`it.account` / `it.network` 降为旁路或转人工**（不再与 `it.vpn` 并列为“三类工单”）：
  - 保留既有受理能力（分类、SLA、派单仍可用）；
  - 但 **不在 V1 演示脚本与主叙事中承诺**，项目口径不把它们当作“V1 演示主线”。
  - 命中账号/网络但字段不全、或证据不足时，同 VPN 一样进入统一受理/追问/转人工，
    不特殊处理、不夸大。
- 其他 IT 子类（email/hardware/software/printer）与业务大类（finance/admin/product/other）
  保持既有“越界不自动处置、转服务台人工”的规则，不作主线。

## 2. VPN 故障分类（固定 5 类）

为 `it.vpn` 增加一个纵向维度 `vpn_fault`（故障类别）。每张 VPN 工单必须归属且只归属一类：

| `vpn_fault` 枚举值 | 中文 | 典型信号 / 例句 |
| --- | --- | --- |
| `connection_failed` | 连接失败 | 连不上、无法建立连接、一直转圈、提示错误码 809/800 |
| `frequent_disconnect` | 频繁掉线 | 登录后频繁掉线、不断重连、网络不稳定 |
| `auth_failed` | 认证失败 | 用户名/密码错误、认证失败、证书过期、错误码 691 |
| `intranet_unreachable` | 内网不可达 | 能连上 VPN 但访问不了内网/远程桌面、ping 不通内网 |
| `multi_user_impact` | 多人故障 | 多人/整个团队同时受影响、全公司、大面积故障 |

**分类与风险的关系（边界矩阵依据）**：
- `auth_failed` 涉及认证/账号，**默认按高风险处理**，即使字段齐也优先人工复核；
- `multi_user_impact` 是群体故障，**必然人工升级**；
- `connection_failed` / `frequent_disconnect` / `intranet_unreachable` 且单户、字段齐、
  有知识依据时，才允许自动建议草稿。

## 3. VPN 固定字段（8 项）

为 `it.vpn` 固定受理字段（`IntakePolicy` 的 `it.vpn` 租户策略必填项），字段名以本表为准：

| 字段 key | 中文 | 说明 |
| --- | --- | --- |
| `device` | 设备 | 资产编号/设备型号，如 laptop-001、MacBook Pro |
| `operating_system` | 系统 | Windows 11 / macOS / Ubuntu 等 |
| `vpn_client` | 客户端 | OpenVPN / AnyConnect / 深信服 / 公司客户端 |
| `client_version` | 客户端版本 | 客户端安装版本，如 3.4.2 |
| `error_code` | 错误码 | 809 / 769 / 691 / 800 等，可为“无” |
| `network` | 网络环境 | 办公网 / 家庭宽带 / 4G5G / 酒店 Wi-Fi |
| `multi_user_impacted` | 是否多人受影响 | 是/否；值为“是”时触发多人故障判定 |
| `recent_change` | 最近变更 | 升级客户端 / 改密码 / 换网络 / 搬工位等 |

字段命名一致性：**`error_code` 取代旧口径的 `error_message`**；`multi_user_impacted`
直接驱动 `multi_user_impact` 风险判定与边界升级。字段补全率统计以这 8 项为单位。

## 4. 场景边界矩阵（固定 3 态）

每条 VPN 工单在受理后落入且仅落入一种处置态：

| 边界态 | 值 | 触发条件 | 系统行为 |
| --- | --- | --- | --- |
| 自动建议 | `auto_suggest` | VPN 单户故障；8 项必填字段齐；`vpn_fault` 为 connection_failed / frequent_disconnect / intranet_unreachable；无敏感/高影响词；知识依据 `vpn-001` 有证据 | 派单 `team-it` + SLA `sla-vpn` + 生成带引用的建议草稿（引用支撑率 100% 才自动回复，否则转人工） |
| 必须追问 | `must_ask` | 分类为 `it.vpn` 但 8 项必填字段不全（缺任一即问） | 挂起进入 `awaiting_customer`，向客户追问缺失字段；追问耗尽仍缺 → 人工 |
| 必须人工升级 | `must_escalate` | `vpn_fault` 为 `auth_failed` 或 `multi_user_impact`；或命中敏感/高影响词（权限、数据、全公司、生产）；或字段追问耗尽；或无知识依据；或非 VPN/越界 | 不自动回复、不自动发消息，转服务台人工队列/人工接管 |

**决策函数（确定性，可单测）**：

```
boundary_vpn(vpn_fault, fields_complete, has_sensitive_risk, has_high_impact, has_evidence):
    if not fields_complete:  return must_ask                 # 字段不全先追问
    if vpn_fault == auth_failed:  return must_escalate       # 认证类高风险
    if vpn_fault == multi_user_impact:  return must_escalate # 群体故障
    if has_sensitive_risk or has_high_impact:  return must_escalate
    if not has_evidence:  return must_escalate               # 无依据不自动建议
    return auto_suggest
```

补充：`fields_complete` 由 8 项必填字段是否齐备决定；高风险词沿用既有
`_SENSITIVE_TERMS` / `_HIGH_IMPACT_TERMS`（删除数据、开通权限、全公司、生产环境等）。

## 5. 主链路（唯一验收路径）

```text
Web 建单 → 受理图分类 it.vpn + vpn_fault → 8 项必填字段追问（按“字段:值”补充）
  → 边界矩阵判定（auto_suggest / must_ask / must_escalate）
  → auto_suggest: 命中 sla-vpn + 派单 team-it + RAG vpn-001 建议（带引用）
  → must_ask: awaiting_customer 追问，耗尽转人工
  → must_escalate: team-service-desk 人工接管，不自动回复
  → 客服接单/处理 → 回访 → 客户确认 → 人工关闭 → 全程审计
```

## 6. 非目标（V1 明确不做）

- **不做“真实 VPN 自动诊断”**：系统做的是「受理 + 分类 + 字段补全 + SLA + 派单 +
  带引用建议 + 人工闭环」，不做自动排障修复、不模拟真实 VPN 后台/网关操作、
  不宣称能端到端自动解决 VPN 连接问题。
- 账号/权限、网络断网作为**旁路**保留能力，不作为演示主线与承诺。
- 更多渠道/复杂多 Agent/行业扩展/通用客服。
- 未经评测的 LLM 自动分类：V1 分类以确定性关键词基线为兜底，LLM 仅作建议/草稿生成。

## 7. 验收指标（VPN 专项口径）

以下指标**只针对 VPN 专项评测集**（`backend/knowledge/vpn_eval_cases.py`，60 条）
独立统计，不再与混合 IT 工单混算：

| 指标 | 门槛 | 统计口径 |
| --- | --- | --- |
| VPN 分类准确率（含 `vpn_fault`） | ≥ 90% | VPN 全部样例分类到 `it.vpn` + `vpn_fault` 的正确率 |
| VPN 字段补全率 | ≥ 95% | 8 项固定字段的缺失检测匹配率（应补尽补） |
| 边界判定正确率 | ≥ 90% | auto_suggest / must_ask / must_escalate 命中率 |
| 自动草稿引用支撑率 | 100% | 仅 auto_suggest 态；任何无证据不自动回复 |
| 闭环可达率 | 100% | 全部 VPN 样例都能进入统一受理/SLA/派单/人工关闭 |
| 非 VPN / 高风险误导向自动处置 | 0 | 负向样例（越界/高风险）不得被当作 it.vpn 自动建议 |

## 8. 项目口径（统一用词）

> **当前实现是「VPN 受理与建议闭环」，不是「真实 VPN 自动诊断」。**
>
> 已完成：本地 Docker + PostgreSQL/Redis 验证；VPN 主链路（分类 it.vpn + vpn_fault、
> 8 项字段补全、SLA、规则派单、带引用建议、人工闭环）；VPN 专项 60 条评测集（口径由
> `docs/product/vpn-v1-scope.md` 固定）；确定性关键词分类为兜底，LLM 仅作建议生成。
>
> 未承诺：不做真实 VPN 自动排障/修复；不做账号/网络主演示；真实企微沙箱端到端、
> 生产知识库泛化、真实模型引用与 P95/成本、长期运行能力**尚未证明**。

## 9. Fortinet 控制面演示扩展边界

本节描述仓库新增的 Fortinet 垂直演示扩展，不改变本文件前文定义的 V1 主产品：V1 仍然是 `it.vpn` 的受理、字段补全、建议与人工闭环。

### 已实现的控制面闭环

面向约 50 人的中小企业，控制面动作收窄为**租户级配置漂移检测与策略重推**：

```text
目标校验 → preview → 保存 diff_hash → 人工审批 → FMG install task
  → 异步轮询 → confirmed / execution_unknown → 人工确认与只读对账
```

- 新业务语义为 `redeploy_tenant_vpn_config`；不宣称单员工重签。
- 现有 `vpn_reissue_operations` 扩展至 schema `v25`，记录 vendor task、提交状态、目标/差异快照和轮询信息。
- `confirm-submission` 只接受人工确认事实：`submitted` 必须带正整数 task id，随后只读对账；`not_submitted` 不允许带 task id，并收敛为失败。
- `submission_unknown` 代表副作用可能已经发生但本地未取得 task id，禁止自动重试。
- 唯一约束、租户锁和旧 task 只读续查共同约束重复提交；`worker_id + lease_expires_at` 已承担租约语义。

### 证据等级与未验证边界

- 已验证：本地 Fake FMG 的真实 HTTPS/HTTP 往返、健康检查、只读 status、成功 task、超时保留 task id、业务拒绝映射、人工确认接口和跨租户拒绝测试。
- 未验证：真实 FMG staging 的 ADOM/Model Device/Policy Package 状态，真实 FortiGate 设备下发，生产长期运行、回滚和厂商侧最终效果。
- trial 许可的设备数与 ADOM 数量必须以激活后 `License Information` widget 的实际显示为准，不写固定“3 设备/3 ADOM”。
- Fake FMG 仅是协议与失败处置演练，不等同真实生产接入证明。

验收演示入口是仓库根目录 `drill_fake_fmg.py`，固定 8 步并在失败时返回非零退出码；生产写入默认关闭，真实 staging 到位后仍需按审批、preview、对账和 canary 清单逐步放量。
