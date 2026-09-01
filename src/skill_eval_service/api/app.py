"""HTTP API。管理系统通过这里提交评测与读取结果。"""

from __future__ import annotations

from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from skill_eval_service import service
from skill_eval_service.config import get_settings
from skill_eval_service.content import SkillContentSource, SkillNotFoundError, build_content_source
from skill_eval_service.db import get_session_factory, init_db
from skill_eval_service.embedding_shim import router as embedding_shim_router
from skill_eval_service.materialize import MaterializeError, UnsafePathError
from skill_eval_service.models import EvaluationTask
from skill_eval_service.repository import latest_result
from skill_eval_service.runner import preflight
from skill_eval_service.schemas import EvaluationDTO, SubmitRequest, SubmitResponse

@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().ensure_dirs()
    init_db()
    yield


app = FastAPI(
    title="Skill 评测服务",
    description="基于 SkillEvaluator 的 Tier 1 评测编排与结果服务",
    version="0.1.0",
    lifespan=lifespan,
)


# Embedding 批量拆分 shim。Tier 2 的 worker 把 SKILL_EVAL_EMBEDDING_BASE_URL
# 指向 <本服务>/embed/v1，而不是直连方舟。原因见 embedding_shim 模块文档。
app.include_router(embedding_shim_router, prefix="/embed/v1", tags=["embedding-shim"])


def get_db() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_content_source() -> SkillContentSource:
    """内容来源。接入管理系统时替换这个依赖即可，其余代码不动。"""
    return build_content_source(get_settings())


@app.get("/healthz")
def healthz() -> dict:
    """健康检查。同时暴露扫描器状态——缺失意味着结果会是 incomplete。"""
    report = preflight(get_settings())
    return {
        "status": "ok" if report.ok else "degraded",
        "skillevaluator": report.binary,
        "version": report.version,
        "missing_scanners": report.missing_scanners,
    }


@app.post("/api/evaluations", response_model=SubmitResponse)
def submit_evaluation(
    request: SubmitRequest,
    session: Session = Depends(get_db),
    source: SkillContentSource = Depends(get_content_source),
) -> SubmitResponse:
    if request.tier not in service.IMPLEMENTED_TIERS:
        raise HTTPException(status_code=501, detail=f"{request.tier} 尚未实现，当前仅支持 tier1")
    try:
        return service.submit(session, source, request)
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (UnsafePathError, MaterializeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, session: Session = Depends(get_db)) -> dict:
    task = session.get(EvaluationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task.id,
        "skill_id": task.skill_id,
        "content_hash": task.content_hash,
        "tier": task.tier,
        "queue": task.queue,
        "state": task.state,
        "attempts": task.attempts,
        "error": task.error,
    }


@app.get("/api/skills/{skill_id:path}/evaluation", response_model=EvaluationDTO)
def get_evaluation(
    skill_id: str,
    content_hash: str | None = None,
    session: Session = Depends(get_db),
) -> EvaluationDTO:
    dto = service.get_evaluation(session, skill_id, content_hash=content_hash)
    if dto is None:
        raise HTTPException(status_code=404, detail="该 skill 尚无评测结果")
    return dto


@app.get("/api/skills/{skill_id:path}/report")
def get_report(skill_id: str, session: Session = Depends(get_db)) -> FileResponse:
    """回传 SkillEvaluator 生成的 HTML 报告。

    这是自生成 HTML，管理系统应以沙箱化 iframe 或独立页面承载，
    不要内联进自身 DOM。
    """
    row = latest_result(session, skill_id)
    path = service.report_path(row.report_html_uri) if row else None
    if path is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return FileResponse(path, media_type="text/html")
