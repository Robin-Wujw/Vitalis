#!/usr/bin/env python3
"""Vitalis Skill 工具：同步数据（连接 Zepp）。

用法:
    python sync.py [--user 001] [--start 2026-08-01] [--end 2026-08-25]
"""

import argparse
import os
import sys

import httpx

API = os.getenv("VITALIS_API", "http://localhost:8000")


def sync(user: str, start: str | None = None, end: str | None = None) -> dict:
    body: dict = {"source": "zepp", "sync_history": True}
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    resp = httpx.post(
        f"{API}/api/v1/connect/zepp",
        json=body,
        headers={"X-User-Id": user},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 Zepp 数据到 Vitalis")
    parser.add_argument("--user", default=os.getenv("VITALIS_USER", "001"))
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()

    try:
        result = sync(args.user, args.start, args.end)
        print(f"[ok] 用户 {args.user} 连接 Zepp：{result.get('auth_mode')}")
        if "sync" in result:
            s = result["sync"]
            print(f"[ok] 同步 {s['days_synced']} 天数据（{s['start']} ~ {s['end']}）")
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())