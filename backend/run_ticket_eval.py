"""IT 服务台 V1 工单链路评测运行器（Day 3 / Day 10）。

用法（无外部依赖，默认 static 模式）：
    uv run python -m backend.run_ticket_eval --json docs/evaluation/ticket-eval-report.json

可选接入真实 PostgreSQL 词法检索（TEST_DATABASE_URL / --database-url）：
    uv run python -m backend.run_ticket_eval --database-url %TEST_DATABASE_URL%
    --json docs/evaluation/ticket-eval-report.json

报告内容：
    - 分类 Top1（总体 + 按场景），字段缺失检测匹配率，人工接管率，团队命中率
    - 知识模式 static/db：static 只验证预期文档存在与门禁规则，
      db 模式执行真实词法检索并统计引用支撑率与 ACL 泄露
    - 每条失败样本可定位（index + scenario + text + reason）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from src.my_agent.helpdesk import (
    IntakePolicy,
    KeywordTicketClassifier,
    TicketCategory,
    assess_and_dispatch,
    missing_required_fields,
)

from .knowledge.models import RetrievalPrincipal
from .knowledge.repository import KnowledgeRepository
from .knowledge.service import answer_status
from .knowledge.ticket_eval_cases import (
    TICKET_EVAL_CASES,
    TICKET_EVAL_VERSION,
    ticket_eval_case_count,
    ticket_eval_scenario_counts,
)

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _actual_category(classification) -> str:
    category = classification.category.value
    subcategory = classification.subcategory
    if subcategory and subcategory != "general":
        return f"{category}.{subcategory}"
    return category


def _expected_missing(case: Mapping[str, Any]) -> tuple[str, ...]:
    fields = case.get("provided_fields") or {}
    return tuple(
        name
        for name in (case.get("required_fields") or ())
        if fields.get(name) in (None, "", [], {})
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * pct)))
    return sorted_values[index]


async def _evaluate_case(
    classifier: KeywordTicketClassifier,
    policy: IntakePolicy,
    case: Mapping[str, Any],
    repository: KnowledgeRepository | None,
    tenant_id: str,
) -> dict[str, Any]:
    """评估单条工单样本（纯组件，无外部模型调用）。"""
    text = str(case["text"])
    fields = dict(case.get("provided_fields") or {})
    expected_category = str(case["expected_category"])

    started = time.monotonic()
    classification = await classifier.classify(text, fields)
    latency_ms = (time.monotonic() - started) * 1000

    actual_category = _actual_category(classification)
    top1 = actual_category == expected_category
    expected_enum = TicketCategory(expected_category.split(".", 1)[0])
    actual_missing = missing_required_fields(fields, expected_enum, policy)
    expected_missing = _expected_missing(case)
    field_check_ok = set(actual_missing) == set(expected_missing)

    decision = assess_and_dispatch(
        text=text,
        category=classification.category,
        classification_needs_review=bool(classification.needs_human_review),
        clarification_exhausted=False,
        policy=policy,
    )
    out_of_scope = "out_of_scope_manual_review" in decision.reason_codes
    has_evidence = bool(case.get("expected_document_ids")) and not bool(
        case.get("forbidden_document_ids")
    )
    manual_actual = bool(not has_evidence or classification.needs_human_review or out_of_scope)
    manual_expected = bool(case.get("expected_human_takeover", False))
    manual_ok = manual_actual == manual_expected

    team_checked = not actual_missing
    team_actual = decision.team_id if team_checked else None
    team_ok = bool(team_checked and team_actual == case.get("expected_team"))

    # 知识/引用：仅 db 模式做真实词法检索并产生指标；
    # static 模式 reference_supported / forbidden_leak 保持 None（N/A，不参与通过判定）。
    forbidden_leak: bool | None = None
    retrieved: list[str] = []
    reference_supported: bool | None = None
    if repository is not None:
        principal = RetrievalPrincipal(
            tenant_id=tenant_id,
            departments=frozenset(case.get("departments") or ()),
            internal=False,
        )
        hits = await repository.lexical_search(principal, text, limit=5)
        retrieved = [hit.document_id for hit in hits]
        forbidden = set(case.get("forbidden_document_ids") or ())
        forbidden_leak = bool(forbidden & set(retrieved))
        expected = set(case.get("expected_document_ids") or ())
        if expected:
            reference_supported = expected.issubset(retrieved)
        else:
            # 无预期文档（no_knowledge / ACL）不参与引用支撑率分母
            reference_supported = None

    status = answer_status(
        decision.reason_codes if not has_evidence else ("gate_passed",),
        auto_reply=has_evidence and not manual_actual,
    )
    return {
        "index": None,  # 由外层填充
        "scenario": str(case.get("scenario")),
        "text": text,
        "expected_category": expected_category,
        "actual_category": actual_category,
        "classification_top1": top1,
        "expected_missing": expected_missing,
        "actual_missing": actual_missing,
        "field_check_ok": field_check_ok,
        "expected_team": str(case.get("expected_team")),
        "actual_team": team_actual,
        "team_ok": team_ok,
        "team_checked": team_checked,
        "expected_human_takeover": manual_expected,
        "actual_human_takeover": manual_actual,
        "manual_ok": manual_ok,
        "expected_document_ids": list(case.get("expected_document_ids") or ()),
        "forbidden_document_ids": list(case.get("forbidden_document_ids") or ()),
        "retrieved_document_ids": retrieved,
        "reference_supported": reference_supported,
        "forbidden_leak": forbidden_leak,
        "knowledge_mode": "db" if repository is not None else "static",
        "answer_status": status,
        "reasonable_expected": bool(case.get("expected_document_ids")),
        "latency_ms": latency_ms,
    }


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    mode = str(results[0].get("knowledge_mode") or "static") if results else "static"
    by_scenario_counts: dict[str, int] = defaultdict(int)
    by_scenario_top1: dict[str, int] = defaultdict(int)
    by_scenario_manual: dict[str, int] = defaultdict(int)
    for item in results:
        scenario = item["scenario"]
        by_scenario_counts[scenario] += 1
        by_scenario_top1[scenario] += int(item["classification_top1"])
        by_scenario_manual[scenario] += int(item["actual_human_takeover"])

    latencies = sorted(float(item["latency_ms"]) for item in results)
    failures: list[dict[str, Any]] = []
    for item in results:
        reasons = []
        if not item["classification_top1"]:
            reasons.append("classification_top1")
        if not item["field_check_ok"]:
            reasons.append("field_check")
        if not item["manual_ok"]:
            reasons.append("manual_takeover")
        if item["team_checked"] and not item["team_ok"]:
            reasons.append("team")
        if mode == "db" and item["forbidden_leak"]:
            reasons.append("acl_leak")
        if (
            mode == "db"
            and item["reasonable_expected"]
            and item["reference_supported"] is False
        ):
            reasons.append("reference_miss")
        if reasons:
            failures.append({
                "index": item["index"],
                "scenario": item["scenario"],
                "text": item["text"],
                "expected_category": item["expected_category"],
                "actual_category": item["actual_category"],
                "reasons": reasons,
            })

    measured_support = [item for item in results if item["reference_supported"] is not None]
    support_denominator = len([item for item in measured_support if item["reasonable_expected"]])
    support_numerator = len(
        [
            item
            for item in measured_support
            if item["reasonable_expected"] and item["reference_supported"]
        ]
    )
    return {
        "total": total,
        "scenario_counts": dict(by_scenario_counts),
        "classification": {
            "top1": _rate(sum(int(item["classification_top1"]) for item in results), total),
            "by_scenario": {
                scenario: _rate(by_scenario_top1[scenario], by_scenario_counts[scenario])
                for scenario in sorted(by_scenario_counts)
            },
        },
        "field_completion": {
            "detection_rate": _rate(
                sum(int(item["field_check_ok"]) for item in results), total
            ),
            "auto_complete_rate": _rate(
                sum(int(not item["actual_missing"]) for item in results), total
            ),
        },
        "manual_handoff": {
            "actual_rate": _rate(
                sum(int(item["actual_human_takeover"]) for item in results), total
            ),
            "expected_rate": _rate(
                sum(int(item["expected_human_takeover"]) for item in results), total
            ),
            "policy_ok_rate": _rate(
                sum(int(item["manual_ok"]) for item in results), total
            ),
        },
        "team": {
            "checked": sum(int(item["team_checked"]) for item in results),
            "match_rate": _rate(
                sum(int(item["team_checked"] and item["team_ok"]) for item in results),
                sum(int(item["team_checked"]) for item in results),
            ),
        },
        "knowledge": {
            "mode": mode,
            "reference_support_rate": (
                _rate(support_numerator, support_denominator)
                if mode == "db" and support_denominator
                else None
            ),
            "reference_support_denominator": support_denominator if mode == "db" else None,
            "acl_leaks": (
                sum(int(item["forbidden_leak"]) for item in results)
                if mode == "db"
                else None
            ),
        },
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
        },
        "failures": failures,
        "failure_count": len(failures),
    }


def _write_report(report: dict[str, Any], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_eval(database_url: str | None, tenant_id: str) -> dict[str, Any]:
    classifier = KeywordTicketClassifier()
    policy = IntakePolicy()
    repository: KnowledgeRepository | None = None
    pool: AsyncConnectionPool | None = None
    if database_url:
        pool = AsyncConnectionPool(database_url, min_size=1, max_size=2, open=False, name="ticket-eval")
        await pool.open(wait=True)
        repository = KnowledgeRepository(pool)
    try:
        results = [
            await _evaluate_case(classifier, policy, case, repository, tenant_id)
            for case in TICKET_EVAL_CASES
        ]
    finally:
        if pool is not None:
            await pool.close()
    for index, item in enumerate(results, start=1):
        item["index"] = index
    return {
        "version": TICKET_EVAL_VERSION,
        "dataset": "ticket_v1",
        "total_cases": ticket_eval_case_count(),
        "scenario_counts": ticket_eval_scenario_counts(),
        "agent": "deterministic-keyword-classifier + v1-intake-policy",
        ** _summarize(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="IT 服务台 V1 工单链路评测")
    parser.add_argument("--json", default="docs/evaluation/ticket-eval-report.json", help="报告输出路径")
    parser.add_argument(
        "--database-url",
        default=os.getenv("TEST_DATABASE_URL", "").strip() or None,
        help="可选 PostgreSQL 连接串；配置后执行真实词法检索",
    )
    parser.add_argument("--tenant", default="demo", help="知识库租户（db 模式使用）")
    parser.add_argument("--require-db", action="store_true", help="只允许 PostgreSQL 真实检索模式通过")
    parser.add_argument("--fail-under-classification", type=float, default=0.0)
    parser.add_argument("--fail-under-field-rate", type=float, default=0.0)
    parser.add_argument("--fail-under-reference", type=float, default=1.0)
    parser.add_argument("--max-acl-leaks", type=int, default=0)
    args = parser.parse_args()
    if args.require_db and not args.database_url:
        raise SystemExit(
            "--require-db 需要配置 --database-url 或 TEST_DATABASE_URL；"
            "static 模式不产生引用/ACL 指标，不允许作为数据库评测通过"
        )

    report = asyncio.run(_run_eval(args.database_url, args.tenant))
    _write_report(report, args.json)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = True
    if report["classification"]["top1"] < args.fail_under_classification:
        ok = False
    if report["field_completion"]["detection_rate"] < args.fail_under_field_rate:
        ok = False
    if report["knowledge"]["mode"] == "db":
        reference = report["knowledge"]["reference_support_rate"]
        acl_leaks = report["knowledge"]["acl_leaks"]
        if reference is None or reference < args.fail_under_reference:
            ok = False
            print(f"引用支撑率未达标: {reference} < {args.fail_under_reference}")
        if acl_leaks is None or acl_leaks > args.max_acl_leaks:
            ok = False
            print(f"ACL 泄露数未达标: {acl_leaks} > {args.max_acl_leaks}")
    elif args.require_db:
        ok = False
        print("--require-db 模式下报告必须为 db 模式")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
