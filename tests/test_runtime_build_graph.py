"""Regression: build_graph must inject rag_service into workflow compilation."""

import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

from backend.runtime import build_graph


class FakeGovernance:
    async def awrap_tool_call(self, *_args, **_kwargs):
        raise AssertionError("workflow compile should not invoke tools")


def _settings(workflow_path):
    return SimpleNamespace(
        agent_graph_mode="workflow",
        agent_workflow_path=workflow_path,
        deepseek_api_key="test-key",
        llm_base_url="https://example.invalid",
        llm_model="test-model",
        model_retry_attempts=1,
    )


def test_build_graph_compiles_rag_workflow_with_injected_service(tmp_path):
    spec_path = tmp_path / "rag-workflow.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "rag-probe",
                "nodes": [{"id": "r", "type": "rag", "config": {"limit": 3}}],
                "edges": [{"source": "START", "target": "r"}],
            }
        ),
        encoding="utf-8",
    )
    graph = build_graph(
        _settings(str(spec_path)),
        checkpointer=MemorySaver(),
        store=MemorySaver(),
        tool_governance=FakeGovernance(),
        rag_service=SimpleNamespace(answer=None),
    )
    assert graph is not None
    names = {node for node in graph.get_graph().nodes}
    assert "r" in names


def test_build_graph_rag_workflow_without_service_still_fails_at_compile(tmp_path):
    spec_path = tmp_path / "rag-workflow.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "rag-probe",
                "nodes": [{"id": "r", "type": "rag", "config": {"limit": 3}}],
                "edges": [{"source": "START", "target": "r"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="rag_service"):
        build_graph(
            _settings(str(spec_path)),
            checkpointer=MemorySaver(),
            store=MemorySaver(),
            tool_governance=FakeGovernance(),
        )
