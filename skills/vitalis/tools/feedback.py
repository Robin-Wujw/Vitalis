#!/usr/bin/env python3
"""Record or list subjective health and training feedback."""

import argparse

from _client import configured_user, print_json, request


def main() -> int:
    parser = argparse.ArgumentParser(description="记录或查询 Vitalis 主观反馈")
    user = configured_user()
    parser.add_argument("--user", default=user, required=not user)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("add", help="记录反馈")
    add.add_argument("--date")
    add.add_argument("--workout-id")
    add.add_argument("--recommendation-id")
    add.add_argument("--rpe", type=float, choices=range(1, 11))
    add.add_argument("--physical-fatigue", type=int, choices=range(1, 6))
    add.add_argument("--mental-state", type=int, choices=range(1, 6))
    add.add_argument("--muscle-soreness", type=int, choices=range(1, 6))
    add.add_argument("--notes")
    listing = subparsers.add_parser("list", help="查询反馈")
    listing.add_argument("--start")
    listing.add_argument("--end")
    args = parser.parse_args()

    if args.command == "add":
        payload = {key: value for key, value in {
            "date": args.date,
            "workout_id": args.workout_id,
            "recommendation_id": args.recommendation_id,
            "session_rpe": args.rpe,
            "physical_fatigue": args.physical_fatigue,
            "mental_state": args.mental_state,
            "muscle_soreness": args.muscle_soreness,
            "notes": args.notes,
        }.items() if value is not None}
        print_json(request("POST", "feedback", args.user, json=payload))
    else:
        params = {key: value for key, value in {
            "start": args.start, "end": args.end
        }.items() if value}
        print_json(request("GET", "feedback", args.user, params=params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
