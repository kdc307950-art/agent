import asyncio
from datetime import UTC, datetime

from backend.outbox_worker import OutboxWorker, TransientDeliveryError


class FakeRepository:
    def __init__(self, events):
        self.events = events
        self.completed = []
        self.failed = []

    async def claim_outbox(self, *, worker_id, lease_seconds, limit, tenant_id=None):
        return [
            event for event in self.events if tenant_id is None or event["tenant_id"] == tenant_id
        ][:limit]

    async def renew_outbox_lease(self, tenant_id, event_id, *, worker_id, lease_seconds):
        return True

    async def complete_outbox(self, tenant_id, event_id, *, worker_id):
        self.completed.append((tenant_id, event_id, worker_id))
        return True

    async def fail_outbox(self, tenant_id, event_id, *, worker_id, error_code, retry_at):
        self.failed.append((tenant_id, event_id, worker_id, error_code, retry_at))
        return True


class Sender:
    def __init__(self, error=None):
        self.error = error
        self.events = []

    async def send(self, event):
        self.events.append(event)
        if self.error:
            raise self.error


class LeaseLosingRepository(FakeRepository):
    async def renew_outbox_lease(self, tenant_id, event_id, *, worker_id, lease_seconds):
        return False


class TrackingRepository(FakeRepository):
    def __init__(self, events):
        super().__init__(events)
        self.renewed: list[str] = []

    async def renew_outbox_lease(self, tenant_id, event_id, *, worker_id, lease_seconds):
        self.renewed.append(event_id)
        return True


def event(event_id, event_type="ticket_message.send", attempts=1):
    return {
        "tenant_id": "tenant-a",
        "event_id": event_id,
        "event_type": event_type,
        "attempts": attempts,
        "payload": {"message": "hello"},
    }


def test_worker_delivers_success_and_marks_unknown_type_dead():
    repository = FakeRepository([event("ok"), event("unknown", "unknown.event")])
    sender = Sender()
    worker = OutboxWorker(repository, {"ticket_message.send": sender})

    result = asyncio.run(worker.run_once(now=datetime(2026, 1, 1, tzinfo=UTC)))

    assert result.claimed == 2
    assert result.delivered == 1
    assert result.dead == 1
    assert repository.completed[0][:2] == ("tenant-a", "ok")
    assert repository.failed[0][1] == "unknown"
    assert repository.failed[0][3] == "unsupported_event_type"


def test_transient_failure_retries_with_backoff_then_becomes_dead():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    retry_repository = FakeRepository([event("retry", attempts=2)])
    dead_repository = FakeRepository([event("dead", attempts=5)])
    sender = Sender(TransientDeliveryError("temporary"))

    retried = asyncio.run(
        OutboxWorker(retry_repository, {"ticket_message.send": sender}, max_attempts=5).run_once(
            now=now
        )
    )
    dead = asyncio.run(
        OutboxWorker(dead_repository, {"ticket_message.send": sender}, max_attempts=5).run_once(
            now=now
        )
    )

    assert retried.retried == 1
    assert retry_repository.failed[0][4] > now
    assert dead.dead == 1
    assert dead_repository.failed[0][4] is None


def test_permanent_sender_failure_is_not_retried():
    repository = FakeRepository([event("bad")])
    sender = Sender(ValueError("invalid payload"))

    result = asyncio.run(
        OutboxWorker(repository, {"ticket_message.send": sender}).run_once(
            now=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )

    assert result.dead == 1
    assert repository.failed[0][3] == "ValueError"
    assert repository.failed[0][4] is None


def test_worker_run_forever_stops_without_busy_wait():
    repository = FakeRepository([])
    worker = OutboxWorker(repository, {})
    stop_event = asyncio.Event()

    async def stop_after_first_poll():
        await asyncio.sleep(0.01)
        stop_event.set()

    async def run():
        await asyncio.gather(
            worker.run_forever(poll_interval_seconds=0.01, stop_event=stop_event),
            stop_after_first_poll(),
        )

    asyncio.run(run())


def test_worker_stops_sender_when_lease_is_lost():
    """租约丢失时取消发送协程，不调用 complete/fail 伪造终态。"""
    repository = LeaseLosingRepository([event("lease-lost")])
    started = asyncio.Event()

    class BlockingSender:
        async def send(self, payload):
            started.set()
            await asyncio.Event().wait()

    worker = OutboxWorker(
        repository,
        {"ticket_message.send": BlockingSender()},
        lease_seconds=1,
    )

    async def run():
        task = asyncio.create_task(worker.run_once(limit=1))
        await asyncio.wait_for(started.wait(), timeout=1)
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(run())
    assert result.claimed == 1
    assert result.lease_lost == 1
    assert result.delivered == 0
    assert repository.completed == []
    assert repository.failed == []


def test_batch_claim_starts_heartbeats_for_waiting_events():
    """前一事件阻塞时，后排已领取事件仍会被续租。"""
    repository = TrackingRepository([event("first"), event("second")])
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class OrderedSender:
        async def send(self, payload):
            if payload["event_id"] == "first":
                first_started.set()
                await release_first.wait()

    worker = OutboxWorker(
        repository,
        {"ticket_message.send": OrderedSender()},
        lease_seconds=1,
    )

    async def run():
        task = asyncio.create_task(worker.run_once(limit=2))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0.45)
        second_renewed_while_waiting = "second" in repository.renewed
        release_first.set()
        result = await asyncio.wait_for(task, timeout=2)
        return result, second_renewed_while_waiting

    result, second_renewed_while_waiting = asyncio.run(run())
    assert second_renewed_while_waiting is True
    assert result.delivered == 2
