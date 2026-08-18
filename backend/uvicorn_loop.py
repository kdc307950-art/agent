from __future__ import annotations

import asyncio


def selector_event_loop_factory(*, use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """Uvicorn loop factory required by psycopg async on Windows."""
    return asyncio.SelectorEventLoop()
