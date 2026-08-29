"""统一环境配置入口。

职责：所有进程入口（API 进程、Worker、CLI 脚本）启动时调用一次
``load_environment()``，把项目根 ``.env``（若存在）加载到 ``os.environ``。
业务模块与 ``backend/settings.py`` **不得**自行加载 ``.env``——它们只从
``os.environ`` 读取，由入口决定环境来源（.env / 编排平台注入 / 测试 monkeypatch）。

幂等性：``load_dotenv(override=False)`` 不覆盖已存在的环境变量，重复调用无副作用；
测试通过 ``monkeypatch.setenv`` 注入时，先注入的变量优先于 .env。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


def load_environment(*, override: bool = False) -> None:
    """加载项目根 ``.env``（若存在）到 os.environ；已存在的变量优先。

    幂等：``load_dotenv(override=False)`` 不覆盖已存在变量，重复调用无副作用；
    测试通过 ``monkeypatch.setenv`` 注入时，先注入的变量优先于 .env。

    显式跳过：环境变量 ``SKIP_DOTENV_LOAD=1`` 时完全跳过 .env 加载——测试进程
    （tests/conftest.py 设置）据此保证「有无 .env 时结果一致」。
    """
    if os.getenv("SKIP_DOTENV_LOAD", "").strip().lower() in ("1", "true", "yes"):
        return
    load_dotenv(override=override)
