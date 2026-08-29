"""IT 服务台知识检索评测运行器 —— 输出可量化的检索指标。

用法（需要 PostgreSQL；评测集支持 seed 开发集与冻结的 hybrid_holdout）：

    # 1) 准备数据：迁移 + 导入脱敏 IT 知识库（幂等）
    uv run python -m backend.seed_demo
    # 2) seed 开发集回归（未配置 embedding 时自动 lexical-only）
    uv run python -m backend.run_knowledge_eval --dataset seed
    # 3) hybrid holdout（需真实 embedding 服务；无 endpoint 时 --embed 直接失败）
    uv run python -m backend.run_knowledge_eval --dataset hybrid_holdout --embed \
        --fail-under-top1 0.80 --fail-under-recall5 0.90 --fail-under-mrr 0.75

指标：Top1 命中率、Recall@k、MRR@k（按分类分项）；同时统计「无检索命中」
用例数 —— 这些用例在受理链路中会触发门禁转人工，不会自动发送建议。

用例分类（holdout 支持）：
- metric：计入 Top1/Recall/MRR 召回指标
- no_answer（expected_none）：期望零命中，单独统计正确拒绝率
- acl（forbidden_document_ids）：不应命中受限文档，单独统计隔离泄露
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from collections.abc import Mapping
from time import monotonic
from typing import Any

from psycopg_pool import AsyncConnectionPool

from .config import load_environment
from .knowledge import (
    HttpEmbeddingProvider,
    KnowledgeRepository,
    PgVectorRetriever,
    RetrievalHit,
    RetrievalPrincipal,
    reciprocal_rank_fusion,
)
from .knowledge.eval_cases import SEED_VERSION, document_ids_by_category
from .knowledge.eval_holdout_cases import HOLDOUT_VERSION

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TENANT = "demo"
DEFAULT_TOPK = 5
DATASETS = ("seed", "hybrid_holdout")


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


def _classify_case(case: Mapping[str, Any]) -> str:
    """用例分类：metric（召回指标）/ no_answer（无答案）/ acl（ACL 隔离）。

    无答案（expected_none）与 ACL 隔离（forbidden_document_ids）单独统计，
    不混入 Top1/Recall/MRR —— 与门禁表（no_answer 单独记录）保持一致。
    """
    if case.get("expected_none"):
        return "no_answer"
    if case.get("forbidden_document_ids"):
        return "acl"
    return "metric"


def _evaluate_case(
    case: Mapping[str, Any],
    hits: list[RetrievalHit],
    topk: int,
    min_similarity: float | None = None,
) -> dict[str, Any]:
    """对单条用例按分类产出评估结果（纯函数，便于单元测试）。

    metric 用例 -> {kind, top1, recall, mrr}
    no_answer 用例 -> {kind, misrecalled, top_similarity}：
        检索返回非空即误召回；当配置 min_similarity（拒答阈值）时，
        仅当最高向量相似度 >= 阈值才算误召回（低于阈值 = 证据不足，正确
        拒绝转人工，与"检索后门禁"语义一致）。
    acl 用例 -> {kind, leaked, leaked_documents}（命中受限文档即泄露）
    """
    kind = _classify_case(case)
    if kind == "no_answer":
        if not hits:
            return {"kind": kind, "misrecalled": False, "top_similarity": None}
        sims = [h.similarity for h in hits if h.similarity is not None]
        top_similarity = max(sims) if sims else None
        if min_similarity is None:
            misrecalled = bool(hits)
        else:
            misrecalled = top_similarity is not None and top_similarity >= min_similarity
        return {
            "kind": kind,
            "misrecalled": misrecalled,
            "top_similarity": top_similarity,
        }
    if kind == "acl":
        forbidden = set(case.get("forbidden_document_ids") or ())
        leaked = [h.document_id for h in hits if h.document_id in forbidden]
        return {"kind": kind, "leaked": bool(leaked), "leaked_documents": leaked}
    top1, recall, mrr = _metrics(hits, case["expected_document_ids"], topk)
    return {"kind": kind, "top1": top1, "recall": recall, "mrr": mrr}


def _load_cases(dataset: str) -> tuple[tuple[Mapping[str, Any], ...], str]:
    """按数据集名加载评测用例与版本号。

    seed：开发期回归集（可随策略演进）；hybrid_holdout：冻结集（变更须递增版本）。
    """
    if dataset == "hybrid_holdout":
        from .knowledge.eval_holdout_cases import HOLDOUT_CASES

        return HOLDOUT_CASES, HOLDOUT_VERSION
    from .knowledge.eval_cases import EVAL_CASES

    return EVAL_CASES, SEED_VERSION


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
    dataset: str,
    min_similarity: float | None = None,
) -> dict:
    started = monotonic()
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

    cases, dataset_version = _load_cases(dataset)

    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=2, open=False, name="knowledge-eval")
    await pool.open(wait=True)
    try:
        repository = KnowledgeRepository(pool)
        vector = None
        if use_embedding:
            embedder = HttpEmbeddingProvider(embedding_endpoint, dimension=embedding_dimension)
            vector = PgVectorRetriever(repository, embedder, dimension=embedding_dimension)

        # 知识库版本（评测报告用）：文档数与最高版本号
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT count(*), coalesce(max(version), 0) FROM knowledge_documents"
                    " WHERE tenant_id = %s",
                    (tenant_id,),
                )
                row = await cursor.fetchone()
                doc_count = row[0] if row else 0
                max_version = row[1] if row else 0

        selected = cases[:limit] if limit > 0 else cases
        totals = {"top1": 0.0, "recall": 0.0, "mrr": 0.0}
        by_category: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "top1": 0.0, "recall": 0.0}
        )
        no_hits: list[str] = []
        no_answer: dict[str, Any] = {"count": 0, "misrecalled": 0, "queries": [], "similarities": []}
        acl: dict[str, Any] = {"count": 0, "leaked": 0, "queries": []}
        category_of = {
            doc_id: category
            for category, docs in document_ids_by_category().items()
            for doc_id in docs
        }

        for case in selected:
            # 每条用例可用独立部门主体（holdout 的 ACL 用例指定 principal_departments）
            departments = frozenset(case.get("principal_departments") or ())
            principal = RetrievalPrincipal(
                tenant_id=tenant_id, departments=departments, internal=True
            )
            hits = await _retrieve(repository, vector, principal, case["query"], topk=topk)
            result = _evaluate_case(case, hits, topk, min_similarity=min_similarity)
            kind = result["kind"]
            if kind == "no_answer":
                no_answer["count"] += 1
                if result["misrecalled"]:
                    no_answer["misrecalled"] += 1
                    no_answer["queries"].append(case["query"])
                    no_answer["similarities"].append(result["top_similarity"])
                continue
            if kind == "acl":
                acl["count"] += 1
                if result["leaked"]:
                    acl["leaked"] += 1
                    acl["queries"].append(case["query"])
                continue
            totals["top1"] += result["top1"]
            totals["recall"] += result["recall"]
            totals["mrr"] += result["mrr"]
            if not hits:
                no_hits.append(case["query"])
            for doc_id in case["expected_document_ids"]:
                category = category_of.get(doc_id, "other")
                by_category[category]["count"] += 1
                by_category[category]["top1"] += result["top1"]
                by_category[category]["recall"] += result["recall"]

        count = len(selected)
        metric_count = count - no_answer["count"] - acl["count"]
        # degraded：评测中向量检索失败必须整体失败（异常向上传播，不产出报告），
        # 因此成功路径恒为 False；线上运行时的降级见运行时 degraded 标记。
        degraded = False
        return {
            "dataset": dataset,
            "dataset_version": dataset_version,
            "count": count,
            "metric_count": metric_count,
            "no_answer_count": no_answer["count"],
            "acl_count": acl["count"],
            "topk": topk,
            "mode": "hybrid" if vector is not None else "lexical-only",
            "embedding_model": embedding_model,
            "dimension": embedding_dimension,
            "knowledge_base": {"documents": doc_count, "max_version": max_version},
            "runtime_seconds": round(monotonic() - started, 3),
            "degraded": degraded,
            "min_similarity": min_similarity,
            "totals": (
                {key: value / metric_count for key, value in totals.items()}
                if metric_count
                else {"top1": 0.0, "recall": 0.0, "mrr": 0.0}
            ),
            "by_category": {
                category: {
                    "count": int(entry["count"]),
                    "top1": entry["top1"] / entry["count"],
                    "recall": entry["recall"] / entry["count"],
                }
                for category, entry in sorted(by_category.items())
            },
            "no_hits": no_hits,
            "no_answer": no_answer,
            "acl": acl,
        }
    finally:
        await pool.close()


def _print_report(report: dict) -> None:
    print("=" * 64)
    print(
        f"检索评测报告（数据集: {report['dataset']}@{report['dataset_version']}，"
        f"模式: {report['mode']}，topk={report['topk']}，用例: {report['count']}）"
    )
    if report["embedding_model"]:
        print(f"embedding model: {report['embedding_model']} (dim={report['dimension']})")
    print(
        f"知识库: {report['knowledge_base']['documents']} 篇文档 / "
        f"max_version={report['knowledge_base']['max_version']}，"
        f"耗时: {report['runtime_seconds']}s"
    )
    print(f"degraded: {report['degraded']}")
    print("=" * 64)
    metric_count = report["metric_count"]
    totals = report["totals"]
    top1_hits = int(round(totals["top1"] * metric_count))
    print(f"Top1 命中率: {totals['top1'] * 100:.1f}%  ({top1_hits}/{metric_count})")
    print(f"Recall@{report['topk']}: {totals['recall'] * 100:.1f}%")
    print(f"MRR@{report['topk']}:   {totals['mrr']:.3f}")
    print("-" * 64)
    print("按分类分项（Top1 / Recall@k / 用例数）:")
    for category, entry in report["by_category"].items():
        print(
            f"  {category:<12} {entry['top1'] * 100:5.1f}%  {entry['recall'] * 100:5.1f}%  n={entry['count']}"
        )
    print("-" * 64)
    no_answer = report["no_answer"]
    if no_answer["count"]:
        threshold = report.get("min_similarity")
        threshold_note = (
            f"（拒答阈值 similarity >= {threshold} 才算误召回）" if threshold is not None else ""
        )
        print(
            f"无答案集（单独记录，不计入召回指标）: {no_answer['count']} 条，"
            f"误召回 {no_answer['misrecalled']} 条（应转人工）{threshold_note}"
        )
        for query in no_answer["queries"]:
            print(f"  - {query}")
    acl = report["acl"]
    if acl["count"]:
        print(
            f"ACL 隔离（单独记录，不计入召回指标）: {acl['count']} 条，"
            f"泄露 {acl['leaked']} 条"
        )
        for query in acl["queries"]:
            print(f"  - {query}")
    no_hits = report["no_hits"]
    print(f"无检索命中（受理链路将转人工，不自动发送）: {len(no_hits)} 条")
    for query in no_hits:
        print(f"  - {query}")


def main() -> None:
    parser = argparse.ArgumentParser(description="IT 服务台知识检索评测")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="评测租户 ID（默认 demo）")
    parser.add_argument(
        "--dataset",
        choices=DATASETS,
        default="seed",
        help="评测数据集：seed（开发期回归）/ hybrid_holdout（冻结门禁集）",
    )
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
        "--report-json",
        default=None,
        help="评测报告 JSON 输出路径（CI artifact 用，如 artifacts/hybrid-eval.json）",
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
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="向量相似度拒答阈值 [0,1]：无答案用例的最高命中相似度低于该值"
        "视为正确拒绝（检索后门禁），仅影响无答案判定，不影响召回指标",
    )
    args = parser.parse_args()

    load_environment()
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
            dataset=args.dataset,
            min_similarity=args.min_similarity,
        )
    )
    _print_report(report)
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入: {args.report_json}")
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
