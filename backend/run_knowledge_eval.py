"""IT 服务台知识检索评测运行器 —— 输出可量化的检索指标。

用法（需要 PostgreSQL，评测集来自 backend/knowledge/eval_cases.py）：

    # 1) 准备数据：迁移 + 导入脱敏 IT 知识库（幂等）
    uv run python -m backend.seed_demo
    # 2) 只跑全文检索基线（未配置 embedding 时自动降级）
    uv run python -m backend.run_knowledge_eval
    # 3) 启用真实 embedding 服务后跑 hybrid（配置了 KNOWLEDGE_EMBEDDING_* 时自动启用）
    uv run python -m backend.run_knowledge_eval --embed

指标：Top1 命中率、Recall@k、MRR@k（按分类分项）；同时统计「无检索命中」
用例数 —— 这些用例在受理链路中会触发门禁转人工，不会自动发送建议。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

from .knowledge import (
    HttpEmbeddingProvider,
    KnowledgeRepository,
    PgVectorRetriever,
    RetrievalHit,
    RetrievalPrincipal,
    reciprocal_rank_fusion,
)
from .knowledge.eval_cases import EVAL_CASES, document_ids_by_category

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TENANT = "demo"
DEFAULT_TOPK = 5


async def _retrieve(
    repository: KnowledgeRepository,
    vector: PgVectorRetriever | None,
    principal: RetrievalPrincipal,
    query: str,
    *,
    topk: int,
) -> list[RetrievalHit]:
    lexical = await repository.lexical_search(principal, query, limit=topk)
    if vector is None:
        return lexical[:topk]
    vector_hits = await vector.search(principal, query, limit=topk)
    return reciprocal_rank_fusion(lexical, vector_hits, limit=topk)


def _metrics(
    hits: list[RetrievalHit], expected: tuple[str, ...], topk: int
) -> tuple[float, float, float]:
    top1 = 1.0 if hits and hits[0].document_id in expected else 0.0
    found = {hit.document_id for hit in hits[:topk]}
    recall = len(found & set(expected)) / len(expected)
    mrr = 0.0
    for rank, hit in enumerate(hits[:topk], start=1):
        if hit.document_id in expected:
            mrr = 1.0 / rank
            break
    return top1, recall, mrr


def resolve_eval_mode(*, embed: bool, embedding_endpoint: str) -> str:
    """评测模式：'hybrid'（配置/强制 embedding）或 'lexical-only'。

    --embed 但未配置 KNOWLEDGE_EMBEDDING_ENDPOINT 时抛错，禁止伪称 hybrid。
    """
    if embed and not embedding_endpoint:
        raise SystemExit("--embed 需要配置 KNOWLEDGE_EMBEDDING_ENDPOINT")
    return "hybrid" if (embed or embedding_endpoint) else "lexical-only"


async def _run_eval(
    conninfo: str,
    *,
    tenant_id: str,
    topk: int,
    limit: int,
    seed: bool,
    embed: bool,
) -> dict:
    if seed:
        from .seed_demo import _seed

        await _seed(tenant_id, conninfo)

    embedding_endpoint = os.getenv("KNOWLEDGE_EMBEDDING_ENDPOINT", "").strip()
    embedding_model = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "").strip() or None
    try:
        embedding_dimension = int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSION", "1536"))
    except ValueError as exc:
        raise SystemExit("KNOWLEDGE_EMBEDDING_DIMENSION 必须是整数") from exc

    mode = resolve_eval_mode(embed=embed, embedding_endpoint=embedding_endpoint)
    use_embedding = mode == "hybrid"

    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=2, open=False, name="knowledge-eval")
    await pool.open(wait=True)
    try:
        repository = KnowledgeRepository(pool)
        vector = None
        if use_embedding:
            embedder = HttpEmbeddingProvider(embedding_endpoint, dimension=embedding_dimension)
            vector = PgVectorRetriever(repository, embedder, dimension=embedding_dimension)
        principal = RetrievalPrincipal(tenant_id=tenant_id, departments=frozenset(), internal=True)

        cases = EVAL_CASES[:limit] if limit > 0 else EVAL_CASES
        totals = {"top1": 0.0, "recall": 0.0, "mrr": 0.0}
        by_category: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "top1": 0.0, "recall": 0.0}
        )
        no_hits: list[str] = []
        category_of = {
            doc_id: category
            for category, docs in document_ids_by_category().items()
            for doc_id in docs
        }

        for case in cases:
            hits = await _retrieve(repository, vector, principal, case["query"], topk=topk)
            top1, recall, mrr = _metrics(hits, case["expected_document_ids"], topk)
            totals["top1"] += top1
            totals["recall"] += recall
            totals["mrr"] += mrr
            if not hits:
                no_hits.append(case["query"])
            for doc_id in case["expected_document_ids"]:
                category = category_of.get(doc_id, "other")
                by_category[category]["count"] += 1
                by_category[category]["top1"] += top1
                by_category[category]["recall"] += recall

        count = len(cases)
        return {
            "count": count,
            "topk": topk,
            "mode": "hybrid" if vector is not None else "lexical-only",
            "embedding_model": embedding_model,
            "totals": {key: value / count for key, value in totals.items()},
            "by_category": {
                category: {
                    "count": int(entry["count"]),
                    "top1": entry["top1"] / entry["count"],
                    "recall": entry["recall"] / entry["count"],
                }
                for category, entry in sorted(by_category.items())
            },
            "no_hits": no_hits,
        }
    finally:
        await pool.close()


def _print_report(report: dict) -> None:
    print("=" * 56)
    print(f"检索评测报告（模式: {report['mode']}，topk={report['topk']}，用例: {report['count']}）")
    if report["embedding_model"]:
        print(f"embedding model: {report['embedding_model']}")
    print("=" * 56)
    totals = report["totals"]
    top1_hits = int(round(totals["top1"] * report["count"]))
    print(f"Top1 命中率: {totals['top1'] * 100:.1f}%  ({top1_hits}/{report['count']})")
    print(f"Recall@{report['topk']}: {totals['recall'] * 100:.1f}%")
    print(f"MRR@{report['topk']}:   {totals['mrr']:.3f}")
    print("-" * 56)
    print("按分类分项（Top1 / Recall@k / 用例数）:")
    for category, entry in report["by_category"].items():
        print(
            f"  {category:<12} {entry['top1'] * 100:5.1f}%  {entry['recall'] * 100:5.1f}%  n={entry['count']}"
        )
    print("-" * 56)
    no_hits = report["no_hits"]
    print(f"无检索命中（受理链路将转人工，不自动发送）: {len(no_hits)} 条")
    for query in no_hits:
        print(f"  - {query}")


def main() -> None:
    parser = argparse.ArgumentParser(description="IT 服务台知识检索评测")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="评测租户 ID（默认 demo）")
    parser.add_argument("--topk", type=int, default=DEFAULT_TOPK, help="检索深度 k（默认 5）")
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0 = 全部）")
    parser.add_argument("--seed", action="store_true", help="评测前先导入幂等种子数据")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="强制启用 embedding 检索（需 KNOWLEDGE_EMBEDDING_ENDPOINT）",
    )
    parser.add_argument(
        "--database-url", default=None, help="PostgreSQL 连接串（默认读 DATABASE_URL / .env）"
    )
    parser.add_argument(
        "--fail-under-top1",
        type=float,
        default=None,
        help="Top1 门禁阈值（低于则非零退出，如 0.80）",
    )
    parser.add_argument(
        "--fail-under-recall5", type=float, default=None, help="Recall@5 门禁阈值（如 0.90）"
    )
    parser.add_argument(
        "--fail-under-mrr", type=float, default=None, help="MRR@5 门禁阈值（如 0.75）"
    )
    args = parser.parse_args()

    load_dotenv()
    conninfo = args.database_url or os.getenv("DATABASE_URL", "").strip()
    if not conninfo:
        raise SystemExit("缺少 DATABASE_URL：请设置环境变量或使用 --database-url")
    report = asyncio.run(
        _run_eval(
            conninfo,
            tenant_id=args.tenant,
            topk=args.topk,
            limit=args.limit,
            seed=args.seed,
            embed=args.embed,
        )
    )
    _print_report(report)
    _enforce_gate(report, args)


def _enforce_gate(report: dict, args: argparse.Namespace) -> None:
    """评测门禁：低于阈值时非零退出（CI 用）。"""
    totals = report["totals"]
    checks = [
        (args.fail_under_top1, totals["top1"], "Top1"),
        (args.fail_under_recall5, totals["recall"], f"Recall@{report['topk']}"),
        (args.fail_under_mrr, totals["mrr"], f"MRR@{report['topk']}"),
    ]
    failures = [
        (
            f"{name}={value * 100:.1f}% < {threshold * 100:.1f}%"
            if name != f"MRR@{report['topk']}"
            else f"{name}={value:.3f} < {threshold}"
        )
        for threshold, value, name in checks
        if threshold is not None and value < threshold
    ]
    if failures:
        raise SystemExit(f"评测门禁未达标: {', '.join(failures)}")


if __name__ == "__main__":
    main()
