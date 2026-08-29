"""Outbox 投递 Worker 进程入口 —— 以独立进程/容器运行常驻 OutboxWorker。

职责：
    - 从环境变量读取数据库连接串与各事件类型的投递端点，装配 sender 路由
    - 把 SIGINT/SIGTERM 转为 stop_event，优雅停止常驻循环
    - 启动 OutboxWorker 轮询 outbox_events 并投递（ticket_message.send /
      survey.send / sla.breached 三类事件）

关键设计：
    - 路由表由环境变量驱动：OUTBOX_TICKET_MESSAGE_ENDPOINT /
      OUTBOX_SURVEY_ENDPOINT / OUTBOX_SLA_ENDPOINT 配置了哪个端点就注册
      哪个 sender；至少配置一个端点才启动
    - 共享密钥：OUTBOX_SHARED_SECRET（≥16 字符）用于 HTTP 投递签名，
      下游接收方据此验签防伪造
    - 租约互斥：多实例部署时依赖 OutboxWorker 的数据库租约保证同一事件
      只被一个实例投递；优雅退出时未完成事件随租约过期被重新领取
"""

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
    """装配仓库与 sender 路由后，启动 OutboxWorker 常驻循环。

    参数：args 为 argparse 解析出的命令行参数（poll_interval / batch_size /
    max_attempts）。
    设计：先校验 DATABASE_URL 与 OUTBOX_SHARED_SECRET，再按环境变量
    注册 HttpOutboxSender；循环退出后 finally 关闭数据库连接池。
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("缺少 DATABASE_URL")
    shared_secret = os.getenv("OUTBOX_SHARED_SECRET", "").strip()
    if len(shared_secret) < 16:
        # 密钥过短无法保证 HMAC 签名强度，直接拒绝启动。
        raise RuntimeError("缺少至少 16 字符的 OUTBOX_SHARED_SECRET")
    senders = {}
    # 事件类型 → 环境变量名映射：配置了端点才注册对应 sender。
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
                # 把终止信号转成 stop_event 置位，让常驻循环优雅退出。
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows 等不支持信号处理器的事件循环：忽略，退化为强制退出。
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
        # 无论正常退出还是异常，都关闭数据库连接池。
        await repository.close()


def main() -> None:
    """命令行入口：加载环境、解析参数、以 asyncio.run 启动 run_worker。"""
    load_environment()
    parser = argparse.ArgumentParser(description="运行客服 Outbox 常驻 Worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
