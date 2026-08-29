import asyncio
import os
import sys

import pytest

# 测试不加载工作区 .env：显式禁用入口的 dotenv 加载 + 由下方固定默认值提供变量，
# 保证「有无 .env 时结果一致」。setdefault 不覆盖显式注入/CI env。
os.environ.setdefault("SKIP_DOTENV_LOAD", "1")
_TEST_ENV_DEFAULTS = {
    "APP_ENV": "test",
    "AUTH_MODE": "dev",
    "TENANT_TOKEN_SECRET": "test-secret",
    "DEEPSEEK_API_KEY": "test-key",
    "LANGGRAPH_AUTO_SETUP": "false",
    # 企微渠道用固定测试凭据（43 字符 AES key），不依赖 .env 的真实企业配置。
    "WECOM_TENANT_ID": "test-tenant",
    "WECOM_TOKEN": "test-wecom-token",
    "WECOM_ENCODING_AES_KEY": "a" * 43,
    "WECOM_CORP_ID": "test-corp",
}
for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


class _FakeAnswerGenerator:
    """确定性 LLM 答案生成 stub：始终 abstained（无外部调用、无建议发送）。

    text 必须满足 GeneratedAnswer 的 min_length=1 约束，故用非空占位。
    """

    async def generate(self, question, contexts):
        from backend.knowledge.service import GeneratedAnswer

        return GeneratedAnswer(text="（stub abstained）", citations=(), abstained=True)


class _FakeRetrievalPlanner:
    """确定性检索规划 stub：不生成补充查询（签名对齐真实 LlmRetrievalPlanner）。"""

    async def next_queries(self, question, hits, round_number):
        return []


@pytest.fixture(autouse=True)
def _fake_external_llm(monkeypatch):
    """替换 runtime 装配的 LLM 服务为 stub：集成测试不得真实调用外部模型。

    仅替换 backend.runtime 模块里的绑定名；直接测试 knowledge/llm 的单元测试
    用自己的 mock，不受影响。
    """
    import backend.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module, "LlmAnswerGenerator", lambda *args, **kwargs: _FakeAnswerGenerator()
    )
    monkeypatch.setattr(
        runtime_module, "LlmRetrievalPlanner", lambda *args, **kwargs: _FakeRetrievalPlanner()
    )


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(config, items):
    if os.getenv("CI", "").lower() == "true" and not os.getenv("TEST_DATABASE_URL", "").strip():
        raise pytest.UsageError("CI 必须配置 TEST_DATABASE_URL，禁止静默跳过 PostgreSQL 集成测试")
    if os.getenv("CI", "").lower() == "true" and not os.getenv("REDIS_URL", "").strip():
        raise pytest.UsageError("CI 必须配置 REDIS_URL，禁止静默跳过 Redis 集成测试")
