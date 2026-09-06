# Fake FMG

该服务是仓库内的 FortiManager JSON-RPC 本地演练夹具，只验证网关协议适配、preview 门禁、异步任务轮询和失败处置。

它不连接真实 FortiManager，不管理真实 FortiGate，也不能作为 staging 或生产写入证据。

`certs/` 内的自签名证书和私钥仅供离线演示。它们是公开的固定演示凭据，严禁用于任何真实环境。

默认令牌为 `fake-token-123`，可通过 `FAKE_FMG_TOKEN` 覆盖。`FAKE_FMG_PREVIEW_MODE=noop` 可用于演示无差异时不进入审批和 install。
