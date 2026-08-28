# 架构总览

> 面向中小企业 IT 服务台的多租户工单自动处置系统：员工通过 Web / 企业微信提交工单，
> 系统自动分类、按租户策略追问必填字段、加载 SLA、规则派单，Agent 检索知识库生成
> 带引用的建议，客服在工作台完成接单、处理、回访与关闭。

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Web | FastAPI + Uvicorn | 同步 API、渠道回调、工作台 |
| Agent | LangGraph（LangChain） | 受理图（intake graph）、分类、追问、派单 |
| 数据库 | PostgreSQL 17（psycopg3 + AsyncConnectionPool） | 业务表 + outbox + worker 指标表 + checkpoints |
| 向量 | pgvector（可选） | HNSW 索引、tenant/部门 ACL |
| 缓存 | Redis（可选） | 限流 / OIDC 撤销 |
| 观测 | OpenTelemetry + Prometheus 文本 + JSON 结构化日志 | /metrics /readyz /livez |
| 前端 | React + Vite | 客服工作台 |

## 进程拓扑

```
企微/钉钉/内部渠道 ──HTTP 回调──▶ FastAPI (app.py)
                                      │  验签/解密/幂等登记（inbound_events, received）
                                      ▼
                              立即返回 202 {"accepted": true}

inbound_worker ──领取 received/failed──▶ 建单 → 受理图 → 分类/追问/派单 → SLA → 澄清 Outbox
outbox_worker  ──领取 outbox_events────▶ 签名投递回调端点（幂等键），失败退避/死信
sla_worker     ──扫描 SLA 超时────────▶ 产出升级事件（幂等写入 outbox）
workflow_recovery ──重放中断意图──────▶ checkpoint/业务提交分裂恢复 + pending 过期翻转
```

四个 Worker 独立进程常驻，`FOR UPDATE SKIP LOCKED` 领取 + 租约续期，多副本安全；
单轮失败记录日志、指数退避、继续下一轮，不终止进程（`worker_loop_errors_total`）。

## 关键设计决策

1. **快速 ACK + 异步 Worker**：Webhook 只做验签解密与幂等登记，立即返回 202；
   建单等重活异步执行，回调方轮询事件状态。避免渠道侧超时重试风暴。
2. **Outbox 模式**：所有外发消息（澄清、回访、SLA 升级）先写 `outbox_events`，
   Worker 投递并支持重放，业务提交与投递解耦。
3. **检查点与业务提交分离 + Recovery**：LangGraph 检查点先落库、业务命令后提交；
   中途崩溃由 `workflow_recovery` 重放意图（`workflow_operations`），保证不丢单。
4. **指标与业务同库**：Worker 跨进程指标写 `worker_metrics` / `worker_heartbeats`
   表（schema v15），API 进程聚合输出。故障语义：PG 不可用时整体不可用（503），
   指标写入失败只记日志不影响业务（`safe_incr/safe_observe/safe_beat`）。
5. **知识检索双层**：lexical（jieba 分词 + pg_trgm 兜底）必开；pgvector 可选，
   未配置 embedding 端点时明确降级 lexical-only。
6. **权限与安全**：租户 token、OIDC（可选）、审计日志、预算控制、工具治理、
   敏感数据脱敏；日志禁止记录正文/Token/密钥/加密 XML。

## 验证状态（截至 2026-08-28）

- Docker 全量回归：254 passed（PostgreSQL 17 + pgvector + Redis，compose.test.yml）。
- 中文检索评测：52 条内部基准集 lexical-only Top1 98.1% / Recall@5 100% / MRR@5 0.990。
- 故障隔离注入测试（worker dependency fault-injection，替身注入）：
  指标写入失败时入站仍 committed、心跳失败不退出、claim 故障不终止循环。
- 企微：自动化测试覆盖验签/解密/幂等/追问/Resume；真实沙箱端到端步骤就绪，待执行。
