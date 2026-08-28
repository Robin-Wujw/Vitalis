#!/usr/bin/env python3
"""Acknowledge one user-scoped health event."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="确认已查看 Vitalis 健康事件")
    user = configured_user()
    parser.add_argument("event_id")
    parser.add_argument("--user", default=user, required=not user)
    args = parser.parse_args()
    print_json(request("POST", f"events/{args.event_id}/acknowledge", args.user))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
