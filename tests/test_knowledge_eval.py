"""知识评测运行器单元测试 —— 指标计算、模式判定与评测集数据完整性。

不依赖 PostgreSQL：只覆盖 backend/run_knowledge_eval.py 的纯逻辑部分
（_metrics / resolve_eval_mode）与 eval_cases 数据契约；检索链路本身
由 tests/test_knowledge_repository_postgres.py 覆盖。
"""

from __future__ import annotations

import pytest

from backend.knowledge import RetrievalHit
from backend.knowledge.eval_cases import EVAL_CASES, document_ids_by_category, eval_case_count
from backend.run_knowledge_eval import _metrics, resolve_eval_mode


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
