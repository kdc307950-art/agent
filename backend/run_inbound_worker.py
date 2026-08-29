"""渠道入站 Worker 进程入口 —— 以独立进程/容器运行常驻 InboundWorker。

职责：
    - 加载环境变量、装配 JSON 日志与运行时上下文（runtime_context）
    - 把 SIGINT/SIGTERM 转为 stop_event，优雅停止常驻循环
    - 启动 InboundWorker 轮询入站事件（inbound_events 表，带租约领取）

关键设计：
    - 进程级隔离：与 Web 服务进程分离，互不抢占资源、互不影响故障
    - 优雅停机：信号处理器只置位 stop_event，Worker 在轮询间隙退出，
      已领取未完成的事件会因租约过期被其它实例重新领取，不丢事件
    - 参数化：轮询间隔 / 批大小 / 最大尝试 / 退避基数 / 租约时长均可调

Usage:
    uv run python -m backend.run_inbound_worker --poll-interval 1 --batch-size 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from .config import load_environment
from .inbound_worker import InboundWorker
from .logging_config import setup_json_logging
from .runtime import runtime_context
from .settings import Settings
from .worker_metrics import WorkerMetricsDB


async def run_worker(args: argparse.Namespace) -> None:
    """装配环境与运行时后，启动 InboundWorker 常驻循环。

    参数：args 为 argparse 解析出的命令行参数（poll_interval / batch_size /
    max_attempts / backoff_base / lease_seconds）。
    设计：先校验 DATABASE_URL 存在，再注册信号处理器，最后在
    runtime_context 上下文中运行；退出时由上下文自动清理资源。
    """
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
                # 把终止信号转成 stop_event 置位，让常驻循环优雅退出。
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows 等不支持信号处理器的事件循环：忽略，退化为强制退出。
                pass
    async with runtime_context(settings) as runtime:
        worker = InboundWorker(
            runtime,
            max_attempts=args.max_attempts,
            backoff_base_seconds=args.backoff_base,
            lease_seconds=args.lease_seconds,
            batch_size=args.batch_size,
            worker_metrics=WorkerMetricsDB(runtime.tickets.pool),
        )
        await worker.run_forever(poll_interval_seconds=args.poll_interval, stop_event=stop_event)


def main() -> None:
    """命令行入口：加载环境、解析参数、以 asyncio.run 启动 run_worker。"""
    load_environment()
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
