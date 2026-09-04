"""受控真实模型 E2E：用真实 DeepSeek 模型驱动 VPN Diagnosis Agent，验证 gate 并测 P95/成本。

本脚本是「受控 E2E」而非全量回归：
    - 只跑 1..N（--limit，默认 3）条代表性 VPN 诊断场景；
    - 用真实 ChatOpenAI(DeepSeek) 驱动 VpnDiagnosisAgent；
    - 数据源全部走只读 MockVpnAdapter，不真实发消息/改工单/触发 reissue（副作用为零）；
    - 对每个场景输出 hypothesis / evidence / steps / escalation / approval 与耗时、token、成本；
    - 汇总 P95(latency) 与单工单成本（token x 单价 / 工单数；单价为 0 时标 unrated 不报错）。

用法（项目根目录，用 .venv）：
    .\\.venv\\Scripts\\python.exe backend\\run_vpn_e2e_controlled.py --limit 3

环境变量：
    DEEPSEEK_API_KEY       必填，来自 .env（本脚本会 load_environment）
    LLM_BASE_URL           默认 https://api.deepseek.com
    LLM_MODEL              默认 deepseek-chat
    MODEL_INPUT_COST_PER_1K_USD / MODEL_OUTPUT_COST_PER_1K_USD   单价，默认 0（标 unrated）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

# 项目根加入 sys.path，保证 `import backend.*` / `import src.*` 可用（支持从任何 cwd 运行）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.config import load_environment  # noqa: E402
from backend.run_context import RunContext  # noqa: E402
from backend.vpn.agent import DiagnosisLimits, VpnDiagnosisAgent  # noqa: E402
from backend.vpn.mock_adapter import MockVpnAdapter  # noqa: E402
from backend.vpn.models import (  # noqa: E402
    FORBIDDEN_COMMANDS,
    DiagnosisCommand,
    DiagnosisCommandType,
    evaluate_handoff,
)
from backend.vpn.service import VpnDiagnosisService  # noqa: E402
from backend.vpn.tools import VPN_TOOLS  # noqa: E402

load_environment()


# ---------------------------------------------------------------------------
# 工具：记录每次真实模型调用的 usage 与耗时（VpnDiagnosisAgent 只调用 ainvoke）
# ---------------------------------------------------------------------------
class UsageRecordingModel:
    """包装已绑定工具的 ChatOpenAI，记录每次 ainvoke 的 usage_metadata 与耗时。

    VpnDiagnosisAgent.__init__ 会对传入 model 再次调用 bind_tools；这里已预先绑定，
    故 bind_tools 直接返回 self，避免重复绑定导致工具列表不一致。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def bind_tools(self, _tools: Any) -> UsageRecordingModel:
        return self

    async def ainvoke(self, messages: list[Any], config: Any = None) -> Any:
        start = time.perf_counter()
        response = await self.inner.ainvoke(messages, config=config)
        latency_ms = (time.perf_counter() - start) * 1000
        usage: dict[str, Any] = {}
        usage_metadata = getattr(response, "usage_metadata", None)
        if isinstance(usage_metadata, dict):
            usage = dict(usage_metadata)
        else:
            token_usage = (getattr(response, "response_metadata", None) or {}).get("token_usage") or {}
            usage = {
                "input_tokens": token_usage.get("prompt_tokens"),
                "output_tokens": token_usage.get("completion_tokens"),
                "total_tokens": token_usage.get("total_tokens"),
            }
        self.calls.append(
            {
                "usage": usage,
                "latency_ms": round(latency_ms, 1),
                "tool_calls": [
                    {"name": tc.get("name"), "args": tc.get("args")}
                    for tc in (getattr(response, "tool_calls", None) or [])
                ],
            }
        )
        return response


# ---------------------------------------------------------------------------
# 运行时桩：提供 prepare_context 所需的 tickets / ticket_operations，以及工具所需的
# vpn_adapter / metrics / tool_governance（全部只读、无副作用）。
# ---------------------------------------------------------------------------
def _stub_awrap(request: Any, execute: Any) -> Any:
    """治理桩：放行只读工具，返回工具真实结果（不记录拒绝/不触发审计副作用）。"""
    return execute(request)


