from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from time import monotonic
from typing import Deque


_TOKEN_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + (tonumber(t[2]) / 1000000)
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last = tonumber(redis.call('HGET', KEYS[1], 'last'))
if tokens == nil then tokens = capacity end
if last == nil then last = now end
tokens = math.min(capacity, tokens + ((now - last) * refill))
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / refill)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last', now)
redis.call('EXPIRE', KEYS[1], math.ceil((capacity / refill) * 2))
return {allowed, retry, math.floor(tokens)}
"""


class InMemoryRateLimiter:
    def __init__(self, capacity: int, window_seconds: int = 60) -> None:
        if capacity < 1 or window_seconds < 1:
            raise ValueError("限流参数必须为正数")
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._requests: dict[str, Deque[float]] = defaultdict(deque)

    async def check(self, principal_key: str, route: str) -> int | None:
        key = f"{principal_key}:{route}"
        now = monotonic()
        entries = self._requests[key]
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= self.capacity:
            return max(1, int(entries[0] + self.window_seconds - now + 0.999))
        entries.append(now)
        return None


class RedisRateLimiter:
    def __init__(self, client, *, capacity: int, refill_per_second: float, key_prefix: str = "rl:v1") -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("Redis 限流参数无效")
        self.client = client
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.key_prefix = key_prefix

    @staticmethod
    def _safe_part(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    async def check(self, principal_key: str, route: str) -> int | None:
        key = f"{self.key_prefix}:{self._safe_part(principal_key)}:{self._safe_part(route)}"
        result = await self.client.eval(
            _TOKEN_BUCKET_SCRIPT,
            1,
            key,
            self.capacity,
            self.refill_per_second,
        )
        allowed, retry_after, _remaining = (int(item) for item in result)
        return None if allowed else max(1, retry_after)
