#!/usr/bin/env python3
"""Fetch one action-first Vitalis morning briefing."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 Vitalis 晨报")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    parser.add_argument("--date", help="YYYY-MM-DD，默认今天")
    args = parser.parse_args()
    print_json(request(
        "GET",
        "morning-briefing",
        args.user,
        params={"day": args.date} if args.date else {},
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
