"""Copilot Worker 进程入口 —— 以独立进程/容器运行常驻 CopilotWorker。

职责：
    - 装配运行时（Postgres/Redis/模型/治理/CopilotService）
    - 把 SIGINT/SIGTERM 转为 stop_event，优雅停止常驻循环
    - 启动 CopilotWorker 轮询 copilot_runs 并执行生成（Web 进程不调模型）

关键设计：
    - 依赖 DEEPSEEK_API_KEY：未配置时拒绝启动（Worker 无模型则无意义）
    - 心跳通过 worker_metrics 上报（/ready 判定 Worker 存活）
    - 优雅退出时未完成任务随租约过期由 recover_orphaned_runs 恢复
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from .config import load_environment
from .copilot.worker import CopilotWorker
from .runtime import runtime_context
from .settings import Settings
from .worker_metrics import WorkerMetricsDB


async def run_worker(args: argparse.Namespace) -> None:
    """装配运行时后启动 CopilotWorker 常驻循环。

    参数：args 为 argparse 解析出的命令行参数（poll_interval / batch_size /
    max_attempts / lease_seconds）。
    """
    load_environment()
    settings = Settings.from_env()
    if not settings.deepseek_api_key:
        raise RuntimeError("Copilot Worker 需要 DEEPSEEK_API_KEY")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signal_name = getattr(signal, name, None)
        if signal_name is not None:
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows 等不支持信号处理器的循环：忽略

    async with runtime_context(settings) as runtime:
        worker_metrics = WorkerMetricsDB(runtime.audit.pool)
        worker = CopilotWorker(
            runtime=runtime,
            max_attempts=args.max_attempts,
            lease_seconds=args.lease_seconds,
            worker_metrics=worker_metrics,
        )
        await worker.run_forever(
            poll_interval_seconds=args.poll_interval,
            limit=args.batch_size,
            stop_event=stop_event,
        )


def main() -> None:
    """命令行入口：解析参数并以 asyncio.run 启动 run_worker。"""
    parser = argparse.ArgumentParser(description="运行 Copilot 常驻 Worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--lease-seconds", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
