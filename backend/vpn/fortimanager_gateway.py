"""FortiManager JSON-RPC command gateway for approved VPN control-plane installs.

This module deliberately sits beside, rather than inside, ``HttpReadonlyVpnAdapter``:
FortiManager writes are asynchronous control-plane operations, while the existing adapter
is a read-only diagnostic data source.  A caller must supply an explicit tenant-to-target
mapping; a user id is never inferred to be a FortiGate device name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_INSTALL_KINDS = frozenset({"package", "device"})


class FortiManagerConfigurationError(ValueError):
    """The local mapping/configuration is incomplete or unsafe."""


class FortiManagerBusinessError(RuntimeError):
    """A JSON-RPC response was accepted by HTTP but rejected by FortiManager."""

    def __init__(self, code: int, message: str, url: str) -> None:
        super().__init__(f"FortiManager [{code}] {message} ({url})")
        self.code = code
        self.message = message
        self.url = url


class FortiManagerPreviewChangedError(FortiManagerBusinessError):
    """The live preview no longer matches the preview approved by a human."""


@dataclass(frozen=True)
class FortiManagerConfig:
    base_url: str
    api_token: str
    access_user: str | None = None
    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    poll_interval_seconds: float = 2.0
    max_wait_seconds: float = 600.0
    verify_tls: bool = True
    ca_bundle_path: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise FortiManagerConfigurationError("FortiManager base_url 必须使用 https")
        if not self.api_token:
            raise FortiManagerConfigurationError("FortiManager API token 不能为空")
        if (
            min(
                self.connect_timeout,
                self.read_timeout,
                self.poll_interval_seconds,
                self.max_wait_seconds,
            )
            <= 0
        ):
            raise FortiManagerConfigurationError("FortiManager 超时与轮询间隔必须大于 0")
        if self.ca_bundle_path and not os.path.isfile(self.ca_bundle_path):
            raise FortiManagerConfigurationError(
                f"FortiManager CA bundle 不存在: {self.ca_bundle_path}"
            )


@dataclass(frozen=True)
class FortiManagerTarget:
    """A tenant's explicitly approved FortiManager installation scope."""

    adom: str
    device: str
    vdom: str = "root"
    package: str | None = None
    install_kind: str = "package"

    def __post_init__(self) -> None:
        for label, value in (("adom", self.adom), ("device", self.device), ("vdom", self.vdom)):
            if not _IDENTIFIER.fullmatch(value):
                raise FortiManagerConfigurationError(f"FortiManager target.{label} 非法")
        if self.package is not None and not _IDENTIFIER.fullmatch(self.package):
            raise FortiManagerConfigurationError("FortiManager target.package 非法")
        if self.install_kind not in _INSTALL_KINDS:
            raise FortiManagerConfigurationError(
                "FortiManager target.install_kind 必须为 package 或 device"
            )
        if self.install_kind == "package" and not self.package:
            raise FortiManagerConfigurationError("package 下发必须配置 FortiManager target.package")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> FortiManagerTarget:
        return cls(
            adom=str(value.get("adom", "")),
            device=str(value.get("device", "")),
            vdom=str(value.get("vdom", "root")),
            package=(str(value["package"]) if value.get("package") else None),
            install_kind=str(value.get("install_kind", "package")),
        )


