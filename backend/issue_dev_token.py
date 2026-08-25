"""开发工具 —— 生成本地开发用租户 token。

用法：
    uv run python -m backend.issue_dev_token --tenant demo --user admin
    （AUTH_MODE=dev 下，鉴权需要 TENANT_TOKEN_SECRET 签发的 token）
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from .security import make_tenant_token


def main() -> None:
    parser = argparse.ArgumentParser(description="签发本地开发租户令牌")
    parser.add_argument("tenant_id")
    parser.add_argument("user_id")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args()
    load_dotenv()
    secret = os.getenv("TENANT_TOKEN_SECRET", "").strip()
    if not secret:
        raise SystemExit("缺少 TENANT_TOKEN_SECRET")
    print(make_tenant_token(args.tenant_id, args.user_id, secret, ttl_seconds=args.ttl))


if __name__ == "__main__":
    main()
