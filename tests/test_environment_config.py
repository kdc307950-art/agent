"""统一环境配置测试（收敛方案阶段五）：
- 显式设置的环境变量优先于 .env（load_environment 不覆盖已存在变量）；
- 进程入口的 load_environment 是幂等的；
- SKIP_DOTENV_LOAD=1 时完全不加载 .env（测试进程默认）。
"""

from __future__ import annotations

import os

from backend.config import load_environment


def test_explicit_env_wins_over_dotenv(monkeypatch):
    """显式系统环境变量优先：即使 .env 里有同名变量，也不被覆盖。"""
    monkeypatch.delenv("SKIP_DOTENV_LOAD", raising=False)  # 允许本测试真实加载 .env
    monkeypatch.setenv("DEEPSEEK_API_KEY", "explicit-value")
    load_environment()
    assert os.environ["DEEPSEEK_API_KEY"] == "explicit-value"


def test_load_environment_is_idempotent(monkeypatch):
    """重复调用 load_environment 无副作用（不覆盖、不报错）。"""
    monkeypatch.delenv("SKIP_DOTENV_LOAD", raising=False)
    monkeypatch.setenv("TENANT_TOKEN_SECRET", "already-set")
    load_environment()
    load_environment()
    load_environment()
    assert os.environ["TENANT_TOKEN_SECRET"] == "already-set"


def test_skip_dotenv_load_prevents_env_file(monkeypatch):
    """SKIP_DOTENV_LOAD=1 时 load_environment 完全不触碰 os.environ。"""
    monkeypatch.setenv("SKIP_DOTENV_LOAD", "1")
    marker = "should-not-exist-after-load"
    monkeypatch.delenv(marker, raising=False)
    load_environment()
    assert os.getenv(marker) is None


def test_settings_do_not_implicitly_load_dotenv():
    """settings 模块只从 os.environ 读取：import 后不产生 .env 副作用。"""
    import backend.settings  # noqa: F401

    # 若 settings 隐式加载 .env，下面这个未设置的变量会被 .env 填充；
    # 本测试进程默认 SKIP_DOTENV_LOAD=1，且 settings 无模块级 load_dotenv。
    marker = "SETTINGS_IMPLICIT_DOTENV_MARKER"
    assert os.getenv(marker) is None
