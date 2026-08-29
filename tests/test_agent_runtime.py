import asyncio
from types import SimpleNamespace

import pytest

from backend.audit import AuditRepository
from backend.migrate_sqlite import migrate_checkpoint_tuples
from backend.repositories import LongTermMemoryRepository, tenant_namespace, tenant_thread_id
from backend.run_context import RunContext
from src.my_agent.agent import _ainvoke_with_retry


def test_finish_run_accepts_awaiting_approval_before_database_call():
    class FailingPool:
        def connection(self):
            raise RuntimeError("database call reached")

    repository = AuditRepository(FailingPool())
    context = RunContext(
        run_id="run-validation",
        request_id="request-validation",
        tenant_id="tenant-a",
        user_id="user-1",
        thread_id="tenant-a:user-1:thread-1",
        scopes=frozenset({"chat:write"}),
        deadline=60,
    )

    async def run():
        with pytest.raises(RuntimeError, match="database call reached"):
            await repository.finish_run(context, "awaiting_approval")

    asyncio.run(run())


def test_retry_retries_transient_model_failure():
    class TransientError(Exception):
        status_code = 503

    class FakeModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, _messages):
            self.calls += 1
            if self.calls < 3:
                raise TransientError("temporary")
            return "ok"

    model = FakeModel()
    result = asyncio.run(_ainvoke_with_retry(model, [], max_retries=2))

    assert result == "ok"
    assert model.calls == 3


def test_retry_does_not_retry_non_transient_failure():
    class FakeModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, _messages):
            self.calls += 1
            raise ValueError("invalid request")

    model = FakeModel()
    with pytest.raises(ValueError):
        asyncio.run(_ainvoke_with_retry(model, [], max_retries=2))

    assert model.calls == 1


def test_memory_namespace_is_tenant_scoped():
    assert tenant_namespace("tenant-a", "user-1") == (
        "memory",
        "v1",
        "tenant-a",
        "user-1",
    )
    with pytest.raises(ValueError):
        tenant_namespace("tenant/a", "user-1")


def test_checkpoint_thread_id_is_owned_by_tenant_and_user():
    assert tenant_thread_id("tenant-a", "user-1", "web-1") == "tenant-a:user-1:web-1"
    with pytest.raises(ValueError):
        tenant_thread_id("tenant-a", "user-1", "other:user")


def test_memory_repository_uses_scoped_namespace():
    class FakeStore:
        def __init__(self):
            self.calls = []

        async def aput(self, namespace, key, value):
            self.calls.append((namespace, key, value))

    store = FakeStore()
    repository = LongTermMemoryRepository(store)
    asyncio.run(repository.put("tenant-a", "user-1", "preferences", {"language": "zh"}))

    assert store.calls == [
        (("memory", "v1", "tenant-a", "user-1"), "preferences", {"language": "zh"})
    ]


def test_checkpoint_migration_preserves_order_and_pending_writes():
    oldest = SimpleNamespace(
        config={"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}},
        parent_config=None,
        checkpoint={"id": "checkpoint-1", "channel_versions": {"messages": "1"}},
        metadata={"step": 1},
        pending_writes=[],
    )
    newest = SimpleNamespace(
        config={"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}},
        parent_config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-1",
            }
        },
        checkpoint={"id": "checkpoint-2", "channel_versions": {"messages": "2"}},
        metadata={"step": 2},
        pending_writes=[("task-1", "messages", "value")],
    )

    class Source:
        def list(self, _config):
            return iter([newest, oldest])

    class Target:
        def __init__(self):
            self.checkpoints = []
            self.writes = []

        async def aput(self, config, checkpoint, metadata, versions):
            self.checkpoints.append((config, checkpoint, metadata, versions))
            return {"configurable": {"checkpoint_id": checkpoint["id"]}}

        async def aput_writes(self, config, writes, task_id):
            self.writes.append((config, writes, task_id))

    target = Target()
    migrated = asyncio.run(migrate_checkpoint_tuples(Source(), target))

    assert migrated == 2
    assert [entry[1]["id"] for entry in target.checkpoints] == [
        "checkpoint-1",
        "checkpoint-2",
    ]
    assert target.writes == [
        ({"configurable": {"checkpoint_id": "checkpoint-2"}}, [("messages", "value")], "task-1")
    ]


def test_checkpoint_migration_can_scope_legacy_thread_ids():
    item = SimpleNamespace(
        config={"configurable": {"thread_id": "legacy", "checkpoint_ns": ""}},
        parent_config=None,
        checkpoint={"id": "checkpoint-1", "channel_versions": {}},
        metadata={},
        pending_writes=[],
    )

    class Source:
        def list(self, _config):
            return iter([item])

    class Target:
        async def aput(self, config, checkpoint, metadata, versions):
            self.config = config
            return config

        async def aput_writes(self, *_args):
            raise AssertionError("unexpected writes")

    target = Target()
    asyncio.run(
        migrate_checkpoint_tuples(Source(), target, lambda value: f"tenant-a:user-1:{value}")
    )
    assert target.config["configurable"]["thread_id"] == "tenant-a:user-1:legacy"