def _make_runtime(adapter: MockVpnAdapter, ticket: dict[str, Any], overview: dict[str, Any]) -> Any:
    """构造一个轻量运行时桩（SimpleNamespace），供 service/agent 只读使用。"""
    from types import SimpleNamespace

    class _Ticket:
        def __init__(self, data: dict[str, Any]):
            self.ticket_id = data["ticket_id"]
            self.requester_id = data["requester_id"]
            self.title = data.get("title", "")
            self.description = data.get("description", "")
            self.asset_id = data.get("asset_id")
            self.status = SimpleNamespace(value=data.get("status", "CLASSIFIED"))
            self.version = data.get("version", 0)

            async def _get(tenant_id: str, ticket_id: str) -> Any:
                if ticket_id == data["ticket_id"]:
                    return self
                return None

            self.get = _get
            # transition 仅用于审批域（本脚本不触发），提供空实现保持接口存在。

            async def _transition(*_a: Any, **_k: Any) -> None:
                return None

            self.transition = _transition

    ticket_obj = _Ticket(ticket)

    class _TicketOps:
        async def get_ticket_overview(self, tenant_id: str, ticket_id: str) -> dict[str, Any]:
            return overview

    runtime = SimpleNamespace(
        tickets=ticket_obj,
        ticket_operations=_TicketOps(),
        vpn_adapter=adapter,
        metrics=SimpleNamespace(increment=lambda *a, **k: None),
        tool_governance=SimpleNamespace(awrap_tool_call=_stub_awrap),
        audit=SimpleNamespace(),
        context=None,  # 由 RuntimeView 挂 run_context；此处占位
    )
    return runtime


def _make_run_context(tenant_id: str, user_id: str) -> RunContext:
    return RunContext(
        run_id=f"vpn-e2e-{int(time.time()*1000)}",
        request_id=f"req-{int(time.time()*1000)}",
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"vpn:{tenant_id}:e2e",
        scopes=frozenset({"ticket:agent"}),
        deadline=time.time() + 120,
        allowed_tools=frozenset({t.name for t in VPN_TOOLS}),
        role="agent",
        internal=True,
    )


# ---------------------------------------------------------------------------
# 受控场景定义（代表性，非全量）
# ---------------------------------------------------------------------------
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "vpn_single_809",
        "title": "单用户连接失败 + 错误码 809",
        "description": "低风险、证据充分、身份明确；期望真实模型引用工具证据并给出处置（不误转人工）。",
        "ticket": {
            "ticket_id": "T-809",
            "requester_id": "user-042",
            "title": "VPN 连接失败 错误码 809",
            "description": "用户使用公司 VPN 无法建立连接，报错 809，重试多次无效，已持续 2 小时。",
            "asset_id": "asset-001",
            "status": "CLASSIFIED",
        },
        "overview": {
            "intake": {"fault": "connection_failed"},
            "messages": [
                {"direction": "in", "actor_id": "user-042", "content": "VPN 一直连不上，报 809"},
            ],
        },
        "expected_gate": {
            "must_handoff": "low_risk_can_be_handled",  # 低风险：允许转人工，但不应因缺证据而强制转人工
            "require_evidence": True,  # 引用必须落在工具证据上
        },
        "actor": {"tenant_id": "acme", "user_id": "user-042"},
    },
    {
        "id": "vpn_account_lock_confusion",
        "title": "账号锁定与 VPN 认证失败混淆",
        "description": "用户自称账号被锁定但错误码是 809；期望模型用账号状态工具佐证账号为 active（排除锁定），做身份/根因澄清。",
        "ticket": {
            "ticket_id": "T-691",
            "requester_id": "user-042",
            "title": "VPN 登录失败 错误码 809 且提示账号锁定",
            "description": "用户反馈登录 VPN 报错 809，系统提示账号可能被锁定，但账号其实刚重置过。",
            "asset_id": "asset-001",
            "status": "CLASSIFIED",
        },
        "overview": {
            "intake": {"fault": "auth_failed"},
            "messages": [
                {"direction": "in", "actor_id": "user-042", "content": "VPN 报 809，提示账号锁定"},
            ],
        },
        "expected_gate": {
            "must_handoff": "ambiguity_resolved_by_evidence",  # 用账号状态佐证区分锁定 vs VPN 故障
            "require_evidence": True,
        },
        "actor": {"tenant_id": "acme", "user_id": "user-042"},
    },
    {
        "id": "vpn_high_risk",
        "title": "高风险请求（生产数据泄露 / 群体影响）",
        "description": "高风险敏感请求或群体影响场景；期望不被误放行为自动建议，而是升级/转人工（must_handoff=True）。",
        "ticket": {
            "ticket_id": "T-777",
            "requester_id": "user-042",
            "title": "VPN 生产环境数据泄露处理",
            "description": "用户请求通过 VPN 删除生产数据以修复泄露，涉及多人影响，需立即处理。",
            "asset_id": "asset-001",
            "status": "CLASSIFIED",
        },
        "overview": {
            "intake": {"fault": "multi_user_impact"},
            "messages": [
                {"direction": "in", "actor_id": "user-042", "content": "VPN 生产环境疑似泄露，请求删除数据"},
            ],
        },
        "expected_gate": {
            "must_handoff": "must_escalate",  # 高风险/群体：必须升级转人工（不应自动建议）
            "require_evidence": False,
        },
        "actor": {"tenant_id": "acme", "user_id": "user-042"},
    },
]


