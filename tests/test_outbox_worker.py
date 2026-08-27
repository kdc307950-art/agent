import asyncio
from datetime import datetime, timezone

from backend.outbox_worker import OutboxWorker, TransientDeliveryError


class FakeRepository:
    def __init__(self, events):
        self.events = events
        self.completed = []
        self.failed = []

    async def claim_outbox(self, *, limit, tenant_id=None):
        return [event for event in self.events if tenant_id is None or event["tenant_id"] == tenant_id][:limit]

    async def complete_outbox(self, tenant_id, event_id):
        self.completed.append((tenant_id, event_id))
        return True

    async def fail_outbox(self, tenant_id, event_id, *, error_code, retry_at):
        self.failed.append((tenant_id, event_id, error_code, retry_at))
        return True


class Sender:
    def __init__(self, error=None):
        self.error = error
        self.events = []

    async def send(self, event):
        self.events.append(event)
        if self.error:
            raise self.error


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

    result = asyncio.run(worker.run_once(now=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert result.claimed == 2
    assert result.delivered == 1
    assert result.dead == 1
    assert repository.completed == [("tenant-a", "ok")]
    assert repository.failed[0][1:3] == ("unknown", "unsupported_event_type")


def test_transient_failure_retries_with_backoff_then_becomes_dead():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry_repository = FakeRepository([event("retry", attempts=2)])
    dead_repository = FakeRepository([event("dead", attempts=5)])
    sender = Sender(TransientDeliveryError("temporary"))

    retried = asyncio.run(
        OutboxWorker(retry_repository, {"ticket_message.send": sender}, max_attempts=5).run_once(now=now)
    )
    dead = asyncio.run(
        OutboxWorker(dead_repository, {"ticket_message.send": sender}, max_attempts=5).run_once(now=now)
    )

    assert retried.retried == 1
    assert retry_repository.failed[0][3] > now
    assert dead.dead == 1
    assert dead_repository.failed[0][3] is None


def test_permanent_sender_failure_is_not_retried():
    repository = FakeRepository([event("bad")])
    sender = Sender(ValueError("invalid payload"))

    result = asyncio.run(
        OutboxWorker(repository, {"ticket_message.send": sender}).run_once(
            now=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
    )

    assert result.dead == 1
    assert repository.failed[0][2] == "ValueError"
    assert repository.failed[0][3] is None


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
