"""Run the SLA scanner as a separate process/container."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from dotenv import load_dotenv

from .sla_worker import SLAWorker
from .tickets import TicketOperationsRepository


async def run_worker(args: argparse.Namespace) -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("缺少 DATABASE_URL")
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
    try:
        await SLAWorker(
            repository,
            interval_seconds=args.interval,
            batch_size=args.batch_size,
        ).run_forever(stop_event=stop_event)
    finally:
        await repository.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="运行客服 SLA 常驻 Worker")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--batch-size", type=int, default=100)
    asyncio.run(run_worker(parser.parse_args()))


if __name__ == "__main__":
    main()
