"""Run the resident Outbox worker as a separate process/container."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from .config import load_environment
from .outbox_worker import HttpOutboxSender, OutboxWorker
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB


async def run_worker(args: argparse.Namespace) -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("缺少 DATABASE_URL")
    shared_secret = os.getenv("OUTBOX_SHARED_SECRET", "").strip()
    if len(shared_secret) < 16:
        raise RuntimeError("缺少至少 16 字符的 OUTBOX_SHARED_SECRET")
    senders = {}
    for event_type, env_name in {
        "ticket_message.send": "OUTBOX_TICKET_MESSAGE_ENDPOINT",
        "survey.send": "OUTBOX_SURVEY_ENDPOINT",
        "sla.breached": "OUTBOX_SLA_ENDPOINT",
    }.items():
        endpoint = os.getenv(env_name, "").strip()
        if endpoint:
            senders[event_type] = HttpOutboxSender(endpoint, shared_secret=shared_secret)
    if not senders:
        raise RuntimeError("至少配置一个 OUTBOX_*_ENDPOINT")

    repository = await TicketOperationsRepository.connect(database_url)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signal_name = getattr(signal, name, None)
        if signal_name is not None:
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
    worker = OutboxWorker(
        repository,
        senders,
        max_attempts=args.max_attempts,
        worker_metrics=WorkerMetricsDB(repository.pool),
    )
    try:
        await worker.run_forever(
            poll_interval_seconds=args.poll_interval,
            limit=args.batch_size,
            stop_event=stop_event,
        )
    finally:
        await repository.close()


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser(description="运行客服 Outbox 常驻 Worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
