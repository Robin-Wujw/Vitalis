"""Rebuild readable report examples from the production synthetic-data pipeline.

This tool creates its own temporary SQLite database, clears vendor notification
configuration, and never sends a report. Run from the repository root after
installing the locked environment. --check compares outputs without writing them.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "examples" / "reports"


def _preview(content: str, name: str, *, markdown_source: bool = False) -> str:
    if markdown_source:
        from markdown import markdown
        content = '<main style="max-width:680px;margin:auto;padding:16px;font-size:16px;line-height:1.65;overflow-wrap:anywhere">' + markdown(content) + '</main>'
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Vitalis · {html.escape(name)} · 合成示例</title></head>'
        '<body style="margin:0"><aside style="margin:12px 16px;font-size:13px;color:#475569">'
        '合成数据 · Vitalis 程序生成 · 未发送通知</aside>' + content + '</body></html>\n'
    )


def _render_examples(day: date):
    from vitalis import bootstrap
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.config import settings
    from vitalis.domain import ActivityRecord, NormalizedDaily, SleepRecord
    from vitalis.intelligence.report_periods import resolve_month_period, resolve_week_period
    from vitalis.intelligence.report_rendering import render_report

    query = bootstrap.get_intelligence_query()
    documents = {
        "morning": query.morning_briefing("demo", day),
        "daily": query.daily("demo", day),
        "evening": query.evening_briefing("demo", day),
        "weekly": query.weekly_briefing("demo", day),
        "monthly": query.monthly_briefing("demo", day),
    }
    if any(document is None for document in documents.values()):
        raise RuntimeError("synthetic analysis did not produce all five reports")
    # A second synthetic user demonstrates a local sleep gap with complete
    # activity observations, using the same repository and analysis use case.
    user = "synthetic-insufficient"
    month = resolve_month_period(day)
    week = resolve_week_period(day)
    start = min(day - timedelta(days=55), month.reference_start)
    with session_scope() as db:
        repository = HealthRepository(db)
        repository.upsert_user(user)
        repository.bind_source_mode(user, "mock")
        current = start
        while current <= day:
            sleep = SleepRecord(
                user_id=user, date=current, sleep_duration=420,
                bedtime=time(23), wake_time=time(6), awake=15,
            ) if current in {week.start, week.start + timedelta(days=2)} else None
            repository.save_daily(NormalizedDaily(
                user_id=user, date=current, sleep=sleep,
                activity=ActivityRecord(
                    user_id=user, date=current, steps=7100, distance_km=5,
                    active_minutes=45, observed_fields=["steps", "distance_km", "active_minutes"],
                ),
            ))
            current += timedelta(days=1)
    cutoff = datetime.combine(day, time(21, 20), ZoneInfo(settings.timezone)).astimezone(timezone.utc)
    bootstrap.get_intelligence_command(
        today_factory=lambda: day, now_factory=lambda: cutoff,
    ).analyze(user, day)
    documents["insufficient-data"] = query.weekly_briefing(user, day)
    return {
        name: (document, render_report(document, "markdown"), render_report(document, "html"))
        for name, document in documents.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--day", type=date.fromisoformat, default=date(2026, 10, 7))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="vitalis-report-examples-") as directory:
        database = Path(directory) / "synthetic.db"
        environment = {
            **os.environ, "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "ZEPP_MOCK": "true", "VITALIS_ENV": "test", "VITALIS_TIMEZONE": "Asia/Shanghai",
            "PUSHPLUS_TOKEN": "", "PUSHPLUS_ACCESS_KEY": "", "VITALIS_PUSH_USER": "",
            "ZEPP_APP_ID": "", "ZEPP_APP_SECRET": "", "ZEPP_ACCESS_TOKEN": "",
            "PYTHONIOENCODING": "utf-8",
        }
        result = subprocess.run(
            [sys.executable, "-I", "-m", "vitalis", "demo", "--database", str(database), "--day", args.day.isoformat()],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            return result.returncode
        os.environ.update(environment)
        examples = _render_examples(args.day)
        files: dict[str, str] = {}
        manifest = {
            "dataset": "synthetic_demo", "generator": "tools/generate_report_examples.py",
            "day": args.day.isoformat(), "real_notifications_sent": 0,
            "pushplus_live_validation": "not_performed", "reports": {},
        }
        for name, (document, markdown, html_report) in examples.items():
            files[f"{name}.md"] = markdown.content
            files[f"{name}.fragment.html"] = html_report.content
            files[f"{name}.html"] = _preview(html_report.content, name)
            files[f"{name}.markdown-preview.html"] = _preview(markdown.content, name, markdown_source=True)
            files[f"{name}.markdown.payload.json"] = json.dumps(markdown.as_dict(), ensure_ascii=False, indent=2) + "\n"
            files[f"{name}.html.payload.json"] = json.dumps(html_report.as_dict(), ensure_ascii=False, indent=2) + "\n"
            manifest["reports"][name] = {
                "markdown": {key: value for key, value in markdown.as_dict().items() if key != "content"},
                "html": {key: value for key, value in html_report.as_dict().items() if key != "content"},
            }
        files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        differences = []
        for relative, content in files.items():
            destination = args.output / relative
            data = content.encode("utf-8")
            if args.check:
                if not destination.is_file() or destination.read_bytes() != data:
                    differences.append(relative)
            else:
                args.output.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink():
                    raise ValueError("report examples must not overwrite a symlink")
                destination.write_bytes(data)
        # SQLite keeps the demo file open through the process-wide SQLAlchemy
        # engine. Dispose it before TemporaryDirectory removes the database on
        # Windows; no health data or credentials leave this process.
        from vitalis.adapters.persistence.database import get_engine
        get_engine().dispose()
        if differences:
            print("report examples differ: " + ", ".join(differences), file=sys.stderr)
            return 1
        print(f"{'checked' if args.check else 'generated'} {len(examples)} report examples; no notifications sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