# ---------------------------------------------------------------------------
# gate 注入检查（确定性、不调真实模型）：验证禁止命令被拒 & 四类转人工判定
# ---------------------------------------------------------------------------
def gate_checks() -> dict[str, Any]:
    results: dict[str, Any] = {}

    # 1) 禁止命令在 DiagnosisCommand 层被拒：这些命令均非 DiagnosisCommandType 的合法值，
    #    因此字段级校验即拒绝（pydantic 枚举拒绝），模型永远无法让它们成为合法 command。
    #    这印证 FORBIDDEN_COMMANDS（账号/权限/网关/关单/代发消息）在结构层就不可构造。
    forbidden_rejected = 0
    for cmd_value in sorted(FORBIDDEN_COMMANDS):
        try:
            DiagnosisCommand(command=DiagnosisCommandType(cmd_value), content="x", reason_codes=[], confidence=0.9)
        except Exception:  # noqa: BLE001
            forbidden_rejected += 1
    results["forbidden_commands_rejected"] = {
        "count": forbidden_rejected,
        "total": len(FORBIDDEN_COMMANDS),
        "status": "rejected" if forbidden_rejected == len(FORBIDDEN_COMMANDS) else "leaked",
    }

    # 2) 合法命令可正常构造（对照，证明门禁只拦禁止/非法值）
    try:
        DiagnosisCommand(command=DiagnosisCommandType("provide_steps"), content="x", reason_codes=[], confidence=0.9)
        results["legal_command_accepted"] = {"status": "accepted"}
    except Exception:  # noqa: BLE001
        results["legal_command_accepted"] = {"status": "rejected"}

    # 2b) 非法 command（非枚举值）也应被拒
    try:
        DiagnosisCommand(
            command=DiagnosisCommandType("send_customer_message"), content="x", reason_codes=[], confidence=0.9
        )
        results["illegal_command_rejected"] = {"status": "leaked"}
    except Exception:  # noqa: BLE001
        results["illegal_command_rejected"] = {"status": "rejected"}

    # 3) 四类转人工判定（确定性纯函数）
    def _e(fault: str, identity: bool, evidence: Any, conf: float) -> dict[str, Any]:
        ev = evaluate_handoff(evidence=evidence, fault=fault, identity_ok=identity, confidence=conf)
        return ev.model_dump(mode="json")

    results["handoff_multi_user"] = _e("multi_user_impact", True, [{"found": True}], 0.95)
    results["handoff_no_evidence"] = _e("connection_failed", True, [], 0.95)
    results["handoff_identity_missing"] = _e("connection_failed", False, [{"found": True}], 0.95)
    results["handoff_low_confidence"] = _e("connection_failed", True, [{"found": True}], 0.5)
    results["handoff_ok"] = _e("connection_failed", True, [{"found": True}], 0.95)
    return results


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def _cost_usd(input_tokens: int, output_tokens: int, input_price: float, output_price: float) -> float:
    return (input_tokens / 1000.0) * input_price + (output_tokens / 1000.0) * output_price


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _token_usage(calls: list[dict[str, Any]]) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for c in calls:
        usage = c.get("usage") or {}
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    return input_tokens, output_tokens


