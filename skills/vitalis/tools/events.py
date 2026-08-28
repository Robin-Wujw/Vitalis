#!/usr/bin/env python3
"""Fetch persistent, explainable health events."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 Vitalis 健康事件")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--type", dest="event_type", help="可选事件类型代码")
    args = parser.parse_args()
    params = {key: value for key, value in {
        "start": args.start, "end": args.end, "event_type": args.event_type
    }.items() if value}
    print_json(request("GET", "events", args.user, params=params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
