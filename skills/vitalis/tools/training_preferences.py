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
    update.add_argument("--rotation-policy", choices=("BALANCE", "ALTERNATE"), default="BALANCE")
    update.add_argument("--treadmill-available", action="store_true")
    update.add_argument(
        "--bad-weather-running-policy",
        choices=("DEFER", "STRENGTH", "RECOVERY"),
        default="DEFER",
    )
    update.add_argument("--available-weekdays", nargs="*", type=int, default=[])
    update.add_argument("--max-session-minutes", type=int)
    update.add_argument("--running-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    update.add_argument("--strength-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    update.add_argument("--equipment", nargs="*", default=[])
    update.add_argument("--pain-status", choices=("NONE", "PRESENT", "UNKNOWN"), default="UNKNOWN")
    update.add_argument("--pain-notes")
    patch = subparsers.add_parser("patch", help="仅更新明确提供的偏好字段")
    patch.add_argument("--running-target", type=int, choices=range(1, 8))
    patch.add_argument("--strength-target", type=int, choices=range(1, 8))
    patch.add_argument("--rotation-policy", choices=("BALANCE", "ALTERNATE"))
    patch.add_argument("--treadmill-available", choices=("true", "false"))
    patch.add_argument("--bad-weather-running-policy", choices=("DEFER", "STRENGTH", "RECOVERY"))
    patch.add_argument("--available-weekdays", nargs="*", type=int)
    patch.add_argument("--max-session-minutes", type=int)
    patch.add_argument("--running-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    patch.add_argument("--strength-experience", choices=("BEGINNER", "INTERMEDIATE", "ADVANCED"))
    patch.add_argument("--equipment", nargs="*")
    patch.add_argument("--pain-status", choices=("NONE", "PRESENT", "UNKNOWN"))
    patch.add_argument("--pain-notes")
    patch.add_argument(
        "--clear",
        action="append",
        choices=(
            "max_session_minutes", "running_experience",
            "strength_experience", "pain_or_injury_notes",
        ),
        default=[],
    )
    args = parser.parse_args()

    if args.command == "get":
        payload = request("GET", "training-preferences", args.user)
    elif args.command == "patch":
        body = {key: value for key, value in {
            "weekly_running_target": args.running_target,
            "weekly_strength_target": args.strength_target,
            "rotation_policy": args.rotation_policy,
            "bad_weather_running_policy": args.bad_weather_running_policy,
            "available_weekdays": args.available_weekdays,
            "max_session_minutes": args.max_session_minutes,
            "running_experience": args.running_experience,
            "strength_experience": args.strength_experience,
            "equipment": args.equipment,
            "pain_or_injury_status": args.pain_status,
            "pain_or_injury_notes": args.pain_notes,
        }.items() if value is not None}
        if args.treadmill_available is not None:
            body["treadmill_available"] = args.treadmill_available == "true"
        for field in args.clear:
            body[field] = None
        payload = request("PATCH", "training-preferences", args.user, json=body)
    else:
        body = {
            "weekly_running_target": args.running_target,
            "weekly_strength_target": args.strength_target,
            "rotation_policy": args.rotation_policy,
            "treadmill_available": args.treadmill_available,
            "bad_weather_running_policy": args.bad_weather_running_policy,
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
