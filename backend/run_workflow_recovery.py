"""工作流恢复 Worker 进程入口 —— 以独立进程/容器运行 WorkflowRecoveryWorker。

职责：
    - 加载环境变量，连接数据库（TicketRepository）
    - 把 SIGINT/SIGTERM 转为 stop_event，优雅停止常驻循环
    - 按 --interval 周期扫描并重放超过宽限期的未完成工作流操作

关键设计：
    - 进程级隔离：恢复逻辑独立于 Web 服务，故障互不影响
    - 宽限期可调：--grace 控制「操作未完成多久才视为可恢复」，避免与
      正常在线提交竞争同一批操作
    - 优雅停机：信号只置位 stop_event，Worker 在扫描间隙退出；
      未完成的操作会在下个周期被重新扫描，不丢恢复任务
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from .config import load_environment
from .tickets import TicketRepository
from .worker_metrics import WorkerMetricsDB
from .workflow_recovery import WorkflowRecoveryWorker


async def run_worker(args: argparse.Namespace) -> None:
    """装配数据库仓库后，启动 WorkflowRecoveryWorker 常驻循环。

    参数：args 为 argparse 解析出的命令行参数（interval / grace）。
    设计：先校验 DATABASE_URL，注册信号处理器；循环退出后 finally
    关闭连接池。
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("缺少 DATABASE_URL")
    repository = await TicketRepository.connect(database_url)
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
    try:
        await WorkflowRecoveryWorker(
            repository,
            interval_seconds=args.interval,
            grace_seconds=args.grace,
            worker_metrics=WorkerMetricsDB(repository.pool),
        ).run_forever(stop_event=stop_event)
    finally:
        # 无论正常退出还是异常，都关闭数据库连接池。
        await repository.close()


def main() -> None:
    """命令行入口：加载环境、解析参数、以 asyncio.run 启动 run_worker。"""
    load_environment()
    parser = argparse.ArgumentParser(description="运行工单工作流恢复 Worker")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--grace", type=int, default=30)
    asyncio.run(run_worker(parser.parse_args()))


if __name__ == "__main__":
    main()
