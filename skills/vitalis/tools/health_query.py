#!/usr/bin/env python3
"""Vitalis Skill 工具：查询健康数据。

用法:
    python health_query.py [--user 001] [--date 2026-08-25]
默认查今天。返回恢复分/训练就绪度/压力等级/解释文本。
"""

import argparse
import os
import sys

import httpx

API = os.getenv("VITALIS_API", "http://localhost:8000")


def query(user: str, day: str | None = None) -> dict:
    params = {}
    if day:
        params["day"] = day
    resp = httpx.get(
        f"{API}/api/v1/health/today",
        params=params,
        headers={"X-User-Id": user},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="查询 Vitalis 健康状态")
    parser.add_argument("--user", default=os.getenv("VITALIS_USER", "001"))
    parser.add_argument("--date", help="日期 YYYY-MM-DD，缺省今天")
    args = parser.parse_args()

    data = query(args.user, args.date)
    date_label = data.get("date", args.date or "today")
    print(f"[健康状态] 用户={args.user} 日期={date_label}")
    if data.get("score") is None:
        print(f"[提示] {data.get('detail', '暂无数据')}")
        return 0
    print(f"恢复分: {data['score']}")
    print(f"恢复水平: {data['sleep']}")
    print(f"训练就绪: {data['training']}")
    print(f"压力等级: {data['stress']}")
    for rule in data.get("matched_rules", []):
        print(f"  - {rule}")
    print(f"\n{data.get('explanation', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())