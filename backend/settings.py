from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


def database_url_from_env() -> str:
    return _required("DATABASE_URL")


def _int_setting(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 必须 >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    x_api_key: str
    database_url: str
    agent_run_timeout_seconds: int
    model_retry_attempts: int
    checkpoint_retention_days: int
    auto_setup: bool

    @classmethod
    def from_env(cls) -> "Settings":
        auto_setup = os.getenv("LANGGRAPH_AUTO_SETUP", "false").strip().lower()
        return cls(
            deepseek_api_key=_required("DEEPSEEK_API_KEY"),
            x_api_key=_required("X_API_KEY"),
            database_url=database_url_from_env(),
            agent_run_timeout_seconds=_int_setting("AGENT_RUN_TIMEOUT_SECONDS", 60, 1),
            model_retry_attempts=_int_setting("MODEL_RETRY_ATTEMPTS", 2, 0),
            checkpoint_retention_days=_int_setting("CHECKPOINT_RETENTION_DAYS", 30, 1),
            auto_setup=auto_setup in {"1", "true", "yes"},
        )
