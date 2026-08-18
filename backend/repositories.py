from __future__ import annotations

import re
from typing import Any


_SAFE_PART = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def tenant_namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    """Build a stable namespace that prevents accidental global memory access."""
    if not _SAFE_PART.fullmatch(tenant_id) or not _SAFE_PART.fullmatch(user_id):
        raise ValueError("租户或用户标识包含非法字符")
    return ("memory", "v1", tenant_id, user_id)


class LongTermMemoryRepository:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def put(self, tenant_id: str, user_id: str, key: str, value: dict[str, Any]) -> None:
        namespace = tenant_namespace(tenant_id, user_id)
        if not _SAFE_PART.fullmatch(key):
            raise ValueError("记忆 key 包含非法字符")
        await self.store.aput(namespace, key, value)

    async def get(self, tenant_id: str, user_id: str, key: str):
        namespace = tenant_namespace(tenant_id, user_id)
        if not _SAFE_PART.fullmatch(key):
            raise ValueError("记忆 key 包含非法字符")
        return await self.store.aget(namespace, key)
