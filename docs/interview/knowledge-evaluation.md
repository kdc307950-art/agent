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

- 内置 **52 条**脱敏 IT 服务台评测（`backend/knowledge/eval_cases.py`）：
  8 个 IT 子分类（vpn/email/account/printer/software/network/hardware/permission）
  × 5 条关键词复述 + 2 条跨文档 + 10 条口语改写用例。
- 每条：query（员工真实问法）+ expected_document_ids（可多文档，算 Recall@k）。
- 运行：`run_knowledge_eval [--seed] [--topk 5] [--embed]`
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

## Hybrid 评测（holdout，独立门禁）

真实 embedding 接入后执行，与 lexical-only 基线**分开记录、分开门禁**：

| 指标 | holdout 门禁 |
|---|---|
| Top1 | ≥ 80% |
| Recall@5 | ≥ 90% |
| MRR@5 | ≥ 0.75 |

- 门禁参数（CI 用，hybrid 专属）：
  `--fail-under-top1 0.80 --fail-under-recall5 0.90 --fail-under-mrr5 0.75`。
- 与 seed 集门禁（Top1 0.95 / Recall@5 0.98）互不混用；
  `--embed` 无 endpoint 时直接失败，禁止以 lexical-only 数字伪称 hybrid 达标。
- 当前状态：**未执行**——本地未配置真实 embedding 服务，hybrid 数字空缺，
  不得以占位或外推值填表。

## 评测报告应记录

数据集名称（seed_eval）、样本数量（52）、检索模式（lexical-only / hybrid）、
知识库版本（seed_demo 当前版本）、embedding 模型（配置时）、评测时间（2026-08-28）、
向量降级标记（degraded=true 时注明原因，hybrid 数字无效）。

## 明确局限（对外表述须一致）

- 指标来自**内部脱敏基准集**，不等同于真实企业知识库的泛化结果。
- embedding 端到端（真实模型服务）未接入本地评测链路，hybrid 指标待补。
- 当前数字只证明演示库上的检索质量，不证明生产数据分布下的表现。
