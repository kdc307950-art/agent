import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.security import OIDCVerifier
from backend.settings import Settings


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, _url):
        return FakeResponse(self.payload)

    async def aclose(self):
        return None


def test_oidc_verifier_checks_claims_and_revocation():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    jwks = {"keys": [{**json.loads(public_jwk), "kid": "key-1", "alg": "RS256", "use": "sig"}]}
    revoked = set()

    class Revocations:
        async def is_revoked(self, jti):
            return jti in revoked

    verifier = OIDCVerifier(
        issuer="https://issuer.example",
        audience="agent-api",
        jwks_url="https://issuer.example/keys",
        tenant_claim="tenant_id",
        clock_skew_seconds=0,
        required_scopes=frozenset({"chat:write"}),
        revocation_store=Revocations(),
    )
    verifier._client = FakeClient(jwks)

    token = jwt.encode(
        {
            "iss": "https://issuer.example",
            "aud": "agent-api",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "scope": "chat:write",
            "jti": "jti-1",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )

    async def run():
        principal = await verifier.verify(token)
        assert principal.tenant_id == "tenant-a"
        revoked.add("jti-1")
        with pytest.raises(ValueError, match="已撤销"):
            await verifier.verify(token)
        await verifier.aclose()

    asyncio.run(run())


def test_oidc_verifier_rejects_wrong_audience():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    verifier = OIDCVerifier(
        issuer="https://issuer.example",
        audience="agent-api",
        jwks_url="https://issuer.example/keys",
        tenant_claim="tenant_id",
        clock_skew_seconds=0,
        required_scopes=frozenset({"chat:write"}),
    )
    verifier._client = FakeClient({"keys": [{**json.loads(public_jwk), "kid": "key-1"}]})
    token = jwt.encode(
        {"iss": "https://issuer.example", "aud": "other-api", "sub": "user-1", "tenant_id": "tenant-a", "exp": int(time.time()) + 60},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )

    async def run():
        with pytest.raises(ValueError, match="校验失败"):
            await verifier.verify(token)
        await verifier.aclose()

    asyncio.run(run())


def test_oidc_verifier_rejects_expired_and_missing_scope():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    verifier = OIDCVerifier(
        issuer="https://issuer.example",
        audience="agent-api",
        jwks_url="https://issuer.example/keys",
        tenant_claim="tenant_id",
        clock_skew_seconds=0,
        required_scopes=frozenset({"chat:write"}),
    )
    verifier._client = FakeClient({"keys": [{**json.loads(public_jwk), "kid": "key-1"}]})

    def token(**extra):
        claims = {
            "iss": "https://issuer.example",
            "aud": "agent-api",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "scope": "chat:read",
            "exp": int(time.time()) + 60,
        }
        claims.update(extra)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})

    async def run():
        with pytest.raises(ValueError, match="校验失败"):
            await verifier.verify(token(exp=int(time.time()) - 1))
        with pytest.raises(PermissionError, match="scope"):
            await verifier.verify(token())
        await verifier.aclose()

    asyncio.run(run())


def test_oidc_verifier_can_require_jti_and_token_age():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    verifier = OIDCVerifier(
        issuer="https://issuer.example",
        audience="agent-api",
        jwks_url="https://issuer.example/keys",
        tenant_claim="tenant_id",
        clock_skew_seconds=0,
        required_scopes=frozenset({"chat:write"}),
        require_jti=True,
        max_token_age_seconds=60,
    )
    verifier._client = FakeClient({"keys": [{**json.loads(public_jwk), "kid": "key-1"}]})

    def token(**extra):
        claims = {
            "iss": "https://issuer.example",
            "aud": "agent-api",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "scope": "chat:write",
            "iat": int(time.time()),
            "jti": "jti-1",
            "exp": int(time.time()) + 60,
        }
        claims.update(extra)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})

    async def run():
        with pytest.raises(ValueError, match="jti"):
            await verifier.verify(token(jti=""))
        with pytest.raises(ValueError, match="年龄"):
            await verifier.verify(token(iat=int(time.time()) - 120, exp=int(time.time()) + 60))
        await verifier.aclose()

    asyncio.run(run())


def test_oidc_verifier_refreshes_jwks_for_rotated_kid():
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(old_key.public_key())), "kid": "old"}
    new_jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(new_key.public_key())), "kid": "new"}

    class RotatingClient:
        def __init__(self):
            self.calls = 0

        async def get(self, _url):
            self.calls += 1
            return FakeResponse({"keys": [old_jwk] if self.calls == 1 else [new_jwk]})

        async def aclose(self):
            return None

    verifier = OIDCVerifier(
        issuer="https://issuer.example",
        audience="agent-api",
        jwks_url="https://issuer.example/keys",
        tenant_claim="tenant_id",
        clock_skew_seconds=0,
        required_scopes=frozenset({"chat:write"}),
    )
    client = RotatingClient()
    verifier._client = client
    token = jwt.encode(
        {
            "iss": "https://issuer.example",
            "aud": "agent-api",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "scope": "chat:write",
            "exp": int(time.time()) + 60,
        },
        new_key,
        algorithm="RS256",
        headers={"kid": "new"},
    )

    async def run():
        principal = await verifier.verify(token)
        assert principal.tenant_id == "tenant-a"
        assert client.calls == 2
        await verifier.aclose()

    asyncio.run(run())


def test_production_rejects_dev_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", "dev-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    with pytest.raises(RuntimeError, match="禁止使用 AUTH_MODE=dev"):
        Settings.from_env()


def test_production_requires_jti_and_metrics_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://issuer.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "agent-api")
    monkeypatch.setenv("OIDC_REVOCATION_MODE", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example")
    monkeypatch.setenv("OIDC_REQUIRE_JTI", "false")
    monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="jti"):
        Settings.from_env()

    monkeypatch.setenv("OIDC_REQUIRE_JTI", "true")
    with pytest.raises(RuntimeError, match="METRICS_AUTH_TOKEN"):
        Settings.from_env()

    monkeypatch.setenv("METRICS_AUTH_TOKEN", "metrics-secret")
    settings = Settings.from_env()
    assert settings.oidc_require_jti is True
    assert settings.metrics_auth_token == "metrics-secret"
