#!/usr/bin/env python3
"""Read or replace health-first concurrent training preferences."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="查询或设置 Vitalis 训练偏好")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("get", help="查询当前训练偏好")
    update = subparsers.add_parser("set", help="完整替换当前训练偏好")
    update.add_argument("--running-target", type=int, default=3, choices=range(1, 8))
    update.add_argument("--strength-target", type=int, default=3, choices=range(1, 8))
    update.add_argument("--available-weekdays", nargs="*", type=int, default=[])
    update.add_argument("--max-session-minutes", type=int)
    update.add_argument("--running-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    update.add_argument("--strength-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    update.add_argument("--equipment", nargs="*", default=[])
    update.add_argument("--pain-status", choices=("NONE", "PRESENT", "UNKNOWN"), default="UNKNOWN")
    update.add_argument("--pain-notes")
    args = parser.parse_args()

    if args.command == "get":
        payload = request("GET", "training-preferences", args.user)
    else:
        body = {
            "weekly_running_target": args.running_target,
            "weekly_strength_target": args.strength_target,
            "available_weekdays": args.available_weekdays,
            "running_experience": args.running_experience,
            "strength_experience": args.strength_experience,
            "equipment": args.equipment,
            "pain_or_injury_status": args.pain_status,
            "pain_or_injury_notes": args.pain_notes,
        }
        if args.max_session_minutes is not None:
            body["max_session_minutes"] = args.max_session_minutes
        payload = request("PUT", "training-preferences", args.user, json=body)
    print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
