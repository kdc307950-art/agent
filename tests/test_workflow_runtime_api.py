"""编排图接入生产 runtime 的回归测试。

覆盖两类风险：
1. HTTP 层：interrupt 事件下发、/chat/resume 的授权与幂等校验、租户隔离
2. 静默失效：编排图节点名与单 Agent 不同，用量计费与工具治理不能因此失灵
"""

import asyncio
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import Command, Interrupt
from typing_extensions import Annotated, TypedDict

from backend.security import make_tenant_token

SECRET = "test-tenant-secret"
INTERRUPT_ID = "abc123def456"


# ========== 假运行时 ==========
class RecordingAudit:
    """记录审计调用，用来断言「计费/审批留痕真的发生了」。"""

    def __init__(self):
        self.started: list[dict] = []
        self.finished: list[tuple[str, dict]] = []
        self.events: list[tuple[str, str, dict]] = []

    async def start_run(self, ctx, metadata=None):
        self.started.append(dict(metadata or {}))

    async def finish_run(self, ctx, status, *, error_code=None, metadata=None):
        self.finished.append((status, dict(metadata or {})))
        return True

    async def record_event(self, ctx, name, *, status=None, payload=None, **_kwargs):
        self.events.append((name, status or "", dict(payload or {})))

    async def get_run(self, *_args, **_kwargs):
        return None

    async def list_events(self, *_args, **_kwargs):
        return []

    def event_names(self) -> list[str]:
        return [name for name, _status, _payload in self.events]


class ScriptedGraph:
    """按脚本产出 astream 事件的假图。

    真图要连 Postgres 和 DeepSeek，这里只关心 backend 如何解释事件流，
    所以把「图产出什么」固定下来，专测 app.py 的处理逻辑。
    """

    def __init__(self, events, *, pending=()):
        self._events = list(events)
        self._pending = tuple(pending)
        self.stream_inputs: list[object] = []
        self.state_configs: list[dict] = []

    async def astream(self, inputs, **_kwargs):
        self.stream_inputs.append(inputs)
        for event in self._events:
            yield event

    async def aget_state(self, config):
        self.state_configs.append(config)
        task = SimpleNamespace(interrupts=self._pending)
        return SimpleNamespace(tasks=(task,) if self._pending else ())


def load_app(monkeypatch, *, graph, audit=None):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("RATE_LIMIT_CAPACITY", "60")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "60")
    module = importlib.reload(importlib.import_module("backend.app"))

    runtime = SimpleNamespace(graph=graph, audit=audit or RecordingAudit())

    @asynccontextmanager
    async def fake_runtime_context(_settings, **_kwargs):
        yield runtime

    module.runtime_context = fake_runtime_context
    return module


def auth_headers(tenant="tenant-a", user="user-1", scopes=("chat:read", "chat:write", "chat:approve")):
    return {"Authorization": "Bearer " + make_tenant_token(tenant, user, SECRET, scopes=tuple(scopes))}


