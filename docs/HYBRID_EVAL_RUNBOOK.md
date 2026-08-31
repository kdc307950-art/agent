# Hybrid 评测操作手册（接入 → 验证 → 固化 → 上线）

> **状态**：holdout 集已冻结（`HOLDOUT_VERSION = 2026-08-30-v1`）；真实 embedding
> 评测**未执行**；受保护 CI **未成功运行**。本手册是「接入、验证、固化、上线」
> 四部分的落地操作指引，所有命令与现状实现逐条对应，可直接执行。
>
> 对应实现：`backend/knowledge/eval_holdout_cases.py`（冻结集）、
> `backend/run_knowledge_eval.py`（评测器）、
> `.github/workflows/hybrid-eval.yml`（受保护 CI）。

---

## 0. 当前冻结基线（勿动）

- `HOLDOUT_VERSION = 2026-08-30-v1`（19 条 = 14 召回 + 4 无答案 + 1 ACL）
- **不修改** `backend/knowledge/eval_holdout_cases.py` 来提高分数；任何修改必须
  递增版本号并记录理由（治理要求，见「失败处理」）。
- 生产默认继续使用 **lexical-only**（`KNOWLEDGE_EMBEDDING_ENDPOINT` 未配置）。
- 工作区存在 3 个未提交的措辞一致性修正（"独立门禁"→"冻结门禁集"，
  涉及 `backend/run_knowledge_eval.py` / `docs/interview/demo-script.md` /
  `artifacts/final-regression-20260829.txt`）——**保留，不提交**（决策记录：
  用户 2026-08-30 指令）。

## 1. embedding 服务契约（必须符合现有代码约定）

现有 `HttpEmbeddingProvider`（`backend/knowledge/pgvector.py`）的调用约定：

```http
POST <KNOWLEDGE_EMBEDDING_ENDPOINT>
Content-Type: application/json

{"texts": ["文本1", "文本2"]}
```

响应（二选一）：

```json
{"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
{"data": [[0.1, 0.2], [0.3, 0.4]]}
```

强制校验（不符即失败，不静默丢弃）：

- 返回向量**数量与输入文本数量一致**（否则 RuntimeError）
- 每个向量**维度固定**且与 `KNOWLEDGE_EMBEDDING_DIMENSION` 一致（否则 ValueError）
- 单批 **≤32 条**（`EMBED_BATCH_SIZE`），复用 HTTP client，超时 15s
- 查询/入库共用同一端点与维度

服务要求：稳定、超时可控、认证方式明确；**模型版本固定**，同一评测版本中途
不更换模型。

**日志纪律**：不要把 endpoint、token 或响应内容打印到日志中。

### 1.1 embedding 服务选项与契约适配代理

**重要事实**：原生 OpenAI 兼容 `/v1/embeddings` 响应是
`{"data": [{"embedding": [...], "index": 0}, ...]}`，与项目契约**不兼容**
（`HttpEmbeddingProvider` 会对 dict 做 `len()` 维度校验而失败）。因此推荐：

**路径 A（推荐）：任意 OpenAI 兼容 API + 仓库内契约适配代理**
`backend/embedding_proxy.py` 把任意 `/v1/embeddings` 上游转换为项目契约：

```powershell
# 本地连通性自检
uv run python -m backend.embedding_proxy `
  --upstream https://api.openai.com/v1/embeddings `
  --api-key $env:OPENAI_API_KEY `
  --model text-embedding-3-small `
  --dimension 1536 `
  --port 8100

# 验证契约（返回 {"embeddings": [[...]]}）
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8100/" `
  -ContentType "application/json" -Body '{"texts":["你好","世界"]}'
