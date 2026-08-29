"""结构化 JSON 日志 —— worker 处理事件统一携带上下文字段。

字段约定：tenant_id / channel / event_id / ticket_id / operation_id /
worker_id / attempt / status / duration_ms。禁止记录 access token、
签名密钥、用户敏感正文与完整加密 XML。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        context = getattr(record, "ctx", None)
        if isinstance(context, dict):
            allowed = (
                "tenant_id",
                "channel",
                "event_id",
                "ticket_id",
                "operation_id",
                "worker_id",
                "attempt",
                "status",
                "duration_ms",
                "error_code",
                "worker_type",
            )
            payload.update({key: value for key, value in context.items() if key in allowed})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_json_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
