"""知识评测运行器单元测试 —— 指标计算、模式判定与评测集数据完整性。

不依赖 PostgreSQL：只覆盖 backend/run_knowledge_eval.py 的纯逻辑部分
（_metrics / resolve_eval_mode / 用例分类 / 数据集加载）与 seed/holdout
评测集数据契约；检索链路本身由 tests/test_knowledge_repository_postgres.py
覆盖。
"""

from __future__ import annotations

import pytest

from backend.knowledge import RetrievalHit
from backend.knowledge.eval_cases import (
    EVAL_CASES,
    SEED_VERSION,
    document_ids_by_category,
    eval_case_count,
)
from backend.knowledge.eval_holdout_cases import (
    HOLDOUT_CASES,
    HOLDOUT_VERSION,
    holdout_case_count,
)
from backend.run_knowledge_eval import (
    _classify_case,
    _evaluate_case,
    _load_cases,
    _metrics,
    resolve_eval_mode,
)


def _hit(document_id: str, rank: int) -> RetrievalHit:
    return RetrievalHit(
        tenant_id="demo",
        document_id=document_id,
        document_version=1,
        chunk_id=f"c-{document_id}-{rank}",
        title=document_id,
        content="content",
        source_uri=None,
        source="lexical",
        source_rank=rank,
    )


def test_metrics_full_hit_at_top1():
    hits = [_hit("vpn-001", 1), _hit("network-001", 2)]
    top1, recall, mrr = _metrics(hits, ("vpn-001",), topk=5)
    assert (top1, recall, mrr) == (1.0, 1.0, 1.0)


def test_metrics_partial_expected_and_rank_position():
    # 期望两个文档，只命中一个且在 rank 3 → Recall 0.5、MRR 1/3、Top1 0。
    hits = [_hit("email-001", 1), _hit("password-001", 2), _hit("vpn-001", 3)]
    top1, recall, mrr = _metrics(hits, ("vpn-001", "network-001"), topk=5)
    assert top1 == 0.0
    assert recall == 0.5
    assert mrr == pytest.approx(1 / 3)


def test_metrics_empty_hits():
    top1, recall, mrr = _metrics([], ("vpn-001",), topk=5)
    assert (top1, recall, mrr) == (0.0, 0.0, 0.0)


def test_metrics_ignores_hits_beyond_topk():
    # 期望文档出现在第 6 位（超出 topk=5）→ 不计入命中。
    hits = [_hit(f"other-{i:03d}", rank) for rank, i in enumerate(range(1, 6), start=1)]
    hits.append(_hit("vpn-001", 6))
    top1, recall, mrr = _metrics(hits, ("vpn-001",), topk=5)
    assert (top1, recall, mrr) == (0.0, 0.0, 0.0)


def test_metrics_duplicate_document_ids_deduped_by_recall():
    hits = [_hit("vpn-001", 1), _hit("vpn-001", 2)]
    top1, recall, mrr = _metrics(hits, ("vpn-001",), topk=5)
    assert (top1, recall, mrr) == (1.0, 1.0, 1.0)


def test_resolve_eval_mode_defaults_to_lexical_only():
    assert resolve_eval_mode(embed=False, embedding_endpoint="") == "lexical-only"


def test_resolve_eval_mode_hybrid_from_endpoint_or_flag():
    assert resolve_eval_mode(embed=False, embedding_endpoint="https://emb.example/v1") == "hybrid"
    assert resolve_eval_mode(embed=True, embedding_endpoint="https://emb.example/v1") == "hybrid"


def test_resolve_eval_mode_rejects_embed_without_endpoint():
    with pytest.raises(SystemExit, match="EMBEDDING_ENDPOINT"):
        resolve_eval_mode(embed=True, embedding_endpoint="")


def test_eval_cases_data_contract():
    """评测集完整性：数量 >= 50，query 非空，期望文档全部可解析到分类。"""
    count = eval_case_count()
    assert count >= 50
    categories = document_ids_by_category()
    known_documents = {doc_id for docs in categories.values() for doc_id in docs}
    for case in EVAL_CASES:
        assert case["query"].strip()
        assert case["expected_document_ids"]
        assert all(doc_id in known_documents for doc_id in case["expected_document_ids"])


# ========== hybrid_holdout：冻结集契约与用例分类 ==========


def test_holdout_version_is_frozen():
    """holdout 版本号固定格式（YYYY-MM-DD-vN）；变更必须递增（治理要求）。"""
    assert HOLDOUT_VERSION.startswith("2026-08-30-v")
    assert HOLDOUT_VERSION.count("-") == 3


