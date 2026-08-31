# syntax=docker/dockerfile:1
# 后端镜像。两阶段构建：builder 用 uv 装依赖，runtime 只带虚拟环境和源码。
#
# 入口不用 `uv run`：uv 每次启动都会校验并可能改写 .venv，而生产容器应跑在
# 只读文件系统上。这里把 .venv/bin 放进 PATH，入口就是普通的 python/uvicorn，
# 容器启动不再依赖包管理器。infra/k8s 的 CronJob command 同步改成 python -m。

# ─── Stage 1: 依赖 ───────────────────────────────────────────────────────────
# uv 二进制源默认走 ghcr.io；ghcr 网络受限时可覆盖：
#   docker build --build-arg UV_IMAGE=ghcr.nju.edu.cn/astral-sh/uv:0.11
# COPY --from 不支持变量，所以先用 ARG 定义一个独立的 uv 源 stage。
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11
FROM ${UV_IMAGE} AS uv-source
FROM python:3.12-slim AS builder

COPY --from=uv-source /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 依赖层单独 COPY：只要 uv.lock 没变，改源码不会触发重新装依赖
COPY pyproject.toml uv.lock ./

# --frozen：严格按 lock 安装，构建期不允许解析出新版本，保证镜像可复现
# --no-install-project：本项目不是标准 package（backend/ 与 src/ 靠 WORKDIR 导入），
#   让 setuptools 去打包它只会猜错目录结构
# --no-dev：pytest 等开发依赖不进生产镜像
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ─── Stage 2: 运行时 ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# 非 root 运行。k8s 的 securityContext.runAsUser 只是声明，
# 镜像里没有对应 uid 的用户时容器照样起不来，所以这里必须真建一个。
RUN useradd --create-home --uid 10001 app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app src/ ./src/
COPY --chown=app:app legacy-demo/ ./legacy-demo/
COPY --chown=app:app pyproject.toml ./

USER app
EXPOSE 8000

# slim 镜像没有 curl，用自带的 python 探活。/livez 只表示进程存活，
# 不查 Postgres/Redis —— 依赖不可用时应由 /readyz 摘流量，而不是重启进程。
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).status==200 else 1)"]

# 刻意不在启动时建表：多副本滚动更新会并发建表。
# 迁移是独立的一次性任务，见 README「启动」一节。
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
