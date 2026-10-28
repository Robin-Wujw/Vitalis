"""Launch a configured Hermes home with private Vitalis API credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_SKILL = runpy.run_path(str(ROOT / "skills" / "vitalis" / "scripts" / "vitalis_api.py"))


class Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid_arguments")


def private_token(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(4097)
        token = raw.decode("ascii").strip()
        if len(raw) > 4096 or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError
        return token
    except (OSError, ValueError) as exc:
        raise ValueError("invalid_access_token") from exc


def main(argv: list[str] | None = None) -> int:
    parser = Arguments(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--hermes-command", default="hermes")
    parser.add_argument("hermes_args", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
        home = args.home.expanduser().resolve()
        if not (home / "config.yaml").is_file() or not (home / "skills" / "vitalis" / "SKILL.md").is_file():
            raise ValueError("invalid_hermes_home")
        origin = _SKILL["api_origin"](args.api_base_url)
        token = private_token(args.token_file.expanduser())
        if not args.hermes_command.strip():
            raise ValueError("invalid_arguments")
        scratch = ROOT / ".tmp" / "hermes-launcher"
        scratch.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        for legacy in ("VITALIS_API", "VITALIS_USER"):
            environment.pop(legacy, None)
        environment.update({
            "HERMES_HOME": str(home),
            "VITALIS_API_BASE_URL": origin,
            "VITALIS_ACCESS_TOKEN": token,
            "TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        })
        remainder = args.hermes_args
        if remainder[:1] == ["--"]:
            remainder = remainder[1:]
        return subprocess.run(
            [args.hermes_command, *remainder], cwd=home, env=environment, check=False,
        ).returncode
    except _SKILL["ClientError"] as exc:
        code, status = exc.code, 2
    except ValueError as exc:
        known = {"invalid_arguments", "invalid_hermes_home", "invalid_access_token"}
        code, status = str(exc) if str(exc) in known else "invalid_arguments", 2
    except FileNotFoundError:
        code, status = "hermes_command_unavailable", 127
    except OSError:
        code, status = "hermes_launch_failed", 1
    print(json.dumps({
        "state": "failed", "failure_code": code, "retryable": False,
        "next_action": "check_configuration",
    }), file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
