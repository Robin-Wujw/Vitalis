#!/usr/bin/env python3
"""Synchronize recent source data without performing analysis locally."""

import argparse
import json
import os
import httpx

API = os.getenv("VITALIS_API", "http://localhost:8000").rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 Vitalis 健康数据")
    configured_user = os.getenv("VITALIS_USER")
    parser.add_argument("--user", default=configured_user, required=not configured_user)
    parser.add_argument("--days", type=int, choices=range(1, 731), default=7)
    args = parser.parse_args()
    response = httpx.post(
        f"{API}/api/v1/health/sync",
        params={"days": args.days},
        headers={"X-User-Id": args.user},
        timeout=120.0,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
