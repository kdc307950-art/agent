"""Backend 包初始化 —— Windows 平台兼容处理。

psycopg 异步连接在 Windows 上要求 Selector 事件循环（见 uvicorn_loop.py），
这里在包导入时做一次兜底：若当前是 Windows 且主线程用的是
ProactorEventLoop，则替换为 SelectorEventLoop。
"""

import asyncio
import sys


if sys.platform == "win32":
    # psycopg async connections require selector-based event loops on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