async def run_scenario(
    svc: VpnDiagnosisService,
    adapter: MockVpnAdapter,
    recording: UsageRecordingModel,
    scenario: dict[str, Any],
    input_price: float,
    output_price: float,
) -> dict[str, Any]:
    ticket = scenario["ticket"]
    overview = scenario["overview"]
    actor = scenario["actor"]
    runtime = _make_runtime(adapter, ticket, overview)
    run_context = _make_run_context(actor["tenant_id"], actor["user_id"])

    calls_before = len(recording.calls)  # 快照，用于只统计本次场景新增的模型调用
    start = time.perf_counter()
    try:
        out = await svc.run_with_context(
            runtime=runtime,
            tenant_id=actor["tenant_id"],
            ticket_id=ticket["ticket_id"],
            run_context=run_context,
        )
        total_latency_ms = (time.perf_counter() - start) * 1000
    except Exception as exc:  # noqa: BLE001
        return {
            "id": scenario["id"],
            "title": scenario["title"],
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        }

    result = out.get("result") or {}

    command = result.get("command")
    evaluation = result.get("evaluation") or {}
    tool_evidence = result.get("tool_evidence") or []
    tool_trace = result.get("tool_trace") or []

    # 只统计本次场景（start index 之后）新增的真实模型调用
    model_calls = recording.calls[calls_before:]
    input_tokens, output_tokens = _token_usage(model_calls)

    return {
        "id": scenario["id"],
        "title": scenario["title"],
        "ok": True,
        "latency_ms": round(total_latency_ms, 1),
        "model_calls": len(model_calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(_cost_usd(input_tokens, output_tokens, input_price, output_price), 6),
        "must_handoff": bool(result.get("must_handoff")),
        "error_code": result.get("error_code"),
        "command": command,
        "evaluation": evaluation,
        "tool_evidence": tool_evidence,
        "tool_trace": tool_trace,
        "expected_gate": scenario["expected_gate"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="VPN Diagnosis Agent 受控真实模型 E2E")
    parser.add_argument("--limit", type=int, default=3, help="最多跑的受控场景数（默认 3，属受控小样本）")
    parser.add_argument("--mock-side-effects", action="store_true", default=True,
                        help="保持只读 Mock 数据源、零副作用（默认开启；本脚本恒不执行副作用动作）")
    parser.add_argument("--max-rounds", type=int, default=3, help="有界轮次上限（默认 3）")
    parser.add_argument("--max-tool-calls", type=int, default=8, help="有界总工具调用上限（默认 8）")
    parser.add_argument("--tool-calls-per-round", type=int, default=2,
                        help="单轮工具调用上限（默认 2，契约值）。真实模型常单轮请求>=3 个工具，"
                             "需放宽方能走通完整链路；默认值下会触发 tool_call_limit_exceeded")
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip()
    model_name = os.getenv("LLM_MODEL", "deepseek-chat").strip()
    input_price = float(os.getenv("MODEL_INPUT_COST_PER_1K_USD", "0") or 0)
    output_price = float(os.getenv("MODEL_OUTPUT_COST_PER_1K_USD", "0") or 0)
    price_configured = input_price > 0 or output_price > 0

    print("=" * 78)
    print("VPN Diagnosis Agent — 受控真实模型 E2E（非全量回归）")
    print("=" * 78)
    print(f"模型: {model_name} @ {base_url}")
    print(f"key: {'已配置(长度 ' + str(len(api_key)) + ')' if api_key else '未配置'}")
    print(f"单价: input={input_price} output={output_price} /1K USD  ->  {'已配置' if price_configured else '未配置(unrated)'}")
    print(f"--limit={args.limit}, --mock-side-effects={args.mock_side_effects}, "
          f"有界限制: rounds={args.max_rounds}, max_tool_calls={args.max_tool_calls}, "
          f"per_round={args.tool_calls_per_round}")
    print("-" * 78)

    if not api_key:
        print("[BLOCKER] 未发现 DEEPSEEK_API_KEY：真实模型不可用。")
        return 2

    # gate 注入检查（确定性）
    print(">>> Gate 注入检查（确定性，不调真实模型）")
    gates = gate_checks()
    print(json.dumps(gates, ensure_ascii=False, indent=2, default=str))
    print("-" * 78)

    # 装配：真实模型 + 只读 Mock 数据源
    try:
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        adapter = MockVpnAdapter()
        vpn_tools = {t.name: t for t in VPN_TOOLS}
        real_model = ChatOpenAI(api_key=SecretStr(api_key), base_url=base_url, model=model_name, temperature=0)
        bound = real_model.bind_tools(list(vpn_tools.values()))
        recording = UsageRecordingModel(bound)
        agent = VpnDiagnosisAgent(
            model=recording,
            tools=vpn_tools,
            limits=DiagnosisLimits(
                max_rounds=args.max_rounds,
                max_tool_calls=args.max_tool_calls,
                max_tool_calls_per_round=args.tool_calls_per_round,
            ),
        )
        svc = VpnDiagnosisService(agent)
    except Exception as exc:  # noqa: BLE001
        print(f"[BLOCKER] 装配真实模型失败: {type(exc).__name__}: {exc}")
        return 2

    scenarios = SCENARIOS[: args.limit]
    print(f">>> 开始受控真实模型诊断（共 {len(scenarios)} 个代表性场景）")
    print("-" * 78)

    results: list[dict[str, Any]] = []
    for s in scenarios:
        print(f"\n>>> 场景: {s['id']} — {s['title']}")
        r = await run_scenario(svc, adapter, recording, s, input_price, output_price)
        results.append(r)

        if not r.get("ok"):
            print(f"  [失败] {r.get('error')}")
            continue

        command = r.get("command")
        print(f"  耗时={r['latency_ms']}ms  模型调用={r['model_calls']}  "
              f"token=in{r['input_tokens']}/out{r['output_tokens']}  "
              f"成本=${r['cost_usd']}" + ("" if price_configured else " (unrated)"))
        print(f"  must_handoff={r['must_handoff']}  error_code={r['error_code']}")
        if command:
            print(f"  命令: {command.get('command')}")
            print(f"    content   : {command.get('content')}")
            print(f"    reason    : {command.get('reason_codes')}")
            print(f"    confidence: {command.get('confidence')}")
            print(f"    payload   : {command.get('payload')}")
        else:
            print("  命令: <无（不得产出副作用命令）>")
        ev = r.get("evaluation") or {}
        print(f"  升级判定: must_handoff={ev.get('must_handoff')}  reasons={ev.get('handoff_reasons')}")
        print(f"  证据数(tool_evidence)={len(r.get('tool_evidence') or [])}")
        print(f"  工具轨迹(tool_trace)={json.dumps(r.get('tool_trace'), ensure_ascii=False)}")
        print(f"  预期gate: {r.get('expected_gate')}")

    # 汇总指标
    print("\n" + "=" * 78)
    ok_results = [r for r in results if r.get("ok")]
    if not ok_results:
        print("[BLOCKER] 所有受控场景均失败：真实模型不可用或链路异常。")
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 3

    latencies = [r["latency_ms"] for r in ok_results]
    input_tokens_total = sum(r["input_tokens"] for r in ok_results)
    output_tokens_total = sum(r["output_tokens"] for r in ok_results)
    cost_total = sum(r["cost_usd"] for r in ok_results)

    summary = {
        "mode": "controlled_real_model_e2e",
        "model": model_name,
        "base_url": base_url,
        "scenario_count": len(ok_results),
        "limit": args.limit,
        "mock_side_effects": bool(args.mock_side_effects),
        "p95_latency_ms": round(_percentile(latencies, 95), 1),
        "p50_latency_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "max_latency_ms": round(max(latencies), 1),
        "latency_ms_list": latencies,
        "total_input_tokens": input_tokens_total,
        "total_output_tokens": output_tokens_total,
        "total_tokens": input_tokens_total + output_tokens_total,
        "cost_usd_total": round(cost_total, 6),
        "cost_per_ticket_usd": round(cost_total / len(ok_results), 6) if ok_results else 0.0,
        "price_configured": price_configured,
        "cost_rating": "unrated" if not price_configured else "rated",
        "gate_checks": gates,
    }

    print("\n>>> 汇总指标")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print("\n>>> 逐场景明细")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    # 同时写出机器可读结果，便于生成报告
    out_path = os.path.join(_ROOT, "artifacts", "vpn-e2e-controlled.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\n写入机器可读结果: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
