import asyncio
from dataclasses import dataclass
import pytest

from backend import backup_restore
from backend.budget import TenantBudget
from backend.readiness import probe_dependencies
from backend.schema import check_schema_ready
from backend.usage import extract_model_usage, usage_cost_usd


@dataclass
class FakeMessage:
    usage_metadata: object = None
    response_metadata: object = None


def test_usage_normalizes_provider_metadata_and_costs() -> None:
    message = FakeMessage(
        usage_metadata={"input_tokens": 12, "output_tokens": 8},
        response_metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 9, "total_tokens": 21}},
    )

    usage = extract_model_usage(message)

    assert usage.input_tokens == 12
    assert usage.output_tokens == 9
    assert usage.total_tokens == 21
    assert usage_cost_usd(usage, input_per_1k=0.5, output_per_1k=1.0) == 0.015


def test_usage_rejects_invalid_or_negative_values() -> None:
    usage = extract_model_usage(
        FakeMessage(response_metadata={"usage": {"input_tokens": -3, "output_tokens": "bad"}})
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_tokens == 0
    assert not usage.known


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, script, _num_keys, key, *arguments):
        self.calls.append((script, key, *arguments))
        current = self.values.get(key, 0)
        if len(arguments) == 1:
            return [int(current < int(arguments[0])), current]
        amount, limit, _expires_at = (int(argument) for argument in arguments)
        if current + amount > limit:
            return [0, current]
        self.values[key] = current + amount
        return [1, self.values[key]]


def test_tenant_budget_is_shared_per_tenant_and_enforces_limit() -> None:
    async def run() -> None:
        redis = FakeRedis()
        budget = TenantBudget(redis, daily_limit_usd=1.0)

        assert await budget.can_start("tenant-a")
        assert await budget.record("tenant-a", 0.6)
        assert await budget.record("tenant-a", 0.4)
        assert not await budget.can_start("tenant-a")
        assert not await budget.record("tenant-a", 0.01)
        assert await budget.can_start("tenant-b")

    asyncio.run(run())


class FakeVerifier:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready

    async def check_ready(self) -> None:
        if not self.ready:
            raise RuntimeError("jwks unavailable")


class FakeApp:
    def __init__(self, settings, *, agent=True, redis_client=None, verifier=None) -> None:
        self.state = type(
            "State",
            (),
            {
                "settings": settings,
                "agent": object() if agent else None,
                "redis_client": redis_client,
                "auth_verifier": verifier,
            },
        )()


class FakeRequest:
    def __init__(self, app) -> None:
        self.app = app


def test_readiness_requires_redis_and_oidc_when_configured(monkeypatch) -> None:
    async def postgres_ok(*_args):
        return "ok"

    async def redis_ok(*_args):
        return "ok"

    monkeypatch.setattr("backend.readiness._probe_postgres", postgres_ok)
    monkeypatch.setattr("backend.readiness._probe_redis", redis_ok)
    settings = type(
        "Settings",
        (),
        {
            "database_url": "postgresql://unused",
            "redis_socket_timeout_seconds": 1,
            "rate_limit_backend": "redis",
            "auth_mode": "oidc",
            "oidc_revocation_mode": "redis",
        },
    )()

    result = asyncio.run(probe_dependencies(FakeRequest(FakeApp(settings, verifier=FakeVerifier(ready=True)))))

    assert result.ok
    assert result.checks == {"agent": "ok", "postgres": "ok", "redis": "ok", "oidc": "ok"}


def test_readiness_fails_when_a_required_dependency_fails(monkeypatch) -> None:
    async def postgres_failed(*_args):
        return "failed"

    monkeypatch.setattr("backend.readiness._probe_postgres", postgres_failed)
    settings = type(
        "Settings",
        (),
        {
            "database_url": "postgresql://unused",
            "redis_socket_timeout_seconds": 1,
            "rate_limit_backend": "memory",
            "auth_mode": "dev",
            "oidc_revocation_mode": "none",
        },
    )()

    result = asyncio.run(probe_dependencies(FakeRequest(FakeApp(settings))))

    assert not result.ok
    assert result.checks == {"agent": "ok", "postgres": "failed"}


def test_backup_and_restore_build_safe_client_commands(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(backup_restore, "_run", lambda command: commands.append(command))
    archive = backup_restore.Path("agent.backup")

    backup_restore.create_backup("postgresql://backup-user@db/agent", archive)
    backup_restore.restore_backup(archive, "postgresql://restore-user@db/agent")

    assert commands == [
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(archive), "postgresql://backup-user@db/agent"],
        [
            "pg_restore",
            "--no-owner",
            "--exit-on-error",
            "--clean",
            "--if-exists",
            "--dbname",
            "postgresql://restore-user@db/agent",
            str(archive),
        ],
    ]


def test_schema_readiness_rejects_missing_or_outdated_schema(monkeypatch) -> None:
    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args):
            return None

        async def fetchone(self):
            return (999,)

    class Connection:
        def cursor(self):
            return Cursor()

    async def no_missing_relations(*_args):
        return []

    monkeypatch.setattr("backend.schema.check_required_relations", no_missing_relations)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="版本不匹配"):
            await check_schema_ready(Connection())

    asyncio.run(run())
