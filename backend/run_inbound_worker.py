"""Run the resident Inbound worker as a separate process/container.

Usage:
    uv run python -m backend.run_inbound_worker --poll-interval 1 --batch-size 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from dotenv import load_dotenv

from .inbound_worker import InboundWorker
from .logging_config import setup_json_logging
from .runtime import runtime_context
from .settings import Settings


async def run_worker(args: argparse.Namespace) -> None:
    if not os.getenv("DATABASE_URL", "").strip():
        raise RuntimeError("缺少 DATABASE_URL")
    setup_json_logging()
    settings = Settings.from_env()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signal_name = getattr(signal, name, None)
        if signal_name is not None:
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
    async with runtime_context(settings) as runtime:
        worker = InboundWorker(
            runtime,
            max_attempts=args.max_attempts,
            backoff_base_seconds=args.backoff_base,
            lease_seconds=args.lease_seconds,
            batch_size=args.batch_size,
        )
        await worker.run_forever(poll_interval_seconds=args.poll_interval, stop_event=stop_event)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="运行渠道入站事件常驻 Worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--backoff-base", type=float, default=30.0)
    parser.add_argument("--lease-seconds", type=int, default=120)
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
