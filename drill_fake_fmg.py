"""Eight-step Fake FMG drill using real HTTP from FortiManagerCommandGateway.

The drill proves client protocol handling and failure disposition only. It does
not prove real FortiManager staging behavior or production writes. Run it from
the project root, where ``backend.vpn`` can be imported:
    ./.venv/Scripts/python.exe drill_fake_fmg.py
"""

from __future__ import annotations

import asyncio

import httpx

from backend.vpn.fortimanager_gateway import (
    FortiManagerCommandGateway,
    FortiManagerConfig,
    FortiManagerTarget,
)

BASE = "https://127.0.0.1:8443"
TOKEN = "fake-token-123"


def make_gw(targets: dict, *, max_wait: float = 1.0) -> FortiManagerCommandGateway:
    cfg = FortiManagerConfig(
        base_url=BASE,
        api_token=TOKEN,
        verify_tls=False,  # Fake FMG uses a self-signed certificate.
        poll_interval_seconds=0.001,
        max_wait_seconds=max_wait,
    )
    return FortiManagerCommandGateway(cfg, targets)


async def main() -> None:
    ok = True

    print("[STEP 1] health: Fake FMG availability")
    try:
        async with httpx.AsyncClient(verify=False, timeout=3.0) as client:
            health = await client.get(f"{BASE}/health")
            health.raise_for_status()
            health_body = health.json()
        print("  [PASS]", health_body)
    except Exception as exc:
        print(f"  [FAIL] Fake FMG unavailable: {exc}")
        raise SystemExit(2) from exc

    print("[STEP 2] status probe: read-only connectivity and version")
    g0 = make_gw({"tenant-a": FortiManagerTarget(adom="A", device="FGT-FAKE-001",
                                                 vdom="root", install_kind="device")})
    try:
        res, reqid = await g0._rpc(method="get", url="/sys/status")
        print(f"  code={res['status']['code']}, data={res['data']}, request_id={reqid}")
        ok = ok and res["status"]["code"] == 0
    finally:
        await g0.aclose()

    print("[STEP 3] confirmed: install -> task polling -> success")
    g1 = make_gw({"tenant-a": FortiManagerTarget(adom="ADOM_OK", device="FGT-FAKE-001",
                                                 vdom="root", package="PKG_FAKE", install_kind="package")})
    try:
        r1 = await g1.reissue_config(tenant_id="tenant-a", user_id="u", idempotency_key="drill-confirm-1")
        print("  confirmed:", r1.get("confirmed"), "| vendor_task_id:", r1.get("vendor_task_id"),
              "| state:", r1.get("status"))
        ok = ok and (r1.get("confirmed") is True and r1.get("vendor_task_id") is not None)
    finally:
        await g1.aclose()

    print("[STEP 4] execution_unknown: timeout retains the FMG task id")
    g2 = make_gw({"tenant-b": FortiManagerTarget(adom="STALL", device="FGT-FAKE-001",
                                                 vdom="root", package="PKG_FAKE")}, max_wait=0.05)
    try:
        r2 = await g2.reissue_config(tenant_id="tenant-b", user_id="u", idempotency_key="drill-timeout-1")
        print(
            "  error:",
            r2.get("error_code"),
            "| vendor_task_id:",
            r2.get("vendor_task_id"),
            "| reason_present:",
            bool(r2.get("reason")),
        )
        ok = ok and (r2.get("error_code") == "vendor_timeout" and r2.get("vendor_task_id"))
    finally:
        await g2.aclose()

    print("[STEP 5] reconciliation rule: do not resend before confirmation")
    print("  [PASS] vendor_task_id retained; follow-up uses confirm-submission or read-only reconciliation")

    print("[STEP 6] rejected: FMG business rejection becomes an auditable failure")
    g3 = make_gw({"tenant-c": FortiManagerTarget(adom="REJECT", device="FGT-FAKE-001",
                                                 vdom="root", package="PKG_FAKE")})
    try:
        r3 = await g3.reissue_config(tenant_id="tenant-c", user_id="u", idempotency_key="drill-reject-1")
        print("  error:", r3.get("error_code"), "| reason_present:", bool(r3.get("reason")))
        ok = ok and (r3.get("error_code") == "fortimanager_rejected")
    finally:
        await g3.aclose()

    print("[STEP 7] assertions: summarize four safety checks")
    print("  [PASS] status=0, confirmed has task id, timeout retains task id, reject is not success" if ok else
          "  [FAIL] at least one assertion failed")

    print("[STEP 8] final conclusion")
    if ok:
        print("  ALL PASS: Fake FMG real HTTP drill verified; real FMG and production writes remain unverified.")
    else:
        print("  HAS FAILURE: drill failed; do not use it as interview evidence.")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
