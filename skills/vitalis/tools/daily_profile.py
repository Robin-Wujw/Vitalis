#!/usr/bin/env python3
"""Fetch one structured DailyProfile without performing local analysis."""

import argparse
import json
import os

import httpx

API = os.getenv("VITALIS_API", "http://localhost:8000")


def query(user: str, day: str | None = None) -> dict:
    response = httpx.get(
        f"{API}/api/v1/intelligence/daily-profile",
        params={"day": day} if day else {},
        headers={"X-User-Id": user},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Vitalis DailyProfile")
    parser.add_argument("--user", default=os.getenv("VITALIS_USER", "001"))
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    args = parser.parse_args()
    print(json.dumps(query(args.user, args.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
