#!/usr/bin/env python3
"""Read bounded typed health timeline summaries."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 Vitalis 健康时间线")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=100)
    args = parser.parse_args()
    params = {key: value for key, value in {
        "start": args.start,
        "end": args.end,
        "limit": args.limit,
    }.items() if value is not None}
    print_json(request("GET", "timeline", args.user, params=params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