def test_holdout_cases_data_contract():
    """holdout 契约：query 必填；metric/no_answer/acl 三种分类互斥且至少一个。

    冻结集要求：
    - expected_none 与 expected_document_ids 互斥（无答案不计召回）
    - forbidden_document_ids 不与 expected_document_ids 冲突
    - 覆盖全部难点维度：口语/跨文档/ACL/错误码/近义词/无答案
    """
    assert holdout_case_count() >= 15
    kinds = {"metric": 0, "no_answer": 0, "acl": 0}
    for case in HOLDOUT_CASES:
        assert case["query"].strip()
        assert "expected_document_ids" in case or case.get("expected_none") or case.get(
            "forbidden_document_ids"
        )
        if case.get("expected_none"):
            assert "expected_document_ids" not in case  # 无答案不计召回
        if case.get("forbidden_document_ids"):
            assert "expected_document_ids" not in case  # 隔离用例单独统计
        kinds[_classify_case(case)] += 1
    assert kinds["metric"] >= 10
    assert kinds["no_answer"] >= 3
    assert kinds["acl"] >= 1
    # ACL 用例必须带部门主体（否则与空部门主体无区别）
    for case in HOLDOUT_CASES:
        if case.get("forbidden_document_ids"):
            assert case.get("principal_departments")


def test_classify_case_three_kinds():
    assert _classify_case({"query": "x", "expected_document_ids": ("a",)}) == "metric"
    assert _classify_case({"query": "x", "expected_none": True}) == "no_answer"
    assert _classify_case({"query": "x", "forbidden_document_ids": ("a",)}) == "acl"


def test_evaluate_metric_case():
    hits = [_hit("vpn-001", 1)]
    result = _evaluate_case({"query": "x", "expected_document_ids": ("vpn-001",)}, hits, topk=5)
    assert result["kind"] == "metric"
    assert (result["top1"], result["recall"], result["mrr"]) == (1.0, 1.0, 1.0)


def test_evaluate_no_answer_case_tracks_misrecall_only():
    # 无答案用例：hits 非空即误召回（未配置拒答阈值时），不进召回指标。
    result = _evaluate_case({"query": "x", "expected_none": True}, [], topk=5)
    assert result == {"kind": "no_answer", "misrecalled": False, "top_similarity": None}
    leaked = _evaluate_case(
        {"query": "x", "expected_none": True}, [_hit("vpn-001", 1)], topk=5
    )
    assert leaked["misrecalled"] is True


def _hit_with_similarity(document_id: str, rank: int, similarity: float) -> RetrievalHit:
    return RetrievalHit(
        tenant_id="demo",
        document_id=document_id,
        document_version=1,
        chunk_id=f"c-{document_id}-{rank}",
        title=document_id,
        content="content",
        source_uri=None,
        source="vector",
        source_rank=rank,
        similarity=similarity,
    )


def test_evaluate_no_answer_respects_min_similarity_threshold():
    # 拒答阈值：命中相似度低于阈值 = 证据不足，正确拒绝（转人工）。
    low = _evaluate_case(
        {"query": "x", "expected_none": True},
        [_hit_with_similarity("vpn-001", 1, 0.30)],
        topk=5,
        min_similarity=0.45,
    )
    assert low["misrecalled"] is False
    assert low["top_similarity"] == 0.30
    # 相似度达到阈值才算误召回（高置信误判）。
    high = _evaluate_case(
        {"query": "x", "expected_none": True},
        [_hit_with_similarity("vpn-001", 1, 0.50)],
        topk=5,
        min_similarity=0.45,
    )
    assert high["misrecalled"] is True
    # 阈值判定只看最高相似度。
    mixed = _evaluate_case(
        {"query": "x", "expected_none": True},
        [
            _hit_with_similarity("a-001", 1, 0.20),
            _hit_with_similarity("b-001", 2, 0.55),
        ],
        topk=5,
        min_similarity=0.45,
    )
    assert mixed["misrecalled"] is True
    assert mixed["top_similarity"] == 0.55


def test_evaluate_acl_case_tracks_leak_only():
    # ACL 隔离用例：命中受限文档即泄露，不进召回指标。
    hits = [_hit("finance-001", 1), _hit("network-001", 2)]
    result = _evaluate_case(
        {"query": "x", "forbidden_document_ids": ("finance-001",)}, hits, topk=5
    )
    assert result["kind"] == "acl"
    assert result["leaked"] is True
    assert result["leaked_documents"] == ["finance-001"]
    clean = _evaluate_case(
        {"query": "x", "forbidden_document_ids": ("finance-001",)},
        [_hit("network-001", 1)],
        topk=5,
    )
    assert clean["leaked"] is False


def test_load_cases_returns_dataset_and_version():
    seed_cases, seed_version = _load_cases("seed")
    assert seed_cases is EVAL_CASES
    assert seed_version == SEED_VERSION
    holdout_cases, holdout_version = _load_cases("hybrid_holdout")
    assert holdout_cases is HOLDOUT_CASES
    assert holdout_version == HOLDOUT_VERSION
