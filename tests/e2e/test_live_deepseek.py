from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

RUN_LIVE_E2E = os.getenv("RUN_LIVE_E2E", "false").strip().lower() == "true"
BASE_URL = os.getenv("LIVE_AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("LIVE_AGENT_TOKEN", "").strip()

pytestmark = [
    pytest.mark.live_e2e,
    pytest.mark.skipif(not RUN_LIVE_E2E, reason="RUN_LIVE_E2E=true is required"),
]


def _events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        events.append(json.loads(line[6:]))
    return events


def _headers() -> dict[str, str]:
    if not TOKEN:
        pytest.fail("LIVE_AGENT_TOKEN is required for live E2E")
    return {"Authorization": f"Bearer {TOKEN}"}


def test_live_text_stream_has_end_event():
    async def run():
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
            return await client.post(
                "/chat/stream",
                headers=_headers(),
                json={
                    "message": "用一句话回答：2+2 等于多少？",
                    "thread_id": f"live-{uuid4().hex}",
                },
            )

    response = asyncio.run(run())
    assert response.status_code == 200, response.text
    events = _events(response.text)
    assert events
    assert events[-1]["type"] == "end"
    assert any(event["type"] == "text" for event in events)
    assert not any(event.get("code") == "agent_failed" for event in events)


def test_live_tool_call_is_governed_and_streamed():
    thread_id = f"live-tool-{uuid4().hex}"

    async def run():
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
            return await client.post(
                "/chat/stream",
                headers=_headers(),
                json={
                    "message": "必须使用计算工具计算 2+2，只返回最终结果。",
                    "thread_id": thread_id,
                },
            )

    response = asyncio.run(run())
    assert response.status_code == 200, response.text
    events = _events(response.text)
    assert events[-1]["type"] == "end"
    assert any(event.get("type") == "tool" for event in events)


def test_live_thread_can_continue_after_first_run():
    thread_id = f"live-thread-{uuid4().hex}"

    async def run():
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
            first = await client.post(
                "/chat/stream",
                headers=_headers(),
                json={"message": "记住这个代号：BLUE-17。", "thread_id": thread_id},
            )
            second = await client.post(
                "/chat/stream",
                headers=_headers(),
                json={"message": "刚才的代号是什么？", "thread_id": thread_id},
            )
            return first, second

    first, second = asyncio.run(run())
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    second_events = _events(second.text)
    assert second_events[-1]["type"] == "end"
    text = "".join(
        event.get("content", "") for event in second_events if event.get("type") == "text"
    )
    assert "BLUE-17" in text
