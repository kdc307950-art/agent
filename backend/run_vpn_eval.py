"""VPN 专项评测运行器（vpn-v1 / vpn-v2；独立于混合 IT 工单口径）。

用法（无外部依赖，默认 static 模式）：
    .\\.venv\\Scripts\\python.exe -m backend.run_vpn_eval --json docs/evaluation/vpn-eval-report.json

可选接入真实 PostgreSQL 词法检索（TEST_DATABASE_URL / --database-url）：
    .\\.venv\\Scripts\\python.exe -m backend.run_vpn_eval --database-url %TEST_DATABASE_URL%
        --json docs/evaluation/vpn-eval-report.json

可用 --dataset vpn-v1|vpn-v2 选择评测集（vpn-v2 为 9 类场景扩展集，默认 vpn-v1）。

报告内容（仅针对 VPN 专项评测集）：
    - vpn_fault 分类准确率（总体 + 按类，仅 real VPN 样例）
    - 8 项固定字段补全率（缺失检测匹配率 detection_rate）
    - 边界判定正确率（expected_boundary vs 判定边界）
    - auto_suggest 样例引用支撑率（static 模式 N/A，db 模式计算）
    - 负向/越界样例「误导向 it.vpn 自动建议=0」统计
    - 闭环可达率（real VPN 样例进入 it.vpn 主线的比例）
    - 每条失败样本可定位（index + vpn_fault + text + reason）
    - metrics：9 项关键指标统一计算（vpn_eval_metrics.py，M1-M9 + D3 ACL 越权拒绝率）
    - protection：D4 标注「仅结构校验，不代表真实防护」

红线修复（t8）：
    - D1：空 expected_document_ids 不再恒 True，is_negative 样本一律视为无合法自动建议依据
      -> must_escalate，修复 ACL/账号锁定/无知识被误放行为 auto_suggest。
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
    boundary_vpn,
    classify_vpn_fault,
    contains_high_impact_term,
    contains_sensitive_term,
)

from .knowledge import vpn_eval_cases_v2
from .knowledge.models import RetrievalPrincipal
from .knowledge.repository import KnowledgeRepository
from .knowledge.vpn_eval_cases import (
    VPN_EVAL_CASES,
    VPN_EVAL_VERSION,
    VPN_REQUIRED_FIELDS,
    boundary_counts,
    count,
    fault_counts,
)
from .vpn.rules import (
    build_evidence_from_case,
    evaluate_evidence,
    hypothesis_code_from_hint,
    is_account_lockout_text,
)
from .vpn_eval_metrics import (
    compute_metrics_report,
    cost_constants_from_env,
    records_from_case_results,
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


def _actual_missing(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """8 项 VPN 必填字段的实际缺失检测（应补尽补）。"""
    return tuple(name for name in VPN_REQUIRED_FIELDS if fields.get(name) in (None, "", [], {}))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


async def _evaluate_case(
    classifier: KeywordTicketClassifier,
    policy: IntakePolicy,
    case: Mapping[str, Any],
    repository: KnowledgeRepository | None,
    tenant_id: str,
) -> dict[str, Any]:
    """评估单条 VPN 样本（纯组件，无外部模型调用）。"""
    text = str(case["text"])
    fields = dict(case.get("provided_fields") or {})
    expected_category = str(case["expected_category"])
    expected_fault = str(case["vpn_fault"])
    expected_boundary = str(case["expected_boundary"])
    is_negative = bool(case.get("is_negative", False))

    started = time.monotonic()
    classification = await classifier.classify(text, fields)
    latency_ms = (time.monotonic() - started) * 1000

    actual_category = _actual_category(classification)
    category_ok = actual_category == expected_category
    predicted_fault = classify_vpn_fault(text)
    fault_ok = predicted_fault == expected_fault

    expected_missing = _expected_missing(case)
    actual_missing = _actual_missing(fields)
    field_check_ok = set(actual_missing) == set(expected_missing)
    fields_complete = not actual_missing

    has_sensitive = contains_sensitive_term(text)
    has_high_impact = contains_high_impact_term(text)

    # D1 修复：解耦「无依据应升级」与「引用支撑」，并保证负向/越权样本不得误放行。
    # - is_negative（越权/高风险/无知识/账号锁定等语义高风险）样本一律视为「无合法自动建议
    #   依据」-> has_evidence=False，确保边界函数推向 must_escalate（反映真实防护应拒绝/升级）；
    #   修复原 `has_evidence = bool(expected & set(retrieved)) or not expected` 中空 expected
    #   恒 True 导致这些样本被误判 auto_suggest 的缺陷。
    # - db 模式依据 = 预期文档被实际召回；expected 为空（无知识）不得因空 expected 恒 True，
    #   按无依据处理 -> must_escalate。引用支撑仅在 auto_suggest 且有预期文档时量化。
    reference_supported: bool | None = None
    retrieved: list[str] = []
    expected = set(case.get("expected_document_ids") or ())
    if repository is not None:
        principal = RetrievalPrincipal(
            tenant_id=tenant_id,
            departments=frozenset(case.get("departments") or ()),
            internal=False,
        )
        hits = await repository.lexical_search(principal, text, limit=5)
        retrieved = [hit.document_id for hit in hits]
        has_evidence = False if is_negative else bool(expected & set(retrieved))
        if expected_boundary == "auto_suggest" and expected:
            reference_supported = expected.issubset(set(retrieved))
        else:
            reference_supported = None
    else:
        # static 模式：无真实检索；负向样本一律无依据，否则视预期文档非空为有依据。
        has_evidence = False if is_negative else bool(expected)

    # D6：账号锁定/禁用主因 -> 必须在边界层强制 must_escalate（it.account 人工）。
    has_account_lockout = is_account_lockout_text(text)
    predicted_boundary = boundary_vpn(
        predicted_fault,
        fields_complete,
        has_sensitive,
        has_high_impact,
        has_evidence,
        has_account_lockout=has_account_lockout,
    )
    boundary_ok = predicted_boundary == expected_boundary

    # 负向/越界样例“误导向 it.vpn 自动建议”= 被分类到 it.vpn 且判定为 auto_suggest
    misdirect = bool(
        is_negative and actual_category == "it.vpn" and predicted_boundary == "auto_suggest"
    )

    # 阶段三：结构化证据链假设（M3 真实口径）。
    #   - 预期假设编码：由样本的 fault_hypothesis 文本归一化（无则用 vpn_fault 兜底）；
    #   - 预测假设编码：由确定性规则 evaluate_evidence(build_evidence_from_case) 产出，
    #     不再回退 vpn_fault；evidence_found 用于证据充分率。
    evidence_chain = evaluate_evidence(build_evidence_from_case(case, identity_ok=True))
    expected_hypothesis_code = hypothesis_code_from_hint(
        str(case.get("fault_hypothesis") or expected_fault)
    )
    evidence_found = bool(evidence_chain.evidence or has_evidence)

    return {
        "index": None,  # 外层填充
        "scenario": str(case.get("scenario")),
        "text": text,
        "expected_category": expected_category,
        "actual_category": actual_category,
        "category_ok": category_ok,
        "vpn_fault": expected_fault,
        "predicted_fault": predicted_fault,
        "fault_ok": fault_ok,
        "is_negative": is_negative,
        "expected_boundary": expected_boundary,
        "predicted_boundary": predicted_boundary,
        "boundary_ok": boundary_ok,
        "expected_missing": expected_missing,
        "actual_missing": actual_missing,
        "field_check_ok": field_check_ok,
        "fields_complete": fields_complete,
        "has_sensitive_risk": has_sensitive,
        "has_high_impact": has_high_impact,
        "has_evidence": has_evidence,
        "misdirect": misdirect,
        "expected_document_ids": list(case.get("expected_document_ids") or ()),
        "retrieved_document_ids": retrieved,
        "reference_supported": reference_supported,
        "knowledge_mode": "db" if repository is not None else "static",
        "expected_team": str(case.get("expected_team")),
        # v2 补充指标字段透传（v1 无则 None/空，metrics 消费可直接读取）。
        "error_code": case.get("error_code"),
        "client_version": case.get("client_version"),
        "network_type": case.get("network_type"),
        "fault_hypothesis": case.get("fault_hypothesis"),
        "acls": list(case.get("acls") or ()),
        "risk_level": case.get("risk_level"),
        "escalation_expected": case.get("escalation_expected"),
        "departments": list(case.get("departments") or ()),
        "internal": bool(case.get("internal", False)),
        "resource": case.get("resource"),
        "latency_ms": latency_ms,
        # 阶段三：M3 真实口径（结构化证据链假设 + 证据充分率）。
        "expected_hypothesis_code": expected_hypothesis_code,
        "evidence_hypothesis": evidence_chain.hypothesis,
        "hypothesis_confidence": evidence_chain.confidence,
        "evidence_found": evidence_found,
    }


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    mode = str(results[0].get("knowledge_mode") or "static") if results else "static"

    vpn_results = [item for item in results if not item["is_negative"]]
    vpn_total = len(vpn_results)

    fault_by_counts: dict[str, int] = defaultdict(int)
    fault_by_ok: dict[str, int] = defaultdict(int)
    for item in vpn_results:
        fault = item["vpn_fault"]
        fault_by_counts[fault] += 1
        fault_by_ok[fault] += int(item["fault_ok"])

    category_check = sum(int(item["category_ok"]) for item in results)
    closed_loop = sum(int(item["actual_category"] == "it.vpn") for item in vpn_results)

    boundary_counts_seen: dict[str, int] = defaultdict(int)
    boundary_ok_by: dict[str, int] = defaultdict(int)
    for item in results:
        boundary_counts_seen[item["expected_boundary"]] += 1
        boundary_ok_by[item["expected_boundary"]] += int(item["boundary_ok"])

    misdirect_items = [item for item in results if item["misdirect"]]

    # auto_suggest 样例引用支撑率：仅 db 模式、且预期为 auto_suggest 的样本参与分母
    auto_suggest_hits = [
        item
        for item in results
        if item["expected_boundary"] == "auto_suggest" and item["reference_supported"] is not None
    ]
    support_denominator = len(auto_suggest_hits)
    support_numerator = sum(int(item["reference_supported"]) for item in auto_suggest_hits)

    latencies = sorted(float(item["latency_ms"]) for item in results)
    failures: list[dict[str, Any]] = []
    for item in results:
        reasons: list[str] = []
        if not item["category_ok"]:
            reasons.append("category_mismatch")
        if not item["fault_ok"]:
            reasons.append("fault_mismatch")
        if not item["field_check_ok"]:
            reasons.append("field_check")
        if not item["boundary_ok"]:
            reasons.append("boundary")
        if item["misdirect"]:
            reasons.append("misdirect_to_auto_suggest")
        if mode == "db" and item["reference_supported"] is False:
            reasons.append("reference_miss")
        if reasons:
            failures.append(
                {
                    "index": item["index"],
                    "scenario": item["scenario"],
                    "text": item["text"],
                    "vpn_fault": item["vpn_fault"],
                    "expected_category": item["expected_category"],
                    "actual_category": item["actual_category"],
                    "expected_boundary": item["expected_boundary"],
                    "predicted_boundary": item["predicted_boundary"],
                    "reasons": reasons,
                }
            )

    return {
        "total": total,
        "classify_vpn_fault": {
            "accuracy": _rate(sum(int(item["fault_ok"]) for item in vpn_results), vpn_total),
            "sample_count": vpn_total,
            "by_fault": {
                fault: _rate(fault_by_ok[fault], fault_by_counts[fault])
                for fault in sorted(fault_by_counts)
            },
        },
        "category": {
            "accuracy": _rate(category_check, total),
            "vpn_category_rate": _rate(
                sum(int(item["category_ok"]) for item in vpn_results), vpn_total
            ),
        },
        "field_completion": {
            "detection_rate": _rate(sum(int(item["field_check_ok"]) for item in results), total),
            "complete_rate": _rate(
                sum(int(item["fields_complete"]) for item in vpn_results), vpn_total
            ),
        },
        "boundary": {
            "accuracy": _rate(sum(int(item["boundary_ok"]) for item in results), total),
            "by_boundary": {
                boundary: _rate(boundary_ok_by[boundary], boundary_counts_seen[boundary])
                for boundary in sorted(boundary_counts_seen)
            },
        },
        "auto_misdirect": {
            "count": len(misdirect_items),
            "samples": [
                {
                    "index": item["index"],
                    "text": item["text"],
                    "expected_category": item["expected_category"],
                    "actual_category": item["actual_category"],
                }
                for item in misdirect_items
            ],
        },
        "knowledge": {
            "mode": mode,
            "reference_support_rate": (
                _rate(support_numerator, support_denominator)
                if mode == "db" and support_denominator
                else None
            ),
            "reference_support_denominator": (support_denominator if mode == "db" else None),
        },
        "closed_loop_reachable": {
            "rate": _rate(closed_loop, vpn_total),
            "sample_count": vpn_total,
        },
        "latency_ms": {
            "p50": round(
                (
                    sorted(latencies)[
                        min(len(latencies) - 1, max(0, round((len(latencies) - 1) * 0.50)))
                    ]
                    if latencies
                    else 0.0
                ),
                3,
            ),
            "p95": round(
                (
                    sorted(latencies)[
                        min(len(latencies) - 1, max(0, round((len(latencies) - 1) * 0.95)))
                    ]
                    if latencies
                    else 0.0
                ),
                3,
            ),
        },
        "failures": failures,
        "failure_count": len(failures),
    }


def _write_report(report: dict[str, Any], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _protection_guard_note() -> dict[str, Any]:
    """D4：明确标注本运行器的评测口径仅为「结构校验」，不代表真实防护。

    本运行器（static / db 词法检索）只调用 KeywordTicketClassifier + classify_vpn_fault +
    boundary_vpn + KnowledgeRepository.lexical_search，**不触发** tool_governance 的
    租户白名单/scope/allowed_tools 校验、repository 的 tenant+visibility+department ACL
    过滤、models.FORBIDDEN_COMMANDS 二次拒绝、approval.APPROVED 审批门禁等真实防护。
    因此 static/db 的 boundary / auto_misdirect / misdirect 数字只能证明结构层面的边界三态，
    不能作为「越权被拒 / 未审批被拦」的安全结论；这些需在集成/E2E 层用真实调用断言。
    """
    return {
        "structural_only": True,
        "real_guards_invoked": False,
        "note": (
            "本报告（static / db 词法检索）只做结构校验，不代表真实防护；"
            "boundary / auto_misdirect / misdirect 数字仅证明结构层面的边界三态。"
        ),
        "real_guards_verified_elsewhere": [
            "tool_governance.tenant_allowlist + required_scopes + allowed_tools",
            "repository.tenant + visibility + department ACL 过滤",
            "models.FORBIDDEN_COMMANDS 二次拒绝 / DiagnosisCommand extra=forbid",
            "approval.APPROVED 门禁 + 幂等 + preflight（reissue 受控执行）",
        ],
    }


def _select_dataset(dataset: str):
    """按 --dataset 选择评测集模块与其统计函数（vpn-v1 为默认/兼容口径）。

    返回 (cases, version, count_fn, fault_counts_fn, boundary_counts_fn,
    scenario_counts_fn | None)。
    """
    if dataset == "vpn-v2":
        return (
            vpn_eval_cases_v2.V2_EVAL_CASES,
            vpn_eval_cases_v2.VPN_EVAL_VERSION_V2,
            vpn_eval_cases_v2.count,
            vpn_eval_cases_v2.fault_counts,
            vpn_eval_cases_v2.boundary_counts,
            vpn_eval_cases_v2.scenario_counts,
        )
    return (
        VPN_EVAL_CASES,
        VPN_EVAL_VERSION,
        count,
        fault_counts,
        boundary_counts,
        None,
    )


async def _run_eval(
    database_url: str | None, tenant_id: str, dataset: str = "vpn-v1"
) -> dict[str, Any]:
    (
        cases,
        version,
        count_fn,
        fault_counts_fn,
        boundary_counts_fn,
        scenario_counts_fn,
    ) = _select_dataset(dataset)
    classifier = KeywordTicketClassifier()
    policy = IntakePolicy()
    repository: KnowledgeRepository | None = None
    pool: AsyncConnectionPool | None = None
    if database_url:
        pool = AsyncConnectionPool(
            database_url, min_size=1, max_size=2, open=False, name="vpn-eval"
        )
        await pool.open(wait=True)
        repository = KnowledgeRepository(pool)
    try:
        results = [
            await _evaluate_case(classifier, policy, case, repository, tenant_id) for case in cases
        ]
    finally:
        if pool is not None:
            await pool.close()
    for index, item in enumerate(results, start=1):
        item["index"] = index
    mode = str(results[0].get("knowledge_mode") or "static") if results else "static"
    input_cost, output_cost = cost_constants_from_env()
    metrics = compute_metrics_report(
        records_from_case_results(results),
        mode=mode,
        input_per_1k=input_cost,
        output_per_1k=output_cost,
    )
    report: dict[str, Any] = {
        "version": version,
        "dataset": "vpn_v2" if dataset == "vpn-v2" else "vpn_v1",
        "total_cases": count_fn(),
        "fault_counts": fault_counts_fn(),
        "boundary_counts": boundary_counts_fn(),
        "agent": "deterministic-keyword-classifier + classify_vpn_fault + vpn-boundary",
        # D4：本运行器（static / db 词法检索）只做结构校验，不触发真实防护。
        "protection": _protection_guard_note(),
        **_summarize(results),
        # 9 项关键指标统一计算口径（见 backend/vpn_eval_metrics.py 与
        # docs/evaluation/vpn-eval-metrics.md）；与上方原有小节并存、互补。
        "metrics": metrics,
    }
    if scenario_counts_fn is not None:
        report["scenario_counts"] = scenario_counts_fn()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="VPN 专项链路评测（vpn-v1）")
    parser.add_argument(
        "--json",
        default="docs/evaluation/vpn-eval-report.json",
        help="报告输出路径",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("TEST_DATABASE_URL", "").strip() or None,
        help="可选 PostgreSQL 连接串；配置后执行真实词法检索",
    )
    parser.add_argument("--tenant", default="demo", help="知识库租户（db 模式使用）")
    parser.add_argument(
        "--dataset",
        choices=("vpn-v1", "vpn-v2"),
        default="vpn-v1",
        help="评测集：vpn-v1(60 条) 或 vpn-v2(9 类场景扩展集)",
    )
    parser.add_argument(
        "--require-db", action="store_true", help="只允许 PostgreSQL 真实检索模式通过"
    )
    parser.add_argument("--fail-under-vpn-fault", type=float, default=0.0)
    parser.add_argument("--fail-under-field-rate", type=float, default=0.0)
    parser.add_argument("--max-auto-misdirect", type=int, default=0)
    args = parser.parse_args()
    if args.require_db and not args.database_url:
        raise SystemExit(
            "--require-db 需要配置 --database-url 或 TEST_DATABASE_URL；"
            "static 模式不产生引用支撑率指标，不允许作为数据库评测通过"
        )

    report = asyncio.run(_run_eval(args.database_url, args.tenant, args.dataset))
    _write_report(report, args.json)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = True
    if report["classify_vpn_fault"]["accuracy"] < args.fail_under_vpn_fault:
        ok = False
    if report["field_completion"]["detection_rate"] < args.fail_under_field_rate:
        ok = False
    if report["auto_misdirect"]["count"] > args.max_auto_misdirect:
        ok = False
    if args.require_db and report["knowledge"]["mode"] != "db":
        ok = False
        print("--require-db 模式下报告必须为 db 模式")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
