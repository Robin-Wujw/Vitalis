"""POST /api/v1/analyze —— AI 分析。"""

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from vitalis.api.deps import require_user_id
from vitalis.models import Decision
from vitalis.services import SummaryService

router = APIRouter(prefix="/analyze", tags=["analyze"])


class AnalyzeRequest(BaseModel):
    agent_query: str = Field(default="分析今天的训练建议", description="用户/Agent 的自然语言请求（LLM 路径使用）")
    day: date | None = None


@router.post("", response_model=None, summary="AI 健康分析")
def analyze(req: AnalyzeRequest, user_id: str = Depends(require_user_id)) -> dict:
    """完整分析：规则 + 统计 + LLM 解释（LLM 只解释，不计算）。"""
    payload = SummaryService().analyze(user_id, day=req.day)
    return payload