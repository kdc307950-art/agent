"""租户日预算：以 Redis 为共享账本，按 UTC 自然日限制每个租户的模型花费。

放在 Redis 而不是进程内，是因为应用会多副本部署——各副本各算各的额度，
等于每多一个副本预算就翻一倍。

两段 Lua 脚本承担的是原子性：「读当前用量 → 判断 → 写回」如果拆成多次
Redis 往返，并发请求会同时读到未超限的旧值、同时通过检查，最终超支。
Lua 在 Redis 单线程里整段执行，不会被其他命令插入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib


class TenantBudgetExceeded(RuntimeError):
    """租户当日额度已耗尽。由调用方转换为对外的 budget_exceeded 结果。"""


# 准入检查：只读不写。用 >= 判断——已经花到上限就不再放行新的运行。
_CURRENT_USAGE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if current >= limit then return {0, current} end
return {1, current}
"""

# 事后记账：累加本次花费。用 > 判断——加上本次仍不超限才写回，
# 超限时保留旧值并返回失败，避免把已经溢出的数字固化到账本里。
# EXAT 设成当日 24:00，key 到点自动消失，不需要额外的清理任务。
_ADD_USAGE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local next_value = current + tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if next_value > limit then return {0, current} end
redis.call('SET', KEYS[1], tostring(next_value), 'EXAT', ARGV[3])
return {1, next_value}
"""


class TenantBudget:
    """按租户原子记账当日用量，单位是微美元（micro-USD）的整数。

    刻意不用浮点数：预算是长期累加的量，float 反复相加会累积舍入误差，
    而且 Redis 里存字符串化的 float 再解析回来同样有精度问题。
    统一乘以 1e6 转成整数，只在出入口做一次换算。

    **这是事后记账模型**：`can_start()` 只做准入，真实花费要等模型返回
    usage metadata 才知道。因此最后一次运行可能小幅超出额度——用一次调用的
    成本换「不预扣、不冻结」的简单实现。需要硬上限时应改为预扣 + 结算回补。
    """

    def __init__(self, client, *, daily_limit_usd: float, key_prefix: str = "budget:v1") -> None:
        if daily_limit_usd <= 0:
            raise ValueError("daily_limit_usd must be > 0")
        self.client = client
        self.daily_limit_micro_usd = int(round(daily_limit_usd * 1_000_000))
        self.key_prefix = key_prefix

    @staticmethod
    def _day_end() -> int:
        """当日 24:00（UTC）的 Unix 时间戳，用作 key 的绝对过期时刻。"""
        now = datetime.now(timezone.utc)
        end = datetime.combine((now + timedelta(days=1)).date(), datetime.min.time(), tzinfo=timezone.utc)
        return int(end.timestamp())

    def _key(self, tenant_id: str) -> str:
        """账本 key：前缀 + 租户 ID 摘要 + UTC 日期。

        租户 ID 取 sha256 前 32 位而不是明文：Redis 的 key 会出现在慢查询日志、
        `KEYS`/`SCAN` 结果和监控面板里，明文租户 ID 属于不该扩散的业务标识。
        日期写进 key 而非依赖 TTL 计算，保证跨日切换是干净的换 key，不是改计数。
        """
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]
        day = datetime.now(timezone.utc).date().isoformat()
        return f"{self.key_prefix}:{digest}:{day}"

    async def can_start(self, tenant_id: str) -> bool:
        """运行开始前的准入检查。True 表示尚有额度。"""
        result = await self.client.eval(_CURRENT_USAGE, 1, self._key(tenant_id), self.daily_limit_micro_usd)
        return bool(int(result[0]))

    async def record(self, tenant_id: str, cost_usd: float) -> bool:
        """运行结束后累加实际花费。False 表示这一笔使额度溢出、未被记入。"""
        amount = max(0, int(round(cost_usd * 1_000_000)))
        # 单价未配置时成本恒为 0，此时不该写 Redis：既省一次往返，
        # 也避免把「预算功能已启用」的假象写进账本。
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
