#!/usr/bin/env python3
"""Replace confirmed exercises for one user-owned strength workout."""

import argparse
import json

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="确认一次力量训练的动作与组次")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    parser.add_argument("--workout-id", required=True)
    parser.add_argument(
        "--focus",
        choices=("PUSH", "PULL", "LEGS", "UPPER", "LOWER", "FULL_BODY", "CHEST", "BACK", "SHOULDERS", "ARMS"),
    )
    parser.add_argument(
        "--exercise-json",
        action="append",
        required=True,
        help='动作 JSON，例如 {"exercise_name":"卧推","sets":4,"repetitions":"8"}',
    )
    args = parser.parse_args()
    exercises = [json.loads(value) for value in args.exercise_json]
    payload = {"session_focus": args.focus, "exercises": exercises}
    print_json(request(
        "POST",
        f"workouts/{args.workout_id}/strength-exercises",
        args.user,
        json=payload,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
