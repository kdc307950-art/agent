import asyncio
from types import SimpleNamespace

from backend.run_context import RunContext
from backend.tool_governance import ToolGovernance, ToolPolicy


class FakeAudit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, **kwargs):
        self.events.append((context.run_id, event_type, kwargs))


def make_request(name, args, context):
    return SimpleNamespace(
        tool_call={"name": name, "args": args, "id": "call-1", "type": "tool_call"},
        tool=object(),
        runtime=SimpleNamespace(context=context),
    )


def make_context(*, scopes=frozenset({"chat:write"}), deadline_seconds=5):
    return RunContext(
        run_id="run-1",
        request_id="request-1",
        tenant_id="tenant-a",
        user_id="user-1",
        thread_id="tenant-a:user-1:thread-1",
        scopes=scopes,
        deadline=asyncio.get_running_loop().time() + deadline_seconds,
    )


def test_tool_success_is_audited():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(audit)
        context = make_context()
        request = make_request("calculate", {"expression": "2+2"}, context)

        async def execute(_request):
            return "计算结果: 4"

        result = await governance.awrap_tool_call(request, execute)
        return result, audit.events

    result, events = asyncio.run(run())
    assert result == "计算结果: 4"
    assert [event[1] for event in events] == ["tool_call_started", "tool_call_completed"]


def test_tool_without_scope_is_rejected_and_audited():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(audit)
        context = make_context(scopes=frozenset())
        request = make_request("calculate", {"expression": "2+2"}, context)
        executed = False

        async def execute(_request):
            nonlocal executed
            executed = True
            return "should not run"

        result = await governance.awrap_tool_call(request, execute)
        return result, executed, audit.events

    result, executed, events = asyncio.run(run())
    assert result.status == "error"
    assert "权限" in result.content
    assert executed is False
    assert events[0][1] == "tool_call_denied"


def test_tool_input_limit_is_enforced():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(audit)
        context = make_context()
        request = make_request("calculate", {"expression": "1" * 600}, context)

        async def execute(_request):
            raise AssertionError("oversized input must not execute")

        return await governance.awrap_tool_call(request, execute)

    result = asyncio.run(run())
    assert result.status == "error"
    assert "长度" in result.content


def test_tenant_allowlist_rejects_tools_not_enabled_for_tenant():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(
            audit,
            tenant_allowlist={"tenant-a": frozenset({"get_weather"})},
        )
        context = make_context()
        request = make_request("calculate", {"expression": "2+2"}, context)

        async def execute(_request):
            raise AssertionError("tenant denied tool must not execute")

        return await governance.awrap_tool_call(request, execute)

    result = asyncio.run(run())
    assert result.status == "error"
    assert "租户" in result.content


def test_tool_timeout_returns_standard_error():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(
            audit,
            policies={
                "slow": ToolPolicy(
                    name="slow",
                    required_scopes=frozenset({"chat:write"}),
                    timeout_seconds=0.01,
                    max_input_chars=100,
                    retryable=False,
                    side_effect=False,
                )
            },
        )
        context = make_context()
        request = make_request("slow", {}, context)

        async def execute(_request):
            await asyncio.sleep(0.05)
            return "late"

        return await governance.awrap_tool_call(request, execute), audit.events

    result, events = asyncio.run(run())
    assert result.status == "error"
    assert "超时" in result.content
    assert events[-1][2]["status"] == "timeout"


def test_side_effect_tool_is_never_retried():
    async def run():
        audit = FakeAudit()
        governance = ToolGovernance(
            audit,
            policies={
                "charge": ToolPolicy(
                    name="charge",
                    required_scopes=frozenset({"chat:write"}),
                    timeout_seconds=1,
                    max_input_chars=100,
                    retryable=True,
                    side_effect=True,
                )
            },
            max_retry_attempts=3,
        )
        context = make_context()
        request = make_request("charge", {}, context)
        calls = 0

        async def execute(_request):
            nonlocal calls
            calls += 1
            raise OSError("temporary")

        result = await governance.awrap_tool_call(request, execute)
        return result, calls, audit.events

    result, calls, events = asyncio.run(run())
    assert result.status == "error"
    assert calls == 1
    assert not any(event[1] == "tool_call_retry" for event in events)
