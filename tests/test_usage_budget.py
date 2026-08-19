import asyncio

from backend.budget import TenantBudget
from backend.usage import extract_model_usage, usage_cost_usd


def test_usage_normalization_and_cost():
    class Message:
        usage_metadata = {"input_tokens": 120, "output_tokens": 80}
        response_metadata = {}

    usage = extract_model_usage(Message())
    assert usage.input_tokens == 120
    assert usage.output_tokens == 80
    assert usage.total_tokens == 200
    assert usage_cost_usd(usage, input_per_1k=0.01, output_per_1k=0.02) == 0.0028


def test_tenant_budget_uses_shared_atomic_redis_scripts():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, *_args):
            self.calls.append(script)
            if "next_value" in script:
                return [1, 100]
            return [1, 0]

    client = FakeRedis()
    budget = TenantBudget(client, daily_limit_usd=1)

    async def run():
        assert await budget.can_start("tenant-a")
        assert await budget.record("tenant-a", 0.0001)

    asyncio.run(run())
    assert len(client.calls) == 2
