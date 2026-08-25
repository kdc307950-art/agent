"""按调用方限流的两种实现。

`InMemoryRateLimiter` 只适用于单进程开发环境——多副本部署时各进程各算各的，
限流额度会随副本数成倍放大，等于没限。生产必须走 `RedisRateLimiter`，
由 `RATE_LIMIT_BACKEND` 选择，且 Redis 不可用时默认 fail-closed
（宁可拒绝请求，也不要在限流失效的状态下继续放量）。

**两者算法并不等价**：内存版是滑动窗口计数，Redis 版是令牌桶。
令牌桶允许攒额度后突发，滑动窗口不允许。所以本地压测的限流表现
不能直接外推到生产，要验证限流行为必须连真实 Redis。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from time import monotonic
from typing import Deque


# 令牌桶。整段在 Redis 里原子执行——「读令牌数 → 计算补充 → 写回」若拆成
# 多次往返，并发请求会同时读到同一个旧值，各自认为还有令牌。
#
# 时间取自 Redis 的 TIME 命令而不是应用传入：多个应用副本的系统时钟存在偏差，
# 用各自的本地时间算补充量会让同一个桶忽快忽慢地回填。
# EXPIRE 设为「桶从空到满所需秒数」的两倍，闲置的 key 自动回收，
# 同时保证一个还在被正常使用的桶不会中途被删掉（删掉等于白送一整桶令牌）。
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
    """进程内滑动窗口限流，仅用于单进程开发环境。"""

    def __init__(self, capacity: int, window_seconds: int = 60) -> None:
        if capacity < 1 or window_seconds < 1:
            raise ValueError("限流参数必须为正数")
        self.capacity = capacity
        self.window_seconds = window_seconds
        # key -> 该 key 在窗口内的请求时刻队列。进程重启即清空。
        self._requests: dict[str, Deque[float]] = defaultdict(deque)

    async def check(self, principal_key: str, route: str) -> int | None:
        """允许则返回 None；超限则返回建议的 Retry-After 秒数。"""
        key = f"{principal_key}:{route}"
        # 用 monotonic 而非 wall clock：系统时间被 NTP 校正或手动改动时，
        # wall clock 可能回拨，导致窗口计算出负值或永久卡死。
        now = monotonic()
        entries = self._requests[key]
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= self.capacity:
            # 最早的那次请求滑出窗口时才有新配额，向上取整避免返回 0。
            return max(1, int(entries[0] + self.window_seconds - now + 0.999))
        entries.append(now)
        return None


class RedisRateLimiter:
    """基于 Redis 令牌桶的共享限流，多副本之间计数一致。key 前缀带版本号，
    改动桶语义时递增前缀即可让旧 key 自然过期，不必手工清库。"""

    def __init__(self, client, *, capacity: int, refill_per_second: float, key_prefix: str = "rl:v1") -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("Redis 限流参数无效")
        self.client = client
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.key_prefix = key_prefix

    @staticmethod
    def _safe_part(value: str) -> str:
        """把租户/用户标识和路由摘要化后再拼进 key。

        既避免明文身份出现在 Redis key、慢查询日志和监控面板里，
        也顺带消除了标识中的冒号把 key 结构撑破的可能。
        """
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    async def check(self, principal_key: str, route: str) -> int | None:
        """允许则返回 None；超限则返回建议的 Retry-After 秒数。"""
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
