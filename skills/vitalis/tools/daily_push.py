#!/usr/bin/env python3
"""Synchronize, analyze, and send one deterministic daily report via PushPlus."""

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vitalis.services.daily_push import run_daily_push  # noqa: E402


def _runtime_value(name: str, private_env: dict, default: str | None = None) -> str | None:
    return os.getenv(name) or private_env.get(name) or default


def main() -> int:
    parser = argparse.ArgumentParser(description="同步、分析并推送 Vitalis 日报")
    private_env = dotenv_values(Path.home() / ".hermes" / ".env")
    configured_user = _runtime_value("VITALIS_USER", private_env)
    parser.add_argument("--user", default=configured_user, required=not configured_user)
    parser.add_argument("--period", choices=("morning", "evening"), default="morning")
    parser.add_argument("--days", type=int, choices=range(1, 8))
    parser.add_argument(
        "--test",
        action="store_true",
        help="真实发送测试报告，但不读取或写入正式调度的去重标记",
    )
    args = parser.parse_args()
    token = _runtime_value("PUSHPLUS_TOKEN", private_env)
    if not token:
        parser.error("PUSHPLUS_TOKEN is required")
    result = run_daily_push(
        args.user,
        token,
        period=args.period,
        api=_runtime_value("VITALIS_API", private_env, "http://localhost:8000"),
        sync_days=args.days,
        test_delivery=args.test,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
