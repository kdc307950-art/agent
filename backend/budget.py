"""Shared Redis-backed tenant usage budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib


class TenantBudgetExceeded(RuntimeError):
    pass


_CURRENT_USAGE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if current >= limit then return {0, current} end
return {1, current}
"""

_ADD_USAGE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local next_value = current + tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if next_value > limit then return {0, current} end
redis.call('SET', KEYS[1], tostring(next_value), 'EXAT', ARGV[3])
return {1, next_value}
"""


class TenantBudget:
    """Atomically account daily micro-USD usage per tenant."""

    def __init__(self, client, *, daily_limit_usd: float, key_prefix: str = "budget:v1") -> None:
        if daily_limit_usd <= 0:
            raise ValueError("daily_limit_usd must be > 0")
        self.client = client
        self.daily_limit_micro_usd = int(round(daily_limit_usd * 1_000_000))
        self.key_prefix = key_prefix

    @staticmethod
    def _day_end() -> int:
        now = datetime.now(timezone.utc)
        end = datetime.combine((now + timedelta(days=1)).date(), datetime.min.time(), tzinfo=timezone.utc)
        return int(end.timestamp())

    def _key(self, tenant_id: str) -> str:
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]
        day = datetime.now(timezone.utc).date().isoformat()
        return f"{self.key_prefix}:{digest}:{day}"

    async def can_start(self, tenant_id: str) -> bool:
        result = await self.client.eval(_CURRENT_USAGE, 1, self._key(tenant_id), self.daily_limit_micro_usd)
        return bool(int(result[0]))

    async def record(self, tenant_id: str, cost_usd: float) -> bool:
        amount = max(0, int(round(cost_usd * 1_000_000)))
        if amount == 0:
            return True
        result = await self.client.eval(
            _ADD_USAGE,
            1,
            self._key(tenant_id),
            amount,
            self.daily_limit_micro_usd,
            self._day_end(),
        )
        return bool(int(result[0]))
