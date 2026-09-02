#!/usr/bin/env python3
"""Read or explicitly patch the user-confirmed profile."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="读取或更新 Vitalis 用户档案")
    user = configured_user()
    parser.add_argument("command", choices=("get", "patch"), nargs="?", default="get")
    parser.add_argument("--user", default=user, required=not user)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--sex", choices=("FEMALE", "MALE", "INTERSEX", "PREFER_NOT_TO_SAY"))
    parser.add_argument("--confirmed-hrmax-bpm", type=int)
    parser.add_argument("--sleep-target-minutes", type=int)
    parser.add_argument("--clear", choices=("sex", "confirmed_hrmax_bpm", "sleep_target_minutes"), action="append", default=[])
    args = parser.parse_args()

    if args.command == "get":
        print_json(request("GET", "profile", args.user))
        return 0
    if args.expected_revision is None:
        parser.error("patch 必须提供 --expected-revision")

    payload = {"expected_revision": args.expected_revision}
    if args.sex is not None:
        payload["sex"] = args.sex
    if args.confirmed_hrmax_bpm is not None:
        payload["confirmed_hrmax_bpm"] = args.confirmed_hrmax_bpm
    if args.sleep_target_minutes is not None:
        payload["sleep_target_minutes"] = args.sleep_target_minutes
    for field in args.clear:
        payload[field] = None
    print_json(request("PATCH", "profile", args.user, json=payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
