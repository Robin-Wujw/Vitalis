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
    parser.add_argument(
        "--detail-backfill", action="store_true",
        help="显式补抓窗口内旧训练明细；每次同步最多四份",
    )
    parser.add_argument(
        "--workout-only", action="store_true",
        help="仅同步全运动历史并有界补抓窗口内旧训练明细",
    )
    parser.add_argument(
        "--details-only", action="store_true",
        help="仅将已保存训练的缺失或旧版明细入队，每次最多四份",
    )
    parser.add_argument(
        "--refresh-before", help="仅明细模式中显式重取该 UTC ISO 时刻之前的已缓存明细",
    )
    args = parser.parse_args()
    if args.details_only and (args.workout_only or args.detail_backfill):
        parser.error("--details-only 不能和其他手动补采模式同时使用")
    if args.refresh_before and not args.details_only:
        parser.error("--refresh-before 仅支持 --details-only")
    params = {"days": args.days}
    if args.detail_backfill:
        params["detail_backfill"] = "true"
    if args.workout_only:
        params["workout_only"] = "true"
    if args.details_only:
        params.update(detail_only="true", enqueue_only="true")
    if args.refresh_before:
        params["detail_refresh_before"] = args.refresh_before
    response = httpx.post(
        f"{API}/api/v1/health/sync",
        params=params,
        headers={"X-User-Id": args.user},
        timeout=120.0,
        trust_env=False,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
