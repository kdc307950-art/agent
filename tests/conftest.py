import asyncio
import os
import sys

import pytest
from dotenv import load_dotenv

load_dotenv()


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(config, items):
    if os.getenv("CI", "").lower() == "true" and not os.getenv("TEST_DATABASE_URL", "").strip():
        raise pytest.UsageError("CI 必须配置 TEST_DATABASE_URL，禁止静默跳过 PostgreSQL 集成测试")
    if os.getenv("CI", "").lower() == "true" and not os.getenv("REDIS_URL", "").strip():
        raise pytest.UsageError("CI 必须配置 REDIS_URL，禁止静默跳过 Redis 集成测试")
