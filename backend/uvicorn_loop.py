"""事件循环工厂 —— Windows 下 Uvicorn 用 Selector 事件循环。

Windows 默认的 ProactorEventLoop 与 psycopg async / subprocess 有不兼容问题，
此工厂在 uvicorn 启动参数 loop= 中传入，强制使用 SelectorEventLoop
（use_subprocess=True 时启用子进程支持版）。
"""

from __future__ import annotations

import asyncio


def selector_event_loop_factory(*, use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """Uvicorn loop factory required by psycopg async on Windows."""
    return asyncio.SelectorEventLoop()
