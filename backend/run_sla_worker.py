"""SLA 扫描 Worker 进程入口 —— 以独立进程/容器运行常驻 SLAWorker。

职责：
    - 加载环境变量，连接数据库（TicketOperationsRepository）
    - 把 SIGINT/SIGTERM 转为 stop_event，优雅停止常驻循环
    - 按 --interval 周期执行 SLA 违约扫描，产出 sla.breached Outbox 事件

关键设计：
    - 进程级隔离：扫描频率（默认 30 秒）与 Web 服务解耦，扫描抖动
      不影响在线 API 延迟
    - 优雅停机：信号只置位 stop_event，Worker 在扫描间隙退出；
      重复扫描由数据库幂等约束兜底，不会重复建单
    - 连接池生命周期：run_worker 内创建、finally 关闭
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from .config import load_environment
from .sla_worker import SLAWorker
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB


async def run_worker(args: argparse.Namespace) -> None:
    """装配数据库仓库后，启动 SLAWorker 常驻循环。

    参数：args 为 argparse 解析出的命令行参数（interval / batch_size）。
    设计：先校验 DATABASE_URL，注册信号处理器；循环退出后 finally
    关闭连接池。
    """
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
                # 把终止信号转成 stop_event 置位，让常驻循环优雅退出。
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows 等不支持信号处理器的事件循环：忽略，退化为强制退出。
                pass
    try:
        await SLAWorker(
            repository,
            interval_seconds=args.interval,
            batch_size=args.batch_size,
            worker_metrics=WorkerMetricsDB(repository.pool),
        ).run_forever(stop_event=stop_event)
    finally:
        # 无论正常退出还是异常，都关闭数据库连接池。
        await repository.close()


def main() -> None:
    """命令行入口：加载环境、解析参数、以 asyncio.run 启动 run_worker。"""
    load_environment()
    parser = argparse.ArgumentParser(description="运行客服 SLA 常驻 Worker")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--batch-size", type=int, default=100)
    asyncio.run(run_worker(parser.parse_args()))


if __name__ == "__main__":
    main()
