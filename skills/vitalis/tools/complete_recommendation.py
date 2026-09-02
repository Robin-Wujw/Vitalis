#!/usr/bin/env python3
"""Explicitly link one recommendation to one completed workout."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="确认训练建议已由指定训练完成")
    user = configured_user()
    parser.add_argument("recommendation_id")
    parser.add_argument("workout_id")
    parser.add_argument("--workout-source", required=True)
    parser.add_argument("--user", default=user, required=not user)
    args = parser.parse_args()
    print_json(request(
        "POST",
        f"recommendations/{args.recommendation_id}/complete",
        args.user,
        json={"workout_source": args.workout_source, "workout_id": args.workout_id},
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