def sse_payloads(response) -> list[dict]:
    import json

    return [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def interrupt_event():
    return {"__interrupt__": (Interrupt(value={"question": "是否批准调用天气服务？"}, id=INTERRUPT_ID),)}


# ========== interrupt 下发 ==========
def test_interrupt_is_streamed_and_end_is_withheld(monkeypatch):
    """挂起时下发 interrupt 事件，且不发 end —— 前端靠 end 的缺席区分「答完」与「等批」。"""
    audit = RecordingAudit()
    module = load_app(monkeypatch, graph=ScriptedGraph([interrupt_event()]), audit=audit)
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", headers=auth_headers(), json={"message": "北京天气"})

    assert response.status_code == 200
    payloads = sse_payloads(response)
    interrupts = [p for p in payloads if p["type"] == "interrupt"]
    assert len(interrupts) == 1
    assert interrupts[0]["interrupt_id"] == INTERRUPT_ID
    assert interrupts[0]["question"] == "是否批准调用天气服务？"
    assert interrupts[0]["thread_id"] == "user_web_001"  # 逻辑 thread_id，不含租户信息
    assert not [p for p in payloads if p["type"] == "end"]
    assert "interrupt_raised" in audit.event_names()
    assert audit.finished[-1][0] == "awaiting_approval"


def test_interrupt_does_not_leak_physical_thread_id(monkeypatch):
    module = load_app(monkeypatch, graph=ScriptedGraph([interrupt_event()]))
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", headers=auth_headers(), json={"message": "北京天气"})

    assert "tenant-a" not in response.text
    assert "user-1" not in response.text


# ========== 陷阱 A：编排图节点名不同，计费不能静默失效 ==========
def test_usage_is_counted_for_orchestration_node_names(monkeypatch):
    """编排图节点叫 weather_agent 而不是 agent，用量审计仍必须触发。"""
    audit = RecordingAudit()
    msg = AIMessage(
        content="北京晴，25 度。",
        usage_metadata={"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
    )
    graph = ScriptedGraph([{"weather_agent": {"messages": [msg]}}])
    module = load_app(monkeypatch, graph=graph, audit=audit)
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", headers=auth_headers(), json={"message": "北京天气"})

    assert response.status_code == 200
    usage_events = [p for name, _s, p in audit.events if name == "model_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["total_tokens"] == 42
    status, metadata = audit.finished[-1]
    assert status == "completed"
    assert metadata["total_tokens"] == 42
    assert [p["content"] for p in sse_payloads(response) if p["type"] == "text"] == ["北京晴，25 度。"]


def test_tool_message_from_any_node_emits_tool_done(monkeypatch):
    """工具完成事件按消息类型识别，不再依赖节点是否叫 tools。"""
    graph = ScriptedGraph([
        {"calc_agent": {"messages": [ToolMessage(content="42", tool_call_id="c1")]}},
    ])
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", headers=auth_headers(), json={"message": "算一下"})

    tool_events = [p for p in sse_payloads(response) if p["type"] == "tool"]
    assert tool_events == [{"type": "tool", "status": "done"}]


def test_single_agent_shape_still_works(monkeypatch):
    """单 Agent 图（节点名 agent/tools）行为不得回退。"""
    msg = AIMessage(
        content="你好",
        usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    )
    graph = ScriptedGraph([{"agent": {"messages": [msg]}}])
    audit = RecordingAudit()
    module = load_app(monkeypatch, graph=graph, audit=audit)
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", headers=auth_headers(), json={"message": "hi"})

    payloads = sse_payloads(response)
    assert [p["content"] for p in payloads if p["type"] == "text"] == ["你好"]
    assert payloads[-1]["type"] == "end"
    assert [p["total_tokens"] for name, _s, p in audit.events if name == "model_usage"] == [8]


# ========== /chat/resume ==========
def test_resume_requires_approve_scope(monkeypatch):
    graph = ScriptedGraph([], pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),))
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/resume",
            headers=auth_headers(scopes=("chat:read", "chat:write")),
            json={"approved": True},
        )

    assert response.status_code == 403
    assert not graph.stream_inputs  # 授权失败不得触达图


def test_resume_approve_passes_command(monkeypatch):
    audit = RecordingAudit()
    graph = ScriptedGraph(
        [{"weather_agent": {"messages": [AIMessage(content="北京晴")]}}],
        pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),),
    )
    module = load_app(monkeypatch, graph=graph, audit=audit)
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/resume",
            headers=auth_headers(),
            json={"approved": True, "interrupt_id": INTERRUPT_ID},
        )

    assert response.status_code == 200
    assert isinstance(graph.stream_inputs[0], Command)
    assert graph.stream_inputs[0].resume == {"approved": True}
    metadata = audit.started[-1]
    assert metadata["approved"] is True
    assert metadata["interrupt_id"] == INTERRUPT_ID
    assert metadata["approver_user_id"] == "user-1"


def test_resume_reject_passes_command(monkeypatch):
    graph = ScriptedGraph(
        [{"approval": {"messages": [AIMessage(content="[已拒绝] 用户取消了 weather 的操作。")]}}],
        pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),),
    )
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post("/chat/resume", headers=auth_headers(), json={"approved": False})

    assert response.status_code == 200
    assert graph.stream_inputs[0].resume == {"approved": False}
    assert "[已拒绝]" in response.text


def test_resume_without_pending_interrupt_returns_409(monkeypatch):
    graph = ScriptedGraph([], pending=())
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post("/chat/resume", headers=auth_headers(), json={"approved": True})

    assert response.status_code == 409
    assert not graph.stream_inputs


def test_resume_with_stale_interrupt_id_returns_409(monkeypatch):
    """防重复审批：拿着过期的 interrupt_id 回来必须失败。"""
    graph = ScriptedGraph([], pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),))
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/resume",
            headers=auth_headers(),
            json={"approved": True, "interrupt_id": "staleid00000"},
        )

    assert response.status_code == 409
    assert not graph.stream_inputs


def test_resume_uses_tenant_scoped_thread(monkeypatch):
    """跨租户审批：读的是自己命名空间下的 thread，拿不到别人的挂起状态。"""
    from backend.repositories import tenant_thread_id

    graph = ScriptedGraph([], pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),))
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        client.post(
            "/chat/resume",
            headers=auth_headers(tenant="tenant-b", user="user-9"),
            json={"approved": True, "thread_id": "shared_thread"},
        )

    expected = tenant_thread_id("tenant-b", "user-9", "shared_thread")
    assert graph.state_configs[0]["configurable"]["thread_id"] == expected
    assert expected != tenant_thread_id("tenant-a", "user-1", "shared_thread")


