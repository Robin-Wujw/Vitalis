#!/usr/bin/env python3
"""Vitalis Skill 工具：完整 AI 分析（规则 + 统计 + LLM 解释）。

用法:
    python analyze.py [--user 001] [--date 2026-08-25] [--query "今天适合跑步吗"]
"""

import argparse
import json
import os
import sys

import httpx

API = os.getenv("VITALIS_API", "http://localhost:8000")


def analyze(user: str, day: str | None = None, query: str = "分析今天的状态") -> dict:
    body = {"agent_query": query}
    if day:
        body["day"] = day
    resp = httpx.post(
        f"{API}/api/v1/analyze",
        json=body,
        headers={"X-User-Id": user},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Vitalis AI 分析")
    parser.add_argument("--user", default=os.getenv("VITALIS_USER", "001"))
    parser.add_argument("--date")
    parser.add_argument("--query", default="分析今天的状态")
    args = parser.parse_args()

    data = analyze(args.user, args.date, args.query)
    print(f"[分析] 用户={args.user} 日期={data.get('date')} 引擎={data.get('engine')}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())