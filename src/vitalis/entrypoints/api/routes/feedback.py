"""One-time feedback page linked from a pushed report.

The token in the path is the only credential: it names one user, one report
date and one use.  Pages carry no third-party assets and are never cached.
"""
from __future__ import annotations

import html as html_mod
import secrets
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from vitalis.application.feedback_links import FeedbackLinkError
from vitalis.bootstrap import get_feedback_link_service
from vitalis.intelligence.contracts import SubjectiveFeedbackInput

router = APIRouter(prefix="/feedback", tags=["feedback"], include_in_schema=False)

TAGS = {
    "alcohol": "饮酒", "late_dinner": "晚餐晚", "caffeine": "咖啡因",
    "illness": "生病", "travel": "出差",
}
PARTS = {
    "neck": "颈部", "shoulders": "肩", "chest": "胸", "back": "背", "lower_back": "下背",
    "arms": "手臂", "core": "核心", "hips": "髋", "glutes": "臀", "quadriceps": "大腿前侧",
    "hamstrings": "大腿后侧", "knees": "膝", "calves": "小腿", "ankles": "脚踝", "feet": "脚",
}
STATE_TEXT = {
    "unknown": "这个反馈链接无效。",
    "used": "这个反馈链接已经用过了，每个链接只能提交一次。",
    "expired": "这个反馈链接已过期。",
}


def _headers(nonce: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _page(body: str, nonce: str, status: int = 200) -> HTMLResponse:
    style = (
        "body{font-family:system-ui,sans-serif;max-width:32rem;margin:1.5rem auto;padding:0 1rem;line-height:1.5}"
        "fieldset{border:1px solid #ccc;border-radius:.5rem;margin:0 0 1rem}"
        "label{display:inline-block;margin:.25rem .75rem .25rem 0}"
        "button{font-size:1rem;padding:.6rem 1.4rem}"
    )
    page = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        f'<title>Vitalis 反馈</title><style nonce="{nonce}">{style}</style></head>'
        f"<body>{body}</body></html>"
    )
    return HTMLResponse(page, status_code=status, headers=_headers(nonce))


def _checkboxes(name: str, options: dict[str, str]) -> str:
    return "".join(
        f'<label><input type="checkbox" name="{name}" value="{key}"/> {html_mod.escape(label)}</label>'
        for key, label in options.items()
    )


@router.get("/{token}", response_class=HTMLResponse)
def feedback_page(token: str) -> HTMLResponse:
    nonce = secrets.token_urlsafe(12)
    try:
        view = get_feedback_link_service().view(token)
    except FeedbackLinkError as exc:
        return _page(f"<p>{STATE_TEXT[exc.state]}</p>", nonce, 410 if exc.state != "unknown" else 404)
    workouts = "".join(
        f'<label><input type="radio" name="workout" value="{html_mod.escape(item["source"])}:{html_mod.escape(item["workout_id"])}"/>'
        f' {html_mod.escape(item["label"])}</label>'
        for item in view.workouts
    )
    rpe = (
        "<fieldset><legend>训练用力程度（RPE 1–10）</legend>"
        f"{workouts}<br/><input type=\"number\" name=\"session_rpe\" min=\"1\" max=\"10\" step=\"1\"/></fieldset>"
        if view.workouts else ""
    )
    plan = {
        True: "<p>按训练记录，今天的主要安排已完成，不用再填。</p>",
        False: "<p>按训练记录，今天还没有完成主要安排。</p>",
    }.get(view.plan_completed, "")
    feelings = "".join(
        f'<label><input type="radio" name="feeling_score" value="{value}"/> {value}</label>'
        for value in range(1, 6)
    )
    body = (
        f"<h1>{view.target_date.isoformat()} 的感觉</h1>{plan}"
        '<form method="post">'
        f"<fieldset><legend>今天感觉（1 很差 – 5 很好）</legend>{feelings}</fieldset>"
        f"{rpe}"
        f"<fieldset><legend>酸痛部位</legend>{_checkboxes('soreness_parts', PARTS)}</fieldset>"
        f"<fieldset><legend>今天的情况</legend>{_checkboxes('behavior_tags', TAGS)}</fieldset>"
        '<button type="submit">提交</button></form>'
        "<p>只在你点提交时记录；打开或不填都不算反馈。</p>"
    )
    return _page(body, nonce)


@router.post("/{token}", response_class=HTMLResponse)
async def submit_feedback(token: str, request: Request) -> HTMLResponse:
    nonce = secrets.token_urlsafe(12)
    # The page posts a plain urlencoded form; parse it without a multipart dependency.
    body = (await request.body())[:8192].decode("utf-8", errors="replace")
    form = parse_qs(body, keep_blank_values=False)

    def first(name: str) -> str | None:
        return (form.get(name) or [None])[0]

    values: dict = {
        "soreness_parts": form.get("soreness_parts", []),
        "behavior_tags": form.get("behavior_tags", []),
    }
    try:
        if first("feeling_score"):
            values["feeling_score"] = int(first("feeling_score"))
        if first("session_rpe") and first("workout"):
            source, _, workout_id = first("workout").partition(":")
            values.update(session_rpe=float(first("session_rpe")), workout_source=source, workout_id=workout_id)
    except ValueError:
        return _page("<p>内容不符合要求。返回上一页修改后再提交。</p>", nonce, 422)
    try:
        feedback = SubjectiveFeedbackInput.model_validate(values)
        get_feedback_link_service().submit(token, feedback)
    except FeedbackLinkError as exc:
        return _page(f"<p>{STATE_TEXT[exc.state]}</p>", nonce, 410 if exc.state != "unknown" else 404)
    except (ValidationError, ValueError):
        return _page("<p>没有可记录的内容，或内容不符合要求。返回上一页修改后再提交。</p>", nonce, 422)
    return _page("<p>已记录，谢谢。对应日期的分析会按这条反馈更新。</p>", nonce)