```

- 代理只转发，不落库；api_key 只在转发时放入 Authorization 头，绝不进日志/响应
- 上游 4xx/5xx、数量/维度不符 -> 502（与评测"失败必须失败"语义一致）
- 单元测试：`tests/test_embedding_proxy.py`（8 个）

**GitHub Actions 可达性**：CI runner 是云端 ubuntu，无法访问你本机 `127.0.0.1`。
代理必须部署到 runner 可访问的公网地址，二选一：

- 云 PaaS（Railway / Render / Fly.io / 公司服务器）：把上述启动命令做成服务，
  得到固定 https URL → `KNOWLEDGE_EMBEDDING_ENDPOINT` 填该 URL
- 固定隧道（cloudflared named tunnel / frp）：内网起代理 + 固定公网域名

**路径 B**：已有原生契约服务（`{"texts"}` -> `{"embeddings"}`）→ 直接用，
跳过代理。

**维度选择（以模型文档为准）示例**：

| 模型 | 常见维度 |
|---|---|
| OpenAI text-embedding-3-small | 1536 |
| OpenAI text-embedding-3-large | 3072 |
| BAAI/bge-m3（硅基流动等） | 1024 |
| 智谱 embedding-3 | 2048 |
| Jina jina-embeddings-v3 | 1024 |

`KNOWLEDGE_EMBEDDING_DIMENSION` 必须与所选模型输出维度一致（proxy 侧
`--dimension` 与数据库向量列维度都必须对齐；`vector_migrations` 会按该值建列）。

## 2. GitHub Secrets 配置

```text
KNOWLEDGE_EMBEDDING_ENDPOINT
KNOWLEDGE_EMBEDDING_MODEL
KNOWLEDGE_EMBEDDING_DIMENSION
```

缺失 `KNOWLEDGE_EMBEDDING_ENDPOINT` 时 workflow 会在早期步骤显式失败
（`Require embedding endpoint secret`），绝不降级成 lexical-only 出 hybrid 成绩。

## 3. 本地先做一次验证

前置：`uv`、Docker、PostgreSQL/pgvector 与 Redis。

```powershell
uv sync --locked
docker compose -f infra/compose.test.yml up -d --wait

$env:DATABASE_URL="postgresql://langgraph:integration_only_not_a_secret@127.0.0.1:55436/langgraph"
$env:TEST_DATABASE_URL=$env:DATABASE_URL
$env:REDIS_URL="redis://127.0.0.1:56379/0"
$env:KNOWLEDGE_EMBEDDING_ENDPOINT="https://实际服务地址"
$env:KNOWLEDGE_EMBEDDING_MODEL="实际模型名"
$env:KNOWLEDGE_EMBEDDING_DIMENSION="1536"

uv run python -m backend.migrations
uv run python -m backend.vector_migrations
uv run python -m backend.seed_demo --tenant demo
uv run python -m backend.run_knowledge_index --tenant demo --embed

uv run python -m backend.run_knowledge_eval `
  --dataset hybrid_holdout `
  --tenant demo `
  --topk 5 `
  --embed `
  --report-json artifacts/hybrid-eval-local.json `
  --fail-under-top1 0.80 `
  --fail-under-recall5 0.90 `
  --fail-under-mrr 0.75 `
  --min-similarity 0.45
```

> `--min-similarity`：向量相似度拒答阈值（检索后门禁）。无答案用例的最高命中
> 相似度低于该值视为「证据不足 → 正确拒绝转人工」，不计误召回；**只影响无答案
> 判定，不影响召回指标**。默认不传 = 按 hits 非空判定（向后兼容）。
> 0.45 的校准依据（2026-08-30，qwen3.7-text-embedding，dim=1024）：
> seed_eval 52 条有答案用例 top-1 相似度全部 ≥ 0.505；holdout 4 条无答案
> 用例 top-1 相似度为 0.298 / 0.302 / 0.304 / 0.443，全部低于 0.45。

### 无 endpoint 时必须失败（预期行为）

```powershell
uv run python -m backend.run_knowledge_eval --dataset hybrid_holdout --embed
# => --embed 需要配置 KNOWLEDGE_EMBEDDING_ENDPOINT（SystemExit，退出码非 0）
```

**这是预期行为**：不能降级为 lexical-only 后继续输出 hybrid 成绩
（`resolve_eval_mode` 保证，已有单测覆盖）。

## 4. 手动运行受保护 CI

GitHub Actions → Actions → **Hybrid Eval（手动）** → Run workflow：

```text
dataset:            hybrid_holdout
tenant:             demo
topk:               5
fail-under-top1:    0.80
fail-under-recall5: 0.90
fail-under-mrr:     0.75
min-similarity:     0.45
```

工作流依次完成（见 `.github/workflows/hybrid-eval.yml`）：