def test_resume_rejects_unknown_fields(monkeypatch):
    graph = ScriptedGraph([], pending=(Interrupt(value={"question": "?"}, id=INTERRUPT_ID),))
    module = load_app(monkeypatch, graph=graph)
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/resume",
            headers=auth_headers(),
            json={"approved": True, "tenant_id": "tenant-victim"},
        )

    assert response.status_code == 422


# ========== 陷阱 B：编排图的工具调用必须经过治理钩子 ==========
@tool
def echo_tool(text: str) -> str:
    """回显输入文本（测试专用）。"""
    return text


class ScriptedModel(FakeMessagesListChatModel):
    """按脚本回复的假模型；create_react_agent 要求真 Runnable，所以继承官方 fake。"""

    def bind_tools(self, _tools, **_kwargs):
        return self


def _tool_call_message():
    return AIMessage(
        content="",
        tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call-1", "type": "tool_call"}],
    )


def _run_tool_node(node):
    """把工具节点塞进最小图里执行 —— ToolNode 需要 LangGraph runtime 才能运行。"""

    class State(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    graph = StateGraph(State)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return asyncio.run(graph.compile().ainvoke({"messages": [_tool_call_message()]}))


def _recording_wrapper(seen: list[str]):
    async def wrapper(request, execute):
        seen.append(request.tool_call["name"])
        return await execute(request)

    return wrapper


def test_tool_node_is_wrapped_by_governance():
    """tool 节点的工具调用必须过 awrap_tool_call，否则租户白名单/超时全部形同虚设。"""
    from src.my_agent.workflow.nodes import BuildContext, tool_factory
    from src.my_agent.workflow.schema import NodeSpec

    seen: list[str] = []
    ctx = BuildContext(
        model=None,
        tools={"echo_tool": echo_tool},
        tool_call_wrapper=_recording_wrapper(seen),
    )
    node = tool_factory(NodeSpec(id="t", type="tool", config={"tools": ["echo_tool"]}), ctx)
    result = _run_tool_node(node)

    assert seen == ["echo_tool"]
    assert result["messages"][-1].content == "hi"


def test_agent_node_is_wrapped_by_governance():
    """子 Agent 内部的工具调用同样要过治理钩子。"""
    from src.my_agent.workflow.nodes import BuildContext, agent_factory
    from src.my_agent.workflow.schema import NodeSpec

    seen: list[str] = []
    model = ScriptedModel(responses=[_tool_call_message(), AIMessage(content="回显完成")])
    ctx = BuildContext(
        model=model,
        tools={"echo_tool": echo_tool},
        tool_call_wrapper=_recording_wrapper(seen),
    )
    agent = agent_factory(
        NodeSpec(id="a", type="agent", config={"name": "echo_agent", "tools": ["echo_tool"]}),
        ctx,
    )
    out = asyncio.run(agent.ainvoke({"messages": [("user", "回显 hi")]}))

    assert seen == ["echo_tool"]
    assert out["messages"][-1].content == "回显完成"


def test_missing_wrapper_still_works():
    """未注入治理钩子时（如单测/CLI demo）不应报错，保持向后兼容。"""
    from src.my_agent.workflow.nodes import BuildContext, tool_factory
    from src.my_agent.workflow.schema import NodeSpec

    ctx = BuildContext(model=None, tools={"echo_tool": echo_tool})
    node = tool_factory(NodeSpec(id="t", type="tool", config={"tools": ["echo_tool"]}), ctx)
    result = _run_tool_node(node)

    assert result["messages"][-1].content == "hi"


# ========== 加载层 ==========
def test_workflow_loader_requires_path(tmp_path):
    from backend.workflow_loader import WorkflowSpecUnavailable, load_workflow_spec

    with pytest.raises(WorkflowSpecUnavailable):
        load_workflow_spec(SimpleNamespace(agent_workflow_path=None))
    with pytest.raises(WorkflowSpecUnavailable):
        load_workflow_spec(SimpleNamespace(agent_workflow_path=str(tmp_path / "missing.json")))


def test_workflow_loader_rejects_bad_json(tmp_path):
    from backend.workflow_loader import WorkflowSpecUnavailable, load_workflow_spec

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkflowSpecUnavailable):
        load_workflow_spec(SimpleNamespace(agent_workflow_path=str(bad)))


def test_workflow_loader_reads_shipped_spec():
    from pathlib import Path

    from backend.workflow_loader import load_workflow_spec

    root = Path(__file__).resolve().parents[1]
    spec = load_workflow_spec(
        SimpleNamespace(agent_workflow_path=str(root / "workflows" / "helpdesk_supervisor.json"))
    )
    assert spec["name"]
    assert any(node["type"] == "human_approval" for node in spec["nodes"])
