"""Server rendering and client file writes have separate, read-only contracts."""

from types import SimpleNamespace

import pytest

from vitalis import api_client


def stored_reports(monkeypatch):
    from vitalis.entrypoints.api.routes import current

    calls = []
    daily = {"analysis_run_id": "synthetic-daily-run", "user_id": "export-owner", "date": "2026-10-07",
        "report_context": {"as_of": "2026-10-07T13:20:00+00:00", "timezone": "Asia/Shanghai"},
        "data_quality": {"status": "SUFFICIENT", "status_label": "数据完整"},
        "features": {"sleep": {"duration_minutes": 420}, "activity": {}, "training": {}},
        "facts": {}, "events": [], "decision": {"action": "INSUFFICIENT_DATA", "action_plan": {}}}
    evening = {"period": "evening", "analysis_run_id": "synthetic-evening-run", "user_id": "export-owner",
        "date": "2026-10-07", "period_start": "2026-10-07", "period_end": "2026-10-07",
        "headline": "合成复盘", "metrics": [], "findings": [], "training": [],
        "suggestions": [], "alerts": [], "sections": []}

    def read(kind, value):
        def result(user, day):
            calls.append((kind, user, day))
            return value
        return result

    monkeypatch.setattr(current, "get_intelligence_query", lambda: SimpleNamespace(
        daily=read("daily", daily), evening_briefing=read("evening", evening),
        morning_briefing=read("morning", None), weekly_briefing=read("weekly", None), monthly_briefing=read("monthly", None)))
    return calls


def test_export_api_selects_correct_saved_report_and_returns_verified_digest(client, monkeypatch):
    import hashlib

    calls = stored_reports(monkeypatch)
    for kind, heading in (("daily", "# 日报 ·"), ("evening", "# 晚报 ·")):
        response = client.get(f"/api/reports/{kind}/export?day=2026-10-07&format=markdown",
            headers={"X-User-Id": "export-owner"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["content"].startswith(heading)
        assert result["content_sha256"] == hashlib.sha256(result["content"].encode()).hexdigest()
        assert result["media_type"] == "text/markdown"
    assert [kind for kind, _, _ in calls] == ["daily", "evening"]
    assert all(user == "export-owner" for _, user, _ in calls)


def test_export_missing_snapshot_is_404_and_does_not_create_one(client, monkeypatch):
    calls = stored_reports(monkeypatch)
    result = client.get("/api/reports/morning/export?day=2026-10-07", headers={"X-User-Id": "export-missing"})
    assert result.status_code == 404
    assert len(calls) == 1


def test_client_export_never_overwrites_a_file(tmp_path):
    target = tmp_path / "report.md"
    result = {"content": "synthetic report", "media_type": "text/markdown", "content_sha256": "digest"}
    api_client.write_export(result, target)
    with pytest.raises(api_client.ClientError):
        api_client.write_export({**result, "content": "changed"}, target)
    assert target.read_text(encoding="utf-8") == "synthetic report"


def test_client_export_creation_race_preserves_other_writers_file(tmp_path, monkeypatch):
    output = tmp_path / "report.md"

    def racing_writer(*_args):
        output.write_text("other writer's file", encoding="utf-8")
        raise FileExistsError("file was created concurrently")

    monkeypatch.setattr(api_client.os, "open", racing_writer)
    with pytest.raises(api_client.ClientError):
        api_client.write_export({"content": "synthetic report"}, output)
    assert output.read_text(encoding="utf-8") == "other writer's file"
