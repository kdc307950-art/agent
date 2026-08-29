# 知识检索与评测

## 检索链路

```
query ──▶ lexical_search（必开，纯 SQL）
            ├─ jieba + IT 词典分词（tokenizer.py，查询与入库同一分词器）
            ├─ plainto_tsquery('simple')：AND 精确召回
            ├─ to_tsquery('simple')：OR 覆盖率召回（解决分词不一致）
            ├─ pg_trgm similarity：错别字/短词兜底
            └─ 排序：命中 token 数 → ts_rank → trigram
       └─（可选）pgvector 向量检索：embedding <=> 余弦距离，HNSW 索引
              └─ RRF 融合 lexical + vector（reciprocal_rank_fusion）
所有召回强制：tenant / published / 有效期 / visibility / 部门 ACL 过滤。
```

- embedding 为可选后端：未配置 `KNOWLEDGE_EMBEDDING_ENDPOINT` 时明确降级
  lexical-only 并记录原因；不静默伪称 hybrid。
- Agentic RAG：Agent 可在有界轮次内生成补充查询（`knowledge/agentic.py`），
  全部查询重复 ACL 过滤；无双路证据/缺引用/高风险/财务类问题禁止自动回复。

## 评测集

- **seed_eval（52 条，开发期回归）**：内置脱敏 IT 服务台评测
  （`backend/knowledge/eval_cases.py`）：8 个 IT 子分类
  （vpn/email/account/printer/software/network/hardware/permission）
  × 5 条关键词复述 + 2 条跨文档 + 10 条口语改写用例。
- **hybrid_holdout（冻结，独立）**：`backend/knowledge/eval_holdout_cases.py`，
  覆盖口语改写、跨文档、多部门 ACL、低频错误码、近义词、无答案问题；
  无答案/ACL 隔离用例单独统计，不混入召回指标。
- 每条：query（员工真实问法）+ expected_document_ids（可多文档，算 Recall@k）；
  holdout 额外支持 expected_none / forbidden_document_ids / principal_departments。
- 运行：`run_knowledge_eval --dataset seed|hybrid_holdout [--topk 5] [--embed]`
  （`--embed` 无 endpoint 直接失败，禁止伪称 hybrid）。

## 实测基线（2026-08-28，lexical-only，topk=5，演示知识库）

| 指标 | 值 |
|---|---|
| Top1 | **98.1%（51/52）** |
| Recall@5 | 100% |
| MRR@5 | 0.990 |
| 无命中（转人工） | 0 条 |
| 分项最弱 | it.account Top1 83.3%（6 中 1 条口语改写未排首位，语义改写依赖 embedding，非实现错误） |

门禁参数（CI 用）：`--fail-under-top1 0.95 --fail-under-recall5 0.98`
（seed 集）；语义改写用例集不在 lexical-only 下设置 80% 门禁。

## Hybrid 评测门禁设计（待接入独立 holdout 集）

真实 embedding 接入后执行，与 lexical-only 基线**分开记录、分开门禁**。
独立 holdout 集已创建（`backend/knowledge/eval_holdout_cases.py`，冻结版本
`2026-08-30-v1`）但**尚未评测**；以下为已冻结的门禁设计。

**数据集分离**
- `seed_eval`（`backend/knowledge/eval_cases.py`，52 条）：开发期回归用，
  检索策略调参时可复用。
- `hybrid_holdout`（`backend/knowledge/eval_holdout_cases.py`）：**冻结且独立**，
  不参与检索策略调参；覆盖口语改写、跨文档、多部门 ACL、低频错误码、近义词、
  无答案问题；无答案用例单独记录，不混入召回指标。

**门禁阈值（hybrid 专属）**

| 指标 | 门禁 |
|---|---|
| 模式 | `hybrid`（`--embed` 强制） |
| 降级 | `degraded=false`（评测中向量失败必须整体失败，不降级出成绩） |
| Top1 | ≥ 80% |
| Recall@5 | ≥ 90% |
| MRR@5 | ≥ 0.75 |
| 无答案集 | 单独记录（正确拒绝率），不混入召回指标 |

```powershell
# seed 开发集（回归，无 embedding 时自动 lexical-only）
python -m backend.run_knowledge_eval --dataset seed
# hybrid holdout（真实 embedding 注入；无 endpoint 时 --embed 直接失败）
python -m backend.run_knowledge_eval --dataset hybrid_holdout --embed `
  --fail-under-top1 0.80 --fail-under-recall5 0.90 --fail-under-mrr 0.75
```

- `--dataset hybrid_holdout --embed` 在未配置 `KNOWLEDGE_EMBEDDING_ENDPOINT`
  时**直接失败**（`resolve_eval_mode` 抛错），禁止降级成 lexical-only 后
  继续产出"hybrid 成绩"。
- 已实现 CLI 阈值参数（`--fail-under-*`），**尚未接入 CI 自动执行**；
  受保护的手动触发 workflow（`workflow_dispatch` + secrets 注入）设计见
  `.github/workflows/hybrid-eval.yml`，不随 PR 自动跑。
- 当前状态：**未执行**——本地未配置真实 embedding 服务，hybrid 数字空缺，
  不得以占位或外推值填表。

## 评测报告应记录

数据集名称与版本（seed_eval / hybrid_holdout@版本）、样本数量、检索模式
（lexical-only / hybrid）、embedding 模型/版本（配置时）、维度、知识库版本
（seed_demo 当前版本）、运行时间、`retrieval_mode`、`degraded`
（degraded=true 时注明原因，hybrid 数字无效）。

## 评测风险控制（阶段四）

- **不外推**：演示集与冻结 holdout 的高分只证明演示库上的检索质量，不代表
  真实企业知识库分布；对外表述不得把演示成绩外推为生产效果。
- **评测失败语义**：embedding 请求超时、限流、维度不一致时，线上请求可降级
  （`degraded=true` 标记），**评测必须失败**（不静默降级出成绩）。
- **holdout 治理**：`hybrid_holdout` 冻结；变更须记录版本与理由，建议维护者
  与检索策略调参者分离（或至少每次变更留痕）。
- **生产指标扩展清单**：除检索效果外，生产可观测性应覆盖 P95 延迟、
  embedding 调用失败率、降级率、每请求成本、ACL 拒绝率、引用门禁拒绝率。
- **可靠性验证单列**：多 Worker 并发与长期运行属于可靠性验证，与检索正确率
  分开评估，不得混为"生产可用"。

## 明确局限（对外表述须一致）

- 指标来自**内部脱敏基准集**，不等同于真实企业知识库的泛化结果。
- embedding 端到端（真实模型服务）未接入本地评测链路，hybrid 指标待补。
- 当前数字只证明演示库上的检索质量，不证明生产数据分布下的表现。