def targets_from_json(raw: str) -> dict[str, FortiManagerTarget]:
    """Parse ``VPN_FMG_TENANT_TARGETS_JSON`` without accepting implicit defaults."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FortiManagerConfigurationError(
            "VPN_FMG_TENANT_TARGETS_JSON 必须是 JSON 对象"
        ) from exc
    if not isinstance(decoded, dict) or not decoded:
        raise FortiManagerConfigurationError("VPN_FMG_TENANT_TARGETS_JSON 必须包含至少一个租户映射")
    targets: dict[str, FortiManagerTarget] = {}
    for tenant_id, target in decoded.items():
        if not isinstance(tenant_id, str) or not _IDENTIFIER.fullmatch(tenant_id):
            raise FortiManagerConfigurationError("VPN_FMG_TENANT_TARGETS_JSON 包含非法 tenant_id")
        if not isinstance(target, dict):
            raise FortiManagerConfigurationError(
                f"租户 {tenant_id} 的 FortiManager target 必须是对象"
            )
        targets[tenant_id] = FortiManagerTarget.from_mapping(target)
    return targets


class FortiManagerCommandGateway:
    """Submit and reconcile a FortiManager install task using JSON-RPC.

    ``idempotency_key`` remains a local safety anchor.  FortiManager does not deduplicate
    JSON-RPC requests by ``id``; after a task id is obtained we only query that task during
    reconciliation, never submit a second install for the same approved operation.
    """

    def __init__(
        self,
        config: FortiManagerConfig,
        targets: dict[str, FortiManagerTarget],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not targets:
            raise FortiManagerConfigurationError("FortiManager 至少需要一个租户 target")
        self._config = config
        self._targets = dict(targets)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.read_timeout,
                pool=config.connect_timeout,
            ),
            # httpx accepts a CA bundle path as its verify argument.  A configured
            # bundle takes precedence over the development-only boolean switch.
            verify=config.ca_bundle_path or config.verify_tls,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def reissue_config(
        self,
        *,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        target_version: str | None = None,
    ) -> dict[str, Any]:
        """Run the tenant's approved install target and wait for its task result.

        ``user_id`` and ``target_version`` are preserved only for the local audit contract;
        neither is transformed into a FortiManager resource path.
        """
        return await self.redeploy_tenant_vpn_config(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            user_id=user_id,
            target_version=target_version,
            enforce_preview=False,
        )

    async def redeploy_tenant_vpn_config(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        action: str = "redeploy_tenant_vpn_config",
        user_id: str | None = None,
        target_version: str | None = None,
        approved_diff_hash: str | None = None,
        enforce_preview: bool = True,
    ) -> dict[str, Any]:
        """Preview, then submit one tenant-scoped install task.

        ``reissue_config`` remains a compatibility path for old callers and deliberately
        skips the new gate.  New production callers must use this method with the hash
        captured in the approval record.
        """
        del user_id, target_version
        if action != "redeploy_tenant_vpn_config":
            return self._failure("unsupported_action", f"不支持的 FMG 动作: {action}")
        try:
            preview: dict[str, Any] | None = None
            if enforce_preview:
                preview = await self.preview_tenant_config(tenant_id=tenant_id)
                if preview["status"] == "noop":
                    return {
                        **preview,
                        "idempotency_key": idempotency_key,
                        "delivered": False,
                        "confirmed": False,
                    }
                if approved_diff_hash is not None:
                    self._require_same_preview(preview, approved_diff_hash)
            task_id, request_id = await self.submit_tenant_install(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                expected_diff_hash=(approved_diff_hash if enforce_preview else None),
            )
        except FortiManagerPreviewChangedError as exc:
            return self._failure("preview_changed", str(exc))
        except FortiManagerBusinessError as exc:
            return self._failure("fortimanager_rejected", str(exc))
        except (TimeoutError, ConnectionError):
            # The submit request may have reached FMG but the task id was not saved.
            return self._failure(
                "submission_unknown",
                "install 请求已发出但未取得 task id，禁止自动重试，需人工确认",
            )
        result = await self.wait_for_task(
            tenant_id=tenant_id, task_id=task_id, external_request_id=request_id
        )
        return {
            **result,
            "idempotency_key": idempotency_key,
            **({"diff_hash": preview["diff_hash"]} if preview else {}),
        }

    async def get_system_status(self) -> dict[str, Any]:
        """Read FMG system status without touching tenant configuration."""
        result, _ = await self._rpc(method="get", url="/sys/status")
        return result.get("data") or {}

    async def validate_device_mapping(self, *, tenant_id: str) -> dict[str, Any]:
        """Verify that a tenant target's device is visible in FMG."""
        target = self._target(tenant_id)
        try:
            result, request_id = await self._rpc(method="get", url="/dvmdb/device")
        except FortiManagerBusinessError as exc:
            return {"ok": False, "tenant_id": tenant_id, "error_code": "device_query_failed", "reason": str(exc)}
        devices = result.get("data") or []
        matched = self._find_named_record(devices, target.device)
        return {
            "ok": matched is not None,
            "tenant_id": tenant_id,
            "device": target.device,
            "record": matched,
            "request_id": request_id,
            "error_code": None if matched is not None else "device_not_found",
        }

    async def validate_package_mapping(self, *, tenant_id: str) -> dict[str, Any]:
        """Verify that a package target is visible in the tenant ADOM."""
        target = self._target(tenant_id)
        if target.install_kind != "package" or not target.package:
            return {"ok": True, "tenant_id": tenant_id, "skipped": True}
        try:
            result, request_id = await self._rpc(
                method="get", url=f"/pm/config/adom/{target.adom}/pkg"
            )
        except FortiManagerBusinessError as exc:
            return {"ok": False, "tenant_id": tenant_id, "error_code": "package_query_failed", "reason": str(exc)}
        packages = result.get("data") or []
        matched = self._find_named_record(packages, target.package)
        return {
            "ok": matched is not None,
            "tenant_id": tenant_id,
            "package": target.package,
            "record": matched,
            "request_id": request_id,
            "error_code": None if matched is not None else "package_not_found",
        }

    async def validate_tenant_target(self, *, tenant_id: str) -> dict[str, Any]:
        """Run the explicit target, device, and package read-only checks."""
        target = self._target(tenant_id)
        device = await self.validate_device_mapping(tenant_id=tenant_id)
        package = await self.validate_package_mapping(tenant_id=tenant_id)
        return {
            "ok": bool(device.get("ok") and package.get("ok")),
            "tenant_id": tenant_id,
            "target": self._target_snapshot(target),
            "device": device,
            "package": package,
        }

    async def preview_tenant_config(self, *, tenant_id: str) -> dict[str, Any]:
        """Generate the current FMG diff and return a stable approval fingerprint."""
        target = self._target(tenant_id)
        data = self._install_data(target, "preview")
        if target.install_kind == "device":
            preview_url = "/securityconsole/install/preview"
            data = {
                "adom": target.adom,
                "device": target.device,
                "flags": ["none"],
                "vdoms": [target.vdom],
            }
        else:
            preview_url = "/securityconsole/install/package"
            data["flags"] = ["preview"]
        result, request_id = await self._rpc(method="exec", url=preview_url, data=data)
        task_id = (result.get("data") or {}).get("task")
        if not isinstance(task_id, int) or task_id <= 0:
            raise FortiManagerBusinessError(-1, "preview 响应未包含有效 task id", preview_url)
        task_result = await self.wait_for_task(
            tenant_id=tenant_id, task_id=task_id, external_request_id=request_id
        )
        if task_result.get("error_code"):
            return self._failure("preview_failed", task_result.get("reason", "preview 任务失败"), task=task_result)
        result, result_request_id = await self._rpc(
            method="exec",
            url="/securityconsole/preview/result",
            data={"adom": target.adom, "device": target.device},
        )
        preview_data = result.get("data") or {}
        diff = preview_data.get("message", preview_data)
        diff_hash = self._stable_hash(diff)
        return {
            "ok": True,
            "status": "noop" if self._is_empty_diff(diff) else "previewed",
            "tenant_id": tenant_id,
            "target": self._target_snapshot(target),
            "preview_task_id": task_id,
            "external_request_id": result_request_id,
            "diff_snapshot": diff,
            "diff_hash": diff_hash,
        }

    async def submit_tenant_install(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        expected_diff_hash: str | None = None,
    ) -> tuple[int, str]:
        """Submit install only after the approved preview still matches."""
        if expected_diff_hash is not None:
            current = await self.preview_tenant_config(tenant_id=tenant_id)
            self._require_same_preview(current, expected_diff_hash)
        return await self.submit_reissue_config(
            tenant_id=tenant_id, idempotency_key=idempotency_key
        )

    async def get_task_status(
        self, *, tenant_id: str, task_id: int, external_request_id: str | None = None
    ) -> dict[str, Any]:
        """Public name for task reconciliation; the old method remains compatible."""
        return await self.get_command_status(
            tenant_id=tenant_id, task_id=task_id, external_request_id=external_request_id
        )

    async def submit_reissue_config(
        self, *, tenant_id: str, idempotency_key: str
    ) -> tuple[int, str]:
        if not idempotency_key:
            raise FortiManagerConfigurationError("FortiManager 写入必须携带本地 idempotency_key")
        target = self._target(tenant_id)
        data: dict[str, Any] = {
            "adom": target.adom,
            "flags": ["none"],
            "scope": [{"name": target.device, "vdom": target.vdom}],
        }
        if target.install_kind == "package":
            data["pkg"] = target.package
        else:
            # The comment is traceable but deliberately excludes user identifiers and secrets.
            data["dev_rev_comments"] = f"vpn-agent approved install {idempotency_key[:32]}"
        result, request_id = await self._rpc(
            method="exec",
            url=f"/securityconsole/install/{target.install_kind}",
            data=data,
        )
        task_id = (result.get("data") or {}).get("task")
        if not isinstance(task_id, int) or task_id <= 0:
            raise FortiManagerBusinessError(
                -1, "install 响应未包含有效 task id", "/securityconsole/install"
            )
        return task_id, request_id

    async def get_command_status(
        self, *, tenant_id: str, task_id: int, external_request_id: str | None = None
    ) -> dict[str, Any]:
        self._target(tenant_id)  # Explicitly deny unconfigured tenants even for reads.
        try:
            data, request_id = await self._rpc(
                method="get", url=f"/task/task/{task_id}", request_id=external_request_id
            )
        except FortiManagerBusinessError as exc:
            return self._failure("fortimanager_task_query_failed", str(exc), task_id=task_id)
        task = data.get("data") or {}
        return self._task_outcome(task, task_id=task_id, external_request_id=request_id)

    async def wait_for_task(
        self, *, tenant_id: str, task_id: int, external_request_id: str
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self._config.max_wait_seconds
        latest: dict[str, Any] = {}
        while True:
            latest = await self.get_command_status(
                tenant_id=tenant_id, task_id=task_id, external_request_id=external_request_id
            )
            if latest.get("confirmed") or latest.get("error_code"):
                return latest
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(self._config.poll_interval_seconds)
        return self._failure(
            "vendor_timeout",
            "FortiManager task 在本地等待窗口内未完成，等待补偿对账",
            vendor_task_id=task_id,
            external_request_id=external_request_id,
            task=latest.get("task"),
        )

    async def _rpc(
        self,
        *,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        request_id = request_id or uuid.uuid4().hex
        params: dict[str, Any] = {"url": url}
        if data is not None:
            params["data"] = data
        payload = {"id": request_id, "method": method, "params": [params], "verbose": 1}
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }
        if self._config.access_user:
            headers["access_user"] = self._config.access_user
        try:
            response = await self._client.post(
                f"{self._config.base_url.rstrip('/')}/jsonrpc", json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("FortiManager JSON-RPC 请求超时，外部结果未知") from exc
        except httpx.TransportError as exc:
            raise ConnectionError("FortiManager JSON-RPC 连接失败") from exc
        if response.status_code in {401, 403}:
            raise FortiManagerBusinessError(
                -response.status_code, f"HTTP {response.status_code}", url
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FortiManagerBusinessError(-1, "FortiManager 返回非 JSON 响应", url) from exc
        if not isinstance(body, dict):
            raise FortiManagerBusinessError(-1, "FortiManager 返回的 JSON 不是对象", url)
        result = (body.get("result") or [{}])[0]
        if not isinstance(result, dict):
            raise FortiManagerBusinessError(-1, "FortiManager result 不是对象", url)
        status = result.get("status") or {}
        code = status.get("code", -1)
        if code != 0:
            raise FortiManagerBusinessError(int(code), str(status.get("message", "unknown")), url)
        return result, request_id

    @staticmethod
    def _stable_hash(value: Any) -> str:
        if isinstance(value, str):
            raw = value.encode("utf-8")
        else:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _is_empty_diff(value: Any) -> bool:
        return value is None or value == "" or value == {} or value == []

    @staticmethod
    def _find_named_record(value: Any, name: str) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if value.get("name") == name:
                return value
            for child in value.values():
                found = FortiManagerCommandGateway._find_named_record(child, name)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = FortiManagerCommandGateway._find_named_record(child, name)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _target_snapshot(target: FortiManagerTarget) -> dict[str, Any]:
        return {
            "adom": target.adom,
            "device": target.device,
            "vdom": target.vdom,
            "package": target.package,
            "install_kind": target.install_kind,
        }

    @staticmethod
    def _install_data(target: FortiManagerTarget, comment_key: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "adom": target.adom,
            "flags": ["none"],
            "scope": [{"name": target.device, "vdom": target.vdom}],
        }
        if target.install_kind == "package":
            data["pkg"] = target.package
        else:
            data["dev_rev_comments"] = f"vpn-agent {comment_key}"
        return data

    @staticmethod
    def _require_same_preview(preview: dict[str, Any], expected_hash: str) -> None:
        if preview.get("status") == "noop":
            raise FortiManagerPreviewChangedError(
                -409, "已批准的 preview 与当前状态不一致：当前已无待下发差异", "/securityconsole/preview"
            )
        actual = preview.get("diff_hash")
        if not isinstance(actual, str) or actual != expected_hash:
            raise FortiManagerPreviewChangedError(
                -409, "已批准的 preview diff_hash 已变化，必须重新 preview", "/securityconsole/preview"
            )

    def _target(self, tenant_id: str) -> FortiManagerTarget:
        target = self._targets.get(tenant_id)
        if target is None:
            raise FortiManagerConfigurationError(
                f"租户 {tenant_id} 未配置 FortiManager 控制面 target"
            )
        return target

    @staticmethod
    def _task_outcome(
        task: dict[str, Any], *, task_id: int, external_request_id: str
    ) -> dict[str, Any]:
        percent = task.get("percent")
        num_err = task.get("num_err", 0)
        base = {
            "found": True,
            "vendor_task_id": task_id,
            "external_request_id": external_request_id,
            "task": task,
        }
        if percent != 100:
            return {**base, "delivered": False, "confirmed": False, "status": "running"}
        if num_err == 0:
            return {**base, "delivered": True, "confirmed": True, "status": "completed"}
        details = [
            str(line.get("detail", "")) for line in task.get("line", []) if isinstance(line, dict)
        ]
        return {
            **base,
            "delivered": False,
            "confirmed": False,
            "error_code": "fortimanager_task_failed",
            "reason": "; ".join(detail for detail in details if detail)
            or "FortiManager task failed",
            "status": "failed",
        }

    @staticmethod
    def _failure(error_code: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "found": False,
            "delivered": False,
            "confirmed": False,
            "error_code": error_code,
            "reason": reason,
            **extra,
        }


def build_fortimanager_gateway_from_env() -> FortiManagerCommandGateway:
    """Construct the gateway only when the explicit production write config is complete."""
    config = FortiManagerConfig(
        base_url=os.getenv("VPN_FMG_BASE_URL", "").strip(),
        api_token=os.getenv("VPN_FMG_API_TOKEN", "").strip(),
        access_user=os.getenv("VPN_FMG_ACCESS_USER", "").strip() or None,
        connect_timeout=float(os.getenv("VPN_FMG_CONNECT_TIMEOUT", "3")),
        read_timeout=float(os.getenv("VPN_FMG_READ_TIMEOUT", "10")),
        poll_interval_seconds=float(os.getenv("VPN_FMG_POLL_INTERVAL_SECONDS", "2")),
        max_wait_seconds=float(os.getenv("VPN_FMG_MAX_WAIT_SECONDS", "600")),
        verify_tls=os.getenv("VPN_FMG_VERIFY_TLS", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    )
    raw_targets = os.getenv("VPN_FMG_TENANT_TARGETS_JSON", "").strip()
    if not raw_targets:
        raise FortiManagerConfigurationError("缺少 VPN_FMG_TENANT_TARGETS_JSON")
    return FortiManagerCommandGateway(config, targets_from_json(raw_targets))
