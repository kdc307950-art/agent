from __future__ import annotations

import hashlib


class RedisRevocationStore:
    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def _key(jti: str) -> str:
        digest = hashlib.sha256(jti.encode("utf-8")).hexdigest()
        return f"oidc:revoked:{digest}"

    async def is_revoked(self, jti: str) -> bool:
        return bool(await self.client.exists(self._key(jti)))

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            raise ValueError("撤销 TTL 必须为正数")
        await self.client.set(self._key(jti), "1", ex=ttl_seconds)
