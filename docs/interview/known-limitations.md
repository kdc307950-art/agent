# 已知限制与诚实的边界

> 面试/对外表述统一口径：**已完成本地 Docker、PostgreSQL、Redis 验证；中文检索
> 指标来自内部基准集，不等同真实企业知识库泛化结果；尚未证明生产环境长期运行
> 能力；企微沙箱真实端到端待执行。**

## 架构级限制

1. **指标与业务同库**：`worker_metrics` / `worker_heartbeats` 与业务表同一
   PostgreSQL 实例。PG 故障时业务与可观测性一起不可用（/readyz 503）。
   已接受的语义：不做"指标降级但业务继续"；未来如需分离，须引入独立指标存储。

2. **单 pending 追问边界**：同一租户/渠道/外部用户只保留一个 awaiting 追问，
   新追问取消旧追问。多工单选择、模糊匹配、短链接系统未实现（无真实需求）。

3. **embedding 可选**：pgvector 后端依赖外部 embedding 服务与维度配置；
   未配置时检索为 lexical-only。演示库的 hybrid holdout 已由受保护 CI 验证，
   生产知识库规模下的效果、成本与长期可靠性仍未验证。

4. **生产长期运行未证明**：未经历生产流量、故障演练与容量验证；
   退避/租约参数（30s 基数、120s 租约、90s 心跳 TTL）为默认值，未按负载调优。

5. **Worker 单机默认参数**：`READINESS_CHECK_WORKERS` 默认关闭（单机 demo 无
   worker 时不误报），生产/CI 必须显式开启。

## 测试与验证边界

6. **评测集为内部基准**：52 条为脱敏 IT 场景构造用例，含 10 条口语改写；
   lexical-only Top1 98.1% 只在演示库上测得，不代表真实企业知识库分布。

7. **企微沙箱真实端到端未执行**：自动化测试覆盖验签/解密/幂等/追问/Resume；
   真实自建应用回调验证步骤就绪（`docs/CHANNEL_SANDBOX.md`），待执行。

8. **live_e2e 测试排除**：真实模型/外部服务的端到端用例带 `live_e2e` 标记，
   默认回归不执行（可能产生外部调用成本）。

9. **已知 flaky 修复**：inbound backoff=0 的毫秒级时钟竞态已在测试中
   （短暂 sleep）规避；这是测试时序问题，不是产品缺陷，但值得在参数化
   重试策略时用 DB 时钟统一 next_attempt_at 计算。

## 安全与数据

10. **渠道回调端点需公网暴露**：企微回调要求公网 HTTPS 域名，本地演示用
    Webhook 转发/隧道；生产需 TLS 终止与白名单。

11. **多租户隔离依赖参数校验**：tenant 访问由 token/OIDC + 仓储层租户过滤
    双层控制；所有知识检索强制 tenant/部门 ACL，但未见多租户压测证据。

12. **审计/预算/撤销**：具备实现（audit.py、budget.py、revocation.py、
    OIDC 可选），但仅在测试环境验证，未在生产流量下评估性能开销。

## Resolution Copilot 边界（异步 Worker 化后）

13. **部门身份透传已落地（发起人快照）**：`copilot_runs` 持久化
    requester_user_id / requester_role / requester_departments /
    requester_internal，POST 入队时从认证主体保存快照，Worker 从运行记录
    恢复真实身份执行部门级 ACL（身份缺失闭锁，不默认全权限）。
    **已验证到集成测试层面；多 Worker 并发与生产环境透传未做压力验证。**

14. **Copilot 生产默认走 lexical-only**：统一 `KnowledgeRetriever` 入口就绪
    （lexical-only / hybrid 双模式 + retrieval_mode 标记 + degraded 降级标记）；
    hybrid 路径已在演示库 holdout CI 验证，但生产配置仍未启用，不能据演示指标
    推断生产效果。

15. **Copilot 异步 Worker 化已完成核心链路**：POST 只入队返回 202、
    Worker 领取/租约续期/退避/dead/恢复、GET 状态轮询、崩溃恢复测试通过；
    但 Worker 进程的长期运行、多副本并发与故障演练未在生产环境验证。

16. **死信管理端点为管理面操作**：`/admin/copilot/runs`（列出 dead）与
    `/admin/copilot/runs/{run_id}/replay`（重放保留原审计）已实现并测试；
    生产级操作审批流（谁可重放、限流、审计联动）未设计。

17. **Hybrid 评测：CI 确认通过**：独立 `hybrid_holdout` 评测集
    （`eval_holdout_cases.py`，冻结版本 `2026-08-30-v1`，19 = 14 召回 + 4 无答案
    + 1 ACL）已用真实 embedding（`qwen3.7-text-embedding`，dim=1024，
    经仓库契约代理 `backend/embedding_proxy.py` 接入）在受保护 CI
    （`workflow_dispatch`）成功运行并全部门禁达标（run `33265164264`，
    2026-08-30，2m15s）：

    | 指标 | 结果 | 门禁 |
    |---|---|---|
    | 模式 / 降级 | hybrid / degraded=false | hybrid / false |
    | Top1 | 85.7%（12/14） | ≥ 80% ✓ |
    | Recall@5 | 96.4% | ≥ 90% ✓ |
    | MRR@5 | 0.929 | ≥ 0.75 ✓ |
    | 无答案误召回 | 0/4（min-similarity 0.45 拒答阈值） | = 0 ✓ |
    | ACL 泄露 | 0/1 | = 0 ✓ |

    seed_eval 的 lexical-only 基线（98.1% Top1）保持不变。**但仍不据此推断
    生产效果**：这是演示库 + 冻结 holdout 上的结果，不等同真实企业知识库
    泛化；生产默认仍走 lexical-only，待小范围启用后的 P95/成本/降级率等
    实际运行数据再评估 hybrid 上线（见 runbook 第 8 节）。

> **对外统一表述**：Copilot 已完成身份快照持久化与统一检索接线；Hybrid
> 检索路径、降级标记、拒答阈值与门禁机制已实现，独立 holdout 集已在受保护
> CI 上全部门禁达标（演示库规模）。当前生产配置仍使用 lexical-only，真实
> 生产知识库规模下的 hybrid 效果与可靠性、成本尚未验证。