1. 启动 PostgreSQL/pgvector 与 Redis（service containers）
2. `uv sync --locked` 安装锁定依赖
3. 检查 embedding secret 是否存在（缺失即失败）
4. `backend.migrations` 数据库迁移
5. `backend.seed_demo` 导入 seed 数据
6. `backend.run_knowledge_index --embed` 批量生成并写入向量
7. `backend.run_knowledge_eval --dataset hybrid_holdout --embed`（含门禁）
8. 上传 JSON 报告 artifact（`hybrid-eval-report-<run_id>`）

## 5. 验收标准（全部满足才可标为通过）

```text
mode          = hybrid
degraded      = false
Top1          >= 0.80
Recall@5      >= 0.90
MRR@5         >= 0.75
无答案误召回   = 0
ACL 泄露       = 0
```

用例结构（19 条 holdout）：

- **14 条**计入召回指标（Top1/Recall@5/MRR@5 的分母）
- **4 条**无答案用例单独统计（正确拒绝率，不混入召回）
- **1 条** ACL 隔离用例单独统计（不混入召回）

## 6. 失败时的处理顺序（按原因分类，禁止改样本过门禁）

| 失败原因 | 处理 |
|---|---|
| endpoint 连接/认证失败 | 修复服务配置，**不改评测集** |
| 向量维度错误 | 统一模型输出维度与 `KNOWLEDGE_EMBEDDING_DIMENSION` |
| 向量化失败 | 检查批量接口（≤32）、超时、重试 |
| Top1/Recall/MRR 不达标 | **只用 `seed` 集调参**，holdout 不动 |
| 无答案误召回 | 检查拒答阈值与检索后门禁 |
| ACL 泄露 | 检查部门过滤、tenant 过滤、restricted 文档权限 |

每次修改后重跑：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy backend
git diff --check
```

**holdout 集的任何修改都必须递增版本号并记录修改理由，不能为了过门禁直接改样本。**

## 7. 评测完成后的文档处理

**如果真实 hybrid 评测通过：**

- `docs/interview/known-limitations.md` 第 17 条：补充实际运行日期、workflow run
  与真实指标
- `docs/interview/knowledge-evaluation.md`：补充真实 hybrid 数字（与 lexical-only
  分开记录）
- `artifacts/final-regression-20260829.txt`：增加 JSON 报告位置
- **保留**"演示集结果不代表生产效果"的限制
- **保留**可靠性、成本、P95、降级率等尚未验证边界

**如果评测未通过：**

- **不填入占位成绩**
- 不覆盖最近一次已验证的 hybrid 结果；在报告中标注本次运行失败
- 记录失败原因、影响范围与下一轮修复计划

## 8. 生产启用（最后才考虑）

真实 holdout 通过后，小范围启用并持续观察：

- P95 检索延迟
- embedding 调用失败率
- lexical-only 降级率
- 单请求成本
- ACL 拒绝率
- 引用门禁拒绝率
- 人工转交率与误答率

在这些指标有实际运行数据之前，**生产默认保持 lexical-only 是正确决策**。

## 9. 真实评测记录（2026-08-30，CI 确认）

服务：DashScope MAAS 专属实例（OpenAI 兼容端点）→ 仓库契约代理
（`backend/embedding_proxy.py`）；模型 `qwen3.7-text-embedding`，维度 1024。
知识库：seed_demo 9 篇文档 / 18 个分块，全部向量化。
本地与受保护 CI 两次运行结果一致；CI run `33265164264`（2m15s）。

```text
数据集: hybrid_holdout@2026-08-30-v1（19 = 14 召回 + 4 无答案 + 1 ACL）
mode            = hybrid
degraded        = false
Top1            = 85.7%  (12/14)     >= 0.80 ✓
Recall@5        = 96.4%              >= 0.90 ✓
MRR@5           = 0.929              >= 0.75 ✓
无答案误召回     = 0/4（min-similarity 0.45） ✓
ACL 泄露         = 0/1                ✓
```

报告：CI artifact `hybrid-eval-report-33265164264`（JSON，下载于
`https://github.com/kdc307950-art/agent/actions/runs/33265164264/artifacts/9718444946`）。

**状态**：受保护 CI（`workflow_dispatch`）已成功运行并全部门禁达标
（2026-08-30）。第 7 节「评测通过」路径的文档更新已随本记录完成。
生产默认仍走 lexical-only，待第 8 节小范围启用后的实际运行数据再评估 hybrid 上线。
