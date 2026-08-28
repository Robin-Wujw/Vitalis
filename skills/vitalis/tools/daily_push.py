#!/usr/bin/env python3
"""Synchronize, analyze, and send one deterministic morning report via PushPlus."""

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vitalis.services.daily_push import run_morning_push  # noqa: E402


def _runtime_value(name: str, private_env: dict, default: str | None = None) -> str | None:
    return os.getenv(name) or private_env.get(name) or default


def main() -> int:
    parser = argparse.ArgumentParser(description="同步、分析并推送 Vitalis 晨报")
    private_env = dotenv_values(Path.home() / ".hermes" / ".env")
    configured_user = _runtime_value("VITALIS_USER", private_env)
    parser.add_argument("--user", default=configured_user, required=not configured_user)
    parser.add_argument("--days", type=int, choices=range(1, 8), default=2)
    args = parser.parse_args()
    token = _runtime_value("PUSHPLUS_TOKEN", private_env)
    if not token:
        parser.error("PUSHPLUS_TOKEN is required")
    result = run_morning_push(
        args.user,
        token,
        api=_runtime_value("VITALIS_API", private_env, "http://localhost:8000"),
        sync_days=args.days,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
