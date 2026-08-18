import asyncio
import os
from uuid import uuid4

import pytest
import redis.asyncio as redis

from backend.rate_limit import RedisRateLimiter


REDIS_URL = os.getenv("REDIS_URL", "").strip()
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="REDIS_URL is not configured")


def test_redis_rate_limiter_is_shared_and_tenant_scoped():
    async def run():
        client = redis.from_url(REDIS_URL, decode_responses=True)
        limiter = RedisRateLimiter(client, capacity=2, refill_per_second=0.001)
        route = f"/probe/{uuid4().hex}"
        try:
            assert await limiter.check("tenant-a:user-1", route) is None
            assert await limiter.check("tenant-a:user-1", route) is None
            assert (await limiter.check("tenant-a:user-1", route)) >= 1
            assert await limiter.check("tenant-b:user-1", route) is None
        finally:
            await client.aclose()

    asyncio.run(run())
